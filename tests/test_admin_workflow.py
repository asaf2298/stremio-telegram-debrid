from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from config import Config
from tg_admin_workflow import AdminWorkflowManager


def _configure_enabled():
    Config.ADMIN_BOT_TOKEN = "bot-token"
    Config.MANAGEMENT_GROUP_ID = -1005555555555
    Config.SUPABASE_URL = "https://example.supabase.co"
    Config.SUPABASE_SERVICE_ROLE_KEY = "secret"


def _manager():
    mgr = AdminWorkflowManager()
    mgr.bot = AsyncMock()
    return mgr


def _fake_message(chat_id=-1005555555555, message_id=1, **kwargs):
    defaults = dict(
        id=message_id,
        chat=SimpleNamespace(id=chat_id),
        video=None,
        document=None,
        audio=None,
        caption=None,
        from_user=SimpleNamespace(id=999),
        text=None,
        reply_to_message_id=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults, reply_text=AsyncMock())


def _fake_cq(workflow_id, action, chat_id=-1005555555555):
    return SimpleNamespace(
        data=f"{workflow_id}|{action}",
        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id)),
        from_user=SimpleNamespace(id=999),
        answer=AsyncMock(),
    )


# ---------------------------------------------------------------------------
# Enabled / config gating
# ---------------------------------------------------------------------------

def test_enabled_requires_all_three_pieces():
    mgr = AdminWorkflowManager()
    assert mgr.enabled() is False
    _configure_enabled()
    assert mgr.enabled() is True


@pytest.mark.asyncio
async def test_start_noop_when_disabled():
    mgr = AdminWorkflowManager()
    await mgr.start()
    assert mgr.is_running is False


def test_callback_from_group_rejects_other_chats():
    _configure_enabled()
    mgr = _manager()
    cq_ok = _fake_cq("wf-1", "py", chat_id=Config.MANAGEMENT_GROUP_ID)
    cq_bad = _fake_cq("wf-1", "py", chat_id=-1009999999999)
    assert mgr._callback_from_group(cq_ok) is True
    assert mgr._callback_from_group(cq_bad) is False


# ---------------------------------------------------------------------------
# Upload -> workflow creation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_on_upload_creates_workflow_and_asks_personal():
    _configure_enabled()
    mgr = _manager()
    message = _fake_message(video=SimpleNamespace(file_name="Movie.2023.1080p.mkv"))

    workflow_row = {"id": "wf-1", "status": "open"}
    prompt_msg = SimpleNamespace(id=555)
    message.reply_text = AsyncMock(return_value=prompt_msg)

    with patch("tg_admin_workflow.store.create_workflow", new=AsyncMock(return_value=workflow_row)) as mock_create, \
         patch("tg_admin_workflow.store.update_workflow", new=AsyncMock()) as mock_update:
        await mgr._on_upload(message)

    mock_create.assert_awaited_once()
    message.reply_text.assert_awaited_once()
    assert "האם זה סרטון אישי" in message.reply_text.call_args[0][0]
    # last call updates step/last_prompt_message_id
    last_call_kwargs = mock_update.call_args_list[-1].kwargs
    assert last_call_kwargs["step"] == "ask_personal"
    assert last_call_kwargs["last_prompt_message_id"] == 555


# ---------------------------------------------------------------------------
# Personal flow end-to-end
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_personal_flow_saves_mapping_with_classification_personal():
    _configure_enabled()
    mgr = _manager()

    workflow = {
        "id": "wf-1",
        "chat_id": Config.MANAGEMENT_GROUP_ID,
        "status": "open",
        "payload": {"message_ids": [10], "file_name": "vacation.mp4", "caption": ""},
    }

    with patch("tg_admin_workflow.store.get_workflow", new=AsyncMock(return_value=workflow)), \
         patch("tg_admin_workflow.store.update_workflow", new=AsyncMock()):
        cq = _fake_cq("wf-1", "py")
        await mgr._on_callback(cq)  # -> ask_personal_name
        cq.answer.assert_awaited()

    # Simulate the text reply providing the display name
    workflow["step"] = "ask_personal_name"
    reply = _fake_message(text="חתונה של דני", reply_to_message_id=777)
    with patch("tg_admin_workflow.store.get_open_workflow_by_prompt", new=AsyncMock(return_value=workflow)), \
         patch("tg_admin_workflow.store.update_workflow", new=AsyncMock()):
        await mgr._on_text_reply(reply)

    assert workflow["payload"]["declared_title"] == "חתונה של דני"

    # Simulate tags reply -> triggers finalize -> upsert_media_mapping
    workflow["step"] = "ask_tags"
    tags_reply = _fake_message(text="1080p, ישראלי", reply_to_message_id=778)
    with patch("tg_admin_workflow.store.get_open_workflow_by_prompt", new=AsyncMock(return_value=workflow)), \
         patch("tg_admin_workflow.store.update_workflow", new=AsyncMock()), \
         patch("tg_admin_workflow.store.mark_workflow_status", new=AsyncMock()), \
         patch(
             "tg_admin_workflow.store.upsert_media_mapping", new=AsyncMock(return_value={"id": "m-1"})
         ) as mock_upsert:
        await mgr._on_text_reply(tags_reply)

    mock_upsert.assert_awaited_once()
    kwargs = mock_upsert.call_args.kwargs
    assert kwargs["classification"] == "personal"
    assert kwargs["declared_title"] == "חתונה של דני"
    assert kwargs["tags"] == ["1080p", "ישראלי"]


# ---------------------------------------------------------------------------
# Content flow: TMDb candidate list + pick
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_content_flow_tmdb_lists_candidates_for_selection():
    _configure_enabled()
    mgr = _manager()

    def _cand(tmdb_id, title, year):
        return SimpleNamespace(
            tmdb_id=tmdb_id,
            media_type="movie",
            title=title,
            original_title=title,
            year=year,
            to_dict=lambda i=tmdb_id, t=title, y=year: {
                "tmdb_id": i,
                "media_type": "movie",
                "title": t,
                "original_title": t,
                "year": y,
            },
        )

    candidates = [
        _cand(2, "Dune", 2021),
        _cand(1, "Dune", 1984),
    ]

    workflow = {
        "id": "wf-2",
        "chat_id": Config.MANAGEMENT_GROUP_ID,
        "status": "open",
        "payload": {
            "message_ids": [20],
            "file_name": "Dune.mkv",
            "caption": "",
            "classification": "movie",
            "stremio_type": "movie",
            "declared_title": "Dune",
        },
    }

    with patch("tg_admin_workflow.tmdb_client.is_configured", return_value=True), \
         patch("tg_admin_workflow.tmdb_client.search", new=AsyncMock(return_value=candidates)) as mock_search, \
         patch("tg_admin_workflow.store.update_workflow", new=AsyncMock()):
        await mgr._search_and_confirm(Config.MANAGEMENT_GROUP_ID, workflow)

    mock_search.assert_awaited_once()
    assert mock_search.await_args.kwargs["limit"] == 15
    assert len(workflow["payload"]["tmdb_candidates"]) == 2

    send_kwargs = mgr.bot.send_message.await_args.kwargs
    markup = send_kwargs["reply_markup"]
    # 2 candidate rows + 1 IMDb row
    assert len(markup.inline_keyboard) == 3
    assert markup.inline_keyboard[0][0].callback_data.endswith("|tp_0")
    assert markup.inline_keyboard[1][0].callback_data.endswith("|tp_1")
    assert "IMDb" in markup.inline_keyboard[2][0].text


@pytest.mark.asyncio
async def test_select_tmdb_candidate_resolves_imdb_and_shows_confirm():
    _configure_enabled()
    mgr = _manager()
    workflow = {
        "id": "wf-2b",
        "chat_id": Config.MANAGEMENT_GROUP_ID,
        "status": "open",
        "payload": {
            "message_ids": [20],
            "file_name": "x.mkv",
            "stremio_type": "movie",
            "tmdb_candidates": [
                {
                    "tmdb_id": 872585,
                    "media_type": "movie",
                    "title": "אופנהיימר",
                    "original_title": "Oppenheimer",
                    "year": 2023,
                }
            ],
        },
    }

    with patch(
        "tg_admin_workflow.tmdb_client.get_external_ids",
        new=AsyncMock(return_value={"imdb_id": "tt15398776"}),
    ), \
         patch("tg_admin_workflow.store.update_workflow", new=AsyncMock()), \
         patch.object(mgr, "_confirm_imdb_pick", new=AsyncMock()) as mock_confirm:
        await mgr._select_tmdb_candidate(Config.MANAGEMENT_GROUP_ID, workflow, 0)

    assert workflow["payload"]["imdb_id"] == "tt15398776"
    assert workflow["payload"]["official_title"] == "אופנהיימר"
    assert workflow["payload"]["tmdb_id"] == 872585
    mock_confirm.assert_awaited_once()


@pytest.mark.asyncio
async def test_confirm_imdb_pick_shows_link_and_buttons():
    _configure_enabled()
    mgr = _manager()
    prompt = SimpleNamespace(id=321)
    mgr.bot.send_message = AsyncMock(return_value=prompt)
    workflow = {
        "id": "wf-2c",
        "chat_id": Config.MANAGEMENT_GROUP_ID,
        "payload": {
            "official_title": "Oppenheimer",
            "imdb_id": "tt15398776",
        },
    }

    with patch("tg_admin_workflow.store.update_workflow", new=AsyncMock()) as mock_update:
        await mgr._confirm_imdb_pick(Config.MANAGEMENT_GROUP_ID, workflow)

    text = mgr.bot.send_message.call_args[0][1]
    assert "tt15398776" in text
    markup = mgr.bot.send_message.call_args.kwargs["reply_markup"]
    labels = [btn.text for row in markup.inline_keyboard for btn in row]
    assert "בדיוק" in labels
    assert "אחד אחר" in labels
    assert mock_update.call_args.kwargs["step"] == "confirm_imdb"


@pytest.mark.asyncio
async def test_ask_title_uses_approve_and_other_buttons():
    _configure_enabled()
    mgr = _manager()
    prompt = SimpleNamespace(id=11)
    mgr.bot.send_message = AsyncMock(return_value=prompt)
    workflow = {
        "id": "wf-title",
        "payload": {"file_name": "HDTV 1997 נתי מדיה הרקולס מדובב.avi"},
    }

    with patch("tg_admin_workflow.store.update_workflow", new=AsyncMock()) as mock_update:
        await mgr._ask_title(Config.MANAGEMENT_GROUP_ID, workflow)

    text = mgr.bot.send_message.call_args[0][1]
    assert "האם לחפש את שם הסרט ב imdb" in text
    assert "HDTV 1997" in text
    labels = [
        btn.text
        for row in mgr.bot.send_message.call_args.kwargs["reply_markup"].inline_keyboard
        for btn in row
    ]
    assert labels == ["אישור", "שם אחר"]
    assert mock_update.call_args.kwargs["step"] == "ask_title"


@pytest.mark.asyncio
async def test_title_approve_uses_detected_filename_for_search():
    _configure_enabled()
    mgr = _manager()
    workflow = {
        "id": "wf-ta",
        "chat_id": Config.MANAGEMENT_GROUP_ID,
        "status": "open",
        "payload": {"file_name": "Detected.mkv", "caption": ""},
    }

    with patch("tg_admin_workflow.store.get_workflow", new=AsyncMock(return_value=workflow)), \
         patch("tg_admin_workflow.store.update_workflow", new=AsyncMock()), \
         patch.object(mgr, "_search_and_confirm", new=AsyncMock()) as mock_search:
        cq = _fake_cq("wf-ta", "ta")
        await mgr._on_callback(cq)

    assert workflow["payload"]["declared_title"] == "Detected.mkv"
    mock_search.assert_awaited_once()


# ---------------------------------------------------------------------------
# Content flow: manual tt validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_manual_tt_rejects_invalid_format():
    _configure_enabled()
    mgr = _manager()
    workflow = {
        "id": "wf-3",
        "chat_id": Config.MANAGEMENT_GROUP_ID,
        "status": "open",
        "step": "ask_manual_tt",
        "payload": {"message_ids": [1], "stremio_type": "movie"},
    }
    reply = _fake_message(text="not-a-valid-id", reply_to_message_id=900)

    with patch("tg_admin_workflow.store.get_open_workflow_by_prompt", new=AsyncMock(return_value=workflow)):
        await mgr._on_text_reply(reply)

    reply.reply_text.assert_awaited_once()
    assert "לא מצאתי" in reply.reply_text.call_args[0][0]
    assert workflow["payload"].get("imdb_id") is None


@pytest.mark.asyncio
async def test_manual_tt_accepts_valid_format_and_fetches_cinemeta_title():
    _configure_enabled()
    mgr = _manager()
    workflow = {
        "id": "wf-4",
        "chat_id": Config.MANAGEMENT_GROUP_ID,
        "status": "open",
        "step": "ask_manual_tt",
        "payload": {"message_ids": [1], "stremio_type": "movie", "file_name": "x.mkv"},
    }
    reply = _fake_message(text="tt15398776", reply_to_message_id=901)

    with patch("tg_admin_workflow.store.get_open_workflow_by_prompt", new=AsyncMock(return_value=workflow)), \
         patch("tg_admin_workflow.store.update_workflow", new=AsyncMock()), \
         patch(
             "tg_admin_workflow.get_metadata_from_cinemeta",
             new=AsyncMock(return_value={"name": "Oppenheimer"}),
         ), \
         patch.object(mgr, "_after_imdb_resolved", new=AsyncMock()) as mock_after:
        await mgr._on_text_reply(reply)

    assert workflow["payload"]["imdb_id"] == "tt15398776"
    assert workflow["payload"]["official_title"] == "Oppenheimer"
    mock_after.assert_awaited_once()


@pytest.mark.asyncio
async def test_manual_tt_extracts_id_from_imdb_url():
    _configure_enabled()
    mgr = _manager()
    workflow = {
        "id": "wf-4b",
        "chat_id": Config.MANAGEMENT_GROUP_ID,
        "status": "open",
        "step": "ask_manual_tt",
        "payload": {"message_ids": [1], "stremio_type": "movie", "file_name": "x.mkv"},
    }
    reply = _fake_message(
        text="https://www.imdb.com/title/tt15398776/?ref_=fn_al_tt_1",
        reply_to_message_id=902,
    )

    with patch("tg_admin_workflow.store.get_open_workflow_by_prompt", new=AsyncMock(return_value=workflow)), \
         patch("tg_admin_workflow.store.update_workflow", new=AsyncMock()), \
         patch(
             "tg_admin_workflow.get_metadata_from_cinemeta",
             new=AsyncMock(return_value={"name": "Oppenheimer"}),
         ), \
         patch.object(mgr, "_after_imdb_resolved", new=AsyncMock()) as mock_after:
        await mgr._on_text_reply(reply)

    assert workflow["payload"]["imdb_id"] == "tt15398776"
    mock_after.assert_awaited_once()


def test_extract_imdb_id_from_various_inputs():
    from tg_admin_workflow import extract_imdb_id

    assert extract_imdb_id("tt7450390") == "tt7450390"
    assert extract_imdb_id("https://www.imdb.com/title/tt7450390/") == "tt7450390"
    assert extract_imdb_id("see TT1234567 please") == "tt1234567"
    assert extract_imdb_id("no id here") is None


# ---------------------------------------------------------------------------
# Content flow: continue with declared name (no tt)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_continue_with_declared_name_sets_personal_classification():
    _configure_enabled()
    mgr = _manager()
    workflow = {
        "id": "wf-5",
        "chat_id": Config.MANAGEMENT_GROUP_ID,
        "status": "open",
        "payload": {"declared_title": "My Custom Rip", "stremio_type": "series"},
    }
    with patch("tg_admin_workflow.store.update_workflow", new=AsyncMock()), \
         patch.object(mgr, "_after_imdb_resolved", new=AsyncMock()) as mock_after:
        await mgr._continue_with_declared_name(Config.MANAGEMENT_GROUP_ID, workflow)

    assert workflow["payload"]["imdb_id"] is None
    assert workflow["payload"]["official_title"] is None
    assert workflow["payload"]["tmdb_id"] is None
    assert workflow["payload"]["classification"] == "personal"
    sent = mgr.bot.send_message.call_args[0][1]
    assert "Personal" in sent
    assert "סדרה" in sent
    mock_after.assert_awaited_once()


@pytest.mark.asyncio
async def test_ask_no_candidate_offers_continue_without_id():
    _configure_enabled()
    mgr = _manager()
    prompt = SimpleNamespace(id=55)
    mgr.bot.send_message = AsyncMock(return_value=prompt)
    workflow = {"id": "wf-nc", "payload": {}}

    with patch("tg_admin_workflow.store.update_workflow", new=AsyncMock()):
        await mgr._ask_no_candidate(Config.MANAGEMENT_GROUP_ID, workflow)

    labels = [
        btn.text
        for row in mgr.bot.send_message.call_args.kwargs["reply_markup"].inline_keyboard
        for btn in row
    ]
    assert any("המשך ללא מזהה" in label for label in labels)


# ---------------------------------------------------------------------------
# Season/episode auto-detect vs manual ask
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_after_imdb_resolved_autodetects_season_episode_from_filename():
    _configure_enabled()
    mgr = _manager()
    workflow = {
        "id": "wf-6",
        "payload": {
            "stremio_type": "series",
            "file_name": "Show.S02E05.1080p.mkv",
        },
    }
    with patch("tg_admin_workflow.store.update_workflow", new=AsyncMock()), \
         patch.object(mgr, "_ask_tags", new=AsyncMock()) as mock_ask_tags:
        await mgr._after_imdb_resolved(Config.MANAGEMENT_GROUP_ID, workflow)

    assert workflow["payload"]["season"] == 2
    assert workflow["payload"]["episode"] == 5
    mock_ask_tags.assert_awaited_once()


@pytest.mark.asyncio
async def test_after_imdb_resolved_asks_manually_when_undetected():
    _configure_enabled()
    mgr = _manager()
    workflow = {
        "id": "wf-7",
        "payload": {"stremio_type": "series", "file_name": "SomeShow.mkv"},
    }
    prompt = SimpleNamespace(id=42)
    mgr.bot.send_message = AsyncMock(return_value=prompt)

    with patch("tg_admin_workflow.store.update_workflow", new=AsyncMock()) as mock_update:
        await mgr._after_imdb_resolved(Config.MANAGEMENT_GROUP_ID, workflow)

    mgr.bot.send_message.assert_awaited_once()
    assert "עונה" in mgr.bot.send_message.call_args[0][1]
    assert mock_update.call_args.kwargs["step"] == "ask_season_episode"


@pytest.mark.asyncio
async def test_season_episode_text_reply_parses_sxxexx():
    _configure_enabled()
    mgr = _manager()
    workflow = {
        "id": "wf-8",
        "chat_id": Config.MANAGEMENT_GROUP_ID,
        "status": "open",
        "step": "ask_season_episode",
        "payload": {},
    }
    reply = _fake_message(text="S01E07", reply_to_message_id=950)

    with patch("tg_admin_workflow.store.get_open_workflow_by_prompt", new=AsyncMock(return_value=workflow)), \
         patch("tg_admin_workflow.store.update_workflow", new=AsyncMock()), \
         patch.object(mgr, "_ask_tags", new=AsyncMock()) as mock_ask_tags:
        await mgr._on_text_reply(reply)

    assert workflow["payload"]["season"] == 1
    assert workflow["payload"]["episode"] == 7
    mock_ask_tags.assert_awaited_once()


@pytest.mark.asyncio
async def test_season_episode_text_reply_rejects_unparseable():
    _configure_enabled()
    mgr = _manager()
    workflow = {
        "id": "wf-9",
        "chat_id": Config.MANAGEMENT_GROUP_ID,
        "status": "open",
        "step": "ask_season_episode",
        "payload": {},
    }
    reply = _fake_message(text="not a season", reply_to_message_id=951)

    with patch("tg_admin_workflow.store.get_open_workflow_by_prompt", new=AsyncMock(return_value=workflow)):
        await mgr._on_text_reply(reply)

    reply.reply_text.assert_awaited_once()
    assert workflow["payload"].get("season") is None


# ---------------------------------------------------------------------------
# Concurrency: two open workflows are distinguished by last_prompt_message_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_replies_route_to_correct_workflow_by_prompt_id():
    _configure_enabled()
    mgr = _manager()

    workflow_a = {"id": "wf-a", "chat_id": Config.MANAGEMENT_GROUP_ID, "status": "open",
                  "step": "ask_personal_name", "payload": {}}
    workflow_b = {"id": "wf-b", "chat_id": Config.MANAGEMENT_GROUP_ID, "status": "open",
                  "step": "ask_personal_name", "payload": {}}

    async def fake_lookup(chat_id, prompt_id):
        return workflow_a if prompt_id == 100 else workflow_b

    reply_a = _fake_message(text="Video A", reply_to_message_id=100)
    reply_b = _fake_message(text="Video B", reply_to_message_id=200)

    with patch("tg_admin_workflow.store.get_open_workflow_by_prompt", new=AsyncMock(side_effect=fake_lookup)), \
         patch("tg_admin_workflow.store.update_workflow", new=AsyncMock()), \
         patch.object(mgr, "_ask_tags", new=AsyncMock()):
        await mgr._on_text_reply(reply_a)
        await mgr._on_text_reply(reply_b)

    assert workflow_a["payload"]["declared_title"] == "Video A"
    assert workflow_b["payload"]["declared_title"] == "Video B"


@pytest.mark.asyncio
async def test_reply_to_unknown_or_closed_workflow_is_ignored():
    _configure_enabled()
    mgr = _manager()
    reply = _fake_message(text="whatever", reply_to_message_id=999)

    with patch("tg_admin_workflow.store.get_open_workflow_by_prompt", new=AsyncMock(return_value=None)):
        await mgr._on_text_reply(reply)  # should not raise

    reply.reply_text.assert_not_awaited()


# ---------------------------------------------------------------------------
# Callback rejects when workflow is closed/expired
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_callback_rejected_when_workflow_not_open():
    _configure_enabled()
    mgr = _manager()
    with patch("tg_admin_workflow.store.get_workflow", new=AsyncMock(return_value={"status": "expired"})):
        cq = _fake_cq("wf-x", "py")
        await mgr._on_callback(cq)

    cq.answer.assert_awaited_once()
    assert cq.answer.call_args.kwargs.get("show_alert") is True


# ---------------------------------------------------------------------------
# TorBox finalize
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_finalize_persists_torbox_fields():
    _configure_enabled()
    mgr = _manager()
    workflow = {
        "id": "wf-tb",
        "chat_id": Config.MANAGEMENT_GROUP_ID,
        "created_by": 999,
        "payload": {
            "source": "torbox",
            "torbox_kind": "webdl",
            "torbox_id": 55,
            "torbox_file_id": 3,
            "torbox_hash": "abc123",
            "torbox_status": "ready",
            "message_ids": [],
            "file_name": "remote.mkv",
            "declared_title": "Remote Movie",
            "classification": "movie",
            "stremio_type": "movie",
            "imdb_id": "tt1234567",
            "tags": ["1080p"],
        },
    }

    with patch("tg_admin_workflow.store.upsert_media_mapping", new=AsyncMock(return_value={"id": "m-tb"})) as mock_upsert, \
         patch("tg_admin_workflow.store.mark_workflow_status", new=AsyncMock()):
        await mgr._finalize(Config.MANAGEMENT_GROUP_ID, workflow)

    kwargs = mock_upsert.call_args.kwargs
    assert kwargs["source"] == "torbox"
    assert kwargs["torbox_kind"] == "webdl"
    assert kwargs["torbox_id"] == 55
    assert kwargs["torbox_file_id"] == 3
    assert kwargs["torbox_hash"] == "abc123"
    assert kwargs["torbox_status"] == "ready"
    assert kwargs["message_ids"] == []
