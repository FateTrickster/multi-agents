import uuid
from datetime import datetime, timezone

import pytest

from app.agents import agent_worker
from app.agents import queue as agent_queue
from app.analysis import scheduler, triggers
from app.models.room import Room
from app.models.user import User, UserRole
from app.routers import rooms as rooms_router
from app.websocket import handlers
from tests.conftest import FakeExecuteResult


class _FakeRedisQueue:
    def __init__(self):
        self.zadd_calls = []
        self.hset_calls = []
        self.expire_calls = []
        self.publish_calls = []

    async def zadd(self, key, mapping):
        self.zadd_calls.append((key, mapping))

    async def hset(self, key, mapping):
        self.hset_calls.append((key, dict(mapping)))

    async def expire(self, key, ttl):
        self.expire_calls.append((key, ttl))

    async def publish(self, channel, message):
        self.publish_calls.append((channel, message))

    async def zcard(self, _key):
        return 0


class _FakeRedisWorker:
    def __init__(self):
        self.values = {}
        self.hashes = {}

    async def exists(self, _key):
        return False

    async def set(self, key, value, nx=False, ex=None):
        _ = ex
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def get(self, key):
        return self.values.get(key)

    async def delete(self, key):
        self.values.pop(key, None)
        return True

    async def setex(self, key, ttl, value):
        _ = ttl
        self.values[key] = value

    async def ttl(self, _key):
        return 1

    async def zcard(self, _key):
        return 0

    async def publish(self, _channel, _message):
        return True

    async def hset(self, key, mapping):
        self.hashes[key] = {str(k): str(v) for k, v in mapping.items()}

    async def expire(self, _key, _ttl):
        return True


class _Cfg:
    class timing:
        mention_entry_enabled = False
        mention_entry_queue_max_wait_sec = 60
        agent_response_timeout_seconds = 30
        room_auto_intervention_cooldown_seconds = 180
        silence_trigger_enabled = True
        silence_threshold_seconds = 60
        warmup_minutes = 0
        rule_trigger_marker_ttl_seconds = 180
        mention_entry_rate_per_sec = 3
        agent_cooldown_seconds = 0

        @staticmethod
        def __getattr__(_name):
            return 0

    class mention:
        enabled = True
        priority = 0
        max_mentions_per_message = 10

    auto_speak = None


class _SchedulerRedis:
    async def smembers(self, _key):
        return ["room-multi-1"]


class _TriggerRedis:
    def __init__(self):
        self.buf = []

    async def lpush(self, _key, sender_id):
        self.buf.insert(0, sender_id)

    async def ltrim(self, _key, _start, _end):
        return None

    async def expire(self, _key, _ttl):
        return None

    async def lrange(self, _key, _start, _end):
        return list(self.buf)

    async def exists(self, _key):
        return False

    async def setex(self, _key, _ttl, _value):
        return None


class _DummyAgent:
    def __init__(self):
        self.called = 0

    async def generate_and_push(self, **_kwargs):
        self.called += 1


@pytest.mark.asyncio
async def test_multi_mode_all_six_roles_can_enqueue(monkeypatch):
    fake_redis = _FakeRedisQueue()

    async def _mode_multi(_room_id):
        return "multi"

    monkeypatch.setattr(agent_queue, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(agent_queue, "get_room_agent_mode", _mode_multi)

    roles = [
        "facilitator",
        "devil_advocate",
        "summarizer",
        "resource_finder",
        "encourager",
        "concept_explainer",
    ]
    for idx, role in enumerate(roles):
        task = {
            "room_id": "room-multi",
            "agent_role": role,
            "reason": "smoke",
            "strategy": "smoke",
            "trigger_type": "mention",
            "priority": 0,
            "triggered_at": float(idx + 1),
        }
        res = await agent_queue.enqueue_task("room-multi", task)
        assert res is not None

    assert len(fake_redis.zadd_calls) == len(roles)
    queued_roles = [call[1][next(iter(call[1]))] for call in fake_redis.zadd_calls]
    assert len(queued_roles) == len(roles)


@pytest.mark.asyncio
async def test_multi_mode_scheduler_committee_and_triggers_not_disabled(monkeypatch):
    fake_redis = _SchedulerRedis()
    fake_trigger_redis = _TriggerRedis()
    committee_called = []
    trigger_enqueued = []

    async def _mode_multi(_room_id):
        return "multi"

    async def _fake_analyze(room_id):
        committee_called.append(room_id)

    async def _fake_elapsed(_room_id):
        return 999.0

    async def _fake_enqueue(*args, **kwargs):
        trigger_enqueued.append((args, kwargs))

    class _TrigCfg:
        class thresholds:
            monopoly_message_count = 2

        class timing:
            warmup_minutes = 0
            rule_trigger_marker_ttl_seconds = 180

        auto_speak = None

    monkeypatch.setattr(scheduler, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(scheduler, "get_room_agent_mode", _mode_multi)
    monkeypatch.setattr(scheduler, "get_elapsed_seconds_from_timer_start", _fake_elapsed)
    monkeypatch.setattr(scheduler, "get_agent_settings", lambda: _Cfg())
    monkeypatch.setattr(scheduler.basic_committee, "analyze_and_dispatch", _fake_analyze)

    monkeypatch.setattr(triggers, "get_room_agent_mode", _mode_multi)
    monkeypatch.setattr(triggers, "enqueue_task", _fake_enqueue)
    monkeypatch.setattr(triggers, "get_redis_client", lambda: fake_trigger_redis)
    monkeypatch.setattr(triggers, "get_agent_settings", lambda: _TrigCfg())
    monkeypatch.setattr(triggers, "get_elapsed_seconds_from_timer_start", _fake_elapsed)

    await scheduler.check_committee_timer()
    assert committee_called == ["room-multi-1"]

    detector = triggers.TriggerDetector()
    await detector.check_monopoly("room-multi-1", "student-1")
    await detector.check_monopoly("room-multi-1", "student-1")
    assert len(trigger_enqueued) >= 1


@pytest.mark.asyncio
async def test_multi_mode_mention_still_creates_agent_tasks(monkeypatch):
    enqueued_roles = []

    async def _mode_multi(_room_id):
        return "multi"

    async def _fake_broadcast(_room_id, _payload):
        return None

    async def _fake_enqueue(_room_id, task, delay_seconds=0):
        _ = delay_seconds
        enqueued_roles.append(task["agent_role"])
        return {"task_id": f"task-{task['agent_role']}", **task}

    monkeypatch.setattr(handlers, "get_room_agent_mode", _mode_multi)
    monkeypatch.setattr(handlers, "get_agent_settings", lambda: _Cfg())
    monkeypatch.setattr(handlers.manager, "broadcast_to_room", _fake_broadcast)
    monkeypatch.setattr(handlers, "enqueue_task", _fake_enqueue)

    user = User(
        id=uuid.uuid4(),
        username="student_multi",
        password_hash="x",
        display_name="Student Multi",
        role=UserRole.student,
    )

    await handlers._trigger_mentions(
        room_id="room-multi",
        source_message_id="msg-1",
        user=user,
        mentions=["facilitator", "summarizer", "resource_finder"],
    )

    assert enqueued_roles == ["facilitator", "summarizer", "resource_finder"]


@pytest.mark.asyncio
async def test_multi_mode_worker_does_not_skip_valid_multi_agent_tasks(monkeypatch):
    fake_redis = _FakeRedisWorker()
    dummy = _DummyAgent()
    status_calls = []

    async def _mode_multi(_room_id):
        return "multi"

    async def _capture_status(**kwargs):
        status_calls.append(kwargs)

    monkeypatch.setattr(agent_worker, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(agent_worker, "get_room_agent_mode", _mode_multi)
    monkeypatch.setattr(agent_worker, "get_agent_settings", lambda: _Cfg())
    monkeypatch.setattr(agent_worker, "set_task_status", _capture_status)
    monkeypatch.setattr(agent_worker, "ROLE_AGENTS", {"facilitator": dummy})
    async def _fake_room_context(_room_id):
        return {}

    async def _fake_recent_messages(_room_id):
        return []

    monkeypatch.setattr(agent_worker, "get_room_context", _fake_room_context)
    monkeypatch.setattr(agent_worker, "get_recent_messages", _fake_recent_messages)

    worker = agent_worker.AgentWorker()
    room_id = str(uuid.uuid4())
    await worker._execute_task(
        room_id,
        {
            "task_id": "task-facilitator",
            "agent_role": "facilitator",
            "trigger_type": "mention",
            "source_message_id": "msg-1",
        },
    )

    assert dummy.called == 1
    blocked = [c for c in status_calls if c.get("reason") == "blocked_by_agent_mode"]
    assert blocked == []


def test_multi_mode_task_script_routes_unchanged(client, fake_db, monkeypatch):
    room_id = uuid.uuid4()
    task_id = uuid.uuid4()
    room = Room(id=room_id, name="R-multi", created_by=uuid.uuid4())
    room.task_id = task_id
    room.agent_mode = "multi"
    room.created_at = datetime.now(timezone.utc)
    fake_db.execute_result = FakeExecuteResult(scalar_value=object())

    async def _fake_get_room(_db, _room_id):
        return room

    async def _fake_get_task_script_state(_db, _room):
        return {
            "task_id": str(task_id),
            "current_status": "collecting evidence",
            "next_goal": "align three perspectives",
            "history": [],
            "pending_proposal": None,
        }

    monkeypatch.setattr(rooms_router.room_service, "get_room", _fake_get_room)
    monkeypatch.setattr(rooms_router.task_script_service, "get_task_script_state", _fake_get_task_script_state)

    resp = client.get(f"/api/rooms/{room_id}/task-script")
    assert resp.status_code == 200
    body = resp.json()
    assert body["current_status"] == "collecting evidence"
    assert body["next_goal"] == "align three perspectives"
