"""In-process refcount + Supabase lease for shared host busy coordination.

Telegram byte streams (/stream/file, /stream/split, /stream/zip) acquire on
generator start and release in finally. TorBox/CDN plays do not touch this flag.

Consumer (sync worker): treat host as busy only when
  busy = true AND busy_until > now()
on personal_host_busy id 'default'. Missing row or expired lease => free.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Optional

import metadata_store as store

logger = logging.getLogger("host_busy")

HOST_ID = "default"
SOURCE_TELEGRAM_STREAM = "telegram_stream"
HEARTBEAT_INTERVAL_SEC = 30
LEASE_SECONDS = 90

_lock = asyncio.Lock()
_active_streams = 0
_heartbeat_task: Optional[asyncio.Task] = None
_last_stream_key: Optional[str] = None


def enabled() -> bool:
    return store.is_configured()


def is_row_busy(row: Optional[dict]) -> bool:
    """Evaluate busy state per sync-worker contract."""
    if not row or not row.get("busy"):
        return False
    busy_until = row.get("busy_until")
    if not busy_until:
        return False
    try:
        expiry = datetime.fromisoformat(str(busy_until).replace("Z", "+00:00"))
    except Exception:
        return False
    return expiry > datetime.now(timezone.utc)


async def get_host_busy(host_id: str = HOST_ID) -> Optional[dict]:
    return await store.get_host_busy(host_id)


async def is_host_busy(host_id: str = HOST_ID) -> bool:
    return is_row_busy(await get_host_busy(host_id))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _busy_until_iso() -> str:
    return (_utcnow() + timedelta(seconds=LEASE_SECONDS)).isoformat()


async def _persist_busy(*, busy: bool, active_streams: int, stream_key: Optional[str]) -> None:
    payload = {
        "id": HOST_ID,
        "busy": busy,
        "busy_until": _busy_until_iso() if busy else None,
        "active_streams": active_streams,
        "source": SOURCE_TELEGRAM_STREAM if busy else None,
        "stream_key": stream_key if busy else None,
        "updated_at": _utcnow().isoformat(),
    }
    await store.upsert_host_busy(payload)


async def _refresh_lease() -> None:
    async with _lock:
        if _active_streams <= 0:
            return
        count = _active_streams
        key = _last_stream_key
    await _persist_busy(busy=True, active_streams=count, stream_key=key)


async def _heartbeat_loop() -> None:
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)
            async with _lock:
                if _active_streams <= 0:
                    return
            await _refresh_lease()
    except asyncio.CancelledError:
        pass


async def acquire(stream_key: str) -> None:
    """Increment refcount; first active stream sets busy + starts heartbeat."""
    global _active_streams, _heartbeat_task, _last_stream_key
    if not enabled():
        return
    async with _lock:
        _active_streams += 1
        _last_stream_key = stream_key
        count = _active_streams
        if count == 1:
            await _persist_busy(busy=True, active_streams=count, stream_key=stream_key)
            if _heartbeat_task is None or _heartbeat_task.done():
                _heartbeat_task = asyncio.create_task(_heartbeat_loop())
        else:
            await _persist_busy(busy=True, active_streams=count, stream_key=stream_key)


async def release() -> None:
    """Decrement refcount; last stream clears busy and stops heartbeat."""
    global _active_streams, _heartbeat_task, _last_stream_key
    if not enabled():
        return
    async with _lock:
        _active_streams = max(0, _active_streams - 1)
        count = _active_streams
        if count > 0:
            await _persist_busy(busy=True, active_streams=count, stream_key=_last_stream_key)
            return
        _last_stream_key = None
        if _heartbeat_task and not _heartbeat_task.done():
            _heartbeat_task.cancel()
        _heartbeat_task = None
        await _persist_busy(busy=False, active_streams=0, stream_key=None)


@asynccontextmanager
async def telegram_stream_guard(stream_key: str):
    await acquire(stream_key)
    try:
        yield
    finally:
        await release()


async def wrap_byte_stream(stream_key: str, gen):
    """Wrap an async byte generator with acquire/release."""
    async with telegram_stream_guard(stream_key):
        async for chunk in gen:
            yield chunk
