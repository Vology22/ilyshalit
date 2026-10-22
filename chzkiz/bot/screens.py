"""Экраны сценария /out: чистые функции, возвращают текст и клавиатуру, без обращения к базе и сети."""
from __future__ import annotations

from pathlib import Path

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .. import config

IMG_DIR = Path(__file__).resolve().parent / "img"
IMG_API = IMG_DIR / "api_token.png"
IMG_ARCHIVE = IMG_DIR / "files_archive.png"
IMG_FINREPORT = IMG_DIR / "files_finreport.png"

TOKEN_PATH = (
    "Профиль → Интеграции по API → Создать токен → Для интеграции вручную → "
    "тип Персональный (не Базовый) → Только на чтение → категории Маркетплейс, Финансы, Аналитика"
)
TOKEN_SAFE = "Токен только читает данные и ничего в вашем кабинете не меняет."
FILES_PATH = (
    "Поставки и заказы → Маркетплейс (FBS) → вкладка Завершенные → Выгрузить в Excel, период до 30 дней"
)


def _cb(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=list(rows))


def start_screen() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "Привет! Выведу из оборота проданные КИЗ и верну в оборот КИЗ по возвратам "
        "ваших товаров на Wildberries FBS. Документы подписываете своей подписью в панели, "
        "флешка остается у вас.\n\nС чего начнем?"
    )
    return text, _kb(
        [_cb("🔑 Подключить WB по API", "sv:api")],
        [_cb("🚀 Сделать сверку", "sv:go")],
        [_cb("📋 Сделать сверку с инструкцией", "sv:ins")],
    )


def out_screen() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "Сверка КИЗ Wildberries FBS\n\n"
        "1️⃣ Проверю статусы ваших КИЗ в Честном знаке\n"
        "2️⃣ Выведу из оборота КИЗ, которые уже проданы\n"
        "3️⃣ Верну в оборот КИЗ, которые покупатель вернул\n\n"
        "С вас 3 клика, остальное сделаю сам."
    )
    return text, _kb(
        [_cb("🚀 Сделать сверку", "sv:go")],
        [_cb("📋 Сделать сверку с инструкцией", "sv:ins")],
        [InlineKeyboardButton(text="▶️ Видео по сверке", url=f"{config.PUBLIC_URL}/instrukciya#video")],
    )


def go_screen(link: str) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "Осталась пара кликов:\n\n"
        "1️⃣ Откройте сверку кнопкой ниже\n"
        "2️⃣ Загрузите отчеты WB или выберите сверку по API\n"
        "3️⃣ Нажмите Вывести и вернуть\n"
        "✅ Готово\n\n"
        "Ссылка разовая и работает 15 минут. Открывайте на компьютере, "
        "где вставлена флешка с подписью."
    )
    return text, _kb([InlineKeyboardButton(text="Открыть сверку", url=link)])


def ins_screen() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "Помогу подготовить данные из Wildberries, потом сверка в 3 клика.\n\n"
        "Как будем сверять?\n\n"
        "🔑 По API: без файлов, токен WB отправляете один раз\n"
        "📁 По файлам: скачиваете отчеты из кабинета WB"
    )
    return text, _kb(
        [_cb("🔑 Через API WB (рекомендую)", "sv:api")],
        [_cb("📁 Через файлы отчетов", "sv:files")],
        [_cb("⬅️ Назад", "sv:menu")],
    )


def api_have_screen() -> tuple[str, InlineKeyboardMarkup]:
    text = "Токен WB уже подключен ✅ Можно к сверке или заменить токен."
    return text, _kb(
        [_cb("Дальше", "sv:fl")],
        [_cb("🔁 Заменить токен", "sv:api_new")],
        [_cb("⬅️ Назад", "sv:ins")],
    )


def api_new_screen() -> tuple[Path, str, InlineKeyboardMarkup]:
    caption = (
        "Как получить токен WB\n\n"
        "1️⃣ Кабинет WB → Профиль (справа вверху)\n"
        "2️⃣ Интеграции по API\n"
        "3️⃣ Создать токен\n"
        "4️⃣ Для интеграции вручную, тип Персональный (не Базовый)\n"
        "5️⃣ Доступ: Только на чтение, категории Маркетплейс, Финансы, Аналитика. Статистика не нужна\n\n"
        f"{TOKEN_SAFE}\n\n"
        "Скопируйте токен и пришлите сюда одним сообщением. "
        "Сообщение удалю сразу, токен храню зашифрованным."
    )
    return IMG_API, caption, _kb([_cb("⬅️ Назад", "sv:ins")])


def api_ok_screen() -> tuple[str, InlineKeyboardMarkup]:
    return "Токен подключен и проверен ✅", _kb([_cb("Дальше", "sv:fl")])


def files_screens() -> list[tuple[Path, str, InlineKeyboardMarkup | None]]:
    cap1 = (
        "Шаг 1 из 2. Архив сборочных заданий\n\n"
        "1️⃣ Кабинет WB → Поставки и заказы → Маркетплейс (FBS)\n"
        "2️⃣ Вкладка Завершенные\n"
        "3️⃣ Выгрузить в Excel, период не больше 30 дней\n\n"
        "Нужен период длиннее: выгрузите его частями, каждая не больше 30 дней. "
        "Панель принимает несколько файлов сразу (.xlsx или .zip)."
    )
    cap2 = (
        "Шаг 2 из 2. Финансовые отчеты за тот же период\n\n"
        "1️⃣ Кабинет WB → Финансовые отчеты (кнопка на главной странице рядом с балансом)\n"
        "2️⃣ Отчеты реализации → Еженедельные\n"
        "3️⃣ У каждой недели два отчета: Основной и По выкупам. Нужны оба\n"
        "4️⃣ Откройте каждый отчет и скачайте детализацию в Excel\n\n"
        "За месяц выходит 8-10 файлов, загрузите в панель все сразу. "
        "Из них панель берет возвраты, цену и дату продажи."
    )
    kb = _kb([_cb("✅ Файлы скачаны", "sv:fl")], [_cb("⬅️ Назад", "sv:ins")])
    return [(IMG_ARCHIVE, cap1, None), (IMG_FINREPORT, cap2, kb)]


def fl_screen() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "Флешка с подписью (УКЭП) вставлена в этот компьютер?\n"
        "С телефона и без флешки вывести КИЗ не получится."
    )
    return text, _kb(
        [_cb("✅ Да, перейти к сверке", "sv:go")],
        [_cb("🤔 Нет, что делать?", "sv:nofl")],
        [_cb("⬅️ Назад", "sv:ins")],
    )


def nofl_text() -> str:
    return (
        "Откройте этот чат в Telegram на компьютере с флешкой, КриптоПро и плагином "
        "и нажмите кнопку там. Или перешлите следующее сообщение на тот компьютер, "
        "ссылка в нем работает 15 минут."
    )


def category_screen() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "Кто вы? Выберите один раз, от этого зависит реферальная программа:\n\n"
        f"🛍 Селлер или менеджер - {config.REF_SELLER_BONUS_OPS} операции за каждого приглашенного, "
        "который оплатит тариф\n"
        f"📦 Фулфилмент - {config.REF_FF_PERCENT}% от оплат приглашенных клиентов, выплаты раз в неделю"
    )
    return text, _kb(
        [_cb("🛍 Селлер / менеджер", "cat:seller")],
        [_cb("📦 Фулфилмент", "cat:fulfillment")],
    )


def cabinet_screen(link: str, balance: int) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"На балансе {balance} операций. Одна операция - один документ вывода или возврата, "
        "принятый Честным знаком.\n\n"
        "В личном кабинете тарифы, история и реферальная ссылка. Ссылка разовая и работает 15 минут."
    )
    return text, _kb([InlineKeyboardButton(text="💳 Личный кабинет", url=link)])


def info_screen() -> tuple[str, InlineKeyboardMarkup]:
    contacts = []
    if config.SUPPORT_TG:
        contacts.append(f"Telegram: @{config.SUPPORT_TG}")
    if config.SUPPORT_EMAIL:
        contacts.append(f"Email: {config.SUPPORT_EMAIL}")
    text = (
        "Тарифы, документы и поддержка\n\n"
        "Одна операция - один документ вывода или возврата в Честном знаке. "
        f"При регистрации {config.FREE_OPS} операции бесплатно.\n\n"
        "Поддержка:\n" + ("\n".join(contacts) or "контакты скоро появятся") + "\n"
        f"Время работы: {config.SUPPORT_HOURS}"
    )
    url = config.PUBLIC_URL
    return text, _kb(
        [InlineKeyboardButton(text="💰 Тарифы и цены", url=f"{url}/tariffs")],
        [InlineKeyboardButton(text="📄 Пользовательское соглашение", url=f"{url}/terms")],
        [InlineKeyboardButton(text="🔒 Политика конфиденциальности", url=f"{url}/privacy")],
        [InlineKeyboardButton(text="💬 Поддержка", url=f"{url}/support")],
    )
