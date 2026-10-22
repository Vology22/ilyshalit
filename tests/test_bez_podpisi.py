from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from chzkiz import chz_mock, db
from chzkiz.web.app import create_app

INN = chz_mock.DEMO_OUR_INN
NET = object()


def _mocksig(value):
    return base64.b64encode(f"MOCKSIG:{value}".encode()).decode("ascii")


def _run_with_doc(tg_id):
    c = TestClient(create_app())
    with db.connect() as conn:
        user = db.get_or_create_user(conn, tg_id)
        token = db.create_login_token(conn, user["id"])
    assert c.post(f"/l/{token}", follow_redirects=True).status_code == 200
    r = c.post("/step1", data={"source": "api"}, follow_redirects=False)
    run_id = r.headers["location"].rsplit("/", 1)[-1]
    p = c.post(f"/step2/{run_id}/auth-key").json()
    r = c.post(
        f"/step2/{run_id}/signin",
        json={"uuid": p["uuid"], "signature": _mocksig(p["data"]), "inn": INN, "owner": "Тест"},
    )
    assert r.status_code == 200
    r = c.post(f"/step4/{run_id}/build", json={"doc_type": "output"})
    assert r.status_code == 200, r.text
    return c, run_id, r.json()


def _doc_row(run_id, doc_id):
    with db.connect() as conn:
        d = db.get_document(conn, doc_id, run_id)
    return d["status"], d["chz_doc_id"]


@pytest.mark.parametrize("sig", [NET, None, "", "   ", 123], ids=["net", "null", "pusto", "probely", "chislo"])
def test_send_without_signature_is_400_and_doc_untouched(sig):
    c, run_id, doc = _run_with_doc(10301)
    before = _doc_row(run_id, doc["doc_id"])
    body = {"doc_id": doc["doc_id"], "chz_type": doc["chz_type"], "product_document": doc["product_document"]}
    if sig is not NET:
        body["signature"] = sig
    r = c.post(f"/step4/{run_id}/send", json=body)
    assert r.status_code == 400, r.text
    assert "Подпись не получена" in r.json()["detail"]
    assert _doc_row(run_id, doc["doc_id"]) == before
    assert not before[1]

    body["signature"] = _mocksig(doc["product_document"])
    r = c.post(f"/step4/{run_id}/send", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["chz_doc_id"]


@pytest.mark.parametrize("drop", ["doc_id", "chz_type", "product_document"])
def test_send_with_missing_field_is_400_not_500(drop):
    c, run_id, doc = _run_with_doc(10302)
    before = _doc_row(run_id, doc["doc_id"])
    body = {"doc_id": doc["doc_id"], "chz_type": doc["chz_type"],
            "product_document": doc["product_document"], "signature": _mocksig(doc["product_document"])}
    body.pop(drop)
    r = c.post(f"/step4/{run_id}/send", json=body)
    assert r.status_code == 400, r.text
    assert "не полностью" in r.json()["detail"]
    assert _doc_row(run_id, doc["doc_id"]) == before


def test_send_empty_body_is_400():
    c, run_id, doc = _run_with_doc(10303)
    r = c.post(f"/step4/{run_id}/send", json={})
    assert r.status_code == 400, r.text


def test_repeat_click_on_sent_doc_still_ok_without_signature():
    c, run_id, doc = _run_with_doc(10304)
    body = {"doc_id": doc["doc_id"], "chz_type": doc["chz_type"],
            "product_document": doc["product_document"], "signature": _mocksig(doc["product_document"])}
    first = c.post(f"/step4/{run_id}/send", json=body)
    assert first.status_code == 200, first.text
    body.pop("signature")
    again = c.post(f"/step4/{run_id}/send", json=body)
    assert again.status_code == 200 and again.json()["chz_doc_id"] == first.json()["chz_doc_id"]


def test_foreign_run_is_still_404_before_any_field_check():
    c, run_id, doc = _run_with_doc(10305)
    c2 = TestClient(create_app())
    with db.connect() as conn:
        user = db.get_or_create_user(conn, 10306)
        token = db.create_login_token(conn, user["id"])
    assert c2.post(f"/l/{token}", follow_redirects=True).status_code == 200
    r = c2.post(f"/step4/{run_id}/send", json={})
    assert r.status_code == 404, r.text
