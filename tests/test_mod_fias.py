"""Место деятельности в документе вывода: панель подставляет его сама."""
from __future__ import annotations

import base64
import json

from fastapi.testclient import TestClient

from chzkiz import chz_api, chz_mock, db
from chzkiz.web.app import create_app


def _mocksig(value: str) -> str:
    return base64.b64encode(f"MOCKSIG:{value}".encode()).decode("ascii")


def _signed_in_run(c: TestClient, tg_id: int) -> str:
    with db.connect() as conn:
        user = db.get_or_create_user(conn, tg_id)
        token = db.create_login_token(conn, user["id"])
    assert c.post(f"/l/{token}", follow_redirects=True).status_code == 200
    r = c.post("/step1", data={"source": "api"}, follow_redirects=False)
    run_id = r.headers["location"].rsplit("/", 1)[-1]
    p = c.post(f"/step2/{run_id}/auth-key").json()
    assert c.post(
        f"/step2/{run_id}/signin",
        json={
            "uuid": p["uuid"],
            "signature": _mocksig(p["data"]),
            "inn": chz_mock.DEMO_OUR_INN,
            "owner": "Тест",
        },
    ).status_code == 200
    return run_id


def _vyvod(c: TestClient, run_id: str) -> dict:
    r = c.post(f"/step4/{run_id}/build", json={"doc_type": "output"})
    assert r.status_code == 200, r.text
    return json.loads(base64.b64decode(r.json()["product_document"]))


def test_mesto_deyatelnosti_podstavlyaetsya_samo():
    c = TestClient(create_app())
    doc = _vyvod(c, _signed_in_run(c, 9301))
    assert doc.get("fias_id") == chz_mock.MOCK_FIAS, doc


def test_mesto_zapominaetsya_za_chelovekom():
    c = TestClient(create_app())
    _vyvod(c, _signed_in_run(c, 9302))
    with db.connect() as conn:
        user = db.get_or_create_user(conn, 9302)
    assert user["fias_id"] == chz_mock.MOCK_FIAS


def test_bez_proshlyh_dokumentov_sborka_trebuet_fias(monkeypatch):
    async def pusto(self, pg: str = "lp") -> str:
        return ""

    monkeypatch.setattr(chz_api.ChzClient, "last_mod_fias", pusto)
    c = TestClient(create_app())
    r = c.post(f"/step4/{_signed_in_run(c, 9303)}/build", json={"doc_type": "output"})
    assert r.status_code == 400 and "ФИАС" in r.json()["detail"], r.text


def test_chz_otvetil_oshibkoy_sborka_trebuet_fias(monkeypatch):
    async def padaet(self, pg: str = "lp") -> str:
        raise RuntimeError("ЧЗ не ответил")

    monkeypatch.setattr(chz_api.ChzClient, "last_mod_fias", padaet)
    c = TestClient(create_app())
    r = c.post(f"/step4/{_signed_in_run(c, 9304)}/build", json={"doc_type": "output"})
    assert r.status_code == 400 and "ФИАС" in r.json()["detail"], r.text


def test_vtoroy_progon_ne_hodit_v_chz_za_mestom(monkeypatch):
    """Адрес запомнен за человеком: второй прогон берет его из памяти, а не из ЧЗ."""
    zvonki = []

    async def schetchik(self, pg: str = "lp") -> str:
        zvonki.append(pg)
        return chz_mock.MOCK_FIAS

    monkeypatch.setattr(chz_api.ChzClient, "last_mod_fias", schetchik)
    c = TestClient(create_app())
    doc1 = _vyvod(c, _signed_in_run(c, 9305))
    doc2 = _vyvod(c, _signed_in_run(c, 9305))
    assert doc1.get("fias_id") == doc2.get("fias_id") == chz_mock.MOCK_FIAS
    assert len(zvonki) == 1, f"в ЧЗ сходили {len(zvonki)} раза, адрес не запомнился"

def test_oplata_pri_vozvrate_voprosa_net():
    c = TestClient(create_app())
    run_id = _signed_in_run(c, 9306)
    page = c.get(f"/step4/{run_id}").text
    assert 'id="paid"' not in page and "Покупатель оплатил" not in page
