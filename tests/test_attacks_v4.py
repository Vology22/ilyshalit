"""Атаки на правки 10.10 (v4): чек с srid продажи, кнопка "вывести и вернуть", шаг 1 и шаг 3."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

from chzkiz import wb_api
from tests.test_v4_chek import _chek, _fin as _finrow, _itogi, _order
from tests.test_v4_shag1 import _api_klient, _api_run, _warnings

STATIC = Path(__file__).resolve().parent.parent / "chzkiz" / "web" / "static"

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');
const scenario = process.argv[3];
function El(id) { this.id = id; this._t = ''; this.disabled = false; this.hidden = false; this.value = '';
  this.attrs = {}; this.className = ''; this.style = {}; this.dataset = {}; this.focused = false;
  this.classList = { add() {}, remove() {}, contains() { return false; } }; }
El.prototype.getAttribute = function (k) { return k in this.attrs ? this.attrs[k] : null; };
El.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
El.prototype.before = function () {}; El.prototype.appendChild = function () {};
El.prototype.addEventListener = function () {}; El.prototype.focus = function () { this.focused = true; };
for (const p of ['textContent', 'innerHTML']) {
  Object.defineProperty(El.prototype, p, { get() { return this._t; }, set(v) { this._t = String(v); } });
}
const els = {};
['status-block', 'cert-box', 'btn-both', 'btn-output', 'btn-return', 'fias', 'paid', 'paid-doc', 'primary-type',
 'primary-number', 'primary-date', 'primary-name', 'sel-output', 'sel-return', 'done-output', 'done-return']
  .forEach(function (id) { els[id] = new El(id); });
els.paid.value = 'no';
els.fias.value = '0c5b2444-70a0-4932-980c-b4dc0d3f02b5';
els['btn-both'].disabled = true;
function box(group, value) {
  const tag = new El('tag');
  const row = new El('row'); row.querySelector = function () { return tag; };
  return { value: value, checked: true, disabled: false, group: group, closest: function () { return row; } };
}
const boxes = [box('output', 'o1'), box('output', 'o2'), box('return', 'r1')];
const created = [];
global.window = global;
global.document = {
  getElementById: function (id) { return els[id] || null; },
  createElement: function () { const e = new El('new'); created.push(e); return e; },
  addEventListener: function () {},
  querySelectorAll: function (sel) {
    const g = /data-group="(\w+)"/.exec(sel)[1];
    const picked = sel.indexOf(':checked') >= 0;
    return boxes.filter(function (b) { return b.group === g && (!picked || (b.checked && !b.disabled)); });
  }
};
global.confirm = function () { return true; };
const intervals = [];
global.setInterval = function (fn) { intervals.push(fn); return intervals.length; };
global.clearInterval = function (id) { if (id) { intervals[id - 1] = null; } };
const calls = [];
let outSent = false;
let retBuildFails = scenario !== 'note';
function reply(status, body) {
  return Promise.resolve({ ok: status < 400, status: status, json: function () { return Promise.resolve(body); } });
}
global.fetch = function (url, opts) {
  const body = opts && opts.body ? JSON.parse(opts.body) : null;
  if (/\/build$/.test(url)) {
    calls.push('build:' + body.doc_type);
    if (body.doc_type === 'output') {
      if (outSent) { return reply(409, { detail: 'Документ этого типа сейчас обрабатывается в Честном знаке, обновите шаг 4 позже.' }); }
      return reply(200, { doc_id: 1, chz_type: 'LK_RECEIPT', product_document: 'AAA',
        note: scenario === 'note' ? 'Без цены в отчете, в документ не вошли: 1, первый 0104.' : '' });
    }
    if (retBuildFails) { return reply(400, { detail: 'Заполните номер и дату документа.' }); }
    return reply(200, { doc_id: 2, chz_type: 'LK_RETURN', product_document: 'BBB', note: '' });
  }
  if (/\/send$/.test(url)) {
    calls.push('send:' + body.chz_type);
    if (body.chz_type === 'LK_RECEIPT') { outSent = true; }
    return reply(200, { ok: true });
  }
  calls.push('status');
  const st = { output: {}, 'return': {} };
  if (outSent) { st.output = { o1: { state: 'processing' }, o2: { state: 'processing' } }; }
  return reply(200, { documents: outSent ? [{ doc_type: 'LK_RECEIPT', status: 'IN_PROGRESS', count: 2 }] : [],
    done: false, states: st, done_counts: {} });
};
global.window.chzSign = { signDetachedBase64: function () { return Promise.resolve('SIG'); } };
vm.runInThisContext(src);
chzRenderCertSelect = function () { return Promise.resolve({ value: 'cert1' }); };
function flush() { return new Promise(function (r) { setTimeout(r, 20); }); }
function snap() {
  return { status: els['status-block']._t, both: els['btn-both']._t, bothDisabled: els['btn-both'].disabled,
    outDisabled: els['btn-output'].disabled, retDisabled: els['btn-return'].disabled,
    note: created.length ? created[0]._t : '', calls: calls.slice(),
    outBoxesOpen: boxes.filter(function (b) { return b.group === 'output' && b.checked && !b.disabled; }).length };
}
(async function () {
  const res = {};
  step4Init('RUN', true, 2, 1, false);
  await flush();
  runBoth();
  await flush();
  res.posle = snap();
  if (scenario === 'povtor') {
    runBoth();
    await flush();
    res.povtor = snap();
  }
  if (scenario === 'opros') {
    intervals.forEach(function (fn) { if (fn) { fn(); } });
    await flush();
    res.opros = snap();
  }
  console.log(JSON.stringify(res));
})().catch(function (e) { console.error(e && e.stack || e); process.exit(1); });
"""


def _node(scenario: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("нет node")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "harness.js"
        path.write_text(HARNESS, encoding="utf-8")
        out = subprocess.run([node, str(path), str(STATIC / "app.js"), scenario],
                             capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr[-800:]
    return json.loads(out.stdout)


def test_chek_staroy_prodazhi_ne_delaet_prodannym_novoe_zadanie_v_puti():
    """Продажа и возврат через минуты по заданию 1, чек той продажи днем позже, код уехал
    новому покупателю заданием 2, по которому в финотчете еще пусто."""
    fin = [
        _finrow("s1", "Продажа", "2026-09-20T10:00:00Z", shk=7),
        _finrow("s1", "Возврат", "2026-09-20T10:07:00Z", shk=7),
    ]
    orders = [_order(1, "s1"), _order(2, "s2", "2026-09-24T10:00:00Z")]
    items = _itogi(orders, fin, [_chek("2026-09-21T00:00:00", "s1")])
    assert len(items) == 1
    assert items[0].get("istochnik") != "чек", items[0]
    assert items[0].get("reason") == "not_sold_yet", items[0]


def test_api_konec_perioda_v_budushchem_ne_obhodit_minimum_30_dney(monkeypatch):
    zaprosy: list = []
    c = _api_klient(monkeypatch, 9811, zaprosy)
    segodnya = date.today()
    run_id = _api_run(c, segodnya, segodnya + timedelta(days=29))
    nachalo = segodnya - timedelta(days=29)
    granica = wb_api.msk_day_unix(nachalo.isoformat()) - wb_api.LOOKBACK_DAYS * 86400
    assert min(a for a, _ in zaprosy) <= granica, _warnings(run_id)


def test_posle_sboya_vozvrata_prichina_ne_stiraetsya_pervym_oprosom():
    res = _node("opros")
    assert "возврат не ушел" in res["posle"]["status"]
    assert "возврат не ушел" in res["opros"]["status"] + res["opros"]["note"], res["opros"]


def test_posle_sboya_vozvrata_povtornoe_nazhatie_ne_shlet_vyvod_snova():
    res = _node("povtor")
    assert res["povtor"]["calls"].count("build:output") == 1, res["povtor"]
    assert "Вывод не ушел" not in res["povtor"]["status"], res["povtor"]["status"]


def test_zametka_vyvoda_o_propushchennyh_kodah_ne_stiraetsya_vozvratom():
    res = _node("note")
    assert res["posle"]["calls"].count("send:LK_RETURN") == 1
    assert "не вошли" in res["posle"]["note"], res["posle"]
