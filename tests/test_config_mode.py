"""Без явного CHZ_MODE сервис не стартует; cookie сессии Secure при HTTPS."""
import pytest
from fastapi.testclient import TestClient

from chzkiz import config, db
from chzkiz.web import app as app_mod


@pytest.mark.parametrize("mode", ["", "  ", "prod", "Real1"])
def test_bez_rezhima_ne_startuem(monkeypatch, mode):
    monkeypatch.setattr(config, "CHZ_MODE", mode)
    with pytest.raises(RuntimeError, match="CHZ_MODE"):
        app_mod.create_app()


@pytest.mark.parametrize("mode", ["mock", "real", " Real "])
def test_yavnyj_rezhim_prinimaetsya(monkeypatch, mode):
    monkeypatch.setattr(config, "CHZ_MODE", mode)
    config.check_mode()


def _login_cookie_header(monkeypatch, public_url: str) -> str:
    monkeypatch.setattr(config, "PUBLIC_URL", public_url)
    client = TestClient(app_mod.create_app())
    with db.connect() as conn:
        user = db.get_or_create_user(conn, 777)
        token = db.create_login_token(conn, user["id"])
    r = client.post(f"/l/{token}", follow_redirects=False)
    assert r.status_code == 303
    return r.headers["set-cookie"].lower()


def test_cookie_secure_pri_https(monkeypatch):
    h = _login_cookie_header(monkeypatch, "https://kiz.example")
    assert "secure" in h
    assert "httponly" in h
    assert "samesite=lax" in h


def test_cookie_bez_secure_pri_http(monkeypatch):
    h = _login_cookie_header(monkeypatch, "http://127.0.0.1:8140")
    assert "secure" not in h
    assert "httponly" in h
