# Supabase pilot backend

This directory implements the server boundary for the hosted usability pilot. GitHub Pages serves only static survey files. The browser sends `POST` requests to `pilot-api`; it receives no Supabase publishable, anonymous, service-role, database, HMAC, or encryption key.

The backend is intentionally fail-closed. Fielding cannot begin until the final hosted instrument is seeded, activated, and matched by the function environment; the privacy notice and consent text have a fixed internal version; the disposable remote E2E has passed and been cleaned up; exactly five production invitations are provisioned; and the PI separately changes the database `fielding_open` gate from its default `false` value. An internal consent version identifies the text shown to participants; it does not by itself assert institutional ethics approval.

## Data boundary

- `private.pilot_invites` stores only HMAC-SHA-256 digests of 256-bit invite tokens. Raw tokens remain in an external OS-local protected directory and are sent individually. Operational secrets and direct PII are forbidden anywhere inside the repository, including ignored `.private/` paths.
- `private.participant_identity` stores only name and normalized phone number encrypted together with AES-256-GCM. Consent version and timestamp are also private.
- `research.pilot_submissions` and `research.pilot_responses` contain pseudonymous IDs, assigned item IDs, answers, timing, and usability feedback. They contain no name, phone number, raw token, user-agent, or ciphertext. The function neither accepts nor stores a browser user-agent field.
- `private.pilot_withdrawal_events` records only the study hash, internal procedure version, selector type, timestamps, outcome, deletion counts, and successful verification. It contains no participant selector, name, phone, invite/participant/submission ID, HMAC, ciphertext, or free-text note.
- Direct identity is excluded from the response payload digest. Server validation also rejects the submitted name or phone when repeated in a research free-text field.
- Both schemas are excluded from the Data API in `config.toml`. Every table has RLS enabled and forced, no permissive policy, and no `PUBLIC`, `anon`, `authenticated`, or `service_role` grant. The hosted project's Data API should also be switched off in Dashboard because this application does not use it.
- The Edge Function connects over the server-only `SUPABASE_DB_URL` and writes identity, submission, 12 responses, and invite consumption in one database transaction. There is no exposed SQL RPC.

This separation prevents an analysis export from accidentally containing direct identifiers. It does not replace the PI's retention/deletion policy or institutional ethics determination.

## Fixed release inputs

- Immutable applied baseline: `v260903-pilot-hosted-1` / `4a07da2785bb2228787f2dd4e57339bd5c132111d693681a12cb62baf64978e7`
- Local staged append-only release candidate: `v260903-pilot-hosted-2`
- Offline source instrument: `b594a196eb7be720e57d974f4b5c6e4437b697e6ae20f01013e830af35707a51`
- Hosted instrument SHA-256: the canonical `instrument_sha256` in `docs/instrument.json` (read it at execution time; do not copy it into export code)
- Allowed browser origin: exactly `https://khdouble.github.io`
- Items per invitation: exactly 12

`tools/render_instrument_seed.py` generated `migrations/202609030002_seed_pilot_instrument.sql` for the immutable v1 baseline: 1 instrument, 12 items, 5 assignment sets, and 60 ordered assignment rows. Migration 002 has raw-byte SHA-256 `c0a8116c0bc8551a4ac0fb77bf776385bc002ad6f26d8b575e83adbac353ebd2`; never edit or regenerate it from the moving `docs/instrument.json`.

The v1 identity and research-payload commitment are frozen in `instrument_history/v260903-pilot-hosted-1.instrument.json`. `tools/render_instrument_transition.py` consumes that commitment and the current v2 `docs/instrument.json`, verifies the immutable 002 bytes and unchanged 12/5/60 research payload, and renders migration 005 once without overwrite. Because the v1 and v2 research basis is identical, the renderer also materializes the expected v1 item text, assignment-set, and code/position/item/assignment mappings as literal preconditions. The instrument contains an exact 11-key `release_source_hashes` map. Migration 005 is deliberately excluded from that map to avoid a circular hash; `deployment-manifest.json` binds the exact raw 005 SHA-256 separately as `deployment_source_hashes.database_instrument_transition`.

Migration 005 first locks the full pilot relation set and validates exact v1 metadata plus the complete v1 research rows with bidirectional `EXCEPT`; shape-preserving sentence or assignment-mapping drift is therefore fatal before any v2 row is inserted. It then inserts v2 inactive and closed, validates its exact 12 items, 5 assignment sets, and 60 assignments, and atomically deactivates v1 and activates v2. It also requires the exact migration-004 column/check/index contract, v2 absence, and globally empty invite, identity, submission, and response tables. It rechecks the sole-active closed v2 state and all-zero mutable tables before commit. Instrument activation never opens fielding.

Migration files are append-only after they have been applied:

- `202609030001_pilot_backend.sql` creates the private and research data boundary.
- `202609030002_seed_pilot_instrument.sql` seeds the immutable hosted instrument and leaves fielding closed.
- `202609030003_pilot_withdrawal_audit.sql` adds the private, forced-RLS, grant-free non-identifying withdrawal completion log.
- `202609030004_pi_manual_test_credentials.sql` adds purpose-bound, digest-only PI manual-test credentials and the distinct synthetic-analysis exclusion. It intentionally refuses a nonempty invitation table and refuses replay against a schema where either new column already exists.
- `202609030005_activate_hosted_instrument_v2.sql` is the one-time append-only v1-to-v2 active-instrument transition. It is renderer-owned and must match the current v2 artifact byte for byte.

## API contract

Endpoint:

`https://mebisrsvasrzwkmsodsw.supabase.co/functions/v1/pilot-api`

Only `POST` and `OPTIONS` from `https://khdouble.github.io` are accepted. Do not send an `apikey` or `Authorization` header.

Load request:

```json
{
  "action": "load",
  "invite_token": "43-character unpadded base64url token",
  "instrument_sha256": "64-character lowercase hosted release hash"
}
```

Successful load returns the fixed version, hash, assignment code, and 12 ordered objects with `assignment_id`, `display_position`, `pilot_item_id`, and `sentence_text`.

Submit request:

```json
{
  "action": "submit",
  "invite_token": "43-character unpadded base64url token",
  "instrument_sha256": "64-character lowercase hosted release hash",
  "consent": {
    "accepted": true,
    "version": "consent-vYYYY-MM-DD-rN internal text version",
    "accepted_at": "ISO-8601 timestamp with timezone"
  },
  "identity": { "name": "participant name", "phone": "010-0000-0000" },
  "session": {
    "started_at": "ISO-8601 timestamp with timezone",
    "finished_at": "ISO-8601 timestamp with timezone",
    "active_duration_seconds": 600
  },
  "responses": [
    {
      "display_position": 1,
      "pilot_item_id": "assigned item ID",
      "choice": 0,
      "reason_code": "NONE",
      "confidence": 3,
      "reason_note": "",
      "started_at": "ISO-8601 timestamp with timezone",
      "finished_at": "ISO-8601 timestamp with timezone",
      "active_duration_seconds": 20
    }
  ],
  "feedback": {
    "fatigue_1to5": 2,
    "zero_vs_99_explanation": "required text",
    "change_vs_stance_explanation": "required text",
    "ui_error_note": "optional text"
  },
  "payload_sha256": "canonical payload digest",
  "idempotency_key": "random UUID v4"
}
```

`responses` must contain exactly 12 unique positions. The server compares every ordered item ID with the invitation's database assignment inside the submission transaction. Choices are `-2`, `-1`, `0`, `1`, `2`, or `99`; confidence is 1--5; reason codes follow the frozen offline protocol. A `99` choice requires a non-`NONE` reason, and `OTHER` alone permits and requires `reason_note`.

The payload digest is lowercase SHA-256 of canonical JSON for this exact object, excluding identity, invite token, and idempotency key:

```text
{
  consent,
  feedback,
  instrument_sha256,
  responses,
  session
}
```

Canonical JSON recursively sorts object keys lexicographically, retains array order, and uses ordinary JSON primitive encoding. The frontend should sort responses by `display_position` before hashing. `_shared/core.ts` is the normative implementation.

A repeated submit is successful only when the invitation, UUID v4 idempotency key, payload digest, and private identity HMAC all match the first committed submission. Any changed retry is rejected.

## Deployment sequence

The mandatory compatibility order for this release is **migration 004 -> migration 005 -> Edge Function -> GitHub Pages**. Never expose the PI page before its server contract is deployed, and never deploy the new Edge code before both its migration-004 schema and active v2 instrument exist.

1. Link the CLI to project ref `mebisrsvasrzwkmsodsw`, run the complete test suite, and require zero release-validator failures. Confirm migration 002 still has its pinned raw hash, migration 005 exactly equals the deterministic renderer output, the v2 instrument has the exact 11-source map, and the deployment manifest binds 005 separately.
2. Before migration 004, verify migration history is exactly 001--003, the sole instrument is the exact v1 hash/version with `is_active=true` and `fielding_open=false`, its exact item text and assignment mapping match the frozen research basis (not merely the 12/5/60 counts), v2 is absent, and sanitized global counts are exactly `invite_count=0`, `identity_count=0`, `submission_count=0`, and `response_count=0`. Any mismatch is a blocker.
3. Run `supabase db push --dry-run` and require exactly 004 followed by 005, with no other migration. Apply once, then confirm local/remote 001--005 parity. Repeat a read-only status check: v1 must be inactive/closed, v2 must be the sole active instrument and closed, both instrument shapes must be 12/5/60, and all four global mutable counts must remain zero.
4. Generate and validate the exactly seven custom function secrets in an external, access-controlled, non-synced directory. Upload that file without displaying or copying its values.
5. Deploy `pilot-api`, confirm the deployed function version, and run only the sanitized CORS/malformed-request probes. A controlled `400` proves routing; `404` or `500` is a deployment blocker. Before any credential is created, an `admin_load` request with a syntactically valid nonexistent credential must return the generic `403 ADMIN_AUTH_FAILED` while the gate remains closed.
6. Publish GitHub Pages last, still with `fieldingEnabled=false` and `remoteE2eVerifiedAt=PENDING_PI`. Verify the deployed manifest, static bytes, exact 11-source map, and separately bound 005 hash. Publishing `admin.html` does not open fielding and grants no administration capability.
7. Manually disable the hosted project's Data API in the Supabase Dashboard and record the check. `config.toml`, RLS, and revokes are necessary defenses but do not prove the hosted Dashboard switch is off.
8. Provision exactly one short-lived `disposable-e2e` invitation, apply its digest-only seed, temporarily open only the E2E database gate, and exercise the real Pages assets and deployed function through `tools/run_remote_e2e.py`. The browser intercepts all requests before sending, fails every destination outside the exact allowlist, and fulfills only the exact `site-config.js` request with the two-field in-memory override. Do not publish or fabricate `remoteE2eVerifiedAt` before success.
9. Treat a successful browser run as pending, not complete: the harness writes `E2E_PASSED_CLEANUP_PENDING` and deliberately exits `3`. The receipt's `tested_config_timestamp` is exactly the UTC second injected into the tested in-memory config. In a `finally` path after a valid disposable invite is loaded, the harness attempts to close the gate and revoke that invite on both success and failure without replacing the original failure. If automatic close reports a warning, run `close-e2e` manually at once.
10. Deliberately remove the disposable test records with the individual-withdrawal tool: preview the invite-scoped deletion, use its exact generated confirmation, and verify that identity, submission, responses, and the disposable invitation are absent. Automatic close/revoke is not database-row cleanup.
11. Run `tools/finalize_remote_e2e.py` with the pending receipt and original private provision file. It re-attests the same staged assets and exact tested timestamp, checks the gate and target rows read-only, and only then writes the non-identifying `E2E_VERIFIED_CLEAN` receipt. Its `fielding_authorized` remains `false`.
12. Copy the clean receipt's unchanged `tested_config_timestamp` to `remoteE2eVerifiedAt`, rebuild and validate the live static release and manifest, publish it, and verify the deployed operational file hashes.
13. Provision exactly five `production` invitations for `PILOT_R01` through `PILOT_R05`, apply their digest-only seed, and inspect gate status.
14. Open production with `manage_fielding_gate.py` only after its local live-release check and all server-side eligibility checks pass. Send each raw invitation URL separately. The database gate is the final switch.

Do not retry either migration after an ambiguous `db push`. First inspect migration history, the exact migration-004 catalog contract, both instrument rows and shapes, the closed gate, and the four global mutable counts. Applied migrations 002, 004, and 005 remain append-only. If 005 committed but a later deployment fails, keep HOLD; an old Edge or Pages release is expected to reject the new active hash. Repair and redeploy the matching v2 release. If a reviewed return to v1 is unavoidable, implement a new forward-fix migration 006 that requires v2 active/closed and globally all-zero mutable tables, then atomically switches v2 off and v1 on. Never edit, delete, rerun, or down-migrate 002/004/005.

## Generate and upload exactly seven custom secrets

Use `admin/generate_function_secrets.py`; do not hand-compose the file. It reads the canonical instrument hash/version, validates the internal consent version, creates three mutually different 32-byte keys from the operating system random source, writes atomically, refuses overwrite by default, and validates the result. `generate` and `validate` print only a count and protected file path, never secret values:

```powershell
$pilotPrivate = Join-Path $env:LOCALAPPDATA "bok-stance-pilot"
$env:BOK_PILOT_PRIVATE_DIR = $pilotPrivate
python -X utf8 supabase/admin/generate_function_secrets.py generate `
  --private-root $pilotPrivate `
  --output (Join-Path $pilotPrivate "pilot-function.env") `
  --consent-version consent-v2026-09-03-r1
python -X utf8 supabase/admin/generate_function_secrets.py validate `
  --private-root $pilotPrivate `
  --input (Join-Path $pilotPrivate "pilot-function.env")
supabase secrets set `
  --project-ref mebisrsvasrzwkmsodsw `
  --env-file (Join-Path $pilotPrivate "pilot-function.env")
```

The custom file contains exactly:

- `PILOT_INSTRUMENT_SHA256`
- `PILOT_INSTRUMENT_VERSION`
- `PILOT_CONSENT_VERSION`
- `INVITE_HMAC_SECRET_B64`
- `IDENTITY_HMAC_SECRET_B64`
- `PII_ENCRYPTION_KEY_B64`
- `PII_KEY_ID`

Hosted Supabase supplies the reserved `SUPABASE_DB_URL` to the Edge Function. It must be absent from the seven-secret custom env file; the validator rejects unexpected names. Local administrator scripts that need a direct Postgres connection read `SUPABASE_DB_URL` only from the current process. Load it from the approved credential manager without putting it in command arguments, shell history, source files, logs, GitHub, Google Drive, or `pilot-function.env`, and remove it from the process after the operation:

```powershell
# Load SUPABASE_DB_URL into this process from the protected credential source.
# Run the required admin command, then clear the process copy:
Remove-Item Env:SUPABASE_DB_URL -ErrorAction SilentlyContinue
```

`--overwrite` on the generator rotates all three cryptographic keys. Do not use it after invitations or identities exist unless a separately reviewed rotation/migration procedure is ready.

## Deploy and verify the Edge Function

Deploy without browser JWT verification because invitation HMAC validation is the application authentication boundary:

```powershell
supabase functions deploy pilot-api `
  --project-ref mebisrsvasrzwkmsodsw `
  --no-verify-jwt
supabase functions list --project-ref mebisrsvasrzwkmsodsw
curl.exe -i -X POST `
  "https://mebisrsvasrzwkmsodsw.supabase.co/functions/v1/pilot-api" `
  -H "Origin: https://khdouble.github.io" `
  -H "Content-Type: application/json" `
  --data-binary '{"action":"invalid"}'
```

The probe must return the function's controlled `400 INVALID_ACTION` response with the exact allowed CORS origin. It contains no invite or participant data and is not a substitute for E2E. Confirm the deployed function version/time in the CLI or Dashboard without exporting logs or secret metadata into the repository.

The hosted Data API switch is a separate manual check: in the Supabase Dashboard, open the project's Data API settings, turn the Data API off, and record the date/time and reviewer outside the public repository. Do not infer the remote setting from local `config.toml`. The Edge Function uses the hosted reserved direct database URL, so it does not require the Data API.

## Provision disposable and production invitations

Run from the repository root after loading only `INVITE_HMAC_SECRET_B64` into the current process from the protected secret source. All output paths must be children of the external private root. Read the current hosted hash dynamically:

```powershell
$pilotPrivate = Join-Path $env:LOCALAPPDATA "bok-stance-pilot"
$env:BOK_PILOT_PRIVATE_DIR = $pilotPrivate
$instrumentHash = (python -X utf8 -c "import json; print(json.load(open('docs/instrument.json', encoding='utf-8'))['instrument_sha256'])").Trim()
$e2eExpiry = [DateTimeOffset]::UtcNow.AddHours(2).ToString("yyyy-MM-ddTHH:mm:ssZ")
python supabase/admin/provision_invites.py `
  --private-root $pilotPrivate `
  --output (Join-Path $pilotPrivate "invite-e2e") `
  --instrument-sha256 $instrumentHash `
  --expires-at $e2eExpiry `
  --mode disposable-e2e `
  --assignment-code PILOT_R01
```

`disposable-e2e` requires exactly one assignment and refuses an expiry more than 24 hours away. Its SQL is visibly marked as disposable. After E2E cleanup, provision production separately:

```powershell
$productionExpiry = "2026-10-30T23:59:59+09:00"
python supabase/admin/provision_invites.py `
  --private-root $pilotPrivate `
  --output (Join-Path $pilotPrivate "invites-production-v1") `
  --instrument-sha256 $instrumentHash `
  --expires-at $productionExpiry `
  --mode production
```

`production` requires exactly five unique assignments and defaults to `PILOT_R01` through `PILOT_R05`. The command refuses every repository-internal path (including `.private/`), any path outside the selected external private root, overwrite, and any noncanonical project URL. It creates:

- `invite_links.private.json`: `provisioning_mode`, invitation UUIDs, assignments, raw tokens/URLs, and expiry; confidential and never printed by the script
- `invite_seed.private.sql`: invitation UUIDs and HMAC digests, instrument/assignment identity, and expiry; it never contains raw tokens but remains private operational material

Tokens use URL fragments (`#invite=...`), so GitHub Pages and HTTP referrer
headers do not receive them. The frontend removes the fragment immediately and
keeps the raw token only in JavaScript memory. A reload therefore requires the
original invitation link.

## PI manual-test credential (synthetic, no admin privilege)

`docs/admin.html` is a dedicated usability-test client, not an administration console. Its `admin_load` and `admin_submit` requests carry a generated ID and 256-bit password only in an exact POST JSON body. The browser holds them only in memory, clears the login controls after load, and uses no URL credential, cookie, session token, log, screenshot, or local storage. The server accepts this purpose only while the active instrument has `fielding_open=false`; participant invitations work only while it is true. Both values are stored in the database only as separately domain-prefixed HMAC digests. Every resulting submission is forced to `synthetic_pi_manual_test`, `excluded_from_analysis=true`, and `pi_manual_test_never_analysis`.

Create at most one credential only after migration 004 and the matching Edge Function are deployed, Pages remains on HOLD, and the closed/all-zero precheck above passes. Load `INVITE_HMAC_SECRET_B64` only into the current process and generate the files below the external private root:

```powershell
$piExpiry = [DateTimeOffset]::UtcNow.AddHours(2).ToString("yyyy-MM-ddTHH:mm:ssZ")
python -X utf8 supabase/admin/provision_invites.py `
  --private-root $pilotPrivate `
  --output (Join-Path $pilotPrivate "pi-manual-test") `
  --instrument-sha256 $instrumentHash `
  --expires-at $piExpiry `
  --mode pi-manual-test `
  --assignment-code PILOT_R01 `
  --seed-via linked-cli
```

Review and type the generated exact seed confirmation. The linked backend performs one serializable, digest-only mutation, requires the gate to be exactly false, verifies the active version and 12-item `PILOT_R01`, requires zero other outstanding PI credential, and independently checks the committed target. If the CLI reports `DO NOT RETRY; RUN LINKED STATUS`, do not seed again: retain the protected files, inspect only sanitized target/aggregate state, and either use or delete the one confirmed row.

Open the protected `pi_manual_test.private.json` locally and type its ID/password into the published `/bok-stance-pilot-site/admin.html` page. Never paste the credential into chat, email, a command, an issue, or a repository file. The credential expires within 24 hours, becomes one-time-used on the first committed submit, permits only an identical idempotent retry, and grants no database, gate, export, or site administration access.

Whether the PI submits or abandons the test, remove the database row promptly. Read the invitation UUID locally from the protected credential file, then preview with the linked backend and bind the cleanup to its exact purpose:

```powershell
python -X utf8 supabase/admin/delete_withdrawn_participant.py `
  --db-backend linked-cli `
  --expected-purpose pi_manual_test `
  --instrument-sha256 $instrumentHash `
  --procedure-version withdrawal-v2026-09-03-r1 `
  --request-received-at $requestReceivedAt `
  --invite-id $piInviteId `
  --dry-run
```

Re-run with the complete generated `--confirm` phrase. The same serializable transaction deletes either the unused credential alone or the linked 12-response synthetic submission, encrypted identity, and credential; verifies target absence; and writes only the non-identifying withdrawal audit. Require `fielding_open=false` and `invite_count=0`, `identity_count=0`, `submission_count=0`, and `response_count=0` afterward. Only then dispose of the external raw credential files under the approved local retention procedure. Never open production while a PI credential or its synthetic submission remains.

## Inspect and change the fielding gate

`admin/manage_fielding_gate.py` accepts five modes:

- `status`: read-only; prints gate/invitation/submission counts and rejects `--confirm`
- `open-e2e`: requires `--invite-id`, zero submissions, and exactly that one eligible unrevoked invitation
- `close-e2e`: closes the gate and revokes exactly the specified disposable invitation
- `open-production`: requires zero submissions, exactly five eligible unrevoked `PILOT_R01..PILOT_R05` invitations, and a locally valid live PI config and matching live deployment manifest
- `close`: closes any open fielding gate

Every mutation requires its exact full confirmation phrase:

```text
OPEN E2E FIELDING <instrument-sha256> <invite-id>
CLOSE E2E FIELDING AND REVOKE <instrument-sha256> <invite-id>
OPEN PRODUCTION FIELDING <instrument-sha256>
CLOSE FIELDING <instrument-sha256>
```

Example operator sequence:

```powershell
# SUPABASE_DB_URL must already exist only in this process.
python -X utf8 supabase/admin/manage_fielding_gate.py status `
  --instrument-sha256 $instrumentHash
python -X utf8 supabase/admin/manage_fielding_gate.py open-e2e `
  --instrument-sha256 $instrumentHash `
  --invite-id $inviteId `
  --confirm "OPEN E2E FIELDING $instrumentHash $inviteId"
# Run the disposable browser-only remote E2E. Exit 3 means browser PASS,
# automatic close/revoke attempted, and deliberate row cleanup is still pending.
$e2eInviteFile = Join-Path $pilotPrivate "invite-e2e\invite_links.private.json"
$e2eIdentityFile = Join-Path $pilotPrivate "remote-e2e-identity.private.json"
$pendingE2eReceipt = Join-Path $pilotPrivate "remote-e2e-receipt.json"
python -X utf8 tools/run_remote_e2e.py `
  --private-root $pilotPrivate `
  --invite-file $e2eInviteFile `
  --identity-file $e2eIdentityFile `
  --receipt $pendingE2eReceipt
if ($LASTEXITCODE -ne 3) {
  throw "Remote E2E did not reach the cleanup-pending state."
}
python -X utf8 supabase/admin/manage_fielding_gate.py status `
  --instrument-sha256 $instrumentHash
```

Each gate mutation locks and rechecks the instrument/invitations in one transaction, verifies the resulting gate state, and fails closed on count or state drift. The E2E harness invokes the same `close-e2e` mutation directly in `finally` once it has validated the private disposable invite, including when later identity, attestation, browser, or receipt steps fail. Cleanup errors are reported separately and do not mask the original E2E error. Always run `status` independently; if automatic close/revoke warns or cannot be verified, run the documented `close-e2e` command manually before diagnosis.

## Two-phase remote E2E evidence and cleanup

The hosted harness uses the real GitHub Pages origin, exact deployed static bytes, and the real Edge Function. It does not publish a temporary live config. Before navigation it compares every required deployed asset with the local staged release and verifies the deployment manifest, instrument digest, release-source hashes, and operational-file hashes. In the browser, CDP `Fetch` pauses every request at the request stage. Only exact static asset URLs and the exact API URL/method are continued; the exact config script alone is fulfilled from memory. An unexpected destination, method, resource type, or token/PII in a URL or header is failed before transmission. Raw token and test identity are permitted only in the body of an exact API `POST` and are never printed, placed in a receipt, or accepted as CLI values.

The harness checks the 12 server assignments, deterministic valid answers, initial success, an identical idempotent retry, rejection of a changed retry, fragment removal, cleared identity inputs, cleared local draft, and zero JavaScript exceptions. It records the exact injected UTC second as `tested_config_timestamp`; it does not substitute a later receipt-writing time. Browser success produces only a protected external pending receipt and exit code `3`, even when automatic close/revoke succeeds, because submitted database rows still require deliberate deletion and independent absence verification.

Use `admin/delete_withdrawn_participant.py --invite-id ... --dry-run` first. Review its scoped counts and exact confirmation phrase, then rerun with that phrase. This single transaction deletes the disposable responses, submission, encrypted identity, and invitation in foreign-key-safe order, verifies absence, and records only the non-identifying migration-003 audit event. Do not treat gate closure, invite revocation, or a pending receipt as deletion.

After deletion, finalize through paths below the same external private root; never pass the raw token or test identity on the command line:

```powershell
$cleanE2eReceipt = Join-Path $pilotPrivate "remote-e2e-clean-receipt.json"
python -X utf8 tools/finalize_remote_e2e.py `
  --private-root $pilotPrivate `
  --pending-receipt $pendingE2eReceipt `
  --invite-file $e2eInviteFile `
  --output $cleanE2eReceipt
```

The finalizer refuses a pending receipt older than 24 hours, a changed provision file or deployed release, an open gate, any remaining disposable invite (including a revoked row), or any remaining target identity/submission/response. A successful run writes a new `E2E_VERIFIED_CLEAN` receipt with `cleanup_verified=true`, `database_zero_verified=true`, `cleanup_required=false`, and `fielding_authorized=false`; it contains no invite ID or participant-linked value. Only this clean receipt may support copying its unchanged `tested_config_timestamp` into the public config. Production still requires the separate live validator and explicit production gate.

## PII-free hosted research export

Install `psycopg` only in the administrator environment and set the server-only `SUPABASE_DB_URL` in the current process. This one-step export reads the hash and version from the current `docs/instrument.json`; obtains responses and feedback through two parameterized, PII-free queries in one repeatable-read, read-only database transaction; applies the hosted validator; and writes only formula-safe validated files under the ignored `exports/` directory:

```powershell
python -m pip install "psycopg[binary]"
python -X utf8 supabase/admin/export_hosted_research.py `
  --expected-submissions 5 `
  --output exports/pilot_YYYYMMDD
```

The output contains `hosted_responses_validated.csv` (12 rows per submitted invitation), `hosted_feedback_validated.csv` (one row per submission), and `hosted_collection_manifest.json`. Exact headers intentionally exclude name, phone, participant/invite identifiers, consent fields, ciphertext, token material, and user-agent. The manifest preserves the hosted `instrument_sha256` and `instrument_version`, records source/output file digests, and declares the usability-only analysis exclusion.

`admin/research_responses_export.sql` and `admin/research_feedback_export.sql` are parameterized query inputs for this exporter; they are not standalone SQL-editor scripts. The exporter fails on any added/reordered column, assignment mismatch, non-integer timing, malformed value, count other than the explicitly expected 3/4/5 submissions, or anything other than exactly 12 assigned response rows per submission. It never persists an unvalidated intermediate CSV.

If the two exact-header CSV files were obtained through an independently controlled query, validate them with the separate hosted collector:

```powershell
python -X utf8 tools/collect_hosted_pilot.py `
  --responses PATH_TO_RESPONSES.csv `
  --feedback PATH_TO_FEEDBACK.csv `
  --expected-submissions 5 `
  --output exports/pilot_YYYYMMDD
```

Do not pass hosted results through the offline `b594...` collector or relabel them with its instrument identity. The hosted collector dynamically verifies the canonical hosted artifact and its offline lineage while retaining the hosted hash/version. Because consent and direct identity correctly remain outside research exports, the collector cannot recompute the original submission payload digest; it instead checks the 64-character digest shape and exact response/feedback consistency for each submission.

## PI-only identity recovery

Names and phone numbers are not readable in the database table. When identity verification is needed, run `admin/identity_ciphertext_export.sql` in the Supabase SQL editor, download its encrypted result directly into the external protected directory, and decrypt locally:

```powershell
$pilotPrivate = Join-Path $env:LOCALAPPDATA "bok-stance-pilot"
$env:BOK_PILOT_PRIVATE_DIR = $pilotPrivate
python supabase/admin/decrypt_identity_export.py `
  --private-root $pilotPrivate `
  --input (Join-Path $pilotPrivate "identity_ciphertext.csv") `
  --output (Join-Path $pilotPrivate "identity_roster.csv")
```

The current process must contain the original `PII_ENCRYPTION_KEY_B64` and matching `PII_KEY_ID`. The tool requires Python's `cryptography` package, accepts files only below the selected external private root, rejects repository paths and overwrite, neutralizes spreadsheet-formula prefixes, and never prints identity rows. The decrypted CSV must follow the PI-fixed access and deletion policy. The committed `.private/` ignore rule is only an accidental-commit backstop, not an authorized storage location.

## PI-only individual withdrawal

Never use the retention-cutoff tool for one person's request. `admin/delete_withdrawn_participant.py` supports either a canonical invitation UUID or a protected identity JSON file. Direct name/phone values are deliberately not accepted as command-line arguments.

For identity lookup, create a UTF-8 JSON file containing exactly `name` and `phone` below the external private root and load `IDENTITY_HMAC_SECRET_B64` only into the current process. The tool reproduces the server's Unicode/phone normalization and keyed identity HMAC without decrypting or printing the database identity. Zero matches fail; multiple matches fail closed and require the invitation UUID.

Always begin with a read-only preview. The request timestamp needs an explicit timezone, and the internal procedure version must match `withdrawal-vYYYY-MM-DD-rN` and not postdate the request:

```powershell
$requestReceivedAt = "2026-09-03T10:00:00+09:00"
$withdrawalVersion = "withdrawal-v2026-09-03-r1"
python -X utf8 supabase/admin/delete_withdrawn_participant.py `
  --instrument-sha256 $instrumentHash `
  --procedure-version $withdrawalVersion `
  --request-received-at $requestReceivedAt `
  --invite-id $inviteId `
  --dry-run
```

For identity-file lookup, replace `--invite-id $inviteId` with:

```powershell
--private-root $pilotPrivate `
--identity-file (Join-Path $pilotPrivate "withdrawal-request.private.json")
```

Review the counts and copy the complete opaque confirmation phrase printed by the preview. Re-run the same selector and parameters with `--confirm "EXACT PHRASE"` instead of `--dry-run`. The phrase binds the exact resolved invitation, instrument, procedure version, request time, and row counts but prints no participant/database identifier.

Confirmed deletion uses a serializable transaction, locks the invitation first, revalidates every link, requires exactly one identity, one submission, and 12 ordered responses for a submitted invitation, unlinks the circular FK, deletes responses/submission/encrypted identity/invitation in dependency order, and verifies all target rows are absent. An unused invitation can be removed without pretending that identity or response rows existed. Any changed target, ambiguous identity, unexpected row count, or failed absence check rolls back.

Only after successful absence verification does the same transaction insert one row into migration 003's `private.pilot_withdrawal_events`. That row retains the instrument, selector type, procedure version, timestamps, outcome, deletion counts, and verification result, but no selector or participant-linked value. The operation is irreversible; retain or dispose of the local request file according to the PI-fixed policy.

## PI-only retention deletion

First archive any authorized PII-free research export and use the PI-fixed retention cutoff. The cutoff is strict: only submissions with `submitted_at` before it are in scope, and `Z` or a numeric timezone offset is mandatory. Preview the exact scope in a database-enforced read-only transaction:

```powershell
$instrumentHash = (python -X utf8 -c "import json; print(json.load(open('docs/instrument.json', encoding='utf-8'))['instrument_sha256'])").Trim()
python -X utf8 supabase/admin/delete_retained_pilot_data.py `
  --instrument-sha256 $instrumentHash `
  --cutoff 2026-11-01T00:00:00+09:00 `
  --dry-run
```

The preview prints counts only and an exact confirmation phrase. Review the scope, then re-run the same hash and cutoff using the entire printed phrase as the quoted value of `--confirm`. There is no default or interactive yes/no deletion mode.

Confirmed deletion is one transaction: it locks the scoped submissions and invitations, unlinks the circular invitation reference, then deletes research responses, research submissions, encrypted identity rows, and invitations in FK-safe order. Every affected-row count and the final absence check must match or the transaction rolls back. The tool never prints record identifiers, row contents, or credentials. No deletion was run while building or testing these tools; a confirmed production deletion is not recoverable through this repository.

## Tests

```powershell
python -m unittest discover -s supabase/tests -v
deno test supabase/functions/pilot-api/_shared/core_test.ts
node tools/test_submission_contract.mjs
```

If Node is not on `PATH` but the listed VS Code installation is present, its bundled runtime can execute both JavaScript/TypeScript harnesses:

```powershell
powershell -ExecutionPolicy Bypass -File tools/run_node_tests.ps1
```

The wrapper waits for each child process and fails on its actual nonzero exit
code; invoking `Code.exe` directly is an asynchronous GUI launch and must not
be used as a release-gate result.

The Python suite audits schema exposure, RLS/grants, the Edge-only boundary, encrypted identity separation and recovery, migration 003's identifier-free audit shape, accidental committed secrets, exact-seven secret generation, 256-bit distinct keys, production/disposable invitation counts, HMAC consistency, fielding-gate confirmations and eligibility, seed parity, external private-storage enforcement, exact invite-site URL validation, hosted export/collection, individual-withdrawal and retention-deletion transaction behavior, spreadsheet safety, and overwrite refusal. The WebCrypto suites check the shared frontend/backend canonical payload vector and normative request/cryptographic helpers.

Do not field if migrations, final instrument seed, Edge deployment, the
backend core WebCrypto suite (Deno or the documented Node harness), or an
end-to-end test against a disposable invitation have not passed.
