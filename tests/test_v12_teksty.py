from fastapi.testclient import TestClient

from chzkiz.bot import screens
from chzkiz.web.app import create_app

client = TestClient(create_app())

OLD = ("Маркетплейс → Сборочные задания", "месяц это два файла", "двумя файлами")


def _captions():
    return [cap for _, cap, _ in screens.files_screens()]


def test_files_path_new():
    assert "Поставки и заказы" in screens.FILES_PATH
    assert "Маркетплейс (FBS)" in screens.FILES_PATH


def test_captions_new():
    cap1, cap2 = _captions()
    assert "Поставки и заказы" in cap1
    assert "Маркетплейс (FBS)" in cap1
    assert "Основной и По выкупам" in cap2


def test_no_old_text():
    page = client.get("/instrukciya").text
    for text in _captions() + [page]:
        for old in OLD:
            assert old not in text


def test_captions_fit_telegram_limit():
    for cap in _captions():
        assert len(cap) <= 1024


def test_instrukciya_new_text():
    page = client.get("/instrukciya").text
    assert "Поставки и заказы" in page
    assert "Маркетплейс (FBS)" in page
    assert "Основной</strong> и <strong>По выкупам" in page
    assert "8-10 файлов" in page
