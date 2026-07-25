import pytest

from config import Config
import torbox_client


def test_parse_link_text_url():
    url, magnet = torbox_client.parse_link_text("https://example.com/file.mkv")
    assert url == "https://example.com/file.mkv"
    assert magnet is None


def test_parse_link_text_magnet():
    url, magnet = torbox_client.parse_link_text("magnet:?xt=urn:btih:abc")
    assert url is None
    assert magnet.startswith("magnet:")


def test_is_ready_requires_finished_and_present():
    assert torbox_client.is_ready({"download_finished": True, "download_present": True})
    assert not torbox_client.is_ready({"download_finished": True, "download_present": False})
    assert not torbox_client.is_ready({})


def test_pick_primary_video_file_prefers_largest_video():
    files = [
        {"id": 1, "mimetype": "video/mp4", "size": 100, "short_name": "small.mp4"},
        {"id": 2, "mimetype": "video/mp4", "size": 9000, "short_name": "big.mp4"},
    ]
    picked = torbox_client.pick_primary_video_file(files)
    assert picked["id"] == 2


def test_item_ids_from_package():
    item = {
        "id": 42,
        "files": [{"id": 7, "mimetype": "video/mp4", "size": 1, "short_name": "x.mkv"}],
    }
    assert torbox_client.item_ids(item, "webdl") == (42, 7)


def test_build_permalink_webdl():
    Config.TORBOX_API_KEY = "test-token"
    url = torbox_client.build_permalink("webdl", 10, 3)
    assert "api/webdl/requestdl" in url
    assert "web_id=10" in url
    assert "file_id=3" in url
    assert "token=test-token" in url


def test_build_permalink_torrent():
    Config.TORBOX_API_KEY = "test-token"
    url = torbox_client.build_permalink("torrent", 5, 1)
    assert "api/torrents/requestdl" in url
    assert "torrent_id=5" in url


def test_magnet_info_hash_extracts_btiH():
    magnet = "magnet:?xt=urn:btih:ABCDEF0123456789ABCDEF0123456789ABCDEF01&dn=test"
    assert torbox_client.magnet_info_hash(magnet) == "abcdef0123456789abcdef0123456789abcdef01"
