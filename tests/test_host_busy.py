from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

import pytest

import host_busy


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
    host_busy._active_streams = 0
    host_busy._heartbeat_task = None
    with patch.object(host_busy, "enabled", return_value=True), \
         patch.object(host_busy, "_persist_busy", new=AsyncMock()) as mock_persist:
        await host_busy.acquire("file/1/2/movie.mkv")
        assert host_busy._active_streams == 1
        mock_persist.assert_awaited_once()
        assert mock_persist.await_args.kwargs["busy"] is True
        assert mock_persist.await_args.kwargs["active_streams"] == 1


@pytest.mark.asyncio
async def test_release_clears_busy_when_last_stream_ends():
    host_busy._active_streams = 1
    host_busy._heartbeat_task = None
    host_busy._last_stream_key = "file/1/2/movie.mkv"
    with patch.object(host_busy, "enabled", return_value=True), \
         patch.object(host_busy, "_persist_busy", new=AsyncMock()) as mock_persist:
        await host_busy.release()
        assert host_busy._active_streams == 0
        mock_persist.assert_awaited_once()
        assert mock_persist.await_args.kwargs["busy"] is False
        assert mock_persist.await_args.kwargs["active_streams"] == 0


@pytest.mark.asyncio
async def test_telegram_stream_guard_releases_on_generator_exit():
    host_busy._active_streams = 0
    host_busy._heartbeat_task = None
    with patch.object(host_busy, "enabled", return_value=True), \
         patch.object(host_busy, "_persist_busy", new=AsyncMock()) as mock_persist:

        async def gen():
            async with host_busy.telegram_stream_guard("file/1/2/x.mkv"):
                yield b"abc"

        chunks = [c async for c in gen()]
        assert chunks == [b"abc"]
        assert mock_persist.await_count == 2
        assert mock_persist.await_args_list[0].kwargs["busy"] is True
        assert mock_persist.await_args_list[1].kwargs["busy"] is False
