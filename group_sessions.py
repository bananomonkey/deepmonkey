import asyncio
import logging
from typing import Dict, List, Optional

import config
import database

logger = logging.getLogger(__name__)

TABLE = "group_chats"

# Сколько последних сообщений участников каждой группы хранить для обучения
# профилей (чтобы было из чего суммировать заметки о личностях).
MEMBER_LOG_LIMIT = 100


class GroupChatSessions:
    """История сообщений ИИ и профили участников отдельно для каждой группы.

    Контекст в общих чатах полностью изолирован от ЛС бота: ключ — chat_id
    группы, а не user_id. Каждая группа ведёт свой независимый диалог
    и накапливает заметки-профили о своих участниках.
    """

    def __init__(self, max_history_turns: int):
        self.max_history_turns = max_history_turns
        self._lock = asyncio.Lock()
        self._data: Dict[str, dict] = {}
        self._load_from_db()

    def _load_from_db(self) -> None:
        try:
            self._data = database.load_table(TABLE)
            logger.info("Загружена история для %d групп из БД", len(self._data))
        except Exception as e:
            logger.error("Не удалось прочитать историю групп из БД: %s", e)

    def _ensure(self, chat_id: int) -> dict:
        key = str(chat_id)
        if key not in self._data:
            self._data[key] = {"history": [], "member_log": [], "member_profiles": {}}
        return self._data[key]

    def get_history(self, chat_id: int) -> List[dict]:
        rec = self._data.get(str(chat_id))
        return list(rec["history"]) if rec else []

    async def append_message(self, chat_id: int, role: str, content: str) -> None:
        async with self._lock:
            rec = self._ensure(chat_id)
            rec["history"].append({"role": role, "content": content})
            max_messages = self.max_history_turns * 2
            if len(rec["history"]) > max_messages:
                rec["history"] = rec["history"][-max_messages:]
            await self._save_to_db()

    async def observe(self, chat_id: int, user_id: int, username: Optional[str], full_name: Optional[str], text: str) -> None:
        """Зафиксировать сообщение участника группы для обучения его профиля."""
        async with self._lock:
            rec = self._ensure(chat_id)
            rec.setdefault("member_log", [])
            rec.setdefault("member_profiles", {})
            rec["member_log"].append({
                "user_id": str(user_id),
                "username": username,
                "name": full_name,
                "text": text,
            })
            if len(rec["member_log"]) > MEMBER_LOG_LIMIT:
                rec["member_log"] = rec["member_log"][-MEMBER_LOG_LIMIT:]
            await self._save_to_db()

    def get_member_log(self, chat_id: int, user_id: int, limit: int = 30) -> List[dict]:
        """Последние сообщения конкретного участника из этого чата."""
        rec = self._data.get(str(chat_id))
        if not rec:
            return []
        return [
            m for m in rec.get("member_log", [])
            if str(m.get("user_id")) == str(user_id)
        ][-limit:]

    def get_member_exported_log(self, chat_id: int, user_id: int, limit: int = 300) -> List[str]:
        """Сообщения участника из полной экспортированной истории (юзербот)."""
        import database
        try:
            messages = database.get_member_log_all_for_user(chat_id, user_id, limit=limit)
        except Exception as e:
            logger.error("Не удалось прочитать экспортированную историю: %s", e)
            return []
        return [m.get("text", "") for m in messages if m.get("text")]

    def get_full_member_log(self, chat_id: int, user_id: int, limit: int = 300) -> List[str]:
        """Объединённые сообщения участника: экспорт (полная история) + live.

        Приоритет у экспортированной истории — она полнее (включает переписку
        до вступления бота). Если её нет, отдаём live-лог.
        """
        texts = self.get_member_exported_log(chat_id, user_id, limit=limit)
        if texts:
            # Портрет строится из последних сообщений, order — от старых к новым.
            return texts
        live = [m.get("text", "") for m in self.get_member_log(chat_id, user_id, limit=limit) if m.get("text")]
        return live

    def find_member_by_username(self, chat_id: int, username: str) -> Optional[int]:
        """Возвращает user_id участника этого чата по его @username или None.

        Ищем и в live-логе (сообщения после вступления бота), и в
        экспортированной истории (юзербот), так как live-лог может быть пуст,
        если участник не писал после вступления бота.
        """
        uname = username.lower()

        rec = self._data.get(str(chat_id))
        if rec:
            for m in rec.get("member_log", []):
                mu = m.get("username")
                if mu and mu.lower() == uname:
                    return int(m["user_id"])

        try:
            import database
            for m in database.load_member_log_all(chat_id):
                mu = m.get("username")
                if mu and str(mu).lower() == uname:
                    uid = m.get("user_id")
                    if uid is not None:
                        return int(uid)
        except Exception as e:
            logger.error("Не удалось искать участника в экспортированной истории: %s", e)

        return None

    def member_display_name(self, chat_id: int, user_id: int) -> str:
        """Последнее известное имя участника (username → один из ников)."""
        # Сначала из экспортированной истории (там больше данных).
        try:
            import database
            for m in reversed(database.load_member_log_all(chat_id)):
                if str(m.get("user_id")) == str(user_id):
                    return m.get("name") or m.get("username") or str(user_id)
        except Exception:
            pass

        rec = self._data.get(str(chat_id))
        if rec:
            for m in reversed(rec.get("member_log", [])):
                if str(m.get("user_id")) == str(user_id):
                    return m.get("name") or m.get("username") or str(user_id)
        return str(user_id)

    def get_member_profiles(self, chat_id: int) -> Dict[str, str]:
        rec = self._data.get(str(chat_id))
        return dict(rec.get("member_profiles", {})) if rec else {}

    def get_member_profile(self, chat_id: int, user_id: int) -> str:
        rec = self._data.get(str(chat_id))
        if not rec:
            return ""
        return rec.get("member_profiles", {}).get(str(user_id), "")

    async def set_member_profile(self, chat_id: int, user_id: int, profile: str) -> None:
        async with self._lock:
            rec = self._ensure(chat_id)
            rec.setdefault("member_profiles", {})[str(user_id)] = profile
            await self._save_to_db()

    async def clear_chat(self, chat_id: int) -> None:
        async with self._lock:
            self._data[str(chat_id)] = {"history": [], "member_log": [], "member_profiles": {}}
            await self._save_to_db()

    async def _save_to_db(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._write_db)
        except Exception as e:
            logger.error("Не удалось сохранить историю групп в БД: %s", e)

    def _write_db(self) -> None:
        database.save_table(TABLE, self._data)


group_sessions = GroupChatSessions(config.HISTORY_MAX_TURNS)
