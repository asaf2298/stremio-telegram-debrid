from unittest.mock import AsyncMock, patch

import pytest

import tmdb_client
from config import Config


def _enable_tmdb():
    Config.TMDB_BEARER_TOKEN = "test-bearer-token"
    tmdb_client._search_cache._data.clear()
    tmdb_client._external_id_cache._data.clear()


class FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data or {}

    def json(self):
        return self._json


def test_is_configured_false_without_token():
    assert tmdb_client.is_configured() is False


@pytest.mark.asyncio
async def test_search_returns_empty_when_not_configured():
    result = await tmdb_client.search("Oppenheimer")
    assert result == []


@pytest.mark.asyncio
async def test_search_movie_hebrew_parses_candidates():
    _enable_tmdb()

    async def fake_get(url, params=None, headers=None):
        assert "language" in params and params["language"] == "he-IL"
        return FakeResponse(
            200,
            {
                "results": [
                    {
                        "id": 872585,
                        "title": "אופנהיימר",
                        "original_title": "Oppenheimer",
                        "release_date": "2023-07-19",
                        "poster_path": "/poster.jpg",
                        "overview": "...",
                    }
                ]
            },
        )

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=fake_get)
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        candidates = await tmdb_client.search("אופנהיימר", media_type="movie")

    assert len(candidates) == 1
    c = candidates[0]
    assert c.tmdb_id == 872585
    assert c.title == "אופנהיימר"
    assert c.original_title == "Oppenheimer"
    assert c.year == 2023
    assert c.stremio_type() == "movie"


@pytest.mark.asyncio
async def test_search_tv_maps_to_series_stremio_type():
    _enable_tmdb()

    async def fake_get(url, params=None, headers=None):
        return FakeResponse(
            200,
            {
                "results": [
                    {
                        "id": 1,
                        "name": "Some Show",
                        "original_name": "Some Show",
                        "first_air_date": "2020-01-01",
                    }
                ]
            },
        )

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=fake_get)
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        candidates = await tmdb_client.search("Some Show", media_type="tv")

    assert candidates[0].stremio_type() == "series"


@pytest.mark.asyncio
async def test_search_returns_empty_on_non_200():
    _enable_tmdb()

    async def fake_get(url, params=None, headers=None):
        return FakeResponse(429, {})

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=fake_get)
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        candidates = await tmdb_client.search("anything")

    assert candidates == []


@pytest.mark.asyncio
async def test_search_gracefully_handles_network_error():
    _enable_tmdb()

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("network down"))
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        candidates = await tmdb_client.search("anything")

    assert candidates == []


@pytest.mark.asyncio
async def test_get_external_ids_returns_imdb_id():
    _enable_tmdb()

    async def fake_get(url, params=None, headers=None):
        return FakeResponse(200, {"imdb_id": "tt15398776"})

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=fake_get)
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        result = await tmdb_client.get_external_ids(872585, "movie")

    assert result["imdb_id"] == "tt15398776"


@pytest.mark.asyncio
async def test_resolve_candidate_imdb_none_when_missing():
    _enable_tmdb()

    async def fake_get(url, params=None, headers=None):
        return FakeResponse(200, {"imdb_id": None})

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=fake_get)
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        candidate = tmdb_client.TmdbCandidate(1, "movie", "T", "T", 2020, None, "")
        imdb = await tmdb_client.resolve_candidate_imdb(candidate)

    assert imdb is None
