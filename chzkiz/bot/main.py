"""Бот только вход и уведомления, вся работа на веб-странице (см. PLAN.md)."""
from __future__ import annotations

import asyncio
import logging
import re

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand, CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

from .. import config, crypto, db, wb_api
from . import screens

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("chzkiz.bot")

COMMANDS = [
    ("start", "начать, подключить WB по API"),
    ("out", "сделать сверку КИЗ"),
    ("instrukciya", "пошаговая инструкция"),
    ("help", "что умеет бот"),
]

HINT_TEXT = "Не понял. /out - сделать сверку, /instrukciya - инструкция, /start - подключить токен WB."
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
        "В боте наберите /start и нажмите Подключить WB по API.\n"
        f"Токен создайте в кабинете WB: {screens.TOKEN_PATH}.\n"
        f"{screens.TOKEN_SAFE}\n"
        "Пришлите токен боту, сообщение с ним бот сразу удалит.\n"
        "Для режима с файлами токен не обязателен.",
    ),
    (
        "Ссылка на панель",
        "Наберите /out и нажмите Сделать сверку, бот пришлет кнопку со ссылкой.\n"
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
    text = f"Шаг {i + 1} из {len(INSTRUKCIYA_STEPS)}. {title}\n\n{body}"
    nav = []
    if i > 0:
        nav.append(InlineKeyboardButton(text="Назад", callback_data=f"ins:{i - 1}"))
    if i < last:
        nav.append(InlineKeyboardButton(text="Дальше", callback_data=f"ins:{i + 1}"))
    rows = [nav]
    if i == last:
        rows.append(
            [InlineKeyboardButton(text="Полная инструкция с видео", url=f"{config.PUBLIC_URL}/instrukciya")]
        )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


class WaitKey(StatesGroup):
    key = State()


def _login_link(tg_id: int) -> str:
    with db.connect() as conn:
        user = db.get_or_create_user(conn, tg_id)
        token = db.create_login_token(conn, user["id"])
    return f"{config.PUBLIC_URL}/l/{token}"


def _has_wb_key(tg_id: int) -> bool:
    with db.connect() as conn:
        user = db.get_or_create_user(conn, tg_id)
        return bool(user["wb_key_enc"])


async def _send_api_new(msg: Message, state: FSMContext) -> None:
    photo, caption, kb = screens.api_new_screen()
    await state.set_state(WaitKey.key)
    await msg.answer_photo(FSInputFile(photo), caption=caption, reply_markup=kb)


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())

    @dp.message(CommandStart())
    async def on_start(message: Message, state: FSMContext) -> None:
        await state.clear()
        with db.connect() as conn:
            db.get_or_create_user(conn, message.from_user.id)
        text, kb = screens.start_screen()
        await message.answer(text, reply_markup=kb)

    @dp.message(WaitKey.key, F.text, ~F.text.startswith("/"))
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
            await message.answer(
                "Этот токен не сработал на запросе заказов WB. Проверьте и пришлите еще раз."
            )
            return
        with db.connect() as conn:
            user = db.get_or_create_user(conn, message.from_user.id)
            db.set_wb_key(conn, user["id"], crypto.encrypt_str(raw_key))
        await state.clear()
        text, kb = screens.api_ok_screen()
        await message.answer(text, reply_markup=kb)

    @dp.message(Command("out"))
    async def on_out(message: Message, state: FSMContext) -> None:
        await state.clear()
        text, kb = screens.out_screen()
        await message.answer(text, reply_markup=kb)

    @dp.callback_query(F.data.startswith("sv:"))
    async def on_sverka(query: CallbackQuery, state: FSMContext) -> None:
        await query.answer()
        msg = query.message
        if msg is None:
            return
        act = query.data.split(":", 1)[1]
        uid = query.from_user.id
        if act == "menu":
            await state.clear()
            text, kb = screens.out_screen()
            await msg.answer(text, reply_markup=kb)
        elif act == "go":
            await state.clear()
            text, kb = screens.go_screen(_login_link(uid))
            await msg.answer(text, reply_markup=kb)
        elif act == "ins":
            await state.clear()
            text, kb = screens.ins_screen()
            await msg.answer(text, reply_markup=kb)
        elif act == "api":
            if _has_wb_key(uid):
                text, kb = screens.api_have_screen()
                await msg.answer(text, reply_markup=kb)
            else:
                await _send_api_new(msg, state)
        elif act == "api_new":
            await _send_api_new(msg, state)
        elif act == "files":
            await state.clear()
            for photo, caption, kb in screens.files_screens():
                await msg.answer_photo(FSInputFile(photo), caption=caption, reply_markup=kb)
        elif act == "fl":
            await state.clear()
            text, kb = screens.fl_screen()
            await msg.answer(text, reply_markup=kb)
        elif act == "nofl":
            await state.clear()
            await msg.answer(screens.nofl_text())
            text, kb = screens.go_screen(_login_link(uid))
            await msg.answer(text, reply_markup=kb)

    @dp.message(Command("instrukciya"))
    async def on_instrukciya(message: Message, state: FSMContext) -> None:
        await state.clear()
        text, kb = instrukciya_page(0)
        await message.answer(text, reply_markup=kb)

    @dp.callback_query(F.data.startswith("ins:"))
    async def on_instrukciya_page(query: CallbackQuery) -> None:
        try:
            i = int(query.data.split(":", 1)[1])
        except ValueError:
            i = 0
        text, kb = instrukciya_page(i)
        if query.message is not None:
            try:
                await query.message.edit_text(text, reply_markup=kb)
            except Exception:
                pass
        await query.answer()

    @dp.message(Command("help"))
    async def on_help(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            "/start - начать, подключить WB по API\n/out - сделать сверку КИЗ\n"
            "/instrukciya - пошаговая инструкция\n"
            "Кнопка Видео по сверке есть в /out, там короткие ролики по шагам."
        )

    # Перезапуск службы между /start и присланным токеном теряет FSM-состояние (MemoryStorage
    # не переживает рестарт) - без этого перехватчика токен повиснет в чате без ответа и без
    # удаления. Ловим в любом состоянии, регистрируя последним, чтобы не перебивать /out, /help.
    @dp.message(F.text, ~F.text.startswith("/"))
    async def on_key_anywhere(message: Message, state: FSMContext) -> None:
        if _looks_like_wb_key(message.text or ""):
            await on_key(message, state)
        else:
            await message.answer(HINT_TEXT)

    # Последним: незнакомая команда, файл, фото, стикер. Раньше бот молчал.
    @dp.message()
    async def on_other(message: Message) -> None:
        await message.answer(HINT_TEXT)

    return dp


def _looks_like_wb_key(text: str) -> bool:
    t = text.strip()
    return len(t) > 20 and t.startswith("eyJ") and t.count(".") == 2 and bool(_JWT_CHARS_RE.fullmatch(t))


async def _check_wb_key(raw_key: str) -> bool:
    client = wb_api.WbClient(raw_key)
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


async def main() -> None:
    config.check_mode()
    bot = Bot(token=config.BOT_TOKEN)
    dp = build_dispatcher()
    await setup_bot(bot)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
