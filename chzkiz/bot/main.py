"""Бот: вход в панель, токен WB, баланс, рефералы и помощь. Вся работа со сверкой на веб-странице.

Навигация одним сообщением: нажатие кнопки меню редактирует текущий экран."""
from __future__ import annotations

import asyncio
import logging
import re

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand, CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Message,
)

from .. import billing, config, crypto, db, staff, wb_api
from . import admin, screens, support

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("chzkiz.bot")

COMMANDS = [
    ("start", "главное меню"),
    ("out", "сделать сверку КИЗ"),
    ("cabinet", "баланс и тарифы"),
    ("info", "помощь, тарифы, документы"),
    ("instrukciya", "пошаговая инструкция"),
    ("help", "что умеет бот"),
]

HINT_TEXT = "Не понял. Откройте меню кнопкой ниже или командой /start."
_JWT_CHARS_RE = re.compile(r"[A-Za-z0-9_.-]+")


INSTRUKCIYA_STEPS = [
    (
        "Подготовка компьютера",
        "Нужны КриптоПро CSP 5 и КриптоПро ЭЦП Browser plug-in.\n"
        "Установите расширение для Chrome или Яндекс Браузера и проверьте, что оно включено.\n"
        "Вставьте флешку с подписью для Честного знака.\n"
        "После установки перезапустите браузер.",
    ),
    (
        "Токен WB API",
        "В боте наберите /start и нажмите Токен WB.\n"
        f"Токен создайте в кабинете WB: {screens.TOKEN_PATH}.\n"
        f"{screens.TOKEN_SAFE}\n"
        "Пришлите токен боту, сообщение с ним бот сразу удалит.\n"
        "Для режима с файлами токен не обязателен.",
    ),
    (
        "Ссылка на панель",
        "Наберите /out и нажмите Открыть панель сверки, бот пришлет кнопку со ссылкой.\n"
        "Ссылка одноразовая и живет 15 минут.\n"
        "После входа панель держит вас 30 дней.",
    ),
    (
        "Загрузка данных",
        "В панели выберите источник: через API WB или через файл Excel.\n"
        "Для файлов загрузите архив сборочных заданий и финотчет за тот же период, можно несколько файлов и zip.\n"
        f"Архив в кабинете WB: {screens.FILES_PATH}.\n"
        "Выберите товарную группу и нажмите Продолжить.",
    ),
    (
        "Вход в Честный знак",
        "Выберите сертификат подписи из списка и нажмите Войти подписью.\n"
        "Закрытый ключ с вашего компьютера никуда не уходит, подпись делает браузер.",
    ),
    (
        "Таблица сверки",
        "Панель покажет цену и дату по каждому коду и четыре группы:\n"
        "Вывести, Вернуть, Уже в порядке, Ошибка.\n"
        "Коды из группы Ошибка в выводе и возврате не участвуют. Исправьте причину и сделайте сверку заново.",
    ),
    (
        "Вывод и возврат",
        "Нажмите Вывести и вернуть: панель отправит оба документа подряд, сначала вывод, потом возврат. Код ФИАС склада панель подставит сама из ваших прошлых документов в Честном знаке, а если не найдет, попросит ввести один раз.\n"
        "Панель спросит подтверждение, браузер подпишет документ, и он уйдет в Честный знак.\n"
        "Нужен один документ: ниже есть кнопки Только вывести и Только вернуть. В оборот возвращаются коды, которые покупатель вернул и которые уже выведены. "
        "Возврат уходит как неоплаченный, первичный документ не нужен.",
    ),
    (
        "Результат",
        "Ответ Честного знака обычно приходит меньше чем за минуту, но может занять до 30 минут.\n"
        "Потом нажмите Скачать отчет.\n"
        "Проверить итог можно в Честном знаке: Документы, Исходящие, Обработан успешно.",
    ),
]


def instrukciya_page(i: int) -> tuple[str, InlineKeyboardMarkup]:
    last = len(INSTRUKCIYA_STEPS) - 1
    i = max(0, min(i, last))
    title, body = INSTRUKCIYA_STEPS[i]
    text = f"Шаг {i + 1} из {len(INSTRUKCIYA_STEPS)}. <b>{title}</b>\n\n{body}"
    nav = []
    if i > 0:
        nav.append(InlineKeyboardButton(text="Назад", callback_data=f"ins:{i - 1}"))
    if i < last:
        nav.append(InlineKeyboardButton(text="Дальше", callback_data=f"ins:{i + 1}"))
    rows = [nav]
    if i == last:
        rows.append(
            [InlineKeyboardButton(text="📖 Полная инструкция с видео", url=f"{config.PUBLIC_URL}/instrukciya")]
        )
    rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data=screens.HOME)])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


class WaitKey(StatesGroup):
    key = State()


def _login_link(tg_id: int, next_page: str = "") -> str:
    with db.connect() as conn:
        user = db.get_or_create_user(conn, tg_id)
        token = db.create_login_token(conn, user["id"])
    tail = f"?next={next_page}" if next_page else ""
    return f"{config.PUBLIC_URL}/l/{token}{tail}"


def _user(tg_id: int):
    with db.connect() as conn:
        return db.get_or_create_user(conn, tg_id)


def _home(tg_id: int) -> tuple[str, InlineKeyboardMarkup]:
    u = _user(tg_id)
    with db.connect() as conn:
        team = staff.is_staff(conn, tg_id)
        sub = billing.subscription(conn, u["id"])
    return screens.home_screen(u["balance"], bool(u["wb_key_enc"]), u["category"], team, sub,
                               support.menu_state(tg_id))


def _sverka(tg_id: int) -> tuple[str, InlineKeyboardMarkup]:
    u = _user(tg_id)
    with db.connect() as conn:
        sub = billing.subscription(conn, u["id"])
    return screens.sverka_screen(bool(u["wb_key_enc"]), u["balance"], sub)


def _balance(tg_id: int) -> tuple[str, InlineKeyboardMarkup]:
    link = _login_link(tg_id, "/cabinet")
    with db.connect() as conn:
        u = db.get_or_create_user(conn, tg_id)
        tariffs = [dict(t) for t in billing.tariffs(conn)]
        sub = billing.subscription(conn, u["id"])
    return screens.balance_screen(u["balance"], tariffs, link, sub)


def _ref(tg_id: int) -> tuple[str, InlineKeyboardMarkup]:
    with db.connect() as conn:
        u = db.get_or_create_user(conn, tg_id)
        code = billing.ensure_ref_code(conn, u["id"])
        stats = billing.ref_stats(conn, u["id"])
    return screens.ref_screen(u["category"], billing.ref_link(code), stats)


def _menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 Меню", callback_data=screens.HOME)]])


async def _show(target: Message | CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    """Экран на месте текущего сообщения бота; команда или фото - новым сообщением."""
    if isinstance(target, CallbackQuery):
        msg = target.message
        if msg is None:
            return
        if msg.text is not None:
            try:
                await msg.edit_text(text, reply_markup=kb)
                return
            except TelegramBadRequest as e:
                if "not modified" in str(e):
                    return
        else:
            # экран был фото (инструкция) - текст в подпись не влезет, заменяем сообщение
            try:
                await msg.delete()
            except TelegramBadRequest:
                pass
        await msg.answer(text, reply_markup=kb)
        return
    await target.answer(text, reply_markup=kb)


async def _send_api_new(target: Message | CallbackQuery, state: FSMContext) -> None:
    photo, caption, kb = screens.api_new_screen()
    await state.set_state(WaitKey.key)
    msg = target.message if isinstance(target, CallbackQuery) else target
    if msg is None:
        return
    if isinstance(target, CallbackQuery):
        try:
            await msg.delete()  # меню уступает место инструкции, чат не копит старые экраны
        except TelegramBadRequest:
            pass
    await msg.answer_photo(FSInputFile(photo), caption=caption, reply_markup=kb)


async def _send_files(msg: Message) -> None:
    media = [InputMediaPhoto(media=FSInputFile(p), caption=c) for p, c in screens.files_screens()]
    await msg.answer_media_group(media)
    text, kb = screens.files_done_screen()
    await msg.answer(text, reply_markup=kb)


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(support.build_group_router())
    dp.include_router(admin.build_router(_show))
    r = Router()
    # личные чаты: в группе операторов бот не отвечает подсказками на их переписку
    r.message.filter(F.chat.type == "private")

    @r.message(CommandStart())
    async def on_start(message: Message, state: FSMContext, command: CommandObject) -> None:
        await state.clear()
        payload = (command.args or "").strip()
        # t.me/бот?start=ref_код - пригласивший закрепляется только при первой регистрации
        ref_code = payload[4:] if payload.startswith("ref_") else ""
        with db.connect() as conn:
            user = db.get_or_create_user(conn, message.from_user.id, ref_code)
        if not user["category"]:
            text, kb = screens.welcome_screen(message.from_user.first_name or "")
        else:
            text, kb = _home(message.from_user.id)
        await message.answer(text, reply_markup=kb)

    @r.callback_query(F.data.startswith("cat:"))
    async def on_category(query: CallbackQuery) -> None:
        category = query.data.split(":", 1)[1]
        with db.connect() as conn:
            user = db.get_or_create_user(conn, query.from_user.id)
            changed = billing.set_category(conn, user["id"], category)
            user = db.get_user(conn, user["id"])
        label = config.CATEGORIES.get(user["category"], "")
        if changed:
            await query.answer(f"Готово: {label}")
        else:
            await query.answer(f"Категория уже выбрана: {label}. Сменить можно через поддержку", show_alert=True)
        await _show(query, *_home(query.from_user.id))

    @r.message(WaitKey.key, F.text, ~F.text.startswith("/"))
    async def on_key(message: Message, state: FSMContext) -> None:
        raw_key = message.text.strip()
        ok = False
        try:
            if raw_key.isascii() and " " not in raw_key:
                ok = await _check_wb_key(raw_key)
        except Exception:
            ok = False
        finally:
            try:
                await message.delete()
            except Exception:
                pass
        if not ok:
            text, kb = screens.api_bad_screen()
            await message.answer(text, reply_markup=kb)
            return
        with db.connect() as conn:
            user = db.get_or_create_user(conn, message.from_user.id)
            db.set_wb_key(conn, user["id"], crypto.encrypt_str(raw_key))
        await state.clear()
        text, kb = screens.api_ok_screen()
        await message.answer(text, reply_markup=kb)

    @r.message(Command("out"))
    async def on_out(message: Message, state: FSMContext) -> None:
        await state.clear()
        await _show(message, *_sverka(message.from_user.id))

    @r.message(Command("cabinet"))
    async def on_cabinet(message: Message, state: FSMContext) -> None:
        await state.clear()
        await _show(message, *_balance(message.from_user.id))

    @r.message(Command("info"))
    async def on_info(message: Message, state: FSMContext) -> None:
        await state.clear()
        await _show(message, *screens.info_screen())

    @r.callback_query(F.data.startswith("m:"))
    async def on_menu(query: CallbackQuery, state: FSMContext) -> None:
        await query.answer()
        act = query.data.split(":", 1)[1]
        uid = query.from_user.id
        if act != "wb_new":
            await state.clear()  # ушли с экрана токена - текст больше не ловим как токен
        if act == "home":
            await _show(query, *_home(uid))
        elif act == "go":
            await _show(query, *_sverka(uid))
        elif act == "link":
            await _show(query, *screens.link_screen(_login_link(uid)))
        elif act == "phone":
            await _show(query, *screens.phone_screen(_login_link(uid)))
        elif act == "wb":
            if _user(uid)["wb_key_enc"]:
                await _show(query, *screens.wb_have_screen())
            else:
                await _send_api_new(query, state)
        elif act == "wb_new":
            await _send_api_new(query, state)
        elif act == "files":
            if query.message is not None:
                await _send_files(query.message)
        elif act == "bal":
            await _show(query, *_balance(uid))
        elif act == "ref":
            await _show(query, *_ref(uid))
        elif act == "help":
            await _show(query, *support.help_screen())
        elif act == "docs":
            await _show(query, *screens.info_screen())

    support.register_private(r, _show)

    @r.message(Command("instrukciya"))
    async def on_instrukciya(message: Message, state: FSMContext) -> None:
        await state.clear()
        await _show(message, *instrukciya_page(0))

    @r.callback_query(F.data.startswith("ins:"))
    async def on_instrukciya_page(query: CallbackQuery) -> None:
        try:
            i = int(query.data.split(":", 1)[1])
        except ValueError:
            i = 0
        await query.answer()
        await _show(query, *instrukciya_page(i))

    @r.message(Command("help"))
    async def on_help(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(HELP_TEXT, reply_markup=_menu_kb())

    # Перезапуск службы между /start и присланным токеном теряет FSM-состояние (MemoryStorage
    # не переживает рестарт) - без этого перехватчика токен повиснет в чате без ответа и без
    # удаления. Ловим в любом состоянии, регистрируя последним, чтобы не перебивать /out, /help.
    @r.message(F.text, ~F.text.startswith("/"))
    async def on_key_anywhere(message: Message, state: FSMContext, bot: Bot) -> None:
        if _looks_like_wb_key(message.text or ""):
            await on_key(message, state)
        elif support.has_open_thread(message.from_user.id):
            await support.deliver(message, bot, confirm=False)  # дописка к открытому обращению
        else:
            await message.answer(HINT_TEXT, reply_markup=_menu_kb())

    # Последним: незнакомая команда, файл, фото, стикер. Раньше бот молчал.
    @r.message()
    async def on_other(message: Message, bot: Bot) -> None:
        is_command = bool(message.text and message.text.startswith("/"))
        if not is_command and support.has_open_thread(message.from_user.id):
            await support.deliver(message, bot, confirm=False)  # скриншот или файл к обращению
        else:
            await message.answer(HINT_TEXT, reply_markup=_menu_kb())

    dp.include_router(r)
    return dp


HELP_TEXT = (
    "<b>Что умеет бот</b>\n\n"
    "/start - главное меню: сверка, токен WB, баланс, приглашения\n"
    "/out - сделать сверку КИЗ\n"
    "/cabinet - баланс, тарифы и личный кабинет\n"
    "/info - тарифы, документы и контакты поддержки\n"
    "/instrukciya - пошаговая инструкция\n\n"
    "Короткие видео по шагам: /out, кнопка Видео по сверке."
)


def _looks_like_wb_key(text: str) -> bool:
    t = text.strip()
    return len(t) > 20 and t.startswith("eyJ") and t.count(".") == 2 and bool(_JWT_CHARS_RE.fullmatch(t))


async def _check_wb_key(raw_key: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=15.0) as http_client:
            r = await http_client.get(
                wb_api.ORDERS_BASE + wb_api.ORDERS_PATH,
                params={"limit": 1, "next": 0},
                headers={"Authorization": raw_key},
            )
        return r.status_code == 200
    except httpx.HTTPError:
        return False


BOT_DESCRIPTION = (
    "Сверка КИЗ Wildberries FBS. Бот выводит из оборота проданные коды маркировки и возвращает "
    "в оборот возвращенные. Подпись остается у вас, документы подписываете в панели."
)
BOT_SHORT_DESCRIPTION = "Вывод и возврат КИЗ по продажам Wildberries FBS в Честном знаке"


async def setup_bot(bot: Bot) -> None:
    await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS])
    await bot.set_my_description(BOT_DESCRIPTION)
    await bot.set_my_short_description(BOT_SHORT_DESCRIPTION)


async def _remind_loop(bot: Bot) -> None:
    """Раз в 5 минут напоминает операторам об обращениях без ответа."""
    while True:
        await asyncio.sleep(300)
        try:
            await support.remind(bot)
        except Exception:  # noqa: BLE001 - таймер не должен останавливать бота
            log.exception("support reminders failed")


async def main() -> None:
    config.check_mode()
    session = AiohttpSession(proxy=config.TG_PROXY) if config.TG_PROXY else None
    bot = Bot(token=config.BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher()
    await setup_bot(bot)
    await admin.sync_staff_commands(bot)
    reminders = asyncio.create_task(_remind_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        reminders.cancel()


if __name__ == "__main__":
    asyncio.run(main())
