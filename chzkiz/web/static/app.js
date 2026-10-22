// Общий JS панели: список сертификатов, шаг 2 (вход), шаг 4 (вывод/возврат + опрос статуса).

// ИНН ЮЛ (10 цифр, разные площадки пишут по-разному) идет раньше личного ИНН подписанта
// в той же строке DN - смотрим его первым, ИП свой ИНН (12 цифр) кладет только во второй OID.
function chzExtractInn(owner) {
  var m = /(?:ИНН ЮЛ|INNLE|OID\.1\.2\.643\.100\.4)\s*=\s*(\d{10})/i.exec(owner || '');
  if (m) { return m[1]; }
  m = /(?:ИНН|INN|OID\.1\.2\.643\.3\.131\.1\.1)\s*=\s*(\d{12}|\d{10})/i.exec(owner || '');
  return m ? m[1] : '';
}

function chzPostJson(url, body) {
  return fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {})
  }).then(function (r) {
    if (!r.ok) {
      return r.json().catch(function () { return {}; }).then(function (e) {
        throw new Error(e.detail || ('Ошибка ' + r.status));
      });
    }
    return r.json();
  });
}

var CHZ_NO_PLUGIN = 'Не найден КриптоПро ЭЦП Browser plug-in. Установите плагин и расширение для браузера, затем обновите страницу.';
var CHZ_PLUGIN_URL = 'https://www.cryptopro.ru/products/cades/plugin';

// плагин отклоняет промис голой строкой, у нее нет .message
function chzErrText(e) {
  if (typeof e === 'string') { return e; }
  return e && e.message ? e.message : '';
}

function chzCertFail(container, e) {
  var text = chzErrText(e);
  if (!text || /плагин|plug-?in|cadesplugin|ожидания/i.test(text)) {
    container.innerHTML = '';
    var msg = document.createElement('p');
    msg.className = 'plugin-missing';
    msg.appendChild(document.createTextNode(CHZ_NO_PLUGIN + ' '));
    var a = document.createElement('a');
    a.href = CHZ_PLUGIN_URL;
    a.target = '_blank';
    a.rel = 'noopener';
    a.textContent = CHZ_PLUGIN_URL;
    msg.appendChild(a);
    container.appendChild(msg);
    return;
  }
  container.textContent = 'Не смог получить список сертификатов: ' + text;
}

function chzRenderCertSelect(container) {
  container.innerHTML = 'Загружаю список сертификатов...';
  return window.chzSign.listCertificates().then(function (certs) {
    if (!certs.length) {
      container.innerHTML = 'Сертификаты не найдены.';
      return null;
    }
    var now = new Date();
    var select = document.createElement('select');
    select.className = 'cert-select';
    var hasValid = false;
    certs.forEach(function (c) {
      var inn = c.inn || chzExtractInn(c.owner);
      var opt = document.createElement('option');
      opt.value = c.id;
      opt.dataset.owner = c.owner;
      opt.dataset.inn = inn;
      var validTo = c.valid_to ? new Date(c.valid_to) : null;
      var expired = validTo && validTo < now;
      var validStr = c.valid_to || '---';
      opt.textContent = c.owner + (inn ? ' (ИНН ' + inn + ')' : '') + ', срок до ' + validStr;
      if (expired) {
        opt.disabled = true;
        opt.textContent += ' (срок истек)';
        opt.style.color = '#999';
      } else {
        hasValid = true;
      }
      select.appendChild(opt);
    });
    if (!hasValid) {
      container.innerHTML = 'Нет действующих сертификатов.';
      return null;
    }
    var info = document.createElement('div');
    info.className = 'cert-info';
    function showInfo() {
      var o = select.options[select.selectedIndex];
      info.textContent = o ? o.textContent : '';
    }
    select.addEventListener('change', showInfo);
    container.innerHTML = '';
    container.appendChild(select);
    container.appendChild(info);
    showInfo();
    return select;
  }).catch(function (e) {
    chzCertFail(container, e);
    return null;
  });
}

function step2Init(runId, isMock) {
  var listEl = document.getElementById('cert-list');
  var btn = document.getElementById('login-btn');
  var errEl = document.getElementById('login-error');
  var helpBlock = document.getElementById('help-block');
  var retryBtn = document.getElementById('retry-certs-btn');
  var selectEl = null;
  var loadDone = false;
  var CERT_LOAD_TIMEOUT = 25000;

  function showHelp() {
    if (helpBlock) {
      helpBlock.style.display = '';
    }
  }

  function hideHelp() {
    if (helpBlock) {
      helpBlock.style.display = 'none';
    }
  }

  function doLoadCerts() {
    hideHelp();
    loadDone = false;
    chzRenderCertSelect(listEl).then(function (select) {
      loadDone = true;
      selectEl = select;
      btn.disabled = !select;
      if (!select && helpBlock && !isMock) {
        showHelp();
      }
    });
  }

  if (!isMock && helpBlock) {
    var timeoutId = setTimeout(function () {
      if (!loadDone) {
        showHelp();
      }
    }, CERT_LOAD_TIMEOUT);
  }

  doLoadCerts();

  if (!isMock && retryBtn) {
    retryBtn.addEventListener('click', function () {
      doLoadCerts();
    });
  }

  btn.addEventListener('click', function () {
    errEl.textContent = '';
    btn.disabled = true;
    var certId = selectEl.value;
    var opt = selectEl.options[selectEl.selectedIndex];
    var owner = opt.dataset.owner;
    var inn = opt.dataset.inn || chzExtractInn(owner);
    if (!inn) {
      errEl.textContent = 'Не нашел ИНН в сертификате, проверка владельца будет отключена. Проверьте сертификат.';
      btn.disabled = false;
      return;
    }

    chzPostJson('/step2/' + runId + '/auth-key', {})
      .then(function (r) {
        return window.chzSign.signAttached(certId, r.data).then(function (sig) {
          return chzPostJson('/step2/' + runId + '/signin', {
            uuid: r.uuid, signature: sig, inn: inn, owner: owner
          });
        });
      })
      .then(function () {
        window.location.href = '/step3/' + runId;
      })
      .catch(function (e) {
        errEl.textContent = 'Не вышло войти: ' + e.message;
        btn.disabled = false;
      });
  });
}

function chzKodov(n) {
  var a = n % 10;
  var b = n % 100;
  if (a === 1 && b !== 11) { return n + ' код'; }
  if (a >= 2 && a <= 4 && (b < 12 || b > 14)) { return n + ' кода'; }
  return n + ' кодов';
}

var CHZ_GROUPS = {
  output: { btn: 'btn-output', verb: 'Вывести', only: 'Только вывести', done: 'Выведено', rowDone: 'выведен', ask: 'Вывод из оборота' },
  'return': { btn: 'btn-return', verb: 'Вернуть', only: 'Только вернуть', done: 'Возвращено', rowDone: 'возвращен', ask: 'Возврат в оборот' }
};
var chzBusy = { output: false, 'return': false };
var chzChain = false;

function chzBoxes(group, onlyPicked) {
  var sel = 'input.pick[data-group="' + group + '"]' + (onlyPicked ? ':checked:not(:disabled)' : '');
  return Array.prototype.slice.call(document.querySelectorAll(sel));
}

function chzRefreshGroup(group) {
  var cfg = CHZ_GROUPS[group];
  var btn = document.getElementById(cfg.btn);
  if (!btn) { return; }
  var n = chzBoxes(group, true).length;
  var counter = document.getElementById('sel-' + group);
  if (counter) { counter.textContent = n; }
  var done = parseInt(btn.getAttribute('data-done') || '0', 10);
  btn.classList.remove('is-done');
  if (chzBusy[group]) {
    btn.disabled = true;
    btn.textContent = 'Идет обработка...';
  } else if (n > 0) {
    btn.disabled = false;
    btn.textContent = cfg.only + ' ' + n;
  } else if (done > 0) {
    btn.disabled = true;
    btn.textContent = cfg.done + ' ' + done;
    btn.classList.add('is-done');
  } else {
    btn.disabled = true;
    btn.textContent = cfg.only + ' 0';
  }
  if (chzChain) { btn.disabled = true; }
  chzRefreshBoth();
}

function chzRefreshBoth() {
  var btn = document.getElementById('btn-both');
  if (!btn) { return; }
  var o = chzBoxes('output', true).length;
  var r = chzBoxes('return', true).length;
  var busy = chzChain || (o > 0 && chzBusy.output) || (r > 0 && chzBusy['return']);
  if (chzChain) {
    btn.textContent = 'Идет отправка...';
  } else if (o > 0 && r > 0) {
    btn.textContent = 'Вывести ' + o + ' и вернуть ' + r;
  } else if (o > 0) {
    btn.textContent = 'Вывести ' + o;
  } else if (r > 0) {
    btn.textContent = 'Вернуть ' + r;
  } else {
    btn.textContent = 'Вывести и вернуть';
  }
  btn.disabled = busy || (o + r === 0);
}

// раскладка по кодам из ответа status: принятые отключаем, ошибки подписываем причиной
function chzApplyStates(data) {
  ['output', 'return'].forEach(function (group) {
    var states = (data.states || {})[group] || {};
    chzBoxes(group, false).forEach(function (box) {
      var st = states[box.value];
      if (!st) { return; }
      var row = box.closest('label');
      var tag = row.querySelector('.tag');
      row.className = 'code-row ' + st.state;
      if (st.state === 'done') {
        box.checked = false;
        box.disabled = true;
        tag.textContent = CHZ_GROUPS[group].rowDone;
      } else if (st.state === 'processing') {
        box.disabled = true;
        tag.textContent = 'в обработке';
      } else {
        box.disabled = false;
        tag.textContent = 'ошибка: ' + (st.reason || 'Честный знак не принял код');
      }
    });
    var btn = document.getElementById(CHZ_GROUPS[group].btn);
    var done = (data.done_counts || {})[group] || 0;
    if (btn) { btn.setAttribute('data-done', done); }
    var line = document.getElementById('done-' + group);
    if (line) {
      line.hidden = !done;
      line.innerHTML = CHZ_GROUPS[group].done + ': <b>' + done + '</b>';
    }
    chzBusy[group] = Object.keys(states).some(function (k) { return states[k].state === 'processing'; });
    chzRefreshGroup(group);
  });
}

function step4Init(runId, isMock, outputCount, returnCount, hasDocs) {
  var statusBlock = document.getElementById('status-block');
  var selectEl = null;
  var certBox = document.getElementById('cert-box');
  if (!certBox) {
    certBox = document.createElement('div');
    statusBlock.before(certBox);
  }
  var noteEl = document.createElement('p');
  noteEl.className = 'hint';
  statusBlock.before(noteEl);
  chzRenderCertSelect(certBox).then(function (select) { selectEl = select; });

  document.addEventListener('change', function (ev) {
    if (ev.target.classList && ev.target.classList.contains('pick')) {
      chzRefreshGroup(ev.target.getAttribute('data-group'));
    }
  });
  document.addEventListener('click', function (ev) {
    var t = ev.target;
    var mode = t.getAttribute && t.getAttribute('data-pick');
    if (!mode) { return; }
    var group = t.getAttribute('data-group');
    chzBoxes(group, false).forEach(function (box) { if (!box.disabled) { box.checked = mode === 'all'; } });
    chzRefreshGroup(group);
  });
  chzRefreshGroup('output');
  chzRefreshGroup('return');

  function prepareDoc(docType) {
    var codes = chzBoxes(docType, true).map(function (box) { return box.value; });
    if (!codes.length) {
      statusBlock.textContent = 'Отметьте хотя бы один код.';
      return null;
    }
    var payload = { doc_type: docType, codes: codes };
    var fiasEl = document.getElementById('fias');
    if (docType === 'output') {
      if (fiasEl && fiasEl.value.trim()) { payload.fias = fiasEl.value.trim(); }
    }
    if (docType === 'return') { payload.paid = false; }
    return payload;
  }

  function askText(docType, payload) {
    return CHZ_GROUPS[docType].ask + ': ' + chzKodov(payload.codes.length) + '.';
  }

  function sendDoc(certId, payload, keepNote) {
    return chzPostJson('/step4/' + runId + '/build', payload)
      .then(function (r) {
        var note = r.note || '';
        noteEl.textContent = keepNote && noteEl.textContent ? noteEl.textContent + (note ? ' ' + note : '') : note;
        return window.chzSign.signDetachedBase64(certId, r.product_document).then(function (sig) {
          return chzPostJson('/step4/' + runId + '/send', {
            doc_id: r.doc_id, chz_type: r.chz_type,
            product_document: r.product_document, signature: sig
          });
        });
      });
  }

  function lockSent(group, codes) {
    chzBoxes(group, false).forEach(function (box) {
      if (codes.indexOf(box.value) < 0) { return; }
      box.disabled = true;
      var tag = box.closest('label').querySelector('.tag');
      if (tag) { tag.textContent = 'в обработке'; }
    });
  }

  function noCert() {
    if (selectEl) { return false; }
    statusBlock.textContent = 'Нет сертификата для подписи. Обновите страницу или вернитесь на шаг 2.';
    return true;
  }

  window.runDoc = function (docType) {
    if (noCert()) { return; }
    var payload = prepareDoc(docType);
    if (!payload) { return; }
    var fiasEl = document.getElementById('fias');
    if (!confirm(askText(docType, payload) + ' Отменить после отправки нельзя. Продолжить?')) { return; }
    var certId = selectEl.value;
    chzBusy[docType] = true;
    chzRefreshGroup(docType);
    noteEl.textContent = '';
    statusBlock.textContent = 'Собираю документ...';
    sendDoc(certId, payload)
      .then(function () {
        statusBlock.textContent = 'Отправлено, жду ответа Честного знака...';
        chzPollStatus(runId, statusBlock, true);
      })
      .catch(function (e) {
        statusBlock.textContent = 'Ошибка: ' + chzErrText(e);
        chzBusy[docType] = false;
        chzRefreshGroup(docType);
        if (fiasEl && /ФИАС/.test(chzErrText(e))) { fiasEl.focus(); }
      });
  };

  window.runBoth = function () {
    var hasOut = chzBoxes('output', true).length > 0;
    var hasRet = chzBoxes('return', true).length > 0;
    if (hasOut && !hasRet) { window.runDoc('output'); return; }
    if (hasRet && !hasOut) { window.runDoc('return'); return; }
    if (noCert()) { return; }
    var outPayload = prepareDoc('output');
    if (!outPayload) { return; }
    var retPayload = prepareDoc('return');
    if (!retPayload) { return; }
    var fiasEl = document.getElementById('fias');
    if (!confirm(askText('output', outPayload) + ' ' + askText('return', retPayload) +
        ' Сначала уйдет вывод, потом возврат. Отменить после отправки нельзя. Продолжить?')) { return; }
    var certId = selectEl.value;
    chzChain = true;
    chzBusy.output = true;
    chzRefreshGroup('output');
    chzRefreshGroup('return');
    noteEl.textContent = '';
    statusBlock.textContent = 'Собираю документ вывода...';
    function finish() {
      chzChain = false;
      chzBusy.output = false;
      chzBusy['return'] = false;
      chzRefreshGroup('output');
      chzRefreshGroup('return');
    }
    sendDoc(certId, outPayload)
      .then(function () {
        lockSent('output', outPayload.codes);
        statusBlock.textContent = 'Вывод отправлен. Собираю документ возврата...';
        return sendDoc(certId, retPayload, true).then(function () {
          finish();
          statusBlock.textContent = 'Вывод и возврат отправлены, жду ответа Честного знака...';
          chzPollStatus(runId, statusBlock, true);
        }, function (e) {
          finish();
          var why = 'Вывод отправлен, а возврат не ушел: ' + chzErrText(e);
          statusBlock.textContent = why;
          noteEl.textContent = (noteEl.textContent ? noteEl.textContent + ' ' : '') + why;
          chzPollStatus(runId, statusBlock, false);
        });
      }, function (e) {
        finish();
        statusBlock.textContent = 'Вывод не ушел: ' + chzErrText(e).replace(/[.\s]+$/, '') + '. Возврат не запускался.';
        if (fiasEl && /ФИАС/.test(chzErrText(e))) { fiasEl.focus(); }
      });
  };
  if (hasDocs) { chzPollStatus(runId, statusBlock, true); }
}

var CHZ_DOC_NAMES = {LK_RECEIPT: 'Вывод из оборота', LK_RETURN: 'Возврат в оборот', LP_RETURN: 'Возврат в оборот'};
var CHZ_DOC_STATUS = {CHECKED_OK: 'принят, обработан успешно', CHECKED_NOT_OK: 'отклонен', PROCESSING_ERROR: 'ошибка обработки',
  IN_PROGRESS: 'в обработке', WAIT_ACCEPTANCE: 'ждет приемки', ACCEPTED: 'принят', REJECTED: 'отклонен', UNDEFINED: 'ждем ответа',
  NEW: 'собран, не отправлен'};

function chzEsc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

var CHZ_WAIT_NOTE = 'Честный знак принял документ и проверяет его. Обычно это от нескольких секунд до 10 минут, ' +
  'иногда дольше. Повторно отправлять не нужно. Не закрывайте страницу: статус обновится здесь сам.';
function chzPollStatus(runId, el, immediate) {
  var elapsed = 0;
  var limit = 30 * 60 * 1000;
  var step = 20 * 1000;
  var timer = null;
  function tick() {
    fetch('/step4/' + runId + '/status').then(function (r) { return r.json(); }).then(function (data) {
      var lines = data.documents.map(function (d) {
        return (CHZ_DOC_NAMES[d.doc_type] || d.doc_type) + ': ' + (CHZ_DOC_STATUS[d.status] || d.status) +
          (d.count ? ', кодов ' + d.count : '') + (d.errors ? ' (' + chzEsc(d.errors) + ')' : '') +
          (d.explain ? '<br><b>Что это значит:</b> ' + chzEsc(d.explain) : '');
      });
      var waiting = data.documents.some(function (d) { return d.status === 'IN_PROGRESS'; });
      el.innerHTML = lines.join('<br>') + (waiting ? '<br><span class="hint">' + CHZ_WAIT_NOTE + '</span>' : '');
      chzApplyStates(data);
      if (data.done) {
        clearInterval(timer);
        el.innerHTML += '<br><a href="/report/' + runId + '">Скачать отчет</a>';
      } else if (elapsed >= limit) {
        clearInterval(timer);
        el.innerHTML += '<br>Долго нет ответа, проверьте позже - отчет появится на этой ссылке.';
      }
    });
  }
  timer = setInterval(function () {
    elapsed += step;
    tick();
  }, step);
  if (immediate) { tick(); }
}
