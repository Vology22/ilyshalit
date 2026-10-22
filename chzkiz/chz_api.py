"""Все имена полей и путей Честного знака (True API) - в одном месте.

Источник: SPEC_API.md (собран 26.09.2026 по markirovka.ru, docs.cryptopro.ru - официальные
страницы у ГИС МТ технически не отдают тело SPA-инструменту чтения, часть полей не подтверждена
буквальной цитатой, см. пометки "не нашел" в SPEC_API.md). Проверить на живой подписи Ильи перед
проверкой в проде: буквенные коды причин, формат ответа cises/info и статусов документа.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

import httpx

from . import config

HOST_PROD = "https://markirovka.crpt.ru"
HOST_SANDBOX = "https://markirovka.sandbox.crptech.ru"

AUTH_KEY_PATH = "/api/v3/true-api/auth/key"
SIMPLE_SIGNIN_PATH = "/api/v3/true-api/auth/simpleSignIn"
CISES_INFO_PATH = "/api/v3/true-api/cises/info"
DOC_CREATE_PATH = "/api/v3/true-api/lk/documents/create"
DOC_INFO_PATH = "/api/v4/true-api/doc/{doc_id}/info"
DOC_LIST_PATH = "/api/v4/true-api/doc/list"

CISES_BATCH_SIZE = 1000

# Статусы кода маркировки (SPEC_API А3: сигнатура точно не подтверждена, эмпирика в песочнице)
STATUS_EMITTED = "EMITTED"
STATUS_APPLIED = "APPLIED"
STATUS_INTRODUCED = "INTRODUCED"
STATUS_RETIRED = "RETIRED"
STATUS_WRITTEN_OFF = "WRITTEN_OFF"
KNOWN_STATUSES = {
    STATUS_EMITTED,
    STATUS_APPLIED,
    STATUS_INTRODUCED,
    STATUS_RETIRED,
    STATUS_WRITTEN_OFF,
}

# Документы: True API, раздел 4.1.8 (вывод) и 4.1.5 (возврат)
DOC_TYPE_RECEIPT = "LK_RECEIPT"  # вывод из оборота
DOC_TYPE_RETURN = "LP_RETURN"  # возврат в оборот
ACTION_DISTANCE = "DISTANCE"  # причина: дистанционная продажа
RETURN_TYPE_REMOTE_SALE = "REMOTE_SALE_RETURN"
PRIMARY_DOC_OTHER = "OTHER"  # вид первичного документа "Прочее", нужно наименование
RETURN_PRIMARY_TYPES = ("RECEIPT", "SALES_RECEIPT", PRIMARY_DOC_OTHER)  # кассовый чек, товарный чек, прочее
DOCUMENT_FORMAT_MANUAL = "MANUAL"

# Статусы обработки документа (SPEC_API А7: официально не подтверждены, эмпирика)
DOC_STATUS_NEW = "NEW"  # у нас в БД - собран, еще не отправлен в ЧЗ (своя пометка, не ответ ЧЗ)
DOC_STATUS_IN_PROGRESS = "IN_PROGRESS"
DOC_STATUS_CHECKED_OK = "CHECKED_OK"
DOC_STATUS_CHECKED_NOT_OK = "CHECKED_NOT_OK"
DOC_STATUS_PROCESSING_ERROR = "PROCESSING_ERROR"
DOC_ACTIVE_STATUSES = (DOC_STATUS_NEW, DOC_STATUS_IN_PROGRESS, DOC_STATUS_CHECKED_OK)
# NEW без chz_doc_id - собран, но не отправлен, можно пересобрать; блокирует повтор только то,
# что уже реально ушло в ЧЗ и висит там (IN_PROGRESS) или принято (CHECKED_OK).
DOC_BLOCKING_STATUSES = (DOC_STATUS_IN_PROGRESS, DOC_STATUS_CHECKED_OK)
# исход документа еще не известен: опрашиваем дальше, сверку не закрываем
DOC_PENDING_STATUSES = (
    DOC_STATUS_IN_PROGRESS, "UNDEFINED", "WAIT_ACCEPTANCE", "WAIT_PARTICIPANT_REGISTRATION", "WAIT_FOR_CONTINUATION",
)

DEFAULT_PG = "lp"


class ChzAuthError(RuntimeError):
    pass


@dataclass
class ChzToken:
    value: str


class ChzClient:
    """Клиент True API. base_url переключается конфигом CHZ_MODE (mock/real).

    transport - для мок-режима: httpx.ASGITransport прямо на наше же FastAPI-приложение,
    без реального похода в сеть на свой же публичный домен (и чтобы работало под TestClient).
    """

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._timeout = timeout
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout, transport=self._transport)

    async def auth_key(self) -> dict[str, Any]:
        async with self._http_client() as client:
            r = await client.get(self.base_url + AUTH_KEY_PATH)
            r.raise_for_status()
            return r.json()

    async def simple_sign_in(self, uuid_: str, signed_data_b64: str) -> str:
        async with self._http_client() as client:
            r = await client.post(
                self.base_url + SIMPLE_SIGNIN_PATH,
                json={"uuid": uuid_, "data": signed_data_b64},
            )
            r.raise_for_status()
            data = r.json()
            token = data.get("token")
            if not token:
                raise ChzAuthError("simpleSignIn не вернул token")
            self.token = token
            return token

    async def cises_info(self, codes: list[str]) -> dict[str, dict[str, Any]]:
        """Возвращает {код: cisInfo}. Бьет запрос на пачки по CISES_BATCH_SIZE."""
        result: dict[str, dict[str, Any]] = {}
        async with self._http_client() as client:
            for i in range(0, len(codes), CISES_BATCH_SIZE):
                batch = codes[i : i + CISES_BATCH_SIZE]
                r = await client.post(
                    self.base_url + CISES_INFO_PATH,
                    json=batch,  # живой ЧЗ ждет голый список, на {"cises": ...} отвечает 400
                    headers=self._headers(),
                )
                if r.status_code == 401:
                    raise ChzAuthError("токен ЧЗ истек (401)")
                r.raise_for_status()
                data = r.json()
                for info in _extract_cis_items(data):
                    cis = info.get("cis")
                    if cis:
                        result[cis] = info
        return result

    async def create_document(
        self, doc_type: str, document: dict[str, Any], signature_b64: str, pg: str = DEFAULT_PG
    ) -> str:
        product_document_b64 = base64.b64encode(
            _json_bytes(document)
        ).decode("ascii")
        async with self._http_client() as client:
            r = await client.post(
                self.base_url + DOC_CREATE_PATH,
                params={"pg": pg},
                json={
                    "document_format": DOCUMENT_FORMAT_MANUAL,
                    "product_document": product_document_b64,
                    "type": doc_type,
                    "signature": signature_b64,
                },
                headers=self._headers(),
            )
            if r.status_code == 401:
                raise ChzAuthError("токен ЧЗ истек (401)")
            r.raise_for_status()
            data = r.json()
            return data.get("id") or data.get("doc_id")

    async def last_mod_fias(self, pg: str = "lp") -> str:
        """Место деятельности из последнего вывода из оборота этого участника.

        Списка МОД наш доступ не отдает, зато в карточке каждого прошлого
        документа вывода лежит senderMod.fiasId. Оттуда и берем, чтобы не
        спрашивать адрес у человека.
        """
        async with self._http_client() as client:
            r = await client.get(
                self.base_url + DOC_LIST_PATH,
                params={"limit": 50, "pg": pg, "documentType": DOC_TYPE_RECEIPT},
                headers=self._headers(),
            )
            r.raise_for_status()
            data = r.json() or {}
            spisok = data.get("results") or data.get("content") or []
            for doc in spisok:
                number = doc.get("number") or doc.get("id")
                if not number:
                    continue
                # адрес берем только из документа, который ЧЗ принял: отклоненный он не сверял
                if doc.get("status") and doc["status"] != DOC_STATUS_CHECKED_OK:
                    continue
                try:
                    info = await client.get(
                        self.base_url + DOC_INFO_PATH.format(doc_id=number),
                        params={"pg": pg},
                        headers=self._headers(),
                    )
                    if info.status_code != 200:
                        continue
                    karta = info.json()
                    if isinstance(karta, list):
                        karta = karta[0] if karta else {}
                    if karta.get("status") and karta["status"] != DOC_STATUS_CHECKED_OK:
                        continue
                    fias = ((karta or {}).get("senderMod") or {}).get("fiasId") or ""
                except (ValueError, AttributeError, httpx.HTTPError):
                    # вместо карточки пришел мусор: смотрим следующий документ
                    continue
                if fias:
                    return str(fias)
        return ""

    async def doc_info(self, doc_id: str) -> dict[str, Any]:
        async with self._http_client() as client:
            r = await client.get(
                self.base_url + DOC_INFO_PATH.format(doc_id=doc_id), headers=self._headers()
            )
            if r.status_code == 401:
                raise ChzAuthError("токен ЧЗ истек (401)")
            r.raise_for_status()
            return r.json()


def _extract_cis_items(data: Any) -> list[dict[str, Any]]:
    """cises/info возвращал в наших тестах то dict {"cisInfo":[...]}, то (не подтверждено
    на живом ЧЗ) список [{"cisInfo": {...}}, ...] - разворачиваем оба варианта в плоский список."""
    if isinstance(data, list):
        out = []
        for entry in data:
            if isinstance(entry, dict) and "cisInfo" in entry:
                out.append(entry["cisInfo"])
            else:
                out.append(entry)
        return out
    if isinstance(data, dict):
        return data.get("cisInfo", data.get("cises", []))
    return []


def _json_bytes(document: dict[str, Any]) -> bytes:
    import json

    return json.dumps(document, ensure_ascii=False, sort_keys=True).encode("utf-8")


def base_url() -> str:
    """URL True API по режиму: mock идет во встроенный поддельный ЧЗ этого же приложения."""
    if config.is_mock():
        return config.PUBLIC_URL + "/mock-chz"
    return HOST_PROD
