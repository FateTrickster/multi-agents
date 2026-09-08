import json

import pytest

from app.agents.committee import BasicCommittee


@pytest.fixture(autouse=True)
def _use_multi_agent_mode(monkeypatch):
    async def _mode_multi(_room_id):
        return "multi"

    monkeypatch.setattr("app.agents.committee.get_room_agent_mode", _mode_multi)


class _FakeRedis:
    def __init__(self):
        self.published = []

    async def publish(self, channel, payload):
        self.published.append((channel, payload))


@pytest.mark.asyncio
async def test_committee_emits_analysis_update_and_queue(monkeypatch):
    committee = BasicCommittee()
    enqueued = []
    fake_redis = _FakeRedis()

    async def _fake_messages(_room_id, limit=50):
        _ = limit
        return [
            {"sender_type": "student", "sender_id": "u1", "content": "我们先定义边界"},
            {"sender_type": "student", "sender_id": "u1", "content": "我同意"},
        ]

    async def _fake_members(_room_id):
        return [{"id": "u1", "display_name": "A"}, {"id": "u2", "display_name": "B"}]

    async def _fake_context(_room_id):
        return {"current_phase": "中期", "phase_goal": "推进", "elapsed_minutes": 40, "recent_interventions": []}

    async def _fake_enqueue(room_id, payload):
        enqueued.append((room_id, payload))

    monkeypatch.setattr("app.agents.committee.get_recent_messages", _fake_messages)
    monkeypatch.setattr("app.agents.committee.get_room_members", _fake_members)
    monkeypatch.setattr("app.agents.committee.get_room_context", _fake_context)
    monkeypatch.setattr("app.agents.committee.enqueue_task", _fake_enqueue)
    monkeypatch.setattr("app.agents.committee.get_redis_client", lambda: fake_redis)

    async def _no_db_snapshot(**_kwargs):
        return "snap-1"

    monkeypatch.setattr(committee, "_save_snapshot", _no_db_snapshot)

    await committee.analyze_and_dispatch("room-x")

    assert len(fake_redis.published) == 1
    channel, payload = fake_redis.published[0]
    assert channel == "room:room-x"
    data = json.loads(payload)
    assert data["type"] == "analysis:update"
    assert "behavioral_report" in data
    assert "social_report" in data
    assert "social_cps_report" in data
    # Depending on computed scores there may or may not be intervention; if yes, only one.
    assert len(enqueued) <= 1
