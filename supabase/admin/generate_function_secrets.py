#!/usr/bin/env python3
"""Generate and validate the seven custom secrets used by the pilot Edge Function.

The hosted Supabase runtime supplies SUPABASE_DB_URL itself. This tool therefore
writes only project-specific values and never prints their contents.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hmac
import json
import os
import re
import secrets
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Callable, Mapping

try:
    from .private_storage import ensure_private_root, private_child
except ImportError:  # Direct script execution from the repository root.
    from private_storage import ensure_private_root, private_child


CUSTOM_SECRET_NAMES = (
    "PILOT_INSTRUMENT_SHA256",
    "PILOT_INSTRUMENT_VERSION",
    "PILOT_CONSENT_VERSION",
    "INVITE_HMAC_SECRET_B64",
    "IDENTITY_HMAC_SECRET_B64",
    "PII_ENCRYPTION_KEY_B64",
    "PII_KEY_ID",
)
KEY_NAMES = (
    "INVITE_HMAC_SECRET_B64",
    "IDENTITY_HMAC_SECRET_B64",
    "PII_ENCRYPTION_KEY_B64",
)
DEFAULT_FILENAME = "pilot-function.env"
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
CONSENT_VERSION_RE = re.compile(
    r"^consent-v(?P<date>\d{4}-\d{2}-\d{2})-r[1-9]\d*$"
)
KEY_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
PLACEHOLDER_RE = re.compile(
    r"(?:PENDING|TBD|TODO|PLACEHOLDER|EXAMPLE|미정|추후|예시|테스트)",
    re.IGNORECASE,
)


def load_release_identity(repository_root: Path) -> tuple[str, str]:
    instrument_path = repository_root / "docs" / "instrument.json"
    try:
        instrument = json.loads(instrument_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("cannot read the canonical docs/instrument.json") from exc
    instrument_hash = instrument.get("instrument_sha256")
    instrument_version = instrument.get("hosted_version")
    if not isinstance(instrument_hash, str) or not HASH_RE.fullmatch(
        instrument_hash
    ):
        raise ValueError("canonical instrument_sha256 is invalid")
    if (
        not isinstance(instrument_version, str)
        or not 1 <= len(instrument_version) <= 80
        or any(ord(character) < 32 for character in instrument_version)
    ):
        raise ValueError("canonical hosted_version is invalid")
    return instrument_hash, instrument_version


def validate_consent_version(value: str) -> str:
    match = CONSENT_VERSION_RE.fullmatch(value)
    if match is None or PLACEHOLDER_RE.search(value):
        raise ValueError(
            "consent version must match consent-vYYYY-MM-DD-rN without placeholders"
        )
    try:
        parsed = date.fromisoformat(match.group("date"))
    except ValueError as exc:
        raise ValueError("consent version contains an invalid calendar date") from exc
    if parsed.isoformat() != match.group("date"):
        raise ValueError("consent version contains an invalid calendar date")
    return value


def validate_key_id(value: str) -> str:
    if not KEY_ID_RE.fullmatch(value) or PLACEHOLDER_RE.search(value):
        raise ValueError(
            "PII key ID must contain 1-64 letters, digits, dots, underscores, or hyphens"
        )
    return value


def decode_secret_256(value: str, field: str) -> bytes:
    normalized = value.strip().replace("-", "+").replace("_", "/")
    normalized += "=" * (-len(normalized) % 4)
    try:
        decoded = base64.b64decode(normalized, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError(f"{field} is not valid base64") from exc
    if len(decoded) != 32:
        raise ValueError(f"{field} must encode exactly 32 bytes")
    return decoded


def generate_secret_values(
    repository_root: Path,
    consent_version: str,
    pii_key_id: str,
    *,
    random_bytes: Callable[[int], bytes] = secrets.token_bytes,
) -> dict[str, str]:
    instrument_hash, instrument_version = load_release_identity(repository_root)
    consent_version = validate_consent_version(consent_version)
    pii_key_id = validate_key_id(pii_key_id)

    key_material: list[bytes] = []
    attempts = 0
    while len(key_material) < len(KEY_NAMES):
        attempts += 1
        if attempts > 128:
            raise ValueError("secure random source repeatedly returned duplicate keys")
        candidate = random_bytes(32)
        if not isinstance(candidate, bytes) or len(candidate) != 32:
            raise ValueError("secure random source did not return exactly 32 bytes")
        if any(hmac.compare_digest(candidate, existing) for existing in key_material):
            continue
        key_material.append(candidate)

    values = {
        "PILOT_INSTRUMENT_SHA256": instrument_hash,
        "PILOT_INSTRUMENT_VERSION": instrument_version,
        "PILOT_CONSENT_VERSION": consent_version,
        "INVITE_HMAC_SECRET_B64": base64.b64encode(key_material[0]).decode("ascii"),
        "IDENTITY_HMAC_SECRET_B64": base64.b64encode(key_material[1]).decode("ascii"),
        "PII_ENCRYPTION_KEY_B64": base64.b64encode(key_material[2]).decode("ascii"),
        "PII_KEY_ID": pii_key_id,
    }
    validate_secret_values(values, repository_root)
    return values


def validate_secret_values(
    values: Mapping[str, str], repository_root: Path
) -> None:
    expected_names = set(CUSTOM_SECRET_NAMES)
    actual_names = set(values)
    missing = sorted(expected_names - actual_names)
    unexpected = sorted(actual_names - expected_names)
    if missing:
        raise ValueError("custom secret file is missing names: " + ", ".join(missing))
    if unexpected:
        raise ValueError(
            "custom secret file contains unexpected names: " + ", ".join(unexpected)
        )

    instrument_hash, instrument_version = load_release_identity(repository_root)
    if values["PILOT_INSTRUMENT_SHA256"] != instrument_hash:
        raise ValueError("PILOT_INSTRUMENT_SHA256 does not match docs/instrument.json")
    if values["PILOT_INSTRUMENT_VERSION"] != instrument_version:
        raise ValueError("PILOT_INSTRUMENT_VERSION does not match docs/instrument.json")
    validate_consent_version(values["PILOT_CONSENT_VERSION"])
    validate_key_id(values["PII_KEY_ID"])

    decoded = [
        decode_secret_256(values[name], name)
        for name in KEY_NAMES
    ]
    for index, left in enumerate(decoded):
        for right in decoded[index + 1:]:
            if hmac.compare_digest(left, right):
                raise ValueError("the three cryptographic keys must be mutually distinct")


def serialize_env(values: Mapping[str, str]) -> str:
    validate_line_values(values)
    return "".join(f"{name}={values[name]}\n" for name in CUSTOM_SECRET_NAMES)


def validate_line_values(values: Mapping[str, str]) -> None:
    for name, value in values.items():
        if (
            not isinstance(value, str)
            or value != value.strip()
            or not value
            or "\n" in value
            or "\r" in value
            or "\x00" in value
        ):
            raise ValueError(f"{name} cannot be represented safely in an env file")


def parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"env line {line_number} is missing '='")
        name, value = line.split("=", 1)
        if name != name.strip() or value != value.strip():
            raise ValueError(f"env line {line_number} has surrounding whitespace")
        if name in values:
            raise ValueError(f"env file defines {name} more than once")
        if name not in CUSTOM_SECRET_NAMES:
            raise ValueError(f"env file contains unexpected name: {name}")
        values[name] = value
    validate_line_values(values)
    return values


def resolve_secret_path(
    repository_root: Path,
    private_root: Path | None,
    supplied_path: Path | None,
    *,
    must_exist: bool,
) -> Path:
    root = ensure_private_root(repository_root, private_root)
    candidate = supplied_path if supplied_path is not None else root / DEFAULT_FILENAME
    return private_child(
        candidate,
        repository_root,
        root,
        must_exist=must_exist,
    )


def atomic_write(path: Path, text: str, *, overwrite: bool) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite existing path: {path}")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temporary_path.chmod(0o600)
        except OSError:
            pass

        if overwrite:
            os.replace(temporary_path, path)
        else:
            try:
                os.link(temporary_path, path)
            except FileExistsError as exc:
                raise FileExistsError(
                    f"refusing to overwrite existing path: {path}"
                ) from exc
            temporary_path.unlink()
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def generate_file(
    repository_root: Path,
    private_root: Path | None,
    output: Path | None,
    consent_version: str,
    pii_key_id: str,
    *,
    overwrite: bool,
) -> Path:
    root = ensure_private_root(repository_root, private_root)
    candidate = output if output is not None else root / DEFAULT_FILENAME
    target = private_child(
        candidate,
        repository_root,
        root,
        must_exist=candidate.exists(),
    )
    if target.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite existing path: {target}")
    values = generate_secret_values(
        repository_root, consent_version, pii_key_id
    )
    atomic_write(target, serialize_env(values), overwrite=overwrite)
    validate_file(repository_root, private_root, target)
    return target


def validate_file(
    repository_root: Path,
    private_root: Path | None,
    input_path: Path | None,
) -> Path:
    target = resolve_secret_path(
        repository_root, private_root, input_path, must_exist=True
    )
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError("cannot read the custom secret file") from exc
    values = parse_env(text)
    validate_secret_values(values, repository_root)
    return target


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subparsers = result.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser(
        "generate", help="create a new validated custom-secret env file"
    )
    generate.add_argument("--private-root", type=Path)
    generate.add_argument(
        "--output",
        type=Path,
        help=(
            "output below the external private root "
            f"(default: private root/{DEFAULT_FILENAME})"
        ),
    )
    generate.add_argument("--consent-version", required=True)
    generate.add_argument("--pii-key-id", default="pilot-pii-v1")
    generate.add_argument(
        "--overwrite",
        action="store_true",
        help="atomically replace an existing file and rotate all three keys",
    )

    validate = subparsers.add_parser(
        "validate", help="validate names and formats without printing values"
    )
    validate.add_argument("--private-root", type=Path)
    validate.add_argument(
        "--input",
        type=Path,
        help=(
            "existing file below the external private root "
            f"(default: private root/{DEFAULT_FILENAME})"
        ),
    )
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    repository_root = Path(__file__).resolve().parents[2]
    try:
        if args.command == "generate":
            path = generate_file(
                repository_root,
                args.private_root,
                args.output,
                args.consent_version,
                args.pii_key_id,
                overwrite=args.overwrite,
            )
            print(
                "Created and validated 7 custom Edge Function secrets "
                f"without displaying values: {path}"
            )
        else:
            path = validate_file(
                repository_root,
                args.private_root,
                args.input,
            )
            print(
                "Validated 7 custom Edge Function secrets "
                f"without displaying values: {path}"
            )
    except (FileExistsError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
