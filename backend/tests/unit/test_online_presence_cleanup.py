import pytest

from app.db import redis_client as redis_client_module


class _FakeRedis:
    def __init__(self):
        self._hashes = {}
        self._sets = {}
        self._values = {}
        self._deleted = set()

    async def set(self, key, value):
        self._values[key] = value

    async def sadd(self, key, *members):
        self._sets.setdefault(key, set()).update(members)

    async def scan_iter(self, match=None, count=200):
        _ = count
        keys = list(self._hashes.keys())
        if match == "room:*:online_user_last_seen":
            keys = [k for k in keys if k.startswith("room:") and k.endswith(":online_user_last_seen")]
        for key in keys:
            yield key

    async def hset(self, key, field, value):
        self._hashes.setdefault(key, {})
        self._hashes[key][field] = str(value)

    async def hgetall(self, key):
        return dict(self._hashes.get(key, {}))

    async def hdel(self, key, *fields):
        if key not in self._hashes:
            return 0
        removed = 0
        for field in fields:
            if field in self._hashes[key]:
                del self._hashes[key][field]
                removed += 1
        return removed

    async def hlen(self, key):
        return len(self._hashes.get(key, {}))

    async def srem(self, key, *members):
        if key not in self._sets:
            return 0
        removed = 0
        for member in members:
            if member in self._sets[key]:
                self._sets[key].remove(member)
                removed += 1
        return removed

    async def smembers(self, key):
        return set(self._sets.get(key, set()))

    async def delete(self, key):
        self._hashes.pop(key, None)
        self._sets.pop(key, None)
        self._deleted.add(key)


@pytest.mark.asyncio
async def test_cleanup_stale_online_presence_prunes_stale_and_dangling(monkeypatch):
    now_ts = 10_000.0
    room_id = "room-1"
    last_seen_key = f"room:{room_id}:online_user_last_seen"
    online_users_key = f"room:{room_id}:online_users"
    conn_counts_key = f"room:{room_id}:online_user_conn_counts"

    fake_redis = _FakeRedis()
    fake_redis._hashes[last_seen_key] = {
        "u_stale": str(now_ts - 500),
        "u_alive": str(now_ts - 10),
    }
    fake_redis._sets[online_users_key] = {"u_stale", "u_alive", "u_dangling"}
    fake_redis._hashes[conn_counts_key] = {"u_stale": "2", "u_alive": "1", "u_dangling": "1"}

    monkeypatch.setattr(redis_client_module, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(redis_client_module.time, "time", lambda: now_ts)

    await redis_client_module.cleanup_stale_online_presence(stale_seconds=120)

    assert fake_redis._hashes[last_seen_key] == {"u_alive": str(now_ts - 10)}
    assert fake_redis._sets[online_users_key] == {"u_alive"}
    assert fake_redis._hashes[conn_counts_key] == {"u_alive": "1"}


@pytest.mark.asyncio
async def test_mark_room_active_records_message_and_activity():
    fake_redis = _FakeRedis()

    await redis_client_module.mark_room_active("room-1", client=fake_redis, message=True, ts=123.0)

    assert fake_redis._values == {
        "room:room-1:last_msg_time": 123.0,
        "room:room-1:last_activity_time": 123.0,
    }
    assert fake_redis._sets["active_rooms"] == {"room-1"}
