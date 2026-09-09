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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

try:
    from .private_storage import PRIVATE_ROOT_ENV, private_child
    from .linked_cli_backend import (
        LinkedCliBackend,
        LinkedCliAmbiguousOutcome,
        LinkedCliError,
        canonical_admin_seed,
        canonical_seed,
    )
except ImportError:  # Direct script execution from the repository root.
    from private_storage import PRIVATE_ROOT_ENV, private_child
    from linked_cli_backend import (
        LinkedCliBackend,
        LinkedCliAmbiguousOutcome,
        LinkedCliError,
        canonical_admin_seed,
        canonical_seed,
    )


HMAC_DOMAIN = b"bok-pilot-invite-v1\0"
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
ASSIGNMENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
DEFAULT_ASSIGNMENTS = tuple(f"PILOT_R{number:02d}" for number in range(1, 6))
DEFAULT_SITE_URL = "https://khdouble.github.io/bok-stance-pilot-site/"
EXPECTED_SITE_SCHEME = "https"
EXPECTED_SITE_HOST = "khdouble.github.io"
EXPECTED_SITE_PATH = "/bok-stance-pilot-site/"
PRODUCTION_MODE = 'production'
DISPOSABLE_E2E_MODE = "disposable-e2e"
PI_MANUAL_TEST_MODE = 'pi-manual-test'
PROVISIONING_MODES = (
    PRODUCTION_MODE,
    DISPOSABLE_E2E_MODE,
    PI_MANUAL_TEST_MODE,
)
DISPOSABLE_MAX_LIFETIME = timedelta(hours=24)
PI_MANUAL_MAX_LIFETIME = timedelta(hours=24)
ADMIN_ID_HMAC_DOMAIN = b'bok-pilot-admin-id-v1\0'
ADMIN_PASSWORD_HMAC_DOMAIN = b'bok-pilot-admin-password-v1\0'
ADMIN_ID_ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
PI_PRIVATE_WARNING = (
    'CONFIDENTIAL: contains a raw PI manual-test ID and password; '
    'never commit or print either value.'
)
SEED_VIA_NONE = "none"
SEED_VIA_LINKED_CLI = "linked-cli"
SEED_VIA_CHOICES = (SEED_VIA_NONE, SEED_VIA_LINKED_CLI)
AMBIGUOUS_COUNT_KEYS = (
    "invite_count",
    "unrevoked_invite_count",
    "eligible_unused_invite_count",
    "used_invite_count",
    "submission_count",
    "identity_count",
    "response_count",
)
PRIVATE_WARNING = (
    "CONFIDENTIAL: contains raw one-time invitation tokens; "
    "never commit or transmit as a batch."
)


def linked_seed_confirmation(
    instrument_sha256: str,
    invite_id: str,
) -> str:
    if not HASH_RE.fullmatch(instrument_sha256):
        raise ValueError("linked seed instrument hash is invalid")
    try:
        canonical_invite = str(uuid.UUID(invite_id))
    except (ValueError, AttributeError) as exc:
        raise ValueError("linked seed invitation UUID is invalid") from exc
    if canonical_invite != invite_id:
        raise ValueError("linked seed invitation UUID is invalid")
    return (
        "SEED ONE DISPOSABLE E2E INVITE "
        f"{instrument_sha256} {canonical_invite}"
    )


def linked_pi_seed_confirmation(
    instrument_sha256: str,
    invite_id: str,
) -> str:
    if not HASH_RE.fullmatch(instrument_sha256):
        raise ValueError('linked PI seed instrument hash is invalid')
    try:
        canonical_invite = str(uuid.UUID(invite_id))
    except (ValueError, AttributeError) as exc:
        raise ValueError('linked PI seed invitation UUID is invalid') from exc
    if canonical_invite != invite_id:
        raise ValueError('linked PI seed invitation UUID is invalid')
    return (
        'SEED ONE PI MANUAL TEST CREDENTIAL '
        f'{instrument_sha256} {canonical_invite}'
    )


def render_pi_manual_seed_sql(values: dict[str, str]) -> str:
    return (
        '-- PI MANUAL TEST CREDENTIAL; digest-only database seed.\n'
        'begin;\n\n'
        'insert into private.pilot_invites (\n'
        '  invite_id, token_hmac, admin_id_hmac, instrument_sha256,\n'
        '  assignment_code, expires_at, invite_purpose\n'
        ') values (\n'
        f'''  {sql_literal(values['invite_id'])}::uuid,\n'''
        f'''  decode({sql_literal(values['admin_password_hmac'])}, 'hex'),\n'''
        f'''  decode({sql_literal(values['admin_id_hmac'])}, 'hex'),\n'''
        f'''  {sql_literal(values['instrument_sha256'])},\n'''
        f'''  {sql_literal(values['assignment_code'])},\n'''
        f'''  {sql_literal(values['expires_at'])}::timestamptz,\n'''
        '''  'pi_manual_test'\n'''
        ');\n\ncommit;\n'
    )


def apply_linked_pi_seed(
    seed_mapping: dict[str, str],
    supplied_confirmation: str,
    backend: object,
) -> None:
    confirmation = linked_pi_seed_confirmation(
        seed_mapping['instrument_sha256'],
        seed_mapping['invite_id'],
    )
    if not hmac.compare_digest(supplied_confirmation, confirmation):
        raise ValueError('linked PI seed confirmation did not match')
    seed_method = getattr(backend, 'seed_pi_manual_test', None)
    if not callable(seed_method):
        raise ValueError('linked PI seed backend is invalid')
    seed_method(dict(seed_mapping))


def load_release_version(
    repository_root: Path,
    instrument_sha256: str,
) -> str:
    try:
        payload = json.loads(
            (repository_root / "docs" / "instrument.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("public instrument identity is invalid") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("instrument_sha256") != instrument_sha256
        or not isinstance(payload.get("hosted_version"), str)
    ):
        raise ValueError("public instrument identity is invalid")
    version = payload["hosted_version"]
    if not re.fullmatch(
        r"^v[0-9]{6}-(?:pilot-hosted|r5-public)-[1-9][0-9]*$",
        version,
    ):
        raise ValueError("public instrument identity is invalid")
    return version


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


def render_seed_sql(sql_rows: list[str], mode: str) -> str:
    return (
        (
            "-- DISPOSABLE E2E INVITE; revoke and delete test data before production fielding.\n"
            if mode == DISPOSABLE_E2E_MODE
            else "-- PRODUCTION INVITATIONS; confidential operational material.\n"
        )
        + "begin;\n\n"
        "insert into private.pilot_invites (\n"
        "  invite_id, token_hmac, instrument_sha256, assignment_code, "
        "expires_at, invite_purpose\n"
        ") values\n"
        + ",\n".join(sql_rows)
        + ";\n\ncommit;\n"
    )


def validate_linked_seed_artifacts(
    private_path: Path,
    seed_path: Path,
    repository_root: Path,
    instrument_sha256: str,
    assignment_codes: tuple[str, ...],
    expires_at: datetime,
    site_url: str,
    hmac_key: bytes,
    *,
    now: datetime | None = None,
) -> dict[str, str]:
    if assignment_codes != (DEFAULT_ASSIGNMENTS[0],):
        raise ValueError(
            "linked-cli seeding requires disposable PILOT_R01"
        )
    try:
        private_resolved = private_path.resolve(strict=True)
        seed_resolved = seed_path.resolve(strict=True)
        repository_resolved = repository_root.resolve(strict=True)
    except OSError as exc:
        raise ValueError("private invitation artifacts are invalid") from exc
    if (
        private_resolved.is_relative_to(repository_resolved)
        or seed_resolved.is_relative_to(repository_resolved)
        or private_resolved.name != "invite_links.private.json"
        or seed_resolved.name != "invite_seed.private.sql"
        or private_resolved.parent != seed_resolved.parent
    ):
        raise ValueError("private invitation artifacts are invalid")
    try:
        payload = json.loads(private_resolved.read_text(encoding="utf-8"))
        actual_sql = seed_resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("private invitation artifacts are invalid") from exc
    if (
        not isinstance(payload, dict)
        or set(payload)
        != {
            "schema_version",
            "provisioning_mode",
            "created_at",
            "instrument_sha256",
            "warning",
            "invites",
        }
        or payload["schema_version"] != "1.0"
        or payload["provisioning_mode"] != DISPOSABLE_E2E_MODE
        or payload["instrument_sha256"] != instrument_sha256
        or payload["warning"] != PRIVATE_WARNING
        or not isinstance(payload["created_at"], str)
        or not isinstance(payload["invites"], list)
        or len(payload["invites"]) != 1
    ):
        raise ValueError("private invitation artifacts are invalid")
    try:
        created = datetime.fromisoformat(
            payload["created_at"].replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ValueError("private invitation artifacts are invalid") from exc
    if created.tzinfo is None or created.utcoffset() is None:
        raise ValueError("private invitation artifacts are invalid")
    invite = payload["invites"][0]
    if (
        not isinstance(invite, dict)
        or set(invite)
        != {
            "invite_id",
            "assignment_code",
            "invite_token",
            "invite_url",
            "expires_at",
        }
        or invite["assignment_code"] != assignment_codes[0]
        or invite["expires_at"]
        != expires_at.astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        or not isinstance(invite["invite_token"], str)
        or not isinstance(invite["invite_url"], str)
    ):
        raise ValueError("private invitation artifacts are invalid")
    invite_id = invite["invite_id"]
    confirmation = linked_seed_confirmation(
        instrument_sha256, invite_id
    )
    del confirmation
    token_text = invite["invite_token"]
    try:
        raw_token = base64.b64decode(
            token_text + "=" * (-len(token_text) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, binascii.Error) as exc:
        raise ValueError("private invitation artifacts are invalid") from exc
    if (
        len(raw_token) != 32
        or base64.urlsafe_b64encode(raw_token)
        .decode("ascii")
        .rstrip("=")
        != token_text
        or invite["invite_url"]
        != f"{validate_site_url(site_url)}#invite={quote(token_text, safe='')}"
    ):
        raise ValueError("private invitation artifacts are invalid")
    token_hmac = hmac.new(
        hmac_key, HMAC_DOMAIN + raw_token, hashlib.sha256
    ).hexdigest()
    expiry_text = invite["expires_at"]
    expected_row = (
        "  ("
        + ", ".join(
            [
                f"{sql_literal(invite_id)}::uuid",
                f"decode({sql_literal(token_hmac)}, 'hex')",
                sql_literal(instrument_sha256),
                sql_literal(assignment_codes[0]),
                f"{sql_literal(expiry_text)}::timestamptz",
                sql_literal('disposable_e2e'),
            ]
        )
        + ")"
    )
    if actual_sql != render_seed_sql(
        [expected_row], DISPOSABLE_E2E_MODE
    ):
        raise ValueError("private invitation artifacts are invalid")
    mapping = {
        "invite_id": invite_id,
        "invite_hmac": token_hmac,
        "instrument_sha256": instrument_sha256,
        "instrument_version": load_release_version(
            repository_root, instrument_sha256
        ),
        "assignment_set_id": assignment_codes[0],
        "assignment_code": assignment_codes[0],
        "expires_at": expiry_text,
    }
    return canonical_seed(
        mapping, now=now or datetime.now(timezone.utc)
    )


def apply_linked_seed(
    seed_mapping: dict[str, str],
    supplied_confirmation: str,
    backend: object,
) -> None:
    confirmation = linked_seed_confirmation(
        seed_mapping["instrument_sha256"],
        seed_mapping["invite_id"],
    )
    if not hmac.compare_digest(supplied_confirmation, confirmation):
        raise ValueError("linked seed confirmation did not match")
    seed_method = getattr(backend, "seed_disposable", None)
    if not callable(seed_method):
        raise ValueError("linked seed backend is invalid")
    seed_method(dict(seed_mapping))


def provision(
    output: Path,
    repository_root: Path,
    instrument_sha256: str,
    assignment_codes: tuple[str, ...],
    expires_at: datetime,
    site_url: str,
    hmac_key: bytes,
    private_root: Path | None = None,
    mode: str = PRODUCTION_MODE,
) -> tuple[Path, Path]:
    if not HASH_RE.fullmatch(instrument_sha256):
        raise ValueError("--instrument-sha256 must be a lowercase SHA-256 digest")
    if mode not in PROVISIONING_MODES:
        raise ValueError("provisioning mode is invalid")
    expected_count = 5 if mode == PRODUCTION_MODE else 1
    if (
        len(assignment_codes) != expected_count
        or len(set(assignment_codes)) != expected_count
    ):
        if mode == PRODUCTION_MODE:
            raise ValueError(
                "production mode requires exactly five unique --assignment-code values"
            )
        raise ValueError(
            "disposable-e2e mode requires exactly one --assignment-code value"
        )
    if any(not ASSIGNMENT_RE.fullmatch(code) for code in assignment_codes):
        raise ValueError("assignment codes may contain only letters, digits, underscore, and hyphen")
    now = datetime.now(timezone.utc)
    if expires_at <= now:
        raise ValueError("invitation expiry must be in the future")
    if (
        mode == DISPOSABLE_E2E_MODE
        and expires_at - now > DISPOSABLE_MAX_LIFETIME
    ):
        raise ValueError("disposable-e2e invitations must expire within 24 hours")
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
            "invite_id": invite_id,
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
                sql_literal(
                    'disposable_e2e'
                    if mode == DISPOSABLE_E2E_MODE
                    else 'participant'
                ),
            ])
            + ")"
        )

    private_path = target / "invite_links.private.json"
    seed_path = target / "invite_seed.private.sql"
    private_payload = {
        "schema_version": "1.0",
        "provisioning_mode": mode,
        "created_at": created_text,
        "instrument_sha256": instrument_sha256,
        "warning": PRIVATE_WARNING,
        "invites": invites,
    }
    private_path.write_text(json.dumps(private_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    seed_path.write_text(
        render_seed_sql(sql_rows, mode),
        encoding="utf-8",
    )
    try:
        private_path.chmod(0o600)
        seed_path.chmod(0o600)
    except OSError:
        pass
    return private_path, seed_path


def validate_pi_manual_artifacts(
    credential_path: Path,
    seed_path: Path,
    repository_root: Path,
    instrument_sha256: str,
    expires_at: datetime,
    hmac_key: bytes,
    *,
    now: datetime | None = None,
) -> dict[str, str]:
    current = now or datetime.now(timezone.utc)
    if (
        current.tzinfo is None
        or current.utcoffset() is None
        or expires_at.tzinfo is None
        or expires_at.utcoffset() is None
    ):
        raise ValueError('PI credential artifacts are invalid')
    current = current.astimezone(timezone.utc)
    expected_expiry = expires_at.astimezone(timezone.utc)
    try:
        credential_resolved = credential_path.resolve(strict=True)
        seed_resolved = seed_path.resolve(strict=True)
        repository_resolved = repository_root.resolve(strict=True)
    except OSError as exc:
        raise ValueError('PI credential artifacts are invalid') from exc
    if (
        credential_resolved.is_relative_to(repository_resolved)
        or seed_resolved.is_relative_to(repository_resolved)
        or credential_resolved.name != 'pi_manual_test.private.json'
        or seed_resolved.name != 'pi_manual_test_seed.private.sql'
        or credential_resolved.parent != seed_resolved.parent
    ):
        raise ValueError('PI credential artifacts are invalid')
    try:
        payload = json.loads(
            credential_resolved.read_text(encoding='utf-8')
        )
        actual_sql = seed_resolved.read_text(encoding='utf-8')
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('PI credential artifacts are invalid') from exc
    expected_keys = {
        'schema_version', 'credential_type', 'created_at',
        'instrument_sha256', 'assignment_code', 'expires_at',
        'warning', 'credential', 'invite_id',
    }
    if (
        not isinstance(payload, dict)
        or set(payload) != expected_keys
        or payload['schema_version'] != '1.0'
        or payload['credential_type'] != 'pi_manual_test'
        or payload['instrument_sha256'] != instrument_sha256
        or payload['assignment_code'] != DEFAULT_ASSIGNMENTS[0]
        or payload['warning'] != PI_PRIVATE_WARNING
        or payload['expires_at']
        != expected_expiry.isoformat().replace(
            '+00:00', 'Z'
        )
        or not isinstance(payload['credential'], dict)
        or set(payload['credential']) != {'admin_id', 'admin_password'}
    ):
        raise ValueError('PI credential artifacts are invalid')
    try:
        created_at = datetime.fromisoformat(
            payload['created_at'].replace('Z', '+00:00')
        )
    except (AttributeError, ValueError) as exc:
        raise ValueError('PI credential artifacts are invalid') from exc
    if (
        created_at.tzinfo is None
        or created_at.utcoffset() is None
        or created_at.astimezone(timezone.utc).isoformat().replace(
            '+00:00', 'Z'
        ) != payload['created_at']
        or created_at.astimezone(timezone.utc) > current
        or created_at.astimezone(timezone.utc) >= expected_expiry
    ):
        raise ValueError('PI credential artifacts are invalid')
    admin_id = payload['credential']['admin_id']
    password = payload['credential']['admin_password']
    if (
        not isinstance(admin_id, str)
        or re.fullmatch(r'PI-[A-Z0-9]{12}', admin_id) is None
        or not isinstance(password, str)
    ):
        raise ValueError('PI credential artifacts are invalid')
    try:
        raw_password = base64.b64decode(
            password + '=' * (-len(password) % 4),
            altchars=b'-_',
            validate=True,
        )
    except (ValueError, binascii.Error) as exc:
        raise ValueError('PI credential artifacts are invalid') from exc
    if (
        len(raw_password) != 32
        or base64.urlsafe_b64encode(raw_password)
        .decode('ascii')
        .rstrip('=') != password
    ):
        raise ValueError('PI credential artifacts are invalid')
    values = {
        'invite_id': payload['invite_id'],
        'admin_id_hmac': hmac.new(
            hmac_key,
            ADMIN_ID_HMAC_DOMAIN + admin_id.encode('ascii'),
            hashlib.sha256,
        ).hexdigest(),
        'admin_password_hmac': hmac.new(
            hmac_key,
            ADMIN_PASSWORD_HMAC_DOMAIN + raw_password,
            hashlib.sha256,
        ).hexdigest(),
        'instrument_sha256': instrument_sha256,
        'instrument_version': load_release_version(
            repository_root, instrument_sha256
        ),
        'assignment_code': DEFAULT_ASSIGNMENTS[0],
        'expires_at': payload['expires_at'],
    }
    canonical = canonical_admin_seed(
        values, now=current
    )
    if actual_sql != render_pi_manual_seed_sql(canonical):
        raise ValueError('PI credential artifacts are invalid')
    return canonical


def provision_pi_manual_test(
    output: Path,
    repository_root: Path,
    instrument_sha256: str,
    expires_at: datetime,
    hmac_key: bytes,
    private_root: Path | None = None,
) -> tuple[Path, Path]:
    if not HASH_RE.fullmatch(instrument_sha256):
        raise ValueError(
            '--instrument-sha256 must be a lowercase SHA-256 digest'
        )
    now = datetime.now(timezone.utc)
    if expires_at <= now or expires_at - now > PI_MANUAL_MAX_LIFETIME:
        raise ValueError(
            'PI manual-test credentials must expire within 24 hours'
        )
    target = ensure_private_output(output, repository_root, private_root)
    target.mkdir(mode=0o700, parents=True, exist_ok=False)
    raw_password = secrets.token_bytes(32)
    password = base64.urlsafe_b64encode(raw_password).decode(
        'ascii'
    ).rstrip('=')
    admin_id = 'PI-' + ''.join(
        secrets.choice(ADMIN_ID_ALPHABET) for _ in range(12)
    )
    invite_id = str(uuid.uuid4())
    expiry_text = expires_at.astimezone(timezone.utc).isoformat().replace(
        '+00:00', 'Z'
    )
    created_text = now.isoformat().replace('+00:00', 'Z')
    values = {
        'invite_id': invite_id,
        'admin_id_hmac': hmac.new(
            hmac_key,
            ADMIN_ID_HMAC_DOMAIN + admin_id.encode('ascii'),
            hashlib.sha256,
        ).hexdigest(),
        'admin_password_hmac': hmac.new(
            hmac_key,
            ADMIN_PASSWORD_HMAC_DOMAIN + raw_password,
            hashlib.sha256,
        ).hexdigest(),
        'instrument_sha256': instrument_sha256,
        'instrument_version': load_release_version(
            repository_root, instrument_sha256
        ),
        'assignment_code': DEFAULT_ASSIGNMENTS[0],
        'expires_at': expiry_text,
    }
    canonical_admin_seed(values, now=now)
    credential_path = target / 'pi_manual_test.private.json'
    seed_path = target / 'pi_manual_test_seed.private.sql'
    payload = {
        'schema_version': '1.0',
        'credential_type': 'pi_manual_test',
        'created_at': created_text,
        'instrument_sha256': instrument_sha256,
        'assignment_code': DEFAULT_ASSIGNMENTS[0],
        'expires_at': expiry_text,
        'warning': PI_PRIVATE_WARNING,
        'credential': {
            'admin_id': admin_id,
            'admin_password': password,
        },
        'invite_id': invite_id,
    }
    credential_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    seed_path.write_text(
        render_pi_manual_seed_sql(values),
        encoding='utf-8',
    )
    try:
        credential_path.chmod(0o600)
        seed_path.chmod(0o600)
    except OSError:
        pass
    return credential_path, seed_path


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
    result.add_argument(
        "--mode",
        choices=PROVISIONING_MODES,
        default=PRODUCTION_MODE,
        help=(
            "production creates exactly five invitations; disposable-e2e creates "
            "one short-lived invitation"
        ),
    )
    result.add_argument(
        "--seed-via",
        choices=SEED_VIA_CHOICES,
        default=SEED_VIA_NONE,
        help=(
            "none writes private artifacts only; linked-cli may seed only "
            "one disposable E2E invitation or one PI manual-test "
            "credential after exact confirmation"
        ),
    )
    result.add_argument("--assignment-code", action="append", dest="assignment_codes")
    return result


def run_pi_manual_provision(
    args: argparse.Namespace,
    repository_root: Path,
    hmac_key: bytes,
    expires_at: datetime,
) -> int:
    assignments = tuple(
        args.assignment_codes or (DEFAULT_ASSIGNMENTS[0],)
    )
    if assignments != (DEFAULT_ASSIGNMENTS[0],):
        raise ValueError(
            'pi-manual-test mode requires exactly PILOT_R01'
        )
    credential_path, seed_path = provision_pi_manual_test(
        output=args.output,
        repository_root=repository_root,
        instrument_sha256=args.instrument_sha256,
        expires_at=expires_at,
        hmac_key=hmac_key,
        private_root=args.private_root,
    )
    if args.seed_via == SEED_VIA_LINKED_CLI:
        mapping = validate_pi_manual_artifacts(
            credential_path,
            seed_path,
            repository_root,
            args.instrument_sha256,
            expires_at,
            hmac_key,
        )
        confirmation = linked_pi_seed_confirmation(
            mapping['instrument_sha256'],
            mapping['invite_id'],
        )
        print(f'Confirmation required: {confirmation}')
        try:
            supplied = input('Type the exact confirmation phrase: ')
        except EOFError as exc:
            raise ValueError(
                'linked PI seed confirmation was not supplied'
            ) from exc
        apply_linked_pi_seed(
            mapping,
            supplied,
            LinkedCliBackend(),
        )
    print(
        'Created 1 PI manual-test credential file in '
        f'{credential_path.parent}'
    )
    print(
        f'Credential: {credential_path.name} '
        '(values not printed; do not commit)'
    )
    print(
        f'Database seed: {seed_path.name} '
        '(contains HMAC digests only)'
    )
    if args.seed_via == SEED_VIA_LINKED_CLI:
        print('Linked Supabase PI seed attestation passed.')
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    repository_root = Path(__file__).resolve().parents[2]
    secret_value = os.environ.get("INVITE_HMAC_SECRET_B64", "")
    try:
        if (
            args.seed_via == SEED_VIA_LINKED_CLI
            and args.mode
            not in {DISPOSABLE_E2E_MODE, PI_MANUAL_TEST_MODE}
        ):
            raise ValueError(
                "linked-cli seeding is limited to disposable-e2e or "
                "pi-manual-test mode"
            )
        hmac_key = decode_key(secret_value)
        default_assignments = (
            DEFAULT_ASSIGNMENTS
            if args.mode == PRODUCTION_MODE
            else (DEFAULT_ASSIGNMENTS[0],)
        )
        assignment_codes = tuple(args.assignment_codes or default_assignments)
        expires_at = parse_expiry(args.expires_at)
        if args.mode == PI_MANUAL_TEST_MODE:
            return run_pi_manual_provision(
                args,
                repository_root,
                hmac_key,
                expires_at,
            )
        private_path, seed_path = provision(
            output=args.output,
            repository_root=repository_root,
            instrument_sha256=args.instrument_sha256,
            assignment_codes=assignment_codes,
            expires_at=expires_at,
            site_url=args.site_url,
            hmac_key=hmac_key,
            private_root=args.private_root,
            mode=args.mode,
        )
        if args.seed_via == SEED_VIA_LINKED_CLI:
            seed_mapping = validate_linked_seed_artifacts(
                private_path,
                seed_path,
                repository_root,
                args.instrument_sha256,
                assignment_codes,
                expires_at,
                args.site_url,
                hmac_key,
            )
            confirmation = linked_seed_confirmation(
                seed_mapping["instrument_sha256"],
                seed_mapping["invite_id"],
            )
            print(f"Confirmation required: {confirmation}")
            try:
                supplied = input("Type the exact confirmation phrase: ")
            except EOFError as exc:
                raise ValueError(
                    "linked seed confirmation was not supplied"
                ) from exc
            apply_linked_seed(
                seed_mapping, supplied, LinkedCliBackend()
            )
    except (ValueError, FileExistsError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except LinkedCliAmbiguousOutcome as exc:
        print(str(exc), file=sys.stderr)
        safe_counts = [
            f"{key}={exc.safe_attestation[key]}"
            for key in AMBIGUOUS_COUNT_KEYS
            if type(exc.safe_attestation.get(key)) is int
            and 0 <= exc.safe_attestation[key] <= 1_000_000
        ]
        if safe_counts:
            print("SAFE COUNTS: " + " ".join(safe_counts), file=sys.stderr)
        return 1
    except LinkedCliError:
        print(
            "ERROR: linked Supabase seed failed; private values were not printed.",
            file=sys.stderr,
        )
        return 1
    count = 5 if args.mode == PRODUCTION_MODE else 1
    label = "production" if args.mode == PRODUCTION_MODE else "disposable E2E"
    print(f"Created {count} {label} private invite(s) in {private_path.parent}")
    print(f"Raw links: {private_path.name} (not printed; do not commit)")
    print(f"Database seed: {seed_path.name} (contains HMAC digests only)")
    if args.seed_via == SEED_VIA_LINKED_CLI:
        print("Linked Supabase seed attestation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
