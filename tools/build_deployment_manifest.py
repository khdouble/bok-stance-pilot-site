#!/usr/bin/env python3
"""Freeze mutable legal and operational files outside the instrument hash."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from build_public_instrument import (
    EXPECTED_RELEASE_SOURCE_KEYS,
    HOSTED_VERSION,
    RELEASE_SOURCE_PATHS,
)
from pi_config import (
    boolean_value,
    has_placeholder,
    parse_pi_values,
    quoted_value,
    validate_live_config,
)
from render_instrument_transition import (
    CURRENT_INSTRUMENT_RELATIVE,
    PREVIOUS_INSTRUMENT_RELATIVE,
    PREVIOUS_INSTRUMENT_SHA256,
    TRANSITION_MIGRATION_RELATIVE,
    load_json,
    render_sql as render_transition_sql,
    validate_transition,
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
DEPLOYMENT_SOURCE_FILES = {
    "database_instrument_transition": ROOT / TRANSITION_MIGRATION_RELATIVE,
}


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def load_bound_instrument(path: Path, repository_root: Path) -> dict[str, object]:
    if set(RELEASE_SOURCE_PATHS) != EXPECTED_RELEASE_SOURCE_KEYS:
        raise ValueError("release source map must contain the exact 11 frozen keys")
    instrument = load_json(path)
    declared_hash = instrument.get("instrument_sha256")
    basis = dict(instrument)
    basis.pop("instrument_sha256", None)
    if (
        not isinstance(declared_hash, str)
        or len(declared_hash) != 64
        or any(character not in "0123456789abcdef" for character in declared_hash)
        or declared_hash != sha256_bytes(canonical_json(basis))
    ):
        raise ValueError("instrument self-digest is invalid")
    if instrument.get("hosted_version") != HOSTED_VERSION:
        raise ValueError("instrument hosted version is invalid")
    release_hashes = instrument.get("release_source_hashes")
    expected_paths = {
        name: repository_root / relative
        for name, relative in RELEASE_SOURCE_PATHS.items()
    }
    if not isinstance(release_hashes, dict) or set(release_hashes) != set(expected_paths):
        raise ValueError("instrument release-source key set is invalid")
    try:
        expected_hashes = {
            name: sha256_bytes(source.read_bytes())
            for name, source in expected_paths.items()
        }
    except OSError as exc:
        raise ValueError("a required release source is unavailable") from exc
    if release_hashes != expected_hashes:
        raise ValueError("instrument release-source hashes are stale")
    transition_path = (repository_root / TRANSITION_MIGRATION_RELATIVE).resolve()
    if any(source.resolve() == transition_path for source in expected_paths.values()):
        raise ValueError("instrument release sources include the transition migration")
    return instrument


def build(state: str, output: Path) -> dict[str, object]:
    if state not in {"staging", "live"}:
        raise ValueError("deployment state is invalid")
    expected_output = DOCS / "deployment-manifest.json"
    if output.resolve() != expected_output.resolve():
        raise ValueError(f"output must be {expected_output}")
    instrument = load_bound_instrument(DOCS / "instrument.json", ROOT)
    instrument_hash = instrument.get("instrument_sha256", "")
    if set(DEPLOYMENT_SOURCE_FILES) != {"database_instrument_transition"}:
        raise ValueError("deployment source map is invalid")
    try:
        deployment_source_bytes = {
            name: path.read_bytes()
            for name, path in DEPLOYMENT_SOURCE_FILES.items()
        }
    except OSError as exc:
        raise ValueError("the instrument transition migration is unavailable") from exc
    transition_bytes = deployment_source_bytes["database_instrument_transition"]
    try:
        transition_text = transition_bytes.decode("utf-8", errors="strict")
        previous = load_json(ROOT / PREVIOUS_INSTRUMENT_RELATIVE)
        transition_contract = validate_transition(previous, instrument, ROOT)
        expected_transition = render_transition_sql(
            transition_contract,
            PREVIOUS_INSTRUMENT_RELATIVE,
            CURRENT_INSTRUMENT_RELATIVE,
        )
    except (UnicodeError, ValueError) as exc:
        raise ValueError("the instrument transition migration is invalid") from exc
    if (
        transition_text != expected_transition
        or instrument_hash not in transition_text
        or PREVIOUS_INSTRUMENT_SHA256 not in transition_text
        or instrument.get("hosted_version") not in transition_text
    ):
        raise ValueError("the instrument transition migration does not match the instrument")

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
    config_version = quoted_value(config, "hostedVersion")
    config_source_hash = quoted_value(config, "sourceOfflineInstrumentSha256")
    if (api_url, origin, base_path) != (
        EXPECTED_API_URL,
        EXPECTED_ORIGIN,
        EXPECTED_BASE_PATH,
    ):
        raise ValueError("site endpoint or GitHub Pages location changed")
    if (
        config_version != instrument.get("hosted_version")
        or config_source_hash != instrument.get("source_offline_instrument_sha256")
    ):
        raise ValueError("site config identity does not match the instrument")

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
        "deployment_source_hashes": {
            name: sha256_bytes(content)
            for name, content in sorted(deployment_source_bytes.items())
        },
    }
    result = {
        "deployment_manifest_sha256": sha256_bytes(canonical_json(payload)),
        **payload,
    }
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
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
