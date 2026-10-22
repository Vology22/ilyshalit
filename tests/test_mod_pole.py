"""Поле места деятельности на шаге 4: введенный код идет в документ и запоминается."""
from __future__ import annotations

import base64
import json

from fastapi.testclient import TestClient

from chzkiz import chz_mock, db
from chzkiz.web.app import create_app

from tests.test_mod_fias import _signed_in_run

KOD = "5f1d2a33-8b4c-4e6d-9a7b-1c2d3e4f5a6b"


def test_vvedennyy_kod_idet_v_dokument_i_zapominaetsya():
    c = TestClient(create_app())
    run_id = _signed_in_run(c, 9401)
    r = c.post(f"/step4/{run_id}/build", json={"doc_type": "output", "fias": " " + KOD.upper() + " "})
    assert r.status_code == 200, r.text
    doc = json.loads(base64.b64decode(r.json()["product_document"]))
    assert doc["fias_id"] == KOD, doc
    with db.connect() as conn:
        user = db.get_or_create_user(conn, 9401)
    assert user["fias_id"] == KOD
    assert user["fias_inn"] == chz_mock.DEMO_OUR_INN


def test_krivoy_kod_otbivaetsya_i_ne_zapominaetsya():
    c = TestClient(create_app())
    run_id = _signed_in_run(c, 9402)
    r = c.post(f"/step4/{run_id}/build", json={"doc_type": "output", "fias": "Москва, Ленина 1"})
    assert r.status_code == 400, r.text
    assert "ФИАС" in r.json()["detail"]
    with db.connect() as conn:
        user = db.get_or_create_user(conn, 9402)
    assert (user["fias_id"] or "") == ""


def test_shag4_pokazyvaet_zapomnennyy_kod():
    c = TestClient(create_app())
    run_id = _signed_in_run(c, 9403)
    with db.connect() as conn:
        user = db.get_or_create_user(conn, 9403)
        db.set_user_fias(conn, user["id"], KOD, chz_mock.DEMO_OUR_INN)
    html = c.get(f"/step4/{run_id}").text
    assert 'id="fias"' in html
    assert f'value="{KOD}"' in html


def test_kod_chuzhogo_inn_ne_podstavlyaetsya_v_pole():
    c = TestClient(create_app())
    run_id = _signed_in_run(c, 9404)
    with db.connect() as conn:
        user = db.get_or_create_user(conn, 9404)
        db.set_user_fias(conn, user["id"], KOD, "9999999999")
    html = c.get(f"/step4/{run_id}").text
    assert 'id="fias"' in html
    assert KOD not in html
