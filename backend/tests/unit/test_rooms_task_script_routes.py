import uuid
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.dependencies import get_current_user
from app.models.room import Room, RoomStatus
from app.models.user import User, UserRole
from app.routers import rooms
from tests.conftest import FakeDBSession, FakeExecuteResult


def _student_user() -> User:
    return User(
        id=uuid.uuid4(),
        username="student_1",
        password_hash="x",
        display_name="Student 1",
        role=UserRole.student,
    )


def _teacher_user() -> User:
    return User(
        id=uuid.uuid4(),
        username="teacher_1",
        password_hash="x",
        display_name="Teacher 1",
        role=UserRole.teacher,
    )


def _build_client(fake_db: FakeDBSession, user: User) -> TestClient:
    app = FastAPI()
    app.include_router(rooms.router, prefix="/api/rooms", tags=["rooms"])

    async def override_get_db():
        yield fake_db

    async def override_get_current_user():
        return user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user
    return TestClient(app)


def test_get_task_script_endpoint_returns_state(fake_db, monkeypatch):
    room_id = uuid.uuid4()
    task_id = uuid.uuid4()
    room = Room(id=room_id, name="R1", created_by=uuid.uuid4())
    room.task_id = task_id
    fake_db.execute_result = FakeExecuteResult(scalar_value=object())
    client = _build_client(fake_db, _student_user())

    async def _fake_get_room(_db, _room_id):
        return room

    async def _fake_state(_db, _room):
        return {"task_id": str(task_id), "current_status": "S", "next_goal": "G", "history": [], "pending_proposal": None}

    monkeypatch.setattr(rooms.room_service, "get_room", _fake_get_room)
    monkeypatch.setattr(rooms.task_script_service, "get_task_script_state", _fake_state)

    resp = client.get(f"/api/rooms/{room_id}/task-script")

    assert resp.status_code == 200
    assert resp.json()["next_goal"] == "G"


def test_list_rooms_hides_timer_ended_rooms_for_student(fake_db, monkeypatch):
    room_running = Room(id=uuid.uuid4(), name="R1", created_by=uuid.uuid4(), status=RoomStatus.waiting, agent_mode="multi")
    room_ended = Room(id=uuid.uuid4(), name="R2", created_by=uuid.uuid4(), status=RoomStatus.waiting, agent_mode="multi")
    room_ended.timer_stopped_at = datetime.now(timezone.utc)
    client = _build_client(fake_db, _student_user())

    async def _fake_get_rooms(_db, _status):
        return [room_running, room_ended]

    async def _fake_member_count(_db, _room_id):
        return 2

    monkeypatch.setattr(rooms.room_service, "get_rooms", _fake_get_rooms)
    monkeypatch.setattr(rooms.room_service, "get_member_count", _fake_member_count)

    resp = client.get("/api/rooms")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == str(room_running.id)


def test_list_rooms_keeps_timer_ended_rooms_for_teacher(fake_db, monkeypatch):
    room_running = Room(id=uuid.uuid4(), name="R1", created_by=uuid.uuid4(), status=RoomStatus.waiting, agent_mode="multi")
    room_ended = Room(id=uuid.uuid4(), name="R2", created_by=uuid.uuid4(), status=RoomStatus.waiting, agent_mode="multi")
    room_ended.timer_stopped_at = datetime.now(timezone.utc)
    client = _build_client(fake_db, _teacher_user())

    async def _fake_get_rooms(_db, _status):
        return [room_running, room_ended]

    async def _fake_member_count(_db, _room_id):
        return 3

    monkeypatch.setattr(rooms.room_service, "get_rooms", _fake_get_rooms)
    monkeypatch.setattr(rooms.room_service, "get_member_count", _fake_member_count)

    resp = client.get("/api/rooms")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2


def test_confirm_task_script_forbidden_for_teacher(fake_db, monkeypatch):
    room_id = uuid.uuid4()
    room = Room(id=room_id, name="R1", created_by=uuid.uuid4())
    fake_db.execute_result = FakeExecuteResult(scalar_value=object())
    client = _build_client(fake_db, _teacher_user())

    async def _fake_get_room(_db, _room_id):
        return room

    monkeypatch.setattr(rooms.room_service, "get_room", _fake_get_room)

    resp = client.post(
        f"/api/rooms/{room_id}/task-script/confirm",
        json={"current_status": "S", "next_goal": "G"},
    )

    assert resp.status_code == 403
    assert "仅学生可确认" in resp.json()["detail"]


def test_confirm_task_script_calls_service_for_student(fake_db, monkeypatch):
    room_id = uuid.uuid4()
    room = Room(id=room_id, name="R1", created_by=uuid.uuid4())
    fake_db.execute_result = FakeExecuteResult(scalar_value=object())
    student = _student_user()
    client = _build_client(fake_db, student)
    called = {}

    async def _fake_get_room(_db, _room_id):
        return room

    async def _fake_confirm(_db, _room, _user, overrides=None, proposal_id=None, lease_id=None):
        called["room"] = _room
        called["user"] = _user
        called["overrides"] = overrides
        called["proposal_id"] = proposal_id
        called["lease_id"] = lease_id
        return {"task_id": "t1", "current_status": "S2", "next_goal": "G2", "history": [], "pending_proposal": None}

    monkeypatch.setattr(rooms.room_service, "get_room", _fake_get_room)
    monkeypatch.setattr(rooms.task_script_service, "confirm_pending_proposal", _fake_confirm)

    resp = client.post(
        f"/api/rooms/{room_id}/task-script/confirm",
        json={"current_status": "S-Edited", "next_goal": "G-Edited", "student_feedback": "先小范围试跑"},
    )

    assert resp.status_code == 200
    assert resp.json()["current_status"] == "S2"
    assert called["room"] is room
    assert called["user"].id == student.id
    assert called["overrides"]["current_status"] == "S-Edited"
    assert called["overrides"]["student_feedback"] == "先小范围试跑"

def test_report_room_activity_updates_last_activity_time(fake_db, monkeypatch):
    room_id = uuid.uuid4()
    room = Room(id=room_id, name="R1", created_by=uuid.uuid4())
    fake_db.execute_result = FakeExecuteResult(scalar_value=object())
    client = _build_client(fake_db, _student_user())

    class _FakeRedis:
        def __init__(self):
            self.set_calls = []
            self.sadd_calls = []

        async def set(self, key, value):
            self.set_calls.append((key, value))

        async def sadd(self, key, value):
            self.sadd_calls.append((key, value))

    fake_redis = _FakeRedis()

    async def _fake_get_room(_db, _room_id):
        return room

    monkeypatch.setattr(rooms.room_service, "get_room", _fake_get_room)
    monkeypatch.setattr(rooms, "get_redis_client", lambda: fake_redis)

    resp = client.post(
        f"/api/rooms/{room_id}/activity",
        json={"activity_type": "writing"},
    )

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["activity_type"] == "writing"
    assert len(fake_redis.set_calls) == 1
    assert fake_redis.set_calls[0][0] == f"room:{room_id}:last_activity_time"
    assert len(fake_redis.sadd_calls) == 1
    assert fake_redis.sadd_calls[0] == ("active_rooms", str(room_id))


def test_get_writing_submit_state_returns_service_result(fake_db, monkeypatch):
    room_id = uuid.uuid4()
    room = Room(id=room_id, name="R1", created_by=uuid.uuid4())
    fake_db.execute_result = FakeExecuteResult(scalar_value=object())
    client = _build_client(fake_db, _student_user())

    async def _fake_get_room(_db, _room_id):
        return room

    async def _fake_get_state(_room_id_str):
        return {"required_confirmations": 3, "confirmations": [], "final_submitted_at": None}

    monkeypatch.setattr(rooms.room_service, "get_room", _fake_get_room)
    monkeypatch.setattr(rooms.writing_submit_service, "get_writing_submit_state", _fake_get_state)

    resp = client.get(f"/api/rooms/{room_id}/writing-submit")

    assert resp.status_code == 200
    assert resp.json()["required_confirmations"] == 3


def test_confirm_writing_submit_forbidden_for_teacher(fake_db, monkeypatch):
    room_id = uuid.uuid4()
    room = Room(id=room_id, name="R1", created_by=uuid.uuid4())
    fake_db.execute_result = FakeExecuteResult(scalar_value=object())
    client = _build_client(fake_db, _teacher_user())

    async def _fake_get_room(_db, _room_id):
        return room

    monkeypatch.setattr(rooms.room_service, "get_room", _fake_get_room)

    resp = client.post(f"/api/rooms/{room_id}/writing-submit/confirm")

    assert resp.status_code == 403


def test_start_room_timer_requires_three_students(fake_db, monkeypatch):
    room_id = uuid.uuid4()
    room = Room(id=room_id, name="R1", created_by=uuid.uuid4())
    teacher = _teacher_user()
    client = _build_client(fake_db, teacher)

    async def _fake_get_room(_db, _room_id):
        return room

    async def _fake_student_count(_db, _room_id):
        return 2

    monkeypatch.setattr(rooms.room_service, "get_room", _fake_get_room)
    monkeypatch.setattr(rooms.room_service, "get_student_count", _fake_student_count)

    resp = client.post(f"/api/rooms/{room_id}/timer/start")

    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "ROOM_STUDENT_COUNT_NOT_ENOUGH"


def test_get_writing_doc_state_returns_service_result(fake_db, monkeypatch):
    room_id = uuid.uuid4()
    room = Room(id=room_id, name="R1", created_by=uuid.uuid4())
    fake_db.execute_result = FakeExecuteResult(scalar_value=object())
    client = _build_client(fake_db, _student_user())

    async def _fake_get_room(_db, _room_id):
        return room

    async def _fake_get_doc(_room_id_str):
        return {"content": "<p>x</p>", "version": 2, "updated_at": None, "updated_by": "u1"}

    monkeypatch.setattr(rooms.room_service, "get_room", _fake_get_room)
    monkeypatch.setattr(rooms.writing_doc_service, "get_writing_doc_state", _fake_get_doc)

    resp = client.get(f"/api/rooms/{room_id}/writing-doc")

    assert resp.status_code == 200
    assert resp.json()["content"] == "<p>x</p>"
    assert resp.json()["version"] == 2


def test_get_writing_doc_change_log_returns_items(fake_db, monkeypatch):
    room_id = uuid.uuid4()
    room = Room(id=room_id, name="R1", created_by=uuid.uuid4())
    fake_db.execute_result = FakeExecuteResult(scalar_value=object())
    client = _build_client(fake_db, _student_user())

    async def _fake_get_room(_db, _room_id):
        return room

    async def _fake_change_log(_room_id_str, limit=30):
        _ = limit
        return [
            {
                "action": "update",
                "at": None,
                "actor_id": "u1",
                "actor_display_name": "Alice",
                "version": 1,
                "summary": "新增约3字",
                "delta_chars": 3,
            }
        ]

    monkeypatch.setattr(rooms.room_service, "get_room", _fake_get_room)
    monkeypatch.setattr(rooms.writing_doc_service, "get_writing_doc_change_log", _fake_change_log)

    resp = client.get(f"/api/rooms/{room_id}/writing-doc/change-log")

    assert resp.status_code == 200
    assert resp.json()["items"][0]["version"] == 1


def test_restore_writing_doc_endpoint_is_disabled(fake_db, monkeypatch):
    room_id = uuid.uuid4()
    room = Room(id=room_id, name="R1", created_by=uuid.uuid4())
    fake_db.execute_result = FakeExecuteResult(scalar_value=object())
    client = _build_client(fake_db, _student_user())

    async def _fake_get_room(_db, _room_id):
        return room

    monkeypatch.setattr(rooms.room_service, "get_room", _fake_get_room)

    resp = client.post(f"/api/rooms/{room_id}/writing-doc/restore", json={"version": 1})

    assert resp.status_code == 410


def test_save_writing_doc_version_forbidden_for_teacher(fake_db, monkeypatch):
    room_id = uuid.uuid4()
    room = Room(id=room_id, name="R1", created_by=uuid.uuid4())
    fake_db.execute_result = FakeExecuteResult(scalar_value=object())
    client = _build_client(fake_db, _teacher_user())

    async def _fake_get_room(_db, _room_id):
        return room

    monkeypatch.setattr(rooms.room_service, "get_room", _fake_get_room)

    resp = client.post(f"/api/rooms/{room_id}/writing-doc/save-version")

    assert resp.status_code == 403


def test_save_writing_doc_version_returns_latest_change_log_for_student(fake_db, monkeypatch):
    room_id = uuid.uuid4()
    room = Room(id=room_id, name="R1", created_by=uuid.uuid4())
    fake_db.execute_result = FakeExecuteResult(scalar_value=object())
    student = _student_user()
    client = _build_client(fake_db, student)
    called = {}

    async def _fake_get_room(_db, _room_id):
        return room

    async def _fake_save(_room_id_str, saved_by, saved_by_display_name):
        called["room_id"] = _room_id_str
        called["saved_by"] = saved_by
        called["saved_by_display_name"] = saved_by_display_name
        return {"version": 2}

    async def _fake_change_log(_room_id_str, limit=30):
        called["limit"] = limit
        return [
            {
                "action": "save_checkpoint",
                "at": None,
                "actor_id": str(student.id),
                "actor_display_name": student.display_name,
                "version": 2,
                "summary": "保存检查点（10字）",
                "delta_chars": 0,
            }
        ]

    monkeypatch.setattr(rooms.room_service, "get_room", _fake_get_room)
    monkeypatch.setattr(rooms.writing_doc_service, "save_writing_doc_version", _fake_save)
    monkeypatch.setattr(rooms.writing_doc_service, "get_writing_doc_change_log", _fake_change_log)

    resp = client.post(f"/api/rooms/{room_id}/writing-doc/save-version")

    assert resp.status_code == 200
    assert resp.json()["items"][0]["version"] == 2
    assert called["room_id"] == str(room_id)
    assert called["saved_by"] == str(student.id)
    assert called["saved_by_display_name"] == student.display_name
    assert called["limit"] == 30


def test_leave_room_before_timer_start_allowed(fake_db, monkeypatch):
    room_id = uuid.uuid4()
    room = Room(id=room_id, name="R1", created_by=uuid.uuid4(), status=RoomStatus.waiting)
    room.timer_started_at = None
    room.timer_stopped_at = None
    fake_db.execute_result = FakeExecuteResult(scalar_value=object())
    student = _student_user()
    client = _build_client(fake_db, student)
    called = {}

    async def _fake_get_room(_db, _room_id):
        return room

    async def _fake_leave(_db, _room_id, _user_id):
        called["room_id"] = _room_id
        called["user_id"] = _user_id

    monkeypatch.setattr(rooms.room_service, "get_room", _fake_get_room)
    monkeypatch.setattr(rooms.room_service, "leave_room", _fake_leave)

    resp = client.post(f"/api/rooms/{room_id}/leave")

    assert resp.status_code == 200
    assert called["room_id"] == room_id
    assert called["user_id"] == student.id


def test_leave_room_after_timer_start_forbidden(fake_db, monkeypatch):
    room_id = uuid.uuid4()
    room = Room(id=room_id, name="R1", created_by=uuid.uuid4(), status=RoomStatus.waiting)
    room.timer_started_at = datetime.now(timezone.utc)
    room.timer_stopped_at = None
    fake_db.execute_result = FakeExecuteResult(scalar_value=object())
    client = _build_client(fake_db, _student_user())

    async def _fake_get_room(_db, _room_id):
        return room

    monkeypatch.setattr(rooms.room_service, "get_room", _fake_get_room)

    resp = client.post(f"/api/rooms/{room_id}/leave")

    assert resp.status_code == 409
