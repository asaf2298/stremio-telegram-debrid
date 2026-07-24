"""Server-only Supabase (PostgREST) client for approved media mappings and
the durable Hebrew group-workflow state.

Security notes:
- Uses SUPABASE_SERVICE_ROLE_KEY. This key bypasses Row Level Security and
  must never be exposed to Telegram, Stremio, or any client-facing response.
- All queries use parameterized PostgREST filters (no string-built SQL), and
  every value that ends up in a URL query string is escaped via urllib.
- Never log the service-role key or raw Supabase response bodies that might
  contain it; only log status codes and high-level context.
"""

import logging
import time
from typing import Optional

import httpx

from config import Config

logger = logging.getLogger("metadata_store")

_REQUEST_TIMEOUT = 8.0

# A workflow left "open" for longer than this is treated as expired so a
# forgotten upload doesn't leave a stale conversation state around forever.
WORKFLOW_TTL_SECONDS = 24 * 3600


def _headers() -> dict:
    key = Config.SUPABASE_SERVICE_ROLE_KEY
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _base_url() -> str:
    return f"{Config.SUPABASE_URL}/rest/v1"


def is_configured() -> bool:
    return Config.metadata_store_enabled()


async def _request(method: str, path: str, params: dict = None, json: dict = None) -> Optional[list]:
    if not is_configured():
        return None
    url = f"{_base_url()}/{path.lstrip('/')}"
    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            resp = await client.request(method, url, params=params, json=json, headers=_headers())
            if resp.status_code >= 400:
                logger.error(
                    "Supabase %s %s failed with status %s", method, path, resp.status_code
                )
                return None
            if not resp.content:
                return []
            return resp.json()
    except Exception as e:
        logger.error(f"Supabase request failed ({method} {path}): {e}")
        return None


# ---------------------------------------------------------------------------
# media_mappings
# ---------------------------------------------------------------------------

async def upsert_media_mapping(
    *,
    source_chat_id: int,
    message_ids: list,
    classification: str,
    declared_title: str,
    zip_entry: str = None,
    stremio_type: str = None,
    imdb_id: str = None,
    tmdb_id: int = None,
    official_title: str = None,
    season: int = None,
    episode: int = None,
    tags: list = None,
    resolution: str = None,
    file_name: str = None,
    created_by: int = None,
) -> Optional[dict]:
    """Insert or update the mapping for a given Telegram source message set.

    Uniqueness is enforced by (source_chat_id, message_ids, coalesce(zip_entry,''))
    at the database level; we upsert via PostgREST's on_conflict-less approach:
    try update-by-match first, fall back to insert.
    """
    payload = {
        "source_chat_id": source_chat_id,
        "message_ids": message_ids,
        "zip_entry": zip_entry,
        "classification": classification,
        "stremio_type": stremio_type,
        "imdb_id": imdb_id,
        "tmdb_id": tmdb_id,
        "declared_title": declared_title,
        "official_title": official_title,
        "season": season,
        "episode": episode,
        "tags": tags or [],
        "resolution": resolution,
        "file_name": file_name,
        "created_by": created_by,
    }

    existing = await get_mapping_by_source(source_chat_id, message_ids, zip_entry)
    if existing:
        result = await _request(
            "PATCH",
            "media_mappings",
            params={"id": f"eq.{existing['id']}"},
            json=payload,
        )
    else:
        result = await _request("POST", "media_mappings", json=payload)

    if result:
        return result[0]
    return None


async def get_mapping_by_source(source_chat_id: int, message_ids: list, zip_entry: str = None) -> Optional[dict]:
    ids_literal = "{" + ",".join(str(int(x)) for x in message_ids) + "}"
    params = {
        "source_chat_id": f"eq.{source_chat_id}",
        "message_ids": f"eq.{ids_literal}",
        "select": "*",
        "limit": "1",
    }
    if zip_entry:
        params["zip_entry"] = f"eq.{zip_entry}"
    else:
        params["zip_entry"] = "is.null"
    rows = await _request("GET", "media_mappings", params=params)
    return rows[0] if rows else None


async def get_mapping_by_imdb(
    imdb_id: str, stremio_type: str, season: int = None, episode: int = None
) -> Optional[dict]:
    if not imdb_id or not stremio_type:
        return None
    params = {
        "imdb_id": f"eq.{imdb_id}",
        "stremio_type": f"eq.{stremio_type}",
        "select": "*",
        "limit": "1",
        "order": "updated_at.desc",
    }
    if season is not None:
        params["season"] = f"eq.{season}"
    else:
        params["season"] = "is.null"
    if episode is not None:
        params["episode"] = f"eq.{episode}"
    else:
        params["episode"] = "is.null"
    rows = await _request("GET", "media_mappings", params=params)
    return rows[0] if rows else None


async def list_personal_mappings(limit: int = 100) -> list:
    params = {
        "classification": "eq.personal",
        "select": "*",
        "order": "created_at.desc",
        "limit": str(limit),
    }
    rows = await _request("GET", "media_mappings", params=params)
    return rows or []


async def get_personal_mapping(mapping_id: str) -> Optional[dict]:
    params = {"id": f"eq.{mapping_id}", "select": "*", "limit": "1"}
    rows = await _request("GET", "media_mappings", params=params)
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# media_workflows (durable Hebrew conversation state)
# ---------------------------------------------------------------------------

async def create_workflow(*, chat_id: int, source_message_id: int, created_by: int = None) -> Optional[dict]:
    payload = {
        "chat_id": chat_id,
        "source_message_id": source_message_id,
        "step": "ask_personal",
        "status": "open",
        "payload": {},
        "created_by": created_by,
    }
    result = await _request("POST", "media_workflows", json=payload)
    return result[0] if result else None


async def get_workflow(workflow_id: str) -> Optional[dict]:
    params = {"id": f"eq.{workflow_id}", "select": "*", "limit": "1"}
    rows = await _request("GET", "media_workflows", params=params)
    row = rows[0] if rows else None
    return _expire_if_stale(row)


async def get_open_workflow_by_prompt(chat_id: int, prompt_message_id: int) -> Optional[dict]:
    params = {
        "chat_id": f"eq.{chat_id}",
        "last_prompt_message_id": f"eq.{prompt_message_id}",
        "status": "eq.open",
        "select": "*",
        "limit": "1",
    }
    rows = await _request("GET", "media_workflows", params=params)
    row = rows[0] if rows else None
    return _expire_if_stale(row)


def _expire_if_stale(row: Optional[dict]) -> Optional[dict]:
    if not row or row.get("status") != "open":
        return row
    updated_at = row.get("updated_at")
    if not updated_at:
        return row
    try:
        from datetime import datetime
        updated_ts = datetime.fromisoformat(updated_at.replace("Z", "+00:00")).timestamp()
    except Exception:
        return row
    if time.time() - updated_ts > WORKFLOW_TTL_SECONDS:
        row["status"] = "expired"
        return row
    return row


async def update_workflow(workflow_id: str, **fields) -> Optional[dict]:
    result = await _request(
        "PATCH", "media_workflows", params={"id": f"eq.{workflow_id}"}, json=fields
    )
    return result[0] if result else None


async def mark_workflow_status(workflow_id: str, status: str) -> Optional[dict]:
    return await update_workflow(workflow_id, status=status)
