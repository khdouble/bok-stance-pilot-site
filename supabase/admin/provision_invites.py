#!/usr/bin/env python3
"""Create one-time pilot invite tokens without exposing them in Git or stdout."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

try:
    from .private_storage import PRIVATE_ROOT_ENV, private_child
except ImportError:  # Direct script execution from the repository root.
    from private_storage import PRIVATE_ROOT_ENV, private_child


HMAC_DOMAIN = b"bok-pilot-invite-v1\0"
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
ASSIGNMENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
DEFAULT_ASSIGNMENTS = tuple(f"PILOT_R{number:02d}" for number in range(1, 6))
DEFAULT_SITE_URL = "https://khdouble.github.io/bok-stance-pilot-site/"
EXPECTED_SITE_SCHEME = "https"
EXPECTED_SITE_HOST = "khdouble.github.io"
EXPECTED_SITE_PATH = "/bok-stance-pilot-site/"


def decode_key(value: str) -> bytes:
    normalized = value.strip().replace("-", "+").replace("_", "/")
    normalized += "=" * (-len(normalized) % 4)
    try:
        decoded = base64.b64decode(normalized, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("INVITE_HMAC_SECRET_B64 is not valid base64") from exc
    if len(decoded) != 32:
        raise ValueError("INVITE_HMAC_SECRET_B64 must encode exactly 32 bytes")
    return decoded


def parse_expiry(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("--expires-at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("--expires-at must include a timezone")
    parsed = parsed.astimezone(timezone.utc)
    if parsed <= datetime.now(timezone.utc):
        raise ValueError("--expires-at must be in the future")
    return parsed


def ensure_private_output(
    output: Path, repository_root: Path, private_root: Path | None = None
) -> Path:
    return private_child(
        output, repository_root, private_root, must_exist=False
    )


def validate_site_url(site_url: str) -> str:
    """Accept only the canonical GitHub Pages project URL."""
    try:
        parsed = urlsplit(site_url)
        parsed_port = parsed.port
    except ValueError as exc:
        raise ValueError("--site-url is malformed") from exc
    if (
        site_url != DEFAULT_SITE_URL
        or parsed.scheme != EXPECTED_SITE_SCHEME
        or parsed.netloc != EXPECTED_SITE_HOST
        or parsed.hostname != EXPECTED_SITE_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed_port is not None
        or parsed.path != EXPECTED_SITE_PATH
        or parsed.query
        or parsed.fragment
        or "?" in site_url
        or "#" in site_url
    ):
        raise ValueError(f"--site-url must exactly equal {DEFAULT_SITE_URL}")
    return DEFAULT_SITE_URL


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def provision(
    output: Path,
    repository_root: Path,
    instrument_sha256: str,
    assignment_codes: tuple[str, ...],
    expires_at: datetime,
    site_url: str,
    hmac_key: bytes,
    private_root: Path | None = None,
) -> tuple[Path, Path]:
    if not HASH_RE.fullmatch(instrument_sha256):
        raise ValueError("--instrument-sha256 must be a lowercase SHA-256 digest")
    if len(assignment_codes) != 5 or len(set(assignment_codes)) != 5:
        raise ValueError("exactly five unique --assignment-code values are required")
    if any(not ASSIGNMENT_RE.fullmatch(code) for code in assignment_codes):
        raise ValueError("assignment codes may contain only letters, digits, underscore, and hyphen")
    canonical_site_url = validate_site_url(site_url)

    target = ensure_private_output(output, repository_root, private_root)
    target.mkdir(mode=0o700, parents=True, exist_ok=False)
    expiry_text = expires_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    created_text = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    invites: list[dict[str, str]] = []
    sql_rows: list[str] = []
    for assignment_code in assignment_codes:
        raw_token = secrets.token_bytes(32)
        token_text = base64.urlsafe_b64encode(raw_token).decode("ascii").rstrip("=")
        token_hmac = hmac.new(hmac_key, HMAC_DOMAIN + raw_token, hashlib.sha256).hexdigest()
        invite_id = str(uuid.uuid4())
        invite_url = f"{canonical_site_url}#invite={quote(token_text, safe='')}"
        invites.append({
            "assignment_code": assignment_code,
            "invite_token": token_text,
            "invite_url": invite_url,
            "expires_at": expiry_text,
        })
        sql_rows.append(
            "  ("
            + ", ".join([
                f"{sql_literal(invite_id)}::uuid",
                f"decode({sql_literal(token_hmac)}, 'hex')",
                sql_literal(instrument_sha256),
                sql_literal(assignment_code),
                f"{sql_literal(expiry_text)}::timestamptz",
            ])
            + ")"
        )

    private_path = target / "invite_links.private.json"
    seed_path = target / "invite_seed.private.sql"
    private_payload = {
        "schema_version": "1.0",
        "created_at": created_text,
        "instrument_sha256": instrument_sha256,
        "warning": "CONFIDENTIAL: contains raw one-time invitation tokens; never commit or transmit as a batch.",
        "invites": invites,
    }
    private_path.write_text(json.dumps(private_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    seed_path.write_text(
        "begin;\n\n"
        "insert into private.pilot_invites (\n"
        "  invite_id, token_hmac, instrument_sha256, assignment_code, expires_at\n"
        ") values\n"
        + ",\n".join(sql_rows)
        + ";\n\ncommit;\n",
        encoding="utf-8",
    )
    try:
        private_path.chmod(0o600)
        seed_path.chmod(0o600)
    except OSError:
        pass
    return private_path, seed_path


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--private-root",
        type=Path,
        help=f"External protected root (default: {PRIVATE_ROOT_ENV} or OS local app-data)",
    )
    result.add_argument("--output", type=Path, required=True, help="New batch directory below the external private root")
    result.add_argument("--instrument-sha256", required=True)
    result.add_argument("--expires-at", required=True, help="ISO-8601 timestamp with timezone")
    result.add_argument("--site-url", default=DEFAULT_SITE_URL)
    result.add_argument("--assignment-code", action="append", dest="assignment_codes")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    repository_root = Path(__file__).resolve().parents[2]
    secret_value = os.environ.get("INVITE_HMAC_SECRET_B64", "")
    try:
        hmac_key = decode_key(secret_value)
        assignment_codes = tuple(args.assignment_codes or DEFAULT_ASSIGNMENTS)
        private_path, seed_path = provision(
            output=args.output,
            repository_root=repository_root,
            instrument_sha256=args.instrument_sha256,
            assignment_codes=assignment_codes,
            expires_at=parse_expiry(args.expires_at),
            site_url=args.site_url,
            hmac_key=hmac_key,
            private_root=args.private_root,
        )
    except (ValueError, FileExistsError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"Created 5 private invites in {private_path.parent}")
    print(f"Raw links: {private_path.name} (not printed; do not commit)")
    print(f"Database seed: {seed_path.name} (contains HMAC digests only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
