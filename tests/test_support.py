"""Поддержка в боте: FAQ, обращения через группу с темами, ответы операторов, админ-панель команды."""
from __future__ import annotations

import datetime
import time

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery, BanChatMember, CopyMessage, CreateChatInviteLink, CreateForumTopic, EditMessageText,
    GetChatMember, SendMessage, SetMessageReaction, UnbanChatMember,
)
from aiogram.types import (
    CallbackQuery, Chat, ChatInviteLink, ChatMemberAdministrator, ForumTopic, Message, MessageId, SharedUser,
    Update, User, UsersShared,
)
from fastapi.testclient import TestClient

from chzkiz import config, db, staff, support
from chzkiz.bot import faq

GROUP = -1001234567890
OWNER = 555000
NOW = datetime.datetime.now


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.topics = 700

    async def close(self):
        return None

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, SendMessage):
            return Message(message_id=900 + len(self.calls), date=NOW(), chat=Chat(id=method.chat_id, type="private"),
                           text=method.text)
        if isinstance(method, CreateForumTopic):
            self.topics += 1
            return ForumTopic(message_thread_id=self.topics, name=method.name, icon_color=0)
        if isinstance(method, CopyMessage):
            return MessageId(message_id=5000 + len(self.calls))
        if isinstance(method, CreateChatInviteLink):
            return ChatInviteLink(invite_link="https://t.me/+invite", creator=User(id=1, is_bot=True, first_name="b"),
                                  creates_join_request=False, is_primary=False, is_revoked=False)
        if isinstance(method, GetChatMember):
            return ChatMemberAdministrator(
                user=User(id=42, is_bot=True, first_name="bot"), can_be_edited=False, is_anonymous=False,
                can_manage_chat=True, can_delete_messages=True, can_manage_video_chats=True,
                can_restrict_members=True, can_promote_members=False, can_change_info=True, can_invite_users=True,
                can_post_stories=False, can_edit_stories=False, can_delete_stories=False, can_manage_topics=True,
            )
        return True

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        if False:
            yield b""

    def of(self, kind):
        return [m for m in self.calls if isinstance(m, kind)]


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TG_IDS", {OWNER})
    monkeypatch.setattr(config, "SUPPORT_CHAT_ID", 0)
    from chzkiz.bot import main as bot_main

    session = FakeSession()
    bot = Bot(token="42:TEST", session=session)
    return bot_main.build_dispatcher(), bot, session


def _bind():
    with db.connect() as conn:
        staff.set_setting(conn, "support_chat_id", str(GROUP))


def _priv(uid, text=None, n=1, **kw):
    return Update(update_id=n, message=Message(
        message_id=n, date=NOW(), chat=Chat(id=uid, type="private"),
        from_user=User(id=uid, is_bot=False, first_name="Иван", username="ivan"), text=text, **kw))


def _grp(uid, text, n, topic=None, name="Оператор"):
    return Update(update_id=n, message=Message(
        message_id=n, date=NOW(), chat=Chat(id=GROUP, type="supergroup", is_forum=True),
        from_user=User(id=uid, is_bot=False, first_name=name), text=text,
        message_thread_id=topic, is_topic_message=topic is not None))


def _cb(uid, data, n, msg_id=901, chat_id=None, chat_type="private"):
    user = User(id=uid, is_bot=False, first_name="Иван")
    msg = Message(message_id=msg_id, date=NOW(), chat=Chat(id=chat_id or uid, type=chat_type), text="x")
    return Update(update_id=n, callback_query=CallbackQuery(id=str(n), from_user=user, chat_instance="c",
                                                            message=msg, data=data))


def _uid(tg):
    with db.connect() as conn:
        return db.get_or_create_user(conn, tg)["id"]


# --- FAQ ---------------------------------------------------------------------------------------

async def test_help_lists_faq_and_answers(env):
    dp, bot, s = env
    await dp.feed_update(bot, _cb(8001, "m:help", 1))
    kb = s.of(EditMessageText)[-1].reply_markup
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert cbs[:len(faq.FAQ)] == [f"faq:{k}" for k, _, _ in faq.FAQ]
    assert "sup:new" in cbs and "m:docs" in cbs and "m:home" in cbs
    await dp.feed_update(bot, _cb(8001, "faq:token", 2))
    assert "Персональный" in s.of(EditMessageText)[-1].text
    await dp.feed_update(bot, _cb(8001, "faq_ok", 3))
    assert s.of(AnswerCallbackQuery)[-1].text == "Рады помочь!"


def test_faq_style():
    import re

    for _, title, body in faq.FAQ:
        assert not re.search("[ёЁ—–«»]", title + body), title
        assert len(body) < 1500


async def test_without_group_shows_contacts(env, monkeypatch):
    monkeypatch.setattr(config, "SUPPORT_TG", "kiz_help")
    dp, bot, s = env
    await dp.feed_update(bot, _cb(8002, "sup:new", 1))
    last = s.of(EditMessageText)[-1]
    assert "@kiz_help" in last.text
    assert not s.of(CreateForumTopic)


# --- привязка группы -----------------------------------------------------------------------------

async def test_bind_only_by_admin(env):
    dp, bot, s = env
    await dp.feed_update(bot, _grp(8003, "/bind", 1))
    assert "только админ" in s.of(SendMessage)[-1].text
    await dp.feed_update(bot, _grp(OWNER, "/bind", 2))
    assert "Группа подключена" in s.of(SendMessage)[-1].text
    with db.connect() as conn:
        assert staff.support_chat_id(conn) == GROUP


async def test_chatid_in_group(env):
    dp, bot, s = env
    await dp.feed_update(bot, _grp(8004, "/chatid", 1))
    assert str(GROUP) in s.of(SendMessage)[-1].text


async def test_bot_silent_on_operator_chatter(env):
    dp, bot, s = env
    _bind()
    await dp.feed_update(bot, _grp(8005, "привет всем", 1))  # общий чат группы, не тема
    assert not s.of(SendMessage)


# --- обращение пользователя и ответ оператора --------------------------------------------------

async def test_full_ticket_flow(env):
    dp, bot, s = env
    _bind()
    with db.connect() as conn:
        staff.add(conn, 8100, "operator", "Ольга", OWNER)
    await dp.feed_update(bot, _cb(8101, "sup:new", 1))
    assert "Напишите вопрос" in s.of(EditMessageText)[-1].text
    await dp.feed_update(bot, _priv(8101, "Не видит флешку", 2))
    topic = s.of(CreateForumTopic)[0]
    assert topic.chat_id == GROUP and "Иван" in topic.name
    card = [m for m in s.of(SendMessage) if m.chat_id == GROUP][0]
    assert "Новое обращение" in card.text and "8101" in card.text and card.message_thread_id == 701
    copy = s.of(CopyMessage)[-1]
    assert copy.chat_id == GROUP and copy.message_thread_id == 701
    assert "передано в поддержку" in s.of(SendMessage)[-1].text
    # дописка без кнопки: тоже в тему, без новой темы и без лишнего экрана
    sent_before = len(s.of(SendMessage))
    await dp.feed_update(bot, _priv(8101, "И еще скрин", 3))
    assert len(s.of(CreateForumTopic)) == 1 and len(s.of(CopyMessage)) == 2
    assert len(s.of(SendMessage)) == sent_before and s.of(SetMessageReaction)
    # ответ оператора уходит пользователю
    await dp.feed_update(bot, _grp(8100, "Переустановите плагин", 4, topic=701))
    reply = s.of(SendMessage)[-1]
    assert reply.chat_id == 8101 and "Поддержка" in reply.text and "Переустановите плагин" in reply.text
    # посторонний в группе не может писать пользователю
    await dp.feed_update(bot, _grp(8199, "я не оператор", 5, topic=701))
    assert s.of(SendMessage)[-1].chat_id == GROUP and "нет в команде" in s.of(SendMessage)[-1].text
    # оператор закрывает
    await dp.feed_update(bot, _grp(8100, "/close", 6, topic=701))
    assert any(m.chat_id == 8101 and "закрыл обращение" in m.text for m in s.of(SendMessage))
    with db.connect() as conn:
        assert not support.is_open(conn, _uid(8101))
    # после закрытия обычный текст не уходит операторам
    copies = len(s.of(CopyMessage))
    await dp.feed_update(bot, _priv(8101, "спасибо", 7))
    assert len(s.of(CopyMessage)) == copies
    # новое обращение - в ту же тему, с новой карточкой
    await dp.feed_update(bot, _cb(8101, "sup:new", 8))
    await dp.feed_update(bot, _priv(8101, "Новый вопрос", 9))
    assert len(s.of(CreateForumTopic)) == 1
    assert sum(1 for m in s.of(SendMessage) if m.chat_id == GROUP and "Новое обращение" in m.text) == 2


def _home_cbs(s):
    kb = s.of(EditMessageText)[-1].reply_markup
    return [b.callback_data for row in kb.inline_keyboard for b in row], kb


async def test_chat_button_notifications_and_history(env):
    dp, bot, s = env
    _bind()
    with db.connect() as conn:
        staff.add(conn, 8300, "operator", "Ольга", OWNER)
    await dp.feed_update(bot, _cb(8301, "m:home", 1))
    assert "sup:chat" not in _home_cbs(s)[0]  # обращения нет - кнопки нет
    await dp.feed_update(bot, _cb(8301, "sup:new", 2))
    await dp.feed_update(bot, _priv(8301, "Не уходит документ", 3))
    # в чате: ответ оператора приходит обычным сообщением
    await dp.feed_update(bot, _grp(8300, "Проверьте подпись", 4, topic=701))
    reply = s.of(SendMessage)[-1]
    assert reply.chat_id == 8301 and reply.text.startswith("🎧 <b>Поддержка</b>")
    # ушел в меню: кнопка чата есть, ответ приходит уведомлением с кнопкой вернуться
    await dp.feed_update(bot, _cb(8301, "m:home", 5))
    cbs, kb = _home_cbs(s)
    assert cbs[0] == "sup:chat" and "новый ответ" not in kb.inline_keyboard[0][0].text
    await dp.feed_update(bot, _grp(8300, "Попробуйте еще раз", 6, topic=701))
    note = s.of(SendMessage)[-1]
    assert note.chat_id == 8301 and "Новый ответ поддержки" in note.text
    assert "sup:chat" in [b.callback_data for row in note.reply_markup.inline_keyboard for b in row]
    await dp.feed_update(bot, _cb(8301, "m:home", 7))
    assert "новый ответ" in _home_cbs(s)[1].inline_keyboard[0][0].text
    # открыл чат: видна переписка, отметка о новом ответе снята
    await dp.feed_update(bot, _cb(8301, "sup:chat", 8))
    chat = s.of(EditMessageText)[-1].text
    assert "Не уходит документ" in chat and "Проверьте подпись" in chat and "Попробуйте еще раз" in chat
    await dp.feed_update(bot, _cb(8301, "m:home", 9))
    assert "новый ответ" not in _home_cbs(s)[1].inline_keyboard[0][0].text
    # оператор закрыл: уведомление, переписка стерта, кнопки в меню нет
    await dp.feed_update(bot, _grp(8300, "/close", 10, topic=701))
    assert any(m.chat_id == 8301 and "Оператор закрыл обращение" in m.text for m in s.of(SendMessage))
    with db.connect() as conn:
        assert support.history(conn, _uid(8301)) == []
    await dp.feed_update(bot, _cb(8301, "m:home", 11))
    assert "sup:chat" not in _home_cbs(s)[0]


def test_chat_keeps_last_five_from_user():
    uid = _uid(8302)
    with db.connect() as conn:
        support.open_thread(conn, uid)
        for i in range(8):
            support.log(conn, uid, "in", f"вопрос {i}")
        for i in range(7):
            support.log(conn, uid, "out", f"ответ {i}")
        rows = support.history(conn, uid)
    assert [r["text"] for r in rows if r["direction"] == "in"] == [f"вопрос {i}" for i in range(3, 8)]
    assert [r["text"] for r in rows if r["direction"] == "out"] == [f"ответ {i}" for i in range(2, 7)]


async def test_user_closes_ticket(env):
    dp, bot, s = env
    _bind()
    await dp.feed_update(bot, _cb(8201, "sup:new", 1))
    await dp.feed_update(bot, _priv(8201, "вопрос", 2))
    await dp.feed_update(bot, _cb(8201, "sup:done", 3))
    assert any(m.chat_id == GROUP and "закрыто пользователем" in m.text for m in s.of(SendMessage))
    with db.connect() as conn:
        assert not support.is_open(conn, _uid(8201))


async def test_reminder_once_per_wait(env):
    from chzkiz.bot import support as bot_support

    dp, bot, s = env
    _bind()
    await dp.feed_update(bot, _cb(8301, "sup:new", 1))
    await dp.feed_update(bot, _priv(8301, "ау", 2))
    uid = _uid(8301)
    with db.connect() as conn:
        conn.execute("UPDATE support_threads SET last_user_at = ? WHERE user_id = ?", (time.time() - 3600, uid))
        conn.commit()
    assert await bot_support.remind(bot) == 1
    assert "ждет ответа" in s.of(SendMessage)[-1].text
    assert await bot_support.remind(bot) == 0


# --- админ-панель --------------------------------------------------------------------------------

async def test_admin_hidden_from_users(env):
    dp, bot, s = env
    await dp.feed_update(bot, _priv(8401, "/admin", 1))
    from chzkiz.bot.main import HINT_TEXT

    assert s.of(SendMessage)[-1].text == HINT_TEXT
    await dp.feed_update(bot, _cb(8401, "adm:team", 2))
    assert s.of(AnswerCallbackQuery)[-1].text == "Нет доступа"


async def test_admin_panel_and_team(env):
    dp, bot, s = env
    _bind()
    await dp.feed_update(bot, _priv(OWNER, "/admin", 1))
    home = s.of(SendMessage)[-1]
    assert "Админ-панель" in home.text and "Группа поддержки подключена" in home.text
    urls = [b.url for row in home.reply_markup.inline_keyboard for b in row if b.url]
    assert any("next=/admin" in u for u in urls)
    # добавляем оператора системной кнопкой выбора пользователя
    await dp.feed_update(bot, _cb(OWNER, "adm:add:operator", 2))
    pick = s.of(SendMessage)[-1]
    assert pick.reply_markup.keyboard[0][0].request_users is not None
    shared = UsersShared(request_id=1, users=[SharedUser(user_id=8500, first_name="Петр")])
    await dp.feed_update(bot, _priv(OWNER, None, 3, users_shared=shared))
    with db.connect() as conn:
        assert staff.role_of(conn, 8500) == "operator"
    assert s.of(CreateChatInviteLink) and any(m.chat_id == 8500 and "команду поддержки" in m.text for m in s.of(SendMessage))
    team = s.of(SendMessage)[-1]
    assert "Петр" in team.text and "оператор" in team.text
    # оператор видит панель, но не управляет командой
    await dp.feed_update(bot, _cb(8500, "adm:add:admin", 4))
    assert s.of(AnswerCallbackQuery)[-1].text == "Это может только админ"
    # админ убирает оператора: доступ и группа
    await dp.feed_update(bot, _cb(OWNER, "adm:rm:8500", 5))
    await dp.feed_update(bot, _cb(OWNER, "adm:rmok:8500", 6))
    with db.connect() as conn:
        assert staff.role_of(conn, 8500) is None
    assert s.of(BanChatMember) and s.of(UnbanChatMember)


async def test_owner_cannot_be_removed(env):
    with db.connect() as conn:
        assert not staff.remove(conn, OWNER)
        assert staff.role_of(conn, OWNER) == "admin"


async def test_tickets_screen_links_to_topics(env):
    dp, bot, s = env
    _bind()
    await dp.feed_update(bot, _cb(8601, "sup:new", 1))
    await dp.feed_update(bot, _priv(8601, "помогите", 2))
    await dp.feed_update(bot, _cb(OWNER, "adm:tickets", 3))
    screen = s.of(EditMessageText)[-1]
    assert "ждет" in screen.text
    urls = [b.url for row in screen.reply_markup.inline_keyboard for b in row if b.url]
    assert urls == ["https://t.me/c/1234567890/701"]


def test_home_menu_button_only_for_team():
    from chzkiz.bot import screens

    cbs = lambda kb: [b.callback_data for row in kb.inline_keyboard for b in row]  # noqa: E731
    assert "adm:home" not in cbs(screens.home_screen(1, True)[1])
    assert "adm:home" in cbs(screens.home_screen(1, True, "", True)[1])


def test_web_admin_for_staff_admin(monkeypatch):
    from chzkiz.web.app import create_app

    monkeypatch.setattr(config, "ADMIN_TG_IDS", set())
    c = TestClient(create_app())
    with db.connect() as conn:
        u = db.get_or_create_user(conn, 8700)
        staff.add(conn, 8700, "admin", "Админ", 1)
        staff.add(conn, 8701, "operator", "Оператор", 1)
        token = db.create_login_token(conn, u["id"])
        op = db.get_or_create_user(conn, 8701)
        op_token = db.create_login_token(conn, op["id"])
    c.post(f"/l/{token}")
    assert c.get("/admin").status_code == 200
    c2 = TestClient(create_app())
    c2.post(f"/l/{op_token}")
    assert c2.get("/admin").status_code == 404  # оператору веб-админка не нужна
