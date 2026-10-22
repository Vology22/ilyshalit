"""Баланс операций, оплата, рефералы, кабинет и админка."""
from __future__ import annotations

import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from chzkiz import billing, chz_mock, config, db, kiz, payments, wb_api
from chzkiz.web.app import create_app


def _mocksig(value: str) -> str:
    return base64.b64encode(f"MOCKSIG:{value}".encode()).decode("ascii")


def _user(tg_id: int, ref: str = ""):
    with db.connect() as conn:
        return db.get_or_create_user(conn, tg_id, ref)


def _client(tg_id: int, next_page: str = "") -> TestClient:
    c = TestClient(create_app())
    with db.connect() as conn:
        user = db.get_or_create_user(conn, tg_id)
        token = db.create_login_token(conn, user["id"])
    tail = f"?next={next_page}" if next_page else ""
    assert c.post(f"/l/{token}{tail}", follow_redirects=True).status_code == 200
    return c


def _bal(user_id: int) -> int:
    with db.connect() as conn:
        return billing.balance(conn, user_id)


def _signed_in_run(c: TestClient) -> str:
    r = c.post("/step1", data={"source": "api"}, follow_redirects=False)
    run_id = r.headers["location"].split("/")[-1]
    payload = c.post(f"/step2/{run_id}/auth-key").json()
    r = c.post(f"/step2/{run_id}/signin", json={
        "uuid": payload["uuid"], "signature": _mocksig(payload["data"]),
        "inn": chz_mock.DEMO_OUR_INN, "owner": "Тест",
    })
    assert r.status_code == 200
    return run_id


def _send(c: TestClient, run_id: str, doc_type: str):
    body = {"doc_type": doc_type} if doc_type == "output" else {"doc_type": doc_type, "paid": False}
    r = c.post(f"/step4/{run_id}/build", json=body)
    if r.status_code != 200:
        return r
    d = r.json()
    return c.post(f"/step4/{run_id}/send", json={
        "doc_id": d["doc_id"], "chz_type": d["chz_type"],
        "product_document": d["product_document"], "signature": _mocksig(d["product_document"]),
    })


# --- регистрация и баланс ------------------------------------------------------------

def test_novyy_polzovatel_poluchaet_besplatnye_operacii():
    u = _user(7001)
    assert u["balance"] == config.FREE_OPS == 3
    with db.connect() as conn:
        h = billing.history(conn, u["id"])
    assert [(x["kind"], x["delta"], x["balance_after"]) for x in h] == [("welcome", 3, 3)]
    assert _user(7001)["balance"] == 3  # повторный вход не начисляет еще раз


def test_starye_polzovateli_poluchayut_operacii_odin_raz():
    u = _user(7002)
    with db.connect() as conn:
        conn.execute("UPDATE users SET welcome_given = 0, balance = 0 WHERE id = ?", (u["id"],))
        conn.execute("DELETE FROM ledger WHERE user_id = ?", (u["id"],))
        conn.commit()
    db._ready_paths.clear()  # как перезапуск после обновления
    assert _bal(u["id"]) == 3
    db._ready_paths.clear()
    assert _bal(u["id"]) == 3


def test_spisanie_i_vozvrat_za_dokument_odin_raz():
    u = _user(7003)
    with db.connect() as conn:
        run_id = db.create_run(conn, u["id"], "api")
        doc_id = db.create_document(conn, run_id, "LK_RECEIPT", ["c1"])
        assert billing.charge_document(conn, u["id"], doc_id) is True
        assert billing.charge_document(conn, u["id"], doc_id) is False
        assert billing.balance(conn, u["id"]) == 2
        assert billing.refund_document(conn, u["id"], doc_id) is True
        assert billing.refund_document(conn, u["id"], doc_id) is False
        assert billing.balance(conn, u["id"]) == 3


def test_pustoy_balans_ne_daet_spisat():
    u = _user(7004)
    with db.connect() as conn:
        billing.admin_adjust(conn, u["id"], -3, "обнулить")
        run_id = db.create_run(conn, u["id"], "api")
        doc_id = db.create_document(conn, run_id, "LK_RECEIPT", ["c1"])
        with pytest.raises(billing.NotEnoughOps):
            billing.charge_document(conn, u["id"], doc_id)
        assert billing.balance(conn, u["id"]) == 0
        assert db.get_document(conn, doc_id, run_id)["charged"] == 0


# --- списание в сценарии шага 4 --------------------------------------------------------

def test_kazhdyy_dokument_spisyvaet_operaciyu():
    c = _client(7010)
    uid = _user(7010)["id"]
    run_id = _signed_in_run(c)
    assert _send(c, run_id, "output").status_code == 200
    assert _bal(uid) == 2
    assert _send(c, run_id, "return").status_code == 200
    assert _bal(uid) == 1
    assert c.get(f"/step4/{run_id}/status").json()["done"] is True
    assert _bal(uid) == 1  # принятые ЧЗ документы не возвращаются


def test_bez_operaciy_dokument_ne_sobiraetsya():
    c = _client(7011)
    uid = _user(7011)["id"]
    with db.connect() as conn:
        billing.admin_adjust(conn, uid, -3, "обнулить")
    run_id = _signed_in_run(c)
    r = _send(c, run_id, "output")
    assert r.status_code == 402 and "Пополните баланс" in r.json()["detail"]


def test_otklonennyy_chz_dokument_vozvrashchaet_operaciyu():
    c = _client(7012)
    uid = _user(7012)["id"]
    run_id = _signed_in_run(c)
    chz_mock.seed_reject(kiz.normalize_kiz(wb_api._fake_cis("781001")))
    assert _send(c, run_id, "output").status_code == 200
    assert _bal(uid) == 2
    c.get(f"/step4/{run_id}/status")
    assert _bal(uid) == 3
    with db.connect() as conn:
        kinds = [h["kind"] for h in billing.history(conn, uid)]
    assert kinds[:2] == ["refund", "spend"]


def test_sboy_otpravki_vozvrashchaet_operaciyu(monkeypatch):
    from chzkiz.web import app as app_mod

    async def obryv(*a, **k):
        raise httpx.ConnectError("обрыв")

    monkeypatch.setattr(app_mod, "_send_raw_document", obryv)
    c = _client(7013)
    uid = _user(7013)["id"]
    run_id = _signed_in_run(c)
    assert _send(c, run_id, "output").status_code == 502
    assert _bal(uid) == 3


# --- оплата ----------------------------------------------------------------------------

def _tariff():
    with db.connect() as conn:
        return billing.tariffs(conn)[0]


def test_startovye_tarify_est():
    with db.connect() as conn:
        names = [t["name"] for t in billing.tariffs(conn)]
    assert names == ["Старт", "Стандарт", "Бизнес"]


def test_oplata_vyklyuchena_bez_provaydera(monkeypatch):
    monkeypatch.setattr(config, "PAYMENT_PROVIDER", "")
    c = _client(7020)
    page = c.get("/cabinet").text
    assert "Оплата пока не подключена" in page
    assert "disabled>Купить" not in page  # кнопка кликабельна, требование платежной системы
    r = c.post("/cabinet/buy", data={"tariff_id": _tariff()["id"]})
    assert r.status_code == 200 and "Оплата пока недоступна" in r.text
    with db.connect() as conn:
        assert billing.user_payments(conn, _user(7020)["id"]) == []


def test_publichnye_stranicy_i_put_pokupki(monkeypatch):
    monkeypatch.setattr(config, "SUPPORT_TG", "kiz_support")
    monkeypatch.setattr(config, "OPERATOR_NAME", "ИП Тестов Тест Тестович")
    c = TestClient(create_app())  # без входа
    r = c.get("/tariffs")
    assert r.status_code == 200
    for t in ("Старт", "590 ₽", "Купить", "Одна операция - один документ"):
        assert t in r.text
    for page, text in (("/privacy", "Политика конфиденциальности"), ("/terms", "Пользовательское соглашение"),
                       ("/support", "@kiz_support")):
        r = c.get(page)
        assert r.status_code == 200 and text in r.text and "ИП Тестов Тест Тестович" in r.text
        assert 'href="/privacy"' in r.text and 'href="/terms"' in r.text  # ссылки в подвале на каждой странице
    r = c.post("/cabinet/buy", data={"tariff_id": _tariff()["id"]})
    assert r.status_code == 200 and "Войдите, чтобы купить" in r.text


def test_nezapolnennye_rekvizity_vidny():
    r = TestClient(create_app()).get("/terms")
    assert "[заполнить]" in r.text


def test_testovaya_oplata_nachislyaet_operacii(monkeypatch):
    monkeypatch.setattr(config, "PAYMENT_PROVIDER", "stub")
    c = _client(7021)
    uid = _user(7021)["id"]
    t = _tariff()
    r = c.post("/cabinet/buy", data={"tariff_id": t["id"]}, follow_redirects=False)
    assert r.status_code == 303 and "/pay/stub/" in r.headers["location"]
    pay_id = r.headers["location"].rsplit("/", 1)[1]
    assert "Тестовая оплата" in c.get(f"/pay/stub/{pay_id}").text
    r = c.post(f"/pay/stub/{pay_id}", data={"action": "pay"}, follow_redirects=True)
    assert "Оплата прошла" in r.text
    assert _bal(uid) == 3 + t["ops"]
    c.post(f"/pay/stub/{pay_id}", data={"action": "pay"})  # повтор не начисляет второй раз
    assert _bal(uid) == 3 + t["ops"]


def test_chuzhoy_platezh_ne_otkryt(monkeypatch):
    monkeypatch.setattr(config, "PAYMENT_PROVIDER", "stub")
    c1 = _client(7022)
    r = c1.post("/cabinet/buy", data={"tariff_id": _tariff()["id"]}, follow_redirects=False)
    pay_id = r.headers["location"].rsplit("/", 1)[1]
    c2 = _client(7023)
    assert c2.post(f"/pay/stub/{pay_id}", data={"action": "pay"}).status_code == 404
    assert _bal(_user(7022)["id"]) == 3


def test_smena_tarifa_ne_menyaet_vystavlennyy_schet():
    u = _user(7024)
    with db.connect() as conn:
        t = billing.tariffs(conn)[0]
        pid = billing.create_payment(conn, u["id"], t, "stub")
        billing.save_tariff(conn, t["id"], t["name"], 999, 100, True, t["sort"])
        billing.apply_payment(conn, pid)
        assert billing.balance(conn, u["id"]) == 3 + t["ops"]


# --- рефералы --------------------------------------------------------------------------

def _pay(user_id: int) -> str:
    with db.connect() as conn:
        pid = billing.create_payment(conn, user_id, billing.tariffs(conn)[0], "stub")
        assert billing.apply_payment(conn, pid)
    return pid


def test_seller_poluchaet_bonus_tolko_za_pervuyu_oplatu():
    a = _user(7030)
    with db.connect() as conn:
        billing.set_category(conn, a["id"], "seller")
        code = billing.ensure_ref_code(conn, a["id"])
    b = _user(7031, code)
    assert b["referred_by"] == a["id"]
    assert _bal(a["id"]) == 3  # просто регистрация бонуса не дает
    _pay(b["id"])
    assert _bal(a["id"]) == 6
    _pay(b["id"])
    assert _bal(a["id"]) == 6
    with db.connect() as conn:
        st = billing.ref_stats(conn, a["id"])
    assert st["invited"] == 1 and st["paid"] == 1 and st["ops"] == 3


def test_fulfilment_poluchaet_procent_s_kazhdoy_oplaty_i_vyplatu():
    a = _user(7040)
    with db.connect() as conn:
        billing.set_category(conn, a["id"], "fulfillment")
        code = billing.ensure_ref_code(conn, a["id"])
        price = billing.tariffs(conn)[0]["price_kop"]
    b = _user(7041, code)
    _pay(b["id"])
    _pay(b["id"])
    assert _bal(a["id"]) == 3  # операций фулфилменту не дают
    with db.connect() as conn:
        st = billing.ref_stats(conn, a["id"])
        assert st["accrued_kop"] == 2 * price * 20 // 100
        upto = billing.max_accrued_id(conn, a["id"])
    _pay(b["id"])  # пришло, пока админ переводил деньги
    with db.connect() as conn:
        assert billing.mark_paid(conn, a["id"], upto) == 2
        st = billing.ref_stats(conn, a["id"])
    assert st["paid_kop"] == 2 * price * 20 // 100
    assert st["accrued_kop"] == price * 20 // 100


def test_priglashennyy_ne_menyaet_priglasivshego_i_sebya_ne_priglasit():
    a = _user(7050)
    with db.connect() as conn:
        code = billing.ensure_ref_code(conn, a["id"])
    assert _user(7050, code)["referred_by"] is None
    b = _user(7051)
    assert _user(7051, code)["referred_by"] is None  # уже зарегистрирован
    assert _user(7052, "nonexistent")["referred_by"] is None
    assert b["id"]


def test_kategoriyu_vybirayut_odin_raz():
    u = _user(7060)
    with db.connect() as conn:
        assert billing.set_category(conn, u["id"], "seller")
        assert not billing.set_category(conn, u["id"], "fulfillment")
        assert not billing.set_category(conn, u["id"], "boss")
        assert billing.set_category(conn, u["id"], "fulfillment", force=True)
        assert db.get_user(conn, u["id"])["category"] == "fulfillment"


# --- кабинет и вход ---------------------------------------------------------------------

def test_kabinet_pokazyvaet_balans_i_ssylku(monkeypatch):
    monkeypatch.setattr(config, "BOT_USERNAME", "kiz_test_bot")
    c = _client(7070)
    r = c.get("/cabinet")
    assert r.status_code == 200
    assert "операций на балансе" in r.text and "https://t.me/kiz_test_bot?start=ref_" in r.text
    assert "3 опер." in c.get("/step1").text  # баланс в шапке
    c.post("/cabinet/category", data={"category": "fulfillment"})
    assert "Реквизиты для выплат" in c.get("/cabinet").text


def test_vhod_vedet_v_kabinet_i_ne_uvodit_naruzhu():
    c = TestClient(create_app())
    with db.connect() as conn:
        u = db.get_or_create_user(conn, 7071)
        t1 = db.create_login_token(conn, u["id"])
        t2 = db.create_login_token(conn, u["id"])
    r = c.post(f"/l/{t1}?next=/cabinet", follow_redirects=False)
    assert r.headers["location"] == "/cabinet"
    r = c.post(f"/l/{t2}?next=https://evil.example", follow_redirects=False)
    assert r.headers["location"] == "/step1"


# --- админка ----------------------------------------------------------------------------

def test_adminka_tolko_dlya_admina(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TG_IDS", {7081})
    assert _client(7080).get("/admin").status_code == 404
    assert "Админка" not in _client(7080).get("/step1").text
    a = _client(7081)
    r = a.get("/admin")
    assert r.status_code == 200 and "выручка за 30 дней" in r.text
    for page in ("users", "tariffs", "payments", "referrals"):
        assert a.get(f"/admin/{page}").status_code == 200


def test_admin_menyaet_tarif_balans_kategoriyu(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TG_IDS", {7090})
    a = _client(7090)
    u = _user(7091)
    t = _tariff()
    a.post("/admin/tariffs", data={"tariff_id": t["id"], "name": "Старт+", "ops": 12, "price": "650,50", "sort": 1, "active": "1"})
    a.post("/admin/tariffs", data={"name": "Мини", "ops": 3, "price": "199", "sort": 0})
    with db.connect() as conn:
        t2 = billing.get_tariff(conn, t["id"])
        assert (t2["name"], t2["ops"], t2["price_kop"]) == ("Старт+", 12, 65050)
        assert "Мини" not in [x["name"] for x in billing.tariffs(conn)]  # добавлен скрытым
    a.post(f"/admin/users/{u['id']}/balance", data={"delta": 10, "note": "подарок"})
    assert _bal(u["id"]) == 13
    r = a.post(f"/admin/users/{u['id']}/balance", data={"delta": -100, "note": ""})
    assert "Нельзя списать" in r.text and _bal(u["id"]) == 13
    a.post(f"/admin/users/{u['id']}/category", data={"category": "fulfillment"})
    assert _user(7091)["category"] == "fulfillment"


def test_ne_admin_ne_menyaet_balans(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TG_IDS", {1})
    c = _client(7095)
    uid = _user(7095)["id"]
    assert c.post(f"/admin/users/{uid}/balance", data={"delta": 100}).status_code == 404
    assert _bal(uid) == 3


# --- ЮKassa ------------------------------------------------------------------------------

def _yk(monkeypatch, handler):
    monkeypatch.setattr(config, "PAYMENT_PROVIDER", "yookassa")
    monkeypatch.setattr(config, "YOOKASSA_SHOP_ID", "123")
    monkeypatch.setattr(config, "YOOKASSA_SECRET_KEY", "test_key")
    real = payments.YooKassaProvider.__init__

    def init(self, transport=None):
        real(self, httpx.MockTransport(handler))

    monkeypatch.setattr(payments.YooKassaProvider, "__init__", init)


def test_yookassa_sozdanie_i_uvedomlenie(monkeypatch):
    seen = {}
    state = {"status": "pending"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            seen["body"] = json.loads(request.content)
            seen["key"] = request.headers["Idempotence-Key"]
            seen["auth"] = request.headers["Authorization"]
            return httpx.Response(200, json={"id": "yk-1", "status": "pending",
                                             "confirmation": {"confirmation_url": "https://yoomoney.example/pay"}})
        return httpx.Response(200, json={"id": "yk-1", "status": state["status"]})

    _yk(monkeypatch, handler)
    c = _client(7100)
    uid = _user(7100)["id"]
    t = _tariff()
    r = c.post("/cabinet/buy", data={"tariff_id": t["id"]}, follow_redirects=False)
    assert r.headers["location"] == "https://yoomoney.example/pay"
    assert seen["body"]["amount"] == {"value": f"{t['price_kop'] // 100}.00", "currency": "RUB"}
    assert seen["auth"].startswith("Basic ") and len(seen["key"]) == 32
    # уведомление с подделанным статусом: верим только ответу ЮKassa
    fake = {"event": "payment.succeeded", "object": {"id": "yk-1", "status": "succeeded"}}
    assert c.post("/pay/yookassa/notify", json=fake).status_code == 200
    assert _bal(uid) == 3
    state["status"] = "succeeded"
    c.post("/pay/yookassa/notify", json=fake)
    c.post("/pay/yookassa/notify", json=fake)
    assert _bal(uid) == 3 + t["ops"]


def test_yookassa_chek_trebuet_email(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"id": "yk-2", "confirmation": {"confirmation_url": "https://x"}})

    _yk(monkeypatch, handler)
    monkeypatch.setattr(config, "YOOKASSA_RECEIPT", True)
    c = _client(7101)
    r = c.post("/cabinet/buy", data={"tariff_id": _tariff()["id"]})
    assert "нужен email" in r.text
    c.post("/cabinet/profile", data={"email": "a@b.ru"})
    r = c.post("/cabinet/buy", data={"tariff_id": _tariff()["id"]}, follow_redirects=False)
    assert r.status_code == 303


# --- бот ---------------------------------------------------------------------------------

async def test_bot_start_s_referalnoy_ssylkoy_i_kategoriya():
    from aiogram.methods import SendMessage

    from tests.test_bot_out import _bot_env, _buttons, _upd
    from tests.test_instrukciya_bot import _cb

    a = _user(7110)
    with db.connect() as conn:
        code = billing.ensure_ref_code(conn, a["id"])
    bot_main, bot, session = _bot_env()
    dp = bot_main.build_dispatcher()
    await dp.feed_update(bot, _upd(7111, f"/start ref_{code}", 1))
    assert _user(7111)["referred_by"] == a["id"]
    sent = [m for m in session.calls if isinstance(m, SendMessage)]
    assert [b.callback_data for b in _buttons(sent[1].reply_markup)] == ["cat:seller", "cat:fulfillment"]
    await dp.feed_update(bot, _cb(7111, "cat:fulfillment", 2, 900))
    assert _user(7111)["category"] == "fulfillment"
    last = [m for m in session.calls if isinstance(m, SendMessage)][-1]
    assert "Готово: Фулфилмент" in last.text
    assert any("/l/" in b.url and "next=/cabinet" in b.url for b in _buttons(last.reply_markup) if b.url)
    await dp.feed_update(bot, _upd(7111, "/info", 3))
    last = [m for m in session.calls if isinstance(m, SendMessage)][-1]
    urls = [b.url for b in _buttons(last.reply_markup)]
    assert [u.rsplit("/", 1)[1] for u in urls] == ["tariffs", "terms", "privacy", "support"]
