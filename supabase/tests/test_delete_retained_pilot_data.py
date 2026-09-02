from __future__ import annotations

import contextlib
import importlib.util
import io
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "admin" / "delete_retained_pilot_data.py"
SPEC = importlib.util.spec_from_file_location("delete_retained_pilot_data", SCRIPT)
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
            raise AssertionError(f"expected SQL containing {marker!r}, got {normalized!r}")
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


class DeleteRetainedPilotDataTest(unittest.TestCase):
    digest = "a" * 64
    cutoff = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)

    def test_cutoff_requires_timezone_and_normalizes_to_utc(self) -> None:
        parsed = MODULE.parse_cutoff("2026-10-01T09:00:00+09:00")
        self.assertEqual(parsed, self.cutoff)
        fractional = MODULE.parse_cutoff("2026-10-01T09:00:00.123456+09:00")
        self.assertEqual(MODULE.utc_text(fractional), "2026-10-01T00:00:00.123456Z")
        with self.assertRaisesRegex(ValueError, "explicit timezone"):
            MODULE.parse_cutoff("2026-10-01T00:00:00")
        with self.assertRaises(ValueError):
            MODULE.parse_instrument_sha256("A" * 64)

    def test_parser_requires_exactly_one_explicit_mode(self) -> None:
        scope = [
            "--cutoff",
            "2026-10-01T00:00:00Z",
            "--instrument-sha256",
            self.digest,
        ]
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                MODULE.parser().parse_args(scope)
            with self.assertRaises(SystemExit):
                MODULE.parser().parse_args(scope + ["--dry-run", "--confirm", "anything"])

    def test_wrong_confirmation_is_rejected_before_database_access(self) -> None:
        stderr = io.StringIO()
        arguments = [
            "--cutoff",
            "2026-10-01T09:00:00+09:00",
            "--instrument-sha256",
            self.digest,
            "--confirm",
            "DELETE SOMETHING",
        ]
        with mock.patch.object(MODULE, "connect_database") as connect:
            with mock.patch.dict(MODULE.os.environ, {}, clear=True):
                with contextlib.redirect_stderr(stderr):
                    result = MODULE.main(arguments)
        self.assertEqual(result, 2)
        connect.assert_not_called()
        expected = MODULE.confirmation_phrase(self.digest, self.cutoff)
        self.assertIn(expected, stderr.getvalue())

    def test_dry_run_is_a_read_only_count_transaction(self) -> None:
        connection = FakeConnection([
            {"contains": "set transaction read only"},
            {"contains": "with target_submissions", "one": (2, 24, 2, 2)},
        ])
        counts = MODULE.preview_deletion(connection, self.digest, self.cutoff)
        self.assertEqual(
            counts,
            {"submissions": 2, "responses": 24, "identities": 2, "invites": 2},
        )
        self.assertEqual(connection.commits, 1)
        self.assertEqual(connection.rollbacks, 0)
        sql = " ".join(event[0] for event in connection.fake_cursor.events)
        self.assertNotIn("delete from", sql)
        self.assertNotIn("update ", sql)
        count_parameters = connection.fake_cursor.events[1][1]
        self.assertEqual(count_parameters, (self.digest, self.cutoff))
        self.assertFalse(connection.fake_cursor.steps)

    def test_confirmed_delete_locks_unlinks_deletes_in_order_and_verifies(self) -> None:
        submission_rows = [
            ("submission-1", "participant-1", "invite-1"),
            ("submission-2", "participant-2", "invite-2"),
        ]
        connection = FakeConnection([
            {"contains": "for update", "all": submission_rows},
            {"contains": "from private.pilot_invites", "all": [("invite-1",), ("invite-2",)]},
            {"contains": "from research.pilot_responses", "one": (24, 2, 2)},
            {"contains": "update private.pilot_invites", "rowcount": 2},
            {"contains": "delete from research.pilot_responses", "rowcount": 24},
            {"contains": "delete from research.pilot_submissions", "rowcount": 2},
            {"contains": "delete from private.participant_identity", "rowcount": 2},
            {"contains": "delete from private.pilot_invites", "rowcount": 2},
            {"contains": "select (select count(*)", "one": (0, 0, 0, 0)},
        ])
        counts = MODULE.delete_retained_data(connection, self.digest, self.cutoff)
        self.assertEqual(
            counts,
            {"submissions": 2, "responses": 24, "identities": 2, "invites": 2},
        )
        self.assertEqual(connection.commits, 1)
        self.assertEqual(connection.rollbacks, 0)
        statements = [event[0] for event in connection.fake_cursor.events]
        mutation_order = [
            next(index for index, sql in enumerate(statements) if sql.startswith("update private.pilot_invites")),
            next(index for index, sql in enumerate(statements) if sql.startswith("delete from research.pilot_responses")),
            next(index for index, sql in enumerate(statements) if sql.startswith("delete from research.pilot_submissions")),
            next(index for index, sql in enumerate(statements) if sql.startswith("delete from private.participant_identity")),
            next(index for index, sql in enumerate(statements) if sql.startswith("delete from private.pilot_invites")),
        ]
        self.assertEqual(mutation_order, sorted(mutation_order))
        self.assertIn("set submission_id = null", statements[3])
        self.assertFalse(connection.fake_cursor.steps)

    def test_row_count_mismatch_rolls_back_and_stops_later_deletes(self) -> None:
        connection = FakeConnection([
            {"contains": "for update", "all": [("submission-1", "participant-1", "invite-1")]},
            {"contains": "from private.pilot_invites", "all": [("invite-1",)]},
            {"contains": "from research.pilot_responses", "one": (12, 1, 1)},
            {"contains": "update private.pilot_invites", "rowcount": 1},
            {"contains": "delete from research.pilot_responses", "rowcount": 12},
            {"contains": "delete from research.pilot_submissions", "rowcount": 0},
        ])
        with self.assertRaisesRegex(MODULE.CountMismatchError, "submission delete"):
            MODULE.delete_retained_data(connection, self.digest, self.cutoff)
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)
        executed = " ".join(event[0] for event in connection.fake_cursor.events)
        self.assertNotIn("delete from private.participant_identity", executed)
        self.assertNotIn("delete from private.pilot_invites", executed)
        self.assertFalse(connection.fake_cursor.steps)

    def test_database_driver_is_loaded_only_inside_connector(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        before_connector = source.split("def connect_database", 1)[0]
        self.assertNotIn("import psycopg", before_connector)
        self.assertIn("import psycopg", source.split("def connect_database", 1)[1])


if __name__ == "__main__":
    unittest.main()
