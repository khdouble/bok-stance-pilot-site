#!/usr/bin/env python3
"""Run one fail-closed disposable E2E against the deployed pilot site.

The published site remains on HOLD.  Inside an ephemeral headless browser only,
the exact site-config.js request is fulfilled with a two-field staging override.
Raw invitation and identity values are read only from protected files outside
the repository and are never printed or written to the public receipt.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote, urljoin, urlsplit


SITE_URL = "https://khdouble.github.io/bok-stance-pilot-site/"
CONFIG_URL = urljoin(SITE_URL, "site-config.js")
API_URL = "https://mebisrsvasrzwkmsodsw.supabase.co/functions/v1/pilot-api"
REMOTE_FILES = (
    "index.html",
    "admin.html",
    "admin.js",
    "app.js",
    "submission-contract.js",
    "styles.css",
    "instrument.json",
    "instrument-hash.js",
    "privacy.html",
    "site-config.js",
    "deployment-manifest.json",
)
BROWSER_ASSETS = (
    # The participant browser flow loads only these assets.  The separate
    # REMOTE_FILES byte attestation covers the admin page and bridge.
    SITE_URL,
    urljoin(SITE_URL, "app.js"),
    urljoin(SITE_URL, "submission-contract.js"),
    urljoin(SITE_URL, "styles.css"),
    urljoin(SITE_URL, "instrument.json"),
    urljoin(SITE_URL, "instrument-hash.js"),
    CONFIG_URL,
)
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
ASSIGNMENT_RE = re.compile(r"^PILOT_R0[1-5]$")
UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
IDENTITY_PURPOSE = "disposable_remote_e2e_test_only"
DEFAULT_INVITE_DIRECTORY = "remote-e2e-invite"
DEFAULT_IDENTITY_FILE = "remote-e2e-identity.private.json"
DEFAULT_RECEIPT_FILE = "remote-e2e-receipt.json"
PENDING_STATUS = "E2E_PASSED_CLEANUP_PENDING"
PENDING_EXIT_CODE = 3
ALLOWED_NETWORK_URLS = frozenset((*BROWSER_ASSETS, API_URL))


class E2EError(RuntimeError):
    """An operator-safe failure message that contains no private values."""


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # Never echo an accidental secret arg.
        self.exit(2, "ERROR: invalid command-line arguments; use --help\n")


def parser() -> argparse.ArgumentParser:
    result = SafeArgumentParser(description=__doc__)
    result.add_argument(
        "--db-backend",
        choices=("direct", "linked-cli"),
        default="direct",
    )
    result.add_argument(
        "--confirm-open-e2e",
        help="exact OPEN E2E FIELDING confirmation for linked-cli",
    )
    result.add_argument(
        "--private-root",
        type=Path,
        help="External protected root; defaults to BOK_PILOT_PRIVATE_DIR/OS app-data",
    )
    result.add_argument(
        "--invite-file",
        type=Path,
        help="provision_invites.py disposable-e2e JSON below the private root",
    )
    result.add_argument(
        "--identity-file",
        type=Path,
        help="test-only identity JSON below the private root",
    )
    result.add_argument(
        "--receipt",
        type=Path,
        help="new non-PII receipt JSON below the private root",
    )
    result.add_argument(
        "--browser", choices=("auto", "chrome", "edge"), default="auto"
    )
    result.add_argument("--timeout-seconds", type=int, default=30)
    return result


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def asset_attestations(hashes: Mapping[str, str]) -> list[dict[str, str]]:
    return [
        {"asset": name, "sha256": hashes[name]}
        for name in sorted(hashes)
    ]


def _parse_utc(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise E2EError(f"{label} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise E2EError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise E2EError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _utc_seconds(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _parse_json_bytes(encoded: bytes, label: str) -> dict[str, Any]:
    try:
        loaded = json.loads(encoded.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise E2EError(f"{label} is not readable valid UTF-8 JSON") from exc
    if not isinstance(loaded, dict):
        raise E2EError(f"{label} must contain one JSON object")
    return loaded


def _load_json_bytes(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        encoded = path.read_bytes()
    except OSError as exc:
        raise E2EError(f"{label} is not readable valid UTF-8 JSON") from exc
    return _parse_json_bytes(encoded, label), encoded


def _load_json(path: Path, label: str) -> dict[str, Any]:
    loaded, _encoded = _load_json_bytes(path, label)
    return loaded


def _canonical_token(value: object) -> str:
    if not isinstance(value, str) or not TOKEN_RE.fullmatch(value):
        raise E2EError("disposable invite token has an invalid shape")
    try:
        decoded = base64.urlsafe_b64decode(value + "=")
    except ValueError as exc:
        raise E2EError("disposable invite token is not canonical base64url") from exc
    if len(decoded) != 32 or base64.urlsafe_b64encode(decoded).decode().rstrip("=") != value:
        raise E2EError("disposable invite token is not canonical base64url")
    return value


def validate_disposable_invite_batch(
    batch: Mapping[str, Any],
    *,
    at: datetime,
) -> dict[str, str]:
    """Validate one private provision batch without returning its raw container."""
    if set(batch) != {
        "schema_version",
        "provisioning_mode",
        "created_at",
        "instrument_sha256",
        "warning",
        "invites",
    }:
        raise E2EError("invite file does not match provision_invites.py schema")
    if (
        batch.get("schema_version") != "1.0"
        or batch.get("provisioning_mode") != "disposable-e2e"
    ):
        raise E2EError("invite file must be disposable-e2e schema 1.0 output")
    digest = batch.get("instrument_sha256")
    if not isinstance(digest, str) or not HASH_RE.fullmatch(digest):
        raise E2EError("invite file instrument digest is invalid")
    invites = batch.get("invites")
    if (
        not isinstance(invites, list)
        or len(invites) != 1
        or not isinstance(invites[0], dict)
    ):
        raise E2EError("disposable-e2e invite file must contain exactly one invite")
    invite_raw = invites[0]
    if set(invite_raw) != {
        "invite_id",
        "assignment_code",
        "invite_token",
        "invite_url",
        "expires_at",
    }:
        raise E2EError("disposable invite entry has an unexpected schema")
    try:
        invite_id = str(uuid.UUID(str(invite_raw.get("invite_id"))))
    except (ValueError, AttributeError) as exc:
        raise E2EError("disposable invite ID must be a canonical UUID") from exc
    if invite_id != invite_raw.get("invite_id"):
        raise E2EError("disposable invite ID must be a canonical UUID")
    assignment = invite_raw.get("assignment_code")
    if not isinstance(assignment, str) or not ASSIGNMENT_RE.fullmatch(assignment):
        raise E2EError("disposable invite assignment is invalid")
    token = _canonical_token(invite_raw.get("invite_token"))
    if invite_raw.get("invite_url") != SITE_URL + "#invite=" + quote(token, safe=""):
        raise E2EError("disposable invite URL is not the canonical hosted URL")
    check_time = at.astimezone(timezone.utc)
    created = _parse_utc(batch.get("created_at"), "invite created_at")
    expires = _parse_utc(invite_raw.get("expires_at"), "invite expires_at")
    if created > check_time + timedelta(minutes=5):
        raise E2EError("disposable invite creation time is in the future")
    if (
        expires <= check_time
        or expires <= created
        or expires - created > timedelta(hours=24, minutes=1)
    ):
        raise E2EError("disposable invite is expired or exceeds the 24-hour lifetime")
    return {
        "invite_id": invite_id,
        "assignment_code": assignment,
        "invite_token": token,
        "instrument_sha256": digest,
    }


def load_private_inputs(
    repository_root: Path,
    private_root_arg: Path | None,
    invite_arg: Path | None,
    identity_arg: Path | None,
    receipt_arg: Path | None,
    *,
    now: datetime | None = None,
) -> tuple[dict[str, str], dict[str, str], Path, Path]:
    """Load provision output and test identity through the external boundary."""
    invite, _invite_path, private_root = load_private_invite(
        repository_root,
        private_root_arg,
        invite_arg,
        now=now,
    )
    identity, receipt_path = load_private_identity_and_receipt(
        repository_root,
        private_root,
        identity_arg,
        receipt_arg,
    )
    return invite, identity, receipt_path, private_root


def load_private_invite(
    repository_root: Path,
    private_root_arg: Path | None,
    invite_arg: Path | None,
    *,
    now: datetime | None = None,
) -> tuple[dict[str, str], Path, Path]:
    """Load the cleanup-capable invite handle before any other input."""
    admin = repository_root / "supabase" / "admin"
    if str(admin) not in sys.path:
        sys.path.insert(0, str(admin))
    from private_storage import ensure_private_root, private_child

    private_root = ensure_private_root(repository_root, private_root_arg)
    invite_path = invite_arg or (
        private_root / DEFAULT_INVITE_DIRECTORY / "invite_links.private.json"
    )
    invite_path = private_child(
        invite_path, repository_root, private_root, must_exist=True
    )
    check_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    batch, encoded = _load_json_bytes(invite_path, "invite file")
    invite = validate_disposable_invite_batch(batch, at=check_now)
    invite["provision_file_sha256"] = _sha256(encoded)
    return invite, invite_path, private_root


def load_private_identity_and_receipt(
    repository_root: Path,
    private_root: Path,
    identity_arg: Path | None,
    receipt_arg: Path | None,
) -> tuple[dict[str, str], Path]:
    from private_storage import private_child

    identity_path = identity_arg or (private_root / DEFAULT_IDENTITY_FILE)
    receipt_path = receipt_arg or (private_root / DEFAULT_RECEIPT_FILE)
    identity_path = private_child(
        identity_path, repository_root, private_root, must_exist=True
    )
    receipt_path = private_child(
        receipt_path, repository_root, private_root, must_exist=False
    )

    identity_raw = _load_json(identity_path, "identity file")
    if set(identity_raw) != {"schema_version", "purpose", "name", "phone"}:
        raise E2EError("identity file has an unexpected schema")
    if identity_raw.get("schema_version") != "1.0" or identity_raw.get("purpose") != IDENTITY_PURPOSE:
        raise E2EError("identity file is not marked disposable remote-E2E test-only")
    name_value = identity_raw.get("name")
    phone_value = identity_raw.get("phone")
    if not isinstance(name_value, str) or name_value != name_value.strip():
        raise E2EError("test identity name must be trimmed text")
    name = unicodedata.normalize("NFC", name_value)
    if name != name_value or not (4 <= len(name) <= 80) or any(ord(c) < 32 for c in name):
        raise E2EError("test identity name has an invalid shape")
    if not isinstance(phone_value, str):
        raise E2EError("test identity phone must be text")
    phone = re.sub(r"\D", "", phone_value)
    if not re.fullmatch(r"01[016789]\d{7,8}", phone):
        raise E2EError("test identity phone has an invalid shape")
    return {"name": name, "phone": phone}, receipt_path


def make_config_override(
    original: bytes, verified_at: datetime
) -> tuple[bytes, str]:
    """Return the browser-memory config used for a disposable E2E.

    A published live release is tested byte-for-byte without a configuration
    change.  The retained staging path changes only the fielding gate and the
    verification timestamp in browser memory.
    """
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise E2EError("deployed site-config.js is not UTF-8") from exc
    field_pattern = re.compile(
        r"(^\s*fieldingEnabled:\s*)(true|false)(\s*,?\s*$)", re.MULTILINE
    )
    time_pattern = re.compile(
        r'(^\s*remoteE2eVerifiedAt:\s*)"[^"]*"(\s*,?\s*$)', re.MULTILINE
    )
    field_matches = list(field_pattern.finditer(text))
    time_matches = list(time_pattern.finditer(text))
    if len(field_matches) != 1 or len(time_matches) != 1:
        raise E2EError("deployed config is not uniquely parseable")
    stamp = _utc_seconds(verified_at)
    if not UTC_RE.fullmatch(stamp) or verified_at > datetime.now(timezone.utc) + timedelta(seconds=1):
        raise E2EError("in-memory E2E verification timestamp is invalid")
    if field_matches[0].group(2) == "true":
        return original, stamp
    original_time = time_matches[0].group(0).split('"')[1]
    overridden = field_pattern.sub(r"\1true\3", text, count=1)
    overridden = time_pattern.sub(
        lambda match: match.group(1) + json.dumps(stamp) + match.group(2),
        overridden,
        count=1,
    )
    restored = field_pattern.sub(r"\1false\3", overridden, count=1)
    restored = time_pattern.sub(
        lambda match: match.group(1) + json.dumps(original_time) + match.group(2),
        restored,
        count=1,
    )
    if restored != text:
        raise E2EError("config override attempted to change more than two fields")
    return overridden.encode("utf-8"), stamp


def fetch_remote_assets(timeout_seconds: int = 20) -> dict[str, bytes]:
    assets: dict[str, bytes] = {}
    for name in REMOTE_FILES:
        request = urllib.request.Request(
            urljoin(SITE_URL, name),
            headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                if response.status != 200 or response.geturl().split("?", 1)[0] != urljoin(SITE_URL, name):
                    raise E2EError(f"deployed {name} did not return exact HTTP 200")
                assets[name] = response.read()
        except (OSError, urllib.error.URLError) as exc:
            raise E2EError(f"could not fetch deployed {name}") from exc
    return assets


def local_assets(repository_root: Path) -> dict[str, bytes]:
    try:
        return {name: (repository_root / "docs" / name).read_bytes() for name in REMOTE_FILES}
    except OSError as exc:
        raise E2EError("could not read a required local deployment asset") from exc


def validate_remote_release(
    remote: Mapping[str, bytes],
    local: Mapping[str, bytes],
    verified_at: datetime,
    *,
    transition_migration_path: Path | None = None,
) -> dict[str, Any]:
    """Fail unless GitHub Pages is the exact, internally consistent local build."""
    if set(remote) != set(REMOTE_FILES) or set(local) != set(REMOTE_FILES):
        raise E2EError("deployment asset set is incomplete")
    for name in REMOTE_FILES:
        if not hmac.compare_digest(_sha256(remote[name]), _sha256(local[name])):
            raise E2EError(f"deployed {name} does not match the local release")

    try:
        manifest = json.loads(remote["deployment-manifest.json"].decode("utf-8"))
        instrument = json.loads(remote["instrument.json"].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise E2EError("deployed manifest or instrument is not valid UTF-8 JSON") from exc
    if not isinstance(manifest, dict) or not isinstance(instrument, dict):
        raise E2EError("deployed manifest or instrument is not a JSON object")
    if set(manifest) != {
        "deployment_manifest_sha256",
        "schema_version",
        "deployment_state",
        "site_url",
        "api_url",
        "hosted_version",
        "instrument_sha256",
        "operational_file_hashes",
        "deployment_source_hashes",
    }:
        raise E2EError("deployed deployment manifest schema is invalid")

    declared_manifest_hash = manifest.get("deployment_manifest_sha256")
    manifest_basis = {
        key: value
        for key, value in manifest.items()
        if key != "deployment_manifest_sha256"
    }
    calculated_manifest_hash = _sha256(_canonical_json(manifest_basis))
    if (
        not isinstance(declared_manifest_hash, str)
        or not HASH_RE.fullmatch(declared_manifest_hash)
        or not hmac.compare_digest(declared_manifest_hash, calculated_manifest_hash)
    ):
        raise E2EError("deployed deployment manifest self-digest is invalid")

    declared_instrument_hash = instrument.get("instrument_sha256")
    instrument_basis = {
        key: value for key, value in instrument.items() if key != "instrument_sha256"
    }
    calculated_instrument_hash = _sha256(_canonical_json(instrument_basis))
    if (
        not isinstance(declared_instrument_hash, str)
        or not HASH_RE.fullmatch(declared_instrument_hash)
        or not hmac.compare_digest(declared_instrument_hash, calculated_instrument_hash)
    ):
        raise E2EError("deployed instrument self-digest is invalid")

    config_text = remote["site-config.js"].decode("utf-8")
    privacy_text = remote["privacy.html"].decode("utf-8")
    override, stamp = make_config_override(remote["site-config.js"], verified_at)
    tools_path = Path(__file__).resolve().parent
    if str(tools_path) not in sys.path:
        sys.path.insert(0, str(tools_path))
    from build_r5_public_instrument import RELEASE_SOURCE_PATHS
    from pi_config import boolean_value, quoted_value, validate_live_config
    from build_r5_deployment_manifest import TRANSITION as transition_path_constant

    try:
        published_fielding = boolean_value(config_text, "fieldingEnabled")
        config_api = quoted_value(config_text, "apiUrl")
        config_origin = quoted_value(config_text, "githubPagesOrigin")
        config_base = quoted_value(config_text, "basePath")
        config_version = quoted_value(config_text, "hostedVersion")
        config_offline_hash = quoted_value(
            config_text, "sourceOfflineInstrumentSha256"
        )
    except ValueError as exc:
        raise E2EError("deployed site config has an invalid schema") from exc
    if published_fielding:
        release_state = "live"
        validation_config = config_text
    else:
        release_state = "staging"
        validation_config = override.decode("utf-8")
    override_errors = validate_live_config(
        validation_config,
        privacy_text,
        today=verified_at.date(),
        now=verified_at + timedelta(seconds=1),
    )
    if override_errors:
        raise E2EError("in-memory config override does not pass live-config validation")

    expected_identity = {
        "schema_version": "1.0",
        "deployment_state": release_state,
        "site_url": SITE_URL,
        "api_url": API_URL,
        "hosted_version": instrument.get("hosted_version"),
        "instrument_sha256": declared_instrument_hash,
    }
    for key, expected in expected_identity.items():
        if manifest.get(key) != expected:
            raise E2EError(f"deployed manifest {key} identity is invalid")
    if (
        config_api != API_URL
        or config_origin + config_base != SITE_URL
        or config_version != instrument.get("hosted_version")
        or config_offline_hash != instrument.get("source_offline_instrument_sha256")
    ):
        raise E2EError("deployed site config identity does not match the instrument")

    operational_hashes = manifest.get("operational_file_hashes")
    if operational_hashes != {
        "privacy_notice": _sha256(remote["privacy.html"]),
        "site_config": _sha256(remote["site-config.js"]),
    }:
        raise E2EError("deployed operational asset hashes are stale")
    repository_root = Path(__file__).resolve().parents[1]
    transition_path = transition_migration_path or transition_path_constant
    try:
        expected_transition_hash = _sha256(transition_path.read_bytes())
    except OSError as exc:
        raise E2EError("could not attest the local instrument transition") from exc
    if manifest.get("deployment_source_hashes") != {
        "database_instrument_transition": expected_transition_hash
    }:
        raise E2EError("deployed transition migration hash is stale")
    release_hashes = instrument.get("release_source_hashes")
    if not isinstance(release_hashes, dict):
        raise E2EError("deployed instrument release hashes are missing")
    try:
        expected_local_release_hashes = {
            name: _sha256((repository_root / relative).read_bytes())
            for name, relative in RELEASE_SOURCE_PATHS.items()
        }
    except OSError as exc:
        raise E2EError("could not attest local release sources") from exc
    if (
        set(release_hashes) != set(expected_local_release_hashes)
        or any(
            release_hashes.get(key) != value
            for key, value in expected_local_release_hashes.items()
        )
    ):
        raise E2EError("deployed release source hashes do not match local sources")
    required_release_hashes = {
        "public_admin": _sha256(remote["admin.html"]),
        "public_admin_bridge": _sha256(remote["admin.js"]),
        "public_app": _sha256(remote["app.js"]),
        "public_contract": _sha256(remote["submission-contract.js"]),
        "public_index": _sha256(remote["index.html"]),
        "public_styles": _sha256(remote["styles.css"]),
    }
    if any(release_hashes.get(key) != value for key, value in required_release_hashes.items()):
        raise E2EError("deployed public asset hashes do not match the instrument")
    hash_script = remote["instrument-hash.js"].decode("utf-8")
    hash_mentions = re.findall(r'"([0-9a-f]{64})"', hash_script)
    if hash_mentions != [declared_instrument_hash]:
        raise E2EError("deployed instrument-hash.js identity is invalid")
    assignments = instrument.get("assignments")
    if not isinstance(assignments, dict) or any(
        not isinstance(assignments.get(code), list) or len(assignments[code]) != 12
        for code in (f"PILOT_R0{number}" for number in range(1, 6))
    ):
        raise E2EError("deployed instrument assignments are incomplete")
    return {
        "manifest": manifest,
        "instrument": instrument,
        "config_override": override,
        "override_timestamp": stamp,
        "published_fielding": published_fielding,
        "asset_hashes": {name: _sha256(remote[name]) for name in REMOTE_FILES},
    }


def _new_linked_backend() -> Any:
    from supabase.admin.linked_cli_backend import LinkedCliBackend
    return LinkedCliBackend()


def read_gate_status(
    repository_root: Path,
    instrument_sha256: str,
    linked_backend: Any | None = None,
) -> dict[str, object]:
    """Use manage_fielding_gate's read-only path; never mutate the DB gate."""
    if linked_backend is not None:
        try:
            return linked_backend.read_status(instrument_sha256)
        except Exception as exc:
            raise E2EError(
                "could not verify the read-only fielding-gate status"
            ) from exc
    if str(repository_root) not in sys.path:
        sys.path.insert(0, str(repository_root))
    from supabase.admin import manage_fielding_gate

    connection = None
    try:
        project_ref = manage_fielding_gate.resolve_project_ref(
            repository_root, None
        )
        database_url = manage_fielding_gate.required_database_url(
            os.environ, project_ref
        )
        connection = manage_fielding_gate.connect_database(database_url)
        return manage_fielding_gate.read_gate_status(connection, instrument_sha256)
    except (OSError, ValueError, manage_fielding_gate.GateError) as exc:
        raise E2EError("could not verify the read-only fielding-gate status") from exc
    except Exception as exc:
        raise E2EError(
            "read-only fielding-gate verification failed " + type(exc).__name__
        ) from exc
    finally:
        if connection is not None:
            connection.close()


def validate_gate_status(status: Mapping[str, object], *, after: bool) -> None:
    expected = {
        "is_active": True,
        "fielding_open": True,
        "unrevoked_invite_count": 1,
        "eligible_unused_invite_count": 0 if after else 1,
        "used_invite_count": 1 if after else 0,
        "submission_count": 1 if after else 0,
    }
    if any(status.get(key) != value for key, value in expected.items()):
        stage = "post-E2E" if after else "pre-E2E"
        raise E2EError(f"{stage} fielding-gate status is not the disposable invariant")


def find_browser(selection: str) -> tuple[Path, str]:
    choices = {
        "chrome": Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        "edge": Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    }
    order = ("chrome", "edge") if selection == "auto" else (selection,)
    for family in order:
        if choices[family].is_file():
            return choices[family], family
    raise E2EError("no supported installed Chrome or Edge browser was found")


def write_receipt(
    path: Path,
    receipt: Mapping[str, object],
    *,
    private_values: tuple[str, ...] = (),
) -> None:
    forbidden = re.compile(
        r"invite|token|identity|phone|name|request|body|payload|idempotency|submission",
        re.IGNORECASE,
    )

    def inspect(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if forbidden.search(str(key)):
                    raise E2EError("receipt schema contains a forbidden private field")
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)

    inspect(dict(receipt))
    serialized = json.dumps(receipt, ensure_ascii=False, sort_keys=True)
    if any(value and value in serialized for value in private_values):
        raise E2EError("receipt contains a private input value")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    encoded = json.dumps(receipt, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            temporary_path.chmod(0o600)
        except OSError:
            pass
        try:
            os.link(temporary_path, path)
        except FileExistsError as exc:
            raise FileExistsError(
                f"refusing to overwrite existing receipt: {path}"
            ) from exc
        temporary_path.unlink()
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


class CdpClient:
    """Small synchronous CDP client with one exact Fetch interception."""

    def __init__(
        self,
        socket: Any,
        config_override: bytes,
        private_values: tuple[str, ...] = (),
    ):
        self.socket = socket
        self.config_override = config_override
        self.next_id = 1
        self.responses: dict[int, dict[str, Any]] = {}
        self.intercept_count = 0
        self.event_failure: str | None = None
        self.seen_urls: set[str] = set()
        self.response_statuses: dict[str, int] = {}
        self.submit_requests: list[tuple[str, dict[str, Any]]] = []
        self.api_statuses: dict[str, int] = {}
        self.javascript_exceptions = 0
        self.private_values = tuple(value for value in private_values if value)

    def _request_violation(
        self,
        request: Mapping[str, Any],
        resource_type: object = None,
    ) -> str | None:
        url = request.get("url")
        if not isinstance(url, str):
            return "browser request did not contain a valid destination"
        scheme = urlsplit(url).scheme.lower()
        if scheme in {"http", "https", "ws", "wss"} and url not in ALLOWED_NETWORK_URLS:
            return "browser attempted an unexpected network destination"
        method = request.get("method", "GET")
        headers = request.get("headers")
        post_data = request.get("postData")
        header_text = (
            json.dumps(headers, ensure_ascii=False, sort_keys=True)
            if isinstance(headers, dict)
            else ""
        )
        if any(
            value in url or value in header_text
            for value in self.private_values
        ):
            return "browser attempted to place private input in URL or headers"
        carries_private = isinstance(post_data, str) and any(
            value in post_data for value in self.private_values
        )
        if (
            method == "POST"
            and url != API_URL
            or carries_private
            and (url != API_URL or method != "POST")
        ):
            return "browser attempted a disallowed POST destination"
        if url == CONFIG_URL and (
            method != "GET" or resource_type not in {None, "Script"}
        ):
            return "browser requested the config through an invalid method or type"
        if url == API_URL and method not in {"POST", "OPTIONS"}:
            return "browser used an invalid API request method"
        if url in BROWSER_ASSETS and url != API_URL and method != "GET":
            return "browser used an invalid static-asset request method"
        return None

    def _check_destination(self, request: Mapping[str, Any]) -> None:
        violation = self._request_violation(request)
        if violation is not None:
            self.event_failure = violation

    def _send(self, method: str, params: Mapping[str, object] | None = None) -> int:
        message_id = self.next_id
        self.next_id += 1
        self.socket.send(
            json.dumps({"id": message_id, "method": method, "params": params or {}})
        )
        return message_id

    def _handle_event(self, message: Mapping[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params") or {}
        if method == "Fetch.requestPaused":
            request = params.get("request") or {}
            request_id = params.get("requestId")
            violation = (
                self._request_violation(
                    request,
                    params.get("resourceType"),
                )
                if isinstance(request, dict)
                else "browser interception returned an invalid request"
            )
            if violation is not None:
                self.event_failure = violation
                if isinstance(request_id, str):
                    self._send(
                        "Fetch.failRequest",
                        {
                            "requestId": request_id,
                            "errorReason": "BlockedByClient",
                        },
                    )
            elif (
                request.get("url") == CONFIG_URL
                and params.get("resourceType") == "Script"
                and isinstance(request_id, str)
            ):
                self.intercept_count += 1
                if self.intercept_count > 1:
                    self.event_failure = "site-config.js was intercepted more than once"
                self._send(
                    "Fetch.fulfillRequest",
                    {
                        "requestId": request_id,
                        "responseCode": 200,
                        "responseHeaders": [
                            {
                                "name": "Content-Type",
                                "value": "application/javascript; charset=utf-8",
                            },
                            {"name": "Cache-Control", "value": "no-store"},
                        ],
                        "body": base64.b64encode(self.config_override).decode("ascii"),
                    },
                )
            else:
                if isinstance(request_id, str):
                    self._send("Fetch.continueRequest", {"requestId": request_id})
        elif method == "Network.requestWillBeSent":
            request = params.get("request") or {}
            if isinstance(request, dict):
                self._check_destination(request)
            url = request.get("url")
            if isinstance(url, str):
                self.seen_urls.add(url)
            if url == API_URL and isinstance(request.get("postData"), str):
                try:
                    body = json.loads(request["postData"])
                except json.JSONDecodeError:
                    body = None
                if isinstance(body, dict) and body.get("action") == "submit":
                    request_id = params.get("requestId")
                    if isinstance(request_id, str):
                        self.submit_requests.append((request_id, body))
        elif method == "Network.responseReceived":
            response = params.get("response") or {}
            url = response.get("url")
            status = response.get("status")
            if isinstance(url, str) and isinstance(status, (int, float)):
                self.response_statuses[url] = int(status)
                if url == API_URL and isinstance(params.get("requestId"), str):
                    self.api_statuses[params["requestId"]] = int(status)
        elif method == "Runtime.exceptionThrown":
            self.javascript_exceptions += 1
        elif method == "Network.webSocketCreated":
            url = params.get("url")
            if isinstance(url, str) and urlsplit(url).scheme.lower() in {"ws", "wss"}:
                self.event_failure = "browser attempted an unexpected network destination"

    def _receive_one(self, timeout: float) -> bool:
        try:
            raw = self.socket.recv(timeout=timeout)
            message = json.loads(raw)
        except TimeoutError:
            # A quiet one-second polling interval is not the command deadline.
            # The caller owns the overall deadline and may continue waiting.
            return False
        except Exception as exc:
            raise E2EError("browser automation transport failed") from exc
        if not isinstance(message, dict):
            raise E2EError("browser automation returned an invalid message")
        if "id" in message and isinstance(message["id"], int):
            self.responses[message["id"]] = message
        else:
            self._handle_event(message)
        return True

    def command(
        self,
        method: str,
        params: Mapping[str, object] | None = None,
        timeout: float = 30,
    ) -> dict[str, Any]:
        message_id = self._send(method, params)
        deadline = time.monotonic() + timeout
        while message_id not in self.responses:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise E2EError(f"browser command timed out: {method}")
            self._receive_one(min(remaining, 1.0))
            if self.event_failure:
                raise E2EError(self.event_failure)
        response = self.responses.pop(message_id)
        if "error" in response:
            raise E2EError(f"browser command failed: {method}")
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    def evaluate(self, expression: str, *, await_promise: bool = False) -> Any:
        result = self.command(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": await_promise,
                "returnByValue": True,
            },
        )
        if "exceptionDetails" in result:
            raise E2EError("browser page evaluation failed")
        remote = result.get("result") or {}
        return remote.get("value")

    def wait_js(self, expression: str, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.evaluate("Boolean(" + expression + ")") is True:
                return
            time.sleep(0.1)
        raise E2EError("hosted page did not reach the expected state")


def _debug_json(port: int, path: str) -> Any:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise E2EError("could not connect to the local browser debugger") from exc


def _start_browser(executable: Path, profile: Path) -> tuple[subprocess.Popen[bytes], int, str, str]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        [
            str(executable),
            "--headless=new",
            "--disable-gpu",
            "--disable-background-networking",
            "--disable-breakpad",
            "--disable-component-update",
            "--disable-extensions",
            "--disable-sync",
            "--no-default-browser-check",
            "--no-first-run",
            "--metrics-recording-only",
            "--remote-debugging-port=0",
            "--user-data-dir=" + str(profile),
            "about:blank",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    port_file = profile / "DevToolsActivePort"
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise E2EError("headless browser exited before automation started")
        if port_file.is_file():
            try:
                port = int(port_file.read_text(encoding="utf-8").splitlines()[0])
                version = _debug_json(port, "/json/version")
                pages = _debug_json(port, "/json/list")
                page = next(
                    entry
                    for entry in pages
                    if entry.get("type") == "page"
                    and isinstance(entry.get("webSocketDebuggerUrl"), str)
                )
                return process, port, page["webSocketDebuggerUrl"], str(version.get("Browser", "unknown"))
            except (OSError, ValueError, IndexError, StopIteration):
                pass
        time.sleep(0.1)
    raise E2EError("headless browser debugger did not become ready")


def _api_fetch_expression(body: Mapping[str, object]) -> str:
    return """
      (async function () {
        const response = await fetch(%s, {
          method: "POST",
          mode: "cors",
          credentials: "omit",
          cache: "no-store",
          redirect: "error",
          referrerPolicy: "no-referrer",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(%s)
        });
        let result = {};
        try { result = await response.json(); } catch (_) {}
        return {status: response.status, result: result};
      })()
    """ % (json.dumps(API_URL), json.dumps(body, ensure_ascii=False))


def _valid_receipt_payload(
    value: object, expected_id: str | None, *, idempotent: bool
) -> tuple[str, str]:
    if not isinstance(value, dict) or value.get("ok") is not True:
        raise E2EError("API success response has an invalid envelope")
    receipt = value.get("receipt")
    if not isinstance(receipt, dict) or receipt.get("idempotent") is not idempotent:
        raise E2EError("API success response has invalid idempotency metadata")
    try:
        submission_id = str(uuid.UUID(str(receipt.get("submission_id"))))
    except (ValueError, AttributeError) as exc:
        raise E2EError("API success response has an invalid receipt ID") from exc
    if expected_id is not None and not hmac.compare_digest(submission_id, expected_id):
        raise E2EError("idempotent retry returned a different receipt ID")
    submitted_at = receipt.get("submitted_at")
    _parse_utc(submitted_at, "API receipt submitted_at")
    return submission_id, str(submitted_at)


def run_browser_flow(
    executable: Path,
    browser_family: str,
    config_override: bytes,
    invite: Mapping[str, str],
    identity: Mapping[str, str],
    instrument: Mapping[str, Any],
    timeout: int,
) -> dict[str, object]:
    try:
        from websockets.sync.client import connect
    except ImportError as exc:
        raise E2EError("installed Python websockets with sync support is required") from exc

    assignments = instrument.get("assignments") or {}
    expected_items = assignments.get(invite["assignment_code"])
    if not isinstance(expected_items, list) or len(expected_items) != 12:
        raise E2EError("disposable assignment is absent from the deployed instrument")
    expected_sentences = [item.get("sentence_text") for item in expected_items]
    if any(not isinstance(sentence, str) for sentence in expected_sentences):
        raise E2EError("deployed assignment contains an invalid sentence")

    process: subprocess.Popen[bytes] | None = None
    socket: Any = None
    with tempfile.TemporaryDirectory(prefix="bok-pilot-e2e-browser-") as profile_text:
        profile = Path(profile_text)
        try:
            process, _port, websocket_url, browser_version = _start_browser(
                executable, profile
            )
            try:
                socket = connect(
                    websocket_url,
                    open_timeout=5,
                    close_timeout=2,
                    max_size=8 * 1024 * 1024,
                )
            except Exception as exc:
                raise E2EError("could not connect to the local browser debugger") from exc
            client = CdpClient(
                socket,
                config_override,
                private_values=(
                    invite["invite_id"],
                    invite["invite_token"],
                    identity["name"],
                    identity["phone"],
                ),
            )
            client.command("Page.enable")
            client.command("Runtime.enable")
            client.command("Network.enable")
            client.command("Network.setCacheDisabled", {"cacheDisabled": True})
            client.command("Network.setBypassServiceWorker", {"bypass": True})
            client.command(
                "Fetch.enable",
                {
                    "patterns": [
                        {
                            "urlPattern": "*",
                            "requestStage": "Request",
                        }
                    ]
                },
            )
            destination = SITE_URL + "#invite=" + quote(
                invite["invite_token"], safe=""
            )
            client.command("Page.navigate", {"url": destination})
            client.wait_js(
                "document.getElementById('identityPanel') && !document.getElementById('identityPanel').hidden",
                timeout,
            )
            token_literal = json.dumps(invite["invite_token"])
            private_before = client.evaluate(
                """
                (function () {
                  const values = [location.href, location.hash, document.cookie];
                  for (let i = 0; i < localStorage.length; i++) values.push(localStorage.getItem(localStorage.key(i)) || "");
                  for (let i = 0; i < sessionStorage.length; i++) values.push(sessionStorage.getItem(sessionStorage.key(i)) || "");
                  return {hashEmpty: location.hash === "", secretAbsent: !values.join("\\n").includes(%s)};
                })()
                """ % token_literal
            )
            if not isinstance(private_before, dict) or not all(private_before.values()):
                raise E2EError("invite fragment or token remained browser-visible")

            identity_expression = """
                (function () {
                  document.getElementById('participantName').value = %s;
                  document.getElementById('participantPhone').value = %s;
                  document.getElementById('consentAccepted').checked = true;
                  document.getElementById('beginSurvey').click();
                  return true;
                })()
            """ % (json.dumps(identity["name"], ensure_ascii=False), json.dumps(identity["phone"]))
            client.evaluate(identity_expression)
            client.wait_js(
                "document.getElementById('tutorialPanel') && !document.getElementById('tutorialPanel').hidden",
                timeout,
            )
            client.evaluate("document.getElementById('beginCore').click(); true")
            client.wait_js(
                "document.getElementById('surveyPanel') && !document.getElementById('surveyPanel').hidden",
                timeout,
            )
            answer_result = client.evaluate(
                """
                (async function () {
                  const expected = %s;
                  const choices = [-2, -1, 0, 1, 2, 99];
                  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
                  for (let index = 0; index < 12; index++) {
                    if (document.getElementById('sentenceText').textContent !== expected[index]) throw new Error('sentence mismatch');
                    const choice = choices[index %% choices.length];
                    document.querySelector('input[name="stance"][value="' + String(choice) + '"]').click();
                    const confidence = document.getElementById('confidence');
                    confidence.value = '3';
                    confidence.dispatchEvent(new Event('change', {bubbles: true}));
                    const reason = document.getElementById('reasonCode');
                    reason.value = choice === 99 ? 'CONTEXT_NEEDED' : 'NONE';
                    reason.dispatchEvent(new Event('change', {bubbles: true}));
                    await pause(75);
                    document.getElementById('nextItem').click();
                    await pause(25);
                  }
                  if (document.getElementById('feedbackPanel').hidden) throw new Error('feedback not reached');
                  document.getElementById('fatigue').value = '2';
                  document.getElementById('zeroVs99').value = 'Zero is neutral; 99 means context is insufficient.';
                  document.getElementById('changeVsStance').value = 'Change direction is distinct from the current stance level.';
                  document.getElementById('uiError').value = '';
                  document.getElementById('reviewSurvey').click();
                  return {
                    count: document.getElementById('reviewCount').textContent.trim(),
                    instrument: document.getElementById('reviewInstrument').textContent.trim(),
                    reviewVisible: !document.getElementById('reviewPanel').hidden
                  };
                })()
                """ % json.dumps(expected_sentences, ensure_ascii=False),
                await_promise=True,
            )
            if answer_result != {
                "count": "12 / 12",
                "instrument": instrument.get("instrument_sha256"),
                "reviewVisible": True,
            }:
                raise E2EError("hosted UI did not complete and review all 12 assignments")
            client.evaluate("document.getElementById('submitSurvey').click(); true")
            client.wait_js(
                "document.getElementById('donePanel') && !document.getElementById('donePanel').hidden",
                timeout,
            )
            if len(client.submit_requests) != 1:
                raise E2EError("hosted UI did not issue exactly one initial submit request")
            initial_request_id, original_body = client.submit_requests[0]
            if (
                original_body.get("invite_token") != invite["invite_token"]
                or original_body.get("instrument_sha256") != instrument.get("instrument_sha256")
                or not isinstance(original_body.get("responses"), list)
                or len(original_body["responses"]) != 12
                or original_body.get("identity") != dict(identity)
            ):
                raise E2EError("hosted UI submit request did not match the E2E assignment")
            initial_status = client.api_statuses.get(initial_request_id)
            if initial_status != 200:
                raise E2EError("initial hosted UI submit did not return HTTP 200")
            body_result = client.command(
                "Network.getResponseBody", {"requestId": initial_request_id}
            )
            response_body = body_result.get("body")
            if not isinstance(response_body, str):
                raise E2EError("initial hosted UI submit response body is unavailable")
            if body_result.get("base64Encoded"):
                response_body = base64.b64decode(response_body).decode("utf-8")
            try:
                initial_json = json.loads(response_body)
            except json.JSONDecodeError as exc:
                raise E2EError("initial hosted UI submit response is not JSON") from exc
            receipt_id, submitted_at = _valid_receipt_payload(
                initial_json, None, idempotent=False
            )

            identical = client.evaluate(
                _api_fetch_expression(original_body), await_promise=True
            )
            if not isinstance(identical, dict) or identical.get("status") != 200:
                raise E2EError("identical retry did not return HTTP 200")
            retry_id, retry_submitted_at = _valid_receipt_payload(
                identical.get("result"), receipt_id, idempotent=True
            )
            if retry_id != receipt_id or retry_submitted_at != submitted_at:
                raise E2EError("identical retry did not return the original receipt")

            changed_body = dict(original_body)
            changed_body["idempotency_key"] = str(uuid.uuid4())
            changed = client.evaluate(
                _api_fetch_expression(changed_body), await_promise=True
            )
            error_code = None
            if isinstance(changed, dict) and isinstance(changed.get("result"), dict):
                error = changed["result"].get("error")
                if isinstance(error, dict):
                    error_code = error.get("code")
            if not isinstance(changed, dict) or changed.get("status") != 409 or error_code != "INVITE_ALREADY_USED":
                raise E2EError("changed retry was not rejected as INVITE_ALREADY_USED")

            cleared = client.evaluate(
                """
                (function () {
                  const storageKeys = [];
                  for (let i = 0; i < localStorage.length; i++) storageKeys.push(localStorage.key(i));
                  return {
                    fragmentRemoved: location.hash === '',
                    inputsCleared: document.getElementById('participantName').value === '' &&
                      document.getElementById('participantPhone').value === '' &&
                      !document.getElementById('consentAccepted').checked,
                    draftCleared: !storageKeys.some(key => key && key.startsWith('bok_stance_hosted_pilot_'))
                  };
                })()
                """
            )
            if not isinstance(cleared, dict) or not all(cleared.values()):
                raise E2EError("hosted UI did not clear fragment, PII inputs, and draft")
            if client.intercept_count != 1:
                raise E2EError("site-config.js was not intercepted exactly once")
            if client.event_failure:
                raise E2EError(client.event_failure)
            if client.javascript_exceptions != 0:
                raise E2EError("hosted page raised a JavaScript exception")
            missing_assets = [url for url in BROWSER_ASSETS if url not in client.seen_urls]
            if missing_assets:
                raise E2EError("browser did not load every required real hosted asset")
            for url in BROWSER_ASSETS:
                if url != CONFIG_URL and client.response_statuses.get(url) != 200:
                    raise E2EError("a required real hosted asset did not return HTTP 200")
            return {
                "browser_family": browser_family,
                "browser_version": browser_version,
                "asset_count": len(BROWSER_ASSETS),
                "config_intercept_count": client.intercept_count,
                "assignment_count": 12,
                "initial_status": initial_status,
                "same_retry_status": identical["status"],
                "changed_retry_status": changed["status"],
                "fragment_removed": cleared["fragmentRemoved"],
                "private_inputs_cleared": cleared["inputsCleared"],
                "draft_cleared": cleared["draftCleared"],
                "javascript_exceptions": client.javascript_exceptions,
            }
        finally:
            if socket is not None:
                try:
                    socket.close()
                except Exception:
                    pass
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def best_effort_close_e2e(
    repository_root: Path,
    instrument_sha256: str,
    invite_id: str,
    linked_backend: Any | None = None,
) -> str:
    """Close the gate and revoke the current disposable invite without logging IDs."""
    if linked_backend is not None:
        linked_backend.close_e2e(instrument_sha256, invite_id)
        return "CLOSED_AND_REVOKED"
    if str(repository_root) not in sys.path:
        sys.path.insert(0, str(repository_root))
    from supabase.admin import manage_fielding_gate

    connection = None
    try:
        project_ref = manage_fielding_gate.resolve_project_ref(
            repository_root, None
        )
        database_url = manage_fielding_gate.required_database_url(
            os.environ, project_ref
        )
        connection = manage_fielding_gate.connect_database(database_url)
        manage_fielding_gate.mutate_gate(
            connection,
            "close-e2e",
            instrument_sha256,
            invite_id,
        )
        return "CLOSED_AND_REVOKED"
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    repository_root = Path(__file__).resolve().parents[1]
    if not 5 <= args.timeout_seconds <= 120:
        print("ERROR: --timeout-seconds must be between 5 and 120", file=sys.stderr)
        return 2
    invite: dict[str, str] | None = None
    identity: dict[str, str] | None = None
    receipt_path: Path | None = None
    release: dict[str, Any] | None = None
    browser_result: dict[str, object] | None = None
    linked_backend: Any | None = None
    linked_open_attempted = False
    failure: str | None = None
    failure_stage = "private-invite"
    cleanup_result = "NOT_ATTEMPTED"
    try:
        tested_at = datetime.now(timezone.utc) - timedelta(seconds=2)
        invite, _invite_path, private_root = load_private_invite(
            repository_root,
            args.private_root,
            args.invite_file,
            now=tested_at,
        )
        failure_stage = "private-identity"
        identity, receipt_path = load_private_identity_and_receipt(
            repository_root,
            private_root,
            args.identity_file,
            args.receipt,
        )
        failure_stage = "remote-assets"
        remote = fetch_remote_assets(timeout_seconds=min(args.timeout_seconds, 30))
        failure_stage = "release-attestation"
        release = validate_remote_release(
            remote, local_assets(repository_root), tested_at
        )
        instrument = release["instrument"]
        manifest = release["manifest"]
        if invite["instrument_sha256"] != instrument.get("instrument_sha256"):
            raise E2EError("disposable invite targets a different deployed instrument")
        failure_stage = "browser-selection"
        executable, browser_family = find_browser(args.browser)
        if args.db_backend == "linked-cli":
            if str(repository_root) not in sys.path:
                sys.path.insert(0, str(repository_root))
            from supabase.admin import manage_fielding_gate

            failure_stage = "gate-argument-validation"
            manage_fielding_gate.validate_action_arguments(
                "open-e2e",
                invite["instrument_sha256"],
                invite["invite_id"],
                args.confirm_open_e2e,
            )
            failure_stage = "linked-backend"
            linked_backend = _new_linked_backend()
            linked_open_attempted = True
            failure_stage = "gate-open"
            linked_backend.open_e2e(
                invite["instrument_sha256"],
                instrument.get("hosted_version"),
                invite["invite_id"],
            )
        elif args.confirm_open_e2e is not None:
            raise E2EError(
                "--confirm-open-e2e is accepted only with linked-cli"
            )
        print("PASS: deployed release assets and identities are exact")

        failure_stage = "gate-precheck"
        before = (
            read_gate_status(
                repository_root,
                invite["instrument_sha256"],
                linked_backend,
            )
            if linked_backend is not None
            else read_gate_status(
                repository_root, invite["instrument_sha256"]
            )
        )
        validate_gate_status(before, after=False)
        print("PASS: disposable E2E gate is open with zero prior submissions")

        failure_stage = "browser-flow"
        browser_result = run_browser_flow(
            executable,
            browser_family,
            release["config_override"],
            invite,
            identity,
            instrument,
            args.timeout_seconds,
        )
        failure_stage = "gate-postcheck"
        after = (
            read_gate_status(
                repository_root,
                invite["instrument_sha256"],
                linked_backend,
            )
            if linked_backend is not None
            else read_gate_status(
                repository_root, invite["instrument_sha256"]
            )
        )
        validate_gate_status(after, after=True)
    except (E2EError, FileExistsError, ValueError) as exc:
        failure = f"ERROR: {exc}"
    except Exception as exc:
        failure = f"ERROR: remote E2E failed ({type(exc).__name__})"
    finally:
        if invite is not None and (
            args.db_backend == "direct" or linked_open_attempted
        ):
            try:
                cleanup_result = (
                    best_effort_close_e2e(
                        repository_root,
                        invite["instrument_sha256"],
                        invite["invite_id"],
                        linked_backend,
                    )
                    if linked_backend is not None
                    else best_effort_close_e2e(
                        repository_root,
                        invite["instrument_sha256"],
                        invite["invite_id"],
                    )
                )
                print("AUTO-CLEANUP: fielding closed and disposable invite revoked")
            except Exception:
                cleanup_result = "AUTO_CLOSE_FAILED"
                print(
                    "AUTO-CLEANUP WARNING: automatic close/revoke failed; "
                    "run the documented manual close immediately.",
                    file=sys.stderr,
                )

    if failure is not None:
        print(f"FAILURE_STAGE: {failure_stage}", file=sys.stderr)
        print(failure, file=sys.stderr)
        return 1
    assert invite is not None
    assert identity is not None
    assert receipt_path is not None
    assert release is not None
    assert browser_result is not None
    instrument = release["instrument"]
    manifest = release["manifest"]
    tested_config_timestamp = release["override_timestamp"]
    receipt = {
            "schema_version": "1.0",
            "status": PENDING_STATUS,
            "tested_config_timestamp": tested_config_timestamp,
            "site_url": SITE_URL,
            "api_url": API_URL,
            "browser_family": browser_result["browser_family"],
            "browser_version": browser_result["browser_version"],
            "hosted_version": instrument.get("hosted_version"),
            "instrument_sha256": instrument.get("instrument_sha256"),
            "deployment_manifest_sha256": manifest.get(
                "deployment_manifest_sha256"
            ),
            "source_attestation_sha256": invite["provision_file_sha256"],
            "asset_attestations": asset_attestations(release["asset_hashes"]),
            "config_override_sha256": _sha256(release["config_override"]),
            "published_config_unchanged": release.get("published_fielding", True) is True,
            "config_intercept_count": browser_result["config_intercept_count"],
            "browser_asset_count": browser_result["asset_count"],
            "assignment_count": browser_result["assignment_count"],
            "initial_submit_http_status": browser_result["initial_status"],
            "initial_submit_ok": True,
            "same_retry_http_status": browser_result["same_retry_status"],
            "same_retry_idempotent": True,
            "changed_retry_http_status": browser_result[
                "changed_retry_status"
            ],
            "changed_retry_rejected": True,
            "fragment_removed": browser_result["fragment_removed"],
            "private_inputs_cleared": browser_result[
                "private_inputs_cleared"
            ],
            "draft_cleared": browser_result["draft_cleared"],
            "javascript_exception_count": browser_result[
                "javascript_exceptions"
            ],
            "database_record_count": 1,
            "cleanup_required": True,
            "fielding_authorized": False,
            "auto_cleanup_result": cleanup_result,
        }
    try:
        write_receipt(
            receipt_path,
            receipt,
            private_values=(
                invite["invite_id"],
                invite["invite_token"],
                identity["name"],
                identity["phone"],
            ),
        )
    except (E2EError, FileExistsError, OSError, ValueError) as exc:
        print("FAILURE_STAGE: receipt-write", file=sys.stderr)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        "PENDING: remote E2E passed, but cleanup verification and finalization "
        "are still required."
    )
    print(
        "ACTION REQUIRED: delete and verify removal of disposable test records, "
        "then run finalize_remote_e2e.py."
    )
    return PENDING_EXIT_CODE


if __name__ == "__main__":
    raise SystemExit(main())
