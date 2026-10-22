from __future__ import annotations

import json
import time

import httpx
import pytest

from chzkiz import chz_api, chz_errors, chz_mock, kiz, sverka, wb_api

INN = chz_mock.DEMO_OUR_INN
TAIL = "\x1d91EE01\x1d92" + "A" * 44


def _patch_wb(monkeypatch, orders, statusy, codes):
    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/api/v3/orders":
            return httpx.Response(200, json={"next": 0, "orders": orders})
        if p == "/api/v3/orders/status":
            return httpx.Response(200, json={"orders": [{"id": i, "wbStatus": s} for i, s in statusy.items()]})
        if p == "/api/marketplace/v3/orders/meta":
            nado = json.loads(request.content)["orders"]
            return httpx.Response(200, json={"orders": [
                {"id": i, "meta": {"sgtin": {"value": [codes[i]]}}} for i in nado]})
        return httpx.Response(404, json={})

    def patched(*args, **kwargs):
        if kwargs.get("transport") is None:
            kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)


async def _items(monkeypatch, sale_at, cancel_at, sale_code, cancel_code, sale_id=501, cancel_id=502):
    orders = [{"id": sale_id, "price": 1000, "createdAt": sale_at},
              {"id": cancel_id, "price": 1000, "createdAt": cancel_at}]
    _patch_wb(monkeypatch, orders, {sale_id: "sold", cancel_id: "canceled_by_client"},
              {sale_id: sale_code, cancel_id: cancel_code})
    return await wb_api.WbClient("test-key-not-real").fetch_items(date_from=wb_api.msk_day_unix("2026-09-01"), date_to=wb_api.msk_day_unix("2026-10-09", end=True))


# отказ ПОЗЖЕ продажи должен остаться: строки времени сравниваются как текст
@pytest.mark.parametrize("sale_at, cancel_at", [
    ("2026-09-22T10:00Z", "2026-09-22T10:00:30Z"),            # без секунд против с секундами
    ("2026-09-22T12:00:00+03:00", "2026-09-22T10:00:00Z"),     # 09:00Z продажа, 10:00Z отказ
    ("2026-09-22T10:00:00Z", "2026-09-22T10:00:00.900Z"),      # отказ на 0.9 с позже
])
async def test_atk_cancel_after_sale_kept_whatever_time_format(monkeypatch, sale_at, cancel_at):
    code = "010460312345678921ATK1010A" + TAIL
    items = await _items(monkeypatch, sale_at, cancel_at, code, code)
    assert wb_api.OP_RETURN in [it["op"] for it in items], items


# тот же код в двух заказах пришел в разном виде: отказ раньше продажи должен уйти
@pytest.mark.parametrize("sale_code, cancel_code", [
    ("010460312345678921ATK1010B", "010460312345678921ATK1010B" + TAIL),
    ("010460312345678921ATK1010B" + TAIL.replace("\x1d", "\\u001d"), "010460312345678921ATK1010B" + TAIL),
])
async def test_atk_resale_with_other_code_form_drops_cancel(monkeypatch, sale_code, cancel_code):
    items = await _items(monkeypatch, "2026-09-22T11:00:00Z", "2026-09-22T09:00:00Z", sale_code, cancel_code,
                       sale_id=502, cancel_id=501)
    norm = kiz.normalize_kiz(sale_code)
    assert norm == kiz.normalize_kiz(cancel_code)
    info = {norm: {"cis": norm, "status": chz_api.STATUS_INTRODUCED, "ownerInn": INN}}
    rows = sverka.classify_items(items, info, INN)
    assert [r["group"] for r in rows] == [sverka.GROUP_OUTPUT], rows


# общий ключ "не найден" стоит раньше места деятельности и подписи
@pytest.mark.parametrize("text, expected", [
    ("Место осуществления деятельности с указанным кодом ФИАС не найдено", "место деятельности"),
    ("Сертификат подписи не найден", "флешку"),
])
def test_atk_explain_specific_rule_beats_not_found(text, expected):
    assert expected in chz_errors.explain(text), chz_errors.explain(text)


# ---- атаки на пакет 10.10: архив, финотчет, страницы ошибок ------------------------------------

from chzkiz import excel_in  # noqa: E402
from chzkiz.web import app as app_mod  # noqa: E402
from tests.test_attacks_mod import _client, _code, _fin, _upload  # noqa: E402
from tests.test_fix_1010 import _FakeWb, _arch_real, _with_key, real  # noqa: E402,F401


# старый возврат по заданию 1 из финотчета, тот же код позже продан заданием 2 (архив свежее отчета)
def test_atk_staryy_vozvrat_drugogo_zadaniya_ne_otmenyaet_novuyu_prodazhu(real):
    f = _fin([("Продажа", _code(71), 1, "500", "2026-10-01"), ("Возврат", _code(71), 1, "500", "2026-10-03")])
    a = _arch_real([(2, _code(71), "500", "Продано", "Продажа", "11:51:00 07.10.2026")])
    _, rows = _upload(_client(9701), [("report.xlsx", f), ("archive.xlsx", a)])
    assert [(r["op"], r["reason"]) for r in rows] == [("sale", "")], rows


def test_atk_staryy_vozvrat_drugogo_zadaniya_s_klyuchom_wb(real, monkeypatch):
    c = _with_key(monkeypatch, 9702)
    monkeypatch.setitem(_FakeWb.table, 8, "sold")
    f = _fin([("Продажа", _code(72), 7, "500", "2026-10-01"), ("Возврат", _code(72), 7, "500", "2026-10-03")])
    a = _arch_real([(8, _code(72), "500", "Продано", "Продажа", "11:51:00 07.10.2026")])
    _, rows = _upload(c, [("report.xlsx", f), ("archive.xlsx", a)])
    assert [(r["op"], r["reason"]) for r in rows] == [("sale", "")], rows


# отказ по заданию 1, тот же код потом продан заданием 2: в архиве обе строки
def test_atk_arhiv_otkaz_potom_prodazha_togo_zhe_koda_bez_klyucha(real):
    a = _arch_real([(1, _code(73), "500", "Отказ покупателем", "Возврат", "10:00:00 01.10.2026"),
                    (2, _code(73), "500", "Продано", "Продажа", "11:51:00 07.10.2026")])
    _, rows = _upload(_client(9703), [("archive.xlsx", a)])
    assert ("sale", "") in [(r["op"], r["reason"]) for r in rows], rows


def test_atk_parse_report_teryaet_vtoroe_zadanie_togo_zhe_koda():
    a = _arch_real([(1, _code(74), "500", "Отказ покупателем", "Возврат", "10:00:00 01.10.2026"),
                    (2, _code(74), "500", "Продано", "Продажа", "11:51:00 07.10.2026")])
    assert sorted(it["order_id"] for it in excel_in.parse_report(a)) == [2]


# финотчет подтверждает повторную продажу, а строка с отказом остается возвратом
def test_atk_prodazha_v_finotchete_posle_otkaza_v_arhive(real):
    f = _fin([("Продажа", _code(75), 2, "500", "2026-10-07")])
    a = _arch_real([(1, _code(75), "500", "Отказ покупателем", "-", None)])
    _, rows = _upload(_client(9704), [("report.xlsx", f), ("archive.xlsx", a)])
    assert [r["op"] for r in rows] == ["sale"], rows


# два кода в одном задании, в финотчете FBS-строки без кода: вернули один экземпляр
@pytest.mark.xfail(reason="в WB одно сборочное задание это один товар", strict=False)
def test_atk_dva_koda_odnogo_zadaniya_odin_vozvrat(real):
    f = _fin([("Продажа", "", 5, "500", "2026-10-01"), ("Продажа", "", 5, "500", "2026-10-01"),
              ("Возврат", "", 5, "500", "2026-10-03")])
    a = _arch_real([(5, _code(76), "500", "Продано", "Продажа", None),
                    (5, _code(77), "500", "Продано", "Продажа", None)])
    _, rows = _upload(_client(9705), [("report.xlsx", f), ("archive.xlsx", a)])
    assert sorted(r["op"] for r in rows) == ["return", "sale"], rows


@pytest.mark.parametrize("text", ["Invalid documentDate", "Invalid signatures", "Field productCost is empty"])
def test_atk_explain_latinskie_markery_v_slitnom_napisanii(text):
    assert chz_errors.UNKNOWN not in chz_errors.explain(text), chz_errors.explain(text)
