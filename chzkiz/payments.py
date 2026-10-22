"""Платежные провайдеры. stub - тестовая страница оплаты без денег, yookassa - ЮKassa API v3.

Статусу из уведомления не верим: по id платежа всегда переспрашиваем провайдера."""
from __future__ import annotations

from typing import Any

import httpx

from . import config

YOOKASSA_API = "https://api.yookassa.ru/v3"


class PaymentError(Exception):
    pass


def _rub(kop: int) -> str:
    return f"{kop // 100}.{kop % 100:02d}"


class StubProvider:
    name = "stub"

    async def create(self, payment: Any, return_url: str, email: str = "") -> tuple[str, str]:
        return payment["id"], f"{config.PUBLIC_URL}/pay/stub/{payment['id']}"

    async def status(self, provider_id: str) -> str:
        return "pending"  # исход заглушки ставит сама страница /pay/stub


class YooKassaProvider:
    name = "yookassa"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not (config.YOOKASSA_SHOP_ID and config.YOOKASSA_SECRET_KEY):
            raise PaymentError("Не заданы YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY")
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=YOOKASSA_API,
            auth=(config.YOOKASSA_SHOP_ID, config.YOOKASSA_SECRET_KEY),
            timeout=20.0,
            transport=self._transport,
        )

    async def create(self, payment: Any, return_url: str, email: str = "") -> tuple[str, str]:
        body: dict[str, Any] = {
            "amount": {"value": _rub(payment["amount_kop"]), "currency": "RUB"},
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": return_url},
            "description": f"Тариф {payment['tariff_name']}"[:128],
            "metadata": {"payment_id": payment["id"]},
        }
        if config.YOOKASSA_RECEIPT:
            if not email:
                raise PaymentError("Для чека нужен email: укажите его в личном кабинете.")
            body["receipt"] = {
                "customer": {"email": email},
                "items": [{
                    "description": f"Операции сверки КИЗ, тариф {payment['tariff_name']}"[:128],
                    "quantity": "1.00",
                    "amount": {"value": _rub(payment["amount_kop"]), "currency": "RUB"},
                    "vat_code": config.YOOKASSA_VAT_CODE,
                    "payment_mode": "full_payment",
                    "payment_subject": "service",
                }],
            }
        async with self._client() as c:
            # Idempotence-Key = наш id: повтор запроса после обрыва не создаст второй платеж
            r = await c.post("/payments", json=body, headers={"Idempotence-Key": payment["id"]})
        if r.status_code >= 400:
            raise PaymentError(f"ЮKassa отказала: {r.status_code} {r.text[:300]}")
        data = r.json()
        url = (data.get("confirmation") or {}).get("confirmation_url") or ""
        if not data.get("id") or not url:
            raise PaymentError("ЮKassa не вернула ссылку на оплату")
        return data["id"], url

    async def status(self, provider_id: str) -> str:
        async with self._client() as c:
            r = await c.get(f"/payments/{provider_id}")
        if r.status_code >= 400:
            raise PaymentError(f"ЮKassa не ответила по платежу: {r.status_code}")
        st = r.json().get("status")
        if st == "succeeded":
            return "succeeded"
        if st == "canceled":
            return "canceled"
        return "pending"  # pending, waiting_for_capture


def provider(transport: httpx.AsyncBaseTransport | None = None) -> StubProvider | YooKassaProvider:
    if config.PAYMENT_PROVIDER == "stub":
        return StubProvider()
    if config.PAYMENT_PROVIDER == "yookassa":
        return YooKassaProvider(transport)
    raise PaymentError("Оплата пока не подключена.")
