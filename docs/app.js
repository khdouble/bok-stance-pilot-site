(function () {
  "use strict";

  var CONFIG = window.PILOT_SITE_CONFIG;
  var CONTRACT = window.PILOT_SUBMISSION_CONTRACT;
  var EXPECTED_INSTRUMENT_SHA = window.PILOT_INSTRUMENT_SHA256;
  var instrument = null;
  var inviteToken = "";
  var assignmentCode = "";
  var identity = { name: "", phone: "" };
  var state = null;
  var activeStartedAt = null;
  var activeAssignmentId = null;
  var submitFailures = 0;
  var DRAFT_TTL_MS = 7 * 24 * 60 * 60 * 1000;

  function byId(id) {
    return document.getElementById(id);
  }

  function nowIso() {
    return new Date().toISOString();
  }

  function showOnly(panelId) {
    [
      "blockedPanel",
      "invitePanel",
      "identityPanel",
      "surveyPanel",
      "feedbackPanel",
      "reviewPanel",
      "donePanel"
    ].forEach(function (id) {
      byId(id).hidden = id !== panelId;
    });
  }

  function setStatus(message, kind) {
    var node = byId("globalStatus");
    node.textContent = message;
    node.className = "status " + (kind || "info");
  }

  function failClosed(message) {
    setStatus("설문을 시작할 수 없습니다.", "error");
    byId("blockedMessage").textContent = message;
    showOnly("blockedPanel");
  }

  function isLocalPreview() {
    var hostAllowed =
      window.location.hostname === "127.0.0.1" ||
      window.location.hostname === "localhost";
    return (
      hostAllowed &&
      new URLSearchParams(window.location.search).get("preview") === "1"
    );
  }

  function isLocalAutoTest() {
    return (
      isLocalPreview() &&
      new URLSearchParams(window.location.search).get("autotest") === "1"
    );
  }

  function meaningfulConfigText(value, minimum, maximum) {
    return (
      typeof value === "string" &&
      value === value.trim() &&
      value.length >= minimum &&
      value.length <= maximum &&
      !/(PENDING|TBD|TODO|PLACEHOLDER|EXAMPLE|미정|추후|예시|테스트)/i.test(
        value
      ) &&
      !/[\u0000-\u001f]/.test(value)
    );
  }

  function validCalendarDate(value, requireNotPast) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) {
      return false;
    }
    var parsed = new Date(value + "T00:00:00Z");
    if (
      !Number.isFinite(parsed.getTime()) ||
      parsed.toISOString().slice(0, 10) !== value
    ) {
      return false;
    }
    return !requireNotPast || value >= new Date().toISOString().slice(0, 10);
  }

  function configReady() {
    var versionMatch =
      typeof CONFIG.privacyNoticeVersion === "string"
        ? CONFIG.privacyNoticeVersion.match(
            /^consent-v(\d{4}-\d{2}-\d{2})-r[1-9]\d*$/
          )
        : null;
    var email =
      typeof CONFIG.contactEmail === "string" ? CONFIG.contactEmail : "";
    var emailDomain = email.includes("@")
      ? email.slice(email.lastIndexOf("@") + 1).toLowerCase()
      : "";
    var regions = [
      "ap-northeast-1",
      "ap-northeast-2",
      "ap-south-1",
      "ap-southeast-1",
      "ap-southeast-2",
      "ap-southeast-3",
      "ap-southeast-4",
      "ca-central-1",
      "eu-central-1",
      "eu-central-2",
      "eu-north-1",
      "eu-south-1",
      "eu-south-2",
      "eu-west-1",
      "eu-west-2",
      "eu-west-3",
      "sa-east-1",
      "us-east-1",
      "us-east-2",
      "us-west-1",
      "us-west-2"
    ];
    var verifiedAt =
      typeof CONFIG.remoteE2eVerifiedAt === "string"
        ? CONFIG.remoteE2eVerifiedAt
        : "";
    var withdrawalMatch =
      typeof CONFIG.withdrawalProcedureVersion === "string"
        ? CONFIG.withdrawalProcedureVersion.match(
            /^withdrawal-v(\d{4}-\d{2}-\d{2})-r[1-9]\d*$/
          )
        : null;
    var verifiedMillis = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(
      verifiedAt
    )
      ? Date.parse(verifiedAt)
      : NaN;
    var verifiedDate = Number.isFinite(verifiedMillis)
      ? new Date(verifiedMillis).toISOString().slice(0, 10)
      : "";
    var ethicsPrefix =
      typeof CONFIG.ethicsDisposition === "string"
        ? CONFIG.ethicsDisposition + ":"
        : "";
    var ethicsSuffix =
      typeof CONFIG.ethicsReference === "string" &&
      CONFIG.ethicsReference.startsWith(ethicsPrefix)
        ? CONFIG.ethicsReference.slice(ethicsPrefix.length)
        : "";

    return (
      versionMatch !== null &&
      validCalendarDate(versionMatch[1], false) &&
      meaningfulConfigText(CONFIG.dataController, 2, 100) &&
      /[A-Za-z가-힣]/.test(CONFIG.dataController) &&
      email.length <= 254 &&
      /^[A-Za-z0-9.!#$%&'*+/=?^_{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$/.test(
        email
      ) &&
      !/(\.invalid|\.example|\.test)$/.test(emailDomain) &&
      emailDomain !== "example.com" &&
      meaningfulConfigText(CONFIG.retentionNotice, 12, 300) &&
      validCalendarDate(CONFIG.retentionEndDate, true) &&
      CONFIG.retentionNotice.includes(CONFIG.retentionEndDate) &&
      /(파기|삭제)/.test(CONFIG.retentionNotice) &&
      regions.includes(CONFIG.dataRegion) &&
      ["approved", "exempt", "not_required"].includes(
        CONFIG.ethicsDisposition
      ) &&
      meaningfulConfigText(ethicsSuffix, 8, 160) &&
      /[A-Za-z가-힣]/.test(ethicsSuffix) &&
      /\d/.test(ethicsSuffix) &&
      withdrawalMatch !== null &&
      validCalendarDate(withdrawalMatch[1], false) &&
      Number.isFinite(verifiedMillis) &&
      new Date(verifiedMillis).toISOString().replace(".000Z", "Z") ===
        verifiedAt &&
      verifiedMillis <= Date.now() &&
      verifiedDate >= versionMatch[1] &&
      verifiedDate >= withdrawalMatch[1] &&
      CONFIG.identityPurpose ===
        "사전 지정 참가자의 응답자료 구별과 제출자료 확인"
    );
  }

  async function loadInstrument() {
    var response = await fetch("instrument.json", {
      cache: "no-store",
      credentials: "omit"
    });
    if (!response.ok) {
      throw new Error("instrument file unavailable");
    }
    var loaded = await response.json();
    var declaredHash = loaded.instrument_sha256;
    var payload = {};
    Object.keys(loaded).forEach(function (key) {
      if (key !== "instrument_sha256") {
        payload[key] = loaded[key];
      }
    });
    var calculatedHash = await CONTRACT.sha256Hex(
      CONTRACT.stableStringify(payload)
    );
    if (
      !declaredHash ||
      declaredHash !== calculatedHash ||
      declaredHash !== EXPECTED_INSTRUMENT_SHA ||
      loaded.hosted_version !== CONFIG.hostedVersion ||
      loaded.source_offline_instrument_sha256 !==
        CONFIG.sourceOfflineInstrumentSha256
    ) {
      throw new Error("instrument integrity mismatch");
    }
    return loaded;
  }

  function extractInviteFromFragment() {
    var fragment = window.location.hash.startsWith("#")
      ? window.location.hash.slice(1)
      : window.location.hash;
    var token = new URLSearchParams(fragment).get("invite") || "";
    if (token) {
      window.history.replaceState(
        null,
        document.title,
        window.location.pathname + window.location.search
      );
      return token;
    }
    return "";
  }

  function validInviteToken(token) {
    return /^[A-Za-z0-9_-]{43}$/.test(token);
  }

  async function apiRequest(body) {
    var controller = new AbortController();
    var timer = window.setTimeout(function () {
      controller.abort();
    }, CONFIG.requestTimeoutMs);
    try {
      var response = await fetch(CONFIG.apiUrl, {
        method: "POST",
        mode: "cors",
        credentials: "omit",
        cache: "no-store",
        redirect: "error",
        referrerPolicy: "no-referrer",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: controller.signal
      });
      var result = {};
      try {
        result = await response.json();
      } catch (error) {
        result = {};
      }
      if (!response.ok || result.ok === false) {
        var requestError = new Error("request rejected");
        requestError.status = response.status;
        requestError.code =
          (result.error && result.error.code) ||
          result.code ||
          "REQUEST_REJECTED";
        requestError.serverMessage =
          (result.error && result.error.message) || "";
        throw requestError;
      }
      return result;
    } finally {
      window.clearTimeout(timer);
    }
  }

  function assignmentFor(code) {
    var assigned = instrument.assignments[code];
    if (!Array.isArray(assigned) || assigned.length !== 12) {
      throw new Error("assignment unavailable");
    }
    return assigned;
  }

  function sameServerItems(serverItems, localItems) {
    if (!Array.isArray(serverItems)) {
      return false;
    }
    if (serverItems.length !== localItems.length) {
      return false;
    }
    return serverItems.every(function (item, index) {
      var expected = localItems[index];
      return (
        Number(item.display_position) === Number(expected.display_position) &&
        item.pilot_item_id === expected.pilot_item_id &&
        item.sentence_text === expected.sentence_text
      );
    });
  }

  async function verifyInvite(token) {
    if (!validInviteToken(token)) {
      throw new Error("invalid invite");
    }
    if (isLocalPreview()) {
      return {
        ok: true,
        assignment_code: "PILOT_R01",
        items: assignmentFor("PILOT_R01")
      };
    }
    return apiRequest({
      action: "load",
      invite_token: token,
      instrument_sha256: instrument.instrument_sha256
    });
  }

  function storageKey() {
    return (
      "bok_stance_hosted_pilot_" +
      instrument.instrument_sha256 +
      "_" +
      assignmentCode
    );
  }

  function purgeExpiredDrafts() {
    var prefix = "bok_stance_hosted_pilot_";
    try {
      for (var index = window.localStorage.length - 1; index >= 0; index -= 1) {
        var key = window.localStorage.key(index);
        if (!key || !key.startsWith(prefix)) {
          continue;
        }
        var stored = JSON.parse(window.localStorage.getItem(key));
        if (
          !stored ||
          typeof stored.saved_at !== "string" ||
          !Number.isFinite(Date.parse(stored.saved_at)) ||
          Date.now() - Date.parse(stored.saved_at) > DRAFT_TTL_MS
        ) {
          window.localStorage.removeItem(key);
        }
      }
    } catch (error) {
      return;
    }
  }

  function emptyAnswer(item) {
    return {
      assignment_id: item.assignment_id,
      display_position: item.display_position,
      pilot_item_id: item.pilot_item_id,
      choice: null,
      reason_code: "",
      confidence: null,
      reason_note: "",
      started_at: null,
      finished_at: null,
      active_duration_seconds: 0
    };
  }

  function freshState() {
    var answers = {};
    assignmentFor(assignmentCode).forEach(function (item) {
      answers[item.assignment_id] = emptyAnswer(item);
    });
    return {
      schema_version: "1.0",
      hosted_version: CONFIG.hostedVersion,
      instrument_sha256: instrument.instrument_sha256,
      assignment_code: assignmentCode,
      current_index: 0,
      session_started_at: null,
      consent_accepted_at: null,
      idempotency_key: crypto.randomUUID(),
      finalized_submission: null,
      answers: answers,
      feedback: {
        fatigue_1to5: null,
        zero_vs_99_explanation: "",
        change_vs_stance_explanation: "",
        ui_error_note: ""
      }
    };
  }

  function stateShapeValid(candidate) {
    if (
      !candidate ||
      candidate.instrument_sha256 !== instrument.instrument_sha256 ||
      candidate.hosted_version !== CONFIG.hostedVersion ||
      candidate.assignment_code !== assignmentCode ||
      typeof candidate.answers !== "object" ||
      !candidate.feedback ||
      !(
        candidate.finalized_submission === undefined ||
        candidate.finalized_submission === null ||
        typeof candidate.finalized_submission === "object"
      ) ||
      !Number.isInteger(candidate.current_index) ||
      candidate.current_index < 0 ||
      candidate.current_index > 11
    ) {
      return false;
    }
    var items = assignmentFor(assignmentCode);
    return items.every(function (item) {
      var answer = candidate.answers[item.assignment_id];
      return (
        answer &&
        answer.assignment_id === item.assignment_id &&
        Number(answer.display_position) === Number(item.display_position) &&
        answer.pilot_item_id === item.pilot_item_id
      );
    });
  }

  function restoreState() {
    try {
      var raw = window.localStorage.getItem(storageKey());
      if (!raw) {
        return freshState();
      }
      var stored = JSON.parse(raw);
      if (
        !stored ||
        typeof stored.saved_at !== "string" ||
        !stored.state ||
        !Number.isFinite(Date.parse(stored.saved_at)) ||
        Date.now() - Date.parse(stored.saved_at) > DRAFT_TTL_MS
      ) {
        window.localStorage.removeItem(storageKey());
        return freshState();
      }
      var candidate = stored.state;
      if (!stateShapeValid(candidate)) {
        window.localStorage.removeItem(storageKey());
        return freshState();
      }
      candidate.finalized_submission = candidate.finalized_submission || null;
      return candidate;
    } catch (error) {
      return freshState();
    }
  }

  function saveState() {
    if (!state) {
      return;
    }
    try {
      window.localStorage.setItem(
        storageKey(),
        JSON.stringify({ saved_at: nowIso(), state: state })
      );
      byId("clearDraft").hidden = false;
    } catch (error) {
      setStatus(
        "이 브라우저에서 임시저장을 사용할 수 없습니다. 창을 닫지 마세요.",
        "error"
      );
    }
  }

  function currentItem() {
    return assignmentFor(assignmentCode)[state.current_index];
  }

  function currentAnswer() {
    return state.answers[currentItem().assignment_id];
  }

  function stopActiveTimer() {
    if (activeStartedAt === null || !activeAssignmentId || !state) {
      return;
    }
    var elapsed = Math.max(0, (performance.now() - activeStartedAt) / 1000);
    var answer = state.answers[activeAssignmentId];
    if (answer) {
      answer.active_duration_seconds =
        Number(answer.active_duration_seconds || 0) + elapsed;
      answer.finished_at = nowIso();
    }
    activeStartedAt = null;
    activeAssignmentId = null;
    saveState();
  }

  function startActiveTimer() {
    if (
      !state ||
      document.hidden ||
      byId("surveyPanel").hidden ||
      activeStartedAt !== null
    ) {
      return;
    }
    var answer = currentAnswer();
    if (!answer.started_at) {
      answer.started_at = nowIso();
    }
    activeAssignmentId = answer.assignment_id;
    activeStartedAt = performance.now();
  }

  function renderStanceChoices() {
    var container = byId("stanceChoices");
    container.replaceChildren();
    instrument.stance_choices.forEach(function (choice) {
      var label = document.createElement("label");
      label.className = "choice";
      var input = document.createElement("input");
      input.type = "radio";
      input.name = "stance";
      input.value = String(choice.value);
      var text = document.createElement("span");
      text.textContent = choice.label;
      label.appendChild(input);
      label.appendChild(text);
      container.appendChild(label);
    });
  }

  function renderItem() {
    stopActiveTimer();
    var item = currentItem();
    var answer = currentAnswer();
    byId("progressText").textContent =
      "문항 " + (state.current_index + 1) + " / 12";
    byId("progressBar").style.width =
      String(((state.current_index + 1) / 12) * 100) + "%";
    byId("sentenceText").textContent = item.sentence_text;
    document.querySelectorAll('input[name="stance"]').forEach(function (node) {
      node.checked =
        answer.choice !== null && Number(node.value) === Number(answer.choice);
    });
    byId("confidence").value =
      answer.confidence === null ? "" : String(answer.confidence);
    byId("reasonCode").value = answer.reason_code || "";
    byId("reasonNote").value = answer.reason_note || "";
    byId("reasonNote").disabled = answer.reason_code !== "OTHER";
    byId("previousItem").disabled = state.current_index === 0;
    byId("nextItem").textContent =
      state.current_index === 11 ? "피드백으로" : "다음";
    byId("itemError").textContent = "";
    showOnly("surveyPanel");
    startActiveTimer();
  }

  function captureCurrentAnswer() {
    var answer = currentAnswer();
    var selected = document.querySelector('input[name="stance"]:checked');
    answer.choice = selected ? Number(selected.value) : null;
    answer.confidence = byId("confidence").value
      ? Number(byId("confidence").value)
      : null;
    answer.reason_code = byId("reasonCode").value;
    answer.reason_note = byId("reasonNote").value.trim();
    saveState();
  }

  function validateAnswer(answer) {
    if (![-2, -1, 0, 1, 2, 99].includes(answer.choice)) {
      return "정책기조를 선택해 주세요.";
    }
    if (![1, 2, 3, 4, 5].includes(answer.confidence)) {
      return "확신도를 선택해 주세요.";
    }
    if (
      answer.choice === 99 &&
      (!answer.reason_code || answer.reason_code === "NONE")
    ) {
      return "99를 선택한 경우 NONE 이외의 이유 코드를 선택해 주세요.";
    }
    if (answer.reason_code === "OTHER" && !answer.reason_note) {
      return "OTHER를 선택한 경우 이유 메모를 입력해 주세요.";
    }
    if (answer.reason_code !== "OTHER" && answer.reason_note) {
      return "이유 메모는 OTHER를 선택한 경우에만 입력해 주세요.";
    }
    return "";
  }

  function completedCount() {
    return assignmentFor(assignmentCode).filter(function (item) {
      return validateAnswer(state.answers[item.assignment_id]) === "";
    }).length;
  }

  function renderFeedback() {
    stopActiveTimer();
    byId("fatigue").value = state.feedback.fatigue_1to5
      ? String(state.feedback.fatigue_1to5)
      : "";
    byId("zeroVs99").value = state.feedback.zero_vs_99_explanation || "";
    byId("changeVsStance").value =
      state.feedback.change_vs_stance_explanation || "";
    byId("uiError").value = state.feedback.ui_error_note || "";
    byId("feedbackError").textContent = "";
    showOnly("feedbackPanel");
  }

  function captureFeedback() {
    state.feedback.fatigue_1to5 = byId("fatigue").value
      ? Number(byId("fatigue").value)
      : null;
    state.feedback.zero_vs_99_explanation = byId("zeroVs99").value.trim();
    state.feedback.change_vs_stance_explanation =
      byId("changeVsStance").value.trim();
    state.feedback.ui_error_note = byId("uiError").value.trim();
    saveState();
  }

  function validateFeedback() {
    if (![1, 2, 3, 4, 5].includes(state.feedback.fatigue_1to5)) {
      return "피로도를 선택해 주세요.";
    }
    if (!state.feedback.zero_vs_99_explanation) {
      return "0과 99의 구분 기준을 입력해 주세요.";
    }
    if (!state.feedback.change_vs_stance_explanation) {
      return "정책변화와 기조 유지의 구분 기준을 입력해 주세요.";
    }
    return "";
  }

  function renderReview() {
    byId("reviewRater").textContent = assignmentCode;
    byId("reviewCount").textContent = completedCount() + " / 12";
    byId("reviewInstrument").textContent = instrument.instrument_sha256;
    byId("submitError").textContent = "";
    byId("fallbackArea").hidden = true;
    byId("editFeedback").disabled = Boolean(state.finalized_submission);
    showOnly("reviewPanel");
  }

  function totalActiveSeconds() {
    return assignmentFor(assignmentCode).reduce(function (total, item) {
      return (
        total +
        Number(state.answers[item.assignment_id].active_duration_seconds || 0)
      );
    }, 0);
  }

  function responsePayload() {
    return assignmentFor(assignmentCode).map(function (item) {
      var answer = state.answers[item.assignment_id];
      return {
        display_position: Number(item.display_position),
        pilot_item_id: item.pilot_item_id,
        choice: Number(answer.choice),
        reason_code: answer.reason_code || "NONE",
        confidence: Number(answer.confidence),
        reason_note: answer.reason_code === "OTHER" ? answer.reason_note : "",
        started_at: answer.started_at,
        finished_at: answer.finished_at,
        active_duration_seconds: Math.floor(
          Math.max(0, Number(answer.active_duration_seconds))
        )
      };
    });
  }

  function researchTextContainsIdentity() {
    var notes = responsePayload()
      .map(function (response) {
        return response.reason_note;
      })
      .concat([
        state.feedback.zero_vs_99_explanation,
        state.feedback.change_vs_stance_explanation,
        state.feedback.ui_error_note
      ])
      .join("\n");
    notes = notes.normalize("NFC");
    var compactNotes = notes.replace(/\s/g, "");
    var compactName = identity.name.normalize("NFC").replace(/\s/g, "");
    var noteDigits = notes.replace(/\D/g, "");
    var phoneDigits = identity.phone.replace(/\D/g, "");
    return (
      (compactName.length >= 2 && compactNotes.includes(compactName)) ||
      (phoneDigits.length >= 8 && noteDigits.includes(phoneDigits))
    );
  }

  async function finalizedSubmissionPayload() {
    if (state.finalized_submission) {
      var existing = state.finalized_submission;
      var recalculated = await CONTRACT.finalizeDigest(existing.basis);
      if (
        recalculated.payload_sha256 === existing.payload_sha256 &&
        recalculated.basis.instrument_sha256 === instrument.instrument_sha256
      ) {
        return {
          basis: recalculated.basis,
          payload_sha256: recalculated.payload_sha256
        };
      }
      state.finalized_submission = null;
      state.idempotency_key = crypto.randomUUID();
    }

    var responses = responsePayload();
    var integerItemTotal = responses.reduce(function (total, response) {
      return total + response.active_duration_seconds;
    }, 0);
    var rawBasis = {
      consent: {
        accepted: true,
        version: CONFIG.privacyNoticeVersion,
        accepted_at: state.consent_accepted_at
      },
      feedback: {
        fatigue_1to5: state.feedback.fatigue_1to5,
        zero_vs_99_explanation: state.feedback.zero_vs_99_explanation,
        change_vs_stance_explanation:
          state.feedback.change_vs_stance_explanation,
        ui_error_note: state.feedback.ui_error_note
      },
      instrument_sha256: instrument.instrument_sha256,
      responses: responses,
      session: {
        started_at: state.session_started_at,
        finished_at: nowIso(),
        active_duration_seconds: Math.max(
          1,
          Math.floor(totalActiveSeconds()),
          integerItemTotal
        )
      }
    };
    var finalized = await CONTRACT.finalizeDigest(rawBasis);
    state.finalized_submission = finalized;
    saveState();
    return finalized;
  }

  function clearPrivateInputs() {
    identity = { name: "", phone: "" };
    byId("participantName").value = "";
    byId("participantPhone").value = "";
    byId("consentAccepted").checked = false;
  }

  async function submitSurvey() {
    var button = byId("submitSurvey");
    if (researchTextContainsIdentity()) {
      byId("submitError").textContent =
        "응답 메모나 피드백에 성명 또는 전화번호를 반복 입력하지 마세요.";
      setStatus("개인정보가 포함된 자유서술을 수정해 주세요.", "error");
      return;
    }
    button.disabled = true;
    byId("editFeedback").disabled = true;
    byId("submitError").textContent = "";
    setStatus("암호화된 연결로 제출하고 있습니다.", "info");
    try {
      var finalized = await finalizedSubmissionPayload();
      var body = {
        action: "submit",
        invite_token: inviteToken,
        instrument_sha256: instrument.instrument_sha256,
        idempotency_key: state.idempotency_key,
        consent: finalized.basis.consent,
        identity: {
          name: identity.name,
          phone: identity.phone
        },
        session: finalized.basis.session,
        responses: finalized.basis.responses,
        feedback: finalized.basis.feedback,
        payload_sha256: finalized.payload_sha256
      };
      var result;
      if (isLocalPreview()) {
        result = {
          ok: true,
          receipt: { submission_id: "LOCAL_PREVIEW_NOT_SUBMITTED" }
        };
      } else {
        result = await apiRequest(body);
      }
      window.localStorage.removeItem(storageKey());
      clearPrivateInputs();
      byId("receiptId").textContent = result.receipt
        ? result.receipt.submission_id
        : result.receipt_id || "RECEIVED";
      setStatus("제출이 완료되었습니다.", "success");
      showOnly("donePanel");
    } catch (error) {
      submitFailures += 1;
      byId("submitError").textContent =
        "제출하지 못했습니다(" +
        (error.code || "NETWORK_ERROR") +
        "). 같은 버튼을 누르면 완전히 동일한 내용으로 재시도합니다.";
      byId("fallbackArea").hidden = submitFailures < 1;
      setStatus("응답은 이 브라우저에 임시 보관되어 있습니다.", "error");
      button.disabled = false;
    }
  }

  function csvSafe(value) {
    var text = value === null || value === undefined ? "" : String(value);
    if (/^\s*[=+\-@]/.test(text)) {
      text = "'" + text;
    }
    return '"' + text.replace(/"/g, '""') + '"';
  }

  function makeCsv(rows, columns) {
    var lines = [columns.map(csvSafe).join(",")];
    rows.forEach(function (row) {
      lines.push(
        columns
          .map(function (column) {
            return csvSafe(row[column]);
          })
          .join(",")
      );
    });
    return "\ufeff" + lines.join("\r\n") + "\r\n";
  }

  function download(filename, content) {
    var blob = new Blob([content], { type: "text/csv;charset=utf-8" });
    var url = URL.createObjectURL(blob);
    var link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }

  function fallbackResponseRows() {
    return assignmentFor(assignmentCode).map(function (item) {
      var answer = state.answers[item.assignment_id];
      var abstain = Number(answer.choice) === 99;
      return {
        assignment_id: item.assignment_id,
        pilot_rater_id: assignmentCode,
        display_position: item.display_position,
        pilot_item_id: item.pilot_item_id,
        sentence_text: item.sentence_text,
        label5: abstain ? "" : answer.choice,
        abstain: abstain ? "true" : "false",
        reason_code: answer.reason_code || "NONE",
        confidence: answer.confidence,
        reason_note: answer.reason_code === "OTHER" ? answer.reason_note : "",
        started_at: answer.started_at,
        finished_at: answer.finished_at,
        response_status: "COMPLETED",
        dataset_role: instrument.dataset_role,
        excluded_from_analysis: "true",
        analysis_exclusion_reason: instrument.analysis_exclusion_reason,
        active_duration_seconds: Math.floor(
          Math.max(0, Number(answer.active_duration_seconds))
        ),
        fieldwork_version: CONFIG.hostedVersion,
        instrument_sha256: instrument.instrument_sha256
      };
    });
  }

  function initializeEvents() {
    byId("verifyInvite").addEventListener("click", async function () {
      var token = byId("inviteCode").value.trim();
      await handleInvite(token);
      byId("inviteCode").value = "";
    });

    byId("beginSurvey").addEventListener("click", function () {
      var name = byId("participantName").value.trim();
      var phone = byId("participantPhone").value.replace(/\D/g, "");
      if (!name || name.length > 80) {
        setStatus("성명을 확인해 주세요.", "error");
        return;
      }
      if (!/^01[016789]\d{7,8}$/.test(phone)) {
        setStatus("휴대전화번호를 확인해 주세요.", "error");
        return;
      }
      if (!byId("consentAccepted").checked) {
        setStatus("개인정보 수집·이용 동의가 필요합니다.", "error");
        return;
      }
      identity = { name: name, phone: phone };
      state = restoreState();
      state.session_started_at = state.session_started_at || nowIso();
      state.consent_accepted_at = state.consent_accepted_at || nowIso();
      saveState();
      if (state.finalized_submission) {
        setStatus(
          "이전 제출 시도가 있습니다. 같은 내용으로 다시 제출할 수 있습니다.",
          "info"
        );
        renderReview();
      } else {
        setStatus(
          "응답은 이 기기에서 7일 동안만 복원 대상으로 임시저장됩니다.",
          "info"
        );
        renderItem();
      }
    });

    byId("stanceChoices").addEventListener("change", captureCurrentAnswer);
    byId("confidence").addEventListener("change", captureCurrentAnswer);
    byId("reasonCode").addEventListener("change", function () {
      var isOther = byId("reasonCode").value === "OTHER";
      byId("reasonNote").disabled = !isOther;
      if (!isOther) {
        byId("reasonNote").value = "";
      }
      captureCurrentAnswer();
    });
    byId("reasonNote").addEventListener("input", captureCurrentAnswer);

    byId("previousItem").addEventListener("click", function () {
      captureCurrentAnswer();
      stopActiveTimer();
      if (state.current_index > 0) {
        state.current_index -= 1;
        saveState();
        renderItem();
      }
    });

    byId("nextItem").addEventListener("click", function () {
      captureCurrentAnswer();
      var message = validateAnswer(currentAnswer());
      if (message) {
        byId("itemError").textContent = message;
        return;
      }
      stopActiveTimer();
      if (state.current_index < 11) {
        state.current_index += 1;
        saveState();
        renderItem();
      } else {
        renderFeedback();
      }
    });

    byId("backToItems").addEventListener("click", function () {
      captureFeedback();
      state.current_index = 11;
      saveState();
      renderItem();
    });

    byId("reviewSurvey").addEventListener("click", function () {
      captureFeedback();
      var feedbackMessage = validateFeedback();
      if (feedbackMessage) {
        byId("feedbackError").textContent = feedbackMessage;
        return;
      }
      if (completedCount() !== 12) {
        byId("feedbackError").textContent =
          "완료되지 않은 문항이 있습니다. 문항 화면으로 돌아가 확인해 주세요.";
        return;
      }
      renderReview();
    });

    byId("editFeedback").addEventListener("click", renderFeedback);
    byId("submitSurvey").addEventListener("click", submitSurvey);
    byId("clearDraft").addEventListener("click", function () {
      if (
        !window.confirm(
          "이 기기에 임시저장된 응답을 삭제하고 처음부터 다시 시작할까요?"
        )
      ) {
        return;
      }
      stopActiveTimer();
      if (state) {
        window.localStorage.removeItem(storageKey());
      }
      state = null;
      submitFailures = 0;
      clearPrivateInputs();
      byId("clearDraft").hidden = true;
      setStatus("이 기기의 임시저장 응답을 삭제했습니다.", "success");
      showOnly("identityPanel");
    });

    byId("downloadResponses").addEventListener("click", function () {
      var columns = [
        "assignment_id",
        "pilot_rater_id",
        "display_position",
        "pilot_item_id",
        "sentence_text",
        "label5",
        "abstain",
        "reason_code",
        "confidence",
        "reason_note",
        "started_at",
        "finished_at",
        "response_status",
        "dataset_role",
        "excluded_from_analysis",
        "analysis_exclusion_reason",
        "active_duration_seconds",
        "fieldwork_version",
        "instrument_sha256"
      ];
      download(
        assignmentCode + "_hosted_responses.csv",
        makeCsv(fallbackResponseRows(), columns)
      );
    });

    byId("downloadFeedback").addEventListener("click", function () {
      var submittedAt = nowIso();
      var row = {
        pilot_rater_id: assignmentCode,
        fatigue_1to5: state.feedback.fatigue_1to5,
        zero_vs_99_explanation: state.feedback.zero_vs_99_explanation,
        change_vs_stance_explanation:
          state.feedback.change_vs_stance_explanation,
        ui_error_note: state.feedback.ui_error_note,
        submitted_at: submittedAt,
        session_started_at: state.session_started_at,
        session_finished_at: submittedAt,
        active_duration_seconds: Math.max(1, Math.floor(totalActiveSeconds())),
        fieldwork_version: CONFIG.hostedVersion,
        instrument_sha256: instrument.instrument_sha256
      };
      var columns = [
        "pilot_rater_id",
        "fatigue_1to5",
        "zero_vs_99_explanation",
        "change_vs_stance_explanation",
        "ui_error_note",
        "submitted_at",
        "session_started_at",
        "session_finished_at",
        "active_duration_seconds",
        "fieldwork_version",
        "instrument_sha256"
      ];
      download(
        assignmentCode + "_hosted_feedback.csv",
        makeCsv([row], columns)
      );
    });

    document.addEventListener("visibilitychange", function () {
      if (document.hidden) {
        stopActiveTimer();
      } else {
        startActiveTimer();
      }
    });
    window.addEventListener("pagehide", stopActiveTimer);
    window.setInterval(function () {
      if (activeStartedAt !== null) {
        stopActiveTimer();
        startActiveTimer();
      }
    }, 15000);
  }

  async function handleInvite(token) {
    setStatus("초대 링크를 확인하고 있습니다.", "info");
    try {
      var result = await verifyInvite(token);
      var code = result.assignment_code || result.pilot_rater_id;
      var localItems = assignmentFor(code);
      if (!sameServerItems(result.items, localItems)) {
        throw new Error("server assignment mismatch");
      }
      inviteToken = token;
      assignmentCode = code;
      setStatus(
        "초대가 확인되었습니다. 안내를 읽고 참가자 식별정보를 입력해 주세요.",
        "success"
      );
      showOnly("identityPanel");
    } catch (error) {
      inviteToken = "";
      assignmentCode = "";
      setStatus(
        "초대 링크를 확인할 수 없습니다. 연구책임자에게 문의해 주세요.",
        "error"
      );
      showOnly("invitePanel");
    }
  }

  async function runLocalAutoTest() {
    byId("participantName").value = "로컬테스트";
    byId("participantPhone").value = "01000000000";
    byId("consentAccepted").checked = true;
    byId("beginSurvey").click();
    var choices = [-2, -1, 0, 1, 2, 99];
    for (var index = 0; index < 12; index += 1) {
      var choice = choices[index % choices.length];
      var radio = document.querySelector(
        'input[name="stance"][value="' + String(choice) + '"]'
      );
      radio.click();
      byId("confidence").value = "3";
      byId("confidence").dispatchEvent(new Event("change"));
      byId("reasonCode").value =
        choice === 99 ? "CONTEXT_NEEDED" : "NONE";
      byId("reasonCode").dispatchEvent(new Event("change"));
      await new Promise(function (resolve) {
        window.setTimeout(resolve, 12);
      });
      byId("nextItem").click();
    }
    byId("fatigue").value = "2";
    byId("zeroVs99").value = "정보가 충분하고 상쇄되면 0, 판단 근거가 없으면 99";
    byId("changeVsStance").value =
      "즉시 조치와 기존 방향 유지 표현을 구분했다";
    byId("reviewSurvey").click();
    byId("submitSurvey").click();
  }

  async function bootstrap() {
    initializeEvents();
    purgeExpiredDrafts();
    var initialInviteToken = extractInviteFromFragment();
    try {
      instrument = await loadInstrument();
    } catch (error) {
      failClosed("설문 파일 무결성을 확인하지 못했습니다.");
      return;
    }
    renderStanceChoices();

    if (!CONFIG.fieldingEnabled && !isLocalPreview()) {
      failClosed(
        "현재 연구책임자의 개인정보 안내·보관기간 및 서버 점검이 완료되지 않아 배포가 잠겨 있습니다."
      );
      return;
    }
    if (!configReady() && !isLocalPreview()) {
      failClosed("개인정보 안내 필수항목이 확정되지 않았습니다.");
      return;
    }
    if (isLocalPreview()) {
      setStatus("로컬 미리보기입니다. 실제 DB에는 제출되지 않습니다.", "info");
      await handleInvite("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA");
      if (isLocalAutoTest()) {
        await runLocalAutoTest();
      }
      return;
    }

    if (initialInviteToken) {
      await handleInvite(initialInviteToken);
    } else {
      setStatus("전달받은 개인별 초대 링크 또는 코드를 확인해 주세요.", "info");
      showOnly("invitePanel");
    }
  }

  bootstrap();
})();
