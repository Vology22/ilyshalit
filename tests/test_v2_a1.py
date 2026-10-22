"""Сверка по API WB по правилу изи чизи: события кода, период по Москве, финотчет, предупреждения."""
from __future__ import annotations

import httpx
import pytest

from chzkiz import config, crypto, db, wb_api
from tests.test_attacks_mod import _client, _signin

GS = chr(29)
PERIOD = ("2026-09-09", "2026-10-09")


def _code(n: int) -> str:
    return f"0104600000000{n:03d}21ABCDE{n:04d}{GS}91EE11{GS}92ZZZZ{n:04d}"


def _k(n: int) -> str:
    return wb_api._kod31(_code(n))


def _order(oid, rid, created="2026-09-12T10:00:00Z", price=150000):
    return {"id": oid, "rid": rid, "createdAt": created, "price": price}


def _fin(srid, oper, dt, shk=None, rrd=1):
    return {"srid": srid, "sellerOperName": oper, "saleDt": dt, "shkId": shk, "rrdId": rrd}


def _env(monkeypatch, orders, statuses, codes, fin=(), exc=()):
    """Подменяет все запросы WbClient данными из теста. codes: {номер задания: [номер кода]}."""
    wb_api._FIN_CACHE.clear()
    st = {oid: {"supplierStatus": "complete", "wbStatus": s} for oid, s in statuses.items()}
    meta = {oid: {"sgtin": {"value": [_code(n) for n in ns]}} for oid, ns in codes.items()}

    async def get_orders(self, date_from, date_to=None):
        return list(orders)

    async def get_status(self, ids):
        return st

    async def get_meta(self, ids):
        return meta

    async def get_fin(self, date_from, date_to):
        return list(fin)

    async def get_exc(self, date_from, date_to, countries=None):
        return list(exc)

    monkeypatch.setattr(wb_api.WbClient, "get_orders", get_orders)
    monkeypatch.setattr(wb_api.WbClient, "get_orders_status", get_status)
    monkeypatch.setattr(wb_api.WbClient, "get_orders_meta", get_meta)
    monkeypatch.setattr(wb_api.WbClient, "get_finance_report", get_fin)
    monkeypatch.setattr(wb_api.WbClient, "get_excise_report", get_exc)


async def _run(client=None):
    client = client or wb_api.WbClient("test-key-not-real")
    df = wb_api.msk_day_unix(PERIOD[0])
    dt = wb_api.msk_day_unix(PERIOD[1], end=True)
    items = await client.fetch_items(date_from=df, date_to=dt)
    return {it["cis_raw"]: it for it in items if not it.get("meta_failed")}


async def test_resale_by_shk_id(monkeypatch):
    _env(
        monkeypatch,
        [_order(1, "R1"), _order(2, "R2")],
        {1: "sold", 2: "sold"},
        {1: [1], 2: [2]},
        fin=[
            _fin("R1", "Продажа", "2026-09-12T11:00:00Z", shk=77),
            _fin("R9", "Продажа", "2026-09-20T12:00:00Z", shk=77),
            _fin("R2", "Продажа", "2026-09-12T11:00:00Z", shk=88),
            _fin("R8", "Возврат", "2026-09-22T12:00:00Z", shk=88),
        ],
    )
    got = await _run()
    assert (got[_k(1)]["op"], got[_k(1)]["date"], got[_k(1)]["istochnik"]) == ("sale", "2026-09-20", "перепродажа")
    assert (got[_k(2)]["op"], got[_k(2)]["date"], got[_k(2)]["istochnik"]) == ("return", "2026-09-22", "перепродажа")
    assert got[_k(1)]["order_id"] == 1 and got[_k(1)]["product_cost"] == 150000


async def test_return_after_sale_is_return(monkeypatch):
    _env(
        monkeypatch, [_order(1, "R1")], {1: "sold"}, {1: [1]},
        fin=[_fin("R1", "Продажа", "2026-09-12T11:00:00Z"), _fin("R1", "Возврат", "2026-09-15T11:00:00Z")],
    )
    it = (await _run())[_k(1)]
    assert (it["op"], it["date"], it["istochnik"], it["reason"]) == ("return", "2026-09-15", "финотчет", "vozvrat_fin")


async def test_sale_after_return_is_sale(monkeypatch):
    _env(
        monkeypatch, [_order(1, "R1")], {1: "sold"}, {1: [1]},
        fin=[_fin("R1", "Возврат", "2026-09-12T11:00:00Z"), _fin("R1", "Продажа", "2026-09-15T11:00:00Z")],
    )
    it = (await _run())[_k(1)]
    assert (it["op"], it["date"], it["istochnik"]) == ("sale", "2026-09-15", "финотчет")


async def test_marking_receipt_is_sale(monkeypatch):
    chek = {"excise_short": _code(1), "fiscal_dt": "2026-09-20", "operation_type_id": 1}
    chuzhoy = {"excise_short": _code(2), "fiscal_dt": "2026-09-20", "operation_type_id": 2}
    _env(monkeypatch, [_order(1, "R1"), _order(2, "R2")], {1: "waiting", 2: "waiting"}, {1: [1], 2: [2]},
         exc=[chek, chuzhoy])
    got = await _run()
    assert (got[_k(1)]["op"], got[_k(1)]["date"], got[_k(1)]["istochnik"]) == ("sale", "2026-09-20", "чек")
    assert got[_k(2)]["reason"] == "not_sold_yet" and got[_k(2)]["istochnik"] == "задание"


async def test_defect_is_return(monkeypatch):
    _env(monkeypatch, [_order(1, "R1")], {1: "defect"}, {1: [1]})
    it = (await _run())[_k(1)]
    assert (it["op"], it["istochnik"], it["reason"]) == ("return", "задание", "otkaz")


async def test_sold_without_finance_row_is_sale_by_created(monkeypatch):
    _env(monkeypatch, [_order(1, "R1", created="2026-09-12T10:00:00Z")], {1: "sold"}, {1: [1]})
    it = (await _run())[_k(1)]
    assert (it["op"], it["date"], it["istochnik"]) == ("sale", "2026-09-12", "задание")


async def test_moscow_day_border(monkeypatch):
    orders = [
        _order(1, "R1", created="2026-09-08T22:30:00Z"),
        _order(2, "R2", created="2026-09-08T20:30:00Z"),
        _order(3, "R3", created="2026-10-09T20:59:00Z"),
        _order(4, "R4", created="2026-10-09T21:30:00Z"),
    ]
    _env(monkeypatch, orders, {i: "defect" for i in (1, 2, 3, 4)}, {i: [i] for i in (1, 2, 3, 4)})
    got = await _run()
    assert got[_k(1)]["date"] == "2026-09-09"
    assert got[_k(3)]["date"] == "2026-10-09"
    assert _k(2) not in got and _k(4) not in got


async def test_finance_403_warns_and_does_not_fail(monkeypatch):
    _env(monkeypatch, [_order(1, "R1")], {1: "sold"}, {1: [1]})

    async def denied(self, date_from, date_to):
        raise httpx.HTTPStatusError("403", request=httpx.Request("POST", "http://x"), response=httpx.Response(403))

    monkeypatch.setattr(wb_api.WbClient, "get_finance_report", denied)
    client = wb_api.WbClient("test-key-not-real")
    got = await _run(client)
    assert got[_k(1)]["op"] == "sale"
    assert client.warnings == [wb_api.WARN_NET_FINANCE]
    assert "Финансы" in client.warnings[0]


async def test_excise_403_warns_about_receipts(monkeypatch):
    _env(monkeypatch, [_order(1, "R1")], {1: "sold"}, {1: [1]})

    async def denied(self, date_from, date_to, countries=None):
        raise httpx.HTTPStatusError("401", request=httpx.Request("POST", "http://x"), response=httpx.Response(401))

    monkeypatch.setattr(wb_api.WbClient, "get_excise_report", denied)
    client = wb_api.WbClient("test-key-not-real")
    await _run(client)
    assert client.warnings == [wb_api.WARN_NET_EXCISE]


def _patch_transport(monkeypatch, handler):
    real = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)


async def test_finance_retries_after_429(monkeypatch):
    wb_api._FIN_CACHE.clear()
    pauses: list[float] = []
    calls: list[int] = []

    async def no_sleep(seconds):
        pauses.append(seconds)

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429)
        if len(calls) == 2:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json=[_fin("R1", "Продажа", "2026-09-12T11:00:00Z")])

    monkeypatch.setattr(wb_api, "_pause", no_sleep)
    _patch_transport(monkeypatch, handler)
    rows = await wb_api.WbClient("test-key-not-real").get_finance_report("2026-07-11", "2026-10-09")
    assert len(rows) == 1 and len(calls) == 3
    assert pauses == [65.0, 7.0]


async def test_finance_gives_up_after_three_retries(monkeypatch):
    wb_api._FIN_CACHE.clear()
    calls: list[int] = []

    async def no_sleep(seconds):
        pass

    def handler(request):
        calls.append(1)
        return httpx.Response(429)

    monkeypatch.setattr(wb_api, "_pause", no_sleep)
    _patch_transport(monkeypatch, handler)
    with pytest.raises(httpx.HTTPStatusError):
        await wb_api.WbClient("test-key-not-real").get_finance_report("2026-07-11", "2026-10-09")
    assert len(calls) == 4


async def test_finance_second_page_when_limit_rows(monkeypatch):
    wb_api._FIN_CACHE.clear()
    monkeypatch.setattr(wb_api, "FIN_LIMIT", 2)
    bodies: list[dict] = []

    def handler(request):
        import json as _json

        body = _json.loads(request.content)
        bodies.append(body)
        if "rrdid" not in body:
            return httpx.Response(200, json=[_fin("A", "Продажа", "2026-09-12T11:00:00Z", rrd=10),
                                             _fin("B", "Продажа", "2026-09-12T11:00:00Z", rrd=11)])
        return httpx.Response(200, json=[_fin("C", "Продажа", "2026-09-12T11:00:00Z", rrd=12)])

    _patch_transport(monkeypatch, handler)
    rows = await wb_api.WbClient("test-key-not-real").get_finance_report("2026-07-11", "2026-10-09")
    assert [r["srid"] for r in rows] == ["A", "B", "C"]
    assert len(bodies) == 2 and bodies[1]["rrdid"] == 11 and bodies[0]["period"] == "daily"
    assert bodies[0]["limit"] == 2 and bodies[0]["dateFrom"] == "2026-07-11"


async def test_finance_answer_is_cached(monkeypatch):
    wb_api._FIN_CACHE.clear()
    calls: list[int] = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=[])

    _patch_transport(monkeypatch, handler)
    c = wb_api.WbClient("test-key-not-real")
    await c.get_finance_report("2026-07-11", "2026-10-09")
    await c.get_finance_report("2026-07-11", "2026-10-09")
    await c.get_finance_report("2026-07-12", "2026-10-09")
    await wb_api.WbClient("drugoy-token").get_finance_report("2026-07-11", "2026-10-09")
    assert len(calls) == 3


async def test_duplicate_code_takes_larger_order_id(monkeypatch):
    _env(
        monkeypatch,
        [_order(9, "R9", created="2026-09-20T10:00:00Z"), _order(5, "R5", created="2026-09-12T10:00:00Z")],
        {5: "sold", 9: "defect"},
        {5: [1], 9: [1]},
    )
    it = (await _run())[_k(1)]
    assert it["order_id"] == 9 and it["op"] == "return"


async def test_old_add_items_call_still_works():
    with db.connect() as conn:
        user = db.get_or_create_user(conn, 7100)
        run_id = db.create_run(conn, user["id"], "api")
        base = {"cis_raw": "a", "cis_norm": "a", "op": "sale", "group": "ok", "reason": ""}
        db.add_items(conn, run_id, [base, {**base, "order_id": 55, "istochnik": "перепродажа"}])
        rows = db.get_items(conn, run_id)
    assert [(r["order_id"], r["istochnik"]) for r in rows] == [(0, "отчет"), (55, "перепродажа")]


def test_warnings_reach_step3(monkeypatch):
    _env(monkeypatch, [_order(1, "R1")], {1: "sold"}, {1: [1]})

    async def denied(self, date_from, date_to):
        raise httpx.HTTPStatusError("403", request=httpx.Request("POST", "http://x"), response=httpx.Response(403))

    monkeypatch.setattr(wb_api.WbClient, "get_finance_report", denied)
    c = _client(7101)
    monkeypatch.setattr(config, "CHZ_MODE", "real")
    with db.connect() as conn:
        user = db.get_or_create_user(conn, 7101)
        db.set_wb_key(conn, user["id"], crypto.encrypt_str("test-key-not-real"))
    r = c.post("/step1", data={"source": "api", "date_from": PERIOD[0], "date_to": PERIOD[1]}, follow_redirects=False)
    assert r.status_code == 302, r.text[:300]
    run_id = r.headers["location"].split("/")[-1]
    monkeypatch.setattr(config, "CHZ_MODE", "mock")
    _signin(c, run_id)
    page = c.get(f"/step3/{run_id}").text
    assert "Токен WB без доступа к категории Финансы" in page
    with db.connect() as conn:
        rows = db.get_items(conn, run_id)
    assert [(x["order_id"], x["istochnik"]) for x in rows] == [(1, "задание")]
