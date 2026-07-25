import logging
import asyncio
import secrets
import re

# Fix Pyrogram event loop crash on Python 3.12/3.14
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

import urllib.parse
import markupsafe
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, Depends, Response
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

from config import Config
from tg_client import tg_client_manager
from tg_admin_workflow import admin_workflow_manager
import metadata_store as store
import host_busy
from utils import (
    format_size,
    matches_episode,
    get_metadata_from_cinemeta,
    matches_subtitle,
    get_search_query_from_filename,
    parse_split_info,
    is_video_file,
)
from zip_helper import (
    list_zip_files,
    TelegramSeekableReader,
    get_zip_entry_data_offset,
    zip_compressed_generator
)
from search_utils import VideoMatcher, parse_video_resolution, get_resolution_score

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] (%(name)s) - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("stremio_addon")

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        print("\n" + "=" * 60)
        print("   TELEGRAM ADDON BY SUNILROY-DEV")
        print("   GitHub: https://github.com/SunilRoy-dev/stremio-telegram-debrid")
        print("   For educational and personal testing only.")
        print("=" * 60 + "\n")

        # Always bring up HTTP so Stremio can fetch manifest.json even if Telegram
        # auth is temporarily broken. Media routes reconnect lazily on demand.
        try:
            Config.validate()
            await tg_client_manager.start()
        except Exception as e:
            logger.error(
                "Telegram client failed to start during boot: %s. "
                "Serving HTTP anyway; streams will retry on first request.",
                e,
            )
            # Still surface TLS guidance even when Telegram boot fails
            try:
                Config.warn_tls_misconfig()
            except Exception:
                pass

        # Optional Hebrew group workflow (separate bot client). Failure here
        # must never take down Stremio streaming.
        try:
            await admin_workflow_manager.start()
        except Exception as e:
            logger.error(f"Admin workflow bot failed to start: {e}")
        yield
    finally:
        await tg_client_manager.stop()
        try:
            await admin_workflow_manager.stop()
        except Exception as e:
            logger.warning(f"Error stopping admin workflow bot: {e}")

app = FastAPI(lifespan=lifespan)

# Stremio Web (and browsers) need CORS. Do NOT pair allow_origins=["*"] with
# allow_credentials=True — browsers reject that combination as "Failed to fetch".
# allow_private_network is required when web.stremio.com installs a local/LAN addon.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_private_network=True,
)

@app.middleware("http")
async def disable_proxy_buffering(request: Request, call_next):
    response = await call_next(request)
    if "/stream/" in request.url.path:
        response.headers["X-Accel-Buffering"] = "no"
    return response


def api_key_matches(provided: str) -> bool:
    expected = Config.API_KEY or ""
    provided = provided or ""
    if not expected:
        return True
    return secrets.compare_digest(provided, expected)


def require_api_key(provided: str = "") -> None:
    if Config.API_KEY and not api_key_matches(provided):
        raise HTTPException(status_code=403, detail="Unauthorized: Invalid API Key")


def log_safe(value: str, max_len: int = 200) -> str:
    """Strip CR/LF from user-controlled values before they hit the log stream
    (prevents log forging / injected fake log lines)."""
    if not value:
        return value
    return re.sub(r'[\r\n]', ' ', str(value))[:max_len]


def content_disposition_inline(filename: str) -> str:
    """Build a safe Content-Disposition value (prevents header injection).

    HTTP header values must be Latin-1. Put Unicode only in the RFC 5987
    ``filename*`` parameter; keep ``filename=`` as an ASCII fallback so
    Hebrew (and other non-ASCII) names do not 500 the stream endpoint.
    """
    safe = re.sub(r'[\r\n"\\\\]', "_", filename or "file")
    safe = safe[:200] or "file"
    encoded = urllib.parse.quote(safe, safe="")
    ascii_fallback = re.sub(r"[^\x20-\x7E]", "_", safe) or "file"
    ascii_fallback = ascii_fallback.replace(";", "_")
    return f"inline; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"


def provider_label(resolution: str = None) -> str:
    """Consistent Stremio provider/name label for every Telegram-backed stream."""
    if resolution and resolution != "Unknown":
        return f"Telegram_bot [{resolution}]"
    return "Telegram_bot"


def format_mapping_title(mapping: dict, *, fallback: str = "video") -> str:
    """Title shown in Stremio, with approved chat tags as a prefix line.

    Example: tags=["דיבוב עברית", "1080p"] + official "המלך האריה"
    -> "המלך האריה\\n[דיבוב עברית] [1080p]"
    """
    base = (
        mapping.get("official_title")
        or mapping.get("declared_title")
        or mapping.get("file_name")
        or fallback
    )
    tags = [str(t).strip() for t in (mapping.get("tags") or []) if str(t).strip()]
    if not tags:
        return base
    prefix = " ".join(f"[{t}]" for t in tags)
    return f"{base}\n{prefix}"


def assert_chat_allowed(chat_id) -> None:
    """Prevent IDOR: only configured Telegram channels (or the Hebrew-workflow
    management group, if configured) may be streamed."""
    allowed = tg_client_manager.get_channel_ids()
    if Config.MANAGEMENT_GROUP_ID:
        allowed = list(allowed) + [Config.MANAGEMENT_GROUP_ID]
    if not allowed:
        raise HTTPException(status_code=403, detail="No channels configured")

    requested = {str(chat_id).strip()}
    try:
        requested.add(str(int(chat_id)))
    except (TypeError, ValueError):
        pass

    allowed_ids = set()
    for cid in allowed:
        allowed_ids.add(str(cid).strip())
        try:
            allowed_ids.add(str(int(cid)))
        except (TypeError, ValueError):
            pass

    if requested.isdisjoint(allowed_ids):
        logger.warning("Blocked stream request for unauthorized chat_id=%s", chat_id)
        raise HTTPException(status_code=403, detail="Chat not allowed")


MAX_SPLIT_PARTS = 200


def parse_message_ids(message_ids: str) -> list:
    """Parse a comma-separated list of Telegram message ids, capped to avoid
    a single request forcing hundreds of sequential Telegram API calls."""
    ids = [int(x) for x in message_ids.split(",") if x.strip().isdigit()]
    if len(ids) > MAX_SPLIT_PARTS:
        raise HTTPException(status_code=400, detail=f"Too many parts (max {MAX_SPLIT_PARTS})")
    return ids


def parse_range_header(range_header: str, total_size: int) -> tuple:
    """Parse a Range header into (start, end, status_code), clamped to the
    file size. Raises HTTPException(416) for an unsatisfiable range."""
    start, end = 0, total_size - 1
    status_code = 200

    if range_header:
        try:
            bytes_range = range_header.replace("bytes=", "").split("-")
            if bytes_range[0]:
                start = int(bytes_range[0])
            if len(bytes_range) > 1 and bytes_range[1]:
                end = int(bytes_range[1])
        except ValueError:
            pass
        else:
            status_code = 206

    if start < 0 or start >= total_size or start > end:
        raise HTTPException(
            status_code=416,
            detail="Range Not Satisfiable",
            headers={"Content-Range": f"bytes */{total_size}"},
        )
    end = min(end, total_size - 1)
    return start, end, status_code


def parse_tgfile_chat_and_msgids(base_id: str) -> tuple:
    """Extract (chat_id, msg_ids_csv) from a tgfile_* catalog/stream id.

    Raises HTTPException(400) on malformed ids instead of letting IndexError
    bubble up as an unhandled 500.
    """
    try:
        if base_id.startswith("tgfile_splitzip_") or base_id.startswith("tgfile_split_") or base_id.startswith("tgfile_zip_"):
            parts = base_id.split("_")
            return parts[2], parts[3]
        parts = base_id.split("_")
        return parts[1], parts[2]
    except IndexError:
        raise HTTPException(status_code=400, detail=f"Malformed id: {base_id}")


def group_tg_messages(messages: list) -> list:
    grouped = {}
    standalone = []
    
    for msg in messages:
        media = msg.video or msg.document or msg.audio
        if not media:
            continue
            
        fn = getattr(media, "file_name", "") or msg.caption or f"Telegram File {msg.id}"
        base, part = parse_split_info(fn)
        
        if base and part is not None:
            key = base.lower()
            if key not in grouped:
                grouped[key] = {
                    "base_name": base,
                    "parts": {}
                }
            grouped[key]["parts"][part] = msg
        else:
            standalone.append(msg)
            
    results = []
    for key, data in grouped.items():
        parts = data["parts"]
        base_name = data["base_name"]
        
        if len(parts) == 1:
            results.append(list(parts.values())[0])
        else:
            sorted_parts = [msg for part, msg in sorted(parts.items())]
            results.append((base_name, sorted_parts))
            
    for msg in standalone:
        results.append(msg)
        
    return results

def verify_api_key(request: Request):
    api_key = request.query_params.get("api_key", "") or request.path_params.get("api_key", "")
    require_api_key(api_key)

def get_manifest(api_key: str = ""):
    catalogs = []
    if store.is_configured():
        catalogs.append({
            "type": "movie",
            "id": "personal_telegram",
            "name": "Personal Telegram",
        })
    return {
        "id": "community.telegram.stremio.addon",
        "version": "1.1.0",
        "name": "Telegram Addon by SunilRoy-dev",
        "description": "Personal Telegram streaming proxy. For educational & personal testing only. Do not use for unauthorized hosting of copyrighted media.",
        "logo": "https://upload.wikimedia.org/wikipedia/commons/8/82/Telegram_logo.svg",
        "resources": ["catalog", "meta", "stream", "subtitles"],
        "types": ["movie", "series"],
        "idPrefixes": ["tgfile_", "tt", "personal_"],
        "catalogs": catalogs,
        "behaviorHints": {
            "configurable": False,
            "configurationRequired": False
        }
    }

@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def landing(request: Request):
    api_key = request.query_params.get("api_key", "")
    if api_key:
        manifest_url = f"{Config.ADDON_URL}/{urllib.parse.quote(api_key)}/manifest.json"
    else:
        manifest_url = f"{Config.ADDON_URL}/manifest.json"
        
    escaped_manifest_url = markupsafe.escape(manifest_url)
    escaped_stremio_url = markupsafe.escape(manifest_url.replace('http://', '').replace('https://', ''))
    
    web_stremio_url = f"https://web.stremio.com/#/addons?addon={urllib.parse.quote(manifest_url)}"
    escaped_web_stremio_url = markupsafe.escape(web_stremio_url)

    # Detect common misconfig: public HTTP or https://IP:port without a TLS proxy
    host = (request.url.hostname or "").lower()
    scheme = (request.url.scheme or "http").lower()
    forwarded = (request.headers.get("x-forwarded-proto") or "").lower()
    effective_scheme = forwarded or scheme
    is_local = host in {"localhost", "127.0.0.1", "::1"}
    tls_warning = ""
    if not is_local and effective_scheme == "http":
        tls_warning = """
                <div class="tls-warning">
                    <strong>HTTPS required for Stremio</strong>
                    This page is being served over plain <code>http://</code> on a public host.
                    Stremio will try <code>https://</code>, uvicorn will log
                    <code>Invalid HTTP request received</code>, and install fails with
                    <strong>Failed to fetch</strong>.
                    Fix: point a domain at this VPS and run
                    <code>deployment/vps/docker-compose.caddy.yml</code>
                    (or Traefik), then set <code>ADDON_URL=https://your.domain</code>
                    — do not use <code>https://IP:7860</code> against bare uvicorn.
                </div>
        """
    
    api_key_section = ""
    if Config.API_KEY:
        escaped_api_key = markupsafe.escape(api_key)
        api_key_section = f"""
                <div class="url-section" style="margin-bottom: 16px;">
                    <div class="section-title">Enter API Key</div>
                    <div class="input-group">
                        <input class="url-box" id="apiKeyInput" type="text" placeholder="Enter your API Key..." value="{escaped_api_key}" oninput="updateManifestUrl()">
                    </div>
                </div>
        """
        
    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Telegram Addon by SunilRoy-dev</title>
            <meta name="description" content="Stream private Telegram files directly inside Stremio. Secure, lightweight, and ranges-supported proxy.">
            <link rel="preconnect" href="https://fonts.googleapis.com">
            <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
            <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
            <style>
                :root {{
                    --bg-dark: #09090b;
                    --bg-card: #18181b;
                    --border-muted: #27272a;
                    --text-primary: #f4f4f5;
                    --text-secondary: #a1a1aa;
                    --text-muted: #71717a;
                    --color-primary: #2563eb;
                    --color-primary-hover: #1d4ed8;
                    --color-accent: #60a5fa;
                    --font-title: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                    --font-body: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                }}
                * {{
                    box-sizing: border-box;
                    margin: 0;
                    padding: 0;
                }}
                body {{
                    font-family: var(--font-body);
                    background-color: var(--bg-dark);
                    color: var(--text-primary);
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    min-height: 100vh;
                    padding: 40px 20px;
                    margin: 0;
                    overflow-x: hidden;
                }}
                .app-card {{
                    background-color: var(--bg-card);
                    border: 1px solid var(--border-muted);
                    border-radius: 12px;
                    padding: 40px;
                    width: 100%;
                    max-width: 680px;
                    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.4);
                    position: relative;
                }}
                .nav-header {{
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                    margin-bottom: 32px;
                }}
                .brand {{
                    display: flex;
                    align-items: center;
                    gap: 10px;
                    font-family: var(--font-title);
                    font-weight: 700;
                    font-size: 1.1rem;
                    letter-spacing: -0.02em;
                    color: var(--text-primary);
                }}
                .brand-logo {{
                    width: 28px;
                    height: 28px;
                }}
                .star-badge {{
                    display: inline-flex;
                    align-items: center;
                    gap: 6px;
                    background: linear-gradient(135deg, #fbbf24 0%, #d97706 100%);
                    color: #09090b;
                    padding: 8px 14px;
                    border-radius: 6px;
                    font-size: 0.78rem;
                    font-weight: 700;
                    text-decoration: none;
                    box-shadow: 0 0 15px rgba(251, 191, 36, 0.3);
                    transition: all 0.3s ease;
                }}
                .star-badge:hover {{
                    transform: translateY(-2px);
                    box-shadow: 0 0 20px rgba(251, 191, 36, 0.6);
                    color: #000000;
                }}
                .hero {{
                    text-align: center;
                    margin-bottom: 32px;
                }}
                .hero h1 {{
                    font-family: var(--font-title);
                    font-size: 2rem;
                    font-weight: 700;
                    line-height: 1.25;
                    letter-spacing: -0.02em;
                    margin: 8px 0 16px 0;
                    color: #ffffff;
                }}
                .hero p {{
                    font-size: 0.95rem;
                    color: var(--text-secondary);
                    line-height: 1.5;
                    max-width: 520px;
                    margin: 0 auto;
                }}
                .url-section {{
                    background: #09090b;
                    border: 1px solid var(--border-muted);
                    border-radius: 8px;
                    padding: 20px;
                    margin-bottom: 24px;
                }}
                .section-title {{
                    font-family: var(--font-title);
                    font-size: 0.8rem;
                    font-weight: 700;
                    text-transform: uppercase;
                    letter-spacing: 0.05em;
                    color: var(--text-secondary);
                    margin-bottom: 12px;
                }}
                .input-group {{
                    display: flex;
                    gap: 10px;
                }}
                .url-box {{
                    flex: 1;
                    background-color: #18181b;
                    border: 1px solid #27272a;
                    color: var(--text-primary);
                    padding: 12px 16px;
                    border-radius: 6px;
                    font-size: 0.85rem;
                    font-family: monospace;
                    outline: none;
                    transition: border-color 0.2s;
                }}
                .url-box:focus {{
                    border-color: var(--color-primary);
                }}
                .btn-copy {{
                    background: #27272a;
                    border: 1px solid #3f3f46;
                    color: var(--text-primary);
                    padding: 0 16px;
                    border-radius: 6px;
                    font-size: 0.85rem;
                    font-weight: 500;
                    cursor: pointer;
                    display: inline-flex;
                    align-items: center;
                    gap: 6px;
                    transition: all 0.2s;
                }}
                .btn-copy:hover {{
                    background: #3f3f46;
                    border-color: #52525b;
                }}
                .button-group {{
                    display: grid;
                    grid-template-columns: 1fr;
                    gap: 12px;
                    margin-bottom: 32px;
                }}
                @media (min-width: 520px) {{
                    .button-group {{
                        grid-template-columns: 1fr 1fr;
                    }}
                }}
                .btn {{
                    padding: 12px 20px;
                    font-family: var(--font-body);
                    font-size: 0.9rem;
                    font-weight: 500;
                    text-decoration: none;
                    border-radius: 6px;
                    text-align: center;
                    display: inline-flex;
                    align-items: center;
                    justify-content: center;
                    gap: 8px;
                    transition: all 0.2s;
                }}
                .btn-primary {{
                    background-color: var(--color-primary);
                    color: #ffffff;
                }}
                .btn-primary:hover {{
                    background-color: var(--color-primary-hover);
                }}
                .btn-secondary {{
                    background: #27272a;
                    border: 1px solid #3f3f46;
                    color: var(--text-primary);
                }}
                .btn-secondary:hover {{
                    background: #3f3f46;
                    border-color: #52525b;
                }}
                .troubleshoot-details {{
                    background: #09090b;
                    border: 1px solid var(--border-muted);
                    border-radius: 8px;
                    padding: 16px;
                    margin-bottom: 24px;
                }}
                .troubleshoot-summary {{
                    font-family: var(--font-title);
                    font-size: 0.9rem;
                    font-weight: 600;
                    color: var(--text-primary);
                    cursor: pointer;
                    display: flex;
                    align-items: center;
                    user-select: none;
                    outline: none;
                }}
                .troubleshoot-content {{
                    margin-top: 14px;
                    font-size: 0.85rem;
                    color: var(--text-secondary);
                    line-height: 1.5;
                    border-top: 1px solid #27272a;
                    padding-top: 14px;
                }}
                .troubleshoot-content ol {{
                    margin-left: 20px;
                    margin-top: 8px;
                }}
                .troubleshoot-content li {{
                    margin-bottom: 6px;
                }}
                .features-grid {{
                    display: grid;
                    grid-template-columns: 1fr;
                    gap: 16px;
                    margin-bottom: 32px;
                }}
                @media (min-width: 600px) {{
                    .features-grid {{
                        grid-template-columns: 1fr 1fr;
                    }}
                }}
                .feature-card {{
                    background: #18181b;
                    border: 1px solid var(--border-muted);
                    border-radius: 8px;
                    padding: 20px;
                }}
                .feature-icon {{
                    width: 36px;
                    height: 36px;
                    background: #27272a;
                    border-radius: 6px;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    color: var(--color-accent);
                    margin-bottom: 12px;
                }}
                .feature-title {{
                    font-family: var(--font-title);
                    font-size: 0.95rem;
                    font-weight: 600;
                    margin-bottom: 6px;
                    color: var(--text-primary);
                }}
                .feature-desc {{
                    font-size: 0.8rem;
                    color: var(--text-secondary);
                    line-height: 1.45;
                }}
                .license-card {{
                    background: #18181b;
                    border: 1px solid var(--border-muted);
                    border-radius: 8px;
                    padding: 20px;
                    margin-bottom: 32px;
                }}
                .license-title {{
                    font-family: var(--font-title);
                    font-size: 0.9rem;
                    font-weight: 600;
                    color: var(--text-primary);
                    margin-bottom: 6px;
                }}
                .license-text {{
                    font-size: 0.8rem;
                    color: var(--text-secondary);
                    line-height: 1.45;
                }}
                .footer {{
                    text-align: center;
                    font-size: 0.78rem;
                    color: var(--text-muted);
                    border-top: 1px solid var(--border-muted);
                    padding-top: 24px;
                    line-height: 1.6;
                }}
                .footer a {{
                    color: var(--text-secondary);
                    text-decoration: none;
                    font-weight: 500;
                    transition: color 0.2s;
                }}
                .footer a:hover {{
                    color: var(--text-primary);
                    text-decoration: underline;
                }}
                .footer em {{
                    display: block;
                    margin-top: 6px;
                    color: var(--text-muted);
                    font-style: normal;
                }}
                .tls-warning {{
                    background: #422006;
                    border: 1px solid #f59e0b;
                    color: #fde68a;
                    border-radius: 8px;
                    padding: 14px 16px;
                    margin-bottom: 20px;
                    font-size: 0.85rem;
                    line-height: 1.5;
                }}
                .tls-warning code {{
                    background: #1c1917;
                    padding: 1px 5px;
                    border-radius: 4px;
                    font-size: 0.8rem;
                }}
            </style>
        </head>
        <body>
            <div class="app-card">
                <div class="nav-header">
                    <div class="brand">
                        <svg class="brand-logo" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                            <path d="M12 22C17.5228 22 22 17.5228 22 12C22 6.47715 17.5228 2 12 2C6.47715 2 2 6.47715 2 12C2 17.5228 6.47715 22 12 22Z" fill="url(#logoGrad)"/>
                            <path fill-rule="evenodd" clip-rule="evenodd" d="M16.974 8.23272C17.1568 7.2796 16.2004 6.5492 15.3533 6.94008L6.46743 11.0398C5.72727 11.3813 5.76103 12.4431 6.51651 12.7336L8.85507 13.6331C9.52554 13.891 10.2831 13.7828 10.8553 13.3486L14.4754 10.6011C14.6195 10.4917 14.7766 10.7042 14.6534 10.8406L11.597 14.2238C11.107 14.7663 11.2335 15.6322 11.854 16.015L15.3854 18.1936C16.1471 18.6635 17.1264 18.0673 17.0792 17.1685L16.974 8.23272Z" fill="white"/>
                            <defs>
                                <linearGradient id="logoGrad" x1="2" y1="2" x2="22" y2="22" gradientUnits="userSpaceOnUse">
                                    <stop stop-color="#3b82f6"/>
                                    <stop offset="1" stop-color="#1d4ed8"/>
                                </linearGradient>
                            </defs>
                        </svg>
                        Stremio Telegram Addon
                    </div>
                    <div class="header-actions">
                        <a href="https://github.com/SunilRoy-dev/stremio-telegram-debrid" target="_blank" class="star-badge">
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" stroke="none" style="margin-right: 4px;"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"></polygon></svg>
                            Star on GitHub
                        </a>
                    </div>
                </div>
                
                <div class="hero">
                    <h1>Stremio Telegram Addon</h1>
                    <p>A self-hosted Stremio addon proxy to stream videos, audios, and segmented archive parts directly from Telegram.</p>
                </div>
                
                {tls_warning}
                {api_key_section}
                <div class="url-section">
                    <div class="section-title">Addon Manifest URL</div>
                    <div class="input-group">
                        <input class="url-box" id="manifestUrl" type="text" readonly value="{escaped_manifest_url}">
                        <button class="btn-copy" id="btnCopy" onclick="copyManifestUrl()">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" class="feather feather-copy"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>
                            <span id="btnCopyText">Copy</span>
                        </button>
                    </div>
                </div>
                
                <div class="button-group">
                    <a class="btn btn-primary" id="installApp" href="stremio://{escaped_stremio_url}">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>
                        Install on Stremio App
                    </a>
                    <a class="btn btn-secondary" id="installWeb" href="{escaped_web_stremio_url}" target="_blank">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path><polyline points="15 3 21 3 21 9"></polyline><line x1="10" y1="14" x2="21" y2="3"></line></svg>
                        Install on Stremio Web
                    </a>
                </div>
                
                <details class="troubleshoot-details">
                    <summary class="troubleshoot-summary">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="margin-right: 8px; color: #fbbf24;"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path><line x1="12" y1="9" x2="12" y2="13"></line><line x1="12" y1="17" x2="12.01" y2="17"></line></svg>
                        Local Deployment Troubleshooting
                    </summary>
                    <div class="troubleshoot-content">
                        This error commonly happens with <strong>local HTTP</strong> installs or missing API keys. Deployed HTTPS hosts (Render, Koyeb, Hugging Face) usually work with the install buttons.
                        <br><br>
                        <strong>If you see "Failed to fetch":</strong>
                        <ol>
                            <li>If logs show <code>Invalid HTTP request received</code>, you called <code>https://</code> on plain uvicorn (HTTP only). Use a domain + Caddy/Traefik, not <code>https://IP:7860</code>.</li>
                            <li>Confirm the server is running and open <code>/health</code> — <code>status</code> should be <code>ok</code>.</li>
                            <li>If you set an <strong>API_KEY</strong>, enter it above so the manifest URL includes <code>/YOUR_KEY/manifest.json</code>.</li>
                            <li>Use HTTPS for public installs. For local installs, paste the manifest URL into the Stremio Desktop app Add-ons box (do not rely on the <code>stremio://</code> button for localhost).</li>
                            <li>Stremio Web → local addon now allows private-network CORS; still prefer Desktop paste for reliability.</li>
                        </ol>
                        <br>
                        For local deployments, Stremio's desktop protocol handler (<strong>stremio://</strong>) may strip local ports and force HTTPS.
                        <br><br>
                        <strong>How to install locally:</strong>
                        <ol>
                            <li>Click the <strong>Copy</strong> button on the manifest URL field above.</li>
                            <li>Open the <strong>Stremio Desktop App</strong>.</li>
                            <li>Navigate to <strong>Add-ons</strong> (puzzle icon in the sidebar).</li>
                            <li>Paste the copied URL directly into the <strong>Add-on Repository URL</strong> input box at the bottom and click <strong>Install</strong>.</li>
                            <li>Alternatively, use the <strong>Install on Stremio Web</strong> button above.</li>
                        </ol>
                    </div>
                </details>
                
                <div class="features-grid">
                    <div class="feature-card">
                        <div class="feature-icon">
                            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"></path><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"></path></svg>
                        </div>
                        <div class="feature-title">Segmented File Stitching</div>
                        <div class="feature-desc">Groups and stitches split file parts (.001, .part1, etc.) into a virtual continuous stream on the fly.</div>
                    </div>
                    <div class="feature-card">
                        <div class="feature-icon">
                            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polygon points="12 2 2 7 12 12 22 7 12 2"></polygon><polyline points="2 17 12 22 22 17"></polyline><polyline points="2 12 12 17 22 12"></polyline></svg>
                        </div>
                        <div class="feature-title">Range-Seek Support</div>
                        <div class="feature-desc">Full byte-range support allows you to skip forward or seek backward instantly inside your media player.</div>
                    </div>
                    <div class="feature-card">
                        <div class="feature-icon">
                            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path></svg>
                        </div>
                        <div class="feature-title">Subtitle Mapping</div>
                        <div class="feature-desc">Scans the channel dynamically for matching subtitle files (.srt, .vtt, .ass) and injects them.</div>
                    </div>
                    <div class="feature-card">
                        <div class="feature-icon">
                            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect><path d="M7 11V7a5 5 0 0 1 10 0v4"></path></svg>
                        </div>
                        <div class="feature-title">Access Control</div>
                        <div class="feature-desc">Protects endpoints with a secure API key check to prevent unauthorized use of your proxy.</div>
                    </div>
                </div>
                
                <div class="license-card">
                    <div class="license-title">License: MIT Non-Commercial License (MIT-NC)</div>
                    <div class="license-text">
                        This software is published under a custom <strong>MIT Non-Commercial License (MIT-NC)</strong>. Sublicensing, commercial distribution, renting, or monetization of this code or its derivatives is strictly prohibited. Attribution must be preserved in all copies.
                    </div>
                </div>
                
                <div class="footer">
                    Developed by <a href="https://github.com/SunilRoy-dev" target="_blank">SunilRoy-dev</a> | Licensed under MIT-NC
                    <em>For educational and personal testing only. Do not use for unauthorized hosting or distribution of copyrighted media.</em>
                </div>
            </div>
            
            <script>
                const baseManifestUrl = "{Config.ADDON_URL}/manifest.json";
                const baseStremioUrl = baseManifestUrl.replace('http://', '').replace('https://', '');
                
                function updateManifestUrl() {{
                    const apiKeyInput = document.getElementById("apiKeyInput");
                    const manifestUrlEl = document.getElementById("manifestUrl");
                    const installAppEl = document.getElementById("installApp");
                    const installWebEl = document.getElementById("installWeb");
                    
                    let apiKey = "";
                    if (apiKeyInput) {{
                        apiKey = apiKeyInput.value.trim();
                    }} else {{
                        apiKey = new URLSearchParams(window.location.search).get("api_key") || "";
                    }}
                    
                    let manifestUrl = baseManifestUrl;
                    let stremioUrl = baseStremioUrl;
                    
                    if (apiKey) {{
                        const encodedKey = encodeURIComponent(apiKey);
                        manifestUrl = "{Config.ADDON_URL}/" + encodedKey + "/manifest.json";
                        stremioUrl = baseStremioUrl.replace("manifest.json", encodedKey + "/manifest.json");
                    }}
                    
                    if (manifestUrlEl) {{
                        manifestUrlEl.value = manifestUrl;
                    }}
                    if (installAppEl) {{
                        installAppEl.href = "stremio://" + stremioUrl;
                    }}
                    if (installWebEl) {{
                        installWebEl.href = "https://web.stremio.com/#/addons?addon=" + encodeURIComponent(manifestUrl);
                    }}
                }}

                function copyManifestUrl() {{
                    var copyText = document.getElementById("manifestUrl");
                    copyText.select();
                    copyText.setSelectionRange(0, 99999);
                    navigator.clipboard.writeText(copyText.value);
                    
                    var btnText = document.getElementById("btnCopyText");
                    var originalText = btnText.innerHTML;
                    btnText.innerHTML = "Copied!";
                    
                    var copyBtn = document.getElementById("btnCopy");
                    
                    copyBtn.style.background = "#22c55e";
                    copyBtn.style.borderColor = "#22c55e";
                    copyBtn.style.color = "#ffffff";
                    
                    setTimeout(function() {{
                        btnText.innerHTML = originalText;
                        copyBtn.style.background = "";
                        copyBtn.style.borderColor = "";
                        copyBtn.style.color = "";
                    }}, 2000);
                }}

                window.onload = function() {{
                    updateManifestUrl();
                }};
            </script>
        </body>
    </html>
    """
    return HTMLResponse(content=html_content)

@app.get("/health")
async def health(request: Request):
    parsed = urllib.parse.urlparse(Config.ADDON_URL or "")
    host = (request.url.hostname or "").lower()
    forwarded = (request.headers.get("x-forwarded-proto") or "").lower()
    effective_scheme = forwarded or (request.url.scheme or "http")
    public_http = effective_scheme == "http" and host not in {"localhost", "127.0.0.1", "::1"}
    return {
        "status": "ok",
        "telegram": bool(getattr(tg_client_manager, "is_running", False)),
        "addon_url": Config.ADDON_URL,
        "api_key_required": bool(Config.API_KEY),
        "request_scheme": effective_scheme,
        "tls_ok_for_stremio": not public_http,
        "hint": (
            None
            if not public_http
            else "Stremio needs HTTPS. Put Caddy/Traefik in front (deployment/vps/docker-compose.caddy.yml). "
                 "https://IP:7860 against bare uvicorn causes 'Invalid HTTP request received'."
        ),
        "addon_url_scheme": parsed.scheme or "",
    }

@app.api_route("/manifest.json", methods=["GET", "HEAD"])
@app.api_route("/{api_key}/manifest.json", methods=["GET", "HEAD"])
async def manifest_endpoint(api_key: str = ""):
    if Config.API_KEY and not api_key_matches(api_key):
        # Keep CORS-friendly JSON body so Stremio/Web shows auth error, not opaque fetch failure
        return JSONResponse({"detail": "Unauthorized: Invalid API Key"}, status_code=403)
    return get_manifest(api_key)

@app.get("/catalog/{type}/{catalog_id}.json", dependencies=[Depends(verify_api_key)])
@app.get("/catalog/{type}/{catalog_id}/{extra}.json", dependencies=[Depends(verify_api_key)])
@app.get("/{api_key}/catalog/{type}/{catalog_id}.json", dependencies=[Depends(verify_api_key)])
@app.get("/{api_key}/catalog/{type}/{catalog_id}/{extra}.json", dependencies=[Depends(verify_api_key)])
async def catalog_handler(
    type: str, 
    catalog_id: str, 
    extra: str = None,
    api_key: str = ""
):
    if catalog_id == "personal_telegram":
        return await _personal_catalog()

    if type not in ["movie", "series"]:
        return {"metas": []}
        
    query = ""
    if extra:
        params = urllib.parse.parse_qs(extra)
        if "search" in params:
            query = params["search"][0]

    try:
        messages = await tg_client_manager.search_messages(query=query, limit=50)
    except Exception as e:
        logger.error(f"Catalog search failed: {e}")
        return {"metas": []}

    grouped_items = group_tg_messages(messages)
    metas = []
    logo_url = f"{Config.ADDON_URL}/stremio_telegram_logo.png" if getattr(Config, "ADDON_URL", None) else None
    
    for item in grouped_items:
        if isinstance(item, tuple):
            base_name, parts = item
            total_size = sum((x.video or x.document or x.audio).file_size for x in parts if (x.video or x.document or x.audio))
            first_msg = parts[0]
            chat_id = first_msg.chat.id
            msg_ids = ",".join(str(x.id) for x in parts)
            
            is_zip = False
            if base_name.lower().endswith(".zip"):
                try:
                    entries = await list_zip_files(tg_client_manager.client, parts)
                    video_entries = [e for e in entries if is_video_file(e.filename)]
                    if video_entries:
                        is_zip = True
                        for entry in video_entries:
                            tg_id = f"tgfile_splitzip_{chat_id}_{msg_ids}//{entry.filename}"
                            metas.append({
                                "id": tg_id,
                                "type": type,
                                "name": entry.filename,
                                "description": f"💾 Telegram ZIP Entry\n📦 Size: {format_size(entry.file_size)}\n📂 ZIP Archive: {base_name}",
                                "poster": logo_url,
                            })
                except Exception as e:
                    logger.error(f"Error reading split ZIP archive: {e}")
                    
            if not is_zip:
                tg_id = f"tgfile_split_{chat_id}_{msg_ids}"
                metas.append({
                    "id": tg_id,
                    "type": type,
                    "name": base_name,
                    "description": f"💾 Telegram File (Split Parts: {len(parts)})\n📦 Total Size: {format_size(total_size)}",
                    "poster": logo_url,
                })
        else:
            msg = item
            media = msg.video or msg.document or msg.audio
            file_name = getattr(media, "file_name", None) or msg.caption or f"Telegram File {msg.id}"
            file_size = media.file_size
            caption = msg.caption or ""
            
            is_zip = False
            if file_name.lower().endswith(".zip"):
                try:
                    entries = await list_zip_files(tg_client_manager.client, msg)
                    video_entries = [e for e in entries if is_video_file(e.filename)]
                    if video_entries:
                        is_zip = True
                        for entry in video_entries:
                            tg_id = f"tgfile_zip_{msg.chat.id}_{msg.id}//{entry.filename}"
                            metas.append({
                                "id": tg_id,
                                "type": type,
                                "name": entry.filename,
                                "description": f"💾 Telegram ZIP Entry\n📦 Size: {format_size(entry.file_size)}\n📂 ZIP Archive: {file_name}",
                                "poster": logo_url,
                            })
                except Exception as e:
                    logger.error(f"Error reading standalone ZIP archive: {e}")
                    
            if not is_zip:
                tg_id = f"tgfile_{msg.chat.id}_{msg.id}"
                metas.append({
                    "id": tg_id,
                    "type": type,
                    "name": file_name,
                    "description": f"💾 Telegram File\n📦 Size: {format_size(file_size)}\n💬 {caption}" if caption else f"💾 Telegram File\n📦 Size: {format_size(file_size)}",
                    "poster": logo_url,
                })
            
    return {"metas": metas}

from fastapi.responses import FileResponse
import os


async def _personal_catalog() -> dict:
    """List all approved personal mappings as a Stremio catalog."""
    if not store.is_configured():
        return {"metas": []}
    try:
        mappings = await store.list_personal_mappings(limit=200)
    except Exception as e:
        logger.error(f"Failed to list personal catalog: {e}")
        return {"metas": []}

    logo_url = f"{Config.ADDON_URL}/stremio_telegram_logo.png" if getattr(Config, "ADDON_URL", None) else None
    metas = []
    for m in mappings:
        title = format_mapping_title(m, fallback="Personal video")
        description = f"📁 {m.get('file_name', '')}"
        metas.append({
            "id": f"personal_{m['id']}",
            "type": "movie",
            "name": title,
            "description": description,
            "poster": logo_url,
        })
    return {"metas": metas}


async def _personal_stream_and_meta(mapping_id: str) -> dict:
    if not store.is_configured():
        return None
    try:
        mapping = await store.get_personal_mapping(mapping_id)
    except Exception as e:
        logger.error(f"Failed to load personal mapping {mapping_id}: {e}")
        return None
    return mapping


@app.get("/stremio_telegram_logo.png")
async def get_logo():
    if os.path.exists("stremio_telegram_logo.png"):
        return FileResponse("stremio_telegram_logo.png")
    return Response(status_code=404)

@app.get("/stremio_telegram_banner.png")
async def get_banner():
    if os.path.exists("stremio_telegram_banner.png"):
        return FileResponse("stremio_telegram_banner.png")
    return Response(status_code=404)

@app.get("/meta/{type}/{meta_id}.json", dependencies=[Depends(verify_api_key)])
@app.get("/{api_key}/meta/{type}/{meta_id}.json", dependencies=[Depends(verify_api_key)])
async def meta_handler(type: str, meta_id: str, api_key: str = ""):
    if meta_id.startswith("personal_"):
        mapping = await _personal_stream_and_meta(meta_id[len("personal_"):])
        if not mapping:
            return {"meta": {}}
        title = format_mapping_title(mapping, fallback="Personal video")
        description = f"📁 {mapping.get('file_name', '')}"
        return {
            "meta": {
                "id": meta_id,
                "type": "movie",
                "name": title,
                "description": description,
                "poster": f"{Config.ADDON_URL}/stremio_telegram_logo.png" if getattr(Config, "ADDON_URL", None) else None,
            }
        }

    if not meta_id.startswith("tgfile_"):
        return {"meta": {}}
        
    try:
        is_zip_entry = False
        zip_entry_filename = ""
        base_meta_id = meta_id
        if "//" in meta_id:
            is_zip_entry = True
            base_meta_id, zip_entry_filename = meta_id.split("//", 1)
            
        chat_id_val = None
        msg_ids_str = ""
        is_split = False
        
        if base_meta_id.startswith("tgfile_splitzip_"):
            is_split = True
            parts = base_meta_id.split("_")
            chat_id = parts[2]
            msg_ids_str = parts[3]
        elif base_meta_id.startswith("tgfile_split_"):
            is_split = True
            parts = base_meta_id.split("_")
            chat_id = parts[2]
            msg_ids_str = parts[3]
        elif base_meta_id.startswith("tgfile_zip_"):
            parts = base_meta_id.split("_")
            chat_id = parts[2]
            msg_ids_str = parts[3]
        else:
            parts = base_meta_id.split("_")
            chat_id = parts[1]
            msg_ids_str = parts[2]
            
        try:
            chat_id_val = int(chat_id)
        except ValueError:
            chat_id_val = chat_id

        assert_chat_allowed(chat_id_val)

        msg_id_list = [int(x) for x in msg_ids_str.split(",") if x.strip().isdigit()]
        
        messages = []
        for msg_id in msg_id_list:
            msg = await tg_client_manager.get_message(msg_id, chat_id=chat_id_val)
            if msg:
                messages.append(msg)
                
        if not messages:
            return {"meta": {}}
            
        first_msg = messages[0]
        media = first_msg.video or first_msg.document or first_msg.audio
        first_fn = getattr(media, "file_name", "video.mp4") or "video.mp4"
        
        if is_zip_entry and zip_entry_filename:
            file_name = zip_entry_filename
            zip_entries = await list_zip_files(tg_client_manager.client, messages)
            file_size = 0
            for entry in zip_entries:
                if entry.filename == zip_entry_filename:
                    file_size = entry.file_size
                    break
            description = f"💾 Telegram ZIP Entry\n📦 Size: {format_size(file_size)}\n📂 ZIP Archive: {first_fn}"
        else:
            file_name = first_fn
            if is_split:
                base_name, _ = parse_split_info(first_fn)
                file_name = base_name or first_fn
                total_size = sum((x.video or x.document or x.audio).file_size for x in messages if (x.video or x.document or x.audio))
                description = f"💾 Telegram File (Split Parts: {len(messages)})\n📦 Total Size: {format_size(total_size)}"
            else:
                total_size = media.file_size
                caption = first_msg.caption or ""
                description = f"💾 Telegram File\n📦 Size: {format_size(total_size)}\n💬 {caption}" if caption else f"💾 Telegram File\n📦 Size: {format_size(total_size)}"
                
        meta = {
            "id": meta_id,
            "type": type,
            "name": file_name,
            "description": description,
            "poster": f"{Config.ADDON_URL}/stremio_telegram_logo.png" if getattr(Config, "ADDON_URL", None) else None,
            "background": f"{Config.ADDON_URL}/stremio_telegram_banner.png" if getattr(Config, "ADDON_URL", None) else None,
            "logo": f"{Config.ADDON_URL}/stremio_telegram_logo.png" if getattr(Config, "ADDON_URL", None) else None,
        }
        
        if type == "series":
            meta["videos"] = [
                {
                    "id": meta_id,
                    "title": file_name,
                    "season": 1,
                    "episode": 1
                }
            ]
            
        return {"meta": meta}
    except Exception as e:
        logger.error(f"Failed to generate metadata for {meta_id}: {e}")
        return {"meta": {}}


async def find_subtitles_for_video(video_filename: str, api_key: str = "", cached_messages=None) -> list:
    subtitles = []
    search_results = cached_messages or []
    query_param = f"?api_key={api_key}" if api_key else ""
    
    if not search_results:
        query = get_search_query_from_filename(video_filename)
        if query:
            try:
                search_results = await tg_client_manager.search_messages(query=query, limit=20)
            except Exception as e:
                logger.error(f"Subtitle search failed for '{log_safe(query)}': {e}")
                
    seen_msg_ids = set()
    for msg in search_results:
        if msg.id in seen_msg_ids:
            continue
            
        doc = msg.document or msg.audio or msg.video
        if not doc:
            continue
            
        sub_fn = getattr(doc, "file_name", "") or ""
        if sub_fn.lower().endswith(('.srt', '.vtt', '.ass')):
            if matches_subtitle(video_filename, sub_fn):
                seen_msg_ids.add(msg.id)
                
                lang = "eng"
                sub_fn_lower = sub_fn.lower()
                if ".spa" in sub_fn_lower or "spanish" in sub_fn_lower:
                    lang = "spa"
                elif ".fre" in sub_fn_lower or "french" in sub_fn_lower:
                    lang = "fre"
                
                subtitles.append({
                    "id": f"tgsub_{msg.chat.id}_{msg.id}",
                    "url": f"{Config.ADDON_URL}/stream/subtitle/{msg.chat.id}/{msg.id}/{urllib.parse.quote(sub_fn)}{query_param}",
                    "lang": lang
                })
                
    return subtitles


async def build_stream_from_mapping(mapping: dict, api_key: str = "") -> dict:
    """Build a Stremio stream item directly from an approved Supabase mapping,
    skipping Cinemeta lookup and fuzzy Telegram search entirely."""
    file_name = mapping.get("file_name") or "video.mp4"
    title = format_mapping_title(mapping, fallback=file_name)
    resolution = mapping.get("resolution")
    source = mapping.get("source") or "telegram"

    if source == "torbox" or mapping.get("torbox_id"):
        import torbox_client

        kind = mapping.get("torbox_kind") or "webdl"
        torbox_id = mapping.get("torbox_id")
        file_id = mapping.get("torbox_file_id") or 0
        if not torbox_id or not torbox_client.is_configured():
            return None
        stream_url = await torbox_client.resolve_stream_url(kind, int(torbox_id), int(file_id))
        if not stream_url:
            return None
        label = provider_label(resolution)
        if label == "Telegram_bot":
            label = "TorBox"
        elif label.startswith("Telegram_bot"):
            label = label.replace("Telegram_bot", "TorBox", 1)
        return {
            "name": label,
            "title": title,
            "url": stream_url,
            "subtitles": [],
            "behaviorHints": {"notWebReady": True},
        }

    chat_id = mapping.get("source_chat_id")
    message_ids = mapping.get("message_ids") or []

    if not message_ids:
        return None

    try:
        assert_chat_allowed(chat_id)
    except HTTPException:
        logger.warning(f"Supabase mapping references disallowed chat_id={chat_id}; ignoring")
        return None

    query_param = f"?api_key={api_key}" if api_key else ""
    msg_ids_csv = ",".join(str(m) for m in message_ids)
    zip_entry = mapping.get("zip_entry")

    if zip_entry:
        stream_url = f"{Config.ADDON_URL}/stream/zip/{chat_id}/{msg_ids_csv}/{urllib.parse.quote(zip_entry)}{query_param}"
    elif len(message_ids) > 1:
        stream_url = f"{Config.ADDON_URL}/stream/split/{chat_id}/{msg_ids_csv}/{urllib.parse.quote(file_name)}{query_param}"
    else:
        stream_url = f"{Config.ADDON_URL}/stream/file/{chat_id}/{message_ids[0]}/{urllib.parse.quote(file_name)}{query_param}"

    try:
        subtitles = await find_subtitles_for_video(zip_entry or file_name, api_key=api_key)
    except Exception:
        subtitles = []

    return {
        "name": provider_label(resolution),
        "title": title,
        "url": stream_url,
        "subtitles": subtitles,
        "behaviorHints": {"notWebReady": True},
    }


@app.get("/stream/{type}/{stream_id}.json")
@app.get("/{api_key}/stream/{type}/{stream_id}.json")
async def stream_handler(
    type: str, 
    stream_id: str,
    request: Request,
    api_key: str = ""
):
    actual_key = api_key or request.query_params.get("api_key", "")
    require_api_key(actual_key)
        
    streams = []
    query_param = f"?api_key={api_key}" if api_key else ""

    if stream_id.startswith("personal_"):
        mapping_id = stream_id[len("personal_"):]
        mapping = await _personal_stream_and_meta(mapping_id)
        if mapping:
            stream = await build_stream_from_mapping(mapping, api_key=api_key)
            if stream:
                streams.append(stream)
        return {"streams": streams}

    if stream_id.startswith("tgfile_"):
        if "//" in stream_id:
            try:
                base_stream_id, zip_entry_filename = stream_id.split("//", 1)
                chat_id, msg_ids = parse_tgfile_chat_and_msgids(base_stream_id)

                try:
                    chat_id_val = int(chat_id)
                except ValueError:
                    chat_id_val = chat_id

                assert_chat_allowed(chat_id_val)

                msg_id_list = [int(x) for x in msg_ids.split(",") if x.strip().isdigit()]

                messages = []
                for msg_id in msg_id_list:
                    msg = await tg_client_manager.get_message(msg_id, chat_id=chat_id_val)
                    if msg:
                        messages.append(msg)
                        
                if messages:
                    zip_entries = await list_zip_files(tg_client_manager.client, messages)
                    file_size = 0
                    for entry in zip_entries:
                        if entry.filename == zip_entry_filename:
                            file_size = entry.file_size
                            break
                            
                    stream_url = f"{Config.ADDON_URL}/stream/zip/{chat_id}/{msg_ids}/{urllib.parse.quote(zip_entry_filename)}{query_param}"
                    subtitles = await find_subtitles_for_video(zip_entry_filename, api_key=api_key)
                    zip_resolution = parse_video_resolution(zip_entry_filename)
                    
                    streams.append({
                        "name": provider_label(zip_resolution),
                        "title": f"{zip_entry_filename}\n💾 Stream ZIP entry | 📦 {format_size(file_size)}",
                        "url": stream_url,
                        "subtitles": subtitles,
                        "behaviorHints": {
                            "notWebReady": True,
                        }
                    })
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Failed resolving zip stream for {stream_id}: {e}")
        elif stream_id.startswith("tgfile_split_"):
            parts = stream_id.split("_")
            if len(parts) >= 4:
                chat_id = parts[2]
                msg_ids = parts[3]
                try:
                    msg_id_list = [int(x) for x in msg_ids.split(",") if x.isdigit()]
                    try:
                        chat_id_val = int(chat_id)
                    except ValueError:
                        chat_id_val = chat_id

                    assert_chat_allowed(chat_id_val)
                    
                    first_msg = await tg_client_manager.get_message(msg_id_list[0], chat_id=chat_id_val)
                    media = first_msg.video or first_msg.document or first_msg.audio
                    first_fn = getattr(media, "file_name", "video.mp4") or "video.mp4"
                    base_name, _ = parse_split_info(first_fn)
                    if not base_name:
                        base_name = first_fn
                        
                    total_size = 0
                    for m_id in msg_id_list:
                        m = await tg_client_manager.get_message(m_id, chat_id=chat_id_val)
                        if m:
                            med = m.video or m.document or m.audio
                            if med:
                                total_size += med.file_size
                                
                    stream_url = f"{Config.ADDON_URL}/stream/split/{chat_id}/{msg_ids}/{urllib.parse.quote(base_name)}{query_param}"
                    split_resolution = parse_video_resolution(base_name)
                    
                    streams.append({
                        "name": provider_label(split_resolution),
                        "title": f"{base_name}\n💾 Stitch stream | 📦 {format_size(total_size)}",
                        "url": stream_url,
                        "behaviorHints": {
                            "notWebReady": True,
                        }
                    })
                except Exception as e:
                    logger.error(f"Failed resolving split stream for {stream_id}: {e}")
        else:
            parts = stream_id.split("_")
            if len(parts) >= 3:
                chat_id = parts[1]
                msg_id = parts[2]
                try:
                    try:
                        chat_id_val = int(chat_id)
                    except ValueError:
                        chat_id_val = chat_id

                    assert_chat_allowed(chat_id_val)

                    msg = await tg_client_manager.get_message(int(msg_id), chat_id=chat_id_val)
                    media = msg.video or msg.document or msg.audio
                    file_name = getattr(media, "file_name", "video.mp4") or "video.mp4"
                    file_size = media.file_size
                    
                    stream_url = f"{Config.ADDON_URL}/stream/file/{chat_id}/{msg_id}/{urllib.parse.quote(file_name)}{query_param}"
                    subtitles = await find_subtitles_for_video(file_name, api_key=api_key)
                    direct_resolution = parse_video_resolution(file_name)
                    
                    streams.append({
                        "name": provider_label(direct_resolution),
                        "title": f"{file_name}\n💾 Direct stream | 📦 {format_size(file_size)}",
                        "url": stream_url,
                        "subtitles": subtitles,
                        "behaviorHints": {
                            "notWebReady": True,
                        }
                    })
                except Exception as e:
                    logger.error(f"Failed resolving direct stream for {stream_id}: {e}")

    elif stream_id.startswith("tt"):
        imdb_id = stream_id
        season = None
        episode = None

        try:
            if ":" in stream_id:
                parts = stream_id.split(":")
                imdb_id = parts[0]
                season = int(parts[1])
                episode = int(parts[2])
        except (IndexError, ValueError):
            logger.warning(f"Malformed tt stream_id: {log_safe(stream_id)}")
            imdb_id = stream_id.split(":")[0]
            season = None
            episode = None

        # Exact approved mapping first (skips Cinemeta + fuzzy Telegram search).
        if store.is_configured():
            try:
                mapping = await store.get_mapping_by_imdb(imdb_id, type, season, episode)
            except Exception as e:
                logger.error(f"Supabase mapping lookup failed for {imdb_id}: {e}")
                mapping = None
            if mapping:
                stream = await build_stream_from_mapping(mapping, api_key=api_key)
                if stream:
                    return {"streams": [stream]}

        try:
            meta = await get_metadata_from_cinemeta(type, imdb_id)
            movie_name = meta.get("name")
            year_str = meta.get("year")
            year = None
            if year_str:
                try:
                    year = int(str(year_str).split("-")[0])
                except Exception:
                    pass
            
            if movie_name:
                matcher = VideoMatcher()
                if type == "series" and season is not None and episode is not None:
                    queries = matcher.make_series_search_queries(movie_name, season, episode)
                else:
                    queries = matcher.make_movie_search_queries(movie_name, year)
                
                logger.info(f"Resolved IMDb {imdb_id} to '{movie_name}'. Searching Telegram with {len(queries)} safe queries...")
                
                # Search target channels for queries in parallel
                search_tasks = [tg_client_manager.search_messages(query=q, limit=100) for q in queries]
                search_results_lists = await asyncio.gather(*search_tasks, return_exceptions=True)
                
                # Dedup results
                seen_messages = set()
                tg_results_flat = []
                for res_list in search_results_lists:
                    if isinstance(res_list, list):
                        for msg in res_list:
                            if msg and (msg.chat.id, msg.id) not in seen_messages:
                                seen_messages.add((msg.chat.id, msg.id))
                                tg_results_flat.append(msg)
                                
                grouped_results = group_tg_messages(tg_results_flat)
                valid_streams = []
                
                for item in grouped_results:
                    if isinstance(item, tuple):
                        base_name, parts = item
                        first_msg = parts[0]
                        media = first_msg.video or first_msg.document or first_msg.audio
                        file_name = getattr(media, "file_name", "") or ""
                        caption = first_msg.caption or ""
                        
                        score = matcher.calculate_match_score(
                            filename=base_name,
                            caption=caption,
                            title=movie_name,
                            year=year,
                            season=season,
                            episode=episode
                        )
                        if score < matcher.score_threshold:
                            continue
                            
                        total_size = sum((x.video or x.document or x.audio).file_size for x in parts if (x.video or x.document or x.audio))
                        msg_ids = ",".join(str(x.id) for x in parts)
                        chat_id = first_msg.chat.id
                        resolution = parse_video_resolution(f"{base_name} {caption}")
                        
                        is_zip = False
                        if base_name.lower().endswith(".zip"):
                            try:
                                entries = await list_zip_files(tg_client_manager.client, parts)
                                video_entries = [e for e in entries if is_video_file(e.filename)]
                                if video_entries:
                                    is_zip = True
                                    for entry in video_entries:
                                        entry_score = matcher.calculate_match_score(
                                            filename=entry.filename,
                                            caption="",
                                            title=movie_name,
                                            year=year,
                                            season=season,
                                            episode=episode
                                        )
                                        if entry_score < matcher.score_threshold:
                                            continue
                                            
                                        entry_res = parse_video_resolution(entry.filename)
                                        stream_url = f"{Config.ADDON_URL}/stream/zip/{chat_id}/{msg_ids}/{urllib.parse.quote(entry.filename)}{query_param}"
                                        subtitles = await find_subtitles_for_video(entry.filename, api_key=api_key, cached_messages=tg_results_flat)
                                        valid_streams.append({
                                            "name": provider_label(entry_res),
                                            "title": f"{entry.filename}\n💾 Stream ZIP entry | 📦 {format_size(entry.file_size)}",
                                            "url": stream_url,
                                            "subtitles": subtitles,
                                            "behaviorHints": {"notWebReady": True},
                                            "_res_score": get_resolution_score(entry_res),
                                            "_file_size": entry.file_size
                                        })
                            except Exception as e:
                                logger.error(f"Error checking split ZIP for IMDB: {e}")
                                
                        if not is_zip:
                            if not is_video_file(base_name):
                                continue
                            stream_url = f"{Config.ADDON_URL}/stream/split/{chat_id}/{msg_ids}/{urllib.parse.quote(base_name)}{query_param}"
                            valid_streams.append({
                                "name": provider_label(resolution),
                                "title": f"{base_name}\n💾 Stitch stream | 📦 {format_size(total_size)}",
                                "url": stream_url,
                                "behaviorHints": {"notWebReady": True},
                                "_res_score": get_resolution_score(resolution),
                                "_file_size": total_size
                            })
                    else:
                        msg = item
                        media = msg.video or msg.document or msg.audio
                        file_name = getattr(media, "file_name", None) or msg.caption or ""
                        caption = msg.caption or ""
                        
                        score = matcher.calculate_match_score(
                            filename=file_name,
                            caption=caption,
                            title=movie_name,
                            year=year,
                            season=season,
                            episode=episode
                        )
                        if score < matcher.score_threshold:
                            continue
                            
                        file_size = media.file_size
                        chat_id = msg.chat.id
                        resolution = parse_video_resolution(f"{file_name} {caption}")
                        
                        is_zip = False
                        if file_name.lower().endswith(".zip"):
                            try:
                                entries = await list_zip_files(tg_client_manager.client, msg)
                                video_entries = [e for e in entries if is_video_file(e.filename)]
                                if video_entries:
                                    is_zip = True
                                    for entry in video_entries:
                                        entry_score = matcher.calculate_match_score(
                                            filename=entry.filename,
                                            caption="",
                                            title=movie_name,
                                            year=year,
                                            season=season,
                                            episode=episode
                                        )
                                        if entry_score < matcher.score_threshold:
                                            continue
                                            
                                        entry_res = parse_video_resolution(entry.filename)
                                        stream_url = f"{Config.ADDON_URL}/stream/zip/{chat_id}/{msg.id}/{urllib.parse.quote(entry.filename)}{query_param}"
                                        subtitles = await find_subtitles_for_video(entry.filename, api_key=api_key, cached_messages=tg_results_flat)
                                        valid_streams.append({
                                            "name": provider_label(entry_res),
                                            "title": f"{entry.filename}\n💾 Stream ZIP entry | 📦 {format_size(entry.file_size)}",
                                            "url": stream_url,
                                            "subtitles": subtitles,
                                            "behaviorHints": {"notWebReady": True},
                                            "_res_score": get_resolution_score(entry_res),
                                            "_file_size": entry.file_size
                                        })
                            except Exception as e:
                                logger.error(f"Error checking standalone ZIP for IMDB: {e}")
                                
                        if not is_zip:
                            if not is_video_file(file_name):
                                continue
                            stream_url = f"{Config.ADDON_URL}/stream/file/{chat_id}/{msg.id}/{urllib.parse.quote(file_name)}{query_param}"
                            subtitles = await find_subtitles_for_video(file_name, api_key=api_key, cached_messages=tg_results_flat)
                            
                            valid_streams.append({
                                "name": provider_label(resolution),
                                "title": f"{file_name}\n💾 Telegram File | 📦 {format_size(file_size)}",
                                "url": stream_url,
                                "subtitles": subtitles,
                                "behaviorHints": {"notWebReady": True},
                                "_res_score": get_resolution_score(resolution),
                                "_file_size": file_size
                            })
                            
                # Sort by resolution, then size
                valid_streams.sort(key=lambda s: (s.get("_res_score", 0), s.get("_file_size", 0)), reverse=True)
                for s in valid_streams:
                    s.pop("_res_score", None)
                    s.pop("_file_size", None)
                    streams.append(s)
                    
        except Exception as e:
            logger.error(f"Cinemeta search/resolve failed: {e}")

    return {"streams": streams}

@app.get("/subtitles/{type}/{id}.json")
@app.get("/subtitles/{type}/{id}/{extra}.json")
@app.get("/{api_key}/subtitles/{type}/{id}.json")
@app.get("/{api_key}/subtitles/{type}/{id}/{extra}.json")
async def subtitles_handler(
    type: str,
    id: str,
    request: Request,
    extra: str = None,
    api_key: str = ""
):
    actual_key = api_key or request.query_params.get("api_key", "")
    require_api_key(actual_key)
        
    subtitles = []
    
    if id.startswith("tgfile_"):
        parts = id.split("_")
        if len(parts) >= 3:
            chat_id = parts[1]
            msg_id = parts[2]
            try:
                try:
                    chat_id_val = int(chat_id)
                except ValueError:
                    chat_id_val = chat_id

                assert_chat_allowed(chat_id_val)

                msg = await tg_client_manager.get_message(int(msg_id), chat_id=chat_id_val)
                media = msg.video or msg.document or msg.audio
                video_filename = getattr(media, "file_name", "") or ""
                if video_filename:
                    subtitles = await find_subtitles_for_video(video_filename, api_key=api_key)
            except Exception as e:
                logger.error(f"Failed to resolve subtitles for direct catalog ID {id}: {e}")
                
    elif id.startswith("tt"):
        imdb_id = id
        season = None
        episode = None
        try:
            if ":" in id:
                parts = id.split(":")
                imdb_id = parts[0]
                season = int(parts[1])
                episode = int(parts[2])
        except (IndexError, ValueError):
            logger.warning(f"Malformed tt subtitles id: {log_safe(id)}")
            imdb_id = id.split(":")[0]
            season = None
            episode = None

        try:
            video_filename = None
            if extra:
                decoded_extra = urllib.parse.unquote(extra)
                if "?" in decoded_extra:
                    decoded_extra = decoded_extra.split("?", 1)[0]
                params = urllib.parse.parse_qs(decoded_extra)
                if "filename" in params:
                    video_filename = params["filename"][0]

            if video_filename:
                logger.info(f"Resolving subtitles directly for filename: '{log_safe(video_filename)}'")
                subtitles = await find_subtitles_for_video(video_filename, api_key=api_key)
            else:
                meta = await get_metadata_from_cinemeta(type, imdb_id)
                movie_name = meta.get("name")
                if movie_name:
                    tg_results = await tg_client_manager.search_messages(query=movie_name, limit=50)
                    for msg in tg_results:
                        media = msg.video or msg.document or msg.audio
                        fn = getattr(media, "file_name", "") or msg.caption or ""
                        if type == "series" and not matches_episode(fn, season, episode):
                            continue
                        video_filename = fn
                        break
                    
                    if video_filename:
                        subtitles = await find_subtitles_for_video(video_filename, api_key=api_key, cached_messages=tg_results)
        except Exception as e:
            logger.error(f"Failed to resolve subtitles for IMDb ID {id}: {e}")
            
    return {"subtitles": subtitles}

@app.api_route("/stream/subtitle/{chat_id}/{message_id}/{filename}", methods=["GET", "HEAD"])
async def tg_subtitle_proxy(
    chat_id: str, 
    message_id: int, 
    filename: str,
    request: Request,
    api_key: str = ""
):
    require_api_key(api_key)
    assert_chat_allowed(chat_id)
        
    try:
        try:
            chat_id_val = int(chat_id)
        except ValueError:
            chat_id_val = chat_id
        msg = await tg_client_manager.get_message(message_id, chat_id=chat_id_val)
    except Exception as e:
        logger.error(f"Proxy failed to fetch subtitle message: {e}")
        raise HTTPException(status_code=404, detail="Subtitle file not found")
        
    if not msg:
        raise HTTPException(status_code=404, detail="Subtitle message not found")
        
    media = msg.document or msg.audio or msg.video
    if not media:
        raise HTTPException(status_code=404, detail="No media found in subtitle message")
        
    content_type = "text/plain"
    filename_lower = filename.lower()
    if filename_lower.endswith(".srt"):
        content_type = "application/x-subrip"
    elif filename_lower.endswith(".vtt"):
        content_type = "text/vtt"
    elif filename_lower.endswith(".ass"):
        content_type = "text/plain"
        
    headers = {
        "Content-Disposition": content_disposition_inline(filename),
        "Access-Control-Allow-Origin": "*",
        "Content-Length": str(media.file_size),
    }
    
    if request.method == "HEAD":
        return Response(
            status_code=200,
            media_type=content_type,
            headers=headers
        )
        
    try:
        logger.info(f"Downloading subtitle file from Telegram: {log_safe(filename)} (msg ID {message_id})")
        file_buffer = await tg_client_manager.client.download_media(msg, in_memory=True)
        content = file_buffer.getvalue()
    except Exception as e:
        logger.error(f"Failed to download subtitle file: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve subtitle media")
        
    return Response(
        content=content,
        media_type=content_type,
        headers=headers
    )

@app.api_route("/stream/file/{chat_id}/{message_id}/{filename}", methods=["GET", "HEAD"])
async def tg_stream_proxy(
    chat_id: str, 
    message_id: int, 
    filename: str, 
    request: Request,
    api_key: str = ""
):
    require_api_key(api_key)
    assert_chat_allowed(chat_id)

    if not tg_client_manager.is_running or not tg_client_manager.client:
        try:
            await tg_client_manager.start()
        except Exception as e:
            logger.error(f"Telegram client unavailable for stream: {e}")
            raise HTTPException(status_code=503, detail="Telegram client unavailable")
        
    try:
        try:
            chat_id_val = int(chat_id)
        except ValueError:
            chat_id_val = chat_id
        # Get fresh message reference for streaming
        try:
            msg = await tg_client_manager.client.get_messages(chat_id=chat_id_val, message_ids=message_id)
        except Exception:
            msg = await tg_client_manager.get_message(message_id, chat_id=chat_id_val)
    except Exception as e:
        logger.error(f"Proxy failed to fetch message: {e}")
        raise HTTPException(status_code=404, detail="Media file not found")
        
    if not msg:
        raise HTTPException(status_code=404, detail="Media message not found")
        
    media = msg.video or msg.document or msg.audio
    if not media:
        raise HTTPException(status_code=404, detail="No playable media found in message")
        
    file_size = media.file_size
    mime_type = media.mime_type or "video/mp4"
    
    if request.method == "GET":
        asyncio.create_task(
            tg_client_manager.send_play_log(filename, chat_id_val, message_id)
        )
    
    range_header = request.headers.get("Range")
    start, end, status_code = parse_range_header(range_header, file_size)
    content_length = end - start + 1
    
    chunk_size = 1024 * 1024
    offset = start // chunk_size
    skip_bytes = start % chunk_size
    
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(content_length),
        "Content-Disposition": content_disposition_inline(filename),
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Access-Control-Allow-Origin": "*",
    }
    
    # Content-Range must only be sent on 206
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    
    if request.method == "HEAD":
        logger.info(f"HEAD request for media '{log_safe(filename)}' (bytes {start}-{end}/{file_size}) - Status {status_code}")
        return Response(
            status_code=status_code,
            media_type=mime_type,
            headers=headers
        )
        
    async def file_generator():
        nonlocal msg
        stream_key = f"file/{chat_id_val}/{message_id}/{filename}"
        async with host_busy.telegram_stream_guard(stream_key):
            bytes_sent = 0
            bytes_to_skip = skip_bytes
            retry_count = 0
            max_retries = 2
            current_offset = offset
            
            while retry_count <= max_retries:
                try:
                    # Stream using message object
                    async for chunk in tg_client_manager.client.stream_media(msg, offset=current_offset):
                        if bytes_to_skip > 0:
                            if bytes_to_skip < len(chunk):
                                chunk = chunk[bytes_to_skip:]
                                bytes_to_skip = 0
                            else:
                                bytes_to_skip -= len(chunk)
                                continue
                                
                        if bytes_sent + len(chunk) > content_length:
                            chunk = chunk[:content_length - bytes_sent]
                            
                        yield chunk
                        bytes_sent += len(chunk)
                        
                        if bytes_sent >= content_length:
                            break
                    # Stream completed successfully
                    break
                except asyncio.CancelledError:
                    logger.info(f"Streaming cancelled by client for message {message_id}")
                    break
                except Exception as e:
                    err_str = str(e).upper()
                    is_expired = "FILEREFERENCEEXPIRED" in type(e).__name__.upper() or "FILE_REFERENCE" in err_str
                    if is_expired and retry_count < max_retries:
                        retry_count += 1
                        logger.warning(f"File reference expired for message {message_id}, refreshing and retrying ({retry_count}/{max_retries})")
                        try:
                            msg = await tg_client_manager.client.get_messages(chat_id=chat_id_val, message_ids=message_id)
                            total_bytes_streamed = start + bytes_sent
                            current_offset = total_bytes_streamed // chunk_size
                            bytes_to_skip = total_bytes_streamed % chunk_size
                            continue
                        except Exception as refresh_err:
                            logger.error(f"Failed to refresh message for reference recovery: {refresh_err}")
                            break
                    else:
                        logger.error(f"Streaming error on message {message_id}: {e}")
                        break
            
    logger.info(f"Streaming media '{log_safe(filename)}' (bytes {start}-{end}/{file_size}) - Status {status_code}")
    
    return StreamingResponse(
        file_generator(),
        status_code=status_code,
        media_type=mime_type,
        headers=headers
    )

@app.api_route("/stream/split/{chat_id}/{message_ids}/{filename}", methods=["GET", "HEAD"])
async def tg_split_stream_proxy(
    chat_id: str, 
    message_ids: str, 
    filename: str, 
    request: Request,
    api_key: str = ""
):
    require_api_key(api_key)
    assert_chat_allowed(chat_id)
        
    msg_id_list = parse_message_ids(message_ids)
    if not msg_id_list:
        raise HTTPException(status_code=400, detail="Invalid message IDs")
        
    try:
        chat_id_val = int(chat_id)
    except ValueError:
        chat_id_val = chat_id
        
    if request.method == "GET":
        asyncio.create_task(
            tg_client_manager.send_play_log(filename, chat_id_val, msg_id_list[0])
        )
        
    chunks_info = []
    total_size = 0
    
    for msg_id in msg_id_list:
        try:
            # Get fresh message reference for streaming
            try:
                msg = await tg_client_manager.client.get_messages(chat_id=chat_id_val, message_ids=msg_id)
            except Exception:
                msg = await tg_client_manager.get_message(msg_id, chat_id=chat_id_val)
                
            if not msg:
                raise HTTPException(status_code=404, detail=f"Message {msg_id} not found")
            media = msg.video or msg.document or msg.audio
            if not media:
                raise HTTPException(status_code=400, detail=f"No media in message {msg_id}")
                
            chunks_info.append({
                "msg": msg,
                "media": media,
                "size": media.file_size,
                "start_byte": total_size,
                "end_byte": total_size + media.file_size - 1
            })
            total_size += media.file_size
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error fetching metadata for msg {msg_id}: {e}")
            raise HTTPException(status_code=500, detail="Failed resolving split file metadata")
            
    range_header = request.headers.get("Range")
    start, end, status_code = parse_range_header(range_header, total_size)
    content_length = end - start + 1
    mime_type = chunks_info[0]["media"].mime_type or "video/mp4"
    
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(content_length),
        "Content-Disposition": content_disposition_inline(filename),
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }
    
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{total_size}"
    
    if request.method == "HEAD":
        return Response(
            status_code=status_code,
            media_type=mime_type,
            headers=headers
        )
        
    async def split_file_generator():
        stream_key = f"split/{chat_id_val}/{message_ids}/{filename}"
        async with host_busy.telegram_stream_guard(stream_key):
            bytes_sent = 0
            block_size = 1024 * 1024  # 1 MB blocks
            
            for chunk in chunks_info:
                c_start = chunk["start_byte"]
                c_end = chunk["end_byte"]
                
                if c_end < start or c_start > end:
                    continue
                    
                read_start = max(c_start, start)
                read_end = min(c_end, end)
                chunk_read_len = read_end - read_start + 1
                
                local_offset = read_start - c_start
                offset_blocks = local_offset // block_size
                skip_bytes = local_offset % block_size
                
                chunk_bytes_sent = 0
                bytes_to_skip = skip_bytes
                
                try:
                    # Stream using message object
                    async for block in tg_client_manager.client.stream_media(chunk["msg"], offset=offset_blocks):
                        if bytes_to_skip > 0:
                            if bytes_to_skip < len(block):
                                block = block[bytes_to_skip:]
                                bytes_to_skip = 0
                            else:
                                bytes_to_skip -= len(block)
                                continue
                                
                        if chunk_bytes_sent + len(block) > chunk_read_len:
                            block = block[:chunk_read_len - chunk_bytes_sent]
                            
                        yield block
                        chunk_bytes_sent += len(block)
                        bytes_sent += len(block)
                        
                        if chunk_bytes_sent >= chunk_read_len:
                            break
                except Exception as e:
                    logger.error(f"Error streaming split chunk: {e}")
                    break
                    
                if bytes_sent >= content_length:
                    break
                
    logger.info(f"Streaming split media '{log_safe(filename)}' (bytes {start}-{end}/{total_size}) - Status {status_code}")
    
    return StreamingResponse(
        split_file_generator(),
        status_code=status_code,
        media_type=mime_type,
        headers=headers
    )

@app.api_route("/stream/zip/{chat_id}/{message_ids}/{filename}", methods=["GET", "HEAD"])
async def tg_zip_stream_proxy(
    chat_id: str,
    message_ids: str,
    filename: str,
    request: Request,
    api_key: str = ""
):
    require_api_key(api_key)
    assert_chat_allowed(chat_id)
        
    msg_id_list = parse_message_ids(message_ids)
    if not msg_id_list:
        raise HTTPException(status_code=400, detail="Invalid message IDs")
        
    try:
        chat_id_val = int(chat_id)
    except ValueError:
        chat_id_val = chat_id
        
    if request.method == "GET":
        asyncio.create_task(
            tg_client_manager.send_play_log(filename, chat_id_val, msg_id_list[0])
        )
        
    messages = []
    for msg_id in msg_id_list:
        # Get fresh message reference for streaming
        try:
            msg = await tg_client_manager.client.get_messages(chat_id=chat_id_val, message_ids=msg_id)
        except Exception:
            msg = await tg_client_manager.get_message(msg_id, chat_id=chat_id_val)
        if msg:
            messages.append(msg)
            
    if not messages:
        raise HTTPException(status_code=404, detail="Messages not found")
        
    zip_entries = await list_zip_files(tg_client_manager.client, messages)
    target_entry = None
    for entry in zip_entries:
        if entry.filename == filename:
            target_entry = entry
            break
            
    if not target_entry:
        raise HTTPException(status_code=404, detail=f"File '{filename}' not found in ZIP archive")
        
    file_size = target_entry.file_size
    mime_type = "video/mp4"
    filename_lower = filename.lower()
    if filename_lower.endswith(".mkv"):
        mime_type = "video/x-matroska"
    elif filename_lower.endswith(".mp4"):
        mime_type = "video/mp4"
    elif filename_lower.endswith(".avi"):
        mime_type = "video/x-msvideo"
        
    range_header = request.headers.get("Range")
    start, end, status_code = parse_range_header(range_header, file_size)
    content_length = end - start + 1
    
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(content_length),
        "Content-Disposition": content_disposition_inline(filename),
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }
    
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    
    if request.method == "HEAD":
        return Response(
            status_code=status_code,
            media_type=mime_type,
            headers=headers
        )
        
    import zipfile
    if target_entry.compress_type == zipfile.ZIP_STORED:
        logger.info(f"ZIP entry '{log_safe(filename)}' is STORED (uncompressed). Using direct offset proxy.")
        reader = TelegramSeekableReader(tg_client_manager.client, messages)
        data_start = await get_zip_entry_data_offset(reader, target_entry.header_offset)
        
        stream_start = data_start + start
        stream_end = data_start + end
        stream_len = stream_end - stream_start + 1
        
        chunks_info = []
        total_size = 0
        
        for part in reader.parts:
            chunks_info.append({
                "message": part["message"],
                "media": part["media"],
                "size": part["size"],
                "start_byte": part["start"],
                "end_byte": part["end"] - 1
            })
            total_size += part["size"]
            
        async def split_file_generator():
            stream_key = f"zip/{chat_id_val}/{message_ids}/{filename}"
            async with host_busy.telegram_stream_guard(stream_key):
                bytes_sent = 0
                block_size = 1024 * 1024
                
                for chunk in chunks_info:
                    c_start = chunk["start_byte"]
                    c_end = chunk["end_byte"]
                    
                    if c_end < stream_start or c_start > stream_end:
                        continue
                        
                    read_start = max(c_start, stream_start)
                    read_end = min(c_end, stream_end)
                    chunk_read_len = read_end - read_start + 1
                    
                    local_offset = read_start - c_start
                    offset_blocks = local_offset // block_size
                    skip_bytes = local_offset % block_size
                    
                    chunk_bytes_sent = 0
                    bytes_to_skip = skip_bytes
                    
                    try:
                        # Stream using message object
                        async for block in tg_client_manager.client.stream_media(chunk["message"], offset=offset_blocks):
                            if bytes_to_skip > 0:
                                if bytes_to_skip < len(block):
                                    block = block[bytes_to_skip:]
                                    bytes_to_skip = 0
                                else:
                                    bytes_to_skip -= len(block)
                                    continue
                                    
                            if chunk_bytes_sent + len(block) > chunk_read_len:
                                block = block[:chunk_read_len - chunk_bytes_sent]
                                
                            yield block
                            chunk_bytes_sent += len(block)
                            bytes_sent += len(block)
                            
                            if chunk_bytes_sent >= chunk_read_len:
                                break
                    except Exception as e:
                        logger.error(f"Error streaming split ZIP chunk: {e}")
                        break
                        
                    if bytes_sent >= stream_len:
                        break
                    
        logger.info(f"Streaming uncompressed ZIP entry '{log_safe(filename)}' (raw bytes {stream_start}-{stream_end}/{total_size}) - Status {status_code}")
        return StreamingResponse(
            split_file_generator(),
            status_code=status_code,
            media_type=mime_type,
            headers=headers
        )
    else:
        logger.info(f"ZIP entry '{log_safe(filename)}' is COMPRESSED (type {target_entry.compress_type}). Streaming on-the-fly decompression.")
        reader = TelegramSeekableReader(tg_client_manager.client, messages)
        zip_stream_key = f"zip/{chat_id_val}/{message_ids}/{filename}"
        return StreamingResponse(
            host_busy.wrap_byte_stream(
                zip_stream_key,
                zip_compressed_generator(reader, filename, start, end),
            ),
            status_code=status_code,
            media_type=mime_type,
            headers=headers
        )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("addon:app", host="0.0.0.0", port=Config.PORT, reload=True, timeout_keep_alive=300)
