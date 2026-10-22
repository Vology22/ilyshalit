"""Все имена полей и путей Wildberries - в одном месте.

Пути и поля источников А и Б сверены с официальной документацией dev.wildberries.ru
(зеркало в markdown - github.com/DragonSigh/wildberries-api-docs, снято 26.09.2026,
сама dev.wildberries.ru - SPA и не отдает тело инструменту чтения страниц напрямую):
- источник А (сборочные заказы FBS + метаданные): dev.wildberries.ru/en/openapi/orders-fbs -
  пути, "orders"/"meta"/"status" и их поля ниже подтверждены примерами ответов на этой странице.
- источник Б (коды маркировки по операциям): dev.wildberries.ru/en/openapi/reports, раздел
  "Report on Products with Mandatory Labeling" (excise-report) - поле excise_short это и есть
  код маркировки (КИЗ) по факту продажи/возврата, подтверждено примером ответа на странице.
  Отчет из PLAN.md (reportDetailByPeriod с полем "kiz") в этой документации не встретился,
  excise-report как источник Б надежнее - оставлен основным, reportDetailByPeriod не используется.
Не подтверждено документацией: точные значения operation_type_id (продажа/возврат) в excise-report -
уточнить эмпирически на живом ключе Ильи, сейчас операции размечает Excel-загрузка руками.
"""
from __future__ import annotations

import asyncio
import codecs
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from . import kiz

ORDERS_BASE = "https://marketplace-api.wildberries.ru"
ANALYTICS_BASE = "https://seller-analytics-api.wildberries.ru"

ORDERS_PATH = "/api/v3/orders"
ORDERS_STATUS_PATH = "/api/v3/orders/status"
ORDERS_META_PATH = "/api/marketplace/v3/orders/meta"
EXCISE_REPORT_PATH = "/api/v1/analytics/excise-report"

# supplierStatus: управляется продавцом
SUPPLIER_STATUS_NEW = "new"
SUPPLIER_STATUS_CONFIRM = "confirm"
SUPPLIER_STATUS_COMPLETE = "complete"
SUPPLIER_STATUS_CANCEL = "cancel"

# wbStatus: статус заказа со стороны WB
WB_STATUS_SOLD = "sold"
WB_STATUS_CANCELED_BY_CLIENT = "canceled_by_client"
WB_STATUS_DECLINED_BY_CLIENT = "declined_by_client"

OP_SALE = "sale"
OP_RETURN = "return"

META_MAX_ATTEMPTS = 3
META_BATCH = 100
CONNECT_MAX_ATTEMPTS = 3
META_RETRY_AFTER_CAP = 30.0


def _retry_after_seconds(response: httpx.Response) -> float:
    value = response.headers.get("Retry-After")
    if value:
        try:
            return min(float(value), META_RETRY_AFTER_CAP)
        except ValueError:
            pass
    return 1.0


async def _request(
    client: httpx.AsyncClient, method: str, url: str, stream: bool = False, **kwargs: Any
) -> httpx.Response:
    # до WB бывает обрыв на рукопожатии (06.10.2026 ConnectTimeout на первом же запросе)
    for attempt in range(CONNECT_MAX_ATTEMPTS):
        try:
            if stream:
                return await client.send(client.build_request(method, url, **kwargs), stream=True)
            return await client.request(method, url, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            if attempt == CONNECT_MAX_ATTEMPTS - 1:
                raise
            await asyncio.sleep(2.0)
    raise RuntimeError("недостижимо")


ORDERS_MAX_SPAN = 30 * 86400 - 1


def _utc(ts: str) -> str:
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")


MSK = timezone(timedelta(hours=3))
FINANCE_BASE = "https://finance-api.wildberries.ru"
FINANCE_PATH = "/api/finance/v1/sales-reports/detailed"
FIN_LIMIT = 100000
FIN_RETRIES = 3
FIN_WAIT = 65.0
FIN_TIMEOUT = 180.0
FIN_CACHE_TTL = 600.0
FIN_FIELDS = ("srid", "shkId", "saleDt", "sellerOperName", "rrdId")
FIN_CHUNK = 65536
FIN_ROW_MAX = 1 << 20
LOOKBACK_DAYS = 60
_FIN_CACHE: dict[tuple[str, str, str], tuple[float, list[dict[str, Any]]]] = {}

WB_STATUSES_OTKAZ = ("canceled_by_client", "declined_by_client", "defect", "canceled")

WARN_NET_FINANCE = (
    "Токен WB без доступа к категории Финансы, поэтому возвраты и перепродажи могут не попасть. "
    "Создайте токен с категориями Маркетплейс, Финансы, Аналитика, только чтение."
)
WARN_FAIL_FINANCE = (
    "Финансовый отчет WB сейчас не отдался, поэтому возвраты и перепродажи могут не попасть. "
    "Повторите сверку через несколько минут."
)
WARN_NET_EXCISE = (
    "Токен WB без доступа к отчету по маркировке, поэтому чеки не учтены. "
    "Создайте токен с категориями Маркетплейс, Финансы, Аналитика, только чтение."
)
WARN_FAIL_EXCISE = (
    "Отчет по маркировке WB сейчас не отдался, поэтому чеки не учтены. "
    "Повторите сверку через несколько минут."
)


async def _pause(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _fin_wait_seconds(response: httpx.Response) -> float:
    try:
        return min(float(response.headers["Retry-After"]), 120.0)
    except (KeyError, ValueError):
        return FIN_WAIT


class _FinRows:
    """Массив строк финотчета по кускам ответа. За три месяца WB отдает около 100 МБ
    по 94 поля в строке, целиком в память это не помещается: от строки остаются FIN_FIELDS."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self._buf = ""
        self._mode = ""
        self._scan = json.JSONDecoder()

    def feed(self, text: str, last: bool = False) -> None:
        buf = self._buf + text
        if not self._mode:
            buf = buf.lstrip(" \t\r\n")
            if buf:
                self._mode = "[" if buf[0] == "[" else "?"
                buf = buf[1:] if self._mode == "[" else buf
        i = 0
        while self._mode == "[":
            while i < len(buf) and buf[i] in " \t\r\n,":
                i += 1
            if i == len(buf):
                break
            if buf[i] == "]":
                self._mode = "]"
                i += 1
                break
            try:
                row, end = self._scan.raw_decode(buf, i)
            except ValueError:
                if last:
                    raise
                break
            if not last and (end == len(buf) or buf[end] not in " \t\r\n,]"):
                break
            if isinstance(row, dict):
                self.rows.append({k: row[k] for k in FIN_FIELDS if k in row})
            i = end
        self._buf = buf[i:]
        if self._mode == "]":
            if self._buf.strip():
                raise ValueError("финотчет WB: лишнее после списка")
            self._buf = ""
        if len(self._buf) > FIN_ROW_MAX:
            raise ValueError("финотчет WB: строка длиннее допустимого")
        if not last:
            return
        if self._mode == "[":
            raise ValueError("финотчет WB: ответ оборван")
        if self._mode == "?" and json.loads(self._buf) is not None:
            raise ValueError("финотчет WB: ответ не список")


async def _fin_rows(response: httpx.Response) -> list[dict[str, Any]]:
    utf8 = codecs.getincrementaldecoder("utf-8-sig")()
    acc = _FinRows()
    async for chunk in response.aiter_bytes(FIN_CHUNK):
        acc.feed(utf8.decode(chunk))
    acc.feed(utf8.decode(b"", True), last=True)
    return acc.rows


def msk_day_unix(value: str, end: bool = False) -> int | None:
    """Начало (или конец) дня ГГГГ-ММ-ДД по Москве, unix-время."""
    try:
        day = datetime.strptime((value or "").strip(), "%Y-%m-%d").replace(tzinfo=MSK)
    except ValueError:
        return None
    return int(day.timestamp()) + (86400 - 1 if end else 0)


def _msk_date(ts: int) -> str:
    return datetime.fromtimestamp(ts, MSK).strftime("%Y-%m-%d")


def _msk_created(value: str) -> str:
    """createdAt заданий и даты финотчета WB приходят в UTC, переводим в московское."""
    value = (value or "").strip()
    if not value:
        return ""
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value[:19]
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(MSK).strftime("%Y-%m-%dT%H:%M:%S")


def _kod31(raw: Any) -> str:
    text = str(raw or "").strip()
    return (kiz.normalize_kiz(text) or "")[:31] or text[:31]


def _meta_codes(meta: dict[str, Any] | None) -> list[str]:
    sgtin = (meta or {}).get("sgtin")
    if isinstance(sgtin, dict):
        value = sgtin.get("value")
        found = value if isinstance(value, list) else [value]
    elif isinstance(sgtin, list):
        found = [y.get("value") if isinstance(y, dict) else y for y in sgtin]
    else:
        found = []
    return [_kod31(z) for z in found if z]


def _last_event(events: list[tuple[str, bool, str]]) -> tuple[str, bool, str] | None:
    # при равном времени продажа сильнее возврата; у чека только дата, и он раньше возврата того же дня:
    # чек подтверждает продажу, что была до него (на данных Ильи и у изи чизи так)
    return max(events, key=lambda e: (e[0], e[1])) if events else None


def sobrat_itogi(
    orders: list[dict[str, Any]],
    statuses: dict[Any, dict[str, Any]],
    metas: dict[Any, dict[str, Any]],
    fin_rows: list[dict[str, Any]],
    excise_rows: list[dict[str, Any]],
    period_from: str,
    period_to: str,
) -> list[dict[str, Any]]:
    """Правило изи чизи: итог по коду это его последнее событие (продажа или возврат)
    из финотчета, заданий, перепродажи и чеков маркировки; в сверку идет код, чье
    последнее событие попало в период по Москве."""
    po_srid: dict[str, list[dict[str, Any]]] = {}
    po_shk: dict[Any, list[dict[str, Any]]] = {}
    for x in fin_rows:
        if x.get("srid"):
            po_srid.setdefault(x["srid"], []).append(x)
        if x.get("shkId"):
            po_shk.setdefault(x["shkId"], []).append(x)
    cheki: dict[str, list[tuple[str, bool, str, str]]] = {}
    for x in excise_rows:
        if str(x.get("operation_type_id")) == "1" and x.get("excise_short"):
            cheki.setdefault(_kod31(x["excise_short"]), []).append(
                ((x.get("fiscal_dt") or "")[:19], True, "чек", x.get("srid") or "")
            )

    def v_periode(ts: str) -> bool:
        return not ts or period_from <= ts[:10] <= period_to

    itog: dict[str, tuple[dict[str, Any], list[tuple[str, bool, str]], str]] = {}
    pary: dict[str, set[Any]] = {}
    failed: list[dict[str, Any]] = []
    videli: set[Any] = set()
    for o in orders:
        order_id = o.get("id")
        if order_id is None or order_id in videli:
            continue
        videli.add(order_id)
        wb_status = (statuses.get(order_id) or {}).get("wbStatus")
        sozdan = _msk_created(o.get("createdAt") or "")
        rid = o.get("rid")
        svoi = po_srid.get(rid, []) if rid else []
        ev: list[tuple[str, bool, str]] = []
        prodazhi: set[str] = set()
        for x in svoi:
            ts = _msk_created(x.get("saleDt") or "")
            if x.get("sellerOperName") == "Продажа":
                ev.append((ts, True, "финотчет"))
                prodazhi.add(rid)
            elif x.get("sellerOperName") == "Возврат":
                ev.append((ts, False, "финотчет"))
        if wb_status == WB_STATUS_SOLD and not any(e[1] for e in ev):
            ev.append((sozdan, True, "задание"))
        if wb_status in WB_STATUSES_OTKAZ:
            ev.append((sozdan, False, "задание"))
        for shk in {x["shkId"] for x in svoi if x.get("shkId")}:
            for x in po_shk.get(shk, []):
                if x.get("srid") != rid and x.get("sellerOperName") in ("Продажа", "Возврат"):
                    ev.append((_msk_created(x.get("saleDt") or ""), x["sellerOperName"] == "Продажа", "перепродажа"))
                    if x["sellerOperName"] == "Продажа":
                        prodazhi.add(x.get("srid"))
        cena = int(o.get("price") or 0)
        if order_id not in metas:
            last = _last_event(ev)
            if last and v_periode(last[0]):
                failed.append(
                    {
                        "order_id": order_id,
                        "cis_raw": "",
                        "op": OP_SALE if last[1] else OP_RETURN,
                        "product_cost": cena,
                        "meta_failed": True,
                    }
                )
            continue
        for kod in _meta_codes(metas[order_id]):
            pary.setdefault(kod, set()).update(prodazhi)
            prev = itog.get(kod)
            if prev and prev[0]["id"] > order_id:
                continue
            itog[kod] = (o, ev, sozdan)

    items: list[dict[str, Any]] = []
    for kod, (o, ev, sozdan) in itog.items():
        # чек парной продажи из финотчета (тот же srid) событием не считается: он выходит днем позже возврата
        last = _last_event(ev + [c[:3] for c in cheki.get(kod, []) if not (c[3] and c[3] in pary[kod])])
        item = {"order_id": o["id"], "cis_raw": kod, "product_cost": int(o.get("price") or 0)}
        if last:
            ts, prodazha, src = last
            if not v_periode(ts):
                continue
            item.update(op=OP_SALE if prodazha else OP_RETURN, date=ts[:10], istochnik=src)
            if not prodazha:
                item["reason"] = "vozvrat_fin" if src in ("финотчет", "перепродажа") else "otkaz"
        else:
            # событий нет: товар еще в пути, в общий счет входит, выводить рано
            if not v_periode(sozdan):
                continue
            item.update(op=OP_SALE, date=sozdan[:10], istochnik="задание", reason="not_sold_yet")
        items.append(item)
    return items + failed


class WbClient:
    def __init__(self, api_key: str, timeout: float = 30.0):
        self.api_key = api_key
        self._timeout = timeout
        self.warnings: list[str] = []

    def _headers(self) -> dict[str, str]:
        return {"Authorization": self.api_key}

    async def get_orders(self, date_from: int, date_to: int | None = None) -> list[dict[str, Any]]:
        # WB отдает заказы не больше чем за 30 дней и без будущего dateTo: месяц берем кусками
        konec = min(date_to or int(time.time()), int(time.time()))
        orders: list[dict[str, Any]] = []
        nachalo = date_from
        while nachalo <= konec:
            kusok = min(nachalo + ORDERS_MAX_SPAN, konec)
            orders.extend(await self._get_orders_kusok(nachalo, kusok))
            nachalo = kusok + 1
        return orders

    async def _get_orders_kusok(self, date_from: int, date_to: int) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": 1000, "next": 0, "dateFrom": date_from, "dateTo": date_to}
        orders: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            while True:
                r = await _request(client, "GET", 
                    ORDERS_BASE + ORDERS_PATH, params=params, headers=self._headers()
                )
                r.raise_for_status()
                data = r.json()
                batch = data.get("orders", [])
                orders.extend(batch)
                nxt = data.get("next")
                if not batch or nxt in (None, params["next"]):
                    break
                params["next"] = nxt
        return orders

    async def get_orders_status(self, order_ids: list[int]) -> dict[int, dict[str, str]]:
        result: dict[int, dict[str, str]] = {}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for i in range(0, len(order_ids), 1000):
                batch = order_ids[i : i + 1000]
                r = await _request(client, "POST", 
                    ORDERS_BASE + ORDERS_STATUS_PATH,
                    json={"orders": batch},
                    headers=self._headers(),
                )
                r.raise_for_status()
                for o in r.json().get("orders", []):
                    result[o["id"]] = {
                        "supplierStatus": o.get("supplierStatus"),
                        "wbStatus": o.get("wbStatus"),
                    }
        return result

    async def get_orders_meta(self, order_ids: list[int]) -> dict[int, dict[str, Any]]:
        """meta пачкой до 100 заказов. Старый GET /api/v3/orders/{id}/meta с 06.10.2026
        отвечает 405. Заказ из пачки, которую WB так и не отдал, в ответ не попадает."""
        result: dict[int, dict[str, Any]] = {}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for i in range(0, len(order_ids), META_BATCH):
                batch = order_ids[i : i + META_BATCH]
                for attempt in range(META_MAX_ATTEMPTS):
                    r = await _request(client, "POST", 
                        ORDERS_BASE + ORDERS_META_PATH,
                        json={"orders": batch},
                        headers=self._headers(),
                    )
                    if r.status_code == 429 and attempt < META_MAX_ATTEMPTS - 1:
                        await asyncio.sleep(_retry_after_seconds(r))
                        continue
                    break
                if r.is_error:
                    continue
                for o in r.json().get("orders", []):
                    if o.get("id") is not None:
                        result[o["id"]] = o.get("meta") or {}
        return result

    async def fetch_items(
        self,
        days_back: int = 7,
        date_from: int | None = None,
        date_to: int | None = None,
    ) -> list[dict[str, Any]]:
        """Сверка по API WB по правилу изи чизи: задания FBS с запасом в 60 дней назад,
        их статусы и коды (meta), дневной финотчет и отчет по маркировке. Итог по коду это
        его последнее событие, в результат идут коды, чье событие попало в период по Москве.
        Предупреждения (нет доступа к финансам или маркировке) остаются в self.warnings.

        date_from/date_to - unix-время периода с шага 1, по умолчанию days_back дней назад.
        Заказ, где meta так и не отдал КИЗ (429 исчерпал повторы либо другая ошибка), не
        пропадает молча: попадает в items меткой meta_failed - вызывающий код (app.py) отделяет
        такие строки от обычных и считает отдельно на шаге 3."""
        self.warnings = []
        now = int(time.time())
        if date_from is None:
            date_from = now - days_back * 86400
        period_from = _msk_date(date_from)
        period_to = _msk_date(date_to or now)
        start = datetime.strptime(period_from, "%Y-%m-%d").replace(tzinfo=MSK) - timedelta(days=LOOKBACK_DAYS)
        orders = await self.get_orders(int(start.timestamp()), date_to)
        if not orders:
            return []
        order_ids = [o["id"] for o in orders if o.get("id") is not None]
        statuses = await self.get_orders_status(order_ids)
        metas = await self.get_orders_meta(order_ids)
        today = datetime.now(MSK).strftime("%Y-%m-%d")
        fin_to = min(period_to, today)
        fin_rows: list[dict[str, Any]] = []
        try:
            fin_rows = await self.get_finance_report(start.strftime("%Y-%m-%d"), fin_to)
        except httpx.HTTPStatusError as e:
            self.warnings.append(WARN_NET_FINANCE if e.response.status_code in (401, 403) else WARN_FAIL_FINANCE)
        except (httpx.HTTPError, ValueError, TypeError, AttributeError, KeyError):
            self.warnings.append(WARN_FAIL_FINANCE)
        excise_rows: list[dict[str, Any]] = []
        try:
            excise_rows = await self.get_excise_report(period_from, fin_to)
        except httpx.HTTPStatusError as e:
            self.warnings.append(WARN_NET_EXCISE if e.response.status_code in (401, 403) else WARN_FAIL_EXCISE)
        except (httpx.HTTPError, ValueError, TypeError, AttributeError, KeyError):
            self.warnings.append(WARN_FAIL_EXCISE)
        return sobrat_itogi(orders, statuses, metas, fin_rows, excise_rows, period_from, period_to)

    async def get_finance_report(self, date_from: str, date_to: str) -> list[dict[str, Any]]:
        """Дневной финотчет продавца. У WB лимит 1 запрос в минуту: на 429 ждем и повторяем,
        ответ на 10 минут кладем в память, чтобы повторная сверка не упиралась в лимит."""
        key = (hashlib.sha256(self.api_key.encode()).hexdigest(), date_from, date_to)
        hit = _FIN_CACHE.get(key)
        if hit and time.time() - hit[0] < FIN_CACHE_TTL:
            return hit[1]
        body: dict[str, Any] = {"dateFrom": date_from, "dateTo": date_to, "limit": FIN_LIMIT, "period": "daily"}
        rows: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=max(self._timeout, FIN_TIMEOUT)) as client:
            while True:
                page = await self._finance_page(client, body)
                rows.extend(page)
                if len(page) < FIN_LIMIT or not page[-1].get("rrdId") or page[-1]["rrdId"] == body.get("rrdid"):
                    break
                body = {**body, "rrdid": page[-1]["rrdId"]}
        now = time.time()
        for k in [k for k, v in _FIN_CACHE.items() if now - v[0] >= FIN_CACHE_TTL]:
            del _FIN_CACHE[k]
        _FIN_CACHE[key] = (now, rows)
        return rows

    async def _finance_page(self, client: httpx.AsyncClient, body: dict[str, Any]) -> list[dict[str, Any]]:
        for attempt in range(FIN_RETRIES + 1):
            r = await _request(
                client, "POST", FINANCE_BASE + FINANCE_PATH, stream=True, json=body, headers=self._headers()
            )
            try:
                if r.status_code != 429 or attempt == FIN_RETRIES:
                    if not r.is_success:
                        await r.aread()
                    r.raise_for_status()
                    return await _fin_rows(r)
                wait = _fin_wait_seconds(r)
            finally:
                await r.aclose()
            await _pause(wait)
        raise RuntimeError("недостижимо")

    async def get_excise_report(
        self, date_from: str, date_to: str, countries: list[str] | None = None
    ) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await _request(client, "POST", 
                ANALYTICS_BASE + EXCISE_REPORT_PATH,
                params={"dateFrom": date_from, "dateTo": date_to},
                json={"countries": countries or []},
                headers=self._headers(),
            )
            r.raise_for_status()
            if r.status_code == 204 or not r.content.strip():
                return []
            return _excise_rows(r.json())


def _excise_rows(payload: Any) -> list[dict[str, Any]]:
    """Строки отчета по маркировке; мусор от WB считаем сбоем, а не пустым отчетом."""
    if not isinstance(payload, dict):
        raise ValueError("отчет маркировки WB: ответ не объект")
    resp = payload.get("response")
    if resp is None:
        return []
    if not isinstance(resp, dict):
        raise ValueError("отчет маркировки WB: response не объект")
    data = resp.get("data")
    if data is None:
        return []
    if not isinstance(data, list):
        raise ValueError("отчет маркировки WB: data не список")
    return [x for x in data if isinstance(x, dict) and x.get("excise_short")]


_FAKE_GTIN = "46031234567890"  # 14 цифр-заглушка для демо-кодов


def _fake_cis(serial: str) -> str:
    return f"01{_FAKE_GTIN}21{serial}\x1d91EE01\x1d92AbCdEf=="


def sample_orders_with_kiz() -> list[dict[str, Any]]:
    """Пример данных без ключа WB - для демо-режима (PLAN.md п.4).

    Набор нарочно бьет во все группы сверки: вывести, вернуть, уже в порядке,
    и все пять причин ошибки (не нашел в ЧЗ, чужой ИНН, не введен в оборот,
    битый формат, дубль). Статусы под эти коды сеет chz_mock.seed_demo().
    """
    return [
        {"order_id": 2001, "cis_raw": _fake_cis("781001"), "op": OP_SALE, "product_cost": 150000, "date": "2026-09-30"},
        {"order_id": 2002, "cis_raw": _fake_cis("781002"), "op": OP_SALE, "product_cost": 99000, "date": "2026-09-29"},
        {"order_id": 2003, "cis_raw": _fake_cis("781003"), "op": OP_RETURN, "product_cost": 210000, "date": "2026-10-01"},
        {"order_id": 2004, "cis_raw": _fake_cis("781004"), "op": OP_SALE, "product_cost": 130000, "date": "2026-10-02"},
        {"order_id": 2005, "cis_raw": "не_код_вообще", "op": OP_SALE, "product_cost": 50000, "date": "2026-10-03"},
        {"order_id": 2006, "cis_raw": _fake_cis("781006"), "op": OP_SALE, "product_cost": 70000, "date": "2026-09-29"},
        {"order_id": 2007, "cis_raw": _fake_cis("781007"), "op": OP_SALE, "product_cost": 88000, "date": "2026-10-01"},
        {"order_id": 2008, "cis_raw": _fake_cis("781001"), "op": OP_SALE, "product_cost": 150000, "date": "2026-09-30"},
    ]
