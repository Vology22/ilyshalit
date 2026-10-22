"""Отказ покупателя (не выкупил) и возврат после покупки: разные строки в сверке и отчете."""
from __future__ import annotations

from io import BytesIO

import openpyxl

from chzkiz import chz_api, excel_out, kiz, sverka, wb_api

INN = "7712345678"


def _info(code: str, status: str) -> dict:
    return {kiz.normalize_kiz(code): {"status": status, "ownerInn": INN}}


def _one(reason: str | None, status: str) -> dict:
    code = wb_api._fake_cis("781001")
    it = {"cis_raw": code, "op": wb_api.OP_RETURN, "product_cost": 100, "date": "2026-10-01"}
    if reason:
        it["reason"] = reason
    return sverka.classify_items([it], _info(code, status), INN)[0]


def test_otkaz_kod_v_oborote_nichego_ne_delat():
    r = _one("otkaz", chz_api.STATUS_INTRODUCED)
    assert r["group"] == sverka.GROUP_OK
    assert r["reason"] == "otkaz_v_oborote"


def test_otkaz_kod_vyveden_vernut():
    r = _one("otkaz", chz_api.STATUS_RETIRED)
    assert r["group"] == sverka.GROUP_RETURN
    assert r["reason"] == "otkaz_vyveden"


def test_nastoyaschiy_vozvrat_ne_pomechen_otkazom():
    assert _one(None, chz_api.STATUS_RETIRED)["reason"] == "return_retired"
    assert _one(None, chz_api.STATUS_INTRODUCED)["reason"] == "already_in_turnover"


def test_v_otchete_otkaz_podpisan_otkazom():
    r = _one("otkaz", chz_api.STATUS_INTRODUCED)
    r.update(id=1, chz_status=chz_api.STATUS_INTRODUCED, tovar="")
    wb = openpyxl.load_workbook(BytesIO(excel_out.build_report([r], [])))
    znacheniya = [c for row in wb["Без изменений"].iter_rows(values_only=True) for c in row]
    assert "Отказ" in znacheniya
    assert "Возврат" not in znacheniya
