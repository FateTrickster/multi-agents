from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException

from app.agents.context_builder import get_recent_messages, get_room_context
from app.agents.llm_client import stream_completion
from app.agents.settings import get_agent_settings
from app.db.redis_client import get_redis_client, mark_room_active
from app.models.room import Room
from app.models.user import User
from app.services import room_service, task_service
from app.services.room_task_script_service import get_or_create_room_task_script, normalize_room_task_scripts

LOCK_TTL_SECONDS = 120
REQUIRED_STUDENT_CONFIRMATIONS = 1


def _to_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, ensure_ascii=False).strip()
    except Exception:
        return str(value).strip()


def _normalize_scripts(raw_scripts) -> dict:
    if isinstance(raw_scripts, dict):
        history = raw_scripts.get("history")
        pending = raw_scripts.get("pending_proposal")
        return {
            "current_status": _to_text(raw_scripts.get("current_status")),
            "next_goal": _to_text(raw_scripts.get("next_goal")),
            "history": history if isinstance(history, list) else [],
            "pending_proposal": pending if isinstance(pending, dict) else None,
        }

    if isinstance(raw_scripts, str):
        return {
            "current_status": raw_scripts.strip(),
            "next_goal": "",
            "history": [],
            "pending_proposal": None,
        }

    return {
        "current_status": "",
        "next_goal": "",
        "history": [],
        "pending_proposal": None,
    }


def _lock_key(room_id: str) -> str:
    return f"task_script_lock:{room_id}"


def _parse_lock_payload(raw_value: str | None) -> dict | None:
    if not raw_value:
        return None
    try:
        data = json.loads(raw_value)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _format_lock_public(data: dict | None, current_user_id: str | None = None) -> dict:
    if not data:
        return {
            "locked": False,
            "owner_user_id": None,
            "owner_display_name": None,
            "proposal_id": None,
            "expires_at": None,
            "is_mine": False,
        }
    owner_id = str(data.get("user_id") or "")
    return {
        "locked": True,
        "owner_user_id": owner_id or None,
        "owner_display_name": data.get("display_name") or None,
        "proposal_id": data.get("proposal_id") or None,
        "expires_at": data.get("expires_at") or None,
        "is_mine": bool(current_user_id and owner_id and current_user_id == owner_id),
    }


async def _broadcast_task_script_updated(room_id: str, reason: str) -> None:
    try:
        redis_client = get_redis_client()
        payload = {
            "type": "task_script:updated",
            "room_id": room_id,
            "reason": reason,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        await redis_client.publish(f"room:{room_id}", json.dumps(payload, ensure_ascii=False))
    except Exception:
        # State is already persisted; broadcast failure should not break core flow.
        return


async def _touch_room_activity(room_id: str) -> None:
    try:
        await mark_room_active(room_id, client=get_redis_client())
    except Exception:
        return


async def _get_lock_raw(room_id: str) -> dict | None:
    redis_client = get_redis_client()
    raw = await redis_client.get(_lock_key(room_id))
    return _parse_lock_payload(raw)


def _build_lock_payload(user: User, proposal_id: str, lease_id: str) -> dict:
    expires_at = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() + LOCK_TTL_SECONDS,
        tz=timezone.utc,
    ).isoformat()
    return {
        "user_id": str(user.id),
        "display_name": user.display_name,
        "proposal_id": proposal_id,
        "lease_id": lease_id,
        "acquired_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": expires_at,
    }


def _extract_json_object(raw_text: str) -> dict:
    text = (raw_text or "").strip()
    if not text:
        raise ValueError("empty model output")

    if "```" in text:
        parts = text.split("```")
        for block in parts:
            block = block.strip()
            if block.startswith("json"):
                block = block[4:].strip()
            if block.startswith("{") and block.endswith("}"):
                return json.loads(block)

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start : end + 1])

    raise ValueError("cannot parse json object from model output")


async def get_task_script_state(db, room: Room) -> dict:
    if not room:
        return {
            "task_id": None,
            "current_status": "",
            "next_goal": "",
            "pending_proposal": None,
            "history": [],
        }
    room_script = await get_or_create_room_task_script(db, room)
    state = normalize_room_task_scripts(room_script.scripts)
    return {
        "task_id": str(room.task_id) if room.task_id else None,
        "current_status": state["current_status"],
        "next_goal": state["next_goal"],
        "pending_proposal": state["pending_proposal"],
        "history": state["history"],
    }


async def get_task_script_lock_state(room_id: str, current_user: User) -> dict:
    lock_data = await _get_lock_raw(room_id)
    return _format_lock_public(lock_data, str(current_user.id))


async def _generate_facilitator_proposal(room_id: str) -> dict:
    context = await get_room_context(room_id)
    history = await get_recent_messages(room_id, limit=30)

    system_prompt = (
        "You are the facilitator for a student collaboration room. "
        "Generate a concise workflow update proposal for the task panel. "
        "Output strict JSON only with keys: current_status, next_goal, change_reason."
    )
    messages = [
        {"role": "user", "content": f"Task description: {context.get('task_description', '')}"},
        {"role": "user", "content": f"Task workflow: {context.get('task_workflow', '')}"},
        {"role": "user", "content": f"Members: {context.get('members_info', '')}"},
    ]
    for msg in history[-20:]:
        messages.append({"role": "user", "content": f"[{msg['display_name']}]: {msg['content']}"})
    messages.append(
        {
            "role": "user",
            "content": "Return JSON now. Keep each field actionable and concise.",
        }
    )

    model = get_agent_settings().models.role_agents.model_version
    text = ""
    async for token in stream_completion(
        system_prompt=system_prompt,
        messages=messages,
        model=model,
        max_tokens=300,
    ):
        text += token

    payload = _extract_json_object(text)
    current_status = _to_text(payload.get("current_status"))
    next_goal = _to_text(payload.get("next_goal"))
    change_reason = _to_text(payload.get("change_reason"))
    if not current_status or not next_goal:
        raise ValueError("facilitator proposal missing required fields")
    return {
        "current_status": current_status,
        "next_goal": next_goal,
        "change_reason": change_reason,
    }


async def propose_facilitator_update(db, room: Room, current_user: User) -> dict:
    if not room.task_id:
        raise HTTPException(status_code=400, detail="Room has no bound task")
    task = await task_service.get_task(db, room.task_id)
    if task is None:
        raise HTTPException(status_code=400, detail="Room has no bound task")

    room_script = await get_or_create_room_task_script(db, room)
    base = normalize_room_task_scripts(room_script.scripts)
    pending = base["pending_proposal"]
    room_id = str(room.id)
    if pending:
        lock_data = await _get_lock_raw(room_id)
        if lock_data and str(lock_data.get("user_id")) != str(current_user.id):
            raise HTTPException(
                status_code=409,
                detail="Pending proposal is being edited by another student",
            )
        raise HTTPException(status_code=409, detail="There is already a pending proposal to confirm")

    generated = await _generate_facilitator_proposal(room_id)
    proposal = {
        "id": str(uuid.uuid4()),
        "agent_role": "facilitator",
        "proposed_at": datetime.now(timezone.utc).isoformat(),
        "requested_by": str(current_user.id),
        "current_status": generated["current_status"],
        "next_goal": generated["next_goal"],
        "change_reason": generated["change_reason"],
        "required_confirmations": REQUIRED_STUDENT_CONFIRMATIONS,
        "confirmations": [],
    }
    room_script.scripts = {
        "current_status": base["current_status"],
        "next_goal": base["next_goal"],
        "history": base["history"],
        "pending_proposal": proposal,
    }
    await db.commit()
    await db.refresh(room_script)
    await _touch_room_activity(room_id)
    await _broadcast_task_script_updated(room_id, reason="proposal_created")
    return await get_task_script_state(db, room)


async def acquire_task_script_lock(db, room: Room, current_user: User) -> dict:
    if not room.task_id:
        raise HTTPException(status_code=400, detail="Room has no bound task")
    task = await task_service.get_task(db, room.task_id)
    if task is None:
        raise HTTPException(status_code=400, detail="Room has no bound task")
    room_script = await get_or_create_room_task_script(db, room)
    state = normalize_room_task_scripts(room_script.scripts)
    pending = state["pending_proposal"]
    if not pending:
        raise HTTPException(status_code=400, detail="No pending proposal to edit")

    room_id = str(room.id)
    proposal_id = str(pending.get("id") or "")
    redis_client = get_redis_client()
    key = _lock_key(room_id)

    existing = await _get_lock_raw(room_id)
    if existing:
        if (
            str(existing.get("user_id")) == str(current_user.id)
            and str(existing.get("proposal_id")) == proposal_id
        ):
            lease_id = str(existing.get("lease_id") or str(uuid.uuid4()))
            payload = _build_lock_payload(current_user, proposal_id=proposal_id, lease_id=lease_id)
            await redis_client.setex(key, LOCK_TTL_SECONDS, json.dumps(payload, ensure_ascii=False))
            await _touch_room_activity(room_id)
            await _broadcast_task_script_updated(room_id, reason="lock_acquired")
            return {
                "acquired": True,
                "lease_id": lease_id,
                "lock": _format_lock_public(payload, str(current_user.id)),
            }

        return {
            "acquired": False,
            "lease_id": None,
            "lock": _format_lock_public(existing, str(current_user.id)),
        }

    lease_id = str(uuid.uuid4())
    payload = _build_lock_payload(current_user, proposal_id=proposal_id, lease_id=lease_id)
    acquired = await redis_client.set(key, json.dumps(payload, ensure_ascii=False), nx=True, ex=LOCK_TTL_SECONDS)
    if not acquired:
        latest = await _get_lock_raw(room_id)
        return {
            "acquired": False,
            "lease_id": None,
            "lock": _format_lock_public(latest, str(current_user.id)),
        }
    await _touch_room_activity(room_id)
    await _broadcast_task_script_updated(room_id, reason="lock_acquired")
    return {
        "acquired": True,
        "lease_id": lease_id,
        "lock": _format_lock_public(payload, str(current_user.id)),
    }


async def renew_task_script_lock(room_id: str, current_user: User, lease_id: str) -> dict:
    redis_client = get_redis_client()
    key = _lock_key(room_id)
    existing = await _get_lock_raw(room_id)
    if not existing:
        raise HTTPException(status_code=409, detail="Lock no longer exists")
    if str(existing.get("user_id")) != str(current_user.id):
        raise HTTPException(status_code=409, detail="Lock is owned by another user")
    if str(existing.get("lease_id")) != str(lease_id):
        raise HTTPException(status_code=409, detail="Lease id mismatch")

    proposal_id = str(existing.get("proposal_id") or "")
    payload = _build_lock_payload(current_user, proposal_id=proposal_id, lease_id=lease_id)
    await redis_client.setex(key, LOCK_TTL_SECONDS, json.dumps(payload, ensure_ascii=False))
    await _touch_room_activity(room_id)
    return {"renewed": True, "lease_id": lease_id, "lock": _format_lock_public(payload, str(current_user.id))}


async def release_task_script_lock(room_id: str, current_user: User, lease_id: str) -> dict:
    redis_client = get_redis_client()
    key = _lock_key(room_id)
    existing = await _get_lock_raw(room_id)
    if not existing:
        return {"released": True, "lock": _format_lock_public(None, str(current_user.id))}
    if str(existing.get("user_id")) != str(current_user.id):
        raise HTTPException(status_code=409, detail="Lock is owned by another user")
    if str(existing.get("lease_id")) != str(lease_id):
        raise HTTPException(status_code=409, detail="Lease id mismatch")

    await redis_client.delete(key)
    await _touch_room_activity(room_id)
    await _broadcast_task_script_updated(room_id, reason="lock_released")
    return {"released": True, "lock": _format_lock_public(None, str(current_user.id))}


async def confirm_pending_proposal(
    db,
    room: Room,
    current_user: User,
    overrides: dict | None = None,
    proposal_id: str | None = None,
    lease_id: str | None = None,
) -> dict:
    if not room.task_id:
        raise HTTPException(status_code=400, detail="Room has no bound task")
    task = await task_service.get_task(db, room.task_id)
    if task is None:
        raise HTTPException(status_code=400, detail="Room has no bound task")

    room_script = await get_or_create_room_task_script(db, room)
    base = normalize_room_task_scripts(room_script.scripts)
    pending = base["pending_proposal"]
    if not pending:
        raise HTTPException(status_code=400, detail="No pending proposal to confirm")

    pending_id = str(pending.get("id") or "")
    if not proposal_id:
        raise HTTPException(status_code=400, detail="proposal_id is required")
    if str(proposal_id) != pending_id:
        raise HTTPException(status_code=409, detail="Proposal has changed, please refresh")
    room_id = str(room.id)
    lock_data = await _get_lock_raw(room_id)
    if not lock_data:
        raise HTTPException(status_code=409, detail="Please acquire edit lock before confirming")
    if str(lock_data.get("proposal_id")) != pending_id:
        raise HTTPException(status_code=409, detail="Proposal lock mismatch, please refresh")
    lock_owned_by_current_user = str(lock_data.get("user_id")) == str(current_user.id)
    if not lock_owned_by_current_user:
        raise HTTPException(status_code=409, detail="Only current editor can confirm this proposal")
    if str(lock_data.get("lease_id")) != str(lease_id or ""):
        raise HTTPException(status_code=409, detail="Lease id mismatch")

    overrides = overrides or {}
    final_current_status = _to_text(pending.get("current_status"))
    final_next_goal = _to_text(pending.get("next_goal"))
    override_current_status = _to_text(overrides.get("current_status"))
    override_next_goal = _to_text(overrides.get("next_goal"))
    if override_current_status:
        final_current_status = override_current_status
    if override_next_goal:
        final_next_goal = override_next_goal

    student_feedback = _to_text(overrides.get("student_feedback"))

    if not final_current_status or not final_next_goal:
        raise HTTPException(status_code=400, detail="Confirm content cannot be empty")

    original_current_status = _to_text(pending.get("current_status"))
    original_next_goal = _to_text(pending.get("next_goal"))
    student_adjusted = final_current_status != original_current_status or final_next_goal != original_next_goal

    now_iso = datetime.now(timezone.utc).isoformat()
    confirmations = pending.get("confirmations")
    if not isinstance(confirmations, list):
        confirmations = []

    if any(str(item.get("user_id")) == str(current_user.id) for item in confirmations if isinstance(item, dict)):
        raise HTTPException(status_code=409, detail="You have already confirmed this proposal")

    confirmations.append(
        {
            "user_id": str(current_user.id),
            "display_name": current_user.display_name,
            "confirmed_at": now_iso,
            "student_feedback": student_feedback,
        }
    )

    total_confirmations = len(confirmations)

    if total_confirmations < REQUIRED_STUDENT_CONFIRMATIONS:
        pending["current_status"] = final_current_status
        pending["next_goal"] = final_next_goal
        pending["confirmations"] = confirmations
        pending["required_confirmations"] = REQUIRED_STUDENT_CONFIRMATIONS
        pending["latest_confirmed_at"] = now_iso

        room_script.scripts = {
            "current_status": base["current_status"],
            "next_goal": base["next_goal"],
            "history": base["history"],
            "pending_proposal": pending,
        }
        await db.commit()
        await db.refresh(room_script)

        if lock_owned_by_current_user:
            try:
                await release_task_script_lock(room_id, current_user, str(lease_id or ""))
            except HTTPException:
                pass
        await _touch_room_activity(room_id)
        await _broadcast_task_script_updated(room_id, reason="proposal_confirmation_progress")
        return await get_task_script_state(db, room)

    new_history = list(base["history"])
    new_history.append(
        {
            "id": pending_id or str(uuid.uuid4()),
            "agent_role": pending.get("agent_role") or "facilitator",
            "confirmed_at": now_iso,
            "confirmed_by": [item.get("user_id") for item in confirmations if isinstance(item, dict)],
            "confirmations": confirmations,
            "required_confirmations": REQUIRED_STUDENT_CONFIRMATIONS,
            "current_status": final_current_status,
            "next_goal": final_next_goal,
            "change_reason": _to_text(pending.get("change_reason")),
            "facilitator_suggested_current_status": original_current_status,
            "facilitator_suggested_next_goal": original_next_goal,
            "student_adjusted": student_adjusted,
        }
    )

    room_script.scripts = {
        "current_status": final_current_status,
        "next_goal": final_next_goal,
        "history": new_history[-100:],
        "pending_proposal": None,
    }

    await db.commit()
    await db.refresh(room_script)

    try:
        if lock_owned_by_current_user:
            await release_task_script_lock(room_id, current_user, str(lease_id or ""))
    except HTTPException:
        # Confirmation already succeeded; lock release conflict should not fail the main write.
        pass
    await _touch_room_activity(room_id)
    await _broadcast_task_script_updated(room_id, reason="proposal_confirmed")
    return await get_task_script_state(db, room)
