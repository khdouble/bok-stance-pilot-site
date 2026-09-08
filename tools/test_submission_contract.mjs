import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import { webcrypto } from "node:crypto";

const repository = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const fixture = JSON.parse(
  await readFile(path.join(repository, "supabase/tests/fixtures/submission_digest_golden.json"), "utf8"),
);
const contractSource = await readFile(path.join(repository, "docs/submission-contract.js"), "utf8");
const context = vm.createContext({ crypto: webcrypto, TextEncoder, Uint8Array });
vm.runInContext(contractSource, context, { filename: "docs/submission-contract.js" });
const contract = context.PILOT_SUBMISSION_CONTRACT;
assert.ok(contract, "frontend submission contract was not exported");

const finalized = await contract.finalizeDigest(fixture.raw_digest_input);
const basis = JSON.parse(JSON.stringify(finalized.basis));
const expected = fixture.expected;

assert.equal(basis.consent.version, expected.consent_version);
assert.equal(basis.consent.accepted_at, expected.consent_accepted_at);
assert.equal(basis.session.started_at, expected.session_started_at);
assert.equal(basis.session.finished_at, expected.session_finished_at);
assert.deepEqual(
  basis.responses.map((response) => response.display_position),
  expected.response_positions,
);
assert.equal(basis.responses[0].pilot_item_id, expected.first_item_id);
assert.equal(basis.responses[0].reason_note, expected.first_reason_note);
assert.equal(basis.responses[1].reason_note, expected.second_reason_note);
assert.equal(basis.feedback.zero_vs_99_explanation, expected.zero_vs_99_explanation);
assert.equal(basis.feedback.ui_error_note, expected.ui_error_note);
assert.equal(finalized.payload_sha256, expected.payload_sha256);
assert.equal(
  await contract.sha256Hex(contract.stableStringify(basis)),
  expected.payload_sha256,
);

console.log(`PASS frontend submission golden vector sha256=${expected.payload_sha256}`);

const withQuality = structuredClone(fixture.raw_digest_input);
withQuality.responses[0].item_quality_code = "TOO_OBVIOUS";
withQuality.responses[0].item_quality_note = "";
const qualityBasis = JSON.parse(JSON.stringify((await contract.finalizeDigest(withQuality)).basis));
assert.equal(qualityBasis.responses[11].item_quality_code, "TOO_OBVIOUS");
assert.equal(qualityBasis.responses[11].item_quality_note, "");
console.log("PASS frontend item-quality digest coverage");