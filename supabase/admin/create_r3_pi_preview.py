#!/usr/bin/env python3
"""Create one H3 pi_preview authorization without printing its raw link."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


H3 = "6cb5d63b5c4a764f9800bb1dd423f99b31b04d3a34854a011f0814a6550c5d41"
DOMAIN = b"bok-pilot-invite-v1\0"
SITE = "https://khdouble.github.io/bok-stance-pilot-site/"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-root", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--expires-hours", type=int, default=12)
    args = parser.parse_args()
    if not 1 <= args.expires_hours <= 24:
        raise SystemExit("--expires-hours must be 1 through 24")
    lines = args.env_file.read_text(encoding="utf-8").splitlines()
    matches = [line.split("=", 1)[1] for line in lines if line.startswith("INVITE_HMAC_SECRET_B64=")]
    if len(matches) != 1:
        raise SystemExit("protected invitation secret is unavailable")
    key = base64.b64decode(matches[0], validate=True)
    if len(key) != 32:
        raise SystemExit("protected invitation secret is invalid")
    raw = secrets.token_bytes(32)
    token = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    digest = hmac.new(key, DOMAIN + raw, hashlib.sha256).hexdigest()
    invite_id = str(uuid.uuid4())
    expires = (datetime.now(timezone.utc) + timedelta(hours=args.expires_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    private_dir = args.private_root / f"r3-pi-preview-{stamp}"
    private_dir.mkdir(parents=True, exist_ok=False)
    private_path = private_dir / "preview_link.private.json"
    private_path.write_text(json.dumps({
        "schema_version": "1.0", "purpose": "pi_preview", "instrument_sha256": H3,
        "expires_at": expires, "invite_url": f"{SITE}#preview={token}",
        "warning": "CONFIDENTIAL: one-time PI preview link; never commit or forward.",
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    migration = Path(__file__).resolve().parents[1] / "migrations" / "202609080008_seed_r3_pi_preview.sql"
    if migration.exists():
        raise SystemExit("preview migration already exists")
    migration.write_text(
        "-- One-time R3 PI preview authorization; raw token is external only.\n"
        "begin;\n"
        "insert into private.pilot_invites (invite_id, token_hmac, instrument_sha256, assignment_code, expires_at, invite_purpose) values "
        f"('{invite_id}'::uuid, decode('{digest}', 'hex'), '{H3}', 'PILOT_R01', '{expires}'::timestamptz, 'pi_preview');\n"
        "commit;\n", encoding="utf-8", newline="\n")
    print(f"Created PI preview artifacts: {private_path.name}; raw link not printed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
