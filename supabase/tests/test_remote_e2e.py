from __future__ import annotations

import base64
import contextlib
import io
import json
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools import run_remote_e2e as module


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class RemoteE2ETests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime.now(timezone.utc) - timedelta(seconds=2)
        self.token = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")
        self.digest = json.loads(
            (REPOSITORY_ROOT / "docs" / "instrument.json").read_text(
                encoding="utf-8"
            )
        )["instrument_sha256"]

    def write_private_fixture(self, root: Path) -> tuple[Path, Path, Path]:
        invite_directory = root / "batch"
        invite_directory.mkdir(parents=True)
        invite_path = invite_directory / "invite_links.private.json"
        identity_path = root / "identity.private.json"
        receipt_path = root / "receipt.json"
        created = self.now - timedelta(minutes=1)
        invite_path.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "provisioning_mode": "disposable-e2e",
                    "created_at": module._utc_seconds(created),
                    "instrument_sha256": self.digest,
                    "warning": "CONFIDENTIAL",
                    "invites": [
                        {
                            "invite_id": str(uuid.uuid4()),
                            "assignment_code": "PILOT_R01",
                            "invite_token": self.token,
                            "invite_url": module.SITE_URL + "#invite=" + self.token,
                            "expires_at": module._utc_seconds(
                                created + timedelta(hours=1)
                            ),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        identity_path.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "purpose": module.IDENTITY_PURPOSE,
                    "name": "테스트 참가자",
                    "phone": "010-1234-5678",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return invite_path, identity_path, receipt_path

    def test_private_inputs_accept_only_external_disposable_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            invite_path, identity_path, receipt_path = self.write_private_fixture(root)
            invite, identity, resolved_receipt, resolved_root = module.load_private_inputs(
                REPOSITORY_ROOT,
                root,
                invite_path,
                identity_path,
                receipt_path,
                now=self.now,
            )
            self.assertEqual(invite["invite_token"], self.token)
            self.assertEqual(invite["assignment_code"], "PILOT_R01")
            self.assertEqual(identity["phone"], "01012345678")
            self.assertEqual(resolved_receipt, receipt_path.resolve())
            self.assertEqual(resolved_root, root)

    def test_private_invite_digest_uses_the_single_parsed_read(self) -> None:
        admin_path = REPOSITORY_ROOT / "supabase" / "admin"
        if str(admin_path) not in module.sys.path:
            module.sys.path.insert(0, str(admin_path))
        import private_storage

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            invite_path, _identity_path, _receipt_path = self.write_private_fixture(root)
            encoded = invite_path.read_bytes()
            batch = json.loads(encoded.decode("utf-8"))
            invite_path.unlink()
            with (
                mock.patch.object(
                    private_storage, "ensure_private_root", return_value=root
                ),
                mock.patch.object(
                    private_storage, "private_child", return_value=invite_path
                ),
                mock.patch.object(
                    module,
                    "_load_json_bytes",
                    return_value=(batch, encoded),
                ) as load_once,
            ):
                invite, resolved_path, resolved_root = module.load_private_invite(
                    REPOSITORY_ROOT,
                    root,
                    invite_path,
                    now=self.now,
                )
            load_once.assert_called_once_with(invite_path, "invite file")
            self.assertEqual(invite["provision_file_sha256"], module._sha256(encoded))
            self.assertEqual(resolved_path, invite_path)
            self.assertEqual(resolved_root, root)

    def test_private_root_inside_repository_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "outside"):
            module.load_private_inputs(
                REPOSITORY_ROOT,
                REPOSITORY_ROOT / "forbidden-private",
                None,
                None,
                None,
                now=self.now,
            )

    def test_production_or_expired_invite_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            invite_path, identity_path, receipt_path = self.write_private_fixture(root)
            payload = json.loads(invite_path.read_text(encoding="utf-8"))
            payload["provisioning_mode"] = "production"
            invite_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(module.E2EError, "disposable-e2e"):
                module.load_private_inputs(
                    REPOSITORY_ROOT,
                    root,
                    invite_path,
                    identity_path,
                    receipt_path,
                    now=self.now,
                )

    def test_config_override_changes_only_two_fields(self) -> None:
        original = (REPOSITORY_ROOT / "docs" / "site-config.js").read_bytes()
        override, stamp = module.make_config_override(original, self.now)
        original_text = original.decode("utf-8")
        override_text = override.decode("utf-8")
        self.assertIn("fieldingEnabled: false", original_text)
        self.assertIn("fieldingEnabled: true", override_text)
        self.assertIn('remoteE2eVerifiedAt: "PENDING_PI"', original_text)
        self.assertIn(f'remoteE2eVerifiedAt: "{stamp}"', override_text)
        restored = override_text.replace(
            "fieldingEnabled: true", "fieldingEnabled: false"
        ).replace(stamp, "PENDING_PI")
        self.assertEqual(restored.encode("utf-8"), original)

    def staged_assets(self) -> dict[str, bytes]:
        assets = module.local_assets(REPOSITORY_ROOT)
        manifest = json.loads(assets["deployment-manifest.json"].decode("utf-8"))
        manifest["deployment_state"] = "staging"
        manifest["operational_file_hashes"] = {
            "privacy_notice": module._sha256(assets["privacy.html"]),
            "site_config": module._sha256(assets["site-config.js"]),
        }
        basis = {
            key: value
            for key, value in manifest.items()
            if key != "deployment_manifest_sha256"
        }
        manifest["deployment_manifest_sha256"] = module._sha256(
            module._canonical_json(basis)
        )
        assets["deployment-manifest.json"] = (
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        return assets

    def test_remote_release_attestation_and_byte_mismatch(self) -> None:
        assets = self.staged_assets()
        result = module.validate_remote_release(assets, dict(assets), self.now)
        self.assertEqual(
            result["instrument"]["instrument_sha256"], self.digest
        )
        changed = dict(assets)
        changed["app.js"] += b"\n"
        with self.assertRaisesRegex(module.E2EError, "does not match"):
            module.validate_remote_release(changed, assets, self.now)

    def test_gate_invariants_are_fail_closed(self) -> None:
        before = {
            "is_active": True,
            "fielding_open": True,
            "unrevoked_invite_count": 1,
            "eligible_unused_invite_count": 1,
            "used_invite_count": 0,
            "submission_count": 0,
        }
        module.validate_gate_status(before, after=False)
        before["fielding_open"] = False
        with self.assertRaisesRegex(module.E2EError, "pre-E2E"):
            module.validate_gate_status(before, after=False)

    def test_gate_status_resolves_and_pins_linked_project(self) -> None:
        from supabase.admin import manage_fielding_gate

        ref = "a" * 20
        connection = mock.Mock()
        expected = {"fielding_open": True}
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
            ) as connect,
            mock.patch.object(
                manage_fielding_gate,
                "read_gate_status",
                return_value=expected,
            ) as read_status,
        ):
            actual = module.read_gate_status(
                REPOSITORY_ROOT, self.digest
            )

        self.assertEqual(actual, expected)
        resolve_ref.assert_called_once_with(REPOSITORY_ROOT, None)
        require_url.assert_called_once_with(module.os.environ, ref)
        connect.assert_called_once_with("private-dsn")
        read_status.assert_called_once_with(connection, self.digest)
        connection.close.assert_called_once_with()

    def test_receipt_refuses_private_keys_values_and_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            with self.assertRaisesRegex(module.E2EError, "private field"):
                module.write_receipt(path, {"invite_token": "not-written"})
            with self.assertRaisesRegex(module.E2EError, "private input"):
                module.write_receipt(
                    path, {"status": self.token}, private_values=(self.token,)
                )
            module.write_receipt(path, {"schema_version": "1.0", "status": "PASS"})
            with self.assertRaises(FileExistsError):
                module.write_receipt(path, {"schema_version": "1.0"})

    def test_cli_exposes_paths_not_raw_private_values(self) -> None:
        destinations = {action.dest for action in module.parser()._actions}
        self.assertTrue({"invite_file", "identity_file", "receipt"} <= destinations)
        self.assertFalse({"invite_token", "name", "phone"} & destinations)

    def test_exact_config_interception_only(self) -> None:
        class Socket:
            def __init__(self) -> None:
                self.messages: list[dict[str, object]] = []

            def send(self, value: str) -> None:
                self.messages.append(json.loads(value))

        socket = Socket()
        client = module.CdpClient(socket, b"override")
        client._handle_event(
            {
                "method": "Fetch.requestPaused",
                "params": {
                    "requestId": "one",
                    "resourceType": "Script",
                    "request": {"url": module.CONFIG_URL},
                },
            }
        )
        self.assertEqual(client.intercept_count, 1)
        self.assertEqual(socket.messages[0]["method"], "Fetch.fulfillRequest")
        client._handle_event(
            {
                "method": "Fetch.requestPaused",
                "params": {
                    "requestId": "two",
                    "resourceType": "Script",
                    "request": {"url": module.CONFIG_URL + "?unexpected=1"},
                },
            }
        )
        self.assertIn("unexpected", client.event_failure or "")

    def test_unexpected_network_is_blocked_before_send(self) -> None:
        class Socket:
            def __init__(self) -> None:
                self.messages: list[dict[str, object]] = []

            def send(self, value: str) -> None:
                self.messages.append(json.loads(value))

        private_value = "private-e2e-value"
        socket = Socket()
        client = module.CdpClient(
            socket,
            b"override",
            private_values=(private_value,),
        )
        client._handle_event(
            {
                "method": "Fetch.requestPaused",
                "params": {
                    "requestId": "blocked",
                    "resourceType": "XHR",
                    "request": {
                        "url": "https://unexpected.example/collect",
                        "method": "POST",
                        "headers": {"X-Test": private_value},
                        "postData": private_value,
                    },
                },
            }
        )
        self.assertEqual(
            socket.messages[0]["method"],
            "Fetch.failRequest",
        )
        self.assertIsNotNone(client.event_failure)
        self.assertNotIn(private_value, client.event_failure or "")

        allowed_socket = Socket()
        allowed = module.CdpClient(
            allowed_socket,
            b"override",
            private_values=(private_value,),
        )
        allowed._handle_event(
            {
                "method": "Fetch.requestPaused",
                "params": {
                    "requestId": "allowed",
                    "resourceType": "Fetch",
                    "request": {
                        "url": module.API_URL,
                        "method": "POST",
                        "headers": {"Content-Type": "application/json"},
                        "postData": json.dumps({"value": private_value}),
                    },
                },
            }
        )
        self.assertIsNone(allowed.event_failure)
        self.assertEqual(
            allowed_socket.messages[0]["method"],
            "Fetch.continueRequest",
        )

    def test_failure_after_invite_load_still_closes_gate(self) -> None:
        invite = {
            "invite_id": str(uuid.uuid4()),
            "assignment_code": "PILOT_R01",
            "invite_token": self.token,
            "instrument_sha256": self.digest,
            "provision_file_sha256": "f" * 64,
        }
        stderr = io.StringIO()
        with (
            mock.patch.object(
                module,
                "load_private_invite",
                return_value=(invite, Path("private"), Path("root")),
            ),
            mock.patch.object(
                module,
                "load_private_identity_and_receipt",
                side_effect=module.E2EError("identity input is invalid"),
            ),
            mock.patch.object(
                module,
                "best_effort_close_e2e",
                return_value="CLOSED_AND_REVOKED",
            ) as close_gate,
            contextlib.redirect_stderr(stderr),
        ):
            result = module.main([])
        self.assertEqual(result, 1)
        close_gate.assert_called_once_with(
            REPOSITORY_ROOT,
            self.digest,
            invite["invite_id"],
        )
        self.assertIn("identity input is invalid", stderr.getvalue())

    def test_browser_success_is_pending_nonzero_with_exact_tested_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            receipt_path = Path(directory) / "pending.json"
            invite = {
                "invite_id": str(uuid.uuid4()),
                "assignment_code": "PILOT_R01",
                "invite_token": self.token,
                "instrument_sha256": self.digest,
                "provision_file_sha256": "f" * 64,
            }
            identity = {
                "name": "TEST PERSON",
                "phone": "01012345678",
            }
            before = {
                "is_active": True,
                "fielding_open": True,
                "unrevoked_invite_count": 1,
                "eligible_unused_invite_count": 1,
                "used_invite_count": 0,
                "submission_count": 0,
            }
            after = dict(
                before,
                eligible_unused_invite_count=0,
                used_invite_count=1,
                submission_count=1,
            )
            observed: dict[str, str] = {}

            def release(_remote: object, _local: object, tested_at: datetime):
                stamp = module._utc_seconds(tested_at)
                observed["stamp"] = stamp
                return {
                    "instrument": {
                        "instrument_sha256": self.digest,
                        "hosted_version": "v260903-pilot-hosted-1",
                    },
                    "manifest": {
                        "deployment_manifest_sha256": "e" * 64,
                    },
                    "config_override": b"override",
                    "override_timestamp": stamp,
                    "asset_hashes": {
                        name: "a" * 64 for name in module.REMOTE_FILES
                    },
                }

            browser_result = {
                "browser_family": "chrome",
                "browser_version": "Chrome test",
                "asset_count": len(module.BROWSER_ASSETS),
                "config_intercept_count": 1,
                "assignment_count": 12,
                "initial_status": 200,
                "same_retry_status": 200,
                "changed_retry_status": 409,
                "fragment_removed": True,
                "private_inputs_cleared": True,
                "draft_cleared": True,
                "javascript_exceptions": 0,
            }
            with (
                mock.patch.object(
                    module,
                    "load_private_invite",
                    return_value=(invite, Path("private"), Path(directory)),
                ),
                mock.patch.object(
                    module,
                    "load_private_identity_and_receipt",
                    return_value=(identity, receipt_path),
                ),
                mock.patch.object(module, "fetch_remote_assets", return_value={}),
                mock.patch.object(module, "local_assets", return_value={}),
                mock.patch.object(
                    module,
                    "validate_remote_release",
                    side_effect=release,
                ),
                mock.patch.object(
                    module,
                    "read_gate_status",
                    side_effect=(before, after),
                ),
                mock.patch.object(
                    module,
                    "find_browser",
                    return_value=(Path("browser"), "chrome"),
                ),
                mock.patch.object(
                    module,
                    "run_browser_flow",
                    return_value=browser_result,
                ),
                mock.patch.object(
                    module,
                    "best_effort_close_e2e",
                    return_value="CLOSED_AND_REVOKED",
                ),
            ):
                result = module.main([])
            self.assertEqual(result, module.PENDING_EXIT_CODE)
            pending = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(pending["status"], module.PENDING_STATUS)
            self.assertEqual(
                pending["tested_config_timestamp"],
                observed["stamp"],
            )
            self.assertFalse(pending["fielding_authorized"])
            self.assertTrue(pending["cleanup_required"])
            self.assertEqual(
                pending["auto_cleanup_result"],
                "CLOSED_AND_REVOKED",
            )
            self.assertEqual(
                len(pending["asset_attestations"]),
                len(module.REMOTE_FILES),
            )


if __name__ == "__main__":
    unittest.main()
