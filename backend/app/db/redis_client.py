import redis.asyncio as redis
import time

from app.config import settings

_redis_client: redis.Redis | None = None


async def init_redis() -> None:
    global _redis_client
    _redis_client = redis.from_url(
        settings.REDIS_URL,
        encoding="utf-8",
        decode_responses=True,
        max_connections=settings.REDIS_MAX_CONNECTIONS,
    )
    await _redis_client.ping()


async def close_redis() -> None:
    global _redis_client
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None


async def clear_online_presence_keys() -> None:
    """Clear stale online presence keys left by ungraceful disconnect/restart."""
    client = get_redis_client()
    patterns = (
        "room:*:online_users",
        "room:*:online_user_conn_counts",
    )
    for pattern in patterns:
        async for key in client.scan_iter(match=pattern, count=200):
            await client.delete(key)


async def touch_online_presence(room_id: str, user_id: str, ts: float | None = None) -> None:
    client = get_redis_client()
    now_ts = ts if ts is not None else time.time()
    await client.hset(f"room:{room_id}:online_user_last_seen", user_id, str(now_ts))


async def mark_room_active(
    room_id: str,
    *,
    client: redis.Redis | None = None,
    message: bool = False,
    ts: float | None = None,
) -> None:
    client = client or get_redis_client()
    now_ts = ts if ts is not None else time.time()
    if message:
        await client.set(f"room:{room_id}:last_msg_time", now_ts)
    await client.set(f"room:{room_id}:last_activity_time", now_ts)
    await client.sadd("active_rooms", room_id)


async def cleanup_stale_online_presence(stale_seconds: int = 120) -> None:
    """
    Remove stale online users by last-seen heartbeat.
    A user is stale when now - last_seen > stale_seconds.
    """
    client = get_redis_client()
    now_ts = time.time()

    async for last_seen_key in client.scan_iter(match="room:*:online_user_last_seen", count=200):
        room_id = last_seen_key[len("room:") : -len(":online_user_last_seen")]
        online_users_key = f"room:{room_id}:online_users"
        conn_counts_key = f"room:{room_id}:online_user_conn_counts"

        last_seen_map = await client.hgetall(last_seen_key)
        stale_user_ids: list[str] = []
        alive_user_ids: set[str] = set()

        for user_id, raw_ts in last_seen_map.items():
            try:
                age_seconds = now_ts - float(raw_ts)
            except (TypeError, ValueError):
                stale_user_ids.append(user_id)
                continue
            if age_seconds > stale_seconds:
                stale_user_ids.append(user_id)
            else:
                alive_user_ids.add(user_id)

        if stale_user_ids:
            await client.hdel(last_seen_key, *stale_user_ids)
            await client.srem(online_users_key, *stale_user_ids)
            await client.hdel(conn_counts_key, *stale_user_ids)

        online_users = await client.smembers(online_users_key)
        dangling_users = [uid for uid in online_users if uid not in alive_user_ids]
        if dangling_users:
            await client.srem(online_users_key, *dangling_users)
            await client.hdel(conn_counts_key, *dangling_users)

        if int(await client.hlen(last_seen_key)) == 0:
            await client.delete(last_seen_key)


async def set_user_active_session_jti(user_id: str, jti: str) -> None:
    client = get_redis_client()
    await client.set(f"user:{user_id}:active_session_jti", jti)


async def get_user_active_session_jti(user_id: str) -> str | None:
    client = get_redis_client()
    return await client.get(f"user:{user_id}:active_session_jti")


def get_redis_client() -> redis.Redis:
    if _redis_client is None:
        raise RuntimeError("Redis client not initialized. Call init_redis() during app startup.")
    return _redis_client
