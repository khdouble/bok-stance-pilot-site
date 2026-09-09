#!/usr/bin/env python3
"""Build a staging or live deployment manifest for the locked R5 release."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from build_r5_public_instrument import RELEASE_SOURCE_PATHS


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
OUTPUT = DOCS / "deployment-manifest.json"
TRANSITION = ROOT / "supabase" / "migrations" / "202609090012_activate_r5_h7.sql"
EXPECTED_ORIGIN = "https://khdouble.github.io"
EXPECTED_PATH = "/bok-stance-pilot-site/"
EXPECTED_API = "https://mebisrsvasrzwkmsodsw.supabase.co/functions/v1/pilot-api"


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def quoted(config: str, key: str) -> str:
    match = re.search(rf"^\s*{re.escape(key)}:\s*\"([^\"]+)\"", config, re.MULTILINE)
    if not match:
        raise ValueError(f"missing quoted site config: {key}")
    return match.group(1)


def boolean(config: str, key: str) -> bool:
    match = re.search(rf"^\s*{re.escape(key)}:\s*(true|false)", config, re.MULTILINE)
    if not match:
        raise ValueError(f"missing boolean site config: {key}")
    return match.group(1) == "true"


def load_instrument() -> dict[str, object]:
    instrument = json.loads((DOCS / "instrument.json").read_text(encoding="utf-8"))
    declared = instrument.pop("instrument_sha256", "")
    if not isinstance(declared, str) or declared != digest(canonical_json(instrument)):
        raise ValueError("instrument self digest is invalid")
    if instrument.get("hosted_version") != "v260909-r5-public-2":
        raise ValueError("instrument is not the expected R5 version")
    if instrument.get("dataset_role") != "r3_content_response_pilot" or instrument.get("excluded_from_analysis") is not True:
        raise ValueError("R5 pilot-only analysis boundary changed")
    hashes = instrument.get("release_source_hashes")
    expected = {name: digest((ROOT / path).read_bytes()) for name, path in RELEASE_SOURCE_PATHS.items()}
    if hashes != expected:
        raise ValueError("instrument release source hashes are stale")
    instrument["instrument_sha256"] = declared
    return instrument


def build(state: str) -> dict[str, object]:
    instrument = load_instrument()
    config = (DOCS / "site-config.js").read_text(encoding="utf-8")
    if (quoted(config, "apiUrl"), quoted(config, "githubPagesOrigin"), quoted(config, "basePath")) != (EXPECTED_API, EXPECTED_ORIGIN, EXPECTED_PATH):
        raise ValueError("site endpoint or Pages location changed")
    if quoted(config, "hostedVersion") != instrument["hosted_version"] or quoted(config, "sourceOfflineInstrumentSha256") != instrument["source_offline_instrument_sha256"]:
        raise ValueError("site config and R5 instrument differ")
    enabled = boolean(config, "fieldingEnabled")
    direct = boolean(config, "directEntryEnabled")
    if state == "staging" and (enabled or direct):
        raise ValueError("R5 staging requires fieldingEnabled=false and directEntryEnabled=false")
    if state == "live" and (not enabled or not direct):
        raise ValueError("R5 live requires fieldingEnabled=true and directEntryEnabled=true")
    migration = TRANSITION.read_text(encoding="utf-8")
    if instrument["instrument_sha256"] not in migration or "e7521402145052c53cd2f4d7c95d4259080526f3a413a40c45f5f4b988cf6403" not in migration:
        raise ValueError("R5 transition migration is not bound to H6 and H7")
    payload = {
        "schema_version": "1.0",
        "deployment_state": state,
        "site_url": EXPECTED_ORIGIN + EXPECTED_PATH,
        "api_url": EXPECTED_API,
        "hosted_version": instrument["hosted_version"],
        "instrument_sha256": instrument["instrument_sha256"],
        "operational_file_hashes": {
            "privacy_notice": digest((DOCS / "privacy.html").read_bytes()),
            "site_config": digest((DOCS / "site-config.js").read_bytes()),
        },
        "deployment_source_hashes": {
            "database_instrument_transition": digest(TRANSITION.read_bytes()),
        },
    }
    return {"deployment_manifest_sha256": digest(canonical_json(payload)), **payload}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", choices=("staging", "live"), required=True)
    args = parser.parse_args()
    result = build(args.state)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"Built R5 deployment manifest state={args.state} sha256={result['deployment_manifest_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
