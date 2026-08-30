import json
import logging
import os
import sqlite3
import time
from typing import Dict

import config

logger = logging.getLogger(__name__)

_db_path = config.DB_FILE_PATH


def _conn() -> sqlite3.Connection:
    d = os.path.dirname(_db_path)
    if d:
        os.makedirs(d, exist_ok=True)
    return sqlite3.connect(_db_path, timeout=30)


def init_table(table: str) -> None:
    conn = _conn()
    try:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {table} "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.commit()
    finally:
        conn.close()


def load_table(table: str) -> Dict[str, dict]:
    """Загрузить всю таблицу {key: value} в словарь."""
    init_table(table)
    conn = _conn()
    try:
        result = {}
        for key, value in conn.execute(f"SELECT key, value FROM {table}"):
            try:
                result[key] = json.loads(value)
            except Exception:
                result[key] = value
        return result
    finally:
        conn.close()


def save_table(table: str, data: Dict) -> None:
    """Полная перезапись таблицы из словаря {key: value}."""
    init_table(table)
    conn = _conn()
    try:
        conn.execute(f"DELETE FROM {table}")
        conn.executemany(
            f"INSERT INTO {table} (key, value) VALUES (?, ?)",
            ((str(k), json.dumps(v, ensure_ascii=False)) for k, v in data.items()),
        )
        conn.commit()
    finally:
        conn.close()


def upsert(table: str, key: str, value) -> None:
    """Записать/обновить одну запись (без перезаписи остальных)."""
    init_table(table)
    conn = _conn()
    try:
        conn.execute(
            f"INSERT OR REPLACE INTO {table} (key, value) VALUES (?, ?)",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()


def get_value(table: str, key: str):
    """Прочитать одну запись из таблицы."""
    init_table(table)
    conn = _conn()
    try:
        row = conn.execute(
            f"SELECT value FROM {table} WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except Exception:
            return row[0]
    finally:
        conn.close()


# --- Экспортированная история чата (из юзербота) ---
# Ключ таблицы member_log_all = chat_id, value = список сообщений вида
# [{"user_id": ..., "username": ..., "name": ..., "text": ..., "date": ...}, ...]

MEMBER_LOG_ALL_TABLE = "member_log_all"
# Сколько сообщений чата хранить в экспортированной истории (наилучший баланс
# данных и размера БД; для портрета берём до 200-300 последних на участника).
# Подняли с 5000 до 30000, чтобы личности копировали более полную манеру
# (среди них больше сообщений целевого человека).
MEMBER_LOG_ALL_LIMIT = 30000


# --- Кэш портретов личностей (чтобы не тратить токены повторно) ---
# Ключ = @username в нижнем регистре, value = {"personality": str,
# "message_count": int, "updated": float}.
PERSONALITY_CACHE_TABLE = "personality_cache"


def get_personality_cache(username: str):
    """Вернуть кэшированный портрет по @username (или None)."""
    key = str(username).lower().lstrip("@")
    val = get_value(PERSONALITY_CACHE_TABLE, key)
    if isinstance(val, dict) and val.get("personality"):
        return val
    return None


def set_personality_cache(username: str, personality: str, message_count: int) -> None:
    """Сохранить портрет участника по @username в кэш."""
    key = str(username).lower().lstrip("@")
    upsert(
        PERSONALITY_CACHE_TABLE,
        key,
        {
            "personality": personality,
            "message_count": int(message_count),
            "updated": time.time(),
        },
    )


def clear_personality_cache() -> None:
    """Очистить все кэшированные портреты (после обновления экспорта)."""
    init_table(PERSONALITY_CACHE_TABLE)
    conn = _conn()
    try:
        conn.execute(f"DELETE FROM {PERSONALITY_CACHE_TABLE}")
        conn.commit()
    finally:
        conn.close()


# --- База знаний об участниках (имена + портреты + кликухи) ---
# Ключ = user_id, value = {"name": str, "portrait": str, "nicknames": [str],
# "message_count": int, "updated": float}. Используется для встраивания
# информации о людях в контекст групповых ответов.
MEMBER_KNOWLEDGE_TABLE = "member_knowledge"


def load_member_knowledge() -> Dict[str, dict]:
    """Загрузить всю базу знаний об участниках {user_id: record}."""
    return load_table(MEMBER_KNOWLEDGE_TABLE)


def get_member_knowledge(user_id) -> dict | None:
    """Портрет/знания об участнике по user_id (или None)."""
    val = get_value(MEMBER_KNOWLEDGE_TABLE, str(user_id))
    return val if isinstance(val, dict) else None


def set_member_knowledge(user_id, name: str, portrait: str, nicknames: list, message_count: int) -> None:
    """Сохранить/обновить знания об участнике в базе."""
    upsert(
        MEMBER_KNOWLEDGE_TABLE,
        str(user_id),
        {
            "name": name or str(user_id),
            "portrait": portrait,
            "nicknames": nicknames or [],
            "message_count": int(message_count),
            "updated": time.time(),
        },
    )


# --- Локальные кликухи/сленг чата (для понимания мемов) ---
# Ключ = chat_id, value = [nickname, ...] — частые короткие слова чата.
CHAT_NICKNAMES_TABLE = "chat_nicknames"


def get_chat_nicknames(chat_id) -> list:
    val = get_value(CHAT_NICKNAMES_TABLE, str(chat_id))
    return val if isinstance(val, list) else []


def set_chat_nicknames(chat_id, nicknames: list) -> None:
    upsert(CHAT_NICKNAMES_TABLE, str(chat_id), nicknames or [])


def load_member_log_all(chat_id: int) -> list:
    """Вернуть экспортированную историю сообщений чата (или [])."""
    val = get_value(MEMBER_LOG_ALL_TABLE, str(chat_id))
    if isinstance(val, list):
        return val
    return []


def save_exported_messages(chat_id: int, messages: list) -> None:
    """Полностью перезаписать экспортированную историю чата (с усечением)."""
    if len(messages) > MEMBER_LOG_ALL_LIMIT:
        messages = messages[-MEMBER_LOG_ALL_LIMIT:]
    upsert(MEMBER_LOG_ALL_TABLE, str(chat_id), messages)


def get_member_log_all_for_user(chat_id: int, user_id: int, limit: int = 300) -> list:
    """Последние сообщения конкретного участника из экспортированной истории."""
    all_messages = load_member_log_all(chat_id)
    matched = [
        m for m in all_messages
        if str(m.get("user_id")) == str(user_id) and (m.get("text") or "").strip()
    ]
    return matched[-limit:]


def iter_member_log_all_chats():
    """Перебрать ВСЕ чаты с экспортированной историей: (chat_id, messages)."""
    table = MEMBER_LOG_ALL_TABLE
    init_table(table)
    conn = _conn()
    try:
        for key, value in conn.execute(f"SELECT key, value FROM {table}"):
            try:
                messages = json.loads(value)
            except Exception:
                messages = []
            if isinstance(messages, list):
                yield str(key), messages
    finally:
        conn.close()


def find_member_globally(username: str, limit: int = 300) -> list:
    """Найти сообщения участника по @username во ВСЕХ чатах базы.

    Используется для инлайн-запросов 'кто такой @X', когда из inline-контекста
    неизвестен чат. Возвращает список текстовых сообщений (от старых к новым).

    Сначала ищем по полю username, затем по отображаемому имени (name) — многие
    участники ставят отображаемое имя, совпадающее с их @username. В Telegram
    Desktop JSON export поле username обычно пустое, поэтому без name-фоллбэка
    такие запросы не находили сообщений.
    """
    uname = str(username).lower().lstrip("@")
    collected = []
    by_name = []
    for _chat_id, messages in iter_member_log_all_chats():
        for m in messages:
            mu = m.get("username")
            if mu and str(mu).lower().lstrip("@") == uname:
                text = (m.get("text") or "").strip()
                if text:
                    collected.append(text)
                continue
            name = m.get("name")
            if name and str(name).strip().lower().lstrip("@") == uname:
                text = (m.get("text") or "").strip()
                if text:
                    by_name.append(text)
    if collected:
        return collected[-limit:]
    return by_name[-limit:]


def get_member_log_all_for_user_global(user_id, limit: int = 300) -> list:
    """Сообщения участника по user_id во ВСЕХ чатах экспортированной истории."""
    collected = []
    for _chat_id, messages in iter_member_log_all_chats():
        for m in messages:
            if str(m.get("user_id")) == str(user_id):
                text = (m.get("text") or "").strip()
                if text:
                    collected.append(text)
    return collected[-limit:]



def diagnose() -> str:
    """Человекочитаемая сводка о состоянии БД — для стартовых логов."""
    d = os.path.dirname(_db_path) or "."
    exists = os.path.exists(_db_path)
    size = os.path.getsize(_db_path) if exists else 0
    dir_ok = os.path.isdir(d) and os.access(d, os.W_OK)
    file_ok = os.access(_db_path, os.W_OK) if exists else None
    return (
        f"path={_db_path} | exists={exists} | size={size}b | "
        f"dir_writable={dir_ok} | file_writable={file_ok}"
    )


def self_test() -> str:
    """Пишет и читает тестовую запись — проверяет, что БД реально персистится."""
    try:
        upsert("_selftest", "key", "value")
        ok = get_value("_selftest", "key") == "value"
        return "persistence self-test: OK" if ok else "persistence self-test: FAILED"
    except Exception as e:
        return f"persistence self-test: ERROR {e}"


# ============================================================
#  Персона чата: бот в конкретной группе копирует личность участника
# ============================================================

PERSONA_TABLE = "settings"
_PERSONA_KEY_PREFIX = "persona_"


def set_chat_persona(chat_id: int, user_id: int) -> None:
    """Задать: в чате chat_id бот отвечает в манере пользователя user_id."""
    upsert(PERSONA_TABLE, f"{_PERSONA_KEY_PREFIX}{chat_id}", int(user_id))


def clear_chat_persona(chat_id: int) -> None:
    """Убрать персону для чата (бот снова отвечает как сам)."""
    upsert(PERSONA_TABLE, f"{_PERSONA_KEY_PREFIX}{chat_id}", None)


def get_chat_persona(chat_id: int):
    """user_id персоны для чата, либо None."""
    return get_value(PERSONA_TABLE, f"{_PERSONA_KEY_PREFIX}{chat_id}")
