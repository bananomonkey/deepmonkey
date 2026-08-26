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
