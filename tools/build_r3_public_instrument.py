#!/usr/bin/env python3
"""Build the R4 public instrument from its locked, key-free source file."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = REPO_ROOT / "instrument_sources" / "r4_20260908.json"
OUTPUT_PATH = REPO_ROOT / "docs" / "instrument.json"
HASH_JS_PATH = REPO_ROOT / "docs" / "instrument-hash.js"
ITEM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
EXPECTED_ASSIGNMENTS = tuple(f"PILOT_R{number:02d}" for number in range(1, 6))
RELEASE_SOURCE_PATHS = {
    "r4_source": Path("instrument_sources/r4_20260908.json"),
    "public_admin": Path("docs/admin.html"),
    "public_admin_bridge": Path("docs/admin.js"),
    "public_app": Path("docs/app.js"),
    "public_contract": Path("docs/submission-contract.js"),
    "public_index": Path("docs/index.html"),
    "public_privacy": Path("docs/privacy.html"),
    "public_styles": Path("docs/styles.css"),
    "supabase_config": Path("supabase/config.toml"),
    "edge_function": Path("supabase/functions/pilot-api/index.ts"),
    "edge_contract": Path("supabase/functions/pilot-api/_shared/core.ts"),
    "database_schema": Path("supabase/migrations/202609030001_pilot_backend.sql"),
    "database_pi_manual_test": Path("supabase/migrations/202609030004_pi_manual_test_credentials.sql"),
    "database_r3_access": Path("supabase/migrations/202609080006_r3_access_contract.sql"),
}


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def require_text(value: Any, field: str, minimum: int = 1, maximum: int = 10000) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum or "\x00" in value:
        raise ValueError(f"{field} must be nonempty UTF-8 text within bounds")
    return value


def load_source(path: Path) -> dict[str, Any]:
    source = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(source, dict):
        raise ValueError("R4 source must be an object")
    required = {
        "schema_version", "hosted_version", "source_offline_instrument_sha256",
        "dataset_role", "excluded_from_analysis", "analysis_exclusion_reason",
        "source_provenance", "tutorial", "core_items", "assignment_orders",
    }
    if set(source) != required:
        raise ValueError("R4 source field set changed")
    if source["schema_version"] != "1.0":
        raise ValueError("R4 source schema version changed")
    if not re.fullmatch(r"v\d{6}-r4-public-\d+", str(source["hosted_version"])):
        raise ValueError("R4 hosted version has an invalid format")
    if not re.fullmatch(r"[0-9a-f]{64}", str(source["source_offline_instrument_sha256"])):
        raise ValueError("R4 source hash must be a SHA-256 digest")
    if (
        source["dataset_role"] != "r3_content_response_pilot"
        or source["excluded_from_analysis"] is not True
        or source["analysis_exclusion_reason"] != "r3_repilot_never_analysis"
    ):
        raise ValueError("R4 pilot-only analysis boundary changed")
    provenance = source["source_provenance"]
    if not isinstance(provenance, dict) or set(provenance) != {
        "canonical_corpus", "canonical_corpus_sha256", "selection_memo", "selection_rule"
    }:
        raise ValueError("R4 provenance field set changed")
    if provenance["canonical_corpus_sha256"] != source["source_offline_instrument_sha256"]:
        raise ValueError("R3 canonical corpus hash is not locked consistently")

    core_rows = source["core_items"]
    if not isinstance(core_rows, list) or len(core_rows) != 12:
        raise ValueError("R4 must contain exactly 12 core items")
    core: dict[str, dict[str, str]] = {}
    for index, row in enumerate(core_rows):
        if not isinstance(row, dict) or set(row) != {"pilot_item_id", "statement_date", "governor", "sentence_text"}:
            raise ValueError(f"core_items[{index}] field set changed")
        item_id = require_text(row["pilot_item_id"], f"core_items[{index}].pilot_item_id", 1, 64)
        if not ITEM_ID_RE.fullmatch(item_id) or item_id in core:
            raise ValueError(f"core_items[{index}] pilot item ID is invalid")
        core[item_id] = {
            "pilot_item_id": item_id,
            "statement_date": require_text(row["statement_date"], f"core_items[{index}].statement_date", 10, 10),
            "governor": require_text(row["governor"], f"core_items[{index}].governor", 2, 20),
            "sentence_text": require_text(row["sentence_text"], f"core_items[{index}].sentence_text"),
        }

    tutorial = source["tutorial"]
    if not isinstance(tutorial, dict) or set(tutorial) != {"title", "instructions", "items"}:
        raise ValueError("R3 tutorial field set changed")
    tutorial_items = tutorial["items"]
    if not isinstance(tutorial_items, list) or len(tutorial_items) != 6:
        raise ValueError("R4 must contain exactly six unscored tutorial items")
    tutorial_ids: set[str] = set()
    normalized_tutorial: list[dict[str, Any]] = []
    for index, row in enumerate(tutorial_items):
        if not isinstance(row, dict) or set(row) != {
            "tutorial_item_id", "sentence_text", "intended_choice", "intended_reason_code", "explanation"
        }:
            raise ValueError(f"tutorial.items[{index}] field set changed")
        tutorial_id = require_text(row["tutorial_item_id"], f"tutorial.items[{index}].tutorial_item_id", 1, 64)
        if not ITEM_ID_RE.fullmatch(tutorial_id) or tutorial_id in tutorial_ids:
            raise ValueError(f"tutorial.items[{index}] identifier is invalid")
        tutorial_ids.add(tutorial_id)
        if row["intended_choice"] not in (-2, -1, 0, 1, 2, 99):
            raise ValueError(f"tutorial.items[{index}] intended choice is invalid")
        normalized_tutorial.append({
            "tutorial_item_id": tutorial_id,
            "sentence_text": require_text(row["sentence_text"], f"tutorial.items[{index}].sentence_text"),
            "intended_choice": row["intended_choice"],
            "intended_reason_code": require_text(row["intended_reason_code"], f"tutorial.items[{index}].intended_reason_code", 1, 32),
            "explanation": require_text(row["explanation"], f"tutorial.items[{index}].explanation"),
        })

    orders = source["assignment_orders"]
    if not isinstance(orders, dict) or tuple(sorted(orders)) != EXPECTED_ASSIGNMENTS:
        raise ValueError("R3 assignment code set changed")
    assignments: dict[str, list[dict[str, Any]]] = {}
    for assignment_code in EXPECTED_ASSIGNMENTS:
        order = orders[assignment_code]
        if not isinstance(order, list) or len(order) != 12 or set(order) != set(core):
            raise ValueError(f"{assignment_code} must order each R3 core item exactly once")
        assignments[assignment_code] = [
            {
                "assignment_id": f"H4_{assignment_code}_{position:02d}",
                "pilot_rater_id": assignment_code,
                "display_position": position,
                "pilot_item_id": item_id,
                "sentence_text": core[item_id]["sentence_text"],
            }
            for position, item_id in enumerate(order, start=1)
        ]
    return {
        "source": source,
        "tutorial": {
            "title": require_text(tutorial["title"], "tutorial.title"),
            "instructions": require_text(tutorial["instructions"], "tutorial.instructions"),
            "items": normalized_tutorial,
        },
        "assignments": assignments,
    }


def build(source_path: Path, output_path: Path) -> dict[str, Any]:
    locked = load_source(source_path)
    release_paths = {name: REPO_ROOT / relative for name, relative in RELEASE_SOURCE_PATHS.items()}
    missing = [str(path) for path in release_paths.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError("missing release source: " + ", ".join(missing))
    source = locked["source"]
    payload = {
        "schema_version": "1.0",
        "hosted_version": source["hosted_version"],
        "source_offline_instrument_sha256": source["source_offline_instrument_sha256"],
        "dataset_role": source["dataset_role"],
        "excluded_from_analysis": source["excluded_from_analysis"],
        "analysis_exclusion_reason": source["analysis_exclusion_reason"],
        "source_provenance": source["source_provenance"],
        "tutorial": locked["tutorial"],
        "items_per_participant": 12,
        "stance_choices": [
            {"value": -2, "label": "-2 명시적 완화 조치"},
            {"value": -1, "label": "-1 완화 기조·편향"},
            {"value": 0, "label": "0 중립·상쇄·균형"},
            {"value": 1, "label": "+1 긴축 기조·편향"},
            {"value": 2, "label": "+2 명시적 긴축 조치"},
            {"value": 99, "label": "99 판단불가·정보부족"},
        ],
        "reason_codes": ["NONE", "IRRELEVANT", "CONTEXT_NEEDED", "MIXED_UNRESOLVED", "TERMINOLOGY", "OTHER"],
        "item_quality_codes": ["NONE", "TOO_OBVIOUS", "UNNATURAL_OR_IMPOSSIBLE", "POLICY_INSTRUMENT_AMBIGUITY", "CONTEXT_REFERENCE_AMBIGUITY", "UI_PROBLEM", "OTHER"],
        "confidence_values": [1, 2, 3, 4, 5],
        "assignments": locked["assignments"],
        "source_hashes": {"r4_source": sha256_file(source_path)},
        "release_source_hashes": {name: sha256_file(path) for name, path in sorted(release_paths.items())},
    }
    result = {"instrument_sha256": sha256_bytes(canonical_json(payload)), **payload}
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    HASH_JS_PATH.write_text("window.PILOT_INSTRUMENT_SHA256 = " + json.dumps(result["instrument_sha256"]) + ";\n", encoding="utf-8", newline="\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE_PATH)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    args = parser.parse_args()
    result = build(args.source.resolve(), args.output.resolve())
    print(f"Built {result['hosted_version']} instrument={result['instrument_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
