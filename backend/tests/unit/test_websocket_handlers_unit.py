import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.models.user import User, UserRole
from app.websocket import handlers


@pytest.fixture(autouse=True)
def _use_multi_agent_mode(monkeypatch):
    async def _mode_multi(_room_id):
        return "multi"

    monkeypatch.setattr(handlers, "get_room_agent_mode", _mode_multi)


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self, value=None):
        self.value = value
        self.calls = 0

    async def execute(self, _stmt):
        self.calls += 1
        return _ScalarResult(self.value)


@pytest.mark.asyncio
async def test_verify_token_returns_none_for_invalid_payload(monkeypatch):
    db = _FakeDB()
    monkeypatch.setattr(handlers, "decode_access_token", lambda _token: {})

    result = await handlers.verify_token("token", db)

    assert result is None
    assert db.calls == 0


@pytest.mark.asyncio
async def test_verify_token_returns_none_for_none_token():
    db = _FakeDB()

    result = await handlers.verify_token(None, db)

    assert result is None
    assert db.calls == 0


@pytest.mark.asyncio
async def test_verify_token_returns_user_for_valid_payload(monkeypatch):
    user_id = uuid.uuid4()
    expected_user = User(
        id=user_id,
        username="student_1",
        password_hash="x",
        display_name="Student 1",
        role=UserRole.student,
    )
    db = _FakeDB(expected_user)
    monkeypatch.setattr(handlers, "decode_access_token", lambda _token: {"sub": str(user_id), "jti": "jti-1"})

    async def _fake_get_active_jti(_user_id):
        return "jti-1"

    monkeypatch.setattr(handlers, "get_user_active_session_jti", _fake_get_active_jti)

    result = await handlers.verify_token("token", db)

    assert result == (expected_user, "jti-1")
    assert db.calls == 1


@pytest.mark.asyncio
async def test_is_room_member_returns_false_for_invalid_room_id():
    db = _FakeDB(value=object())

    result = await handlers.is_room_member(uuid.uuid4(), "not-a-uuid", db)

    assert result is False
    assert db.calls == 0


@pytest.mark.asyncio
async def test_trigger_mentions_legacy_path_enqueues_and_broadcasts_queued(monkeypatch):
    calls = []
    enqueued = []
    created_entries = []
    user = User(
        id=uuid.uuid4(),
        username="student_1",
        password_hash="x",
        display_name="Student 1",
        role=UserRole.student,
    )

    async def _fake_broadcast(room_id, payload):
        calls.append((room_id, payload))

    async def _fake_enqueue(room_id, task, delay_seconds=0):
        enqueued.append((room_id, task, delay_seconds))
        return {"task_id": "task-1", **task}

    async def _fake_create_entry(**kwargs):
        created_entries.append(kwargs)
        return {"entry_id": "entry-1", **kwargs}

    class _Mention:
        enabled = True
        priority = 0
        max_mentions_per_message = 3

    class _Timing:
        mention_entry_enabled = False
        mention_entry_queue_max_wait_sec = 60

    class _Cfg:
        mention = _Mention()
        timing = _Timing()

    monkeypatch.setattr(handlers.manager, "broadcast_to_room", _fake_broadcast)
    monkeypatch.setattr(handlers, "enqueue_task", _fake_enqueue)
    monkeypatch.setattr(handlers, "create_mention_entry", _fake_create_entry)
    monkeypatch.setattr(handlers, "get_agent_settings", lambda: _Cfg())

    await handlers._trigger_mentions(
        room_id="room-1",
        source_message_id="msg-1",
        user=user,
        mentions=[" facilitator ", "UNKNOWN", "facilitator", ""],
    )

    statuses = [payload["status"] for _, payload in calls if payload["type"] == "agent:ack"]
    assert statuses == ["accepted", "unsupported"]
    assert any(payload["type"] == "agent:queued" for _, payload in calls)
    assert len(enqueued) == 1
    assert created_entries == []
    assert enqueued[0][1]["agent_role"] == "facilitator"


@pytest.mark.asyncio
async def test_trigger_mentions_entry_queue_path_creates_entry_and_no_queued(monkeypatch):
    calls = []
    enqueued = []
    created_entries = []
    user = User(
        id=uuid.uuid4(),
        username="student_1",
        password_hash="x",
        display_name="Student 1",
        role=UserRole.student,
    )

    async def _fake_broadcast(room_id, payload):
        calls.append((room_id, payload))

    async def _fake_enqueue(room_id, task, delay_seconds=0):
        enqueued.append((room_id, task, delay_seconds))
        return {"task_id": "task-1", **task}

    async def _fake_create_entry(**kwargs):
        created_entries.append(kwargs)
        return {"entry_id": "entry-1", **kwargs}

    class _Mention:
        enabled = True
        priority = 0
        max_mentions_per_message = 3

    class _Timing:
        mention_entry_enabled = True
        mention_entry_queue_max_wait_sec = 60

    class _Cfg:
        mention = _Mention()
        timing = _Timing()

    monkeypatch.setattr(handlers.manager, "broadcast_to_room", _fake_broadcast)
    monkeypatch.setattr(handlers, "enqueue_task", _fake_enqueue)
    monkeypatch.setattr(handlers, "create_mention_entry", _fake_create_entry)
    monkeypatch.setattr(handlers, "get_agent_settings", lambda: _Cfg())

    await handlers._trigger_mentions(
        room_id="room-1",
        source_message_id="msg-1",
        user=user,
        mentions=[" facilitator "],
    )

    assert enqueued == []
    assert len(created_entries) == 1
    assert created_entries[0]["room_id"] == "room-1"
    assert created_entries[0]["agent_role"] == "facilitator"
    assert created_entries[0]["source_message_id"] == "msg-1"
    assert created_entries[0]["student_name"] == "Student 1"
    assert created_entries[0]["trigger_type"] == "mention"
    assert any(payload["type"] == "agent:ack" for _, payload in calls)
    assert not any(payload["type"] == "agent:queued" for _, payload in calls)
    ack_payload = next(payload for _, payload in calls if payload["type"] == "agent:ack")
    assert ack_payload["entry_id"] == "entry-1"
    assert ack_payload["queue_mode"] == "entry_queue"


@pytest.mark.asyncio
async def test_trigger_mentions_accepts_concept_explainer(monkeypatch):
    calls = []
    enqueued = []
    user = User(
        id=uuid.uuid4(),
        username="student_1",
        password_hash="x",
        display_name="Student 1",
        role=UserRole.student,
    )

    async def _fake_broadcast(room_id, payload):
        calls.append((room_id, payload))

    async def _fake_enqueue(room_id, task, delay_seconds=0):
        enqueued.append((room_id, task, delay_seconds))
        return {"task_id": "task-concept-1", **task}

    async def _fake_create_entry(**kwargs):
        return {"entry_id": "entry-1", **kwargs}

    class _Mention:
        enabled = True
        priority = 0
        max_mentions_per_message = 1

    class _Timing:
        mention_entry_enabled = False
        mention_entry_queue_max_wait_sec = 60

    class _Cfg:
        mention = _Mention()
        timing = _Timing()

    monkeypatch.setattr(handlers.manager, "broadcast_to_room", _fake_broadcast)
    monkeypatch.setattr(handlers, "enqueue_task", _fake_enqueue)
    monkeypatch.setattr(handlers, "create_mention_entry", _fake_create_entry)
    monkeypatch.setattr(handlers, "get_agent_settings", lambda: _Cfg())

    await handlers._trigger_mentions(
        room_id="room-1",
        source_message_id="msg-1",
        user=user,
        mentions=["concept_explainer"],
    )

    ack_payload = next(payload for _, payload in calls if payload.get("type") == "agent:ack")
    assert ack_payload["agent_role"] == "concept_explainer"
    assert ack_payload["status"] == "accepted"

    queued_payload = next(payload for _, payload in calls if payload.get("type") == "agent:queued")
    assert queued_payload["agent_role"] == "concept_explainer"
    assert queued_payload["status"] == "queued"

    assert len(enqueued) == 1
    task_payload = enqueued[0][1]
    assert task_payload["agent_role"] == "concept_explainer"
    assert task_payload["trigger_type"] == "mention"
    assert task_payload["target_dimension"] == "user_request"
    assert task_payload["source_message_id"] == "msg-1"


@pytest.mark.asyncio
async def test_handle_chat_message_broadcasts_and_triggers(monkeypatch):
    created_at = datetime.now(timezone.utc)
    msg = SimpleNamespace(
        id=uuid.uuid4(),
        seq_num=12,
        content="hello",
        created_at=created_at,
    )
    saved = {}
    broadcast_payloads = []
    triggered = {"mentions": None, "monopoly": None}

    async def _fake_save(_db, room_id, user_id, content, mentions, connection_id=None, loadmsg_id=None):
        saved["room_id"] = room_id
        saved["user_id"] = user_id
        saved["content"] = content
        saved["mentions"] = mentions
        saved["connection_id"] = connection_id
        saved["loadmsg_id"] = loadmsg_id
        return msg

    async def _fake_broadcast(_room_id, payload):
        broadcast_payloads.append(payload)

    async def _fake_trigger_mentions(room_id, source_message_id, user, mentions):
        triggered["mentions"] = (room_id, source_message_id, user.display_name, mentions)

    async def _fake_check_monopoly(room_id, sender_id):
        triggered["monopoly"] = (room_id, sender_id)

    user = User(
        id=uuid.uuid4(),
        username="student_1",
        password_hash="x",
        display_name="Student 1",
        role=UserRole.student,
    )

    monkeypatch.setattr(handlers.MessageService, "save_student_message", _fake_save)
    monkeypatch.setattr(handlers.manager, "broadcast_to_room", _fake_broadcast)
    monkeypatch.setattr(handlers, "_trigger_mentions", _fake_trigger_mentions)
    monkeypatch.setattr(handlers.trigger_detector, "check_monopoly", _fake_check_monopoly)
    monkeypatch.setattr(handlers, "get_redis_client", lambda: (_ for _ in ()).throw(RuntimeError("redis down")))

    await handlers.handle_chat_message(
        data={"type": "chat:message", "content": "  hello  ", "mentions": ["facilitator"]},
        room_id="room-1",
        user=user,
        db=object(),
    )

    assert saved["content"] == "hello"
    assert saved["mentions"] == ["facilitator"]
    assert broadcast_payloads[0]["type"] == "chat:new_message"
    assert broadcast_payloads[0]["seq_num"] == 12
    assert triggered["mentions"][3] == ["facilitator"]
    assert triggered["monopoly"] == ("room-1", str(user.id))


@pytest.mark.asyncio
async def test_handle_chat_message_without_mentions_does_not_create_entry(monkeypatch):
    created_at = datetime.now(timezone.utc)
    msg = SimpleNamespace(
        id=uuid.uuid4(),
        seq_num=13,
        content="hello no mention",
        created_at=created_at,
    )
    broadcast_payloads = []
    created_entries = []

    async def _fake_save(_db, _room_id, _user_id, _content, _mentions, connection_id=None, loadmsg_id=None):
        _ = (connection_id, loadmsg_id)
        return msg

    async def _fake_broadcast(_room_id, payload):
        broadcast_payloads.append(payload)

    async def _fake_create_entry(**kwargs):
        created_entries.append(kwargs)
        return {"entry_id": "entry-x", **kwargs}

    async def _fake_check_monopoly(_room_id, _sender_id):
        return None

    user = User(
        id=uuid.uuid4(),
        username="student_1",
        password_hash="x",
        display_name="Student 1",
        role=UserRole.student,
    )

    monkeypatch.setattr(handlers.MessageService, "save_student_message", _fake_save)
    monkeypatch.setattr(handlers.manager, "broadcast_to_room", _fake_broadcast)
    monkeypatch.setattr(handlers.trigger_detector, "check_monopoly", _fake_check_monopoly)
    monkeypatch.setattr(handlers, "create_mention_entry", _fake_create_entry)
    monkeypatch.setattr(handlers, "get_redis_client", lambda: (_ for _ in ()).throw(RuntimeError("redis down")))

    await handlers.handle_chat_message(
        data={"type": "chat:message", "content": "hello no mention", "mentions": []},
        room_id="room-1",
        user=user,
        db=object(),
    )

    assert broadcast_payloads
    assert broadcast_payloads[0]["type"] == "chat:new_message"
    assert created_entries == []


@pytest.mark.asyncio
async def test_handle_chat_message_blocks_mentions_when_agent_busy(monkeypatch):
    saved = {"called": False}
    ws_sent = []

    class _FakeRedis:
        async def exists(self, key):
            return key == "room:room-1:agent_lock"

        async def zcard(self, _key):
            return 0

    class _FakeWS:
        async def send_json(self, payload):
            ws_sent.append(payload)

    async def _fake_save(*_args, **_kwargs):
        saved["called"] = True

    user = User(
        id=uuid.uuid4(),
        username="student_1",
        password_hash="x",
        display_name="Student 1",
        role=UserRole.student,
    )

    monkeypatch.setattr(handlers, "get_redis_client", lambda: _FakeRedis())
    monkeypatch.setattr(handlers.MessageService, "save_student_message", _fake_save)

    await handlers.handle_chat_message(
        data={"type": "chat:message", "content": "hello", "mentions": ["resource_finder"]},
        room_id="room-1",
        user=user,
        db=object(),
        websocket=_FakeWS(),
    )

    assert saved["called"] is False
    assert ws_sent
    assert ws_sent[0]["type"] == "agent:mention_blocked"


@pytest.mark.asyncio
async def test_handle_chat_message_blocks_mentions_when_agent_cooling(monkeypatch):
    saved = {"called": False}
    ws_sent = []

    class _FakeRedis:
        async def exists(self, key):
            return key == "cooldown:room-1:resource_finder"

        async def zcard(self, _key):
            return 0

        async def ttl(self, key):
            if key == "cooldown:room-1:resource_finder":
                return 4
            return -2

    class _FakeWS:
        async def send_json(self, payload):
            ws_sent.append(payload)

    async def _fake_save(*_args, **_kwargs):
        saved["called"] = True

    user = User(
        id=uuid.uuid4(),
        username="student_1",
        password_hash="x",
        display_name="Student 1",
        role=UserRole.student,
    )

    monkeypatch.setattr(handlers, "get_redis_client", lambda: _FakeRedis())
    monkeypatch.setattr(handlers.MessageService, "save_student_message", _fake_save)

    await handlers.handle_chat_message(
        data={"type": "chat:message", "content": "hello", "mentions": ["resource_finder"]},
        room_id="room-1",
        user=user,
        db=object(),
        websocket=_FakeWS(),
    )

    assert saved["called"] is False
    assert ws_sent
    assert ws_sent[0]["type"] == "agent:mention_blocked"
    assert ws_sent[0]["reason"] == "agent_cooling"
    assert ws_sent[0]["agent_role"] == "resource_finder"
    assert "4" in ws_sent[0]["message"]
