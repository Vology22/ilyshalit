"""Повтор вывода из новой сверки, пока документ прошлой еще проверяется в ЧЗ: новая сверка видит
те же коды к выводу, и второй документ ЧЗ отклоняет."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

import chzkiz
from chzkiz import chz_api, chz_mock, db, kiz
from chzkiz.web import app as app_mod
from tests.test_attacks_1010c import FIAS, INN, _doc, _send_body
from tests.test_attacks_mod import _client, _code, _fin, _signin, _upload

PKG = Path(chzkiz.__file__).parent
B = kiz.normalize_kiz(_code(7))
V_OBRABOTKE = {"status": "IN_PROGRESS", "errors": None}


def _prodavec(tg_id: int):
    for n in (1, 7):
        chz_mock.seed_cis(kiz.normalize_kiz(_code(n)), chz_api.STATUS_INTRODUCED, INN)
    return _client(tg_id)


def _sverka(c) -> str:
    f = _fin([("Продажа", _code(1), 5001, "500", "2026-10-01"),
              ("Продажа", _code(7), 5007, "700", "2026-10-02")])
    run_id, _ = _upload(c, [("report.xlsx", f)])
    _signin(c, run_id)
    return run_id


def _chz(monkeypatch, otvet) -> None:
    async def fake_send(client, product_document_b64, doc_type, signature_b64, pg=None):
        return "CHZ-PRIOR-1"

    async def fake_info(self, doc_id):
        if isinstance(otvet, Exception):
            raise otvet
        return otvet

    monkeypatch.setattr(app_mod, "_send_raw_document", fake_send)
    monkeypatch.setattr(chz_api.ChzClient, "doc_info", fake_info)


def _build(c, run_id, **extra):
    return c.post(f"/step4/{run_id}/build", json={"doc_type": "output", "fias": FIAS, **extra})


def _vyvod_ushel(c, **extra) -> tuple[str, str]:
    run_id = _sverka(c)
    r = _build(c, run_id, **extra)
    assert r.status_code == 200, r.text
    built = r.json()
    assert c.post(f"/step4/{run_id}/send", json=_send_body(built)).status_code == 200
    assert _doc(built["doc_id"], run_id)["status"] == chz_api.DOC_STATUS_IN_PROGRESS
    return run_id, built["doc_id"]


def _docs(run_id: str) -> list[dict]:
    with db.connect() as conn:
        return [dict(d) for d in db.get_documents(conn, run_id)]


@pytest.mark.parametrize("extra", [{}, {"codes": [_code(1), _code(7)]}])
def test_povtor_iz_novoy_sverki_poka_dokument_v_obrabotke(monkeypatch, extra):
    _chz(monkeypatch, V_OBRABOTKE)
    c = _prodavec(13101 + len(extra))
    _vyvod_ushel(c)
    second = _sverka(c)
    r = _build(c, second, **extra)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "из прошлой сверки" in detail and "Повторно отправлять не нужно" in detail
    assert _docs(second) == [], "повторный документ на те же коды собран"


def test_dokument_prinyat_poka_shla_novaya_sverka(monkeypatch):
    _chz(monkeypatch, {"status": "CHECKED_OK", "errors": None})
    c = _prodavec(13103)
    first, doc_id = _vyvod_ushel(c)
    second = _sverka(c)
    r = _build(c, second)
    assert r.status_code == 409, r.text
    assert "принял эти коды" in r.json()["detail"]
    assert _doc(doc_id, first)["status"] == chz_api.DOC_STATUS_CHECKED_OK, "исход прошлого документа не записан"
    assert _docs(second) == []


def test_dokument_prinyat_v_sosedney_vkladke(monkeypatch):
    otvet = dict(V_OBRABOTKE)
    _chz(monkeypatch, otvet)
    c = _prodavec(13111)
    first, doc_id = _vyvod_ushel(c)
    second = _sverka(c)
    otvet["status"] = "CHECKED_OK"
    assert c.get(f"/step4/{first}/status").json()["done"] is True
    r = _build(c, second)
    assert r.status_code == 409, r.text
    assert "принял эти коды" in r.json()["detail"]
    assert _docs(second) == [], "вторая сверка отправила уже принятые коды"


def test_sverka_posle_otveta_chz_ne_blokiruetsya(monkeypatch):
    _chz(monkeypatch, {"status": "CHECKED_OK", "errors": None})
    c = _prodavec(13112)
    first, _ = _vyvod_ushel(c)
    assert c.get(f"/step4/{first}/status").json()["done"] is True
    r = _build(c, _sverka(c))
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 2


def test_otklonennyy_dokument_ne_meshaet_povtoru(monkeypatch):
    _chz(monkeypatch, {"status": "CHECKED_NOT_OK", "errors": ["14: Недопустимый статус кода идентификации"]})
    c = _prodavec(13104)
    first, doc_id = _vyvod_ushel(c)
    r = _build(c, _sverka(c))
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 2
    assert _doc(doc_id, first)["status"] == chz_api.DOC_STATUS_CHECKED_NOT_OK


def test_net_svyazi_s_chz_povtor_ne_uhodit(monkeypatch):
    _chz(monkeypatch, httpx.ConnectError("[Errno 111] Connection refused"))
    c = _prodavec(13105)
    _vyvod_ushel(c)
    second = _sverka(c)
    r = _build(c, second)
    assert r.status_code == 409, r.text
    assert "еще обрабатывается" in r.json()["detail"]
    assert _docs(second) == []


def test_svobodnye_kody_uhodyat_zanyatye_propuskayutsya(monkeypatch):
    _chz(monkeypatch, V_OBRABOTKE)
    c = _prodavec(13106)
    _vyvod_ushel(c, codes=[_code(1)])
    second = _sverka(c)
    r = _build(c, second, codes=[_code(1), _code(7)])
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 1 and "пропущены: 1" in r.json()["note"]
    assert [json.loads(d["codes_json"]) for d in _docs(second)] == [[B]]


def test_drugoy_prodavec_ne_blokiruetsya(monkeypatch):
    _chz(monkeypatch, V_OBRABOTKE)
    _vyvod_ushel(_prodavec(13107))
    other = _prodavec(13108)
    r = _build(other, _sverka(other))
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 2


def test_davnyaya_sverka_ne_blokiruet(monkeypatch):
    _chz(monkeypatch, httpx.ConnectError("нет связи"))
    c = _prodavec(13109)
    first, _ = _vyvod_ushel(c)
    with db.connect() as conn:
        conn.execute("UPDATE runs SET created_at = created_at - 2 * 86400 WHERE id = ?", (first,))
        conn.commit()
    r = _build(c, _sverka(c))
    assert r.status_code == 200, r.text


def test_opros_statusa_pishet_ishod_kak_ranshe(monkeypatch):
    _chz(monkeypatch, {"status": "CHECKED_OK", "errors": None})
    c = _prodavec(13110)
    first, doc_id = _vyvod_ushel(c)
    data = c.get(f"/step4/{first}/status").json()
    assert data["done"] is True
    assert _doc(doc_id, first)["status"] == chz_api.DOC_STATUS_CHECKED_OK


def test_shag4_govorit_skolko_zhdat():
    js = (PKG / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert "Повторно отправлять не нужно" in js
    assert "d.status === 'IN_PROGRESS'" in js
