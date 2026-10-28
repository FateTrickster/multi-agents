import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest

from app.services import writing_snapshot_service as svc
from app.services.export_service import build_export_zip_bytes


class FakeRedis:
    def __init__(self):
        self.data = {}

    async def hsetnx(self, key, field, value):
        group = self.data.setdefault(key, {})
        if field in group:
            return 0
        group[field] = value
        return 1

    async def hgetall(self, key):
        return self.data.get(key, {})


@pytest.mark.asyncio
async def test_five_minute_snapshots_are_full_and_not_duplicated(monkeypatch):
    room_id = uuid4()
    start = datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc)
    redis = FakeRedis()

    class Result:
        def all(self):
            return [(room_id, start)]

    class Db:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def execute(self, _):
            return Result()

    monkeypatch.setattr(svc, "AsyncSessionLocal", Db)
    monkeypatch.setattr(svc, "get_redis_client", lambda: redis)
    content = {"content": "<p>第一版协作成果</p>", "version": 2, "updated_at": start.isoformat(), "updated_by": "student-1"}
    monkeypatch.setattr(svc, "get_writing_doc_state", AsyncMock(return_value=content))
    assert await svc.snapshot_active_writing_docs(start + timedelta(minutes=4)) == 0
    assert await svc.snapshot_active_writing_docs(start + timedelta(minutes=5)) == 1
    assert await svc.snapshot_active_writing_docs(start + timedelta(minutes=5, seconds=30)) == 0
    content = {**content, "content": "<p>第二版协作成果</p>", "version": 3}
    svc.get_writing_doc_state.return_value = content
    assert await svc.snapshot_active_writing_docs(start + timedelta(minutes=10)) == 1
    snapshots = await svc.get_writing_snapshots(str(room_id))
    assert [x["elapsed_minutes"] for x in snapshots] == [5, 10]
    assert [x["content_html"] for x in snapshots] == ["<p>第一版协作成果</p>", "<p>第二版协作成果</p>"]
    assert [x["version"] for x in snapshots] == [2, 3]


@pytest.mark.asyncio
async def test_never_edited_document_has_no_snapshot(monkeypatch):
    room_id = uuid4()
    start = datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc)
    redis = FakeRedis()

    class Db:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        async def execute(self, _):
            class Result:
                def all(self): return [(room_id, start)]
            return Result()

    monkeypatch.setattr(svc, "AsyncSessionLocal", Db)
    monkeypatch.setattr(svc, "get_redis_client", lambda: redis)
    monkeypatch.setattr(svc, "get_writing_doc_state", AsyncMock(return_value={"version": 0, "content": ""}))
    assert await svc.snapshot_active_writing_docs(start + timedelta(minutes=10)) == 0
    assert await svc.get_writing_snapshots(str(room_id)) == []
