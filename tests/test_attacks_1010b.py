"""Атаки на слитую версию 10.10: время финотчета, мусор от WB, частичный документ ЧЗ, двойная отправка."""
from __future__ import annotations

import asyncio

import httpx

from chzkiz import chz_api, kiz, wb_api
from chzkiz.web import app as app_mod
from tests.test_attacks_mod import _code
from tests.test_v2_a1 import _env, _fin, _k, _order, _run
from tests.test_v2_b import _run as _run_step4

FIAS = "0c5b2444-70a0-4932-980c-b4dc0d3f02b5"
_REAL_EXCISE = wb_api.WbClient.get_excise_report
_REAL_FINANCE = wb_api.WbClient.get_finance_report


async def test_saledt_finotcheta_v_utc_granica_dnya_po_moskve(monkeypatch):
    # orderDt финотчета совпадает с createdAt задания (UTC) до секунды на живом кеше, значит saleDt тоже UTC
    _env(
        monkeypatch,
        [_order(1, "R1"), _order(2, "R2", created="2026-09-08T10:00:00Z")],
        {1: "sold", 2: "sold"},
        {1: [1], 2: [2]},
        fin=[
            _fin("R1", "Продажа", "2026-10-09T22:30:00Z"),
            _fin("R2", "Продажа", "2026-09-08T21:30:00Z"),
        ],
    )
    got = await _run()
    assert _k(1) not in got, "продажа 10.10 01:30 по Москве попала в период до 09.10"
    assert _k(2) in got and got[_k(2)]["date"] == "2026-09-09", "продажа 09.09 00:30 по Москве потеряна"


def _musor(monkeypatch, body: str):
    async def fake_request(client, method, url, **kwargs):
        return httpx.Response(200, text=body, request=httpx.Request(method, url))

    monkeypatch.setattr(wb_api, "_request", fake_request)


async def test_aktsiz_otchet_musor_ne_ronyaet_sverku(monkeypatch):
    _env(monkeypatch, [_order(1, "R1")], {1: "sold"}, {1: [1]})
    monkeypatch.setattr(wb_api.WbClient, "get_excise_report", _REAL_EXCISE)
    _musor(monkeypatch, "<html>502 Bad Gateway</html>")
    client = wb_api.WbClient("test-key-not-real")
    got = await _run(client)
    assert _k(1) in got
    assert wb_api.WARN_FAIL_EXCISE in client.warnings


async def test_aktsiz_otchet_response_null_ne_ronyaet_sverku(monkeypatch):
    _env(monkeypatch, [_order(1, "R1")], {1: "sold"}, {1: [1]})
    monkeypatch.setattr(wb_api.WbClient, "get_excise_report", _REAL_EXCISE)
    _musor(monkeypatch, '{"response": null}')
    client = wb_api.WbClient("test-key-not-real")
    got = await _run(client)
    assert _k(1) in got


async def test_finotchet_musor_ne_ronyaet_sverku(monkeypatch):
    _env(monkeypatch, [_order(1, "R1")], {1: "sold"}, {1: [1]})
    monkeypatch.setattr(wb_api.WbClient, "get_finance_report", _REAL_FINANCE)
    _musor(monkeypatch, "<html>502 Bad Gateway</html>")
    client = wb_api.WbClient("test-key-not-real")
    got = await _run(client)
    assert _k(1) in got
    assert wb_api.WARN_FAIL_FINANCE in client.warnings


def test_oshibka_bez_koda_ne_delaet_ostalnye_kody_prinyatymi():
    a, b = kiz.normalize_kiz(_code(1)), kiz.normalize_kiz(_code(2))
    info = {
        "status": chz_api.DOC_STATUS_CHECKED_NOT_OK,
        "errors": [f"{a}: недопустимый статус кода", "Документ не прошел форматно-логический контроль"],
    }
    split = app_mod._partition(info, chz_api.DOC_STATUS_CHECKED_NOT_OK, [a, b])
    assert split is None or b not in split[0], "код b записан принятым, хотя ЧЗ отклонил документ общей ошибкой"


async def test_dvoynoe_otpravit_ne_daet_dvuh_dokumentov(monkeypatch):
    c, run_id, _ = _run_step4(8901)
    r = c.post(f"/step4/{run_id}/build", json={"doc_type": "output", "fias": FIAS})
    assert r.status_code == 200, r.text
    built = r.json()
    calls: list[int] = []

    async def fake_send(client, product_document_b64, doc_type, signature_b64, pg=None):
        calls.append(1)
        await asyncio.sleep(0.2)
        return f"CHZ-DOC-{len(calls)}"

    monkeypatch.setattr(app_mod, "_send_raw_document", fake_send)
    body = {"doc_id": built["doc_id"], "product_document": built["product_document"],
            "chz_type": built["chz_type"], "signature": "U0lH"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=c.app), base_url="http://testserver",
                                 cookies=dict(c.cookies)) as ac:
        r1, r2 = await asyncio.gather(ac.post(f"/step4/{run_id}/send", json=body),
                                      ac.post(f"/step4/{run_id}/send", json=body))
    assert r1.status_code == 200 and r2.status_code == 200
    assert len(calls) == 1, f"в Честный знак ушло документов: {len(calls)}"


async def test_chek_s_odnoy_datoy_ne_perebivaet_vozvrat_togo_zhe_dnya(monkeypatch):
    # продажа накануне, чек датой следующего дня, в тот же день возврат: итог возврат, как у изи чизи
    chek = {"excise_short": _code(1), "fiscal_dt": "2026-09-16", "operation_type_id": 1}
    _env(
        monkeypatch, [_order(1, "R1")], {1: "sold"}, {1: [1]},
        fin=[_fin("R1", "Продажа", "2026-09-15T13:09:00Z"), _fin("R1", "Возврат", "2026-09-16T12:42:00Z")],
        exc=[chek],
    )
    it = (await _run())[_k(1)]
    assert it["op"] == wb_api.OP_RETURN and it["date"] == "2026-09-16"
