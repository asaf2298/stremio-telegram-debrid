"""In-process refcount + Supabase lease for shared host busy coordination.

Telegram byte streams (/stream/file, /stream/split, /stream/zip) acquire on
generator start and release in finally. TorBox/CDN plays do not touch this flag.

Consumer (sync worker): treat host as busy only when
  busy = true AND busy_until > now()
on personal_host_busy id 'default'. Missing row or expired lease => free.

Concurrency notes:
- Refcount mutations happen under ``_lock``; HTTP never runs while holding it.
- ``_generation`` bumps on every 0↔busy transition. ``_write_desired`` re-reads
  after each persist and rewrites until the generation matches (prevents a
  stale heartbeat from re-setting busy after the last stream ends).
- Supabase is only written on 0→1, N→0, and heartbeat — not every Range GET.
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
_generation = 0
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


def _reset_for_tests() -> None:
    """Test helper — clear in-process state."""
    global _active_streams, _generation, _heartbeat_task, _last_stream_key
    if _heartbeat_task and not _heartbeat_task.done():
        _heartbeat_task.cancel()
    _active_streams = 0
    _generation = 0
    _heartbeat_task = None
    _last_stream_key = None


async def _persist_busy(*, busy: bool, active_streams: int, stream_key: Optional[str]) -> bool:
    """Write one row. Returns False if Supabase rejected / is unavailable."""
    payload = {
        "id": HOST_ID,
        "busy": busy,
        "busy_until": _busy_until_iso() if busy else None,
        "active_streams": active_streams,
        "source": SOURCE_TELEGRAM_STREAM if busy else None,
        "stream_key": stream_key if busy else None,
        "updated_at": _utcnow().isoformat(),
    }
    result = await store.upsert_host_busy(payload)
    if result is None:
        logger.warning(
            "personal_host_busy persist failed (busy=%s active_streams=%s)",
            busy,
            active_streams,
        )
        return False
    return True


async def _write_desired() -> None:
    """Persist current in-memory truth; retry if state changed mid-write.

    Must NOT be called while holding ``_lock``.
    """
    if not enabled():
        return
    while True:
        async with _lock:
            gen = _generation
            busy = _active_streams > 0
            count = _active_streams
            key = _last_stream_key
        await _persist_busy(busy=busy, active_streams=count, stream_key=key)
        async with _lock:
            if gen == _generation:
                return
            # State changed while we were writing (e.g. last stream released
            # during a heartbeat). Loop and converge to the new truth.


async def _heartbeat_loop() -> None:
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)
            async with _lock:
                if _active_streams <= 0:
                    return
            await _write_desired()
    except asyncio.CancelledError:
        pass


async def acquire(stream_key: str) -> bool:
    """Increment refcount. Returns True if a slot was taken (must release)."""
    global _active_streams, _generation, _heartbeat_task, _last_stream_key
    transition_to_busy = False
    async with _lock:
        if not enabled():
            return False
        _active_streams += 1
        _last_stream_key = stream_key
        if _active_streams == 1:
            _generation += 1
            transition_to_busy = True
            if _heartbeat_task is None or _heartbeat_task.done():
                _heartbeat_task = asyncio.create_task(_heartbeat_loop())

    if transition_to_busy:
        await _write_desired()
    return True


async def release() -> None:
    """Decrement refcount; clear busy when the last stream ends.

    Always decrements when count > 0 so a mid-session enabled() flip cannot
    leak the in-process refcount. Persist is skipped when Supabase is off.
    """
    global _active_streams, _generation, _heartbeat_task, _last_stream_key
    transition_to_free = False
    async with _lock:
        if _active_streams <= 0:
            return
        _active_streams -= 1
        if _active_streams == 0:
            _generation += 1
            _last_stream_key = None
            transition_to_free = True
            if _heartbeat_task and not _heartbeat_task.done():
                _heartbeat_task.cancel()
            _heartbeat_task = None

    if transition_to_free:
        await _write_desired()


@asynccontextmanager
async def telegram_stream_guard(stream_key: str):
    taken = await acquire(stream_key)
    try:
        yield
    finally:
        if taken:
            await release()


async def wrap_byte_stream(stream_key: str, gen):
    """Wrap an async byte generator with acquire/release."""
    async with telegram_stream_guard(stream_key):
        async for chunk in gen:
            yield chunk
