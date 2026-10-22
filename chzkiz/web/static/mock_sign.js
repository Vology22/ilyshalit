// Поддельная подпись для демо-режима (CHZ_MODE=mock). Совпадает по интерфейсу с cades_real.js:
// подпись строки X - это base64("MOCKSIG:" + X), проверяется тем же способом на chz_mock.py.
window.chzSign = {
  listCertificates: function () {
    return fetch('/mock-chz/certs').then(function (r) { return r.json(); });
  },
  signAttached: function (certId, data) {
    return Promise.resolve(btoa('MOCKSIG:' + data));
  },
  signDetachedBase64: function (certId, contentBase64) {
    return Promise.resolve(btoa('MOCKSIG:' + contentBase64));
  }
};
