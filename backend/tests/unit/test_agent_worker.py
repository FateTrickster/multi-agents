import asyncio

import pytest

from app.agents import agent_worker


@pytest.fixture(autouse=True)
def _use_multi_agent_mode(monkeypatch):
    async def _mode_multi(_room_id):
        return "multi"

    monkeypatch.setattr(agent_worker, "get_room_agent_mode", _mode_multi)


class _FakeRedis:
    def __init__(self):
        self.keys = set()
        self.setex_calls = []
        self.values = {}
        self.delete_calls = []
        self.publish_calls = []
        self.hashes = {}

    async def get(self, _key):
        return self.values.get(_key)

    async def exists(self, key):
        return key in self.keys

    async def set(self, _key, _value, nx=False, ex=None):
        _ = ex
        if nx and _key in self.values:
            return False
        self.values[_key] = _value
        return True

    async def setex(self, key, ttl, value):
        self.setex_calls.append((key, ttl, value))
        self.keys.add(key)

    async def incr(self, _key):
        return 1

    async def expire(self, _key, _ttl):
        return True

    async def hset(self, key, mapping):
        bucket = self.hashes.setdefault(key, {})
        bucket.update({str(k): str(v) for k, v in mapping.items()})
        return True

    async def hgetall(self, key):
        return self.hashes.get(key, {})

    async def delete(self, _key):
        self.delete_calls.append(_key)
        self.values.pop(_key, None)
        return True

    async def publish(self, channel, message):
        self.publish_calls.append((channel, message))
        return 1

    async def ttl(self, _key):
        return 5

    async def zcard(self, _key):
        return 0


class _FakeRedisWithTtl:
    def __init__(self):
        self.now = 0
        self.values = {}
        self.expire_at = {}
        self.setex_calls = []
        self.delete_calls = []
        self.publish_calls = []
        self.hashes = {}

    def advance(self, seconds: int):
        self.now += int(seconds)

    def _is_expired(self, key):
        exp = self.expire_at.get(key)
        return exp is not None and self.now >= exp

    async def get(self, key):
        if self._is_expired(key):
            self.values.pop(key, None)
            self.expire_at.pop(key, None)
            return None
        return self.values.get(key)

    async def exists(self, key):
        if self._is_expired(key):
            self.values.pop(key, None)
            self.expire_at.pop(key, None)
            return False
        return key in self.values

    async def set(self, key, value, nx=False, ex=None):
        if nx and await self.exists(key):
            return False
        self.values[key] = value
        if ex is not None:
            self.expire_at[key] = self.now + int(ex)
        return True

    async def setex(self, key, ttl, value):
        self.values[key] = value
        self.expire_at[key] = self.now + int(ttl)
        self.setex_calls.append((key, ttl, value))
        return True

    async def incr(self, key):
        current = int((await self.get(key)) or 0) + 1
        self.values[key] = str(current)
        return current

    async def expire(self, key, ttl):
        if key not in self.values:
            # Hash-only keys should still support expire in tests.
            if key in self.hashes:
                self.expire_at[key] = self.now + int(ttl)
                return True
            return False
        self.expire_at[key] = self.now + int(ttl)
        return True

    async def hset(self, key, mapping):
        bucket = self.hashes.setdefault(key, {})
        bucket.update({str(k): str(v) for k, v in mapping.items()})
        return True

    async def hgetall(self, key):
        return self.hashes.get(key, {})

    async def delete(self, key):
        self.delete_calls.append(key)
        self.values.pop(key, None)
        self.expire_at.pop(key, None)
        return True

    async def publish(self, channel, message):
        self.publish_calls.append((channel, message))
        return 1

    async def ttl(self, key):
        if await self.exists(key):
            exp = self.expire_at.get(key)
            if exp is None:
                return -1
            return max(1, int(exp - self.now))
        return -2

    async def zcard(self, _key):
        return 0


class _CfgTiming:
    global_intervention_limit_per_hour = 99
    agent_cooldown_seconds = 60
    room_auto_intervention_cooldown_seconds = 180
    agent_response_timeout_seconds = 1


class _Cfg:
    timing = _CfgTiming()
    auto_speak = None


class _DummyAgent:
    async def generate_and_push(self, **_kwargs):
        return None


class _SlowAgent:
    async def generate_and_push(self, **_kwargs):
        await asyncio.sleep(6)


class _ErrorAgent:
    async def generate_and_push(self, **_kwargs):
        raise RuntimeError("boom")


class _BlockingAgent:
    def __init__(self, started_event: asyncio.Event, release_event: asyncio.Event, counter: dict):
        self.started_event = started_event
        self.release_event = release_event
        self.counter = counter

    async def generate_and_push(self, **_kwargs):
        self.counter["running"] += 1
        self.counter["max_running"] = max(self.counter["max_running"], self.counter["running"])
        self.started_event.set()
        try:
            await self.release_event.wait()
        finally:
            self.counter["running"] -= 1


async def _fake_set_task_status(**_kwargs):
    return None


@pytest.mark.asyncio
async def test_worker_drops_auto_task_when_room_auto_cooldown(monkeypatch):
    fake_redis = _FakeRedis()
    fake_redis.keys.add("cooldown:r1:auto_intervention")
    requeued = []

    async def _fake_requeue(_room_id, _task, delay_seconds=0):
        requeued.append(delay_seconds)

    monkeypatch.setattr(agent_worker, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(agent_worker, "get_agent_settings", lambda: _Cfg())
    monkeypatch.setattr(agent_worker, "requeue_task", _fake_requeue)
    monkeypatch.setattr(agent_worker, "ROLE_AGENTS", {"encourager": _DummyAgent()})

    worker = agent_worker.AgentWorker()
    await worker._execute_task("r1", {"agent_role": "encourager", "trigger_type": "committee"})

    assert requeued == []


@pytest.mark.asyncio
async def test_worker_drops_mention_task_on_role_cooldown(monkeypatch):
    fake_redis = _FakeRedis()
    fake_redis.keys.add("cooldown:r2:encourager")

    monkeypatch.setattr(agent_worker, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(agent_worker, "get_agent_settings", lambda: _Cfg())
    monkeypatch.setattr(agent_worker, "set_task_status", _fake_set_task_status)
    monkeypatch.setattr(agent_worker, "ROLE_AGENTS", {"encourager": _DummyAgent()})

    worker = agent_worker.AgentWorker()
    await worker._execute_task(
        "r2",
        {
            "agent_role": "encourager",
            "trigger_type": "mention",
            "source_message_id": "m-1",
            "task_id": "t-1",
        },
    )

    assert len(fake_redis.publish_calls) == 1
    assert '"reason": "role_cooldown"' in fake_redis.publish_calls[0][1]


@pytest.mark.asyncio
async def test_worker_marks_timeout_terminal_status_when_agent_generation_times_out(monkeypatch):
    fake_redis = _FakeRedis()
    status_calls = []

    async def _fake_context(_room_id):
        return {"current_phase": "middle"}

    async def _fake_history(_room_id):
        return []

    monkeypatch.setattr(agent_worker, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(agent_worker, "get_agent_settings", lambda: _Cfg())
    async def _capture_set_task_status(**kwargs):
        status_calls.append(kwargs)

    monkeypatch.setattr(agent_worker, "set_task_status", _capture_set_task_status)
    monkeypatch.setattr(agent_worker, "get_room_context", _fake_context)
    monkeypatch.setattr(agent_worker, "get_recent_messages", _fake_history)
    monkeypatch.setattr(agent_worker, "ROLE_AGENTS", {"resource_finder": _SlowAgent()})

    worker = agent_worker.AgentWorker()
    await worker._execute_task(
        "r3",
        {
            "agent_role": "resource_finder",
            "trigger_type": "mention",
            "source_message_id": "m-timeout",
            "task_id": "t-timeout",
        },
    )

    assert any('"type": "agent:timeout"' in msg for _, msg in fake_redis.publish_calls)
    assert any(call.get("status") == "timeout" and call.get("reason") == "worker_timeout" for call in status_calls)
    assert "room:r3:agent_lock" in fake_redis.delete_calls


@pytest.mark.asyncio
async def test_worker_marks_dropped_terminal_status(monkeypatch):
    fake_redis = _FakeRedis()
    fake_redis.keys.add("cooldown:r2:encourager")
    status_calls = []

    monkeypatch.setattr(agent_worker, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(agent_worker, "get_agent_settings", lambda: _Cfg())
    async def _capture_set_task_status(**kwargs):
        status_calls.append(kwargs)

    monkeypatch.setattr(agent_worker, "set_task_status", _capture_set_task_status)
    monkeypatch.setattr(agent_worker, "ROLE_AGENTS", {"encourager": _DummyAgent()})

    worker = agent_worker.AgentWorker()
    await worker._execute_task(
        "r2",
        {
            "agent_role": "encourager",
            "trigger_type": "mention",
            "source_message_id": "m-1",
            "task_id": "t-drop",
        },
    )

    assert any(call.get("status") == "dropped" and call.get("drop_reason") == "role_cooldown" for call in status_calls)


@pytest.mark.asyncio
async def test_worker_multiple_mentions_same_role_second_requeued(monkeypatch):
    fake_redis = _FakeRedisWithTtl()
    called = {"count": 0}

    class _CfgTimingShort:
        global_intervention_limit_per_hour = 99
        agent_cooldown_seconds = 12
        room_auto_intervention_cooldown_seconds = 180
        agent_response_timeout_seconds = 1

    class _CfgShort:
        timing = _CfgTimingShort()
        auto_speak = None

    class _CountAgent:
        async def generate_and_push(self, **_kwargs):
            called["count"] += 1
            return None

    async def _fake_context(_room_id):
        return {"current_phase": "middle"}

    async def _fake_history(_room_id):
        return []

    monkeypatch.setattr(agent_worker, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(agent_worker, "get_agent_settings", lambda: _CfgShort())
    monkeypatch.setattr(agent_worker, "set_task_status", _fake_set_task_status)
    monkeypatch.setattr(agent_worker, "get_room_context", _fake_context)
    monkeypatch.setattr(agent_worker, "get_recent_messages", _fake_history)
    monkeypatch.setattr(agent_worker, "ROLE_AGENTS", {"resource_finder": _CountAgent()})

    worker = agent_worker.AgentWorker()
    task = {"agent_role": "resource_finder", "trigger_type": "mention", "source_message_id": "m-1", "task_id": "t-1"}

    await worker._execute_task("r4", task)
    await worker._execute_task("r4", task)
    fake_redis.advance(10)
    await worker._execute_task("r4", task)
    fake_redis.advance(3)
    await worker._execute_task("r4", task)

    assert called["count"] == 2
    cooldown_msgs = [msg for _, msg in fake_redis.publish_calls if '"reason": "role_cooldown"' in msg]
    assert len(cooldown_msgs) == 2
    assert "cooldown:r4:resource_finder" in fake_redis.values


@pytest.mark.asyncio
async def test_worker_requeues_when_agent_lock_not_acquired(monkeypatch):
    fake_redis = _FakeRedis()
    fake_redis.values["room:r6:agent_lock"] = "other-worker"
    requeued = []

    async def _fake_requeue(_room_id, _task, delay_seconds=0):
        requeued.append(delay_seconds)

    monkeypatch.setattr(agent_worker, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(agent_worker, "get_agent_settings", lambda: _Cfg())
    monkeypatch.setattr(agent_worker, "set_task_status", _fake_set_task_status)
    monkeypatch.setattr(agent_worker, "requeue_task", _fake_requeue)
    monkeypatch.setattr(agent_worker, "ROLE_AGENTS", {"resource_finder": _DummyAgent()})

    worker = agent_worker.AgentWorker()
    await worker._execute_task("r6", {"agent_role": "resource_finder", "trigger_type": "mention"})

    assert requeued == [5]


@pytest.mark.asyncio
async def test_worker_releases_lock_when_agent_raises(monkeypatch):
    fake_redis = _FakeRedis()

    async def _fake_context(_room_id):
        return {"current_phase": "middle"}

    async def _fake_history(_room_id):
        return []

    monkeypatch.setattr(agent_worker, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(agent_worker, "get_agent_settings", lambda: _Cfg())
    monkeypatch.setattr(agent_worker, "set_task_status", _fake_set_task_status)
    monkeypatch.setattr(agent_worker, "get_room_context", _fake_context)
    monkeypatch.setattr(agent_worker, "get_recent_messages", _fake_history)
    monkeypatch.setattr(agent_worker, "ROLE_AGENTS", {"resource_finder": _ErrorAgent()})

    worker = agent_worker.AgentWorker()
    with pytest.raises(RuntimeError):
        await worker._execute_task("r7", {"agent_role": "resource_finder", "trigger_type": "mention"})

    assert "room:r7:agent_lock" in fake_redis.delete_calls


@pytest.mark.asyncio
async def test_worker_global_concurrency_limit_one_requeues_extra_task(monkeypatch):
    fake_redis = _FakeRedis()
    requeued = []
    started_event = asyncio.Event()
    release_event = asyncio.Event()
    counter = {"running": 0, "max_running": 0}

    class _CfgTimingOne:
        global_intervention_limit_per_hour = 99
        agent_cooldown_seconds = 0
        room_auto_intervention_cooldown_seconds = 180
        agent_response_timeout_seconds = 5
        agent_global_concurrency_limit = 1

    class _CfgOne:
        timing = _CfgTimingOne()
        auto_speak = None

    async def _fake_context(_room_id):
        return {"current_phase": "middle"}

    async def _fake_history(_room_id):
        return []

    async def _fake_requeue(_room_id, _task, delay_seconds=0):
        requeued.append((_room_id, delay_seconds))

    monkeypatch.setattr(agent_worker, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(agent_worker, "get_agent_settings", lambda: _CfgOne())
    monkeypatch.setattr(agent_worker, "set_task_status", _fake_set_task_status)
    monkeypatch.setattr(agent_worker, "get_room_context", _fake_context)
    monkeypatch.setattr(agent_worker, "get_recent_messages", _fake_history)
    monkeypatch.setattr(agent_worker, "requeue_task", _fake_requeue)
    monkeypatch.setattr(
        agent_worker,
        "ROLE_AGENTS",
        {"resource_finder": _BlockingAgent(started_event, release_event, counter)},
    )
    agent_worker.AgentWorker._global_semaphore = None
    agent_worker.AgentWorker._global_semaphore_limit = 0

    worker = agent_worker.AgentWorker()
    task1 = asyncio.create_task(
        worker._execute_task("r8", {"agent_role": "resource_finder", "trigger_type": "mention", "task_id": "t1"})
    )
    await asyncio.wait_for(started_event.wait(), timeout=1)
    await worker._execute_task("r9", {"agent_role": "resource_finder", "trigger_type": "mention", "task_id": "t2"})
    release_event.set()
    await task1

    assert counter["max_running"] == 1
    assert requeued == [("r9", 2)]


@pytest.mark.asyncio
async def test_worker_global_token_released_after_exception(monkeypatch):
    fake_redis = _FakeRedis()
    status_calls = []

    class _CfgTimingOne:
        global_intervention_limit_per_hour = 99
        agent_cooldown_seconds = 0
        room_auto_intervention_cooldown_seconds = 180
        agent_response_timeout_seconds = 5
        agent_global_concurrency_limit = 1

    class _CfgOne:
        timing = _CfgTimingOne()
        auto_speak = None

    async def _fake_context(_room_id):
        return {"current_phase": "middle"}

    async def _fake_history(_room_id):
        return []

    async def _capture_set_task_status(**kwargs):
        status_calls.append(kwargs)

    monkeypatch.setattr(agent_worker, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(agent_worker, "get_agent_settings", lambda: _CfgOne())
    monkeypatch.setattr(agent_worker, "set_task_status", _capture_set_task_status)
    monkeypatch.setattr(agent_worker, "get_room_context", _fake_context)
    monkeypatch.setattr(agent_worker, "get_recent_messages", _fake_history)
    monkeypatch.setattr(agent_worker, "ROLE_AGENTS", {"resource_finder": _ErrorAgent()})
    agent_worker.AgentWorker._global_semaphore = None
    agent_worker.AgentWorker._global_semaphore_limit = 0

    worker = agent_worker.AgentWorker()
    with pytest.raises(RuntimeError):
        await worker._execute_task("r10", {"agent_role": "resource_finder", "trigger_type": "mention", "task_id": "t-ex-1"})

    monkeypatch.setattr(agent_worker, "ROLE_AGENTS", {"resource_finder": _DummyAgent()})
    await worker._execute_task("r10", {"agent_role": "resource_finder", "trigger_type": "mention", "task_id": "t-ex-2"})

    assert any(call.get("task_id") == "t-ex-1" and call.get("status") == "failed" for call in status_calls)
    assert any(call.get("task_id") == "t-ex-2" and call.get("status") == "running" for call in status_calls)


@pytest.mark.asyncio
async def test_worker_broadcasts_running_for_silence_without_source_message_id(monkeypatch):
    fake_redis = _FakeRedis()
    status_calls = []

    async def _fake_context(_room_id):
        return {"current_phase": "middle"}

    async def _fake_history(_room_id):
        return []

    async def _capture_set_task_status(**kwargs):
        status_calls.append(kwargs)

    monkeypatch.setattr(agent_worker, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(agent_worker, "get_agent_settings", lambda: _Cfg())
    monkeypatch.setattr(agent_worker, "set_task_status", _capture_set_task_status)
    monkeypatch.setattr(agent_worker, "get_room_context", _fake_context)
    monkeypatch.setattr(agent_worker, "get_recent_messages", _fake_history)
    monkeypatch.setattr(agent_worker, "ROLE_AGENTS", {"facilitator": _DummyAgent()})

    worker = agent_worker.AgentWorker()
    await worker._execute_task(
        "r11",
        {
            "agent_role": "facilitator",
            "trigger_type": "silence",
            "task_id": "t-silence-running-1",
            "reason": "silence",
        },
    )

    assert any('"type": "agent:running"' in msg for _, msg in fake_redis.publish_calls)
    assert any('"task_id": "t-silence-running-1"' in msg for _, msg in fake_redis.publish_calls)
    assert any('"trigger_type": "silence"' in msg for _, msg in fake_redis.publish_calls)
    assert any(call.get("task_id") == "t-silence-running-1" and call.get("status") == "running" for call in status_calls)

