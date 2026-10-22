"""Сборка итогового Excel-отчета: Изменены, Без изменений, Сводка, Выведено, Возвращено, Ошибки, Все коды,
и Excel-предпросмотра до подписи: Будут изменены, Без изменений.

Выведено/Возвращено/Ошибки строятся по факту ответа Честного знака (таблица documents),
а не по группе сверки - группа это только намерение до отправки, реальный исход решает ЧЗ.
"""
from __future__ import annotations

import json
from io import BytesIO
from typing import Any

import openpyxl
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from . import chz_api, chz_errors, sverka

COLUMNS = ["Код (как получен)", "Код (нормальный)", "Операция", "Статус в ЧЗ", "ИНН владельца", "Причина"]
COLUMNS_ERR = COLUMNS + ["Что делать"]
OP_LABELS = {"sale": "Продажа", "return": "Возврат"}
CHZ_STATUS_RU = {
    "INTRODUCED": "в обороте",
    "RETIRED": "выведен из оборота",
    "WITHDRAWN": "выведен из оборота",
    "EMITTED": "эмитирован",
    "APPLIED": "нанесен",
    "WRITTEN_OFF": "списан",
    "DISAGGREGATION": "расформирован",
}


def _split_rows(
    rows: list[dict[str, Any]], docs: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Раскладывает коды по исходу отправленных в ЧЗ документов: принят - в Выведено/Возвращено,
    отклонен - в Ошибки. Код без отправленного документа (не собирали, или собрали и не отправили)
    в эти три листа не попадает - его сюда обещать нельзя, он есть только в Все коды."""
    by_code: dict[str, tuple[str, str | None]] = {}
    for d in docs:
        status = d.get("status")
        if status == chz_api.DOC_STATUS_CHECKED_OK:
            sheet = "Выведено" if d.get("doc_type") == chz_api.DOC_TYPE_RECEIPT else "Возвращено"
        elif status in (chz_api.DOC_STATUS_CHECKED_NOT_OK, chz_api.DOC_STATUS_PROCESSING_ERROR):
            sheet = "Ошибки"
        else:
            continue  # NEW/IN_PROGRESS - документ еще не закрыт, рано класть в исход
        try:
            codes = json.loads(d.get("codes_json") or "[]")
        except (TypeError, ValueError):
            codes = []
        for code in codes:
            by_code[code] = (sheet, d.get("errors"))

    output_rows: list[dict[str, Any]] = []
    return_rows: list[dict[str, Any]] = []
    error_rows: list[dict[str, Any]] = []
    buckets = {"Выведено": output_rows, "Возвращено": return_rows, "Ошибки": error_rows}
    for r in rows:
        if r.get("group") == sverka.GROUP_ERROR:
            # дубль по коду не подменять исходом другой строки с тем же cis_norm
            error_rows.append(r)
            continue
        hit = by_code.get(r.get("cis_norm"))
        if hit:
            sheet, doc_errors = hit
            row = dict(r)
            if sheet == "Ошибки" and doc_errors:
                row["reason"] = doc_errors
            buckets[sheet].append(row)
    return output_rows, return_rows, error_rows


def counts_by_outcome(rows: list[dict[str, Any]], docs: list[dict[str, Any]]) -> dict[str, int]:
    output_rows, return_rows, error_rows = _split_rows(rows, docs)
    return {"output": len(output_rows), "return": len(return_rows), "error": len(error_rows)}


def build_report(rows: list[dict[str, Any]], docs: list[dict[str, Any]] | None = None) -> bytes:
    docs = docs or []
    output_rows, return_rows, error_rows = _split_rows(rows, docs)
    ok_rows = [r for r in rows if r.get("group") == sverka.GROUP_OK]

    wb = openpyxl.Workbook()
    izm_ws = wb.active
    izm_ws.title = "Изменены"
    bez_ws = wb.create_sheet("Без изменений")
    summary = wb.create_sheet("Сводка")
    summary.append(["Группа", "Количество"])
    summary.append([sverka.GROUP_LABELS[sverka.GROUP_OUTPUT], len(output_rows)])
    summary.append([sverka.GROUP_LABELS[sverka.GROUP_RETURN], len(return_rows)])
    summary.append([sverka.GROUP_LABELS[sverka.GROUP_OK], len(ok_rows)])
    summary.append([sverka.GROUP_LABELS[sverka.GROUP_ERROR], len(error_rows)])
    summary.append(["Всего", len(rows)])
    _oformit(summary, filtr=False)

    # первыми два листа в том же виде, что у бота конкурента: так их читает человек
    izmeneny = [(r, "Выбыл из оборота") for r in output_rows] + [(r, "В обороте") for r in return_rows]
    tronuty = {_klyuch(r) for r, _ in izmeneny}
    bez_izmeneniy = []
    for r in rows:
        if _klyuch(r) in tronuty:
            continue
        oshibka = next((e for e in error_rows if _klyuch(e) == _klyuch(r)), None)
        if oshibka is not None:
            prichina = sverka.REASON_LABELS.get(oshibka.get("reason"), oshibka.get("reason") or "")
        elif r.get("group") in (sverka.GROUP_OUTPUT, sverka.GROUP_RETURN):
            prichina = "документ в Честный знак не отправлялся"
        else:
            prichina = sverka.REASON_LABELS.get(r.get("reason"), r.get("reason") or "")
        bez_izmeneniy.append((r, prichina))
    _fill_vid(izm_ws, "КИЗы изменены", "Новый статус", izmeneny)
    _fill_vid(bez_ws, "КИЗы без изменений", "Объяснение", bez_izmeneniy)

    _fill_sheet(wb.create_sheet("Выведено"), output_rows)
    _fill_sheet(wb.create_sheet("Возвращено"), return_rows)
    _fill_sheet(wb.create_sheet("Ошибки"), error_rows, explain=True)
    _fill_sheet(wb.create_sheet("Все коды"), rows)

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


CHZ_STATUS_LABELS = {
    chz_api.STATUS_INTRODUCED: "В обороте",
    chz_api.STATUS_RETIRED: "Выбыл",
    chz_api.STATUS_EMITTED: "Эмитирован",
    chz_api.STATUS_APPLIED: "Нанесен",
    chz_api.STATUS_WRITTEN_OFF: "Списан",
}


def _klyuch(r: dict[str, Any]) -> Any:
    return r.get("id") if r.get("id") is not None else (r.get("cis_norm"), r.get("op"))


def _fill_vid(ws: Worksheet, zagolovok: str, shestaya: str, rows: list[tuple[dict[str, Any], str]]) -> None:
    ws.append([zagolovok])
    ws.append(["КИЗ", "ИНН владельца", "Склад", "Статус в отчете", "Статус в ЧЗ", shestaya,
               "Название товара", "Цена, руб", "Дата"])
    for r, shestoe in rows:
        cena = r.get("product_cost") or 0
        ws.append(
            [
                _for_cell(r.get("cis_norm", "")),
                r.get("owner_inn") or "",
                "Мой склад (FBS)",
                "Вывод" if r.get("op") == "sale" else ("Отказ" if r.get("reason") in sverka.OTKAZ_REASONS else "Возврат"),
                CHZ_STATUS_LABELS.get(r.get("chz_status"), r.get("chz_status") or "не найден"),
                shestoe,
                r.get("tovar") or "",
                round(cena / 100, 2) if cena else "",
                r.get("date") or "",
            ]
        )
    for kol, shirina in zip("ABCDEFGHI", (34, 14, 16, 15, 14, 34, 32, 10, 12)):
        ws.column_dimensions[kol].width = shirina
    _oformit(ws, 2)


def _for_cell(value: str) -> str:
    """GS (0x1D) в исходном коде - не валидный символ XML, openpyxl такую ячейку не пишет.
    В отчете разделитель не нужен, меняем на пробел."""
    return value.replace("\x1d", " ") if value else value


def _fill_sheet(ws: Worksheet, rows: list[dict[str, Any]], explain: bool = False) -> None:
    ws.append(COLUMNS_ERR if explain else COLUMNS)
    for r in rows:
        prichina = sverka.REASON_LABELS.get(r.get("reason"), r.get("reason", ""))
        ws.append(
            [
                _for_cell(r.get("cis_raw", "")),
                _for_cell(r.get("cis_norm", "")),
                "Отказ" if r.get("reason") in sverka.OTKAZ_REASONS else OP_LABELS.get(r.get("op"), r.get("op", "")),
                CHZ_STATUS_RU.get(r.get("chz_status"), r.get("chz_status") or ""),
                r.get("owner_inn") or "",
                prichina,
            ] + ([_chto_delat(r.get("reason"))] if explain else [])
        )
    _oformit(ws)
    _shirina(ws)


REASON_ACTIONS = {
    "sale_in_turnover": "Выведите код из оборота на шаге 4.",
    "return_retired": "Верните код в оборот на шаге 4.",
    "already_output": "Действий не нужно, код уже выведен из оборота.",
    "already_in_turnover": "Действий не нужно, код уже в обороте.",
    "not_found": "Проверьте, что код ваш и введен в оборот. Если код верный, возьмите его целиком, вместе с хвостом, и загрузите отчет заново.",
    "wrong_owner": "Код принадлежит другому ИНН, его нельзя вывести с вашей подписью. Проверьте, что вошли подписью нужной организации.",
    "not_introduced": "Код еще не введен в оборот. Введите его в оборот в личном кабинете Честного знака и повторите сверку.",
    "bad_format": "Код записан с ошибкой или обрезан. Возьмите код целиком из отчета или с этикетки и загрузите заново.",
    "duplicate": "Код встретился дважды с одной датой операции. Лишнюю строку можно не учитывать, действий не нужно.",
    "wb_canceled": "Заказ отменен, возврата в отчете нет. Выводить код не нужно, он остается в обороте.",
    "not_sold_yet": "Товар еще не продан. Вернитесь к сверке после продажи, выводить рано.",
    "otkaz_v_oborote": "Действий не нужно, покупатель отказался, код в обороте.",
    "otkaz_vyveden": "Верните код в оборот на шаге 4.",
    "vozvrat_fin": "Действий не нужно, возврат уже учтен.",
    "vozvrat_fin_v_oborote": "Действий не нужно, код уже в обороте.",
    "vozvrat_fin_vyveden": "Верните код в оборот на шаге 4.",
    "withdraw_other": "Код выведен не дистанционной продажей, панель такой код вернуть не может. Верните его в личном кабинете Честного знака.",
}

PREVIEW_COLUMNS = [
    "КИЗ", "ИНН владельца", "Статус в отчете", "Статус в ЧЗ", None, "Название товара",
    "Документ", "Номер документа", "Дата документа", "Сумма",
]
PREVIEW_NEW_STATUS = {sverka.GROUP_OUTPUT: "Выбыл", sverka.GROUP_RETURN: "В обороте"}


def _chto_delat(reason: Any) -> str:
    """Для причин сверки готовый совет, для текста ошибки самого ЧЗ - разбор из chz_errors."""
    if reason in REASON_ACTIONS:
        return REASON_ACTIONS[reason]
    return chz_errors.explain(reason)


def _oformit(ws: Worksheet, header_row: int = 1, filtr: bool = True) -> None:
    for c in ws[header_row]:
        c.font = Font(bold=True)
    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)
    if filtr and ws.max_column:
        ws.auto_filter.ref = f"A{header_row}:{get_column_letter(ws.max_column)}{max(ws.max_row, header_row)}"


def _shirina(ws: Worksheet, header_row: int = 1, predel: int = 60) -> None:
    for col in ws.columns:
        dlina = max((len(str(c.value)) for c in col if c.row >= header_row and c.value is not None), default=0)
        ws.column_dimensions[col[0].column_letter].width = min(max(dlina + 2, 10), predel)


def _cell(value: Any) -> Any:
    if isinstance(value, str):
        return ILLEGAL_CHARACTERS_RE.sub(" ", value.replace("\x1d", " "))
    return value


def _data_ru(value: Any) -> str:
    value = value or ""
    if len(value) == 10 and value[4] == "-" and value[7] == "-":
        return f"{value[8:10]}.{value[5:7]}.{value[:4]}"
    return value


def split_preview(
    rows: list[dict[str, Any]], docs: list[dict[str, Any]] | None = None
) -> tuple[list[tuple[dict[str, Any], str]], list[tuple[dict[str, Any], str]]]:
    """Что изменится в ЧЗ, если отправить документы: вывод и возврат против всего остального.
    Коды, по которым ЧЗ уже принял документ, больше не изменятся - их кладем в "Без изменений"."""
    done_out, done_ret, _ = _split_rows(rows, docs or [])
    prinyato = {_klyuch(r) for r in done_out + done_ret}
    budut: list[tuple[dict[str, Any], str]] = []
    bez: list[tuple[dict[str, Any], str]] = []
    for r in rows:
        if _klyuch(r) in prinyato:
            bez.append((r, "документ уже принят в Честном знаке"))
        elif r.get("group") == sverka.GROUP_OUTPUT and (r.get("product_cost") or 0) <= 0:
            bez.append((r, "нет цены в отчете, вывод без цены не собирается"))
        elif r.get("group") in PREVIEW_NEW_STATUS:
            budut.append((r, PREVIEW_NEW_STATUS[r["group"]]))
        else:
            bez.append((r, sverka.REASON_LABELS.get(r.get("reason"), r.get("reason") or "")))
    return budut, bez


def _preview_row(r: dict[str, Any], tretya: str) -> list[Any]:
    cena = r.get("product_cost") or 0
    nomer = r.get("order_id")
    if r.get("reason") in ("not_sold_yet", "wb_canceled"):
        v_otchete = "-"
    else:
        v_otchete = "Вывод" if r.get("op") == "sale" else "Возврат"
    status = r.get("chz_status")
    v_chz = CHZ_STATUS_LABELS.get(status, status) if status else ("Не найден" if r.get("reason") == "not_found" else "-")
    return [
        r.get("cis_norm", ""), r.get("owner_inn") or "", v_otchete, v_chz, tretya,
        r.get("tovar") or "", "Заказ", str(nomer) if nomer else "", _data_ru(r.get("date")),
        round(cena / 100, 2) if cena else "",
    ]


def _fill_preview(ws: Worksheet, tretya: str, rows: list[tuple[dict[str, Any], str]]) -> None:
    ws.append([tretya if c is None else c for c in PREVIEW_COLUMNS])
    for r, text in rows:
        ws.append([_cell(v) for v in _preview_row(r, text)])
    for row in ws.iter_rows(min_row=2):
        for c in row:
            if isinstance(c.value, str) and c.value.startswith("="):
                c.data_type = "s"
    for c in ws["J"][1:]:
        c.number_format = "0.00"
    _oformit(ws)
    _shirina(ws)


def build_preview(rows: list[dict[str, Any]], docs: list[dict[str, Any]] | None = None) -> bytes:
    budut, bez = split_preview(rows, docs)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Будут изменены"
    _fill_preview(ws, "Новый статус", budut)
    _fill_preview(wb.create_sheet("Без изменений"), "Объяснение", bez)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
