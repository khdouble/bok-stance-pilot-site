#!/usr/bin/env python3
"""Freeze mutable legal and operational files outside the instrument hash."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from pi_config import (
    boolean_value,
    has_placeholder,
    parse_pi_values,
    quoted_value,
    validate_live_config,
)


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
EXPECTED_ORIGIN = "https://khdouble.github.io"
EXPECTED_BASE_PATH = "/bok-stance-pilot-site/"
EXPECTED_API_URL = (
    "https://mebisrsvasrzwkmsodsw.supabase.co/functions/v1/pilot-api"
)
OPERATIONAL_FILES = {
    "privacy_notice": DOCS / "privacy.html",
    "site_config": DOCS / "site-config.js",
}


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def build(state: str, output: Path) -> dict[str, object]:
    expected_output = DOCS / "deployment-manifest.json"
    if output.resolve() != expected_output.resolve():
        raise ValueError(f"output must be {expected_output}")
    instrument = json.loads(
        (DOCS / "instrument.json").read_text(encoding="utf-8")
    )
    instrument_hash = instrument.get("instrument_sha256", "")
    if (
        not isinstance(instrument_hash, str)
        or len(instrument_hash) != 64
        or any(character not in "0123456789abcdef" for character in instrument_hash)
    ):
        raise ValueError("instrument hash is invalid")

    config = OPERATIONAL_FILES["site_config"].read_text(encoding="utf-8")
    privacy = OPERATIONAL_FILES["privacy_notice"].read_text(encoding="utf-8")
    enabled = boolean_value(config, "fieldingEnabled")
    parse_pi_values(config)
    pending = has_placeholder(config, privacy)
    pi_errors = validate_live_config(config, privacy)
    if state == "staging":
        if enabled:
            raise ValueError("staging requires fieldingEnabled=false")
        if not pending and pi_errors:
            raise ValueError(
                "staging PI configuration is neither pending nor valid: "
                + "; ".join(pi_errors)
            )
    if state == "live" and (not enabled or pending or pi_errors):
        detail = "; ".join(pi_errors) if pi_errors else "placeholder remains"
        raise ValueError(
            "live requires fieldingEnabled=true and strictly valid PI values: "
            + detail
        )

    api_url = quoted_value(config, "apiUrl")
    origin = quoted_value(config, "githubPagesOrigin")
    base_path = quoted_value(config, "basePath")
    if (api_url, origin, base_path) != (
        EXPECTED_API_URL,
        EXPECTED_ORIGIN,
        EXPECTED_BASE_PATH,
    ):
        raise ValueError("site endpoint or GitHub Pages location changed")

    payload: dict[str, object] = {
        "schema_version": "1.0",
        "deployment_state": state,
        "site_url": origin + base_path,
        "api_url": api_url,
        "hosted_version": instrument.get("hosted_version", ""),
        "instrument_sha256": instrument_hash,
        "operational_file_hashes": {
            name: sha256_bytes(path.read_bytes())
            for name, path in sorted(OPERATIONAL_FILES.items())
        },
    }
    result = {
        "deployment_manifest_sha256": sha256_bytes(canonical_json(payload)),
        **payload,
    }
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", choices=("staging", "live"), required=True)
    parser.add_argument(
        "--output", type=Path, default=DOCS / "deployment-manifest.json"
    )
    args = parser.parse_args()
    result = build(args.state, args.output)
    print(
        "Built deployment manifest "
        f"state={result['deployment_state']} "
        f"sha256={result['deployment_manifest_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
