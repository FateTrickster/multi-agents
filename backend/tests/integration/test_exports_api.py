import io
import zipfile
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.dependencies import get_current_user
from app.models.user import User, UserRole
from app.routers import exports


def _build_app(user: User | None, fake_db):
    app = FastAPI()
    app.include_router(exports.router, prefix="/api/exports", tags=["exports"])

    async def override_get_db():
        yield fake_db

    app.dependency_overrides[get_db] = override_get_db
    if user is not None:
        async def override_get_current_user():
            return user

        app.dependency_overrides[get_current_user] = override_get_current_user
    return app


def _teacher_user() -> User:
    return User(
        id=uuid4(),
        username="teacher_export",
        password_hash="x",
        display_name="Teacher Export",
        role=UserRole.teacher,
    )


def _student_user() -> User:
    return User(
        id=uuid4(),
        username="student_export",
        password_hash="x",
        display_name="Student Export",
        role=UserRole.student,
    )


def test_teacher_can_access_rooms(fake_db, monkeypatch):
    async def _mock_list_export_rooms(*_args, **_kwargs):
        return [
            {
                "room_id": str(uuid4()),
                "name": "E2E-Room-1",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "status": "active",
                "student_count": 3,
                "message_count": 20,
                "agent_message_count": 5,
                "has_writing_doc": True,
            }
        ]

    monkeypatch.setattr("app.routers.exports.export_service.list_export_rooms", _mock_list_export_rooms)
    client = TestClient(_build_app(_teacher_user(), fake_db))

    resp = client.get("/api/exports/rooms")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_student_cannot_access_rooms(fake_db):
    client = TestClient(_build_app(_student_user(), fake_db))
    resp = client.get("/api/exports/rooms")
    assert resp.status_code == 403


def test_exports_requires_login(fake_db):
    client = TestClient(_build_app(None, fake_db))
    resp = client.get("/api/exports/rooms")
    assert resp.status_code == 401


def test_teacher_preview_returns_counts(fake_db, monkeypatch):
    async def _mock_build_export_payload(*_args, **_kwargs):
        return {
            "matched_room_ids": [str(uuid4())],
            "counts": {
                "rooms": 1,
                "room_members": 3,
                "messages": 12,
                "agent_tasks": 4,
                "analysis_snapshots": 2,
                "writing_docs": 1,
                "writing_change_logs": 5,
            },
            "estimated_files": ["export_summary.json", "messages.csv", "agent_tasks.csv", "writing_docs.csv"],
            "data": {
                "rooms": [],
                "room_members": [],
                "messages": [],
                "agent_tasks": [],
                "analysis_snapshots": [],
                "writing_docs": [],
                "writing_change_logs": [],
                "room_writing_html": {},
            },
            "filters": {},
        }

    monkeypatch.setattr("app.routers.exports.export_service.build_export_payload", _mock_build_export_payload)
    client = TestClient(_build_app(_teacher_user(), fake_db))

    resp = client.post("/api/exports/preview", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["counts"]["messages"] == 12


def test_teacher_download_returns_zip_and_required_files(fake_db, monkeypatch):
    room_id = str(uuid4())

    async def _mock_build_export_payload(*_args, **_kwargs):
        return {
            "filters": {},
            "matched_room_ids": [room_id],
            "counts": {
                "rooms": 1,
                "room_members": 1,
                "messages": 1,
                "agent_tasks": 1,
                "analysis_snapshots": 0,
                "writing_docs": 1,
                "writing_change_logs": 0,
            },
            "estimated_files": [],
            "data": {
                "rooms": [
                    {
                        "room_id": room_id,
                        "name": "Export Room",
                        "task_id": "",
                        "created_by": "",
                        "status": "active",
                        "created_at": "",
                        "ended_at": "",
                        "timer_started_at": "",
                        "timer_deadline_at": "",
                        "timer_stopped_at": "",
                        "locked_member_ids": "",
                    }
                ],
                "room_members": [
                    {
                        "room_member_id": "m1",
                        "room_id": room_id,
                        "user_id": "u1",
                        "username": "s1",
                        "display_name": "Student 1",
                        "user_role": "student",
                        "joined_at": "",
                    }
                ],
                "messages": [
                    {
                        "message_id": "msg1",
                        "room_id": room_id,
                        "seq_num": 1,
                        "sender_id": "u1",
                        "sender_username": "s1",
                        "sender_name": "Student 1",
                        "sender_type": "student",
                        "agent_role": "",
                        "content": "你好",
                        "message_status": "ok",
                        "source_message_id": "",
                        "source_display_name_snapshot": "",
                        "source_content_preview_snapshot": "",
                        "created_at": "",
                    }
                ],
                "agent_tasks": [
                    {
                        "task_id": "t1",
                        "room_id": room_id,
                        "trigger_type": "silence",
                        "agent_role": "facilitator",
                        "status": "replied",
                        "reason": "",
                        "error": "",
                        "drop_reason": "",
                        "queued_at": "",
                        "running_at": "",
                        "finished_at": "",
                        "source_message_id": "",
                        "created_at": "",
                    }
                ],
                "analysis_snapshots": [],
                "writing_docs": [
                    {
                        "room_id": room_id,
                        "content_html": "<h1>标题</h1><p><strong>加粗</strong></p>",
                        "version": 1,
                        "updated_at": "",
                        "updated_by": "",
                        "updated_by_display_name": "",
                        "content_text_preview": "标题",
                    }
                ],
                "writing_change_logs": [],
                "room_writing_html": {room_id: "<h1>标题</h1><p>段落</p>"},
            },
        }

    monkeypatch.setattr("app.routers.exports.export_service.build_export_payload", _mock_build_export_payload)
    client = TestClient(_build_app(_teacher_user(), fake_db))

    resp = client.post("/api/exports/download", json={})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/zip")

    with zipfile.ZipFile(io.BytesIO(resp.content), "r") as zf:
        names = set(zf.namelist())
        assert "export_summary.json" in names
        assert "messages.csv" in names
        assert "agent_tasks.csv" in names
        assert "writing_docs.csv" in names
        assert f"room_writing_{room_id}.html" in names


def test_student_cannot_preview_or_download_even_with_direct_api_calls(fake_db):
    client = TestClient(_build_app(_student_user(), fake_db))
    for route in ("/api/exports/preview", "/api/exports/download"):
        response = client.post(route, json={})
        assert response.status_code == 403, (route, response.text)


def test_public_teacher_registration_is_forbidden(fake_db, monkeypatch):
    from app.routers import auth
    app = FastAPI()
    app.include_router(auth.router, prefix="/api/auth")

    async def override_get_db():
        yield fake_db

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    response = client.post("/api/auth/register", json={
        "username": "self_assigned_teacher",
        "display_name": "Unauthorized teacher",
        "password": "test_password_123",
        "role": "teacher",
    })
    assert response.status_code == 403
