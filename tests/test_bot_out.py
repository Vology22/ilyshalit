"""Бот: меню одним сообщением, сверка, токен WB, файлы, баланс, приглашения, помощь."""
import re

from aiogram.methods import (
    AnswerCallbackQuery, DeleteMessage, EditMessageText, SendMediaGroup, SendMessage, SendPhoto,
)

from tests.test_instrukciya_bot import _buttons, _cb
from tests.test_regress_1 import _bot_env, _upd


def _all_screen_texts():
    from chzkiz.bot import screens

    texts = [
        screens.welcome_screen("Иван")[0], screens.home_screen(3, False)[0], screens.home_screen(1, True, "seller")[0],
        screens.sverka_screen(False, 0)[0], screens.sverka_screen(True, 5)[0], screens.link_screen("http://x")[0],
        screens.phone_screen("http://x")[0], screens.wb_have_screen()[0], screens.api_new_screen()[1],
        screens.api_ok_screen()[0], screens.api_bad_screen()[0], screens.files_done_screen()[0],
        screens.balance_screen(2, [{"name": "Старт", "ops": 10, "price_kop": 59000}], "http://x")[0],
        screens.ref_screen("seller", "https://t.me/b?start=ref_x", _stats())[0],
        screens.ref_screen("fulfillment", "https://t.me/b?start=ref_x", _stats())[0],
        screens.info_screen()[0],
    ]
    return texts + [c for _, c in screens.files_screens()]


def _stats():
    return {"invited": 2, "paid": 1, "ops": 3, "accrued_kop": 11800, "paid_kop": 0}


def _kb_texts():
    from chzkiz.bot import screens

    kbs = [
        screens.welcome_screen()[1], screens.home_screen(3, False)[1], screens.sverka_screen(False, 0)[1],
        screens.link_screen("http://x")[1], screens.phone_screen("http://x")[1], screens.wb_have_screen()[1],
        screens.api_new_screen()[2], screens.api_ok_screen()[1], screens.files_done_screen()[1],
        screens.info_screen()[1],
    ]
    return [b.text for k in kbs for b in _buttons(k)]


def _sent(session, kind):
    return [m for m in session.calls if isinstance(m, kind)]


def _screens(session):
    """Все экраны, которые видел человек: новые сообщения и правки на месте."""
    return [m for m in session.calls if isinstance(m, (SendMessage, EditMessageText))]


def _links(session):
    return [b.url for m in _screens(session) if m.reply_markup
            for b in _buttons(m.reply_markup) if b.url and "/l/" in b.url]


def _cbs(markup):
    return [b.callback_data for b in _buttons(markup) if b.callback_data]


def _set_key(uid):
    from chzkiz import crypto, db

    with db.connect() as conn:
        user = db.get_or_create_user(conn, uid)
        db.set_wb_key(conn, user["id"], crypto.encrypt_str("k" * 40))


def _set_cat(uid, cat="seller"):
    from chzkiz import billing, db

    with db.connect() as conn:
        user = db.get_or_create_user(conn, uid)
        billing.set_category(conn, user["id"], cat)


# --- экраны -----------------------------------------------------------------------------

def test_texts_have_no_yo_or_long_dash():
    for t in _all_screen_texts() + _kb_texts():
        assert not re.search("[ёЁ—–«»]", t), t


def test_captions_fit_telegram_limits():
    from chzkiz.bot import screens

    for cap in [screens.api_new_screen()[1]] + [c for _, c in screens.files_screens()]:
        assert len(re.sub("<[^>]+>", "", cap)) <= 1024
    for t in _all_screen_texts():
        assert len(t) <= 4096


def test_images_exist():
    from chzkiz.bot import screens

    for p in (screens.IMG_API, screens.IMG_ARCHIVE, screens.IMG_FINREPORT):
        assert p.is_file() and p.stat().st_size > 10_000


def test_home_shows_balance_and_token_status():
    from chzkiz.bot import screens

    text, kb = screens.home_screen(21, True, "fulfillment")
    assert "21 операция" in text and "подключен ✅" in text and "Фулфилмент" in text
    assert "КИЗ по возвратам ваших товаров" in text
    assert _cbs(kb) == ["m:go", "m:wb", "m:files", "m:bal", "m:ref", "ins:0", "m:help"]
    assert "не подключен" in screens.home_screen(0, False)[0]


def test_ops_word():
    from chzkiz.bot.screens import ops_word

    assert [ops_word(n) for n in (1, 2, 5, 11, 12, 21, 22, 25, 101)] == [
        "1 операция", "2 операции", "5 операций", "11 операций", "12 операций",
        "21 операция", "22 операции", "25 операций", "101 операция",
    ]


def test_sverka_screen_adapts_to_token_and_balance():
    from chzkiz import config
    from chzkiz.bot import screens

    text, kb = screens.sverka_screen(True, 5)
    assert _cbs(kb) == ["m:link", "m:home"]
    assert [b.url for b in _buttons(kb) if b.url] == [config.PUBLIC_URL + "/instrukciya#video"]
    assert "/l/" not in text
    text, kb = screens.sverka_screen(False, 0)
    assert _cbs(kb) == ["m:link", "m:wb_new", "m:files", "m:bal", "m:home"]
    assert "Операции закончились" in text


def test_every_screen_leads_home():
    from chzkiz.bot import screens

    for kb in (screens.sverka_screen(True, 1)[1], screens.link_screen("http://x")[1], screens.phone_screen("http://x")[1],
               screens.wb_have_screen()[1], screens.api_ok_screen()[1], screens.files_done_screen()[1],
               screens.balance_screen(1, [], "http://x")[1], screens.ref_screen("seller", "x", _stats())[1],
               screens.info_screen()[1]):
        assert "m:home" in _cbs(kb)


def test_ref_screen_share_button():
    from chzkiz.bot import screens

    text, kb = screens.ref_screen("seller", "https://t.me/kiz_bot?start=ref_abc", _stats())
    assert "<code>https://t.me/kiz_bot?start=ref_abc</code>" in text and "Получено: <b>3 операции</b>" in text
    share = [b.url for b in _buttons(kb) if b.url][0]
    assert share.startswith("https://t.me/share/url?url=https%3A%2F%2Ft.me%2Fkiz_bot")
    text, kb = screens.ref_screen("fulfillment", "ref_abc", _stats())
    assert "20%" in text and "118 ₽" in text
    assert not [b for b in _buttons(kb) if b.url]  # без имени бота делиться нечем


# --- сценарии ----------------------------------------------------------------------------

async def test_new_user_start_asks_category_then_menu():
    bot_main, bot, session = _bot_env()
    dp = bot_main.build_dispatcher()
    await dp.feed_update(bot, _upd(9707, "/start", 1))
    state = await dp.fsm.get_context(bot, chat_id=9707, user_id=9707).get_state()
    assert state is None
    first = _sent(session, SendMessage)[0]
    assert "Привет, T!" in first.text and _cbs(first.reply_markup) == ["cat:seller", "cat:fulfillment"]
    await dp.feed_update(bot, _cb(9707, "cat:seller", 2, 901))
    answers = _sent(session, AnswerCallbackQuery)
    assert answers[-1].text == "Готово: Селлер / менеджер"
    home = _sent(session, EditMessageText)[-1]
    assert home.text.startswith("<b>Сверка КИЗ") and "m:go" in _cbs(home.reply_markup)


async def test_known_user_start_opens_menu():
    bot_main, bot, session = _bot_env()
    _set_cat(9708)
    dp = bot_main.build_dispatcher()
    await dp.feed_update(bot, _upd(9708, "/start", 1))
    assert _cbs(_sent(session, SendMessage)[0].reply_markup)[0] == "m:go"


async def test_menu_edits_one_message_instead_of_spamming():
    bot_main, bot, session = _bot_env()
    _set_cat(9709)
    dp = bot_main.build_dispatcher()
    for n, data in enumerate(["m:go", "m:bal", "m:ref", "m:help", "ins:0", "m:home"], start=1):
        await dp.feed_update(bot, _cb(9709, data, n, 901))
    assert not _sent(session, SendMessage)
    edits = _sent(session, EditMessageText)
    assert len(edits) == 6 and {e.message_id for e in edits} == {901}
    assert len(_sent(session, AnswerCallbackQuery)) == 6  # часики на кнопке не висят


async def test_out_command_has_no_link():
    bot_main, bot, session = _bot_env()
    dp = bot_main.build_dispatcher()
    await dp.feed_update(bot, _upd(9701, "/out", 1))
    assert not _links(session)
    assert _cbs(_sent(session, SendMessage)[0].reply_markup)[0] == "m:link"


async def test_link_gives_fresh_link_only_in_button():
    bot_main, bot, session = _bot_env()
    dp = bot_main.build_dispatcher()
    await dp.feed_update(bot, _cb(9702, "m:link", 1, 901))
    await dp.feed_update(bot, _cb(9702, "m:link", 2, 902))
    links = _links(session)
    assert len(links) == 2 and links[0] != links[1]
    for m in _screens(session):
        assert "/l/" not in m.text and "http" not in m.text
    await dp.feed_update(bot, _cb(9702, "m:phone", 3, 902))
    assert len(_links(session)) == 3 and "С телефона" in _screens(session)[-1].text


async def test_wb_without_token_waits_key_and_sends_photo():
    bot_main, bot, session = _bot_env()
    dp = bot_main.build_dispatcher()
    await dp.feed_update(bot, _cb(9703, "m:wb", 1, 901))
    photos = _sent(session, SendPhoto)
    assert len(photos) == 1
    assert str(photos[0].photo.path).endswith("api_token.png")
    assert _cbs(photos[0].reply_markup) == ["m:home"]
    assert [d.message_id for d in _sent(session, DeleteMessage)] == [901]  # старое меню убрано
    state = await dp.fsm.get_context(bot, chat_id=9703, user_id=9703).get_state()
    assert state == bot_main.WaitKey.key.state
    await dp.feed_update(bot, _cb(9703, "m:home", 2, 950))  # отмена: текст больше не ловим как токен
    assert await dp.fsm.get_context(bot, chat_id=9703, user_id=9703).get_state() is None


async def test_wb_with_token_offers_replace():
    bot_main, bot, session = _bot_env()
    _set_key(9704)
    dp = bot_main.build_dispatcher()
    await dp.feed_update(bot, _cb(9704, "m:wb", 1, 901))
    assert not _sent(session, SendPhoto)
    assert _cbs(_sent(session, EditMessageText)[0].reply_markup) == ["m:go", "m:wb_new", "m:home"]
    await dp.feed_update(bot, _cb(9704, "m:wb_new", 2, 902))
    assert len(_sent(session, SendPhoto)) == 1
    state = await dp.fsm.get_context(bot, chat_id=9704, user_id=9704).get_state()
    assert state == bot_main.WaitKey.key.state


async def test_good_and_bad_token(monkeypatch):
    bot_main, bot, session = _bot_env()
    results = iter([False, True])

    async def check(raw_key):
        return next(results)

    monkeypatch.setattr(bot_main, "_check_wb_key", check)
    dp = bot_main.build_dispatcher()
    await dp.feed_update(bot, _cb(9710, "m:wb", 1, 901))
    await dp.feed_update(bot, _upd(9710, "eyJhbGciOiJIUzI1NiJ9.bad.token1234567890", 2))
    assert "Токен не сработал" in _sent(session, SendMessage)[-1].text
    await dp.feed_update(bot, _upd(9710, "eyJhbGciOiJIUzI1NiJ9.good.token1234567890", 3))
    last = _sent(session, SendMessage)[-1]
    assert "подключен и проверен" in last.text and _cbs(last.reply_markup) == ["m:go", "m:home"]
    assert len(_sent(session, DeleteMessage)) == 3  # меню + оба сообщения с токенами


async def test_files_sends_album_then_buttons():
    bot_main, bot, session = _bot_env()
    dp = bot_main.build_dispatcher()
    await dp.feed_update(bot, _cb(9705, "m:files", 1, 901))
    album = _sent(session, SendMediaGroup)
    assert len(album) == 1
    assert [p.media.path.name for p in album[0].media] == ["files_archive.png", "files_finreport.png"]
    assert album[0].media[0].caption.startswith("<b>Шаг 1 из 2")
    last = _sent(session, SendMessage)[-1]
    assert _cbs(last.reply_markup) == ["m:link", "m:home"]


async def test_balance_screen_lists_tariffs_and_cabinet_link():
    bot_main, bot, session = _bot_env()
    dp = bot_main.build_dispatcher()
    await dp.feed_update(bot, _upd(9711, "/cabinet", 1))
    m = _sent(session, SendMessage)[-1]
    assert "Баланс: 3 операции" in m.text and "Старт: 10 операций за <b>590 ₽</b>" in m.text
    assert any(u.endswith("?next=/cabinet") for u in _links(session))


async def test_unknown_input_gets_hint_with_menu_button():
    bot_main, bot, session = _bot_env()
    dp = bot_main.build_dispatcher()
    await dp.feed_update(bot, _upd(9712, "привет", 1))
    m = _sent(session, SendMessage)[-1]
    assert m.text == bot_main.HINT_TEXT and _cbs(m.reply_markup) == ["m:home"]
