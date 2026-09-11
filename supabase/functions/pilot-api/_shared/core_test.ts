import {
  ALLOWED_ORIGIN,
  canonicalJson,
  ClientError,
  decodeBase64Url256,
  encryptJson,
  hmacSha256Hex,
  payloadDigestBasis,
  sha256Hex,
  validateAdminLoadRequest,
  validateAdminSubmitRequest,
  validateSubmitRequest,
} from "./core.ts";

function assert(condition: boolean, message = "assertion failed"): void {
  if (!condition) throw new Error(message);
}

function validBody(): Record<string, unknown> {
  const started = "2026-09-03T00:00:00.000Z";
  const finished = "2026-09-03T00:10:00.000Z";
  return {
    action: "submit",
    invite_token: "A".repeat(43),
    instrument_sha256: "a".repeat(64),
    consent: { accepted: true, version: "consent-v1", accepted_at: started },
    identity: { name: "홍 길동", phone: "+82 10-1234-5678" },
    session: {
      started_at: started,
      finished_at: finished,
      active_duration_seconds: 120,
    },
    responses: Array.from({ length: 12 }, (_, index) => ({
      display_position: index + 1,
      pilot_item_id: `USP_${String(index + 1).padStart(3, "0")}`,
      choice: 0,
      reason_code: "NONE",
      confidence: 3,
      reason_note: "",
      started_at: "2026-09-03T00:01:00.000Z",
      finished_at: "2026-09-03T00:01:10.000Z",
      active_duration_seconds: 5,
    })),
    feedback: {
      fatigue_1to5: 2,
      zero_vs_99_explanation: "구별 가능",
      change_vs_stance_explanation: "구별 가능",
      ui_error_note: "",
    },
    payload_sha256: "b".repeat(64),
    idempotency_key: "123e4567-e89b-42d3-a456-426614174000",
  };
}

function validAdminBody(): Record<string, unknown> {
  const body = validBody();
  body.action = 'admin_submit';
  body.admin_id = 'PI-ABCDEFGHIJKL';
  body.admin_password = 'A'.repeat(43);
  delete body.invite_token;
  return body;
}

Deno.test("production CORS origin is exact", () => {
  assert(ALLOWED_ORIGIN === "https://khdouble.github.io");
});

Deno.test("invite token must encode exactly 256 bits", () => {
  assert(decodeBase64Url256("A".repeat(43)).byteLength === 32);
  let rejected = false;
  try {
    decodeBase64Url256("A".repeat(42));
  } catch (error) {
    rejected = error instanceof ClientError;
  }
  assert(rejected);
  rejected = false;
  try {
    decodeBase64Url256("A".repeat(42) + "B");
  } catch (error) {
    rejected = error instanceof ClientError;
  }
  assert(rejected);
});

Deno.test("submit validation normalizes identity and sorts positions", () => {
  const body = validBody();
  const responses = body.responses as Array<Record<string, unknown>>;
  body.responses = [...responses].reverse();
  const result = validateSubmitRequest(body, "consent-v1", Date.parse("2026-09-03T00:11:00.000Z"));
  assert(result.identity.phone === "01012345678");
  assert(result.identity.name === "홍 길동");
  assert(result.responses[0].display_position === 1);
  assert(result.responses[11].display_position === 12);
});

Deno.test("submit validation accepts completely blank optional feedback", () => {
  const body = validBody();
  body.feedback = {
    fatigue_1to5: null,
    zero_vs_99_explanation: "",
    change_vs_stance_explanation: "",
    ui_error_note: "",
  };
  const result = validateSubmitRequest(
    body,
    "consent-v1",
    Date.parse("2026-09-03T00:11:00.000Z"),
  );
  assert(result.feedback.fatigue_1to5 === null);
  assert(result.feedback.zero_vs_99_explanation === "");
  assert(result.feedback.change_vs_stance_explanation === "");
});

Deno.test("submit validation rejects anything other than exactly 12 unique positions", () => {
  const body = validBody();
  (body.responses as unknown[]).pop();
  let rejected = false;
  try {
    validateSubmitRequest(body, "consent-v1", Date.parse("2026-09-03T00:11:00.000Z"));
  } catch (error) {
    rejected = error instanceof ClientError && error.code === "INVALID_RESPONSES";
  }
  assert(rejected);
});

Deno.test("submit contract rejects browser user-agent collection", () => {
  const body = validBody();
  (body.session as Record<string, unknown>).client_user_agent = "must-not-be-collected";
  let rejected = false;
  try {
    validateSubmitRequest(body, "consent-v1", Date.parse("2026-09-03T00:11:00.000Z"));
  } catch (error) {
    rejected = error instanceof ClientError && error.code === "INVALID_REQUEST";
  }
  assert(rejected);
});

Deno.test("payload digest excludes direct identity and invite token", async () => {
  const first = validateSubmitRequest(validBody(), "consent-v1", Date.parse("2026-09-03T00:11:00.000Z"));
  const secondBody = validBody();
  secondBody.identity = { name: "다른 이름", phone: "010-9999-8888" };
  secondBody.invite_token = "B".repeat(43);
  const second = validateSubmitRequest(secondBody, "consent-v1", Date.parse("2026-09-03T00:11:00.000Z"));
  const firstHash = await sha256Hex(canonicalJson(payloadDigestBasis(first)));
  const secondHash = await sha256Hex(canonicalJson(payloadDigestBasis(second)));
  assert(firstHash === secondHash);
});

Deno.test("HMAC is deterministic and domain separated", async () => {
  const secret = new Uint8Array(32).fill(7);
  const message = decodeBase64Url256("A".repeat(43));
  const first = await hmacSha256Hex(secret, "domain-a", message);
  const second = await hmacSha256Hex(secret, "domain-a", message);
  const third = await hmacSha256Hex(secret, "domain-b", message);
  assert(first.length === 64 && first === second && first !== third);
});

Deno.test('admin load requires exact canonical credentials', () => {
  const valid = validateAdminLoadRequest({
    action: 'admin_load',
    admin_id: 'PI-ABCDEFGHIJKL',
    admin_password: 'A'.repeat(43),
    instrument_sha256: 'a'.repeat(64),
  });
  assert(valid.admin_id === 'PI-ABCDEFGHIJKL');
  for (const [admin_id, admin_password] of [
    ['pi-ABCDEFGHIJKL', 'A'.repeat(43)],
    ['PI-ABCDEFGHIJK', 'A'.repeat(43)],
    ['PI-ABCDEFGHIJKL', 'A'.repeat(42)],
    ['PI-ABCDEFGHIJKL', 'A'.repeat(42) + 'B'],
  ]) {
    let rejected = false;
    try {
      validateAdminLoadRequest({
        action: 'admin_load',
        admin_id,
        admin_password,
        instrument_sha256: 'a'.repeat(64),
      });
    } catch (error) {
      rejected = error instanceof ClientError &&
        error.status === 403 &&
        error.code === 'ADMIN_AUTH_FAILED';
    }
    assert(rejected);
  }
});

Deno.test('admin submit reuses normal validation without invite token', () => {
  const result = validateAdminSubmitRequest(
    validAdminBody(),
    'consent-v1',
    Date.parse('2026-09-03T00:11:00.000Z'),
  );
  assert(result.action === 'admin_submit');
  assert(result.admin_id === 'PI-ABCDEFGHIJKL');
  assert(result.responses.length === 12);
  assert(!('invite_token' in result));
});

Deno.test('admin payload digest excludes both credential fields', () => {
  const first = validateAdminSubmitRequest(
    validAdminBody(),
    'consent-v1',
    Date.parse('2026-09-03T00:11:00.000Z'),
  );
  const changedBody = validAdminBody();
  changedBody.admin_id = 'PI-ZYXWVUTSRQPO';
  changedBody.admin_password = 'B'.repeat(42) + 'Q';
  const second = validateAdminSubmitRequest(
    changedBody,
    'consent-v1',
    Date.parse('2026-09-03T00:11:00.000Z'),
  );
  assert(
    canonicalJson(payloadDigestBasis(first)) ===
      canonicalJson(payloadDigestBasis(second)),
  );
});

Deno.test('missing admin credential is generic auth failure', () => {
  const body = validAdminBody();
  delete body.admin_password;
  let rejected = false;
  try {
    validateAdminSubmitRequest(
      body,
      'consent-v1',
      Date.parse('2026-09-03T00:11:00.000Z'),
    );
  } catch (error) {
    rejected = error instanceof ClientError &&
      error.status === 403 &&
      error.code === 'ADMIN_AUTH_FAILED';
  }
  assert(rejected);
});

Deno.test('malformed admin submit credential is generic auth failure', () => {
  for (const [admin_id, admin_password] of [
    ['PI-abcdefgh1234', 'A'.repeat(43)],
    ['PI-ABCDEFGHIJKL', 'A'.repeat(44)],
    ['PI-ABCDEFGHIJKL', 'A'.repeat(42) + '='],
  ]) {
    const body = validAdminBody();
    body.admin_id = admin_id;
    body.admin_password = admin_password;
    let rejected = false;
    try {
      validateAdminSubmitRequest(
        body,
        'consent-v1',
        Date.parse('2026-09-03T00:11:00.000Z'),
      );
    } catch (error) {
      rejected = error instanceof ClientError &&
        error.status === 403 &&
        error.code === 'ADMIN_AUTH_FAILED';
    }
    assert(rejected);
  }
});

Deno.test("direct identity is encrypted with randomized AES-GCM output", async () => {
  const key = new Uint8Array(32).fill(9);
  const identity = { name: "홍길동", phone: "01012345678" };
  const first = await encryptJson(key, identity, "instrument:invite-hmac");
  const second = await encryptJson(key, identity, "instrument:invite-hmac");
  assert(first.iv_b64.length === 16);
  assert(first.ciphertext_b64 !== second.ciphertext_b64);
  assert(!first.ciphertext_b64.includes("홍길동"));
  assert(!first.ciphertext_b64.includes("01012345678"));
});

Deno.test("research free text cannot repeat submitted phone", () => {
  const body = validBody();
  (body.feedback as Record<string, unknown>).ui_error_note = "연락처 010-1234-5678로 연락";
  let rejected = false;
  try {
    validateSubmitRequest(body, "consent-v1", Date.parse("2026-09-03T00:11:00.000Z"));
  } catch (error) {
    rejected = error instanceof ClientError && error.code === "IDENTIFIER_IN_RESEARCH_TEXT";
  }
  assert(rejected);
});

Deno.test("backend matches shared frontend submission golden vector", async () => {
  const fixture = JSON.parse(await Deno.readTextFile(
    "supabase/tests/fixtures/submission_digest_golden.json",
  ));
  const raw = fixture.raw_digest_input;
  const request = {
    action: "submit",
    invite_token: "A".repeat(43),
    instrument_sha256: raw.instrument_sha256,
    consent: raw.consent,
    identity: fixture.identity,
    session: raw.session,
    responses: raw.responses,
    feedback: raw.feedback,
    payload_sha256: fixture.expected.payload_sha256,
    idempotency_key: "123e4567-e89b-42d3-a456-426614174000",
  };
  const normalized = validateSubmitRequest(
    request,
    fixture.consent_version,
    Date.parse(fixture.now_for_validation),
  );
  const basis = payloadDigestBasis(normalized) as Record<string, any>;
  const expected = fixture.expected;
  assert(basis.consent.version === expected.consent_version);
  assert(basis.consent.accepted_at === expected.consent_accepted_at);
  assert(basis.session.started_at === expected.session_started_at);
  assert(basis.session.finished_at === expected.session_finished_at);
  assert(JSON.stringify(basis.responses.map((response: any) => response.display_position)) === JSON.stringify(expected.response_positions));
  assert(basis.responses[0].pilot_item_id === expected.first_item_id);
  assert(basis.responses[0].reason_note === expected.first_reason_note);
  assert(basis.responses[1].reason_note === expected.second_reason_note);
  assert(basis.feedback.zero_vs_99_explanation === expected.zero_vs_99_explanation);
  assert(basis.feedback.ui_error_note === expected.ui_error_note);
  assert(await sha256Hex(canonicalJson(basis)) === expected.payload_sha256);
});
