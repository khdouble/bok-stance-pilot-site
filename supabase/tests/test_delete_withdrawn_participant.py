from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import importlib.util
import io
import json
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path


ADMIN = Path(__file__).resolve().parents[1] / "admin"
if str(ADMIN) not in sys.path:
    sys.path.insert(0, str(ADMIN))
SCRIPT = ADMIN / "delete_withdrawn_participant.py"
SPEC = importlib.util.spec_from_file_location(
    "delete_withdrawn_participant",
    SCRIPT,
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeCursor:
    def __init__(self, steps: list[dict[str, object]]) -> None:
        self.steps = list(steps)
        self.events: list[tuple[str, object]] = []
        self.current: dict[str, object] = {}
        self.rowcount = -1

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        return False

    def execute(self, sql: str, parameters: object = None) -> None:
        if not self.steps:
            raise AssertionError(f"unexpected SQL: {sql}")
        self.current = self.steps.pop(0)
        normalized = " ".join(sql.lower().split())
        marker = str(self.current["contains"]).lower()
        if marker not in normalized:
            raise AssertionError(
                f"expected SQL containing {marker!r}, got {normalized!r}"
            )
        self.events.append((normalized, parameters))
        self.rowcount = int(self.current.get("rowcount", -1))

    def fetchone(self) -> object:
        return self.current.get("one")

    def fetchall(self) -> object:
        return self.current.get("all", [])


class FakeTransaction:
    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection

    def __enter__(self) -> "FakeTransaction":
        self.connection.transactions_started += 1
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        if exc_type is None:
            self.connection.commits += 1
        else:
            self.connection.rollbacks += 1
        return False


class FakeConnection:
    def __init__(self, steps: list[dict[str, object]]) -> None:
        self.fake_cursor = FakeCursor(steps)
        self.transactions_started = 0
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self)

    def cursor(self) -> FakeCursor:
        return self.fake_cursor

    def close(self) -> None:
        self.closed = True


class DeleteWithdrawnParticipantTest(unittest.TestCase):
    digest = "a" * 64
    invite_id = "11111111-1111-4111-8111-111111111111"
    participant_id = "22222222-2222-4222-8222-222222222222"
    submission_id = "33333333-3333-4333-8333-333333333333"
    received_at = datetime(2026, 9, 3, 1, 2, 3, tzinfo=timezone.utc)
    procedure_version = "withdrawal-v2026-09-03-r1"

    def invite_selector(self) -> object:
        return MODULE.Selector("invite_id", self.invite_id)

    def submitted_scope_steps(
        self,
        *,
        lock: bool,
        response_delete_count: int = 12,
        include_mutations: bool = False,
    ) -> list[dict[str, object]]:
        lock_marker = "for update" if lock else "from private.pilot_invites"
        steps: list[dict[str, object]] = [
            {
                "contains": lock_marker,
                "one": (
                    self.invite_id,
                    self.digest,
                    "used-at",
                    self.submission_id,
                ),
            },
            {
                "contains": "from private.participant_identity",
                "all": [(self.participant_id, "b" * 64)],
            },
            {
                "contains": "from research.pilot_submissions",
                "all": [
                    (
                        self.submission_id,
                        self.participant_id,
                        self.digest,
                    )
                ],
            },
            {
                "contains": "from research.pilot_responses",
                "all": [(position,) for position in range(1, 13)],
            },
        ]
        if include_mutations:
            steps.extend(
                [
                    {
                        "contains": "update private.pilot_invites",
                        "rowcount": 1,
                    },
                    {
                        "contains": "delete from research.pilot_responses",
                        "rowcount": response_delete_count,
                    },
                ]
            )
            if response_delete_count == 12:
                steps.extend(
                    [
                        {
                            "contains": "delete from research.pilot_submissions",
                            "rowcount": 1,
                        },
                        {
                            "contains": "delete from private.participant_identity",
                            "rowcount": 1,
                        },
                        {
                            "contains": "delete from private.pilot_invites",
                            "rowcount": 1,
                        },
                        {
                            "contains": "select (select count(*)",
                            "one": (0, 0, 0, 0),
                        },
                        {
                            "contains": "insert into private.pilot_withdrawal_events",
                            "rowcount": 1,
                        },
                    ]
                )
        return steps

    def test_identity_normalization_and_hmac_match_backend_contract(self) -> None:
        name, phone = MODULE.normalize_identity(
            "  e\u0301 김  ",
            "+82 (10)-1234-5678",
        )
        self.assertEqual(name, "é 김")
        self.assertEqual(phone, "01012345678")
        key = bytes(range(32))
        canonical = (
            '{"name":"é 김","phone":"01012345678"}'.encode("utf-8")
        )
        expected = hmac.new(
            key,
            MODULE.IDENTITY_HMAC_DOMAIN + canonical,
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(MODULE.identity_hmac_hex(name, phone, key), expected)
        encoded = base64.b64encode(key).decode("ascii")
        self.assertEqual(MODULE.decode_identity_key(encoded), key)

    def test_identity_file_must_be_external_and_has_exact_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repository = base / "repository"
            repository.mkdir()
            private_root = base / "private"
            private_root.mkdir()
            identity_file = private_root / "withdrawal-request.json"
            identity_file.write_text(
                json.dumps(
                    {"name": " 김현학 ", "phone": "010-1234-5678"},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                MODULE.load_normalized_identity(
                    identity_file,
                    repository,
                    private_root,
                ),
                ("김현학", "01012345678"),
            )
            inside = repository / "identity.json"
            inside.write_text(
                '{"name":"김현학","phone":"01012345678"}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "could not be read"):
                MODULE.load_normalized_identity(
                    inside,
                    repository,
                    private_root,
                )

    def test_versions_timestamps_and_invite_ids_are_strict(self) -> None:
        parsed = MODULE.parse_timestamp(
            "2026-09-03T10:02:03+09:00",
            "--request-received-at",
        )
        self.assertEqual(parsed, self.received_at)
        self.assertEqual(
            MODULE.parse_procedure_version(
                self.procedure_version,
                self.received_at,
            ),
            self.procedure_version,
        )
        with self.assertRaisesRegex(ValueError, "cannot follow"):
            MODULE.parse_procedure_version(
                "withdrawal-v2026-09-04-r1",
                self.received_at,
            )
        with self.assertRaises(ValueError):
            MODULE.parse_invite_id(
                "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"
            )
        self.assertEqual(
            MODULE.parse_invite_id(self.invite_id),
            self.invite_id,
        )

    def test_preview_is_read_only_and_confirmation_does_not_expose_ids(self) -> None:
        connection = FakeConnection(
            [{"contains": "set transaction isolation level repeatable read"}]
            + self.submitted_scope_steps(lock=False)
        )
        scope = MODULE.preview_withdrawal(
            connection,
            self.invite_selector(),
            self.digest,
        )
        self.assertEqual(
            scope.counts,
            {
                "submissions": 1,
                "responses": 12,
                "identities": 1,
                "invites": 1,
            },
        )
        phrase = MODULE.confirmation_phrase(
            scope,
            self.digest,
            self.procedure_version,
            self.received_at,
        )
        self.assertNotIn(self.invite_id, phrase)
        self.assertNotIn(self.participant_id, phrase)
        self.assertNotIn(self.submission_id, phrase)
        statements = " ".join(
            event[0] for event in connection.fake_cursor.events
        )
        self.assertIn("read only", statements)
        self.assertNotIn("for update", statements)
        self.assertNotIn("delete from", statements)
        self.assertNotIn("update ", statements)
        self.assertFalse(connection.fake_cursor.steps)

    def test_identity_selector_fails_closed_when_multiple_records_match(self) -> None:
        connection = FakeConnection(
            [
                {
                    "contains": "set transaction isolation level repeatable read",
                },
                {
                    "contains": "where identity_hmac",
                    "all": [(self.invite_id,), (str(uuid.uuid4()),)],
                },
            ]
        )
        selector = MODULE.Selector("normalized_identity", "b" * 64)
        with self.assertRaises(MODULE.AmbiguousTargetError):
            MODULE.preview_withdrawal(
                connection,
                selector,
                self.digest,
            )
        self.assertEqual(connection.rollbacks, 1)
        self.assertFalse(connection.fake_cursor.steps)

    def test_submitted_delete_locks_in_order_verifies_then_audits(self) -> None:
        preview_connection = FakeConnection(
            [{"contains": "set transaction isolation level repeatable read"}]
            + self.submitted_scope_steps(lock=False)
        )
        expected = MODULE.preview_withdrawal(
            preview_connection,
            self.invite_selector(),
            self.digest,
        )
        delete_connection = FakeConnection(
            [{"contains": "set transaction isolation level serializable"}]
            + self.submitted_scope_steps(
                lock=True,
                include_mutations=True,
            )
        )
        counts, event_id = MODULE.delete_withdrawn_participant(
            delete_connection,
            self.invite_selector(),
            self.digest,
            self.procedure_version,
            self.received_at,
            expected,
        )
        self.assertEqual(counts, expected.counts)
        self.assertEqual(str(uuid.UUID(event_id)), event_id)
        self.assertEqual(delete_connection.commits, 1)
        self.assertEqual(delete_connection.rollbacks, 0)
        statements = [
            event[0] for event in delete_connection.fake_cursor.events
        ]
        mutation_markers = (
            "update private.pilot_invites",
            "delete from research.pilot_responses",
            "delete from research.pilot_submissions",
            "delete from private.participant_identity",
            "delete from private.pilot_invites",
            "insert into private.pilot_withdrawal_events",
        )
        positions = [
            next(
                index
                for index, statement in enumerate(statements)
                if marker in statement
            )
            for marker in mutation_markers
        ]
        self.assertEqual(positions, sorted(positions))
        audit_parameters = delete_connection.fake_cursor.events[-1][1]
        self.assertEqual(audit_parameters[2], self.procedure_version)
        self.assertEqual(audit_parameters[3], "invite_id")
        for identifier in (
            self.invite_id,
            self.participant_id,
            self.submission_id,
        ):
            self.assertNotIn(identifier, audit_parameters)
        self.assertFalse(delete_connection.fake_cursor.steps)

    def test_row_count_mismatch_rolls_back_before_identity_delete_or_audit(self) -> None:
        preview_connection = FakeConnection(
            [{"contains": "set transaction isolation level repeatable read"}]
            + self.submitted_scope_steps(lock=False)
        )
        expected = MODULE.preview_withdrawal(
            preview_connection,
            self.invite_selector(),
            self.digest,
        )
        connection = FakeConnection(
            [{"contains": "set transaction isolation level serializable"}]
            + self.submitted_scope_steps(
                lock=True,
                response_delete_count=11,
                include_mutations=True,
            )
        )
        with self.assertRaisesRegex(
            MODULE.CountMismatchError,
            "response delete",
        ):
            MODULE.delete_withdrawn_participant(
                connection,
                self.invite_selector(),
                self.digest,
                self.procedure_version,
                self.received_at,
                expected,
            )
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)
        statements = " ".join(
            event[0] for event in connection.fake_cursor.events
        )
        self.assertNotIn(
            "delete from private.participant_identity",
            statements,
        )
        self.assertNotIn(
            "insert into private.pilot_withdrawal_events",
            statements,
        )
        self.assertFalse(connection.fake_cursor.steps)

    def test_unused_invite_deletes_only_invite_then_audits(self) -> None:
        tag = MODULE.invite_confirmation_tag(self.invite_id)
        expected = MODULE.WithdrawalScope(
            self.invite_id,
            None,
            None,
            "deleted_unused_invite",
            {
                "submissions": 0,
                "responses": 0,
                "identities": 0,
                "invites": 1,
            },
            tag,
        )
        connection = FakeConnection(
            [
                {"contains": "set transaction isolation level serializable"},
                {
                    "contains": "for update",
                    "one": (self.invite_id, self.digest, None, None),
                },
                {
                    "contains": "from private.participant_identity",
                    "all": [],
                },
                {
                    "contains": "from research.pilot_submissions",
                    "all": [],
                },
                {
                    "contains": "delete from private.pilot_invites",
                    "rowcount": 1,
                },
                {
                    "contains": "select (select count(*)",
                    "one": (0, 0, 0, 0),
                },
                {
                    "contains": "insert into private.pilot_withdrawal_events",
                    "rowcount": 1,
                },
            ]
        )
        counts, _ = MODULE.delete_withdrawn_participant(
            connection,
            self.invite_selector(),
            self.digest,
            self.procedure_version,
            self.received_at,
            expected,
        )
        self.assertEqual(counts, expected.counts)
        statements = " ".join(
            event[0] for event in connection.fake_cursor.events
        )
        self.assertNotIn("delete from research.pilot_responses", statements)
        self.assertNotIn("delete from research.pilot_submissions", statements)
        self.assertNotIn(
            "delete from private.participant_identity",
            statements,
        )
        self.assertFalse(connection.fake_cursor.steps)

    def test_database_driver_is_loaded_only_inside_connector(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        before_connector = source.split("def connect_database", 1)[0]
        self.assertNotIn("import psycopg", before_connector)
        self.assertIn(
            "import psycopg",
            source.split("def connect_database", 1)[1],
        )
        self.assertNotIn("--name", source)
        self.assertNotIn("--phone", source)


if __name__ == "__main__":
    unittest.main()
