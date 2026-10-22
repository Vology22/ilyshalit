"""Синтетическая выжимка 20-30 строк, повторяющая разметку реального отчета WB (26.09.2026):
заголовок "Тип документа" (не "Обоснование для оплаты", там тоже встречается "Продажа"/"Возврат"),
логистика дублирует КИЗ строки продажи без своего "Тип документа", GS иногда как текст "\\u001d".
Настоящий файл клиента в репозиторий не кладем - только этот сгенерированный аналог его разметки.
"""
from __future__ import annotations

import zipfile
from io import BytesIO

import openpyxl
import pytest

from chzkiz import excel_in

N_COLS = 60
COL_TIP_DOKUMENTA = 9
COL_OBOSNOVANIE = 10
COL_STIKER = 43
COL_SBOROCHNOE = 53
COL_KOD_MARKIROVKI = 54
COL_SRID = 56


def _headers() -> list[str]:
    headers = [f"Колонка {i}" for i in range(N_COLS)]
    headers[COL_TIP_DOKUMENTA] = "Тип документа"
    headers[COL_OBOSNOVANIE] = "Обоснование для оплаты"
    headers[COL_STIKER] = "Стикер МП"
    headers[COL_SBOROCHNOE] = "Номер сборочного задания"
    headers[COL_KOD_MARKIROVKI] = "Код маркировки"
    headers[COL_SRID] = "Srid"
    return headers


def _row(tip="", obosnovanie="", kiz_val="", sborochnoe="", srid="") -> list[str]:
    row = [""] * N_COLS
    row[COL_TIP_DOKUMENTA] = tip
    row[COL_OBOSNOVANIE] = obosnovanie
    row[COL_KOD_MARKIROVKI] = kiz_val
    row[COL_SBOROCHNOE] = sborochnoe
    row[COL_SRID] = srid
    return row


def make_report(rows: list[list[str]]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(_headers())
    for r in rows:
        ws.append(r)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


GS1_GTIN = "04600000000017"  # тот же 14-значный GTIN, что и в test_attacks.py


def gs1(serial: str, escape: str = "literal") -> str:
    gs = "\x1d" if escape == "byte" else "\\u001d"
    return f"01{GS1_GTIN}21{serial}{gs}91EE01{gs}92AbCdEf=="


def test_parse_report_finds_sale_with_kiz():
    content = make_report([_row(tip="Продажа", kiz_val=gs1("100001"), sborochnoe="СЗ-1")])
    items = excel_in.parse_report(content)
    assert len(items) == 1
    assert items[0]["op"] == "sale"


def test_parse_report_finds_return_with_kiz():
    content = make_report([_row(tip="Возврат", kiz_val=gs1("100002"), sborochnoe="СЗ-2")])
    items = excel_in.parse_report(content)
    assert len(items) == 1
    assert items[0]["op"] == "return"


def test_parse_report_literal_gs_escape_preserved_as_raw():
    content = make_report([_row(tip="Продажа", kiz_val=gs1("100003", escape="literal"))])
    items = excel_in.parse_report(content)
    assert "\\u001d" in items[0]["cis_raw"]


def test_parse_report_skips_logistics_row_without_doc_type():
    """Строка логистики: КИЗ заполнен, "Тип документа" пуст - это не операция, пропускаем.
    До правки такая строка ошибочно считалась бы продажей."""
    content = make_report(
        [
            _row(tip="Продажа", kiz_val=gs1("100004"), sborochnoe="СЗ-4"),
            _row(tip="", obosnovanie="Доставка", kiz_val=gs1("100005"), sborochnoe="СЗ-4"),
        ]
    )
    items = excel_in.parse_report(content)
    assert len(items) == 1
    assert items[0]["cis_raw"] == gs1("100004")


def test_parse_report_ignores_obosnovanie_column_values():
    """"Обоснование для оплаты" тоже содержит слово "Продажа" - не должно приниматься
    за операцию, если "Тип документа" при этом пуст."""
    content = make_report(
        [_row(tip="", obosnovanie="Продажа", kiz_val=gs1("100006"), sborochnoe="СЗ-6")]
    )
    items = excel_in.parse_report(content)
    assert items == []


def test_parse_report_dedup_same_kiz_same_op_across_logistics_lines():
    """Одна продажа с логистикой делит один КИЗ на несколько строк - дедуп по (код, операция)."""
    code = gs1("100007")
    content = make_report(
        [
            _row(tip="Продажа", kiz_val=code, sborochnoe="СЗ-7"),
            _row(tip="Продажа", kiz_val=code, sborochnoe="СЗ-7"),
        ]
    )
    items = excel_in.parse_report(content)
    assert len(items) == 1


def test_parse_report_skips_rows_without_kiz():
    content = make_report(
        [
            _row(tip="Продажа", kiz_val="", sborochnoe="СЗ-8"),
            _row(tip="Продажа", kiz_val=gs1("100009"), sborochnoe="СЗ-9"),
        ]
    )
    items = excel_in.parse_report(content)
    assert len(items) == 1


def test_parse_report_mixed_batch_like_real_report():
    """20+ строк одним файлом: продажи, возвраты, логистика-дубли, пустые КИЗ."""
    rows = []
    for i in range(10):
        code = gs1(f"2{i:05d}")
        rows.append(_row(tip="Продажа", kiz_val=code, sborochnoe=f"СЗ-{i}"))
        rows.append(_row(tip="", obosnovanie="Логистика", kiz_val=code, sborochnoe=f"СЗ-{i}"))
    for i in range(5):
        rows.append(_row(tip="Возврат", kiz_val=gs1(f"3{i:05d}"), sborochnoe=f"СЗ-в{i}"))
    for i in range(6):
        rows.append(_row(tip="", obosnovanie="Хранение", kiz_val="", sborochnoe=f"СЗ-х{i}"))
    content = make_report(rows)
    items = excel_in.parse_report(content)
    assert len(items) == 15  # 10 продаж + 5 возвратов, логистика и пустые КИЗ не считаются
    assert sum(1 for it in items if it["op"] == "sale") == 10
    assert sum(1 for it in items if it["op"] == "return") == 5


def test_missing_kiz_column_raises_column_error():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Тип документа", "Прочее"])
    ws.append(["Продажа", "x"])
    buf = BytesIO()
    wb.save(buf)
    with pytest.raises(excel_in.ExcelColumnError):
        excel_in.parse_report(buf.getvalue())


def test_empty_file_raises_column_error():
    wb = openpyxl.Workbook()
    buf = BytesIO()
    wb.save(buf)
    with pytest.raises(excel_in.ExcelColumnError):
        excel_in.parse_report(buf.getvalue())


# --- zip-обертка отчета (WB иногда отдает архивом) --------------------------------------

def _xlsx_bytes() -> bytes:
    content = make_report([_row(tip="Продажа", kiz_val=gs1("500001"), sborochnoe="СЗ-1")])
    return content


def _zip_of(members: dict[str, bytes]) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_unwrap_xlsx_from_zip_single_member_ok():
    zip_bytes = _zip_of({"report.xlsx": _xlsx_bytes()})
    xlsx_bytes = excel_in.unwrap_xlsx_from_zip(zip_bytes)
    items = excel_in.parse_report(xlsx_bytes)
    assert len(items) == 1 and items[0]["op"] == "sale"


def test_unwrap_xlsx_from_zip_cp866_name_ok():
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        # ZipInfo хранит filename как cp437-строку без юникод-флага (легаси zip); имя, что
        # кладет неюникодный cp866-архив, при чтении через cp437 выглядит как эта строка.
        cp866_name = "отчет.xlsx".encode("cp866").decode("cp437")
        info = zipfile.ZipInfo(cp866_name)
        info.flag_bits &= ~0x800  # без юникод-флага - как реальный cp866 архив
        z.writestr(info, _xlsx_bytes())
    xlsx_bytes = excel_in.unwrap_xlsx_from_zip(buf.getvalue())
    items = excel_in.parse_report(xlsx_bytes)
    assert len(items) == 1


def test_unwrap_xlsx_from_zip_bomb_by_ratio_rejected():
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("report.xlsx", b"0" * 20_000_000)
    with pytest.raises(excel_in.ExcelColumnError):
        excel_in.unwrap_xlsx_from_zip(buf.getvalue())


def test_unwrap_xlsx_from_zip_multiple_xlsx_rejected():
    zip_bytes = _zip_of({"a.xlsx": _xlsx_bytes(), "b.xlsx": _xlsx_bytes()})
    with pytest.raises(excel_in.ExcelColumnError):
        excel_in.unwrap_xlsx_from_zip(zip_bytes)


@pytest.mark.parametrize("name", ["../evil.xlsx", "/etc/evil.xlsx", "a/../../b.xlsx"])
def test_unwrap_xlsx_from_zip_unsafe_path_rejected(name):
    zip_bytes = _zip_of({name: _xlsx_bytes()})
    with pytest.raises(excel_in.ExcelColumnError):
        excel_in.unwrap_xlsx_from_zip(zip_bytes)


def test_unwrap_xlsx_from_zip_no_xlsx_member_rejected():
    zip_bytes = _zip_of({"report.txt": b"not xlsx"})
    with pytest.raises(excel_in.ExcelColumnError):
        excel_in.unwrap_xlsx_from_zip(zip_bytes)


def test_collect_xlsx_nested_and_plain():
    rep = _xlsx_bytes()
    inner = _zip_of({"vykup.xlsx": rep})
    outer = _zip_of({"osn/848854273.zip": inner, "848854275.xlsx": rep, "shot.png": b"png"})
    got = excel_in.collect_xlsx("archive.zip", outer)
    assert sorted(n for n, _ in got) == ["848854275.xlsx", "vykup.xlsx"]
    assert excel_in.collect_xlsx("r.xlsx", rep) == [("r.xlsx", rep)]


def test_collect_xlsx_zip_without_reports_rejected():
    with pytest.raises(excel_in.ExcelColumnError):
        excel_in.collect_xlsx("a.zip", _zip_of({"shot.png": b"png"}))


def test_collect_xlsx_too_deep_rejected():
    z = _zip_of({"r.xlsx": _xlsx_bytes()})
    for i in range(excel_in.ZIP_MAX_DEPTH + 1):
        z = _zip_of({f"l{i}.zip": z})
    with pytest.raises(excel_in.ExcelColumnError):
        excel_in.collect_xlsx("deep.zip", z)


def test_parse_report_fbs_row_without_kiz_needs_meta():
    data = make_report(
        [
            _row("Продажа", "Продажа", "", "5812549572"),
            _row("Возврат", "Возврат", "", "5775440075"),
            _row("", "Логистика", "", "5812549572"),
        ]
    )
    items = excel_in.parse_report(data)
    assert [(it["order_id"], it["op"], it["need_meta"]) for it in items] == [
        (5812549572, excel_in.OP_SALE, True),
        (5775440075, excel_in.OP_RETURN, True),
    ]
    assert all(it["cis_raw"] == "" for it in items)


def test_parse_report_skips_pvz_compensation_typed_as_sale():
    data = make_report([_row("Продажа", "Возмещение за выдачу и возврат товаров на ПВЗ", "", "5812549572")])
    assert excel_in.parse_report(data) == []


def test_parse_report_marks_fbo_row_with_kiz():
    data = make_report([_row("Продажа", "Продажа", gs1("fbo1"), "")])
    items = excel_in.parse_report(data)
    assert len(items) == 1 and items[0]["fbo"] is True


def test_fill_fbs_codes_by_order_meta(monkeypatch):
    import asyncio

    from chzkiz.web import app as app_mod

    class FakeClient:
        def __init__(self, key):
            assert key == "ключ"

        async def get_orders_meta(self, ids):
            assert ids == [111, 222]
            return {111: {"sgtin": {"value": ["код1", "код2"]}}}

    monkeypatch.setattr(app_mod.wb_api, "WbClient", FakeClient)
    monkeypatch.setattr(app_mod, "_decrypt", lambda v: "ключ")
    base = {"op": excel_in.OP_SALE, "product_cost": 0, "date": ""}
    items = [
        {**base, "cis_raw": "", "order_id": 111, "need_meta": True},
        {**base, "cis_raw": "", "order_id": 222, "need_meta": True},
        {**base, "cis_raw": "X"},
    ]
    out, failed = asyncio.run(app_mod._fill_fbs_codes("enc", items))
    assert [it["cis_raw"] for it in out] == ["код1", "код2", "X"]
    assert failed == 1


def test_mark_wb_canceled_only_sales_without_return(monkeypatch):
    import asyncio

    from chzkiz.web import app as app_mod

    class FakeClient:
        def __init__(self, key):
            assert key == "ключ"

        async def get_orders_status(self, ids):
            assert ids == [111, 222]
            return {111: {"wbStatus": "canceled_by_client"}, 222: {"wbStatus": "sold"}}

    monkeypatch.setattr(app_mod.wb_api, "WbClient", FakeClient)
    monkeypatch.setattr(app_mod, "_decrypt", lambda v: "ключ")
    base = {"product_cost": 0, "date": ""}
    items = [
        {**base, "cis_raw": "к1", "op": excel_in.OP_SALE, "order_id": 111},
        {**base, "cis_raw": "к2", "op": excel_in.OP_SALE, "order_id": 222},
        {**base, "cis_raw": "к3", "op": excel_in.OP_SALE, "order_id": 333},
        {**base, "cis_raw": "к3", "op": excel_in.OP_RETURN, "order_id": 333},
        {**base, "cis_raw": "к4", "op": excel_in.OP_SALE},
    ]
    out = asyncio.run(app_mod._mark_wb_canceled("enc", items))
    assert [it.get("reason") for it in out] == ["wb_canceled", None, None, None, None]
