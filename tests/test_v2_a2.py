"""Шаг 4: выбор кодов, ФИАС, отказ ЧЗ словами, частичная обработка, шаг 2 без плагина."""
from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import httpx
import openpyxl
import pytest
from fastapi import HTTPException

from chzkiz import chz_api, chz_errors, chz_mock, db, kiz, sverka
from chzkiz.web import app as app_mod
from chzkiz.web.app import create_app
from tests.test_attacks_mod import _client, _code, _fin, _rows, _signin, _upload

INN = chz_mock.DEMO_OUR_INN
FIAS = "0c5b2444-70a0-4932-980c-b4dc0d3f02b5"
STATIC = Path(__file__).resolve().parent.parent / "chzkiz" / "web" / "static"


def _norm(n: int) -> str:
    return kiz.normalize_kiz(_code(n))


def _sig(value: str) -> str:
    return base64.b64encode(f"MOCKSIG:{value}".encode()).decode("ascii")


def _run(tg: int, sales=(), returns=(), prices=None, why=None):
    """sales и returns: номера кодов. prices: {номер: цена строкой}, why: {номер: withdrawReason}."""
    prices = prices or {}
    rows = [("Продажа", _code(n), 7000 + n, prices.get(n, "500"), "2026-10-01") for n in sales]
    rows += [("Возврат", _code(n), 8000 + n, "500", "2026-10-02") for n in returns]
    for n in sales:
        chz_mock.seed_cis(_norm(n), chz_api.STATUS_INTRODUCED, INN)
    for n in returns:
        chz_mock.seed_cis(_norm(n), chz_api.STATUS_RETIRED, INN)
        if (why or {}).get(n):
            chz_mock._cises[_norm(n)]["withdrawReason"] = why[n]
    c = _client(tg)
    run_id, _ = _upload(c, [("report.xlsx", _fin(rows))])
    _signin(c, run_id)
    return c, run_id


def _build(c, run_id, doc_type="output", **extra):
    return c.post(f"/step4/{run_id}/build", json={"doc_type": doc_type, "paid": False, **extra})


def _doc(r) -> dict:
    return json.loads(base64.b64decode(r.json()["product_document"]))


def _send(c, run_id, r):
    d = r.json()
    return c.post(f"/step4/{run_id}/send", json={
        "doc_id": d["doc_id"], "chz_type": d["chz_type"],
        "product_document": d["product_document"], "signature": _sig(d["product_document"]),
    })


def _docs(run_id):
    with db.connect() as conn:
        return [dict(d) for d in db.get_documents(conn, run_id)]


def _sheet(c, run_id, name):
    wb = openpyxl.load_workbook(io.BytesIO(c.get(f"/report/{run_id}").content))
    return [row for row in wb[name].iter_rows(min_row=2, values_only=True) if row[1]]


def test_build_sobiraet_dokument_tolko_iz_vybrannyh_kodov():
    c, run_id = _run(9701, sales=[1, 2, 3, 4])
    r = _build(c, run_id, codes=[_norm(2), _norm(4)])
    assert r.status_code == 200, r.text
    assert [p["cis"] for p in _doc(r)["products"]] == [_norm(2), _norm(4)]
    assert [json.loads(d["codes_json"]) for d in _docs(run_id)] == [[_norm(2), _norm(4)]]


def test_limit_ogranichivaet_chislo_kodov():
    c, run_id = _run(9702, sales=[1, 2, 3])
    r = _build(c, run_id, limit=2)
    assert r.status_code == 200 and len(_doc(r)["products"]) == 2, r.text
    assert _build(c, run_id, limit=0).status_code == 400


def test_chuzhoy_kod_daet_400_s_ponyatnym_tekstom():
    c, run_id = _run(9703, sales=[1, 2], returns=[5])
    r = _build(c, run_id, codes=[_norm(1), _norm(5)])
    assert r.status_code == 400 and "не входит в группу" in r.json()["detail"], r.text
    r = _build(c, run_id, codes=["0104600000000999219999NETTAKOGO"])
    assert r.status_code == 400, r.text
    assert _build(c, run_id, codes=[]).status_code == 400
    assert not _docs(run_id)


def test_kod_bez_ceny_ne_valit_ostalnye():
    c, run_id = _run(9704, sales=[1, 2, 3], prices={2: "0"})
    r = _build(c, run_id, codes=[_norm(1), _norm(2), _norm(3)])
    assert r.status_code == 200, r.text
    assert [p["cis"] for p in _doc(r)["products"]] == [_norm(1), _norm(3)]
    assert r.json()["skipped"] == [{"cis": _norm(2), "reason": "no_price"}]
    assert "Без цены" in r.json()["note"]
    # без списка кодов код без цены тоже не берется
    c, run_id = _run(9705, sales=[1, 2], prices={2: "0"})
    r = _build(c, run_id)
    assert r.status_code == 200 and len(_doc(r)["products"]) == 1, r.text
    # один код без цены и больше ничего: понятный отказ
    c, run_id = _run(9706, sales=[1], prices={1: "0"})
    r = _build(c, run_id, codes=[_norm(1)])
    assert r.status_code == 400 and "Нет цены" in r.json()["detail"], r.text


def test_vyvod_bez_fias_daet_400_a_s_fias_iz_formy_prohodit(monkeypatch):
    async def pusto(self, pg: str = "lp") -> str:
        return ""

    monkeypatch.setattr(chz_api.ChzClient, "last_mod_fias", pusto)
    c, run_id = _run(9707, sales=[1])
    r = _build(c, run_id)
    assert r.status_code == 400 and "ФИАС" in r.json()["detail"] and "Профиль" in r.json()["detail"], r.text
    assert not _docs(run_id)
    assert _build(c, run_id, fias="не фиас").status_code == 400
    r = _build(c, run_id, fias=FIAS)
    assert r.status_code == 200 and _doc(r)["fias_id"] == FIAS, r.text
    page = c.get(f"/step4/{run_id}").text
    assert FIAS in page, "введенный код запомнился и виден в поле"


def test_stranica_pokazyvaet_najdennyj_fias_i_podskazku_kogda_ne_nashla(monkeypatch):
    c, run_id = _run(9708, sales=[1])
    assert chz_mock.MOCK_FIAS in c.get(f"/step4/{run_id}").text

    async def pusto(self, pg: str = "lp") -> str:
        return ""

    monkeypatch.setattr(chz_api.ChzClient, "last_mod_fias", pusto)
    c, run_id = _run(9709, sales=[1])
    page = c.get(f"/step4/{run_id}").text
    assert "не нашла место деятельности" in page and 'id="fias"' in page


def test_otkaz_chz_s_telom_400_pokazyvaetsya_slovami(monkeypatch):
    c, run_id = _run(9710, sales=[1, 2])
    r = _build(c, run_id)

    def otkaz(signature_b64, expected_value):
        raise HTTPException(status_code=400, detail={"error_message": "Код находится в недопустимом статусе"})

    monkeypatch.setattr(chz_mock, "_check_sign", otkaz)
    s = _send(c, run_id, r)
    assert s.status_code == 400, s.text
    assert "Повторно выводить не нужно" in s.json()["detail"] and "недопустимом статусе" in s.json()["detail"]
    st = c.get(f"/step4/{run_id}/status").json()
    d = st["documents"][0]
    assert d["status"] == chz_api.DOC_STATUS_PROCESSING_ERROR
    assert "недопустимом статусе" in d["errors"] and "Повторно выводить не нужно" in d["explain"]
    assert st["states"]["output"][_norm(1)]["state"] == "failed"


def test_telo_otveta_chz_razbiraetsya_v_raznyh_formah():
    mk = lambda body: httpx.Response(400, json=body)  # noqa: E731
    assert app_mod._reject_text(mk({"error_message": "Нет места"})) == "Нет места"
    assert app_mod._reject_text(mk({"errorMessage": "Плохая подпись"})) == "Плохая подпись"
    assert app_mod._reject_text(mk({"errors": [{"message": "a"}, {"message": "b"}]})) == "a; b"
    assert app_mod._reject_text(httpx.Response(400, text="сырой текст")) == "сырой текст"
    assert "400" in app_mod._reject_text(httpx.Response(400, text=""))


@pytest.mark.parametrize("text", [
    "Код находится в недопустимом статусе",
    "Коды находятся в недопустимых статусах",
    "cisInvalidStatus",
    "INVALID_STATUS for cis",
    "Выявлены коды идентификации с недопустимым статусом",
])
def test_nedopustimyj_status_obyasnyaetsya(text):
    assert "Повторно выводить не нужно" in chz_errors.explain(text)


@pytest.mark.parametrize("why, group", [
    ("RETAIL", sverka.GROUP_ERROR),
    ("EAS_TRADE", sverka.GROUP_ERROR),
    ("DISTANCE", sverka.GROUP_RETURN),
    ("BY_SAMPLES", sverka.GROUP_RETURN),
    (None, sverka.GROUP_RETURN),
])
def test_vozvrat_tolko_dlya_kodov_vyvedennyh_distancionno(why, group):
    c, run_id = _run(9720, returns=[5], why={5: why})
    row = _rows(run_id)[0]
    assert row["group_name"] == group, (row["group_name"], row["reason"])
    if group == sverka.GROUP_ERROR:
        assert row["reason"] == "withdraw_other" and sverka.REASON_LABELS[row["reason"]]
        r = _build(c, run_id, "return")
        assert r.status_code == 400, "код в ошибках не попадает в возврат"


def test_oshibka_po_odnomu_kodu_otklonyaet_ves_dokument():
    # ЧЗ обрабатывает документ целиком: ошибка по одному коду, принятых кодов нет
    c, run_id = _run(9730, sales=[1, 2, 3])
    chz_mock.seed_reject(_norm(2))
    r = _build(c, run_id)
    assert _send(c, run_id, r).status_code == 200
    st = c.get(f"/step4/{run_id}/status").json()
    assert st["done"] is True
    states = st["states"]["output"]
    assert states[_norm(1)]["state"] == states[_norm(3)]["state"] == "failed"
    assert "других кодах" in states[_norm(1)]["reason"]
    assert states[_norm(2)]["state"] == "failed" and "других кодах" not in states[_norm(2)]["reason"]
    assert st["done_counts"]["output"] == 0
    docs = _docs(run_id)
    assert {d["status"] for d in docs} == {chz_api.DOC_STATUS_CHECKED_NOT_OK}
    vse = sorted(k for d in docs for k in json.loads(d.get("codes_json") or "[]"))
    assert vse == sorted([_norm(1), _norm(2), _norm(3)])
    assert not _sheet(c, run_id, "Выведено")
    assert sorted(x[1] for x in _sheet(c, run_id, "Ошибки")) == vse


def test_otvet_bez_privyazki_k_kodam_otklonyaet_dokument_celikom():
    codes = [_norm(1), _norm(2)]
    info = {"status": "CHECKED_NOT_OK", "errors": ["Общая ошибка документа"]}
    assert app_mod._partition(info, "CHECKED_NOT_OK", codes) is None
    info = {"status": "CHECKED_NOT_OK", "errors": [{"message": "Нельзя", "cis": _norm(2)}]}
    ok, failed = app_mod._partition(info, "CHECKED_NOT_OK", codes)
    assert ok == [] and failed == {"Нельзя": [_norm(2)], app_mod._NE_PRINYAT_VMESTE: [_norm(1)]}
    info = {"status": "CHECKED_NOT_OK", "errors": [{"message": "Нельзя", "cis": _norm(2)}, "Общая ошибка"]}
    assert app_mod._partition(info, "CHECKED_NOT_OK", codes) is None
    assert app_mod._partition(info, "CHECKED_OK", codes) is None


def test_peresborka_ne_beret_vypolnennye_kody():
    c, run_id = _run(9740, sales=[1, 2, 3])
    chz_mock.seed_reject(_norm(2))
    assert _send(c, run_id, _build(c, run_id)).status_code == 200
    c.get(f"/step4/{run_id}/status")
    # ЧЗ отклонил документ целиком: в новую сборку снова идут все три кода
    r = _build(c, run_id)
    assert r.status_code == 200, r.text
    assert sorted(p["cis"] for p in _doc(r)["products"]) == sorted([_norm(1), _norm(2), _norm(3)])
    chz_mock._reject_codes.clear()
    assert _send(c, run_id, _build(c, run_id, codes=[_norm(1)])).status_code == 200
    c.get(f"/step4/{run_id}/status")
    # выполненный код в списке пропускается с пояснением
    r = _build(c, run_id, codes=[_norm(1), _norm(2)])
    assert [p["cis"] for p in _doc(r)["products"]] == [_norm(2)]
    assert r.json()["skipped"] == [{"cis": _norm(1), "reason": "done"}] and r.json()["note"]
    # без списка: только то, что еще не принято
    r = _build(c, run_id)
    assert sorted(p["cis"] for p in _doc(r)["products"]) == sorted([_norm(2), _norm(3)])
    assert _send(c, run_id, r).status_code == 200
    st = c.get(f"/step4/{run_id}/status").json()
    assert st["done_counts"]["output"] == 3
    assert {d["status"] for d in _docs(run_id)} == {chz_api.DOC_STATUS_CHECKED_OK}
    r = _build(c, run_id)
    assert r.status_code == 400 and "уже обработаны" in r.json()["detail"], r.text
    assert len(_sheet(c, run_id, "Выведено")) == 3 and not _sheet(c, run_id, "Ошибки")


def test_poka_dokument_v_obrabotke_novyj_ne_sobirayut():
    c, run_id = _run(9741, sales=[1, 2])
    assert _send(c, run_id, _build(c, run_id, codes=[_norm(1)])).status_code == 200
    r = _build(c, run_id, codes=[_norm(2)])
    assert r.status_code == 409, r.text


def test_stranica_shaga_4_galochki_schetchik_i_ssylka():
    c, run_id = _run(9742, sales=[1, 2, 3], prices={3: "0"}, returns=[5])
    page = c.get(f"/step4/{run_id}").text
    assert page.count('class="pick"') == 4
    assert 'data-pick="all"' in page and 'data-pick="none"' in page
    assert "Выбрать все" in page and "Снять все" in page
    assert 'id="sel-output">2<' in page and 'id="sel-return">1<' in page
    assert page.count(" checked") == 3, "код без цены не выбран"
    assert "нет цены" in page
    assert f'href="/preview/{run_id}"' in page and "Посмотреть что изменится" in page
    assert page.index("Посмотреть что изменится") < page.index('id="btn-output"')
    # после обработки выбор отключен, строка Выведено
    assert _send(c, run_id, _build(c, run_id)).status_code == 200
    c.get(f"/step4/{run_id}/status")
    page = c.get(f"/step4/{run_id}").text
    assert "Выведено: <b>2</b>" in page and page.count(" disabled>") >= 2


def test_shag_2_bez_plagina_pokazyvaet_tekst_i_ssylku():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "Не найден КриптоПро ЭЦП Browser plug-in. Установите плагин и расширение для браузера, затем обновите страницу." in js
    assert "https://www.cryptopro.ru/products/cades/plugin" in js
    assert "typeof e === 'string'" in js, "плагин отклоняет промис строкой, у нее нет message"
