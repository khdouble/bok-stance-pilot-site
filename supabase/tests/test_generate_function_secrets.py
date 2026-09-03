from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


ADMIN = Path(__file__).resolve().parents[1] / "admin"
if str(ADMIN) not in sys.path:
    sys.path.insert(0, str(ADMIN))

SCRIPT = ADMIN / "generate_function_secrets.py"
SPEC = importlib.util.spec_from_file_location(
    "generate_function_secrets", SCRIPT
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class GenerateFunctionSecretsTest(unittest.TestCase):
    def make_roots(self, directory: str) -> tuple[Path, Path]:
        base = Path(directory)
        repository = base / "repository"
        docs = repository / "docs"
        docs.mkdir(parents=True)
        (docs / "instrument.json").write_text(
            json.dumps(
                {
                    "instrument_sha256": "a" * 64,
                    "hosted_version": "v260903-pilot-hosted-1",
                }
            ),
            encoding="utf-8",
        )
        private_root = base / "local-app-data" / "bok-stance-pilot"
        return repository, private_root

    def test_generates_exactly_seven_valid_custom_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository, private_root = self.make_roots(directory)
            target = MODULE.generate_file(
                repository,
                private_root,
                None,
                "consent-v2026-09-03-r1",
                "pilot-pii-v1",
                overwrite=False,
            )
            values = MODULE.parse_env(target.read_text(encoding="utf-8"))
            self.assertEqual(
                tuple(values),
                MODULE.CUSTOM_SECRET_NAMES,
            )
            self.assertNotIn("SUPABASE_DB_URL", values)
            decoded = [
                base64.b64decode(values[name], validate=True)
                for name in MODULE.KEY_NAMES
            ]
            self.assertEqual([len(value) for value in decoded], [32, 32, 32])
            self.assertEqual(len(set(decoded)), 3)
            self.assertEqual(
                MODULE.validate_file(repository, private_root, target),
                target.resolve(),
            )
            self.assertEqual(
                list(target.parent.glob(f".{target.name}.*.tmp")),
                [],
            )

    def test_refuses_overwrite_unless_explicit_and_revalidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository, private_root = self.make_roots(directory)
            target = private_root / "custom.env"
            MODULE.generate_file(
                repository,
                private_root,
                target,
                "consent-v2026-09-03-r1",
                "pilot-pii-v1",
                overwrite=False,
            )
            first = target.read_text(encoding="utf-8")
            with self.assertRaises(FileExistsError):
                MODULE.generate_file(
                    repository,
                    private_root,
                    target,
                    "consent-v2026-09-03-r1",
                    "pilot-pii-v1",
                    overwrite=False,
                )
            MODULE.generate_file(
                repository,
                private_root,
                target,
                "consent-v2026-09-03-r1",
                "pilot-pii-v2",
                overwrite=True,
            )
            second = target.read_text(encoding="utf-8")
            self.assertNotEqual(first, second)
            self.assertEqual(
                MODULE.parse_env(second)["PII_KEY_ID"],
                "pilot-pii-v2",
            )

    def test_validation_rejects_duplicate_keys_and_unexpected_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository, _ = self.make_roots(directory)
            same_key = base64.b64encode(bytes(range(32))).decode("ascii")
            values = {
                "PILOT_INSTRUMENT_SHA256": "a" * 64,
                "PILOT_INSTRUMENT_VERSION": "v260903-pilot-hosted-1",
                "PILOT_CONSENT_VERSION": "consent-v2026-09-03-r1",
                "INVITE_HMAC_SECRET_B64": same_key,
                "IDENTITY_HMAC_SECRET_B64": same_key,
                "PII_ENCRYPTION_KEY_B64": base64.b64encode(
                    bytes(range(32, 64))
                ).decode("ascii"),
                "PII_KEY_ID": "pilot-pii-v1",
            }
            with self.assertRaisesRegex(ValueError, "mutually distinct"):
                MODULE.validate_secret_values(values, repository)
            with self.assertRaisesRegex(ValueError, "unexpected name"):
                MODULE.parse_env(
                    MODULE.serialize_env(values)
                    + "SUPABASE"
                    + "_DB_URL=postgresql://not-allowed\n"
                )

    def test_private_path_and_invalid_governance_values_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository, private_root = self.make_roots(directory)
            with self.assertRaises(ValueError):
                MODULE.generate_file(
                    repository,
                    repository / ".private",
                    repository / ".private" / "pilot-function.env",
                    "consent-v2026-09-03-r1",
                    "pilot-pii-v1",
                    overwrite=False,
                )
            with self.assertRaises(ValueError):
                MODULE.generate_file(
                    repository,
                    private_root,
                    Path(directory) / "elsewhere.env",
                    "consent-v2026-09-03-r1",
                    "pilot-pii-v1",
                    overwrite=False,
                )
            with self.assertRaises(ValueError):
                MODULE.generate_secret_values(
                    repository,
                    "PENDING_PI",
                    "pilot-pii-v1",
                )

    def test_cli_never_prints_generated_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            private_root = Path(directory) / "operator-private"
            output = private_root / "pilot-function.env"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = MODULE.main(
                    [
                        "generate",
                        "--private-root",
                        str(private_root),
                        "--output",
                        str(output),
                        "--consent-version",
                        "consent-v2026-09-03-r1",
                    ]
                )
            self.assertEqual(result, 0)
            values = MODULE.parse_env(output.read_text(encoding="utf-8"))
            rendered = stdout.getvalue()
            for name in MODULE.KEY_NAMES:
                self.assertNotIn(values[name], rendered)
            self.assertIn("without displaying values", rendered)


if __name__ == "__main__":
    unittest.main()
