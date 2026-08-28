# -*- coding: utf-8 -*-
"""
chat_import.py — импорт экспортированного чата (JSON) в базу бота
(таблица member_log_all), чтобы бот мог строить портреты участников
из полной истории чата, включая сообщения до вступления бота.

Поддерживаются два формата:

1) Telegram Desktop / chat_export (как result.json):
   {
     "name": "Чат",
     "type": "private_supergroup",
     "id": 2601260245,
     "messages": [
       {
         "id": ...,
         "date": "2026-06-07T00:00:29",
         "date_unixtime": "...",
         "from": "Yally",            # имя
         "from_id": "user5445238981",# "user<id>" / "channel<id>" / "chat<id>"
         "text": "...",              # строка ИЛИ список объектов с "text"
         ...
       }
     ]
   }

2) Плагин ChatExport (extragram):
   {
     "chat_name": "...",
     "dialog_id": <id>,
     "messages": [ {"from_id", "from_name", "text", "date", ...} ]
   }
"""

import json
import logging
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)


def _extract_text(raw) -> str:
    """Достать текст из поля 'text' (строка или список rich-элементов)."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts = []
        for el in raw:
            if isinstance(el, str):
                parts.append(el)
            elif isinstance(el, dict):
                t = el.get("text")
                if isinstance(t, str):
                    parts.append(t)
                elif isinstance(t, list):
                    parts.append(_extract_text(t))
        return "".join(parts)
    return str(raw)


def _normalize_from_id(raw):
    """Привести from_id к (user_id, is_user).

    Telegram Desktop: 'user123' / 'channel123' / 'chat123'.
    Плагин ChatExport: число (или '123').
    """
    if raw is None:
        return None, False
    s = str(raw).strip()
    low = s.lower()
    if low.startswith("user"):
        return low[len("user"):], True
    if low.startswith("channel") or low.startswith("chat"):
        return low[len("channel"):] if low.startswith("channel") else low[len("chat"):], False
    return low, True


def parse_chat_export_json(raw: str) -> Dict:
    """Разобрать JSON экспорта чата.

    Возвращает {"chat_id": int|None, "chat_name": str, "messages": [...]}.
    messages — список записей member_log_all:
        {"user_id", "username", "name", "text", "date"}.
    username в экспорте обычно отсутствует (None) — заполняется отдельно из live-лога.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Файл не является корректным JSON: {e}")

    if not isinstance(data, dict):
        raise ValueError("Корень JSON должен быть объектом { ... }")

    # --- Формат плагина ChatExport: chat_id из dialog_id (число) ---
    chat_id = data.get("dialog_id")
    if chat_id is None and data.get("id") is not None:
        chat_id = data.get("id")  # Telegram Desktop: топ-уровневый id

    chat_name = data.get("chat_name") or data.get("name") or ""

    raw_messages = data.get("messages")
    if not isinstance(raw_messages, list):
        raise ValueError("В JSON нет списка 'messages'")

    messages: List[Dict] = []
    seen = set()
    for m in raw_messages:
        if not isinstance(m, dict):
            continue
        if str(m.get("type", "message")) not in ("message", "", None):
            if isinstance(m.get("type"), str) and m["type"] != "message":
                # пропускаем служебные (service), оставляем только простые сообщения
                if m["type"] in ("service",):
                    continue

        text = _extract_text(m.get("text"))
        if not text or not text.strip():
            continue

        from_id_raw = m.get("from_id")
        user_id, is_user = _normalize_from_id(from_id_raw)
        if not user_id:
            continue

        from_name = m.get("from_name") or m.get("from") or ""
        if not from_name:
            from_name = str(user_id)

        date = m.get("date_unixtime") or m.get("date") or ""

        key = (str(user_id), str(date), str(text))
        if key in seen:
            continue
        seen.add(key)

        messages.append({
            "user_id": str(user_id),
            "username": None,
            "name": str(from_name),
            "text": str(text)[:4000],
            "date": str(date),
        })

    return {
        "chat_id": chat_id,
        "chat_name": chat_name,
        "messages": messages,
    }
