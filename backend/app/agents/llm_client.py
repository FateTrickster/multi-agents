from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from urllib.parse import urlsplit, urlunsplit

import anthropic

from app.agents.model_pool import MODEL_CANDIDATES
from app.config import settings

try:
    from openai import (
        APIConnectionError,
        APITimeoutError,
        APIStatusError,
        AsyncOpenAI,
        InternalServerError,
        RateLimitError,
    )
except Exception:
    AsyncOpenAI = None
    APIConnectionError = Exception
    APITimeoutError = Exception
    APIStatusError = Exception
    InternalServerError = Exception
    RateLimitError = Exception

_anthropic_client: anthropic.AsyncAnthropic | None = None
_openai_client: AsyncOpenAI | None = None

_MODEL_COOLDOWN_SECONDS = 180
_SWITCH_LOG_LIMIT = 30

_model_health: dict[str, dict] = {}
_active_model: str | None = None
_switch_events: list[dict] = []
_model_lock = asyncio.Lock()


def _provider() -> str:
    return (settings.LLM_PROVIDER or "openai_compatible").strip().lower()


def get_model_candidates() -> list[str]:
    result = []
    seen = set()
    for model in MODEL_CANDIDATES:
        normalized = str(model or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _ensure_model_health() -> None:
    for model in get_model_candidates():
        if model not in _model_health:
            _model_health[model] = {
                "cooldown_until": 0.0,
                "last_error": None,
                "last_checked": None,
                "last_ok": None,
                "failures": 0,
            }


def _append_switch_event(event_type: str, model: str, reason: str) -> None:
    _switch_events.append(
        {
            "ts": time.time(),
            "type": event_type,
            "model": model,
            "reason": reason,
        }
    )
    if len(_switch_events) > _SWITCH_LOG_LIMIT:
        del _switch_events[0 : len(_switch_events) - _SWITCH_LOG_LIMIT]


def normalize_openai_base_url(base_url: str) -> str:
    """
    Normalize OpenAI-compatible base URLs for common relay misconfigurations.

    Fixes:
    - "https://host" -> "https://host/v1"
    - ".../v1/chat/completions" -> ".../v1"
    """
    raw = (base_url or "").strip()
    if not raw:
        return raw

    parts = urlsplit(raw)
    path = (parts.path or "").rstrip("/")

    if path.endswith("/chat/completions"):
        path = path[: -len("/chat/completions")]

    if path in {"", "/"}:
        path = "/v1"

    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def get_openai_client() -> AsyncOpenAI:
    global _openai_client
    if AsyncOpenAI is None:
        raise RuntimeError("openai package is not installed. Please install it in backend dependencies.")

    if _openai_client is None:
        # Compatibility strategy:
        # - If OPENAI_API_KEY is provided, use OPENAI_BASE_URL (or official default).
        # - Otherwise fall back to AI_API_KEY + AI_BASE_URL for relay providers.
        if settings.OPENAI_API_KEY:
            api_key = settings.OPENAI_API_KEY
            base_url = settings.OPENAI_BASE_URL or "https://api.openai.com/v1"
        else:
            api_key = settings.AI_API_KEY
            base_url = settings.AI_BASE_URL

        if not api_key:
            raise RuntimeError("OPENAI_API_KEY (or AI_API_KEY) is required for OpenAI-compatible calls.")
        if not base_url:
            raise RuntimeError("OPENAI_BASE_URL (or AI_BASE_URL) is required for OpenAI-compatible calls.")

        normalized_base_url = normalize_openai_base_url(base_url)
        _openai_client = AsyncOpenAI(api_key=api_key, base_url=normalized_base_url)
    return _openai_client


def get_claude_client() -> anthropic.AsyncAnthropic:
    global _anthropic_client
    if _anthropic_client is None:
        api_key = settings.ANTHROPIC_API_KEY or settings.AI_API_KEY or settings.OPENAI_API_KEY
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY (or AI_API_KEY/OPENAI_API_KEY) is required for Anthropic calls.")
        _anthropic_client = anthropic.AsyncAnthropic(
            api_key=api_key,
            base_url=settings.AI_BASE_URL,
        )
    return _anthropic_client


async def _probe_model_once(model: str) -> None:
    client = get_openai_client()
    response = await client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "Reply with one short word."}],
        max_tokens=8,
        stream=False,
    )
    if not response or not getattr(response, "choices", None):
        raise RuntimeError("Model probe returned no choices.")


async def _mark_success(model: str, reason: str) -> None:
    global _active_model
    now = time.time()
    async with _model_lock:
        _ensure_model_health()
        state = _model_health[model]
        state["last_checked"] = now
        state["last_ok"] = now
        state["last_error"] = None
        state["failures"] = 0
        state["cooldown_until"] = 0.0

        if _active_model != model:
            _active_model = model
            _append_switch_event("switch", model, reason)


async def _mark_failure(model: str, exc: Exception, cooldown: bool) -> None:
    global _active_model
    now = time.time()
    async with _model_lock:
        _ensure_model_health()
        state = _model_health[model]
        state["last_checked"] = now
        state["last_error"] = str(exc)
        state["failures"] += 1
        if cooldown:
            state["cooldown_until"] = now + _MODEL_COOLDOWN_SECONDS
        if _active_model == model and cooldown:
            _active_model = None


def _is_retryable_error(exc: Exception) -> bool:
    if isinstance(exc, (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError)):
        return True

    if isinstance(exc, APIStatusError):
        status = getattr(exc, "status_code", None)
        return status in {429, 500, 502, 503, 504}

    msg = str(exc).lower()
    markers = [
        "connection error",
        "timeout",
        "timed out",
        "503",
        "429",
        "502",
        "504",
        "connection reset",
        "connection aborted",
        "基础连接已经关闭",
    ]
    return any(m in msg for m in markers)


async def _ordered_candidates(preferred_model: str | None) -> list[str]:
    async with _model_lock:
        _ensure_model_health()
        pool = get_model_candidates()
        if not pool:
            return []

        order: list[str] = []
        if _active_model in pool:
            order.append(_active_model)

        preferred = (preferred_model or "").strip()
        if preferred in pool and preferred not in order:
            order.append(preferred)

        for model in pool:
            if model not in order:
                order.append(model)

        now = time.time()
        available = [m for m in order if _model_health[m]["cooldown_until"] <= now]
        return available or order


async def initialize_model_routing() -> str | None:
    if _provider() not in {"openai", "openai_compatible", "openai-compatible"}:
        return None

    pool = get_model_candidates()
    if not pool:
        return None

    for model in pool:
        try:
            await _probe_model_once(model)
            await _mark_success(model, "startup_probe")
            return model
        except Exception as exc:
            await _mark_failure(model, exc, cooldown=True)
    return None


async def refresh_model_routing() -> str | None:
    if _provider() not in {"openai", "openai_compatible", "openai-compatible"}:
        return None

    pool = get_model_candidates()
    if not pool:
        return None

    now = time.time()
    best: str | None = None

    async with _model_lock:
        _ensure_model_health()
        candidates = [m for m in pool if _model_health[m]["cooldown_until"] <= now]

    for model in candidates:
        try:
            await _probe_model_once(model)
            best = model
            break
        except Exception as exc:
            await _mark_failure(model, exc, cooldown=True)

    if best:
        await _mark_success(best, "periodic_refresh")
    return best


async def get_model_routing_status() -> dict:
    async with _model_lock:
        _ensure_model_health()
        now = time.time()
        models = []
        for model in get_model_candidates():
            state = _model_health[model]
            cooldown_until = float(state["cooldown_until"] or 0.0)
            models.append(
                {
                    "model": model,
                    "is_active": model == _active_model,
                    "last_error": state["last_error"],
                    "failures": state["failures"],
                    "last_checked": state["last_checked"],
                    "last_ok": state["last_ok"],
                    "cooldown_until": cooldown_until,
                    "cooldown_remaining": max(0, int(cooldown_until - now)),
                }
            )

        return {
            "provider": _provider(),
            "active_model": _active_model,
            "pool": get_model_candidates(),
            "models": models,
            "recent_switch_events": list(_switch_events),
        }


async def _stream_openai(
    system_prompt: str,
    messages: list[dict],
    model: str,
    max_tokens: int,
) -> AsyncIterator[str]:
    client = get_openai_client()

    req_messages = [{"role": "system", "content": system_prompt}, *messages]
    stream = await client.chat.completions.create(
        model=model,
        messages=req_messages,
        max_tokens=max_tokens,
        stream=True,
        **({'extra_body': {'thinking': {'type': 'disabled'}}} if model == 'deepseek-flash' else {}),
    )

    async for chunk in stream:
        delta = chunk.choices[0].delta.content if chunk.choices else None
        if delta:
            yield delta


async def _stream_anthropic(
    system_prompt: str,
    messages: list[dict],
    model: str,
    max_tokens: int,
) -> AsyncIterator[str]:
    client = get_claude_client()
    async with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=messages,
    ) as stream:
        async for text in stream.text_stream:
            yield text


async def stream_completion(
    system_prompt: str,
    messages: list[dict],
    model: str,
    max_tokens: int = 1024,
):
    provider = _provider()

    if provider in {"openai", "openai_compatible", "openai-compatible"}:
        candidates = await _ordered_candidates(model)
        if not candidates:
            raise RuntimeError("No models configured in fixed model pool.")

        last_error: Exception | None = None
        for candidate in candidates:
            produced_token = False
            try:
                async for token in _stream_openai(system_prompt, messages, candidate, max_tokens):
                    produced_token = True
                    yield token

                if not produced_token:
                    raise RuntimeError("Connection error.")

                await _mark_success(candidate, "request_success")
                return
            except Exception as exc:
                last_error = exc
                retryable = _is_retryable_error(exc)
                await _mark_failure(candidate, exc, cooldown=retryable)
                if not retryable:
                    raise
                continue

        if last_error is not None:
            raise last_error
        raise RuntimeError("No available model in fixed model pool.")

    async for token in _stream_anthropic(system_prompt, messages, model, max_tokens):
        yield token
