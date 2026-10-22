import time

from chzkiz import config, db


def test_login_token_consumed_once():
    with db.connect() as conn:
        user = db.get_or_create_user(conn, 111)
        token = db.create_login_token(conn, user["id"])
        assert db.consume_login_token(conn, token) == user["id"]
        assert db.consume_login_token(conn, token) is None


def test_login_token_unknown_returns_none():
    with db.connect() as conn:
        assert db.consume_login_token(conn, "no-such-token") is None


def test_login_token_expired(monkeypatch):
    with db.connect() as conn:
        user = db.get_or_create_user(conn, 112)
        token = db.create_login_token(conn, user["id"])
        conn.execute(
            "UPDATE login_tokens SET expires_at = ? WHERE token = ?",
            (time.time() - 1, token),
        )
        conn.commit()
        assert db.consume_login_token(conn, token) is None


def test_login_token_ttl_honors_config(monkeypatch):
    monkeypatch.setattr(config, "LOGIN_LINK_TTL_MIN", 1)
    with db.connect() as conn:
        user = db.get_or_create_user(conn, 116)
        before = time.time()
        token = db.create_login_token(conn, user["id"])
        row = conn.execute(
            "SELECT expires_at FROM login_tokens WHERE token = ?", (token,)
        ).fetchone()
        assert 0 < row["expires_at"] - before <= 61, "срок ссылки не совпадает с LOGIN_LINK_TTL_MIN"
        assert db.consume_login_token(conn, token) == user["id"], "ссылка в пределах TTL должна работать"


def test_session_created_and_read():
    with db.connect() as conn:
        user = db.get_or_create_user(conn, 113)
        session_id = db.create_session(conn, user["id"])
        assert db.get_session_user_id(conn, session_id) == user["id"]


def test_session_expired_returns_none():
    with db.connect() as conn:
        user = db.get_or_create_user(conn, 114)
        session_id = db.create_session(conn, user["id"])
        conn.execute(
            "UPDATE sessions SET expires_at = ? WHERE session_id = ?",
            (time.time() - 1, session_id),
        )
        conn.commit()
        assert db.get_session_user_id(conn, session_id) is None
