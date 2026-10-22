"""Сверка по API WB: месяц режется на куски по 30 дней, задания в пути считаются как в архиве,
сбой WB не выдается за неверный ключ."""
from __future__ import annotations

import json
import time

import httpx
from fastapi.testclient import TestClient

from chzkiz import config, crypto, db, wb_api
from chzkiz.web.app import create_app
from tests.test_regress_2 import _login

KOD = "010460312345678921abcdefghijklm"


def _podmena(monkeypatch, handler):
    real = httpx.AsyncClient

    def patched(*args, **kwargs):
        if kwargs.get("transport") is None:
            kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)


async def test_mesyac_rezhetsya_na_kuski_po_30_dney(monkeypatch):
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/api/v3/orders":
            return httpx.Response(404, json={})
        p = dict(request.url.params)
        seen.append(p)
        if int(p["dateTo"]) - int(p["dateFrom"]) >= 30 * 86400 or int(p["dateTo"]) > time.time():
            return httpx.Response(400, json={"title": "bad period"})
        return httpx.Response(200, json={"next": 0, "orders": [{"id": len(seen)}]})

    _podmena(monkeypatch, handler)
    seichas = int(time.time())
    orders = await wb_api.WbClient("test-key-not-real").get_orders(seichas - 45 * 86400, seichas + 86400)
    assert [o["id"] for o in orders] == [1, 2], seen
    assert int(seen[0]["dateFrom"]) == seichas - 45 * 86400
    assert int(seen[1]["dateFrom"]) == int(seen[0]["dateTo"]) + 1


async def test_zadaniya_v_puti_idut_v_sverku_kak_v_arhive(monkeypatch):
    """Архив заданий и конкурент считают все задания с кодом, в том числе не доставленные.
    Режим API их выкидывал: за ту же неделю 62 кода вместо 94. Заказ в пути без кода в счет не идет."""
    statusy = {101: "sold", 102: "declined_by_client", 103: "ready_for_pickup", 104: "waiting", 105: "sorted"}
    kody = {101: [KOD[:-1] + "1"], 102: [KOD[:-1] + "2"], 103: [KOD[:-1] + "3"], 104: []}

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/api/v3/orders":
            return httpx.Response(200, json={"next": 0, "orders": [{"id": i, "price": 1000} for i in statusy]})
        if p == "/api/v3/orders/status":
            return httpx.Response(200, json={"orders": [{"id": i, "wbStatus": s} for i, s in statusy.items()]})
        if p == "/api/marketplace/v3/orders/meta":
            nado = json.loads(request.content)["orders"]
            return httpx.Response(200, json={"orders": [
                {"id": i, "meta": {"sgtin": {"value": kody[i]}}} for i in nado if i in kody]})
        return httpx.Response(404, json={})

    _podmena(monkeypatch, handler)
    items = await wb_api.WbClient("test-key-not-real").fetch_items()
    po_zakazu = {it["order_id"]: it for it in items}
    assert sorted(po_zakazu) == [101, 102, 103], items
    assert (po_zakazu[101]["op"], po_zakazu[101].get("reason")) == (wb_api.OP_SALE, None)
    assert (po_zakazu[102]["op"], po_zakazu[102].get("reason")) == (wb_api.OP_RETURN, "otkaz")
    assert (po_zakazu[103]["op"], po_zakazu[103].get("reason")) == (wb_api.OP_SALE, "not_sold_yet")


def _klient(monkeypatch, tg_id, kod_otveta):
    c = TestClient(create_app())
    _login(c, tg_id)
    with db.connect() as conn:
        user = db.get_or_create_user(conn, tg_id)
        db.set_wb_key(conn, user["id"], crypto.encrypt_str("test-key-not-real"))
    monkeypatch.setattr(config, "CHZ_MODE", "real")
    _podmena(monkeypatch, lambda req: httpx.Response(kod_otveta, json={}))
    return c


def test_sboy_wb_ne_vydaetsya_za_plohoy_klyuch(monkeypatch):
    """WB ответил 500 или отказал по периоду: человека нельзя гнать менять рабочий ключ."""
    r = _klient(monkeypatch, 97101, 500).post("/step1", data={"source": "api"}, follow_redirects=False)
    assert "ответ 500" in r.text and "Проверьте ключ" not in r.text, r.text[:300]


def test_otkaz_po_klyuchu_govorit_pro_klyuch(monkeypatch):
    r = _klient(monkeypatch, 97102, 401).post("/step1", data={"source": "api"}, follow_redirects=False)
    assert "WB не принял токен" in r.text, r.text[:300]
