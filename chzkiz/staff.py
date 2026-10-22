"""Команда поддержки и настройки бота в базе.

Админы из ADMIN_TG_IDS (.env) - всегда админы, их нельзя убрать из бота: так доступ не потерять,
даже если удалить всех остальных. Остальные админы и операторы хранятся в таблице staff."""
from __future__ import annotations

import sqlite3
import time

from . import config

ROLES = {"admin": "админ", "operator": "оператор"}


def role_of(conn: sqlite3.Connection, tg_id: int) -> str | None:
    if tg_id in config.ADMIN_TG_IDS:
        return "admin"
    row = conn.execute("SELECT role FROM staff WHERE tg_id = ?", (tg_id,)).fetchone()
    return row[0] if row else None


def is_admin(conn: sqlite3.Connection, tg_id: int) -> bool:
    return role_of(conn, tg_id) == "admin"


def is_staff(conn: sqlite3.Connection, tg_id: int) -> bool:
    return role_of(conn, tg_id) is not None


def members(conn: sqlite3.Connection) -> list[dict]:
    """Вся команда: сначала админы из .env, затем из базы."""
    out = []
    in_db = {r["tg_id"]: dict(r) for r in conn.execute("SELECT * FROM staff ORDER BY role, added_at")}
    for tg_id in sorted(config.ADMIN_TG_IDS):
        row = in_db.pop(tg_id, None)
        out.append({"tg_id": tg_id, "role": "admin", "name": (row or {}).get("name", ""), "owner": True})
    for row in in_db.values():
        out.append({**row, "owner": False})
    return out


def add(conn: sqlite3.Connection, tg_id: int, role: str, name: str, added_by: int) -> None:
    if role not in ROLES:
        raise ValueError(role)
    conn.execute(
        "INSERT INTO staff (tg_id, role, name, added_by, added_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(tg_id) DO UPDATE SET role = excluded.role, name = excluded.name",
        (tg_id, role, name[:100], added_by, time.time()),
    )
    conn.commit()


def remove(conn: sqlite3.Connection, tg_id: int) -> bool:
    """Админа из .env убрать нельзя - False."""
    if tg_id in config.ADMIN_TG_IDS:
        return False
    cur = conn.execute("DELETE FROM staff WHERE tg_id = ?", (tg_id,))
    conn.commit()
    return cur.rowcount == 1


def set_name(conn: sqlite3.Connection, tg_id: int, name: str) -> None:
    if name:
        conn.execute("UPDATE staff SET name = ? WHERE tg_id = ? AND name != ?", (name[:100], tg_id, name[:100]))
        conn.commit()


# --- настройки -------------------------------------------------------------------------------

def get_setting(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else ""


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def support_chat_id(conn: sqlite3.Connection) -> int:
    """Группа поддержки: из .env или привязанная командой /bind."""
    if config.SUPPORT_CHAT_ID:
        return config.SUPPORT_CHAT_ID
    value = get_setting(conn, "support_chat_id")
    return int(value) if value.lstrip("-").isdigit() else 0
