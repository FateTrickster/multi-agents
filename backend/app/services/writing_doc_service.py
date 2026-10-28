from __future__ import annotations

import json
from datetime import datetime, timezone

from app.db.redis_client import get_redis_client

CHANGE_LOG_MAX_LEN = 100


def _state_key(room_id: str) -> str:
    return f"room:{room_id}:writing_doc_state"


def _version_key(room_id: str) -> str:
    return f"room:{room_id}:writing_doc_version"


def _change_log_key(room_id: str) -> str:
    return f"room:{room_id}:writing_doc_change_log"


def _empty_state() -> dict:
    return {
        "content": "",
        "version": 0,
        "updated_at": None,
        "updated_by": None,
        "updated_by_display_name": None,
    }


async def get_writing_doc_state(room_id: str) -> dict:
    redis_client = get_redis_client()
    raw = await redis_client.get(_state_key(room_id))
    if not raw:
        return _empty_state()
    try:
        parsed = json.loads(raw)
    except Exception:
        return _empty_state()
    if not isinstance(parsed, dict):
        return _empty_state()
    return {
        "content": str(parsed.get("content") or ""),
        "version": int(parsed.get("version") or 0),
        "updated_at": parsed.get("updated_at"),
        "updated_by": parsed.get("updated_by"),
        "updated_by_display_name": parsed.get("updated_by_display_name"),
    }


async def _append_change_log(room_id: str, item: dict) -> None:
    redis_client = get_redis_client()
    await redis_client.lpush(_change_log_key(room_id), json.dumps(item, ensure_ascii=False))
    # Keep the complete research event history; never trim past edits.


def _build_change_summary(before_content: str, after_content: str, action: str) -> tuple[str, int]:
    before_len = len(before_content.strip())
    after_len = len(after_content.strip())
    delta = after_len - before_len
    if action == "save_checkpoint":
        return f"保存检查点（{after_len}字）", 0
    if delta > 0:
        return f"新增约{delta}字", delta
    if delta < 0:
        return f"删除约{abs(delta)}字", delta
    return "格式或内容调整", 0


async def append_writing_doc_change_log(
    room_id: str,
    action: str,
    actor_id: str,
    actor_display_name: str | None,
    version: int,
    before_content: str,
    after_content: str,
) -> dict:
    summary, delta_chars = _build_change_summary(before_content, after_content, action)
    item = {
        "action": action,
        "at": datetime.now(timezone.utc).isoformat(),
        "actor_id": str(actor_id),
        "actor_display_name": actor_display_name or None,
        "version": int(version),
        "summary": summary,
        "delta_chars": int(delta_chars),
    }
    await _append_change_log(room_id, item)
    return item


async def get_writing_doc_change_log(room_id: str, limit: int = 30) -> list[dict]:
    redis_client = get_redis_client()
    raw_items = await redis_client.lrange(_change_log_key(room_id), 0, max(0, limit - 1))
    results: list[dict] = []
    for raw in raw_items:
        try:
            parsed = json.loads(raw)
        except Exception:
            continue
        if not isinstance(parsed, dict):
            continue
        results.append(
            {
                "action": str(parsed.get("action") or "update"),
                "at": parsed.get("at"),
                "actor_id": parsed.get("actor_id"),
                "actor_display_name": parsed.get("actor_display_name"),
                "version": int(parsed.get("version") or 0),
                "summary": str(parsed.get("summary") or ""),
                "delta_chars": int(parsed.get("delta_chars") or 0),
            }
        )
    return results


async def apply_writing_doc_update(
    room_id: str,
    content: str,
    updated_by: str,
    updated_by_display_name: str | None = None,
) -> dict:
    redis_client = get_redis_client()
    version = int(await redis_client.incr(_version_key(room_id)))
    state = {
        "content": str(content or ""),
        "version": version,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": str(updated_by),
        "updated_by_display_name": (updated_by_display_name or None),
    }
    await redis_client.set(_state_key(room_id), json.dumps(state, ensure_ascii=False))
    return state


async def apply_writing_doc_update_with_base_version(
    room_id: str,
    content: str,
    updated_by: str,
    updated_by_display_name: str | None = None,
    base_version: int | None = None,
) -> tuple[dict, bool]:
    """
    Returns (state, applied):
      - applied=True: update accepted and persisted
      - applied=False: client is stale; return current authoritative state
    """
    current_state = await get_writing_doc_state(room_id)
    before_content = str(current_state.get("content") or "")
    current_version = int(current_state.get("version") or 0)
    if base_version is not None and int(base_version) < current_version:
        return current_state, False

    next_state = await apply_writing_doc_update(
        room_id,
        content,
        updated_by,
        updated_by_display_name=updated_by_display_name,
    )
    await append_writing_doc_change_log(
        room_id=room_id,
        action="update",
        actor_id=updated_by,
        actor_display_name=updated_by_display_name,
        version=int(next_state.get("version") or 0),
        before_content=before_content,
        after_content=str(next_state.get("content") or ""),
    )
    return next_state, True


async def save_writing_doc_version(
    room_id: str,
    saved_by: str,
    saved_by_display_name: str | None = None,
) -> dict:
    state = await get_writing_doc_state(room_id)
    if int(state.get("version") or 0) <= 0:
        raise ValueError("writing doc is empty")

    return await append_writing_doc_change_log(
        room_id=room_id,
        action="save_checkpoint",
        actor_id=str(saved_by),
        actor_display_name=saved_by_display_name,
        version=int(state.get("version") or 0),
        before_content=str(state.get("content") or ""),
        after_content=str(state.get("content") or ""),
    )
