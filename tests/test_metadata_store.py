from unittest.mock import AsyncMock, patch

import pytest

import metadata_store as store
from config import Config


def _enable_supabase():
    Config.SUPABASE_URL = "https://example.supabase.co"
    Config.SUPABASE_SERVICE_ROLE_KEY = "service-role-secret"


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, content=b"{}"):
        self.status_code = status_code
        self._json = json_data if json_data is not None else []
        self.content = content

    def json(self):
        return self._json


def test_is_configured_false_by_default():
    assert store.is_configured() is False


def test_is_configured_true_when_both_set():
    _enable_supabase()
    assert store.is_configured() is True


@pytest.mark.asyncio
async def test_get_mapping_by_source_not_configured_returns_none():
    result = await store.get_mapping_by_source(-100123, [1, 2])
    assert result is None


@pytest.mark.asyncio
async def test_upsert_media_mapping_inserts_when_no_existing_row():
    _enable_supabase()

    calls = []

    async def fake_request(method, url, params=None, json=None, headers=None):
        calls.append((method, url, params, json))
        if method == "GET":
            return FakeResponse(200, [])
        return FakeResponse(200, [{"id": "new-uuid", **(json or {})}])

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.request = AsyncMock(side_effect=fake_request)
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        result = await store.upsert_media_mapping(
            source_chat_id=-100123,
            message_ids=[10],
            classification="movie",
            declared_title="Some Movie",
            stremio_type="movie",
            imdb_id="tt1234567",
        )

    assert result is not None
    assert result["id"] == "new-uuid"
    methods = [c[0] for c in calls]
    assert methods == ["GET", "POST"]


@pytest.mark.asyncio
async def test_upsert_media_mapping_updates_when_existing_row_found():
    _enable_supabase()

    async def fake_request(method, url, params=None, json=None, headers=None):
        if method == "GET":
            return FakeResponse(200, [{"id": "existing-uuid", "source_chat_id": -100123}])
        assert method == "PATCH"
        return FakeResponse(200, [{"id": "existing-uuid", **(json or {})}])

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.request = AsyncMock(side_effect=fake_request)
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        result = await store.upsert_media_mapping(
            source_chat_id=-100123,
            message_ids=[10],
            classification="movie",
            declared_title="Updated Title",
        )

    assert result["id"] == "existing-uuid"
    assert result["declared_title"] == "Updated Title"


@pytest.mark.asyncio
async def test_get_mapping_by_imdb_null_season_episode_filters():
    _enable_supabase()
    captured_params = {}

    async def fake_request(method, url, params=None, json=None, headers=None):
        captured_params.update(params or {})
        return FakeResponse(200, [{"id": "x", "imdb_id": "tt1234567"}])

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.request = AsyncMock(side_effect=fake_request)
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        result = await store.get_mapping_by_imdb("tt1234567", "movie")

    assert result["id"] == "x"
    assert captured_params["season"] == "is.null"
    assert captured_params["episode"] == "is.null"


@pytest.mark.asyncio
async def test_get_mapping_by_imdb_with_season_episode():
    _enable_supabase()
    captured_params = {}

    async def fake_request(method, url, params=None, json=None, headers=None):
        captured_params.update(params or {})
        return FakeResponse(200, [{"id": "x"}])

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.request = AsyncMock(side_effect=fake_request)
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        await store.get_mapping_by_imdb("tt1234567", "series", season=1, episode=3)

    assert captured_params["season"] == "eq.1"
    assert captured_params["episode"] == "eq.3"


@pytest.mark.asyncio
async def test_request_returns_none_on_http_error():
    _enable_supabase()

    async def fake_request(method, url, params=None, json=None, headers=None):
        return FakeResponse(500, [])

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.request = AsyncMock(side_effect=fake_request)
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        result = await store.get_mapping_by_source(-100123, [1])

    assert result is None


@pytest.mark.asyncio
async def test_create_and_update_workflow():
    _enable_supabase()

    async def fake_request(method, url, params=None, json=None, headers=None):
        if method == "POST":
            return FakeResponse(200, [{"id": "wf-1", "status": "open", **(json or {})}])
        if method == "PATCH":
            return FakeResponse(200, [{"id": "wf-1", "status": "open", **(json or {})}])
        return FakeResponse(200, [])

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.request = AsyncMock(side_effect=fake_request)
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        workflow = await store.create_workflow(chat_id=-100999, source_message_id=42)
        assert workflow["id"] == "wf-1"

        updated = await store.update_workflow("wf-1", step="ask_title")
        assert updated["step"] == "ask_title"


def test_expire_if_stale_marks_old_open_workflow_expired():
    from datetime import datetime, timedelta, timezone

    old_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat().replace("+00:00", "Z")
    row = {"status": "open", "updated_at": old_ts}
    result = store._expire_if_stale(row)
    assert result["status"] == "expired"


def test_expire_if_stale_keeps_recent_open_workflow():
    from datetime import datetime, timezone

    recent_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    row = {"status": "open", "updated_at": recent_ts}
    result = store._expire_if_stale(row)
    assert result["status"] == "open"
