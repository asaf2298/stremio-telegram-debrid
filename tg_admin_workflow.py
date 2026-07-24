"""Hebrew metadata-approval workflow running inside a private Telegram group.

Design:
- A separate Pyrogram bot client (ADMIN_BOT_TOKEN) is used only for this
  workflow. The existing USER_SESSION_STRING client is untouched and keeps
  doing all Stremio streaming/search.
- The bot only reacts inside MANAGEMENT_GROUP_ID. Every group member may
  answer questions/approve metadata (no per-user allowlist).
- Conversation state is durable in Supabase (`media_workflows`), keyed by
  (chat_id, source_message_id) and (chat_id, last_prompt_message_id) so that
  multiple uploads can be in progress at once: each question the bot sends
  is tracked, and the next reply is matched back to its workflow via
  Telegram's native "reply to message" feature.
- All strings are Hebrew. This module never touches Stremio-facing HTTP
  endpoints directly; it only writes approved mappings that addon.py reads.
"""

import logging
import re

from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from config import Config
import metadata_store as store
import tmdb_client
from utils import parse_season_episode, get_metadata_from_cinemeta
from search_utils import parse_video_resolution

logger = logging.getLogger("tg_admin_workflow")

_IMDB_RE = re.compile(r"^tt\d{5,9}$", re.IGNORECASE)
_SKIP_WORDS = {"דלג", "-", "skip", "none"}
_CONFIRM_WORDS = {"אישור", "אשר", "ok", "כן"}


def _kb(rows: list) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(text, callback_data=cb) for text, cb in row] for row in rows]
    )


def _cb(workflow_id: str, action: str) -> str:
    return f"{workflow_id}|{action}"


class AdminWorkflowManager:
    def __init__(self):
        self.bot: Client = None
        self.is_running = False

    def enabled(self) -> bool:
        return Config.admin_workflow_enabled()

    def _initialize(self):
        self.bot = Client(
            name="tg_stremio_admin_bot",
            api_id=Config.API_ID,
            api_hash=Config.API_HASH,
            bot_token=Config.ADMIN_BOT_TOKEN,
            in_memory=True,
            no_updates=False,
        )
        self._register_handlers()

    async def start(self):
        if not self.enabled():
            logger.info("Hebrew admin workflow disabled (missing bot/group/Supabase config).")
            return
        if self.is_running:
            return
        try:
            if not self.bot:
                self._initialize()
            await self.bot.start()
            self.is_running = True
            logger.info("Admin workflow bot started (Hebrew group workflow enabled).")
        except Exception as e:
            logger.error(f"Failed to start admin workflow bot: {e}")
            self.is_running = False

    async def stop(self):
        if self.is_running and self.bot:
            try:
                await self.bot.stop()
            except Exception as e:
                logger.warning(f"Error stopping admin workflow bot: {e}")
            self.is_running = False

    # ------------------------------------------------------------------
    # Handler registration
    # ------------------------------------------------------------------

    def _register_handlers(self):
        group_id = Config.MANAGEMENT_GROUP_ID

        @self.bot.on_message(
            filters.chat(group_id) & (filters.video | filters.document | filters.audio)
        )
        async def on_upload(client, message: Message):
            try:
                await self._on_upload(message)
            except Exception as e:
                logger.error(f"Error handling upload workflow start: {e}")

        @self.bot.on_message(filters.chat(group_id) & filters.text & filters.reply)
        async def on_reply(client, message: Message):
            try:
                await self._on_text_reply(message)
            except Exception as e:
                logger.error(f"Error handling workflow text reply: {e}")

        @self.bot.on_callback_query()
        async def on_callback(client, cq: CallbackQuery):
            try:
                if not self._callback_from_group(cq):
                    await cq.answer()
                    return
                await self._on_callback(cq)
            except Exception as e:
                logger.error(f"Error handling workflow callback: {e}")
                try:
                    await cq.answer("שגיאה, נסה שוב", show_alert=True)
                except Exception:
                    pass

    def _callback_from_group(self, cq: CallbackQuery) -> bool:
        """CallbackQuery has no top-level .chat (unlike Message), so
        filters.chat() can't be used here — check manually instead."""
        return bool(cq.message and cq.message.chat.id == Config.MANAGEMENT_GROUP_ID)

    # ------------------------------------------------------------------
    # Upload entrypoint
    # ------------------------------------------------------------------

    async def _on_upload(self, message: Message):
        media = message.video or message.document or message.audio
        if not media:
            return

        file_name = getattr(media, "file_name", "") or message.caption or f"Telegram File {message.id}"
        created_by = message.from_user.id if message.from_user else None

        workflow = await store.create_workflow(
            chat_id=message.chat.id,
            source_message_id=message.id,
            created_by=created_by,
        )
        if not workflow:
            logger.error("Failed to create workflow row in Supabase for upload %s", message.id)
            return

        payload = {
            "message_ids": [message.id],
            "file_name": file_name,
            "caption": message.caption or "",
        }
        await store.update_workflow(workflow["id"], payload=payload)

        prompt = await message.reply_text(
            f"התקבל קובץ חדש: `{file_name}`\n\nהאם זה סרטון אישי?",
            reply_markup=_kb(
                [
                    [("כן, אישי", _cb(workflow["id"], "py")), ("לא, חפש תוכן", _cb(workflow["id"], "pn"))],
                ]
            ),
        )
        await store.update_workflow(
            workflow["id"], step="ask_personal", last_prompt_message_id=prompt.id
        )

    # ------------------------------------------------------------------
    # Callback (button) handling
    # ------------------------------------------------------------------

    async def _on_callback(self, cq: CallbackQuery):
        try:
            workflow_id, action = cq.data.split("|", 1)
        except ValueError:
            await cq.answer()
            return

        workflow = await store.get_workflow(workflow_id)
        if not workflow or workflow.get("status") != "open":
            await cq.answer("השאלה הזו לא בתוקף יותר", show_alert=True)
            return

        await cq.answer()
        chat_id = workflow["chat_id"]
        payload = workflow.get("payload") or {}

        if action == "py":
            await self._ask_personal_name(chat_id, workflow)
        elif action == "pn":
            await self._ask_category(chat_id, workflow)
        elif action == "cm":
            payload["classification"] = "movie"
            payload["stremio_type"] = "movie"
            await store.update_workflow(workflow_id, payload=payload)
            workflow["payload"] = payload
            await self._ask_title(chat_id, workflow)
        elif action == "cs":
            payload["classification"] = "series"
            payload["stremio_type"] = "series"
            await store.update_workflow(workflow_id, payload=payload)
            workflow["payload"] = payload
            await self._ask_title(chat_id, workflow)
        elif action == "ca":
            await self._ask_anime_subtype(chat_id, workflow)
        elif action == "am":
            payload["classification"] = "anime"
            payload["stremio_type"] = "movie"
            await store.update_workflow(workflow_id, payload=payload)
            workflow["payload"] = payload
            await self._ask_title(chat_id, workflow)
        elif action == "as":
            payload["classification"] = "anime"
            payload["stremio_type"] = "series"
            await store.update_workflow(workflow_id, payload=payload)
            workflow["payload"] = payload
            await self._ask_title(chat_id, workflow)
        elif action == "tc_approve":
            await self._apply_tmdb_candidate(chat_id, workflow)
        elif action == "tc_manual" or action == "mt_from_confirm":
            await self._ask_manual_tt(chat_id, workflow)
        elif action == "tc_name" or action == "un":
            await self._continue_with_declared_name(chat_id, workflow)
        elif action == "edit_tags":
            await self._ask_tags(chat_id, workflow, editing=True)
        elif action == "reclassify":
            payload = {
                "message_ids": payload.get("message_ids", []),
                "file_name": payload.get("file_name", ""),
                "caption": payload.get("caption", ""),
            }
            await store.update_workflow(workflow_id, payload=payload, status="open")
            workflow["payload"] = payload
            await self._ask_personal(chat_id, workflow)
        else:
            logger.warning(f"Unknown workflow action: {action}")

    # ------------------------------------------------------------------
    # Text-reply handling
    # ------------------------------------------------------------------

    async def _on_text_reply(self, message: Message):
        if not message.reply_to_message_id:
            return
        workflow = await store.get_open_workflow_by_prompt(message.chat.id, message.reply_to_message_id)
        if not workflow or workflow.get("status") != "open":
            return

        step = workflow.get("step")
        text = (message.text or "").strip()
        chat_id = message.chat.id
        payload = workflow.get("payload") or {}

        if step == "ask_personal_name":
            payload["declared_title"] = text
            await store.update_workflow(workflow["id"], payload=payload)
            workflow["payload"] = payload
            await self._ask_tags(chat_id, workflow)

        elif step == "ask_title":
            payload["declared_title"] = text
            await store.update_workflow(workflow["id"], payload=payload)
            workflow["payload"] = payload
            await self._search_and_confirm(chat_id, workflow)

        elif step == "ask_manual_tt":
            candidate = text.strip()
            if not _IMDB_RE.match(candidate):
                await message.reply_text(
                    "מזהה לא תקין. שלח בפורמט tt1234567 (למשל tt15398776)."
                )
                return
            imdb_id = candidate.lower()
            payload["imdb_id"] = imdb_id
            try:
                cine = await get_metadata_from_cinemeta(payload.get("stremio_type", "movie"), imdb_id)
                if cine.get("name"):
                    payload["official_title"] = cine["name"]
            except Exception:
                pass
            await store.update_workflow(workflow["id"], payload=payload)
            workflow["payload"] = payload
            await self._after_imdb_resolved(chat_id, workflow)

        elif step == "ask_season_episode":
            m = re.search(r"\bs\s*(\d{1,2})\s*e\s*(\d{1,3})\b", text, re.IGNORECASE)
            if not m:
                m2 = re.search(r"(\d{1,2})\s*[xX]\s*(\d{1,3})", text)
                if m2:
                    season, episode = int(m2.group(1)), int(m2.group(2))
                else:
                    await message.reply_text(
                        "לא הצלחתי להבין. שלח בפורמט S01E02 או 1x02."
                    )
                    return
            else:
                season, episode = int(m.group(1)), int(m.group(2))
            payload["season"] = season
            payload["episode"] = episode
            await store.update_workflow(workflow["id"], payload=payload)
            workflow["payload"] = payload
            await self._ask_tags(chat_id, workflow)

        elif step in ("ask_tags", "edit_tags"):
            tags = []
            if text.lower() not in _SKIP_WORDS:
                tags = [t.strip() for t in re.split(r"[,\s]+", text) if t.strip()]
            payload["tags"] = tags
            await store.update_workflow(workflow["id"], payload=payload)
            workflow["payload"] = payload
            await self._finalize(chat_id, workflow)

    # ------------------------------------------------------------------
    # Step senders
    # ------------------------------------------------------------------

    async def _ask_personal(self, chat_id: int, workflow: dict):
        file_name = (workflow.get("payload") or {}).get("file_name", "")
        prompt = await self.bot.send_message(
            chat_id,
            f"סיווג מחדש עבור: `{file_name}`\n\nהאם זה סרטון אישי?",
            reply_markup=_kb(
                [[("כן, אישי", _cb(workflow["id"], "py")), ("לא, חפש תוכן", _cb(workflow["id"], "pn"))]]
            ),
        )
        await store.update_workflow(workflow["id"], step="ask_personal", last_prompt_message_id=prompt.id)

    async def _ask_personal_name(self, chat_id: int, workflow: dict):
        prompt = await self.bot.send_message(chat_id, "איך להציג את הסרטון?")
        await store.update_workflow(
            workflow["id"],
            step="ask_personal_name",
            last_prompt_message_id=prompt.id,
            payload={**(workflow.get("payload") or {}), "classification": "personal"},
        )

    async def _ask_category(self, chat_id: int, workflow: dict):
        prompt = await self.bot.send_message(
            chat_id,
            "איזה סוג תוכן זה?",
            reply_markup=_kb(
                [
                    [
                        ("סרט", _cb(workflow["id"], "cm")),
                        ("סדרה", _cb(workflow["id"], "cs")),
                        ("אנימה", _cb(workflow["id"], "ca")),
                    ]
                ]
            ),
        )
        await store.update_workflow(workflow["id"], step="ask_category", last_prompt_message_id=prompt.id)

    async def _ask_anime_subtype(self, chat_id: int, workflow: dict):
        prompt = await self.bot.send_message(
            chat_id,
            "אנימה - סרט או סדרה?",
            reply_markup=_kb([[("סרט", _cb(workflow["id"], "am")), ("סדרה", _cb(workflow["id"], "as"))]]),
        )
        await store.update_workflow(workflow["id"], step="ask_anime_subtype", last_prompt_message_id=prompt.id)

    async def _ask_title(self, chat_id: int, workflow: dict):
        payload = workflow.get("payload") or {}
        detected = payload.get("file_name") or payload.get("caption") or ""
        prompt = await self.bot.send_message(
            chat_id,
            f"מה שם הסרט/הסדרה?\nהשם שזוהה מהקובץ: `{detected}`\n"
            f"שלח שם, או שלח \"אישור\" כדי להשתמש בשם שזוהה.",
        )
        await store.update_workflow(workflow["id"], step="ask_title", last_prompt_message_id=prompt.id)

    async def _search_and_confirm(self, chat_id: int, workflow: dict):
        payload = workflow.get("payload") or {}
        title = payload.get("declared_title") or ""
        if title.strip().lower() in {w.lower() for w in _CONFIRM_WORDS}:
            title = payload.get("file_name") or payload.get("caption") or title
            payload["declared_title"] = title
            await store.update_workflow(workflow["id"], payload=payload)
            workflow["payload"] = payload

        stremio_type = payload.get("stremio_type", "movie")
        media_type = "tv" if stremio_type == "series" else "movie"

        candidates = []
        if tmdb_client.is_configured():
            candidates = await tmdb_client.search(title, media_type=media_type)

        if not candidates:
            await self._ask_no_candidate(chat_id, workflow)
            return

        top = candidates[0]
        imdb_id = await tmdb_client.resolve_candidate_imdb(top)

        payload["tmdb_candidate"] = top.to_dict()
        payload["tmdb_candidate_imdb"] = imdb_id
        await store.update_workflow(workflow["id"], payload=payload)
        workflow["payload"] = payload

        year_str = f" ({top.year})" if top.year else ""
        imdb_line = f"IMDb: `{imdb_id}`" if imdb_id else "IMDb: לא נמצא"
        prompt = await self.bot.send_message(
            chat_id,
            f"מצאתי:\n*{top.title}*{year_str}\n{top.original_title}\n{imdb_line}\n\nהאם זה נכון?",
            reply_markup=_kb(
                [
                    [("✅ אישור", _cb(workflow["id"], "tc_approve"))],
                    [("🔢 יש לי tt אחר", _cb(workflow["id"], "tc_manual")), ("➡️ המשך עם השם", _cb(workflow["id"], "tc_name"))],
                ]
            ),
        )
        await store.update_workflow(workflow["id"], step="ask_tmdb_confirm", last_prompt_message_id=prompt.id)

    async def _ask_no_candidate(self, chat_id: int, workflow: dict):
        prompt = await self.bot.send_message(
            chat_id,
            "לא נמצאה התאמה אוטומטית. יש לך מזהה IMDb (tt...)?",
            reply_markup=_kb(
                [[("🔢 יש לי tt", _cb(workflow["id"], "mt_from_confirm")), ("➡️ המשך עם השם", _cb(workflow["id"], "un"))]]
            ),
        )
        await store.update_workflow(workflow["id"], step="ask_tmdb_confirm", last_prompt_message_id=prompt.id)

    async def _apply_tmdb_candidate(self, chat_id: int, workflow: dict):
        payload = workflow.get("payload") or {}
        candidate = payload.get("tmdb_candidate") or {}
        payload["official_title"] = candidate.get("title")
        payload["tmdb_id"] = candidate.get("tmdb_id")
        payload["imdb_id"] = payload.get("tmdb_candidate_imdb")
        await store.update_workflow(workflow["id"], payload=payload)
        workflow["payload"] = payload
        await self._after_imdb_resolved(chat_id, workflow)

    async def _continue_with_declared_name(self, chat_id: int, workflow: dict):
        payload = workflow.get("payload") or {}
        payload["official_title"] = None
        payload["imdb_id"] = None
        payload["tmdb_id"] = None
        await store.update_workflow(workflow["id"], payload=payload)
        workflow["payload"] = payload
        await self._after_imdb_resolved(chat_id, workflow)

    async def _ask_manual_tt(self, chat_id: int, workflow: dict):
        prompt = await self.bot.send_message(chat_id, "שלח מזהה IMDb בפורמט tt1234567")
        await store.update_workflow(workflow["id"], step="ask_manual_tt", last_prompt_message_id=prompt.id)

    async def _after_imdb_resolved(self, chat_id: int, workflow: dict):
        payload = workflow.get("payload") or {}
        stremio_type = payload.get("stremio_type")
        needs_se = stremio_type == "series" and payload.get("season") is None
        if needs_se:
            filename = payload.get("file_name") or payload.get("caption") or ""
            season, episode = parse_season_episode(filename)
            if season is not None and episode is not None:
                payload["season"] = season
                payload["episode"] = episode
                await store.update_workflow(workflow["id"], payload=payload)
                workflow["payload"] = payload
                await self._ask_tags(chat_id, workflow)
                return
            prompt = await self.bot.send_message(chat_id, "מה העונה והפרק? (לדוגמה: S01E02)")
            await store.update_workflow(workflow["id"], step="ask_season_episode", last_prompt_message_id=prompt.id)
            return
        await self._ask_tags(chat_id, workflow)

    async def _ask_tags(self, chat_id: int, workflow: dict, editing: bool = False):
        prompt = await self.bot.send_message(
            chat_id, "תגים אופציונליים (איכות/שפה)? למשל: 1080p, דיבוב עברית\nאו שלח \"דלג\"."
        )
        step = "edit_tags" if editing else "ask_tags"
        # Reopen the workflow in case it was already marked "done" (edit flow).
        await store.update_workflow(
            workflow["id"], step=step, last_prompt_message_id=prompt.id, status="open"
        )

    async def _finalize(self, chat_id: int, workflow: dict):
        payload = workflow.get("payload") or {}
        message_ids = payload.get("message_ids") or []
        file_name = payload.get("file_name")
        tags = payload.get("tags") or []
        declared_title = payload.get("declared_title") or file_name or "קובץ"
        resolution = parse_video_resolution(f"{' '.join(tags)} {file_name or ''}")
        if resolution == "Unknown":
            resolution = None

        classification = payload.get("classification", "personal")
        stremio_type = payload.get("stremio_type")

        mapping = await store.upsert_media_mapping(
            source_chat_id=chat_id,
            message_ids=message_ids,
            classification=classification,
            declared_title=declared_title,
            stremio_type=stremio_type,
            imdb_id=payload.get("imdb_id"),
            tmdb_id=payload.get("tmdb_id"),
            official_title=payload.get("official_title"),
            season=payload.get("season"),
            episode=payload.get("episode"),
            tags=tags,
            resolution=resolution,
            file_name=file_name,
            created_by=workflow.get("created_by"),
        )

        await store.mark_workflow_status(workflow["id"], "done")

        if not mapping:
            await self.bot.send_message(chat_id, "❌ שמירה נכשלה, נסה שוב.")
            return

        title_line = payload.get("official_title") or declared_title
        summary_lines = [f"✅ נשמר בהצלחה: *{title_line}*"]
        if payload.get("imdb_id"):
            summary_lines.append(f"IMDb: `{payload['imdb_id']}`")
        if tags:
            summary_lines.append(f"תגים: {', '.join(tags)}")
        if classification == "personal":
            summary_lines.append("קטגוריה: Personal Telegram")

        await self.bot.send_message(
            chat_id,
            "\n".join(summary_lines),
            reply_markup=_kb(
                [[("✏️ ערוך תגים", _cb(workflow["id"], "edit_tags")), ("🔄 קטגוריה חדשה", _cb(workflow["id"], "reclassify"))]]
            ),
        )


admin_workflow_manager = AdminWorkflowManager()
