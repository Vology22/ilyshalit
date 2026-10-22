"""Обращения в поддержку: тема пользователя в группе операторов, статус и короткая переписка.

Переписка нужна только для экрана чата в боте: храним по KEEP последних сообщений с каждой
стороны и стираем все, когда обращение закрыто. Полная история остается в теме группы."""
from __future__ import annotations

import sqlite3
import time
from typing import Any

KEEP = 5  # сообщений пользователя (и столько же ответов оператора) в памяти бота


def get_thread(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM support_threads WHERE user_id = ?", (user_id,)).fetchone()


def is_open(conn: sqlite3.Connection, user_id: int) -> bool:
    t = get_thread(conn, user_id)
    return bool(t and t["status"] == "open")


def set_topic(conn: sqlite3.Connection, user_id: int, topic_id: int) -> None:
    conn.execute(
        "INSERT INTO support_threads (user_id, topic_id) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET topic_id = excluded.topic_id",
        (user_id, topic_id),
    )
    conn.commit()


def open_thread(conn: sqlite3.Connection, user_id: int) -> bool:
    """Открыть обращение. True - было закрыто (новое обращение), False - уже открыто."""
    now = time.time()
    conn.execute("INSERT OR IGNORE INTO support_threads (user_id) VALUES (?)", (user_id,))
    cur = conn.execute(
        "UPDATE support_threads SET status = 'open', opened_at = ? WHERE user_id = ? AND status != 'open'",
        (now, user_id),
    )
    conn.commit()
    return cur.rowcount == 1


def close_thread(conn: sqlite3.Connection, user_id: int) -> bool:
    """Закрыть обращение и стереть его переписку из базы."""
    cur = conn.execute(
        "UPDATE support_threads SET status = 'closed' WHERE user_id = ? AND status = 'open'", (user_id,)
    )
    conn.execute("DELETE FROM support_messages WHERE user_id = ?", (user_id,))
    conn.commit()
    return cur.rowcount == 1


def user_by_topic(conn: sqlite3.Connection, topic_id: int) -> int | None:
    row = conn.execute("SELECT user_id FROM support_threads WHERE topic_id = ?", (topic_id,)).fetchone()
    return row[0] if row else None


def log(conn: sqlite3.Connection, user_id: int, direction: str, text: str) -> None:
    """direction: in - от пользователя, out - ответ оператора."""
    now = time.time()
    conn.execute(
        "INSERT INTO support_messages (user_id, direction, text, created_at) VALUES (?, ?, ?, ?)",
        (user_id, direction, text[:4000], now),
    )
    conn.execute(
        "DELETE FROM support_messages WHERE user_id = ? AND direction = ? AND id NOT IN "
        "(SELECT id FROM support_messages WHERE user_id = ? AND direction = ? ORDER BY id DESC LIMIT ?)",
        (user_id, direction, user_id, direction, KEEP),
    )
    col = "last_user_at" if direction == "in" else "last_op_at"
    conn.execute(f"UPDATE support_threads SET {col} = ? WHERE user_id = ?", (now, user_id))
    conn.commit()


def history(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT direction, text, created_at FROM support_messages WHERE user_id = ? ORDER BY id", (user_id,)
    ).fetchall()


def mark_seen(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("UPDATE support_threads SET seen_at = ? WHERE user_id = ?", (time.time(), user_id))
    conn.commit()


def chat_state(conn: sqlite3.Connection, user_id: int) -> str:
    """Для кнопки в меню: '' - обращения нет, open - открыто, unread - есть непрочитанный ответ."""
    t = get_thread(conn, user_id)
    if not t or t["status"] != "open":
        return ""
    if t["last_op_at"] and t["last_op_at"] > (t["seen_at"] or 0):
        return "unread"
    return "open"


def open_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM support_threads WHERE status = 'open'").fetchone()[0]


def user_context(conn: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    """Что оператору полезно знать сразу: баланс, токен, последняя сверка и ее ошибки, оплаты."""
    u = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    run = conn.execute(
        "SELECT id, status, inn, created_at FROM runs WHERE user_id = ? ORDER BY created_at DESC LIMIT 1", (user_id,)
    ).fetchone()
    errors: list[str] = []
    if run:
        errors = [
            r[0] for r in conn.execute(
                "SELECT DISTINCT errors FROM documents WHERE run_id = ? AND errors IS NOT NULL AND errors != '' LIMIT 3",
                (run["id"],),
            )
        ]
    paid = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(amount_kop), 0) FROM payments WHERE user_id = ? AND status = 'succeeded'",
        (user_id,),
    ).fetchone()
    return {"user": u, "run": run, "errors": errors, "payments": paid[0], "paid_kop": paid[1]}


def open_threads(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    """Открытые обращения: сначала те, где пользователь ждет ответа дольше всех."""
    return conn.execute(
        "SELECT t.*, u.tg_id, "
        "CASE WHEN t.last_user_at IS NOT NULL AND (t.last_op_at IS NULL OR t.last_op_at < t.last_user_at) "
        "THEN 1 ELSE 0 END AS waiting "
        "FROM support_threads t JOIN users u ON u.id = t.user_id WHERE t.status = 'open' "
        "ORDER BY waiting DESC, t.last_user_at ASC LIMIT ?",
        (limit,),
    ).fetchall()


def waiting_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM support_threads WHERE status = 'open' AND last_user_at IS NOT NULL "
        "AND (last_op_at IS NULL OR last_op_at < last_user_at)"
    ).fetchone()[0]


def remind_due(conn: sqlite3.Connection, minutes: int) -> list[tuple[int, int, int]]:
    """Обращения без ответа дольше minutes, о которых еще не напоминали после последнего сообщения
    пользователя. Возвращает (user_id, topic_id, минут ждет) и помечает напоминание."""
    now = time.time()
    rows = conn.execute(
        "SELECT user_id, topic_id, last_user_at FROM support_threads WHERE status = 'open' "
        "AND topic_id IS NOT NULL AND last_user_at IS NOT NULL AND last_user_at < ? "
        "AND (last_op_at IS NULL OR last_op_at < last_user_at) "
        "AND (reminded_at IS NULL OR reminded_at < last_user_at)",
        (now - minutes * 60,),
    ).fetchall()
    for r in rows:
        conn.execute("UPDATE support_threads SET reminded_at = ? WHERE user_id = ?", (now, r["user_id"]))
    conn.commit()
    return [(r["user_id"], r["topic_id"], int((now - r["last_user_at"]) // 60)) for r in rows]
