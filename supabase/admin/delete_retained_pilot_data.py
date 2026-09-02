#!/usr/bin/env python3
"""Preview or delete pilot records that have passed an explicit retention cutoff."""

from __future__ import annotations

import argparse
import hmac
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence


HASH_RE = re.compile(r"^[0-9a-f]{64}$")
COUNT_KEYS = ("submissions", "responses", "identities", "invites")

PREVIEW_COUNTS_SQL = """
with target_submissions as (
  select submission_id, participant_id, invite_id
  from research.pilot_submissions
  where instrument_sha256 = %s
    and submitted_at < %s
)
select
  (select count(*) from target_submissions) as submissions,
  (select count(*)
     from research.pilot_responses as response
     join target_submissions as target using (submission_id)) as responses,
  (select count(*)
     from private.participant_identity as identity
     join target_submissions as target using (participant_id)) as identities,
  (select count(*)
     from private.pilot_invites as invite
     join target_submissions as target using (invite_id)) as invites
"""

LOCK_SUBMISSIONS_SQL = """
select submission_id, participant_id, invite_id
from research.pilot_submissions
where instrument_sha256 = %s
  and submitted_at < %s
order by submission_id
for update
"""

LOCK_INVITES_SQL = """
select invite_id
from private.pilot_invites
where invite_id = any(%s::uuid[])
order by invite_id
for update
"""

LOCKED_COUNTS_SQL = """
select
  (select count(*)
     from research.pilot_responses
     where submission_id = any(%s::uuid[])) as responses,
  (select count(*)
     from private.participant_identity
     where participant_id = any(%s::uuid[])) as identities,
  (select count(*)
     from private.pilot_invites
     where invite_id = any(%s::uuid[])) as invites
"""

UNLINK_INVITES_SQL = """
update private.pilot_invites as invite
set submission_id = null
where (invite.invite_id, invite.submission_id) in (
  select linked.invite_id, linked.submission_id
  from unnest(%s::uuid[], %s::uuid[]) as linked(invite_id, submission_id)
)
"""

DELETE_RESPONSES_SQL = """
delete from research.pilot_responses
where submission_id = any(%s::uuid[])
"""

DELETE_SUBMISSIONS_SQL = """
delete from research.pilot_submissions
where submission_id = any(%s::uuid[])
"""

DELETE_IDENTITIES_SQL = """
delete from private.participant_identity
where participant_id = any(%s::uuid[])
"""

DELETE_INVITES_SQL = """
delete from private.pilot_invites
where invite_id = any(%s::uuid[])
"""

VERIFY_GONE_SQL = """
select
  (select count(*)
     from research.pilot_responses
     where submission_id = any(%s::uuid[])) as responses,
  (select count(*)
     from research.pilot_submissions
     where submission_id = any(%s::uuid[])) as submissions,
  (select count(*)
     from private.participant_identity
     where participant_id = any(%s::uuid[])) as identities,
  (select count(*)
     from private.pilot_invites
     where invite_id = any(%s::uuid[])) as invites
"""


class RetentionError(RuntimeError):
    """A safe, operator-facing retention operation failure."""


class CountMismatchError(RetentionError):
    """Raised so the surrounding transaction rolls back on integrity drift."""


class DependencyUnavailableError(RetentionError):
    """Raised when the administrator has not installed the database driver."""


def parse_cutoff(value: str) -> datetime:
    """Parse an ISO-8601 cutoff and require its timezone to be explicit."""
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("--cutoff must be an ISO-8601 timestamp with an explicit timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("--cutoff must include an explicit timezone (Z or a numeric offset)")
    return parsed.astimezone(timezone.utc)


def parse_instrument_sha256(value: str) -> str:
    """Accept only the canonical lowercase digest used by the database."""
    if not HASH_RE.fullmatch(value):
        raise ValueError("--instrument-sha256 must be a 64-character lowercase SHA-256 digest")
    return value


def utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("cutoff must include a timezone")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def confirmation_phrase(instrument_sha256: str, cutoff: datetime) -> str:
    """Return the exact, deterministic phrase required for destructive mode."""
    digest = parse_instrument_sha256(instrument_sha256)
    return f"DELETE RETAINED PILOT DATA {digest} BEFORE {utc_text(cutoff)}"


def _counts(row: Sequence[Any] | None, keys: Sequence[str]) -> dict[str, int]:
    if row is None or len(row) != len(keys):
        raise CountMismatchError("database count result had an unexpected shape")
    result: dict[str, int] = {}
    for key, raw_value in zip(keys, row):
        if not isinstance(raw_value, int) or isinstance(raw_value, bool) or raw_value < 0:
            raise CountMismatchError(f"database returned an invalid {key} count")
        result[key] = raw_value
    return result


def _ensure_unique(rows: Sequence[Sequence[Any]], column: int, label: str) -> None:
    values = [row[column] for row in rows]
    if len(set(values)) != len(values):
        raise CountMismatchError(f"locked target rows contain duplicate {label} values")


def _ensure_identifier_set(expected: Iterable[Any], actual: Iterable[Any], label: str) -> None:
    expected_values = set(expected)
    actual_values = set(actual)
    if expected_values != actual_values:
        raise CountMismatchError(
            f"locked {label} set mismatch: expected {len(expected_values)}, got {len(actual_values)}"
        )


def _execute_checked(
    cursor: Any,
    label: str,
    sql: str,
    parameters: Sequence[Any],
    expected: int,
) -> None:
    cursor.execute(sql, parameters)
    actual = cursor.rowcount
    if actual != expected:
        raise CountMismatchError(f"{label} row-count mismatch: expected {expected}, got {actual}")


def preview_deletion(
    connection: Any,
    instrument_sha256: str,
    cutoff: datetime,
) -> dict[str, int]:
    """Count the deletion scope in a database-enforced read-only transaction."""
    digest = parse_instrument_sha256(instrument_sha256)
    cutoff_utc = parse_cutoff(utc_text(cutoff))
    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute("set transaction read only")
            cursor.execute(PREVIEW_COUNTS_SQL, (digest, cutoff_utc))
            return _counts(cursor.fetchone(), COUNT_KEYS)


def delete_retained_data(
    connection: Any,
    instrument_sha256: str,
    cutoff: datetime,
) -> dict[str, int]:
    """Delete one locked retention scope atomically, rolling back on any drift."""
    digest = parse_instrument_sha256(instrument_sha256)
    cutoff_utc = parse_cutoff(utc_text(cutoff))
    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute(LOCK_SUBMISSIONS_SQL, (digest, cutoff_utc))
            target_rows = cursor.fetchall()
            for row in target_rows:
                if len(row) != 3 or any(value is None for value in row):
                    raise CountMismatchError("locked submission result had an unexpected shape")
            _ensure_unique(target_rows, 0, "submission_id")
            _ensure_unique(target_rows, 1, "participant_id")
            _ensure_unique(target_rows, 2, "invite_id")

            submission_ids = [row[0] for row in target_rows]
            participant_ids = [row[1] for row in target_rows]
            invite_ids = [row[2] for row in target_rows]
            submission_count = len(submission_ids)
            if submission_count == 0:
                return {key: 0 for key in COUNT_KEYS}

            cursor.execute(LOCK_INVITES_SQL, (invite_ids,))
            locked_invite_rows = cursor.fetchall()
            if any(len(row) != 1 for row in locked_invite_rows):
                raise CountMismatchError("locked invite result had an unexpected shape")
            _ensure_identifier_set(invite_ids, (row[0] for row in locked_invite_rows), "invite")

            cursor.execute(LOCKED_COUNTS_SQL, (submission_ids, participant_ids, invite_ids))
            related = _counts(cursor.fetchone(), ("responses", "identities", "invites"))
            if related["identities"] != submission_count:
                raise CountMismatchError(
                    "identity target count mismatch: "
                    f"expected {submission_count}, got {related['identities']}"
                )
            if related["invites"] != submission_count:
                raise CountMismatchError(
                    "invite target count mismatch: "
                    f"expected {submission_count}, got {related['invites']}"
                )

            expected: dict[str, int] = {
                "submissions": submission_count,
                "responses": related["responses"],
                "identities": related["identities"],
                "invites": related["invites"],
            }
            _execute_checked(
                cursor,
                "invite unlink",
                UNLINK_INVITES_SQL,
                (invite_ids, submission_ids),
                expected["invites"],
            )
            _execute_checked(
                cursor,
                "response delete",
                DELETE_RESPONSES_SQL,
                (submission_ids,),
                expected["responses"],
            )
            _execute_checked(
                cursor,
                "submission delete",
                DELETE_SUBMISSIONS_SQL,
                (submission_ids,),
                expected["submissions"],
            )
            _execute_checked(
                cursor,
                "identity delete",
                DELETE_IDENTITIES_SQL,
                (participant_ids,),
                expected["identities"],
            )
            _execute_checked(
                cursor,
                "invite delete",
                DELETE_INVITES_SQL,
                (invite_ids,),
                expected["invites"],
            )

            cursor.execute(VERIFY_GONE_SQL, (submission_ids, submission_ids, participant_ids, invite_ids))
            remaining = _counts(
                cursor.fetchone(),
                ("responses", "submissions", "identities", "invites"),
            )
            if any(remaining.values()):
                raise CountMismatchError("post-delete verification found retained target rows")
            return expected


def connect_database(database_url: str) -> Any:
    """Load psycopg only for an actual administrative database operation."""
    try:
        import psycopg  # type: ignore[import-not-found]
    except ImportError as exc:
        raise DependencyUnavailableError(
            "Install the 'psycopg' package in the administrator environment."
        ) from exc
    return psycopg.connect(
        database_url,
        connect_timeout=10,
        application_name="bok-hosted-pilot-retention",
        sslmode="require",
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--cutoff",
        required=True,
        help="Delete submissions strictly before this ISO-8601 timestamp; timezone is required",
    )
    result.add_argument("--instrument-sha256", required=True)
    mode = result.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Use a read-only transaction to count rows and print the confirmation phrase",
    )
    mode.add_argument(
        "--confirm",
        metavar="EXACT_PHRASE",
        help="Delete only when this exactly matches the generated phrase",
    )
    return result


def _print_counts(prefix: str, counts: Mapping[str, int]) -> None:
    print(
        f"{prefix}: submissions={counts['submissions']}, responses={counts['responses']}, "
        f"identities={counts['identities']}, invites={counts['invites']}"
    )


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        digest = parse_instrument_sha256(args.instrument_sha256)
        cutoff = parse_cutoff(args.cutoff)
        required_phrase = confirmation_phrase(digest, cutoff)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.confirm is not None and not hmac.compare_digest(args.confirm, required_phrase):
        print("ERROR: --confirm did not exactly match the generated phrase.", file=sys.stderr)
        print(f"Required phrase: {required_phrase}", file=sys.stderr)
        return 2

    database_url = os.environ.get("SUPABASE_DB_URL", "").strip()
    if not database_url:
        print("ERROR: server-only SUPABASE_DB_URL is required in the current process.", file=sys.stderr)
        return 2

    connection = None
    try:
        connection = connect_database(database_url)
        if args.dry_run:
            counts = preview_deletion(connection, digest, cutoff)
            _print_counts("DRY RUN (read-only)", counts)
            print("No rows were changed.")
            print(f"Required confirmation phrase: {required_phrase}")
        else:
            counts = delete_retained_data(connection, digest, cutoff)
            _print_counts("DELETED", counts)
        return 0
    except RetentionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception:
        # Driver failures can include connection details; do not echo them.
        print("ERROR: database operation failed; no credentials or row data were printed.", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                # Do not let a driver close error expose connection details.
                pass


if __name__ == "__main__":
    raise SystemExit(main())
