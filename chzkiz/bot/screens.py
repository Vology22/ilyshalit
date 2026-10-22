"""Экраны бота: чистые функции, возвращают текст (HTML) и клавиатуру, без обращения к базе и сети.

Навигация одним сообщением: кнопки меню редактируют текущий экран, а не шлют новый.
Ссылка входа в панель живет только в кнопке, в тексте ее нет."""
from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .. import billing, config

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

HOME = "m:home"


def _cb(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _url(text: str, url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, url=url)


def _kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[r for r in rows if r])


def _home_row() -> list[InlineKeyboardButton]:
    return [_cb("🏠 Меню", HOME)]


def ops_word(n: int) -> str:
    """1 сверка, 3 сверки, 5 сверок."""
    return billing.sverki(n)


def _sub_line(sub: dict[str, Any] | None) -> str:
    if not sub:
        return ""
    return f"\n♾ Безлимит до <b>{billing.date(sub['ends_at'])}</b>, ИНН {len(sub['inns'])} из {sub['inn_limit']}"


def _rub(kop: int) -> str:
    rub = f"{kop // 100:,}".replace(",", " ")
    return f"{rub},{kop % 100:02d} ₽" if kop % 100 else f"{rub} ₽"


# --- знакомство и меню --------------------------------------------------------------

def welcome_screen(name: str = "") -> tuple[str, InlineKeyboardMarkup]:
    hello = f"Привет, {escape(name)}!" if name else "Привет!"
    text = (
        f"👋 <b>{hello}</b>\n\n"
        "Я вывожу из оборота проданные КИЗ и возвращаю в оборот КИЗ по возвратам ваших товаров "
        "на Wildberries FBS. Документы вы подписываете своей подписью, флешка остается у вас.\n\n"
        f"🎁 Дарю <b>{ops_word(config.FREE_OPS)}</b> на старт.\n\n"
        "<b>Кто вы?</b> Выберите один раз, от этого зависит реферальная программа:\n"
        f"🛍 <b>Селлер или менеджер</b>: {ops_word(config.REF_SELLER_BONUS_OPS)} за каждого приглашенного, "
        "который оплатит тариф\n"
        f"📦 <b>Фулфилмент</b>: {config.REF_FF_PERCENT}% от оплат приглашенных клиентов, выплаты раз в неделю"
    )
    return text, _kb(
        [_cb("🛍 Селлер / менеджер", "cat:seller")],
        [_cb("📦 Фулфилмент", "cat:fulfillment")],
    )


# старое имя: категорию спрашивает тот же экран знакомства
category_screen = welcome_screen


def home_screen(
    balance: int, has_key: bool, category: str = "", team: bool = False, sub: dict[str, Any] | None = None,
    chat: str = "",
) -> tuple[str, InlineKeyboardMarkup]:
    """chat: '' - обращения нет, open - открыто, unread - оператор ответил, пользователь не видел."""
    who = config.CATEGORIES.get(category, "")
    text = (
        "<b>Сверка КИЗ · Wildberries FBS</b>\n\n"
        "Выведу из оборота проданные КИЗ и верну в оборот КИЗ по возвратам ваших товаров. "
        "Подпись остается у вас: документы подписываете в панели на компьютере.\n\n"
        f"💳 Баланс: <b>{ops_word(balance)}</b>{_sub_line(sub)}\n"
        f"🔑 Токен WB: {'подключен ✅' if has_key else 'не подключен'}"
        + (f"\n👤 {who}" if who else "")
    )
    chat_row = []
    if chat:
        label = "💬 Чат с поддержкой · 🔴 новый ответ" if chat == "unread" else "💬 Чат с поддержкой"
        chat_row = [_cb(label, "sup:chat")]
    return text, _kb(
        chat_row,
        [_cb("🚀 Сделать сверку", "m:go")],
        [_cb("🔑 Токен WB", "m:wb"), _cb("📁 По файлам", "m:files")],
        [_cb("💳 Баланс и тарифы", "m:bal"), _cb("🎁 Пригласить", "m:ref")],
        [_cb("📖 Инструкция", "ins:0"), _cb("💬 Помощь", "m:help")],
        [_cb("🛠 Админ-панель", "adm:home")] if team else [],
    )


# --- сверка -----------------------------------------------------------------------------

def sverka_screen(has_key: bool, balance: int, sub: dict[str, Any] | None = None) -> tuple[str, InlineKeyboardMarkup]:
    source = (
        "🔑 Данные возьму из WB по API, токен уже подключен."
        if has_key else
        "📁 Токен WB не подключен: загрузите в панели файлы отчетов WB или подключите токен, "
        "тогда данные возьму сам."
    )
    empty = balance < 1 and not sub
    low = "\n\n⚠️ Сверки закончились: купите сверки или безлимит, чтобы отправить документы." if empty else ""
    text = (
        "🚀 <b>Сверка КИЗ</b>\n\n"
        "1️⃣ Проверю статусы ваших КИЗ в Честном знаке\n"
        "2️⃣ Выведу из оборота КИЗ, которые уже проданы\n"
        "3️⃣ Верну в оборот КИЗ, которые покупатель вернул\n\n"
        f"{source}{low}"
    )
    rows: list[list[InlineKeyboardButton]] = [[_cb("🔗 Открыть панель сверки", "m:link")]]
    if not has_key:
        rows.append([_cb("🔑 Подключить токен", "m:wb_new"), _cb("📁 Какие файлы", "m:files")])
    if empty:
        rows.append([_cb("💳 Купить сверки", "m:bal")])
    rows.append([_url("▶️ Видео по сверке", f"{config.PUBLIC_URL}/instrukciya#video")])
    rows.append(_home_row())
    return text, _kb(*rows)


def link_screen(link: str) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "🔗 <b>Ссылка на панель готова</b>\n\n"
        "Откройте ее на компьютере, где:\n"
        "• вставлена флешка с подписью (УКЭП)\n"
        "• установлены КриптоПро CSP и плагин для браузера\n\n"
        "Ссылка разовая и работает 15 минут."
    )
    return text, _kb(
        [_url("🖥 Открыть панель", link)],
        [_cb("📱 Я с телефона", "m:phone")],
        _home_row(),
    )


def phone_screen(link: str) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "📱 <b>С телефона подписать документы не получится</b>\n\n"
        "Откройте этот чат в Telegram на компьютере с флешкой и нажмите кнопку там. "
        "Или перешлите это сообщение на тот компьютер: ссылка в кнопке работает 15 минут."
    )
    return text, _kb([_url("🖥 Открыть панель", link)], _home_row())


# --- токен WB -------------------------------------------------------------------------

def wb_have_screen() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "🔑 <b>Токен WB подключен</b> ✅\n\n"
        "Данные для сверки возьму из WB сам. Если токен отозвали или он истек, замените его."
    )
    return text, _kb(
        [_cb("🚀 Сделать сверку", "m:go")],
        [_cb("🔁 Заменить токен", "m:wb_new")],
        _home_row(),
    )


def api_new_screen() -> tuple[Path, str, InlineKeyboardMarkup]:
    caption = (
        "🔑 <b>Как получить токен WB</b>\n\n"
        "1️⃣ Кабинет WB → Профиль (справа вверху)\n"
        "2️⃣ Интеграции по API\n"
        "3️⃣ Создать токен\n"
        "4️⃣ Для интеграции вручную, тип Персональный (не Базовый)\n"
        "5️⃣ Доступ: Только на чтение, категории Маркетплейс, Финансы, Аналитика. Статистика не нужна\n\n"
        f"{TOKEN_SAFE}\n\n"
        "<b>Пришлите токен сюда одним сообщением.</b> Сообщение удалю сразу, токен храню зашифрованным."
    )
    return IMG_API, caption, _kb([_cb("✖️ Отмена", HOME)])


def api_ok_screen() -> tuple[str, InlineKeyboardMarkup]:
    return (
        "✅ <b>Токен WB подключен и проверен</b>\n\nТеперь можно делать сверку без файлов.",
        _kb([_cb("🚀 Сделать сверку", "m:go")], _home_row()),
    )


def api_bad_screen() -> tuple[str, InlineKeyboardMarkup]:
    return (
        "❌ <b>Токен не сработал</b> на запросе заказов WB.\n\n"
        "Проверьте, что тип Персональный и выбраны категории Маркетплейс, Финансы, Аналитика, "
        "и пришлите токен еще раз.",
        _kb([_cb("✖️ Отмена", HOME)]),
    )


# --- файлы ------------------------------------------------------------------------------

def files_screens() -> list[tuple[Path, str]]:
    cap1 = (
        "<b>Шаг 1 из 2. Архив сборочных заданий</b>\n\n"
        "1️⃣ Кабинет WB → Поставки и заказы → Маркетплейс (FBS)\n"
        "2️⃣ Вкладка Завершенные\n"
        "3️⃣ Выгрузить в Excel, период не больше 30 дней\n\n"
        "Нужен период длиннее: выгрузите его частями, каждая не больше 30 дней. "
        "Панель принимает несколько файлов сразу (.xlsx или .zip)."
    )
    cap2 = (
        "<b>Шаг 2 из 2. Финансовые отчеты за тот же период</b>\n\n"
        "1️⃣ Кабинет WB → Финансовые отчеты (кнопка на главной странице рядом с балансом)\n"
        "2️⃣ Отчеты реализации → Еженедельные\n"
        "3️⃣ У каждой недели два отчета: Основной и По выкупам. Нужны оба\n"
        "4️⃣ Откройте каждый отчет и скачайте детализацию в Excel\n\n"
        "За месяц выходит 8-10 файлов, загрузите в панель все сразу. "
        "Из них панель берет возвраты, цену и дату продажи."
    )
    return [(IMG_ARCHIVE, cap1), (IMG_FINREPORT, cap2)]


def files_done_screen() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "📁 <b>Сверка по файлам</b>\n\n"
        "Скачайте оба вида файлов по инструкции выше, затем откройте панель и загрузите их все сразу."
    )
    return text, _kb([_cb("✅ Файлы скачаны, открыть панель", "m:link")], _home_row())


# --- баланс, рефералы, помощь ------------------------------------------------------------

def balance_screen(
    balance: int, tariffs: list[Any], link: str, sub: dict[str, Any] | None = None
) -> tuple[str, InlineKeyboardMarkup]:
    lines = [f"💳 <b>Баланс: {ops_word(balance)}</b>{_sub_line(sub)}\n"]
    if sub:
        inns = ", ".join(sub["inns"]) or "закрепятся первыми подписями"
        lines.append(f"ИНН безлимита: {escape(inns)}\n")
    lines.append(
        "Одна сверка = один запуск со всеми документами вывода и возврата. "
        "Если Честный знак не принял ни один документ сверки, она вернется.\n"
    )
    if tariffs:
        lines.append("<b>Тарифы</b>")
        for t in tariffs:
            what = (f"{t['days']} дней, до {t['inn_limit']} ИНН" if t["kind"] == "unlim" else ops_word(t["ops"]))
            lines.append(f"• {escape(t['name'])}: {what} за <b>{_rub(t['price_kop'])}</b>")
    lines.append("\nКупить тариф и посмотреть историю можно в личном кабинете. Ссылка в кнопке разовая, 15 минут.")
    return "\n".join(lines), _kb([_url("💳 Личный кабинет", link)], _home_row())


# старое имя для /cabinet
def cabinet_screen(link: str, balance: int) -> tuple[str, InlineKeyboardMarkup]:
    return balance_screen(balance, [], link)


def ref_screen(category: str, ref_link: str, stats: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    if category == "fulfillment":
        offer = (
            f"Вы получаете <b>{config.REF_FF_PERCENT}%</b> от всех оплат приглашенных клиентов. "
            "Выплаты раз в неделю на реквизиты из личного кабинета."
        )
        earned = f"К выплате: <b>{_rub(stats['accrued_kop'])}</b>, выплачено: {_rub(stats['paid_kop'])}"
    else:
        offer = (
            f"За каждого приглашенного, который оплатит тариф, вы получите "
            f"<b>{ops_word(config.REF_SELLER_BONUS_OPS)}</b> на баланс."
        )
        earned = f"Получено: <b>{ops_word(stats['ops'])}</b>"
    has_url = ref_link.startswith("https://")
    text = (
        "🎁 <b>Пригласите коллег</b>\n\n"
        f"{offer}\n\n"
        f"Ваша ссылка:\n<code>{escape(ref_link)}</code>\n\n"
        f"👥 Пришли по ссылке: <b>{stats['invited']}</b>, оплатили: <b>{stats['paid']}</b>\n"
        f"{earned}"
    )
    share = (
        "https://t.me/share/url?url=" + quote(ref_link, safe="")
        + "&text=" + quote("Сверка КИЗ для Wildberries FBS: вывод и возврат в Честном знаке в пару кликов", safe="")
    )
    return text, _kb([_url("📤 Поделиться", share)] if has_url else [], _home_row())


def info_screen() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "📄 <b>Тарифы и документы</b>\n\n"
        "Одна сверка = один запуск со всеми документами вывода и возврата. "
        f"При регистрации {ops_word(config.FREE_OPS)} бесплатно. "
        "Есть безлимит на месяц для 2 или 3 ИНН.\n\n"
        f"Поддержка отвечает прямо в этом боте. Время работы: {escape(config.SUPPORT_HOURS)}"
    )
    url = config.PUBLIC_URL
    return text, _kb(
        [_url("💰 Тарифы и цены", f"{url}/tariffs")],
        [_url("📄 Соглашение", f"{url}/terms"), _url("🔒 Конфиденциальность", f"{url}/privacy")],
        [_cb("✍️ Написать в поддержку", "sup:new")],
        [_cb("‹ Помощь", "m:help"), *_home_row()],
    )
