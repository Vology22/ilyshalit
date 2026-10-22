"""Чек маркировки с srid продажи из финотчета не считается новым событием кода."""
from __future__ import annotations

from chzkiz import wb_api

GS = chr(29)
KOD = f"010460000000000121ABCDE0001{GS}91EE11{GS}92ZZZZ0001"
META = {"sgtin": {"value": [KOD]}}


def _order(oid, rid, created="2026-09-12T10:00:00Z"):
    return {"id": oid, "rid": rid, "createdAt": created, "price": 150000}


def _fin(srid, oper, dt, shk=None):
    return {"srid": srid, "sellerOperName": oper, "saleDt": dt, "shkId": shk}


def _chek(dt, srid):
    return {"excise_short": KOD, "fiscal_dt": dt, "operation_type_id": 1, "srid": srid}


def _itogi(orders, fin, exc, period=("2026-09-09", "2026-10-09")):
    metas = {o["id"]: META for o in orders}
    return wb_api.sobrat_itogi(orders, {}, metas, fin, exc, *period)


def test_vozvrat_posle_prodazhi_pereveshivaet_chek_sleduyushchego_dnya():
    fin = [
        _fin("s1", "Продажа", "2026-10-04T09:28:00Z"),
        _fin("s1", "Возврат", "2026-10-04T09:35:00Z"),
    ]
    items = _itogi([_order(1, "s1")], fin, [_chek("2026-10-05T00:00:00", "s1")])
    assert len(items) == 1
    assert items[0]["op"] == wb_api.OP_RETURN
    assert items[0]["date"] == "2026-10-04"


def test_chek_bez_parnoj_prodazhi_ostaetsya_sobytiem():
    items = _itogi([_order(1, "s1")], [], [_chek("2026-09-21T00:00:00", "net-takogo")])
    assert len(items) == 1
    assert items[0]["op"] == wb_api.OP_SALE
    assert items[0]["date"] == "2026-09-21"
    assert items[0]["istochnik"] == "чек"


def test_novaya_prodazha_posle_vozvrata_daet_prodazhu():
    fin = [
        _fin("s1", "Продажа", "2026-09-20T10:00:00Z", shk=7),
        _fin("s1", "Возврат", "2026-09-20T11:00:00Z", shk=7),
        _fin("s2", "Продажа", "2026-09-25T10:00:00Z", shk=7),
    ]
    exc = [_chek("2026-09-21T00:00:00", "s1"), _chek("2026-09-26T00:00:00", "s2")]
    items = _itogi([_order(1, "s1"), _order(2, "s2", "2026-09-24T10:00:00Z")], fin, exc)
    assert len(items) == 1
    assert items[0]["op"] == wb_api.OP_SALE
    assert items[0]["date"] == "2026-09-25"


def test_prodazha_v_poslednij_den_perioda_ne_vypadaet_iz_za_cheka():
    fin = [_fin("s1", "Продажа", "2026-10-09T05:03:00Z")]
    items = _itogi([_order(1, "s1")], fin, [_chek("2026-10-10T00:00:00", "s1")])
    assert len(items) == 1
    assert items[0]["op"] == wb_api.OP_SALE
    assert items[0]["date"] == "2026-10-09"
