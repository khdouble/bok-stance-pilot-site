# Disposable remote E2E

`run_remote_e2e.py` verifies the real GitHub Pages assets and real pilot API
without publishing a live site configuration. It intercepts only the exact
deployed `site-config.js` request inside an ephemeral headless browser and
changes only `fieldingEnabled` and `remoteE2eVerifiedAt` in memory. It never
opens a database gate, deploys files, or changes the repository. Once it has
validated a disposable invitation, its `finally` path makes a best-effort
`close-e2e` call so ordinary failures do not intentionally leave the gate
open; operators must still perform and verify the explicit cleanup below.

## Safety boundary

- Keep the published `docs/site-config.js` on HOLD (`fieldingEnabled: false`).
- Put the disposable provision output, test identity, and receipt outside the
  repository under `BOK_PILOT_PRIVATE_DIR` (or pass an absolute external
  `--private-root`). Repository paths and paths containing the repository are
  refused.
- Supply secrets only through JSON files. There are no CLI options for a raw
  invite token, participant name, or phone number. The harness produces no
  screenshots, trace, video, downloads, request-body logs, or browser profile.
- Every browser HTTP(S) and WebSocket request is intercepted before sending.
  Only the exact staged asset URLs and exact pilot API URL are allowed. Private
  values are forbidden in URLs and headers and may occur only in the `POST`
  body sent to that exact API.
- The receipt is new-file-only and contains deployment hashes and Boolean/count
  checks, never the invite, identity, request payload, idempotency key, or
  submission ID.

The identity file must be UTF-8 JSON with exactly this schema; replace the two
descriptive placeholders only in the external private copy:

```json
{
  "schema_version": "1.0",
  "purpose": "disposable_remote_e2e_test_only",
  "name": "TEST-ONLY-NAME",
  "phone": "TEST-ONLY-MOBILE"
}
```

## Operator sequence

1. Build, validate, and deploy the staging release while HOLD remains in
   place. The local `docs/` bytes and deployed bytes must be identical, and the
   staging deployment manifest must be current.
2. Use `supabase/admin/provision_invites.py --mode disposable-e2e` to create
   exactly one invite below the external private root, then apply its private
   SQL seed through the approved database channel.
3. Separately use `supabase/admin/manage_fielding_gate.py open-e2e` with its
   exact confirmation phrase. The harness only calls that module's read-only
   status path and requires one eligible invite and zero submissions.
4. Run the harness using paths, never raw values:

   ```powershell
   python -B -X utf8 tools/run_remote_e2e.py `
     --private-root C:\ABSOLUTE\PROTECTED\bok-stance-pilot `
     --invite-file C:\ABSOLUTE\PROTECTED\bok-stance-pilot\e2e\invite_links.private.json `
     --identity-file C:\ABSOLUTE\PROTECTED\bok-stance-pilot\remote-e2e-identity.private.json `
     --receipt C:\ABSOLUTE\PROTECTED\bok-stance-pilot\remote-e2e-receipt.json
   ```

   With no file arguments, defaults are
   `<private-root>/remote-e2e-invite/invite_links.private.json`,
   `<private-root>/remote-e2e-identity.private.json`, and
   `<private-root>/remote-e2e-receipt.json`.

The harness fails closed unless it observes the exact staged assets, one
config interception, all 12 assigned items, a successful initial submission,
an idempotent identical retry, a rejected changed retry, cleared browser PII
and draft state, and the one-record post-E2E database invariant. Browser
success writes an `E2E_PASSED_CLEANUP_PENDING` receipt and intentionally exits
with status 3. That receipt has `cleanup_required: true` and
`fielding_authorized: false`, so it cannot authorize promotion. Its
`tested_config_timestamp` is the one exact UTC value injected into the
in-memory configuration.

## Mandatory cleanup

The harness attempts `close-e2e` in `finally` after it has safely loaded the
disposable invitation. Whether the run passes or fails, immediately complete
and independently verify cleanup:

1. Run `manage_fielding_gate.py close-e2e` with the disposable invite ID and
   its exact confirmation phrase. This closes the gate and revokes the invite.
2. Use `delete_withdrawn_participant.py --invite-id ... --dry-run`, review its
   exact scope, then repeat with its generated confirmation phrase to delete
   the disposable identity and research rows.
3. Run the gate `status` check.
4. Run the cleanup finalizer only after the destructive withdrawal has
   completed:

   ```powershell
   python -B -X utf8 tools/finalize_remote_e2e.py `
     --private-root C:\ABSOLUTE\PROTECTED\bok-stance-pilot `
     --pending-receipt C:\ABSOLUTE\PROTECTED\bok-stance-pilot\remote-e2e-receipt.json `
     --invite-file C:\ABSOLUTE\PROTECTED\bok-stance-pilot\e2e\invite_links.private.json `
     --output C:\ABSOLUTE\PROTECTED\bok-stance-pilot\remote-e2e-clean-receipt.json
   ```

   The finalizer does not delete anything. It revalidates the pending receipt,
   hashes, deployed site/API/instrument, and freshness, then uses the pinned
   linked project and a read-only database transaction to prove that the gate
   is closed, the disposable invitation is revoked or absent, and its
   identity, submission, and response row counts are all zero.

Only a zero exit from the finalizer and its new-file-only
`E2E_VERIFIED_CLEAN` receipt are promotable. Copy that receipt's unchanged
`tested_config_timestamp` to `remoteE2eVerifiedAt`. The clean receipt still
sets `fielding_authorized: false`; it is evidence of E2E completion and
verified cleanup, not permission to open production fielding.
