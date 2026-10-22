"""Второй круг атак на починку: пофайловое схлопывание, ФИАС по ИНН, поиск адреса в ЧЗ."""
from __future__ import annotations

import base64
import json

import httpx

from chzkiz import chz_api, chz_mock, db, wb_api
from chzkiz.web import app as app_mod  # noqa: F401
from tests.test_attacks_mod import _arch, _chz_with, _client, _code, _fin, _rows, _signin, _upload


def test_povtornaya_prodazha_v_sleduyushchem_finotchete_otbrasyvaetsya():
    """Дата выпала из ключа между файлами: код продан и возвращен на прошлой неделе,
    продан снова на этой. Продажа из второго файла совпала с ключом первой и отброшена,
    код считается возвращенным и в панели не выводится."""
    c = _client(9501)
    f1 = _fin([("Продажа", _code(7), 5001, "500", "2026-10-01"),
               ("Возврат", _code(7), 5001, "500", "2026-10-02")])
    f2 = _fin([("Продажа", _code(7), 5002, "700", "2026-10-08")])
    run_id, rows = _upload(c, [("w1.xlsx", f1), ("w2.xlsx", f2)])
    assert len(rows) == 3, [(r["op"], r["date"]) for r in rows]
    _signin(c, run_id)
    assert [r["op"] for r in _rows(run_id)] == [wb_api.OP_SALE]


def test_arhiv_s_finotchetom_teryaet_vtoruyu_prodazhu_posle_vozvrata():
    """Архив дает продажу без даты, финотчет: продажа, возврат, продажа. Обе продажи отчета
    совпали с архивом по коду и отброшены, остался возврат: сверка берет его последним."""
    c = _client(9502)
    f = _fin([("Продажа", _code(7), 5001, "500", "2026-10-01"),
              ("Возврат", _code(7), 5001, "500", "2026-10-02"),
              ("Продажа", _code(7), 5001, "700", "2026-10-03")])
    run_id, rows = _upload(c, [("archive.xlsx", _arch([(5001, _code(7), "500")])), ("report.xlsx", f)])
    _signin(c, run_id)
    assert [r["op"] for r in _rows(run_id)] == [wb_api.OP_SALE], [(r["op"], r["date"]) for r in rows]


def test_klyuch_po_nomeru_zadaniya_skhlopyvaet_raznye_ekzemplyary():
    """В архиве задание 1000 с кодом 1, в финотчете то же задание 1000 с другим кодом 2.
    Ключ #1000 совпал, строка кода 2 отброшена, а ее цена уехала в строку кода 1."""
    c = _client(9503)
    run_id, rows = _upload(c, [
        ("archive.xlsx", _arch([(1000, _code(1), "")])),
        ("report.xlsx", _fin([("Продажа", _code(2), 1000, "700", "2026-10-01")])),
    ])
    assert len(rows) == 1, [(r["cis_raw"][:20], r["product_cost"]) for r in rows]
    assert [r["product_cost"] for r in rows if _code(1) in r["cis_raw"]] == [0]


def test_poisk_adresa_teryaetsya_kogda_pyat_poslednih_dokumentov_otkloneny():
    """doc/list берется с limit=5. Пять свежих отклоненных (в том числе наши же вывода
    без адреса) закрывают принятый документ шестым: адрес не находится никогда."""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == chz_api.DOC_LIST_PATH:
            n = int(request.url.params.get("limit", 0))
            allv = [{"number": f"d{i}", "status": "CHECKED_NOT_OK"} for i in range(5)]
            allv.append({"number": "ok", "status": "CHECKED_OK"})
            return httpx.Response(200, json={"results": allv[:n]})
        number = path.split("/")[-2]
        return httpx.Response(200, json={"status": "CHECKED_OK" if number == "ok" else "CHECKED_NOT_OK",
                                          "senderMod": {"fiasId": "GOOD-FIAS"}})

    chz = chz_api.ChzClient("http://chz.test", token="t", transport=httpx.MockTransport(handler))
    import asyncio
    assert asyncio.run(chz.last_mod_fias("lp")) == "GOOD-FIAS"


def test_tajmaut_odnoj_kartochki_obryvaet_poisk_adresa():
    """try/except ловит только ValueError и AttributeError. Сетевая ошибка на первой
    карточке вылетает из цикла, вторая рабочая карточка не читается."""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == chz_api.DOC_LIST_PATH:
            return httpx.Response(200, json={"results": [{"number": "a"}, {"number": "b"}]})
        if path.split("/")[-2] == "a":
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json={"status": "CHECKED_OK", "senderMod": {"fiasId": "GOOD-FIAS"}})

    chz = chz_api.ChzClient("http://chz.test", token="t", transport=httpx.MockTransport(handler))
    import asyncio
    assert asyncio.run(chz.last_mod_fias("lp")) == "GOOD-FIAS"


def test_staryj_adres_bez_inn_podstavlyaetsya_pri_pustom_inn_progona(monkeypatch):
    """После миграции у человека fias_id от прошлой версии и пустой fias_inn. Прогон с пустым
    inn (signin берет его из тела запроса) сравнивает '' == '' и берет старый адрес без проверки."""
    async def new(self, pg: str = "lp") -> str:
        return "FIAS-NEW"

    monkeypatch.setattr(chz_api.ChzClient, "last_mod_fias", new)
    c = _client(9505)
    with db.connect() as conn:
        uid = db.get_or_create_user(conn, 9505)["id"]
        db.set_user_fias(conn, uid, "FIAS-OLD", "")
    r = c.post("/step1", data={"source": "api"}, follow_redirects=False)
    run_id = r.headers["location"].rsplit("/", 1)[-1]
    _signin(c, run_id)
    with db.connect() as conn:
        conn.execute("UPDATE runs SET inn = '' WHERE id = ?", (run_id,))
        conn.commit()
    r = c.post(f"/step4/{run_id}/build", json={"doc_type": "output"})
    assert r.status_code == 200, r.text
    doc = json.loads(base64.b64decode(r.json()["product_document"]))
    assert doc["fias_id"] == "FIAS-NEW", doc["fias_id"]
