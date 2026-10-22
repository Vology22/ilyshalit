"""Классификация сверки: пять групп из PLAN.md - вывести, вернуть, уже в порядке, ошибка."""
from __future__ import annotations

from typing import Any

from . import chz_api, kiz
from .wb_api import OP_RETURN, OP_SALE

GROUP_OUTPUT = "output"  # вывести
GROUP_RETURN = "return"  # вернуть
GROUP_OK = "ok"  # уже в порядке
GROUP_ERROR = "error"  # ошибка

GROUP_LABELS = {
    GROUP_OUTPUT: "Вывести",
    GROUP_RETURN: "Вернуть",
    GROUP_OK: "Уже в порядке",
    GROUP_ERROR: "Ошибка",
}

REASON_LABELS = {
    "sale_in_turnover": "продан, в обороте",
    "return_retired": "возвращен, выведен из оборота",
    "already_output": "продан, уже выведен",
    "already_in_turnover": "возвращен, уже в обороте",
    "not_found": "код не найден в ЧЗ",
    "wrong_owner": "чужой ИНН владельца",
    "not_introduced": "код не введен в оборот",
    "bad_format": "битый формат кода",
    "duplicate": "дубль кода",
    "wb_canceled": "у WB отказ покупателя, возврата в отчете нет",
    "not_sold_yet": "еще не продан, выводить рано",
    "otkaz_v_oborote": "отказ покупателя, товар не выкуплен, код в обороте",
    "otkaz_vyveden": "отказ покупателя, код выведен, вернуть в оборот",
    "vozvrat_fin": "покупатель вернул товар, возврат в финотчете",
    "vozvrat_fin_v_oborote": "покупатель вернул товар, возврат в финотчете, код уже в обороте",
    "vozvrat_fin_vyveden": "покупатель вернул товар, возврат в финотчете, код выведен, вернуть в оборот",
    "withdraw_other": "выведен из оборота не как дистанционная продажа, вернуть этим документом нельзя",
}

# возврат REMOTE_SALE_RETURN принимает только коды, выведенные дистанционной продажей или по образцам
RETURNABLE_WITHDRAW = ("DISTANCE", "BY_SAMPLES")

OTKAZ_REASONS = ("otkaz_v_oborote", "otkaz_vyveden")


def classify_items(
    items: list[dict[str, Any]], chz_info: dict[str, dict[str, Any]], our_inn: str
) -> list[dict[str, Any]]:
    """items: [{cis_raw, op, product_cost, date}], chz_info: {нормальный_код: cisInfo}.

    Один и тот же код в одном отчете может встретиться дважды: настоящий дубль - это
    две строки с одинаковой (операция, дата), вторая идет строкой "Ошибка: дубль".
    Продажа и возврат одного кода (или цепочка продажа-возврат-продажа) - это не дубль,
    а сворачивание в одну сделку: побеждает последняя по дате операция, более ранние
    строки просто не попадают в результат (без пометки ошибкой)."""
    by_norm: dict[str, list[int]] = {}
    normed = [kiz.normalize_kiz(it["cis_raw"]) for it in items]
    for idx, norm in enumerate(normed):
        by_norm.setdefault(norm, []).append(idx)

    dropped: set[int] = set()  # заменены более поздней операцией того же кода
    duplicate: set[int] = set()  # настоящий дубль: та же операция и та же дата
    winner_op: dict[int, str] = {}
    for idxs in by_norm.values():
        if len(idxs) < 2:
            continue
        seen_key: dict[tuple[str, str], int] = {}
        remaining: list[int] = []
        for i in idxs:
            key = (items[i]["op"], items[i].get("date") or "")
            if key in seen_key:
                duplicate.add(i)
            else:
                seen_key[key] = i
                remaining.append(i)
        if len(remaining) > 1:
            last_idx = _pick_last_operation(items, remaining)
            winner_op[last_idx] = items[last_idx]["op"]
            for i in remaining:
                if i != last_idx:
                    dropped.add(i)

    result: list[dict[str, Any]] = []
    for idx, it in enumerate(items):
        if idx in dropped:
            continue
        norm = normed[idx]
        row = dict(it, cis_norm=norm, chz_status=None, owner_inn=None)
        if idx in duplicate:
            group, reason = GROUP_ERROR, "duplicate"
        elif it.get("reason") == "wb_canceled" and winner_op.get(idx, it["op"]) == OP_SALE:
            group, reason = GROUP_ERROR, "wb_canceled"
        elif it.get("reason") == "not_sold_yet" and winner_op.get(idx, it["op"]) == OP_SALE:
            group, reason = GROUP_OK, "not_sold_yet"
        else:
            op = winner_op.get(idx, it["op"])
            group, reason = _classify_one(norm, op, chz_info, our_inn)
            if it.get("reason") == "otkaz" and op == OP_RETURN:
                reason = {"already_in_turnover": "otkaz_v_oborote", "return_retired": "otkaz_vyveden"}.get(reason, reason)
            if it.get("reason") == "vozvrat_fin" and op == OP_RETURN:
                reason = {"already_in_turnover": "vozvrat_fin_v_oborote", "return_retired": "vozvrat_fin_vyveden"}.get(reason, reason)
        row["group"] = group
        row["reason"] = reason
        info = chz_info.get(norm)
        if info:
            row["chz_status"] = info.get("status")
            row["owner_inn"] = info.get("ownerInn")
            row["tovar"] = info.get("productName") or ""
        result.append(row)
    return result


def _pick_last_operation(items: list[dict[str, Any]], idxs: list[int]) -> int:
    """Без даты продажа считается раньше возврата (тай-брейк)."""
    def key(i: int) -> tuple[str, int]:
        date = items[i].get("date") or ""
        rank = 0 if items[i]["op"] == OP_SALE else 1
        return (date, rank)

    return max(idxs, key=key)


def _classify_one(
    norm: str,
    op: str,
    chz_info: dict[str, dict[str, Any]],
    our_inn: str,
) -> tuple[str, str]:
    if not kiz.is_well_formed(norm):
        return GROUP_ERROR, "bad_format"

    info = chz_info.get(norm)
    if info is None:
        return GROUP_ERROR, "not_found"

    # чужой владелец мешает только там, где нужно действие: код, уже выведенный WB (РВБ)
    # или оператором ЕАЭС, - это сделанная продажа, а не ошибка (живой отчет 05.10)
    owner_inn = info.get("ownerInn")
    foreign = bool(owner_inn) and owner_inn != our_inn

    status = info.get("status")
    if status not in (chz_api.STATUS_INTRODUCED, chz_api.STATUS_RETIRED):
        return GROUP_ERROR, "wrong_owner" if foreign else "not_introduced"

    if status == chz_api.STATUS_INTRODUCED:
        if op == OP_SALE:
            if foreign:
                return GROUP_ERROR, "wrong_owner"
            return GROUP_OUTPUT, "sale_in_turnover"
        return GROUP_OK, "already_in_turnover"

    # status == STATUS_RETIRED
    if op == OP_RETURN:
        if foreign:
            return GROUP_ERROR, "wrong_owner"
        why = str(info.get("withdrawReason") or "").strip().upper()
        if why and why not in RETURNABLE_WITHDRAW:
            return GROUP_ERROR, "withdraw_other"
        return GROUP_RETURN, "return_retired"
    return GROUP_OK, "already_output"


def counts_by_group(rows: list[dict[str, Any]]) -> dict[str, int]:
    out = {GROUP_OUTPUT: 0, GROUP_RETURN: 0, GROUP_OK: 0, GROUP_ERROR: 0}
    for r in rows:
        if r["group"] in out:
            out[r["group"]] += 1
    return out
