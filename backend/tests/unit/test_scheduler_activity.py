from datetime import datetime, timezone

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
        self._ttls = {}

    async def smembers(self, _key):
        return list(self._active_rooms)

    async def get(self, key):
        return self._values.get(key)

    async def exists(self, key):
        return key in self._locks

    async def setex(self, key, _ttl, _val):
        self._locks.add(key)
        self._ttls[key] = int(_ttl)
        self._values[key] = str(_val)


class _Timing:
    silence_trigger_enabled = True
    silence_threshold_seconds = 120
    warmup_minutes = 3
    rule_trigger_marker_ttl_seconds = 180
    room_auto_intervention_cooldown_seconds = 180
    time_progress_jitter_enabled = True
    time_progress_jitter_min_seconds = 30
    time_progress_jitter_max_seconds = 90
    emotional_support_enabled = True
    emotional_support_check_interval_seconds = 30
    emotional_support_keywords = [
        "不会",
        "不知道",
        "太难",
        "好难",
        "算了",
        "烦",
        "好烦",
        "没人说",
        "随便",
        "不想",
        "没意思",
        "做不下去",
        "卡住",
        "放弃",
    ]
    emotional_support_recent_window_seconds = 120
    emotional_support_cooldown_seconds = 180


class _AutoSpeak:
    facilitator_silence_enabled = True
    time_progress_enabled = True


class _Cfg:
    timing = _Timing()
    auto_speak = _AutoSpeak()


@pytest.mark.asyncio
async def test_check_silence_uses_last_msg_time_as_episode_anchor(monkeypatch):
    now = 1_000.0
    room_id = "room-1"
    fake_redis = _FakeRedis(
        active_rooms=[room_id],
        values={
            f"room:{room_id}:last_activity_time": str(now - 30),   # system activity
            f"room:{room_id}:last_msg_time": str(now - 400),       # chat activity
        },
    )
    enqueued = []

    async def _fake_enqueue(room_id_arg, payload):
        enqueued.append((room_id_arg, payload))

    async def _fake_elapsed(_room_id):
        return 1_000.0

    monkeypatch.setattr(scheduler, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(scheduler, "get_agent_settings", lambda: _Cfg())
    monkeypatch.setattr(scheduler, "enqueue_task", _fake_enqueue)
    monkeypatch.setattr(scheduler.time, "time", lambda: now)
    monkeypatch.setattr(scheduler, "get_elapsed_seconds_from_timer_start", _fake_elapsed)

    await scheduler.check_silence()

    assert len(enqueued) == 1
    assert enqueued[0][0] == room_id
    assert fake_redis._values[f"silence_triggered_activity:{room_id}"] == str(now - 400)


@pytest.mark.asyncio
async def test_check_silence_falls_back_to_last_msg_time(monkeypatch):
    now = 2_000.0
    room_id = "room-2"
    fake_redis = _FakeRedis(
        active_rooms=[room_id],
        values={
            f"room:{room_id}:last_msg_time": str(now - 300),  # no last_activity_time
        },
    )
    enqueued = []

    async def _fake_enqueue(room_id_arg, payload):
        enqueued.append((room_id_arg, payload))

    async def _fake_elapsed(_room_id):
        return 1_000.0

    monkeypatch.setattr(scheduler, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(scheduler, "get_agent_settings", lambda: _Cfg())
    monkeypatch.setattr(scheduler, "enqueue_task", _fake_enqueue)
    monkeypatch.setattr(scheduler.time, "time", lambda: now)
    monkeypatch.setattr(scheduler, "get_elapsed_seconds_from_timer_start", _fake_elapsed)

    await scheduler.check_silence()

    assert len(enqueued) == 1
    assert enqueued[0][0] == room_id
    assert enqueued[0][1]["trigger_type"] == "silence"
    assert await fake_redis.exists(f"recent_rule_trigger:{room_id}:silence")
    assert await fake_redis.exists(f"silence_triggered_activity:{room_id}")
    assert fake_redis._values[f"silence_triggered_activity:{room_id}"] == str(now - 300)


@pytest.mark.asyncio
async def test_check_silence_skips_when_recent_rule_trigger_exists(monkeypatch):
    now = 3_000.0
    room_id = "room-3"
    fake_redis = _FakeRedis(
        active_rooms=[room_id],
        values={
            f"room:{room_id}:last_activity_time": str(now - 400),
        },
    )
    fake_redis._locks.add(f"recent_rule_trigger:{room_id}:silence")
    enqueued = []

    async def _fake_enqueue(room_id_arg, payload):
        enqueued.append((room_id_arg, payload))

    async def _fake_elapsed(_room_id):
        return 1_000.0

    monkeypatch.setattr(scheduler, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(scheduler, "get_agent_settings", lambda: _Cfg())
    monkeypatch.setattr(scheduler, "enqueue_task", _fake_enqueue)
    monkeypatch.setattr(scheduler.time, "time", lambda: now)
    monkeypatch.setattr(scheduler, "get_elapsed_seconds_from_timer_start", _fake_elapsed)

    await scheduler.check_silence()

    assert enqueued == []


@pytest.mark.asyncio
async def test_check_silence_sets_recent_rule_ttl_with_longer_window(monkeypatch):
    now = 4_000.0
    room_id = "room-4"
    fake_redis = _FakeRedis(
        active_rooms=[room_id],
        values={
            f"room:{room_id}:last_activity_time": str(now - 400),
        },
    )
    enqueued = []

    async def _fake_enqueue(room_id_arg, payload):
        enqueued.append((room_id_arg, payload))

    async def _fake_elapsed(_room_id):
        return 1_000.0

    monkeypatch.setattr(scheduler, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(scheduler, "get_agent_settings", lambda: _Cfg())
    monkeypatch.setattr(scheduler, "enqueue_task", _fake_enqueue)
    monkeypatch.setattr(scheduler.time, "time", lambda: now)
    monkeypatch.setattr(scheduler, "get_elapsed_seconds_from_timer_start", _fake_elapsed)

    await scheduler.check_silence()

    recent_key = f"recent_rule_trigger:{room_id}:silence"
    lock_key = f"trigger_lock:{room_id}:silence"
    episode_key = f"silence_triggered_activity:{room_id}"
    assert len(enqueued) == 1
    assert await fake_redis.exists(recent_key)
    assert await fake_redis.exists(lock_key)
    assert await fake_redis.exists(episode_key)
    assert fake_redis._ttls[recent_key] > _Timing.rule_trigger_marker_ttl_seconds
    assert fake_redis._ttls[episode_key] == scheduler.SILENCE_EPISODE_MARKER_TTL_SECONDS


@pytest.mark.asyncio
async def test_check_silence_trigger_lock_still_prevents_enqueue(monkeypatch):
    now = 5_000.0
    room_id = "room-5"
    fake_redis = _FakeRedis(
        active_rooms=[room_id],
        values={
            f"room:{room_id}:last_activity_time": str(now - 500),
        },
    )
    fake_redis._locks.add(f"trigger_lock:{room_id}:silence")
    enqueued = []

    async def _fake_enqueue(room_id_arg, payload):
        enqueued.append((room_id_arg, payload))

    async def _fake_elapsed(_room_id):
        return 1_000.0

    monkeypatch.setattr(scheduler, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(scheduler, "get_agent_settings", lambda: _Cfg())
    monkeypatch.setattr(scheduler, "enqueue_task", _fake_enqueue)
    monkeypatch.setattr(scheduler.time, "time", lambda: now)
    monkeypatch.setattr(scheduler, "get_elapsed_seconds_from_timer_start", _fake_elapsed)

    await scheduler.check_silence()

    assert enqueued == []


@pytest.mark.asyncio
async def test_check_silence_skips_when_same_episode_marker_exists(monkeypatch):
    now = 6_000.0
    room_id = "room-6"
    activity = str(now - 500)
    fake_redis = _FakeRedis(
        active_rooms=[room_id],
        values={
            f"room:{room_id}:last_msg_time": activity,
            f"room:{room_id}:last_activity_time": str(now - 10),
            f"silence_triggered_activity:{room_id}": activity,
        },
    )
    enqueued = []

    async def _fake_enqueue(room_id_arg, payload):
        enqueued.append((room_id_arg, payload))

    async def _fake_elapsed(_room_id):
        return 1_000.0

    monkeypatch.setattr(scheduler, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(scheduler, "get_agent_settings", lambda: _Cfg())
    monkeypatch.setattr(scheduler, "enqueue_task", _fake_enqueue)
    monkeypatch.setattr(scheduler.time, "time", lambda: now)
    monkeypatch.setattr(scheduler, "get_elapsed_seconds_from_timer_start", _fake_elapsed)

    await scheduler.check_silence()

    assert enqueued == []


@pytest.mark.asyncio
async def test_check_silence_allows_when_episode_marker_differs_and_refreshes(monkeypatch):
    now = 7_000.0
    room_id = "room-7"
    current_activity = str(now - 500)
    fake_redis = _FakeRedis(
        active_rooms=[room_id],
        values={
            f"room:{room_id}:last_msg_time": current_activity,
            f"room:{room_id}:last_activity_time": str(now - 20),
            f"silence_triggered_activity:{room_id}": str(now - 900),
        },
    )
    enqueued = []

    async def _fake_enqueue(room_id_arg, payload):
        enqueued.append((room_id_arg, payload))

    async def _fake_elapsed(_room_id):
        return 1_000.0

    monkeypatch.setattr(scheduler, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(scheduler, "get_agent_settings", lambda: _Cfg())
    monkeypatch.setattr(scheduler, "enqueue_task", _fake_enqueue)
    monkeypatch.setattr(scheduler.time, "time", lambda: now)
    monkeypatch.setattr(scheduler, "get_elapsed_seconds_from_timer_start", _fake_elapsed)

    await scheduler.check_silence()

    episode_key = f"silence_triggered_activity:{room_id}"
    assert len(enqueued) == 1
    assert fake_redis._values[episode_key] == current_activity


@pytest.mark.asyncio
async def test_check_silence_does_not_reenqueue_when_last_activity_changes_but_last_msg_same(monkeypatch):
    now = 8_000.0
    room_id = "room-8"
    msg_anchor = str(now - 500)
    fake_redis = _FakeRedis(
        active_rooms=[room_id],
        values={
            f"room:{room_id}:last_msg_time": msg_anchor,
            f"room:{room_id}:last_activity_time": str(now - 5),
            f"silence_triggered_activity:{room_id}": msg_anchor,
        },
    )
    enqueued = []

    async def _fake_enqueue(room_id_arg, payload):
        enqueued.append((room_id_arg, payload))

    async def _fake_elapsed(_room_id):
        return 1_000.0

    monkeypatch.setattr(scheduler, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(scheduler, "get_agent_settings", lambda: _Cfg())
    monkeypatch.setattr(scheduler, "enqueue_task", _fake_enqueue)
    monkeypatch.setattr(scheduler.time, "time", lambda: now)
    monkeypatch.setattr(scheduler, "get_elapsed_seconds_from_timer_start", _fake_elapsed)

    await scheduler.check_silence()

    assert enqueued == []


@pytest.mark.asyncio
async def test_check_silence_allows_new_episode_when_last_msg_changes(monkeypatch):
    now = 9_000.0
    room_id = "room-9"
    old_msg_anchor = str(now - 700)
    new_msg_anchor = str(now - 500)
    fake_redis = _FakeRedis(
        active_rooms=[room_id],
        values={
            f"room:{room_id}:last_msg_time": new_msg_anchor,
            f"room:{room_id}:last_activity_time": str(now - 2),
            f"silence_triggered_activity:{room_id}": old_msg_anchor,
        },
    )
    enqueued = []

    async def _fake_enqueue(room_id_arg, payload):
        enqueued.append((room_id_arg, payload))

    async def _fake_elapsed(_room_id):
        return 1_000.0

    monkeypatch.setattr(scheduler, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(scheduler, "get_agent_settings", lambda: _Cfg())
    monkeypatch.setattr(scheduler, "enqueue_task", _fake_enqueue)
    monkeypatch.setattr(scheduler.time, "time", lambda: now)
    monkeypatch.setattr(scheduler, "get_elapsed_seconds_from_timer_start", _fake_elapsed)

    await scheduler.check_silence()

    assert len(enqueued) == 1
    assert fake_redis._values[f"silence_triggered_activity:{room_id}"] == new_msg_anchor


@pytest.mark.asyncio
async def test_time_progress_jitter_schedules_without_immediate_enqueue(monkeypatch):
    now = 1000.0
    room_id = "tp-room-1"
    fake_redis = _FakeRedis(active_rooms=[room_id], values={f"room:{room_id}:last_activity_time": str(now - 10)})
    enqueued = []

    async def _fake_enqueue(room_id_arg, payload):
        enqueued.append((room_id_arg, payload))

    async def _fake_elapsed(_room_id):
        return 20 * 60

    async def _fake_snapshot(_room_id):
        return {"timer_started_at": datetime.fromtimestamp(100, tz=timezone.utc), "script_state": {}}

    async def _fake_submit_state(_room_id):
        return {"confirmations": []}

    monkeypatch.setattr(scheduler, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(scheduler, "get_agent_settings", lambda: _Cfg())
    monkeypatch.setattr(scheduler, "enqueue_task", _fake_enqueue)
    monkeypatch.setattr(scheduler.time, "time", lambda: now)
    monkeypatch.setattr(scheduler, "get_elapsed_seconds_from_timer_start", _fake_elapsed)
    monkeypatch.setattr(scheduler, "_load_room_snapshot", _fake_snapshot)
    monkeypatch.setattr(scheduler.writing_submit_service, "get_writing_submit_state", _fake_submit_state)

    await scheduler.check_time_progress_reminders()

    assert enqueued == []
    assert await fake_redis.exists(f"time_progress_scheduled:{room_id}:20")


@pytest.mark.asyncio
async def test_time_progress_jitter_waits_until_due_then_enqueue(monkeypatch):
    base_now = 2000.0
    room_id = "tp-room-2"
    fake_redis = _FakeRedis(
        active_rooms=[room_id],
        values={
            f"room:{room_id}:last_activity_time": str(base_now - 10),
            f"time_progress_scheduled:{room_id}:20": '{"scheduled_at":1900.0,"due_at":2050.0,"node_minutes":20,"jitter_seconds":50}',
        },
    )
    fake_redis._locks.add(f"time_progress_scheduled:{room_id}:20")
    enqueued = []

    async def _fake_enqueue(room_id_arg, payload):
        enqueued.append((room_id_arg, payload))

    async def _fake_elapsed(_room_id):
        return 20 * 60

    async def _fake_snapshot(_room_id):
        return {"timer_started_at": datetime.fromtimestamp(100, tz=timezone.utc), "script_state": {}}

    async def _fake_submit_state(_room_id):
        return {"confirmations": []}

    monkeypatch.setattr(scheduler, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(scheduler, "get_agent_settings", lambda: _Cfg())
    monkeypatch.setattr(scheduler, "enqueue_task", _fake_enqueue)
    monkeypatch.setattr(scheduler, "get_elapsed_seconds_from_timer_start", _fake_elapsed)
    monkeypatch.setattr(scheduler, "_load_room_snapshot", _fake_snapshot)
    monkeypatch.setattr(scheduler.writing_submit_service, "get_writing_submit_state", _fake_submit_state)

    monkeypatch.setattr(scheduler.time, "time", lambda: 2040.0)
    await scheduler.check_time_progress_reminders()
    assert enqueued == []

    monkeypatch.setattr(scheduler.time, "time", lambda: 2051.0)
    await scheduler.check_time_progress_reminders()
    assert len(enqueued) == 1
    assert enqueued[0][0] == room_id
    assert enqueued[0][1]["trigger_type"] == "time_progress"


@pytest.mark.asyncio
async def test_time_progress_jitter_is_stable_for_same_room_and_node():
    v1 = scheduler._stable_time_progress_jitter_seconds(
        room_id="stable-room",
        node_minutes=20,
        min_seconds=30,
        max_seconds=90,
    )
    v2 = scheduler._stable_time_progress_jitter_seconds(
        room_id="stable-room",
        node_minutes=20,
        min_seconds=30,
        max_seconds=90,
    )
    assert v1 == v2
    assert 30 <= v1 <= 90


@pytest.mark.asyncio
async def test_time_progress_jitter_existing_schedule_does_not_reschedule(monkeypatch):
    now = 3000.0
    room_id = "tp-room-3"
    original_payload = '{"scheduled_at":2900.0,"due_at":4000.0,"node_minutes":20,"jitter_seconds":60}'
    fake_redis = _FakeRedis(
        active_rooms=[room_id],
        values={
            f"room:{room_id}:last_activity_time": str(now - 10),
            f"time_progress_scheduled:{room_id}:20": original_payload,
        },
    )
    fake_redis._locks.add(f"time_progress_scheduled:{room_id}:20")
    enqueued = []

    async def _fake_enqueue(room_id_arg, payload):
        enqueued.append((room_id_arg, payload))

    async def _fake_elapsed(_room_id):
        return 20 * 60

    async def _fake_snapshot(_room_id):
        return {"timer_started_at": datetime.fromtimestamp(100, tz=timezone.utc), "script_state": {}}

    async def _fake_submit_state(_room_id):
        return {"confirmations": []}

    monkeypatch.setattr(scheduler, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(scheduler, "get_agent_settings", lambda: _Cfg())
    monkeypatch.setattr(scheduler, "enqueue_task", _fake_enqueue)
    monkeypatch.setattr(scheduler.time, "time", lambda: now)
    monkeypatch.setattr(scheduler, "get_elapsed_seconds_from_timer_start", _fake_elapsed)
    monkeypatch.setattr(scheduler, "_load_room_snapshot", _fake_snapshot)
    monkeypatch.setattr(scheduler.writing_submit_service, "get_writing_submit_state", _fake_submit_state)

    await scheduler.check_time_progress_reminders()
    assert enqueued == []
    assert fake_redis._values[f"time_progress_scheduled:{room_id}:20"] == original_payload


@pytest.mark.asyncio
async def test_time_progress_lock_marker_prevents_duplicate_enqueue(monkeypatch):
    now = 4000.0
    room_id = "tp-room-4"
    fake_redis = _FakeRedis(
        active_rooms=[room_id],
        values={f"room:{room_id}:last_activity_time": str(now - 10)},
    )
    fake_redis._locks.add(f"trigger_lock:{room_id}:time_progress:100:20")
    enqueued = []

    async def _fake_enqueue(room_id_arg, payload):
        enqueued.append((room_id_arg, payload))

    async def _fake_elapsed(_room_id):
        return 20 * 60

    async def _fake_snapshot(_room_id):
        return {"timer_started_at": datetime.fromtimestamp(100, tz=timezone.utc), "script_state": {}}

    async def _fake_submit_state(_room_id):
        return {"confirmations": []}

    monkeypatch.setattr(scheduler, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(scheduler, "get_agent_settings", lambda: _Cfg())
    monkeypatch.setattr(scheduler, "enqueue_task", _fake_enqueue)
    monkeypatch.setattr(scheduler.time, "time", lambda: now)
    monkeypatch.setattr(scheduler, "get_elapsed_seconds_from_timer_start", _fake_elapsed)
    monkeypatch.setattr(scheduler, "_load_room_snapshot", _fake_snapshot)
    monkeypatch.setattr(scheduler.writing_submit_service, "get_writing_submit_state", _fake_submit_state)

    await scheduler.check_time_progress_reminders()
    assert enqueued == []


@pytest.mark.asyncio
async def test_time_progress_jitter_disabled_keeps_old_immediate_behavior(monkeypatch):
    class _TimingNoJitter(_Timing):
        time_progress_jitter_enabled = False

    class _CfgNoJitter:
        timing = _TimingNoJitter()
        auto_speak = _AutoSpeak()

    now = 5000.0
    room_id = "tp-room-5"
    fake_redis = _FakeRedis(active_rooms=[room_id], values={f"room:{room_id}:last_activity_time": str(now - 10)})
    enqueued = []

    async def _fake_enqueue(room_id_arg, payload):
        enqueued.append((room_id_arg, payload))

    async def _fake_elapsed(_room_id):
        return 20 * 60

    async def _fake_snapshot(_room_id):
        return {"timer_started_at": datetime.fromtimestamp(100, tz=timezone.utc), "script_state": {}}

    async def _fake_submit_state(_room_id):
        return {"confirmations": []}

    monkeypatch.setattr(scheduler, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(scheduler, "get_agent_settings", lambda: _CfgNoJitter())
    monkeypatch.setattr(scheduler, "enqueue_task", _fake_enqueue)
    monkeypatch.setattr(scheduler.time, "time", lambda: now)
    monkeypatch.setattr(scheduler, "get_elapsed_seconds_from_timer_start", _fake_elapsed)
    monkeypatch.setattr(scheduler, "_load_room_snapshot", _fake_snapshot)
    monkeypatch.setattr(scheduler.writing_submit_service, "get_writing_submit_state", _fake_submit_state)

    await scheduler.check_time_progress_reminders()

    assert len(enqueued) == 1
    assert enqueued[0][1]["trigger_type"] == "time_progress"


@pytest.mark.asyncio
async def test_emotional_support_enqueues_encourager_on_keyword(monkeypatch):
    room_id = "emo-room-1"
    fake_redis = _FakeRedis(active_rooms=[room_id], values={})
    enqueued = []

    async def _fake_enqueue(room_id_arg, payload):
        enqueued.append((room_id_arg, payload))

    async def _fake_recent_students(_room_id, _window):
        return [
            {
                "id": "msg-1",
                "user_id": "u-1",
                "content": "我不想做了，好烦",
                "created_at": 0.0,
            }
        ]

    async def _fake_has_recent_encourager(_room_id, _window):
        return False

    monkeypatch.setattr(scheduler, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(scheduler, "get_agent_settings", lambda: _Cfg())
    monkeypatch.setattr(scheduler, "enqueue_task", _fake_enqueue)
    monkeypatch.setattr(scheduler, "_load_recent_student_messages", _fake_recent_students)
    monkeypatch.setattr(scheduler, "_has_recent_encourager_reply", _fake_has_recent_encourager)

    await scheduler.check_emotional_support()

    assert len(enqueued) == 1
    assert enqueued[0][0] == room_id
    assert enqueued[0][1]["agent_role"] == "encourager"
    assert enqueued[0][1]["trigger_type"] == "emotional_support"


@pytest.mark.asyncio
async def test_emotional_support_no_keyword_no_enqueue(monkeypatch):
    room_id = "emo-room-2"
    fake_redis = _FakeRedis(active_rooms=[room_id], values={})
    enqueued = []

    async def _fake_enqueue(room_id_arg, payload):
        enqueued.append((room_id_arg, payload))

    async def _fake_recent_students(_room_id, _window):
        return [{"id": "msg-2", "user_id": "u-2", "content": "我们继续", "created_at": 0.0}]

    async def _fake_has_recent_encourager(_room_id, _window):
        return False

    monkeypatch.setattr(scheduler, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(scheduler, "get_agent_settings", lambda: _Cfg())
    monkeypatch.setattr(scheduler, "enqueue_task", _fake_enqueue)
    monkeypatch.setattr(scheduler, "_load_recent_student_messages", _fake_recent_students)
    monkeypatch.setattr(scheduler, "_has_recent_encourager_reply", _fake_has_recent_encourager)

    await scheduler.check_emotional_support()
    assert enqueued == []


@pytest.mark.asyncio
async def test_emotional_support_skip_when_cooldown_exists(monkeypatch):
    room_id = "emo-room-3"
    fake_redis = _FakeRedis(active_rooms=[room_id], values={})
    fake_redis._locks.add(f"recent_rule_trigger:{room_id}:emotional_support")
    enqueued = []

    async def _fake_enqueue(room_id_arg, payload):
        enqueued.append((room_id_arg, payload))

    async def _fake_recent_students(_room_id, _window):
        return [{"id": "msg-3", "user_id": "u-3", "content": "太难了", "created_at": 0.0}]

    async def _fake_has_recent_encourager(_room_id, _window):
        return False

    monkeypatch.setattr(scheduler, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(scheduler, "get_agent_settings", lambda: _Cfg())
    monkeypatch.setattr(scheduler, "enqueue_task", _fake_enqueue)
    monkeypatch.setattr(scheduler, "_load_recent_student_messages", _fake_recent_students)
    monkeypatch.setattr(scheduler, "_has_recent_encourager_reply", _fake_has_recent_encourager)

    await scheduler.check_emotional_support()
    assert enqueued == []


@pytest.mark.asyncio
async def test_emotional_support_skip_when_recent_encourager_exists(monkeypatch):
    room_id = "emo-room-4"
    fake_redis = _FakeRedis(active_rooms=[room_id], values={})
    enqueued = []

    async def _fake_enqueue(room_id_arg, payload):
        enqueued.append((room_id_arg, payload))

    async def _fake_recent_students(_room_id, _window):
        return [{"id": "msg-4", "user_id": "u-4", "content": "不知道怎么做", "created_at": 0.0}]

    async def _fake_has_recent_encourager(_room_id, _window):
        return True

    monkeypatch.setattr(scheduler, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(scheduler, "get_agent_settings", lambda: _Cfg())
    monkeypatch.setattr(scheduler, "enqueue_task", _fake_enqueue)
    monkeypatch.setattr(scheduler, "_load_recent_student_messages", _fake_recent_students)
    monkeypatch.setattr(scheduler, "_has_recent_encourager_reply", _fake_has_recent_encourager)

    await scheduler.check_emotional_support()
    assert enqueued == []


@pytest.mark.asyncio
async def test_emotional_support_agent_message_keyword_does_not_trigger(monkeypatch):
    room_id = "emo-room-5"
    fake_redis = _FakeRedis(active_rooms=[room_id], values={})
    enqueued = []

    async def _fake_enqueue(room_id_arg, payload):
        enqueued.append((room_id_arg, payload))

    async def _fake_recent_students(_room_id, _window):
        return []

    async def _fake_has_recent_encourager(_room_id, _window):
        return False

    monkeypatch.setattr(scheduler, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(scheduler, "get_agent_settings", lambda: _Cfg())
    monkeypatch.setattr(scheduler, "enqueue_task", _fake_enqueue)
    monkeypatch.setattr(scheduler, "_load_recent_student_messages", _fake_recent_students)
    monkeypatch.setattr(scheduler, "_has_recent_encourager_reply", _fake_has_recent_encourager)

    await scheduler.check_emotional_support()
    assert enqueued == []


@pytest.mark.asyncio
async def test_emotional_support_skip_when_disabled(monkeypatch):
    class _TimingDisabled(_Timing):
        emotional_support_enabled = False

    class _CfgDisabled:
        timing = _TimingDisabled()
        auto_speak = _AutoSpeak()

    room_id = "emo-room-6"
    fake_redis = _FakeRedis(active_rooms=[room_id], values={})
    enqueued = []

    async def _fake_enqueue(room_id_arg, payload):
        enqueued.append((room_id_arg, payload))

    async def _fake_recent_students(_room_id, _window):
        return [{"id": "msg-6", "user_id": "u-6", "content": "不想", "created_at": 0.0}]

    async def _fake_has_recent_encourager(_room_id, _window):
        return False

    monkeypatch.setattr(scheduler, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(scheduler, "get_agent_settings", lambda: _CfgDisabled())
    monkeypatch.setattr(scheduler, "enqueue_task", _fake_enqueue)
    monkeypatch.setattr(scheduler, "_load_recent_student_messages", _fake_recent_students)
    monkeypatch.setattr(scheduler, "_has_recent_encourager_reply", _fake_has_recent_encourager)

    await scheduler.check_emotional_support()
    assert enqueued == []


@pytest.mark.asyncio
async def test_emotional_support_skip_when_trigger_lock_exists(monkeypatch):
    room_id = "emo-room-7"
    fake_redis = _FakeRedis(active_rooms=[room_id], values={})
    fake_redis._locks.add(f"trigger_lock:{room_id}:emotional_support")
    enqueued = []

    async def _fake_enqueue(room_id_arg, payload):
        enqueued.append((room_id_arg, payload))

    async def _fake_recent_students(_room_id, _window):
        return [{"id": "msg-7", "user_id": "u-7", "content": "太难", "created_at": 0.0}]

    async def _fake_has_recent_encourager(_room_id, _window):
        return False

    monkeypatch.setattr(scheduler, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(scheduler, "get_agent_settings", lambda: _Cfg())
    monkeypatch.setattr(scheduler, "enqueue_task", _fake_enqueue)
    monkeypatch.setattr(scheduler, "_load_recent_student_messages", _fake_recent_students)
    monkeypatch.setattr(scheduler, "_has_recent_encourager_reply", _fake_has_recent_encourager)

    await scheduler.check_emotional_support()
    assert enqueued == []
