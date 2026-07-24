import importlib
import os

import config


def _reload_config():
    importlib.reload(config)
    return config.Config


def test_supabase_url_strips_trailing_rest_v1_suffix():
    os.environ["SUPABASE_URL"] = "https://example.supabase.co/rest/v1/"
    Config = _reload_config()
    assert Config.SUPABASE_URL == "https://example.supabase.co"


def test_supabase_url_without_suffix_is_unchanged():
    os.environ["SUPABASE_URL"] = "https://example.supabase.co"
    Config = _reload_config()
    assert Config.SUPABASE_URL == "https://example.supabase.co"


def test_supabase_url_without_trailing_slash_before_suffix():
    os.environ["SUPABASE_URL"] = "https://example.supabase.co/rest/v1"
    Config = _reload_config()
    assert Config.SUPABASE_URL == "https://example.supabase.co"
