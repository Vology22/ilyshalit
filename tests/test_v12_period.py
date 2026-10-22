from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chzkiz.web.app import create_app
from tests.test_regress_2 import _login

TEMPLATE = Path(__file__).resolve().parent.parent / "chzkiz" / "web" / "templates" / "step1.html"


def test_knopki_perioda_na_shage_1():
    c = TestClient(create_app())
    _login(c, 9801)
    html = c.get("/step1").text
    for days, title in ((30, "Последний месяц"), (92, "Три месяца")):
        m = re.search(r'<button[^>]*data-days="%d"[^>]*>%s</button>' % (days, title), html)
        assert m, title
        assert 'type="button"' in m.group(0)
        assert re.search(r'data-end="\d{4}-\d{2}-\d{2}"', m.group(0))


@pytest.mark.skipif(shutil.which("node") is None, reason="node не установлен")
@pytest.mark.parametrize(
    "end,days,expected",
    [
        ("2026-10-12", 30, "2026-09-13"),
        ("2026-10-12", 92, "2026-07-13"),
        ("2026-03-01", 30, "2026-01-31"),
        ("2024-03-01", 30, "2024-02-01"),
        ("2026-01-15", 30, "2025-12-17"),
    ],
)
def test_raschet_nachala_perioda(end, days, expected):
    text = TEMPLATE.read_text(encoding="utf-8")
    func = re.search(r"function periodStart\(.*?\n\}\n", text, re.S).group(0)
    script = func + "console.log(periodStart(%s, %d));" % (json.dumps(end), days)
    out = subprocess.run(
        [shutil.which("node"), "-e", script], capture_output=True, text=True, timeout=30
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == expected
