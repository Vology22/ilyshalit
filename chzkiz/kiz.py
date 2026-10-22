"""Нормализация кода маркировки (КИЗ): убрать GS-разделители и хвост криптоподписи."""
import re

GS = "\x1d"  # ASCII Group Separator, разделитель полей в DataMatrix ЧЗ
_TAIL_PREFIXES = ("91", "92")  # AI хвоста: 91 ключ проверки, 92 подпись
_LITERAL_GS_RE = re.compile(r"\\u001d", re.IGNORECASE)  # GS как текст в отчетах WB (файл клиента 26.09)
_XML_ESCAPED_GS_RE = re.compile(r"_x001d_", re.IGNORECASE)  # GS, который Excel пишет как OOXML-escape
_REPLACEMENT_GS_RE = re.compile("\ufffd")  # GS, потерявший кодировку при выгрузке архива заданий (файл клиента 09.10)


def normalize_kiz(raw: str) -> str:
    """Возвращает нормальный вид кода: без GS и без хвоста криптоподписи.

    Формат кода: (01)<gtin>(21)<серия>GS(91)<ключ>GS(92)<подпись>.
    В выгрузках Wildberries символ GS иногда приходит не байтом, а текстом
    "\\u001d" (6 знаков) или как OOXML-escape "_x001D_" - заменяем оба на настоящий GS перед разбором.
    Если разделителей GS нет вовсе - обрезать хвост нельзя, возвращаем как есть.
    """
    raw = _LITERAL_GS_RE.sub(GS, raw.strip())
    raw = _XML_ESCAPED_GS_RE.sub(GS, raw)
    raw = _REPLACEMENT_GS_RE.sub(GS, raw)
    if GS not in raw:
        return raw
    kept = []
    for part in raw.split(GS):
        if part.startswith(_TAIL_PREFIXES):
            break
        kept.append(part)
    return "".join(kept).strip()


def is_well_formed(normalized: str) -> bool:
    """Грубая проверка формата: начинается с (01) - 14 цифр gtin - (21) - серия."""
    if not normalized.startswith("01"):
        return False
    body = normalized[2:]
    if len(body) < 14 or not body[:14].isdigit():
        return False
    rest = body[14:]
    return rest.startswith("21") and len(rest) > 2
