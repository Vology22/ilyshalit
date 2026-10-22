"""Личный кабинет (баланс, тарифы, оплата, рефералы) и админка. Подключается из app.create_app."""
from __future__ import annotations

import logging
import re
from typing import Any, Callable

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import billing, config, db, payments

log = logging.getLogger("chzkiz.account")

EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
PAYOUT_MAX = 500


def register(
    app: FastAPI,
    templates: Jinja2Templates,
    require_user: Callable[[Request], int],
    error_page: Callable[..., HTMLResponse],
) -> None:
    def require_admin(request: Request) -> int:
        user_id = require_user(request)
        with db.connect() as conn:
            user = db.get_user(conn, user_id)
        if not user or user["tg_id"] not in config.ADMIN_TG_IDS:
            raise HTTPException(status_code=404, detail="Not Found")  # не выдаем, что админка есть
        return user_id

    def back(url: str) -> RedirectResponse:
        return RedirectResponse(url=url, status_code=303)

    # --- личный кабинет -------------------------------------------------------------

    @app.get("/cabinet")
    async def cabinet(request: Request, paid: str = "") -> HTMLResponse:
        user_id = require_user(request)
        notice = ""
        if paid:
            notice = await _check_return(user_id, paid)
        with db.connect() as conn:
            user = db.get_user(conn, user_id)
            code = billing.ensure_ref_code(conn, user_id)
            ctx = {
                "user": user,
                "notice": notice,
                "tariffs": billing.tariffs(conn),
                "history": billing.history(conn, user_id),
                "payments_list": billing.user_payments(conn, user_id),
                "ref_link": billing.ref_link(code),
                "ref": billing.ref_stats(conn, user_id),
                "rewards": billing.user_rewards(conn, user_id),
            }
        ctx.update(
            categories=config.CATEGORIES,
            kind_labels=billing.KIND_LABELS,
            payment_labels=billing.PAYMENT_LABELS,
            reward_labels=billing.REWARD_LABELS,
            rub=billing.rub,
            payments_on=config.payments_enabled(),
            need_email=config.PAYMENT_PROVIDER == "yookassa" and config.YOOKASSA_RECEIPT,
            ref_bonus=config.REF_SELLER_BONUS_OPS,
            ref_percent=config.REF_FF_PERCENT,
        )
        return templates.TemplateResponse(request, "cabinet.html", ctx)

    @app.post("/cabinet/category")
    async def cabinet_category(request: Request, category: str = Form("")) -> RedirectResponse:
        user_id = require_user(request)
        with db.connect() as conn:
            billing.set_category(conn, user_id, category)
        return back("/cabinet")

    @app.post("/cabinet/profile", response_model=None)
    async def cabinet_profile(
        request: Request, email: str = Form(None), payout: str = Form(None)
    ) -> RedirectResponse | HTMLResponse:
        user_id = require_user(request)
        if email is not None:
            email = email.strip()
            if email and not EMAIL_RE.fullmatch(email):
                return error_page("Email выглядит неправильно, проверьте адрес.")
        if payout is not None:
            payout = payout.strip()[:PAYOUT_MAX]
        with db.connect() as conn:
            billing.set_profile(conn, user_id, email=email, payout=payout)
        return back("/cabinet#profile")

    def pay_info(request: Request, title: str, message: str, guest: bool = False) -> HTMLResponse:
        return templates.TemplateResponse(request, "pay_info.html", {
            "title": title, "message": message, "bot": config.BOT_USERNAME if guest else "",
            "back_url": "/tariffs" if guest else "/cabinet", "back_text": "К тарифам" if guest else "Вернуться в кабинет",
        })

    @app.post("/cabinet/buy", response_model=None)
    async def cabinet_buy(request: Request, tariff_id: int = Form(...)) -> RedirectResponse | HTMLResponse:
        try:
            user_id = require_user(request)
        except HTTPException as e:
            if e.status_code != 401:
                raise
            return pay_info(request, "Войдите, чтобы купить",
                            "Покупка проходит в личном кабинете. Откройте бота, нажмите /cabinet и перейдите по ссылке.",
                            guest=True)
        if not config.payments_enabled():
            # кнопка кликабельна всегда (требование платежной системы), пока оплаты нет - заглушка
            return pay_info(request, "Оплата пока недоступна",
                            "Мы подключаем платежный сервис. Оплата появится в ближайшее время, "
                            "а пока можно пользоваться бесплатными операциями.")
        try:
            prov = payments.provider()
        except payments.PaymentError as e:
            return error_page(str(e))
        with db.connect() as conn:
            tariff = billing.get_tariff(conn, tariff_id)
            if not tariff or not tariff["active"]:
                return error_page("Такого тарифа нет. Обновите страницу кабинета.")
            user = db.get_user(conn, user_id)
            payment_id = billing.create_payment(conn, user_id, tariff, prov.name)
            payment = billing.get_payment(conn, payment_id)
        try:
            provider_id, url = await prov.create(
                payment, f"{config.PUBLIC_URL}/cabinet?paid={payment_id}", user["email"] if user else ""
            )
        except (payments.PaymentError, Exception) as e:  # noqa: BLE001 - сеть, ответ провайдера
            log.warning("payment create failed %s: %s", payment_id, e)
            with db.connect() as conn:
                billing.cancel_payment(conn, payment_id)
            msg = str(e) if isinstance(e, payments.PaymentError) else "Платежный сервис не ответил."
            return error_page(f"Не получилось начать оплату. {msg} Попробуйте еще раз чуть позже.")
        with db.connect() as conn:
            billing.set_payment_provider_data(conn, payment_id, provider_id, url)
        return RedirectResponse(url=url, status_code=303)

    async def _check_return(user_id: int, payment_id: str) -> str:
        """Покупатель вернулся с оплаты: уведомление могло еще не дойти - спрашиваем сами."""
        with db.connect() as conn:
            p = billing.get_payment(conn, payment_id)
        if not p or p["user_id"] != user_id:
            return ""
        if p["status"] == "pending" and p["provider"] == "yookassa" and p["provider_id"]:
            await _sync_payment(p)
            with db.connect() as conn:
                p = billing.get_payment(conn, payment_id)
        if p["status"] == "succeeded":
            return f"Оплата прошла: начислено {p['ops']} операций."
        if p["status"] == "canceled":
            return "Оплата отменена, операции не начислены."
        return "Ждем подтверждения оплаты. Обновите страницу через минуту."

    async def _sync_payment(p: Any) -> None:
        try:
            status = await payments.YooKassaProvider().status(p["provider_id"])
        except payments.PaymentError as e:
            log.warning("payment status failed %s: %s", p["id"], e)
            return
        with db.connect() as conn:
            if status == "succeeded":
                billing.apply_payment(conn, p["id"])
            elif status == "canceled":
                billing.cancel_payment(conn, p["id"])

    # --- публичные страницы: тарифы, документы, поддержка ----------------------------------

    @app.get("/tariffs")
    async def tariffs_page(request: Request) -> HTMLResponse:
        with db.connect() as conn:
            items = billing.tariffs(conn)
        return templates.TemplateResponse(request, "tariffs.html", {"tariffs": items, "rub": billing.rub, "d": docs_info()})

    @app.get("/privacy")
    async def privacy(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "privacy.html", {"d": docs_info()})

    @app.get("/terms")
    async def terms(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "terms.html", {"d": docs_info()})

    @app.get("/support")
    async def support(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "support.html", {"d": docs_info()})

    # --- тестовая оплата ---------------------------------------------------------------

    def stub_payment(request: Request, payment_id: str) -> Any:
        user_id = require_user(request)
        if config.PAYMENT_PROVIDER != "stub":
            raise HTTPException(status_code=404, detail="Not Found")
        with db.connect() as conn:
            p = billing.get_payment(conn, payment_id)
        if not p or p["user_id"] != user_id or p["provider"] != "stub":
            raise HTTPException(status_code=404, detail="Платеж не найден.")
        return p

    @app.get("/pay/stub/{payment_id}")
    async def pay_stub_page(request: Request, payment_id: str) -> HTMLResponse:
        p = stub_payment(request, payment_id)
        return templates.TemplateResponse(request, "pay_stub.html", {"p": p, "rub": billing.rub})

    @app.post("/pay/stub/{payment_id}")
    async def pay_stub_do(request: Request, payment_id: str, action: str = Form("")) -> RedirectResponse:
        p = stub_payment(request, payment_id)
        with db.connect() as conn:
            if action == "pay":
                billing.apply_payment(conn, p["id"])
            else:
                billing.cancel_payment(conn, p["id"])
        return back(f"/cabinet?paid={p['id']}")

    # --- уведомления ЮKassa --------------------------------------------------------------

    @app.post("/pay/yookassa/notify")
    async def yookassa_notify(request: Request) -> JSONResponse:
        if config.PAYMENT_PROVIDER != "yookassa":
            raise HTTPException(status_code=404, detail="Not Found")
        try:
            body = await request.json()
            provider_id = str(body["object"]["id"])
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False}, status_code=400)
        with db.connect() as conn:
            p = billing.get_payment_by_provider_id(conn, "yookassa", provider_id)
        if not p:
            return JSONResponse({"ok": True})  # чужой или удаленный платеж - повторять не нужно
        try:
            # тело уведомления не проверяем подписью - статус берем у самой ЮKassa
            status = await payments.YooKassaProvider().status(provider_id)
        except payments.PaymentError:
            return JSONResponse({"ok": False}, status_code=502)  # ЮKassa пришлет уведомление еще раз
        with db.connect() as conn:
            if status == "succeeded":
                billing.apply_payment(conn, p["id"])
            elif status == "canceled":
                billing.cancel_payment(conn, p["id"])
        return JSONResponse({"ok": True})

    # --- админка ---------------------------------------------------------------------------

    def admin_page(request: Request, section: str, **ctx: Any) -> HTMLResponse:
        ctx.update(section=section, rub=billing.rub, categories=config.CATEGORIES,
                   payment_labels=billing.PAYMENT_LABELS, reward_labels=billing.REWARD_LABELS)
        return templates.TemplateResponse(request, "admin.html", ctx)

    @app.get("/admin")
    async def admin_home(request: Request) -> HTMLResponse:
        require_admin(request)
        with db.connect() as conn:
            return admin_page(request, "home", s=billing.admin_summary(conn), due=billing.payouts_due(conn))

    @app.get("/admin/users")
    async def admin_users(request: Request, q: str = "") -> HTMLResponse:
        require_admin(request)
        with db.connect() as conn:
            return admin_page(request, "users", users=billing.admin_users(conn, q), q=q)

    @app.post("/admin/users/{user_id}/category")
    async def admin_user_category(request: Request, user_id: int, category: str = Form("")) -> RedirectResponse:
        require_admin(request)
        with db.connect() as conn:
            billing.set_category(conn, user_id, category, force=True)
        return back(f"/admin/users?q={user_id}")

    @app.post("/admin/users/{user_id}/balance", response_model=None)
    async def admin_user_balance(
        request: Request, user_id: int, delta: int = Form(...), note: str = Form("")
    ) -> RedirectResponse | HTMLResponse:
        require_admin(request)
        if delta == 0:
            return back(f"/admin/users?q={user_id}")
        with db.connect() as conn:
            if not db.get_user(conn, user_id):
                raise HTTPException(status_code=404, detail="Пользователь не найден.")
            try:
                billing.admin_adjust(conn, user_id, delta, note.strip()[:200] or "Корректировка")
            except billing.NotEnoughOps:
                return error_page("Нельзя списать больше, чем есть на балансе.")
        return back(f"/admin/users?q={user_id}")

    @app.get("/admin/tariffs")
    async def admin_tariffs(request: Request) -> HTMLResponse:
        require_admin(request)
        with db.connect() as conn:
            return admin_page(request, "tariffs", tariffs=billing.tariffs(conn, active_only=False))

    @app.post("/admin/tariffs", response_model=None)
    async def admin_tariff_save(
        request: Request,
        tariff_id: str = Form(""),
        name: str = Form(...),
        ops: int = Form(...),
        price: str = Form(...),
        sort: int = Form(0),
        active: str = Form(""),
    ) -> RedirectResponse | HTMLResponse:
        require_admin(request)
        name = name.strip()[:60]
        try:
            price_kop = round(float(price.replace(",", ".").replace(" ", "")) * 100)
        except ValueError:
            return error_page("Цена должна быть числом, например 990 или 990.50.")
        if not name or ops <= 0 or price_kop <= 0:
            return error_page("Нужны название, число операций больше нуля и цена больше нуля.")
        with db.connect() as conn:
            billing.save_tariff(conn, int(tariff_id) if tariff_id.isdigit() else None, name, ops, price_kop,
                                bool(active), sort)
        return back("/admin/tariffs")

    @app.get("/admin/payments")
    async def admin_payments(request: Request) -> HTMLResponse:
        require_admin(request)
        with db.connect() as conn:
            return admin_page(request, "payments", payments_list=billing.admin_payments(conn))

    @app.get("/admin/referrals")
    async def admin_referrals(request: Request) -> HTMLResponse:
        require_admin(request)
        with db.connect() as conn:
            due = [dict(d, upto=billing.max_accrued_id(conn, d["id"])) for d in billing.payouts_due(conn)]
            return admin_page(request, "referrals", due=due, rewards=billing.admin_rewards(conn))

    @app.post("/admin/referrals/{referrer_id}/paid")
    async def admin_referral_paid(request: Request, referrer_id: int, upto: int = Form(...)) -> RedirectResponse:
        require_admin(request)
        with db.connect() as conn:
            billing.mark_paid(conn, referrer_id, upto)
        return back("/admin/referrals")


TODO_MARK = "[заполнить]"


def docs_info() -> dict[str, Any]:
    """Реквизиты и контакты для документов. Незаполненное видно сразу, а не пустым местом."""
    contacts = []
    if config.SUPPORT_TG:
        contacts.append(f"Telegram @{config.SUPPORT_TG}")
    if config.SUPPORT_EMAIL:
        contacts.append(f"email {config.SUPPORT_EMAIL}")
    return {
        "service": config.SERVICE_NAME,
        "site": config.PUBLIC_URL,
        "bot": config.BOT_USERNAME,
        "operator": config.OPERATOR_NAME or TODO_MARK,
        "inn": config.OPERATOR_INN or TODO_MARK,
        "ogrn": config.OPERATOR_OGRN,
        "contacts": ", ".join(contacts) or TODO_MARK,
        "tg": config.SUPPORT_TG,
        "email": config.SUPPORT_EMAIL,
        "hours": config.SUPPORT_HOURS,
        "date": config.DOCS_DATE,
        "free_ops": config.FREE_OPS,
        "ref_bonus": config.REF_SELLER_BONUS_OPS,
        "ref_percent": config.REF_FF_PERCENT,
    }


def nav_info(request: Request | None, session_cookie: str) -> dict[str, Any] | None:
    """Для шапки: баланс и ссылка на админку. None - не вошел."""
    if not isinstance(request, Request):  # шаблон ошибки рендерится без request
        return None
    session_id = request.cookies.get(session_cookie)
    if not session_id:
        return None
    with db.connect() as conn:
        user_id = db.get_session_user_id(conn, session_id)
        user = db.get_user(conn, user_id) if user_id else None
    if not user:
        return None
    return {"balance": user["balance"], "is_admin": user["tg_id"] in config.ADMIN_TG_IDS}
