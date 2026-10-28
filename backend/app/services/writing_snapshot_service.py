"""Five-minute, server-side writing snapshots for longitudinal research.

Snapshots are stored in Redis without expiry, separate from capped edit logs.
The persisted Redis data must be included in the deployment's AOF/RDB backups.
"""
from __future__ import annotations

import json
import re
from html import unescape
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select

from app.db.redis_client import get_redis_client
from app.db.session import AsyncSessionLocal
from app.models.room import Room, RoomStatus
from app.services.writing_doc_service import get_writing_doc_state

SNAPSHOT_INTERVAL_SECONDS = 300


def snapshot_key(room_id: str) -> str:
    return f"room:{room_id}:writing_doc_snapshots_5min"


def snapshot_record(room_id: str, elapsed_slot: int, anchor: datetime, state: dict, captured_at: datetime) -> dict:
    boundary = anchor.timestamp() + elapsed_slot * SNAPSHOT_INTERVAL_SECONDS
    html = str(state.get("content") or "")
    plain_text = unescape(re.sub(r"<[^>]+>", "", re.sub(r"</(?:p|div|li|h[1-6])>", "\\n", html, flags=re.I))).strip()
    return {
        "room_id": room_id,
        "elapsed_minutes": elapsed_slot * 5,
        "scheduled_at": datetime.fromtimestamp(boundary, timezone.utc).isoformat(),
        "captured_at": captured_at.isoformat(),
        "version": int(state.get("version") or 0),
        "updated_at": state.get("updated_at"),
        "updated_by": state.get("updated_by"),
        "content_html": html,
        "content_text": plain_text,
        "text_length": len(plain_text),
    }


async def snapshot_active_writing_docs(now: datetime | None = None) -> int:
    """Capture one authoritative document state per elapsed five-minute interval.

    Use room creation as the stable anchor, independent of client sessions. The
    schedule intentionally skips empty/never-edited documents. A delayed tick
    is timestamped with both the scheduled slot and actual capture time; it
    never invents the contents of missed intervals.
    """
    now = now or datetime.now(timezone.utc)
    redis_client = get_redis_client()
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Room.id, Room.created_at).where(Room.status != RoomStatus.ended)
        )).all()

    count = 0
    for room_id, created_at in rows:
        if not created_at:
            continue
        anchor = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
        slot = int((now - anchor).total_seconds() // SNAPSHOT_INTERVAL_SECONDS)
        if slot < 1:
            continue
        rid = str(room_id)
        state = await get_writing_doc_state(rid)
        if int(state.get("version") or 0) < 1:
            continue
        item = snapshot_record(rid, slot, anchor, state, now)
        saved = await redis_client.hsetnx(snapshot_key(rid), str(slot), json.dumps(item, ensure_ascii=False))
        count += int(bool(saved))
    return count


async def get_writing_snapshots(room_id: str) -> list[dict]:
    client = get_redis_client()
    items = await client.hgetall(snapshot_key(str(UUID(str(room_id)))))
    snapshots = []
    for raw in items.values():
        try:
            item = json.loads(raw)
            if isinstance(item, dict):
                snapshots.append(item)
        except (ValueError, TypeError):
            continue
    return sorted(snapshots, key=lambda item: int(item.get("elapsed_minutes") or 0))
