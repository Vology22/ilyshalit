"""Поддержка в боте: FAQ, обращение к живому человеку и ответы операторов.

Операторы сидят в закрытой супергруппе с темами (привязывается командой /bind в группе): на каждого пользователя
своя тема, сверху карточка с контекстом. Сообщение пользователя копируется в его тему, ответ
оператора в теме уходит пользователю от имени бота.

У пользователя экран чата: последние сообщения обращения и кнопка закрыть. Пока обращение открыто,
кнопка чата есть в главном меню. Ответ оператора, пришедший вне чата, приходит уведомлением."""
from __future__ import annotations

import logging
from datetime import datetime
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReactionTypeEmoji

from .. import config, db, staff, support, wb_api
from . import faq
from .screens import HOME, _cb, _home_row, _kb, _rub, _url, ops_word

log = logging.getLogger("chzkiz.support")


class SupportWrite(StatesGroup):
    text = State()  # пользователь в чате поддержки: все его сообщения уходят операторам


# --- экраны --------------------------------------------------------------------------------

def help_screen() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "💬 <b>Помощь</b>\n\n"
        "Выберите вопрос. Не нашли ответ, напишите нам: ответит живой человек."
    )
    rows = [[_cb(title, f"faq:{key}")] for key, title, _ in faq.FAQ]
    rows.append([_cb("✍️ Написать в поддержку", "sup:new")])
    rows.append([_cb("📄 Тарифы и документы", "m:docs")])
    rows.append(_home_row())
    return text, _kb(*rows)


def faq_screen(key: str) -> tuple[str, InlineKeyboardMarkup]:
    title, body = faq.BY_KEY.get(key, faq.BY_KEY[faq.FAQ[0][0]])
    return f"<b>{title}</b>\n\n{body}", _kb(
        [_cb("👍 Помогло", "faq_ok"), _cb("✍️ Написать человеку", "sup:new")],
        [_cb("‹ Все вопросы", "m:help")],
        _home_row(),
    )


def write_screen() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "✍️ <b>Напишите вопрос</b>\n\n"
        "Опишите, что случилось, одним или несколькими сообщениями. Можно приложить скриншот или файл.\n\n"
        f"Ответит оператор прямо в этом чате. Время работы: {escape(config.SUPPORT_HOURS)}."
    )
    return text, _kb([_cb("✖️ Отмена", "m:help")])


KIND_WORDS = {"photo": "фото", "document": "файл", "video": "видео", "voice": "голосовое", "sticker": "стикер",
              "audio": "аудио", "animation": "гифка", "video_note": "кружок"}


def _line(text: str) -> str:
    if text.startswith("[") and text.endswith("]"):
        return "📎 " + KIND_WORDS.get(text[1:-1], "вложение")
    text = text if len(text) <= 300 else text[:300] + "..."
    return escape(text)


def chat_screen(history: list) -> tuple[str, InlineKeyboardMarkup]:
    """Открытое обращение: последние сообщения и подсказка писать прямо сюда."""
    lines = ["💬 <b>Чат с поддержкой</b>\n"]
    for m in history:
        who = "👤 <b>Вы</b>" if m["direction"] == "in" else "🎧 <b>Поддержка</b>"
        lines.append(f"{who}: {_line(m['text'])}")
    if not history:
        lines.append("Сообщений пока нет.")
    lines.append(
        "\nПишите сюда: текст, скриншот или файл, все уйдет оператору. "
        f"Время работы: {escape(config.SUPPORT_HOURS)}."
    )
    return "\n".join(lines), _kb([_cb("✅ Вопрос решен", "sup:done")], _home_row())


def notify_kb() -> InlineKeyboardMarkup:
    return _kb([_cb("💬 Открыть чат", "sup:chat")], [_cb("✅ Вопрос решен", "sup:done")])


def contacts_screen() -> tuple[str, InlineKeyboardMarkup]:
    """Группа операторов не подключена: даем прямые контакты."""
    lines = []
    if config.SUPPORT_TG:
        lines.append(f"Telegram: @{escape(config.SUPPORT_TG)}")
    if config.SUPPORT_EMAIL:
        lines.append(f"Email: {escape(config.SUPPORT_EMAIL)}")
    text = (
        "✍️ <b>Связаться с поддержкой</b>\n\n"
        + ("\n".join(lines) or "Чат с поддержкой временно недоступен, попробуйте чуть позже.")
        + f"\n\nВремя работы: {escape(config.SUPPORT_HOURS)}"
    )
    rows = [[_url("✍️ Написать", f"https://t.me/{config.SUPPORT_TG}")]] if config.SUPPORT_TG else []
    rows.append([_cb("‹ Все вопросы", "m:help")])
    rows.append(_home_row())
    return text, _kb(*rows)


def sent_screen() -> tuple[str, InlineKeyboardMarkup]:
    return (
        "✅ <b>Сообщение передано в поддержку</b>\n\n"
        "Ответим здесь же. Можете дописать подробности или прислать скриншот, они тоже дойдут.",
        _kb([_cb("✅ Вопрос решен", "sup:done")], _home_row()),
    )


def closed_screen(by_operator: bool) -> tuple[str, InlineKeyboardMarkup]:
    head = "🔔 <b>Оператор закрыл обращение</b>" if by_operator else "✅ <b>Обращение закрыто, спасибо!</b>"
    return (
        f"{head}\n\nПереписка удалена из бота. Если вопрос остался или появится новый, напишите через Помощь.",
        _kb([_cb("💬 Помощь", "m:help")], _home_row()),
    )


def _reply_kb() -> InlineKeyboardMarkup:
    return _kb([_cb("✅ Вопрос решен", "sup:done")], _home_row())


def _card(user_row, tg_user, ctx: dict) -> str:
    name = escape(tg_user.full_name) if tg_user else "-"
    uname = f" @{escape(tg_user.username)}" if tg_user and tg_user.username else ""
    u = user_row
    lines = [
        "🆕 <b>Новое обращение</b>",
        f"👤 {name}{uname} · tg <code>{u['tg_id']}</code> · №{u['id']}",
        f"🏷 {config.CATEGORIES.get(u['category'], 'категория не выбрана')}",
        f"💳 Баланс: {ops_word(u['balance'])} · оплат: {ctx['payments']} на {_rub(ctx['paid_kop'])}",
        f"🔑 Токен WB: {'есть' if u['wb_key_enc'] else 'нет'}",
    ]
    run = ctx["run"]
    if run:
        when = datetime.fromtimestamp(run["created_at"], wb_api.MSK).strftime("%d.%m %H:%M")
        lines.append(f"🧾 Последняя сверка: {when}, ИНН {escape(run['inn'] or '-')}, статус {escape(run['status'])}")
    for e in ctx["errors"]:
        lines.append(f"⚠️ {escape(e[:200])}")
    lines.append("\nОтвечайте сообщением в этой теме, /close - закрыть обращение.")
    return "\n".join(lines)


def _close_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Закрыть обращение", callback_data=f"sup:close:{user_id}")
    ]])


# --- пользователь -> операторы -------------------------------------------------------------

REMIND_AFTER_MIN = 30
NO_CAPTION = {"sticker", "video_note", "dice", "location", "venue", "contact", "poll"}


def chat_id() -> int:
    with db.connect() as conn:
        return staff.support_chat_id(conn)


def enabled() -> bool:
    return bool(chat_id())


def has_open_thread(tg_id: int) -> bool:
    if not enabled():
        return False
    with db.connect() as conn:
        user = db.get_or_create_user(conn, tg_id)
        return support.is_open(conn, user["id"])


async def _new_topic(bot: Bot, user_row, tg_user) -> int:
    name = (tg_user.full_name if tg_user else "") or f"Пользователь {user_row['tg_id']}"
    topic = await bot.create_forum_topic(chat_id(), name=f"{name[:100]} · №{user_row['id']}")
    with db.connect() as conn:
        support.set_topic(conn, user_row["id"], topic.message_thread_id)
    return topic.message_thread_id


async def submit(message: Message, bot: Bot) -> bool:
    """Сообщение пользователя - в его тему. True - доставлено операторам."""
    tg_user = message.from_user
    with db.connect() as conn:
        user = db.get_or_create_user(conn, tg_user.id)
        thread = support.get_thread(conn, user["id"])
        is_new = support.open_thread(conn, user["id"])
        ctx = support.user_context(conn, user["id"]) if is_new else None
    topic = thread["topic_id"] if thread and thread["topic_id"] else None
    group = chat_id()
    for attempt in range(2):
        try:
            if topic is None:
                topic = await _new_topic(bot, user, tg_user)
                is_new = True
                if ctx is None:
                    with db.connect() as conn:
                        ctx = support.user_context(conn, user["id"])
            if is_new:
                await bot.send_message(group, _card(user, tg_user, ctx), message_thread_id=topic,
                                       reply_markup=_close_kb(user["id"]))
                is_new = False
            await message.copy_to(group, message_thread_id=topic)
            break
        except TelegramBadRequest as e:
            # тему удалили в группе - заводим новую и пробуем еще раз
            if attempt == 0 and "thread" in str(e).lower():
                topic = None
                continue
            log.warning("support submit failed for user %s: %s", user["id"], e)
            return False
    with db.connect() as conn:
        support.log(conn, user["id"], "in", message.text or message.caption or f"[{message.content_type}]")
    return True


# --- операторы -> пользователь -------------------------------------------------------------

def _in_support_chat(obj: Message | CallbackQuery) -> bool:
    chat = obj.chat if isinstance(obj, Message) else (obj.message.chat if obj.message else None)
    if chat is None or chat.type not in ("group", "supergroup"):
        return False
    group = chat_id()
    return bool(group) and chat.id == group


def _user_state(storage: BaseStorage | None, bot: Bot, tg_id: int) -> FSMContext | None:
    """Состояние пользователя в его личном чате с ботом, из обработчика в группе операторов."""
    if storage is None:
        return None
    return FSMContext(storage=storage, key=StorageKey(bot_id=bot.id, chat_id=tg_id, user_id=tg_id))


async def _close(
    bot: Bot, user_id: int, by_operator: bool, topic: int | None = None, storage: BaseStorage | None = None
) -> bool:
    with db.connect() as conn:
        closed = support.close_thread(conn, user_id)
        user = db.get_user(conn, user_id)
        thread = support.get_thread(conn, user_id)
    if not closed or user is None:
        return False
    topic = topic or (thread["topic_id"] if thread else None)
    if by_operator:
        state = _user_state(storage, bot, user["tg_id"])
        if state and await state.get_state() == SupportWrite.text.state:
            await state.clear()  # следующее сообщение уже не дописка к закрытому обращению
        text, kb = closed_screen(True)
        try:
            await bot.send_message(user["tg_id"], text, reply_markup=kb)
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
    if topic:
        who = "оператором" if by_operator else "пользователем"
        try:
            await bot.send_message(chat_id(), f"✅ Обращение закрыто {who}.", message_thread_id=topic)
        except TelegramBadRequest:
            pass
    return True


def build_group_router() -> Router:
    """Сообщения в группе операторов. Подключается до роутера личных чатов."""
    r = Router()

    @r.message(Command("chatid"), F.chat.type.in_({"group", "supergroup"}))
    async def chat_id(message: Message) -> None:
        await message.reply(
            f"ID этого чата: <code>{message.chat.id}</code>\n"
            f"Темы: {'включены' if message.chat.is_forum else 'выключены, включите в настройках группы'}"
        )

    @r.message(Command("bind"), F.chat.type == "supergroup")
    async def bind(message: Message, bot: Bot) -> None:
        with db.connect() as conn:
            allowed = staff.is_admin(conn, message.from_user.id)
        if not allowed:
            await message.reply("Привязать группу может только админ бота.")
            return
        if not message.chat.is_forum:
            await message.reply("Включите в настройках группы Темы и повторите /bind.")
            return
        me = await bot.get_chat_member(message.chat.id, bot.id)
        if getattr(me, "status", "") != "administrator" or not getattr(me, "can_manage_topics", False):
            await message.reply("Сделайте бота администратором с правом управлять темами и повторите /bind.")
            return
        with db.connect() as conn:
            staff.set_setting(conn, "support_chat_id", str(message.chat.id))
        await message.reply(
            "✅ Группа подключена к поддержке. Новые обращения будут приходить сюда, на каждого пользователя "
            "своя тема. Отвечайте сообщением в теме, /close закрывает обращение."
        )

    @r.message(Command("close"), _in_support_chat)
    async def op_close(message: Message, bot: Bot, fsm_storage: BaseStorage | None = None) -> None:
        if not message.message_thread_id:
            return
        with db.connect() as conn:
            user_id = support.user_by_topic(conn, message.message_thread_id)
        if user_id is None or not await _close(bot, user_id, True, message.message_thread_id, fsm_storage):
            await message.reply("Открытого обращения в этой теме нет.")

    @r.callback_query(F.data.startswith("sup:close:"), _in_support_chat)
    async def op_close_button(query: CallbackQuery, bot: Bot, fsm_storage: BaseStorage | None = None) -> None:
        try:
            user_id = int(query.data.rsplit(":", 1)[1])
        except ValueError:
            await query.answer()
            return
        done = await _close(bot, user_id, True, storage=fsm_storage)
        await query.answer("Обращение закрыто" if done else "Уже закрыто")

    @r.message(_in_support_chat)
    async def op_reply(message: Message, bot: Bot, fsm_storage: BaseStorage | None = None) -> None:
        if not message.message_thread_id or (message.from_user and message.from_user.is_bot):
            return
        if message.text and message.text.startswith("/"):
            return
        with db.connect() as conn:
            user_id = support.user_by_topic(conn, message.message_thread_id)
            user = db.get_user(conn, user_id) if user_id else None
        if user is None:
            return  # служебная тема или общий чат операторов
        with db.connect() as conn:
            allowed = message.from_user is not None and staff.is_staff(conn, message.from_user.id)
            if allowed:
                staff.set_name(conn, message.from_user.id, message.from_user.full_name)
        if not allowed:
            await message.reply("⚠️ Не отправлено: вас нет в команде поддержки. Админ добавит вас в /admin → Команда.")
            return
        # пользователь в чате - обычное сообщение; ушел в меню - уведомление с кнопкой вернуться в чат
        state = _user_state(fsm_storage, bot, user["tg_id"])
        in_chat = bool(state) and await state.get_state() == SupportWrite.text.state
        head = "🎧 <b>Поддержка</b>" if in_chat else "🔔 <b>Новый ответ поддержки</b>"
        kb = _reply_kb() if in_chat else notify_kb()
        try:
            if message.text:
                await bot.send_message(user["tg_id"], f"{head}\n\n{escape(message.text)}", reply_markup=kb)
            elif message.content_type in NO_CAPTION:
                await bot.send_message(user["tg_id"], head)
                await message.copy_to(user["tg_id"], reply_markup=kb)
            else:
                caption = head + (f"\n\n{escape(message.caption)}" if message.caption else "")
                await message.copy_to(user["tg_id"], caption=caption, reply_markup=kb)
        except TelegramForbiddenError:
            await message.reply("⚠️ Не доставлено: пользователь заблокировал бота.")
            return
        except TelegramBadRequest as e:
            await message.reply(f"⚠️ Не доставлено: {escape(str(e))[:200]}")
            return
        with db.connect() as conn:
            support.open_thread(conn, user["id"])  # ответ оператора держит обращение открытым
            support.log(conn, user["id"], "out", message.text or message.caption or f"[{message.content_type}]")
            if in_chat:
                support.mark_seen(conn, user["id"])
        try:
            await message.react([ReactionTypeEmoji(emoji="👍")])
        except TelegramBadRequest:
            pass

    return r


# --- кнопки и сообщения в личном чате ----------------------------------------------------------

def register_private(r: Router, show) -> None:
    """Обработчики в личном чате; show - функция показа экрана из main."""

    @r.callback_query(F.data.startswith("faq:"))
    async def on_faq(query: CallbackQuery) -> None:
        await query.answer()
        await show(query, *faq_screen(query.data.split(":", 1)[1]))

    @r.callback_query(F.data == "faq_ok")
    async def on_faq_ok(query: CallbackQuery) -> None:
        await query.answer("Рады помочь!")

    @r.callback_query(F.data.in_({"sup:new", "sup:chat"}))
    async def on_new(query: CallbackQuery, state: FSMContext) -> None:
        await query.answer()
        if not enabled():
            await show(query, *contacts_screen())
            return
        await state.set_state(SupportWrite.text)
        with db.connect() as conn:
            user = db.get_or_create_user(conn, query.from_user.id)
            if not support.is_open(conn, user["id"]):
                await show(query, *write_screen())
                return
            support.mark_seen(conn, user["id"])
            history = support.history(conn, user["id"])
        await show(query, *chat_screen(history))

    @r.callback_query(F.data == "sup:done")
    async def on_done(query: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        await state.clear()
        with db.connect() as conn:
            user = db.get_or_create_user(conn, query.from_user.id)
        await _close(bot, user["id"], False)
        await query.answer()
        await show(query, *closed_screen(False))

    @r.message(SupportWrite.text, ~F.text.startswith("/"))
    async def on_write(message: Message, bot: Bot) -> None:
        # остаемся в чате: следующие сообщения тоже уходят оператору
        await deliver(message, bot, confirm=not has_open_thread(message.from_user.id))


async def deliver(message: Message, bot: Bot, confirm: bool) -> None:
    """Передать сообщение операторам и ответить пользователю: экраном или реакцией."""
    if not await submit(message, bot):
        await message.answer(
            "⚠️ Не получилось передать сообщение. Попробуйте еще раз через минуту.",
            reply_markup=_kb(_home_row()),
        )
        return
    if confirm:
        text, kb = sent_screen()
        await message.answer(text, reply_markup=kb)
    else:
        try:
            await message.react([ReactionTypeEmoji(emoji="👍")])  # дописка дошла, без лишнего сообщения
        except TelegramBadRequest:
            pass


async def remind(bot: Bot) -> int:
    """Напомнить в темах, где пользователь ждет ответа дольше REMIND_AFTER_MIN. Вызывается по таймеру."""
    group = chat_id()
    if not group:
        return 0
    with db.connect() as conn:
        due = support.remind_due(conn, REMIND_AFTER_MIN)
    for _, topic, waited in due:
        try:
            await bot.send_message(group, f"⏰ Пользователь ждет ответа {waited} мин.", message_thread_id=topic)
        except TelegramBadRequest as e:
            log.warning("support remind failed in topic %s: %s", topic, e)
    return len(due)


def menu_state(tg_id: int) -> str:
    """Для кнопки чата в главном меню: '', open или unread."""
    if not enabled():
        return ""
    with db.connect() as conn:
        user = db.get_or_create_user(conn, tg_id)
        return support.chat_state(conn, user["id"])


__all__ = ["SupportWrite", "build_group_router", "chat_screen", "deliver", "enabled", "has_open_thread",
           "help_screen", "menu_state", "register_private", "remind", "HOME"]
