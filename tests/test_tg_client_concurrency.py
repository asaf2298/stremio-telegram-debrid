from unittest.mock import MagicMock, patch

from tg_client import TelegramClientManager


def test_user_client_allows_concurrent_get_file_for_stremio_range_probes():
    """Stremio probes the MP4 tail (moov) while the main stream is open.

    Pyrogram's default max_concurrent_transmissions=1 serializes get_file and
    causes the probe to block forever behind the main download.
    """
    manager = TelegramClientManager()
    with patch("tg_client.Config") as cfg, patch("tg_client.Client") as client_cls:
        cfg.USER_SESSION_STRING = "session"
        cfg.BOT_TOKEN = ""
        cfg.API_ID = 1
        cfg.API_HASH = "hash"
        cfg.validate = MagicMock()
        client_cls.return_value = MagicMock()

        manager.initialize()

        kwargs = client_cls.call_args.kwargs
        assert kwargs["max_concurrent_transmissions"] == 8
        assert kwargs["session_string"] == "session"


def test_bot_client_allows_concurrent_get_file_for_stremio_range_probes():
    manager = TelegramClientManager()
    with patch("tg_client.Config") as cfg, patch("tg_client.Client") as client_cls:
        cfg.USER_SESSION_STRING = ""
        cfg.BOT_TOKEN = "1:token"
        cfg.API_ID = 1
        cfg.API_HASH = "hash"
        cfg.validate = MagicMock()
        client_cls.return_value = MagicMock()

        manager.initialize()

        kwargs = client_cls.call_args.kwargs
        assert kwargs["max_concurrent_transmissions"] == 8
        assert kwargs["bot_token"] == "1:token"
