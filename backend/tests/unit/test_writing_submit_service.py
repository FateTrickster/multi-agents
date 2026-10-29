import uuid

import pytest

from app.models.user import User, UserRole
from app.services import writing_submit_service


class _FakeRedis:
    def __init__(self):
        self._store = {}

    async def get(self, key):
        return self._store.get(key)

    async def set(self, key, value):
        self._store[key] = value

    async def delete(self, key):
        self._store.pop(key, None)


def _build_user(name: str) -> User:
    return User(
        id=uuid.uuid4(),
        username=name,
        password_hash="x",
        display_name=name,
        role=UserRole.student,
    )


@pytest.mark.asyncio
async def test_one_student_can_finalize(monkeypatch):
    fake_redis = _FakeRedis()
    monkeypatch.setattr(writing_submit_service, "get_redis_client", lambda: fake_redis)
    student = _build_user("s1")
    state, finalized, action = await writing_submit_service.confirm_writing_submit("room-1", student)
    assert finalized is True
    assert action == "finalized"
    assert state["required_confirmations"] == 1
    assert len(state["confirmations"]) == 1
    assert state["final_submitted_at"] is not None
    again, again_finalized, again_action = await writing_submit_service.confirm_writing_submit("room-1", student)
    assert again_finalized is False
    assert again_action == "finalized"
    assert again["final_submitted_at"] == state["final_submitted_at"]


@pytest.mark.asyncio
async def test_clear_writing_submit_state_resets(monkeypatch):
    fake_redis = _FakeRedis()
    monkeypatch.setattr(writing_submit_service, "get_redis_client", lambda: fake_redis)
    room_id = "room-2"
    student = _build_user("s1")

    await writing_submit_service.confirm_writing_submit(room_id, student)
    state = await writing_submit_service.clear_writing_submit_state(room_id)

    assert state["confirmations"] == []
    assert state["final_submitted_at"] is None


@pytest.mark.asyncio
async def test_confirm_writing_submit_can_toggle_before_finalized(monkeypatch):
    fake_redis = _FakeRedis()
    monkeypatch.setattr(writing_submit_service, "get_redis_client", lambda: fake_redis)
    room_id = "room-3"
    student = _build_user("s1")

    state1, finalized1, action1 = await writing_submit_service.confirm_writing_submit(room_id, student)
    state2, finalized2, action2 = await writing_submit_service.confirm_writing_submit(room_id, student)

    assert finalized1 is True
    assert action1 == "finalized"
    assert len(state1["confirmations"]) == 1
    assert finalized2 is False
    assert action2 == "finalized"
    assert len(state2["confirmations"]) == 1
