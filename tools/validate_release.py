#!/usr/bin/env python3
"""Fail-closed static validation for the public hosted pilot repository."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from build_public_instrument import RELEASE_SOURCE_PATHS
from pi_config import (
    boolean_value,
    has_placeholder,
    parse_pi_values,
    validate_live_config,
)

EXPECTED_API_URL = (
    "https://mebisrsvasrzwkmsodsw.supabase.co/functions/v1/pilot-api"
)
EXPECTED_ORIGIN = "https://khdouble.github.io"
FORBIDDEN_PAYLOAD_MARKERS = {
    "intended_label",
    "intended_abstain",
    "intended_reason_code",
    "construct",
    "facilitator_rationale",
    "clarity_band",
}
SECRET_PATTERNS = {
    "Supabase secret key": re.compile(r"sb_secret_[A-Za-z0-9_-]{12,}"),
    "GitHub personal access token": re.compile(
        r"(?:ghp|github_pat)_[A-Za-z0-9_]{12,}"
    ),
    "JWT-like credential": re.compile(
        r"eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}"
    ),
    "Postgres credential URL": re.compile(
        r"postgres(?:ql)?://[^:\s]+:[^@\s]+@", re.IGNORECASE
    ),
}


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


class Validation:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []

    def check(self, name: str, condition: bool, detail: str) -> None:
        self.checks.append(
            {"check": name, "passed": bool(condition), "detail": detail}
        )

    @property
    def failures(self) -> list[dict[str, Any]]:
        return [item for item in self.checks if not item["passed"]]


def public_files(root: Path) -> list[Path]:
    ignored_parts = {".git", "__pycache__", ".private", "responses", "exports"}
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and not any(part in ignored_parts for part in path.parts)
    )


def load_source_assignments(source_pilot: Path) -> dict[str, list[dict[str, object]]]:
    columns = [
        "assignment_id",
        "pilot_rater_id",
        "display_position",
        "pilot_item_id",
        "sentence_text",
    ]
    result: dict[str, list[dict[str, object]]] = {}
    with (source_pilot / "response_template.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as handle:
        for raw in csv.DictReader(handle):
            row: dict[str, object] = {
                "assignment_id": raw["assignment_id"],
                "pilot_rater_id": raw["pilot_rater_id"],
                "display_position": int(raw["display_position"]),
                "pilot_item_id": raw["pilot_item_id"],
                "sentence_text": raw["sentence_text"],
            }
            if list(row) != columns:
                raise AssertionError("payload column order changed")
            result.setdefault(raw["pilot_rater_id"], []).append(row)
    for rows in result.values():
        rows.sort(key=lambda row: int(row["display_position"]))
    return result


def validate(root: Path, source_pilot: Path, expected_fielding: str) -> Validation:
    validation = Validation()
    docs = root / "docs"
    instrument_path = docs / "instrument.json"
    config_path = docs / "site-config.js"
    index_path = docs / "index.html"
    app_path = docs / "app.js"
    contract_path = docs / "submission-contract.js"
    privacy_path = docs / "privacy.html"
    required = [
        instrument_path,
        docs / "instrument-hash.js",
        config_path,
        index_path,
        app_path,
        contract_path,
        docs / "styles.css",
        privacy_path,
        docs / "404.html",
        docs / ".nojekyll",
        docs / "deployment-manifest.json",
        root / ".gitignore",
        root / "supabase" / "config.toml",
        root / "supabase" / "functions" / "pilot-api" / "index.ts",
        root / "supabase" / "functions" / "pilot-api" / "_shared" / "core.ts",
        root / "supabase" / "migrations" / "202609030001_pilot_backend.sql",
    ]
    validation.check(
        "required_public_files",
        all(path.is_file() for path in required),
        ", ".join(str(path.relative_to(root)) for path in required),
    )
    if validation.failures:
        return validation

    instrument = json.loads(instrument_path.read_text(encoding="utf-8"))
    declared_hash = instrument.pop("instrument_sha256", "")
    calculated_hash = sha256_bytes(canonical_json(instrument))
    hash_js = (docs / "instrument-hash.js").read_text(encoding="utf-8")
    validation.check(
        "instrument_sha256",
        bool(re.fullmatch(r"[0-9a-f]{64}", declared_hash))
        and declared_hash == calculated_hash
        and declared_hash in hash_js,
        f"declared={declared_hash} calculated={calculated_hash}",
    )
    validation.check(
        "hosted_parent_distinct",
        declared_hash != instrument.get("source_offline_instrument_sha256")
        and instrument.get("hosted_version") == "v260903-pilot-hosted-1",
        (
            f"hosted={declared_hash} "
            f"parent={instrument.get('source_offline_instrument_sha256')}"
        ),
    )
    validation.check(
        "analysis_exclusion",
        instrument.get("dataset_role") == "synthetic_usability_pilot"
        and instrument.get("excluded_from_analysis") is True
        and instrument.get("analysis_exclusion_reason")
        == "synthetic_usability_only_never_analysis",
        "synthetic pilot must remain excluded",
    )

    assignments = instrument.get("assignments", {})
    expected_raters = [f"PILOT_R{index:02d}" for index in range(1, 6)]
    assignment_shape = (
        sorted(assignments) == expected_raters
        and all(len(assignments[rater]) == 12 for rater in expected_raters)
        and all(
            [row["display_position"] for row in assignments[rater]]
            == list(range(1, 13))
            for rater in expected_raters
        )
    )
    validation.check(
        "assignment_shape",
        assignment_shape,
        f"raters={sorted(assignments)}",
    )
    all_rows = [row for rows in assignments.values() for row in rows]
    validation.check(
        "assignment_uniqueness",
        len(all_rows) == 60
        and len({row["assignment_id"] for row in all_rows}) == 60
        and all(
            len({row["pilot_item_id"] for row in assignments[rater]}) == 12
            for rater in expected_raters
        ),
        f"rows={len(all_rows)}",
    )
    payload_text = json.dumps(instrument, ensure_ascii=False).lower()
    validation.check(
        "facilitator_key_absent",
        not any(marker in payload_text for marker in FORBIDDEN_PAYLOAD_MARKERS),
        "participant payload contains no intended answer or rationale fields",
    )

    source_assignments = load_source_assignments(source_pilot)
    validation.check(
        "frozen_source_assignment_exact",
        assignments == source_assignments,
        "public rows equal frozen response_template rows and order",
    )
    source_hashes = instrument.get("source_hashes", {})
    source_paths = {
        "response_template": source_pilot / "response_template.csv",
        "participant_items": source_pilot / "participant_items.csv",
        "pilot_fieldwork_config": source_pilot / "pilot_fieldwork_config.json",
        "pilot_protocol": source_pilot / "pilot_protocol.md",
        "parent_manifest": source_pilot / "fieldwork" / "manifest.json",
    }
    source_hash_ok = all(
        source_hashes.get(key) == sha256_file(path)
        for key, path in source_paths.items()
    )
    validation.check(
        "frozen_source_hashes",
        source_hash_ok,
        f"sources={sorted(source_paths)}",
    )
    release_hashes = instrument.get("release_source_hashes", {})
    expected_release_paths = {
        name: root / relative
        for name, relative in RELEASE_SOURCE_PATHS.items()
    }
    release_hash_ok = (
        sorted(release_hashes) == sorted(expected_release_paths)
        and all(
            release_hashes.get(name) == sha256_file(path)
            for name, path in expected_release_paths.items()
        )
    )
    validation.check(
        "release_source_hashes",
        release_hash_ok,
        f"sources={sorted(expected_release_paths)}",
    )

    config_text = config_path.read_text(encoding="utf-8")
    index_text = index_path.read_text(encoding="utf-8")
    app_text = app_path.read_text(encoding="utf-8")
    contract_text = contract_path.read_text(encoding="utf-8")
    privacy_text = privacy_path.read_text(encoding="utf-8")
    validation.check(
        "api_and_origin_exact",
        EXPECTED_API_URL in config_text
        and EXPECTED_API_URL.rsplit("/functions/", 1)[0] in index_text
        and EXPECTED_ORIGIN in config_text,
        "configured GitHub origin and Supabase function endpoint",
    )
    csp_match = re.search(
        r'<meta\s+http-equiv="Content-Security-Policy"\s+content="([^"]+)"',
        index_text,
    )
    csp = csp_match.group(1) if csp_match else ""
    validation.check(
        "strict_csp",
        bool(csp)
        and "connect-src 'self' " + EXPECTED_API_URL.rsplit("/functions/", 1)[0]
        in csp
        and "'unsafe-inline'" not in csp
        and "'unsafe-eval'" not in csp
        and "form-action 'none'" in csp,
        csp,
    )
    inline_scripts = re.findall(
        r"<script(?![^>]*\bsrc=)[^>]*>.*?</script>",
        index_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    validation.check(
        "no_inline_script",
        not inline_scripts,
        f"inline_script_blocks={len(inline_scripts)}",
    )
    validation.check(
        "no_default_response",
        not re.search(r"<input[^>]+\bchecked\b", index_text, re.IGNORECASE)
        and '<option value=""' in index_text,
        "no radio, checkbox or select answer is preselected",
    )
    validation.check(
        "pii_not_persisted_client_side",
        "JSON.stringify({ saved_at: nowIso(), state: state })" in app_text
        and "state.identity" not in app_text
        and "state.name" not in app_text
        and "state.phone" not in app_text
        and "client_user_agent" not in app_text
        and "navigator.userAgent" not in app_text,
        "TTL local draft and fallback exports exclude identity and user-agent",
    )
    validation.check(
        "invite_fragment_removed",
        "history.replaceState" in app_text
        and "sessionStorage" not in app_text
        and "localStorage.setItem" in app_text
        and r"/^[A-Za-z0-9_-]{43}$/" in app_text,
        "raw invite is removed from URL and retained in memory only",
    )
    validation.check(
        "draft_retention_and_deletion",
        "DRAFT_TTL_MS = 7 * 24 * 60 * 60 * 1000" in app_text
        and "purgeExpiredDrafts" in app_text
        and 'byId("clearDraft")' in app_text
        and "7일 동안만 복원" in privacy_text,
        "stale drafts are purged on access and have an explicit deletion control",
    )
    validation.check(
        "submit_contract_alignment",
        "CONTRACT.finalizeDigest" in app_text
        and "payload_sha256: finalized.payload_sha256" in app_text
        and "result.receipt.submission_id" in app_text
        and "toFixed(3)" not in app_text
        and "normalizeDigestBasis" in contract_text
        and "stableStringify" in contract_text,
        "normalized canonical digest, integer timing, and receipt envelope",
    )
    validation.check(
        "pii_notice_present",
        all(
            phrase in privacy_text
            for phrase in ("수집 목적", "수집 항목", "보유기간", "동의 거부")
        ),
        "privacy notice contains mandatory headings",
    )

    seed_paths = sorted(
        (root / "supabase" / "migrations").glob("*_seed_pilot_instrument.sql")
    )
    seed_text = (
        seed_paths[0].read_text(encoding="utf-8")
        if len(seed_paths) == 1
        else ""
    )
    validation.check(
        "instrument_seed_parity",
        len(seed_paths) == 1
        and declared_hash in seed_text
        and "__PILOT_" not in seed_text
        and seed_text.count("PILOT_R") >= 5,
        f"seed_files={len(seed_paths)} hash={declared_hash}",
    )

    try:
        enabled = boolean_value(config_text, "fieldingEnabled")
        parse_pi_values(config_text)
        pending = has_placeholder(config_text, privacy_text)
        pi_errors = validate_live_config(config_text, privacy_text)
        if expected_fielding == "staging":
            fielding_ok = not enabled and (pending or not pi_errors)
        else:
            fielding_ok = enabled and not pending and not pi_errors
    except ValueError as error:
        enabled = False
        pending = True
        pi_errors = [str(error)]
        fielding_ok = False
    validation.check(
        "fielding_gate",
        fielding_ok,
        (
            f"expected={expected_fielding} enabled={enabled} "
            f"pending={pending} errors={pi_errors}"
        ),
    )
    deployment_path = docs / "deployment-manifest.json"
    deployment = json.loads(deployment_path.read_text(encoding="utf-8"))
    deployment_hash = deployment.pop("deployment_manifest_sha256", "")
    deployment_files = {
        "privacy_notice": privacy_path,
        "site_config": config_path,
    }
    deployment_ok = (
        deployment_hash == sha256_bytes(canonical_json(deployment))
        and deployment.get("deployment_state") == expected_fielding
        and deployment.get("instrument_sha256") == declared_hash
        and deployment.get("hosted_version") == instrument.get("hosted_version")
        and deployment.get("site_url")
        == "https://khdouble.github.io/bok-stance-pilot-site/"
        and deployment.get("api_url") == EXPECTED_API_URL
        and deployment.get("operational_file_hashes")
        == {
            name: sha256_file(path)
            for name, path in sorted(deployment_files.items())
        }
    )
    validation.check(
        "deployment_manifest",
        deployment_ok,
        f"state={expected_fielding} hash={deployment_hash}",
    )

    secret_hits: list[str] = []
    for path in public_files(root):
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".ico"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for name, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                secret_hits.append(f"{path.relative_to(root)}:{name}")
    validation.check(
        "secret_scan",
        not secret_hits,
        "none" if not secret_hits else ", ".join(secret_hits),
    )
    forbidden_names = [
        path
        for path in public_files(root)
        if any(
            marker in path.name.lower()
            for marker in ("facilitator_key", "response_export", "private_roster")
        )
    ]
    validation.check(
        "forbidden_files_absent",
        not forbidden_names,
        "none"
        if not forbidden_names
        else ", ".join(str(path.relative_to(root)) for path in forbidden_names),
    )
    return validation


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-pilot", type=Path, required=True)
    parser.add_argument(
        "--expect-fielding",
        choices=("staging", "live"),
        default="staging",
    )
    parser.add_argument("--json-report", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    result = validate(root, args.source_pilot.resolve(), args.expect_fielding)
    if args.json_report:
        args.json_report.write_text(
            json.dumps(
                {
                    "status": "PASS" if not result.failures else "FAIL",
                    "checks": result.checks,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    status = "PASS" if not result.failures else "FAIL"
    print(
        f"[{status}] checks={len(result.checks)} "
        f"failures={len(result.failures)}"
    )
    for failure in result.failures:
        print(f"[FAIL] {failure['check']}: {failure['detail']}")
    return 0 if not result.failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
