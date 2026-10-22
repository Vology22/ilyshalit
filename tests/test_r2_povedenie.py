"""Поведение, которое раньше проверялось только поиском строк в коде."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from chzkiz import config, wb_api
from tests.test_v4_chek import META, _chek, _fin, _order

APP_JS = Path(config.__file__).parent / "web" / "static" / "app.js"

HARNESS = r"""
const vm = require('vm');
const calls = [];
const boxes = {
  output: ['O1', 'O2'].map(mk),
  'return': ['R1'].map(mk),
};
function mk(v) {
  const tag = {textContent: ''};
  return {value: v, checked: true, disabled: false, getAttribute: () => null,
          closest: () => ({querySelector: () => tag})};
}
const status = {textContent: '', before: () => {}};
const ctx = {
  console, JSON, Promise, Array, Object, parseInt, setInterval: () => 0, clearInterval: () => {},
  setTimeout: () => 0, clearTimeout: () => {},
  confirm: () => true,
  document: {
    getElementById: (id) => (id === 'status-block' ? status : null),
    createElement: () => ({before: () => {}}),
    addEventListener: () => {},
    querySelectorAll: (sel) => boxes[/data-group="(\w+)"/.exec(sel)[1]],
  },
  fetch: (url, opts) => {
    const body = JSON.parse(opts.body);
    calls.push({url: url, body: body});
    return Promise.resolve({ok: true, json: () => Promise.resolve(
      {doc_id: 1, chz_type: 'LK_RECEIPT', product_document: 'doc', note: ''})});
  },
};
ctx.window = ctx;
ctx.window.chzSign = {signDetachedBase64: () => Promise.resolve('sig')};
vm.createContext(ctx);
vm.runInContext(process.argv[1], ctx);
vm.runInContext(`
  chzRenderCertSelect = function () { return Promise.resolve({value: 'cert'}); };
  chzPollStatus = function () {};
  step4Init(1, false, 2, 1, false);
`, ctx);
const tick = () => new Promise((ok) => setImmediate(ok));
(async () => {
  await tick();
  ctx.runBoth();
  for (let i = 0; i < 10; i++) { await tick(); }
  console.log(JSON.stringify(calls));
})();
"""


def _zapros_knopki():
    node = shutil.which("node")
    if not node:
        pytest.skip("нет node")
    js = APP_JS.read_text(encoding="utf-8")
    out = subprocess.run([node, "-e", HARNESS, js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr[-400:]
    return [c["body"] for c in json.loads(out.stdout) if c["url"].endswith("/build")]


def test_obshchaya_knopka_shlet_vyvod_potom_vozvrat_so_svoimi_kodami():
    builds = _zapros_knopki()
    assert [(b["doc_type"], b["codes"]) for b in builds] == [
        ("output", ["O1", "O2"]),
        ("return", ["R1"]),
    ]


def test_oplata_tolko_u_vozvrata_a_u_vyvoda_polya_net():
    builds = {b["doc_type"]: b for b in _zapros_knopki()}
    assert builds["return"]["paid"] is False
    assert "paid" not in builds["output"]


def test_chek_pereprodazhi_sleduyushchego_dnya_daet_vozvrat():
    fin = [
        _fin("s1", "Продажа", "2026-09-20T10:00:00Z", shk=7),
        _fin("s1", "Возврат", "2026-09-21T10:00:00Z", shk=7),
        _fin("s2", "Продажа", "2026-09-25T10:00:00Z", shk=7),
        _fin("s2", "Возврат", "2026-09-25T11:00:00Z", shk=7),
    ]
    items = wb_api.sobrat_itogi(
        [_order(1, "s1")], {}, {1: META}, fin,
        [_chek("2026-09-26T00:00:00", "s2")], "2026-09-09", "2026-10-09",
    )
    assert len(items) == 1
    assert items[0]["op"] == wb_api.OP_RETURN
