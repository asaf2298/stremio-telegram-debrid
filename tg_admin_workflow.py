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

import asyncio
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
import torbox_client
from utils import parse_season_episode, get_metadata_from_cinemeta
from search_utils import parse_video_resolution

logger = logging.getLogger("tg_admin_workflow")

# Find tt##### anywhere in free text / IMDb URLs (not only exact full-string match).
_IMDB_FIND_RE = re.compile(r"tt\d{5,9}", re.IGNORECASE)
_SKIP_WORDS = {"דלג", "-", "skip", "none"}
_CONFIRM_WORDS = {"אישור", "אשר", "ok", "כן"}

# Hard cap for TMDb pick-list buttons (Telegram keyboards get unwieldy past this).
_TMDB_CHOICE_LIMIT = 15
_BUTTON_LABEL_MAX = 60
_TORBOX_POLL_SECONDS = 10
_TORBOX_POLL_MAX_MINUTES = 180
_REPLY_HINT = "\n\nיש לצוטט את התשובה"


def _stremio_type_hebrew(stremio_type: str) -> str:
    return "סדרה" if stremio_type == "series" else "סרט"


def extract_imdb_id(text: str) -> str | None:
    """Extract the first IMDb tt id from free text or a full IMDb URL."""
    if not text:
        return None
    m = _IMDB_FIND_RE.search(text)
    return m.group(0).lower() if m else None


def _kb(rows: list) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(text, callback_data=cb) for text, cb in row] for row in rows]
    )


def _cb(workflow_id: str, action: str) -> str:
    return f"{workflow_id}|{action}"


def _candidate_button_label(candidate: dict) -> str:
    title = candidate.get("title") or candidate.get("original_title") or "ללא שם"
    year = candidate.get("year")
    label = f"{title} ({year})" if year else str(title)
    if len(label) > _BUTTON_LABEL_MAX:
        return label[: _BUTTON_LABEL_MAX - 3] + "..."
    return label


def _remember_cleanup_id(workflow: dict, message_id: int) -> None:
    """Append a group message id to the workflow's deletable noise list."""
    if not message_id:
        return
    payload = workflow.get("payload")
    if payload is None:
        workflow["payload"] = payload = {}
    ids = payload.get("cleanup_message_ids")
    if not isinstance(ids, list):
        ids = []
    mid = int(message_id)
    if mid not in ids:
        ids.append(mid)
    payload["cleanup_message_ids"] = ids


class AdminWorkflowManager:
    def __init__(self):
        self.bot: Client = None
        self.is_running = False
        self._torbox_cancel: dict[str, asyncio.Event] = {}

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

        if Config.torbox_enabled():
            @self.bot.on_message(filters.chat(group_id) & filters.text & ~filters.reply)
            async def on_link(client, message: Message):
                if not message.text or message.text.startswith("/"):
                    return
                url, magnet = torbox_client.parse_link_text(message.text)
                if not url and not magnet:
                    return
                try:
                    await self._on_torbox_link(message, url, magnet)
                except Exception as e:
                    logger.error(f"Error handling TorBox link: {e}")

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

    async def _persist_workflow_payload(self, workflow: dict) -> None:
        payload = workflow.get("payload")
        if payload is None:
            return
        await store.update_workflow(workflow["id"], payload=payload)

    async def _track_bot_message(self, workflow: dict, message, *, persist: bool = True) -> int | None:
        if not message:
            return None
        mid = getattr(message, "id", None)
        _remember_cleanup_id(workflow, mid)
        if persist:
            await self._persist_workflow_payload(workflow)
        return mid

    async def _track_user_reply(self, workflow: dict, message: Message) -> None:
        _remember_cleanup_id(workflow, message.id)
        await self._persist_workflow_payload(workflow)

    async def _cleanup_workflow_chat(
        self, chat_id: int, workflow: dict, *, keep_message_ids: set[int] | None = None
    ) -> None:
        """Delete workflow Q&A noise for all group members. Keeps source file/link + success."""
        keep = {int(x) for x in (keep_message_ids or set()) if x}
        source_id = workflow.get("source_message_id")
        if source_id:
            keep.add(int(source_id))

        payload = workflow.get("payload") or {}
        to_delete = [
            int(mid)
            for mid in (payload.get("cleanup_message_ids") or [])
            if mid and int(mid) not in keep
        ]
        if not to_delete:
            return

        for i in range(0, len(to_delete), 100):
            batch = to_delete[i : i + 100]
            try:
                await self.bot.delete_messages(chat_id, batch)
            except Exception:
                for mid in batch:
                    try:
                        await self.bot.delete_messages(chat_id, mid)
                    except Exception as e:
                        logger.debug("Could not delete workflow message %s: %s", mid, e)

    async def _set_workflow_prompt(
        self,
        workflow: dict,
        *,
        step: str,
        message_id: int,
        payload_patch: dict = None,
        status: str = None,
    ) -> None:
        payload = dict(workflow.get("payload") or {})
        if payload_patch:
            payload.update(payload_patch)
        workflow["payload"] = payload
        _remember_cleanup_id(workflow, message_id)
        fields = {
            "step": step,
            "last_prompt_message_id": message_id,
            "payload": workflow["payload"],
        }
        if status:
            fields["status"] = status
        await store.update_workflow(workflow["id"], **fields)

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
            "cleanup_message_ids": [],
        }
        await store.update_workflow(workflow["id"], payload=payload)
        workflow["payload"] = payload

        prompt = await message.reply_text(
            f"התקבל קובץ חדש: `{file_name}`\n\nהאם זה סרטון אישי?",
            reply_markup=_kb(
                [
                    [("כן, אישי", _cb(workflow["id"], "py")), ("לא, חפש תוכן", _cb(workflow["id"], "pn"))],
                ]
            ),
        )
        await self._set_workflow_prompt(workflow, step="ask_personal", message_id=prompt.id)

    # ------------------------------------------------------------------
    # TorBox link entry (API only — no TorBox Telegram bot parsing)
    # ------------------------------------------------------------------

    async def _on_torbox_link(self, message: Message, url: str = None, magnet: str = None):
        chat_id = message.chat.id
        if await store.has_open_torbox_wait(chat_id):
            await message.reply_text("⏳ כבר רץ תהליך TorBox פתוח בקבוצה. המתן או בטל אותו.")
            return

        created_by = message.from_user.id if message.from_user else None
        kind = "torrent" if magnet else "webdl"

        if url:
            original_url = url.strip()
            url = torbox_client.normalize_web_download_url(url)
            if url != original_url:
                await message.reply_text(
                    f"הקישור הומר לפורמט Google Drive ל־TorBox:\n`{url}`"
                )

        existing = None
        if url:
            existing = await torbox_client.find_web_download_by_link(url)
        elif magnet:
            existing = await torbox_client.find_torrent_by_magnet(magnet)

        if existing:
            pkg_id, file_id = torbox_client.item_ids(existing, kind)
            mapping = await store.get_mapping_by_torbox(kind, pkg_id, file_id)
            if mapping:
                title = mapping.get("official_title") or mapping.get("declared_title") or mapping.get("file_name")
                imdb = mapping.get("imdb_id") or "ללא tt"
                await message.reply_text(
                    f"ℹ️ הקובץ כבר במסד הנתונים:\n*{title}*\nIMDb: `{imdb}`"
                )
                return
            await self._start_torbox_workflow(
                message, existing, kind, url=url, magnet=magnet, created_by=created_by
            )
            return

        created = None
        if url:
            created = await torbox_client.create_web_download(url)
        else:
            created = await torbox_client.create_torrent(magnet)

        if not created:
            await message.reply_text("❌ לא הצלחתי להוסיף את ההורדה ל-TorBox.")
            return

        new_id = None
        if kind == "webdl":
            new_id = created.get("webdownload_id") or created.get("id")
        else:
            new_id = created.get("torrent_id") or created.get("id")
        try:
            new_id = int(new_id) if new_id is not None else None
        except (TypeError, ValueError):
            new_id = None

        item = None
        if new_id:
            if kind == "webdl":
                item = await torbox_client.get_web_download(new_id)
            else:
                item = await torbox_client.get_torrent(new_id)
        if not item:
            if url:
                item = await torbox_client.find_web_download_by_link(url)
            elif magnet:
                item = await torbox_client.find_torrent_by_magnet(magnet)
        if not item:
            item = {"id": new_id, "name": torbox_client.filename_from_url(url) if url else torbox_client.filename_from_magnet(magnet)}

        await self._start_torbox_workflow(
            message, item, kind, url=url, magnet=magnet, created_by=created_by
        )

    async def _start_torbox_workflow(
        self,
        message: Message,
        item: dict,
        kind: str,
        *,
        url: str = None,
        magnet: str = None,
        created_by: int = None,
    ):
        chat_id = message.chat.id
        pkg_id, file_id = torbox_client.item_ids(item, kind)
        file_name = torbox_client.item_display_name(item)
        link_hash = torbox_client.web_link_hash(url) if url else torbox_client.magnet_info_hash(magnet or "")

        workflow = await store.create_workflow(
            chat_id=chat_id,
            source_message_id=message.id,
            created_by=created_by,
        )
        if not workflow:
            await message.reply_text("❌ לא הצלחתי לפתוח תהליך עבודה.")
            return

        payload = {
            "source": "torbox",
            "torbox_kind": kind,
            "torbox_id": pkg_id,
            "torbox_file_id": file_id,
            "torbox_hash": link_hash,
            "torbox_status": "ready" if torbox_client.is_ready(item) else "pending",
            "file_name": file_name,
            "message_ids": [],
            "caption": "",
            "original_url": url or magnet or "",
            "cleanup_message_ids": [],
        }
        await store.update_workflow(workflow["id"], payload=payload)
        workflow["payload"] = payload

        if torbox_client.is_ready(item):
            note = await message.reply_text(f"✅ נמצא ב-TorBox: `{file_name}`\nמתחיל שאלות זיהוי…")
            await self._track_bot_message(workflow, note)
            await self._ask_personal(chat_id, workflow)
        else:
            await self._begin_torbox_wait(chat_id, workflow, item)

    async def _begin_torbox_wait(self, chat_id: int, workflow: dict, item: dict):
        payload = workflow.get("payload") or {}
        progress = int((item or {}).get("progress") or 0)
        if progress <= 1:
            progress_pct = int(progress * 100)
        else:
            progress_pct = int(progress)
        status = await self.bot.send_message(
            chat_id,
            f"⏳ ממתין ל-TorBox… {progress_pct}%\n`{payload.get('file_name', '')}`",
            reply_markup=_kb([[("❌ ביטול", _cb(workflow["id"], "tb_cancel"))]]),
        )
        payload["torbox_status_message_id"] = status.id
        await self._set_workflow_prompt(
            workflow,
            step="torbox_wait",
            message_id=status.id,
            payload_patch=payload,
        )
        cancel = asyncio.Event()
        self._torbox_cancel[workflow["id"]] = cancel
        asyncio.create_task(self._torbox_poll_loop(workflow["id"]))

    async def _torbox_poll_loop(self, workflow_id: str):
        cancel = self._torbox_cancel.get(workflow_id)
        if not cancel:
            cancel = asyncio.Event()
            self._torbox_cancel[workflow_id] = cancel
        iterations = int((_TORBOX_POLL_MAX_MINUTES * 60) / _TORBOX_POLL_SECONDS)
        try:
            for _ in range(iterations):
                if cancel.is_set():
                    return
                workflow = await store.get_workflow(workflow_id)
                if not workflow or workflow.get("status") != "open":
                    return
                if workflow.get("step") != "torbox_wait":
                    return
                payload = workflow.get("payload") or {}
                kind = payload.get("torbox_kind")
                torbox_id = payload.get("torbox_id")
                if not kind or not torbox_id:
                    return
                if kind == "webdl":
                    item = await torbox_client.get_web_download(int(torbox_id))
                else:
                    item = await torbox_client.get_torrent(int(torbox_id))
                if not item:
                    await asyncio.sleep(_TORBOX_POLL_SECONDS)
                    continue
                pkg_id, file_id = torbox_client.item_ids(item, kind)
                payload["torbox_id"] = pkg_id
                payload["torbox_file_id"] = file_id
                payload["file_name"] = torbox_client.item_display_name(item)
                await store.update_workflow(workflow_id, payload=payload)
                await self._refresh_torbox_wait_message(workflow, item)
                if torbox_client.is_ready(item):
                    payload["torbox_status"] = "ready"
                    await store.update_workflow(workflow_id, payload=payload, step="ask_personal")
                    workflow = await store.get_workflow(workflow_id) or workflow
                    workflow["payload"] = payload
                    ready_note = await self.bot.send_message(
                        workflow["chat_id"],
                        f"✅ ההורדה ב-TorBox מוכנה: `{payload['file_name']}`\nמתחיל שאלות זיהוי…",
                    )
                    await self._track_bot_message(workflow, ready_note)
                    await self._ask_personal(workflow["chat_id"], workflow)
                    return
                if (item.get("error") or "").strip():
                    await self.bot.send_message(
                        workflow["chat_id"],
                        f"❌ שגיאה ב-TorBox: {item.get('error')}",
                    )
                    await store.mark_workflow_status(workflow_id, "cancelled")
                    return
                await asyncio.sleep(_TORBOX_POLL_SECONDS)
            workflow = await store.get_workflow(workflow_id)
            if workflow and workflow.get("status") == "open":
                await self.bot.send_message(
                    workflow["chat_id"],
                    "⏱️ זמן ההמתנה ל-TorBox נגמר. נסה שוב מאוחר יותר.",
                )
                await store.mark_workflow_status(workflow_id, "cancelled")
        finally:
            self._torbox_cancel.pop(workflow_id, None)

    async def _refresh_torbox_wait_message(self, workflow: dict, item: dict):
        payload = workflow.get("payload") or {}
        msg_id = payload.get("torbox_status_message_id")
        if not msg_id:
            return
        progress = int(item.get("progress") or 0)
        progress_pct = int(progress * 100) if progress <= 1 else int(progress)
        state = item.get("download_state") or "…"
        try:
            await self.bot.edit_message_text(
                workflow["chat_id"],
                msg_id,
                f"⏳ ממתין ל-TorBox… {progress_pct}% ({state})\n`{payload.get('file_name', '')}`",
                reply_markup=_kb([[("❌ ביטול", _cb(workflow["id"], "tb_cancel"))]]),
            )
        except Exception:
            pass

    async def _cancel_torbox_wait(self, workflow: dict):
        wf_id = workflow["id"]
        cancel = self._torbox_cancel.get(wf_id)
        if cancel:
            cancel.set()
        self._torbox_cancel.pop(wf_id, None)
        payload = workflow.get("payload") or {}
        msg_id = payload.get("torbox_status_message_id")
        if msg_id:
            try:
                await self.bot.edit_message_text(
                    workflow["chat_id"],
                    msg_id,
                    "❌ בוטל. ההורדה ב-TorBox ממשיכה ברקע.",
                )
            except Exception:
                pass
        await store.mark_workflow_status(wf_id, "cancelled")

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

        if workflow.get("step") == "torbox_wait":
            if action == "tb_cancel":
                await cq.answer()
                await self._cancel_torbox_wait(workflow)
            else:
                await cq.answer("ממתין ל-TorBox…", show_alert=True)
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
        elif action == "tc_approve" or action == "im_ok":
            await self._apply_tmdb_candidate(chat_id, workflow)
        elif action == "im_retry":
            await self._show_tmdb_candidate_list(chat_id, workflow)
        elif action == "ta":
            payload["declared_title"] = (
                payload.get("file_name") or payload.get("caption") or payload.get("declared_title") or ""
            )
            await store.update_workflow(workflow_id, payload=payload)
            workflow["payload"] = payload
            await self._search_and_confirm(chat_id, workflow)
        elif action == "tn":
            await self._ask_title_other(chat_id, workflow)
        elif action.startswith("tp_"):
            try:
                idx = int(action[3:])
            except ValueError:
                logger.warning(f"Invalid TMDb pick action: {action}")
                return
            await self._select_tmdb_candidate(chat_id, workflow, idx)
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
                "cleanup_message_ids": payload.get("cleanup_message_ids") or [],
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

        await self._track_user_reply(workflow, message)

        if step == "torbox_wait":
            hint = await message.reply_text(
                "⏳ עדיין ממתין ל-TorBox. אפשר לבטל עם הכפתור בהודעת ההמתנה."
            )
            await self._track_bot_message(workflow, hint)
            return

        if step == "ask_personal_name":
            payload["declared_title"] = text
            await store.update_workflow(workflow["id"], payload=payload)
            workflow["payload"] = payload
            await self._ask_tags(chat_id, workflow)

        elif step == "ask_title_other":
            payload["declared_title"] = text
            await store.update_workflow(workflow["id"], payload=payload)
            workflow["payload"] = payload
            await self._search_and_confirm(chat_id, workflow)

        elif step == "ask_title":
            hint = await message.reply_text("השתמש בכפתורים בהודעה למעלה: אישור או שם אחר.")
            await self._track_bot_message(workflow, hint)
            return

        elif step == "ask_manual_tt":
            imdb_id = extract_imdb_id(text)
            if not imdb_id:
                err = await message.reply_text(
                    "לא מצאתי מזהה tt. שלח tt1234567 או קישור IMDb מלא "
                    "(למשל https://www.imdb.com/title/tt15398776/)."
                )
                await self._track_bot_message(workflow, err)
                return
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
                    err = await message.reply_text(
                        "לא הצלחתי להבין. שלח בפורמט S01E02 או 1x02."
                    )
                    await self._track_bot_message(workflow, err)
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
        await self._set_workflow_prompt(workflow, step="ask_personal", message_id=prompt.id)

    async def _ask_personal_name(self, chat_id: int, workflow: dict):
        prompt = await self.bot.send_message(
            chat_id, f"איך להציג את הסרטון?{_REPLY_HINT}"
        )
        await self._set_workflow_prompt(
            workflow,
            step="ask_personal_name",
            message_id=prompt.id,
            payload_patch={**(workflow.get("payload") or {}), "classification": "personal"},
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
        await self._set_workflow_prompt(workflow, step="ask_category", message_id=prompt.id)

    async def _ask_anime_subtype(self, chat_id: int, workflow: dict):
        prompt = await self.bot.send_message(
            chat_id,
            "אנימה - סרט או סדרה?",
            reply_markup=_kb([[("סרט", _cb(workflow["id"], "am")), ("סדרה", _cb(workflow["id"], "as"))]]),
        )
        await self._set_workflow_prompt(workflow, step="ask_anime_subtype", message_id=prompt.id)

    async def _ask_title(self, chat_id: int, workflow: dict):
        payload = workflow.get("payload") or {}
        detected = payload.get("file_name") or payload.get("caption") or ""
        prompt = await self.bot.send_message(
            chat_id,
            f"מה שם הסרט/הסדרה?\n"
            f"השם שזוהה מהקובץ: {detected}\n"
            f"האם לחפש את שם הסרט ב imdb או שתרצה שאחפש שם אחר?",
            reply_markup=_kb(
                [[("אישור", _cb(workflow["id"], "ta")), ("שם אחר", _cb(workflow["id"], "tn"))]]
            ),
        )
        await self._set_workflow_prompt(workflow, step="ask_title", message_id=prompt.id)

    async def _ask_title_other(self, chat_id: int, workflow: dict):
        prompt = await self.bot.send_message(
            chat_id, f"מה שם הסרט/הסדרה לחיפוש?{_REPLY_HINT}"
        )
        await self._set_workflow_prompt(workflow, step="ask_title_other", message_id=prompt.id)

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
            candidates = await tmdb_client.search(
                title, media_type=media_type, limit=_TMDB_CHOICE_LIMIT
            )

        if not candidates:
            await self._ask_no_candidate(chat_id, workflow)
            return

        payload["tmdb_candidates"] = [c.to_dict() for c in candidates]
        # Clear legacy single-candidate fields from older flow versions.
        payload.pop("tmdb_candidate", None)
        payload.pop("tmdb_candidate_imdb", None)
        await store.update_workflow(workflow["id"], payload=payload)
        workflow["payload"] = payload
        await self._show_tmdb_candidate_list(chat_id, workflow)

    async def _show_tmdb_candidate_list(self, chat_id: int, workflow: dict):
        payload = workflow.get("payload") or {}
        candidates = payload.get("tmdb_candidates") or []
        if not candidates:
            await self._ask_no_candidate(chat_id, workflow)
            return

        rows = [
            [(_candidate_button_label(d), _cb(workflow["id"], f"tp_{i}"))]
            for i, d in enumerate(candidates)
        ]
        rows.append(
            [("🔢 להזין קישור / tt מ־IMDb", _cb(workflow["id"], "tc_manual"))]
        )

        prompt = await self.bot.send_message(
            chat_id,
            f"נמצאו {len(candidates)} התאמות — בחר אחת:\n"
            f"(או הזן קישור / מזהה IMDb אם אף אחת לא מתאימה)",
            reply_markup=_kb(rows),
        )
        await self._set_workflow_prompt(workflow, step="ask_tmdb_confirm", message_id=prompt.id)

    async def _ask_no_candidate(self, chat_id: int, workflow: dict):
        prompt = await self.bot.send_message(
            chat_id,
            "לא נמצאה התאמה אוטומטית. יש לך קישור או מזהה IMDb (tt...)?",
            reply_markup=_kb(
                [[
                    ("🔢 להזין קישור / tt מ־IMDb", _cb(workflow["id"], "mt_from_confirm")),
                    ("➡️ המשך ללא מזהה", _cb(workflow["id"], "un")),
                ]]
            ),
        )
        await self._set_workflow_prompt(workflow, step="ask_tmdb_confirm", message_id=prompt.id)

    async def _select_tmdb_candidate(self, chat_id: int, workflow: dict, index: int):
        payload = workflow.get("payload") or {}
        candidates = payload.get("tmdb_candidates") or []
        if index < 0 or index >= len(candidates):
            err = await self.bot.send_message(chat_id, "הבחירה לא תקפה. נסה שוב מהרשימה.")
            await self._track_bot_message(workflow, err)
            return

        candidate = candidates[index]
        imdb_id = None
        try:
            ids = await tmdb_client.get_external_ids(
                candidate.get("tmdb_id"), candidate.get("media_type")
            )
            if ids:
                imdb_id = ids.get("imdb_id")
        except Exception as e:
            logger.warning(f"Failed resolving IMDb for TMDb pick {index}: {e}")

        payload["tmdb_candidate"] = candidate
        payload["tmdb_candidate_imdb"] = imdb_id
        payload["official_title"] = candidate.get("title")
        payload["tmdb_id"] = candidate.get("tmdb_id")
        payload["imdb_id"] = imdb_id
        await store.update_workflow(workflow["id"], payload=payload)
        workflow["payload"] = payload
        await self._confirm_imdb_pick(chat_id, workflow)

    async def _confirm_imdb_pick(self, chat_id: int, workflow: dict):
        payload = workflow.get("payload") or {}
        title = payload.get("official_title") or payload.get("declared_title") or "ללא שם"
        imdb_id = payload.get("imdb_id")
        if imdb_id:
            link_line = f"https://www.imdb.com/title/{imdb_id}/"
        else:
            link_line = "לא נמצא מזהה IMDb"
        prompt = await self.bot.send_message(
            chat_id,
            f"נבחר: *{title}*\nIMDb: {link_line}",
            reply_markup=_kb(
                [[
                    ("בדיוק", _cb(workflow["id"], "im_ok")),
                    ("אחד אחר", _cb(workflow["id"], "im_retry")),
                ]]
            ),
        )
        await self._set_workflow_prompt(workflow, step="confirm_imdb", message_id=prompt.id)

    async def _apply_tmdb_candidate(self, chat_id: int, workflow: dict):
        payload = workflow.get("payload") or {}
        candidate = payload.get("tmdb_candidate") or {}
        if not candidate and payload.get("tmdb_candidates"):
            # Legacy approve with only a list stored — take the first entry.
            await self._select_tmdb_candidate(chat_id, workflow, 0)
            return
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
        payload["classification"] = "personal"
        stremio_type = payload.get("stremio_type", "movie")
        type_label = _stremio_type_hebrew(stremio_type)
        await store.update_workflow(workflow["id"], payload=payload)
        workflow["payload"] = payload
        await store.update_workflow(workflow["id"], payload=payload)
        workflow["payload"] = payload
        note = await self.bot.send_message(
            chat_id,
            f"סווג כ־Personal (יופיע בקטלוג Personal Telegram).\n"
            f"סוג שנבחר: {type_label}.",
        )
        await self._track_bot_message(workflow, note)
        await self._after_imdb_resolved(chat_id, workflow)

    async def _ask_manual_tt(self, chat_id: int, workflow: dict):
        prompt = await self.bot.send_message(
            chat_id,
            "שלח קישור IMDb או מזהה tt "
            "(למשל `tt15398776` או https://www.imdb.com/title/tt15398776/)."
            f"{_REPLY_HINT}",
        )
        await self._set_workflow_prompt(workflow, step="ask_manual_tt", message_id=prompt.id)

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
            prompt = await self.bot.send_message(
                chat_id, f"מה העונה והפרק? (לדוגמה: S01E02){_REPLY_HINT}"
            )
            await self._set_workflow_prompt(workflow, step="ask_season_episode", message_id=prompt.id)
            return
        await self._ask_tags(chat_id, workflow)

    async def _ask_tags(self, chat_id: int, workflow: dict, editing: bool = False):
        prompt = await self.bot.send_message(
            chat_id,
            f"תגים אופציונליים (איכות/שפה)? למשל: 1080p, דיבוב עברית\nאו שלח \"דלג\".{_REPLY_HINT}",
        )
        step = "edit_tags" if editing else "ask_tags"
        await self._set_workflow_prompt(workflow, step=step, message_id=prompt.id, status="open")

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
            source=payload.get("source", "telegram"),
            torbox_kind=payload.get("torbox_kind"),
            torbox_id=payload.get("torbox_id"),
            torbox_file_id=payload.get("torbox_file_id"),
            torbox_hash=payload.get("torbox_hash"),
            torbox_status=payload.get("torbox_status"),
        )

        await store.mark_workflow_status(workflow["id"], "done")

        if not mapping:
            await self.bot.send_message(chat_id, "❌ שמירה נכשלה, נסה שוב.")
            return

        title_line = payload.get("official_title") or declared_title
        if tags:
            title_line = f"{title_line}\n" + " ".join(f"[{t}]" for t in tags)
        summary_lines = [f"✅ נשמר בהצלחה:\n*{title_line}*"]
        if payload.get("imdb_id"):
            summary_lines.append(f"IMDb: `{payload['imdb_id']}`")
        if tags:
            summary_lines.append(f"תגים: {', '.join(tags)}")
        if classification == "personal":
            summary_lines.append("קטגוריה: Personal Telegram")
            if stremio_type:
                summary_lines.append(f"סוג: {_stremio_type_hebrew(stremio_type)}")
        elif payload.get("source") == "torbox":
            summary_lines.append("מקור: TorBox")

        # Post-save edit buttons temporarily hidden: the workflow is marked
        # "done" before this message is sent, so callbacks were rejected as
        # expired ("השאלה הזו לא בתוקף יותר"). Handlers for edit_tags /
        # reclassify remain below for a later fix that reopens the workflow.
        # reply_markup=_kb(
        #     [[
        #         ("✏️ ערוך תגים", _cb(workflow["id"], "edit_tags")),
        #         ("🔄 קטגוריה חדשה", _cb(workflow["id"], "reclassify")),
        #     ]]
        # )
        success = await self.bot.send_message(chat_id, "\n".join(summary_lines))
        await self._cleanup_workflow_chat(chat_id, workflow, keep_message_ids={success.id})


admin_workflow_manager = AdminWorkflowManager()
