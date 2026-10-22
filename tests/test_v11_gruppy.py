from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from chzkiz import chz_api, config, db
from chzkiz.web.app import create_app
from tests.test_regress_2 import INN, _build, _login, _send, _signin

SPRAVOCHNIK = (
    "lp shoes tobacco perfumery tires electronics milk bicycle wheelchairs alcohol otp water furs "
    "beer ncp bio antiseptic petfood seafood nabeer softdrinks meat vetpharma toys radio titan "
    "conserve vegetableoil opticfiber chemistry books grocery pharmaraw construction fire heater "
    "cableraw autofluids polymer sweets carparts furslp nicotindev gadgets frozen fertilizers "
    "homeware pyrotechnics"
).split()
NOVYE = ["chemistry", "toys", "autofluids"]


def _pg(tg_id: int) -> str | None:
    with db.connect() as conn:
        return conn.execute("SELECT pg FROM users WHERE tg_id = ?", (tg_id,)).fetchone()["pg"]


def _step1(c: TestClient, pg: str):
    return c.post("/step1", data={"source": "api", "pg": pg}, follow_redirects=False)


def test_spisok_raven_spravochniku():
    assert len(SPRAVOCHNIK) == 48
    assert set(config.PG_OPTIONS) == set(SPRAVOCHNIK)
    assert len(config.PG_OPTIONS) == 48


def test_pervye_tri_na_meste():
    assert list(config.PG_OPTIONS.items())[:3] == [
        ("lp", "Одежда (lp)"), ("shoes", "Обувь (shoes)"), ("bio", "БАДы (bio)"),
    ]
    assert config.DEFAULT_PG == "lp"


def test_podpisi_s_kodom_po_alfavitu_bez_povtorov():
    labels = list(config.PG_OPTIONS.values())
    assert len(set(labels)) == len(labels)
    for code, label in config.PG_OPTIONS.items():
        assert label.endswith(f" ({code})"), label
    assert labels[3:] == sorted(labels[3:])


def test_shag1_pokazyvaet_vse_gruppy():
    c = TestClient(create_app())
    _login(c, 9701)
    r = c.get("/step1")
    assert r.status_code == 200
    for code, label in config.PG_OPTIONS.items():
        assert f'<option value="{code}"' in r.text, code
        assert label in r.text, label
    assert '<option value="lp" selected>' in r.text


@pytest.mark.parametrize("code", NOVYE)
def test_vybor_novoy_gruppy_sohranyaetsya(code):
    c = TestClient(create_app())
    _login(c, 9702)
    assert _step1(c, code).status_code == 302
    assert _pg(9702) == code
    assert f'<option value="{code}" selected>' in c.get("/step1").text


def test_neizvestnyy_kod_ne_sohranyaetsya():
    c = TestClient(create_app())
    _login(c, 9703)
    _step1(c, "toys")
    _step1(c, "igrushki")
    assert _pg(9703) == "toys"


@pytest.mark.parametrize("code", NOVYE)
def test_dokument_uhodit_s_vybrannoy_gruppoy(monkeypatch, code):
    seen: list[httpx.Request] = []
    real = httpx.AsyncClient

    async def rec(req: httpx.Request) -> None:
        seen.append(req)

    def patched(*args, **kwargs):
        kwargs["event_hooks"] = {"request": [rec]}
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    c = TestClient(create_app())
    _login(c, 9704)
    r = _step1(c, code)
    assert r.status_code == 302, r.text[:200]
    run_id = r.headers["location"].rsplit("/", 1)[-1]
    assert _signin(c, run_id, INN).status_code == 200
    _send(c, run_id, _build(c, run_id, "output"))
    creates = [q for q in seen if q.url.path.endswith(chz_api.DOC_CREATE_PATH)]
    assert creates, "документ не отправлялся"
    assert creates[0].url.params.get("pg") == code
