import json
import logging
import os
import sqlite3
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
