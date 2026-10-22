import re
from pathlib import Path

from fastapi.testclient import TestClient

from chzkiz import config
from chzkiz.bot import screens
from chzkiz.web.app import create_app

client = TestClient(create_app())
STATIC = Path(__file__).resolve().parent.parent / "chzkiz" / "web" / "static"
NAMES = ("api", "files", "vyvod", "nav", "bot")


def _srcs(html: str) -> list[str]:
    return re.findall(r'<video[^>]*\ssrc="([^"]+)"', html)


def test_instrukciya_has_video_section_first():
    html = client.get("/instrukciya").text
    assert 'id="video"' in html
    assert html.index('id="video"') < html.index('id="api"')
    assert 'href="#video"' in html
    tags = re.findall(r"<video[^>]*>", html)
    assert tags and all("controls" in t and 'preload="metadata"' in t and "poster=" in t for t in tags)


def test_every_video_src_is_a_file_and_served():
    srcs = _srcs(client.get("/instrukciya").text)
    for name in NAMES:
        assert f"/static/ins/video/{name}.mp4" in srcs
    for src in srcs:
        assert src.startswith("/static/")
        path = STATIC / src.removeprefix("/static/")
        assert path.is_file() and path.stat().st_size > 10_000, src
        assert path.stat().st_size < 4 * 1024 * 1024, src
        assert client.get(src).status_code == 200


def test_api_and_files_sections_have_own_video():
    html = client.get("/instrukciya").text
    api = html[html.index('<h2 id="api">'):html.index('<h2 id="files">')]
    files = html[html.index('<h2 id="files">'):html.index('<h2 id="panel">')]
    assert "/static/ins/video/api.mp4" in api
    assert "/static/ins/video/files.mp4" in files


def test_bot_video_button_leads_to_video_section():
    _, kb = screens.sverka_screen(True, 1)
    buttons = [b for row in kb.inline_keyboard for b in row if b.url]
    assert len(buttons) == 1
    assert buttons[0].url == config.PUBLIC_URL + "/instrukciya#video"
