#!/usr/bin/env python3
"""Build the public, key-free hosted pilot instrument from the frozen source."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path


HOSTED_VERSION = "v260903-pilot-hosted-1"
SOURCE_INSTRUMENT_SHA256 = (
    "b594a196eb7be720e57d974f4b5c6e4437b697e6ae20f01013e830af35707a51"
)
PAYLOAD_COLUMNS = [
    "assignment_id",
    "pilot_rater_id",
    "display_position",
    "pilot_item_id",
    "sentence_text",
]
REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_SOURCE_PATHS = {
    "public_app": Path("docs/app.js"),
    "public_contract": Path("docs/submission-contract.js"),
    "public_index": Path("docs/index.html"),
    "public_styles": Path("docs/styles.css"),
    "supabase_config": Path("supabase/config.toml"),
    "edge_function": Path("supabase/functions/pilot-api/index.ts"),
    "edge_contract": Path("supabase/functions/pilot-api/_shared/core.ts"),
    "database_schema": Path(
        "supabase/migrations/202609030001_pilot_backend.sql"
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


def read_rows(path: Path) -> list[dict[str, object]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [column for column in PAYLOAD_COLUMNS if column not in reader.fieldnames]
        if missing:
            raise ValueError(f"response template is missing columns: {missing}")
        rows: list[dict[str, object]] = []
        for raw in reader:
            rows.append(
                {
                    "assignment_id": raw["assignment_id"],
                    "pilot_rater_id": raw["pilot_rater_id"],
                    "display_position": int(raw["display_position"]),
                    "pilot_item_id": raw["pilot_item_id"],
                    "sentence_text": raw["sentence_text"],
                }
            )
    return rows


def build(source_pilot: Path, output: Path) -> dict[str, object]:
    response_template = source_pilot / "response_template.csv"
    parent_manifest_path = source_pilot / "fieldwork" / "manifest.json"
    config_path = source_pilot / "pilot_fieldwork_config.json"
    participant_items_path = source_pilot / "participant_items.csv"
    pilot_protocol_path = source_pilot / "pilot_protocol.md"
    for path in (
        response_template,
        parent_manifest_path,
        config_path,
        participant_items_path,
        pilot_protocol_path,
    ):
        if not path.is_file():
            raise FileNotFoundError(path)
    release_paths = {
        name: REPO_ROOT / relative
        for name, relative in RELEASE_SOURCE_PATHS.items()
    }
    for path in release_paths.values():
        if not path.is_file():
            raise FileNotFoundError(path)

    parent_manifest = json.loads(parent_manifest_path.read_text(encoding="utf-8"))
    if parent_manifest["instrument_sha256"] != SOURCE_INSTRUMENT_SHA256:
        raise ValueError("frozen offline instrument hash differs")

    rows = read_rows(response_template)
    grouped: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        grouped.setdefault(str(row["pilot_rater_id"]), []).append(row)
    expected_raters = [f"PILOT_R{index:02d}" for index in range(1, 6)]
    if sorted(grouped) != expected_raters:
        raise ValueError(f"unexpected rater IDs: {sorted(grouped)}")
    for rater_id, assigned in grouped.items():
        assigned.sort(key=lambda row: int(row["display_position"]))
        if [row["display_position"] for row in assigned] != list(range(1, 13)):
            raise ValueError(f"{rater_id} does not have positions 1..12")
        if len({row["pilot_item_id"] for row in assigned}) != 12:
            raise ValueError(f"{rater_id} item IDs are not unique")

    payload: dict[str, object] = {
        "schema_version": "1.0",
        "hosted_version": HOSTED_VERSION,
        "source_offline_instrument_sha256": SOURCE_INSTRUMENT_SHA256,
        "dataset_role": "synthetic_usability_pilot",
        "excluded_from_analysis": True,
        "analysis_exclusion_reason": "synthetic_usability_only_never_analysis",
        "items_per_participant": 12,
        "stance_choices": [
            {"value": -2, "label": "-2 명시적 완화 조치"},
            {"value": -1, "label": "-1 완화 기조·편향"},
            {"value": 0, "label": "0 중립·상쇄·균형"},
            {"value": 1, "label": "+1 긴축 기조·편향"},
            {"value": 2, "label": "+2 명시적 긴축 조치"},
            {"value": 99, "label": "99 판단불가·정보부족"},
        ],
        "reason_codes": [
            "NONE",
            "IRRELEVANT",
            "CONTEXT_NEEDED",
            "MIXED_UNRESOLVED",
            "TERMINOLOGY",
            "OTHER",
        ],
        "confidence_values": [1, 2, 3, 4, 5],
        "assignments": grouped,
        "source_hashes": {
            "response_template": sha256_file(response_template),
            "participant_items": sha256_file(participant_items_path),
            "pilot_fieldwork_config": sha256_file(config_path),
            "pilot_protocol": sha256_file(pilot_protocol_path),
            "parent_manifest": sha256_file(parent_manifest_path),
        },
        "release_source_hashes": {
            name: sha256_file(path)
            for name, path in sorted(release_paths.items())
        },
    }
    instrument_sha256 = sha256_bytes(canonical_json(payload))
    result = {"instrument_sha256": instrument_sha256, **payload}

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    hash_js = output.with_name("instrument-hash.js")
    hash_js.write_text(
        "window.PILOT_INSTRUMENT_SHA256 = "
        + json.dumps(instrument_sha256)
        + ";\n",
        encoding="utf-8",
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-pilot", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "docs" / "instrument.json",
    )
    args = parser.parse_args()
    result = build(args.source_pilot.resolve(), args.output.resolve())
    print(
        f"Built {result['hosted_version']} "
        f"instrument={result['instrument_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
