from __future__ import annotations

import os
import random
import time
import traceback
from datetime import datetime, timezone
from uuid import UUID

from fastapi import WebSocket, WebSocketDisconnect
from jose import JWTError
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.agent_messages import (
    MENTION_ACCEPTED,
    MENTION_QUEUED,
    MENTION_REASON_TEMPLATE,
    MENTION_STRATEGY,
    MENTION_UNSUPPORTED,
)
from app.agents.agent_mode import can_use_agent_role, get_room_agent_mode
from app.agents.queue import create_mention_entry, enqueue_task
from app.agents.queue import queue_key
from app.agents.role_agents import ROLE_AGENTS
from app.agents.settings import get_agent_settings
from app.analysis.triggers import trigger_detector
from app.db.redis_client import get_redis_client, mark_room_active, touch_online_presence
from app.db.redis_client import get_user_active_session_jti
from app.db.session import AsyncSessionLocal
from app.models.room_member import RoomMember
from app.models.user import User
from app.schemas.message import ChatMessageFrame
from app.services.auth_service import decode_access_token
from app.services.message_service import MessageService
from app.services import writing_doc_service
from app.websocket.manager import manager

WS_DEBUG_LOG = os.getenv("WS_DEBUG_LOG", "").lower() == "true"
AGENT_DEBUG_LOG = os.getenv("AGENT_DEBUG_LOG", "").lower() == "true"


def _short_jti(session_jti: str | None) -> str | None:
    if not session_jti:
        return None
    return str(session_jti)[:8]


def _new_connection_id() -> str:
    return f"ws-{int(time.time() * 1000) % 10000000}-{random.randint(1000, 9999)}"


def log_ws_debug(
    event: str,
    room_id: str,
    connection_id: str,
    user_id: str | None = None,
    username: str | None = None,
    session_jti: str | None = None,
    extra: dict | None = None,
) -> None:
    if not WS_DEBUG_LOG:
        return
    payload = {
        "event": event,
        "ts": datetime.now(timezone.utc).isoformat(),
        "room_id": room_id,
        "connection_id": connection_id,
        "user_id": user_id,
        "username": username,
        "session_jti": _short_jti(session_jti),
    }
    if extra:
        payload["extra"] = extra
    print("[WS-DEBUG]", payload, flush=True)


def log_agent_debug(event: str, extra: dict | None = None) -> None:
    if not AGENT_DEBUG_LOG:
        return
    payload = {"event": event, "ts": datetime.now(timezone.utc).isoformat()}
    if extra:
        payload["extra"] = extra
    print("[AGENT-MENTION-DEBUG]", payload, flush=True)


async def verify_token(token: str | None, db: AsyncSession) -> tuple[User, str] | None:
    if not isinstance(token, str) or not token.strip():
        return None
    try:
        payload = decode_access_token(token)
        user_id = payload.get("sub")
        token_jti = payload.get("jti")
        if not user_id:
            return None
        if not token_jti:
            return None
        parsed_id = UUID(user_id)
    except (JWTError, ValueError, TypeError, AttributeError):
        return None

    try:
        active_jti = await get_user_active_session_jti(user_id)
        if active_jti and active_jti != token_jti:
            return None
    except RuntimeError:
        # Redis unavailable: keep backward-compatible behavior.
        pass

    result = await db.execute(select(User).where(User.id == parsed_id))
    user = result.scalar_one_or_none()
    if user is None:
        return None
    return user, token_jti


async def is_room_member(user_id: UUID, room_id: str, db: AsyncSession) -> bool:
    try:
        parsed_room_id = UUID(room_id)
    except ValueError:
        return False

    result = await db.execute(
        select(RoomMember.id).where(
            RoomMember.room_id == parsed_room_id,
            RoomMember.user_id == user_id,
        )
    )
    return result.scalar_one_or_none() is not None


def _normalized_mentions(mentions: list[str] | None) -> list[str]:
    if not mentions:
        return []

    cfg = get_agent_settings()
    max_mentions = max(1, cfg.mention.max_mentions_per_message)

    normalized = []
    seen = set()
    for raw in mentions:
        role = (raw or "").strip().lower()
        if not role or role in seen:
            continue
        seen.add(role)
        normalized.append(role)
        if len(normalized) >= max_mentions:
            break
    return normalized


async def _trigger_mentions(room_id: str, source_message_id: str, user: User, mentions: list[str] | None) -> None:
    cfg = get_agent_settings()
    if not cfg.mention.enabled:
        return

    room_mode = await get_room_agent_mode(room_id)
    for role in _normalized_mentions(mentions):
        log_agent_debug(
            "agent_mention_role_start",
            {
                "room_id": room_id,
                "source_message_id": source_message_id,
                "agent_role": role,
                "username": user.display_name,
            },
        )

        if role not in ROLE_AGENTS:
            log_agent_debug(
                "agent_mention_unsupported_broadcast_start",
                {"room_id": room_id, "source_message_id": source_message_id, "agent_role": role},
            )
            await manager.broadcast_to_room(
                room_id,
                {
                    "type": "agent:ack",
                    "agent_role": role,
                    "source_message_id": source_message_id,
                    "status": "unsupported",
                    "message": "Current version does not support this agent role.",
                },
            )
            log_agent_debug(
                "agent_mention_unsupported_broadcast_done",
                {"room_id": room_id, "source_message_id": source_message_id, "agent_role": role},
            )
            continue

        if not can_use_agent_role(room_mode, role):
            log_agent_debug(
                "agent_mention_blocked_by_mode",
                {
                    "room_id": room_id,
                    "source_message_id": source_message_id,
                    "agent_role": role,
                    "agent_mode": room_mode,
                },
            )
            continue

        mention_entry_enabled = bool(getattr(cfg.timing, "mention_entry_enabled", False))
        mention_entry_payload = None
        if mention_entry_enabled:
            now_ts = time.time()
            expire_ts = now_ts + max(1, int(getattr(cfg.timing, "mention_entry_queue_max_wait_sec", 60)))
            mention_entry_payload = await create_mention_entry(
                room_id=room_id,
                agent_role=role,
                source_message_id=source_message_id,
                student_name=user.display_name,
                reason=f"Student {user.display_name} mentioned @{role}.",
                strategy="Prioritize responding to this student and provide a concrete next step.",
                trigger_type="mention",
                created_at=now_ts,
                expire_at=expire_ts,
            )

        ack_payload = {
            "type": "agent:ack",
            "agent_role": role,
            "source_message_id": source_message_id,
            "status": "accepted",
            "message": "Agent mention accepted.",
        }
        if mention_entry_payload is not None:
            ack_payload["entry_id"] = mention_entry_payload.get("entry_id")
            ack_payload["queue_mode"] = "entry_queue"

        log_agent_debug(
            "agent_mention_ack_broadcast_start",
            {"room_id": room_id, "source_message_id": source_message_id, "agent_role": role},
        )
        await manager.broadcast_to_room(room_id, ack_payload)
        log_agent_debug(
            "agent_mention_ack_broadcast_done",
            {"room_id": room_id, "source_message_id": source_message_id, "agent_role": role},
        )

        if mention_entry_enabled:
            log_agent_debug(
                "agent_mention_entry_created",
                {
                    "room_id": room_id,
                    "source_message_id": source_message_id,
                    "agent_role": role,
                    "entry_id": mention_entry_payload.get("entry_id") if mention_entry_payload else None,
                },
            )
            continue

        log_agent_debug(
            "agent_enqueue_start",
            {
                "room_id": room_id,
                "source_message_id": source_message_id,
                "agent_role": role,
                "queue_key": queue_key(room_id),
                "priority": cfg.mention.priority,
                "trigger_type": "mention",
            },
        )
        task = await enqueue_task(
            room_id,
            {
                "room_id": room_id,
                "agent_role": role,
                "reason": f"Student {user.display_name} mentioned @{role}.",
                "strategy": "Prioritize responding to this student and provide a concrete next step.",
                "priority": cfg.mention.priority,
                "trigger_type": "mention",
                "target_dimension": "user_request",
                "evidence": [],
                "current_phase": "unknown",
                "student_name": user.display_name,
                "source_message_id": source_message_id,
                "triggered_at": time.time(),
            },
        )
        if not task:
            log_agent_debug(
                "agent_enqueue_skipped_by_mode",
                {
                    "room_id": room_id,
                    "source_message_id": source_message_id,
                    "agent_role": role,
                    "agent_mode": room_mode,
                },
            )
            continue
        log_agent_debug(
            "agent_enqueue_done",
            {
                "room_id": room_id,
                "source_message_id": source_message_id,
                "agent_role": role,
                "task_id": task.get("task_id"),
                "queue_key": queue_key(room_id),
            },
        )

        log_agent_debug(
            "agent_queued_broadcast_start",
            {
                "room_id": room_id,
                "source_message_id": source_message_id,
                "agent_role": role,
                "task_id": task.get("task_id"),
            },
        )
        await manager.broadcast_to_room(
            room_id,
            {
                "type": "agent:queued",
                "agent_role": role,
                "source_message_id": source_message_id,
                "status": "queued",
                "task_id": task["task_id"],
                "message": "Agent request queued.",
            },
        )
        log_agent_debug(
            "agent_queued_broadcast_done",
            {
                "room_id": room_id,
                "source_message_id": source_message_id,
                "agent_role": role,
                "task_id": task.get("task_id"),
            },
        )


async def _touch_online_presence_best_effort(room_id: str, user_id: str) -> None:
    try:
        await touch_online_presence(room_id, user_id)
    except RuntimeError:
        pass


async def _is_agent_pipeline_busy(room_id: str) -> bool:
    try:
        redis_client = get_redis_client()
    except RuntimeError:
        return False

    try:
        if await redis_client.exists(f"room:{room_id}:agent_lock"):
            return True
        pending = int(await redis_client.zcard(queue_key(room_id)))
        return pending > 0
    except Exception:
        return False


async def _first_cooling_mention_role(room_id: str, mentions: list[str] | None) -> str | None:
    normalized_mentions = _normalized_mentions(mentions)
    if not normalized_mentions:
        return None
    try:
        redis_client = get_redis_client()
    except RuntimeError:
        return None
    for role in normalized_mentions:
        try:
            if await redis_client.exists(f"cooldown:{room_id}:{role}"):
                return role
        except Exception:
            continue
    return None


async def _cooldown_remaining_seconds(room_id: str, role: str) -> int:
    try:
        redis_client = get_redis_client()
    except RuntimeError:
        return 1
    try:
        ttl = await redis_client.ttl(f"cooldown:{room_id}:{role}")
        if isinstance(ttl, int) and ttl > 0:
            return ttl
    except Exception:
        pass
    return 1


async def handle_chat_message(
    data: dict,
    room_id: str,
    user: User,
    db: AsyncSession,
    websocket: WebSocket | None = None,
    connection_id: str | None = None,
    session_jti: str | None = None,
) -> None:
    chat_started = time.perf_counter()
    raw_content = str(data.get("content") or "")
    loadmsg_id = None
    if "loadmsg:" in raw_content:
        loadmsg_id = raw_content.split()[0]
    log_ws_debug(
        "chat_message_received",
        room_id,
        connection_id or "unknown",
        user_id=str(user.id),
        username=user.display_name,
        session_jti=session_jti,
        extra={"loadmsg_id": loadmsg_id},
    )
    try:
        frame = ChatMessageFrame.model_validate(data)
    except ValidationError:
        log_ws_debug(
            "chat_message_error",
            room_id,
            connection_id or "unknown",
            user_id=str(user.id),
            username=user.display_name,
            session_jti=session_jti,
            extra={
                "loadmsg_id": loadmsg_id,
                "stage": "chat_frame_validated",
                "error": "validation_error",
                "duration_ms": round((time.perf_counter() - chat_started) * 1000, 3),
            },
        )
        return
    log_ws_debug(
        "chat_frame_validated",
        room_id,
        connection_id or "unknown",
        user_id=str(user.id),
        username=user.display_name,
        session_jti=session_jti,
        extra={"loadmsg_id": loadmsg_id, "duration_ms": round((time.perf_counter() - chat_started) * 1000, 3)},
    )

    content = frame.content.strip()
    mentions = frame.mentions
    if not content:
        return
    cooling_role = await _first_cooling_mention_role(room_id, mentions)
    if cooling_role:
        remaining_seconds = await _cooldown_remaining_seconds(room_id, cooling_role)
        if websocket is not None:
            await websocket.send_json(
                {
                    "type": "agent:mention_blocked",
                    "reason": "agent_cooling",
                    "agent_role": cooling_role,
                    "message": f"该智能体正在冷却中，请在 {remaining_seconds} 秒后重试。",
                }
            )
        return

    normalized_mentions = _normalized_mentions(mentions)
    if normalized_mentions and await _is_agent_pipeline_busy(room_id):
        if websocket is not None:
            await websocket.send_json(
                {
                    "type": "agent:mention_blocked",
                    "reason": "agent_busy",
                    "message": "智能体正在处理其他请求，请稍后再 @ 智能体。",
                }
            )
        return

    try:
        save_started = time.perf_counter()
        log_ws_debug(
            "save_student_message_start",
            room_id,
            connection_id or "unknown",
            user_id=str(user.id),
            username=user.display_name,
            session_jti=session_jti,
            extra={"loadmsg_id": loadmsg_id},
        )
        msg = await MessageService.save_student_message(
            db,
            room_id,
            str(user.id),
            content,
            mentions,
            connection_id=connection_id,
            loadmsg_id=loadmsg_id,
        )
        log_ws_debug(
            "save_student_message_done",
            room_id,
            connection_id or "unknown",
            user_id=str(user.id),
            username=user.display_name,
            session_jti=session_jti,
            extra={"loadmsg_id": loadmsg_id, "duration_ms": round((time.perf_counter() - save_started) * 1000, 3)},
        )

        try:
            redis_client = get_redis_client()
        except RuntimeError:
            redis_client = None

        now_ts = msg.created_at.timestamp() if msg.created_at else None
        if redis_client is not None and now_ts is not None:
            redis_activity_started = time.perf_counter()
            log_ws_debug(
                "redis_room_activity_update_start",
                room_id,
                connection_id or "unknown",
                user_id=str(user.id),
                username=user.display_name,
                session_jti=session_jti,
                extra={"loadmsg_id": loadmsg_id},
            )
            await mark_room_active(room_id, client=redis_client, message=True, ts=now_ts)
            await redis_client.setnx(f"room:{room_id}:start_time", now_ts)
            log_ws_debug(
                "redis_room_activity_update_done",
                room_id,
                connection_id or "unknown",
                user_id=str(user.id),
                username=user.display_name,
                session_jti=session_jti,
                extra={"loadmsg_id": loadmsg_id, "duration_ms": round((time.perf_counter() - redis_activity_started) * 1000, 3)},
            )
        await _touch_online_presence_best_effort(room_id, str(user.id))

        broadcast_started = time.perf_counter()
        log_ws_debug(
            "broadcast_chat_new_message_start",
            room_id,
            connection_id or "unknown",
            user_id=str(user.id),
            username=user.display_name,
            session_jti=session_jti,
            extra={"loadmsg_id": loadmsg_id},
        )
        await manager.broadcast_to_room(
            room_id,
            {
                "type": "chat:new_message",
                "id": str(msg.id),
                "seq_num": msg.seq_num,
                "sender_type": "student",
                "sender_id": str(user.id),
                "display_name": user.display_name,
                "content": msg.content,
                "created_at": msg.created_at.isoformat() if msg.created_at else None,
            },
        )
        log_ws_debug(
            "broadcast_chat_new_message_done",
            room_id,
            connection_id or "unknown",
            user_id=str(user.id),
            username=user.display_name,
            session_jti=session_jti,
            extra={"loadmsg_id": loadmsg_id, "duration_ms": round((time.perf_counter() - broadcast_started) * 1000, 3)},
        )

        monopoly_started = time.perf_counter()
        log_ws_debug(
            "trigger_monopoly_start",
            room_id,
            connection_id or "unknown",
            user_id=str(user.id),
            username=user.display_name,
            session_jti=session_jti,
            extra={"loadmsg_id": loadmsg_id},
        )
        await trigger_detector.check_monopoly(room_id, str(user.id))
        log_ws_debug(
            "trigger_monopoly_done",
            room_id,
            connection_id or "unknown",
            user_id=str(user.id),
            username=user.display_name,
            session_jti=session_jti,
            extra={"loadmsg_id": loadmsg_id, "duration_ms": round((time.perf_counter() - monopoly_started) * 1000, 3)},
        )

        mention_started = time.perf_counter()
        log_ws_debug(
            "trigger_mentions_start",
            room_id,
            connection_id or "unknown",
            user_id=str(user.id),
            username=user.display_name,
            session_jti=session_jti,
            extra={"loadmsg_id": loadmsg_id},
        )
        await _trigger_mentions(room_id, str(msg.id), user, mentions)
        log_ws_debug(
            "trigger_mentions_done",
            room_id,
            connection_id or "unknown",
            user_id=str(user.id),
            username=user.display_name,
            session_jti=session_jti,
            extra={"loadmsg_id": loadmsg_id, "duration_ms": round((time.perf_counter() - mention_started) * 1000, 3)},
        )
        log_ws_debug(
            "chat_message_done",
            room_id,
            connection_id or "unknown",
            user_id=str(user.id),
            username=user.display_name,
            session_jti=session_jti,
            extra={"loadmsg_id": loadmsg_id, "duration_ms": round((time.perf_counter() - chat_started) * 1000, 3)},
        )
    except Exception as exc:
        log_ws_debug(
            "chat_message_error",
            room_id,
            connection_id or "unknown",
            user_id=str(user.id),
            username=user.display_name,
            session_jti=session_jti,
            extra={
                "loadmsg_id": loadmsg_id,
                "stage": "chat_pipeline",
                "exception_class": type(exc).__name__,
                "exception_message": str(exc),
                "duration_ms": round((time.perf_counter() - chat_started) * 1000, 3),
                "traceback": traceback.format_exc(limit=3),
            },
        )
        raise


async def handle_writing_update(websocket: WebSocket, data: dict, room_id: str, user: User) -> None:
    content = str(data.get("content") or "")
    base_version_raw = data.get("base_version")
    try:
        base_version = int(base_version_raw) if base_version_raw is not None else None
    except (TypeError, ValueError):
        base_version = None
    # Soft guard to avoid extremely large payloads over websocket.
    if len(content) > 200_000:
        return

    state, applied = await writing_doc_service.apply_writing_doc_update_with_base_version(
        room_id,
        content,
        str(user.id),
        updated_by_display_name=user.display_name,
        base_version=base_version,
    )
    await _touch_online_presence_best_effort(room_id, str(user.id))
    if not applied:
        await websocket.send_json(
            {
                "type": "writing:resync",
                "room_id": room_id,
                "content": state["content"],
                "version": state["version"],
                "updated_at": state["updated_at"],
                "updated_by": state["updated_by"],
                "updated_by_display_name": state.get("updated_by_display_name"),
                "reason": "stale_base_version",
            }
        )
        return

    await manager.broadcast_to_room(
        room_id,
        {
            "type": "writing:updated",
            "room_id": room_id,
            "content": state["content"],
            "version": state["version"],
            "updated_at": state["updated_at"],
            "updated_by": state["updated_by"],
            "updated_by_display_name": state.get("updated_by_display_name"),
        },
    )


async def handle_writing_awareness(data: dict, room_id: str, user: User) -> None:
    await _touch_online_presence_best_effort(room_id, str(user.id))
    payload = {
        "type": "writing:awareness",
        "room_id": room_id,
        "user_id": str(user.id),
        "display_name": user.display_name,
        "is_editing": bool(data.get("is_editing")),
        "cursor": data.get("cursor"),
        "at": datetime.now(timezone.utc).isoformat(),
    }
    await manager.broadcast_to_room(room_id, payload)


async def websocket_endpoint(
    websocket: WebSocket,
    room_id: str,
) -> None:
    connection_id = _new_connection_id()
    user: User | None = None
    session_jti: str | None = None
    redis_client = None
    manager_connected = False

    log_ws_debug("ws_accept_start", room_id, connection_id)
    await websocket.accept()
    log_ws_debug("ws_accept_done", room_id, connection_id)

    log_ws_debug("auth_frame_wait_start", room_id, connection_id)
    try:
        auth_data = await websocket.receive_json()
        log_ws_debug("auth_frame_received", room_id, connection_id, extra={"frame_type": auth_data.get("type")})
    except WebSocketDisconnect:
        log_ws_debug("websocket_disconnect", room_id, connection_id, extra={"stage": "auth_frame_wait"})
        return
    except Exception as exc:
        log_ws_debug(
            "unexpected_exception",
            room_id,
            connection_id,
            extra={
                "stage": "auth_frame_wait",
                "exception_class": type(exc).__name__,
                "exception_message": str(exc),
                "traceback": traceback.format_exc(limit=3),
            },
        )
        try:
            await websocket.close(code=4002, reason="Auth timeout")
        except RuntimeError:
            # Client already gone during handshake.
            pass
        return

    if auth_data.get("type") != "auth":
        log_ws_debug("auth_token_invalid", room_id, connection_id, extra={"reason": "first_frame_not_auth"})
        try:
            await websocket.close(code=4001, reason="First frame must be auth")
        except RuntimeError:
            pass
        return

    log_ws_debug("auth_token_verify_start", room_id, connection_id)
    log_ws_debug("db_session_open", room_id, connection_id, extra={"stage": "auth"})
    async with AsyncSessionLocal() as db:
        verified = await verify_token(auth_data.get("token", ""), db)
        if not verified:
            log_ws_debug("db_session_closed", room_id, connection_id, extra={"stage": "auth"})
            log_ws_debug("auth_token_invalid", room_id, connection_id, extra={"reason": "token_verify_failed"})
            try:
                await websocket.close(code=4001, reason="Invalid token")
            except RuntimeError:
                pass
            return
        user, session_jti = verified
        log_ws_debug(
            "auth_token_verified",
            room_id,
            connection_id,
            user_id=str(user.id),
            username=user.display_name,
            session_jti=session_jti,
        )

        log_ws_debug(
            "room_member_check_start",
            room_id,
            connection_id,
            user_id=str(user.id),
            username=user.display_name,
            session_jti=session_jti,
        )
        if not await is_room_member(user.id, room_id, db):
            log_ws_debug("db_session_closed", room_id, connection_id, extra={"stage": "auth"})
            log_ws_debug(
                "room_member_forbidden",
                room_id,
                connection_id,
                user_id=str(user.id),
                username=user.display_name,
                session_jti=session_jti,
            )
            try:
                await websocket.close(code=4003, reason="Not a room member")
            except RuntimeError:
                pass
            return
    log_ws_debug("db_session_closed", room_id, connection_id, extra={"stage": "auth"})
    log_ws_debug(
        "room_member_checked",
        room_id,
        connection_id,
        user_id=str(user.id),
        username=user.display_name,
        session_jti=session_jti,
    )

    log_ws_debug(
        "manager_connect_start",
        room_id,
        connection_id,
        user_id=str(user.id),
        username=user.display_name,
        session_jti=session_jti,
    )
    await manager.connect(websocket, room_id, user, session_jti=session_jti)
    manager_connected = True
    log_ws_debug(
        "manager_connect_done",
        room_id,
        connection_id,
        user_id=str(user.id),
        username=user.display_name,
        session_jti=session_jti,
    )

    redis_client = get_redis_client()
    log_ws_debug(
        "room_user_join_broadcast_start",
        room_id,
        connection_id,
        user_id=str(user.id),
        username=user.display_name,
        session_jti=session_jti,
    )
    online_count = int(await redis_client.scard(f"room:{room_id}:online_users"))
    await manager.broadcast_to_room(
        room_id,
        {
            "type": "room:user_join",
            "user_id": str(user.id),
            "display_name": user.display_name,
            "online_count": online_count,
        },
    )
    log_ws_debug(
        "room_user_join_broadcast_done",
        room_id,
        connection_id,
        user_id=str(user.id),
        username=user.display_name,
        session_jti=session_jti,
        extra={"online_count": online_count},
    )

    try:
        log_ws_debug(
            "receive_loop_start",
            room_id,
            connection_id,
            user_id=str(user.id),
            username=user.display_name,
            session_jti=session_jti,
        )
        while True:
            data = await websocket.receive_json()
            if data.get("type") == "presence:ping":
                log_ws_debug(
                    "presence_ping_received",
                    room_id,
                    connection_id,
                    user_id=str(user.id),
                    username=user.display_name,
                    session_jti=session_jti,
                )
                await _touch_online_presence_best_effort(room_id, str(user.id))
                continue
            if data.get("type") == "chat:message":
                log_ws_debug(
                    "db_session_open",
                    room_id,
                    connection_id,
                    user_id=str(user.id),
                    username=user.display_name,
                    session_jti=session_jti,
                    extra={"stage": "chat_message"},
                )
                async with AsyncSessionLocal() as db:
                    await handle_chat_message(
                        data,
                        room_id,
                        user,
                        db,
                        websocket=websocket,
                        connection_id=connection_id,
                        session_jti=session_jti,
                    )
                log_ws_debug(
                    "db_session_closed",
                    room_id,
                    connection_id,
                    user_id=str(user.id),
                    username=user.display_name,
                    session_jti=session_jti,
                    extra={"stage": "chat_message"},
                )
                continue
            if data.get("type") == "writing:update":
                await handle_writing_update(websocket, data, room_id, user)
                continue
            if data.get("type") == "writing:awareness":
                await handle_writing_awareness(data, room_id, user)
    except WebSocketDisconnect:
        log_ws_debug(
            "websocket_disconnect",
            room_id,
            connection_id,
            user_id=str(user.id),
            username=user.display_name,
            session_jti=session_jti,
            extra={"stage": "receive_loop"},
        )
    except RuntimeError as exc:
        # Starlette may raise RuntimeError instead of WebSocketDisconnect on closed sockets.
        if "WebSocket is not connected" not in str(exc):
            log_ws_debug(
                "runtime_error",
                room_id,
                connection_id,
                user_id=str(user.id) if user else None,
                username=user.display_name if user else None,
                session_jti=session_jti,
                extra={
                    "exception_class": type(exc).__name__,
                    "exception_message": str(exc),
                    "traceback": traceback.format_exc(limit=3),
                },
            )
            raise
        log_ws_debug(
            "runtime_error",
            room_id,
            connection_id,
            user_id=str(user.id),
            username=user.display_name,
            session_jti=session_jti,
            extra={
                "exception_class": type(exc).__name__,
                "exception_message": str(exc),
                "traceback": traceback.format_exc(limit=3),
            },
        )
    except Exception as exc:
        log_ws_debug(
            "unexpected_exception",
            room_id,
            connection_id,
            user_id=str(user.id) if user else None,
            username=user.display_name if user else None,
            session_jti=session_jti,
            extra={
                "exception_class": type(exc).__name__,
                "exception_message": str(exc),
                "traceback": traceback.format_exc(limit=3),
            },
        )
        raise
    finally:
        if user and manager_connected:
            log_ws_debug(
                "manager_disconnect_start",
                room_id,
                connection_id,
                user_id=str(user.id),
                username=user.display_name,
                session_jti=session_jti,
            )
            await manager.disconnect(websocket, room_id, str(user.id), session_jti=session_jti)
            log_ws_debug(
                "manager_disconnect_done",
                room_id,
                connection_id,
                user_id=str(user.id),
                username=user.display_name,
                session_jti=session_jti,
            )
            if redis_client is None:
                redis_client = get_redis_client()
            log_ws_debug(
                "room_user_leave_broadcast_start",
                room_id,
                connection_id,
                user_id=str(user.id),
                username=user.display_name,
                session_jti=session_jti,
            )
            online_count = int(await redis_client.scard(f"room:{room_id}:online_users"))
            await manager.broadcast_to_room(
                room_id,
                {
                    "type": "room:user_leave",
                    "user_id": str(user.id),
                    "display_name": user.display_name,
                    "online_count": online_count,
                },
            )
            log_ws_debug(
                "room_user_leave_broadcast_done",
                room_id,
                connection_id,
                user_id=str(user.id),
                username=user.display_name,
                session_jti=session_jti,
                extra={"online_count": online_count},
            )
