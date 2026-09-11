import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";

const adminSource = await readFile("docs/admin.js", "utf8");
const html = await readFile("docs/admin.html", "utf8");
const appSource = await readFile("docs/app.js", "utf8");
const participantHtml = await readFile("docs/index.html", "utf8");

function sourceRange(source, startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start + startMarker.length);
  assert.notEqual(start, -1, "missing source marker: " + startMarker);
  assert.notEqual(end, -1, "missing source marker: " + endMarker);
  return source.slice(start, end);
}

const sandbox = { window: {} };
vm.runInNewContext(adminSource, sandbox, { filename: "docs/admin.js" });
const bridge = sandbox.window.PILOT_ADMIN_BRIDGE;

assert.ok(Object.isFrozen(bridge));

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((onResolve, onReject) => {
    resolve = onResolve;
    reject = onReject;
  });
  return { promise, reject, resolve };
}

const sessionLifecycle = bridge.createSessionLifecycle();
assert.ok(Object.isFrozen(sessionLifecycle));
const resolved = deferred();
const resolvedEpoch = sessionLifecycle.capture();
let resolvedAbortCount = 0;
let resolvedState = null;
let resolvedDom = "purged";
const releaseResolved = sessionLifecycle.track(resolvedEpoch, {
  abort() {
    resolvedAbortCount += 1;
  }
});
const resolvedContinuation = resolved.promise.then(() => {
  if (sessionLifecycle.isCurrent(resolvedEpoch)) {
    resolvedState = { restored: true };
    resolvedDom = "mutated";
  }
});
sessionLifecycle.invalidate();
assert.equal(resolvedAbortCount, 1);
assert.notEqual(sessionLifecycle.capture(), resolvedEpoch);
resolved.resolve({ ok: true });
await resolvedContinuation;
releaseResolved();
assert.equal(resolvedState, null);
assert.equal(resolvedDom, "purged");
assert.throws(() =>
  sessionLifecycle.track(resolvedEpoch, { abort() {} })
);

const rejected = deferred();
const rejectedEpoch = sessionLifecycle.capture();
let rejectedAbortCount = 0;
let rejectedState = null;
let rejectedDom = "purged";
sessionLifecycle.track(rejectedEpoch, {
  abort() {
    rejectedAbortCount += 1;
  }
});
const rejectedContinuation = rejected.promise.catch(() => {
  if (sessionLifecycle.isCurrent(rejectedEpoch)) {
    rejectedState = { restored: true };
    rejectedDom = "mutated";
  }
});
sessionLifecycle.invalidate();
assert.equal(rejectedAbortCount, 1);
rejected.reject(new Error("safe synthetic rejection"));
await rejectedContinuation;
assert.equal(rejectedState, null);
assert.equal(rejectedDom, "purged");

const adminId = "PI-ABCDEFGHIJKL";
const adminPassword = "A".repeat(43);
const instrumentSha = "a".repeat(64);
const credentials = bridge.validateCredentials(adminId, adminPassword);
assert.ok(Object.isFrozen(credentials));
assert.deepEqual(JSON.parse(JSON.stringify(credentials)), {
  admin_id: adminId,
  admin_password: adminPassword
});
for (const invalid of [
  ["pi-ABCDEFGHIJKL", adminPassword],
  ["PI-ABCDEFGHIJK", adminPassword],
  [adminId, "A".repeat(42)],
  [adminId, "B".repeat(43)],
  [adminId, "=".repeat(43)]
]) {
  assert.throws(() => bridge.validateCredentials(...invalid));
}

const load = bridge.makeLoadRequest(credentials, instrumentSha);
assert.deepEqual(JSON.parse(JSON.stringify(load)), {
  action: "admin_load",
  admin_id: adminId,
  admin_password: adminPassword,
  instrument_sha256: instrumentSha
});
assert.equal(
  bridge.configIsPinned({
    apiUrl:
      "https://mebisrsvasrzwkmsodsw.supabase.co/functions/v1/pilot-api"
  }),
  true
);
assert.equal(bridge.configIsPinned({ apiUrl: "https://example.invalid" }), false);

const submission = {
  instrument_sha256: instrumentSha,
  idempotency_key: "00000000-0000-4000-8000-000000000000",
  consent: { accepted: true },
  identity: { name: "SYNTHETIC", phone: "01000000000" },
  session: {},
  responses: [],
  feedback: {},
  payload_sha256: "b".repeat(64)
};
const submit = bridge.makeSubmitRequest(credentials, submission);
assert.equal(submit.action, "admin_submit");
assert.equal(submit.admin_id, adminId);
assert.equal(submit.admin_password, adminPassword);
assert.equal(Object.prototype.hasOwnProperty.call(submit, "invite_token"), false);
assert.equal(submit.idempotency_key, submission.idempotency_key);
assert.throws(() =>
  bridge.makeSubmitRequest(credentials, {
    ...submission,
    invite_token: "forbidden"
  })
);

assert.match(html, /data-pilot-mode="admin"/);
assert.match(html, /id="adminId"/);
assert.match(
  html,
  /id="adminPassword"[^>]*type="password"|type="password"[^>]*id="adminPassword"/
);
assert.match(html, /데이터베이스 관리 권한/);
assert.match(html, /제공하지 않습니다/);
assert.match(html, /synthetic_pi_manual_test/);
assert.match(html, /분석에서[^<]*제외/);
assert.match(html, /script src="admin\.js"/);
assert.doesNotMatch(html, /<script(?!\s+src=)[^>]*>/);
assert.doesNotMatch(html, /(?:src|href)="https?:\/\//);
assert.match(
  html,
  /connect-src 'self' https:\/\/mebisrsvasrzwkmsodsw\.supabase\.co/
);
assert.doesNotMatch(adminSource, /localStorage|sessionStorage|console\.|cookie/);
for (const id of [
  "globalStatus",
  "clearDraft",
  "blockedPanel",
  "blockedMessage",
  "invitePanel",
  "adminId",
  "adminPassword",
  "verifyInvite",
  "identityPanel",
  "participantName",
  "participantPhone",
  "consentAccepted",
  "beginSurvey",
  "surveyPanel",
  "progressText",
  "progressBar",
  "sentenceText",
  "stanceChoices",
  "confidence",
  "reasonCode",
  "reasonNote",
  "itemError",
  "previousItem",
  "nextItem",
  "feedbackPanel",
  "fatigue",
  "zeroVs99",
  "changeVsStance",
  "uiError",
  "feedbackError",
  "backToItems",
  "reviewSurvey",
  "reviewPanel",
  "reviewRater",
  "reviewCount",
  "reviewInstrument",
  "submitError",
  "editFeedback",
  "submitSurvey",
  "fallbackArea",
  "downloadResponses",
  "downloadFeedback",
  "donePanel",
  "receiptId"
]) {
  assert.equal(
    (html.match(new RegExp('id="' + id + '"', "g")) || []).length,
    1,
    "admin page must contain exactly one #" + id
  );
}
assert.doesNotMatch(html, /id="inviteCode"/);
for (const id of ["adminId", "adminPassword"]) {
  const input = html.match(new RegExp('<input id="' + id + '"[^>]*>'))?.[0];
  assert.ok(input);
  assert.doesNotMatch(input, /\svalue=/);
}
assert.match(appSource, /var ADMIN_MODE/);
assert.match(appSource, /adminMemoryDraft/);
assert.match(appSource, /ADMIN_BRIDGE\.makeSubmitRequest/);
assert.match(
  appSource,
  /clearAdminLoginFields\(\);\s*await handleAdminLogin/
);
assert.match(
  appSource,
  /if \(ADMIN_MODE\) \{\s*clearAdminUrlState\(\);/
);
assert.match(
  appSource,
  /var raw = ADMIN_MODE\s*\? adminMemoryDraft\s*:\s*window\.localStorage\.getItem/
);
assert.match(
  appSource,
  /if \(ADMIN_MODE\) \{\s*adminMemoryDraft = serialized;\s*\} else \{\s*window\.localStorage\.setItem/
);
const participantHandler = sourceRange(
  appSource,
  "async function handleInvite(",
  "async function handleAdminLogin("
);
const adminHandler = sourceRange(
  appSource,
  "async function handleAdminLogin(",
  "async function runLocalAutoTest("
);
assert.doesNotMatch(participantHandler, /code !== "PILOT_R01"/);
assert.match(participantHandler, /sameServerItems\(result\.items, localItems\)/);
assert.match(adminHandler, /code !== "PILOT_R01"/);
assert.match(adminHandler, /ADMIN_LIFECYCLE\.capture\(\)/);
assert.match(adminHandler, /verifyAdmin\(credentials, adminEpoch\)/);
assert.ok(
  (adminHandler.match(/adminEpochIsCurrent\(adminEpoch\)/g) || []).length >= 3
);
assert.equal((appSource.match(/code !== "PILOT_R01"/g) || []).length, 1);

const apiRequest = sourceRange(
  appSource,
  "async function apiRequest(",
  "function sameServerItems("
);
const apiPinCall = apiRequest.indexOf("assertAdminApiPinned();");
const epochCall = apiRequest.indexOf("assertAdminContinuation(adminEpoch);");
const controllerTrack = apiRequest.indexOf(
  "ADMIN_LIFECYCLE.track(adminEpoch, controller)"
);
const fetchCall = apiRequest.indexOf("fetch(CONFIG.apiUrl");
assert.ok(
  apiPinCall >= 0 &&
    epochCall > apiPinCall &&
    controllerTrack > epochCall &&
    fetchCall > controllerTrack
);
assert.match(apiRequest, /releaseAdminController\(\)/);
const apiPinGuard = sourceRange(
  appSource,
  "function assertAdminApiPinned(",
  "function validInviteToken("
);
assert.match(apiPinGuard, /ADMIN_MODE/);
assert.match(apiPinGuard, /ADMIN_BRIDGE\.configIsPinned\(CONFIG\)/);

const cleanup = sourceRange(
  appSource,
  "function clearAdminSessionState(",
  "async function submitSurvey("
);
for (const required of [
  "activeStartedAt = null",
  "activeAssignmentId = null",
  "adminMemoryDraft = null",
  "clearAccessCredentials();",
  "clearPrivateInputs();",
  "state = null",
  'assignmentCode = ""',
  "submitFailures = 0"
]) {
  assert.ok(cleanup.includes(required), "admin cleanup missing: " + required);
}
const finalizer = sourceRange(
  appSource,
  "async function finalizedSubmissionPayload(",
  "function clearPrivateInputs("
);
assert.ok(
  (finalizer.match(/assertAdminContinuation\(adminEpoch\)/g) || []).length >= 3
);
const submitHandler = sourceRange(
  appSource,
  "async function submitSurvey(",
  "function csvSafe("
);
assert.match(submitHandler, /ADMIN_LIFECYCLE\.capture\(\)/);
assert.match(submitHandler, /finalizedSubmissionPayload\(adminEpoch\)/);
assert.match(submitHandler, /apiRequest\(body, adminEpoch\)/);
assert.ok(
  (
    submitHandler.match(
      /adminContinuationIsCurrent\(adminEpoch\)/g
    ) || []
  ).length >= 4
);
const lifecycle = sourceRange(
  appSource,
  'window.addEventListener("pagehide"',
  "window.setInterval("
);
assert.match(lifecycle, /pagehide[\s\S]*clearAdminSessionState\(\)/);
assert.match(lifecycle, /pageshow[\s\S]*event\.persisted/);
const pagehide = lifecycle.slice(0, lifecycle.indexOf("pageshow"));
const pageshow = lifecycle.slice(lifecycle.indexOf("pageshow"));
assert.ok(
  pagehide.indexOf("invalidateAdminSession();") >= 0 &&
    pagehide.indexOf("invalidateAdminSession();") <
      pagehide.indexOf("clearAdminSessionState();")
);
assert.ok(
  pageshow.indexOf("invalidateAdminSession();") >= 0 &&
    pageshow.indexOf("invalidateAdminSession();") <
      pageshow.indexOf("clearAdminSessionState();")
);
assert.match(
  appSource,
  /error\.code === "ADMIN_CREDENTIAL_ALREADY_USED"/
);
assert.doesNotMatch(participantHtml, /data-pilot-mode="admin"|admin\.js/);
for (const page of [html, participantHtml]) {
  assert.match(page, /사용성 피드백 \(선택\)/);
  assert.match(page, /모두 선택사항/);
  assert.doesNotMatch(page, /피드백이 모두 입력/);
}
assert.match(
  appSource,
  /state\.feedback\.fatigue_1to5 !== null[\s\S]*!\[1, 2, 3, 4, 5\]\.includes/
);
assert.doesNotMatch(appSource, /피로도를 선택해 주세요/);
assert.doesNotMatch(appSource, /구분 기준을 입력해 주세요/);

console.log(
  "PASS admin frontend contract, lifecycle, optional-feedback, and static security tests=3"
);
