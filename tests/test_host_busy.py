import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

import pytest

import host_busy


@pytest.fixture(autouse=True)
def _reset_host_busy_state():
    host_busy._reset_for_tests()
    yield
    host_busy._reset_for_tests()


def test_is_row_busy_false_when_missing():
    assert host_busy.is_row_busy(None) is False


def test_is_row_busy_false_when_not_busy():
    assert host_busy.is_row_busy({"busy": False, "busy_until": "2099-01-01T00:00:00+00:00"}) is False


def test_is_row_busy_true_when_busy_and_future_lease():
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    assert host_busy.is_row_busy({"busy": True, "busy_until": future}) is True


def test_is_row_busy_false_when_lease_expired():
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    assert host_busy.is_row_busy({"busy": True, "busy_until": past}) is False


@pytest.mark.asyncio
async def test_acquire_sets_busy_on_first_stream():
    with patch.object(host_busy, "enabled", return_value=True), \
         patch.object(host_busy, "_persist_busy", new=AsyncMock(return_value=True)) as mock_persist:
        taken = await host_busy.acquire("file/1/2/movie.mkv")
        assert taken is True
        assert host_busy._active_streams == 1
        mock_persist.assert_awaited_once()
        assert mock_persist.await_args.kwargs["busy"] is True
        assert mock_persist.await_args.kwargs["active_streams"] == 1


@pytest.mark.asyncio
async def test_second_acquire_does_not_persist():
    with patch.object(host_busy, "enabled", return_value=True), \
         patch.object(host_busy, "_persist_busy", new=AsyncMock(return_value=True)) as mock_persist:
        await host_busy.acquire("file/1/2/a.mkv")
        await host_busy.acquire("file/1/2/b.mkv")
        assert host_busy._active_streams == 2
        mock_persist.assert_awaited_once()


@pytest.mark.asyncio
async def test_release_clears_busy_when_last_stream_ends():
    with patch.object(host_busy, "enabled", return_value=True), \
         patch.object(host_busy, "_persist_busy", new=AsyncMock(return_value=True)) as mock_persist:
        await host_busy.acquire("file/1/2/movie.mkv")
        mock_persist.reset_mock()
        await host_busy.release()
        assert host_busy._active_streams == 0
        mock_persist.assert_awaited_once()
        assert mock_persist.await_args.kwargs["busy"] is False
        assert mock_persist.await_args.kwargs["active_streams"] == 0


@pytest.mark.asyncio
async def test_release_mid_streams_does_not_persist():
    with patch.object(host_busy, "enabled", return_value=True), \
         patch.object(host_busy, "_persist_busy", new=AsyncMock(return_value=True)) as mock_persist:
        await host_busy.acquire("a")
        await host_busy.acquire("b")
        mock_persist.reset_mock()
        await host_busy.release()
        assert host_busy._active_streams == 1
        mock_persist.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_heartbeat_cannot_rebusy_after_release():
    """Heartbeat persist in flight during release must converge to busy=false."""
    writes = []
    release_started = asyncio.Event()
    heartbeat_in_persist = asyncio.Event()

    async def racing_persist(*, busy, active_streams, stream_key):
        writes.append({"busy": busy, "count": active_streams})
        # Second busy write = stale heartbeat overlapping release
        if busy and len([w for w in writes if w["busy"]]) == 2:
            heartbeat_in_persist.set()
            await release_started.wait()
        return True

    with patch.object(host_busy, "enabled", return_value=True), \
         patch.object(host_busy, "_persist_busy", new=AsyncMock(side_effect=racing_persist)):
        await host_busy.acquire("x")
        hb = asyncio.create_task(host_busy._write_desired())
        await heartbeat_in_persist.wait()
        rel = asyncio.create_task(host_busy.release())
        await asyncio.sleep(0)
        release_started.set()
        await asyncio.gather(hb, rel)

    assert host_busy._active_streams == 0
    assert writes[-1]["busy"] is False
    assert writes[-1]["count"] == 0


@pytest.mark.asyncio
async def test_telegram_stream_guard_releases_on_generator_exit():
    with patch.object(host_busy, "enabled", return_value=True), \
         patch.object(host_busy, "_persist_busy", new=AsyncMock(return_value=True)) as mock_persist:

        async def gen():
            async with host_busy.telegram_stream_guard("file/1/2/x.mkv"):
                yield b"abc"

        chunks = [c async for c in gen()]
        assert chunks == [b"abc"]
        assert mock_persist.await_count == 2
        assert mock_persist.await_args_list[0].kwargs["busy"] is True
        assert mock_persist.await_args_list[1].kwargs["busy"] is False


@pytest.mark.asyncio
async def test_acquire_returns_false_when_disabled():
    with patch.object(host_busy, "enabled", return_value=False):
        taken = await host_busy.acquire("x")
        assert taken is False
        assert host_busy._active_streams == 0


@pytest.mark.asyncio
async def test_release_still_decrements_if_enabled_flips_off():
    with patch.object(host_busy, "enabled", return_value=True), \
         patch.object(host_busy, "_persist_busy", new=AsyncMock(return_value=True)):
        await host_busy.acquire("x")
    with patch.object(host_busy, "enabled", return_value=False), \
         patch.object(host_busy, "_persist_busy", new=AsyncMock(return_value=True)) as mock_persist:
        await host_busy.release()
        assert host_busy._active_streams == 0
        mock_persist.assert_not_awaited()
