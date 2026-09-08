from datetime import datetime, timedelta, timezone

import pytest

from app.analysis import scheduler


@pytest.fixture(autouse=True)
def _use_multi_agent_mode(monkeypatch):
    async def _mode_multi(_room_id):
        return "multi"

    monkeypatch.setattr(scheduler, "get_room_agent_mode", _mode_multi)


class _FakeRedis:
    def __init__(self, active_rooms, values=None):
        self._active_rooms = active_rooms
        self._values = values or {}
        self._locks = set()

    async def smembers(self, _key):
        return list(self._active_rooms)

    async def get(self, key):
        return self._values.get(key)

    async def exists(self, key):
        return key in self._locks

    async def setex(self, key, _ttl, _value):
        self._locks.add(key)


class _AutoSpeak:
    facilitator_silence_enabled = True
    time_progress_enabled = True


class _Timing:
    rule_trigger_marker_ttl_seconds = 180
    time_progress_jitter_enabled = False


class _Cfg:
    auto_speak = _AutoSpeak()
    timing = _Timing()


class _CfgTimeProgressOff:
    class _Auto:
        facilitator_silence_enabled = True
        time_progress_enabled = False

    auto_speak = _Auto()
    timing = _Timing()


@pytest.mark.asyncio
async def test_time_progress_reminder_triggers_once_per_node(monkeypatch):
    room_id = "room-1"
    now = 10000.0
    started_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fake_redis = _FakeRedis(
        active_rooms=[room_id],
        values={f"room:{room_id}:last_activity_time": str(now - 30)},
    )
    enqueued = []

    async def _fake_enqueue(room_id_arg, payload):
        enqueued.append((room_id_arg, payload))

    async def _fake_elapsed(_room_id):
        return 46 * 60.0

    async def _fake_snapshot(_room_id):
        return {
            "timer_started_at": started_at,
            "script_state": {
                "current_status": "status",
                "next_goal": "goal",
                "history": [{"id": "h1"}],
                "pending_proposal": None,
            },
        }

    async def _fake_submit_state(_room_id):
        return {"confirmations": [], "final_submitted_at": None}

    monkeypatch.setattr(scheduler, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(scheduler, "get_agent_settings", lambda: _Cfg())
    monkeypatch.setattr(scheduler, "enqueue_task", _fake_enqueue)
    monkeypatch.setattr(scheduler.time, "time", lambda: now)
    monkeypatch.setattr(scheduler, "get_elapsed_seconds_from_timer_start", _fake_elapsed)
    monkeypatch.setattr(scheduler, "_load_room_snapshot", _fake_snapshot)
    monkeypatch.setattr(scheduler.writing_submit_service, "get_writing_submit_state", _fake_submit_state)

    await scheduler.check_time_progress_reminders()
    await scheduler.check_time_progress_reminders()

    assert len(enqueued) == 1
    assert enqueued[0][0] == room_id
    assert enqueued[0][1]["trigger_type"] == "time_progress"
    assert enqueued[0][1]["node_minutes"] == 45
    assert enqueued[0][1]["progress_status"] == "normal"
    assert await fake_redis.exists(f"recent_rule_trigger:{room_id}:time_progress")


@pytest.mark.asyncio
async def test_time_progress_reminder_marks_late_phase_slow(monkeypatch):
    room_id = "room-2"
    now = 20000.0
    started_at = datetime.now(timezone.utc) - timedelta(minutes=96)
    fake_redis = _FakeRedis(
        active_rooms=[room_id],
        values={f"room:{room_id}:last_activity_time": str(now - 1200)},
    )
    enqueued = []

    async def _fake_enqueue(room_id_arg, payload):
        enqueued.append((room_id_arg, payload))

    async def _fake_elapsed(_room_id):
        return 96 * 60.0

    async def _fake_snapshot(_room_id):
        return {
            "timer_started_at": started_at,
            "script_state": {
                "current_status": "暂无",
                "next_goal": "",
                "history": [],
                "pending_proposal": None,
            },
        }

    async def _fake_submit_state(_room_id):
        return {"confirmations": [], "final_submitted_at": None}

    monkeypatch.setattr(scheduler, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(scheduler, "get_agent_settings", lambda: _Cfg())
    monkeypatch.setattr(scheduler, "enqueue_task", _fake_enqueue)
    monkeypatch.setattr(scheduler.time, "time", lambda: now)
    monkeypatch.setattr(scheduler, "get_elapsed_seconds_from_timer_start", _fake_elapsed)
    monkeypatch.setattr(scheduler, "_load_room_snapshot", _fake_snapshot)
    monkeypatch.setattr(scheduler.writing_submit_service, "get_writing_submit_state", _fake_submit_state)

    await scheduler.check_time_progress_reminders()

    assert len(enqueued) == 1
    payload = enqueued[0][1]
    assert payload["node_minutes"] == 95
    assert payload["current_phase"] == "late"
    assert payload["progress_status"] == "slow"


@pytest.mark.asyncio
async def test_time_progress_respects_dedicated_toggle(monkeypatch):
    room_id = "room-3"
    fake_redis = _FakeRedis(active_rooms=[room_id], values={})
    enqueued = []

    async def _fake_enqueue(room_id_arg, payload):
        enqueued.append((room_id_arg, payload))

    async def _fake_elapsed(_room_id):
        return 40 * 60.0

    monkeypatch.setattr(scheduler, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(scheduler, "get_agent_settings", lambda: _CfgTimeProgressOff())
    monkeypatch.setattr(scheduler, "enqueue_task", _fake_enqueue)
    monkeypatch.setattr(scheduler, "get_elapsed_seconds_from_timer_start", _fake_elapsed)

    await scheduler.check_time_progress_reminders()
    assert enqueued == []
