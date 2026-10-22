"""Баланс операций, тарифы, платежи и реферальные начисления.

Операция = один документ вывода или возврата. Списывается перед отправкой в Честный знак
и возвращается, если отправка сорвалась или ЧЗ отклонил документ целиком. Все изменения
баланса идут через _move: одна транзакция, запись в ledger с балансом после."""
from __future__ import annotations

import secrets
import sqlite3
import string
import time
import uuid
from typing import Any

from . import config

KIND_LABELS = {
    "welcome": "Бесплатно при регистрации",
    "purchase": "Покупка тарифа",
    "spend": "Документ отправлен в Честный знак",
    "refund": "Возврат: документ не принят",
    "referral": "Бонус за приглашенного",
    "admin": "Корректировка администратором",
}
PAYMENT_LABELS = {"pending": "Ожидает оплаты", "succeeded": "Оплачен", "canceled": "Отменен"}
REWARD_LABELS = {"accrued": "Начислено, к выплате", "paid": "Выплачено", "credited": "Зачислено на баланс"}

NOT_ENOUGH = "Закончились операции. Пополните баланс в личном кабинете: каждый документ вывода или возврата - одна операция."


class NotEnoughOps(Exception):
    pass


def _move(conn: sqlite3.Connection, user_id: int, delta: int, kind: str, ref: str = "", note: str = "") -> int:
    """Меняет баланс и пишет движение. Без commit: вызывающий решает, где граница транзакции.
    Списание не уводит баланс в минус - иначе NotEnoughOps."""
    if delta < 0:
        cur = conn.execute(
            "UPDATE users SET balance = balance + ? WHERE id = ? AND balance >= ?", (delta, user_id, -delta)
        )
        if cur.rowcount != 1:
            raise NotEnoughOps(NOT_ENOUGH)
    else:
        conn.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (delta, user_id))
    balance = conn.execute("SELECT balance FROM users WHERE id = ?", (user_id,)).fetchone()[0]
    conn.execute(
        "INSERT INTO ledger (user_id, delta, kind, ref, note, balance_after, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, delta, kind, ref, note, balance, time.time()),
    )
    return balance


def balance(conn: sqlite3.Connection, user_id: int) -> int:
    row = conn.execute("SELECT balance FROM users WHERE id = ?", (user_id,)).fetchone()
    return row[0] if row else 0


def history(conn: sqlite3.Connection, user_id: int, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM ledger WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit)
    ).fetchall()


# --- списание за документ ------------------------------------------------------

def charge_document(conn: sqlite3.Connection, user_id: int, doc_id: str, note: str = "") -> bool:
    """Списать операцию за документ. False - уже списана раньше (повторная отправка того же
    документа после сбоя связи второй раз не списывает). NotEnoughOps - баланс пуст."""
    try:
        cur = conn.execute("UPDATE documents SET charged = 1 WHERE id = ? AND charged = 0", (doc_id,))
        if cur.rowcount != 1:
            conn.commit()
            return False
        _move(conn, user_id, -1, "spend", doc_id, note)
        conn.commit()
        return True
    except BaseException:
        conn.rollback()
        raise


def refund_document(conn: sqlite3.Connection, user_id: int, doc_id: str, note: str = "") -> bool:
    """Вернуть операцию за документ, если она была списана. Повторный вызов ничего не делает."""
    cur = conn.execute("UPDATE documents SET charged = 0 WHERE id = ? AND charged = 1", (doc_id,))
    if cur.rowcount != 1:
        conn.commit()
        return False
    _move(conn, user_id, 1, "refund", doc_id, note)
    conn.commit()
    return True


def settle_document(conn: sqlite3.Connection, user_id: int, doc_id: str, chz_doc_id: str) -> None:
    """ЧЗ вынес решение. Все части документа отклонены - операцию возвращаем;
    принята хотя бы часть кодов - операция потрачена."""
    if not chz_doc_id:
        return
    statuses = {
        r[0] for r in conn.execute("SELECT status FROM documents WHERE chz_doc_id = ?", (chz_doc_id,))
    }
    if statuses and statuses == {"CHECKED_NOT_OK"}:
        refund_document(conn, user_id, doc_id, "Честный знак отклонил документ")


# --- тарифы ----------------------------------------------------------------------

def tariffs(conn: sqlite3.Connection, active_only: bool = True) -> list[sqlite3.Row]:
    where = "WHERE active = 1" if active_only else ""
    return conn.execute(f"SELECT * FROM tariffs {where} ORDER BY sort, id").fetchall()


def get_tariff(conn: sqlite3.Connection, tariff_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM tariffs WHERE id = ?", (tariff_id,)).fetchone()


def save_tariff(
    conn: sqlite3.Connection, tariff_id: int | None, name: str, ops: int, price_kop: int, active: bool, sort: int
) -> int:
    if tariff_id:
        conn.execute(
            "UPDATE tariffs SET name = ?, ops = ?, price_kop = ?, active = ?, sort = ? WHERE id = ?",
            (name, ops, price_kop, int(active), sort, tariff_id),
        )
    else:
        cur = conn.execute(
            "INSERT INTO tariffs (name, ops, price_kop, active, sort, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (name, ops, price_kop, int(active), sort, time.time()),
        )
        tariff_id = cur.lastrowid
    conn.commit()
    return tariff_id


# --- платежи -----------------------------------------------------------------------

def create_payment(conn: sqlite3.Connection, user_id: int, tariff: sqlite3.Row, provider: str) -> str:
    """Цена и число операций копируются в платеж: смена тарифа админом не меняет уже выставленный счет."""
    payment_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO payments (id, user_id, tariff_id, tariff_name, ops, amount_kop, provider, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (payment_id, user_id, tariff["id"], tariff["name"], tariff["ops"], tariff["price_kop"], provider, time.time()),
    )
    conn.commit()
    return payment_id


def set_payment_provider_data(conn: sqlite3.Connection, payment_id: str, provider_id: str, url: str) -> None:
    conn.execute(
        "UPDATE payments SET provider_id = ?, confirmation_url = ? WHERE id = ?", (provider_id, url, payment_id)
    )
    conn.commit()


def get_payment(conn: sqlite3.Connection, payment_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()


def get_payment_by_provider_id(conn: sqlite3.Connection, provider: str, provider_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM payments WHERE provider = ? AND provider_id = ?", (provider, provider_id)
    ).fetchone()


def user_payments(conn: sqlite3.Connection, user_id: int, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM payments WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (user_id, limit)
    ).fetchall()


def cancel_payment(conn: sqlite3.Connection, payment_id: str) -> None:
    conn.execute("UPDATE payments SET status = 'canceled' WHERE id = ? AND status = 'pending'", (payment_id,))
    conn.commit()


def apply_payment(conn: sqlite3.Connection, payment_id: str) -> bool:
    """Платеж прошел: операции на баланс и реферальное начисление. Ровно один раз, даже если
    уведомление пришло дважды или одновременно с возвратом покупателя на сайт."""
    try:
        cur = conn.execute(
            "UPDATE payments SET status = 'succeeded', paid_at = ? WHERE id = ? AND status = 'pending'",
            (time.time(), payment_id),
        )
        if cur.rowcount != 1:
            conn.commit()
            return False
        p = get_payment(conn, payment_id)
        _move(conn, p["user_id"], p["ops"], "purchase", payment_id, f"Тариф {p['tariff_name']}")
        _reward_referrer(conn, p)
        conn.commit()
        return True
    except BaseException:
        conn.rollback()
        raise


def _reward_referrer(conn: sqlite3.Connection, p: sqlite3.Row) -> None:
    user = conn.execute("SELECT id, tg_id, referred_by FROM users WHERE id = ?", (p["user_id"],)).fetchone()
    if not user or not user["referred_by"]:
        return
    referrer = conn.execute("SELECT id, category FROM users WHERE id = ?", (user["referred_by"],)).fetchone()
    if not referrer:
        return
    now = time.time()
    if referrer["category"] == "fulfillment":
        amount = p["amount_kop"] * config.REF_FF_PERCENT // 100
        conn.execute(
            "INSERT OR IGNORE INTO ref_rewards (referrer_id, referee_id, payment_id, kind, amount_kop, status, created_at) "
            "VALUES (?, ?, ?, 'money', ?, 'accrued', ?)",
            (referrer["id"], user["id"], p["id"], amount, now),
        )
        return
    # селлеру/менеджеру - только за первую успешную оплату приглашенного
    paid_before = conn.execute(
        "SELECT COUNT(*) FROM payments WHERE user_id = ? AND status = 'succeeded' AND id != ?", (user["id"], p["id"])
    ).fetchone()[0]
    if paid_before:
        return
    cur = conn.execute(
        "INSERT OR IGNORE INTO ref_rewards (referrer_id, referee_id, payment_id, kind, ops, status, created_at, paid_at) "
        "VALUES (?, ?, ?, 'ops', ?, 'credited', ?, ?)",
        (referrer["id"], user["id"], p["id"], config.REF_SELLER_BONUS_OPS, now, now),
    )
    if cur.rowcount == 1:
        _move(conn, referrer["id"], config.REF_SELLER_BONUS_OPS, "referral", p["id"],
              f"Приглашенный пользователь #{user['id']} оплатил тариф")


# --- профиль и рефералы ------------------------------------------------------------

_REF_ALPHABET = string.ascii_lowercase + string.digits


def ensure_ref_code(conn: sqlite3.Connection, user_id: int) -> str:
    row = conn.execute("SELECT ref_code FROM users WHERE id = ?", (user_id,)).fetchone()
    if row and row[0]:
        return row[0]
    for _ in range(10):
        code = "".join(secrets.choice(_REF_ALPHABET) for _ in range(8))
        try:
            conn.execute("UPDATE users SET ref_code = ? WHERE id = ? AND ref_code IS NULL", (code, user_id))
            conn.commit()
        except sqlite3.IntegrityError:
            continue
        return conn.execute("SELECT ref_code FROM users WHERE id = ?", (user_id,)).fetchone()[0]
    raise RuntimeError("Не удалось выдать реферальный код")


def ref_link(code: str) -> str:
    if config.BOT_USERNAME:
        return f"https://t.me/{config.BOT_USERNAME}?start=ref_{code}"
    return f"ref_{code}"


def set_category(conn: sqlite3.Connection, user_id: int, category: str, force: bool = False) -> bool:
    """Пользователь выбирает категорию один раз, сменить ее может только админ (force):
    иначе селлер переключился бы на фулфилмент ради выплат деньгами."""
    if category not in config.CATEGORIES:
        return False
    sql = "UPDATE users SET category = ? WHERE id = ?" + ("" if force else " AND category = ''")
    cur = conn.execute(sql, (category, user_id))
    conn.commit()
    return cur.rowcount == 1


def set_profile(conn: sqlite3.Connection, user_id: int, email: str | None = None, payout: str | None = None) -> None:
    if email is not None:
        conn.execute("UPDATE users SET email = ? WHERE id = ?", (email, user_id))
    if payout is not None:
        conn.execute("UPDATE users SET payout_details = ? WHERE id = ?", (payout, user_id))
    conn.commit()


def ref_stats(conn: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    invited = conn.execute("SELECT COUNT(*) FROM users WHERE referred_by = ?", (user_id,)).fetchone()[0]
    paid = conn.execute(
        "SELECT COUNT(DISTINCT p.user_id) FROM payments p JOIN users u ON u.id = p.user_id "
        "WHERE u.referred_by = ? AND p.status = 'succeeded'",
        (user_id,),
    ).fetchone()[0]
    sums = conn.execute(
        "SELECT COALESCE(SUM(ops), 0), "
        "COALESCE(SUM(CASE WHEN kind = 'money' AND status = 'accrued' THEN amount_kop END), 0), "
        "COALESCE(SUM(CASE WHEN kind = 'money' AND status = 'paid' THEN amount_kop END), 0) "
        "FROM ref_rewards WHERE referrer_id = ?",
        (user_id,),
    ).fetchone()
    return {"invited": invited, "paid": paid, "ops": sums[0], "accrued_kop": sums[1], "paid_kop": sums[2]}


def user_rewards(conn: sqlite3.Connection, user_id: int, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM ref_rewards WHERE referrer_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit)
    ).fetchall()


# --- админка -------------------------------------------------------------------------

def admin_adjust(conn: sqlite3.Connection, user_id: int, delta: int, note: str) -> int:
    try:
        new_balance = _move(conn, user_id, delta, "admin", "", note)
        conn.commit()
        return new_balance
    except BaseException:
        conn.rollback()
        raise


def payouts_due(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Фулфилменты с невыплаченными начислениями - список на еженедельную выплату."""
    return conn.execute(
        "SELECT u.id, u.tg_id, u.payout_details, COUNT(r.id) AS n, SUM(r.amount_kop) AS amount_kop, "
        "MIN(r.created_at) AS since FROM ref_rewards r JOIN users u ON u.id = r.referrer_id "
        "WHERE r.kind = 'money' AND r.status = 'accrued' GROUP BY u.id ORDER BY amount_kop DESC"
    ).fetchall()


def mark_paid(conn: sqlite3.Connection, referrer_id: int, upto_id: int) -> int:
    """Отметить выплаченными начисления до upto_id включительно: то, что пришло, пока админ
    переводил деньги, остается к следующей выплате."""
    cur = conn.execute(
        "UPDATE ref_rewards SET status = 'paid', paid_at = ? "
        "WHERE referrer_id = ? AND kind = 'money' AND status = 'accrued' AND id <= ?",
        (time.time(), referrer_id, upto_id),
    )
    conn.commit()
    return cur.rowcount


def max_accrued_id(conn: sqlite3.Connection, referrer_id: int) -> int:
    row = conn.execute(
        "SELECT MAX(id) FROM ref_rewards WHERE referrer_id = ? AND kind = 'money' AND status = 'accrued'",
        (referrer_id,),
    ).fetchone()
    return row[0] or 0


def admin_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    now = time.time()
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
    revenue = {
        label: q("SELECT COALESCE(SUM(amount_kop), 0) FROM payments WHERE status = 'succeeded' AND paid_at >= ?", now - days * 86400)
        for label, days in (("day", 1), ("week", 7), ("month", 30))
    }
    revenue["all"] = q("SELECT COALESCE(SUM(amount_kop), 0) FROM payments WHERE status = 'succeeded'")
    return {
        "users": q("SELECT COUNT(*) FROM users"),
        "sellers": q("SELECT COUNT(*) FROM users WHERE category = 'seller'"),
        "ff": q("SELECT COUNT(*) FROM users WHERE category = 'fulfillment'"),
        "no_cat": q("SELECT COUNT(*) FROM users WHERE category = ''"),
        "paying": q("SELECT COUNT(DISTINCT user_id) FROM payments WHERE status = 'succeeded'"),
        "revenue": revenue,
        # списания минус возвраты за 30 дней
        "spent_month": -q(
            "SELECT COALESCE(SUM(delta), 0) FROM ledger WHERE kind IN ('spend', 'refund') AND created_at >= ?",
            now - 30 * 86400,
        ),
        "due_kop": q("SELECT COALESCE(SUM(amount_kop), 0) FROM ref_rewards WHERE kind = 'money' AND status = 'accrued'"),
        "payments_pending": q("SELECT COUNT(*) FROM payments WHERE status = 'pending'"),
    }


def admin_users(conn: sqlite3.Connection, search: str = "", limit: int = 200) -> list[sqlite3.Row]:
    sql = (
        "SELECT u.*, (SELECT COUNT(*) FROM users r WHERE r.referred_by = u.id) AS invited, "
        "(SELECT COALESCE(SUM(amount_kop), 0) FROM payments p WHERE p.user_id = u.id AND p.status = 'succeeded') AS paid_kop "
        "FROM users u"
    )
    args: list[Any] = []
    if search.strip():
        sql += " WHERE CAST(u.tg_id AS TEXT) LIKE ? OR CAST(u.id AS TEXT) = ? OR u.email LIKE ?"
        s = search.strip()
        args = [f"%{s}%", s, f"%{s}%"]
    sql += " ORDER BY u.id DESC LIMIT ?"
    args.append(limit)
    return conn.execute(sql, args).fetchall()


def admin_payments(conn: sqlite3.Connection, limit: int = 200) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT p.*, u.tg_id FROM payments p JOIN users u ON u.id = p.user_id ORDER BY p.created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()


def admin_rewards(conn: sqlite3.Connection, limit: int = 200) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT r.*, a.tg_id AS referrer_tg, b.tg_id AS referee_tg FROM ref_rewards r "
        "JOIN users a ON a.id = r.referrer_id JOIN users b ON b.id = r.referee_id ORDER BY r.id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def rub(kop: int | None) -> str:
    kop = kop or 0
    s = f"{kop // 100:,}".replace(",", " ")
    return f"{s},{kop % 100:02d} ₽" if kop % 100 else f"{s} ₽"
