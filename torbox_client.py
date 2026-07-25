"""TorBox API client for web downloads and torrents.

Used by the optional Hebrew admin workflow and Stremio stream resolution.
Docs: https://api-docs.torbox.app/
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Optional
from urllib.parse import unquote, urlparse

import httpx

from config import Config

logger = logging.getLogger("torbox_client")

BASE_URL = "https://api.torbox.app/v1"
_REQUEST_TIMEOUT = 30.0

_MAGNET_RE = re.compile(r"^magnet:\?", re.IGNORECASE)
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_BTIH_RE = re.compile(r"btih:([a-fA-F0-9]{32,40})", re.IGNORECASE)
_VIDEO_MIMES = ("video/", "application/x-matroska")


def is_configured() -> bool:
    return Config.torbox_enabled()


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {Config.TORBOX_API_KEY}",
        "Accept": "application/json",
    }


def _unwrap_data(resp_json: Any) -> Any:
    if isinstance(resp_json, dict) and "data" in resp_json:
        return resp_json["data"]
    return resp_json


async def _request(
    method: str,
    path: str,
    *,
    params: dict = None,
    json: dict = None,
    data: dict = None,
) -> Any:
    if not is_configured():
        return None
    url = f"{BASE_URL}/{path.lstrip('/')}"
    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT, follow_redirects=False) as client:
            resp = await client.request(
                method, url, params=params, json=json, data=data, headers=_headers()
            )
            if resp.status_code >= 400:
                logger.warning("TorBox %s %s failed: %s %s", method, path, resp.status_code, resp.text[:200])
                return None
            if not resp.content:
                return {}
            return resp.json()
    except Exception as e:
        logger.error("TorBox request failed %s %s: %s", method, path, e)
        return None


def parse_link_text(text: str) -> tuple[Optional[str], Optional[str]]:
    """Return (url, magnet) — exactly one set when input is a supported link."""
    if not text:
        return None, None
    raw = text.strip().split()[0]
    if _MAGNET_RE.match(raw):
        return None, raw
    if _URL_RE.match(raw):
        return raw, None
    return None, None


def filename_from_url(url: str) -> str:
    path = urlparse(url).path
    name = unquote(path.rsplit("/", 1)[-1]) if path else ""
    return name or "download"


def filename_from_magnet(magnet: str) -> str:
    m = re.search(r"[?&]dn=([^&]+)", magnet, re.IGNORECASE)
    if m:
        return unquote(m.group(1).replace("+", " "))
    return "torrent"


def web_link_hash(url: str) -> str:
    return hashlib.md5(url.strip().encode()).hexdigest()


def magnet_info_hash(magnet: str) -> Optional[str]:
    m = _BTIH_RE.search(magnet or "")
    return m.group(1).lower() if m else None


def normalize_name(name: str) -> str:
    if not name:
        return ""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    return re.sub(r"\s+", " ", base.strip().lower())


def is_ready(item: dict) -> bool:
    if not item:
        return False
    return bool(item.get("download_finished")) and bool(item.get("download_present"))


def pick_primary_video_file(files: list) -> Optional[dict]:
    if not files:
        return None
    videos = [f for f in files if isinstance(f, dict) and str(f.get("mimetype", "")).startswith(_VIDEO_MIMES)]
    pool = videos or [f for f in files if isinstance(f, dict)]
    if not pool:
        return None
    return max(pool, key=lambda f: int(f.get("size") or 0))


def item_display_name(item: dict) -> str:
    files = item.get("files") or []
    primary = pick_primary_video_file(files)
    if primary:
        return primary.get("short_name") or primary.get("name") or item.get("name") or "file"
    return item.get("name") or "file"


def item_ids(item: dict, kind: str) -> tuple[int, int]:
    """Return (torbox_id, file_id) for webdl or torrent package."""
    pkg_id = int(item.get("id") or item.get("id_") or 0)
    files = item.get("files") or []
    primary = pick_primary_video_file(files)
    file_id = int((primary or {}).get("id") or (primary or {}).get("id_") or 0)
    return pkg_id, file_id


async def create_web_download(url: str) -> Optional[dict]:
    result = await _request("POST", "api/webdl/createwebdownload", data={"link": url})
    if not result:
        return None
    data = _unwrap_data(result)
    if isinstance(data, dict):
        return data
    return None


async def create_torrent(magnet: str) -> Optional[dict]:
    result = await _request("POST", "api/torrents/createtorrent", data={"magnet": magnet})
    if not result:
        return None
    data = _unwrap_data(result)
    if isinstance(data, dict):
        return data
    return None


async def list_web_downloads(*, bypass_cache: bool = True) -> list:
    params = {"bypass_cache": "true" if bypass_cache else "false", "limit": "1000"}
    result = await _request("GET", "api/webdl/mylist", params=params)
    if not result:
        return []
    data = _unwrap_data(result)
    return data if isinstance(data, list) else ([data] if data else [])


async def list_torrents(*, bypass_cache: bool = True) -> list:
    params = {"bypass_cache": "true" if bypass_cache else "false", "limit": "1000"}
    result = await _request("GET", "api/torrents/mylist", params=params)
    if not result:
        return []
    data = _unwrap_data(result)
    return data if isinstance(data, list) else ([data] if data else [])


async def get_web_download(web_id: int) -> Optional[dict]:
    result = await _request(
        "GET", "api/webdl/mylist", params={"id": str(web_id), "bypass_cache": "true"}
    )
    if not result:
        return None
    data = _unwrap_data(result)
    if isinstance(data, list):
        return data[0] if data else None
    return data if isinstance(data, dict) else None


async def get_torrent(torrent_id: int) -> Optional[dict]:
    result = await _request(
        "GET", "api/torrents/mylist", params={"id": str(torrent_id), "bypass_cache": "true"}
    )
    if not result:
        return None
    data = _unwrap_data(result)
    if isinstance(data, list):
        return data[0] if data else None
    return data if isinstance(data, dict) else None


async def find_web_download_by_link(url: str) -> Optional[dict]:
    link_hash = web_link_hash(url)
    cached = await _request("GET", "api/webdl/checkcached", params={"hash": link_hash, "format": "object"})
    if isinstance(cached, dict):
        for key, val in cached.items():
            if key == link_hash and val:
                if isinstance(val, dict) and val.get("id"):
                    return val
    target = normalize_name(filename_from_url(url))
    for item in await list_web_downloads():
        if not isinstance(item, dict):
            continue
        if str(item.get("hash", "")).lower() == link_hash:
            return item
        if target and normalize_name(item_display_name(item)) == target:
            return item
    return None


async def find_torrent_by_magnet(magnet: str) -> Optional[dict]:
    ih = magnet_info_hash(magnet)
    if not ih:
        return None
    for item in await list_torrents():
        if not isinstance(item, dict):
            continue
        item_hash = str(item.get("hash", "")).lower()
        if item_hash == ih or item_hash.endswith(ih) or ih.endswith(item_hash):
            return item
    return None


def build_permalink(kind: str, torbox_id: int, file_id: int) -> Optional[str]:
    if not is_configured() or not torbox_id:
        return None
    token = Config.TORBOX_API_KEY
    if kind == "webdl":
        return (
            f"{BASE_URL}/api/webdl/requestdl"
            f"?token={token}&web_id={torbox_id}&file_id={file_id}&redirect=true"
        )
    if kind == "torrent":
        return (
            f"{BASE_URL}/api/torrents/requestdl"
            f"?token={token}&torrent_id={torbox_id}&file_id={file_id}&redirect=true"
        )
    return None


async def resolve_stream_url(kind: str, torbox_id: int, file_id: int) -> Optional[str]:
    """Return a redirect permalink that mints a fresh CDN URL on each play."""
    return build_permalink(kind, int(torbox_id), int(file_id))
