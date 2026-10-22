import datetime

from tests.test_regress_1 import _bot_env, _upd


def _cb(uid: int, data: str, n: int, msg_id: int):
    from aiogram.types import CallbackQuery, Chat, Message, Update, User

    user = User(id=uid, is_bot=False, first_name="T")
    msg = Message(message_id=msg_id, date=datetime.datetime.now(), chat=Chat(id=uid, type="private"), text="x")
    return Update(update_id=n, callback_query=CallbackQuery(id=str(n), from_user=user, chat_instance="c", message=msg, data=data))


def _buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


def test_page_boundaries():
    from chzkiz.bot.main import INSTRUKCIYA_STEPS, instrukciya_page

    total = len(INSTRUKCIYA_STEPS)
    text, kb = instrukciya_page(0)
    assert text.startswith(f"Шаг 1 из {total}")
    assert [b.text for b in _buttons(kb)] == ["Дальше"]
    text, kb = instrukciya_page(total - 1)
    assert text.startswith(f"Шаг {total} из {total}")
    labels = [b.text for b in _buttons(kb)]
    assert "Назад" in labels and "Дальше" not in labels
    assert any(b.url and b.url.endswith("/instrukciya") for b in _buttons(kb))
    assert instrukciya_page(-5)[0] == instrukciya_page(0)[0]
    assert instrukciya_page(total + 7)[0] == instrukciya_page(total - 1)[0]


async def test_instrukciya_command_and_paging():
    from aiogram.methods import EditMessageText, SendMessage

    bot_main, bot, session = _bot_env()
    dp = bot_main.build_dispatcher()
    await dp.feed_update(bot, _upd(9301, "/instrukciya", 1))
    sent = [m for m in session.calls if isinstance(m, SendMessage)]
    assert sent and sent[-1].text.startswith("Шаг 1 из")
    await dp.feed_update(bot, _cb(9301, "ins:1", 2, 901))
    await dp.feed_update(bot, _cb(9301, "ins:0", 3, 901))
    edits = [m for m in session.calls if isinstance(m, EditMessageText)]
    assert [e.text.split(".")[0].split(" из")[0] for e in edits] == ["Шаг 2", "Шаг 1"]
    last = len(bot_main.INSTRUKCIYA_STEPS) - 1
    await dp.feed_update(bot, _cb(9301, f"ins:{last}", 4, 901))
    edits = [m for m in session.calls if isinstance(m, EditMessageText)]
    labels = [b.text for row in edits[-1].reply_markup.inline_keyboard for b in row]
    assert labels[0] == "Назад" and "Дальше" not in labels


async def test_other_commands_still_work():
    from aiogram.methods import SendMessage

    bot_main, bot, session = _bot_env()
    dp = bot_main.build_dispatcher()
    await dp.feed_update(bot, _upd(9302, "/help", 1))
    sent = [m for m in session.calls if isinstance(m, SendMessage)]
    assert "/out" in sent[-1].text and "/instrukciya" in sent[-1].text
