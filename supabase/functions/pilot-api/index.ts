import postgres from "npm:postgres@3.4.7";
import {
  ALLOWED_ORIGIN,
  canonicalJson,
  ClientError,
  decodeBase64Url256,
  decodeSecret256,
  encryptJson,
  hmacSha256Hex,
  payloadDigestBasis,
  RESPONSE_COUNT,
  sha256Hex,
  SubmitRequest,
  validateLoadRequest,
  validateSubmitRequest,
} from "./_shared/core.ts";

const MAX_REQUEST_BYTES = 256 * 1024;
const INVITE_HMAC_DOMAIN = "bok-pilot-invite-v1";
const IDENTITY_HMAC_DOMAIN = "bok-pilot-identity-v1";

interface RuntimeConfig {
  databaseUrl: string;
  expectedInstrumentSha256: string;
  expectedInstrumentVersion: string;
  consentVersion: string;
  inviteHmacSecret: Uint8Array;
  identityHmacSecret: Uint8Array;
  piiEncryptionKey: Uint8Array;
  piiKeyId: string;
}

interface AssignmentRow {
  assignment_code: string;
  assignment_id: string;
  display_position: number;
  pilot_item_id: string;
  sentence_text: string;
}

let sqlClient: ReturnType<typeof postgres> | undefined;

function requiredEnv(name: string): string {
  const value = Deno.env.get(name)?.trim();
  if (!value) throw new Error(`Missing required server configuration: ${name}`);
  return value;
}

function runtimeConfig(): RuntimeConfig {
  const expectedInstrumentSha256 = requiredEnv("PILOT_INSTRUMENT_SHA256");
  if (!/^[0-9a-f]{64}$/.test(expectedInstrumentSha256)) {
    throw new Error("PILOT_INSTRUMENT_SHA256 must be a lowercase SHA-256 digest.");
  }
  const expectedInstrumentVersion = requiredEnv("PILOT_INSTRUMENT_VERSION");
  const consentVersion = requiredEnv("PILOT_CONSENT_VERSION");
  const piiKeyId = requiredEnv("PII_KEY_ID");
  if (!/^[A-Za-z0-9._-]{1,64}$/.test(piiKeyId)) throw new Error("PII_KEY_ID has an invalid format.");
  return {
    databaseUrl: requiredEnv("SUPABASE_DB_URL"),
    expectedInstrumentSha256,
    expectedInstrumentVersion,
    consentVersion,
    inviteHmacSecret: decodeSecret256(requiredEnv("INVITE_HMAC_SECRET_B64"), "INVITE_HMAC_SECRET_B64"),
    identityHmacSecret: decodeSecret256(requiredEnv("IDENTITY_HMAC_SECRET_B64"), "IDENTITY_HMAC_SECRET_B64"),
    piiEncryptionKey: decodeSecret256(requiredEnv("PII_ENCRYPTION_KEY_B64"), "PII_ENCRYPTION_KEY_B64"),
    piiKeyId,
  };
}

function database(url: string): ReturnType<typeof postgres> {
  if (!sqlClient) {
    sqlClient = postgres(url, {
      max: 1,
      idle_timeout: 20,
      connect_timeout: 10,
      prepare: false,
      ssl: "require",
    });
  }
  return sqlClient;
}

function responseHeaders(): HeadersInit {
  return {
    "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "content-type",
    "Access-Control-Max-Age": "600",
    "Cache-Control": "no-store, max-age=0",
    "Content-Type": "application/json; charset=utf-8",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Vary": "Origin",
  };
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: responseHeaders() });
}

function unavailableInvite(): ClientError {
  return new ClientError(403, "INVITE_UNAVAILABLE", "초대 링크가 유효하지 않거나 만료되었습니다.");
}

function assertInstrument(requestHash: string, config: RuntimeConfig): void {
  if (requestHash !== config.expectedInstrumentSha256) {
    throw new ClientError(409, "INSTRUMENT_MISMATCH", "The survey instrument has changed; reload the survey.");
  }
}

async function tokenHmac(inviteToken: string, config: RuntimeConfig): Promise<string> {
  const tokenBytes = decodeBase64Url256(inviteToken);
  return await hmacSha256Hex(config.inviteHmacSecret, INVITE_HMAC_DOMAIN, tokenBytes);
}

function assertExactAssignment(rows: AssignmentRow[]): void {
  if (rows.length !== RESPONSE_COUNT) {
    throw new Error("Configured assignment does not contain exactly 12 items.");
  }
  const positions = new Set(rows.map((row) => Number(row.display_position)));
  const items = new Set(rows.map((row) => row.pilot_item_id));
  if (positions.size !== RESPONSE_COUNT || items.size !== RESPONSE_COUNT) {
    throw new Error("Configured assignment contains duplicate positions or items.");
  }
  for (let position = 1; position <= RESPONSE_COUNT; position += 1) {
    if (!positions.has(position)) throw new Error("Configured assignment positions are not contiguous.");
  }
}

async function loadAssignment(
  db: ReturnType<typeof postgres>,
  tokenHmacHex: string,
  instrumentSha256: string,
  expectedVersion: string,
): Promise<{ assignmentCode: string; rows: AssignmentRow[] }> {
  return await db.begin(async (tx) => {
    const invites = await tx<{
      assignment_code: string;
      instrument_version: string;
      expected_response_count: number;
      expires_at: Date;
      revoked_at: Date | null;
      used_at: Date | null;
      is_active: boolean;
      fielding_open: boolean;
    }[]>`
      select
        v.assignment_code,
        i.instrument_version,
        i.expected_response_count,
        v.expires_at,
        v.revoked_at,
        v.used_at,
        i.is_active,
        i.fielding_open
      from private.pilot_invites as v
      join research.pilot_instruments as i
        on i.instrument_sha256 = v.instrument_sha256
      where v.token_hmac = decode(${tokenHmacHex}, 'hex')
        and v.instrument_sha256 = ${instrumentSha256}
      limit 1
    `;
    if (invites.length !== 1) throw unavailableInvite();
    const invite = invites[0];
    if (
      invite.revoked_at !== null || invite.used_at !== null || !invite.is_active || !invite.fielding_open ||
      new Date(invite.expires_at).getTime() <= Date.now() ||
      invite.instrument_version !== expectedVersion || Number(invite.expected_response_count) !== RESPONSE_COUNT
    ) throw unavailableInvite();

    const rows = await tx<AssignmentRow[]>`
      select
        a.assignment_code,
        a.assignment_id,
        a.display_position,
        a.pilot_item_id,
        p.sentence_text
      from research.pilot_assignments as a
      join research.pilot_items as p
        on p.instrument_sha256 = a.instrument_sha256
       and p.pilot_item_id = a.pilot_item_id
      where a.instrument_sha256 = ${instrumentSha256}
        and a.assignment_code = ${invite.assignment_code}
      order by a.display_position
    `;
    assertExactAssignment(rows);
    return { assignmentCode: invite.assignment_code, rows };
  });
}

async function submitAtomically(
  db: ReturnType<typeof postgres>,
  tokenHmacHex: string,
  request: SubmitRequest,
  config: RuntimeConfig,
): Promise<{ submission_id: string; submitted_at: string; idempotent: boolean }> {
  const identityCanonical = canonicalJson({ name: request.identity.name, phone: request.identity.phone });
  const identityHmacHex = await hmacSha256Hex(config.identityHmacSecret, IDENTITY_HMAC_DOMAIN, identityCanonical);
  const identityAad = `${request.instrument_sha256}:${tokenHmacHex}`;
  const encrypted = await encryptJson(config.piiEncryptionKey, {
    name: request.identity.name,
    phone: request.identity.phone,
  }, identityAad);
  const participantId = crypto.randomUUID();
  const submissionId = crypto.randomUUID();
  const researchSession = {
    started_at: request.session.started_at,
    finished_at: request.session.finished_at,
    active_duration_seconds: request.session.active_duration_seconds,
  };

  return await db.begin(async (tx) => {
    const invites = await tx<{
      invite_id: string;
      assignment_code: string;
      instrument_version: string;
      expected_response_count: number;
      expires_at: Date;
      revoked_at: Date | null;
      used_at: Date | null;
      submission_id: string | null;
      is_active: boolean;
      fielding_open: boolean;
    }[]>`
      select
        v.invite_id,
        v.assignment_code,
        v.expires_at,
        v.revoked_at,
        v.used_at,
        v.submission_id,
        i.instrument_version,
        i.expected_response_count,
        i.is_active,
        i.fielding_open
      from private.pilot_invites as v
      join research.pilot_instruments as i
        on i.instrument_sha256 = v.instrument_sha256
      where v.token_hmac = decode(${tokenHmacHex}, 'hex')
        and v.instrument_sha256 = ${request.instrument_sha256}
      for update of v
    `;
    if (invites.length !== 1) throw unavailableInvite();
    const invite = invites[0];

    if (invite.used_at !== null) {
      const existing = await tx<{
        submission_id: string;
        submitted_at: Date;
      }[]>`
        select s.submission_id, s.submitted_at
        from research.pilot_submissions as s
        join private.participant_identity as p
          on p.participant_id = s.participant_id
        where s.invite_id = ${invite.invite_id}::uuid
          and s.idempotency_key = ${request.idempotency_key}::uuid
          and s.payload_sha256 = ${request.payload_sha256}
          and p.identity_hmac = decode(${identityHmacHex}, 'hex')
        limit 1
      `;
      if (existing.length === 1) {
        return {
          submission_id: existing[0].submission_id,
          submitted_at: new Date(existing[0].submitted_at).toISOString(),
          idempotent: true,
        };
      }
      throw new ClientError(409, "INVITE_ALREADY_USED", "This invitation has already submitted a response.");
    }
    if (
      invite.revoked_at !== null || !invite.is_active || !invite.fielding_open ||
      new Date(invite.expires_at).getTime() <= Date.now() ||
      invite.instrument_version !== config.expectedInstrumentVersion ||
      Number(invite.expected_response_count) !== RESPONSE_COUNT
    ) throw unavailableInvite();

    const assignments = await tx<AssignmentRow[]>`
      select
        a.assignment_code,
        a.assignment_id,
        a.display_position,
        a.pilot_item_id,
        p.sentence_text
      from research.pilot_assignments as a
      join research.pilot_items as p
        on p.instrument_sha256 = a.instrument_sha256
       and p.pilot_item_id = a.pilot_item_id
      where a.instrument_sha256 = ${request.instrument_sha256}
        and a.assignment_code = ${invite.assignment_code}
      order by a.display_position
    `;
    assertExactAssignment(assignments);
    for (let index = 0; index < RESPONSE_COUNT; index += 1) {
      const assigned = assignments[index];
      const response = request.responses[index];
      if (
        Number(assigned.display_position) !== response.display_position ||
        assigned.pilot_item_id !== response.pilot_item_id
      ) {
        throw new ClientError(409, "ASSIGNMENT_MISMATCH", "The response set does not match this invitation.");
      }
    }

    await tx`
      insert into private.participant_identity (
        participant_id,
        invite_id,
        identity_ciphertext_b64,
        identity_iv_b64,
        identity_aad,
        identity_hmac,
        encryption_key_id,
        consent_version,
        consent_accepted_at
      ) values (
        ${participantId}::uuid,
        ${invite.invite_id}::uuid,
        ${encrypted.ciphertext_b64},
        ${encrypted.iv_b64},
        ${identityAad},
        decode(${identityHmacHex}, 'hex'),
        ${config.piiKeyId},
        ${request.consent.version},
        ${request.consent.accepted_at}::timestamptz
      )
    `;

    await tx`
      insert into research.pilot_submissions (
        submission_id,
        participant_id,
        invite_id,
        instrument_sha256,
        instrument_version,
        assignment_code,
        idempotency_key,
        payload_sha256,
        session_started_at,
        session_finished_at,
        active_duration_seconds,
        fatigue_1to5,
        zero_vs_99_explanation,
        change_vs_stance_explanation,
        ui_error_note
      ) values (
        ${submissionId}::uuid,
        ${participantId}::uuid,
        ${invite.invite_id}::uuid,
        ${request.instrument_sha256},
        ${config.expectedInstrumentVersion},
        ${invite.assignment_code},
        ${request.idempotency_key}::uuid,
        ${request.payload_sha256},
        ${researchSession.started_at}::timestamptz,
        ${researchSession.finished_at}::timestamptz,
        ${researchSession.active_duration_seconds},
        ${request.feedback.fatigue_1to5},
        ${request.feedback.zero_vs_99_explanation},
        ${request.feedback.change_vs_stance_explanation},
        ${request.feedback.ui_error_note}
      )
    `;

    const responseRows = request.responses.map((response, index) => ({
      submission_id: submissionId,
      instrument_sha256: request.instrument_sha256,
      display_position: response.display_position,
      assignment_id: assignments[index].assignment_id,
      pilot_item_id: response.pilot_item_id,
      stance_label: response.choice === 99 ? null : response.choice,
      abstain: response.choice === 99,
      reason_code: response.reason_code,
      confidence: response.confidence,
      reason_note: response.reason_note,
      started_at: response.started_at,
      finished_at: response.finished_at,
      active_duration_seconds: response.active_duration_seconds,
    }));
    await tx`
      insert into research.pilot_responses ${tx(responseRows,
        "submission_id",
        "instrument_sha256",
        "display_position",
        "assignment_id",
        "pilot_item_id",
        "stance_label",
        "abstain",
        "reason_code",
        "confidence",
        "reason_note",
        "started_at",
        "finished_at",
        "active_duration_seconds"
      )}
    `;

    const updated = await tx<{ submitted_at: Date }[]>`
      update private.pilot_invites
      set used_at = now(), submission_id = ${submissionId}::uuid
      where invite_id = ${invite.invite_id}::uuid
        and used_at is null
      returning used_at as submitted_at
    `;
    if (updated.length !== 1) throw new Error("Invite state changed during submission.");
    return {
      submission_id: submissionId,
      submitted_at: new Date(updated[0].submitted_at).toISOString(),
      idempotent: false,
    };
  });
}

Deno.serve(async (request: Request): Promise<Response> => {
  const origin = request.headers.get("origin");
  if (origin !== ALLOWED_ORIGIN) {
    return new Response(JSON.stringify({ ok: false, error: { code: "ORIGIN_DENIED", message: "Origin not allowed." } }), {
      status: 403,
      headers: {
        "Cache-Control": "no-store, max-age=0",
        "Content-Type": "application/json; charset=utf-8",
        "X-Content-Type-Options": "nosniff",
      },
    });
  }
  if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: responseHeaders() });
  if (request.method !== "POST") return jsonResponse(405, { ok: false, error: { code: "METHOD_NOT_ALLOWED", message: "POST required." } });
  if (!request.headers.get("content-type")?.toLowerCase().startsWith("application/json")) {
    return jsonResponse(415, { ok: false, error: { code: "UNSUPPORTED_MEDIA_TYPE", message: "application/json required." } });
  }
  const declaredLength = Number(request.headers.get("content-length") ?? "0");
  if (Number.isFinite(declaredLength) && declaredLength > MAX_REQUEST_BYTES) {
    return jsonResponse(413, { ok: false, error: { code: "PAYLOAD_TOO_LARGE", message: "Request body is too large." } });
  }

  try {
    const raw = await request.text();
    if (new TextEncoder().encode(raw).byteLength > MAX_REQUEST_BYTES) {
      throw new ClientError(413, "PAYLOAD_TOO_LARGE", "Request body is too large.");
    }
    let body: unknown;
    try {
      body = JSON.parse(raw);
    } catch {
      throw new ClientError(400, "INVALID_JSON", "Request body is not valid JSON.");
    }
    if (body === null || typeof body !== "object" || Array.isArray(body)) {
      throw new ClientError(400, "INVALID_REQUEST", "Request body must be an object.");
    }

    const config = runtimeConfig();
    const db = database(config.databaseUrl);
    const action = (body as Record<string, unknown>).action;
    if (action === "load") {
      const load = validateLoadRequest(body);
      assertInstrument(load.instrument_sha256, config);
      const hmac = await tokenHmac(load.invite_token, config);
      const assignment = await loadAssignment(db, hmac, load.instrument_sha256, config.expectedInstrumentVersion);
      return jsonResponse(200, {
        ok: true,
        instrument: {
          version: config.expectedInstrumentVersion,
          sha256: config.expectedInstrumentSha256,
          response_count: RESPONSE_COUNT,
        },
        assignment_code: assignment.assignmentCode,
        items: assignment.rows.map((row) => ({
          assignment_id: row.assignment_id,
          display_position: Number(row.display_position),
          pilot_item_id: row.pilot_item_id,
          sentence_text: row.sentence_text,
        })),
      });
    }
    if (action === "submit") {
      const submit = validateSubmitRequest(body, config.consentVersion);
      assertInstrument(submit.instrument_sha256, config);
      const calculatedPayloadHash = await sha256Hex(canonicalJson(payloadDigestBasis(submit)));
      if (calculatedPayloadHash !== submit.payload_sha256) {
        throw new ClientError(400, "PAYLOAD_HASH_MISMATCH", "The submission payload hash does not match.");
      }
      const hmac = await tokenHmac(submit.invite_token, config);
      const receipt = await submitAtomically(
        db,
        hmac,
        submit,
        config,
      );
      return jsonResponse(200, { ok: true, receipt });
    }
    throw new ClientError(400, "INVALID_ACTION", "action must be load or submit.");
  } catch (error) {
    if (error instanceof ClientError) {
      return jsonResponse(error.status, { ok: false, error: { code: error.code, message: error.message } });
    }
    // Never log request bodies, invite tokens, identity fields, ciphertext, or database error details.
    console.error("pilot-api request failed", error instanceof Error ? error.name : "unknown_error");
    return jsonResponse(500, { ok: false, error: { code: "SERVER_ERROR", message: "The response could not be processed." } });
  }
});
