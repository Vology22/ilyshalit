"""Бот и страница инструкции: единый путь токена WB и выгрузки файлов, описание бота, постеры роликов."""
import re
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from chzkiz.bot import main as bot_main
from chzkiz.bot import screens
from chzkiz.web.app import create_app
from tests.test_regress_1 import _bot_env, _upd

client = TestClient(create_app())


def _bot_texts() -> str:
    parts = [b for _, b in bot_main.INSTRUKCIYA_STEPS]
    parts.append(screens.api_new_screen()[1])
    parts += [c for _, c in screens.files_screens()]
    parts.append(bot_main.HINT_TEXT)
    return "\n".join(parts)


def test_token_path_in_bot_and_page():
    page = client.get("/instrukciya").text
    bot = _bot_texts()
    assert screens.TOKEN_PATH in page
    assert screens.TOKEN_PATH in bot
    for word in ("Маркетплейс", "Финансы", "Аналитика", "Только на чтение", "Персональный"):
        assert word in page and word in bot
    assert "Для интеграции вручную" in page and "Для интеграции вручную" in bot
    assert screens.TOKEN_SAFE in page and screens.TOKEN_SAFE in bot


def test_files_path_same_in_bot_and_page():
    page = client.get("/instrukciya").text
    assert screens.FILES_PATH in page
    assert screens.FILES_PATH in _bot_texts()
    cap = screens.files_screens()[0][1]
    assert "Вкладка Завершенные\n" in cap and "Вкладка Архив" not in cap and "3 месяцев" not in cap
    assert "затем <strong>Архив" not in page


def test_no_word_klyuch_about_wb():
    texts = _bot_texts() + client.get("/instrukciya").text
    for m in re.finditer(r"[^\n.]*\b[Кк]люч\w*[^\n.]*", texts):
        assert "подпис" in m.group(0).lower() or "эцп" in m.group(0).lower(), m.group(0)


def test_step7_covers_return():
    body = dict((t, b) for t, b in bot_main.INSTRUKCIYA_STEPS)["Вывод и возврат"]
    assert "Только вернуть" in body
    assert "Вывести" in body


async def test_help_mentions_video():
    from aiogram.methods import SendMessage

    mod, bot, session = _bot_env()
    dp = mod.build_dispatcher()
    await dp.feed_update(bot, _upd(9401, "/help", 1))
    sent = [m for m in session.calls if isinstance(m, SendMessage)]
    assert "Видео" in sent[-1].text


async def test_setup_bot_sets_descriptions():
    bot = AsyncMock()
    await bot_main.setup_bot(bot)
    bot.set_my_commands.assert_awaited_once()
    bot.set_my_description.assert_awaited_once_with(bot_main.BOT_DESCRIPTION)
    bot.set_my_short_description.assert_awaited_once_with(bot_main.BOT_SHORT_DESCRIPTION)
    assert len(bot_main.BOT_DESCRIPTION) <= 512 and len(bot_main.BOT_SHORT_DESCRIPTION) <= 120


def test_page_restart_and_videos():
    page = client.get("/instrukciya").text
    assert "Начать заново" in page and 'href="/step1"' in page
    videos = re.findall(r"<video[^>]*>", page)
    assert len(videos) >= 5
    for v in videos:
        assert 'preload="metadata"' in v and "poster=" in v, v
        poster = re.search(r'poster="([^"]+)"', v).group(1)
        assert client.get(poster).status_code == 200, poster


def test_page_images_served_and_excel_text():
    page = client.get("/instrukciya").text
    imgs = re.findall(r'<img src="([^"]+)"', page)
    for need in ("panel_step2.png", "panel_step3.png", "panel_step4.png", "api_token.png"):
        assert any(i.endswith(need) for i in imgs), need
    for i in imgs:
        assert client.get(i).status_code == 200, i
    assert "Посмотреть что изменится" in page
    assert "Будут изменены" in page
    assert "с двумя листами" not in page
