#!/usr/bin/env python3
"""Decrypt a PI-only identity query export into an external protected CSV."""

from __future__ import annotations

import argparse
import base64
import binascii
import csv
import json
import os
import sys
from pathlib import Path

try:
    from .private_storage import PRIVATE_ROOT_ENV, private_child
except ImportError:  # Direct script execution from the repository root.
    from private_storage import PRIVATE_ROOT_ENV, private_child

try:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError as exc:  # pragma: no cover - depends on the administrator environment
    raise SystemExit("Install the 'cryptography' package before decrypting identity data.") from exc


INPUT_COLUMNS = (
    "participant_id",
    "submission_id",
    "assignment_code",
    "identity_ciphertext_b64",
    "identity_iv_b64",
    "identity_aad",
    "encryption_key_id",
    "consent_version",
    "consent_accepted_at",
    "submitted_at",
)
OUTPUT_COLUMNS = (
    "participant_id",
    "submission_id",
    "assignment_code",
    "name",
    "phone",
    "consent_version",
    "consent_accepted_at",
    "submitted_at",
)


def decode_key(value: str) -> bytes:
    normalized = value.strip().replace("-", "+").replace("_", "/")
    normalized += "=" * (-len(normalized) % 4)
    try:
        decoded = base64.b64decode(normalized, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("PII_ENCRYPTION_KEY_B64 is not valid base64") from exc
    if len(decoded) != 32:
        raise ValueError("PII_ENCRYPTION_KEY_B64 must encode exactly 32 bytes")
    return decoded


def path_below_private(
    path: Path,
    repository_root: Path,
    must_exist: bool,
    private_root: Path | None = None,
) -> Path:
    return private_child(
        path, repository_root, private_root, must_exist=must_exist
    )


def excel_safe(value: str) -> str:
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


def decrypt_export(
    input_path: Path,
    output_path: Path,
    repository_root: Path,
    key: bytes,
    expected_key_id: str,
    private_root: Path | None = None,
) -> int:
    source = path_below_private(
        input_path, repository_root, must_exist=True, private_root=private_root
    )
    target = path_below_private(
        output_path, repository_root, must_exist=False, private_root=private_root
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    aes = AESGCM(key)
    output_rows: list[dict[str, str]] = []
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != INPUT_COLUMNS:
            raise ValueError("ciphertext export header does not exactly match identity_ciphertext_export.sql")
        for line_number, row in enumerate(reader, start=2):
            if row["encryption_key_id"] != expected_key_id:
                raise ValueError(f"line {line_number}: encryption_key_id does not match PII_KEY_ID")
            try:
                ciphertext = base64.b64decode(row["identity_ciphertext_b64"], validate=True)
                iv = base64.b64decode(row["identity_iv_b64"], validate=True)
                plaintext = aes.decrypt(iv, ciphertext, row["identity_aad"].encode("utf-8"))
                identity = json.loads(plaintext.decode("utf-8"))
            except (ValueError, binascii.Error, UnicodeDecodeError, json.JSONDecodeError, InvalidTag) as exc:
                raise ValueError(f"line {line_number}: identity decryption or decoding failed") from exc
            if set(identity) != {"name", "phone"}:
                raise ValueError(f"line {line_number}: decrypted identity schema is invalid")
            if not all(isinstance(value, str) for value in identity.values()):
                raise ValueError(f"line {line_number}: decrypted identity fields must be text")
            output_rows.append({
                "participant_id": row["participant_id"],
                "submission_id": row["submission_id"],
                "assignment_code": row["assignment_code"],
                "name": excel_safe(identity["name"]),
                "phone": excel_safe(identity["phone"]),
                "consent_version": row["consent_version"],
                "consent_accepted_at": row["consent_accepted_at"],
                "submitted_at": row["submitted_at"],
            })
    with target.open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(output_rows)
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return len(output_rows)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--private-root",
        type=Path,
        help=f"External protected root (default: {PRIVATE_ROOT_ENV} or OS local app-data)",
    )
    result.add_argument("--input", type=Path, required=True, help="Encrypted SQL export below the external private root")
    result.add_argument("--output", type=Path, required=True, help="New decrypted CSV below the external private root")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    repository_root = Path(__file__).resolve().parents[2]
    try:
        key = decode_key(os.environ.get("PII_ENCRYPTION_KEY_B64", ""))
        key_id = os.environ.get("PII_KEY_ID", "").strip()
        if not key_id:
            raise ValueError("PII_KEY_ID is required")
        row_count = decrypt_export(
            args.input,
            args.output,
            repository_root,
            key,
            key_id,
            private_root=args.private_root,
        )
    except (ValueError, FileExistsError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"Decrypted {row_count} identity rows to {args.output.resolve()} (contents not printed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
