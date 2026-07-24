import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Ensure config validation never fails during import in tests: provide the
# bare minimum required env vars before any module under test imports Config.
os.environ.setdefault("API_ID", "12345")
os.environ.setdefault("API_HASH", "test_hash")
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("TELEGRAM_CHANNEL_ID", "-1001111111111")
os.environ.setdefault("ADDON_URL", "http://localhost:7860")

import pytest


@pytest.fixture(autouse=True)
def _reset_config_optional_fields():
    """Each test controls its own optional feature-flag env vars explicitly."""
    from config import Config

    optional_fields = [
        "ADMIN_BOT_TOKEN",
        "MANAGEMENT_GROUP_ID",
        "TMDB_BEARER_TOKEN",
        "SUPABASE_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
    ]
    saved = {f: getattr(Config, f) for f in optional_fields}
    for f in optional_fields:
        setattr(Config, f, "")
    yield
    for f, v in saved.items():
        setattr(Config, f, v)
