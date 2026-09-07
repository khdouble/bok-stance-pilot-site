#!/usr/bin/env python3
"""Safely preview and execute one participant's verified pilot withdrawal."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import sys
import unicodedata
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, NamedTuple, Sequence

try:
    from .private_storage import PRIVATE_ROOT_ENV, private_child
    from .linked_cli_backend import LinkedCliAmbiguousOutcome
except ImportError:  # Direct script execution from the repository root.
    from private_storage import PRIVATE_ROOT_ENV, private_child
    from linked_cli_backend import LinkedCliAmbiguousOutcome


HASH_RE = re.compile(r"^[0-9a-f]{64}$")
PROCEDURE_VERSION_RE = re.compile(
    r"^withdrawal-v(?P<date>\d{4}-\d{2}-\d{2})-r[1-9]\d*$"
)
PHONE_RE = re.compile(r"^01[016789]\d{7,8}$")
CONTROL_CHARS_RE = re.compile(
    r"[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]"
)
IDENTITY_HMAC_DOMAIN = b"bok-pilot-identity-v1\0"
CONFIRMATION_DOMAIN = b"bok-pilot-withdrawal-confirm-v1\0"
COUNT_KEYS = ("submissions", "responses", "identities", "invites")
AMBIGUOUS_WITHDRAWAL_COUNT_KEYS = (
    "target_invites",
    "target_identities",
    "target_submissions",
    "target_responses",
    "matching_audits",
)

FIND_IDENTITY_INVITES_SQL = """
select invite_id
from private.participant_identity
where identity_hmac = decode(%s, 'hex')
order by invite_id
"""

READ_INVITE_SQL = """
select invite_id, instrument_sha256, used_at, submission_id
from private.pilot_invites
where invite_id = %s::uuid
"""

LOCK_INVITE_SQL = READ_INVITE_SQL + "\nfor update"

READ_IDENTITIES_SQL = """
select participant_id, encode(identity_hmac, 'hex')
from private.participant_identity
where invite_id = %s::uuid
order by participant_id
"""

LOCK_IDENTITIES_SQL = READ_IDENTITIES_SQL + "\nfor update"

READ_SUBMISSIONS_SQL = """
select submission_id, participant_id, instrument_sha256
from research.pilot_submissions
where invite_id = %s::uuid
order by submission_id
"""

LOCK_SUBMISSIONS_SQL = READ_SUBMISSIONS_SQL + "\nfor update"

READ_RESPONSES_SQL = """
select display_position
from research.pilot_responses
where submission_id = %s::uuid
order by display_position
"""

LOCK_RESPONSES_SQL = READ_RESPONSES_SQL + "\nfor update"

UNLINK_INVITE_SQL = """
update private.pilot_invites
set submission_id = null
where invite_id = %s::uuid
  and submission_id = %s::uuid
"""

DELETE_RESPONSES_SQL = """
delete from research.pilot_responses
where submission_id = %s::uuid
"""

DELETE_SUBMISSION_SQL = """
delete from research.pilot_submissions
where submission_id = %s::uuid
  and participant_id = %s::uuid
  and invite_id = %s::uuid
"""

DELETE_IDENTITY_SQL = """
delete from private.participant_identity
where participant_id = %s::uuid
  and invite_id = %s::uuid
"""

DELETE_INVITE_SQL = """
delete from private.pilot_invites
where invite_id = %s::uuid
  and submission_id is null
"""

VERIFY_GONE_SQL = """
select
  (select count(*)
     from research.pilot_responses
     where submission_id = %s::uuid) as responses,
  (select count(*)
     from research.pilot_submissions
     where submission_id = %s::uuid) as submissions,
  (select count(*)
     from private.participant_identity
     where participant_id = %s::uuid) as identities,
  (select count(*)
     from private.pilot_invites
     where invite_id = %s::uuid) as invites
"""

INSERT_AUDIT_SQL = """
insert into private.pilot_withdrawal_events (
  withdrawal_event_id,
  instrument_sha256,
  procedure_version,
  selector_kind,
  request_received_at,
  outcome,
  deleted_invites,
  deleted_identities,
  deleted_submissions,
  deleted_responses,
  verification_passed
) values (
  %s::uuid,
  %s,
  %s,
  %s,
  %s::timestamptz,
  %s,
  %s,
  %s,
  %s,
  %s,
  true
)
"""


class WithdrawalError(RuntimeError):
    """A safe, operator-facing individual-withdrawal failure."""


class TargetNotFoundError(WithdrawalError):
    """Raised when the selector has no current database target."""


class AmbiguousTargetError(WithdrawalError):
    """Raised when an identity selector is not unique."""


class CountMismatchError(WithdrawalError):
    """Raised so the transaction rolls back on relational or count drift."""


class DependencyUnavailableError(WithdrawalError):
    """Raised when the administrator has not installed the database driver."""


class Selector(NamedTuple):
    kind: str
    database_value: str


class WithdrawalScope(NamedTuple):
    invite_id: Any
    participant_id: Any | None
    submission_id: Any | None
    outcome: str
    counts: dict[str, int]
    confirmation_tag: str


def parse_instrument_sha256(value: str) -> str:
    if not HASH_RE.fullmatch(value):
        raise ValueError(
            "--instrument-sha256 must be a 64-character lowercase SHA-256 digest"
        )
    return value


def parse_timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"{field} must be an ISO-8601 timestamp with an explicit timezone"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include an explicit timezone")
    return parsed.astimezone(timezone.utc)


def utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_procedure_version(value: str, request_received_at: datetime) -> str:
    match = PROCEDURE_VERSION_RE.fullmatch(value)
    if match is None:
        raise ValueError(
            "--procedure-version must match withdrawal-vYYYY-MM-DD-rN"
        )
    try:
        version_date = date.fromisoformat(match.group("date"))
    except ValueError as exc:
        raise ValueError("--procedure-version contains an invalid date") from exc
    if version_date > request_received_at.date():
        raise ValueError(
            "--procedure-version date cannot follow --request-received-at"
        )
    return value


def parse_invite_id(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError("--invite-id must be a canonical UUID") from exc
    canonical = str(parsed)
    if value != canonical:
        raise ValueError("--invite-id must be a canonical lowercase UUID")
    return canonical


def decode_identity_key(value: str) -> bytes:
    normalized = value.strip().replace("-", "+").replace("_", "/")
    normalized += "=" * (-len(normalized) % 4)
    try:
        decoded = base64.b64decode(normalized, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError(
            "IDENTITY_HMAC_SECRET_B64 is not valid base64"
        ) from exc
    if len(decoded) != 32:
        raise ValueError(
            "IDENTITY_HMAC_SECRET_B64 must encode exactly 32 bytes"
        )
    return decoded


def normalize_identity(name_value: object, phone_value: object) -> tuple[str, str]:
    if not isinstance(name_value, str) or not isinstance(phone_value, str):
        raise ValueError("identity file must contain text name and phone fields")
    name = unicodedata.normalize("NFC", name_value.strip())
    raw_phone = unicodedata.normalize("NFC", phone_value.strip())
    if (
        not 1 <= len(name) <= 80
        or CONTROL_CHARS_RE.search(name)
        or not 8 <= len(raw_phone) <= 30
        or CONTROL_CHARS_RE.search(raw_phone)
    ):
        raise ValueError("identity file contains an invalid identity value")
    phone = re.sub(r"[\s().-]", "", raw_phone)
    if phone.startswith("+82"):
        phone = "0" + phone[3:]
    if phone.startswith("0082"):
        phone = "0" + phone[4:]
    if not PHONE_RE.fullmatch(phone):
        raise ValueError("identity file contains an invalid mobile phone value")
    return name, phone


def load_normalized_identity(
    path: Path,
    repository_root: Path,
    private_root: Path | None = None,
) -> tuple[str, str]:
    try:
        source = private_child(
            path,
            repository_root,
            private_root,
            must_exist=True,
        )
        payload = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or set(payload) != {"name", "phone"}:
            raise ValueError("identity object must have exactly name and phone")
        return normalize_identity(payload["name"], payload["phone"])
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(
            "external identity file could not be read or validated"
        ) from exc


def identity_hmac_hex(name: str, phone: str, key: bytes) -> str:
    canonical = json.dumps(
        {"name": name, "phone": phone},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(
        key,
        IDENTITY_HMAC_DOMAIN + canonical,
        hashlib.sha256,
    ).hexdigest()


def invite_confirmation_tag(invite_id: object) -> str:
    try:
        invite_bytes = uuid.UUID(str(invite_id)).bytes
    except (ValueError, AttributeError) as exc:
        raise CountMismatchError(
            "database returned an invalid invitation identifier"
        ) from exc
    return hashlib.sha256(
        CONFIRMATION_DOMAIN + invite_bytes
    ).hexdigest()[:20]


def _rows(cursor: Any) -> list[Sequence[Any]]:
    result = cursor.fetchall()
    if not isinstance(result, list):
        result = list(result)
    return result


def _execute_checked(
    cursor: Any,
    label: str,
    sql: str,
    parameters: Sequence[Any],
    expected: int,
) -> None:
    cursor.execute(sql, parameters)
    if cursor.rowcount != expected:
        raise CountMismatchError(
            f"{label} row-count mismatch: expected {expected}, "
            f"got {cursor.rowcount}"
        )


def _inspect_scope(
    cursor: Any,
    selector: Selector,
    instrument_sha256: str,
    *,
    lock: bool,
) -> WithdrawalScope:
    digest = parse_instrument_sha256(instrument_sha256)
    if selector.kind == "normalized_identity":
        cursor.execute(FIND_IDENTITY_INVITES_SQL, (selector.database_value,))
        candidates = _rows(cursor)
        if any(len(row) != 1 or row[0] is None for row in candidates):
            raise CountMismatchError(
                "identity lookup returned an unexpected result shape"
            )
        if len(candidates) == 0:
            raise TargetNotFoundError(
                "no submitted pilot record matched the supplied identity"
            )
        if len(candidates) > 1:
            raise AmbiguousTargetError(
                "identity matched multiple pilot records; use an invitation ID"
            )
        invite_id = candidates[0][0]
    elif selector.kind == "invite_id":
        invite_id = selector.database_value
    else:
        raise ValueError("selector kind is invalid")

    cursor.execute(
        LOCK_INVITE_SQL if lock else READ_INVITE_SQL,
        (invite_id,),
    )
    invite = cursor.fetchone()
    if invite is None:
        raise TargetNotFoundError("no pilot invitation matched the selector")
    if len(invite) != 4 or any(value is None for value in invite[:2]):
        raise CountMismatchError(
            "invitation lookup returned an unexpected result shape"
        )
    locked_invite_id, invite_instrument, used_at, linked_submission = invite
    if str(locked_invite_id) != str(invite_id):
        raise CountMismatchError("locked invitation did not match the selector")
    if invite_instrument != digest:
        raise TargetNotFoundError(
            "the selector did not match this pilot instrument"
        )

    cursor.execute(
        LOCK_IDENTITIES_SQL if lock else READ_IDENTITIES_SQL,
        (locked_invite_id,),
    )
    identities = _rows(cursor)
    if any(
        len(row) != 2 or row[0] is None or not isinstance(row[1], str)
        for row in identities
    ):
        raise CountMismatchError(
            "identity lookup returned an unexpected result shape"
        )

    cursor.execute(
        LOCK_SUBMISSIONS_SQL if lock else READ_SUBMISSIONS_SQL,
        (locked_invite_id,),
    )
    submissions = _rows(cursor)
    if any(
        len(row) != 3 or any(value is None for value in row)
        for row in submissions
    ):
        raise CountMismatchError(
            "submission lookup returned an unexpected result shape"
        )

    if len(identities) == 0 and len(submissions) == 0:
        if selector.kind == "normalized_identity":
            raise CountMismatchError(
                "identity target disappeared during scope verification"
            )
        if used_at is not None or linked_submission is not None:
            raise CountMismatchError(
                "unused invitation has inconsistent submission state"
            )
        return WithdrawalScope(
            locked_invite_id,
            None,
            None,
            "deleted_unused_invite",
            {
                "submissions": 0,
                "responses": 0,
                "identities": 0,
                "invites": 1,
            },
            invite_confirmation_tag(locked_invite_id),
        )

    if len(identities) != 1 or len(submissions) != 1:
        raise CountMismatchError(
            "target must have exactly one identity and one submission"
        )
    participant_id, stored_identity_hmac = identities[0]
    submission_id, submitted_participant_id, submission_instrument = (
        submissions[0]
    )
    if (
        str(participant_id) != str(submitted_participant_id)
        or str(linked_submission) != str(submission_id)
        or used_at is None
        or submission_instrument != digest
    ):
        raise CountMismatchError(
            "target identity, invitation, and submission links are inconsistent"
        )
    if (
        selector.kind == "normalized_identity"
        and not hmac.compare_digest(
            stored_identity_hmac,
            selector.database_value,
        )
    ):
        raise CountMismatchError(
            "locked identity no longer matches the supplied selector"
        )

    cursor.execute(
        LOCK_RESPONSES_SQL if lock else READ_RESPONSES_SQL,
        (submission_id,),
    )
    response_rows = _rows(cursor)
    positions = [
        row[0]
        for row in response_rows
        if len(row) == 1
        and isinstance(row[0], int)
        and not isinstance(row[0], bool)
    ]
    if (
        len(positions) != len(response_rows)
        or positions != list(range(1, 13))
    ):
        raise CountMismatchError(
            "submitted target does not have exactly 12 ordered responses"
        )
    return WithdrawalScope(
        locked_invite_id,
        participant_id,
        submission_id,
        "deleted_submission",
        {
            "submissions": 1,
            "responses": 12,
            "identities": 1,
            "invites": 1,
        },
        invite_confirmation_tag(locked_invite_id),
    )


def preview_withdrawal(
    connection: Any,
    selector: Selector,
    instrument_sha256: str,
) -> WithdrawalScope:
    """Resolve and count one target in a database-enforced read-only snapshot."""
    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute(
                "set transaction isolation level repeatable read, read only"
            )
            return _inspect_scope(
                cursor,
                selector,
                instrument_sha256,
                lock=False,
            )


def _same_scope(
    expected: WithdrawalScope,
    actual: WithdrawalScope,
) -> bool:
    return (
        str(expected.invite_id) == str(actual.invite_id)
        and (
            expected.participant_id is None
            and actual.participant_id is None
            or str(expected.participant_id) == str(actual.participant_id)
        )
        and (
            expected.submission_id is None
            and actual.submission_id is None
            or str(expected.submission_id) == str(actual.submission_id)
        )
        and expected.outcome == actual.outcome
        and expected.counts == actual.counts
        and hmac.compare_digest(
            expected.confirmation_tag,
            actual.confirmation_tag,
        )
    )


def delete_withdrawn_participant(
    connection: Any,
    selector: Selector,
    instrument_sha256: str,
    procedure_version: str,
    request_received_at: datetime,
    expected_scope: WithdrawalScope,
) -> tuple[dict[str, int], str]:
    """Delete one locked target and add a non-identifying audit row atomically."""
    digest = parse_instrument_sha256(instrument_sha256)
    version = parse_procedure_version(
        procedure_version,
        request_received_at,
    )
    event_id = str(uuid.uuid4())
    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute("set transaction isolation level serializable")
            actual_scope = _inspect_scope(
                cursor,
                selector,
                digest,
                lock=True,
            )
            if not _same_scope(expected_scope, actual_scope):
                raise CountMismatchError(
                    "target changed after preview; run a new dry-run"
                )

            if actual_scope.outcome == "deleted_submission":
                assert actual_scope.submission_id is not None
                assert actual_scope.participant_id is not None
                _execute_checked(
                    cursor,
                    "invitation unlink",
                    UNLINK_INVITE_SQL,
                    (
                        actual_scope.invite_id,
                        actual_scope.submission_id,
                    ),
                    1,
                )
                _execute_checked(
                    cursor,
                    "response delete",
                    DELETE_RESPONSES_SQL,
                    (actual_scope.submission_id,),
                    12,
                )
                _execute_checked(
                    cursor,
                    "submission delete",
                    DELETE_SUBMISSION_SQL,
                    (
                        actual_scope.submission_id,
                        actual_scope.participant_id,
                        actual_scope.invite_id,
                    ),
                    1,
                )
                _execute_checked(
                    cursor,
                    "identity delete",
                    DELETE_IDENTITY_SQL,
                    (
                        actual_scope.participant_id,
                        actual_scope.invite_id,
                    ),
                    1,
                )

            _execute_checked(
                cursor,
                "invitation delete",
                DELETE_INVITE_SQL,
                (actual_scope.invite_id,),
                1,
            )
            cursor.execute(
                VERIFY_GONE_SQL,
                (
                    actual_scope.submission_id,
                    actual_scope.submission_id,
                    actual_scope.participant_id,
                    actual_scope.invite_id,
                ),
            )
            remaining = cursor.fetchone()
            if (
                remaining is None
                or len(remaining) != 4
                or any(
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or value != 0
                    for value in remaining
                )
            ):
                raise CountMismatchError(
                    "post-delete verification found retained target rows"
                )

            counts = actual_scope.counts
            _execute_checked(
                cursor,
                "withdrawal audit insert",
                INSERT_AUDIT_SQL,
                (
                    event_id,
                    digest,
                    version,
                    selector.kind,
                    request_received_at,
                    actual_scope.outcome,
                    counts["invites"],
                    counts["identities"],
                    counts["submissions"],
                    counts["responses"],
                ),
                1,
            )
    return dict(expected_scope.counts), event_id


def confirmation_phrase(
    scope: WithdrawalScope,
    instrument_sha256: str,
    procedure_version: str,
    request_received_at: datetime,
) -> str:
    digest = parse_instrument_sha256(instrument_sha256)
    version = parse_procedure_version(
        procedure_version,
        request_received_at,
    )
    counts = scope.counts
    return (
        f"DELETE ONE PILOT WITHDRAWAL {scope.confirmation_tag} "
        f"{digest} {version} REQUESTED {utc_text(request_received_at)} "
        f"COUNTS {counts['submissions']}/{counts['responses']}/"
        f"{counts['identities']}/{counts['invites']}"
    )


def connect_database(database_url: str) -> Any:
    try:
        import psycopg  # type: ignore[import-not-found]
    except ImportError as exc:
        raise DependencyUnavailableError(
            "Install the 'psycopg' package in the administrator environment."
        ) from exc
    return psycopg.connect(
        database_url,
        connect_timeout=10,
        application_name="bok-hosted-pilot-withdrawal",
        sslmode="require",
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        '--expected-purpose',
        choices=('participant', 'disposable_e2e', 'pi_manual_test'),
        help=(
            'Linked backend only: require this exact credential purpose '
            'again inside preview and delete'
        ),
    )
    result.add_argument(
        "--db-backend",
        choices=("direct", "linked-cli"),
        default="direct",
        help="Database transport; direct preserves the existing psycopg path",
    )
    result.add_argument("--instrument-sha256", required=True)
    result.add_argument("--procedure-version", required=True)
    result.add_argument(
        "--request-received-at",
        required=True,
        help="ISO-8601 request receipt timestamp with timezone",
    )
    result.add_argument(
        "--private-root",
        type=Path,
        help=(
            "External protected root used by --identity-file "
            f"(default: {PRIVATE_ROOT_ENV} or OS local app-data)"
        ),
    )
    selector = result.add_mutually_exclusive_group(required=True)
    selector.add_argument("--invite-id")
    selector.add_argument(
        "--identity-file",
        type=Path,
        help=(
            "External protected UTF-8 JSON containing exactly name and phone; "
            "PII is never accepted as a command-line value"
        ),
    )
    mode = result.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve the exact scope read-only and print a confirmation phrase",
    )
    mode.add_argument(
        "--confirm",
        metavar="EXACT_PHRASE",
        help="Delete only when this exactly matches the current preview phrase",
    )
    return result


def _new_linked_backend() -> Any:
    try:
        from .linked_cli_backend import LinkedCliBackend
    except ImportError:  # Direct script execution from the repository root.
        from linked_cli_backend import LinkedCliBackend
    return LinkedCliBackend()


def _scope_from_linked_preview(
    preview: Mapping[str, object],
    invite_id: str,
) -> WithdrawalScope:
    expected_keys = {
        "outcome",
        "submissions",
        "responses",
        "identities",
        "invites",
        "confirmation_tag",
    }
    if not isinstance(preview, Mapping) or set(preview) != expected_keys:
        raise WithdrawalError("linked withdrawal preview was invalid")
    outcome = preview["outcome"]
    expected_counts = (
        {"submissions": 0, "responses": 0, "identities": 0, "invites": 1}
        if outcome == "deleted_unused_invite"
        else {"submissions": 1, "responses": 12, "identities": 1, "invites": 1}
        if outcome == "deleted_submission"
        else None
    )
    if expected_counts is None or any(
        type(preview[key]) is not int or preview[key] != count
        for key, count in expected_counts.items()
    ):
        raise WithdrawalError("linked withdrawal preview was invalid")
    tag = preview["confirmation_tag"]
    if (
        not isinstance(tag, str)
        or not hmac.compare_digest(tag, invite_confirmation_tag(invite_id))
    ):
        raise WithdrawalError("linked withdrawal preview was invalid")
    return WithdrawalScope(
        invite_id,
        None,
        None,
        str(outcome),
        expected_counts,
        tag,
    )


def _linked_preview_payload(scope: WithdrawalScope) -> dict[str, object]:
    return {
        "outcome": scope.outcome,
        **scope.counts,
        "confirmation_tag": scope.confirmation_tag,
    }


def _counts_from_linked_result(
    result: Mapping[str, object],
    scope: WithdrawalScope,
) -> dict[str, int]:
    expected = {
        "operation": "WITHDRAWAL_DELETED",
        "outcome": scope.outcome,
        "deleted_submissions": scope.counts["submissions"],
        "deleted_responses": scope.counts["responses"],
        "deleted_identities": scope.counts["identities"],
        "deleted_invites": scope.counts["invites"],
        "verification_passed": True,
        "audit_inserted": True,
    }
    if (
        not isinstance(result, Mapping)
        or set(result) != set(expected)
        or any(type(result[key]) is not type(value) or result[key] != value
               for key, value in expected.items())
    ):
        raise WithdrawalError("linked withdrawal deletion attestation was invalid")
    return {
        "submissions": int(result["deleted_submissions"]),
        "responses": int(result["deleted_responses"]),
        "identities": int(result["deleted_identities"]),
        "invites": int(result["deleted_invites"]),
    }


def _print_counts(prefix: str, counts: Mapping[str, int]) -> None:
    print(
        f"{prefix}: submissions={counts['submissions']}, "
        f"responses={counts['responses']}, "
        f"identities={counts['identities']}, "
        f"invites={counts['invites']}"
    )


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    repository_root = Path(__file__).resolve().parents[2]
    try:
        if args.expected_purpose is not None and args.db_backend != 'linked-cli':
            raise ValueError(
                '--expected-purpose is supported only by linked-cli'
            )
        digest = parse_instrument_sha256(args.instrument_sha256)
        received_at = parse_timestamp(
            args.request_received_at,
            "--request-received-at",
        )
        if received_at > datetime.now(timezone.utc):
            raise ValueError(
                "--request-received-at cannot be in the future"
            )
        version = parse_procedure_version(
            args.procedure_version,
            received_at,
        )
        if args.db_backend == "linked-cli" and args.identity_file is not None:
            raise ValueError(
                "--db-backend linked-cli supports --invite-id only"
            )
        if args.invite_id is not None:
            selector = Selector(
                "invite_id",
                parse_invite_id(args.invite_id),
            )
        else:
            name, phone = load_normalized_identity(
                args.identity_file,
                repository_root,
                args.private_root,
            )
            identity_key = decode_identity_key(
                os.environ.get("IDENTITY_HMAC_SECRET_B64", "")
            )
            selector = Selector(
                "normalized_identity",
                identity_hmac_hex(name, phone, identity_key),
            )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    connection = None
    linked_backend = None
    try:
        if args.db_backend == "linked-cli":
            linked_backend = _new_linked_backend()
            preview = linked_backend.withdrawal_preview(
                digest,
                selector.database_value,
                expected_purpose=args.expected_purpose,
            )
            scope = _scope_from_linked_preview(
                preview, selector.database_value
            )
        else:
            database_url = os.environ.get("SUPABASE_DB_URL", "").strip()
            if not database_url:
                print(
                    "ERROR: server-only SUPABASE_DB_URL is required "
                    "in the current process.",
                    file=sys.stderr,
                )
                return 2
            connection = connect_database(database_url)
            scope = preview_withdrawal(connection, selector, digest)
        required_phrase = confirmation_phrase(
            scope,
            digest,
            version,
            received_at,
        )
        if args.dry_run:
            _print_counts("DRY RUN (read-only)", scope.counts)
            print("No rows were changed.")
            print(f"Required confirmation phrase: {required_phrase}")
            return 0
        if not hmac.compare_digest(args.confirm, required_phrase):
            print(
                "ERROR: --confirm did not exactly match the current "
                "preview phrase.",
                file=sys.stderr,
            )
            print(
                f"Required phrase: {required_phrase}",
                file=sys.stderr,
            )
            return 2
        if linked_backend is not None:
            result = linked_backend.withdrawal_delete(
                digest,
                selector.database_value,
                version,
                utc_text(received_at),
                _linked_preview_payload(scope),
                expected_purpose=args.expected_purpose,
            )
            counts = _counts_from_linked_result(result, scope)
        else:
            counts, _event_id = delete_withdrawn_participant(
                connection,
                selector,
                digest,
                version,
                received_at,
                scope,
            )
        _print_counts("DELETED AND VERIFIED", counts)
        print("A non-identifying withdrawal completion audit was recorded.")
        return 0
    except LinkedCliAmbiguousOutcome as exc:
        print(str(exc), file=sys.stderr)
        safe_counts = [
            f"{key}={exc.safe_attestation[key]}"
            for key in AMBIGUOUS_WITHDRAWAL_COUNT_KEYS
            if type(exc.safe_attestation.get(key)) is int
            and 0 <= exc.safe_attestation[key] <= 1_000_000
        ]
        if safe_counts:
            print(
                "SAFE COUNTS: " + " ".join(safe_counts),
                file=sys.stderr,
            )
        return 1
    except WithdrawalError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception:
        # Driver failures can contain credentials or query parameters.
        print(
            "ERROR: database operation failed; no credentials, selectors, "
            "or row data were printed.",
            file=sys.stderr,
        )
        return 1
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
