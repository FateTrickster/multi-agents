from types import SimpleNamespace

import pytest

from app.agents import mention_entry_worker as mew


@pytest.fixture(autouse=True)
def _use_multi_agent_mode(monkeypatch):
    async def _mode_multi(_room_id):
        return "multi"

    monkeypatch.setattr(mew, "get_room_agent_mode", _mode_multi)


def _cfg(enabled: bool, rate: int = 3):
    return SimpleNamespace(
        timing=SimpleNamespace(
            mention_entry_enabled=enabled,
            mention_entry_rate_per_sec=rate,
        ),
        mention=SimpleNamespace(priority=0),
    )


@pytest.mark.asyncio
async def test_process_once_noop_when_disabled(monkeypatch):
    called = {"pop": 0}

    async def _fake_pop(_limit):
        called["pop"] += 1
        return []

    monkeypatch.setattr(mew, "get_agent_settings", lambda: _cfg(False))
    monkeypatch.setattr(mew, "pop_due_mention_entries", _fake_pop)
    worker = mew.MentionEntryWorker()
    processed = await worker._process_once()

    assert processed == 0
    assert called["pop"] == 0


@pytest.mark.asyncio
async def test_process_once_enqueue_success_updates_status_and_broadcasts(monkeypatch):
    marks = []
    broadcasts = []
    enqueued = []
    entry = {
        "entry_id": "e1",
        "room_id": "r1",
        "agent_role": "facilitator",
        "source_message_id": "m1",
        "student_name": "stu",
        "reason": "rr",
        "strategy": "ss",
        "trigger_type": "mention",
        "expire_at": "9999999999",
    }

    async def _fake_pop(_limit):
        return [entry]

    async def _fake_enqueue(room_id, task, delay_seconds=0):
        enqueued.append((room_id, task, delay_seconds))
        return {"task_id": "t1"}

    async def _fake_mark(entry_id, status, reason=None, task_id=None, error=None):
        marks.append((entry_id, status, reason, task_id, error))

    async def _fake_broadcast(room_id, payload):
        broadcasts.append((room_id, payload))

    monkeypatch.setattr(mew, "get_agent_settings", lambda: _cfg(True))
    monkeypatch.setattr(mew, "pop_due_mention_entries", _fake_pop)
    monkeypatch.setattr(mew, "enqueue_task", _fake_enqueue)
    monkeypatch.setattr(mew, "mark_mention_entry_status", _fake_mark)
    monkeypatch.setattr(mew.manager, "broadcast_to_room", _fake_broadcast)
    worker = mew.MentionEntryWorker()
    processed = await worker._process_once()

    assert processed == 1
    assert len(enqueued) == 1
    assert enqueued[0][0] == "r1"
    assert any(m[0] == "e1" and m[1] == "queued" and m[3] == "t1" for m in marks)
    assert any(p["type"] == "agent:queued" and p["task_id"] == "t1" for _, p in broadcasts)


@pytest.mark.asyncio
async def test_process_once_entry_timeout_dropped(monkeypatch):
    marks = []
    broadcasts = []
    enqueued = []
    entry = {
        "entry_id": "e-timeout",
        "room_id": "r1",
        "agent_role": "facilitator",
        "source_message_id": "m1",
        "trigger_type": "mention",
        "expire_at": "1",
    }

    async def _fake_pop(_limit):
        return [entry]

    async def _fake_enqueue(*_args, **_kwargs):
        enqueued.append(True)
        return {"task_id": "t-never"}

    async def _fake_mark(entry_id, status, reason=None, task_id=None, error=None):
        marks.append((entry_id, status, reason, task_id, error))

    async def _fake_broadcast(room_id, payload):
        broadcasts.append((room_id, payload))

    monkeypatch.setattr(mew, "get_agent_settings", lambda: _cfg(True))
    monkeypatch.setattr(mew, "pop_due_mention_entries", _fake_pop)
    monkeypatch.setattr(mew, "enqueue_task", _fake_enqueue)
    monkeypatch.setattr(mew, "mark_mention_entry_status", _fake_mark)
    monkeypatch.setattr(mew.manager, "broadcast_to_room", _fake_broadcast)
    monkeypatch.setattr(mew.time, "time", lambda: 9999.0)
    worker = mew.MentionEntryWorker()
    processed = await worker._process_once()

    assert processed == 1
    assert not enqueued
    assert any(m[0] == "e-timeout" and m[1] == "dropped" and m[2] == "mention_entry_timeout" for m in marks)
    assert any(p["type"] == "agent:dropped" and p["reason"] == "mention_entry_timeout" for _, p in broadcasts)


@pytest.mark.asyncio
async def test_process_once_enqueue_error_marks_failed(monkeypatch):
    marks = []
    broadcasts = []
    entry = {
        "entry_id": "e-fail",
        "room_id": "r1",
        "agent_role": "facilitator",
        "source_message_id": "m1",
        "student_name": "stu",
        "reason": "rr",
        "strategy": "ss",
        "trigger_type": "mention",
        "expire_at": "9999999999",
    }

    async def _fake_pop(_limit):
        return [entry]

    async def _fake_enqueue(*_args, **_kwargs):
        raise RuntimeError("enqueue boom")

    async def _fake_mark(entry_id, status, reason=None, task_id=None, error=None):
        marks.append((entry_id, status, reason, task_id, error))

    async def _fake_broadcast(room_id, payload):
        broadcasts.append((room_id, payload))

    monkeypatch.setattr(mew, "get_agent_settings", lambda: _cfg(True))
    monkeypatch.setattr(mew, "pop_due_mention_entries", _fake_pop)
    monkeypatch.setattr(mew, "enqueue_task", _fake_enqueue)
    monkeypatch.setattr(mew, "mark_mention_entry_status", _fake_mark)
    monkeypatch.setattr(mew.manager, "broadcast_to_room", _fake_broadcast)
    worker = mew.MentionEntryWorker()
    processed = await worker._process_once()

    assert processed == 1
    assert any(m[0] == "e-fail" and m[1] == "failed" and m[2] == "mention_entry_enqueue_error" for m in marks)
    assert any(p["type"] == "agent:failed" and p["reason"] == "mention_entry_enqueue_error" for _, p in broadcasts)


@pytest.mark.asyncio
async def test_process_once_respects_rate_limit(monkeypatch):
    pop_calls = []

    async def _fake_pop(limit):
        pop_calls.append(limit)
        return []

    monkeypatch.setattr(mew, "get_agent_settings", lambda: _cfg(True, rate=3))
    monkeypatch.setattr(mew, "pop_due_mention_entries", _fake_pop)
    worker = mew.MentionEntryWorker()
    processed = await worker._process_once()

    assert processed == 0
    assert pop_calls == [3]
