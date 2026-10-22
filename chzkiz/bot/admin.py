"""Админ-панель в боте (/admin): сводка, открытые обращения, команда поддержки.

Видна только команде. Админ добавляет и убирает людей, оператор видит сводку и обращения.
Человека выбирают системной кнопкой Telegram «выбрать пользователя», ID вводить не нужно."""
from __future__ import annotations

import logging
import time
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand, BotCommandScopeChat, CallbackQuery, InlineKeyboardMarkup, KeyboardButton,
    KeyboardButtonRequestUsers, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove,
)

from .. import billing, config, db, staff, support
from . import support as bot_support
from .screens import _cb, _home_row, _kb, _rub, _url

log = logging.getLogger("chzkiz.admin")

ADMIN_COMMANDS = [("admin", "админ-панель")]


class AddStaff(StatesGroup):
    who = State()


def _role(tg_id: int) -> str | None:
    with db.connect() as conn:
        return staff.role_of(conn, tg_id)


def _topic_url(group: int, topic: int) -> str:
    # ссылка на тему супергруппы: t.me/c/<id без -100>/<тема>
    return f"https://t.me/c/{str(group).removeprefix('-100')}/{topic}"


def _login_link(tg_id: int, next_page: str) -> str:
    with db.connect() as conn:
        user = db.get_or_create_user(conn, tg_id)
        token = db.create_login_token(conn, user["id"])
    return f"{config.PUBLIC_URL}/l/{token}?next={next_page}"


# --- экраны ---------------------------------------------------------------------------------

def home_screen(tg_id: int) -> tuple[str, InlineKeyboardMarkup]:
    role = _role(tg_id)
    with db.connect() as conn:
        s = billing.admin_summary(conn)
        new_day = conn.execute("SELECT COUNT(*) FROM users WHERE created_at >= ?", (time.time() - 86400,)).fetchone()[0]
        opened = support.open_count(conn)
        waiting = support.waiting_count(conn)
        group = staff.support_chat_id(conn)
    text = (
        "🛠 <b>Админ-панель</b>\n\n"
        f"👥 Пользователей: <b>{s['users']}</b> (+{new_day} за сутки)\n"
        f"💰 Выручка: сутки <b>{_rub(s['revenue']['day'])}</b>, 30 дней <b>{_rub(s['revenue']['month'])}</b>\n"
        f"🎫 Обращений открыто: <b>{opened}</b>, ждут ответа: <b>{waiting}</b>\n"
        f"💸 К выплате фулфилментам: {_rub(s['due_kop'])}\n\n"
        + ("💬 Группа поддержки подключена" if group
           else "⚠️ Группа поддержки не подключена: добавьте бота админом в группу с темами и напишите там /bind")
        + f"\n👤 Вы: {staff.ROLES.get(role or '', '-')}"
    )
    rows = [
        [_cb(f"🎫 Обращения ({opened})", "adm:tickets"), _cb("👥 Команда", "adm:team")],
    ]
    if role == "admin":
        rows.append([_url("🌐 Админка на сайте", _login_link(tg_id, "/admin"))])
    rows.append([_cb("🔄 Обновить", "adm:home")])
    rows.append(_home_row())
    return text, _kb(*rows)


def tickets_screen() -> tuple[str, InlineKeyboardMarkup]:
    with db.connect() as conn:
        threads = support.open_threads(conn, 15)
        group = staff.support_chat_id(conn)
    if not threads:
        return "🎫 <b>Обращения</b>\n\nОткрытых обращений нет 🎉", _kb([_cb("‹ Назад", "adm:home")])
    now = time.time()
    lines = ["🎫 <b>Открытые обращения</b>\n"]
    rows = []
    for t in threads:
        if t["waiting"]:
            state = f"⏳ ждет {int((now - t['last_user_at']) // 60)} мин"
        else:
            state = "✅ отвечено"
        lines.append(f"• №{t['user_id']} · tg <code>{t['tg_id']}</code> · {state}")
        if group and t["topic_id"]:
            rows.append([_url(f"№{t['user_id']} · открыть тему", _topic_url(group, t["topic_id"]))])
    rows.append([_cb("🔄 Обновить", "adm:tickets"), _cb("‹ Назад", "adm:home")])
    return "\n".join(lines), _kb(*rows)


def team_screen(viewer: int) -> tuple[str, InlineKeyboardMarkup]:
    with db.connect() as conn:
        team = staff.members(conn)
        can_manage = staff.is_admin(conn, viewer)
    lines = ["👥 <b>Команда поддержки</b>\n"]
    rows = []
    for m in team:
        icon = "👑" if m["role"] == "admin" else "🎧"
        name = escape(m["name"]) if m["name"] else "без имени"
        extra = " · владелец" if m["owner"] else ""
        you = " (вы)" if m["tg_id"] == viewer else ""
        lines.append(f"{icon} {name}{you} · <code>{m['tg_id']}</code> · {staff.ROLES[m['role']]}{extra}")
        if can_manage and not m["owner"] and m["tg_id"] != viewer:
            rows.append([_cb(f"❌ Убрать {m['name'] or m['tg_id']}", f"adm:rm:{m['tg_id']}")])
    lines.append("\n👑 админ управляет командой, 🎧 оператор отвечает пользователям.")
    if can_manage:
        rows.insert(0, [_cb("➕ Оператор", "adm:add:operator"), _cb("➕ Админ", "adm:add:admin")])
    rows.append([_cb("‹ Назад", "adm:home")])
    return "\n".join(lines), _kb(*rows)


def _pick_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👤 Выбрать человека", request_users=KeyboardButtonRequestUsers(
                request_id=1, user_is_bot=False, max_quantity=1, request_name=True, request_username=True))],
            [KeyboardButton(text="Отмена")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


async def set_staff_commands(bot: Bot, tg_id: int, on: bool) -> None:
    """Команда /admin в меню бота только у команды."""
    from .main import COMMANDS

    base = [BotCommand(command=c, description=d) for c, d in COMMANDS]
    extra = [BotCommand(command=c, description=d) for c, d in ADMIN_COMMANDS] if on else []
    try:
        await bot.set_my_commands(base + extra, scope=BotCommandScopeChat(chat_id=tg_id))
    except TelegramBadRequest:
        pass  # человек еще не запускал бота


async def sync_staff_commands(bot: Bot) -> None:
    with db.connect() as conn:
        team = staff.members(conn)
    for m in team:
        await set_staff_commands(bot, m["tg_id"], True)


# --- обработчики ------------------------------------------------------------------------------

def build_router(show) -> Router:
    r = Router()
    r.message.filter(F.chat.type == "private")

    @r.message(Command("admin"))
    async def admin_cmd(message: Message, state: FSMContext) -> None:
        if not _role(message.from_user.id):
            from .main import HINT_TEXT, _menu_kb

            await message.answer(HINT_TEXT, reply_markup=_menu_kb())
            return
        await state.clear()
        with db.connect() as conn:
            if staff.role_of(conn, message.from_user.id) and message.from_user.id not in config.ADMIN_TG_IDS:
                staff.set_name(conn, message.from_user.id, message.from_user.full_name)
        await show(message, *home_screen(message.from_user.id))

    @r.callback_query(F.data.startswith("adm:"))
    async def admin_cb(query: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        uid = query.from_user.id
        role = _role(uid)
        if not role:
            await query.answer("Нет доступа", show_alert=True)
            return
        parts = query.data.split(":")
        act = parts[1]
        admin_only = act in ("add", "rm", "rmok")
        if admin_only and role != "admin":
            await query.answer("Это может только админ", show_alert=True)
            return
        await query.answer()
        if act == "home":
            await show(query, *home_screen(uid))
        elif act == "tickets":
            await show(query, *tickets_screen())
        elif act == "team":
            await show(query, *team_screen(uid))
        elif act == "add":
            new_role = parts[2] if len(parts) > 2 and parts[2] in staff.ROLES else "operator"
            await state.set_state(AddStaff.who)
            await state.update_data(role=new_role)
            if query.message is not None:
                await query.message.answer(
                    f"➕ <b>Новый {staff.ROLES[new_role]}</b>\n\n"
                    "Нажмите кнопку ниже и выберите человека из своих чатов. Или пришлите его Telegram ID "
                    "числом (узнать можно у @userinfobot).",
                    reply_markup=_pick_keyboard(),
                )
        elif act == "rm" and len(parts) > 2:
            tg = int(parts[2])
            await show(query, f"Убрать <code>{tg}</code> из команды? Он потеряет доступ к группе поддержки.", _kb(
                [_cb("Да, убрать", f"adm:rmok:{tg}"), _cb("Отмена", "adm:team")]))
        elif act == "rmok" and len(parts) > 2:
            tg = int(parts[2])
            with db.connect() as conn:
                removed = staff.remove(conn, tg)
                group = staff.support_chat_id(conn)
            if removed:
                await set_staff_commands(bot, tg, False)
                if group:
                    try:  # исключить из группы: бан и сразу разбан, чтобы мог вернуться по новой ссылке
                        await bot.ban_chat_member(group, tg)
                        await bot.unban_chat_member(group, tg, only_if_banned=True)
                    except TelegramBadRequest as e:
                        log.warning("kick %s from support failed: %s", tg, e)
            await show(query, *team_screen(uid))

    @r.message(AddStaff.who)
    async def add_who(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.text == "Отмена":
            await state.clear()
            await message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())
            await show(message, *team_screen(message.from_user.id))
            return
        name = ""
        if message.users_shared and message.users_shared.users:
            picked = message.users_shared.users[0]
            tg_id = picked.user_id
            name = " ".join(x for x in (picked.first_name, picked.last_name) if x) or (picked.username or "")
        elif message.text and message.text.strip().isdigit():
            tg_id = int(message.text.strip())
        else:
            await message.answer("Выберите человека кнопкой или пришлите ID числом.", reply_markup=_pick_keyboard())
            return
        data = await state.get_data()
        role = data.get("role", "operator")
        await state.clear()
        with db.connect() as conn:
            if tg_id in config.ADMIN_TG_IDS:
                staff.set_name(conn, tg_id, name)
            else:
                staff.add(conn, tg_id, role, name, message.from_user.id)
            group = staff.support_chat_id(conn)
        await set_staff_commands(bot, tg_id, True)
        who = escape(name) if name else f"<code>{tg_id}</code>"
        await message.answer(f"✅ {who} теперь {staff.ROLES[role]}.", reply_markup=ReplyKeyboardRemove())
        invite = ""
        if group:
            try:
                link = await bot.create_chat_invite_link(group, name=f"{role} {tg_id}"[:32], member_limit=1)
                invite = link.invite_link
            except TelegramBadRequest as e:
                log.warning("invite link failed: %s", e)
        notice = (
            f"👋 Вас добавили в команду поддержки «{escape(config.SERVICE_NAME)}» ({staff.ROLES[role]}).\n\n"
            "Обращения пользователей приходят в группу поддержки, на каждого своя тема. "
            "Отвечайте сообщением в теме, /close закрывает обращение. Сводка и обращения: /admin."
        )
        try:
            kb = _kb([_url("💬 Вступить в группу поддержки", invite)]) if invite else None
            await bot.send_message(tg_id, notice, reply_markup=kb)
        except (TelegramBadRequest, TelegramForbiddenError):
            if invite:
                await message.answer(
                    "Не получилось написать человеку: он еще не запускал бота. Перешлите ему разовую ссылку "
                    f"в группу поддержки:\n{invite}"
                )
            else:
                await message.answer("Не получилось написать человеку: попросите его запустить бота и нажать /admin.")
        await show(message, *team_screen(message.from_user.id))

    return r


__all__ = ["build_router", "sync_staff_commands", "set_staff_commands", "home_screen", "bot_support"]
