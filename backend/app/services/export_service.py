from __future__ import annotations

import csv
import io
import json
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.redis_client import get_redis_client
from app.models.analysis import AnalysisSnapshot
from app.models.message import Message, SenderType
from app.models.room import Room
from app.models.task import Task
from app.models.room_task_script import RoomTaskScript
from app.models.room_member import RoomMember
from app.models.user import User
from app.services.writing_snapshot_service import get_writing_snapshots


@dataclass
class ExportInclude:
    rooms: bool = True
    room_members: bool = True
    messages: bool = True
    agent_tasks: bool = True
    agent_mention_requests: bool = True
    agent_task_events: bool = True
    analysis_snapshots: bool = True
    writing_docs: bool = True
    writing_change_logs: bool = True
    writing_snapshots: bool = True
    task_definitions: bool = True
    task_flow_states: bool = True
    writing_submission_states: bool = True
    room_writing_html: bool = True


def _to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _iso(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _normalize_dt(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _stable_json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def _csv_bytes(rows: list[dict[str, Any]], fieldnames: list[str]) -> bytes:
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow({field: _to_text(row.get(field)) for field in fieldnames})
    return stream.getvalue().encode("utf-8-sig")


async def _matched_room_ids(
    db: AsyncSession,
    room_id: str | None,
    room_name_prefix: str | None,
    start_time: datetime | None,
    end_time: datetime | None,
    room_ids: list[str] | None = None,
) -> list[str]:
    start_time = _normalize_dt(start_time)
    end_time = _normalize_dt(end_time)

    stmt = select(Room.id).order_by(Room.created_at.asc())
    if room_ids is not None:
        if not room_ids:
            return []
        stmt = stmt.where(Room.id.in_([UUID(rid) for rid in room_ids]))
    if room_id:
        stmt = stmt.where(Room.id == UUID(room_id))
    if room_name_prefix:
        stmt = stmt.where(Room.name.like(f"{room_name_prefix}%"))
    if start_time:
        stmt = stmt.where(Room.created_at >= start_time)
    if end_time:
        stmt = stmt.where(Room.created_at <= end_time)
    rows = (await db.execute(stmt)).scalars().all()
    return [str(item) for item in rows]


async def list_export_rooms(
    db: AsyncSession,
    room_name_prefix: str | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    start_time = _normalize_dt(start_time)
    end_time = _normalize_dt(end_time)

    student_count_subq = (
        select(func.count(RoomMember.id))
        .where(RoomMember.room_id == Room.id)
        .scalar_subquery()
    )
    message_count_subq = (
        select(func.count(Message.id))
        .where(Message.room_id == Room.id)
        .scalar_subquery()
    )
    agent_message_count_subq = (
        select(func.count(Message.id))
        .where(Message.room_id == Room.id, Message.sender_type == SenderType.agent)
        .scalar_subquery()
    )

    stmt = (
        select(
            Room.id.label("room_id"),
            Room.name.label("name"),
            Room.created_at.label("created_at"),
            Room.status.label("status"),
            student_count_subq.label("student_count"),
            message_count_subq.label("message_count"),
            agent_message_count_subq.label("agent_message_count"),
        )
        .select_from(Room)
        .order_by(Room.created_at.desc())
        .limit(limit)
    )
    if room_name_prefix:
        stmt = stmt.where(Room.name.like(f"{room_name_prefix}%"))
    if start_time:
        stmt = stmt.where(Room.created_at >= start_time)
    if end_time:
        stmt = stmt.where(Room.created_at <= end_time)

    rows = (await db.execute(stmt)).mappings().all()
    redis_client = None
    try:
        redis_client = get_redis_client()
    except RuntimeError:
        redis_client = None

    result: list[dict[str, Any]] = []
    for row in rows:
        rid = str(row["room_id"])
        has_writing_doc = False
        if redis_client is not None:
            has_writing_doc = bool(await redis_client.exists(f"room:{rid}:writing_doc_state"))
        result.append(
            {
                "room_id": rid,
                "name": row["name"],
                "created_at": _iso(row["created_at"]),
                "status": str(row["status"]) if row["status"] is not None else "",
                "student_count": int(row["student_count"] or 0),
                "message_count": int(row["message_count"] or 0),
                "agent_message_count": int(row["agent_message_count"] or 0),
                "has_writing_doc": has_writing_doc,
            }
        )
    return result


async def build_export_payload(
    db: AsyncSession,
    *,
    room_id: str | None = None,
    room_name_prefix: str | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    include: ExportInclude | None = None,
    room_ids: list[str] | None = None,
) -> dict[str, Any]:
    include = include or ExportInclude()
    room_ids = await _matched_room_ids(db, room_id, room_name_prefix, start_time, end_time, room_ids)
    room_uuids = [UUID(rid) for rid in room_ids]

    rooms: list[dict[str, Any]] = []
    room_members: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    agent_tasks: list[dict[str, Any]] = []
    agent_mention_requests: list[dict[str, Any]] = []
    agent_task_events: list[dict[str, Any]] = []
    analysis_snapshots: list[dict[str, Any]] = []
    writing_docs: list[dict[str, Any]] = []
    writing_change_logs: list[dict[str, Any]] = []
    writing_snapshots: list[dict[str, Any]] = []
    task_definitions: list[dict[str, Any]] = []
    task_flow_states: list[dict[str, Any]] = []
    writing_submission_states: list[dict[str, Any]] = []
    room_writing_html: dict[str, str] = {}

    if room_ids and include.rooms:
        stmt = (
            select(
                Room.id,
                Room.name,
                Room.task_id,
                Room.created_by,
                Room.status,
                Room.created_at,
                Room.ended_at,
                Room.timer_started_at,
                Room.timer_deadline_at,
                Room.timer_stopped_at,
                Room.locked_member_ids,
                Room.agent_mode,
            )
            .where(Room.id.in_(room_uuids))
            .order_by(Room.created_at.asc())
        )
        for row in (await db.execute(stmt)).mappings().all():
            rooms.append(
                {
                    "room_id": str(row["id"]),
                    "name": row["name"],
                    "task_id": str(row["task_id"]) if row["task_id"] else "",
                    "created_by": str(row["created_by"]) if row["created_by"] else "",
                    "status": str(row["status"]) if row["status"] is not None else "",
                    "created_at": _iso(row["created_at"]),
                    "ended_at": _iso(row["ended_at"]),
                    "timer_started_at": _iso(row["timer_started_at"]),
                    "timer_deadline_at": _iso(row["timer_deadline_at"]),
                    "timer_stopped_at": _iso(row["timer_stopped_at"]),
                    "locked_member_ids": row["locked_member_ids"],
                    "agent_mode": row["agent_mode"],
                }
            )

    if room_ids and include.task_definitions:
        stmt = (
            select(Task.id, Task.title, Task.requirements, Task.scripts,
                   Task.discussion_template, Task.created_by)
            .join(Room, Room.task_id == Task.id)
            .where(Room.id.in_(room_uuids))
            .distinct()
        )
        for row in (await db.execute(stmt)).mappings().all():
            task_definitions.append({
                "task_id": str(row["id"]), "title": row["title"],
                "requirements": row["requirements"], "scripts": row["scripts"],
                "discussion_template": row["discussion_template"],
                "created_by": str(row["created_by"]),
            })

    if room_ids and include.task_flow_states:
        stmt = select(RoomTaskScript).where(RoomTaskScript.room_id.in_(room_uuids))
        for item in (await db.execute(stmt)).scalars().all():
            task_flow_states.append({
                "room_id": str(item.room_id),
                "task_id": str(item.task_id) if item.task_id else "",
                "scripts": item.scripts,
                "created_at": _iso(item.created_at),
                "updated_at": _iso(item.updated_at),
            })

    if room_ids and include.room_members:
        stmt = (
            select(
                RoomMember.id.label("room_member_id"),
                RoomMember.room_id,
                RoomMember.user_id,
                User.username,
                User.display_name,
                User.role.label("user_role"),
                RoomMember.joined_at,
            )
            .outerjoin(User, User.id == RoomMember.user_id)
            .where(RoomMember.room_id.in_(room_uuids))
            .order_by(RoomMember.joined_at.asc())
        )
        for row in (await db.execute(stmt)).mappings().all():
            room_members.append(
                {
                    "room_member_id": str(row["room_member_id"]),
                    "room_id": str(row["room_id"]),
                    "user_id": str(row["user_id"]) if row["user_id"] else "",
                    "username": row["username"] or "",
                    "display_name": row["display_name"] or "",
                    "user_role": str(row["user_role"]) if row["user_role"] is not None else "",
                    "joined_at": _iso(row["joined_at"]),
                }
            )

    if room_ids and include.messages:
        stmt = (
            select(
                Message.id.label("message_id"),
                Message.room_id,
                Message.seq_num,
                Message.sender_id,
                User.username.label("sender_username"),
                User.display_name.label("sender_name"),
                Message.sender_type,
                Message.agent_role,
                Message.content,
                Message.status.label("message_status"),
                Message.source_message_id,
                Message.source_display_name_snapshot,
                Message.source_content_preview_snapshot,
                Message.created_at,
            )
            .outerjoin(User, User.id == Message.sender_id)
            .where(Message.room_id.in_(room_uuids))
            .order_by(Message.room_id.asc(), Message.seq_num.asc())
        )
        for row in (await db.execute(stmt)).mappings().all():
            messages.append(
                {
                    "message_id": str(row["message_id"]),
                    "room_id": str(row["room_id"]),
                    "seq_num": int(row["seq_num"] or 0),
                    "sender_id": str(row["sender_id"]) if row["sender_id"] else "",
                    "sender_username": row["sender_username"] or "",
                    "sender_name": row["sender_name"] or "",
                    "sender_type": str(row["sender_type"]) if row["sender_type"] is not None else "",
                    "agent_role": row["agent_role"] or "",
                    "content": row["content"] or "",
                    "message_status": str(row["message_status"]) if row["message_status"] is not None else "",
                    "source_message_id": str(row["source_message_id"]) if row["source_message_id"] else "",
                    "source_display_name_snapshot": row["source_display_name_snapshot"] or "",
                    "source_content_preview_snapshot": row["source_content_preview_snapshot"] or "",
                    "created_at": _iso(row["created_at"]),
                }
            )

    if room_ids and include.analysis_snapshots:
        stmt = (
            select(
                AnalysisSnapshot.id.label("snapshot_id"),
                AnalysisSnapshot.room_id,
                AnalysisSnapshot.analyzed_message_count,
                AnalysisSnapshot.selected_agent_role,
                AnalysisSnapshot.should_intervene,
                AnalysisSnapshot.selected_strategy,
                AnalysisSnapshot.emotional_report,
                AnalysisSnapshot.emotion_flags,
                AnalysisSnapshot.cognitive_report,
                AnalysisSnapshot.behavioral_report,
                AnalysisSnapshot.social_report,
                AnalysisSnapshot.social_cps_report,
                AnalysisSnapshot.dispatcher_decision,
                AnalysisSnapshot.created_at,
            )
            .where(AnalysisSnapshot.room_id.in_(room_uuids))
            .order_by(AnalysisSnapshot.created_at.asc())
        )
        for row in (await db.execute(stmt)).mappings().all():
            analysis_snapshots.append(
                {
                    "snapshot_id": str(row["snapshot_id"]),
                    "room_id": str(row["room_id"]),
                    "analyzed_message_count": int(row["analyzed_message_count"] or 0),
                    "selected_agent_role": row["selected_agent_role"] or "",
                    "should_intervene": bool(row["should_intervene"]),
                    "selected_strategy": row["selected_strategy"] or "",
                    "emotional_report": row["emotional_report"],
                    "emotion_flags": row["emotion_flags"],
                    "cognitive_report": row["cognitive_report"],
                    "behavioral_report": row["behavioral_report"],
                    "social_report": row["social_report"],
                    "social_cps_report": row["social_cps_report"],
                    "dispatcher_decision": row["dispatcher_decision"],
                    "created_at": _iso(row["created_at"]),
                }
            )

    needs_redis = bool(room_ids and (
        include.agent_tasks or include.agent_mention_requests or include.agent_task_events or include.writing_docs or include.writing_change_logs
        or include.room_writing_html or include.writing_snapshots
        or include.writing_submission_states
    ))
    redis_client = get_redis_client() if needs_redis else None

    if room_ids and redis_client is not None:
        if include.agent_tasks:
            room_set = set(room_ids)
            async for key in redis_client.scan_iter(match="agent:task:*", count=1000):
                payload = await redis_client.hgetall(key)
                if not payload:
                    continue
                if str(payload.get("room_id") or "") not in room_set:
                    continue
                agent_tasks.append(
                    {
                        "task_id": str(payload.get("task_id") or ""),
                        "room_id": str(payload.get("room_id") or ""),
                        "trigger_type": str(payload.get("trigger_type") or ""),
                        "agent_role": str(payload.get("agent_role") or ""),
                        "status": str(payload.get("status") or ""),
                        "reason": str(payload.get("reason") or ""),
                        "error": str(payload.get("error") or ""),
                        "drop_reason": str(payload.get("drop_reason") or ""),
                        "queued_at": str(payload.get("queued_at") or ""),
                        "running_at": str(payload.get("running_at") or ""),
                        "finished_at": str(payload.get("finished_at") or ""),
                        "source_message_id": str(payload.get("source_message_id") or ""),
                        "created_at": str(payload.get("created_at") or ""),
                    }
                )

        if include.agent_task_events:
            for rid in room_ids:
                for raw in await redis_client.lrange(f"agent:task_events:{rid}", 0, -1):
                    try:
                        event = json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(event, dict):
                        agent_task_events.append({"room_id": rid, **event})

        if include.agent_mention_requests:
            room_set = set(room_ids)
            async for key in redis_client.scan_iter(match="agent:mention_entry:*", count=1000):
                entry = await redis_client.hgetall(key)
                if not entry or entry.get("room_id") not in room_set:
                    continue
                status = await redis_client.hgetall(f"agent:mention_entry_status:{entry.get('entry_id')}")
                agent_mention_requests.append({
                    "room_id": entry.get("room_id"),
                    "entry_id": entry.get("entry_id"),
                    "agent_role": entry.get("agent_role"),
                    "source_message_id": entry.get("source_message_id"),
                    "student_name": entry.get("student_name"),
                    "reason": entry.get("reason"),
                    "strategy": entry.get("strategy"),
                    "trigger_type": entry.get("trigger_type"),
                    "created_at": entry.get("created_at"),
                    "status": status.get("status"),
                    "status_reason": status.get("reason"),
                    "task_id": status.get("task_id"),
                    "error": status.get("error"),
                    "updated_at": status.get("updated_at"),
                })

        if include.writing_docs or include.writing_change_logs or include.room_writing_html or include.writing_snapshots or include.writing_submission_states:
            for rid in room_ids:
                state_raw = await redis_client.get(f"room:{rid}:writing_doc_state")
                if state_raw:
                    try:
                        state = json.loads(state_raw)
                    except json.JSONDecodeError:
                        state = {"raw": state_raw, "content": ""}

                    content = str(state.get("content") or "")
                    if include.writing_docs:
                        writing_docs.append(
                            {
                                "room_id": rid,
                                "content_html": content,
                                "version": int(state.get("version") or 0),
                                "updated_at": str(state.get("updated_at") or ""),
                                "updated_by": str(state.get("updated_by") or ""),
                                "updated_by_display_name": str(state.get("updated_by_display_name") or ""),
                                "content_text_preview": content.replace("\n", " ")[:120],
                            }
                        )
                    if include.room_writing_html:
                        room_writing_html[rid] = content

                if include.writing_submission_states:
                    submission_raw = await redis_client.get(f"room:{rid}:writing_submit_state")
                    if submission_raw:
                        try:
                            submission = json.loads(submission_raw)
                        except (ValueError, TypeError):
                            submission = {"raw": submission_raw}
                        writing_submission_states.append({
                            "room_id": rid,
                            "required_confirmations": submission.get("required_confirmations"),
                            "confirmations": submission.get("confirmations"),
                            "final_submitted_at": submission.get("final_submitted_at"),
                        })
                if include.writing_snapshots:
                    writing_snapshots.extend(await get_writing_snapshots(rid))
                if include.writing_change_logs:
                    log_items = await redis_client.lrange(f"room:{rid}:writing_doc_change_log", 0, -1)
                    for raw in log_items:
                        try:
                            item = json.loads(raw)
                        except json.JSONDecodeError:
                            item = {"raw": raw}
                        writing_change_logs.append(
                            {
                                "room_id": rid,
                                "action": str(item.get("action") or ""),
                                "at": str(item.get("at") or ""),
                                "actor_id": str(item.get("actor_id") or ""),
                                "actor_display_name": str(item.get("actor_display_name") or ""),
                                "version": str(item.get("version") or ""),
                                "summary": str(item.get("summary") or ""),
                                "delta_chars": str(item.get("delta_chars") or ""),
                            }
                        )

    files = []
    if include.rooms:
        files.append("rooms.csv")
    if include.room_members:
        files.append("room_members.csv")
    if include.messages:
        files.append("messages.csv")
    if include.agent_tasks:
        files.append("agent_tasks.csv")
    if include.agent_mention_requests:
        files.append("agent_mention_requests.csv")
    if include.agent_task_events:
        files.append("agent_task_events.csv")
    if include.analysis_snapshots:
        files.append("analysis_snapshots.csv")
    if include.task_definitions:
        files.append("task_definitions.csv")
    if include.task_flow_states:
        files.append("task_flow_states.csv")
    if include.writing_submission_states:
        files.append("writing_submission_states.csv")
    if include.writing_docs:
        files.append("writing_docs.csv")
    if include.writing_snapshots:
        files.append("writing_snapshots_5min.csv")
    if include.writing_change_logs:
        files.append("writing_change_logs.csv")
    if include.room_writing_html:
        files.extend([f"room_writing_{rid}.html" for rid in room_writing_html.keys()])
    files.append("export_summary.json")

    return {
        "filters": {
            "room_id": room_id,
            "room_name_prefix": room_name_prefix,
            "start_time": _iso(start_time),
            "end_time": _iso(end_time),
        },
        "matched_room_ids": room_ids,
        "counts": {
            "rooms": len(rooms),
            "room_members": len(room_members),
            "messages": len(messages),
            "agent_tasks": len(agent_tasks),
            "agent_mention_requests": len(agent_mention_requests),
            "agent_task_events": len(agent_task_events),
            "analysis_snapshots": len(analysis_snapshots),
            "writing_docs": len(writing_docs),
            "writing_change_logs": len(writing_change_logs),
            "writing_snapshots": len(writing_snapshots),
            "task_definitions": len(task_definitions),
            "task_flow_states": len(task_flow_states),
            "writing_submission_states": len(writing_submission_states),
        },
        "estimated_files": files,
        "data": {
            "rooms": rooms,
            "room_members": room_members,
            "messages": messages,
            "agent_tasks": agent_tasks,
            "agent_mention_requests": agent_mention_requests,
            "agent_task_events": agent_task_events,
            "analysis_snapshots": analysis_snapshots,
            "writing_docs": writing_docs,
            "writing_change_logs": writing_change_logs,
            "writing_snapshots": writing_snapshots,
            "task_definitions": task_definitions,
            "task_flow_states": task_flow_states,
            "writing_submission_states": writing_submission_states,
            "room_writing_html": room_writing_html,
        },
    }


def build_export_zip_bytes(payload: dict[str, Any]) -> bytes:
    rows = payload["data"]
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "export_summary.json",
            _stable_json_bytes(
                {
                    "ok": True,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "filters": payload["filters"],
                    "matched_room_ids": payload["matched_room_ids"],
                    "counts": payload["counts"],
                    "estimated_files": payload["estimated_files"],
                    "encoding": {"csv": "utf-8-sig", "json": "utf-8"},
                }
            ),
        )
        zf.writestr(
            "rooms.csv",
            _csv_bytes(
                rows["rooms"],
                [
                    "room_id",
                    "name",
                    "task_id",
                    "created_by",
                    "status",
                    "created_at",
                    "ended_at",
                    "timer_started_at",
                    "timer_deadline_at",
                    "timer_stopped_at",
                    "locked_member_ids",
                    "agent_mode",
                ],
            ),
        )
        zf.writestr(
            "room_members.csv",
            _csv_bytes(
                rows["room_members"],
                ["room_member_id", "room_id", "user_id", "username", "display_name", "user_role", "joined_at"],
            ),
        )
        zf.writestr(
            "messages.csv",
            _csv_bytes(
                rows["messages"],
                [
                    "message_id",
                    "room_id",
                    "seq_num",
                    "sender_id",
                    "sender_username",
                    "sender_name",
                    "sender_type",
                    "agent_role",
                    "content",
                    "message_status",
                    "source_message_id",
                    "source_display_name_snapshot",
                    "source_content_preview_snapshot",
                    "created_at",
                ],
            ),
        )
        zf.writestr(
            "agent_tasks.csv",
            _csv_bytes(
                rows["agent_tasks"],
                [
                    "task_id",
                    "room_id",
                    "trigger_type",
                    "agent_role",
                    "status",
                    "reason",
                    "error",
                    "drop_reason",
                    "queued_at",
                    "running_at",
                    "finished_at",
                    "source_message_id",
                    "created_at",
                ],
            ),
        )
        zf.writestr(
            "agent_task_events.csv",
            _csv_bytes(rows.get("agent_task_events", []), [
                "room_id", "task_id", "at", "agent_role", "trigger_type",
                "status", "reason", "source_message_id", "queued_at",
                "running_at", "finished_at", "error", "drop_reason", "created_at",
            ]),
        )
        zf.writestr(
            "agent_mention_requests.csv",
            _csv_bytes(rows.get("agent_mention_requests", []), [
                "room_id", "entry_id", "agent_role", "source_message_id",
                "student_name", "reason", "strategy", "trigger_type",
                "created_at", "status", "status_reason", "task_id", "error", "updated_at",
            ]),
        )
        zf.writestr(
            "analysis_snapshots.csv",
            _csv_bytes(
                rows["analysis_snapshots"],
                [
                    "snapshot_id",
                    "room_id",
                    "analyzed_message_count",
                    "selected_agent_role",
                    "should_intervene",
                    "selected_strategy",
                    "emotional_report",
                    "emotion_flags",
                    "cognitive_report",
                    "behavioral_report",
                    "social_report",
                    "social_cps_report",
                    "dispatcher_decision",
                    "created_at",
                ],
            ),
        )
        for filename, fields in (
            ("task_definitions.csv", ["task_id", "title", "requirements", "scripts", "discussion_template", "created_by"]),
            ("task_flow_states.csv", ["room_id", "task_id", "scripts", "created_at", "updated_at"]),
            ("writing_submission_states.csv", ["room_id", "required_confirmations", "confirmations", "final_submitted_at"]),
        ):
            zf.writestr(filename, _csv_bytes(rows.get(filename.removesuffix(".csv"), []), fields))
        zf.writestr(
            "writing_docs.csv",
            _csv_bytes(
                rows["writing_docs"],
                [
                    "room_id",
                    "content_html",
                    "version",
                    "updated_at",
                    "updated_by",
                    "updated_by_display_name",
                    "content_text_preview",
                ],
            ),
        )
        zf.writestr(
            "writing_snapshots_5min.csv",
            _csv_bytes(
                rows.get("writing_snapshots", []),
                ["room_id", "elapsed_minutes", "scheduled_at", "captured_at", "version", "updated_at", "updated_by", "content_html", "content_text", "text_length"],
            ),
        )
        zf.writestr(
            "writing_change_logs.csv",
            _csv_bytes(
                rows["writing_change_logs"],
                ["room_id", "action", "at", "actor_id", "actor_display_name", "version", "summary", "delta_chars"],
            ),
        )
        for room_id, html in rows["room_writing_html"].items():
            zf.writestr(f"room_writing_{room_id}.html", (html or "").encode("utf-8"))
    return out.getvalue()
