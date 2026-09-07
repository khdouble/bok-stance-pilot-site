from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


ADMIN = Path(__file__).resolve().parents[1] / "admin"
if str(ADMIN) not in sys.path:
    sys.path.insert(0, str(ADMIN))
import private_storage as PRIVATE_STORAGE

SCRIPT = ADMIN / "provision_invites.py"
SPEC = importlib.util.spec_from_file_location("provision_invites", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ProvisionInvitesTest(unittest.TestCase):
    def test_parser_defaults_to_file_only_seed_mode(self) -> None:
        args = MODULE.parser().parse_args(
            [
                "--output",
                "external",
                "--instrument-sha256",
                "a" * 64,
                "--expires-at",
                "2026-09-04T00:00:00Z",
            ]
        )
        self.assertEqual(args.seed_via, MODULE.SEED_VIA_NONE)

    def test_creates_five_private_links_and_digest_only_sql(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repository = base / "repository"
            repository.mkdir()
            private_root = base / "local-app-data" / "bok-stance-pilot"
            output = private_root / "batch-01"
            key = bytes(range(32))
            private_path, seed_path = MODULE.provision(
                output=output,
                repository_root=repository,
                instrument_sha256="a" * 64,
                assignment_codes=MODULE.DEFAULT_ASSIGNMENTS,
                expires_at=datetime.now(timezone.utc) + timedelta(days=7),
                site_url=MODULE.DEFAULT_SITE_URL,
                hmac_key=key,
                private_root=private_root,
            )
            payload = json.loads(private_path.read_text(encoding="utf-8"))
            sql = seed_path.read_text(encoding="utf-8")
            self.assertEqual(payload["provisioning_mode"], "production")
            self.assertEqual(len(payload["invites"]), 5)
            tokens = [row["invite_token"] for row in payload["invites"]]
            invite_ids = [row["invite_id"] for row in payload["invites"]]
            self.assertEqual(len(set(tokens)), 5)
            self.assertEqual(len(set(invite_ids)), 5)
            for row in payload["invites"]:
                token = row["invite_token"]
                invite_id = row["invite_id"]
                self.assertEqual(str(uuid.UUID(invite_id)), invite_id)
                self.assertEqual(len(token), 43)
                self.assertIn("/#invite=", row["invite_url"])
                self.assertNotIn(token, sql)
                self.assertIn(invite_id, sql)
                raw = base64.urlsafe_b64decode(token + "=")
                digest = hmac.new(key, MODULE.HMAC_DOMAIN + raw, hashlib.sha256).hexdigest()
                self.assertIn(digest, sql)
            self.assertNotIn(base64.b64encode(key).decode("ascii"), sql)
            self.assertIn("PRODUCTION INVITATIONS", sql)
            self.assertEqual(sql.count("'participant'"), 5)
            self.assertNotIn("'disposable_e2e'", sql)

    def test_disposable_e2e_creates_one_marked_short_lived_invite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repository = base / "repository"
            repository.mkdir()
            private_root = base / "local-app-data" / "bok-stance-pilot"
            private_path, seed_path = MODULE.provision(
                output=private_root / "e2e-01",
                repository_root=repository,
                instrument_sha256="c" * 64,
                assignment_codes=("PILOT_R03",),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
                site_url=MODULE.DEFAULT_SITE_URL,
                hmac_key=bytes(range(32)),
                private_root=private_root,
                mode=MODULE.DISPOSABLE_E2E_MODE,
            )
            payload = json.loads(private_path.read_text(encoding="utf-8"))
            sql = seed_path.read_text(encoding="utf-8")
            self.assertEqual(payload["provisioning_mode"], "disposable-e2e")
            self.assertEqual(len(payload["invites"]), 1)
            self.assertEqual(
                payload["invites"][0]["assignment_code"],
                "PILOT_R03",
            )
            self.assertIn("DISPOSABLE E2E INVITE", sql)
            self.assertEqual(sql.count("'disposable_e2e'"), 1)
            self.assertNotIn("'participant'", sql)

    def test_linked_seed_revalidates_artifacts_and_sends_no_raw_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repository = base / "repository"
            (repository / "docs").mkdir(parents=True)
            digest = "c" * 64
            version = "v260903-pilot-hosted-1"
            (repository / "docs" / "instrument.json").write_text(
                json.dumps(
                    {
                        "instrument_sha256": digest,
                        "hosted_version": version,
                    }
                ),
                encoding="utf-8",
            )
            private_root = base / "local-app-data" / "bok-stance-pilot"
            expiry = datetime.now(timezone.utc) + timedelta(hours=2)
            key = bytes(range(32))
            private_path, seed_path = MODULE.provision(
                output=private_root / "e2e-linked",
                repository_root=repository,
                instrument_sha256=digest,
                assignment_codes=("PILOT_R01",),
                expires_at=expiry,
                site_url=MODULE.DEFAULT_SITE_URL,
                hmac_key=key,
                private_root=private_root,
                mode=MODULE.DISPOSABLE_E2E_MODE,
            )
            payload = json.loads(private_path.read_text(encoding="utf-8"))
            raw_token = payload["invites"][0]["invite_token"]
            mapping = MODULE.validate_linked_seed_artifacts(
                private_path,
                seed_path,
                repository,
                digest,
                ("PILOT_R01",),
                expiry,
                MODULE.DEFAULT_SITE_URL,
                key,
            )
            self.assertEqual(
                set(mapping),
                {
                    "invite_id",
                    "invite_hmac",
                    "instrument_sha256",
                    "instrument_version",
                    "assignment_set_id",
                    "assignment_code",
                    "expires_at",
                },
            )
            self.assertNotIn(raw_token, json.dumps(mapping))
            self.assertEqual(mapping["instrument_version"], version)

            class FakeBackend:
                def __init__(self) -> None:
                    self.calls: list[dict[str, str]] = []

                def seed_disposable(
                    self, value: dict[str, str]
                ) -> None:
                    self.calls.append(value)

            backend = FakeBackend()
            phrase = MODULE.linked_seed_confirmation(
                digest, mapping["invite_id"]
            )
            with self.assertRaisesRegex(ValueError, "confirmation"):
                MODULE.apply_linked_seed(mapping, "wrong", backend)
            self.assertEqual(backend.calls, [])
            MODULE.apply_linked_seed(mapping, phrase, backend)
            self.assertEqual(backend.calls, [mapping])
            self.assertNotIn(raw_token, json.dumps(backend.calls))

    def test_linked_seed_rejects_tampering_and_non_disposable_assignment(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repository = base / "repository"
            (repository / "docs").mkdir(parents=True)
            digest = "e" * 64
            (repository / "docs" / "instrument.json").write_text(
                json.dumps(
                    {
                        "instrument_sha256": digest,
                        "hosted_version": "v260903-pilot-hosted-1",
                    }
                ),
                encoding="utf-8",
            )
            private_root = base / "private"
            expiry = datetime.now(timezone.utc) + timedelta(hours=2)
            key = bytes(range(32))
            private_path, seed_path = MODULE.provision(
                output=private_root / "batch",
                repository_root=repository,
                instrument_sha256=digest,
                assignment_codes=("PILOT_R01",),
                expires_at=expiry,
                site_url=MODULE.DEFAULT_SITE_URL,
                hmac_key=key,
                private_root=private_root,
                mode=MODULE.DISPOSABLE_E2E_MODE,
            )
            seed_path.write_text(
                seed_path.read_text(encoding="utf-8") + "-- tampered\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "artifacts"):
                MODULE.validate_linked_seed_artifacts(
                    private_path,
                    seed_path,
                    repository,
                    digest,
                    ("PILOT_R01",),
                    expiry,
                    MODULE.DEFAULT_SITE_URL,
                    key,
                )
            with self.assertRaisesRegex(ValueError, "PILOT_R01"):
                MODULE.validate_linked_seed_artifacts(
                    private_path,
                    seed_path,
                    repository,
                    digest,
                    ("PILOT_R02",),
                    expiry,
                    MODULE.DEFAULT_SITE_URL,
                    key,
                )

    def test_ambiguous_linked_seed_exits_one_with_only_safe_counts(
        self,
    ) -> None:
        repository = SCRIPT.resolve().parents[2]
        instrument = json.loads(
            (repository / "docs" / "instrument.json").read_text(
                encoding="utf-8"
            )
        )
        digest = instrument["instrument_sha256"]
        counts = {
            "invite_count": 1,
            "unrevoked_invite_count": 1,
            "eligible_unused_invite_count": 1,
            "used_invite_count": 0,
            "submission_count": 0,
            "identity_count": 0,
            "response_count": 0,
        }

        class FakeBackend:
            def __init__(self) -> None:
                self.calls: list[dict[str, str]] = []

            def seed_disposable(
                self, mapping: dict[str, str]
            ) -> None:
                self.calls.append(mapping)
                raise MODULE.LinkedCliAmbiguousOutcome(
                    {
                        "is_active": True,
                        "fielding_open": False,
                        **counts,
                    }
                )

        backend = FakeBackend()
        captured_phrase: dict[str, str] = {}
        original_confirmation = MODULE.linked_seed_confirmation

        def capture_confirmation(
            instrument_sha256: str, invite_id: str
        ) -> str:
            phrase = original_confirmation(
                instrument_sha256, invite_id
            )
            captured_phrase["value"] = phrase
            return phrase

        def supply_confirmation(_prompt: str) -> str:
            return captured_phrase["value"]

        with tempfile.TemporaryDirectory() as directory:
            private_root = Path(directory) / "private"
            output = private_root / "ambiguous"
            stdout = io.StringIO()
            stderr = io.StringIO()
            key = bytes(range(32))
            expiry = (
                datetime.now(timezone.utc) + timedelta(hours=2)
            ).isoformat()
            with mock.patch.dict(
                MODULE.os.environ,
                {
                    "INVITE_HMAC_SECRET_B64":
                        base64.b64encode(key).decode("ascii")
                },
                clear=True,
            ), mock.patch.object(
                MODULE, "LinkedCliBackend", return_value=backend
            ), mock.patch.object(
                MODULE,
                "linked_seed_confirmation",
                side_effect=capture_confirmation,
            ), mock.patch(
                "builtins.input", side_effect=supply_confirmation
            ), contextlib.redirect_stdout(
                stdout
            ), contextlib.redirect_stderr(
                stderr
            ):
                result = MODULE.main(
                    [
                        "--private-root",
                        str(private_root),
                        "--output",
                        str(output),
                        "--instrument-sha256",
                        digest,
                        "--expires-at",
                        expiry,
                        "--mode",
                        MODULE.DISPOSABLE_E2E_MODE,
                        "--seed-via",
                        MODULE.SEED_VIA_LINKED_CLI,
                    ]
                )
            self.assertEqual(result, 1)
            self.assertEqual(len(backend.calls), 1)
            rendered_error = stderr.getvalue()
            self.assertIn(
                "linked Supabase mutation outcome is ambiguous; "
                "DO NOT RETRY; RUN LINKED STATUS",
                rendered_error,
            )
            for key_name, value in counts.items():
                self.assertIn(
                    f"{key_name}={value}", rendered_error
                )
            mapping = backend.calls[0]
            private_payload = json.loads(
                (
                    output / "invite_links.private.json"
                ).read_text(encoding="utf-8")
            )
            private_token = private_payload["invites"][0]["invite_token"]
            for private_value in (
                digest,
                mapping["invite_id"],
                mapping["invite_hmac"],
                private_token,
            ):
                self.assertNotIn(private_value, rendered_error)
            self.assertNotIn("is_active", rendered_error)
            self.assertNotIn("fielding_open", rendered_error)

    def test_production_and_disposable_counts_remain_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repository = base / "repository"
            repository.mkdir()
            private_root = base / "local-app-data" / "bok-stance-pilot"
            common = dict(
                repository_root=repository,
                instrument_sha256="d" * 64,
                site_url=MODULE.DEFAULT_SITE_URL,
                hmac_key=bytes(range(32)),
                private_root=private_root,
            )
            with self.assertRaisesRegex(ValueError, "exactly five"):
                MODULE.provision(
                    output=private_root / "bad-production",
                    assignment_codes=("PILOT_R01",),
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
                    **common,
                )
            with self.assertRaisesRegex(ValueError, "exactly one"):
                MODULE.provision(
                    output=private_root / "bad-e2e-count",
                    assignment_codes=MODULE.DEFAULT_ASSIGNMENTS,
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
                    mode=MODULE.DISPOSABLE_E2E_MODE,
                    **common,
                )
            with self.assertRaisesRegex(ValueError, "within 24 hours"):
                MODULE.provision(
                    output=private_root / "bad-e2e-expiry",
                    assignment_codes=("PILOT_R01",),
                    expires_at=datetime.now(timezone.utc) + timedelta(days=2),
                    mode=MODULE.DISPOSABLE_E2E_MODE,
                    **common,
                )

    def test_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repository = base / "repository"
            repository.mkdir()
            private_root = base / "local-app-data" / "bok-stance-pilot"
            output = private_root / "batch-01"
            arguments = dict(
                output=output,
                repository_root=repository,
                instrument_sha256="b" * 64,
                assignment_codes=MODULE.DEFAULT_ASSIGNMENTS,
                expires_at=datetime.now(timezone.utc) + timedelta(days=7),
                site_url=MODULE.DEFAULT_SITE_URL,
                hmac_key=bytes(range(32)),
                private_root=private_root,
            )
            MODULE.provision(**arguments)
            with self.assertRaises(FileExistsError):
                MODULE.provision(**arguments)

    def test_rejects_repository_private_and_paths_outside_external_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repository = base / "repository"
            repository.mkdir()
            external_root = base / "local-app-data" / "bok-stance-pilot"
            with self.assertRaises(ValueError):
                MODULE.ensure_private_output(
                    repository / ".private" / "batch",
                    repository,
                    repository / ".private",
                )
            with self.assertRaises(ValueError):
                MODULE.ensure_private_output(
                    base / "elsewhere" / "batch",
                    repository,
                    external_root,
                )
            with self.assertRaises(ValueError):
                MODULE.ensure_private_output(
                    external_root,
                    repository,
                    external_root,
                )

    def test_private_root_must_be_absolute_and_disjoint_from_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repository = base / "repository"
            repository.mkdir()
            with self.assertRaises(ValueError):
                MODULE.ensure_private_output(
                    Path("relative-private") / "batch",
                    repository,
                    Path("relative-private"),
                )
            with self.assertRaises(ValueError):
                MODULE.ensure_private_output(
                    base / "batch",
                    repository,
                    base,
                )

    def test_private_root_environment_override_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            configured = base / "operator-private"
            result = PRIVATE_STORAGE.default_private_root({
                PRIVATE_STORAGE.PRIVATE_ROOT_ENV: str(configured),
            })
            self.assertEqual(result, configured)

    @unittest.skipUnless(os.name == "nt", "Windows LocalAppData default")
    def test_windows_default_is_local_app_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            local_app_data = Path(directory) / "Local"
            result = PRIVATE_STORAGE.default_private_root({
                "LOCALAPPDATA": str(local_app_data),
            })
            self.assertEqual(
                result,
                local_app_data / PRIVATE_STORAGE.APPLICATION_DIRECTORY,
            )

    def test_site_url_must_be_the_exact_canonical_project_url(self) -> None:
        self.assertEqual(
            MODULE.validate_site_url(MODULE.DEFAULT_SITE_URL),
            MODULE.DEFAULT_SITE_URL,
        )
        invalid_urls = (
            "http://khdouble.github.io/bok-stance-pilot-site/",
            "https://KHDOUBLE.github.io/bok-stance-pilot-site/",
            "https://khdouble.github.io/bok-stance-pilot-site",
            "https://khdouble.github.io/other/",
            "https://khdouble.github.io.evil.example/bok-stance-pilot-site/",
            "https://khdouble.github.io@evil.example/bok-stance-pilot-site/",
            "https://user@khdouble.github.io/bok-stance-pilot-site/",
            "https://khdouble.github.io:443/bok-stance-pilot-site/",
            "https://khdouble.github.io/bok-stance-pilot-site/?source=test",
            "https://khdouble.github.io/bok-stance-pilot-site/#invite=attacker",
            "https://khdouble.github.io/bok-stance-pilot-site/?",
            "https://khdouble.github.io/bok-stance-pilot-site/#",
        )
        for site_url in invalid_urls:
            with self.subTest(site_url=site_url), self.assertRaises(ValueError):
                MODULE.validate_site_url(site_url)

    def test_secret_is_exactly_256_bits(self) -> None:
        key = bytes(range(32))
        encoded = base64.b64encode(key).decode("ascii")
        self.assertEqual(MODULE.decode_key(encoded), key)
        with self.assertRaises(ValueError):
            MODULE.decode_key(base64.b64encode(bytes(31)).decode("ascii"))


    def test_pi_manual_credential_is_external_raw_once_and_seed_digest_only(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repository = base / "repository"
            (repository / "docs").mkdir(parents=True)
            digest = "f" * 64
            version = "v260903-pilot-hosted-1"
            (repository / "docs" / "instrument.json").write_text(
                json.dumps({
                    "instrument_sha256": digest,
                    "hosted_version": version,
                }),
                encoding="utf-8",
            )
            private_root = base / "local-app-data" / "bok-stance-pilot"
            expiry = datetime.now(timezone.utc) + timedelta(hours=2)
            key = bytes(range(32))
            credential_path, seed_path = MODULE.provision_pi_manual_test(
                output=private_root / "pi-account",
                repository_root=repository,
                instrument_sha256=digest,
                expires_at=expiry,
                hmac_key=key,
                private_root=private_root,
            )
            payload = json.loads(
                credential_path.read_text(encoding="utf-8")
            )
            admin_id = payload["credential"]["admin_id"]
            password = payload["credential"]["admin_password"]
            self.assertRegex(admin_id, r"^PI-[A-Z0-9]{12}$")
            self.assertEqual(len(password), 43)
            raw_password = base64.urlsafe_b64decode(password + "=")
            self.assertEqual(len(raw_password), 32)
            sql = seed_path.read_text(encoding="utf-8")
            self.assertNotIn(admin_id, sql)
            self.assertNotIn(password, sql)
            self.assertIn("'pi_manual_test'", sql)
            mapping = MODULE.validate_pi_manual_artifacts(
                credential_path,
                seed_path,
                repository,
                digest,
                expiry,
                key,
            )
            self.assertEqual(
                set(mapping),
                {
                    "invite_id",
                    "admin_id_hmac",
                    "admin_password_hmac",
                    "instrument_sha256",
                    "instrument_version",
                    "assignment_code",
                    "expires_at",
                },
            )
            self.assertNotIn(admin_id, json.dumps(mapping))
            self.assertNotIn(password, json.dumps(mapping))
            self.assertIn(mapping["admin_id_hmac"], sql)
            self.assertIn(mapping["admin_password_hmac"], sql)
            self.assertNotEqual(
                mapping["admin_id_hmac"],
                mapping["admin_password_hmac"],
            )

            class FakeBackend:
                def __init__(self) -> None:
                    self.calls: list[dict[str, str]] = []

                def seed_pi_manual_test(
                    self, values: dict[str, str]
                ) -> None:
                    self.calls.append(values)

            backend = FakeBackend()
            phrase = MODULE.linked_pi_seed_confirmation(
                digest, mapping["invite_id"]
            )
            with self.assertRaisesRegex(ValueError, "confirmation"):
                MODULE.apply_linked_pi_seed(mapping, "wrong", backend)
            self.assertEqual(backend.calls, [])
            MODULE.apply_linked_pi_seed(mapping, phrase, backend)
            self.assertEqual(backend.calls, [mapping])

            tampered = dict(payload)
            tampered["credential"] = dict(payload["credential"])
            tampered["credential"]["admin_password"] = (
                password[:-1] + ("A" if password[-1] != "A" else "B")
            )
            credential_path.write_text(
                json.dumps(tampered),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "artifacts"):
                MODULE.validate_pi_manual_artifacts(
                    credential_path,
                    seed_path,
                    repository,
                    digest,
                    expiry,
                    key,
                )
            invalid_created = dict(payload)
            invalid_created["created_at"] = invalid_created["expires_at"]
            credential_path.write_text(
                json.dumps(invalid_created),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "artifacts"):
                MODULE.validate_pi_manual_artifacts(
                    credential_path,
                    seed_path,
                    repository,
                    digest,
                    expiry,
                    key,
                )

    def test_pi_manual_main_stdout_never_contains_raw_credential(
        self,
    ) -> None:
        repository = SCRIPT.resolve().parents[2]
        instrument = json.loads(
            (repository / "docs" / "instrument.json").read_text(
                encoding="utf-8"
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            private_root = Path(directory) / "private"
            output = private_root / "pi-account"
            key = bytes(range(32))
            stdout = io.StringIO()
            with mock.patch.dict(
                MODULE.os.environ,
                {
                    "INVITE_HMAC_SECRET_B64":
                        base64.b64encode(key).decode("ascii")
                },
                clear=True,
            ), contextlib.redirect_stdout(stdout):
                result = MODULE.main([
                    "--private-root", str(private_root),
                    "--output", str(output),
                    "--instrument-sha256",
                    instrument["instrument_sha256"],
                    "--expires-at",
                    (
                        datetime.now(timezone.utc) + timedelta(hours=2)
                    ).isoformat(),
                    "--mode", MODULE.PI_MANUAL_TEST_MODE,
                ])
            self.assertEqual(result, 0)
            payload = json.loads(
                (output / "pi_manual_test.private.json").read_text(
                    encoding="utf-8"
                )
            )
            rendered = stdout.getvalue()
            self.assertNotIn(payload["credential"]["admin_id"], rendered)
            self.assertNotIn(
                payload["credential"]["admin_password"], rendered
            )
            self.assertIn("values not printed", rendered)

    def test_pi_manual_linked_seed_routes_only_digest_mapping(self) -> None:
        repository = SCRIPT.resolve().parents[2]
        instrument = json.loads(
            (repository / "docs" / "instrument.json").read_text(
                encoding="utf-8"
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            private_root = Path(directory) / "private"
            output = private_root / "pi-linked"
            key = bytes(range(32))

            class FakeBackend:
                instances: list["FakeBackend"] = []

                def __init__(self) -> None:
                    self.calls: list[dict[str, str]] = []
                    self.instances.append(self)

                def seed_pi_manual_test(
                    self, values: dict[str, str]
                ) -> None:
                    self.calls.append(values)

            stdout = io.StringIO()
            with (
                mock.patch.dict(
                    MODULE.os.environ,
                    {
                        "INVITE_HMAC_SECRET_B64":
                            base64.b64encode(key).decode("ascii")
                    },
                    clear=True,
                ),
                mock.patch.object(
                    MODULE, "LinkedCliBackend", FakeBackend
                ),
                mock.patch.object(
                    MODULE,
                    "linked_pi_seed_confirmation",
                    return_value="CONFIRM PI SEED",
                ),
                mock.patch("builtins.input", return_value="CONFIRM PI SEED"),
                contextlib.redirect_stdout(stdout),
            ):
                result = MODULE.main([
                    "--private-root", str(private_root),
                    "--output", str(output),
                    "--instrument-sha256",
                    instrument["instrument_sha256"],
                    "--expires-at",
                    (
                        datetime.now(timezone.utc) + timedelta(hours=2)
                    ).isoformat(),
                    "--mode", MODULE.PI_MANUAL_TEST_MODE,
                    "--seed-via", MODULE.SEED_VIA_LINKED_CLI,
                ])
            self.assertEqual(result, 0)
            self.assertEqual(len(FakeBackend.instances), 1)
            self.assertEqual(len(FakeBackend.instances[0].calls), 1)
            mapping = FakeBackend.instances[0].calls[0]
            self.assertEqual(set(mapping), {
                "invite_id",
                "admin_id_hmac",
                "admin_password_hmac",
                "instrument_sha256",
                "instrument_version",
                "assignment_code",
                "expires_at",
            })
            payload = json.loads(
                (output / "pi_manual_test.private.json").read_text(
                    encoding="utf-8"
                )
            )
            rendered = stdout.getvalue()
            self.assertNotIn(payload["credential"]["admin_id"], rendered)
            self.assertNotIn(
                payload["credential"]["admin_password"], rendered
            )
            self.assertNotIn(
                payload["credential"]["admin_id"], json.dumps(mapping)
            )
            self.assertNotIn(
                payload["credential"]["admin_password"], json.dumps(mapping)
            )

    def test_pi_manual_expiry_and_assignment_fail_closed(self) -> None:
        args = MODULE.parser().parse_args([
            "--output", "external",
            "--instrument-sha256", "a" * 64,
            "--expires-at", "2026-09-04T00:00:00Z",
            "--mode", MODULE.PI_MANUAL_TEST_MODE,
        ])
        self.assertEqual(args.mode, MODULE.PI_MANUAL_TEST_MODE)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repository = base / "repository"
            (repository / "docs").mkdir(parents=True)
            (repository / "docs" / "instrument.json").write_text(
                json.dumps({
                    "instrument_sha256": "a" * 64,
                    "hosted_version": "v260903-pilot-hosted-1",
                }),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "within 24 hours"):
                MODULE.provision_pi_manual_test(
                    output=base / "private" / "late",
                    repository_root=repository,
                    instrument_sha256="a" * 64,
                    expires_at=(
                        datetime.now(timezone.utc) + timedelta(hours=25)
                    ),
                    hmac_key=bytes(range(32)),
                    private_root=base / "private",
                )


if __name__ == "__main__":
    unittest.main()
