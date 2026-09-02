from __future__ import annotations

import base64
import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


ADMIN = Path(__file__).resolve().parents[1] / "admin"
if str(ADMIN) not in sys.path:
    sys.path.insert(0, str(ADMIN))
SCRIPT = ADMIN / "decrypt_identity_export.py"
SPEC = importlib.util.spec_from_file_location("decrypt_identity_export", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class DecryptIdentityExportTest(unittest.TestCase):
    def test_decrypts_only_below_private_and_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repository = base / "repository"
            repository.mkdir()
            private = base / "local-app-data" / "bok-stance-pilot"
            private.mkdir(parents=True)
            source = private / "cipher.csv"
            target = private / "identity.csv"
            key = bytes(range(32))
            iv = bytes(range(12))
            aad = "instrument-hash:invite-hmac"
            identity = {
                "name": "TEST PARTICIPANT",
                "phone": "01000000000",
            }
            ciphertext = AESGCM(key).encrypt(iv, json.dumps(identity).encode("utf-8"), aad.encode("utf-8"))
            with source.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=MODULE.INPUT_COLUMNS, lineterminator="\n")
                writer.writeheader()
                writer.writerow({
                    "participant_id": "00000000-0000-4000-8000-000000000001",
                    "submission_id": "00000000-0000-4000-8000-000000000002",
                    "assignment_code": "PILOT_R01",
                    "identity_ciphertext_b64": base64.b64encode(ciphertext).decode("ascii"),
                    "identity_iv_b64": base64.b64encode(iv).decode("ascii"),
                    "identity_aad": aad,
                    "encryption_key_id": "pilot-pii-v1",
                    "consent_version": "consent-v1",
                    "consent_accepted_at": "2026-09-03T00:00:00Z",
                    "submitted_at": "2026-09-03T00:10:00Z",
                })
            count = MODULE.decrypt_export(
                source,
                target,
                repository,
                key,
                "pilot-pii-v1",
                private_root=private,
            )
            self.assertEqual(count, 1)
            with target.open("r", encoding="utf-8-sig", newline="") as handle:
                output = list(csv.DictReader(handle))
            self.assertEqual(output[0]["name"], identity["name"])
            self.assertEqual(output[0]["phone"], identity["phone"])
            with self.assertRaises(FileExistsError):
                MODULE.decrypt_export(
                    source,
                    target,
                    repository,
                    key,
                    "pilot-pii-v1",
                    private_root=private,
                )

    def test_rejects_repository_private_and_path_outside_external_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repository = base / "repository"
            repository.mkdir()
            external_root = base / "local-app-data" / "bok-stance-pilot"
            with self.assertRaises(ValueError):
                MODULE.path_below_private(
                    repository / ".private" / "cipher.csv",
                    repository,
                    must_exist=False,
                    private_root=repository / ".private",
                )
            with self.assertRaises(ValueError):
                MODULE.path_below_private(
                    base / "elsewhere" / "cipher.csv",
                    repository,
                    must_exist=False,
                    private_root=external_root,
                )

    def test_formula_prefix_is_neutralized(self) -> None:
        self.assertEqual(MODULE.excel_safe("=cmd"), "'=cmd")
        self.assertEqual(MODULE.excel_safe("safe"), "safe")


if __name__ == "__main__":
    unittest.main()
