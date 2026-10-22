"""Шаг 1 и шаг 3: два поля файлов, период от 30 дней, блок принятых файлов над таблицей."""
from __future__ import annotations

import re
from datetime import date, timedelta

import httpx
from fastapi.testclient import TestClient

from chzkiz import config, crypto, db, wb_api
from chzkiz.web import app as app_mod
from tests.test_bez_klyucha_wb import XLSX, _arch, _client, _code, _fin, _rows
from tests.test_regress_2 import _login
from tests.test_wb_period import _podmena

ARH = _arch([(5001, _code(1), 1500), (5002, _code(2), 1800)])
FIN = _fin([("Продажа", _code(1), 5001, 1500, "2026-10-01"), ("Продажа", _code(2), 5002, 1800, "2026-10-02"),
            ("Продажа", _code(2), 5002, 1800, "2026-10-03")])


def _post(c, files):
    r = c.post("/step1", data={"source": "excel"},
               files=[(field, (n, b, XLSX)) for field, n, b in files], follow_redirects=False)
    assert r.status_code == 302, r.text[:300]
    return r.headers["location"].rsplit("/", 1)[-1]


def _layout(run_id):
    return sorted((x["cis_norm"], x["op"], x["product_cost"], x["date"]) for x in _rows(run_id))


def test_step1_otdaet_dva_polya_i_30_dney():
    c = _client(9601)
    html = c.get("/step1").text
    assert 'name="file"' in html and 'name="file_fin"' in html
    nachalo = (date.today() - timedelta(days=29)).isoformat()
    assert re.search(r'name="date_from" value="%s"' % nachalo, html)
    assert "Минимальный период сверки 30 дней" in html


def test_arhiv_v_pervom_pole_finotchet_vo_vtorom_kak_v_starom():
    c = _client(9602)
    novyj = _post(c, [("file", "arh.xlsx", ARH), ("file_fin", "fin.xlsx", FIN)])
    staryj = _post(c, [("file", "arh.xlsx", ARH), ("file", "fin.xlsx", FIN)])
    assert _layout(novyj) and _layout(novyj) == _layout(staryj)


def test_fayly_ne_v_to_pole_razbirayutsya_po_soderzhimomu():
    c = _client(9603)
    pereputano = _post(c, [("file", "fin.xlsx", FIN), ("file_fin", "arh.xlsx", ARH)])
    verno = _post(c, [("file", "arh.xlsx", ARH), ("file_fin", "fin.xlsx", FIN)])
    assert _layout(pereputano) == _layout(verno)


def test_tolko_staroe_pole_file_rabotaet():
    c = _client(9604)
    run_id = _post(c, [("file", "fin.xlsx", FIN)])
    assert {x["cis_norm"] for x in _rows(run_id)} == {_code(1), _code(2)}


def _api_klient(monkeypatch, tg_id, zaprosy):
    c = TestClient(app_mod.create_app())
    _login(c, tg_id)
    with db.connect() as conn:
        user = db.get_or_create_user(conn, tg_id)
        db.set_wb_key(conn, user["id"], crypto.encrypt_str("test-key-not-real"))
    monkeypatch.setattr(config, "CHZ_MODE", "real")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/orders":
            zaprosy.append((int(request.url.params["dateFrom"]), int(request.url.params["dateTo"])))
            return httpx.Response(200, json={"next": 0, "orders": []})
        return httpx.Response(200, json={})

    _podmena(monkeypatch, handler)
    return c


def _warnings(run_id):
    with db.connect() as conn:
        return conn.execute("SELECT warnings FROM runs WHERE id = ?", (run_id,)).fetchone()["warnings"]


def _api_run(c, ot, do):
    r = c.post("/step1", data={"source": "api", "date_from": ot.isoformat(), "date_to": do.isoformat()},
               follow_redirects=False)
    assert r.status_code == 302, r.text[:300]
    return r.headers["location"].rsplit("/", 1)[-1]


def test_api_period_7_dney_rasshiryaetsya_do_30(monkeypatch):
    zaprosy: list = []
    c = _api_klient(monkeypatch, 9605, zaprosy)
    segodnya = date.today()
    run_id = _api_run(c, segodnya - timedelta(days=6), segodnya)
    nachalo = segodnya - timedelta(days=29)
    assert min(a for a, _ in zaprosy) == wb_api.msk_day_unix(nachalo.isoformat()) - wb_api.LOOKBACK_DAYS * 86400
    ozhidaemo = f"Период расширен до 30 дней: с {nachalo:%d.%m.%Y} по {segodnya:%d.%m.%Y}"
    assert ozhidaemo in _warnings(run_id).split("\n")


def test_api_period_45_dney_ne_menyaetsya(monkeypatch):
    zaprosy: list = []
    c = _api_klient(monkeypatch, 9606, zaprosy)
    segodnya = date.today()
    nachalo = segodnya - timedelta(days=44)
    run_id = _api_run(c, nachalo, segodnya)
    assert min(a for a, _ in zaprosy) == wb_api.msk_day_unix(nachalo.isoformat()) - wb_api.LOOKBACK_DAYS * 86400
    assert "Период расширен" not in _warnings(run_id)


def test_shag3_pokazyvaet_prinyatye_fayly_i_knopki_nad_tablicey():
    c = _client(9607)
    run_id = _post(c, [("file", "arh.xlsx", ARH), ("file_fin", "fin.xlsx", FIN)])
    html = c.get(f"/step3/{run_id}").text
    assert "arh.xlsx" in html and "архив заданий" in html
    assert "fin.xlsx" in html and "финансовый отчет" in html
    assert "Строк в отчетах: 5. Уникальных кодов для сверки: 2" in html
    assert html.count('id="preview-link"') == 1
    assert html.index('id="preview-link"') < html.index('id="items-table"')
    assert html.count("Дальше: вывод и возврат") == 2


def test_shag3_dlya_api_tolko_unikalnye_kody():
    c = _client(9608)
    r = c.post("/step1", data={"source": "api"}, follow_redirects=False)
    run_id = r.headers["location"].rsplit("/", 1)[-1]
    html = c.get(f"/step3/{run_id}").text
    assert "Уникальных кодов для сверки:" in html and "Строк в отчетах" not in html


def test_zapusk_bez_novoy_kolonki_otkryvaetsya():
    c = _client(9609)
    run_id = _post(c, [("file", "fin.xlsx", FIN)])
    with db.connect() as conn:
        conn.execute("UPDATE runs SET files_info = '' WHERE id = ?", (run_id,))
        conn.commit()
    r = c.get(f"/step3/{run_id}")
    assert r.status_code == 200 and "Уникальных кодов" not in r.text and 'id="preview-link"' in r.text
