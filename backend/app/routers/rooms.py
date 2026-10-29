from typing import Optional
from uuid import UUID
import json
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.redis_client import get_redis_client, mark_room_active
from app.db.session import get_db
from app.dependencies import get_current_user, require_teacher
from app.exceptions import RoomMemberForbiddenError, RoomNotFoundError
from app.models.room_member import RoomMember
from app.models.user import User, UserRole
from app.schemas.message import MessageHistoryResponse
from app.schemas.room import (
    RoomActivityRequest,
    RoomCreate,
    RoomDeleteRequest,
    RoomResponse,
    RoomTaskBindRequest,
    RoomUpdate,
    TaskScriptConfirmRequest,
    TaskScriptLeaseRequest,
    WritingSubmitStateResponse,
    WritingDocStateResponse,
    WritingDocChangeLogResponse,
)
from app.services import room_service, task_script_service, task_service, writing_submit_service, writing_doc_service
from app.services.message_service import MessageService

router = APIRouter()
MIN_WRITING_SUBMIT_CHARS = 50


async def _ensure_room_member(db: AsyncSession, room_id: UUID, user_id: UUID) -> None:
    membership = await db.execute(
        select(RoomMember.id).where(
            RoomMember.room_id == room_id,
            RoomMember.user_id == user_id,
        )
    )
    if membership.scalar_one_or_none() is None:
        raise RoomMemberForbiddenError()


async def _touch_room_activity(room_id: str) -> None:
    try:
        await mark_room_active(room_id, client=get_redis_client())
    except RuntimeError:
        return


async def _publish_room_timer_update(room) -> None:
    try:
        redis_client = get_redis_client()
        payload = {
            "type": "room:timer_updated",
            "room_id": str(room.id),
            "timer_started_at": room.timer_started_at.isoformat() if room.timer_started_at else None,
            "timer_deadline_at": room.timer_deadline_at.isoformat() if room.timer_deadline_at else None,
            "timer_stopped_at": room.timer_stopped_at.isoformat() if room.timer_stopped_at else None,
        }
        await redis_client.publish(f"room:{room.id}", json.dumps(payload, ensure_ascii=False))
    except RuntimeError:
        pass


async def _publish_writing_submit_updated(room_id: str, state: dict) -> None:
    try:
        redis_client = get_redis_client()
        payload = {
            "type": "room:writing_submit_updated",
            "room_id": room_id,
            "state": state,
        }
        await redis_client.publish(f"room:{room_id}", json.dumps(payload, ensure_ascii=False))
    except RuntimeError:
        pass


async def _publish_writing_doc_updated(room_id: str, state: dict, reason: str = "updated") -> None:
    try:
        redis_client = get_redis_client()
        payload = {
            "type": "writing:updated",
            "room_id": room_id,
            "content": state.get("content") or "",
            "version": int(state.get("version") or 0),
            "updated_at": state.get("updated_at"),
            "updated_by": state.get("updated_by"),
            "updated_by_display_name": state.get("updated_by_display_name"),
            "reason": reason,
        }
        await redis_client.publish(f"room:{room_id}", json.dumps(payload, ensure_ascii=False))
    except RuntimeError:
        pass


async def _get_online_count(room_id: str) -> int | None:
    try:
        redis_client = get_redis_client()
    except RuntimeError:
        return None
    return int(await redis_client.scard(f"room:{room_id}:online_users"))


def _plain_text_length(content: str) -> int:
    text = re.sub(r"<[^>]+>", "", str(content or ""))
    text = text.replace("&nbsp;", " ").strip()
    return len(text)


@router.get("", response_model=list[RoomResponse])
async def list_rooms(
    status: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rooms = await room_service.get_rooms(db, status)
    if current_user.role == UserRole.student:
        rooms = [room for room in rooms if room.timer_stopped_at is None]
    result = []
    for room in rooms:
        count = await room_service.get_member_count(db, room.id)
        student_count = await room_service.get_student_count(db, room.id)
        online_count = await _get_online_count(str(room.id))
        resp = RoomResponse.model_validate(room)
        resp.member_count = count
        resp.student_count = student_count
        resp.online_count = online_count if online_count is not None else count
        result.append(resp)
    return result


@router.post("", response_model=RoomResponse, status_code=201)
async def create_room(
    data: RoomCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_teacher),
):
    room = await room_service.create_room(db, data, current_user.id)
    return RoomResponse.model_validate(room)


@router.get("/{room_id}", response_model=RoomResponse)
async def get_room(
    room_id: UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    room = await room_service.get_room(db, room_id)
    if not room:
        raise RoomNotFoundError()
    count = await room_service.get_member_count(db, room.id)
    student_count = await room_service.get_student_count(db, room.id)
    online_count = await _get_online_count(str(room.id))
    resp = RoomResponse.model_validate(room)
    resp.member_count = count
    resp.student_count = student_count
    resp.online_count = online_count if online_count is not None else count
    return resp


@router.post("/{room_id}/join", status_code=200)
async def join_room(
    room_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    room = await room_service.get_room(db, room_id)
    if not room:
        raise RoomNotFoundError()
    await room_service.join_room(db, room_id, current_user.id)
    return {"detail": "已加入房间"}




@router.post("/{room_id}/leave", status_code=200)
async def leave_room(
    room_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    room = await room_service.get_room(db, room_id)
    if not room:
        raise RoomNotFoundError()
    if room.timer_started_at is not None and room.timer_stopped_at is None:
        raise HTTPException(status_code=409, detail="Room members are locked after timer starts")
    await room_service.leave_room(db, room_id, current_user.id)
    return {"detail": "Left room"}

@router.patch("/{room_id}", response_model=RoomResponse)
async def update_room(
    room_id: UUID,
    data: RoomUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_teacher),
):
    room = await room_service.get_room(db, room_id)
    if not room:
        raise RoomNotFoundError()
    room = await room_service.update_room_status(db, room, data.status)
    count = await room_service.get_member_count(db, room.id)
    student_count = await room_service.get_student_count(db, room.id)
    online_count = await _get_online_count(str(room.id))
    resp = RoomResponse.model_validate(room)
    resp.member_count = count
    resp.student_count = student_count
    resp.online_count = online_count if online_count is not None else count
    return resp


@router.patch("/{room_id}/task", response_model=RoomResponse)
async def bind_room_task(
    room_id: UUID,
    data: RoomTaskBindRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_teacher),
):
    room = await room_service.get_room(db, room_id)
    if not room:
        raise RoomNotFoundError()

    task = await task_service.get_task(db, data.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    room = await room_service.bind_room_task(db, room, data.task_id)
    count = await room_service.get_member_count(db, room.id)
    student_count = await room_service.get_student_count(db, room.id)
    online_count = await _get_online_count(str(room.id))
    resp = RoomResponse.model_validate(room)
    resp.member_count = count
    resp.student_count = student_count
    resp.online_count = online_count if online_count is not None else count
    return resp


@router.post("/{room_id}/timer/start", response_model=RoomResponse, status_code=200)
async def start_room_timer(
    room_id: UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_teacher),
):
    room = await room_service.get_room(db, room_id)
    if not room:
        raise RoomNotFoundError()
    student_count = await room_service.get_student_count(db, room.id)
    room = await room_service.start_room_timer(db, room)
    writing_state = await writing_submit_service.clear_writing_submit_state(str(room.id))
    count = await room_service.get_member_count(db, room.id)

    try:
        redis_client = get_redis_client()
        await redis_client.sadd("active_rooms", str(room.id))
    except RuntimeError:
        pass
    await _publish_room_timer_update(room)
    await _publish_writing_submit_updated(str(room.id), writing_state)

    resp = RoomResponse.model_validate(room)
    resp.member_count = count
    resp.student_count = student_count
    online_count = await _get_online_count(str(room.id))
    resp.online_count = online_count if online_count is not None else count
    return resp


@router.post("/{room_id}/timer/reset", response_model=RoomResponse, status_code=200)
async def reset_room_timer(
    room_id: UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_teacher),
):
    room = await room_service.get_room(db, room_id)
    if not room:
        raise RoomNotFoundError()

    room = await room_service.reset_room_timer(db, room)
    writing_state = await writing_submit_service.clear_writing_submit_state(str(room.id))
    count = await room_service.get_member_count(db, room.id)
    student_count = await room_service.get_student_count(db, room.id)

    try:
        redis_client = get_redis_client()
        await redis_client.srem("active_rooms", str(room.id))
    except RuntimeError:
        pass
    await _publish_room_timer_update(room)
    await _publish_writing_submit_updated(str(room.id), writing_state)

    resp = RoomResponse.model_validate(room)
    resp.member_count = count
    resp.student_count = student_count
    online_count = await _get_online_count(str(room.id))
    resp.online_count = online_count if online_count is not None else count
    return resp


@router.delete("/{room_id}", status_code=200)
async def delete_room(
    room_id: UUID,
    data: RoomDeleteRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_teacher),
):
    room = await room_service.get_room(db, room_id)
    if not room:
        raise RoomNotFoundError()

    if room.name != data.confirm_name.strip():
        raise HTTPException(status_code=400, detail="房间名称确认不匹配")

    await room_service.delete_room(db, room_id)

    try:
        redis_client = get_redis_client()
        room_id_str = str(room_id)
        await redis_client.srem("active_rooms", room_id_str)
        await redis_client.delete(
            f"agent_queue:{room_id_str}",
            f"room:{room_id_str}:last_msg_time",
            f"room:{room_id_str}:last_activity_time",
            f"room:{room_id_str}:start_time",
            f"room:{room_id_str}:online_users",
            f"room:{room_id_str}:online_user_conn_counts",
            f"room:{room_id_str}:writing_doc_state",
            f"room:{room_id_str}:writing_doc_version",
            f"room:{room_id_str}:writing_doc_change_log",
            f"room:{room_id_str}:writing_doc_history",
            f"room:{room_id_str}:writing_submit_state",
            f"room:{room_id_str}:agent_lock",
            f"room:{room_id_str}:recent_senders",
            f"trigger_lock:{room_id_str}:silence",
            f"trigger_lock:{room_id_str}:monopoly",
        )
    except RuntimeError:
        pass

    return {"detail": "房间已删除"}


@router.get("/{room_id}/messages", response_model=MessageHistoryResponse)
async def get_messages(
    room_id: UUID,
    before_seq: int | None = Query(None),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    room = await room_service.get_room(db, room_id)
    if not room:
        raise RoomNotFoundError()

    await _ensure_room_member(db, room_id, current_user.id)

    return await MessageService.get_history_messages(db, str(room_id), before_seq, limit)


@router.post("/{room_id}/activity", status_code=200)
async def report_room_activity(
    room_id: UUID,
    data: RoomActivityRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    room = await room_service.get_room(db, room_id)
    if not room:
        raise RoomNotFoundError()
    await _ensure_room_member(db, room_id, current_user.id)

    await _touch_room_activity(str(room_id))
    return {"ok": True, "activity_type": data.activity_type}


@router.get("/{room_id}/writing-doc", response_model=WritingDocStateResponse, status_code=200)
async def get_writing_doc_state(
    room_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    room = await room_service.get_room(db, room_id)
    if not room:
        raise RoomNotFoundError()
    await _ensure_room_member(db, room_id, current_user.id)

    state = await writing_doc_service.get_writing_doc_state(str(room_id))
    return WritingDocStateResponse.model_validate(state)


@router.get("/{room_id}/writing-doc/change-log", response_model=WritingDocChangeLogResponse, status_code=200)
async def get_writing_doc_change_log(
    room_id: UUID,
    limit: int = Query(30, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    room = await room_service.get_room(db, room_id)
    if not room:
        raise RoomNotFoundError()
    await _ensure_room_member(db, room_id, current_user.id)

    items = await writing_doc_service.get_writing_doc_change_log(str(room_id), limit=limit)
    return WritingDocChangeLogResponse.model_validate({"items": items})


@router.post("/{room_id}/writing-doc/save-version", response_model=WritingDocChangeLogResponse, status_code=200)
async def save_writing_doc_version(
    room_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    room = await room_service.get_room(db, room_id)
    if not room:
        raise RoomNotFoundError()
    await _ensure_room_member(db, room_id, current_user.id)
    if current_user.role != UserRole.student:
        raise HTTPException(status_code=403, detail="Only students can save writing versions")

    try:
        await writing_doc_service.save_writing_doc_version(
            str(room_id),
            saved_by=str(current_user.id),
            saved_by_display_name=current_user.display_name,
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Writing doc is empty")

    items = await writing_doc_service.get_writing_doc_change_log(str(room_id), limit=30)
    return WritingDocChangeLogResponse.model_validate({"items": items})


@router.post("/{room_id}/writing-doc/restore", status_code=410)
async def restore_writing_doc_version(
    room_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    room = await room_service.get_room(db, room_id)
    if not room:
        raise RoomNotFoundError()
    await _ensure_room_member(db, room_id, current_user.id)
    raise HTTPException(status_code=410, detail="Writing doc restore is disabled")


@router.get("/{room_id}/writing-submit", response_model=WritingSubmitStateResponse, status_code=200)
async def get_writing_submit_state(
    room_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    room = await room_service.get_room(db, room_id)
    if not room:
        raise RoomNotFoundError()
    await _ensure_room_member(db, room_id, current_user.id)

    state = await writing_submit_service.get_writing_submit_state(str(room_id))
    return WritingSubmitStateResponse.model_validate(state)


@router.post("/{room_id}/writing-submit/confirm", response_model=WritingSubmitStateResponse, status_code=200)
async def confirm_writing_submit(
    room_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    room = await room_service.get_room(db, room_id)
    if not room:
        raise RoomNotFoundError()
    await _ensure_room_member(db, room_id, current_user.id)
    if current_user.role != UserRole.student:
        raise HTTPException(status_code=403, detail="Only students can confirm writing submission")
    if not room.timer_started_at:
        raise HTTPException(
            status_code=400,
            detail={"message": "任务计时尚未开始，暂不能提交", "code": "TIMER_NOT_STARTED"},
        )

    writing_state = await writing_doc_service.get_writing_doc_state(str(room_id))
    plain_len = _plain_text_length(writing_state.get("content") or "")
    if plain_len < MIN_WRITING_SUBMIT_CHARS:
        raise HTTPException(
            status_code=400,
            detail={
                "message": f"写作内容至少需要 {MIN_WRITING_SUBMIT_CHARS} 字，当前 {plain_len} 字",
                "code": "WRITING_TOO_SHORT",
                "min_chars": MIN_WRITING_SUBMIT_CHARS,
                "current_chars": plain_len,
            },
        )

    state, finalized, action = await writing_submit_service.confirm_writing_submit(str(room_id), current_user)
    state["action"] = action
    if finalized:
        room = await room_service.stop_room_timer(db, room)
        await _publish_room_timer_update(room)

    await _touch_room_activity(str(room_id))
    await _publish_writing_submit_updated(str(room_id), state)
    return WritingSubmitStateResponse.model_validate(state)


@router.get("/{room_id}/task-script", status_code=200)
async def get_task_script_state(
    room_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    room = await room_service.get_room(db, room_id)
    if not room:
        raise RoomNotFoundError()
    await _ensure_room_member(db, room_id, current_user.id)

    return await task_script_service.get_task_script_state(db, room)


@router.get("/{room_id}/task-script/lock", status_code=200)
async def get_task_script_lock_state(
    room_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    room = await room_service.get_room(db, room_id)
    if not room:
        raise RoomNotFoundError()
    await _ensure_room_member(db, room_id, current_user.id)
    return await task_script_service.get_task_script_lock_state(str(room_id), current_user)


@router.post("/{room_id}/task-script/lock/acquire", status_code=200)
async def acquire_task_script_lock(
    room_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    room = await room_service.get_room(db, room_id)
    if not room:
        raise RoomNotFoundError()
    await _ensure_room_member(db, room_id, current_user.id)
    if current_user.role != UserRole.student:
        raise HTTPException(status_code=403, detail="Only students can edit task script proposals")
    return await task_script_service.acquire_task_script_lock(db, room, current_user)


@router.post("/{room_id}/task-script/lock/renew", status_code=200)
async def renew_task_script_lock(
    room_id: UUID,
    data: TaskScriptLeaseRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    room = await room_service.get_room(db, room_id)
    if not room:
        raise RoomNotFoundError()
    await _ensure_room_member(db, room_id, current_user.id)
    if current_user.role != UserRole.student:
        raise HTTPException(status_code=403, detail="Only students can edit task script proposals")
    return await task_script_service.renew_task_script_lock(str(room_id), current_user, data.lease_id)


@router.post("/{room_id}/task-script/lock/release", status_code=200)
async def release_task_script_lock(
    room_id: UUID,
    data: TaskScriptLeaseRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    room = await room_service.get_room(db, room_id)
    if not room:
        raise RoomNotFoundError()
    await _ensure_room_member(db, room_id, current_user.id)
    if current_user.role != UserRole.student:
        raise HTTPException(status_code=403, detail="Only students can edit task script proposals")
    return await task_script_service.release_task_script_lock(str(room_id), current_user, data.lease_id)


@router.post("/{room_id}/task-script/proposals/facilitator", status_code=200)
async def propose_task_script_by_facilitator(
    room_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    room = await room_service.get_room(db, room_id)
    if not room:
        raise RoomNotFoundError()
    await _ensure_room_member(db, room_id, current_user.id)
    return await task_script_service.propose_facilitator_update(db, room, current_user)


@router.post("/{room_id}/task-script/confirm", status_code=200)
async def confirm_task_script_proposal(
    room_id: UUID,
    data: TaskScriptConfirmRequest | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    room = await room_service.get_room(db, room_id)
    if not room:
        raise RoomNotFoundError()
    await _ensure_room_member(db, room_id, current_user.id)
    if current_user.role != UserRole.student:
        raise HTTPException(status_code=403, detail="仅学生可确认流程提案")

    overrides = (data.model_dump(exclude_none=True) if data else {})
    return await task_script_service.confirm_pending_proposal(
        db,
        room,
        current_user,
        overrides=overrides,
        proposal_id=(data.proposal_id if data else None),
        lease_id=(data.lease_id if data else None),
    )
