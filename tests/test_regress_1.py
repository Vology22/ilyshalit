"""Атаки противника: каждый тест тут должен падать на текущем коде. Не коммитить."""
from __future__ import annotations

import base64
import datetime
import io
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import httpx
import openpyxl
import pytest
from fastapi.testclient import TestClient

from chzkiz import chz_api, chz_mock, config, crypto, db, excel_in, kiz, sverka, wb_api
from chzkiz.web.app import create_app

APP_DIR = Path(__file__).resolve().parent.parent
# синтетические коды, не клиентские
GTIN = "04600000000017"
CODE = f"01{GTIN}21ATTACKSER0001"
CODE_RAW = f"{CODE}\\u001d91EE01\\u001d92" + "A" * 44


def _mocksig(value: str) -> str:
    return base64.b64encode(f"MOCKSIG:{value}".encode()).decode("ascii")


def _login(c: TestClient, tg_id: int) -> None:
    with db.connect() as conn:
        user = db.get_or_create_user(conn, tg_id)
        token = db.create_login_token(conn, user["id"])
    r = c.post(f"/l/{token}", follow_redirects=True)
    assert r.status_code == 200


def _new_api_run(c: TestClient, tg_id: int) -> str:
    _login(c, tg_id)
    r = c.post("/step1", data={"source": "api"}, follow_redirects=False)
    assert r.status_code == 302
    return r.headers["location"].rsplit("/", 1)[-1]


def _signin(c: TestClient, run_id: str, inn: str):
    p = c.post(f"/step2/{run_id}/auth-key").json()
    return c.post(
        f"/step2/{run_id}/signin",
        json={"uuid": p["uuid"], "signature": _mocksig(p["data"]), "inn": inn, "owner": "Тест"},
    )


def _signed_in_run(c: TestClient, tg_id: int, inn: str = chz_mock.DEMO_OUR_INN) -> str:
    run_id = _new_api_run(c, tg_id)
    assert _signin(c, run_id, inn).status_code == 200
    return run_id


def _build(c: TestClient, run_id: str, doc_type: str) -> dict:
    r = c.post(f"/step4/{run_id}/build", json={"doc_type": doc_type})
    assert r.status_code == 200
    return r.json()


def _send(c: TestClient, run_id: str, d: dict):
    return c.post(
        f"/step4/{run_id}/send",
        json={
            "doc_id": d["doc_id"],
            "chz_type": d["chz_type"],
            "product_document": d["product_document"],
            "signature": _mocksig(d["product_document"]),
        },
    )


def _report_sheet_codes(c: TestClient, run_id: str, sheet: str) -> list[str]:
    r = c.get(f"/report/{run_id}")
    assert r.status_code == 200
    ws = openpyxl.load_workbook(io.BytesIO(r.content))[sheet]
    return [row[1] for row in ws.iter_rows(min_row=2, values_only=True) if row[1]]


def _xlsx(rows: list[tuple]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in rows:
        ws.append(list(row))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --- 1. отчет и сводка врут: строятся по группам сверки, а не по ответу ЧЗ ----------

def test_report_lists_output_rejected_by_chz():
    c = TestClient(create_app())
    run_id = _signed_in_run(c, 8101)
    r = _send(c, run_id, _build(c, run_id, "output"))
    chz_id = r.json()["chz_doc_id"]
    # ЧЗ отклонил документ вывода
    chz_mock._docs[chz_id].update(polled=True, status=chz_api.DOC_STATUS_CHECKED_NOT_OK)
    st = c.get(f"/step4/{run_id}/status").json()
    assert st["done"] is True
    assert _report_sheet_codes(c, run_id, "Выведено") == [], "ЧЗ отклонил вывод, а отчет пишет коды в Выведено"


def test_report_claims_return_that_was_never_sent():
    c = TestClient(create_app())
    run_id = _signed_in_run(c, 8102)
    _send(c, run_id, _build(c, run_id, "output"))
    st = c.get(f"/step4/{run_id}/status").json()
    assert st["done"] is True
    kinds = {d["doc_type"] for d in st["documents"]}
    assert chz_api.DOC_TYPE_RETURN not in kinds
    assert _report_sheet_codes(c, run_id, "Возвращено") == [], "возврат не отправлялся, а отчет пишет коды в Возвращено"


# --- 2. двойной вывод: нет защиты от повтора ------------------------------------

def test_double_click_output_creates_two_chz_documents():
    c = TestClient(create_app())
    run_id = _signed_in_run(c, 8201)
    d1 = _build(c, run_id, "output")
    d2 = c.post(f"/step4/{run_id}/build", json={"doc_type": "output"})
    if d2.status_code == 200:
        _send(c, run_id, d1)
        _send(c, run_id, d2.json())
    else:
        _send(c, run_id, d1)
        _send(c, run_id, d1)
    receipts = [d for d in chz_mock._docs.values() if d["type"] == chz_api.DOC_TYPE_RECEIPT]
    assert len(receipts) == 1, f"в ЧЗ ушло {len(receipts)} документа вывода на одни и те же коды"


def test_output_offered_again_after_chz_accepted_it():
    c = TestClient(create_app())
    run_id = _signed_in_run(c, 8202)
    _send(c, run_id, _build(c, run_id, "output"))
    assert c.get(f"/step4/{run_id}/status").json()["done"] is True
    r = c.post(f"/step4/{run_id}/build", json={"doc_type": "output"})
    assert r.status_code != 200, "после принятого вывода панель снова собирает вывод тех же кодов"


def test_send_accepts_doc_id_of_other_users_run():
    a = TestClient(create_app())
    run_a = _signed_in_run(a, 8203)
    doc_a = _build(a, run_a, "output")
    b = TestClient(create_app())
    run_b = _signed_in_run(b, 8204)
    doc_b = _build(b, run_b, "output")
    forged = dict(doc_b, doc_id=doc_a["doc_id"])
    _send(b, run_b, forged)
    with db.connect() as conn:
        row = db.get_document(conn, doc_a["doc_id"], run_a)
    assert row["chz_doc_id"] is None, "чужой запуск переписал документ пользователя A"


# --- 3. продажа и возврат одного КИЗ в одном отчете -----------------------------

def test_sale_and_return_of_same_kiz_is_not_withdrawn():
    content = _xlsx([("Код маркировки", "Тип документа"), (CODE_RAW, "Продажа"), (CODE_RAW, "Возврат")])
    items = excel_in.parse_report(content)
    info = {CODE: {"cis": CODE, "status": chz_api.STATUS_INTRODUCED, "ownerInn": "7700000000"}}
    rows = sverka.classify_items([dict(it, product_cost=0) for it in items], info, "7700000000")
    groups = [r["group"] for r in rows]
    assert sverka.GROUP_OUTPUT not in groups, f"товар вернулся, а код идет на вывод: {groups}"


# --- 4. xlsx-бомба размерности -------------------------------------------------

def test_xlsx_dimension_bomb_parsed_without_memory_blowup(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "Код маркировки"
    ws["XFD1048576"] = "x"
    path = tmp_path / "bomb.xlsx"
    wb.save(path)
    assert path.stat().st_size < config.UPLOAD_MAX_BYTES
    script = (
        "import resource,sys\n"
        "lim=1536*1024*1024\n"
        "resource.setrlimit(resource.RLIMIT_AS,(lim,lim))\n"
        "from chzkiz import excel_in\n"
        "data=open(sys.argv[1],'rb').read()\n"
        "try:\n"
        "    excel_in.parse_report(data)\n"
        "except excel_in.ExcelColumnError:\n"
        "    pass\n"
        "print('OK')\n"
    )
    try:
        p = subprocess.run(
            [sys.executable, "-c", script, str(path)],
            cwd=str(APP_DIR), capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("файл в 5 КБ разбирается дольше 60 секунд")
    assert p.returncode == 0 and "OK" in p.stdout, p.stderr[-300:]


# --- 5. загрузка не того файла: 500 вместо понятной ошибки ----------------------

def _zip_with_xlsx() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("report.xlsx", _xlsx([("Код маркировки", "Тип документа"), (CODE_RAW, "Продажа")]))
    return buf.getvalue()


@pytest.mark.parametrize(
    "name,payload",
    [("report.xlsx", b""), ("report.xlsx", b"not an xlsx at all"), ("report.zip", "zip")],
)
def test_upload_wrong_file_gives_400_not_500(name, payload):
    if payload == "zip":
        # zip без xlsx внутри; zip с отчетом теперь узнается по содержимому и принимается
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("readme.txt", b"no report")
        payload = buf.getvalue()
    c = TestClient(create_app(), raise_server_exceptions=False)
    _login(c, 8501)
    r = c.post(
        "/step1",
        data={"source": "excel"},
        files={"file": (name, payload, "application/octet-stream")},
        follow_redirects=False,
    )
    assert r.status_code == 400, f"{name}: {r.status_code} {r.text[:80]}"


def test_upload_several_reports_and_nested_zip():
    # экземпляры различаются серийником, а не криптохвостом: сверка сравнивает коды
    # после нормализации, и хвост в сравнение не идет
    code2 = CODE_RAW.replace("ATTACKSER0001", "ATTACKSER0002")
    code3 = CODE_RAW.replace("ATTACKSER0001", "ATTACKSER0003")
    rep1 = _xlsx([("Код маркировки", "Тип документа"), (CODE_RAW, "Продажа")])
    rep2 = _xlsx([("Код маркировки", "Тип документа"), (code2, "Возврат")])
    rep3 = _xlsx([("Код маркировки", "Тип документа"), (code3, "Продажа")])
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("848854275.xlsx", rep2)
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as z:
        z.writestr("848854275.zip", inner.getvalue())
        z.writestr("848854273.xlsx", rep3)
        z.writestr("__MACOSX/._848854273.xlsx", b"junk")
    c = TestClient(create_app(), raise_server_exceptions=False)
    _login(c, 8502)
    r = c.post(
        "/step1",
        data={"source": "excel"},
        files=[
            ("file", ("a.xlsx", rep1, "application/octet-stream")),
            ("file", ("archive.zip", outer.getvalue(), "application/octet-stream")),
            ("file", ("a_again.xlsx", rep1, "application/octet-stream")),
        ],
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text[:200]
    run_id = r.headers["location"].rsplit("/", 1)[-1]
    with db.connect() as conn:
        rows = conn.execute("SELECT cis_raw, op FROM items WHERE run_id = ?", (run_id,)).fetchall()
    assert len(rows) == 3, rows
    assert sorted(r[1] for r in rows) == ["return", "sale", "sale"]


def test_upload_skips_file_without_kiz_column():
    rep = _xlsx([("Код маркировки", "Тип документа"), (CODE_RAW, "Продажа")])
    tasks = _xlsx([("№ задания", "QR-код поставки", "Стикер"), ("1", "WB-GI-1", "123")])
    arc = io.BytesIO()
    with zipfile.ZipFile(arc, "w") as z:
        z.writestr("848854273.xlsx", rep)
    c = TestClient(create_app(), raise_server_exceptions=False)
    _login(c, 8503)
    r = c.post(
        "/step1",
        data={"source": "excel"},
        files=[
            ("file", ("archive (4).xlsx", tasks, "application/octet-stream")),
            ("file", ("archive.zip", arc.getvalue(), "application/octet-stream")),
        ],
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text[:300]
    run_id = r.headers["location"].rsplit("/", 1)[-1]
    with db.connect() as conn:
        n = conn.execute("SELECT count(*) FROM items WHERE run_id = ?", (run_id,)).fetchone()[0]
        skipped = conn.execute("SELECT skipped_files FROM runs WHERE id = ?", (run_id,)).fetchone()[0]
    assert n == 1
    assert skipped == "archive (4).xlsx"

    r = c.post(
        "/step1",
        data={"source": "excel"},
        files=[
            ("file", ("archive (4).xlsx", tasks, "application/octet-stream")),
            ("file", ("tasks2.xlsx", tasks, "application/octet-stream")),
        ],
        follow_redirects=False,
    )
    assert r.status_code == 400 and "Ни в одном файле" in r.text
    r = c.post(
        "/step1",
        data={"source": "excel"},
        files=[("file", ("archive (4).xlsx", tasks, "application/octet-stream"))],
        follow_redirects=False,
    )
    assert r.status_code == 400 and "Не нашел колонку с КИЗ" in r.text


# --- 6. сбой ЧЗ: 500 наружу, битые коды уходят в ЧЗ -----------------------------

def test_status_survives_chz_timeout(monkeypatch):
    c = TestClient(create_app(), raise_server_exceptions=False)
    run_id = _signed_in_run(c, 8601)
    _send(c, run_id, _build(c, run_id, "output"))

    async def boom(self, doc_id):
        raise httpx.ConnectTimeout("timeout")

    monkeypatch.setattr(chz_api.ChzClient, "doc_info", boom)
    r = c.get(f"/step4/{run_id}/status")
    assert r.status_code == 200 and r.json()["done"] is False, r.status_code


def test_signin_survives_chz_timeout(monkeypatch):
    c = TestClient(create_app(), raise_server_exceptions=False)
    run_id = _new_api_run(c, 8602)

    async def boom(self, codes):
        raise httpx.ReadTimeout("timeout")

    monkeypatch.setattr(chz_api.ChzClient, "cises_info", boom)
    r = _signin(c, run_id, chz_mock.DEMO_OUR_INN)
    assert r.status_code in (400, 502, 503, 504), r.status_code


def test_bad_format_codes_are_not_sent_to_chz(monkeypatch):
    sent: list[str] = []
    orig = chz_api.ChzClient.cises_info

    async def spy(self, codes):
        sent.extend(codes)
        return await orig(self, codes)

    monkeypatch.setattr(chz_api.ChzClient, "cises_info", spy)
    c = TestClient(create_app())
    _signed_in_run(c, 8603)
    bad = [x for x in sent if not kiz.is_well_formed(x)]
    assert bad == [], f"в ЧЗ ушли коды, которые сверка сама считает битыми: {bad}"


# --- 7. ИНН из браузера: пустой ИНН выключает проверку владельца ------------------

def test_empty_inn_disables_owner_check():
    c = TestClient(create_app())
    run_id = _signed_in_run(c, 8701, inn="")
    with db.connect() as conn:
        rows = [dict(r) for r in db.get_items(conn, run_id)]
    foreign = [r for r in rows if r["owner_inn"] == chz_mock.DEMO_OTHER_INN]
    assert foreign, "в демо нет кода с чужим ИНН"
    assert all(r["group"] == sverka.GROUP_ERROR for r in foreign), (
        "код чужой фирмы попал в Вывести, потому что ИНН пустой"
    )


def test_inn_regex_takes_company_inn_from_legal_entity_cert():
    js = (APP_DIR / "chzkiz" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    m = re.search(r"function chzExtractInn\(owner\) \{\s*var m = /(.+?)/i\.exec", js)
    assert m, "не нашел регулярку chzExtractInn"
    rx = re.compile(m.group(1), re.IGNORECASE)
    # SubjectName КЭП юрлица после 2022: ИНН ЮЛ (фирма) и ИНН (физлицо, директор)
    subject = "CN=ООО Тест, O=ООО Тест, ИНН ЮЛ=7700000000, ОГРН=1027700000000, SN=Иванов, ИНН=770000000001"
    got = rx.search(subject).group(1)
    assert got == "7700000000", f"взят ИНН {got} вместо ИНН фирмы"


# --- 8. демо и боевой режим протекают друг в друга -------------------------------

def test_real_mode_without_wb_key_gets_no_demo_codes(monkeypatch):
    c = TestClient(create_app())
    _login(c, 8801)
    monkeypatch.setattr(config, "CHZ_MODE", "real")
    c.post("/step1", data={"source": "api"}, follow_redirects=False)
    with db.connect() as conn:
        raw = [r["cis_raw"] for r in conn.execute("SELECT cis_raw FROM items").fetchall()]
    fake = [x for x in raw if "46031234567890" in x or x == "не_код_вообще"]
    assert fake == [], f"в боевом режиме пользователю без ключа подсунуты демо-коды: {len(fake)}"


def test_mock_mode_does_not_pull_real_wb_orders(monkeypatch):
    called: list[int] = []

    async def fake_fetch(self, days_back=7):
        called.append(1)
        return []

    monkeypatch.setattr(wb_api.WbClient, "fetch_items", fake_fetch)
    c = TestClient(create_app())
    _login(c, 8802)
    with db.connect() as conn:
        user = db.get_or_create_user(conn, 8802)
        db.set_wb_key(conn, user["id"], crypto.encrypt_str("test-key-not-real"))
    c.post("/step1", data={"source": "api"}, follow_redirects=False)
    assert called == [], "в демо-режиме панель пошла в настоящий WB с ключом клиента"


# --- 9. источник WB по API: поля не совпадают с документацией WB -----------------

async def test_wb_fetch_items_with_documented_responses(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/api/v3/orders":
            return httpx.Response(200, json={"next": 0, "orders": [{"id": 101, "price": 1014, "nmId": 1}]})
        if p == "/api/v3/orders/status":
            return httpx.Response(200, json={"orders": [{"id": 101, "supplierStatus": "complete", "wbStatus": "sold"}]})
        if p == "/api/marketplace/v3/orders/meta":
            return httpx.Response(200, json={"orders": [{"id": 101, "meta": {
                "imei": {"value": None}, "uin": {"value": None}, "gtin": {"value": None},
                "sgtin": {"value": [CODE]}, "expiration": {"value": None}}}]})
        return httpx.Response(404, json={})

    real = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    items = await wb_api.WbClient("test-key-not-real").fetch_items()
    assert [(i["cis_raw"], i["op"]) for i in items] == [(CODE, wb_api.OP_SALE)], items


# --- 10. форма ответа cises/info (НАДО ПРОВЕРИТЬ на живом ЧЗ) ----------------------

async def test_cises_info_parses_list_shaped_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"cisInfo": {"requestedCis": CODE, "cis": CODE,
                                                       "status": "INTRODUCED", "ownerInn": "7700000000"}}])

    cl = chz_api.ChzClient("https://chz.invalid", token="t", transport=httpx.MockTransport(handler))
    res = await cl.cises_info([CODE])
    assert res.get(CODE, {}).get("status") == "INTRODUCED" or res.get(CODE, {}).get("cisInfo", {}).get("status") == "INTRODUCED"


# --- 11. GS в xlsx, записанный Excel как _x001D_ -----------------------------------

def test_xml_escaped_gs_in_xlsx_is_normalized():
    raw = f"{CODE}_x001D_91EE01_x001D_92" + "A" * 44
    content = _xlsx([("Код маркировки", "Тип документа"), (raw, "Продажа")])
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        xml = b"".join(z.read(n) for n in z.namelist() if n.endswith(".xml"))
    assert b"_x001D_" in xml and b"_x005F_x001D_" not in xml
    items = excel_in.parse_report(content)
    assert kiz.normalize_kiz(items[0]["cis_raw"]) == CODE, items[0]["cis_raw"][:60]


# --- 12. бот ---------------------------------------------------------------------

def _bot_env():
    from aiogram import Bot
    from aiogram.client.session.base import BaseSession
    from aiogram.methods import SendMessage
    from aiogram.types import Chat, Message

    class FakeSession(BaseSession):
        def __init__(self):
            super().__init__()
            self.calls = []

        async def close(self):
            return None

        async def make_request(self, bot, method, timeout=None):
            self.calls.append(method)
            if isinstance(method, SendMessage):
                return Message(message_id=900 + len(self.calls), date=datetime.datetime.now(),
                               chat=Chat(id=method.chat_id, type="private"), text=method.text)
            return True

        async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
            if False:
                yield b""

    from chzkiz.bot import main as bot_main

    session = FakeSession()
    return bot_main, Bot(token="42:TEST", session=session), session


def _upd(uid: int, text: str, n: int):
    from aiogram.types import Chat, Message, Update, User

    return Update(update_id=n, message=Message(
        message_id=n, date=datetime.datetime.now(), chat=Chat(id=uid, type="private"),
        from_user=User(id=uid, is_bot=False, first_name="T"), text=text))


async def test_bot_out_after_start_gives_login_link(monkeypatch):
    bot_main, bot, session = _bot_env()

    async def no_key(raw_key):
        return False

    monkeypatch.setattr(bot_main, "_check_wb_key", no_key)
    dp = bot_main.build_dispatcher()
    await dp.feed_update(bot, _upd(9101, "/start", 1))
    await dp.feed_update(bot, _upd(9101, "/out", 2))
    from tests.test_instrukciya_bot import _cb

    await dp.feed_update(bot, _cb(9101, "sv:go", 3, 902))
    from aiogram.methods import SendMessage

    links = [
        b.url
        for m in session.calls if isinstance(m, SendMessage) and m.reply_markup
        for row in m.reply_markup.inline_keyboard for b in row
        if b.url and "/l/" in b.url
    ]
    assert links, "после /start команда /out съедается как ключ WB, ссылки на панель нет"


async def test_bot_deletes_key_message_with_cyrillic(monkeypatch):
    from aiogram.methods import DeleteMessage

    bot_main, bot, session = _bot_env()
    real = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(lambda req: httpx.Response(401))
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    dp = bot_main.build_dispatcher()
    from tests.test_instrukciya_bot import _cb

    await dp.feed_update(bot, _cb(9102, "sv:api", 1, 901))
    try:
        await dp.feed_update(bot, _upd(9102, "вот ключ: testkeynotreal", 2))
    except Exception:
        pass
    deleted = [m for m in session.calls if isinstance(m, DeleteMessage)]
    assert deleted, "сообщение с ключом осталось в чате: ответа нет, удаления нет"


# --- 13. страница ошибки без экранирования ----------------------------------------

def test_error_page_escapes_headers_from_file():
    c = TestClient(create_app())
    _login(c, 9301)
    content = _xlsx([("<img src=x onerror=alert(1)>", "Тип документа"), ("x", "Продажа")])
    r = c.post("/step1", data={"source": "excel"},
               files={"file": ("r.xlsx", content, "application/octet-stream")}, follow_redirects=False)
    assert r.status_code == 400
    assert "<img src=x" not in r.text, "заголовок из файла вставлен в HTML как есть"


# --- 14. Excel-путь: цена товара теряется, вывод уходит с product_cost 0 ---------

def test_excel_sale_carries_price_into_receipt_doc():
    from chzkiz import chz_docs

    content = _xlsx([
        ("Код маркировки", "Тип документа", "Цена розничная с учетом согласованной скидки"),
        (CODE_RAW, "Продажа", 1500),
    ])
    items = excel_in.parse_report(content)
    doc = chz_docs.build_receipt_doc(
        "7700000000", [{"cis": CODE, "product_cost": items[0].get("product_cost", 0)}]
    )
    assert doc["products"][0]["product_cost"] > 0, "в документ вывода уходит цена 0"
