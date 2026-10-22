"""Отчет Excel в виде отчета бота конкурента: листы Изменены и Без изменений."""
from __future__ import annotations

from io import BytesIO

import openpyxl

from chzkiz import chz_api, excel_out, sverka


def _row(i, cis, op, group, reason, status="INTRODUCED"):
    return {"id": i, "cis_raw": cis, "cis_norm": cis, "op": op, "group": group, "reason": reason,
            "chz_status": status, "owner_inn": "7712345678", "product_cost": 159900,
            "date": "2026-09-29", "tovar": "Юбка карго"}


def _listy(rows, docs):
    wb = openpyxl.load_workbook(BytesIO(excel_out.build_report(rows, docs)))
    return {ws.title: [list(r) for r in ws.iter_rows(values_only=True)] for ws in wb.worksheets}


def test_listy_kak_u_konkurenta_i_kody_po_mestam():
    rows = [
        _row(1, "010460000000001521AAA", "sale", sverka.GROUP_OUTPUT, "sale_in_turnover"),
        _row(2, "010460000000001521BBB", "return", sverka.GROUP_RETURN, "return_retired", "RETIRED"),
        _row(3, "010460000000001521CCC", "sale", sverka.GROUP_OK, "already_output", "RETIRED"),
        _row(4, "010460000000001521DDD", "sale", sverka.GROUP_OUTPUT, "sale_in_turnover"),
    ]
    docs = [{"doc_type": chz_api.DOC_TYPE_RECEIPT, "status": chz_api.DOC_STATUS_CHECKED_OK,
             "codes_json": '["010460000000001521AAA"]'},
            {"doc_type": chz_api.DOC_TYPE_RETURN, "status": chz_api.DOC_STATUS_CHECKED_OK,
             "codes_json": '["010460000000001521BBB"]'}]
    listy = _listy(rows, docs)
    assert list(listy)[:3] == ["Изменены", "Без изменений", "Сводка"]
    izm = listy["Изменены"]
    assert izm[1][:6] == ["КИЗ", "ИНН владельца", "Склад", "Статус в отчете", "Статус в ЧЗ", "Новый статус"]
    assert {(r[0], r[5]) for r in izm[2:]} == {("010460000000001521AAA", "Выбыл из оборота"),
                                              ("010460000000001521BBB", "В обороте")}
    assert izm[2][6] == "Юбка карго" and izm[2][7] == 1599
    bez = {r[0]: r[5] for r in listy["Без изменений"][2:]}
    assert bez == {"010460000000001521CCC": "продан, уже выведен",
                   "010460000000001521DDD": "документ в Честный знак не отправлялся"}


def test_otklonennyy_vyvod_ne_v_izmenennyh():
    rows = [_row(1, "010460000000001521AAA", "sale", sverka.GROUP_OUTPUT, "sale_in_turnover")]
    docs = [{"doc_type": chz_api.DOC_TYPE_RECEIPT, "status": chz_api.DOC_STATUS_CHECKED_NOT_OK,
             "codes_json": '["010460000000001521AAA"]', "errors": "недопустимый статус"}]
    listy = _listy(rows, docs)
    assert listy["Изменены"][2:] == []
    assert listy["Без изменений"][2][5] == "недопустимый статус"


def test_nazvanie_tovara_iz_chz_popadaet_v_stroku():
    items = [{"cis_raw": "010460000000001521AAA", "op": "sale", "product_cost": 100, "date": ""}]
    norm = items[0]["cis_raw"]
    info = {norm: {"status": "INTRODUCED", "ownerInn": "7712345678", "productName": "Жилет"}}
    rows = sverka.classify_items(items, info, "7712345678")
    assert rows[0]["tovar"] == "Жилет"
