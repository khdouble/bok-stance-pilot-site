#!/usr/bin/env python3
"""Validate and collect PII-free Supabase exports for the hosted pilot only."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


OFFLINE_PARENT_SHA256 = "de96c00e9f95a9035cfc54a1bf18cc0d24b3465f7b5edc05cd9669a3fbfa7dee"
HOSTED_VERSION = "v260911-r5-public-4"
RESPONSE_COUNT = 12
UUID_V4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
HEX_64 = re.compile(r"^[0-9a-f]{64}$")
INTEGER = re.compile(r"^(?:0|-?[1-9][0-9]*)$")
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
REASON_CODES = {"NONE", "IRRELEVANT", "CONTEXT_NEEDED", "MIXED_UNRESOLVED", "TERMINOLOGY", "OTHER"}
RESPONSE_COLUMNS = (
    "submission_id", "assignment_code", "instrument_sha256", "instrument_version",
    "display_position", "assignment_id", "pilot_item_id", "sentence_text", "label5",
    "abstain", "reason_code", "confidence", "reason_note", "started_at", "finished_at",
    "active_duration_seconds", "response_status", "dataset_role", "excluded_from_analysis",
    "analysis_exclusion_reason", "submitted_at", "payload_sha256",
)
FEEDBACK_COLUMNS = (
    "submission_id", "assignment_code", "instrument_sha256", "instrument_version",
    "fatigue_1to5", "zero_vs_99_explanation", "change_vs_stance_explanation", "ui_error_note",
    "session_started_at", "session_finished_at", "active_duration_seconds", "submitted_at",
    "dataset_role", "excluded_from_analysis", "analysis_exclusion_reason", "payload_sha256",
)
COMMON_FIXED = {
    "instrument_version": HOSTED_VERSION,
    "dataset_role": "r3_content_response_pilot",
    "excluded_from_analysis": "true",
    "analysis_exclusion_reason": "r3_repilot_never_analysis",
}


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_instrument(path: Path) -> tuple[str, dict[str, list[dict[str, Any]]]]:
    instrument = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(instrument, dict):
        raise ValueError("instrument root must be an object")
    declared = instrument.get("instrument_sha256")
    payload = dict(instrument)
    payload.pop("instrument_sha256", None)
    calculated = hashlib.sha256(canonical_json(payload)).hexdigest()
    if not isinstance(declared, str) or not HEX_64.fullmatch(declared) or declared != calculated:
        raise ValueError("hosted instrument hash is invalid")
    if declared == OFFLINE_PARENT_SHA256:
        raise ValueError("hosted collector refuses the parent offline instrument hash")
    if instrument.get("hosted_version") != HOSTED_VERSION or instrument.get("items_per_participant") != RESPONSE_COUNT:
        raise ValueError("hosted instrument version or response count changed")
    if instrument.get("source_offline_instrument_sha256") != OFFLINE_PARENT_SHA256:
        raise ValueError("hosted instrument parent offline hash changed")
    for field, expected in COMMON_FIXED.items():
        if field == "instrument_version":
            continue
        instrument_value: object = instrument.get(field)
        expected_value: object = expected == "true" if field == "excluded_from_analysis" else expected
        if instrument_value != expected_value:
            raise ValueError(f"hosted instrument {field} changed")
    assignments = instrument.get("assignments")
    expected_codes = [f"PILOT_R{number:02d}" for number in range(1, 6)]
    if not isinstance(assignments, dict) or sorted(assignments) != expected_codes:
        raise ValueError("hosted instrument assignments are invalid")
    for code in expected_codes:
        rows = assignments[code]
        if len(rows) != RESPONSE_COUNT or [row["display_position"] for row in rows] != list(range(1, 13)):
            raise ValueError(f"{code} is not an ordered 12-item assignment")
    return declared, assignments


def read_csv_exact(path: Path, expected: tuple[str, ...]) -> list[dict[str, str]]:
    if not path.is_file():
        raise ValueError(f"input CSV does not exist: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != expected:
            raise ValueError(f"unexpected CSV header in {path.name}")
        rows = list(reader)
    if any(None in row or any(value is None for value in row.values()) for row in rows):
        raise ValueError(f"malformed extra CSV fields in {path.name}")
    return rows


def strict_integer(value: str, field: str, minimum: int, maximum: int) -> int:
    if not INTEGER.fullmatch(value):
        raise ValueError(f"{field} must be an exact integer")
    parsed = int(value)
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{field} is outside {minimum}..{maximum}")
    return parsed


def timestamp(value: str, field: str) -> datetime:
    if not TIMESTAMP.fullmatch(value):
        raise ValueError(f"{field} must use UTC millisecond ISO format")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def validate_fixed(row: dict[str, str], instrument_hash: str, prefix: str) -> None:
    if row["instrument_sha256"] != instrument_hash:
        raise ValueError(f"{prefix}: hosted instrument hash mismatch")
    for field, expected in COMMON_FIXED.items():
        if row[field] != expected:
            raise ValueError(f"{prefix}: {field} mismatch")
    if not HEX_64.fullmatch(row["payload_sha256"]):
        raise ValueError(f"{prefix}: payload_sha256 is invalid")
    if not UUID_V4.fullmatch(row["submission_id"]):
        raise ValueError(f"{prefix}: submission_id must be UUID v4")


def validate_collection(
    responses: list[dict[str, str]],
    feedback: list[dict[str, str]],
    instrument_hash: str,
    assignments: dict[str, list[dict[str, Any]]],
    expected_submissions: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    if expected_submissions not in {3, 4, 5}:
        raise ValueError("expected_submissions must be 3, 4, or 5")
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row_number, row in enumerate(responses, start=2):
        validate_fixed(row, instrument_hash, f"responses line {row_number}")
        grouped[row["submission_id"]].append(row)
    if len(grouped) != expected_submissions or len(responses) != expected_submissions * RESPONSE_COUNT:
        raise ValueError("response export does not match expected submission count times 12")
    feedback_by_id: dict[str, dict[str, str]] = {}
    for row_number, row in enumerate(feedback, start=2):
        validate_fixed(row, instrument_hash, f"feedback line {row_number}")
        if row["submission_id"] in feedback_by_id:
            raise ValueError("feedback export contains duplicate submission_id")
        feedback_by_id[row["submission_id"]] = row
    if len(feedback_by_id) != expected_submissions or set(feedback_by_id) != set(grouped):
        raise ValueError("response and feedback submission sets differ")

    used_codes: set[str] = set()
    current_time = now or datetime.now(timezone.utc)
    validated_response_rows: list[dict[str, str]] = []
    validated_feedback_rows: list[dict[str, str]] = []
    for submission_id in sorted(grouped):
        rows = sorted(grouped[submission_id], key=lambda row: strict_integer(row["display_position"], "display_position", 1, 12))
        feedback_row = feedback_by_id[submission_id]
        code = feedback_row["assignment_code"]
        if code not in assignments or code in used_codes:
            raise ValueError("assignment_code must be a unique hosted assignment")
        used_codes.add(code)
        if any(row["assignment_code"] != code for row in rows):
            raise ValueError(f"{submission_id}: assignment_code differs across rows")
        if len(rows) != RESPONSE_COUNT:
            raise ValueError(f"{submission_id}: exactly 12 response rows required")
        session_start = timestamp(feedback_row["session_started_at"], "session_started_at")
        session_finish = timestamp(feedback_row["session_finished_at"], "session_finished_at")
        submitted = timestamp(feedback_row["submitted_at"], "submitted_at")
        session_active = strict_integer(feedback_row["active_duration_seconds"], "session active_duration_seconds", 1, 21600)
        if session_start >= session_finish or session_finish > submitted or submitted > current_time + timedelta(minutes=5):
            raise ValueError(f"{submission_id}: session/submission timing is invalid")
        if session_active > (session_finish - session_start).total_seconds() + 5:
            raise ValueError(f"{submission_id}: active session time exceeds wall time")
        if feedback_row["fatigue_1to5"]:
            strict_integer(feedback_row["fatigue_1to5"], "fatigue_1to5", 1, 5)
        if len(feedback_row["zero_vs_99_explanation"]) > 2000 or len(feedback_row["change_vs_stance_explanation"]) > 2000:
            raise ValueError(f"{submission_id}: usability feedback exceeds 2000 characters")
        if len(feedback_row["ui_error_note"]) > 2000:
            raise ValueError(f"{submission_id}: ui_error_note exceeds 2000 characters")

        expected_rows = assignments[code]
        total_item_active = 0
        for index, (row, expected) in enumerate(zip(rows, expected_rows, strict=True), start=1):
            if strict_integer(row["display_position"], "display_position", 1, 12) != index:
                raise ValueError(f"{submission_id}: positions must be exactly 1 through 12")
            for field in ("assignment_id", "pilot_item_id", "sentence_text"):
                if row[field] != str(expected[field]):
                    raise ValueError(f"{submission_id} position {index}: {field} differs from hosted instrument")
            if row["response_status"] != "COMPLETED":
                raise ValueError(f"{submission_id} position {index}: response_status must be COMPLETED")
            if row["abstain"] not in {"true", "false"}:
                raise ValueError(f"{submission_id} position {index}: abstain must be lowercase boolean")
            if row["reason_code"] not in REASON_CODES:
                raise ValueError(f"{submission_id} position {index}: reason_code is invalid")
            if row["abstain"] == "true":
                if row["label5"] != "" or row["reason_code"] == "NONE":
                    raise ValueError(f"{submission_id} position {index}: invalid abstention encoding")
            elif strict_integer(row["label5"], "label5", -2, 2) not in {-2, -1, 0, 1, 2}:
                raise ValueError(f"{submission_id} position {index}: label5 is invalid")
            if row["reason_code"] == "OTHER":
                if not row["reason_note"].strip() or len(row["reason_note"]) > 1000:
                    raise ValueError(f"{submission_id} position {index}: OTHER requires reason_note")
            elif row["reason_note"] != "":
                raise ValueError(f"{submission_id} position {index}: reason_note allowed only for OTHER")
            strict_integer(row["confidence"], "confidence", 1, 5)
            item_start = timestamp(row["started_at"], "started_at")
            item_finish = timestamp(row["finished_at"], "finished_at")
            item_active = strict_integer(row["active_duration_seconds"], "item active_duration_seconds", 0, 21600)
            total_item_active += item_active
            if item_start < session_start or item_finish > session_finish or item_start >= item_finish:
                raise ValueError(f"{submission_id} position {index}: item timing outside session")
            if item_active > (item_finish - item_start).total_seconds() + 1:
                raise ValueError(f"{submission_id} position {index}: active item time exceeds wall time")
            for field in ("submitted_at", "payload_sha256", "instrument_sha256", "instrument_version"):
                if row[field] != feedback_row[field]:
                    raise ValueError(f"{submission_id}: {field} differs between response and feedback exports")
            validated_response_rows.append(row)
        if total_item_active > session_active + 5:
            raise ValueError(f"{submission_id}: total item active time exceeds session active time")
        validated_feedback_rows.append(feedback_row)
    return {
        "responses": validated_response_rows,
        "feedback": validated_feedback_rows,
        "submission_count": expected_submissions,
        "response_count": len(validated_response_rows),
        "assignment_codes": sorted(used_codes),
    }


def excel_safe(value: str) -> str:
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


def safe_rows(rows: list[dict[str, str]], free_text_fields: set[str]) -> list[dict[str, str]]:
    return [
        {key: excel_safe(value) if key in free_text_fields else value for key, value in row.items()}
        for row in rows
    ]


def ensure_output(output: Path, repository_root: Path) -> Path:
    exports_root = (repository_root / "exports").resolve()
    target = output.resolve()
    try:
        target.relative_to(exports_root)
    except ValueError as exc:
        raise ValueError(f"--output must be below {exports_root}") from exc
    if target == exports_root:
        raise ValueError("--output must be a new collection directory below exports")
    if os.path.lexists(target):
        raise FileExistsError(f"refusing to overwrite existing output: {target}")
    return target


def write_collection(
    output: Path,
    repository_root: Path,
    validated: dict[str, Any],
    response_input: Path,
    feedback_input: Path,
    instrument_path: Path,
    instrument_hash: str,
) -> Path:
    target = ensure_output(output, repository_root)
    target.mkdir(parents=True, exist_ok=False)
    response_output = target / "hosted_responses_validated.csv"
    feedback_output = target / "hosted_feedback_validated.csv"
    with response_output.open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RESPONSE_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(safe_rows(validated["responses"], {"sentence_text", "reason_note"}))
    with feedback_output.open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FEEDBACK_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(safe_rows(validated["feedback"], {
            "zero_vs_99_explanation", "change_vs_stance_explanation", "ui_error_note",
        }))
    manifest = {
        "schema_version": "1.0",
        "collector": "collect_hosted_pilot.py",
        "source_system": "hosted_supabase",
        "offline_collector_used": False,
        "instrument_identity_preserved": True,
        "instrument_sha256": instrument_hash,
        "instrument_version": HOSTED_VERSION,
        "parent_offline_instrument_sha256": OFFLINE_PARENT_SHA256,
        "dataset_role": "r3_content_response_pilot",
        "excluded_from_analysis": True,
        "analysis_exclusion_reason": "r3_repilot_never_analysis",
        "submission_count": validated["submission_count"],
        "response_count": validated["response_count"],
        "assignment_codes": validated["assignment_codes"],
        "input_sha256": {
            "responses": sha256_file(response_input),
            "feedback": sha256_file(feedback_input),
            "instrument": sha256_file(instrument_path),
        },
        "outputs": {
            response_output.name: sha256_file(response_output),
            feedback_output.name: sha256_file(feedback_output),
        },
        "spreadsheet_formula_prefixes_escaped": True,
        "direct_identifiers_present": False,
    }
    manifest_path = target / "hosted_collection_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest_path


def parser() -> argparse.ArgumentParser:
    repository_root = Path(__file__).resolve().parents[1]
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--responses", type=Path, required=True)
    result.add_argument("--feedback", type=Path, required=True)
    result.add_argument("--instrument", type=Path, default=repository_root / "docs" / "instrument.json")
    result.add_argument("--expected-submissions", type=int, choices=(3, 4, 5), required=True)
    result.add_argument("--output", type=Path, required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    repository_root = Path(__file__).resolve().parents[1]
    try:
        instrument_path = args.instrument.resolve()
        expected_instrument = (repository_root / "docs" / "instrument.json").resolve()
        if instrument_path != expected_instrument:
            raise ValueError(f"--instrument must be {expected_instrument}")
        instrument_hash, assignments = load_instrument(instrument_path)
        response_rows = read_csv_exact(args.responses.resolve(), RESPONSE_COLUMNS)
        feedback_rows = read_csv_exact(args.feedback.resolve(), FEEDBACK_COLUMNS)
        validated = validate_collection(
            response_rows, feedback_rows, instrument_hash, assignments, args.expected_submissions,
        )
        manifest = write_collection(
            args.output, repository_root, validated, args.responses.resolve(), args.feedback.resolve(),
            instrument_path, instrument_hash,
        )
    except (ValueError, FileExistsError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(
        f"PASS hosted collection submissions={validated['submission_count']} "
        f"responses={validated['response_count']} manifest={manifest}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
