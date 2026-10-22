import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from chzkiz import config

APP_JS = Path(config.__file__).parent / "web" / "static" / "app.js"


def _kodov(numbers):
    node = shutil.which("node")
    if not node:
        pytest.skip("нет node")
    js = APP_JS.read_text(encoding="utf-8")
    body = re.search(r"function chzKodov\(n\) \{.*?\n\}\n", js, re.S).group(0)
    code = body + "console.log(JSON.stringify(%s.map(chzKodov)));" % json.dumps(numbers)
    out = subprocess.run([node, "-e", code], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr[-400:]
    return json.loads(out.stdout)


def test_kodov_sklonyaetsya():
    assert _kodov([1, 2, 4, 5, 11, 12, 14, 21, 22, 25, 101, 110, 111, 112]) == [
        "1 код", "2 кода", "4 кода", "5 кодов", "11 кодов", "12 кодов", "14 кодов",
        "21 код", "22 кода", "25 кодов", "101 код", "110 кодов", "111 кодов", "112 кодов"]


def test_okno_podtverzhdeniya_sklonyaet_chislo():
    js = APP_JS.read_text(encoding="utf-8")
    assert "' кодов.'" not in js
    assert "chzKodov(payload.codes.length)" in js
