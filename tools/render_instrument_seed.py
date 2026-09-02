#!/usr/bin/env python3
"""Render one immutable PostgreSQL seed migration from docs/instrument.json."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any


EXPECTED_SCHEMA_VERSION = "1.0"
EXPECTED_HOSTED_VERSION = "v260903-pilot-hosted-1"
EXPECTED_SOURCE_SHA256 = "b594a196eb7be720e57d974f4b5c6e4437b697e6ae20f01013e830af35707a51"
EXPECTED_ASSIGNMENT_CODES = tuple(f"PILOT_R{number:02d}" for number in range(1, 6))
EXPECTED_ITEMS_PER_ASSIGNMENT = 12
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,96}$")
PLACEHOLDER_RE = re.compile(r"__[A-Z][A-Z0-9_]*__|\b(?:PLACEHOLDER|TODO|TBD)\b", re.IGNORECASE)
ROW_KEYS = {
    "assignment_id",
    "pilot_rater_id",
    "display_position",
    "pilot_item_id",
    "sentence_text",
}


class DuplicateJsonKey(ValueError):
    pass


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKey(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def load_json(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_keys)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, DuplicateJsonKey) as exc:
        raise ValueError(f"cannot read a unique-key UTF-8 instrument JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ValueError("instrument JSON root must be an object")
    return loaded


def require_exact_int(value: object, expected: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value != expected:
        raise ValueError(f"{field} must be integer {expected}")


def validate_instrument(instrument: dict[str, Any]) -> dict[str, Any]:
    serialized = json.dumps(instrument, ensure_ascii=False)
    match = PLACEHOLDER_RE.search(serialized)
    if match:
        raise ValueError(f"instrument contains forbidden placeholder marker: {match.group(0)}")

    declared_hash = instrument.get("instrument_sha256")
    if not isinstance(declared_hash, str) or not HASH_RE.fullmatch(declared_hash):
        raise ValueError("instrument_sha256 must be a lowercase SHA-256 digest")
    hash_payload = dict(instrument)
    del hash_payload["instrument_sha256"]
    calculated_hash = hashlib.sha256(canonical_json(hash_payload)).hexdigest()
    if declared_hash != calculated_hash:
        raise ValueError(f"instrument hash mismatch: declared={declared_hash} calculated={calculated_hash}")
    if instrument.get("schema_version") != EXPECTED_SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {EXPECTED_SCHEMA_VERSION}")
    if instrument.get("hosted_version") != EXPECTED_HOSTED_VERSION:
        raise ValueError(f"hosted_version must be {EXPECTED_HOSTED_VERSION}")
    if instrument.get("source_offline_instrument_sha256") != EXPECTED_SOURCE_SHA256:
        raise ValueError("source_offline_instrument_sha256 does not match the frozen offline instrument")
    require_exact_int(instrument.get("items_per_participant"), EXPECTED_ITEMS_PER_ASSIGNMENT, "items_per_participant")
    if (
        instrument.get("dataset_role") != "synthetic_usability_pilot"
        or instrument.get("excluded_from_analysis") is not True
        or instrument.get("analysis_exclusion_reason") != "synthetic_usability_only_never_analysis"
    ):
        raise ValueError("pilot analysis-exclusion contract changed")

    assignments = instrument.get("assignments")
    if not isinstance(assignments, dict) or tuple(sorted(assignments)) != EXPECTED_ASSIGNMENT_CODES:
        raise ValueError("assignments must contain exactly PILOT_R01 through PILOT_R05")

    assignment_ids: set[str] = set()
    item_text: dict[str, str] = {}
    item_counts: Counter[str] = Counter()
    normalized: dict[str, list[dict[str, Any]]] = {}
    for assignment_code in EXPECTED_ASSIGNMENT_CODES:
        rows = assignments[assignment_code]
        if not isinstance(rows, list) or len(rows) != EXPECTED_ITEMS_PER_ASSIGNMENT:
            raise ValueError(f"{assignment_code} must contain exactly 12 rows")
        normalized_rows: list[dict[str, Any]] = []
        positions: set[int] = set()
        assignment_item_ids: set[str] = set()
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or set(row) != ROW_KEYS:
                raise ValueError(f"{assignment_code}[{index}] has an invalid field set")
            assignment_id = row["assignment_id"]
            pilot_rater_id = row["pilot_rater_id"]
            position = row["display_position"]
            item_id = row["pilot_item_id"]
            sentence = row["sentence_text"]
            if not isinstance(assignment_id, str) or not ID_RE.fullmatch(assignment_id):
                raise ValueError(f"{assignment_code}[{index}] assignment_id is invalid")
            if assignment_id in assignment_ids:
                raise ValueError(f"duplicate assignment_id: {assignment_id}")
            assignment_ids.add(assignment_id)
            if pilot_rater_id != assignment_code:
                raise ValueError(f"{assignment_code}[{index}] pilot_rater_id mismatch")
            if isinstance(position, bool) or not isinstance(position, int) or not 1 <= position <= 12:
                raise ValueError(f"{assignment_code}[{index}] display_position is invalid")
            if position in positions:
                raise ValueError(f"{assignment_code} contains duplicate display_position {position}")
            positions.add(position)
            if not isinstance(item_id, str) or not ID_RE.fullmatch(item_id):
                raise ValueError(f"{assignment_code}[{index}] pilot_item_id is invalid")
            if item_id in assignment_item_ids:
                raise ValueError(f"{assignment_code} contains duplicate pilot_item_id {item_id}")
            assignment_item_ids.add(item_id)
            if (
                not isinstance(sentence, str)
                or not 1 <= len(sentence) <= 10000
                or "\x00" in sentence
                or PLACEHOLDER_RE.search(sentence)
            ):
                raise ValueError(f"{assignment_code}[{index}] sentence_text is invalid")
            if item_id in item_text and item_text[item_id] != sentence:
                raise ValueError(f"sentence_text differs across assignments for {item_id}")
            item_text[item_id] = sentence
            item_counts[item_id] += 1
            normalized_rows.append({
                "assignment_id": assignment_id,
                "pilot_rater_id": pilot_rater_id,
                "display_position": position,
                "pilot_item_id": item_id,
                "sentence_text": sentence,
            })
        normalized_rows.sort(key=lambda value: value["display_position"])
        if [row["display_position"] for row in normalized_rows] != list(range(1, 13)):
            raise ValueError(f"{assignment_code} positions must be exactly 1 through 12")
        normalized[assignment_code] = normalized_rows

    if len(assignment_ids) != 60 or len(item_text) != 12:
        raise ValueError("instrument must contain 60 unique assignment IDs over exactly 12 unique items")
    if set(item_counts.values()) != {5}:
        raise ValueError("every pilot item must occur once in each of the five assignments")
    return {
        "instrument_sha256": declared_hash,
        "instrument_version": instrument["hosted_version"],
        "source_offline_sha256": instrument["source_offline_instrument_sha256"],
        "items": item_text,
        "assignments": normalized,
    }


def sql_text(value: str) -> str:
    if "\x00" in value:
        raise ValueError("PostgreSQL text cannot contain NUL")
    return "'" + value.replace("'", "''") + "'"


def render_sql(validated: dict[str, Any], source_path: Path) -> str:
    instrument_hash = validated["instrument_sha256"]
    instrument_version = validated["instrument_version"]
    source_hash = validated["source_offline_sha256"]
    item_rows = [
        f"  ({sql_text(instrument_hash)}, {sql_text(item_id)}, {sql_text(sentence)})"
        for item_id, sentence in sorted(validated["items"].items())
    ]
    assignment_set_rows = [
        f"  ({sql_text(instrument_hash)}, {sql_text(code)}, 12)"
        for code in EXPECTED_ASSIGNMENT_CODES
    ]
    assignment_rows: list[str] = []
    for code in EXPECTED_ASSIGNMENT_CODES:
        for row in validated["assignments"][code]:
            assignment_rows.append(
                "  ("
                + ", ".join([
                    sql_text(instrument_hash),
                    sql_text(code),
                    str(row["display_position"]),
                    sql_text(row["pilot_item_id"]),
                    sql_text(row["assignment_id"]),
                ])
                + ")"
            )
    source_label = source_path.as_posix()
    sql = f"""-- Generated by tools/render_instrument_seed.py from {source_label}.
-- Immutable hosted pilot seed: no upsert, delete, identifier, invite, or secret data.
begin;
set local standard_conforming_strings = on;

insert into research.pilot_instruments (
  instrument_sha256,
  instrument_version,
  source_offline_sha256,
  expected_response_count,
  is_active,
  fielding_open,
  activated_at
) values (
  {sql_text(instrument_hash)},
  {sql_text(instrument_version)},
  {sql_text(source_hash)},
  12,
  true,
  false,
  now()
);

insert into research.pilot_items (
  instrument_sha256, pilot_item_id, sentence_text
) values
{",\n".join(item_rows)};

insert into research.pilot_assignment_sets (
  instrument_sha256, assignment_code, expected_item_count
) values
{",\n".join(assignment_set_rows)};

insert into research.pilot_assignments (
  instrument_sha256, assignment_code, display_position, pilot_item_id, assignment_id
) values
{",\n".join(assignment_rows)};

do $pilot_seed_validation$
declare
  invalid_assignment_count integer;
begin
  if (
    select count(*) from research.pilot_items
    where instrument_sha256 = {sql_text(instrument_hash)}
  ) <> 12 then
    raise exception 'pilot seed item count is not 12';
  end if;

  if (
    select count(*) from research.pilot_assignment_sets
    where instrument_sha256 = {sql_text(instrument_hash)}
  ) <> 5 then
    raise exception 'pilot seed assignment-set count is not 5';
  end if;

  if (
    select count(*) from research.pilot_assignments
    where instrument_sha256 = {sql_text(instrument_hash)}
  ) <> 60 then
    raise exception 'pilot seed assignment row count is not 60';
  end if;

  select count(*) into invalid_assignment_count
  from (
    select assignment_code
    from research.pilot_assignments
    where instrument_sha256 = {sql_text(instrument_hash)}
    group by assignment_code
    having count(*) <> 12
       or count(distinct display_position) <> 12
       or count(distinct pilot_item_id) <> 12
       or min(display_position) <> 1
       or max(display_position) <> 12
  ) as invalid_assignments;
  if invalid_assignment_count <> 0 then
    raise exception 'pilot seed contains an invalid 12-item assignment';
  end if;

  if exists (
    select 1 from research.pilot_instruments
    where instrument_sha256 = {sql_text(instrument_hash)}
      and (instrument_version <> {sql_text(instrument_version)}
        or source_offline_sha256 <> {sql_text(source_hash)}
        or expected_response_count <> 12
        or not is_active
        or fielding_open)
  ) then
    raise exception 'pilot seed instrument state is invalid';
  end if;
end;
$pilot_seed_validation$;

commit;
"""
    marker = PLACEHOLDER_RE.search(sql)
    if marker:
        raise ValueError(f"rendered SQL contains forbidden placeholder marker: {marker.group(0)}")
    return sql


def ensure_paths(input_path: Path, output_path: Path, repository_root: Path) -> tuple[Path, Path]:
    expected_input = (repository_root / "docs" / "instrument.json").resolve()
    resolved_input = input_path.resolve()
    if resolved_input != expected_input or not resolved_input.is_file():
        raise ValueError(f"--input must be the repository instrument: {expected_input}")
    migrations_root = (repository_root / "supabase" / "migrations").resolve()
    resolved_output = output_path.resolve()
    try:
        relative_output = resolved_output.relative_to(migrations_root)
    except ValueError as exc:
        raise ValueError(f"--output must be inside {migrations_root}") from exc
    if len(relative_output.parts) != 1 or resolved_output.suffix.lower() != ".sql":
        raise ValueError("--output must name one .sql file directly below supabase/migrations")
    if os.path.lexists(resolved_output):
        raise FileExistsError(f"refusing to overwrite existing output: {resolved_output}")
    return resolved_input, resolved_output


def render_to_path(input_path: Path, output_path: Path, repository_root: Path) -> dict[str, int | str]:
    source, target = ensure_paths(input_path, output_path, repository_root)
    validated = validate_instrument(load_json(source))
    sql = render_sql(validated, Path("docs/instrument.json"))
    with target.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(sql)
    return {
        "instrument_sha256": validated["instrument_sha256"],
        "instruments": 1,
        "items": len(validated["items"]),
        "assignment_sets": len(validated["assignments"]),
        "assignments": sum(len(rows) for rows in validated["assignments"].values()),
    }


def parser() -> argparse.ArgumentParser:
    repository_root = Path(__file__).resolve().parents[1]
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--input", type=Path, default=repository_root / "docs" / "instrument.json")
    result.add_argument("--output", type=Path, required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    repository_root = Path(__file__).resolve().parents[1]
    try:
        counts = render_to_path(args.input, args.output, repository_root)
    except (ValueError, FileExistsError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(
        "Rendered immutable seed "
        f"instrument={counts['instrument_sha256']} "
        f"rows=instrument:{counts['instruments']},items:{counts['items']},"
        f"assignment_sets:{counts['assignment_sets']},assignments:{counts['assignments']} "
        f"output={args.output.resolve()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
