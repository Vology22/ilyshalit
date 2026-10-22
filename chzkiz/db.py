"""Слой базы (SQLite). Один пользователь - свои данные, проверка владения на каждом чтении."""
from __future__ import annotations

import json
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER UNIQUE NOT NULL,
    wb_key_enc TEXT,
    pg TEXT NOT NULL DEFAULT 'lp',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS login_tokens (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    expires_at REAL NOT NULL,
    used INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    inn TEXT,
    cert_owner TEXT,
    chz_token_enc TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    report_path TEXT,
    last_summary_counts TEXT,
    meta_failed_count INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    cis_raw TEXT NOT NULL,
    cis_norm TEXT NOT NULL,
    op TEXT NOT NULL,
    date TEXT NOT NULL DEFAULT '',
    chz_status TEXT,
    owner_inn TEXT,
    group_name TEXT NOT NULL,
    reason TEXT NOT NULL,
    product_cost INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    chz_doc_id TEXT,
    status TEXT NOT NULL DEFAULT 'NEW',
    codes_json TEXT NOT NULL,
    errors TEXT
);
"""


def get_conn(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or config.DB_PATH
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Старые базы созданы до этих колонок - добавляем, если PRAGMA их не видит."""
    def add_col(table: str, col: str, decl: str) -> None:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    add_col("items", "date", "TEXT NOT NULL DEFAULT ''")
    add_col("runs", "last_summary_counts", "TEXT")
    add_col("runs", "meta_failed_count", "INTEGER NOT NULL DEFAULT 0")
    add_col("runs", "skipped_files", "TEXT NOT NULL DEFAULT ''")
    add_col("users", "fias_id", "TEXT NOT NULL DEFAULT ''")
    add_col("users", "fias_inn", "TEXT NOT NULL DEFAULT ''")
    add_col("items", "istochnik", "TEXT NOT NULL DEFAULT ''")
    add_col("items", "tovar", "TEXT NOT NULL DEFAULT ''")
    add_col("items", "order_id", "INTEGER NOT NULL DEFAULT 0")
    add_col("runs", "warnings", "TEXT NOT NULL DEFAULT ''")
    add_col("runs", "files_info", "TEXT NOT NULL DEFAULT ''")
    add_col("documents", "done_at", "REAL")


@contextmanager
def connect(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    conn = get_conn(db_path)
    try:
        init_schema(conn)
        yield conn
    finally:
        conn.close()


# --- пользователи -----------------------------------------------------------

def get_or_create_user(conn: sqlite3.Connection, tg_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)).fetchone()
    if row:
        return row
    conn.execute(
        "INSERT INTO users (tg_id, created_at) VALUES (?, ?)", (tg_id, time.time())
    )
    conn.commit()
    return conn.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)).fetchone()


def set_wb_key(conn: sqlite3.Connection, user_id: int, wb_key_enc: str) -> None:
    conn.execute("UPDATE users SET wb_key_enc = ? WHERE id = ?", (wb_key_enc, user_id))
    conn.commit()


def set_user_pg(conn: sqlite3.Connection, user_id: int, pg: str) -> None:
    conn.execute("UPDATE users SET pg = ? WHERE id = ?", (pg, user_id))
    conn.commit()


def get_user(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


# --- одноразовая ссылка входа и сессия --------------------------------------

def create_login_token(conn: sqlite3.Connection, user_id: int) -> str:
    token = secrets.token_urlsafe(24)
    expires_at = time.time() + config.LOGIN_LINK_TTL_MIN * 60
    conn.execute(
        "INSERT INTO login_tokens (token, user_id, expires_at) VALUES (?, ?, ?)",
        (token, user_id, expires_at),
    )
    conn.commit()
    return token


def consume_login_token(conn: sqlite3.Connection, token: str) -> int | None:
    row = conn.execute(
        "SELECT * FROM login_tokens WHERE token = ?", (token,)
    ).fetchone()
    if not row or row["used"] or row["expires_at"] < time.time():
        return None
    conn.execute("UPDATE login_tokens SET used = 1 WHERE token = ?", (token,))
    conn.commit()
    return row["user_id"]


def peek_login_token(conn: sqlite3.Connection, token: str) -> int | None:
    row = conn.execute(
        "SELECT * FROM login_tokens WHERE token = ?", (token,)
    ).fetchone()
    if not row or row["used"] or row["expires_at"] < time.time():
        return None
    return row["user_id"]


def create_session(conn: sqlite3.Connection, user_id: int) -> str:
    session_id = secrets.token_urlsafe(32)
    expires_at = time.time() + config.SESSION_TTL_HOURS * 3600
    conn.execute(
        "INSERT INTO sessions (session_id, user_id, expires_at) VALUES (?, ?, ?)",
        (session_id, user_id, expires_at),
    )
    conn.commit()
    return session_id


def get_session_user_id(conn: sqlite3.Connection, session_id: str) -> int | None:
    row = conn.execute(
        "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    if not row or row["expires_at"] < time.time():
        return None
    return row["user_id"]


# --- сверка (runs / items) ---------------------------------------------------

def create_run(conn: sqlite3.Connection, user_id: int, source: str) -> str:
    run_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO runs (id, user_id, source, created_at) VALUES (?, ?, ?, ?)",
        (run_id, user_id, source, time.time()),
    )
    conn.commit()
    return run_id


def get_run(conn: sqlite3.Connection, run_id: str, user_id: int) -> sqlite3.Row | None:
    """Отдает запуск только если он принадлежит user_id - изоляция пользователей."""
    return conn.execute(
        "SELECT * FROM runs WHERE id = ? AND user_id = ?", (run_id, user_id)
    ).fetchone()


def set_run_chz_login(
    conn: sqlite3.Connection, run_id: str, inn: str, cert_owner: str, chz_token_enc: str
) -> None:
    conn.execute(
        "UPDATE runs SET inn = ?, cert_owner = ?, chz_token_enc = ?, status = 'logged_in' "
        "WHERE id = ?",
        (inn, cert_owner, chz_token_enc, run_id),
    )
    conn.commit()


def set_run_status(conn: sqlite3.Connection, run_id: str, status: str) -> None:
    conn.execute("UPDATE runs SET status = ? WHERE id = ?", (status, run_id))
    conn.commit()


def set_run_report(conn: sqlite3.Connection, run_id: str, path: str) -> None:
    conn.execute("UPDATE runs SET report_path = ? WHERE id = ?", (path, run_id))
    conn.commit()


def set_run_summary_counts(conn: sqlite3.Connection, run_id: str, counts_json: str) -> None:
    conn.execute("UPDATE runs SET last_summary_counts = ? WHERE id = ?", (counts_json, run_id))
    conn.commit()


def set_run_skipped_files(conn: sqlite3.Connection, run_id: str, names: list[str]) -> None:
    conn.execute("UPDATE runs SET skipped_files = ? WHERE id = ?", (", ".join(names), run_id))
    conn.commit()


def set_run_meta_failed_count(conn: sqlite3.Connection, run_id: str, count: int) -> None:
    conn.execute("UPDATE runs SET meta_failed_count = ? WHERE id = ?", (count, run_id))
    conn.commit()


def set_run_warnings(conn: sqlite3.Connection, run_id: str, warnings: list[str]) -> None:
    conn.execute("UPDATE runs SET warnings = ? WHERE id = ?", (chr(10).join(warnings), run_id))
    conn.commit()


def set_run_files_info(conn: sqlite3.Connection, run_id: str, info_json: str) -> None:
    conn.execute("UPDATE runs SET files_info = ? WHERE id = ?", (info_json, run_id))
    conn.commit()


def add_items(conn: sqlite3.Connection, run_id: str, items: list[dict[str, Any]]) -> None:
    conn.executemany(
        "INSERT INTO items (run_id, cis_raw, cis_norm, op, date, chz_status, owner_inn, "
        "group_name, reason, product_cost, istochnik, tovar, order_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                run_id,
                it["cis_raw"],
                it["cis_norm"],
                it["op"],
                it.get("date") or "",
                it.get("chz_status"),
                it.get("owner_inn"),
                it["group"],
                it["reason"],
                it.get("product_cost", 0),
                it.get("istochnik") or ("архив" if it.get("archive") else "отчет"),
                it.get("tovar") or "",
                it.get("order_id") or 0,
            )
            for it in items
        ],
    )
    conn.commit()


def get_items(conn: sqlite3.Connection, run_id: str, group: str | None = None) -> list[sqlite3.Row]:
    # group_name как есть (колонка) плюс алиас "group" (ждет sverka.py/excel_out.py) - одна строка, два имени.
    cols = '*, group_name AS "group"'
    if group:
        return conn.execute(
            f"SELECT {cols} FROM items WHERE run_id = ? AND group_name = ?", (run_id, group)
        ).fetchall()
    return conn.execute(f"SELECT {cols} FROM items WHERE run_id = ?", (run_id,)).fetchall()


def clear_items(conn: sqlite3.Connection, run_id: str) -> None:
    conn.execute("DELETE FROM items WHERE run_id = ?", (run_id,))
    conn.commit()


# --- документы ---------------------------------------------------------------

def create_document(
    conn: sqlite3.Connection, run_id: str, doc_type: str, codes: list[str]
) -> str:
    doc_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO documents (id, run_id, doc_type, codes_json) VALUES (?, ?, ?, ?)",
        (doc_id, run_id, doc_type, json.dumps(codes)),
    )
    conn.commit()
    return doc_id


def reuse_document(conn: sqlite3.Connection, doc_id: str, codes: list[str]) -> bool:
    """Пересборка того же документа вместо нового: не ушел в ЧЗ (NEW) или ЧЗ/сеть отказали
    (PROCESSING_ERROR/CHECKED_NOT_OK) - коды и статус обновляем на месте, id остается тем же.
    False - документ уже ушел в отправку, его коды трогать нельзя."""
    cur = conn.execute(
        "UPDATE documents SET codes_json = ?, status = 'NEW', chz_doc_id = NULL, errors = NULL "
        "WHERE id = ? AND chz_doc_id IS NULL AND status IN ('NEW', 'PROCESSING_ERROR')",
        (json.dumps(codes), doc_id),
    )
    conn.commit()
    return cur.rowcount == 1


def split_document(
    conn: sqlite3.Connection, doc_id: str, ok_codes: list[str], failed: dict[str, list[str]]
) -> None:
    """Документ обработан частично: принятые коды остаются в CHECKED_OK, отклоненные
    идут отдельными строками CHECKED_NOT_OK по причине. Номер в ЧЗ у всех частей один."""
    doc = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    parts: list[tuple[str, list[str], str | None]] = []
    if ok_codes:
        parts.append(("CHECKED_OK", ok_codes, None))
    for reason, codes in failed.items():
        parts.append(("CHECKED_NOT_OK", codes, reason))
    if doc is None or not parts:
        return
    status, codes, errors = parts[0]
    conn.execute(
        "UPDATE documents SET status = ?, codes_json = ?, errors = ? WHERE id = ?",
        (status, json.dumps(codes), errors, doc_id),
    )
    for status, codes, errors in parts[1:]:
        conn.execute(
            "INSERT INTO documents (id, run_id, doc_type, chz_doc_id, status, codes_json, errors) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, doc["run_id"], doc["doc_type"], doc["chz_doc_id"], status,
             json.dumps(codes), errors),
        )
    conn.commit()


def release_codes(
    conn: sqlite3.Connection, run_id: str, doc_type: str, codes: list[str], keep_id: str | None
) -> None:
    """Коды уходят в новый документ: из старых неудачных и несобранных строк их убираем,
    чтобы прежняя ошибка не перекрыла новый исход. Принятые и идущие в ЧЗ строки не трогаем."""
    gone = set(codes)
    for d in get_documents(conn, run_id):
        if d["doc_type"] != doc_type or d["id"] == keep_id:
            continue
        if d["status"] in ("IN_PROGRESS", "CHECKED_OK", DOC_SENDING):
            continue
        old = json.loads(d["codes_json"] or "[]")
        left = [c for c in old if c not in gone]
        if len(left) == len(old):
            continue
        if left:
            conn.execute("UPDATE documents SET codes_json = ? WHERE id = ?", (json.dumps(left), d["id"]))
        else:
            conn.execute("DELETE FROM documents WHERE id = ?", (d["id"],))
    conn.commit()


def set_document_chz_id(conn: sqlite3.Connection, doc_id: str, chz_doc_id: str) -> None:
    conn.execute(
        "UPDATE documents SET chz_doc_id = ?, status = 'IN_PROGRESS' WHERE id = ?",
        (chz_doc_id, doc_id),
    )
    conn.commit()


DOC_SENDING = "SENDING"  # наш статус на время отправки в ЧЗ


def claim_document(conn: sqlite3.Connection, doc_id: str, run_id: str) -> bool:
    """Занять документ под отправку; второй одновременный запрос получит False."""
    cur = conn.execute(
        "UPDATE documents SET status = ? WHERE id = ? AND run_id = ? AND chz_doc_id IS NULL "
        "AND status IN ('NEW', 'PROCESSING_ERROR')",
        (DOC_SENDING, doc_id, run_id),
    )
    conn.commit()
    return cur.rowcount == 1


def unclaim_document(conn: sqlite3.Connection, doc_id: str, status: str) -> None:
    conn.execute("UPDATE documents SET status = ? WHERE id = ? AND status = ?", (status, doc_id, DOC_SENDING))
    conn.commit()


def reset_sending(conn: sqlite3.Connection) -> None:
    """После перезапуска отправок в полете нет: зависший SENDING снова можно отправить."""
    conn.execute("UPDATE documents SET status = 'NEW' WHERE status = ? AND chz_doc_id IS NULL", (DOC_SENDING,))
    conn.commit()


def set_document_status(
    conn: sqlite3.Connection, doc_id: str, status: str, errors: str | None = None
) -> None:
    conn.execute(
        "UPDATE documents SET status = ?, errors = ? WHERE id = ?", (status, errors, doc_id)
    )
    conn.commit()


def get_document(conn: sqlite3.Connection, doc_id: str, run_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM documents WHERE id = ? AND run_id = ?", (doc_id, run_id)
    ).fetchone()


def get_documents(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM documents WHERE run_id = ?", (run_id,)).fetchall()


def get_prior_documents(
    conn: sqlite3.Connection, user_id: int, inn: str, run_id: str, run_created: float, doc_type: str, since: float
) -> list[sqlite3.Row]:
    """Документы того же продавца из других сверок, о которых сверка run_id знать не могла:
    ЧЗ еще не ответил либо принял их уже после ее запуска."""
    return conn.execute(
        "SELECT d.* FROM documents d JOIN runs r ON r.id = d.run_id "
        "WHERE r.user_id = ? AND r.inn = ? AND r.created_at >= ? AND d.run_id != ? "
        "AND d.doc_type = ? AND d.chz_doc_id != '' "
        "AND (d.status = 'IN_PROGRESS' OR (d.status = 'CHECKED_OK' AND d.done_at > ?))",
        (user_id, inn, since, run_id, doc_type, run_created),
    ).fetchall()


def mark_document_done(conn: sqlite3.Connection, run_id: str, chz_doc_id: str) -> None:
    conn.execute(
        "UPDATE documents SET done_at = ? WHERE run_id = ? AND chz_doc_id = ? AND done_at IS NULL",
        (time.time(), run_id, chz_doc_id),
    )
    conn.commit()


def set_user_fias(conn: sqlite3.Connection, user_id: int, fias_id: str, inn: str = "") -> None:
    conn.execute(
        "UPDATE users SET fias_id = ?, fias_inn = ? WHERE id = ?", (fias_id, inn, user_id)
    )
    conn.commit()
