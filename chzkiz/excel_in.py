"""Разбор Excel-отчета WB: колонка КИЗ по заголовку, операция по колонке "Тип документа".

Сверено 26.09.2026 на реальном еженедельном отчете о реализации клиента (85 колонок):
заголовок "Код маркировки", заголовок "Тип документа" со значениями "Продажа"/"Возврат".
Строки без этих значений (логистика, хранение и т.п.) не операция - пропускаются, даже
если КИЗ у них тоже заполнен: одна продажа с логистикой делит один КИЗ на несколько строк.

Защита от xlsx-бомбы размерности (файл в несколько КБ с заявленным диапазоном на миллион
строк): read_only + reset_dimensions (иначе openpyxl готовит ячейки на весь заявленный
диапазон уже при открытии) плюс жесткие потолки на число строк и колонок при чтении.
"""
from __future__ import annotations

import re
import zipfile
from datetime import date
from io import BytesIO
from typing import Any

import openpyxl

from . import kiz as kiz_mod
from .wb_api import OP_RETURN, OP_SALE

KIZ_HEADER_MARKERS = ("киз", "код маркировки")
OP_HEADER_MARKERS = ("тип документа",)
REASON_HEADER_MARKERS = ("обоснование для оплаты",)
ORDER_HEADER_MARKERS = ("номер сборочного задания",)
# выгрузка архива сборочных заданий: коды на отдельном листе, там "№ задания" и "Стоимость"
ARCHIVE_ORDER_MARKERS = ("№ задания",)
ARCHIVE_PRICE_MARKERS = ("стоимость",)
ARCHIVE_STATUS_MARKERS = ("статус задания",)
ARCHIVE_OP_MARKERS = ("тип операции",)
ARCHIVE_DATE_HEADERS = ("дата", "дата документа")
ARCHIVE_OP_VALUES = {"продажа": "Продажа", "возврат": "Возврат"}
OP_VALUES = {"продажа": OP_SALE, "возврат": OP_RETURN}
# какая колонка нужна ЧЗ, не подтверждено, см. README.md
PRICE_PRIMARY_MARKERS = ("вайлдберриз реализовал товар (пр)",)
PRICE_FALLBACK_MARKERS = ("цена розничная с учетом согласованной скидки",)
DATE_HEADER_MARKERS = ("дата продажи", "дата документа")

MAX_ROWS = 200_000
MAX_COLS = 300

# WB иногда отдает отчет zip-ом с одним xlsx внутри. Разворачиваем только когда браузер
# явно прислал zip-content-type - у "не того файла" в тестах content-type всегда generic,
# так что это не пересекается с проверкой "битый файл -> 400".
ZIP_CONTENT_TYPES = {"application/zip", "application/x-zip-compressed", "application/x-zip"}
ZIP_MAX_UNPACKED_BYTES = 50 * 1024 * 1024
ZIP_MAX_RATIO = 100
ZIP_MAX_DEPTH = 3
MAX_REPORTS = 30


class ExcelColumnError(ValueError):
    def __init__(self, headers: list[str], message: str | None = None):
        self.headers = headers
        self.no_kiz = message is None
        if message is None:
            found = ", ".join(h for h in headers if h) or "заголовков нет"
            message = (
                f"Не нашел колонку с КИЗ (заголовок должен содержать 'киз' или "
                f"'код маркировки'). Найденные заголовки: {found}"
            )
        super().__init__(message)


_PRICE_SPACE_CHARS = (" ", "\xa0", " ", " ", " ")


def _date_iso(cell: Any) -> str:
    """ГГГГ-ММ-ДД: сверка ищет последнюю операцию сравнением строк дат."""
    if cell in (None, ""):
        return ""
    if isinstance(cell, date):
        return cell.isoformat()[:10]
    text = str(cell).strip()
    m = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    if re.match(r"\d{4}-\d{2}-\d{2}", text):
        return text[:10]
    return text


def _archive_date_iso(cell: Any) -> str:
    """Дата из КИЗ-листа архива вида "11:51:00 07.10.2026": время стоит перед датой."""
    if cell in (None, ""):
        return ""
    if isinstance(cell, date):
        return cell.isoformat()[:10]
    text = str(cell).strip()
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    m = re.search(r"\d{4}-\d{2}-\d{2}", text)
    return m.group(0) if m else ""


def _read_archive_statuses(wb: Any) -> dict[int, str]:
    """Статус задания по номеру с листа, где есть колонки "№ задания" и "Статус задания".
    Остальные колонки листа (покупатель, телефон) не читаем."""
    out: dict[int, str] = {}
    for ws in _sheet_order(wb):
        ws.reset_dimensions()
        row_iter = ws.iter_rows(values_only=True, max_col=MAX_COLS)
        header_row = next(row_iter, ())
        headers = [str(h).strip() if h is not None else "" for h in header_row]
        order_col = _find_column(headers, ARCHIVE_ORDER_MARKERS)
        status_col = _find_column(headers, ARCHIVE_STATUS_MARKERS)
        if order_col is None or status_col is None:
            continue
        for i, row in enumerate(row_iter):
            if i >= MAX_ROWS:
                raise ExcelColumnError(headers, message=f"Слишком много строк в файле (> {MAX_ROWS}).")
            order_id = _order_id(row[order_col]) if order_col < len(row) else None
            status = row[status_col] if status_col < len(row) else None
            if order_id and status not in (None, ""):
                out.setdefault(order_id, str(status).strip())
    return out


def _to_kopecks(value: Any) -> int:
    if value is None or str(value).strip() == "":
        return 0
    s = str(value).strip()
    for ch in _PRICE_SPACE_CHARS:
        s = s.replace(ch, "")
    s = s.replace(",", ".")
    try:
        return abs(int(round(float(s) * 100)))
    except (TypeError, ValueError):
        return 0


def _decode_zip_name(info: zipfile.ZipInfo) -> str:
    """imя в zip без UTF-8 флага (бит 0x800) - легаси-кодировка, у WB встречается cp866."""
    if info.flag_bits & 0x800:
        return info.filename
    raw = info.filename.encode("cp437")
    try:
        return raw.decode("cp866")
    except UnicodeDecodeError:
        return info.filename


def _is_unsafe_zip_path(name: str) -> bool:
    if name.startswith("/") or name.startswith("\\") or ":" in name:
        return True
    return ".." in name.replace("\\", "/").split("/")


def unwrap_xlsx_from_zip(content: bytes) -> bytes:
    """zip от WB с единственным xlsx внутри - разворачиваем в память, на диск не пишем.

    Отказ: бомба (распакованный объем > 50 МБ или сжатие > 100 раз), несколько xlsx
    (какой из них нужный - непонятно, честная ошибка со списком), небезопасный путь
    записи (../, абсолютный) - even не пытаемся распаковать такой член."""
    try:
        zf = zipfile.ZipFile(BytesIO(content))
    except zipfile.BadZipFile as e:
        raise ExcelColumnError([], message=f"Не смог прочитать zip: {e}") from e

    infos = zf.infolist()
    _check_zip_bomb(infos)

    xlsx_members = []
    for info in infos:
        if info.is_dir():
            continue
        name = _decode_zip_name(info)
        if _is_unsafe_zip_path(name):
            raise ExcelColumnError([], message=f"Небезопасный путь в архиве: {name}")
        if name.lower().endswith(".xlsx"):
            xlsx_members.append(info)

    if not xlsx_members:
        raise ExcelColumnError([], message="В архиве нет xlsx-файла.")
    if len(xlsx_members) > 1:
        names = ", ".join(_decode_zip_name(i) for i in xlsx_members)
        raise ExcelColumnError([], message=f"В архиве несколько xlsx-файлов: {names}")

    return zf.read(xlsx_members[0])


def _check_zip_bomb(infos: list[zipfile.ZipInfo]) -> None:
    total_compressed = sum(i.compress_size for i in infos)
    total_uncompressed = sum(i.file_size for i in infos)
    if total_uncompressed > ZIP_MAX_UNPACKED_BYTES:
        raise ExcelColumnError([], message="Архив разворачивается больше чем в 50 МБ, отказ.")
    if total_compressed > 0 and total_uncompressed / max(total_compressed, 1) > ZIP_MAX_RATIO:
        raise ExcelColumnError([], message="Подозрительно большая степень сжатия архива, отказ.")


def collect_xlsx(
    name: str, content: bytes, depth: int = 0, budget: list[int] | None = None
) -> list[tuple[str, bytes]]:
    """Все отчеты из загруженного файла: xlsx как есть, zip с несколькими xlsx, zip из zip-ов
    (WB отдает недельный отчет архивом, клиент складывает основной и по выкупам в общий).

    zip узнаем по содержимому, а не по content-type: xlsx тоже zip, его выдает
    [Content_Types].xml в корне. Не zip - отдаем как есть, parse_report сам скажет, что не так."""
    if not zipfile.is_zipfile(BytesIO(content)):
        return [(name, content)]
    try:
        zf = zipfile.ZipFile(BytesIO(content))
    except zipfile.BadZipFile as e:
        raise ExcelColumnError([], message=f"Не смог прочитать zip {name}: {e}") from e
    if "[Content_Types].xml" in zf.namelist():
        return [(name, content)]
    if depth >= ZIP_MAX_DEPTH:
        raise ExcelColumnError([], message=f"Слишком глубокая вложенность архивов: {name}")

    infos = zf.infolist()
    _check_zip_bomb(infos)
    if budget is None:
        budget = [ZIP_MAX_UNPACKED_BYTES * 2]
    out: list[tuple[str, bytes]] = []
    for info in infos:
        if info.is_dir():
            continue
        member = _decode_zip_name(info)
        if _is_unsafe_zip_path(member):
            raise ExcelColumnError([], message=f"Небезопасный путь в архиве: {member}")
        base = member.replace("\\", "/").rsplit("/", 1)[-1]
        if member.startswith("__MACOSX/") or base.startswith(("._", "~$")):
            continue
        if not base.lower().endswith((".xlsx", ".zip")):
            continue
        budget[0] -= info.file_size
        if budget[0] < 0:
            raise ExcelColumnError([], message="Архивы вместе разворачиваются больше чем в 100 МБ, отказ.")
        if base.lower().endswith(".xlsx"):
            out.append((base, zf.read(info)))
        else:
            out.extend(collect_xlsx(base, zf.read(info), depth + 1, budget))
        if len(out) > MAX_REPORTS:
            raise ExcelColumnError([], message=f"Больше {MAX_REPORTS} отчетов за раз, разбейте загрузку.")
    if not out and depth == 0:
        raise ExcelColumnError([], message=f"В архиве {name} нет xlsx-отчетов.")
    return out


def _sheet_order(wb: Any) -> list[Any]:
    """Активный лист первым (как было раньше), затем остальные по порядку в книге."""
    sheets = list(wb.worksheets)
    active = wb.active
    if active is not None and active in sheets:
        sheets.remove(active)
        sheets.insert(0, active)
    return sheets


def _pick_sheet(wb: Any) -> tuple[Any, list[str], int, Any]:
    """Первый лист, где в первой строке есть колонка КИЗ. Возвращает (лист, заголовки,
    номер колонки КИЗ, итератор строк после заголовка). Не нашли нигде: ExcelColumnError
    с заголовками активного листа."""
    first_headers: list[str] | None = None
    for ws in _sheet_order(wb):
        ws.reset_dimensions()
        row_iter = ws.iter_rows(values_only=True, max_col=MAX_COLS)
        try:
            header_row = next(row_iter)
        except StopIteration:
            header_row = ()
        headers = [str(h).strip() if h is not None else "" for h in header_row]
        if first_headers is None:
            first_headers = headers
        if len(headers) > MAX_COLS:
            raise ExcelColumnError(headers, message=f"Слишком много колонок в файле (> {MAX_COLS}).")
        kiz_col = _find_column(headers, KIZ_HEADER_MARKERS)
        if kiz_col is not None:
            return ws, headers, kiz_col, row_iter
    raise ExcelColumnError(first_headers or [])


def parse_report(content: bytes) -> list[dict[str, Any]]:
    try:
        wb = openpyxl.load_workbook(BytesIO(content), data_only=True, read_only=True)
        if not wb.worksheets:
            raise ExcelColumnError([])
        _ws, headers, kiz_col, row_iter = _pick_sheet(wb)
    except ExcelColumnError:
        raise
    except Exception as e:
        raise ExcelColumnError([], message=f"Не смог прочитать файл как Excel-отчет: {e}") from e

    op_col = _find_column(headers, OP_HEADER_MARKERS)
    price_col = _find_column(headers, PRICE_PRIMARY_MARKERS)
    price_fallback_col = _find_column(headers, PRICE_FALLBACK_MARKERS)
    date_col = _find_column(headers, DATE_HEADER_MARKERS)
    reason_col = _find_column(headers, REASON_HEADER_MARKERS)
    order_col = _find_column(headers, ORDER_HEADER_MARKERS)
    # архив: нет "Тип документа", но есть "№ задания". Его колонки даты и типа операции
    # заполнены не у всех строк, поэтому операцию потом ставят живые статусы WB
    archive = False
    arh_op_col = arh_date_col = None
    if op_col is None and order_col is None:
        archive_order_col = _find_column(headers, ARCHIVE_ORDER_MARKERS)
        if archive_order_col is not None:
            archive = True
            order_col = archive_order_col
            date_col = None
            arh_op_col = _find_column(headers, ARCHIVE_OP_MARKERS)
            arh_date_col = next((i for i, h in enumerate(headers) if h.lower() in ARCHIVE_DATE_HEADERS), None)
            if price_col is None and price_fallback_col is None:
                price_fallback_col = _find_column(headers, ARCHIVE_PRICE_MARKERS)

    data_rows: list[tuple] = []
    for i, row in enumerate(row_iter):
        if i >= MAX_ROWS:
            raise ExcelColumnError(headers, message=f"Слишком много строк в файле (> {MAX_ROWS}).")
        data_rows.append(row)

    if op_col is None and not archive:
        op_col = _find_operation_column_by_values(data_rows)
    arh_statuses = _read_archive_statuses(wb) if archive else {}

    seen: set[tuple[str, str, str]] = set()
    arh_pos: dict[tuple[str, str, str], int] = {}
    items: list[dict[str, Any]] = []
    for row in data_rows:
        raw = row[kiz_col] if kiz_col < len(row) else None
        has_kiz = raw is not None and str(raw).strip() != ""
        order_id = _order_id(row[order_col]) if order_col is not None and order_col < len(row) else None
        if not has_kiz and (order_id is None or archive):
            continue

        if op_col is not None:
            cell = row[op_col] if op_col < len(row) else None
            op = OP_VALUES.get(str(cell).strip().lower()) if cell else None
            if op is None:
                continue  # не строка операции (логистика, хранение, комиссии...)
            reason = row[reason_col] if reason_col is not None and reason_col < len(row) else None
            if reason not in (None, "") and str(reason).strip().lower() not in OP_VALUES:
                continue  # у FBS "Продажа" стоит и на возмещении за ПВЗ (отчеты клиента 06.10.2026)
        else:
            op = OP_SALE  # колонку операции не нашли - без нее считаем все продажей

        date_cell = row[date_col] if date_col is not None and date_col < len(row) else None
        date_str = _date_iso(date_cell)
        price_cell = row[price_col] if price_col is not None and price_col < len(row) else None
        if price_cell in (None, ""):
            price_cell = (
                row[price_fallback_col]
                if price_fallback_col is not None and price_fallback_col < len(row)
                else None
            )

        if not has_kiz:
            # FBS: в отчете код пустой, его отдает WB по номеру сборочного задания
            key = (f"#{order_id}", op, date_str)
            if key in seen:
                continue
            seen.add(key)
            items.append(
                {
                    "cis_raw": "",
                    "order_id": order_id,
                    "need_meta": True,
                    "op": op,
                    "product_cost": _to_kopecks(price_cell),
                    "date": date_str,
                }
            )
            continue

        cis_raw = str(raw).strip()
        norm = kiz_mod.normalize_kiz(cis_raw)
        key = (norm, op, date_str)
        replace_at = None
        if key in seen:
            # у архива нет даты: один код в нескольких заданиях оставляем последним по номеру задания
            if not archive or (order_id or 0) <= (items[arh_pos[key]].get("order_id") or 0):
                continue  # тот же КИЗ той же операции той же датой - строка логистики той же продажи
            replace_at = arh_pos[key]
        seen.add(key)

        item = {
            "cis_raw": cis_raw,
            "op": op,
            "product_cost": _to_kopecks(price_cell),
            "date": date_str,
            "order_id": order_id,
            "fbo": not archive and order_col is not None and order_id is None,
        }
        if archive:
            item["archive"] = True
            op_cell = row[arh_op_col] if arh_op_col is not None and arh_op_col < len(row) else None
            date_cell = row[arh_date_col] if arh_date_col is not None and arh_date_col < len(row) else None
            item["arh_status"] = arh_statuses.get(order_id, "")
            item["arh_op"] = ARCHIVE_OP_VALUES.get(str(op_cell).strip().lower(), "") if op_cell else ""
            item["arh_date"] = _archive_date_iso(date_cell)
            if replace_at is not None:
                items[replace_at] = item
                continue
            arh_pos[key] = len(items)
        items.append(item)
    return items


def _order_id(cell: Any) -> int | None:
    s = str(cell).strip() if cell is not None else ""
    if s.endswith(".0"):
        s = s[:-2]
    return int(s) if s.isdigit() and int(s) > 0 else None


def _find_column(headers: list[str], markers: tuple[str, ...]) -> int | None:
    for i, h in enumerate(headers):
        low = h.lower()
        if any(marker in low for marker in markers):
            return i
    return None


def _find_operation_column_by_values(data_rows: list[tuple]) -> int | None:
    """Резервный поиск, если заголовка "Тип документа" нет: колонка со значениями
    ровно "Продажа"/"Возврат" (без примесей типа "Обоснование для оплаты")."""
    max_cols = max((len(r) for r in data_rows), default=0)
    counts = [0] * max_cols
    for row in data_rows:
        for i, cell in enumerate(row):
            if cell is None:
                continue
            if str(cell).strip().lower() in OP_VALUES:
                counts[i] += 1
    if not counts or max(counts) == 0:
        return None
    return counts.index(max(counts))
