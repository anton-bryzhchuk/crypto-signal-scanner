import asyncio
import logging
from telethon import TelegramClient, events
from telethon.tl.types import User, Channel, Chat
from db import storage
from ai.filter import filter_message, add_negative_feedback
from config import TG_API_ID, TG_API_HASH, TG_PHONE

logger = logging.getLogger(__name__)


class TGReader:
    def __init__(self, on_alert_callback):
        self.client   = TelegramClient("session_reader", TG_API_ID, TG_API_HASH)
        self.on_alert = on_alert_callback
        self._priority_user_ids: set[int] = set()
        self._vip_user_ids: set[int] = set()
        self._vip_names: dict[int, str] = {}  # id → ім'я
        self._valid_chat_ids: list = []
        self._topic_filter: dict[int | str, set[int]] = {}  # chat_id → allowed topic IDs (empty = all)
        self._vip_topics: dict[int | str, set[int]] = {}    # chat_id → VIP topic IDs (bypass filter)

    async def start(self):
        await self.client.start(phone=TG_PHONE)
        await self._load_priority_users()
        await self._load_vip_users()
        await self._resolve_chats()
        self._load_vip_topics()
        await self._register_handlers()
        logger.info("TG Reader запущено")

    async def _resolve_chats(self):
        """Будуємо set chat_id і topic фільтри для швидкої перевірки в хендлері."""
        chats_db = storage.get_chats()
        if not chats_db:
            return

        for c in chats_db:
            raw = c["chat_id"].strip()
            topic_ids_str = c.get("topic_ids", "") or ""
            allowed_topics: set[int] = set()
            if topic_ids_str.strip():
                for t in topic_ids_str.split(","):
                    t = t.strip()
                    if t.isdigit():
                        allowed_topics.add(int(t))

            self._valid_chat_ids.append(raw)
            if raw.lstrip("-").isdigit():
                n = int(raw)
                self._valid_chat_ids.append(n)
                if raw.startswith("-100"):
                    canonical = int(raw[4:])
                    self._valid_chat_ids.append(canonical)
                    if allowed_topics:
                        # Зберігаємо всі варіанти ID щоб фільтр точно спрацював
                        self._topic_filter[n] = allowed_topics        # -1002279306987
                        self._topic_filter[canonical] = allowed_topics # 2279306987
                        self._topic_filter[raw] = allowed_topics       # "-1002279306987"
                elif n < 0:
                    self._valid_chat_ids.append(-n)

        logger.info(f"Моніторимо {len(chats_db)} чатів (all-messages mode)")

    async def _load_priority_users(self):
        users = storage.get_priority_users()
        for u in users:
            uid = u["user_id"]
            if uid.lstrip("-").isdigit():
                self._priority_user_ids.add(int(uid))
            else:
                try:
                    entity = await self.client.get_entity(uid)
                    self._priority_user_ids.add(entity.id)
                except Exception as e:
                    logger.warning(f"Не вдалось знайти {uid}: {e}")

    async def _load_vip_users(self):
        self._vip_user_ids.clear()
        self._vip_names.clear()
        for u in storage.get_vip_users():
            uid = u["user_id"]
            if uid.lstrip("-").isdigit():
                iid = int(uid)
                self._vip_user_ids.add(iid)
                self._vip_names[iid] = u.get("note", "VIP") or "VIP"
        logger.info(f"VIP users: {self._vip_names}")

    def _load_vip_topics(self):
        self._vip_topics.clear()
        for row in storage.get_vip_topics():
            cid = row["chat_id"].strip()
            tid = int(row["topic_id"])
            # Зберігаємо всі варіанти ID чату (як у _topic_filter)
            for key in self._chat_id_variants(cid):
                if key not in self._vip_topics:
                    self._vip_topics[key] = set()
                self._vip_topics[key].add(tid)
        logger.info(f"VIP topics: {self._vip_topics}")

    def _chat_id_variants(self, raw: str) -> list:
        """Повертає всі варіанти chat_id для зберігання в dict."""
        variants = [raw]
        if raw.lstrip("-").isdigit():
            n = int(raw)
            variants.append(n)
            if raw.startswith("-100"):
                variants.append(int(raw[4:]))
        return variants

    async def add_priority_user(self, user_id: str, username: str = "", note: str = ""):
        storage.add_priority_user(user_id, username, note)
        await self._load_priority_users()

    async def add_vip_user(self, user_id: str, note: str = ""):
        storage.add_vip_user(user_id, note)
        await self._load_vip_users()

    def _is_monitored(self, chat_id) -> bool:
        """Перевіряємо чи цей чат потрібно моніторити."""
        return chat_id in self._valid_chat_ids

    async def _register_handlers(self):
        if not self._valid_chat_ids:
            logger.warning("Список чатів порожній")

        # Слухаємо ВСІ повідомлення, фільтруємо в хендлері
        @self.client.on(events.NewMessage())
        async def handle_message(event):
            try:
                peer_id   = event.chat_id
                sender_id = event.message.sender_id
                is_vip    = sender_id in self._vip_user_ids if sender_id else False

                # Пропускаємо якщо чат не моніториться І відправник не VIP
                if not self._is_monitored(peer_id) and not is_vip:
                    return
                await self._process_message(event)
            except Exception as e:
                logger.error(f"handle_message error: {e}")

    def _is_vip_topic(self, chat_id, msg) -> bool:
        """Повертає True якщо повідомлення з VIP-топіку (форвардити без фільтрів)."""
        vip = self._vip_topics.get(chat_id)
        if not vip:
            return False
        reply_to = getattr(msg, "reply_to", None)
        if reply_to is None:
            return 1 in vip
        top_id = getattr(reply_to, "reply_to_top_id", None) or getattr(reply_to, "reply_to_msg_id", None)
        if top_id is None:
            return 1 in vip
        return top_id in vip

    def _is_allowed_topic(self, chat_id, msg) -> bool:
        """Якщо для чату задані топіки — пропускаємо тільки повідомлення з них."""
        allowed = self._topic_filter.get(chat_id)
        if not allowed:
            return True
        reply_to = getattr(msg, "reply_to", None)
        if reply_to is None:
            # Повідомлення без reply_to — це General (topic_id=1)
            return 1 in allowed
        top_id = getattr(reply_to, "reply_to_top_id", None) or getattr(reply_to, "reply_to_msg_id", None)
        if top_id is None:
            return 1 in allowed
        return top_id in allowed

    async def _process_message(self, event):
        msg  = event.message
        text = msg.text or ""
        if len(text) < 20:
            return

        peer_id   = event.chat_id
        sender_id = msg.sender_id
        is_vip      = sender_id in self._vip_user_ids if sender_id else False
        is_vip_topic = self._is_vip_topic(peer_id, msg)
        is_priority = (sender_id in self._priority_user_ids if sender_id else False) or is_vip

        # Topic-фільтр не застосовуємо до VIP — їх читаємо скрізь
        if not is_vip and not is_vip_topic and not self._is_allowed_topic(peer_id, msg):
            return

        if storage.is_seen(text):
            return
        storage.mark_seen(text)

        try:
            chat = await event.get_chat()
            chat_name = getattr(chat, "title", None) or getattr(chat, "username", str(chat.id))
        except Exception:
            chat_name = "unknown"

        try:
            sender = await event.get_sender()
            sender_name = ""
            if isinstance(sender, User):
                sender_name = f"@{sender.username}" if sender.username else f"id:{sender.id}"
            elif isinstance(sender, (Channel, Chat)):
                sender_name = sender.title or ""
        except Exception:
            sender_name = ""

        source = chat_name + (f" / {sender_name}" if sender_name else "")
        if is_vip:
            source = f"👑 {source}"
        elif is_vip_topic:
            source = f"📢 {source}"
        elif is_priority:
            source = f"⭐ {source}"

        # ── Посилання на повідомлення ──────────────────────────────────
        msg_link = ""
        try:
            cid = peer_id
            mid = msg.id
            if str(cid).startswith("-100"):
                msg_link = f"https://t.me/c/{str(cid)[4:]}/{mid}"
            elif cid < 0:
                msg_link = f"https://t.me/c/{abs(cid)}/{mid}"
            else:
                msg_link = f"https://t.me/c/{cid}/{mid}"
        except Exception:
            pass

        # ── Підтягуємо повідомлення на яке відповідає VIP ─────────────
        replied_text = ""
        if is_vip and msg.reply_to_msg_id:
            try:
                replied_msg = await self.client.get_messages(peer_id, ids=msg.reply_to_msg_id)
                if replied_msg and replied_msg.text:
                    replied_text = replied_msg.text[:300]
            except Exception:
                pass

        await asyncio.sleep(0.3)

        # ── Фільтр або форс для VIP / VIP топіку ──────────────────────
        if is_vip_topic and not is_vip:
            # VIP топік: форвардимо без фільтрів з мінімальним score
            result = {"send": True, "score": 6, "summary": text[:300],
                      "category": "other", "urgency": "low", "coins": [], "keyword_hit": ""}
        elif is_vip:
            # VIP: пропускаємо pre-filter, Claude оцінює завжди
            result = filter_message(text, source=source, is_priority_user=True, bypass_prefilter=True)
            # Якщо Claude недоступний (no_credits/error) — надсилаємо сирий текст
            if result.get("_error"):
                result["send"] = True
                result["score"] = 5
                result["summary"] = text[:300]
                result["category"] = "other"
            elif result.get("score", 0) < 3:
                # Зовсім беззмістовне (< 3) — пропускаємо навіть VIP
                return
            else:
                result["send"] = True
                if result.get("score", 0) < 5:
                    result["score"] = 5
        else:
            result = filter_message(text, source=source, is_priority_user=is_priority)

        if result.get("send"):
            full_text = text
            if replied_text:
                full_text = f"↩️ <i>Відповідає на:</i> «{replied_text}»\n\n{text}"

            alert_id = storage.save_alert("tg_message", full_text, result.get("score", 0), source)
            await self.on_alert({
                "alert_id":   alert_id,
                "type":       "tg",
                "source":     source,
                "summary":    result.get("summary", text[:200]),
                "category":   result.get("category", "other"),
                "urgency":    result.get("urgency", "low"),
                "score":      result.get("score", 0),
                "coins":      result.get("coins", []),
                "keyword_hit": result.get("keyword_hit", ""),
                "raw":        full_text,
                "is_vip":     is_vip,
                "vip_name":   self._vip_names.get(sender_id, "VIP") if is_vip else "",
                "msg_link":   msg_link,
            })

    def add_negative(self, text: str, source: str = ""):
        add_negative_feedback(text, source=source)

    async def get_forum_topics(self, chat_id: str) -> list[dict]:
        """Повертає список топіків форум-групи."""
        try:
            from telethon.tl.functions.channels import GetForumTopicsRequest
            entity = await self.client.get_entity(chat_id)
            result = await self.client(GetForumTopicsRequest(
                channel=entity, offset_date=0, offset_id=0,
                offset_topic=0, limit=100, q=""
            ))
            topics = []
            for t in result.topics:
                topics.append({"id": t.id, "title": t.title})
            return topics
        except Exception as e:
            logger.error(f"get_forum_topics error: {e}")
            return []
