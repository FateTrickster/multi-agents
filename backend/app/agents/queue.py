from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timezone

from app.agents.agent_mode import (
    AGENT_MODE_SINGLE,
    can_use_agent_role,
    get_room_agent_mode,
)
from app.db.redis_client import get_redis_client

QUEUE_KEY_PREFIX = "agent_queue"
TASK_STATUS_KEY_PREFIX = "agent:task"
TASK_STATUS_TTL_SECONDS = 24 * 60 * 60
MENTION_ENTRY_QUEUE_KEY = "agent:mention_entry_queue"
MENTION_ENTRY_KEY_PREFIX = "agent:mention_entry"
MENTION_ENTRY_STATUS_KEY_PREFIX = "agent:mention_entry_status"
MENTION_ENTRY_TTL_SECONDS = 24 * 60 * 60
RUNNING_TASK_KEY_PREFIX = "agent:running"
FOLLOWUP_TASK_KEY_PREFIX = "agent:followup"
COALESCE_LOCK_KEY_PREFIX = "agent:coalesce-lock"
COALESCE_LOCK_TTL_SECONDS = 3
MAX_COALESCED_SOURCE_MESSAGE_IDS = 50
AGENT_DEBUG_LOG = os.getenv("AGENT_DEBUG_LOG", "").lower() == "true"


def _agent_log(event: str, extra: dict | None = None) -> None:
    if not AGENT_DEBUG_LOG:
        return
    payload = {"event": event, "ts": datetime.now(timezone.utc).isoformat()}
    if extra:
        payload["extra"] = extra
    print("[AGENT-QUEUE-DEBUG]", payload, flush=True)


def queue_key(room_id: str) -> str:
    return f"{QUEUE_KEY_PREFIX}:{room_id}"


def task_status_key(task_id: str) -> str:
    return f"{TASK_STATUS_KEY_PREFIX}:{task_id}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def mention_entry_key(entry_id: str) -> str:
    return f"{MENTION_ENTRY_KEY_PREFIX}:{entry_id}"


def mention_entry_status_key(entry_id: str) -> str:
    return f"{MENTION_ENTRY_STATUS_KEY_PREFIX}:{entry_id}"


def running_task_key(room_id: str, agent_role: str) -> str:
    return f"{RUNNING_TASK_KEY_PREFIX}:{room_id}:{agent_role}"


def followup_task_key(room_id: str, agent_role: str) -> str:
    return f"{FOLLOWUP_TASK_KEY_PREFIX}:{room_id}:{agent_role}"


def coalesce_lock_key(room_id: str, agent_role: str) -> str:
    return f"{COALESCE_LOCK_KEY_PREFIX}:{room_id}:{agent_role}"


def _normalize_task(task: dict) -> dict:
    normalized = dict(task)
    normalized.setdefault("task_id", str(uuid.uuid4()))
    normalized.setdefault("priority", 5)
    normalized.setdefault("triggered_at", time.time())
    normalized.setdefault("room_id", "")
    normalized.setdefault("agent_role", "")
    normalized.setdefault("trigger_type", "manual")
    normalized.setdefault("target_dimension", "none")
    normalized.setdefault("reason", "System requests one collaboration support intervention.")
    normalized.setdefault("strategy", "Provide one concise and actionable help for the current discussion.")
    normalized.setdefault("evidence", [])
    normalized.setdefault("current_phase", "unknown")
    normalized.setdefault("source_message_id", None)
    normalized.setdefault("snapshot_id", None)
    normalized.setdefault("intervention_id", None)
    return normalized


def _validate_task(task: dict) -> None:
    required = [
        "room_id",
        "agent_role",
        "trigger_type",
        "reason",
        "strategy",
        "priority",
        "triggered_at",
    ]
    missing = [key for key in required if task.get(key) in (None, "")]
    if missing:
        raise ValueError(f"agent task missing required fields: {missing}")


def _merge_source_message_ids(task: dict, incoming_source_message_id: str | None) -> list[str]:
    merged_ids: list[str] = []
    existing = task.get("source_message_ids")
    if isinstance(existing, list):
        merged_ids.extend(str(x) for x in existing if x)
    elif isinstance(existing, str) and existing:
        merged_ids.append(existing)

    current_primary = str(task.get("source_message_id") or "")
    if current_primary and not merged_ids:
        merged_ids.append(current_primary)

    incoming = str(incoming_source_message_id or "")
    if incoming and incoming not in merged_ids:
        merged_ids.append(incoming)
    if len(merged_ids) > MAX_COALESCED_SOURCE_MESSAGE_IDS:
        merged_ids = merged_ids[-MAX_COALESCED_SOURCE_MESSAGE_IDS:]
    return merged_ids


async def _read_room_queue_entries(redis_client, room_id: str) -> list[tuple[float, str, dict]]:
    qkey = queue_key(room_id)
    entries_with_scores: list[tuple[str, float]] = []
    try:
        scored = await redis_client.zrange(qkey, 0, -1, withscores=True)
        entries_with_scores = [(str(raw), float(score)) for raw, score in (scored or [])]
    except Exception:
        raw_entries = await redis_client.zrangebyscore(qkey, min=0, max=float("inf"))
        entries_with_scores = [(str(raw), time.time()) for raw in (raw_entries or [])]

    parsed: list[tuple[float, str, dict]] = []
    for raw, score in entries_with_scores:
        try:
            task_data = json.loads(raw)
            parsed.append((float(score), raw, task_data))
        except Exception:
            continue
    return parsed


async def _coalesce_into_existing_task(
    *,
    redis_client,
    room_id: str,
    existing_score: float,
    existing_raw: str,
    existing_task: dict,
    incoming_task: dict,
) -> dict:
    qkey = queue_key(room_id)
    merged_task = dict(existing_task)
    merged_task["merged_count"] = int(existing_task.get("merged_count") or 1) + 1
    merged_task["latest_message_id"] = str(incoming_task.get("source_message_id") or "")
    merged_task["source_message_ids"] = _merge_source_message_ids(
        merged_task,
        incoming_task.get("source_message_id"),
    )
    total_merged = int(merged_task.get("merged_count") or 1)
    retained_count = len(merged_task.get("source_message_ids") or [])
    dropped_count = max(0, total_merged - retained_count)
    merged_task["truncated_source_message_ids"] = dropped_count > 0
    merged_task["dropped_source_message_count"] = dropped_count
    merged_task["triggered_at"] = float(existing_task.get("triggered_at") or incoming_task.get("triggered_at") or time.time())

    await redis_client.zrem(qkey, existing_raw)
    await redis_client.zadd(qkey, {json.dumps(merged_task, ensure_ascii=False, sort_keys=True): existing_score})
    await update_task_status_fields(
        str(merged_task.get("task_id") or ""),
        merged_count=str(merged_task.get("merged_count")),
        latest_message_id=str(merged_task.get("latest_message_id") or ""),
        source_message_ids=json.dumps(merged_task.get("source_message_ids") or [], ensure_ascii=False),
        truncated_source_message_ids=str(bool(merged_task.get("truncated_source_message_ids"))),
        dropped_source_message_count=str(int(merged_task.get("dropped_source_message_count") or 0)),
    )
    _agent_log(
        "single_socratic_task_coalesced",
        {
            "room_id": room_id,
            "task_id": merged_task.get("task_id"),
            "merged_count": merged_task.get("merged_count"),
            "latest_message_id": merged_task.get("latest_message_id"),
        },
    )
    return merged_task


async def enqueue_task(room_id: str, task: dict, delay_seconds: float = 0.0) -> dict | None:
    redis_client = get_redis_client()
    execute_at = time.time() + max(0.0, float(delay_seconds))
    normalized = _normalize_task(task)
    normalized["room_id"] = normalized.get("room_id") or room_id
    agent_mode = await get_room_agent_mode(str(normalized["room_id"]))
    if not can_use_agent_role(agent_mode, str(normalized.get("agent_role") or "")):
        _agent_log(
            "agent_enqueue_skipped_by_mode",
            {
                "task_id": normalized.get("task_id"),
                "room_id": normalized.get("room_id"),
                "agent_role": normalized.get("agent_role"),
                "agent_mode": agent_mode,
            },
        )
        return None

    role = str(normalized.get("agent_role") or "").strip().lower()
    coalesce_owner: str | None = None
    coalesce_key: str | None = None
    if agent_mode == AGENT_MODE_SINGLE and role == "socratic":
        coalesce_key = coalesce_lock_key(room_id, role)
        coalesce_owner = str(uuid.uuid4())
        acquired = await redis_client.set(
            coalesce_key,
            coalesce_owner,
            nx=True,
            ex=COALESCE_LOCK_TTL_SECONDS,
        )
        attempts = 0
        while not acquired and attempts < 20:
            attempts += 1
            await asyncio.sleep(0.01)
            acquired = await redis_client.set(
                coalesce_key,
                coalesce_owner,
                nx=True,
                ex=COALESCE_LOCK_TTL_SECONDS,
            )
        if not acquired:
            _agent_log(
                "single_socratic_coalesce_lock_busy",
                {"room_id": room_id, "agent_role": role, "attempts": attempts},
            )
            # Fail closed for this enqueue to avoid duplicate task creation under contention.
            return None

        try:
            queue_entries = await _read_room_queue_entries(redis_client, room_id)
            queued_socratic = [
                (score, raw, queued_task)
                for score, raw, queued_task in queue_entries
                if str(queued_task.get("agent_role") or "").strip().lower() == "socratic"
            ]
            if queued_socratic:
                queued_socratic.sort(key=lambda item: float(item[2].get("triggered_at") or 0))
                score, raw, queued_task = queued_socratic[0]
                return await _coalesce_into_existing_task(
                    redis_client=redis_client,
                    room_id=room_id,
                    existing_score=score,
                    existing_raw=raw,
                    existing_task=queued_task,
                    incoming_task=normalized,
                )

            running_key = running_task_key(room_id, "socratic")
            followup_key = followup_task_key(room_id, "socratic")
            running_task_id = await redis_client.get(running_key)
            if running_task_id:
                followup_task_id = await redis_client.get(followup_key)
                if followup_task_id:
                    for score, raw, queued_task in queue_entries:
                        if str(queued_task.get("task_id") or "") == str(followup_task_id):
                            return await _coalesce_into_existing_task(
                                redis_client=redis_client,
                                room_id=room_id,
                                existing_score=score,
                                existing_raw=raw,
                                existing_task=queued_task,
                                incoming_task=normalized,
                            )
                    await redis_client.delete(followup_key)
        finally:
            if coalesce_key and coalesce_owner:
                current_owner = await redis_client.get(coalesce_key)
                if str(current_owner or "") == coalesce_owner:
                    await redis_client.delete(coalesce_key)

    _validate_task(normalized)
    qkey = queue_key(room_id)
    _agent_log(
        "agent_enqueue_start",
        {
            "task_id": normalized.get("task_id"),
            "room_id": room_id,
            "agent_role": normalized.get("agent_role"),
            "trigger_type": normalized.get("trigger_type"),
            "source_message_id": normalized.get("source_message_id"),
            "queue_key": qkey,
            "priority": normalized.get("priority"),
            "created_at": normalized.get("triggered_at"),
            "execute_at": execute_at,
        },
    )
    task_json = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
    await redis_client.zadd(qkey, {task_json: execute_at})
    if agent_mode == AGENT_MODE_SINGLE and role == "socratic":
        running_key = running_task_key(room_id, "socratic")
        if await redis_client.get(running_key):
            await redis_client.setex(
                followup_task_key(room_id, "socratic"),
                TASK_STATUS_TTL_SECONDS,
                str(normalized.get("task_id") or ""),
            )
    await set_task_status(
        task_id=str(normalized.get("task_id")),
        room_id=room_id,
        agent_role=str(normalized.get("agent_role") or ""),
        trigger_type=str(normalized.get("trigger_type") or "manual"),
        status="queued",
        reason=str(normalized.get("reason") or ""),
        source_message_id=normalized.get("source_message_id"),
        queued_at=now_iso(),
        created_at=str(normalized.get("created_at") or normalized.get("triggered_at") or now_iso()),
    )
    # For auto triggers (silence/committee/time_progress/...) we also emit queued events,
    # so clients can observe a full lifecycle before stream events arrive.
    trigger_type = str(normalized.get("trigger_type") or "manual").strip().lower()
    if trigger_type != "mention":
        queued_payload = {
            "type": "agent:queued",
            "task_id": str(normalized.get("task_id") or ""),
            "room_id": room_id,
            "agent_role": str(normalized.get("agent_role") or ""),
            "trigger_type": trigger_type,
            "source_message_id": normalized.get("source_message_id"),
            "reason": str(normalized.get("reason") or ""),
            "status": "queued",
        }
        try:
            await redis_client.publish(f"room:{room_id}", json.dumps(queued_payload, ensure_ascii=False))
        except Exception as exc:
            _agent_log(
                "agent_queued_broadcast_failed",
                {
                    "task_id": normalized.get("task_id"),
                    "room_id": room_id,
                    "agent_role": normalized.get("agent_role"),
                    "trigger_type": trigger_type,
                    "exception_class": type(exc).__name__,
                    "exception_message": str(exc),
                },
            )
    qlen = int(await redis_client.zcard(qkey))
    _agent_log(
        "agent_enqueue_done",
        {
            "task_id": normalized.get("task_id"),
            "room_id": room_id,
            "queue_key": qkey,
            "queue_length": qlen,
        },
    )
    return normalized




async def dequeue_tasks(room_id: str) -> list[dict]:
    redis_client = get_redis_client()
    qkey = queue_key(room_id)
    now = time.time()
    raw_tasks = await redis_client.zrangebyscore(qkey, min=0, max=now)
    if not raw_tasks:
        _agent_log(
            "agent_worker_task_none",
            {"room_id": room_id, "queue_key": qkey, "queue_length": int(await redis_client.zcard(qkey))},
        )
        return []

    await redis_client.zrem(qkey, *raw_tasks)

    parsed: list[dict] = []
    for raw in raw_tasks:
        try:
            parsed.append(json.loads(raw))
        except Exception:
            continue

    parsed.sort(
        key=lambda t: (
            int(t.get("priority", 5)),
            float(t.get("triggered_at", 0)),
            str(t.get("task_id", "")),
        )
    )
    _agent_log(
        "agent_worker_task_popped",
        {
            "room_id": room_id,
            "queue_key": qkey,
            "popped_count": len(parsed),
            "remaining_queue_length": int(await redis_client.zcard(qkey)),
            "task_ids": [str(t.get("task_id")) for t in parsed],
        },
    )
    return parsed


async def requeue_task(room_id: str, task: dict, delay_seconds: float = 5.0) -> dict:
    return await enqueue_task(room_id, task, delay_seconds=delay_seconds)


async def set_task_status(
    *,
    task_id: str,
    room_id: str,
    agent_role: str,
    trigger_type: str,
    status: str,
    reason: str = "",
    source_message_id: str | None = None,
    running_at: str | None = None,
    finished_at: str | None = None,
    error: str | None = None,
    drop_reason: str | None = None,
    created_at: str | None = None,
    queued_at: str | None = None,
) -> None:
    if not task_id:
        return
    redis_client = get_redis_client()
    key = task_status_key(task_id)
    payload = {
        "task_id": task_id,
        "room_id": room_id,
        "agent_role": agent_role,
        "trigger_type": trigger_type,
        "status": status,
        "reason": reason or "",
        "source_message_id": source_message_id or "",
        # Preserve prior phase timestamps and errors across later status transitions.
        **({"running_at": running_at} if running_at is not None else {}),
        **({"finished_at": finished_at} if finished_at is not None else {}),
        **({"error": error} if error is not None else {}),
        **({"drop_reason": drop_reason} if drop_reason is not None else {}),
    }
    if created_at is not None:
        payload["created_at"] = created_at
    if queued_at is not None:
        payload["queued_at"] = queued_at
    await redis_client.hset(key, mapping=payload)
    await redis_client.rpush(
        f"agent:task_events:{room_id}",
        json.dumps({"at": now_iso(), **payload}, ensure_ascii=False),
    )
    # Task audit records are research evidence and must not expire.


async def update_task_status_fields(task_id: str, **fields: str) -> None:
    if not task_id or not fields:
        return
    redis_client = get_redis_client()
    key = task_status_key(task_id)
    await redis_client.hset(key, mapping={k: str(v) for k, v in fields.items()})
    old = await redis_client.hgetall(key)
    if old.get("room_id"):
        await redis_client.rpush(
            f"agent:task_events:{old['room_id']}",
            json.dumps({"at": now_iso(), "task_id": task_id, **fields}, ensure_ascii=False),
        )
    # Preserve subsequent status updates for longitudinal export.


async def get_task_status(task_id: str) -> dict[str, str]:
    if not task_id:
        return {}
    redis_client = get_redis_client()
    key = task_status_key(task_id)
    data = await redis_client.hgetall(key)
    return {str(k): str(v) for k, v in (data or {}).items()}


async def create_mention_entry(
    *,
    room_id: str,
    agent_role: str,
    source_message_id: str,
    student_name: str,
    reason: str,
    strategy: str,
    trigger_type: str = "mention",
    entry_id: str | None = None,
    created_at: float | None = None,
    expire_at: float | None = None,
) -> dict[str, str]:
    redis_client = get_redis_client()
    now_ts = float(created_at if created_at is not None else time.time())
    expiry_ts = float(expire_at if expire_at is not None else now_ts + MENTION_ENTRY_TTL_SECONDS)
    eid = str(entry_id or uuid.uuid4())
    entry = {
        "entry_id": eid,
        "room_id": room_id,
        "agent_role": agent_role,
        "source_message_id": source_message_id,
        "student_name": student_name,
        "reason": reason,
        "strategy": strategy,
        "trigger_type": trigger_type or "mention",
        "created_at": str(now_ts),
        "expire_at": str(expiry_ts),
    }
    ekey = mention_entry_key(eid)
    skey = mention_entry_status_key(eid)
    await redis_client.hset(ekey, mapping=entry)
    # Keep mention request/status history for research export.
    await redis_client.hset(
        skey,
        mapping={
            "entry_id": eid,
            "status": "queued",
            "reason": "",
            "task_id": "",
            "error": "",
            "updated_at": now_iso(),
        },
    )
    # Keep mention request/status history for research export.
    await redis_client.zadd(MENTION_ENTRY_QUEUE_KEY, {eid: now_ts})
    return entry


async def get_mention_entry(entry_id: str) -> dict[str, str]:
    if not entry_id:
        return {}
    redis_client = get_redis_client()
    data = await redis_client.hgetall(mention_entry_key(entry_id))
    return {str(k): str(v) for k, v in (data or {}).items()}


async def pop_due_mention_entries(limit: int) -> list[dict[str, str]]:
    safe_limit = max(0, int(limit))
    if safe_limit <= 0:
        return []
    redis_client = get_redis_client()
    now_ts = time.time()
    # NOTE: Step-1 assumes single consumer; atomic multi-consumer pop can be added later via Lua.
    entry_ids = await redis_client.zrangebyscore(
        MENTION_ENTRY_QUEUE_KEY,
        min=0,
        max=now_ts,
        start=0,
        num=safe_limit,
    )
    if not entry_ids:
        return []
    await redis_client.zrem(MENTION_ENTRY_QUEUE_KEY, *entry_ids)
    entries: list[dict[str, str]] = []
    for entry_id in entry_ids:
        entry = await get_mention_entry(str(entry_id))
        if entry:
            entries.append(entry)
    return entries


async def mark_mention_entry_status(
    entry_id: str,
    status: str,
    reason: str | None = None,
    task_id: str | None = None,
    error: str | None = None,
) -> None:
    if not entry_id:
        return
    redis_client = get_redis_client()
    payload = {
        "entry_id": entry_id,
        "status": status,
        "reason": reason or "",
        "task_id": task_id or "",
        "error": error or "",
        "updated_at": now_iso(),
    }
    skey = mention_entry_status_key(entry_id)
    await redis_client.hset(skey, mapping=payload)
    # Keep mention request/status history for research export.


async def remove_mention_entry_from_queue(entry_id: str) -> None:
    if not entry_id:
        return
    redis_client = get_redis_client()
    await redis_client.zrem(MENTION_ENTRY_QUEUE_KEY, entry_id)


async def get_mention_entry_queue_length() -> int:
    redis_client = get_redis_client()
    return int(await redis_client.zcard(MENTION_ENTRY_QUEUE_KEY))
