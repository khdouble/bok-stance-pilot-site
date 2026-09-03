from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools import finalize_remote_e2e as module
from tools import run_remote_e2e as e2e


class FinalizeRemoteE2ETests(unittest.TestCase):
    def pending(self, tested_at: datetime) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "status": e2e.PENDING_STATUS,
            "tested_config_timestamp": e2e._utc_seconds(tested_at),
            "site_url": e2e.SITE_URL,
            "api_url": e2e.API_URL,
            "browser_family": "chrome",
            "browser_version": "Chrome test",
            "hosted_version": "v260903-pilot-hosted-1",
            "instrument_sha256": "a" * 64,
            "deployment_manifest_sha256": "b" * 64,
            "source_attestation_sha256": "c" * 64,
            "asset_attestations": e2e.asset_attestations(
                {name: "d" * 64 for name in e2e.REMOTE_FILES}
            ),
            "config_override_sha256": "e" * 64,
            "published_hold_preserved": True,
            "config_intercept_count": 1,
            "browser_asset_count": len(e2e.BROWSER_ASSETS),
            "assignment_count": 12,
            "initial_submit_http_status": 200,
            "initial_submit_ok": True,
            "same_retry_http_status": 200,
            "same_retry_idempotent": True,
            "changed_retry_http_status": 409,
            "changed_retry_rejected": True,
            "fragment_removed": True,
            "private_inputs_cleared": True,
            "draft_cleared": True,
            "javascript_exception_count": 0,
            "database_record_count": 1,
            "cleanup_required": True,
            "fielding_authorized": False,
            "auto_cleanup_result": "CLOSED_AND_REVOKED",
        }

    def test_pending_receipt_timestamp_is_exact_and_fresh(self) -> None:
        now = datetime.now(timezone.utc)
        pending = self.pending(now - timedelta(minutes=5))
        parsed = module.validate_pending_receipt(pending, now=now)
        self.assertEqual(
            e2e._utc_seconds(parsed),
            pending["tested_config_timestamp"],
        )
        pending["tested_config_timestamp"] = e2e._utc_seconds(
            now - timedelta(days=2)
        )
        with self.assertRaisesRegex(module.FinalizeError, "fresh"):
            module.validate_pending_receipt(pending, now=now)

    def test_finalizer_refuses_before_cleanup(self) -> None:
        now = datetime.now(timezone.utc) - timedelta(minutes=1)
        pending = self.pending(now)
        invite = {
            "invite_id": str(uuid.uuid4()),
            "instrument_sha256": "a" * 64,
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "clean.json"
            with (
                mock.patch.object(
                    module,
                    "_load_inputs",
                    return_value=(pending, invite, output, now),
                ),
                mock.patch.object(
                    module,
                    "attest_current_release",
                    return_value={
                        "asset_hashes": {
                            name: "d" * 64 for name in e2e.REMOTE_FILES
                        }
                    },
                ),
                mock.patch.object(
                    module,
                    "read_cleanup_state",
                    return_value={
                        "fielding_open": False,
                        "invite_count": 1,
                        "unrevoked_invite_count": 0,
                        "identity_count": 1,
                        "submission_count": 1,
                        "response_count": 12,
                    },
                ),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                result = module.main([])
            self.assertEqual(result, 1)
            self.assertFalse(output.exists())

    def test_cleanup_query_is_pinned_and_read_only(self) -> None:
        from supabase.admin import manage_fielding_gate

        connection = mock.MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [
            (False,),
            (0, 0),
            (0, 0, 0),
        ]
        ref = "a" * 20
        invite_id = str(uuid.uuid4())
        with (
            mock.patch.object(
                manage_fielding_gate,
                "resolve_project_ref",
                return_value=ref,
            ) as resolve_ref,
            mock.patch.object(
                manage_fielding_gate,
                "required_database_url",
                return_value="private-dsn",
            ) as require_url,
            mock.patch.object(
                manage_fielding_gate,
                "connect_database",
                return_value=connection,
            ),
        ):
            state = module.read_cleanup_state(
                module.REPOSITORY_ROOT,
                "a" * 64,
                invite_id,
            )
        module.validate_cleanup_state(state)
        resolve_ref.assert_called_once_with(module.REPOSITORY_ROOT, None)
        require_url.assert_called_once_with(module.os.environ, ref)
        statements = [
            " ".join(call.args[0].lower().split())
            for call in cursor.execute.call_args_list
        ]
        self.assertEqual(
            statements[0],
            "set transaction isolation level repeatable read, read only",
        )
        self.assertFalse(any(statement.startswith("update ") for statement in statements))
        self.assertFalse(any(statement.startswith("delete ") for statement in statements))
        connection.close.assert_called_once_with()

    def test_cleanup_validation_rejects_revoked_invite_residue(self) -> None:
        with self.assertRaisesRegex(module.FinalizeError, "cleanup is incomplete"):
            module.validate_cleanup_state(
                {
                    "fielding_open": False,
                    "invite_count": 1,
                    "unrevoked_invite_count": 0,
                    "identity_count": 0,
                    "submission_count": 0,
                    "response_count": 0,
                }
            )

    def test_cleanup_validation_accepts_all_zero(self) -> None:
        module.validate_cleanup_state(
            {
                "fielding_open": False,
                "invite_count": 0,
                "unrevoked_invite_count": 0,
                "identity_count": 0,
                "submission_count": 0,
                "response_count": 0,
            }
        )

    def test_finalizer_writes_clean_nonidentifying_receipt_after_cleanup(self) -> None:
        tested_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        pending = self.pending(tested_at)
        invite_id = str(uuid.uuid4())
        invite = {
            "invite_id": invite_id,
            "instrument_sha256": "a" * 64,
        }
        clean_state = {
            "fielding_open": False,
            "invite_count": 0,
            "unrevoked_invite_count": 0,
            "identity_count": 0,
            "submission_count": 0,
            "response_count": 0,
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "clean.json"
            with (
                mock.patch.object(
                    module,
                    "_load_inputs",
                    return_value=(pending, invite, output, tested_at),
                ),
                mock.patch.object(
                    module,
                    "attest_current_release",
                    return_value={
                        "asset_hashes": {
                            name: "d" * 64 for name in e2e.REMOTE_FILES
                        }
                    },
                ),
                mock.patch.object(
                    module,
                    "read_cleanup_state",
                    return_value=clean_state,
                ),
            ):
                result = module.main([])
            self.assertEqual(result, 0)
            clean = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(clean["status"], module.FINAL_STATUS)
            self.assertEqual(
                clean["tested_config_timestamp"],
                pending["tested_config_timestamp"],
            )
            self.assertFalse(clean["cleanup_required"])
            self.assertFalse(clean["fielding_authorized"])
            rendered = json.dumps(clean, ensure_ascii=False)
            self.assertNotIn(invite_id, rendered)
            self.assertNotIn("invite_id", rendered)


if __name__ == "__main__":
    unittest.main()
