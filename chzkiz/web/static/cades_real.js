// Настоящая подпись через плагин КриптоПро CAdES Browser Plug-in (cadesplugin_api.js).
// Интерфейс совпадает с mock_sign.js: listCertificates/signAttached/signDetachedBase64.

var CADESCOM_CADES_BES = 1;
var CADESCOM_BASE64_TO_BINARY = 1;
var CAPICOM_CURRENT_USER_STORE = 2;
var CAPICOM_MY_STORE = "My";
var CAPICOM_STORE_OPEN_MAXIMUM_ALLOWED = 2;
var CADESCOM_CONTAINER_STORE = 100;
var CAPICOM_CERTIFICATE_FIND_SHA1_HASH = 0;

// cadesplugin до загрузки расширения - промис, без ожидания CreateObjectAsync падает
function chzPluginReady() {
  if (typeof window.cadesplugin === "undefined") {
    return Promise.reject(new Error("плагин КриптоПро не найден в браузере"));
  }
  return Promise.resolve(window.cadesplugin);
}

function* chzOpenStore(kind) {
  var oStore = yield cadesplugin.CreateObjectAsync("CAdESCOM.Store");
  if (kind === "container") {
    // сертификат с токена, не установленный в Личное: так его видят ЧЗ и другие площадки
    yield oStore.Open(CADESCOM_CONTAINER_STORE);
  } else {
    yield oStore.Open(CAPICOM_CURRENT_USER_STORE, CAPICOM_MY_STORE, CAPICOM_STORE_OPEN_MAXIMUM_ALLOWED);
  }
  return oStore;
}

function chzFindCert(thumbprint) {
  return chzPluginReady().then(function () {
    return cadesplugin.async_spawn(function* () {
      var kinds = ["my", "container"];
      for (var k = 0; k < kinds.length; k++) {
        var oStore;
        try {
          oStore = yield* chzOpenStore(kinds[k]);
        } catch (e) {
          continue;
        }
        var certs = yield oStore.Certificates;
        var found = yield certs.Find(CAPICOM_CERTIFICATE_FIND_SHA1_HASH, thumbprint);
        var n = yield found.Count;
        if (n > 0) {
          var cert = yield found.Item(1);
          return { cert: cert, store: oStore };
        }
        yield oStore.Close();
      }
      throw new Error("сертификат не найден, обновите страницу");
    });
  });
}

function chzSignWith(thumbprint, content, detached) {
  return chzFindCert(thumbprint).then(function (res) {
    return cadesplugin.async_spawn(function* () {
      try {
        var oSigner = yield cadesplugin.CreateObjectAsync("CAdESCOM.CPSigner");
        yield oSigner.propset_Certificate(res.cert);
        var oSignedData = yield cadesplugin.CreateObjectAsync("CAdESCOM.CadesSignedData");
        yield oSignedData.propset_ContentEncoding(CADESCOM_BASE64_TO_BINARY);
        yield oSignedData.propset_Content(content);
        return yield oSignedData.SignCades(oSigner, CADESCOM_CADES_BES, detached);
      } finally {
        yield res.store.Close();
      }
    });
  });
}

window.chzSign = {
  listCertificates: function () {
    return chzPluginReady().then(function () {
      return cadesplugin.async_spawn(function* () {
        var out = [];
        var seen = {};
        var kinds = ["my", "container"];
        var lastError = null;
        for (var k = 0; k < kinds.length; k++) {
          var oStore;
          try {
            oStore = yield* chzOpenStore(kinds[k]);
          } catch (e) {
            lastError = e;
            continue;
          }
          try {
            var certs = yield oStore.Certificates;
            var count = yield certs.Count;
            for (var i = 1; i <= count; i++) {
              var cert = yield certs.Item(i);
              var thumb = yield cert.Thumbprint;
              if (seen[thumb]) {
                continue;
              }
              seen[thumb] = true;
              var owner = yield cert.SubjectName;
              out.push({
                id: thumb,
                owner: owner,
                inn: chzExtractInn(owner),
                valid_to: yield cert.ValidToDate
              });
            }
          } catch (e) {
            lastError = e;
          } finally {
            yield oStore.Close();
          }
        }
        if (!out.length && lastError) {
          throw lastError;
        }
        return out;
      });
    });
  },

  signAttached: function (certId, data) {
    return chzSignWith(certId, btoa(data), false);
  },

  signDetachedBase64: function (certId, contentBase64) {
    return chzSignWith(certId, contentBase64, true);
  }
};
