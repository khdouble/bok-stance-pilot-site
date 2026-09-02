# Supabase pilot backend

This directory implements the server boundary for the hosted usability pilot. GitHub Pages serves only static survey files. The browser sends `POST` requests to `pilot-api`; it receives no Supabase publishable, anonymous, service-role, database, HMAC, or encryption key.

The backend is intentionally fail-closed. Fielding cannot begin until the final hosted instrument is seeded, activated, and matched by the function environment; five one-time invitations are provisioned; the consent version is approved; and the PI separately changes the database `fielding_open` gate from its default `false` value.

## Data boundary

- `private.pilot_invites` stores only HMAC-SHA-256 digests of 256-bit invite tokens. Raw tokens remain in an external OS-local protected directory and are sent individually. Operational secrets and direct PII are forbidden anywhere inside the repository, including ignored `.private/` paths.
- `private.participant_identity` stores only name and normalized phone number encrypted together with AES-256-GCM. Consent version and timestamp are also private.
- `research.pilot_submissions` and `research.pilot_responses` contain pseudonymous IDs, assigned item IDs, answers, timing, and usability feedback. They contain no name, phone number, raw token, user-agent, or ciphertext. The function neither accepts nor stores a browser user-agent field.
- Direct identity is excluded from the response payload digest. Server validation also rejects the submitted name or phone when repeated in a research free-text field.
- Both schemas are excluded from the Data API in `config.toml`. Every table has RLS enabled and forced, no permissive policy, and no `PUBLIC`, `anon`, `authenticated`, or `service_role` grant. The hosted project's Data API should also be switched off in Dashboard because this application does not use it.
- The Edge Function connects over the server-only `SUPABASE_DB_URL` and writes identity, submission, 12 responses, and invite consumption in one database transaction. There is no exposed SQL RPC.

This separation prevents an analysis export from accidentally containing direct identifiers. It does not replace the PI's retention/deletion policy or institutional ethics determination.

## Fixed release inputs

- Hosted instrument version: `v260903-pilot-hosted-1`
- Offline source instrument: `b594a196eb7be720e57d974f4b5c6e4437b697e6ae20f01013e830af35707a51`
- Hosted instrument SHA-256: the canonical `instrument_sha256` in `docs/instrument.json` (read it at execution time; do not copy it into export code)
- Allowed browser origin: exactly `https://khdouble.github.io`
- Items per invitation: exactly 12

`tools/render_instrument_seed.py` generated `migrations/202609030002_seed_pilot_instrument.sql` directly from `docs/instrument.json`: 1 instrument, 12 items, 5 assignment sets, and 60 ordered assignment rows. The renderer recomputes the declared canonical hash and fails on version/count/schema drift, placeholders, unsafe output paths, or overwrite. The database active row, `PILOT_INSTRUMENT_SHA256`, and the hash sent by the frontend must all be identical. Instrument activation does not open fielding: the seed leaves `fielding_open=false`.

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
    "version": "approved consent version",
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

1. Link the CLI to project ref `mebisrsvasrzwkmsodsw` and apply migrations.
2. Verify the committed generated migration still has exact parity with `docs/instrument.json` by running the test suite, then apply it. For a legitimate future instrument, render to a new migration filename; never overwrite an applied migration.
3. Create three different 32-byte random keys for invitation HMAC, identity HMAC, and PII encryption. Keep them and the Postgres URI in an external protected environment file based on `.env.example`. On Windows, use `%LOCALAPPDATA%\bok-stance-pilot`; never use this Google Drive/Git repository, even an ignored path.
4. Upload the server environment with `supabase secrets set --env-file "$env:LOCALAPPDATA\bok-stance-pilot\pilot-function.env"`.
5. Deploy with `supabase functions deploy pilot-api --no-verify-jwt`.
6. Provision a disposable test invitation, temporarily open the fielding gate, run the real browser smoke test, revoke that invitation, and return the gate to `false`.
7. Obtain PI fielding clearance. Then set `fielding_open=true`, `fielding_opened_at=now()`, and `fielding_closed_at=null` for the one active instrument.
8. Provision the five production invitations, apply their digest-only SQL in the SQL editor, and send each raw URL separately.

The real secret file must define:

- `SUPABASE_DB_URL`: server-side Postgres/Supavisor connection URI; never put it in GitHub Pages
- `PILOT_INSTRUMENT_SHA256`: final hosted release digest
- `PILOT_INSTRUMENT_VERSION`: `v260903-pilot-hosted-1`
- `PILOT_CONSENT_VERSION`: approved consent text version
- `INVITE_HMAC_SECRET_B64`: base64 of 32 random bytes
- `IDENTITY_HMAC_SECRET_B64`: base64 of a different 32 random bytes
- `PII_ENCRYPTION_KEY_B64`: base64 of a third 32 random bytes
- `PII_KEY_ID`: nonsecret rotation label such as `pilot-pii-v1`

Supabase supplies some standard environment names automatically, but the deployment must verify `SUPABASE_DB_URL` is the intended pooled server connection before fielding.

## Provision five invitations

Run from the repository root after putting `INVITE_HMAC_SECRET_B64` in the current process environment. Set `BOK_PILOT_PRIVATE_DIR` to an access-controlled, non-synced directory outside the repository; the Windows OS-local default below is recommended. Read the current hosted hash from the canonical instrument instead of copying a possibly stale value:

```powershell
$pilotPrivate = Join-Path $env:LOCALAPPDATA "bok-stance-pilot"
$env:BOK_PILOT_PRIVATE_DIR = $pilotPrivate
$instrumentHash = (python -X utf8 -c "import json; print(json.load(open('docs/instrument.json', encoding='utf-8'))['instrument_sha256'])").Trim()
python supabase/admin/provision_invites.py `
  --private-root $pilotPrivate `
  --output (Join-Path $pilotPrivate "invites-v1") `
  --instrument-sha256 $instrumentHash `
  --expires-at 2026-10-01T00:00:00+09:00
```

The command refuses every repository-internal path (including `.private/`), refuses paths outside the selected external private root, and refuses to overwrite an existing batch. Its `--site-url` accepts only the exact canonical HTTPS project URL, with no userinfo, port, query, fragment, alternate path, or look-alike host. It creates:

- `invite_links.private.json`: five raw links; confidential and never printed by the script
- `invite_seed.private.sql`: invite UUIDs and HMAC digests only; safe for the SQL editor, but still kept private operational material

Tokens use URL fragments (`#invite=...`), so GitHub Pages and HTTP referrer
headers do not receive them. The frontend removes the fragment immediately and
keeps the raw token only in JavaScript memory. A reload therefore requires the
original invitation link.

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

The current process must contain the original `PII_ENCRYPTION_KEY_B64` and matching `PII_KEY_ID`. The tool requires Python's `cryptography` package, accepts files only below the selected external private root, rejects repository paths and overwrite, neutralizes spreadsheet-formula prefixes, and never prints identity rows. The decrypted CSV must follow the approved access and deletion policy. The committed `.private/` ignore rule is only an accidental-commit backstop, not an approved storage location.

## PI-only retention deletion

First archive any approved PII-free research export and determine the institutionally approved cutoff. The cutoff is strict: only submissions with `submitted_at` before it are in scope, and `Z` or a numeric timezone offset is mandatory. Preview the exact scope in a database-enforced read-only transaction:

```powershell
$instrumentHash = (python -X utf8 -c "import json; print(json.load(open('docs/instrument.json', encoding='utf-8'))['instrument_sha256'])").Trim()
python -X utf8 supabase/admin/delete_retained_pilot_data.py `
  --instrument-sha256 $instrumentHash `
  --cutoff 2026-12-31T23:59:59+09:00 `
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

The Python suite audits schema exposure, RLS/grants, the Edge-only boundary, encrypted identity separation and recovery, accidental committed secrets, 256-bit token generation, HMAC consistency, seed parity, external private-storage enforcement, exact invite-site URL validation, hosted export/collection, retention deletion transaction behavior, spreadsheet safety, and overwrite refusal. The WebCrypto suites check the shared frontend/backend canonical payload vector and normative request/cryptographic helpers.

Do not field if migrations, final instrument seed, Edge deployment, the
backend core WebCrypto suite (Deno or the documented Node harness), or an
end-to-end test against a disposable invitation have not passed.
