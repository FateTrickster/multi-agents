from __future__ import annotations

from datetime import datetime, timezone

from app.db.redis_client import get_redis_client
from app.models.user import User

WRITING_REQUIRED_CONFIRMATIONS = 1


def _writing_submit_key(room_id: str) -> str:
    return f"room:{room_id}:writing_submit_state"


def _normalize_state(raw_state) -> dict:
    state = raw_state if isinstance(raw_state, dict) else {}
    confirmations = state.get("confirmations")
    if not isinstance(confirmations, list):
        confirmations = []
    normalized = {
        "required_confirmations": WRITING_REQUIRED_CONFIRMATIONS,
        "confirmations": [item for item in confirmations if isinstance(item, dict)],
        "final_submitted_at": state.get("final_submitted_at"),
    }
    return normalized


async def get_writing_submit_state(room_id: str) -> dict:
    redis_client = get_redis_client()
    raw = await redis_client.get(_writing_submit_key(room_id))
    if not raw:
        return _normalize_state({})
    try:
        import json

        data = json.loads(raw)
    except Exception:
        return _normalize_state({})
    return _normalize_state(data)


async def clear_writing_submit_state(room_id: str) -> dict:
    redis_client = get_redis_client()
    await redis_client.delete(_writing_submit_key(room_id))
    return _normalize_state({})


async def confirm_writing_submit(room_id: str, current_user: User) -> tuple[dict, bool, str]:
    redis_client = get_redis_client()
    state = await get_writing_submit_state(room_id)

    if state.get("final_submitted_at"):
        return state, False, "finalized"

    user_id = str(current_user.id)
    already_confirmed = any(str(item.get("user_id")) == user_id for item in state["confirmations"])
    action = "unconfirmed" if already_confirmed else "confirmed"
    if already_confirmed:
        state["confirmations"] = [item for item in state["confirmations"] if str(item.get("user_id")) != user_id]
    else:
        state["confirmations"].append(
            {
                "user_id": user_id,
                "display_name": current_user.display_name,
                "confirmed_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    finalized = len(state["confirmations"]) >= WRITING_REQUIRED_CONFIRMATIONS
    if finalized and not state.get("final_submitted_at"):
        state["final_submitted_at"] = datetime.now(timezone.utc).isoformat()
        action = "finalized"

    import json

    await redis_client.set(_writing_submit_key(room_id), json.dumps(state, ensure_ascii=False))
    return state, finalized, action
