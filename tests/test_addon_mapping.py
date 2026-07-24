from unittest.mock import AsyncMock, patch

import pytest

import addon
from config import Config


def test_provider_label_with_resolution():
    assert addon.provider_label("1080p") == "Telegram_bot [1080p]"


def test_provider_label_without_resolution():
    assert addon.provider_label(None) == "Telegram_bot"
    assert addon.provider_label("Unknown") == "Telegram_bot"


def test_format_mapping_title_prefixes_tags():
    assert addon.format_mapping_title({
        "official_title": "המלך האריה",
        "tags": ["דיבוב עברית", "1080p"],
    }) == "[דיבוב עברית] [1080p] המלך האריה"


def test_format_mapping_title_without_tags():
    assert addon.format_mapping_title({
        "declared_title": "Only Title",
        "tags": [],
    }) == "Only Title"


def test_content_disposition_inline_ascii_fallback_for_hebrew():
    value = addon.content_disposition_inline("לולו_סרט.mkv")
    value.encode("latin-1")  # must not raise — used as an HTTP header
    assert "filename*=UTF-8''" in value
    assert "%D7%9C" in value  # Hebrew percent-encoded in filename*
    assert 'filename="' in value
    # quoted filename= must stay ASCII
    quoted = value.split('filename="', 1)[1].split('"', 1)[0]
    quoted.encode("ascii")


def test_assert_chat_allowed_accepts_configured_channel():
    addon.assert_chat_allowed(-1001111111111)  # from conftest TELEGRAM_CHANNEL_ID


def test_assert_chat_allowed_rejects_unknown_chat():
    with pytest.raises(Exception):
        addon.assert_chat_allowed(-1009999999999)


def test_assert_chat_allowed_accepts_management_group_when_configured():
    Config.MANAGEMENT_GROUP_ID = -1002222222222
    try:
        addon.assert_chat_allowed(-1002222222222)
    finally:
        Config.MANAGEMENT_GROUP_ID = ""


@pytest.mark.asyncio
async def test_build_stream_from_mapping_single_file_url():
    mapping = {
        "source_chat_id": -1001111111111,
        "message_ids": [42],
        "zip_entry": None,
        "file_name": "Movie.2023.1080p.mkv",
        "official_title": "Movie Official",
        "declared_title": "Movie Declared",
        "resolution": "1080p",
        "tags": ["דיבוב עברית", "1080p"],
    }
    with patch("addon.find_subtitles_for_video", new=AsyncMock(return_value=[])):
        stream = await addon.build_stream_from_mapping(mapping)

    assert stream["name"] == "Telegram_bot [1080p]"
    assert stream["title"] == "[דיבוב עברית] [1080p] Movie Official"
    assert "/stream/file/-1001111111111/42/" in stream["url"]


@pytest.mark.asyncio
async def test_build_stream_from_mapping_split_url():
    mapping = {
        "source_chat_id": -1001111111111,
        "message_ids": [1, 2, 3],
        "zip_entry": None,
        "file_name": "Movie.mkv",
        "declared_title": "Movie",
        "resolution": None,
    }
    with patch("addon.find_subtitles_for_video", new=AsyncMock(return_value=[])):
        stream = await addon.build_stream_from_mapping(mapping)

    assert stream["name"] == "Telegram_bot"
    assert "/stream/split/-1001111111111/1,2,3/" in stream["url"]


@pytest.mark.asyncio
async def test_build_stream_from_mapping_zip_url():
    mapping = {
        "source_chat_id": -1001111111111,
        "message_ids": [7],
        "zip_entry": "inner.mkv",
        "file_name": "archive.zip",
        "declared_title": "Archived Movie",
        "resolution": "4K",
    }
    with patch("addon.find_subtitles_for_video", new=AsyncMock(return_value=[])):
        stream = await addon.build_stream_from_mapping(mapping)

    assert stream["name"] == "Telegram_bot [4K]"
    assert "/stream/zip/-1001111111111/7/inner.mkv" in stream["url"]


@pytest.mark.asyncio
async def test_build_stream_from_mapping_rejects_disallowed_chat():
    mapping = {
        "source_chat_id": -1009999999999,  # not in TELEGRAM_CHANNEL_ID / MANAGEMENT_GROUP_ID
        "message_ids": [1],
        "file_name": "x.mkv",
        "declared_title": "x",
    }
    result = await addon.build_stream_from_mapping(mapping)
    assert result is None


@pytest.mark.asyncio
async def test_build_stream_from_mapping_empty_message_ids_returns_none():
    mapping = {"source_chat_id": -1001111111111, "message_ids": [], "declared_title": "x"}
    result = await addon.build_stream_from_mapping(mapping)
    assert result is None


@pytest.mark.asyncio
async def test_stream_handler_tt_uses_supabase_mapping_before_cinemeta():
    """Supabase-first: if a mapping exists for the tt id, Cinemeta and the
    Telegram fuzzy search must never be invoked."""
    Config.SUPABASE_URL = "https://example.supabase.co"
    Config.SUPABASE_SERVICE_ROLE_KEY = "secret"

    mapping = {
        "source_chat_id": -1001111111111,
        "message_ids": [55],
        "zip_entry": None,
        "file_name": "Approved.mkv",
        "official_title": "Approved Movie",
        "declared_title": "Approved Movie",
        "resolution": "1080p",
    }

    fake_request = MockRequest()

    with patch("addon.store.is_configured", return_value=True), \
         patch("addon.store.get_mapping_by_imdb", new=AsyncMock(return_value=mapping)) as mock_lookup, \
         patch("addon.get_metadata_from_cinemeta", new=AsyncMock()) as mock_cinemeta, \
         patch("addon.find_subtitles_for_video", new=AsyncMock(return_value=[])):
        result = await addon.stream_handler(
            type="movie", stream_id="tt15398776", request=fake_request, api_key=""
        )

    mock_lookup.assert_awaited_once()
    mock_cinemeta.assert_not_called()
    assert len(result["streams"]) == 1
    assert result["streams"][0]["title"] == "Approved Movie"
    assert result["streams"][0]["name"] == "Telegram_bot [1080p]"


@pytest.mark.asyncio
async def test_stream_handler_tt_falls_back_to_cinemeta_when_no_mapping():
    Config.SUPABASE_URL = "https://example.supabase.co"
    Config.SUPABASE_SERVICE_ROLE_KEY = "secret"

    fake_request = MockRequest()

    with patch("addon.store.is_configured", return_value=True), \
         patch("addon.store.get_mapping_by_imdb", new=AsyncMock(return_value=None)), \
         patch("addon.get_metadata_from_cinemeta", new=AsyncMock(return_value={})) as mock_cinemeta:
        result = await addon.stream_handler(
            type="movie", stream_id="tt15398776", request=fake_request, api_key=""
        )

    mock_cinemeta.assert_awaited_once()
    assert result["streams"] == []


@pytest.mark.asyncio
async def test_personal_catalog_lists_mappings():
    Config.SUPABASE_URL = "https://example.supabase.co"
    Config.SUPABASE_SERVICE_ROLE_KEY = "secret"

    rows = [
        {"id": "abc-123", "declared_title": "My Vacation", "file_name": "vac.mp4", "tags": ["1080p"]},
    ]
    with patch("addon.store.list_personal_mappings", new=AsyncMock(return_value=rows)):
        result = await addon._personal_catalog()

    assert len(result["metas"]) == 1
    assert result["metas"][0]["id"] == "personal_abc-123"
    assert result["metas"][0]["name"] == "[1080p] My Vacation"


@pytest.mark.asyncio
async def test_personal_catalog_empty_when_not_configured():
    result = await addon._personal_catalog()
    assert result == {"metas": []}


@pytest.mark.asyncio
async def test_stream_handler_personal_prefix_builds_stream():
    Config.SUPABASE_URL = "https://example.supabase.co"
    Config.SUPABASE_SERVICE_ROLE_KEY = "secret"

    mapping = {
        "source_chat_id": -1001111111111,
        "message_ids": [99],
        "zip_entry": None,
        "file_name": "vac.mp4",
        "declared_title": "My Vacation",
        "resolution": None,
    }
    fake_request = MockRequest()

    with patch("addon.store.is_configured", return_value=True), \
         patch("addon._personal_stream_and_meta", new=AsyncMock(return_value=mapping)), \
         patch("addon.find_subtitles_for_video", new=AsyncMock(return_value=[])):
        result = await addon.stream_handler(
            type="movie", stream_id="personal_abc-123", request=fake_request, api_key=""
        )

    assert len(result["streams"]) == 1
    assert result["streams"][0]["title"] == "My Vacation"


class MockRequest:
    """Minimal stand-in for fastapi.Request used only for .query_params.get()."""

    class _QP:
        def get(self, *_args, **_kwargs):
            return ""

    query_params = _QP()
