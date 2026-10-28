from types import SimpleNamespace

import pytest

from app.agents import agent_mode
from app.agents import agent_worker
from app.agents import mention_entry_worker as mew
from app.agents import queue as agent_queue
from app.analysis import scheduler, triggers
from app.routers import debug as debug_router
from app.websocket import handlers


class _FakeRedisQueue:
    def __init__(self):
        self.zadd_calls = []
        self.hset_calls = []
        self.expire_calls = []
        self.publish_calls = []
        self.list_pushes = []
        self.values = {}
        self.zsets = {}

    async def rpush(self, key, value):
        self.list_pushes.append((key, value))

    async def zadd(self, key, mapping):
        self.zadd_calls.append((key, mapping))
        bucket = self.zsets.setdefault(key, [])
        for raw, score in mapping.items():
            bucket.append((float(score), raw))

    async def hset(self, key, mapping):
        self.hset_calls.append((key, dict(mapping)))

    async def expire(self, key, ttl):
        self.expire_calls.append((key, ttl))

    async def publish(self, channel, message):
        self.publish_calls.append((channel, message))

    async def zcard(self, _key):
        return len(self.zsets.get(_key, []))

    async def zrange(self, key, start, end, withscores=False):
        items = sorted(self.zsets.get(key, []), key=lambda x: x[0])
        if end == -1:
            sliced = items[start:]
        else:
            sliced = items[start : end + 1]
        if withscores:
            return [(raw, score) for score, raw in sliced]
        return [raw for score, raw in sliced]

    async def zrangebyscore(self, key, min=0, max=0):  # noqa: A002
        result = []
        for score, raw in self.zsets.get(key, []):
            if float(min) <= score <= float(max):
                result.append(raw)
        return result

    async def zrem(self, key, *members):
        member_set = set(members)
        self.zsets[key] = [(s, r) for (s, r) in self.zsets.get(key, []) if r not in member_set]

    async def set(self, key, value, nx=False, ex=None):
        _ = ex
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def get(self, key):
        return self.values.get(key)

    async def delete(self, key):
        existed = key in self.values
        self.values.pop(key, None)
        return 1 if existed else 0


class _FakeRedisWorker:
    def __init__(self):
        self.values = {}
        self.publish_calls = []
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

    async def publish(self, channel, message):
        self.publish_calls.append((channel, message))

    async def hset(self, key, mapping):
        self.hashes[key] = {str(k): str(v) for k, v in mapping.items()}

    async def expire(self, _key, _ttl):
        return True


class _Cfg:
    class timing:
        silence_trigger_enabled = True
        silence_threshold_seconds = 60
        warmup_minutes = 0
        rule_trigger_marker_ttl_seconds = 180
        room_auto_intervention_cooldown_seconds = 180
        mention_entry_enabled = True
        mention_entry_rate_per_sec = 3
        agent_response_timeout_seconds = 30
        agent_cooldown_seconds = 0

        @staticmethod
        def __getattr__(_name):
            return 0

    class mention:
        enabled = True
        priority = 0
        max_mentions_per_message = 3

    auto_speak = None


class _DummyUser:
    def __init__(self):
        self.display_name = "Student"


class _DummyAgent:
    def __init__(self):
        self.called = 0

    async def generate_and_push(self, **_kwargs):
        self.called += 1


class _SchedulerRedis:
    def __init__(self):
        self._active_rooms = ["r-none", "r-single", "r-multi"]

    async def smembers(self, _key):
        return list(self._active_rooms)


@pytest.mark.asyncio
async def test_helper_modes_and_roles():
    assert agent_mode.normalize_agent_mode(None) == "multi"
    assert agent_mode.normalize_agent_mode("multi") == "multi"
    assert agent_mode.normalize_agent_mode("bad") == "none"

    assert agent_mode.can_use_agent_role("none", "facilitator") is False
    assert agent_mode.can_use_agent_role("single", "socratic") is True
    assert agent_mode.can_use_agent_role("single", "facilitator") is False
    assert agent_mode.can_use_agent_role("multi", "facilitator") is True

    assert agent_mode.should_run_auto_dispatcher("none") is False
    assert agent_mode.should_run_auto_dispatcher("single") is False
    assert agent_mode.should_run_auto_dispatcher("multi") is True


@pytest.mark.asyncio
async def test_queue_root_gating_blocks_none_and_single_non_socratic(monkeypatch):
    fake_redis = _FakeRedisQueue()

    async def _mode_none(_room_id):
        return "none"

    monkeypatch.setattr(agent_queue, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(agent_queue, "get_room_agent_mode", _mode_none)

    blocked = await agent_queue.enqueue_task("room-1", {"room_id": "room-1", "agent_role": "facilitator", "reason": "r", "strategy": "s", "trigger_type": "mention", "priority": 1, "triggered_at": 1.0})
    assert blocked is None
    assert fake_redis.zadd_calls == []

    async def _mode_single(_room_id):
        return "single"

    monkeypatch.setattr(agent_queue, "get_room_agent_mode", _mode_single)
    blocked_single = await agent_queue.enqueue_task("room-1", {"room_id": "room-1", "agent_role": "facilitator", "reason": "r", "strategy": "s", "trigger_type": "mention", "priority": 1, "triggered_at": 1.0})
    assert blocked_single is None
    assert fake_redis.zadd_calls == []


@pytest.mark.asyncio
async def test_queue_root_gating_allows_single_socratic_and_multi_role(monkeypatch):
    fake_redis = _FakeRedisQueue()
    monkeypatch.setattr(agent_queue, "get_redis_client", lambda: fake_redis)

    async def _mode_single(_room_id):
        return "single"

    monkeypatch.setattr(agent_queue, "get_room_agent_mode", _mode_single)
    allowed_single = await agent_queue.enqueue_task("room-1", {"room_id": "room-1", "agent_role": "socratic", "reason": "r", "strategy": "s", "trigger_type": "mention", "priority": 1, "triggered_at": 1.0})
    assert allowed_single is not None
    assert len(fake_redis.zadd_calls) == 1

    async def _mode_multi(_room_id):
        return "multi"

    monkeypatch.setattr(agent_queue, "get_room_agent_mode", _mode_multi)
    allowed_multi = await agent_queue.enqueue_task("room-1", {"room_id": "room-1", "agent_role": "facilitator", "reason": "r", "strategy": "s", "trigger_type": "mention", "priority": 1, "triggered_at": 2.0})
    assert allowed_multi is not None
    assert len(fake_redis.zadd_calls) == 2


@pytest.mark.asyncio
async def test_worker_skips_task_before_llm_when_mode_blocks(monkeypatch):
    fake_redis = _FakeRedisWorker()
    dummy = _DummyAgent()
    status_calls = []

    async def _capture_status(**kwargs):
        status_calls.append(kwargs)

    monkeypatch.setattr(agent_worker, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(agent_worker, "get_agent_settings", lambda: _Cfg())
    monkeypatch.setattr(agent_worker, "set_task_status", _capture_status)
    monkeypatch.setattr(agent_worker, "ROLE_AGENTS", {"facilitator": dummy, "socratic": dummy})

    async def _mode_none(_room_id):
        return "none"

    monkeypatch.setattr(agent_worker, "get_room_agent_mode", _mode_none)
    w = agent_worker.AgentWorker()
    await w._execute_task("r1", {"task_id": "t1", "agent_role": "facilitator", "trigger_type": "mention"})
    assert dummy.called == 0
    assert any(c.get("status") == "skipped" and c.get("reason") == "blocked_by_agent_mode" for c in status_calls)


@pytest.mark.asyncio
async def test_mention_entry_worker_skips_when_mode_blocks(monkeypatch):
    marks = []

    async def _fake_mark(entry_id, status, reason=None, task_id=None, error=None):
        marks.append((entry_id, status, reason, task_id, error))

    async def _mode_none(_room_id):
        return "none"

    async def _fake_enqueue(*_args, **_kwargs):
        raise AssertionError("enqueue_task should not be called when mode blocks")

    monkeypatch.setattr(mew, "get_room_agent_mode", _mode_none)
    monkeypatch.setattr(mew, "mark_mention_entry_status", _fake_mark)
    monkeypatch.setattr(mew, "enqueue_task", _fake_enqueue)

    worker = mew.MentionEntryWorker()
    await worker._handle_entry(
        {
            "entry_id": "e1",
            "room_id": "room-1",
            "agent_role": "facilitator",
            "source_message_id": "m1",
            "trigger_type": "mention",
            "expire_at": "9999999999",
        },
        now_ts=1.0,
    )

    assert any(m[0] == "e1" and m[1] == "skipped" and m[2] == "blocked_by_agent_mode" for m in marks)


@pytest.mark.asyncio
async def test_websocket_mentions_blocked_in_none_mode_before_enqueue(monkeypatch):
    broadcasts = []
    enqueued = []
    created_entries = []

    async def _mode_none(_room_id):
        return "none"

    async def _fake_broadcast(room_id, payload):
        broadcasts.append((room_id, payload))

    async def _fake_enqueue(*args, **kwargs):
        enqueued.append((args, kwargs))
        return {"task_id": "t"}

    async def _fake_create_entry(**kwargs):
        created_entries.append(kwargs)
        return {"entry_id": "e1", **kwargs}

    monkeypatch.setattr(handlers, "get_room_agent_mode", _mode_none)
    monkeypatch.setattr(handlers.manager, "broadcast_to_room", _fake_broadcast)
    monkeypatch.setattr(handlers, "enqueue_task", _fake_enqueue)
    monkeypatch.setattr(handlers, "create_mention_entry", _fake_create_entry)
    monkeypatch.setattr(handlers, "get_agent_settings", lambda: _Cfg())

    await handlers._trigger_mentions("room-1", "msg-1", _DummyUser(), ["facilitator"])

    assert enqueued == []
    assert created_entries == []
    assert broadcasts == []


@pytest.mark.asyncio
async def test_scheduler_and_committee_skip_in_none_and_single(monkeypatch):
    fake_redis = _SchedulerRedis()
    called_rooms = []

    async def _mode(room_id):
        if room_id == "r-multi":
            return "multi"
        if room_id == "r-single":
            return "single"
        return "none"

    async def _fake_analyze(room_id):
        called_rooms.append(room_id)

    async def _fake_elapsed(_room_id):
        return 999.0

    monkeypatch.setattr(scheduler, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(scheduler, "get_room_agent_mode", _mode)
    monkeypatch.setattr(scheduler, "get_elapsed_seconds_from_timer_start", _fake_elapsed)
    monkeypatch.setattr(scheduler.basic_committee, "analyze_and_dispatch", _fake_analyze)
    monkeypatch.setattr(scheduler, "get_agent_settings", lambda: _Cfg())

    await scheduler.check_committee_timer()

    assert called_rooms == ["r-multi"]


@pytest.mark.asyncio
async def test_triggers_skip_monopoly_when_not_multi(monkeypatch):
    enqueued = []

    async def _mode(_room_id):
        return "single"

    async def _fake_enqueue(*args, **kwargs):
        enqueued.append((args, kwargs))

    monkeypatch.setattr(triggers, "get_room_agent_mode", _mode)
    monkeypatch.setattr(triggers, "enqueue_task", _fake_enqueue)

    detector = triggers.TriggerDetector()
    await detector.check_monopoly("room-1", "u1")

    assert enqueued == []


@pytest.mark.asyncio
async def test_debug_trigger_respects_agent_mode(monkeypatch):
    async def _mode(_room_id):
        return "none"

    monkeypatch.setattr(debug_router, "get_room_agent_mode", _mode)
    payload = await debug_router.trigger_agent(room_id="room-1", role="facilitator", background_tasks=None)

    assert payload["status"] == "blocked_by_agent_mode"
    assert payload["agent_mode"] == "none"
