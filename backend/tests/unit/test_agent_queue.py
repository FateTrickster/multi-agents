import pytest

from app.agents import queue as agent_queue


class _FakeRedis:
    def __init__(self):
        self._z = {}
        self._h = {}
        self._exp = {}
        self._pub = []
        self._l = {}

    async def zadd(self, key, mapping):
        bucket = self._z.setdefault(key, {})
        for member, score in mapping.items():
            bucket[member] = score

    async def zrangebyscore(self, key, min, max, start=None, num=None):
        bucket = self._z.get(key, {})
        items = [(member, score) for member, score in bucket.items() if min <= score <= max]
        items.sort(key=lambda x: x[1])
        members = [member for member, _ in items]
        if start is not None and num is not None:
            return members[int(start) : int(start) + int(num)]
        return members

    async def zrem(self, key, *members):
        bucket = self._z.get(key, {})
        for member in members:
            bucket.pop(member, None)

    async def zcard(self, key):
        return len(self._z.get(key, {}))

    async def rpush(self, key, value):
        self._l.setdefault(key, []).append(value)

    async def hset(self, key, mapping):
        bucket = self._h.setdefault(key, {})
        bucket.update({str(k): str(v) for k, v in mapping.items()})
        return True

    async def hgetall(self, key):
        return self._h.get(key, {})

    async def expire(self, _key, _ttl):
        self._exp[_key] = _ttl
        return True

    async def publish(self, channel, payload):
        self._pub.append((channel, payload))
        return 1


@pytest.mark.asyncio
async def test_enqueue_and_dequeue_orders_by_priority_then_time(monkeypatch):
    fake_redis = _FakeRedis()
    monkeypatch.setattr(agent_queue, "get_redis_client", lambda: fake_redis)
    async def _mode_multi(_room_id):
        return "multi"
    monkeypatch.setattr(agent_queue, "get_room_agent_mode", _mode_multi)

    await agent_queue.enqueue_task("r1", {"agent_role": "facilitator", "priority": 2, "triggered_at": 2}, delay_seconds=0)
    await agent_queue.enqueue_task("r1", {"agent_role": "summarizer", "priority": 0, "triggered_at": 3}, delay_seconds=0)
    await agent_queue.enqueue_task("r1", {"agent_role": "encourager", "priority": 0, "triggered_at": 1}, delay_seconds=0)

    tasks = await agent_queue.dequeue_tasks("r1")

    assert [t["agent_role"] for t in tasks] == ["encourager", "summarizer", "facilitator"]


@pytest.mark.asyncio
async def test_enqueue_writes_queued_task_status(monkeypatch):
    fake_redis = _FakeRedis()
    monkeypatch.setattr(agent_queue, "get_redis_client", lambda: fake_redis)
    async def _mode_multi(_room_id):
        return "multi"
    monkeypatch.setattr(agent_queue, "get_room_agent_mode", _mode_multi)

    task = await agent_queue.enqueue_task(
        "room-status",
        {
            "task_id": "task-status-1",
            "agent_role": "facilitator",
            "trigger_type": "mention",
            "reason": "test reason",
            "priority": 1,
            "triggered_at": 1.0,
            "source_message_id": "msg-1",
        },
    )

    key = agent_queue.task_status_key(task["task_id"])
    status = fake_redis._h.get(key, {})
    assert status.get("status") == "queued"
    assert status.get("task_id") == "task-status-1"
    assert status.get("room_id") == "room-status"
    assert status.get("agent_role") == "facilitator"
    assert status.get("source_message_id") == "msg-1"
    assert key not in fake_redis._exp  # Audit records must remain exportable


@pytest.mark.asyncio
async def test_enqueue_silence_broadcasts_agent_queued(monkeypatch):
    fake_redis = _FakeRedis()
    monkeypatch.setattr(agent_queue, "get_redis_client", lambda: fake_redis)
    async def _mode_multi(_room_id):
        return "multi"
    monkeypatch.setattr(agent_queue, "get_room_agent_mode", _mode_multi)

    task = await agent_queue.enqueue_task(
        "room-auto-1",
        {
            "task_id": "task-auto-queued-1",
            "agent_role": "facilitator",
            "trigger_type": "silence",
            "reason": "silence reached threshold",
            "priority": 2,
            "triggered_at": 1.0,
        },
    )

    assert task["task_id"] == "task-auto-queued-1"
    assert fake_redis._pub
    payload = fake_redis._pub[-1][1]
    assert '"type": "agent:queued"' in payload
    assert '"task_id": "task-auto-queued-1"' in payload
    assert '"trigger_type": "silence"' in payload
    assert '"status": "queued"' in payload


@pytest.mark.asyncio
async def test_create_mention_entry_writes_hash_status_and_queue(monkeypatch):
    fake_redis = _FakeRedis()
    monkeypatch.setattr(agent_queue, "get_redis_client", lambda: fake_redis)

    entry = await agent_queue.create_mention_entry(
        room_id="room-a",
        agent_role="facilitator",
        source_message_id="msg-100",
        student_name="stu-a",
        reason="manual mention",
        strategy="assist",
        entry_id="entry-1",
        created_at=100.0,
        expire_at=160.0,
    )

    assert entry["entry_id"] == "entry-1"
    assert fake_redis._h[agent_queue.mention_entry_key("entry-1")]["room_id"] == "room-a"
    status = fake_redis._h[agent_queue.mention_entry_status_key("entry-1")]
    assert status["status"] == "queued"
    assert status["entry_id"] == "entry-1"
    assert fake_redis._z[agent_queue.MENTION_ENTRY_QUEUE_KEY]["entry-1"] == 100.0
    assert agent_queue.mention_entry_key("entry-1") not in fake_redis._exp
    assert agent_queue.mention_entry_status_key("entry-1") not in fake_redis._exp


@pytest.mark.asyncio
async def test_get_mention_entry_and_queue_length(monkeypatch):
    fake_redis = _FakeRedis()
    monkeypatch.setattr(agent_queue, "get_redis_client", lambda: fake_redis)
    await agent_queue.create_mention_entry(
        room_id="room-a",
        agent_role="facilitator",
        source_message_id="msg-1",
        student_name="stu-1",
        reason="r1",
        strategy="s1",
        entry_id="entry-a",
        created_at=10.0,
    )
    await agent_queue.create_mention_entry(
        room_id="room-b",
        agent_role="resource_finder",
        source_message_id="msg-2",
        student_name="stu-2",
        reason="r2",
        strategy="s2",
        entry_id="entry-b",
        created_at=20.0,
    )

    got = await agent_queue.get_mention_entry("entry-a")
    qlen = await agent_queue.get_mention_entry_queue_length()
    assert got["entry_id"] == "entry-a"
    assert got["agent_role"] == "facilitator"
    assert qlen == 2


@pytest.mark.asyncio
async def test_pop_due_mention_entries_order_and_remove(monkeypatch):
    fake_redis = _FakeRedis()
    monkeypatch.setattr(agent_queue, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(agent_queue.time, "time", lambda: 25.0)

    await agent_queue.create_mention_entry(
        room_id="r1",
        agent_role="facilitator",
        source_message_id="m1",
        student_name="s1",
        reason="x",
        strategy="y",
        entry_id="e1",
        created_at=10.0,
    )
    await agent_queue.create_mention_entry(
        room_id="r2",
        agent_role="facilitator",
        source_message_id="m2",
        student_name="s2",
        reason="x",
        strategy="y",
        entry_id="e2",
        created_at=20.0,
    )
    await agent_queue.create_mention_entry(
        room_id="r3",
        agent_role="facilitator",
        source_message_id="m3",
        student_name="s3",
        reason="x",
        strategy="y",
        entry_id="e3",
        created_at=30.0,
    )

    popped = await agent_queue.pop_due_mention_entries(2)
    assert [item["entry_id"] for item in popped] == ["e1", "e2"]
    assert "e1" not in fake_redis._z[agent_queue.MENTION_ENTRY_QUEUE_KEY]
    assert "e2" not in fake_redis._z[agent_queue.MENTION_ENTRY_QUEUE_KEY]
    assert "e3" in fake_redis._z[agent_queue.MENTION_ENTRY_QUEUE_KEY]


@pytest.mark.asyncio
async def test_mark_status_and_remove_entry(monkeypatch):
    fake_redis = _FakeRedis()
    monkeypatch.setattr(agent_queue, "get_redis_client", lambda: fake_redis)
    await agent_queue.create_mention_entry(
        room_id="r1",
        agent_role="facilitator",
        source_message_id="m1",
        student_name="s1",
        reason="x",
        strategy="y",
        entry_id="e1",
        created_at=10.0,
    )

    await agent_queue.mark_mention_entry_status(
        "e1",
        "dropped",
        reason="mention_entry_timeout",
        task_id="task-x",
        error="expired",
    )
    await agent_queue.remove_mention_entry_from_queue("e1")

    status = fake_redis._h[agent_queue.mention_entry_status_key("e1")]
    assert status["status"] == "dropped"
    assert status["reason"] == "mention_entry_timeout"
    assert status["task_id"] == "task-x"
    assert status["error"] == "expired"
    assert "e1" not in fake_redis._z.get(agent_queue.MENTION_ENTRY_QUEUE_KEY, {})
