import os
import logging
from urllib.parse import urlparse

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger("config")

class Config:
    PORT = int(os.getenv("PORT", 7860))
    ADDON_URL = os.getenv("ADDON_URL", f"http://localhost:{PORT}").rstrip("/")
    API_KEY = os.getenv("API_KEY", "")
    CACHE_TTL = int(os.getenv("CACHE_TTL", 1800))
    TIMEZONE = os.getenv("TIMEZONE", "UTC")

    API_ID = os.getenv("API_ID")
    API_HASH = os.getenv("API_HASH")
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    USER_SESSION_STRING = os.getenv("USER_SESSION_STRING", "")

    TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
    LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID")

    # Optional direct TLS for uvicorn (prefer a reverse proxy like Caddy/Traefik in production)
    SSL_CERTFILE = os.getenv("SSL_CERTFILE", "")
    SSL_KEYFILE = os.getenv("SSL_KEYFILE", "")

    @classmethod
    def validate(cls):
        missing = []
        if not cls.API_ID:
            missing.append("API_ID")
        if not cls.API_HASH:
            missing.append("API_HASH")
        if not cls.BOT_TOKEN and not cls.USER_SESSION_STRING:
            missing.append("BOT_TOKEN or USER_SESSION_STRING")
        if not cls.TELEGRAM_CHANNEL_ID:
            missing.append("TELEGRAM_CHANNEL_ID")

        if missing:
            raise ValueError(
                f"Missing critical configuration variables: {', '.join(missing)}. "
                "Please configure them in your environment or a .env file."
            )

        try:
            cls.API_ID = int(cls.API_ID)
        except (ValueError, TypeError):
            raise ValueError("API_ID must be a valid integer.")

        if cls.TELEGRAM_CHANNEL_ID and isinstance(cls.TELEGRAM_CHANNEL_ID, str):
            val = cls.TELEGRAM_CHANNEL_ID.strip()
            if val.startswith("-") or val.isdigit():
                try:
                    cls.TELEGRAM_CHANNEL_ID = int(val)
                except ValueError:
                    pass

        if cls.LOG_CHANNEL_ID and isinstance(cls.LOG_CHANNEL_ID, str):
            val = cls.LOG_CHANNEL_ID.strip()
            if val.startswith("-") or val.isdigit():
                try:
                    cls.LOG_CHANNEL_ID = int(val)
                except ValueError:
                    pass

        cls.warn_tls_misconfig()

    @classmethod
    def warn_tls_misconfig(cls):
        """Stremio remote installs require HTTPS. Plain uvicorn is HTTP-only."""
        parsed = urlparse(cls.ADDON_URL or "")
        scheme = (parsed.scheme or "").lower()
        host = (parsed.hostname or "").lower()
        local_hosts = {"localhost", "127.0.0.1", "::1"}
        has_direct_tls = bool(cls.SSL_CERTFILE and cls.SSL_KEYFILE)
        behind_proxy = os.getenv("BEHIND_REVERSE_PROXY", "").lower() in {"1", "true", "yes"}
        if behind_proxy:
            return

        if scheme == "https" and host and host not in local_hosts and not has_direct_tls:
            logger.warning(
                "ADDON_URL is %s but this process speaks plain HTTP unless you put "
                "Caddy/Traefik/nginx (or SSL_CERTFILE/SSL_KEYFILE) in front. "
                "Using https:// against port %s causes uvicorn "
                "'Invalid HTTP request received' and Stremio 'Failed to fetch'. "
                "See deployment/vps/docker-compose.caddy.yml",
                cls.ADDON_URL,
                cls.PORT,
            )
        elif scheme == "http" and host and host not in local_hosts:
            logger.warning(
                "ADDON_URL is plain HTTP on a public host (%s). Stremio will usually "
                "force HTTPS for remote addons and fail with 'Failed to fetch'. "
                "Point a domain at this server and terminate TLS with Caddy/Traefik.",
                host,
            )

        if not cls.API_KEY and host and host not in local_hosts:
            logger.warning(
                "API_KEY is not set and this addon is reachable on a public host (%s). "
                "Anyone with the manifest URL can stream from your Telegram channels "
                "and consume your bandwidth. Set API_KEY to require a shared secret.",
                host,
            )
