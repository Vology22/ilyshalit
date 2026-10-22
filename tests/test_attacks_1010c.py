"""Второй круг: обрыв туннеля до ЧЗ, мусор в ответах ЧЗ, статус UNDEFINED, отчет при документе в отправке,
устаревший документ после пересборки."""
from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest

from chzkiz import chz_api, chz_mock, db, kiz
from chzkiz.web import app as app_mod
from tests.test_attacks_mod import _client, _code, _fin, _signin, _upload
from tests.test_v2_b import _run as _run_step4

FIAS = "0c5b2444-70a0-4932-980c-b4dc0d3f02b5"
INN = chz_mock.DEMO_OUR_INN


def _ac(c, raise_errors: bool = True) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=c.app, raise_app_exceptions=raise_errors),
        base_url="http://testserver", cookies=dict(c.cookies),
    )


def _send_body(built: dict) -> dict:
    return {"doc_id": built["doc_id"], "product_document": built["product_document"],
            "chz_type": built["chz_type"], "signature": "U0lH"}


def _build_output(c, run_id, **extra) -> dict:
    r = c.post(f"/step4/{run_id}/build", json={"doc_type": "output", "fias": FIAS, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _doc(doc_id: str, run_id: str) -> dict:
    with db.connect() as conn:
        return dict(db.get_document(conn, doc_id, run_id))


def _chz_transport(monkeypatch, handler) -> None:
    def fake_http_client(self):
        return httpx.AsyncClient(timeout=5.0, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(chz_api.ChzClient, "_http_client", fake_http_client)


@pytest.mark.parametrize("oshibka", [
    httpx.RemoteProtocolError("Server disconnected without sending a response."),
    httpx.ReadError("Connection reset by peer"),
    httpx.ConnectError("[Errno 111] Connection refused"),
])
async def test_obryv_tunnelya_do_chz_pri_otpravke_ne_500(monkeypatch, oshibka):
    # до ЧЗ ходим через ssh-туннель chz-haimi-tunnel: его рестарт рвет запрос не таймаутом
    c, run_id, _ = _run_step4(8951)
    built = _build_output(c, run_id)

    async def fake_send(client, product_document_b64, doc_type, signature_b64, pg=None):
        raise oshibka

    monkeypatch.setattr(app_mod, "_send_raw_document", fake_send)
    async with _ac(c, raise_errors=False) as ac:
        r = await ac.post(f"/step4/{run_id}/send", json=_send_body(built))
    assert r.status_code == 502, f"обрыв связи с ЧЗ отдан как {r.status_code}: {r.text[:120]}"


async def test_status_undefined_ne_zakryvaet_sverku(monkeypatch):
    c, run_id, ids = _run_step4(8952)
    built = _build_output(c, run_id)

    async def fake_send(client, product_document_b64, doc_type, signature_b64, pg=None):
        return "CHZ-UNDEF-1"

    async def fake_info(self, doc_id):
        return {"status": "UNDEFINED", "errors": None}

    monkeypatch.setattr(app_mod, "_send_raw_document", fake_send)
    monkeypatch.setattr(chz_api.ChzClient, "doc_info", fake_info)
    assert c.post(f"/step4/{run_id}/send", json=_send_body(built)).status_code == 200
    data = c.get(f"/step4/{run_id}/status").json()
    assert data["done"] is False, "ЧЗ еще не определил исход (в панели 'ждем ответа'), а сверка закрыта и отчет собран"
    again = c.post(f"/step4/{run_id}/build", json={"doc_type": "output", "fias": FIAS})
    assert again.status_code != 200, "код из документа без исхода снова собран в новый вывод"


async def test_status_ne_gotovo_poka_vozvrat_v_otpravke(monkeypatch):
    c, run_id, _ = _run_step4(8953)
    out = _build_output(c, run_id)

    async def fake_send(client, product_document_b64, doc_type, signature_b64, pg=None):
        if doc_type == chz_api.DOC_TYPE_RECEIPT:
            return "CHZ-OUT-1"
        await asyncio.sleep(0.5)
        return "CHZ-RET-1"

    async def fake_info(self, doc_id):
        return {"status": "CHECKED_OK" if doc_id == "CHZ-OUT-1" else "IN_PROGRESS", "errors": None}

    monkeypatch.setattr(app_mod, "_send_raw_document", fake_send)
    monkeypatch.setattr(chz_api.ChzClient, "doc_info", fake_info)
    assert c.post(f"/step4/{run_id}/send", json=_send_body(out)).status_code == 200
    r = c.post(f"/step4/{run_id}/build", json={"doc_type": "return", "paid": False})
    assert r.status_code == 200, r.text
    ret = r.json()

    async with _ac(c) as ac:
        async def poll():
            await asyncio.sleep(0.15)
            return await ac.get(f"/step4/{run_id}/status")

        sent, st = await asyncio.gather(ac.post(f"/step4/{run_id}/send", json=_send_body(ret)), poll())
    assert sent.status_code == 200
    assert st.json()["done"] is False, "возврат еще летит в ЧЗ, а сверка объявлена готовой и отчет собран без него"


def _dva_vyvoda(tg_id: int):
    for n in (1, 7):
        chz_mock.seed_cis(kiz.normalize_kiz(_code(n)), chz_api.STATUS_INTRODUCED, INN)
    c = _client(tg_id)
    f = _fin([("Продажа", _code(1), 5001, "500", "2026-10-01"),
              ("Продажа", _code(7), 5007, "700", "2026-10-02")])
    run_id, _ = _upload(c, [("report.xlsx", f)])
    _signin(c, run_id)
    return c, run_id


async def test_staryy_dokument_posle_peresborki_ne_uhodit_v_chz(monkeypatch):
    # вторая вкладка пересобрала тот же документ (тот же doc_id) на меньший выбор, первая дописала подпись
    c, run_id = _dva_vyvoda(8954)
    a, b = kiz.normalize_kiz(_code(1)), kiz.normalize_kiz(_code(7))
    first = _build_output(c, run_id)
    second = _build_output(c, run_id, codes=[a])
    assert first["doc_id"] == second["doc_id"]
    ushlo: list[set[str]] = []

    async def fake_send(client, product_document_b64, doc_type, signature_b64, pg=None):
        text = base64.b64decode(product_document_b64).decode()
        ushlo.append({x for x in (a, b) if x in text})
        return "CHZ-STALE-1"

    monkeypatch.setattr(app_mod, "_send_raw_document", fake_send)
    r = c.post(f"/step4/{run_id}/send", json=_send_body(first))
    v_baze = set(json.loads(_doc(first["doc_id"], run_id)["codes_json"]))
    assert r.status_code != 200 or ushlo[0] == v_baze, (
        f"в ЧЗ ушли коды {sorted(ushlo[0])}, а в базе документ записан с {sorted(v_baze)}"
    )


async def test_html_vmesto_nomera_dokumenta_ne_stanovitsya_nomerom(monkeypatch):
    c, run_id, _ = _run_step4(8955)
    built = _build_output(c, run_id)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body>Ведутся технические работы</body></html>",
                              headers={"content-type": "text/html"})

    _chz_transport(monkeypatch, handler)
    r = c.post(f"/step4/{run_id}/send", json=_send_body(built))
    saved = _doc(built["doc_id"], run_id)["chz_doc_id"]
    assert not saved or "<" not in saved, f"номером документа в ЧЗ записана страница: {saved[:60]}"


async def test_musor_v_doc_info_ne_ronyaet_status(monkeypatch):
    c, run_id, _ = _run_step4(8956)
    built = _build_output(c, run_id)

    async def fake_send(client, product_document_b64, doc_type, signature_b64, pg=None):
        return "CHZ-INFO-1"

    monkeypatch.setattr(app_mod, "_send_raw_document", fake_send)
    assert c.post(f"/step4/{run_id}/send", json=_send_body(built)).status_code == 200

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>502 Bad Gateway</html>", headers={"content-type": "text/html"})

    _chz_transport(monkeypatch, handler)
    async with _ac(c, raise_errors=False) as ac:
        r = await ac.get(f"/step4/{run_id}/status")
    assert r.status_code == 200, f"опрос статуса упал {r.status_code}, панель перестает обновляться"
