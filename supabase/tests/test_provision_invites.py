from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import json
import os
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
