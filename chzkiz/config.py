"""Настройки из переменных окружения. Один модуль, больше их никто не читает напрямую."""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
FERNET_KEY = os.environ.get("FERNET_KEY", "")
# Только для ручной проверки реального API WB стороной разработчика, в сценарий пользователя не идет.
WB_DEMO_TOKEN = os.environ.get("WB_DEMO_TOKEN", "")
# mock | real. Без значения по умолчанию: забытый .env не должен молча включать демо-режим в проде.
CHZ_MODE = os.environ.get("CHZ_MODE", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://127.0.0.1:8140").rstrip("/")
WEB_PORT = int(os.environ.get("CHZ_KIZ_WEB_PORT", "8140"))
DB_PATH = os.environ.get("CHZ_KIZ_DB", str(BASE_DIR / "data" / "chz_kiz.db"))
REPORTS_DIR = os.environ.get("CHZ_KIZ_REPORTS", str(BASE_DIR / "data" / "reports"))

LOGIN_LINK_TTL_MIN = 15
SESSION_TTL_HOURS = 720
UPLOAD_MAX_BYTES = 20 * 1024 * 1024

DEFAULT_PG = "lp"  # товарная группа ЧЗ по умолчанию (легкая промышленность)
# Коды товарных групп по справочнику True API (приложение 1). На живых документах проверена только lp.
PG_OPTIONS = {
    "lp": "Одежда (lp)",
    "shoes": "Обувь (shoes)",
    "bio": "БАДы (bio)",
    "carparts": "Автозапчасти (carparts)",
    "alcohol": "Алкоголь (alcohol)",
    "otp": "Альтернативный табак (otp)",
    "antiseptic": "Антисептики (antiseptic)",
    "grocery": "Бакалея (grocery)",
    "nabeer": "Безалкогольное пиво (nabeer)",
    "bicycle": "Велосипеды (bicycle)",
    "vetpharma": "Ветеринарные препараты (vetpharma)",
    "perfumery": "Духи и туалетная вода (perfumery)",
    "toys": "Игрушки (toys)",
    "cableraw": "Кабельная продукция (cableraw)",
    "conserve": "Консервы (conserve)",
    "petfood": "Корма для животных (petfood)",
    "chemistry": "Косметика, бытовая химия, гигиена (chemistry)",
    "pharmaraw": "Лекарственное сырье (pharmaraw)",
    "wheelchairs": "Медицинские изделия (wheelchairs)",
    "milk": "Молочная продукция (milk)",
    "seafood": "Морепродукты (seafood)",
    "autofluids": "Моторные масла (autofluids)",
    "meat": "Мясные изделия (meat)",
    "furslp": "Натуральный мех (furslp)",
    "ncp": "Никотиносодержащая продукция (ncp)",
    "gadgets": "Ноутбуки и смартфоны (gadgets)",
    "opticfiber": "Оптоволокно (opticfiber)",
    "heater": "Отопительные приборы (heater)",
    "books": "Печатная продукция (books)",
    "beer": "Пиво (beer)",
    "pyrotechnics": "Пиротехника (pyrotechnics)",
    "fire": "Пожарная безопасность (fire)",
    "polymer": "Полимерные трубы (polymer)",
    "frozen": "Полуфабрикаты и заморозка (frozen)",
    "radio": "Радиоэлектроника (radio)",
    "vegetableoil": "Растительные масла (vegetableoil)",
    "sweets": "Сладости (sweets)",
    "softdrinks": "Соки и безалкогольные напитки (softdrinks)",
    "construction": "Стройматериалы (construction)",
    "tobacco": "Табак (tobacco)",
    "titan": "Титановая продукция (titan)",
    "homeware": "Товары для дома (homeware)",
    "furs": "Товары из меха (furs)",
    "fertilizers": "Удобрения (fertilizers)",
    "water": "Упакованная вода (water)",
    "electronics": "Фотокамеры и вспышки (electronics)",
    "tires": "Шины (tires)",
    "nicotindev": "Электронные сигареты (nicotindev)",
}


def is_mock() -> bool:
    return CHZ_MODE.strip().lower() == "mock"


def check_mode() -> None:
    """Вызывается при старте панели и бота: без явного CHZ_MODE не запускаемся."""
    mode = CHZ_MODE.strip().lower()
    if mode not in ("mock", "real"):
        raise RuntimeError(
            f"CHZ_MODE должен быть mock или real, сейчас: {CHZ_MODE!r}. Проверьте, что .env загружен."
        )


def cookie_secure() -> bool:
    """Cookie сессии только по HTTPS, если панель опубликована по HTTPS."""
    return PUBLIC_URL.lower().startswith("https://")
