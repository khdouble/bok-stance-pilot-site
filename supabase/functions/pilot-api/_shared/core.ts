export const ALLOWED_ORIGIN = "https://khdouble.github.io";
export const RESPONSE_COUNT = 12;
export const STANCE_CHOICES = [-2, -1, 0, 1, 2, 99] as const;
export const REASON_CODES = [
  "NONE",
  "IRRELEVANT",
  "CONTEXT_NEEDED",
  "MIXED_UNRESOLVED",
  "TERMINOLOGY",
  "OTHER",
] as const;

const HEX_64 = /^[0-9a-f]{64}$/;
const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const ITEM_ID = /^[A-Za-z0-9_-]{1,64}$/;
const CONTROL_CHARS = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/;

export class ClientError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ClientError";
    this.status = status;
    this.code = code;
  }
}

type JsonObject = Record<string, unknown>;

export interface LoadRequest {
  action: "load";
  invite_token: string;
  instrument_sha256: string;
}

export interface ConsentRecord {
  accepted: true;
  version: string;
  accepted_at: string;
}

export interface IdentityRecord {
  name: string;
  phone: string;
}

export interface SessionRecord {
  started_at: string;
  finished_at: string;
  active_duration_seconds: number;
}

export interface ResponseRecord {
  display_position: number;
  pilot_item_id: string;
  choice: -2 | -1 | 0 | 1 | 2 | 99;
  reason_code: typeof REASON_CODES[number];
  confidence: number;
  reason_note: string;
  started_at: string;
  finished_at: string;
  active_duration_seconds: number;
}

export interface FeedbackRecord {
  fatigue_1to5: number;
  zero_vs_99_explanation: string;
  change_vs_stance_explanation: string;
  ui_error_note: string;
}

export interface SubmitRequest {
  action: "submit";
  invite_token: string;
  instrument_sha256: string;
  consent: ConsentRecord;
  identity: IdentityRecord;
  session: SessionRecord;
  responses: ResponseRecord[];
  feedback: FeedbackRecord;
  payload_sha256: string;
  idempotency_key: string;
}

function objectValue(value: unknown, field: string): JsonObject {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new ClientError(400, "INVALID_REQUEST", `${field} must be an object.`);
  }
  return value as JsonObject;
}

function exactKeys(
  value: JsonObject,
  required: readonly string[],
  optional: readonly string[],
  field: string,
): void {
  const allowed = new Set([...required, ...optional]);
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) {
      throw new ClientError(400, "INVALID_REQUEST", `${field}.${key} is not allowed.`);
    }
  }
  for (const key of required) {
    if (!(key in value)) {
      throw new ClientError(400, "INVALID_REQUEST", `${field}.${key} is required.`);
    }
  }
}

function textValue(
  value: unknown,
  field: string,
  minLength: number,
  maxLength: number,
  trim = true,
): string {
  if (typeof value !== "string") {
    throw new ClientError(400, "INVALID_REQUEST", `${field} must be text.`);
  }
  const normalized = (trim ? value.trim() : value).normalize("NFC");
  if (normalized.length < minLength || normalized.length > maxLength || CONTROL_CHARS.test(normalized)) {
    throw new ClientError(400, "INVALID_REQUEST", `${field} has an invalid length or character.`);
  }
  return normalized;
}

function integerValue(value: unknown, field: string, minimum: number, maximum: number): number {
  if (!Number.isInteger(value) || (value as number) < minimum || (value as number) > maximum) {
    throw new ClientError(400, "INVALID_REQUEST", `${field} must be an integer from ${minimum} to ${maximum}.`);
  }
  return value as number;
}

function isoTimestamp(value: unknown, field: string): string {
  const text = textValue(value, field, 20, 40);
  if (!/(?:Z|[+-]\d{2}:\d{2})$/.test(text) || !Number.isFinite(Date.parse(text))) {
    throw new ClientError(400, "INVALID_REQUEST", `${field} must be an ISO-8601 timestamp with a timezone.`);
  }
  return new Date(text).toISOString();
}

function instrumentHash(value: unknown): string {
  if (typeof value !== "string" || !HEX_64.test(value)) {
    throw new ClientError(400, "INVALID_INSTRUMENT", "instrument_sha256 must be a lowercase SHA-256 hex digest.");
  }
  return value;
}

function normalizePhone(value: unknown): string {
  let phone = textValue(value, "identity.phone", 8, 30).replace(/[\s().-]/g, "");
  if (phone.startsWith("+82")) phone = `0${phone.slice(3)}`;
  if (phone.startsWith("0082")) phone = `0${phone.slice(4)}`;
  if (!/^01[016789]\d{7,8}$/.test(phone)) {
    throw new ClientError(400, "INVALID_IDENTITY", "전화번호는 유효한 국내 휴대전화 번호여야 합니다.");
  }
  return phone;
}

export function validateLoadRequest(input: unknown): LoadRequest {
  const body = objectValue(input, "request");
  exactKeys(body, ["action", "invite_token", "instrument_sha256"], [], "request");
  if (body.action !== "load") {
    throw new ClientError(400, "INVALID_ACTION", "action must be load.");
  }
  return {
    action: "load",
    invite_token: textValue(body.invite_token, "invite_token", 43, 43, false),
    instrument_sha256: instrumentHash(body.instrument_sha256),
  };
}

export function validateSubmitRequest(
  input: unknown,
  expectedConsentVersion: string,
  nowMs = Date.now(),
): SubmitRequest {
  const body = objectValue(input, "request");
  exactKeys(body, [
    "action",
    "invite_token",
    "instrument_sha256",
    "consent",
    "identity",
    "session",
    "responses",
    "feedback",
    "payload_sha256",
    "idempotency_key",
  ], [], "request");
  if (body.action !== "submit") {
    throw new ClientError(400, "INVALID_ACTION", "action must be submit.");
  }

  const consentInput = objectValue(body.consent, "consent");
  exactKeys(consentInput, ["accepted", "version", "accepted_at"], [], "consent");
  if (consentInput.accepted !== true) {
    throw new ClientError(400, "CONSENT_REQUIRED", "Consent must be accepted before submission.");
  }
  const consent: ConsentRecord = {
    accepted: true,
    version: textValue(consentInput.version, "consent.version", 1, 80),
    accepted_at: isoTimestamp(consentInput.accepted_at, "consent.accepted_at"),
  };
  if (consent.version !== expectedConsentVersion) {
    throw new ClientError(409, "CONSENT_VERSION_MISMATCH", "The consent form has changed; reload before submitting.");
  }

  const identityInput = objectValue(body.identity, "identity");
  exactKeys(identityInput, ["name", "phone"], [], "identity");
  const identity: IdentityRecord = {
    name: textValue(identityInput.name, "identity.name", 1, 80),
    phone: normalizePhone(identityInput.phone),
  };

  const sessionInput = objectValue(body.session, "session");
  exactKeys(sessionInput, ["started_at", "finished_at", "active_duration_seconds"], [], "session");
  const session: SessionRecord = {
    started_at: isoTimestamp(sessionInput.started_at, "session.started_at"),
    finished_at: isoTimestamp(sessionInput.finished_at, "session.finished_at"),
    active_duration_seconds: integerValue(sessionInput.active_duration_seconds, "session.active_duration_seconds", 1, 21600),
  };
  const sessionStart = Date.parse(session.started_at);
  const sessionFinish = Date.parse(session.finished_at);
  if (sessionStart >= sessionFinish || sessionFinish > nowMs + 5 * 60_000) {
    throw new ClientError(400, "INVALID_TIMING", "Session timestamps are inconsistent.");
  }
  if (session.active_duration_seconds > (sessionFinish - sessionStart) / 1000 + 5) {
    throw new ClientError(400, "INVALID_TIMING", "Session active time exceeds wall-clock time.");
  }
  const acceptedAt = Date.parse(consent.accepted_at);
  if (acceptedAt > sessionFinish || acceptedAt > nowMs + 5 * 60_000) {
    throw new ClientError(400, "INVALID_TIMING", "Consent timestamp is inconsistent.");
  }

  if (!Array.isArray(body.responses) || body.responses.length !== RESPONSE_COUNT) {
    throw new ClientError(400, "INVALID_RESPONSES", `Exactly ${RESPONSE_COUNT} responses are required.`);
  }
  const positions = new Set<number>();
  const responses = body.responses.map((raw, index): ResponseRecord => {
    const response = objectValue(raw, `responses[${index}]`);
    exactKeys(response, [
      "display_position",
      "pilot_item_id",
      "choice",
      "reason_code",
      "confidence",
      "started_at",
      "finished_at",
      "active_duration_seconds",
    ], ["reason_note"], `responses[${index}]`);
    const displayPosition = integerValue(response.display_position, `responses[${index}].display_position`, 1, RESPONSE_COUNT);
    if (positions.has(displayPosition)) {
      throw new ClientError(400, "INVALID_RESPONSES", "Response positions must be unique.");
    }
    positions.add(displayPosition);
    const pilotItemId = textValue(response.pilot_item_id, `responses[${index}].pilot_item_id`, 1, 64);
    if (!ITEM_ID.test(pilotItemId)) {
      throw new ClientError(400, "INVALID_RESPONSES", "pilot_item_id has an invalid format.");
    }
    if (!Number.isInteger(response.choice) || !(STANCE_CHOICES as readonly number[]).includes(response.choice as number)) {
      throw new ClientError(400, "INVALID_RESPONSES", "choice must be -2, -1, 0, 1, 2, or 99.");
    }
    if (typeof response.reason_code !== "string" || !(REASON_CODES as readonly string[]).includes(response.reason_code)) {
      throw new ClientError(400, "INVALID_RESPONSES", "reason_code is invalid.");
    }
    const reasonCode = response.reason_code as typeof REASON_CODES[number];
    const reasonNote = response.reason_note === undefined
      ? ""
      : textValue(response.reason_note, `responses[${index}].reason_note`, 0, 1000);
    if (response.choice === 99 && reasonCode === "NONE") {
      throw new ClientError(400, "INVALID_RESPONSES", "Choice 99 requires a non-NONE reason code.");
    }
    if (reasonCode === "OTHER" && reasonNote.length === 0) {
      throw new ClientError(400, "INVALID_RESPONSES", "OTHER requires a reason note.");
    }
    if (reasonCode !== "OTHER" && reasonNote.length !== 0) {
      throw new ClientError(400, "INVALID_RESPONSES", "reason_note is only allowed with OTHER.");
    }
    const startedAt = isoTimestamp(response.started_at, `responses[${index}].started_at`);
    const finishedAt = isoTimestamp(response.finished_at, `responses[${index}].finished_at`);
    const startedMs = Date.parse(startedAt);
    const finishedMs = Date.parse(finishedAt);
    const activeSeconds = integerValue(response.active_duration_seconds, `responses[${index}].active_duration_seconds`, 0, 21600);
    if (startedMs < sessionStart - 1000 || finishedMs > sessionFinish + 1000 || startedMs >= finishedMs) {
      throw new ClientError(400, "INVALID_TIMING", "Item timestamps must fall within the session.");
    }
    if (activeSeconds > (finishedMs - startedMs) / 1000 + 1) {
      throw new ClientError(400, "INVALID_TIMING", "Item active time exceeds wall-clock time.");
    }
    return {
      display_position: displayPosition,
      pilot_item_id: pilotItemId,
      choice: response.choice as ResponseRecord["choice"],
      reason_code: reasonCode,
      confidence: integerValue(response.confidence, `responses[${index}].confidence`, 1, 5),
      reason_note: reasonNote,
      started_at: startedAt,
      finished_at: finishedAt,
      active_duration_seconds: activeSeconds,
    };
  }).sort((a, b) => a.display_position - b.display_position);
  const totalItemActiveSeconds = responses.reduce((total, response) => total + response.active_duration_seconds, 0);
  if (totalItemActiveSeconds > session.active_duration_seconds + 5) {
    throw new ClientError(400, "INVALID_TIMING", "Total item active time exceeds session active time.");
  }

  const feedbackInput = objectValue(body.feedback, "feedback");
  exactKeys(feedbackInput, [
    "fatigue_1to5",
    "zero_vs_99_explanation",
    "change_vs_stance_explanation",
  ], ["ui_error_note"], "feedback");
  const feedback: FeedbackRecord = {
    fatigue_1to5: integerValue(feedbackInput.fatigue_1to5, "feedback.fatigue_1to5", 1, 5),
    zero_vs_99_explanation: textValue(feedbackInput.zero_vs_99_explanation, "feedback.zero_vs_99_explanation", 1, 2000),
    change_vs_stance_explanation: textValue(feedbackInput.change_vs_stance_explanation, "feedback.change_vs_stance_explanation", 1, 2000),
    ui_error_note: feedbackInput.ui_error_note === undefined
      ? ""
      : textValue(feedbackInput.ui_error_note, "feedback.ui_error_note", 0, 2000),
  };

  const researchFreeText = [
    ...responses.map((response) => response.reason_note),
    feedback.zero_vs_99_explanation,
    feedback.change_vs_stance_explanation,
    feedback.ui_error_note,
  ].join("\n");
  const compactResearchText = researchFreeText.replace(/\s/g, "");
  const identityName = identity.name.replace(/\s/g, "");
  const researchDigits = researchFreeText.replace(/\D/g, "");
  if (
    (identityName.length >= 2 && compactResearchText.includes(identityName)) ||
    (identity.phone.length >= 8 && researchDigits.includes(identity.phone))
  ) {
    throw new ClientError(
      400,
      "IDENTIFIER_IN_RESEARCH_TEXT",
      "이름이나 전화번호를 문항 메모 또는 사용성 피드백에 반복 입력하지 마세요.",
    );
  }

  const payloadSha256 = typeof body.payload_sha256 === "string" ? body.payload_sha256.toLowerCase() : "";
  if (!HEX_64.test(payloadSha256)) {
    throw new ClientError(400, "INVALID_PAYLOAD_HASH", "payload_sha256 must be a SHA-256 hex digest.");
  }
  if (typeof body.idempotency_key !== "string" || !UUID_V4.test(body.idempotency_key)) {
    throw new ClientError(400, "INVALID_IDEMPOTENCY_KEY", "idempotency_key must be a UUID v4.");
  }

  return {
    action: "submit",
    invite_token: textValue(body.invite_token, "invite_token", 43, 43, false),
    instrument_sha256: instrumentHash(body.instrument_sha256),
    consent,
    identity,
    session,
    responses,
    feedback,
    payload_sha256: payloadSha256,
    idempotency_key: body.idempotency_key.toLowerCase(),
  };
}

export function decodeBase64Url256(value: string, field = "invite_token"): Uint8Array {
  if (!/^[A-Za-z0-9_-]{43}$/.test(value)) {
    throw new ClientError(400, "INVALID_INVITE", `${field} must encode exactly 256 bits.`);
  }
  const base64 = value.replace(/-/g, "+").replace(/_/g, "/") + "=";
  let binary: string;
  try {
    binary = atob(base64);
  } catch {
    throw new ClientError(400, "INVALID_INVITE", `${field} is not valid base64url.`);
  }
  const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
  if (bytes.byteLength !== 32) {
    throw new ClientError(400, "INVALID_INVITE", `${field} must encode exactly 256 bits.`);
  }
  let canonical = "";
  for (const byte of bytes) canonical += String.fromCharCode(byte);
  canonical = btoa(canonical).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  if (canonical !== value) {
    throw new ClientError(400, "INVALID_INVITE", `${field} is not canonical base64url.`);
  }
  return bytes;
}

export function decodeSecret256(value: string, field: string): Uint8Array {
  const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
  if (!/^[A-Za-z0-9+/]+={0,2}$/.test(normalized)) {
    throw new Error(`${field} is not valid base64.`);
  }
  const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "=");
  let binary: string;
  try {
    binary = atob(padded);
  } catch {
    throw new Error(`${field} is not valid base64.`);
  }
  const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
  if (bytes.byteLength !== 32) throw new Error(`${field} must encode exactly 256 bits.`);
  return bytes;
}

function bytesToHex(bytes: Uint8Array): string {
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export async function hmacSha256Hex(secret: Uint8Array, domain: string, payload: Uint8Array | string): Promise<string> {
  const key = await crypto.subtle.importKey("raw", secret, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const prefix = new TextEncoder().encode(`${domain}\0`);
  const content = typeof payload === "string" ? new TextEncoder().encode(payload) : payload;
  const message = new Uint8Array(prefix.length + content.length);
  message.set(prefix);
  message.set(content, prefix.length);
  const signature = await crypto.subtle.sign("HMAC", key, message);
  return bytesToHex(new Uint8Array(signature));
}

export function canonicalJson(value: unknown): string {
  if (value === null || typeof value === "boolean" || typeof value === "string") return JSON.stringify(value);
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new Error("Non-finite numbers are not valid JSON.");
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (typeof value === "object") {
    const object = value as Record<string, unknown>;
    return `{${Object.keys(object).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(object[key])}`).join(",")}}`;
  }
  throw new Error("Unsupported canonical JSON value.");
}

export function payloadDigestBasis(request: SubmitRequest): JsonObject {
  return {
    consent: request.consent,
    feedback: request.feedback,
    instrument_sha256: request.instrument_sha256,
    responses: request.responses,
    session: request.session,
  };
}

export async function sha256Hex(value: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return bytesToHex(new Uint8Array(digest));
}

export async function encryptJson(
  keyBytes: Uint8Array,
  value: unknown,
  aad: string,
): Promise<{ ciphertext_b64: string; iv_b64: string }> {
  const key = await crypto.subtle.importKey("raw", keyBytes, "AES-GCM", false, ["encrypt"]);
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv, additionalData: new TextEncoder().encode(aad), tagLength: 128 },
    key,
    new TextEncoder().encode(JSON.stringify(value)),
  );
  const encode = (bytes: Uint8Array): string => {
    let binary = "";
    for (const byte of bytes) binary += String.fromCharCode(byte);
    return btoa(binary);
  };
  return { ciphertext_b64: encode(new Uint8Array(ciphertext)), iv_b64: encode(iv) };
}
