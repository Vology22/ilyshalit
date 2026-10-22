"""Поддельный Честный знак с тем же HTTP-интерфейсом - для демо без флешки клиента.

Подпись мокается по одной простой договоренности с браузерным chzkiz/web/static/mock_sign.js:
подпись над строкой X - это base64("MOCKSIG:" + X). Никакой настоящей криптографии тут нет.
"""
from __future__ import annotations

import base64
import json
import secrets
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from . import chz_api
from . import kiz as kiz_mod
from . import wb_api

router = APIRouter(prefix="/mock-chz")

DEMO_OUR_INN = "7712345678"
DEMO_OTHER_INN = "9998887766"
DEMO_CERTS = [
    {"id": "1", "owner": "Иванов Иван Иванович", "inn": DEMO_OUR_INN, "valid_to": "2027-06-01"},
    {"id": "2", "owner": "ООО Ромашка Трейд", "inn": "1234567890", "valid_to": "2026-11-15"},
]

_auth_data: dict[str, str] = {}
_tokens: set[str] = set()
_cises: dict[str, dict[str, Any]] = {}
_docs: dict[str, dict[str, Any]] = {}
_reject_codes: set[str] = set()


def reset_state() -> None:
    """Для изоляции тестов между собой - обнулить весь стенд ЧЗ."""
    _auth_data.clear()
    _tokens.clear()
    _cises.clear()
    _docs.clear()
    _reject_codes.clear()


def seed_reject(cis_norm: str) -> None:
    """Документ, где есть этот код, при первом опросе статуса придет CHECKED_NOT_OK с errors -
    так тестируем отказ ЧЗ, не только принятие."""
    _reject_codes.add(cis_norm)


def seed_cis(cis_norm: str, status: str, owner_inn: str) -> None:
    _cises[cis_norm] = {
        "cis": cis_norm, "status": status, "ownerInn": owner_inn, "productName": "Товар для примера"
    }


def seed_demo(our_inn: str = DEMO_OUR_INN) -> None:
    """Статусы для wb_api.sample_orders_with_kiz() - демо без ключей ЧЗ/WB (PLAN.md п.4).

    Бьет во все пять групп сверки: 2001 вывести, 2002 уже выведен, 2003 вернуть,
    2004 чужой ИНН, 2005 битый формат (без сева), 2006 не найден (без сева),
    2007 не введен в оборот, 2008 дубль кода 2001 - обрабатывает порядок в sverka.
    """
    samples = wb_api.sample_orders_with_kiz()

    def n(raw: str) -> str:
        return kiz_mod.normalize_kiz(raw)

    seed_cis(n(samples[0]["cis_raw"]), chz_api.STATUS_INTRODUCED, our_inn)
    seed_cis(n(samples[1]["cis_raw"]), chz_api.STATUS_RETIRED, our_inn)
    seed_cis(n(samples[2]["cis_raw"]), chz_api.STATUS_RETIRED, our_inn)
    seed_cis(n(samples[3]["cis_raw"]), chz_api.STATUS_INTRODUCED, DEMO_OTHER_INN)
    seed_cis(n(samples[6]["cis_raw"]), chz_api.STATUS_EMITTED, our_inn)


def _mock_sign(value: str) -> str:
    return base64.b64encode(f"MOCKSIG:{value}".encode("utf-8")).decode("ascii")


def _check_sign(signature_b64: str, expected_value: str) -> bool:
    try:
        decoded = base64.b64decode(signature_b64).decode("utf-8")
    except Exception:
        return False
    return decoded == f"MOCKSIG:{expected_value}"


def _require_token(request: Request) -> None:
    auth = request.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    if token not in _tokens:
        raise HTTPException(status_code=401, detail="mock: токен ЧЗ не найден или истек")


@router.get("/certs")
async def certs() -> list[dict[str, str]]:
    """Не часть True API - только для мока браузерного выбора сертификата."""
    return DEMO_CERTS


@router.post("/seed")
async def seed(body: dict[str, Any]) -> dict[str, bool]:
    """Роут живет только при CHZ_MODE=mock - посадить свой код в реестр стенда нужным
    статусом для демо-сценария без флешки клиента и без загрузки через Telegram."""
    seed_cis(str(body["cis_norm"]), str(body["status"]), str(body.get("owner_inn", DEMO_OUR_INN)))
    return {"ok": True}


@router.get(chz_api.AUTH_KEY_PATH)
async def auth_key() -> dict[str, str]:
    uid = uuid.uuid4().hex
    data = secrets.token_hex(16)
    _auth_data[uid] = data
    return {"uuid": uid, "data": data}


@router.post(chz_api.SIMPLE_SIGNIN_PATH)
async def simple_sign_in(body: dict[str, Any]) -> dict[str, str]:
    uid = body.get("uuid", "")
    signature = body.get("data", "")
    expected = _auth_data.get(uid)
    if expected is None or not _check_sign(signature, expected):
        raise HTTPException(status_code=400, detail="mock: подпись входа не прошла проверку")
    token = "mock-token-" + uuid.uuid4().hex
    _tokens.add(token)
    return {"token": token}


@router.post(chz_api.CISES_INFO_PATH)
async def cises_info(body: list[str], request: Request) -> list[dict[str, Any]]:
    _require_token(request)
    return [{"cisInfo": _cises[c]} for c in body if c in _cises]


@router.post(chz_api.DOC_CREATE_PATH)
async def documents_create(body: dict[str, Any], request: Request) -> dict[str, str]:
    _require_token(request)
    product_document_b64 = body.get("product_document", "")
    signature_b64 = body.get("signature", "")
    if not _check_sign(signature_b64, product_document_b64):
        raise HTTPException(status_code=400, detail="mock: подпись документа не прошла проверку")
    document = json.loads(base64.b64decode(product_document_b64).decode("utf-8"))
    items = document.get("products") or document.get("products_list") or []
    codes = [p.get("cis") or p.get("ki") for p in items]
    doc_id = uuid.uuid4().hex
    _docs[doc_id] = {
        "type": body.get("type"),
        "codes": codes,
        "status": chz_api.DOC_STATUS_IN_PROGRESS,
        "polled": False,
    }
    return {"id": doc_id}


MOCK_PAST_RECEIPT = "mock-past-receipt"
MOCK_FIAS = "11111111-2222-3333-4444-555555555555"


@router.get(chz_api.DOC_LIST_PATH)
async def doc_list(request: Request) -> dict[str, Any]:
    _require_token(request)
    return {"results": [{"number": MOCK_PAST_RECEIPT, "type": chz_api.DOC_TYPE_RECEIPT}]}


@router.get(chz_api.DOC_INFO_PATH.replace("{doc_id}", "{doc_id}"))
async def doc_info(doc_id: str, request: Request) -> dict[str, Any]:
    _require_token(request)
    if doc_id == MOCK_PAST_RECEIPT:
        # прошлый вывод из оборота: оттуда панель забирает место деятельности
        return {"id": doc_id, "senderMod": {"fiasId": MOCK_FIAS}}
    doc = _docs.get(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="mock: документ не найден")
    if not doc["polled"]:
        doc["polled"] = True
        rejected = [c for c in doc["codes"] if c in _reject_codes]
        if rejected:
            doc["status"] = chz_api.DOC_STATUS_CHECKED_NOT_OK
            doc["errors"] = [f"mock: код отклонен ({c})" for c in rejected]
        else:
            doc["status"] = chz_api.DOC_STATUS_CHECKED_OK
            new_status = (
                chz_api.STATUS_RETIRED
                if doc["type"] == chz_api.DOC_TYPE_RECEIPT
                else chz_api.STATUS_INTRODUCED
            )
            for cis in doc["codes"]:
                if cis in _cises:
                    _cises[cis]["status"] = new_status
    return {"id": doc_id, "status": doc["status"], "errors": doc.get("errors", [])}
