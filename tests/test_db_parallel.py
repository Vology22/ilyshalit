"""База под параллельной нагрузкой: WAL, ожидание блокировки, схема один раз на файл."""
import threading

from chzkiz import db


def test_wal_i_ozhidanie_vklyucheny():
    with db.connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 30000


def test_shema_ne_proveryaetsya_na_kazhdom_soedinenii(monkeypatch):
    with db.connect():
        pass
    calls = []
    real = db.init_schema
    monkeypatch.setattr(db, "init_schema", lambda conn: (calls.append(1), real(conn)))
    for _ in range(5):
        with db.connect() as conn:
            conn.execute("SELECT 1 FROM users").fetchall()
    assert calls == []


def test_paralelnye_zapisi_bez_database_is_locked():
    errors = []

    def worker(n):
        try:
            for i in range(20):
                with db.connect() as conn:
                    user = db.get_or_create_user(conn, 50_000 + n * 100 + i)
                    db.create_login_token(conn, user["id"])
                    conn.execute("SELECT COUNT(*) FROM users").fetchone()
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 12 * 20
