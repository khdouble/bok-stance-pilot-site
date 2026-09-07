(function () {
  "use strict";

  var ADMIN_ID = /^PI-[A-Z0-9]{12}$/;
  var ADMIN_PASSWORD = /^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$/;
  var INSTRUMENT_SHA = /^[0-9a-f]{64}$/;
  var EXPECTED_API_URL =
    "https://mebisrsvasrzwkmsodsw.supabase.co/functions/v1/pilot-api";

  function invalidCredentials() {
    return new Error("PI manual-test credentials are invalid");
  }

  function validateCredentials(adminId, adminPassword) {
    if (
      typeof adminId !== "string" ||
      typeof adminPassword !== "string" ||
      !ADMIN_ID.test(adminId) ||
      !ADMIN_PASSWORD.test(adminPassword)
    ) {
      throw invalidCredentials();
    }
    return Object.freeze({
      admin_id: adminId,
      admin_password: adminPassword
    });
  }

  function checkedCredentials(credentials) {
    if (!credentials || typeof credentials !== "object") {
      throw invalidCredentials();
    }
    return validateCredentials(
      credentials.admin_id,
      credentials.admin_password
    );
  }

  function checkedInstrumentSha(instrumentSha256) {
    if (
      typeof instrumentSha256 !== "string" ||
      !INSTRUMENT_SHA.test(instrumentSha256)
    ) {
      throw new Error("PI manual-test instrument is invalid");
    }
    return instrumentSha256;
  }

  function configIsPinned(config) {
    return Boolean(
      config &&
      typeof config === "object" &&
      config.apiUrl === EXPECTED_API_URL
    );
  }

  function createSessionLifecycle() {
    var epoch = 0;
    var controllers = new Set();

    function capture() {
      return epoch;
    }

    function isCurrent(candidate) {
      return Number.isSafeInteger(candidate) && candidate === epoch;
    }

    function track(candidate, controller) {
      if (
        !isCurrent(candidate) ||
        !controller ||
        typeof controller.abort !== "function"
      ) {
        throw new Error("PI manual-test session is unavailable");
      }
      controllers.add(controller);
      var released = false;
      return function () {
        if (!released) {
          controllers.delete(controller);
          released = true;
        }
      };
    }

    function invalidate() {
      epoch += 1;
      controllers.forEach(function (controller) {
        try {
          controller.abort();
        } catch (error) {
          // Continue invalidating every tracked request.
        }
      });
      controllers.clear();
    }

    return Object.freeze({
      capture: capture,
      invalidate: invalidate,
      isCurrent: isCurrent,
      track: track
    });
  }

  function makeLoadRequest(credentials, instrumentSha256) {
    var checked = checkedCredentials(credentials);
    return Object.freeze({
      action: "admin_load",
      admin_id: checked.admin_id,
      admin_password: checked.admin_password,
      instrument_sha256: checkedInstrumentSha(instrumentSha256)
    });
  }

  function makeSubmitRequest(credentials, submission) {
    var checked = checkedCredentials(credentials);
    if (
      !submission ||
      typeof submission !== "object" ||
      Array.isArray(submission)
    ) {
      throw new Error("PI manual-test submission is invalid");
    }
    ["action", "admin_id", "admin_password", "invite_token"].forEach(
      function (forbidden) {
        if (Object.prototype.hasOwnProperty.call(submission, forbidden)) {
          throw new Error("PI manual-test submission is invalid");
        }
      }
    );
    var request = {
      action: "admin_submit",
      admin_id: checked.admin_id,
      admin_password: checked.admin_password
    };
    Object.keys(submission).forEach(function (key) {
      request[key] = submission[key];
    });
    return Object.freeze(request);
  }

  window.PILOT_ADMIN_BRIDGE = Object.freeze({
    configIsPinned: configIsPinned,
    createSessionLifecycle: createSessionLifecycle,
    makeLoadRequest: makeLoadRequest,
    makeSubmitRequest: makeSubmitRequest,
    validateCredentials: validateCredentials
  });
})();
