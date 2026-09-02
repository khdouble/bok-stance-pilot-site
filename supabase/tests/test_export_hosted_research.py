from __future__ import annotations

import importlib.util
import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "supabase" / "admin" / "export_hosted_research.py"
SPEC = importlib.util.spec_from_file_location("export_hosted_research", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class Column:
    def __init__(self, name: str) -> None:
        self.name = name


class Cursor:
    def __init__(self, columns: tuple[str, ...], rows: list[tuple[object, ...]]) -> None:
        self.description = [Column(name) for name in columns]
        self._rows = rows

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows


class Transaction:
    def __init__(self, connection: "Connection") -> None:
        self.connection = connection

    def __enter__(self) -> None:
        self.connection.transaction_entered = True

    def __exit__(self, exception_type: object, exception: object, traceback: object) -> None:
        self.connection.transaction_exited = True


class Connection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.transaction_entered = False
        self.transaction_exited = False

    def transaction(self) -> Transaction:
        return Transaction(self)

    def execute(self, query: str, parameters: object = None) -> Cursor:
        self.calls.append((query, parameters))
        if "from research.pilot_responses" in query:
            return Cursor(MODULE.COLLECTOR.RESPONSE_COLUMNS, [])
        if "from research.pilot_submissions" in query:
            return Cursor(MODULE.COLLECTOR.FEEDBACK_COLUMNS, [])
        return Cursor((), [])


class ExportHostedResearchTest(unittest.TestCase):
    def test_queries_are_bound_and_pii_free(self) -> None:
        for path in (MODULE.RESPONSE_QUERY_PATH, MODULE.FEEDBACK_QUERY_PATH):
            query = MODULE.query_text(path)
            self.assertEqual(query.count("%s"), 2)
            self.assertIsNone(re.search(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", query.lower()))
            for forbidden in (
                "private.", "participant_id", "invite_id", "identity_ciphertext",
                "identity_hmac", "token_hmac", "client_user_agent", "name", "phone",
            ):
                self.assertNotIn(forbidden, query.lower())

    def test_snapshot_uses_one_repeatable_read_transaction_and_bound_identity(self) -> None:
        connection = Connection()
        instrument_hash = "a" * 64
        responses, feedback = MODULE.fetch_snapshot(
            connection, instrument_hash, MODULE.COLLECTOR.HOSTED_VERSION,
        )
        self.assertEqual(responses, [])
        self.assertEqual(feedback, [])
        self.assertTrue(connection.transaction_entered)
        self.assertTrue(connection.transaction_exited)
        self.assertIn("repeatable read read only", connection.calls[0][0].lower())
        bound_calls = [parameters for _, parameters in connection.calls if parameters is not None]
        self.assertEqual(bound_calls, [
            (instrument_hash, MODULE.COLLECTOR.HOSTED_VERSION),
            (instrument_hash, MODULE.COLLECTOR.HOSTED_VERSION),
        ])

    def test_database_url_is_server_environment_only(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be set"):
            MODULE.required_database_url({})
        with self.assertRaisesRegex(ValueError, "PostgreSQL URI"):
            MODULE.required_database_url({"SUPABASE_DB_URL": "not-a-url"})
        value = "postgresql://server.example/postgres?sslmode=require"
        self.assertEqual(MODULE.required_database_url({"SUPABASE_DB_URL": value}), value)

    def test_database_values_have_strict_csv_representation(self) -> None:
        korea = timezone(timedelta(hours=9))
        moment = datetime(2026, 9, 3, 9, 0, 0, 123456, tzinfo=korea)
        self.assertEqual(MODULE.csv_value(moment), "2026-09-03T00:00:00.123Z")
        self.assertEqual(MODULE.csv_value(True), "true")
        self.assertEqual(MODULE.csv_value(False), "false")
        self.assertEqual(MODULE.csv_value(None), "")
        with self.assertRaisesRegex(ValueError, "timezone-naive"):
            MODULE.csv_value(datetime(2026, 9, 3))

    def test_cursor_header_drift_fails_closed(self) -> None:
        class BadConnection:
            def execute(self, query: str, parameters: object) -> Cursor:
                return Cursor(("submission_id", "name"), [("id", "PII")])

        with self.assertRaisesRegex(ValueError, "columns differ"):
            MODULE.execute_exact_query(
                BadConnection(), "select", ("a" * 64, "version"),
                MODULE.COLLECTOR.FEEDBACK_COLUMNS,
            )


if __name__ == "__main__":
    unittest.main()
