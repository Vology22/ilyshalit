"""Баланс сверок, безлимит, тарифы, платежи и реферальные начисления.

Сверка = один запуск, сколько бы документов вывода и возврата в нем ни ушло. Оплачивается
перед отправкой первого документа: по безлимиту, если он покрывает ИНН подписи, иначе одной
сверкой с баланса. Возвращается, если ни один документ не дошел или ЧЗ отклонил все целиком.
Все изменения баланса идут через _move: одна транзакция, запись в ledger с балансом после."""
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
    "spend": "Сверка",
    "unlim": "Сверка по безлимиту",
    "refund": "Возврат: сверка не состоялась",
    "referral": "Бонус за приглашенного",
    "admin": "Корректировка администратором",
}
PAYMENT_LABELS = {"pending": "Ожидает оплаты", "succeeded": "Оплачен", "canceled": "Отменен"}
REWARD_LABELS = {"accrued": "Начислено, к выплате", "paid": "Выплачено", "credited": "Зачислено на баланс"}

NOT_ENOUGH = "Закончились сверки. Купите сверки или безлимит в личном кабинете."
DAY = 86400


class NotEnoughOps(Exception):
    pass


class InnLimit(NotEnoughOps):
    """Безлимит есть, но все его ИНН заняты другими организациями, а сверок на балансе нет."""


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


# --- списание за сверку ------------------------------------------------------------

def inn_limit_text(sub: dict[str, Any]) -> str:
    inns = ", ".join(sub["inns"]) or "-"
    return (
        f"Ваш безлимит на {sub['inn_limit']} ИНН уже занят: {inns}. Эта подпись другой организации. "
        "Войдите подписью одной из этих организаций, перейдите на безлимит с большим числом ИНН "
        "или купите разовые сверки."
    )


def subscription(conn: sqlite3.Connection, user_id: int, now: float | None = None) -> dict[str, Any] | None:
    """Действующий безлимит: лимит ИНН, срок и уже закрепленные ИНН. None - безлимита нет или он истек."""
    row = conn.execute("SELECT * FROM subscriptions WHERE user_id = ?", (user_id,)).fetchone()
    if not row or row["ends_at"] <= (now or time.time()):
        return None
    inns = [r[0] for r in conn.execute(
        "SELECT inn FROM sub_inns WHERE user_id = ? ORDER BY added_at, inn", (user_id,)
    )]
    return {"inn_limit": row["inn_limit"], "ends_at": row["ends_at"], "inns": inns}


def _covers(sub: dict[str, Any] | None, inn: str) -> bool:
    return bool(sub and inn and (inn in sub["inns"] or len(sub["inns"]) < sub["inn_limit"]))


def check_run(conn: sqlite3.Connection, user_id: int, run: Any) -> str:
    """Проверка до подписи, без списания. Пустая строка - сверку можно оплатить, иначе причина отказа."""
    if run["charged"]:
        return ""
    sub = subscription(conn, user_id)
    inn = run["inn"] or ""
    if _covers(sub, inn) or balance(conn, user_id) >= 1:
        return ""
    return inn_limit_text(sub) if sub and inn else NOT_ENOUGH


def charge_run(conn: sqlite3.Connection, user_id: int, run_id: str, inn: str) -> bool:
    """Оплатить сверку: по безлимиту, если он покрывает ИНН подписи, иначе одна сверка с баланса.
    False - сверка уже оплачена (второй документ той же сверки не списывает). NotEnoughOps или
    InnLimit - платить нечем."""
    try:
        cur = conn.execute("UPDATE runs SET charged = 1 WHERE id = ? AND charged = 0", (run_id,))
        if cur.rowcount != 1:
            conn.commit()
            return False
        sub = subscription(conn, user_id)
        if _covers(sub, inn):
            conn.execute(
                "INSERT OR IGNORE INTO sub_inns (user_id, inn, added_at) VALUES (?, ?, ?)", (user_id, inn, time.time())
            )
            conn.execute("UPDATE runs SET charge_kind = 'unlim' WHERE id = ?", (run_id,))
            _move(conn, user_id, 0, "unlim", run_id, f"Сверка по безлимиту, ИНН {inn}")
        else:
            try:
                _move(conn, user_id, -1, "spend", run_id, f"Сверка, ИНН {inn}" if inn else "")
            except NotEnoughOps:
                if sub and inn:
                    raise InnLimit(inn_limit_text(sub)) from None
                raise
            conn.execute("UPDATE runs SET charge_kind = 'ops' WHERE id = ?", (run_id,))
        conn.commit()
        return True
    except BaseException:
        conn.rollback()
        raise


def refund_run(conn: sqlite3.Connection, user_id: int, run_id: str, note: str, exclude_doc: str = "") -> bool:
    """Вернуть сверку, если ни один ее документ не дошел до Честного знака или все отклонены целиком.
    Документ, который ждет решения ЧЗ или прямо сейчас отправляется, держит оплату. Повтор безопасен."""
    busy = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE run_id = ? AND id != ? AND ("
        "(COALESCE(chz_doc_id, '') != '' AND status != 'CHECKED_NOT_OK') OR status = 'SENDING')",
        (run_id, exclude_doc),
    ).fetchone()[0]
    if busy:
        return False
    run = conn.execute("SELECT charge_kind FROM runs WHERE id = ? AND charged = 1", (run_id,)).fetchone()
    if not run:
        return False
    conn.execute("UPDATE runs SET charged = 0, charge_kind = '' WHERE id = ?", (run_id,))
    if run["charge_kind"] == "ops":
        _move(conn, user_id, 1, "refund", run_id, note)
    conn.commit()
    return True


def settle_document(conn: sqlite3.Connection, user_id: int, doc_id: str, chz_doc_id: str) -> None:
    """ЧЗ вынес решение. Документ отклонен целиком - проверяем, не пора ли вернуть сверку:
    если другие ее документы тоже не приняты, сверка не состоялась."""
    if not chz_doc_id:
        return
    statuses = {
        r[0] for r in conn.execute("SELECT status FROM documents WHERE chz_doc_id = ?", (chz_doc_id,))
    }
    if statuses and statuses == {"CHECKED_NOT_OK"}:
        row = conn.execute("SELECT run_id FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if row:
            refund_run(conn, user_id, row["run_id"], "Честный знак отклонил документы сверки")


# --- тарифы ----------------------------------------------------------------------

def tariffs(conn: sqlite3.Connection, active_only: bool = True) -> list[sqlite3.Row]:
    where = "WHERE active = 1" if active_only else ""
    return conn.execute(f"SELECT * FROM tariffs {where} ORDER BY sort, id").fetchall()


def get_tariff(conn: sqlite3.Connection, tariff_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM tariffs WHERE id = ?", (tariff_id,)).fetchone()


def save_tariff(
    conn: sqlite3.Connection, tariff_id: int | None, name: str, ops: int, price_kop: int, active: bool, sort: int,
    kind: str = "pack", inn_limit: int = 0, days: int = 0,
) -> int:
    if kind != "unlim":
        kind, inn_limit, days = "pack", 0, 0
    else:
        ops = 0
    if tariff_id:
        conn.execute(
            "UPDATE tariffs SET name = ?, kind = ?, ops = ?, inn_limit = ?, days = ?, price_kop = ?, active = ?, "
            "sort = ? WHERE id = ?",
            (name, kind, ops, inn_limit, days, price_kop, int(active), sort, tariff_id),
        )
    else:
        cur = conn.execute(
            "INSERT INTO tariffs (name, kind, ops, inn_limit, days, price_kop, active, sort, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, kind, ops, inn_limit, days, price_kop, int(active), sort, time.time()),
        )
        tariff_id = cur.lastrowid
    conn.commit()
    return tariff_id


# --- платежи -----------------------------------------------------------------------

def create_payment(conn: sqlite3.Connection, user_id: int, tariff: sqlite3.Row, provider: str) -> str:
    """Цена и состав тарифа копируются в платеж: смена тарифа админом не меняет уже выставленный счет."""
    payment_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO payments (id, user_id, tariff_id, tariff_name, kind, ops, inn_limit, days, amount_kop, provider, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (payment_id, user_id, tariff["id"], tariff["name"], tariff["kind"], tariff["ops"], tariff["inn_limit"],
         tariff["days"], tariff["price_kop"], provider, time.time()),
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
    """Платеж прошел: сверки на баланс или безлимит, и реферальное начисление. Ровно один раз, даже если
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
        if p["kind"] == "unlim":
            ends = _extend_subscription(conn, p["user_id"], p["inn_limit"], p["days"])
            _move(conn, p["user_id"], 0, "purchase", payment_id, f"{p['tariff_name']}, до {date(ends)}")
        else:
            _move(conn, p["user_id"], p["ops"], "purchase", payment_id, f"Тариф {p['tariff_name']}")
        _reward_referrer(conn, p)
        conn.commit()
        return True
    except BaseException:
        conn.rollback()
        raise


def _extend_subscription(conn: sqlite3.Connection, user_id: int, inn_limit: int, days: int) -> float:
    """Действующий безлимит продлевается от его конца, ИНН сохраняются, лимит берется больший.
    Истекший начинается заново с сегодняшнего дня и с пустым списком ИНН."""
    now = time.time()
    row = conn.execute("SELECT * FROM subscriptions WHERE user_id = ?", (user_id,)).fetchone()
    if row and row["ends_at"] > now:
        ends = row["ends_at"] + days * DAY
        conn.execute(
            "UPDATE subscriptions SET ends_at = ?, inn_limit = MAX(inn_limit, ?) WHERE user_id = ?",
            (ends, inn_limit, user_id),
        )
        return ends
    ends = now + days * DAY
    conn.execute("DELETE FROM sub_inns WHERE user_id = ?", (user_id,))
    conn.execute(
        "INSERT OR REPLACE INTO subscriptions (user_id, inn_limit, starts_at, ends_at) VALUES (?, ?, ?, ?)",
        (user_id, inn_limit, now, ends),
    )
    return ends


def reset_inns(conn: sqlite3.Connection, user_id: int) -> None:
    """Админ освобождает ИНН безлимита (ошиблись подписью); срок не меняется."""
    conn.execute("DELETE FROM sub_inns WHERE user_id = ?", (user_id,))
    conn.commit()


def date(ts: float) -> str:
    return time.strftime("%d.%m.%Y", time.localtime(ts))


def tariff_text(t: Any) -> str:
    """Что дает тариф или платеж: "3 сверки" или "безлимит до 2 ИНН на 30 дней"."""
    if t["kind"] == "unlim":
        return f"безлимит до {t['inn_limit']} ИНН на {t['days']} дней"
    return sverki(t["ops"])


def sverki(n: int) -> str:
    """1 сверка, 3 сверки, 5 сверок."""
    n = int(n or 0)
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} сверка"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} сверки"
    return f"{n} сверок"


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
        # оплаченные сверки за 30 дней, с баланса и по безлимиту
        "spent_month": q("SELECT COUNT(*) FROM runs WHERE charged = 1 AND created_at >= ?", now - 30 * DAY),
        "subs_active": q("SELECT COUNT(*) FROM subscriptions WHERE ends_at > ?", now),
        "due_kop": q("SELECT COALESCE(SUM(amount_kop), 0) FROM ref_rewards WHERE kind = 'money' AND status = 'accrued'"),
        "payments_pending": q("SELECT COUNT(*) FROM payments WHERE status = 'pending'"),
        "support_open": q("SELECT COUNT(*) FROM support_threads WHERE status = 'open'"),
    }


def admin_users(conn: sqlite3.Connection, search: str = "", limit: int = 200) -> list[sqlite3.Row]:
    sql = (
        "SELECT u.*, (SELECT COUNT(*) FROM users r WHERE r.referred_by = u.id) AS invited, "
        "(SELECT COALESCE(SUM(amount_kop), 0) FROM payments p WHERE p.user_id = u.id AND p.status = 'succeeded') AS paid_kop, "
        "s.ends_at AS sub_ends, s.inn_limit AS sub_limit, "
        "(SELECT GROUP_CONCAT(inn, ', ') FROM sub_inns i WHERE i.user_id = u.id) AS sub_inns "
        "FROM users u LEFT JOIN subscriptions s ON s.user_id = u.id"
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
