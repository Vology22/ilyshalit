"""Сборка документов вывода/возврата КИЗ. Поля по True API: LK_RECEIPT (раздел 4.1.8), LP_RETURN (4.1.5)."""
from __future__ import annotations

from datetime import date
from typing import Any

from . import chz_api


def _primary_fields(primary: dict[str, str], names: tuple[str, str, str, str]) -> dict[str, str]:
    kind, number, day, custom = names
    out = {
        kind: primary["type"],
        number: primary["number"],
        day: primary.get("date") or date.today().isoformat(),
    }
    if primary["type"] == chz_api.PRIMARY_DOC_OTHER:
        out[custom] = primary["custom_name"]
    return out


def build_receipt_doc(
    inn: str,
    products: list[dict[str, Any]],
    primary_document: dict[str, str] | None = None,
    action_date: str | None = None,
    fias_id: str | None = None,
) -> dict[str, Any]:
    """LK_RECEIPT - вывод из оборота при дистанционной продаже (FBS).

    products: [{"cis": "...", "product_cost": копейки}]
    primary_document: {"type", "number", "date", "custom_name"}, при DISTANCE ЧЗ его не требует.
    """
    doc: dict[str, Any] = {
        "inn": inn,
        "action": chz_api.ACTION_DISTANCE,
        "action_date": action_date or date.today().isoformat(),
        "products": [
            {"cis": p["cis"], "product_cost": int(p.get("product_cost", 0))}
            for p in products
        ],
    }
    if fias_id:
        # ЧЗ сверяет место выбытия с зарегистрированным МОД участника, у ИП без КПП
        doc["fias_id"] = fias_id
    if primary_document:
        doc.update(_primary_fields(
            primary_document,
            ("document_type", "document_number", "document_date", "primary_document_custom_name"),
        ))
    return doc


def build_return_doc(
    inn: str,
    products: list[dict[str, Any]],
    primary_document: dict[str, str] | None = None,
) -> dict[str, Any]:
    """LP_RETURN - возврат в оборот после дистанционной продажи.

    products: [{"cis": "...", "paid": True/False, "primary_document": {...}}]
    Для оплаченного товара ЧЗ требует первичный документ, для неоплаченного его быть не должно.
    """
    items = []
    for p in products:
        paid = bool(p.get("paid", True))
        item: dict[str, Any] = {"ki": p["cis"], "paid": paid}
        if paid:
            primary = p.get("primary_document") or primary_document
            if not primary:
                raise ValueError(f"нет первичного документа для оплаченного возврата {p['cis']}")
            item.update(_primary_fields(
                primary,
                ("primary_document_type", "primary_document_number",
                 "primary_document_date", "primary_document_custom_name"),
            ))
        items.append(item)
    return {
        "trade_participant_inn": inn,
        "return_type": chz_api.RETURN_TYPE_REMOTE_SALE,
        "products_list": items,
    }
