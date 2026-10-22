import re

from aiogram.methods import SendMessage, SendPhoto

from tests.test_instrukciya_bot import _buttons, _cb
from tests.test_regress_1 import _bot_env, _upd


def _all_screen_texts():
    from chzkiz.bot import screens

    texts = [screens.start_screen()[0], screens.out_screen()[0], screens.go_screen("http://x/l/t")[0],
             screens.ins_screen()[0], screens.api_have_screen()[0], screens.api_new_screen()[1],
             screens.api_ok_screen()[0], screens.fl_screen()[0], screens.nofl_text()]
    texts += [c for _, c, _ in screens.files_screens()]
    return texts


def _kb_texts():
    from chzkiz.bot import screens

    kbs = [screens.start_screen()[1], screens.out_screen()[1], screens.go_screen("http://x")[1],
           screens.ins_screen()[1], screens.api_have_screen()[1], screens.api_new_screen()[2],
           screens.fl_screen()[1]] + [k for _, _, k in screens.files_screens() if k]
    return [b.text for k in kbs for b in _buttons(k)]


def _sent(session, kind):
    return [m for m in session.calls if isinstance(m, kind)]


def _links(session):
    return [b.url for m in _sent(session, SendMessage) if m.reply_markup
            for b in _buttons(m.reply_markup) if b.url and "/l/" in b.url]


def _set_key(uid):
    from chzkiz import crypto, db

    with db.connect() as conn:
        user = db.get_or_create_user(conn, uid)
        db.set_wb_key(conn, user["id"], crypto.encrypt_str("k" * 40))


def test_out_screen_buttons():
    from chzkiz.bot import screens

    text, kb = screens.out_screen()
    assert text.startswith("Сверка КИЗ Wildberries FBS")
    cbs = [b.callback_data for b in _buttons(kb) if b.callback_data]
    assert cbs == ["sv:go", "sv:ins"]
    urls = [b.url for b in _buttons(kb) if b.url]
    assert len(urls) == 1 and urls[0].endswith("/instrukciya#video")
    assert "/l/" not in text


def test_start_screen_buttons():
    from chzkiz.bot import screens

    _, kb = screens.start_screen()
    assert [b.callback_data for b in _buttons(kb)] == ["sv:api", "sv:go", "sv:ins"]


def test_start_screen_mentions_returns_by_goods():
    from chzkiz.bot import screens

    text, _ = screens.start_screen()
    assert "КИЗ по возвратам ваших товаров" in text
    assert "возвращенные по вашим продажам" not in text


def test_texts_have_no_yo_or_long_dash():
    for t in _all_screen_texts() + _kb_texts():
        assert not re.search("[ёЁ—–«»]", t), t


def test_images_exist():
    from chzkiz.bot import screens

    for p in (screens.IMG_API, screens.IMG_ARCHIVE, screens.IMG_FINREPORT):
        assert p.is_file() and p.stat().st_size > 10_000


async def test_out_command_has_no_link():
    bot_main, bot, session = _bot_env()
    dp = bot_main.build_dispatcher()
    await dp.feed_update(bot, _upd(9701, "/out", 1))
    assert not _links(session)
    kb = _sent(session, SendMessage)[0].reply_markup
    assert [b.callback_data for b in _buttons(kb) if b.callback_data] == ["sv:go", "sv:ins"]


async def test_go_gives_fresh_link_without_link_in_text():
    bot_main, bot, session = _bot_env()
    dp = bot_main.build_dispatcher()
    await dp.feed_update(bot, _cb(9702, "sv:go", 1, 901))
    await dp.feed_update(bot, _cb(9702, "sv:go", 2, 902))
    links = _links(session)
    assert len(links) == 2 and links[0] != links[1]
    for m in _sent(session, SendMessage):
        assert "/l/" not in m.text and "http" not in m.text


async def test_api_without_token_waits_key_and_sends_photo():
    bot_main, bot, session = _bot_env()
    dp = bot_main.build_dispatcher()
    await dp.feed_update(bot, _cb(9703, "sv:api", 1, 901))
    photos = _sent(session, SendPhoto)
    assert len(photos) == 1
    assert str(photos[0].photo.path).endswith("api_token.png")
    assert [b.callback_data for b in _buttons(photos[0].reply_markup)] == ["sv:ins"]
    state = await dp.fsm.get_context(bot, chat_id=9703, user_id=9703).get_state()
    assert state == bot_main.WaitKey.key.state


async def test_api_with_token_offers_next_or_replace():
    bot_main, bot, session = _bot_env()
    _set_key(9704)
    dp = bot_main.build_dispatcher()
    await dp.feed_update(bot, _cb(9704, "sv:api", 1, 901))
    assert not _sent(session, SendPhoto)
    kb = _sent(session, SendMessage)[0].reply_markup
    assert [b.callback_data for b in _buttons(kb)] == ["sv:fl", "sv:api_new", "sv:ins"]
    await dp.feed_update(bot, _cb(9704, "sv:api_new", 2, 902))
    assert len(_sent(session, SendPhoto)) == 1
    state = await dp.fsm.get_context(bot, chat_id=9704, user_id=9704).get_state()
    assert state == bot_main.WaitKey.key.state


async def test_files_sends_two_photos_then_buttons():
    bot_main, bot, session = _bot_env()
    dp = bot_main.build_dispatcher()
    await dp.feed_update(bot, _cb(9705, "sv:files", 1, 901))
    photos = _sent(session, SendPhoto)
    assert [p.photo.path.name for p in photos] == ["files_archive.png", "files_finreport.png"]
    assert photos[0].caption.startswith("Шаг 1 из 2") and photos[1].caption.startswith("Шаг 2 из 2")
    assert photos[0].reply_markup is None
    assert [b.callback_data for b in _buttons(photos[1].reply_markup)] == ["sv:fl", "sv:ins"]
    assert [b.text for b in _buttons(photos[1].reply_markup)][0] == "✅ Файлы скачаны"


async def test_fl_and_nofl_flow():
    bot_main, bot, session = _bot_env()
    dp = bot_main.build_dispatcher()
    await dp.feed_update(bot, _cb(9706, "sv:fl", 1, 901))
    kb = _sent(session, SendMessage)[0].reply_markup
    assert [b.callback_data for b in _buttons(kb)] == ["sv:go", "sv:nofl", "sv:ins"]
    await dp.feed_update(bot, _cb(9706, "sv:nofl", 2, 902))
    assert len(_links(session)) == 1
    assert len(_sent(session, SendMessage)) == 3


async def test_start_does_not_wait_key():
    bot_main, bot, session = _bot_env()
    dp = bot_main.build_dispatcher()
    await dp.feed_update(bot, _upd(9707, "/start", 1))
    state = await dp.fsm.get_context(bot, chat_id=9707, user_id=9707).get_state()
    assert state is None
    kb = _sent(session, SendMessage)[0].reply_markup
    assert [b.callback_data for b in _buttons(kb)] == ["sv:api", "sv:go", "sv:ins"]
