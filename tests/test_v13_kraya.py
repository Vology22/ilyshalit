"""Атаки на правку v13: повтор кодов в ЧЗ из новой сверки."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from chzkiz import chz_api, db, kiz
from chzkiz.web import app as app_mod
from tests.test_attacks_1010c import FIAS, _ac, _send_body
from tests.test_attacks_mod import _code
from tests.test_v13_povtor import V_OBRABOTKE, _build, _chz, _docs, _prodavec, _sverka, _vyvod_ushel

C1 = kiz.normalize_kiz(_code(1))
C7 = kiz.normalize_kiz(_code(7))


def _build_or_fail(c, run_id, **extra):
    try:
        return _build(c, run_id, **extra)
    except Exception as e:
        pytest.fail(f"сборка упала 500: {e!r}")


def _s_kodom(run_id: str, code: str) -> list[dict]:
    return [d for d in _docs(run_id) if code in json.loads(d["codes_json"])]


async def test_sborka_i_otpravka_odnoy_sverki_odnovremenno_dayut_vtoroy_dokument(monkeypatch):
    """Пока сборка ждет ответа ЧЗ по документу прошлой сверки, документ этой же сверки уходит
    в ЧЗ. Сборка берет список документов до ожидания и создает новый документ на уже отправленный код."""
    _chz(monkeypatch, V_OBRABOTKE)
    c = _prodavec(13301)
    _vyvod_ushel(c, codes=[_code(1)])
    second = _sverka(c)
    ids = iter(["CHZ-OWN-1", "CHZ-OWN-2"])

    async def fake_send(client, product_document_b64, doc_type, signature_b64, pg=None):
        return next(ids)

    async def fake_info(self, doc_id):
        if doc_id == "CHZ-PRIOR-1":
            await asyncio.sleep(0.5)
        return V_OBRABOTKE

    monkeypatch.setattr(app_mod, "_send_raw_document", fake_send)
    monkeypatch.setattr(chz_api.ChzClient, "doc_info", fake_info)
    built = _build(c, second, codes=[_code(7)]).json()
    async with _ac(c) as ac:
        async def send_later():
            await asyncio.sleep(0.15)
            return await ac.post(f"/step4/{second}/send", json=_send_body(built))

        await asyncio.gather(
            ac.post(f"/step4/{second}/build", json={"doc_type": "output", "fias": FIAS, "codes": [_code(7)]}),
            send_later(),
        )
    mine = _s_kodom(second, C7)
    assert len(mine) == 1, [(d["status"], d["chz_doc_id"]) for d in mine]


@pytest.mark.parametrize("status", ["ACCEPTED", "PROCESSING"])
def test_neizvestnyy_status_chz_ne_dolzhen_osvobozhdat_kody(monkeypatch, status):
    """Статус не из пяти известных: панель пишет его в базу как есть, код не занят и не свободен
    по таблице состояний, документ собирается. ACCEPTED в app.js подписан как принят."""
    _chz(monkeypatch, V_OBRABOTKE)
    c = _prodavec(13303 + len(status))
    _vyvod_ushel(c)
    _chz(monkeypatch, {"status": status, "errors": None})
    second = _sverka(c)
    r = _build(c, second)
    assert r.status_code == 409, r.text
    assert _docs(second) == []


@pytest.mark.parametrize("exc", [RuntimeError("boom"), OSError("reset"), httpx.InvalidURL("bad id")])
def test_lyuboe_isklyuchenie_doc_info_ne_rushit_sborku(monkeypatch, exc):
    """ТЗ: ЧЗ молчит - коды заняты. Исключение вне списка ChzAuthError, HTTPError, ValueError дает 500."""
    _chz(monkeypatch, V_OBRABOTKE)
    c = _prodavec(13310 + len(str(exc)))
    _vyvod_ushel(c)
    _chz(monkeypatch, exc)
    second = _sverka(c)
    r = _build_or_fail(c, second)
    assert r.status_code == 409, r.text
    assert _docs(second) == []


def test_bityy_token_sverki_ne_rushit_sborku(monkeypatch):
    """Токен ЧЗ в сверке не расшифровался: до правки возврат собирался без токена, теперь 500."""
    _chz(monkeypatch, V_OBRABOTKE)
    c = _prodavec(13320)
    _vyvod_ushel(c)
    second = _sverka(c)
    with db.connect() as conn:
        conn.execute("UPDATE runs SET chz_token_enc = 'garbage' WHERE id = ?", (second,))
        conn.commit()
    r = _build_or_fail(c, second)
    assert r.status_code == 409, r.text


@pytest.mark.parametrize("raw", ["garbage", "null", '[{"a": 1}]', "5"])
def test_bityy_spisok_kodov_chuzhoy_sverki_ne_rushit_sborku(monkeypatch, raw):
    """Строка документа из другой сверки с битым codes_json: _code_states такое терпит,
    новый _prior_states падает, и сборка в этой сверке дает 500."""
    _chz(monkeypatch, V_OBRABOTKE)
    c = _prodavec(13330 + len(raw))
    first, _ = _vyvod_ushel(c)
    second = _sverka(c)
    with db.connect() as conn:
        conn.execute("UPDATE documents SET codes_json = ? WHERE run_id = ?", (raw, first))
        conn.commit()
    r = _build_or_fail(c, second)
    assert r.status_code in (200, 409), r.text


def test_zanyatye_kody_bez_spiska_idut_v_skipped(monkeypatch):
    """ТЗ п.1: занятые идут в skipped. Без поля codes массив пуст и в note ни слова."""
    _chz(monkeypatch, V_OBRABOTKE)
    c = _prodavec(13340)
    _vyvod_ushel(c, codes=[_code(1)])
    second = _sverka(c)
    r = _build(c, second)
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 1
    assert [x["cis"] for x in r.json()["skipped"]] == [C1], r.json()["skipped"]


def test_opros_statusa_ne_menyaet_formu_otveta(monkeypatch):
    """ТЗ п.5: опрос ведет себя как до правки. Служебная метка done_at ушла в ответ браузеру."""
    _chz(monkeypatch, {"status": "CHECKED_OK", "errors": None})
    c = _prodavec(13350)
    first, _ = _vyvod_ushel(c)
    doc = c.get(f"/step4/{first}/status").json()["documents"][0]
    assert "done_at" not in doc, sorted(doc)
