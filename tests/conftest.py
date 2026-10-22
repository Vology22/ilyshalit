import os
import shutil
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

_TMP = tempfile.mkdtemp(prefix="chzkiz_test_")
os.environ["CHZ_MODE"] = "mock"
os.environ["FERNET_KEY"] = Fernet.generate_key().decode("ascii")
os.environ["PUBLIC_URL"] = "http://testserver"
os.environ["CHZ_KIZ_DB"] = str(Path(_TMP) / "test.db")
os.environ["CHZ_KIZ_REPORTS"] = str(Path(_TMP) / "reports")
os.environ["BOT_TOKEN"] = ""

import pytest  # noqa: E402

from chzkiz import chz_mock  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_state():
    chz_mock.reset_state()
    db_path = Path(os.environ["CHZ_KIZ_DB"])
    # В WAL рядом с базой лежат -wal и -shm: чужой -wal от старой базы испортил бы новую.
    for p in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if p.exists():
            p.unlink()
    yield
    chz_mock.reset_state()


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_TMP, ignore_errors=True)
