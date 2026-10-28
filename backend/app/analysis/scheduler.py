from __future__ import annotations

import json
import hashlib
import time
from datetime import datetime, timezone
from uuid import UUID

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app.agents.agent_mode import get_room_agent_mode, should_run_auto_dispatcher
from app.agents.committee import basic_committee
from app.agents.llm_client import refresh_model_routing
from app.agents.agent_messages import (
    SILENCE_REASON_TEMPLATE,
    SILENCE_STRATEGY,
    TIME_PROGRESS_PHASE_TEXT,
    TIME_PROGRESS_PROGRESS_TEXT,
    TIME_PROGRESS_REASON_TEMPLATE,
    TIME_PROGRESS_STRATEGY_NORMAL,
    TIME_PROGRESS_STRATEGY_SLOW,
)
from app.agents.queue import enqueue_task
from app.agents.settings import get_agent_settings
from app.analysis.timer_phase import get_elapsed_seconds_from_timer_start
from app.db.redis_client import cleanup_stale_online_presence, get_redis_client
from app.db.session import AsyncSessionLocal
from app.models.message import Message, SenderType
from app.models.room import Room
from app.models.task import Task
from app.services import writing_submit_service
from app.services.writing_snapshot_service import snapshot_active_writing_docs

scheduler = AsyncIOScheduler()
TIME_NODE_MINUTES = (20, 45, 70, 95, 115)
TIME_NODE_LOCK_TTL_SECONDS = 4 * 3600
SILENCE_EPISODE_MARKER_TTL_SECONDS = 4 * 3600
TIME_PROGRESS_SCHEDULE_TTL_SECONDS = 30 * 60
EMOTIONAL_SUPPORT_LOCK_TTL_SECONDS = 60


def _decode_room_id(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return str(value or "").strip()


def _normalize_script_value(raw_scripts) -> dict:
    if not isinstance(raw_scripts, dict):
        return {"current_status": "", "next_goal": "", "history": [], "pending_proposal": None}
    history = raw_scripts.get("history")
    pending = raw_scripts.get("pending_proposal")
    return {
        "current_status": str(raw_scripts.get("current_status") or "").strip(),
        "next_goal": str(raw_scripts.get("next_goal") or "").strip(),
        "history": history if isinstance(history, list) else [],
        "pending_proposal": pending if isinstance(pending, dict) else None,
    }


def _is_meaningful_text(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    return normalized not in {"none", "null", "n/a", "-", "?", "??", "???"}


def _phase_label(elapsed_minutes: int) -> str:
    if elapsed_minutes < 40:
        return "early"
    if elapsed_minutes < 90:
        return "middle"
    return "late"


def _scheduler_log(event: str, extra: dict | None = None) -> None:
    payload = {"event": event}
    if extra:
        payload.update(extra)
    print(f"[Scheduler] {json.dumps(payload, ensure_ascii=False)}")


def _stable_time_progress_jitter_seconds(
    *,
    room_id: str,
    node_minutes: int,
    min_seconds: int,
    max_seconds: int,
) -> int:
    low = int(min_seconds)
    high = int(max_seconds)
    if high < low:
        low, high = high, low
    if low == high:
        return low
    raw = f"{room_id}:{node_minutes}".encode("utf-8")
    digest = hashlib.sha1(raw).hexdigest()
    bucket = int(digest[:8], 16)
    return low + (bucket % (high - low + 1))


def _assess_progress(
    *,
    phase: str,
    script_state: dict,
    submit_confirm_count: int,
    has_recent_activity: bool,
) -> tuple[str, str]:
    history_count = len(script_state.get("history") or [])
    has_pending = bool(script_state.get("pending_proposal"))
    has_current_status = _is_meaningful_text(script_state.get("current_status") or "")
    has_next_goal = _is_meaningful_text(script_state.get("next_goal") or "")

    if phase == "early":
        is_normal = has_next_goal or has_pending or history_count >= 1
    elif phase == "middle":
        is_normal = (history_count >= 1 or has_pending) and has_current_status and has_next_goal and has_recent_activity
    else:
        is_normal = submit_confirm_count >= 1 or ((history_count >= 2 or has_pending) and has_next_goal and has_recent_activity)

    status = "normal" if is_normal else "slow"
    details = (
        f"phase={phase}, history_count={history_count}, has_pending={has_pending}, "
        f"has_current_status={has_current_status}, has_next_goal={has_next_goal}, "
        f"submit_confirm_count={submit_confirm_count}, recent_activity={has_recent_activity}"
    )
    return status, details


def _build_time_nudge_text(
    *,
    elapsed_minutes: int,
    node_minutes: int,
    phase: str,
    progress_status: str,
    progress_details: str,
) -> tuple[str, str]:
    phase_text = TIME_PROGRESS_PHASE_TEXT.get(phase, "未知阶段")
    progress_text = TIME_PROGRESS_PROGRESS_TEXT.get(progress_status, "状态未知")
    reason = TIME_PROGRESS_REASON_TEMPLATE.format(
        elapsed_minutes=elapsed_minutes,
        node_minutes=node_minutes,
        phase_text=phase_text,
        progress_text=progress_text,
    )
    if progress_status == "slow":
        strategy = TIME_PROGRESS_STRATEGY_SLOW.format(progress_details=progress_details)
    else:
        strategy = TIME_PROGRESS_STRATEGY_NORMAL.format(progress_details=progress_details)
    return reason, strategy


def _match_emotional_support_keyword(content: str, keywords: list[str]) -> str | None:
    text = str(content or "")
    for kw in keywords:
        if kw and kw in text:
            return kw
    return None


async def _load_room_snapshot(room_id: str) -> dict | None:
    try:
        parsed_room_id = UUID(room_id)
    except (ValueError, TypeError):
        return None

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(
                Room.timer_started_at,
                Room.timer_stopped_at,
                Task.scripts.label("task_scripts"),
            )
            .outerjoin(Task, Task.id == Room.task_id)
            .where(Room.id == parsed_room_id)
        )
        row = result.first()

    if row is None:
        return None

    timer_started_at = row.timer_started_at
    timer_stopped_at = row.timer_stopped_at
    if timer_started_at is None or timer_stopped_at is not None:
        return None
    if timer_started_at.tzinfo is None:
        timer_started_at = timer_started_at.replace(tzinfo=timezone.utc)

    return {
        "timer_started_at": timer_started_at,
        "script_state": _normalize_script_value(row.task_scripts),
    }


async def _load_recent_student_messages(room_id: str, window_seconds: int) -> list[dict]:
    try:
        parsed_room_id = UUID(room_id)
    except (ValueError, TypeError):
        return []
    cutoff_ts = time.time() - max(1, int(window_seconds))
    cutoff = datetime.fromtimestamp(cutoff_ts, tz=timezone.utc)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Message.id, Message.sender_id, Message.content, Message.created_at)
            .where(Message.room_id == parsed_room_id)
            .where(Message.sender_type == SenderType.student)
            .where(Message.created_at >= cutoff)
            .order_by(Message.created_at.desc())
            .limit(30)
        )
        rows = result.all()

    out = []
    for row in rows:
        created_at = row.created_at
        if created_at is None:
            continue
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        out.append(
            {
                "id": str(row.id),
                "user_id": str(row.sender_id) if row.sender_id else "",
                "content": str(row.content or ""),
                "created_at": created_at.timestamp(),
            }
        )
    return out


async def _has_recent_encourager_reply(room_id: str, window_seconds: int) -> bool:
    try:
        parsed_room_id = UUID(room_id)
    except (ValueError, TypeError):
        return False
    cutoff_ts = time.time() - max(1, int(window_seconds))
    cutoff = datetime.fromtimestamp(cutoff_ts, tz=timezone.utc)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Message.id)
            .where(Message.room_id == parsed_room_id)
            .where(Message.sender_type == SenderType.agent)
            .where(Message.agent_role == "encourager")
            .where(Message.created_at >= cutoff)
            .limit(1)
        )
        return result.first() is not None


async def check_silence() -> None:
    cfg = get_agent_settings()
    auto_speak = getattr(cfg, "auto_speak", None)
    facilitator_silence_enabled = True if auto_speak is None else getattr(auto_speak, "facilitator_silence_enabled", True)
    if not cfg.timing.silence_trigger_enabled or not facilitator_silence_enabled:
        return

    redis_client = get_redis_client()
    active_rooms = await redis_client.smembers("active_rooms")

    now = time.time()
    warmup_seconds = cfg.timing.warmup_minutes * 60
    lock_ttl = cfg.timing.silence_threshold_seconds + 60
    base_rule_marker_ttl = max(30, int(getattr(cfg.timing, "rule_trigger_marker_ttl_seconds", 180)))
    room_auto_cooldown = int(getattr(cfg.timing, "room_auto_intervention_cooldown_seconds", 180))
    silence_rule_ttl = max(
        base_rule_marker_ttl,
        int(cfg.timing.silence_threshold_seconds) + 180,
        room_auto_cooldown + 60,
    )

    for room_id in active_rooms:
        room_mode = await get_room_agent_mode(room_id)
        if not should_run_auto_dispatcher(room_mode):
            continue

        last_msg_time = await redis_client.get(f"room:{room_id}:last_msg_time")
        last_activity_time = await redis_client.get(f"room:{room_id}:last_activity_time")
        episode_anchor = last_msg_time or last_activity_time
        anchor_source = "last_msg_time" if last_msg_time else "last_activity_time"
        if not episode_anchor:
            continue

        elapsed_seconds = await get_elapsed_seconds_from_timer_start(room_id)
        if elapsed_seconds is None:
            # Timer has not started yet; time-based auto triggers are disabled.
            continue
        if elapsed_seconds < warmup_seconds:
            continue

        silence = now - float(episode_anchor)
        if silence < cfg.timing.silence_threshold_seconds:
            continue

        recent_rule_key = f"recent_rule_trigger:{room_id}:silence"
        if await redis_client.exists(recent_rule_key):
            _scheduler_log(
                "silence_skip_recent_rule_trigger",
                {"room_id": room_id, "ttl": silence_rule_ttl, "silence_seconds": int(silence)},
            )
            continue

        lock_key = f"trigger_lock:{room_id}:silence"
        if await redis_client.exists(lock_key):
            continue

        episode_marker_key = f"silence_triggered_activity:{room_id}"
        episode_marker_val = await redis_client.get(episode_marker_key)
        if episode_marker_val and str(episode_marker_val) == str(episode_anchor):
            _scheduler_log(
                "silence_skip_same_episode",
                {
                    "room_id": room_id,
                    "episode_anchor": str(episode_anchor),
                    "anchor_source": anchor_source,
                    "marker_value": str(episode_marker_val),
                    "silence_seconds": int(silence),
                },
            )
            continue

        await redis_client.setex(lock_key, lock_ttl, "1")
        await redis_client.setex(recent_rule_key, silence_rule_ttl, "1")
        await redis_client.setex(
            episode_marker_key,
            SILENCE_EPISODE_MARKER_TTL_SECONDS,
            str(episode_anchor),
        )
        _scheduler_log(
            "silence_set_recent_rule_trigger",
            {"room_id": room_id, "ttl": silence_rule_ttl, "silence_seconds": int(silence)},
        )
        _scheduler_log(
            "silence_set_episode_marker",
            {
                "room_id": room_id,
                "episode_anchor": str(episode_anchor),
                "anchor_source": anchor_source,
                "ttl": SILENCE_EPISODE_MARKER_TTL_SECONDS,
            },
        )
        await enqueue_task(
            room_id,
            {
                "room_id": room_id,
                "agent_role": "facilitator",
                "reason": f"房间沉默已超过 {int(silence)} 秒，需要重新推动讨论。",
                "strategy": "提出一个具体问题，邀请成员基于已有观点给出下一步分析。",
                "priority": 2,
                "trigger_type": "silence",
                "target_dimension": "behavioral",
                "evidence": [f"silence_seconds={int(silence)}"],
                "current_phase": _phase_label(int(elapsed_seconds // 60)),
                "triggered_at": now,
            },
        )


async def check_committee_timer() -> None:
    cfg = get_agent_settings()
    auto_speak = getattr(cfg, "auto_speak", None)
    committee_enabled = True if auto_speak is None else getattr(auto_speak, "committee_enabled", True)
    if not committee_enabled:
        return

    redis_client = get_redis_client()
    active_rooms = await redis_client.smembers("active_rooms")
    warmup_seconds = cfg.timing.warmup_minutes * 60
    for room_id in active_rooms:
        room_mode = await get_room_agent_mode(room_id)
        if not should_run_auto_dispatcher(room_mode):
            continue

        elapsed_seconds = await get_elapsed_seconds_from_timer_start(room_id)
        if elapsed_seconds is None or elapsed_seconds < warmup_seconds:
            # Committee timer is also gated by teacher-started timer and warmup period.
            continue
        await basic_committee.analyze_and_dispatch(room_id)


async def check_time_progress_reminders() -> None:
    cfg = get_agent_settings()
    auto_speak = getattr(cfg, "auto_speak", None)
    time_progress_enabled = True if auto_speak is None else getattr(auto_speak, "time_progress_enabled", True)
    if not time_progress_enabled:
        return

    redis_client = get_redis_client()
    active_rooms = await redis_client.smembers("active_rooms")
    now = time.time()
    rule_marker_ttl = max(30, int(getattr(cfg.timing, "rule_trigger_marker_ttl_seconds", 180)))
    jitter_enabled = bool(getattr(cfg.timing, "time_progress_jitter_enabled", True))
    jitter_min_seconds = int(getattr(cfg.timing, "time_progress_jitter_min_seconds", 30))
    jitter_max_seconds = int(getattr(cfg.timing, "time_progress_jitter_max_seconds", 90))

    for raw_room_id in active_rooms:
        room_id = _decode_room_id(raw_room_id)
        if not room_id:
            continue
        room_mode = await get_room_agent_mode(room_id)
        if not should_run_auto_dispatcher(room_mode):
            continue

        elapsed_seconds = await get_elapsed_seconds_from_timer_start(room_id)
        if elapsed_seconds is None:
            continue

        elapsed_minutes = int(elapsed_seconds // 60)
        matched_node = next((n for n in TIME_NODE_MINUTES if elapsed_minutes >= n), None)
        if matched_node is None:
            continue

        # We only emit the newest node each run; older nodes are naturally skipped.
        for node in reversed(TIME_NODE_MINUTES):
            if elapsed_minutes >= node:
                matched_node = node
                break

        snapshot = await _load_room_snapshot(room_id)
        if snapshot is None:
            continue
        cycle_id = int(snapshot["timer_started_at"].timestamp())
        lock_key = f"trigger_lock:{room_id}:time_progress:{cycle_id}:{matched_node}"
        if await redis_client.exists(lock_key):
            continue

        jitter_seconds = 0
        due_at_for_log = None
        if jitter_enabled:
            schedule_key = f"time_progress_scheduled:{room_id}:{matched_node}"
            scheduled_raw = await redis_client.get(schedule_key)
            if not scheduled_raw:
                jitter_seconds = _stable_time_progress_jitter_seconds(
                    room_id=room_id,
                    node_minutes=matched_node,
                    min_seconds=jitter_min_seconds,
                    max_seconds=jitter_max_seconds,
                )
                due_at = now + jitter_seconds
                due_at_for_log = due_at
                scheduled_payload = {
                    "scheduled_at": now,
                    "due_at": due_at,
                    "node_minutes": matched_node,
                    "jitter_seconds": jitter_seconds,
                }
                await redis_client.setex(
                    schedule_key,
                    TIME_PROGRESS_SCHEDULE_TTL_SECONDS,
                    json.dumps(scheduled_payload, ensure_ascii=False),
                )
                _scheduler_log(
                    "time_progress_schedule_jitter",
                    {
                        "room_id": room_id,
                        "node_minutes": matched_node,
                        "jitter_seconds": jitter_seconds,
                        "due_at": due_at,
                        "now": now,
                    },
                )
                continue

            due_at = None
            try:
                scheduled_obj = json.loads(str(scheduled_raw))
                due_at = float(scheduled_obj.get("due_at"))
                jitter_seconds = int(scheduled_obj.get("jitter_seconds") or 0)
            except Exception:
                due_at = None

            if due_at is None:
                jitter_seconds = _stable_time_progress_jitter_seconds(
                    room_id=room_id,
                    node_minutes=matched_node,
                    min_seconds=jitter_min_seconds,
                    max_seconds=jitter_max_seconds,
                )
                due_at = now + jitter_seconds
                repaired_payload = {
                    "scheduled_at": now,
                    "due_at": due_at,
                    "node_minutes": matched_node,
                    "jitter_seconds": jitter_seconds,
                }
                await redis_client.setex(
                    schedule_key,
                    TIME_PROGRESS_SCHEDULE_TTL_SECONDS,
                    json.dumps(repaired_payload, ensure_ascii=False),
                )
                _scheduler_log(
                    "time_progress_schedule_jitter",
                    {
                        "room_id": room_id,
                        "node_minutes": matched_node,
                        "jitter_seconds": jitter_seconds,
                        "due_at": due_at,
                        "now": now,
                    },
                )
                continue

            if now < due_at:
                due_at_for_log = due_at
                _scheduler_log(
                    "time_progress_wait_jitter_due",
                    {
                        "room_id": room_id,
                        "node_minutes": matched_node,
                        "jitter_seconds": jitter_seconds,
                        "due_at": due_at,
                        "now": now,
                    },
                )
                continue
            due_at_for_log = due_at

        last_activity_time = await redis_client.get(f"room:{room_id}:last_activity_time")
        if not last_activity_time:
            last_activity_time = await redis_client.get(f"room:{room_id}:last_msg_time")
        has_recent_activity = False
        if last_activity_time:
            try:
                has_recent_activity = (now - float(last_activity_time)) <= 300
            except (TypeError, ValueError):
                has_recent_activity = False

        submit_state = await writing_submit_service.get_writing_submit_state(room_id)
        submit_confirm_count = len(submit_state.get("confirmations") or [])
        phase = _phase_label(elapsed_minutes)
        progress_status, progress_details = _assess_progress(
            phase=phase,
            script_state=snapshot["script_state"],
            submit_confirm_count=submit_confirm_count,
            has_recent_activity=has_recent_activity,
        )
        reason, strategy = _build_time_nudge_text(
            elapsed_minutes=elapsed_minutes,
            node_minutes=matched_node,
            phase=phase,
            progress_status=progress_status,
            progress_details=progress_details,
        )

        await redis_client.setex(lock_key, TIME_NODE_LOCK_TTL_SECONDS, "1")
        await redis_client.setex(f"recent_rule_trigger:{room_id}:time_progress", rule_marker_ttl, "1")
        if jitter_enabled:
            _scheduler_log(
                "time_progress_jitter_due_enqueue",
                {
                    "room_id": room_id,
                    "node_minutes": matched_node,
                    "jitter_seconds": jitter_seconds,
                    "due_at": due_at_for_log,
                    "now": now,
                },
            )
        await enqueue_task(
            room_id,
            {
                "room_id": room_id,
                "agent_role": "facilitator",
                "reason": reason,
                "strategy": strategy,
                "priority": 1,
                "trigger_type": "time_progress",
                "target_dimension": "behavioral",
                "evidence": [
                    f"elapsed_minutes={elapsed_minutes}",
                    f"progress_status={progress_status}",
                    f"node_minutes={matched_node}",
                ],
                "triggered_at": now,
                "current_phase": phase,
                "progress_status": progress_status,
                "elapsed_minutes": elapsed_minutes,
                "node_minutes": matched_node,
            },
        )


async def check_emotional_support() -> None:
    cfg = get_agent_settings()
    timing = cfg.timing
    if not bool(getattr(timing, "emotional_support_enabled", True)):
        _scheduler_log("emotional_support_skip_disabled", {})
        return

    redis_client = get_redis_client()
    active_rooms = await redis_client.smembers("active_rooms")
    keywords = list(getattr(timing, "emotional_support_keywords", []) or [])
    window_seconds = int(getattr(timing, "emotional_support_recent_window_seconds", 120))
    cooldown_seconds = int(getattr(timing, "emotional_support_cooldown_seconds", 180))

    for raw_room_id in active_rooms:
        room_id = _decode_room_id(raw_room_id)
        if not room_id:
            continue
        room_mode = await get_room_agent_mode(room_id)
        if not should_run_auto_dispatcher(room_mode):
            continue

        lock_key = f"trigger_lock:{room_id}:emotional_support"
        if await redis_client.exists(lock_key):
            continue

        recent_key = f"recent_rule_trigger:{room_id}:emotional_support"
        if await redis_client.exists(recent_key):
            _scheduler_log(
                "emotional_support_skip_cooldown",
                {"room_id": room_id, "cooldown_seconds": cooldown_seconds},
            )
            continue

        if await _has_recent_encourager_reply(room_id, window_seconds):
            _scheduler_log(
                "emotional_support_skip_recent_encourager",
                {"room_id": room_id, "cooldown_seconds": cooldown_seconds},
            )
            continue

        recent_students = await _load_recent_student_messages(room_id, window_seconds)
        matched = None
        for msg in recent_students:
            kw = _match_emotional_support_keyword(msg.get("content", ""), keywords)
            if kw:
                matched = (msg, kw)
                break

        if not matched:
            continue

        msg, keyword = matched
        _scheduler_log(
            "emotional_support_keyword_matched",
            {
                "room_id": room_id,
                "keyword": keyword,
                "message_id": msg.get("id", ""),
                "user_id": msg.get("user_id", ""),
                "cooldown_seconds": cooldown_seconds,
            },
        )

        await redis_client.setex(lock_key, EMOTIONAL_SUPPORT_LOCK_TTL_SECONDS, "1")
        await redis_client.setex(recent_key, cooldown_seconds, "1")
        await redis_client.setex(
            f"emotional_support:last_matched:{room_id}",
            max(cooldown_seconds, 60),
            json.dumps(
                {
                    "message_id": msg.get("id", ""),
                    "user_id": msg.get("user_id", ""),
                    "keyword": keyword,
                    "matched_at": time.time(),
                },
                ensure_ascii=False,
            ),
        )

        _scheduler_log(
            "emotional_support_enqueue",
            {
                "room_id": room_id,
                "keyword": keyword,
                "message_id": msg.get("id", ""),
                "user_id": msg.get("user_id", ""),
                "cooldown_seconds": cooldown_seconds,
            },
        )
        await enqueue_task(
            room_id,
            {
                "room_id": room_id,
                "agent_role": "encourager",
                "trigger_type": "emotional_support",
                "reason": "检测到学生可能存在挫败或消极情绪，需要给予情绪支持。",
                "strategy": "用鼓励式、低压力的问题回应学生情绪，帮助学生重新表达困难，并邀请同伴支持。",
                "priority": 1,
                "target_dimension": "emotional",
                "evidence": [
                    f"keyword={keyword}",
                    f"message_id={msg.get('id', '')}",
                    f"user_id={msg.get('user_id', '')}",
                ],
                "triggered_at": time.time(),
                "current_phase": "middle",
            },
        )


async def check_online_presence_cleanup() -> None:
    await cleanup_stale_online_presence(stale_seconds=120)


def start_scheduler() -> None:
    if scheduler.running:
        return

    cfg = get_agent_settings()
    scheduler.add_job(check_silence, "interval", seconds=30, id="check_silence", replace_existing=True)
    scheduler.add_job(snapshot_active_writing_docs, 'interval', seconds=30, id='writing_snapshots_5min', replace_existing=True, max_instances=1)
    scheduler.add_job(
        check_time_progress_reminders,
        "interval",
        seconds=30,
        id="check_time_progress_reminders",
        replace_existing=True,
    )
    scheduler.add_job(
        check_committee_timer,
        "interval",
        minutes=max(1, cfg.timing.analysis_interval_minutes),
        id="check_committee",
        replace_existing=True,
    )
    scheduler.add_job(
        check_emotional_support,
        "interval",
        seconds=max(5, int(getattr(cfg.timing, "emotional_support_check_interval_seconds", 30))),
        id="check_emotional_support",
        replace_existing=True,
    )
    scheduler.add_job(
        refresh_model_routing,
        "interval",
        seconds=300,
        id="refresh_model_routing",
        replace_existing=True,
    )
    scheduler.add_job(
        check_online_presence_cleanup,
        "interval",
        seconds=60,
        id="cleanup_online_presence",
        replace_existing=True,
    )
    scheduler.start()


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown()

