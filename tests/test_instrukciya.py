import re

from fastapi.testclient import TestClient

from chzkiz.web.app import create_app

client = TestClient(create_app())


def test_instrukciya_returns_200():
    r = client.get("/instrukciya")
    assert r.status_code == 200


def test_instrukciya_has_section_headers():
    r = client.get("/instrukciya")
    assert r.status_code == 200
    h2_tags = re.findall(r"<h2[^>]*>(.*?)</h2>", r.text)
    assert len(h2_tags) == 6, f"expected 6 h2 headers, got {len(h2_tags)}: {h2_tags}"
    assert "Видео" in h2_tags[0]
    assert "API WB" in h2_tags[1]
    assert "файлы" in h2_tags[2]
    assert "В панели" in h2_tags[3]
    assert "компьютере" in h2_tags[4]
    assert "ошибок сверки" in h2_tags[5]


def test_instrukciya_images_are_served():
    r = client.get("/instrukciya")
    for name in ("api_token", "files_archive", "files_finreport"):
        assert f"/static/ins/{name}.png" in r.text
        assert client.get(f"/static/ins/{name}.png").status_code == 200


def test_template_no_forbidden_chars():
    with open("chzkiz/web/templates/instrukciya.html", encoding="utf-8") as f:
        content = f.read()
    forbidden = re.findall(r"[ёЁ–—«»]", content)
    assert not forbidden, f"forbidden characters found: {set(forbidden)}"