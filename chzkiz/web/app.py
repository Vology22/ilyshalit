"""FastAPI-приложение панели: одноразовый вход, 4 шага сверки, отчет, мок-ЧЗ."""
from __future__ import annotations

import asyncio
import json
import re
import time
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.concurrency import run_in_threadpool
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import chz_api, chz_docs, chz_errors, config, db, excel_in, excel_out, kiz, sverka, wb_api
from ..chz_mock import router as mock_router

SESSION_COOKIE = "sid"
WEB_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
templates.env.globals["chz_labels"] = excel_out.CHZ_STATUS_LABELS


FIAS_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
FIAS_NEEDED = (
    "Честный знак требует место деятельности (код ФИАС) для вывода из оборота, а панель не нашла его сама. "
    "Введите код в поле над кнопкой и повторите. Где взять: личный кабинет Честного знака, Профиль, "
    "Места осуществления деятельности, код ФИАС у адреса склада (36 знаков)."
)


def create_app() -> FastAPI:
    config.check_mode()
    app = FastAPI(title="Сверка КИЗ")
    with db.connect() as conn:
        db.reset_sending(conn)
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")
    if config.is_mock():
        app.include_router(mock_router)

    def chz_client(token: str | None = None) -> chz_api.ChzClient:
        """В моке идет напрямую в это же ASGI-приложение, без реального похода в сеть
        на собственный публичный домен (нужно и для тестов, и от лишней сетевой петли в проде)."""
        transport = httpx.ASGITransport(app=app) if config.is_mock() else None
        return chz_api.ChzClient(chz_api.base_url(), token=token, transport=transport)

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    # GET ссылку не гасит: предпросмотр ссылок в чатах открывает ее раньше человека
    @app.get("/l/{token}", response_model=None)
    async def login_link_page(request: Request, token: str) -> HTMLResponse | RedirectResponse:
        with db.connect() as conn:
            if db.peek_login_token(conn, token) is None:
                if _has_live_session(conn, request):
                    return RedirectResponse(url="/step1", status_code=302)
                return _error_page("Ссылка недействительна или уже использована. Запросите новую в боте, /out.")
        return templates.TemplateResponse(request, "login.html", {"token": token})

    @app.post("/l/{token}", response_model=None)
    async def login_link(request: Request, token: str) -> HTMLResponse | RedirectResponse:
        with db.connect() as conn:
            user_id = db.consume_login_token(conn, token)
            if user_id is None:
                if _has_live_session(conn, request):
                    return RedirectResponse(url="/step1", status_code=303)
                return _error_page("Ссылка недействительна или уже использована. Запросите новую в боте, /out.")
            session_id = db.create_session(conn, user_id)
        resp = RedirectResponse(url="/step1", status_code=303)
        resp.set_cookie(
            SESSION_COOKIE,
            session_id,
            max_age=config.SESSION_TTL_HOURS * 3600,
            httponly=True,
            secure=config.cookie_secure(),
            samesite="lax",
        )
        return resp

    @app.get("/step1")
    async def step1_form(request: Request) -> HTMLResponse:
        user_id = _require_user(request)
        today = time.strftime("%Y-%m-%d")
        month_ago = time.strftime("%Y-%m-%d", time.localtime(time.time() - 29 * 86400))
        with db.connect() as conn:
            user = db.get_user(conn, user_id)
        return templates.TemplateResponse(
            request,
            "step1.html",
            {
                "date_from": month_ago,
                "date_to": today,
                "pg": (user["pg"] if user else None) or config.DEFAULT_PG,
                "pg_options": config.PG_OPTIONS,
            },
        )

    @app.post("/step1")
    async def step1_submit(
        request: Request,
        source: str = Form(...),
        date_from: str = Form(""),
        date_to: str = Form(""),
        pg: str = Form(""),
        file: list[UploadFile] | None = File(None),
        file_fin: list[UploadFile] | None = File(None),
    ) -> Any:
        user_id = _require_user(request)
        meta_failed_count = 0
        api_warnings: list[str] = []
        skipped: list[str] = []
        files_info: dict[str, Any] = {}
        with db.connect() as conn:
            user = db.get_user(conn, user_id)
            if pg and pg in config.PG_OPTIONS:
                db.set_user_pg(conn, user_id, pg)
            run_id = db.create_run(conn, user_id, source)
            if source == "excel":
                uploads = [f for f in (file or []) + (file_fin or []) if f.filename]
                if not uploads:
                    return _error_page("Файл не выбран.")
                reports: list[tuple[str, bytes]] = []
                for up in uploads:
                    content = await up.read()
                    if len(content) > config.UPLOAD_MAX_BYTES:
                        return _error_page(f"Файл {up.filename} больше 20 МБ.")
                    try:
                        reports.extend(excel_in.collect_xlsx(up.filename, content))
                    except excel_in.ExcelColumnError as e:
                        return _error_page(str(e))
                if len(reports) > excel_in.MAX_REPORTS:
                    return _error_page(f"Больше {excel_in.MAX_REPORTS} отчетов за раз, разбейте загрузку.")
                items = []
                parts: list[list[dict[str, Any]]] = []
                for name, content in reports:
                    try:
                        part = await run_in_threadpool(excel_in.parse_report, content)
                    except excel_in.ExcelColumnError as e:
                        # в архив кабинета WB попадают и выгрузки без КИЗ (сборочные задания),
                        # из-за них не валим всю загрузку
                        if len(reports) > 1 and e.no_kiz:
                            skipped.append(name)
                            continue
                        return _error_page(f"{name}: {e}" if len(reports) > 1 else str(e))
                    parts.append(part)
                    files_info.setdefault("files", []).append(
                        {"name": name, "kind": "archive" if any(it.get("archive") for it in part) else "fin", "rows": len(part)}
                    )
                files_info["rows"] = sum(len(part) for part in parts)
                # есть архив заданий: коды считаем только по нему, как у конкурента. Финотчет тогда
                # сверяет строки архива и отдает им цену, своих кодов в сверку не добавляет.
                # Внутри одного файла строки не схлопываем: у кода бывает продажа, возврат и снова продажа.
                parts.sort(key=lambda part: 0 if any(it.get("archive") for it in part) else 1)
                est_arhiv = any(it.get("archive") for part in parts for it in part)
                fin_events = _fin_events(parts) if est_arhiv else {}
                # строки архива ждут своей пары из финотчета (у архива даты нет), строки
                # финотчетов сравниваются вместе с датой: один код продают, возвращают и
                # продают снова, и каждое событие должно остаться отдельной строкой.
                arh_seen: dict[tuple[str, str], dict[str, Any]] = {}
                arh_svoi_klyuchi: dict[int, list[tuple[str, str]]] = {}
                fin_seen: dict[tuple[tuple[str, str], str], dict[str, Any]] = {}
                for part in parts:
                    iz_arhiva = any(it.get("archive") for it in part)
                    svoi_arh: dict[tuple[str, str], dict[str, Any]] = {}
                    svoi_fin: dict[tuple[tuple[str, str], str], dict[str, Any]] = {}
                    for it in part:
                        data = it.get("date") or ""
                        glavnaya = None
                        for k in _excel_keys(it, poisk=True):
                            if k in arh_seen:
                                # экземпляр из архива закрывает ровно одно событие финотчета,
                                # повтор той же строки из второго архива пару не забирает
                                glavnaya = arh_seen[k]
                                if not iz_arhiva:
                                    for kk in arh_svoi_klyuchi.pop(id(glavnaya), []):
                                        arh_seen.pop(kk, None)
                                break
                            if (k, data) in fin_seen:
                                glavnaya = fin_seen[(k, data)]
                                break
                        if glavnaya is not None:
                            # у главной строки цены может не быть, а в отброшенной она есть
                            if not glavnaya.get("product_cost") and it.get("product_cost"):
                                glavnaya["product_cost"] = it["product_cost"]
                            continue
                        if est_arhiv and not iz_arhiva:
                            continue
                        items.append(it)
                        klyuchi = _excel_keys(it, poisk=False)
                        if iz_arhiva:
                            arh_svoi_klyuchi[id(it)] = klyuchi
                            for k in klyuchi:
                                svoi_arh.setdefault(k, it)
                        else:
                            for k in klyuchi:
                                svoi_fin.setdefault((k, data), it)
                    arh_seen.update(svoi_arh)
                    fin_seen.update(svoi_fin)
                bez_wb = not (user and user["wb_key_enc"])
                if any(it.get("archive") for it in items) and not config.is_mock():
                    if bez_wb:
                        items = _apply_archive_statuses(items)
                    else:
                        try:
                            items = await _apply_wb_statuses(user["wb_key_enc"], items)
                        except httpx.HTTPError:
                            return _error_page("Не удалось получить статусы заданий из WB, попробуйте еще раз.")
                if est_arhiv:
                    items = _apply_fin_events(items, fin_events)
                if not config.is_mock() and user and user["wb_key_enc"]:
                    try:
                        items = await _fill_missing_order_dates(user["wb_key_enc"], items)
                    except (httpx.HTTPError, ValueError, TypeError, AttributeError, KeyError):
                        api_warnings.append(
                            "Не удалось получить даты заказов WB; часть дат в отчете останется пустой."
                        )
                if len(skipped) == len(reports):
                    return _error_page(
                        "Ни в одном файле нет колонки с КИЗ (нужен еженедельный отчет о реализации): "
                        + ", ".join(skipped)
                    )
                fbo_count = sum(1 for it in items if it.get("fbo"))
                # продажи со склада WB (FBO) выводит сам WB, сверяем только FBS
                items = [it for it in items if not it.get("fbo")]
                if fbo_count and not items:
                    return _error_page("В отчете только продажи со склада WB (FBO), коды по ним выводит сам WB. Сверять нечего.")
                if any(it.get("need_meta") for it in items) and not config.is_mock() and bez_wb:
                    # без ключа WB код по номеру задания брать негде: такие строки откладываем,
                    # остальное человек сверяет по файлам, как у него и просили
                    bez_koda = sum(1 for it in items if it.get("need_meta"))
                    items = [it for it in items if not it.get("need_meta")]
                    if not items:
                        return _error_page(
                            "В отчете нет ни одного кода маркировки: у этих строк код дает WB "
                            "по номеру задания, для них нужен ключ WB. Загрузите архив заданий "
                            "или подключите токен WB в боте: /start → Подключить WB по API."
                        )
                    meta_failed_count = bez_koda
                if any(it.get("need_meta") for it in items):
                    try:
                        items, meta_failed_count = await _fill_fbs_codes(
                            None if config.is_mock() else user["wb_key_enc"], items
                        )
                    except httpx.HTTPError:
                        return _error_page("Не удалось получить коды заказов FBS из WB, попробуйте еще раз.")
                if not config.is_mock() and user and user["wb_key_enc"] and any(it.get("order_id") for it in items):
                    try:
                        items = await _mark_wb_canceled(user["wb_key_enc"], items)
                    except httpx.HTTPError:
                        return _error_page("Не удалось получить статусы заданий FBS из WB, попробуйте еще раз.")
            elif config.is_mock():
                # боевой ключ WB в мок-режим не пускаем, только демо
                from ..chz_mock import seed_demo

                items = wb_api.sample_orders_with_kiz()
                seed_demo()
            else:
                wb_key_enc = user["wb_key_enc"] if user else None
                if not wb_key_enc:
                    return _error_page("Подключите токен WB в боте: /start → Подключить WB по API.")
                wb_key = _decrypt(wb_key_enc)
                client = wb_api.WbClient(wb_key)
                date_from, date_to, period_warning = _min_period(date_from, date_to)
                if period_warning:
                    api_warnings.append(period_warning)
                df = wb_api.msk_day_unix(date_from)
                dt = wb_api.msk_day_unix(date_to, end=True)  # включительно весь выбранный день по Москве
                try:
                    if df is not None:
                        items = await client.fetch_items(date_from=df, date_to=dt)
                    else:
                        items = await client.fetch_items(days_back=30)
                except httpx.HTTPStatusError as e:
                    if e.response.status_code in (401, 403):
                        return _error_page("WB не принял токен. Проверьте токен в боте: /start → Подключить WB по API.")
                    return _error_page(f"WB не отдал заказы, ответ {e.response.status_code}. Попробуйте еще раз через пару минут.")
                except httpx.HTTPError:
                    return _error_page("Нет связи с WB. Попробуйте еще раз через пару минут.")
                except (ValueError, TypeError, AttributeError, KeyError):
                    return _error_page("WB ответил непонятно. Попробуйте еще раз через пару минут.")
                meta_failed_count = sum(1 for it in items if it.get("meta_failed"))
                items = [it for it in items if not it.get("meta_failed")]
                api_warnings = api_warnings + client.warnings
            files_info["unique"] = len({it["cis_raw"] for it in items})
            db.add_items(
                conn,
                run_id,
                [
                    {
                        "cis_raw": it["cis_raw"],
                        "cis_norm": it["cis_raw"],
                        "op": it["op"],
                        "date": it.get("date", ""),
                        "group": "pending",
                        "reason": it.get("reason", ""),
                        "product_cost": it.get("product_cost", 0),
                        "archive": bool(it.get("archive")),
                        "istochnik": it.get("istochnik"),
                        "order_id": it.get("order_id"),
                    }
                    for it in items
                ],
            )
            if api_warnings:
                db.set_run_warnings(conn, run_id, api_warnings)
            if meta_failed_count:
                db.set_run_meta_failed_count(conn, run_id, meta_failed_count)
            if skipped:
                db.set_run_skipped_files(conn, run_id, skipped)
            db.set_run_files_info(conn, run_id, json.dumps(files_info, ensure_ascii=False))
        return RedirectResponse(url=f"/step2/{run_id}", status_code=302)

    @app.get("/step2/{run_id}")
    async def step2_form(request: Request, run_id: str) -> HTMLResponse:
        user_id = _require_user(request)
        _get_run_or_404(user_id, run_id)
        return templates.TemplateResponse(
            request, "step2.html", {"run_id": run_id, "is_mock": config.is_mock()}
        )

    @app.post("/step2/{run_id}/auth-key")
    async def step2_auth_key(request: Request, run_id: str) -> dict[str, Any]:
        user_id = _require_user(request)
        _get_run_or_404(user_id, run_id)
        return await chz_client().auth_key()

    @app.post("/step2/{run_id}/signin")
    async def step2_signin(request: Request, run_id: str, body: dict[str, Any]) -> dict[str, bool]:
        user_id = _require_user(request)
        _get_run_or_404(user_id, run_id)
        try:
            token = await chz_client().simple_sign_in(body["uuid"], body["signature"])
        except (chz_api.ChzAuthError, httpx.HTTPStatusError):
            raise HTTPException(status_code=400, detail="Вход в Честный знак не прошел.")
        inn = str(body.get("inn", ""))
        owner = str(body.get("owner", ""))
        with db.connect() as conn:
            db.set_run_chz_login(conn, run_id, inn, owner, _encrypt(token))
            raw_items = db.get_items(conn, run_id)
            codes = [it["cis_raw"] for it in raw_items]
            norm_codes = list({_norm(c) for c in codes})
            from .. import kiz as kiz_mod

            well_formed = [c for c in norm_codes if kiz_mod.is_well_formed(c)]
            try:
                chz_info = await chz_client(token).cises_info(well_formed)
            except httpx.HTTPError:
                raise HTTPException(
                    status_code=502, detail="Честный знак не ответил, повторите"
                )
            rows = sverka.classify_items(
                [
                    {
                        "cis_raw": it["cis_raw"],
                        "op": it["op"],
                        "product_cost": it["product_cost"],
                        "date": it["date"] if "date" in it.keys() else "",
                        "reason": it["reason"] if "reason" in it.keys() else "",
                        # откуда строка пришла, нужно для разбивки на шаге 3
                        "archive": "istochnik" in it.keys() and it["istochnik"] == "архив",
                        "istochnik": it["istochnik"] if "istochnik" in it.keys() else None,
                        "order_id": it["order_id"] if "order_id" in it.keys() else 0,
                    }
                    for it in raw_items
                ],
                chz_info,
                inn,
            )
            db.clear_items(conn, run_id)
            db.add_items(conn, run_id, rows)
            db.set_run_status(conn, run_id, "sverka_done")
        return {"ok": True}

    @app.get("/step3/{run_id}")
    async def step3(request: Request, run_id: str) -> HTMLResponse:
        user_id = _require_user(request)
        with db.connect() as conn:
            run = _get_run_or_404(user_id, run_id, conn)
            rows = [dict(r) for r in db.get_items(conn, run_id)]
            polzovatel = db.get_user(conn, user_id)
        bez_wb = not (polzovatel and polzovatel["wb_key_enc"])
        counts = sverka.counts_by_group(rows)
        iz_arhiva = sum(1 for r in rows if r.get("istochnik") == "архив")
        iz_otcheta = sum(1 for r in rows if r.get("istochnik") == "отчет")
        istochniki = (
            f"из архива заданий {iz_arhiva}, из отчета о реализации {iz_otcheta}"
            if iz_arhiva and iz_otcheta
            else ""
        )
        try:
            files_info = json.loads(run["files_info"] or "{}")
        except (KeyError, IndexError, ValueError):
            files_info = {}
        return templates.TemplateResponse(
            request,
            "step3.html",
            {
                "run_id": run_id,
                "files_info": files_info,
                "rows": rows,
                "counts": counts,
                "group_labels": sverka.GROUP_LABELS,
                "reason_labels": sverka.REASON_LABELS,
                "total": len(rows),
                "istochniki": istochniki,
                "our_inn": run["inn"],
                "meta_failed_count": run["meta_failed_count"],
                "skipped_files": run["skipped_files"],
                "warnings": [w for w in (run["warnings"] or "").split(chr(10)) if w],
                "bez_wb": bez_wb,
                "iz_arhiva": iz_arhiva,
            },
        )

    @app.get("/step4/{run_id}")
    async def step4_form(request: Request, run_id: str) -> HTMLResponse:
        user_id = _require_user(request)
        with db.connect() as conn:
            run = _get_run_or_404(user_id, run_id, conn)
            rows = [dict(r) for r in db.get_items(conn, run_id)]
            docs = [dict(d) for d in db.get_documents(conn, run_id)]
        counts = sverka.counts_by_group(rows)
        groups = {
            sverka.GROUP_OUTPUT: _group_view(rows, docs, sverka.GROUP_OUTPUT, chz_api.DOC_TYPE_RECEIPT),
            sverka.GROUP_RETURN: _group_view(rows, docs, sverka.GROUP_RETURN, chz_api.DOC_TYPE_RETURN),
        }
        out = groups[sverka.GROUP_OUTPUT]
        fias_value = await _fias_mod(user_id, run) if out["total"] > out["done"] else ""
        return templates.TemplateResponse(
            request,
            "step4.html",
            {
                "run_id": run_id,
                "counts": counts,
                "groups": groups,
                "documents": docs,
                "is_mock": config.is_mock(),
                "output_locked": out["busy"] > 0,
                "return_locked": groups[sverka.GROUP_RETURN]["busy"] > 0,
                "fias_saved": fias_value,
            },
        )

    async def _fias_mod(user_id: int, run) -> str:
        """Место деятельности для вывода из оборота.

        Один раз берем из прошлых документов вывода в ЧЗ и запоминаем за человеком:
        списка МОД наш доступ не отдает, а спрашивать адрес у человека незачем.
        """
        with db.connect() as conn:
            user = db.get_user(conn, user_id)
        saved = (user["fias_id"] if user and "fias_id" in user.keys() else "") or ""
        saved_inn = (user["fias_inn"] if user and "fias_inn" in user.keys() else "") or ""
        # адрес привязан к участнику: у другого ИНН своя площадка, запомненное не подходит
        if saved and saved_inn and saved_inn == (run["inn"] or ""):
            return saved
        token = _decrypt(run["chz_token_enc"]) if run["chz_token_enc"] else None
        if not token:
            return ""
        pg = (user["pg"] if user else None) or config.DEFAULT_PG
        try:
            fias = await chz_client(token).last_mod_fias(pg)
        except Exception:
            return ""
        if fias:
            with db.connect() as conn:
                db.set_user_fias(conn, user_id, fias, run["inn"] or "")
        return fias

    @app.post("/step4/{run_id}/build")
    async def step4_build(request: Request, run_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """doc_type: 'output' или 'return'. codes - коды из группы (без списка берутся все доступные),
        limit - не больше стольких кодов. Возвращает документ base64 для подписи в браузере."""
        user_id = _require_user(request)
        want = body.get("doc_type")
        if want not in ("output", "return"):
            raise HTTPException(status_code=400, detail="Неизвестный тип документа.")
        if want == "return" and not isinstance(body.get("paid"), bool):
            raise HTTPException(status_code=400, detail="Отметьте, оплатил ли покупатель товар.")
        group = sverka.GROUP_OUTPUT if want == "output" else sverka.GROUP_RETURN
        chz_type = chz_api.DOC_TYPE_RECEIPT if want == "output" else chz_api.DOC_TYPE_RETURN
        with db.connect() as conn:
            run = _get_run_or_404(user_id, run_id, conn)
            rows = [dict(r) for r in db.get_items(conn, run_id)]
        in_group = [r for r in rows if r["group_name"] == group]
        if not in_group:
            raise HTTPException(status_code=400, detail="Нет кодов в этой группе.")
        group_codes = {r["cis_norm"] for r in in_group}
        prior = await _prior_states(chz_client, user_id, run, chz_type, group_codes)
        with db.connect() as conn:
            docs = [dict(d) for d in db.get_documents(conn, run_id)]
        same_type = [d for d in docs if d["doc_type"] == chz_type]
        if any(d["status"] == chz_api.DOC_STATUS_IN_PROGRESS for d in same_type):
            raise HTTPException(
                status_code=409, detail="Документ этого типа сейчас обрабатывается в Честном знаке, обновите шаг 4 позже."
            )
        states = _code_states(same_type, chz_type)
        if prior:
            asked = body.get("codes")
            names = {_norm(c) for c in asked if isinstance(c, str)} if isinstance(asked, list) else group_codes
            if names and names <= prior.keys():
                busy = any(prior[c][0] == STATE_BUSY for c in names)
                raise HTTPException(status_code=409, detail=PRIOR_BUSY if busy else PRIOR_DONE)
            for code, state in prior.items():
                _raise_state(states, code, state)
        picked, skipped = _pick_codes(body, in_group, states, group)
        if prior and not isinstance(body.get("codes"), list):
            skipped.extend({"cis": c, "reason": state[0]} for c, state in sorted(prior.items()))
        fias_id = None
        if want == "output":
            fias_vvod = str(body.get("fias") or "").strip().lower()
            if fias_vvod:
                if not FIAS_RE.fullmatch(fias_vvod):
                    raise HTTPException(
                        status_code=400,
                        detail="Код ФИАС выглядит не так: нужно 36 знаков вида 0c5b2444-70a0-4932-980c-b4dc0d3f02b5.",
                    )
                with db.connect() as conn:
                    db.set_user_fias(conn, user_id, fias_vvod, run["inn"] or "")
            fias_id = fias_vvod or await _fias_mod(user_id, run)
            if not fias_id:
                raise HTTPException(status_code=400, detail=FIAS_NEEDED)
            document = chz_docs.build_receipt_doc(
                run["inn"],
                [{"cis": r["cis_norm"], "product_cost": r["product_cost"]} for r in picked],
                fias_id=fias_id,
            )
        else:
            paid = body.get("paid") is True
            document = chz_docs.build_return_doc(
                run["inn"],
                [{"cis": r["cis_norm"], "paid": paid} for r in picked],
                _return_primary(body) if paid else None,
            )
        import base64

        doc_b64 = base64.b64encode(json.dumps(document, ensure_ascii=False, sort_keys=True).encode()).decode()
        codes = [r["cis_norm"] for r in picked]
        with db.connect() as conn:
            # документ, не дошедший до ЧЗ, пересобираем на месте; принятые и отклоненные остаются историей
            reusable = next(
                (d for d in same_type if not d["chz_doc_id"]
                 and d["status"] in (chz_api.DOC_STATUS_NEW, chz_api.DOC_STATUS_PROCESSING_ERROR)),
                None,
            )
            if reusable and db.reuse_document(conn, reusable["id"], codes):
                doc_id = reusable["id"]
            else:
                doc_id = db.create_document(conn, run_id, chz_type, codes)
            db.release_codes(conn, run_id, chz_type, codes, doc_id)
        return {
            "doc_id": doc_id,
            "chz_type": chz_type,
            "product_document": doc_b64,
            "count": len(codes),
            "skipped": skipped,
            "note": _skipped_note(skipped),
        }

    @app.post("/step4/{run_id}/send")
    async def step4_send(request: Request, run_id: str, body: dict[str, Any]) -> dict[str, Any]:
        user_id = _require_user(request)
        with db.connect() as conn:
            run = _get_run_or_404(user_id, run_id, conn)
            token = _decrypt(run["chz_token_enc"]) if run["chz_token_enc"] else None
            if body.get("doc_id") in (None, ""):
                raise HTTPException(status_code=400, detail="Документ собран не полностью. Нажмите кнопку еще раз.")
            doc = db.get_document(conn, body["doc_id"], run_id)
            user = db.get_user(conn, user_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="Документ не найден в этом запуске.")
        if doc["chz_doc_id"]:
            return {"ok": True, "chz_doc_id": doc["chz_doc_id"]}  # уже отправлен, повтор клика не дублирует в ЧЗ
        if not body.get("chz_type") or not isinstance(body.get("product_document"), str) or not body["product_document"]:
            raise HTTPException(status_code=400, detail="Документ собран не полностью. Нажмите кнопку еще раз.")
        if not isinstance(body.get("signature"), str) or not body["signature"].strip():
            raise HTTPException(
                status_code=400,
                detail="Подпись не получена. Проверьте, что флешка вставлена и КриптоПро открыт, и нажмите кнопку еще раз.",
            )
        if not token:
            raise HTTPException(status_code=400, detail="Нет входа в Честный знак.")
        with db.connect() as conn:
            claimed = db.claim_document(conn, body["doc_id"], run_id)
            fresh = db.get_document(conn, body["doc_id"], run_id) if claimed else None
        if not claimed:
            # двойной клик или вторая вкладка: ждем исход первой отправки, второй раз в ЧЗ не шлем
            return await _wait_sent(body["doc_id"], run_id)
        try:
            _same_document_or_409(body, fresh)
            client = chz_client(token)
            pg = (user["pg"] if user else None) or config.DEFAULT_PG
            try:
                # документ уже собран base64 на шаге build (детально подписан в браузере),
                # ChzClient.create_document ждет несобранный dict - шлем сырым httpx напрямую
                chz_doc_id = await _send_raw_document(
                    client, body["product_document"], body["chz_type"], body["signature"], pg
                )
            except chz_api.ChzAuthError:
                raise HTTPException(status_code=401, detail="Токен ЧЗ истек, войдите заново на шаге 2.")
            except httpx.HTTPStatusError as e:
                # ЧЗ ответил и отклонил документ (например, подпись не сошлась) - это не сбой связи,
                # причину берем из тела ответа; повтор сборки/отправки разрешен через PROCESSING_ERROR.
                text = _reject_text(e.response)
                with db.connect() as conn:
                    db.set_document_status(conn, body["doc_id"], chz_api.DOC_STATUS_PROCESSING_ERROR, text)
                answer = chz_errors.explain(text)
                tail = "" if answer.startswith(text) else f" Ответ Честного знака: {text}"
                raise HTTPException(
                    status_code=502 if e.response.status_code >= 500 else 400,
                    detail=f"Честный знак отклонил документ. {answer}{tail}",
                )
            except httpx.TimeoutException as e:
                with db.connect() as conn:
                    db.set_document_status(conn, body["doc_id"], chz_api.DOC_STATUS_PROCESSING_ERROR, str(e)[:500])
                raise HTTPException(status_code=502, detail=f"Честный знак не ответил: {e}")
            except httpx.HTTPError:
                # обрыв связи не таймаутом (перезапуск туннеля, сброс соединения): дошел ли документ, неизвестно
                with db.connect() as conn:
                    db.set_document_status(conn, body["doc_id"], chz_api.DOC_STATUS_PROCESSING_ERROR, OBRYV_SVYAZI)
                raise HTTPException(
                    status_code=502,
                    detail=f"{OBRYV_SVYAZI}. Перед повтором проверьте Документы - Исходящие в личном кабинете: "
                           "если документ там есть, повторно его не отправляйте.",
                )
            with db.connect() as conn:
                db.set_document_chz_id(conn, body["doc_id"], chz_doc_id)
            return {"ok": True, "chz_doc_id": chz_doc_id}
        finally:
            with db.connect() as conn:
                db.unclaim_document(conn, body["doc_id"], doc["status"])

    @app.get("/step4/{run_id}/status")
    async def step4_status(request: Request, run_id: str) -> dict[str, Any]:
        user_id = _require_user(request)
        with db.connect() as conn:
            run = _get_run_or_404(user_id, run_id, conn)
            token = _decrypt(run["chz_token_enc"]) if run["chz_token_enc"] else None
            docs = [dict(d) for d in db.get_documents(conn, run_id)]
        client = chz_client(token) if token else None
        for d in docs:
            if d["chz_doc_id"] and d["status"] in (chz_api.DOC_STATUS_IN_PROGRESS,) and client:
                await _refresh_document(client, d)
        with db.connect() as conn:
            docs = [dict(d) for d in db.get_documents(conn, run_id)]
        for d in docs:
            d.pop("done_at", None)
        sent_docs = [d for d in docs if d.get("chz_doc_id")]
        all_done = (
            bool(sent_docs)
            and not any(d["status"] == db.DOC_SENDING for d in docs)
            and all(d["status"] not in (chz_api.DOC_STATUS_IN_PROGRESS,) for d in sent_docs)
        )
        if all_done:
            await _finalize_run(run_id, user_id)
        states: dict[str, dict[str, dict[str, str]]] = {}
        done_counts = {sverka.GROUP_OUTPUT: 0, sverka.GROUP_RETURN: 0}
        for key, chz_type in ((sverka.GROUP_OUTPUT, chz_api.DOC_TYPE_RECEIPT), (sverka.GROUP_RETURN, chz_api.DOC_TYPE_RETURN)):
            states[key] = {}
            for code, (state, why) in _code_states(docs, chz_type).items():
                states[key][code] = {"state": state, "reason": chz_errors.explain(why) if state == STATE_FAILED and why else ""}
                done_counts[key] += state == STATE_DONE
        for d in docs:
            d["explain"] = chz_errors.explain(d.get("errors")) if d.get("errors") else ""
            d["count"] = len(json.loads(d.pop("codes_json", None) or "[]"))
        return {"documents": docs, "done": all_done, "states": states, "done_counts": done_counts}

    @app.get("/report/{run_id}")
    async def report(request: Request, run_id: str) -> FileResponse:
        user_id = _require_user(request)
        with db.connect() as conn:
            run = _get_run_or_404(user_id, run_id, conn)
        if not run["report_path"] or not Path(run["report_path"]).exists():
            raise HTTPException(status_code=404, detail="Отчет еще не готов.")
        return FileResponse(run["report_path"], filename=f"kiz_{run_id[:8]}.xlsx")

    @app.get("/preview/{run_id}")
    async def preview(request: Request, run_id: str) -> Response:
        user_id = _require_user(request)
        with db.connect() as conn:
            _get_run_or_404(user_id, run_id, conn)
            rows = [dict(r) for r in db.get_items(conn, run_id)]
            docs = [dict(d) for d in db.get_documents(conn, run_id)]
        if not rows:
            raise HTTPException(status_code=404, detail="Сначала нужна сверка.")
        return Response(
            excel_out.build_preview(rows, docs),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="preview_{run_id[:8]}.xlsx"'},
        )

    @app.get("/instrukciya")
    async def instrukciya(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "instrukciya.html", {})

    @app.get("/")
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/step1", status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def net_vhoda(request: Request, exc: StarletteHTTPException) -> Any:
        if exc.status_code == 401 and request.method == "GET" and "text/html" in request.headers.get("accept", ""):
            return templates.TemplateResponse(request, "net_vhoda.html", {"message": exc.detail}, status_code=401)
        if exc.status_code in (400, 404) and request.method == "GET" and "text/html" in request.headers.get("accept", ""):
            message = "Страница не найдена." if exc.detail == "Not Found" else str(exc.detail)
            return _error_page(message, exc.status_code)
        return await http_exception_handler(request, exc)

    return app


def _norm(cis_raw: str) -> str:
    from .. import kiz

    return kiz.normalize_kiz(cis_raw)


def _encrypt(value: str) -> str:
    from .. import crypto

    return crypto.encrypt_str(value)


WB_CANCELED = {"canceled", "canceled_by_client", "declined_by_client"}
WB_STATUS_SOLD = "sold"
WB_STATUS_RETURNED = {"canceled_by_client", "declined_by_client"}


async def _mark_wb_canceled(wb_key_enc: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Продажа FBS из финотчета, по которой WB уже пишет отказ покупателя, а возврата
    в этом отчете нет: панель такой код сама не выводит, показывает ошибкой."""
    returned = {it.get("order_id") for it in items if it["op"] != wb_api.OP_SALE}
    ids = sorted({it["order_id"] for it in items if it.get("order_id") and it["op"] == wb_api.OP_SALE} - returned)
    if not ids:
        return items
    statuses = await wb_api.WbClient(_decrypt(wb_key_enc)).get_orders_status(ids)
    bad = {o for o, s in statuses.items() if s.get("wbStatus") in WB_CANCELED}
    return [
        {**it, "reason": "wb_canceled"} if it.get("order_id") in bad and it["op"] == wb_api.OP_SALE else it
        for it in items
    ]


async def _apply_wb_statuses(wb_key_enc: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Строки из архива заданий: операцию ставит живой статус WB, а не файл.
    sold - продажа, отказ покупателя - возврат, остальное (еще не доставлено или статус
    неизвестен) - группа ok с причиной not_sold_yet."""
    ids = sorted({it["order_id"] for it in items if it.get("archive") and it.get("order_id")})
    statuses = await wb_api.WbClient(_decrypt(wb_key_enc)).get_orders_status(ids) if ids else {}
    out: list[dict[str, Any]] = []
    for it in items:
        if not it.get("archive"):
            out.append(it)
            continue
        status = (statuses.get(it.get("order_id")) or {}).get("wbStatus")
        if status == WB_STATUS_SOLD:
            out.append({**it, "op": wb_api.OP_SALE})
        elif status in WB_STATUS_RETURNED:
            out.append({**it, "op": wb_api.OP_RETURN, "reason": "otkaz"})
        else:
            out.append({**it, "op": wb_api.OP_SALE, "reason": "not_sold_yet"})
    return out


async def _fill_missing_order_dates(
    wb_key_enc: str, items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    ids = {
        int(item["order_id"])
        for item in items
        if item.get("order_id") and not item.get("date")
    }
    if not ids:
        return items
    known_dates = []
    for item in items:
        value = str(item.get("date") or "")[:10]
        try:
            known_dates.append(date.fromisoformat(value))
        except ValueError:
            continue
    end = datetime.now(wb_api.MSK).date()
    if known_dates:
        start = min(known_dates) - timedelta(days=wb_api.LOOKBACK_DAYS)
    else:
        start = end - timedelta(days=90)
    orders = await wb_api.WbClient(_decrypt(wb_key_enc)).get_orders(
        wb_api.msk_day_unix(start.isoformat()), wb_api.msk_day_unix(end.isoformat(), end=True)
    )
    dates = {
        int(order["id"]): wb_api._msk_created(order.get("createdAt") or "")[:10]
        for order in orders
        if order.get("id") is not None and order.get("createdAt")
    }
    filled = []
    for item in items:
        order_id = item.get("order_id")
        created = dates.get(int(order_id)) if order_id else None
        filled.append({**item, "date": created} if not item.get("date") and created else item)
    return filled


def _apply_archive_statuses(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Без ключа WB операцию строки архива ставит статус задания из самого файла
    (или тип операции листа КИЗ, если статуса нет): раскладка та же, что у живых статусов."""
    out: list[dict[str, Any]] = []
    for it in items:
        if not it.get("archive"):
            out.append(it)
            continue
        status = (it.get("arh_status") or "").strip().lower()
        file_op = it.get("arh_op") or ""
        if status == "продано" or (not status and file_op == "Продажа"):
            out.append({**it, "op": wb_api.OP_SALE})
        elif "отказ" in status or "отмен" in status or (not status and file_op == "Возврат"):
            out.append({**it, "op": wb_api.OP_RETURN, "reason": "otkaz"})
        else:
            out.append({**it, "op": wb_api.OP_SALE, "reason": "not_sold_yet"})
    return out


def _min_period(date_from: str, date_to: str) -> tuple[str, str, str]:
    try:
        start = date.fromisoformat(date_from)
    except ValueError:
        return date_from, date_to, ""
    try:
        end = date.fromisoformat(date_to) if date_to else date.today()
    except ValueError:
        return date_from, date_to, ""
    end = min(end, date.today())
    if (end - start).days >= 29:
        return date_from, date_to, ""
    try:
        start = end - timedelta(days=29)
    except OverflowError:
        return date_from, date_to, ""
    warning = f"Период расширен до 30 дней: с {start:%d.%m.%Y} по {end:%d.%m.%Y}"
    return start.isoformat(), end.isoformat(), warning


def _fin_events(parts: list[list[dict[str, Any]]]) -> dict[str, list[tuple[str, str, int | None]]]:
    """События финотчета (дата, операция) по коду или заданию строк архива. Строка финотчета
    с кодом ищет пару только по коду, без кода - по номеру задания, как в _excel_keys."""
    arh = [it for part in parts for it in part if it.get("archive")]
    kody = {kiz.normalize_kiz(it["cis_raw"]) for it in arh if it.get("cis_raw")}
    zadaniya = {it["order_id"] for it in arh if it.get("order_id")}
    events: dict[str, list[tuple[str, str, int | None]]] = {}
    for part in parts:
        for it in part:
            if it.get("archive"):
                continue
            cis = kiz.normalize_kiz(it["cis_raw"] or "")
            if cis:
                key = cis if cis in kody else None
            else:
                key = f"#{it['order_id']}" if it.get("order_id") in zadaniya else None
            if key:
                events.setdefault(key, []).append((it.get("date") or "", it["op"], it.get("order_id") or None))
    return events


def _apply_fin_events(
    items: list[dict[str, Any]], events: dict[str, list[tuple[str, str, int | None]]]
) -> list[dict[str, Any]]:
    """Дата строки архива - дата последнего события пары из финотчета, иначе дата из файла
    архива. Последний возврат в финотчете делает строку возвратом, последняя продажа
    снимает "еще не продан"."""
    out: list[dict[str, Any]] = []
    for it in items:
        if not it.get("archive"):
            out.append(it)
            continue
        found = list(events.get(kiz.normalize_kiz(it["cis_raw"] or ""), []))
        if it.get("order_id"):
            found += events.get(f"#{it['order_id']}", [])
        row = dict(it)
        # событие по более раннему заданию не трогает строку более позднего задания того же кода
        found = [e for e in found if e[2] is None or e[2] >= (it.get("order_id") or 0)]
        last = max(found, key=lambda e: (e[0], 0 if e[1] == wb_api.OP_SALE else 1)) if found else None
        row["date"] = (last[0] if last else "") or it.get("arh_date") or ""
        if last and last[1] == wb_api.OP_RETURN:
            row.update(op=wb_api.OP_RETURN, reason="vozvrat_fin")
        elif last:
            row["op"] = wb_api.OP_SALE
            row.pop("reason", None)
        out.append(row)
    return out


def _excel_keys(it: dict[str, Any], poisk: bool = False) -> list[tuple[str, str]]:
    # код одного экземпляра в архиве и в финотчете записан по-разному, сравнивать можно
    # только после нормализации. У строки FBS из финотчета кода еще нет вовсе, ее со
    # строкой архива роднит только номер задания: по заданию ищем пару лишь тогда, когда
    # кода нет, иначе два разных кода одного задания слиплись бы в одну строку.
    out: list[tuple[str, str]] = []
    cis = kiz.normalize_kiz(it["cis_raw"] or "")
    if cis:
        out.append((cis, it["op"]))
    if it.get("order_id") and not (poisk and cis):
        out.append((f"#{it['order_id']}", it["op"]))
    return out


async def _fill_fbs_codes(wb_key_enc: str | None, items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Код для строки FBS из финотчета: meta сборочного задания по ключу WB.
    Задание без кода считаем как сбой получения КИЗ."""
    order_ids = sorted({it["order_id"] for it in items if it.get("need_meta")})
    metas = await wb_api.WbClient(_decrypt(wb_key_enc)).get_orders_meta(order_ids) if wb_key_enc else {}
    out: list[dict[str, Any]] = []
    failed = 0
    for it in items:
        if not it.get("need_meta"):
            out.append(it)
            continue
        codes = ((metas.get(it["order_id"]) or {}).get("sgtin") or {}).get("value") or []
        if isinstance(codes, str):
            codes = [codes]
        if not codes:
            failed += 1
            continue
        out.extend({**it, "cis_raw": c, "need_meta": False} for c in codes)
    return out, failed


def _decrypt(value: str) -> str:
    from .. import crypto

    return crypto.decrypt_str(value)


def _require_user(request: Request) -> int:
    session_id = request.cookies.get(SESSION_COOKIE)
    if not session_id:
        raise HTTPException(status_code=401, detail="Нет входа. Откройте ссылку из бота, /out.")
    with db.connect() as conn:
        user_id = db.get_session_user_id(conn, session_id)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Сессия истекла. Откройте ссылку из бота, /out.")
    return user_id


def _get_run_or_404(user_id: int, run_id: str, conn=None):
    if conn is not None:
        run = db.get_run(conn, run_id, user_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Запуск не найден.")
        return run
    with db.connect() as c:
        run = db.get_run(c, run_id, user_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Запуск не найден.")
        return run


def _has_live_session(conn, request: Request) -> bool:
    session_id = request.cookies.get(SESSION_COOKIE)
    return bool(session_id) and db.get_session_user_id(conn, session_id) is not None


def _error_page(message: str, status_code: int = 400) -> HTMLResponse:
    page = templates.get_template("oshibka.html").render(message=message)
    return HTMLResponse(page, status_code=status_code)


def _unix_from_date_str(value: str) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return int(time.mktime(time.strptime(value, "%Y-%m-%d")))
    except ValueError:
        return None


STATE_DONE, STATE_BUSY, STATE_FAILED = "done", "processing", "failed"
_STATE_RANK = {STATE_FAILED: 0, STATE_BUSY: 1, STATE_DONE: 2}
PRIOR_WINDOW = 24 * 3600
PRIOR_BUSY = (
    "Эти коды уже отправлены в Честный знак документом из прошлой сверки, он еще обрабатывается "
    "(обычно до 10 минут). Повторно отправлять не нужно: дождитесь ответа и запустите сверку заново."
)
PRIOR_DONE = (
    "Честный знак только что принял эти коды в документе из прошлой сверки. "
    "Запустите сверку заново: они уйдут из списка."
)


def _code_states(docs: list[dict[str, Any]], chz_type: str) -> dict[str, tuple[str, str]]:
    """Код -> (состояние, текст ошибки) по документам одного типа. Принятый код сильнее ошибки
    старой попытки; документ NEW (собран, не отправлен) состоянием не считается."""
    out: dict[str, tuple[str, str]] = {}
    for d in docs:
        if d["doc_type"] != chz_type:
            continue
        if d["status"] == chz_api.DOC_STATUS_CHECKED_OK:
            state = STATE_DONE
        elif d["status"] in (chz_api.DOC_STATUS_IN_PROGRESS, db.DOC_SENDING):
            state = STATE_BUSY
        elif d["status"] in (chz_api.DOC_STATUS_CHECKED_NOT_OK, chz_api.DOC_STATUS_PROCESSING_ERROR):
            state = STATE_FAILED
        else:
            continue
        try:
            codes = json.loads(d.get("codes_json") or "[]")
        except ValueError:
            codes = []
        for c in codes:
            prev = out.get(c)
            if prev and _STATE_RANK[prev[0]] > _STATE_RANK[state]:
                continue
            out[c] = (state, d.get("errors") or "")
    return out


def _raise_state(states: dict[str, tuple[str, str]], code: str, state: tuple[str, str]) -> None:
    if code not in states or _STATE_RANK[states[code][0]] < _STATE_RANK[state[0]]:
        states[code] = state


_DOC_OUTCOMES = (
    chz_api.DOC_STATUS_CHECKED_OK,
    chz_api.DOC_STATUS_CHECKED_NOT_OK,
    chz_api.DOC_STATUS_PROCESSING_ERROR,
)


def _prior_codes(d: dict[str, Any]) -> set[str]:
    try:
        raw = json.loads(d["codes_json"] or "[]")
    except (ValueError, TypeError):
        return set()
    return {c for c in raw if isinstance(c, str)} if isinstance(raw, list) else set()


async def _refresh_document(client: Any, d: dict[str, Any], known_only: bool = False) -> bool:
    """Спрашивает у ЧЗ исход документа и пишет его в базу. False - исход еще не известен.
    known_only - незнакомый статус исходом не считать и в базу не писать."""
    try:
        info = await client.doc_info(d["chz_doc_id"])
    except (chz_api.ChzAuthError, httpx.HTTPError, ValueError):
        return False
    status, errors = _parse_doc_info(info)
    if not status or not isinstance(status, str) or status in chz_api.DOC_PENDING_STATUSES:
        return False
    if known_only and status not in _DOC_OUTCOMES:
        return False
    codes = list(dict.fromkeys(json.loads(d["codes_json"] or "[]")))
    split = _partition(info, status, codes)
    with db.connect() as conn:
        if split:
            db.split_document(conn, d["id"], *split)
        else:
            db.set_document_status(conn, d["id"], status, errors)
        db.mark_document_done(conn, d["run_id"], d["chz_doc_id"])
    return True


async def _prior_states(
    make_client: Any, user_id: int, run: Any, chz_type: str, codes: set[str]
) -> dict[str, tuple[str, str]]:
    """Коды группы из документов других сверок этого продавца, о которых эта сверка не знает:
    ЧЗ еще не ответил либо принял документ уже после ее запуска. Она видит такие коды
    прежними, и без этой проверки в ЧЗ уходит повтор."""
    with db.connect() as conn:
        prior = [
            dict(d) for d in db.get_prior_documents(
                conn, user_id, run["inn"], run["id"], run["created_at"], chz_type, time.time() - PRIOR_WINDOW
            )
        ]
    out: dict[str, tuple[str, str]] = {}
    client = None
    for d in prior:
        hit = codes & _prior_codes(d)
        if not hit:
            continue
        if d["status"] == chz_api.DOC_STATUS_CHECKED_OK:
            fresh = {c: (STATE_DONE, "") for c in hit}
        else:
            known = False
            try:
                if client is None and run["chz_token_enc"]:
                    client = make_client(_decrypt(run["chz_token_enc"]))
                if client is not None:
                    known = await _refresh_document(client, d, known_only=True)
            except Exception:
                known = False
            if not known:
                fresh = {c: (STATE_BUSY, "") for c in hit}
            else:
                with db.connect() as conn:
                    parts = [
                        dict(x) for x in db.get_documents(conn, d["run_id"]) if x["chz_doc_id"] == d["chz_doc_id"]
                    ]
                done = _code_states(parts, chz_type)
                fresh = {c: done[c] for c in hit if done.get(c, ("", ""))[0] == STATE_DONE}
        for code, state in fresh.items():
            _raise_state(out, code, state)
    return out


def _flatten_error(data: Any) -> str:
    if isinstance(data, str):
        return data.strip()
    if isinstance(data, list):
        return "; ".join(t for t in (_flatten_error(x) for x in data) if t)
    if isinstance(data, dict):
        for key in ("error_message", "errorMessage", "message", "detail", "description", "error", "errors", "text"):
            if data.get(key):
                text = _flatten_error(data[key])
                if text:
                    return text
        return json.dumps(data, ensure_ascii=False) if data else ""
    return str(data) if data else ""


def _reject_text(response: httpx.Response) -> str:
    """Причина отказа из тела ответа ЧЗ: сам raise_for_status отдает только 'Client error 400'."""
    try:
        text = _flatten_error(response.json())
    except ValueError:
        text = response.text.strip()
    return (text or f"код ответа {response.status_code}")[:500]


def _entry_text(e: Any) -> str:
    if isinstance(e, dict):
        return str(e.get("message") or e.get("errorMessage") or e.get("text") or e)
    return str(e)


async def _wait_sent(doc_id: str, run_id: str, seconds: float = 45.0) -> dict[str, Any]:
    """Документ уже уходит в ЧЗ другим запросом: ждем его исход вместо второй отправки."""
    for _ in range(int(seconds / 0.25)):
        await asyncio.sleep(0.25)
        with db.connect() as conn:
            d = db.get_document(conn, doc_id, run_id)
        if d is not None and d["chz_doc_id"]:
            return {"ok": True, "chz_doc_id": d["chz_doc_id"]}
        if d is None or d["status"] != db.DOC_SENDING:
            break
    raise HTTPException(
        status_code=409,
        detail="Этот документ уже отправлялся. Обновите страницу, там видно, принял его Честный знак или нет.",
    )


def _errors_by_code(info: Any, codes: list[str]) -> dict[str, str]:
    """Ошибки документа, в тексте которых назван код документа: {код: причина без самого кода}."""
    if isinstance(info, list):
        info = info[0] if info else {}
    errs = info.get("errors") if isinstance(info, dict) else None
    entries = errs if isinstance(errs, list) else ([] if errs is None else [errs])
    pats = {c: re.compile(r"(?<![0-9A-Za-z])" + re.escape(c) + r"(?![0-9A-Za-z])") for c in codes}
    found: dict[str, str] = {}
    for e in entries:
        text = _entry_text(e)
        blob = text if isinstance(e, str) else text + " " + json.dumps(e, ensure_ascii=False)
        hit = [c for c, pat in pats.items() if pat.search(blob)]
        if not hit:
            return {}  # ошибка без кода: документ отклонен целиком
        clean = text
        for c in hit:
            clean = clean.replace(c, "")
        clean = re.sub(r"\(\s*\)|\[\s*\]", "", clean)
        clean = re.sub(r"\s+", " ", clean).strip(" :,;")
        for c in hit:
            found.setdefault(c, clean or "Честный знак не принял этот код")
    return found


_NE_PRINYAT_VMESTE = "Честный знак не принял документ из-за ошибок в других кодах. Этот код можно отправить заново."


def _partition(info: Any, status: str | None, codes: list[str]) -> tuple[list[str], dict[str, list[str]]] | None:
    """ЧЗ отклонил документ с ошибками по кодам: ([], {причина: коды}). Документ обрабатывается целиком,
    принятых кодов в нем нет. None - ошибки не привязаны к кодам, документ отклонен целиком."""
    if status != chz_api.DOC_STATUS_CHECKED_NOT_OK:
        return None
    by_code = _errors_by_code(info, codes)
    if not by_code:
        return None
    failed: dict[str, list[str]] = {}
    for c in codes:
        failed.setdefault(by_code.get(c, _NE_PRINYAT_VMESTE), []).append(c)
    return [], failed


def _group_view(rows: list[dict[str, Any]], docs: list[dict[str, Any]], group: str, chz_type: str) -> dict[str, Any]:
    states = _code_states(docs, chz_type)
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in rows:
        code = r["cis_norm"]
        if r["group_name"] != group or code in seen:
            continue
        seen.add(code)
        state, why = states.get(code, ("", ""))
        no_price = group == sverka.GROUP_OUTPUT and (r["product_cost"] or 0) <= 0
        items.append({
            "cis": code,
            "tovar": r.get("tovar") or "",
            "price": f"{(r['product_cost'] or 0) / 100:,.2f}".replace(",", " ").replace(".", ",") if not no_price and group == sverka.GROUP_OUTPUT else "",
            "state": state,
            "reason": chz_errors.explain(why) if state == STATE_FAILED and why else "",
            "no_price": no_price,
            "checked": state not in (STATE_DONE, STATE_BUSY) and not no_price,
        })
    return {
        "items": items,
        "total": len(items),
        "done": sum(1 for i in items if i["state"] == STATE_DONE),
        "busy": sum(1 for i in items if i["state"] == STATE_BUSY),
        "selected": sum(1 for i in items if i["checked"]),
    }


def _pick_codes(
    body: dict[str, Any], in_group: list[dict[str, Any]], states: dict[str, tuple[str, str]], group: str
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Строки для документа и пропущенные коды с причиной (no_price, done, processing)."""
    by_norm: dict[str, dict[str, Any]] = {}
    for r in in_group:
        by_norm.setdefault(r["cis_norm"], r)
    asked = body.get("codes")
    explicit = asked is not None
    if explicit:
        if not isinstance(asked, list) or not asked or not all(isinstance(c, str) for c in asked):
            raise HTTPException(status_code=400, detail="Отметьте хотя бы один код.")
        names: list[str] = []
        for c in asked:
            norm = _norm(c)
            if norm not in by_norm:
                raise HTTPException(
                    status_code=400,
                    detail=f"Код {c.strip()[:40]} не входит в группу «{sverka.GROUP_LABELS[group]}» этого запуска, в документ его добавить нельзя.",
                )
            if norm not in names:
                names.append(norm)
    else:
        names = list(by_norm)
    picked: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for c in names:
        state = states.get(c, ("", ""))[0]
        if state in (STATE_DONE, STATE_BUSY):
            if explicit:
                skipped.append({"cis": c, "reason": state})
        elif group == sverka.GROUP_OUTPUT and (by_norm[c]["product_cost"] or 0) <= 0:
            skipped.append({"cis": c, "reason": "no_price"})
        else:
            picked.append(by_norm[c])
    limit = body.get("limit")
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise HTTPException(status_code=400, detail="Лимит должен быть целым числом больше нуля.")
        picked = picked[:limit]
    if not picked:
        no_price = [x["cis"] for x in skipped if x["reason"] == "no_price"]
        if no_price:
            raise HTTPException(
                status_code=400,
                detail=f"Нет цены в отчете у кодов: {len(no_price)}, первый {no_price[0][:40]}. Вывод без цены не собирается, загрузите отчет с ценой продажи или выберите другие коды.",
            )
        raise HTTPException(
            status_code=400, detail="Нет кодов для документа: выбранные уже обработаны или ушли в Честный знак."
        )
    return picked, skipped


def _skipped_note(skipped: list[dict[str, str]]) -> str:
    parts = []
    no_price = [x["cis"] for x in skipped if x["reason"] == "no_price"]
    if no_price:
        parts.append(
            f"Без цены в отчете, в документ не вошли: {len(no_price)}, первый {no_price[0][:40]}. "
            "Загрузите отчет с ценой продажи, чтобы вывести и их."
        )
    again = [x for x in skipped if x["reason"] in (STATE_DONE, STATE_BUSY)]
    if again:
        parts.append(f"Уже обработаны или отправлены, пропущены: {len(again)}.")
    return " ".join(parts)


def _parse_doc_info(info: Any) -> tuple[str | None, str | None]:
    """doc/info живого ЧЗ не документирован буквально: форма ответа гуляет между dict и
    списком с одним dict, errors - между null/строкой/списком строк или списком dict.
    Разбираем терпимо, без единого варианта, который валит ручку 500-й."""
    if isinstance(info, list):
        info = info[0] if info else {}
    if not isinstance(info, dict):
        info = {}
    status = info.get("status")
    errors = info.get("errors")
    if errors is None:
        errors = []
    elif isinstance(errors, str):
        errors = [errors]
    elif not isinstance(errors, list):
        errors = [errors]
    texts = []
    for e in errors:
        if isinstance(e, dict):
            texts.append(str(e.get("message") or e.get("errorMessage") or e.get("text") or e))
        else:
            texts.append(str(e))
    return status, (", ".join(texts) or None)


def _visible_text(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if unicodedata.category(ch) != "Cf").strip()


def _return_primary(body: dict[str, Any]) -> dict[str, str]:
    kind = str(body.get("primary_type") or chz_api.PRIMARY_DOC_OTHER)
    number = _visible_text(body.get("primary_number"))
    day = _visible_text(body.get("primary_date"))
    name = _visible_text(body.get("primary_name"))
    if kind not in chz_api.RETURN_PRIMARY_TYPES:
        raise HTTPException(status_code=400, detail="Неизвестный вид первичного документа.")
    if not number:
        raise HTTPException(status_code=400, detail="Укажите номер первичного документа.")
    if len(number) > 255:
        raise HTTPException(status_code=400, detail="Номер первичного документа длиннее 255 знаков.")
    try:
        parsed = date.fromisoformat(day)
    except ValueError:
        raise HTTPException(status_code=400, detail="Укажите дату первичного документа.")
    today = date.today()
    try:
        oldest = today.replace(year=today.year - 5)
    except ValueError:
        oldest = today.replace(year=today.year - 5, day=28)
    if parsed > today or parsed < oldest:
        raise HTTPException(status_code=400, detail="Дата первичного документа должна быть не позже сегодня и не старше 5 лет.")
    if kind == chz_api.PRIMARY_DOC_OTHER and not name:
        raise HTTPException(status_code=400, detail="Укажите наименование первичного документа.")
    return {"type": kind, "number": number, "date": parsed.isoformat(), "custom_name": name}


OBRYV_SVYAZI = "Связь с Честным знаком оборвалась при отправке, документ мог не дойти"
CHZ_DOC_ID_RE = re.compile(r"[0-9A-Za-z._:-]{1,100}")


def _doc_codes(product_document_b64: Any) -> list[str] | None:
    import base64

    try:
        data = json.loads(base64.b64decode(str(product_document_b64), validate=True))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    items = data.get("products") if "products" in data else data.get("products_list")
    if not isinstance(items, list):
        return None
    codes = [(p.get("cis") or p.get("ki")) if isinstance(p, dict) else None for p in items]
    return codes if all(isinstance(c, str) for c in codes) else None


def _same_document_or_409(body: dict[str, Any], doc: Any) -> None:
    """Подписан и прислан тот же документ, что лежит в базе: после пересборки в другой вкладке
    старая подпись ушла бы в ЧЗ с одними кодами, а в базе числились бы другие."""
    sent = _doc_codes(body.get("product_document"))
    try:
        ours = json.loads(doc["codes_json"] or "[]")
    except ValueError:
        ours = []
    if sent is None or sorted(set(sent)) != sorted(set(ours)) or body.get("chz_type") != doc["doc_type"]:
        raise HTTPException(
            status_code=409,
            detail="Документ изменился после подписи. Обновите страницу, соберите и подпишите его заново.",
        )


async def _send_raw_document(
    client: chz_api.ChzClient, product_document_b64: str, doc_type: str, signature_b64: str, pg: str = config.DEFAULT_PG
) -> str:
    async with client._http_client() as http_client:
        r = await http_client.post(
            client.base_url + chz_api.DOC_CREATE_PATH,
            params={"pg": pg},
            json={
                "document_format": chz_api.DOCUMENT_FORMAT_MANUAL,
                "product_document": product_document_b64,
                "type": doc_type,
                "signature": signature_b64,
            },
            headers=client._headers(),
        )
        if r.status_code == 401:
            raise chz_api.ChzAuthError("токен ЧЗ истек (401)")
        r.raise_for_status()
        try:
            data = r.json()
        except ValueError:
            data = r.text  # 4.1.1: в ответ приходит только идентификатор документа, JSON не обещан
        if isinstance(data, dict):
            data = data.get("id") or data.get("doc_id")
        doc_id = str(data or "").strip().strip('"')
        if not CHZ_DOC_ID_RE.fullmatch(doc_id):  # страница техработ с кодом 200 номером не считается
            raise HTTPException(
                status_code=502,
                detail="Честный знак принял запрос, но не вернул номер документа. Перед повтором проверьте Документы - Исходящие в личном кабинете.",
            )
        return doc_id


async def _finalize_run(run_id: str, user_id: int) -> None:
    """Собрать итоговый Excel по факту ответа ЧЗ (документы), сохранить путь.

    Пересобираем при каждом вызове (не только один раз): между опросами статус документа
    в ЧЗ может смениться (например, принят -> отклонен), отчет должен это подхватывать.
    В бот сводку шлем заново, только когда счетчики по исходу (counts_by_outcome) реально
    изменились с прошлой отправки - иначе на каждый опрос статуса бот долбил бы тем же текстом.
    """
    with db.connect() as conn:
        run = db.get_run(conn, run_id, user_id)
        if run is None:
            return
        rows = [dict(r) for r in db.get_items(conn, run_id)]  # get_items уже дает и group, и group_name
        docs = [dict(d) for d in db.get_documents(conn, run_id)]
        content = await run_in_threadpool(excel_out.build_report, rows, docs)
        Path(config.REPORTS_DIR).mkdir(parents=True, exist_ok=True)
        path = str(Path(config.REPORTS_DIR) / f"{run_id}.xlsx")
        Path(path).write_bytes(content)
        db.set_run_report(conn, run_id, path)
        db.set_run_status(conn, run_id, "done")
        user = db.get_user(conn, user_id)
        counts = excel_out.counts_by_outcome(rows, docs)
        counts_json = json.dumps(counts, sort_keys=True)
        changed = counts_json != (run["last_summary_counts"] or "")
        if changed:
            db.set_run_summary_counts(conn, run_id, counts_json)
    if changed and user and config.BOT_TOKEN:
        text = (
            "Сверка готова.\n"
            f"Всего кодов: {len(rows)}\n"
            f"Выведено: {counts.get('output', 0)}\n"
            f"Возвращено: {counts.get('return', 0)}\n"
            f"Ошибок: {counts.get('error', 0)}\n"
            f"Отчет: {config.PUBLIC_URL}/report/{run_id}"
        )
        try:
            async with httpx.AsyncClient(timeout=10.0) as http_client:
                await http_client.post(
                    f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage",
                    json={"chat_id": user["tg_id"], "text": text},
                )
        except httpx.HTTPError:
            pass
