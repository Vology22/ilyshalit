from __future__ import annotations

import re
from pathlib import Path

from tests.test_v2_a2 import _run

STATIC = Path(__file__).resolve().parent.parent / "chzkiz" / "web" / "static"


def test_stranica_shaga_4_otdaet_glavnuyu_i_dve_vtorostepennye_knopki():
    c, run_id = _run(9751, sales=[1, 2], returns=[5])
    page = c.get(f"/step4/{run_id}").text
    for el in ('id="btn-both"', 'id="btn-output"', 'id="btn-return"', 'id="preview-link"'):
        assert page.count(el) == 1, el
    assert "runBoth()" in page and "runDoc('output')" in page and "runDoc('return')" in page
    assert page.index('id="preview-link"') < page.index('id="btn-both"') < page.index('id="btn-output"') < page.index('id="btn-return"')
    assert "Только вывести 2" in page and "Только вернуть 1" in page
    assert re.search(r'id="btn-both"[^>]*disabled', page)


def test_skript_shaga_4_pereispolzuet_odin_put_dokumenta():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert js.count("'/build'") == 1 and js.count("'/send'") == 1
    assert "window.runBoth" in js and "Возврат не запускался" in js
    assert "' и вернуть '" in js
