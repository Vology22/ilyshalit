"""Навигация панели: ссылки сверху, Начать заново, главная и страница без входа."""
from __future__ import annotations

import re

from fastapi.testclient import TestClient

from chzkiz import db
from chzkiz.web.app import create_app


def _vhod(tg_id: int) -> TestClient:
    with db.connect() as conn:
        user = db.get_or_create_user(conn, tg_id)
        token = db.create_login_token(conn, user["id"])
    client = TestClient(create_app())
    client.post(f"/l/{token}", follow_redirects=True)
    return client


def test_s_instrukcii_odin_klik_do_sverki():
    r = TestClient(create_app()).get("/instrukciya")
    assert r.status_code == 200
    assert re.search(r"<a href=./step1.[^>]*>Сверка</a>", r.text), r.text[:600]


def test_na_shage_nachat_zanovo_i_instrukciya():
    r = _vhod(9601).get("/step1")
    assert r.status_code == 200
    assert re.search(r"<a href=./step1.>Начать заново</a>", r.text)
    assert re.search(r"<a href=./instrukciya.[^>]*>Инструкция</a>", r.text)


def test_glavnaya_vedet_na_pervyy_shag():
    r = TestClient(create_app()).get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/step1"


def test_bez_vhoda_brauzer_vidit_stranicu_a_ne_json():
    c = TestClient(create_app())
    r = c.get("/step1", headers={"accept": "text/html,application/xhtml+xml"})
    assert r.status_code == 401
    assert "<h1>Нет входа</h1>" in r.text and "/out" in r.text
    assert c.get("/step1").json()["detail"].startswith("Нет входа")
