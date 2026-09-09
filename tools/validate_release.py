#!/usr/bin/env python3
"""Validate the active R5 public release before deployment or fielding."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Check:
    check: str
    passed: bool
    detail: str


@dataclass
class ValidationResult:
    checks: list[Check]

    @property
    def failures(self) -> list[dict[str, object]]:
        return [
            {"check": item.check, "passed": item.passed, "detail": item.detail}
            for item in self.checks
            if not item.passed
        ]


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _unique_object(path: Path) -> dict[str, Any]:
    def reject(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject)
    if not isinstance(value, dict):
        raise ValueError("JSON root is not an object")
    return value


def validate(
    repository_root: Path,
    _legacy_source_pilot: Path | None = None,
    expected_fielding: str = "live",
) -> ValidationResult:
    """Validate the active R5 release; the legacy source argument is ignored."""
    checks: list[Check] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append(Check(name, passed, detail))

    root = repository_root.resolve()
    if expected_fielding not in {"live", "staging"}:
        check("requested_state", False, "expected fielding must be live or staging")
        return ValidationResult(checks)
    tools_path = root / "tools"
    if str(tools_path) not in sys.path:
        sys.path.insert(0, str(tools_path))
    try:
        import build_r5_deployment_manifest as deployment
        import build_r5_public_instrument as instrument_builder
        import pi_config
    except ImportError as exc:
        check("r5_tools_import", False, type(exc).__name__)
        return ValidationResult(checks)

    required = (
        "index.html", "privacy.html", "app.js", "site-config.js",
        "instrument.json", "instrument-hash.js", "deployment-manifest.json",
    )
    missing = [name for name in required if not (root / "docs" / name).is_file()]
    check("required_public_files", not missing, ", ".join(missing) or "present")
    if missing or root != instrument_builder.REPO_ROOT.resolve():
        if root != instrument_builder.REPO_ROOT.resolve():
            check("repository_root", False, "R5 builders are not loaded from the requested repository")
        return ValidationResult(checks)

    instrument_path = root / "docs" / "instrument.json"
    try:
        instrument = _unique_object(instrument_path)
        declared = instrument.pop("instrument_sha256")
        digest_ok = isinstance(declared, str) and declared == _digest(_canonical_json(instrument))
        instrument["instrument_sha256"] = declared
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError, KeyError) as exc:
        instrument = {}
        declared = ""
        digest_ok = False
        check("instrument_self_digest", False, type(exc).__name__)
    else:
        check("instrument_self_digest", digest_ok, "canonical SHA-256")

    if instrument:
        expected_sources = {
            name: _digest((root / relative).read_bytes())
            for name, relative in instrument_builder.RELEASE_SOURCE_PATHS.items()
        }
        check(
            "release_source_hashes",
            instrument.get("release_source_hashes") == expected_sources,
            "R5 source-map attestation",
        )
        check(
            "r5_identity",
            instrument.get("hosted_version") == "v260910-r5-public-3"
            and instrument.get("dataset_role") == "r3_content_response_pilot"
            and instrument.get("excluded_from_analysis") is True
            and instrument.get("analysis_exclusion_reason") == "r3_repilot_never_analysis",
            "version and pilot-only boundary",
        )
        try:
            with tempfile.TemporaryDirectory() as directory:
                temporary = Path(directory)
                old_hash_path = instrument_builder.HASH_JS_PATH
                instrument_builder.HASH_JS_PATH = temporary / "instrument-hash.js"
                try:
                    rebuilt = instrument_builder.build(
                        root / "instrument_sources" / "r5_20260908.json",
                        temporary / "instrument.json",
                    )
                finally:
                    instrument_builder.HASH_JS_PATH = old_hash_path
                check(
                    "instrument_rebuild",
                    rebuilt == instrument and (temporary / "instrument.json").read_bytes() == instrument_path.read_bytes(),
                    "deterministic R5 build",
                )
        except (OSError, ValueError, TypeError) as exc:
            check("instrument_rebuild", False, type(exc).__name__)

    config_path = root / "docs" / "site-config.js"
    privacy_path = root / "docs" / "privacy.html"
    try:
        config = config_path.read_text(encoding="utf-8")
        privacy = privacy_path.read_text(encoding="utf-8")
        errors = pi_config.validate_live_config(
            config, privacy, today=datetime.now(timezone.utc).date(), now=datetime.now(timezone.utc)
        )
        check("live_governance_config", not errors, "; ".join(errors) or "valid")
    except (OSError, UnicodeError, ValueError) as exc:
        check("live_governance_config", False, type(exc).__name__)

    try:
        expected_manifest = deployment.build(expected_fielding)
        actual_manifest = _unique_object(root / "docs" / "deployment-manifest.json")
        check(
            "deployment_manifest",
            actual_manifest == expected_manifest,
            f"state={expected_fielding}",
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        check("deployment_manifest", False, type(exc).__name__)

    return ValidationResult(checks)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--expect-fielding", choices=("live", "staging"), default="live")
    args = parser.parse_args()
    result = validate(args.repository, expected_fielding=args.expect_fielding)
    for item in result.checks:
        print(f"{'PASS' if item.passed else 'FAIL'}: {item.check} — {item.detail}")
    return 1 if result.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
