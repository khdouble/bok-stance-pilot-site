#!/usr/bin/env python3
"""Inspect and change the pilot fielding gate through a server-only DB connection.

Opening actions are fail-closed: disposable E2E requires exactly one eligible
unrevoked invitation, while production requires a locally validated live
release and exactly five eligible production assignments. Closing is always
available with an explicit confirmation phrase.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Mapping
from urllib.parse import unquote, urlsplit


HASH_RE = re.compile(r"^[0-9a-f]{64}$")
PROJECT_REF_RE = re.compile(r"^[a-z0-9]{20}$")
EXPECTED_ASSIGNMENTS = tuple(
    f"PILOT_R{number:02d}" for number in range(1, 6)
)
ACTIONS = (
    "status",
    "open-e2e",
    "close-e2e",
    "open-production",
    "close",
)


class GateError(RuntimeError):
    """A safe, operator-facing refusal reason."""


def parse_instrument_sha256(value: str) -> str:
    if not HASH_RE.fullmatch(value):
        raise ValueError(
            "--instrument-sha256 must be a lowercase SHA-256 digest"
        )
    return value


def parse_invite_id(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise ValueError("--invite-id must be a UUID") from exc
    if str(parsed) != value.lower():
        raise ValueError("--invite-id must use canonical UUID text")
    return str(parsed)


def parse_project_ref(value: str) -> str:
    if value != value.strip() or not PROJECT_REF_RE.fullmatch(value):
        raise ValueError("project ref must be exactly 20 lowercase letters or digits")
    return value


def resolve_project_ref(repository_root: Path, supplied: str | None) -> str:
    linked_path = repository_root / "supabase" / ".temp" / "project-ref"
    linked: str | None = None
    if linked_path.is_file():
        try:
            text = linked_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError("cannot read the linked Supabase project ref") from exc
        if len(text.splitlines()) != 1:
            raise ValueError("linked Supabase project ref file is malformed")
        linked = parse_project_ref(text.strip())

    if supplied is None:
        if linked is None:
            raise ValueError(
                "no linked Supabase project ref; run supabase link or pass --project-ref"
            )
        return linked

    explicit = parse_project_ref(supplied)
    if linked is not None and not hmac.compare_digest(explicit, linked):
        raise ValueError("--project-ref does not match the linked Supabase project")
    return explicit


def required_database_url(
    environment: Mapping[str, str], project_ref: str
) -> str:
    project_ref = parse_project_ref(project_ref)
    value = environment.get("SUPABASE_DB_URL", "")
    if not value:
        raise ValueError(
            "server-only SUPABASE_DB_URL is required in the current process"
        )
    if value != value.strip() or any(character.isspace() for character in value):
        raise ValueError(
            "SUPABASE_DB_URL must be a PostgreSQL URI without whitespace"
        )
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").lower()
        username = unquote(parsed.username or "")
        _ = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("SUPABASE_DB_URL is not a valid PostgreSQL URI") from exc
    if parsed.scheme not in {"postgres", "postgresql"} or parsed.fragment:
        raise ValueError("SUPABASE_DB_URL is not a valid PostgreSQL URI")

    direct = (
        hostname == f"db.{project_ref}.supabase.co"
        and username == "postgres"
    )
    pooled = (
        hostname.endswith(".pooler.supabase.com")
        and hostname != "pooler.supabase.com"
        and username == f"postgres.{project_ref}"
    )
    if not (direct or pooled):
        raise ValueError(
            "SUPABASE_DB_URL does not target the expected Supabase project"
        )
    return value


def confirmation_phrase(
    action: str,
    instrument_sha256: str,
    invite_id: str | None = None,
) -> str:
    if action == "open-e2e":
        if invite_id is None:
            raise ValueError("open-e2e requires --invite-id")
        return f"OPEN E2E FIELDING {instrument_sha256} {invite_id}"
    if action == "close-e2e":
        if invite_id is None:
            raise ValueError("close-e2e requires --invite-id")
        return (
            f"CLOSE E2E FIELDING AND REVOKE {instrument_sha256} {invite_id}"
        )
    if action == "open-production":
        return f"OPEN PRODUCTION FIELDING {instrument_sha256}"
    if action == "close":
        return f"CLOSE FIELDING {instrument_sha256}"
    raise ValueError("status does not use a confirmation phrase")


def connect_database(database_url: str):
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError(
            'psycopg is required; install it with: python -m pip install "psycopg[binary]"'
        ) from exc
    return psycopg.connect(database_url, connect_timeout=10)


def _instrument_row(cursor, instrument_sha256: str, *, lock: bool):
    suffix = " for update" if lock else ""
    cursor.execute(
        """
        select
          is_active,
          fielding_open,
          fielding_opened_at,
          fielding_closed_at
        from research.pilot_instruments
        where instrument_sha256 = %s
        """
        + suffix,
        (instrument_sha256,),
    )
    row = cursor.fetchone()
    if row is None:
        raise GateError("the requested instrument does not exist")
    return row


def read_gate_status(connection, instrument_sha256: str) -> dict[str, object]:
    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute("set transaction read only")
            instrument = _instrument_row(
                cursor, instrument_sha256, lock=False
            )
            cursor.execute(
                """
                select
                  count(*)::integer,
                  count(*) filter (
                    where revoked_at is null
                  )::integer,
                  count(*) filter (
                    where revoked_at is null
                      and used_at is null
                      and expires_at > now()
                  )::integer,
                  count(*) filter (
                    where used_at is not null
                  )::integer
                from private.pilot_invites
                where instrument_sha256 = %s
                """,
                (instrument_sha256,),
            )
            invite_counts = cursor.fetchone()
            cursor.execute(
                """
                select count(*)::integer
                from research.pilot_submissions
                where instrument_sha256 = %s
                """,
                (instrument_sha256,),
            )
            submission_count = cursor.fetchone()
    if invite_counts is None or submission_count is None:
        raise GateError("database status query returned no result")
    return {
        "instrument_sha256": instrument_sha256,
        "is_active": bool(instrument[0]),
        "fielding_open": bool(instrument[1]),
        "fielding_opened_at": instrument[2],
        "fielding_closed_at": instrument[3],
        "invite_count": int(invite_counts[0]),
        "unrevoked_invite_count": int(invite_counts[1]),
        "eligible_unused_invite_count": int(invite_counts[2]),
        "used_invite_count": int(invite_counts[3]),
        "submission_count": int(submission_count[0]),
    }


def _locked_invites(cursor, instrument_sha256: str) -> list[tuple]:
    cursor.execute(
        """
        select
          invite_id::text,
          assignment_code,
          (revoked_at is null) as unrevoked,
          (
            revoked_at is null
            and used_at is null
            and expires_at > now()
          ) as eligible
        from private.pilot_invites
        where instrument_sha256 = %s
        order by invite_id
        for update
        """,
        (instrument_sha256,),
    )
    return list(cursor.fetchall())


def _submission_count(cursor, instrument_sha256: str) -> int:
    cursor.execute(
        """
        select count(*)::integer
        from research.pilot_submissions
        where instrument_sha256 = %s
        """,
        (instrument_sha256,),
    )
    row = cursor.fetchone()
    if row is None:
        raise GateError("submission count query returned no result")
    return int(row[0])


def _set_gate(cursor, instrument_sha256: str, open_gate: bool) -> None:
    if open_gate:
        cursor.execute(
            """
            update research.pilot_instruments
            set
              fielding_open = true,
              fielding_opened_at = now(),
              fielding_closed_at = null
            where instrument_sha256 = %s
              and is_active
              and not fielding_open
            """,
            (instrument_sha256,),
        )
    else:
        cursor.execute(
            """
            update research.pilot_instruments
            set
              fielding_open = false,
              fielding_closed_at = now()
            where instrument_sha256 = %s
            """,
            (instrument_sha256,),
        )
    if cursor.rowcount != 1:
        raise GateError("fielding gate update affected an unexpected row count")


def _verify_gate(cursor, instrument_sha256: str, expected_open: bool) -> None:
    cursor.execute(
        """
        select fielding_open
        from research.pilot_instruments
        where instrument_sha256 = %s
        """,
        (instrument_sha256,),
    )
    row = cursor.fetchone()
    if row is None or bool(row[0]) is not expected_open:
        raise GateError("fielding gate verification failed")


def mutate_gate(
    connection,
    action: str,
    instrument_sha256: str,
    invite_id: str | None,
) -> None:
    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute("set transaction isolation level serializable")
            instrument = _instrument_row(
                cursor, instrument_sha256, lock=True
            )
            is_active = bool(instrument[0])
            fielding_open = bool(instrument[1])

            if action in {"open-e2e", "open-production"}:
                if not is_active:
                    raise GateError("the requested instrument is not active")
                if fielding_open:
                    raise GateError("fielding gate is already open")
                invites = _locked_invites(cursor, instrument_sha256)
                submissions = _submission_count(
                    cursor, instrument_sha256
                )
                unrevoked = [row for row in invites if bool(row[2])]
                eligible = [row for row in invites if bool(row[3])]
                if submissions != 0:
                    raise GateError(
                        "opening requires zero existing submissions; clean E2E data first"
                    )

                if action == "open-e2e":
                    if invite_id is None:
                        raise GateError("open-e2e requires an invite ID")
                    if (
                        len(unrevoked) != 1
                        or len(eligible) != 1
                        or str(eligible[0][0]) != invite_id
                    ):
                        raise GateError(
                            "E2E opening requires exactly the specified one "
                            "eligible unrevoked invitation"
                        )
                else:
                    assignments = tuple(
                        sorted(str(row[1]) for row in eligible)
                    )
                    if (
                        len(unrevoked) != 5
                        or len(eligible) != 5
                        or assignments != EXPECTED_ASSIGNMENTS
                    ):
                        raise GateError(
                            "production opening requires exactly five eligible "
                            "unrevoked PILOT_R01..PILOT_R05 invitations"
                        )

                _set_gate(cursor, instrument_sha256, True)
                _verify_gate(cursor, instrument_sha256, True)
                return

            _set_gate(cursor, instrument_sha256, False)
            if action == "close-e2e":
                if invite_id is None:
                    raise GateError("close-e2e requires an invite ID")
                cursor.execute(
                    """
                    update private.pilot_invites
                    set revoked_at = coalesce(revoked_at, now())
                    where instrument_sha256 = %s
                      and invite_id = %s::uuid
                    """,
                    (instrument_sha256, invite_id),
                )
                if cursor.rowcount != 1:
                    raise GateError(
                        "disposable invite revocation affected an unexpected row count"
                    )
                cursor.execute(
                    """
                    select revoked_at is not null
                    from private.pilot_invites
                    where instrument_sha256 = %s
                      and invite_id = %s::uuid
                    """,
                    (instrument_sha256, invite_id),
                )
                revoked = cursor.fetchone()
                if revoked is None or not bool(revoked[0]):
                    raise GateError("disposable invite revocation verification failed")
            _verify_gate(cursor, instrument_sha256, False)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assert_local_live_release(
    repository_root: Path,
    instrument_sha256: str,
) -> None:
    tools_path = repository_root / "tools"
    if str(tools_path) not in sys.path:
        sys.path.insert(0, str(tools_path))
    from pi_config import (  # type: ignore[import-not-found]
        boolean_value,
        has_placeholder,
        validate_live_config,
    )

    config_path = repository_root / "docs" / "site-config.js"
    privacy_path = repository_root / "docs" / "privacy.html"
    manifest_path = repository_root / "docs" / "deployment-manifest.json"
    config = config_path.read_text(encoding="utf-8")
    privacy = privacy_path.read_text(encoding="utf-8")
    errors = validate_live_config(config, privacy)
    if (
        not boolean_value(config, "fieldingEnabled")
        or has_placeholder(config, privacy)
        or errors
    ):
        raise GateError(
            "local PI configuration is not a validated live release"
        )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    declared_hash = manifest.get("deployment_manifest_sha256", "")
    payload = {
        key: value
        for key, value in manifest.items()
        if key != "deployment_manifest_sha256"
    }
    expected_file_hashes = {
        "privacy_notice": _sha256_file(privacy_path),
        "site_config": _sha256_file(config_path),
    }
    calculated_hash = hashlib.sha256(_canonical_json(payload)).hexdigest()
    if (
        manifest.get("deployment_state") != "live"
        or manifest.get("instrument_sha256") != instrument_sha256
        or manifest.get("operational_file_hashes") != expected_file_hashes
        or not isinstance(declared_hash, str)
        or not hmac.compare_digest(declared_hash, calculated_hash)
    ):
        raise GateError(
            "local live deployment manifest is missing, stale, or invalid"
        )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("action", choices=ACTIONS)
    result.add_argument("--instrument-sha256", required=True)
    result.add_argument(
        "--project-ref",
        help=(
            "expected Supabase project ref; defaults to the exact linked "
            "supabase/.temp/project-ref"
        ),
    )
    result.add_argument("--invite-id")
    result.add_argument(
        "--confirm",
        help="exact action-specific confirmation phrase for every mutation",
    )
    return result


def validate_action_arguments(
    action: str,
    instrument_sha256: str,
    invite_id: str | None,
    confirm: str | None,
) -> None:
    if action in {"open-e2e", "close-e2e"}:
        if invite_id is None:
            raise ValueError(f"{action} requires --invite-id")
    elif invite_id is not None:
        raise ValueError("--invite-id is accepted only for E2E actions")

    if action == "status":
        if confirm is not None:
            raise ValueError("status does not accept --confirm")
        return
    expected = confirmation_phrase(
        action, instrument_sha256, invite_id
    )
    if confirm is None or not hmac.compare_digest(confirm, expected):
        raise ValueError(
            "confirmation did not match; required phrase: " + expected
        )


def print_status(status: Mapping[str, object]) -> None:
    for key in (
        "instrument_sha256",
        "is_active",
        "fielding_open",
        "fielding_opened_at",
        "fielding_closed_at",
        "invite_count",
        "unrevoked_invite_count",
        "eligible_unused_invite_count",
        "used_invite_count",
        "submission_count",
    ):
        print(f"{key}={status[key]}")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    repository_root = Path(__file__).resolve().parents[2]
    connection = None
    try:
        instrument_sha256 = parse_instrument_sha256(
            args.instrument_sha256
        )
        invite_id = parse_invite_id(args.invite_id)
        validate_action_arguments(
            args.action,
            instrument_sha256,
            invite_id,
            args.confirm,
        )
        project_ref = resolve_project_ref(
            repository_root, args.project_ref
        )
        if args.action == "open-production":
            assert_local_live_release(
                repository_root, instrument_sha256
            )
        database_url = required_database_url(os.environ, project_ref)
        connection = connect_database(database_url)
        if args.action == "status":
            print_status(
                read_gate_status(connection, instrument_sha256)
            )
        else:
            mutate_gate(
                connection,
                args.action,
                instrument_sha256,
                invite_id,
            )
            print(
                f"Completed {args.action}; run status to independently verify."
            )
    except (GateError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # Do not print DB connection details or credentials.
        print(
            "ERROR: fielding gate operation failed "
            f"({type(exc).__name__})",
            file=sys.stderr,
        )
        return 1
    finally:
        if connection is not None:
            connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
