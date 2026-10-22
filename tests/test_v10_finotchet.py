"""Финотчет за длинный период: ответ читается кусками, от строки остаются только нужные поля."""
from __future__ import annotations

import json
import tracemalloc

import httpx
import pytest

from chzkiz import wb_api
from tests.test_v2_a1 import _env, _fin, _k, _order, _patch_transport, _run

REAL_FINANCE = wb_api.WbClient.get_finance_report


def _polnaya(srid, oper, dt, shk=None, rrd=1):
    row = {f"pole_{i}": f"значение {i} товар №{rrd}" for i in range(89)}
    row.update(_fin(srid, oper, dt, shk, rrd))
    return row


def _razobrat(body: bytes, chunk: int) -> list[dict]:
    import codecs

    utf8 = codecs.getincrementaldecoder("utf-8-sig")()
    acc = wb_api._FinRows()
    for i in range(0, len(body), chunk):
        acc.feed(utf8.decode(body[i : i + chunk]))
    acc.feed(utf8.decode(b"", True), last=True)
    return acc.rows


@pytest.mark.parametrize("chunk", [1, 3, 7, 64, 1000, 65536])
def test_kuski_dayut_te_zhe_stroki(chunk):
    data = [_polnaya(f"R{n}", "Продажа" if n % 3 else "Возврат", "2026-09-12T11:00:00Z", shk=n * 7, rrd=n) for n in range(1, 40)]
    data.insert(5, {"srid": "bez-ostalnyh"})
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    want = [{k: x[k] for k in wb_api.FIN_FIELDS if k in x} for x in data]
    assert _razobrat(body, chunk) == want


@pytest.mark.parametrize("chunk", [1, 5, 65536])
def test_probely_bom_i_ne_slovari(chunk):
    body = "﻿ \n[ 1, \"x\", null, {\"srid\": \"A\", \"lishnee\": {\"a\": [1, {\"b\": \"]\"}]}} ,\n[2], {\"srid\": \"B]\"} ]\n".encode("utf-8")
    assert _razobrat(body, chunk) == [{"srid": "A"}, {"srid": "B]"}]


@pytest.mark.parametrize("body", [b"", b"  \n", b"null", b"[]", b" [ ] ", b"[] \n\n"])
def test_pustoy_otvet_eto_pustoy_spisok(body):
    assert _razobrat(body, 2) == []


@pytest.mark.parametrize(
    "body",
    [
        b"<html>502 Bad Gateway</html>",
        b'{"data": []}',
        b'"stroka"',
        b"17",
        b'[{"srid": "A"}',
        b'[{"srid": "A"}, {"srid": "B"',
        b'[{"srid": "A"}, {"srid": ',
        b"[",
        b'[{"srid": "A"} musor]',
        b'[{"srid": "A"}][{"srid": "B"}]',
        b'[{"srid": "A"}]<html>',
        b'[]{"error": 1}',
        b"[]]",
        "\u00a0[]".encode("utf-8"),
        b"\x0c[]",
    ],
)
@pytest.mark.parametrize("chunk", [1, 4, 65536])
def test_musor_i_obryv_eto_oshibka(body, chunk):
    with pytest.raises(ValueError):
        _razobrat(body, chunk)


@pytest.mark.parametrize("body", [b'[1.5,{"srid":"A"}]', b'[2e3, {"srid": "A"}, -0.25]', b'[12345678, {"srid": "A"} ] \n'])
@pytest.mark.parametrize("chunk", [1, 2, 3, 4, 5, 65536])
def test_chislo_na_granice_kuska(body, chunk):
    assert _razobrat(body, chunk) == [{"srid": "A"}]


@pytest.mark.parametrize("chunk", [1, 50, 65536])
def test_stroka_dlinnee_predela_eto_oshibka(monkeypatch, chunk):
    monkeypatch.setattr(wb_api, "FIN_ROW_MAX", 300)
    ok = json.dumps([{"srid": "A", "x": "я" * 200}, {"srid": "B", "x": "я" * 200}], ensure_ascii=False).encode("utf-8")
    assert _razobrat(ok, 50) == [{"srid": "A"}, {"srid": "B"}]
    with pytest.raises(ValueError):
        _razobrat(json.dumps([{"srid": "A", "x": "я" * 5000}], ensure_ascii=False).encode("utf-8"), min(chunk, 50))
    with pytest.raises(ValueError):
        _razobrat(b'[{"srid": "A", "x": "' + b"z" * 5000, min(chunk, 50))


async def test_v_stroke_ostayutsya_tolko_nuzhnye_polya(monkeypatch):
    wb_api._FIN_CACHE.clear()
    data = [_polnaya("R1", "Продажа", "2026-09-12T11:00:00Z", shk=5, rrd=10)]

    def handler(request):
        return httpx.Response(200, content=json.dumps(data, ensure_ascii=False).encode("utf-8"))

    _patch_transport(monkeypatch, handler)
    c = wb_api.WbClient("test-key-not-real")
    rows = await c.get_finance_report("2026-07-11", "2026-10-09")
    assert rows == [_fin("R1", "Продажа", "2026-09-12T11:00:00Z", shk=5, rrd=10)]
    assert await c.get_finance_report("2026-07-11", "2026-10-09") is rows


async def test_itog_sverki_ne_zavisit_ot_lishnih_poley(monkeypatch):
    orders = [_order(1, "R1"), _order(2, "R2"), _order(3, "R3"), _order(4, "R4")]
    statuses = {1: "sold", 2: "sold", 3: "sold", 4: "sold"}
    codes = {1: [1], 2: [2], 3: [3], 4: [4]}
    fin = [
        _fin("R1", "Продажа", "2026-09-12T11:00:00Z", shk=101, rrd=1),
        _fin("R2", "Продажа", "2026-09-13T11:00:00Z", shk=102, rrd=2),
        _fin("R2", "Возврат", "2026-09-20T09:00:00Z", shk=102, rrd=3),
        _fin("R3", "Продажа", "2026-09-14T11:00:00Z", shk=103, rrd=4),
        _fin("R3", "Возврат", "2026-09-18T09:00:00Z", shk=103, rrd=5),
        _fin("X9", "Продажа", "2026-09-25T09:00:00Z", shk=103, rrd=6),
        _fin("R4", "Логистика", "2026-09-15T11:00:00Z", shk=104, rrd=7),
    ]
    full = [_polnaya(x["srid"], x["sellerOperName"], x["saleDt"], x["shkId"], x["rrdId"]) for x in fin]
    _env(monkeypatch, orders, statuses, codes, fin=full)
    want = await _run()
    assert len(want) >= 3

    def handler(request):
        return httpx.Response(200, content=json.dumps(full, ensure_ascii=False).encode("utf-8"))

    _env(monkeypatch, orders, statuses, codes)
    monkeypatch.setattr(wb_api.WbClient, "get_finance_report", REAL_FINANCE)
    _patch_transport(monkeypatch, handler)
    client = wb_api.WbClient("test-key-not-real")
    got = await _run(client)
    assert got == want
    assert client.warnings == []


async def test_bolshoy_otvet_ne_lezhit_v_pamyati_celikom(monkeypatch):
    wb_api._FIN_CACHE.clear()
    n = 2500
    body = json.dumps(
        [_polnaya(f"R{i}", "Продажа", "2026-09-12T11:00:00Z", shk=i, rrd=i) for i in range(1, n + 1)],
        ensure_ascii=False,
    ).encode("utf-8")
    assert len(body) > 4_000_000

    async def kuskami():
        for i in range(0, len(body), 65536):
            yield body[i : i + 65536]

    def handler(request):
        return httpx.Response(200, content=kuskami())

    _patch_transport(monkeypatch, handler)
    client = wb_api.WbClient("test-key-not-real")
    tracemalloc.start()
    try:
        rows = await client.get_finance_report("2026-07-11", "2026-10-09")
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert len(rows) == n and set(rows[0]) == set(wb_api.FIN_FIELDS)
    assert peak < len(body) // 2, f"пик {peak} при теле {len(body)}"


async def test_posle_bitoy_stroki_ostatok_otveta_ne_chitaetsya():
    vydano = 0

    async def kuskami():
        nonlocal vydano
        yield b'[{"srid": "A"}, {"srid": },'
        for _ in range(200):
            vydano += 1
            yield b'{"srid": "B"},' * 4682

    with pytest.raises(ValueError):
        await wb_api._fin_rows(httpx.Response(200, content=kuskami()))
    assert vydano < 40


async def test_otkaz_dostupa_ostaetsya_oshibkoy_statusa(monkeypatch):
    wb_api._FIN_CACHE.clear()

    async def telo():
        yield b'{"title": "unauthorized"}'

    def handler(request):
        return httpx.Response(401, content=telo())

    _patch_transport(monkeypatch, handler)
    with pytest.raises(httpx.HTTPStatusError) as e:
        await wb_api.WbClient("test-key-not-real").get_finance_report("2026-07-11", "2026-10-09")
    assert e.value.response.status_code == 401 and "unauthorized" in e.value.response.text


async def test_oborvannyy_otvet_daet_preduprezhdenie_a_ne_padenie(monkeypatch):
    _env(monkeypatch, [_order(1, "R1")], {1: "sold"}, {1: [1]})
    monkeypatch.setattr(wb_api.WbClient, "get_finance_report", REAL_FINANCE)

    def handler(request):
        return httpx.Response(200, content=b'[{"srid": "R1", "sellerOperName": "\xd0\x9f\xd1\x80\xd0\xbe')

    _patch_transport(monkeypatch, handler)
    client = wb_api.WbClient("test-key-not-real")
    got = await _run(client)
    assert _k(1) in got
    assert wb_api.WARN_FAIL_FINANCE in client.warnings
