from __future__ import annotations

import contextlib
import importlib.util
import io
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "admin"
    / "manage_fielding_gate.py"
)
SPEC = importlib.util.spec_from_file_location("manage_fielding_gate", SCRIPT)
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

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self)

    def cursor(self) -> FakeCursor:
        return self.fake_cursor


class ManageFieldingGateTest(unittest.TestCase):
    digest = "a" * 64
    invite_id = str(uuid.UUID("11111111-1111-4111-8111-111111111111"))

    def test_status_is_read_only(self) -> None:
        connection = FakeConnection(
            [
                {"contains": "set transaction read only"},
                {
                    "contains": "from research.pilot_instruments",
                    "one": (True, False, None, None),
                },
                {
                    "contains": "from private.pilot_invites",
                    "one": (1, 1, 1, 0),
                },
                {
                    "contains": "from research.pilot_submissions",
                    "one": (0,),
                },
            ]
        )
        status = MODULE.read_gate_status(connection, self.digest)
        self.assertTrue(status["is_active"])
        self.assertFalse(status["fielding_open"])
        self.assertEqual(status["eligible_unused_invite_count"], 1)
        self.assertEqual(status["submission_count"], 0)
        statements = " ".join(
            sql for sql, _ in connection.fake_cursor.events
        )
        self.assertNotIn("update ", statements)
        self.assertEqual(connection.commits, 1)
        self.assertFalse(connection.fake_cursor.steps)

    def test_open_e2e_requires_exactly_the_named_only_eligible_invite(self) -> None:
        connection = FakeConnection(
            [
                {"contains": "set transaction isolation level serializable"},
                {
                    "contains": "from research.pilot_instruments",
                    "one": (True, False, None, None),
                },
                {
                    "contains": "from private.pilot_invites",
                    "all": [
                        (self.invite_id, "PILOT_R01", True, True),
                    ],
                },
                {
                    "contains": "from research.pilot_submissions",
                    "one": (0,),
                },
                {
                    "contains": "update research.pilot_instruments",
                    "rowcount": 1,
                },
                {
                    "contains": "select fielding_open",
                    "one": (True,),
                },
            ]
        )
        MODULE.mutate_gate(
            connection,
            "open-e2e",
            self.digest,
            self.invite_id,
        )
        self.assertEqual(connection.commits, 1)
        self.assertEqual(connection.rollbacks, 0)
        self.assertFalse(connection.fake_cursor.steps)

        extra_id = str(
            uuid.UUID("22222222-2222-4222-8222-222222222222")
        )
        refused = FakeConnection(
            [
                {"contains": "set transaction isolation level serializable"},
                {
                    "contains": "from research.pilot_instruments",
                    "one": (True, False, None, None),
                },
                {
                    "contains": "from private.pilot_invites",
                    "all": [
                        (self.invite_id, "PILOT_R01", True, True),
                        (extra_id, "PILOT_R02", True, True),
                    ],
                },
                {
                    "contains": "from research.pilot_submissions",
                    "one": (0,),
                },
            ]
        )
        with self.assertRaisesRegex(MODULE.GateError, "exactly"):
            MODULE.mutate_gate(
                refused,
                "open-e2e",
                self.digest,
                self.invite_id,
            )
        self.assertEqual(refused.commits, 0)
        self.assertEqual(refused.rollbacks, 1)

    def test_open_production_requires_exact_assignment_set_and_no_data(self) -> None:
        rows = [
            (
                str(uuid.uuid4()),
                assignment,
                True,
                True,
            )
            for assignment in MODULE.EXPECTED_ASSIGNMENTS
        ]
        connection = FakeConnection(
            [
                {"contains": "set transaction isolation level serializable"},
                {
                    "contains": "from research.pilot_instruments",
                    "one": (True, False, None, None),
                },
                {
                    "contains": "from private.pilot_invites",
                    "all": rows,
                },
                {
                    "contains": "from research.pilot_submissions",
                    "one": (0,),
                },
                {
                    "contains": "update research.pilot_instruments",
                    "rowcount": 1,
                },
                {
                    "contains": "select fielding_open",
                    "one": (True,),
                },
            ]
        )
        MODULE.mutate_gate(
            connection,
            "open-production",
            self.digest,
            None,
        )
        self.assertEqual(connection.commits, 1)

        with_existing_data = FakeConnection(
            [
                {"contains": "set transaction isolation level serializable"},
                {
                    "contains": "from research.pilot_instruments",
                    "one": (True, False, None, None),
                },
                {
                    "contains": "from private.pilot_invites",
                    "all": rows,
                },
                {
                    "contains": "from research.pilot_submissions",
                    "one": (1,),
                },
            ]
        )
        with self.assertRaisesRegex(MODULE.GateError, "zero existing"):
            MODULE.mutate_gate(
                with_existing_data,
                "open-production",
                self.digest,
                None,
            )
        self.assertEqual(with_existing_data.rollbacks, 1)

    def test_close_e2e_closes_gate_and_revokes_invite_atomically(self) -> None:
        connection = FakeConnection(
            [
                {"contains": "set transaction isolation level serializable"},
                {
                    "contains": "from research.pilot_instruments",
                    "one": (True, True, None, None),
                },
                {
                    "contains": "update research.pilot_instruments",
                    "rowcount": 1,
                },
                {
                    "contains": "update private.pilot_invites",
                    "rowcount": 1,
                },
                {
                    "contains": "select revoked_at is not null",
                    "one": (True,),
                },
                {
                    "contains": "select fielding_open",
                    "one": (False,),
                },
            ]
        )
        MODULE.mutate_gate(
            connection,
            "close-e2e",
            self.digest,
            self.invite_id,
        )
        self.assertEqual(connection.commits, 1)
        self.assertEqual(connection.rollbacks, 0)
        statements = [
            sql for sql, _ in connection.fake_cursor.events
        ]
        self.assertTrue(
            statements[0].startswith(
                "set transaction isolation level serializable"
            )
        )
        self.assertLess(
            next(
                index
                for index, sql in enumerate(statements)
                if sql.startswith("update research.pilot_instruments")
            ),
            next(
                index
                for index, sql in enumerate(statements)
                if sql.startswith("update private.pilot_invites")
            ),
        )

    def test_confirmation_is_checked_before_database_access(self) -> None:
        stderr = io.StringIO()
        with mock.patch.object(MODULE, "connect_database") as connect:
            with mock.patch.dict(
                MODULE.os.environ,
                {"SUPABASE_DB_URL": "postgresql://server/db"},
                clear=True,
            ):
                with contextlib.redirect_stderr(stderr):
                    result = MODULE.main(
                        [
                            "open-e2e",
                            "--instrument-sha256",
                            self.digest,
                            "--invite-id",
                            self.invite_id,
                            "--confirm",
                            "wrong",
                        ]
                    )
        self.assertEqual(result, 2)
        connect.assert_not_called()
        self.assertIn(
            MODULE.confirmation_phrase(
                "open-e2e", self.digest, self.invite_id
            ),
            stderr.getvalue(),
        )

    def test_current_staging_release_cannot_open_production(self) -> None:
        repository = SCRIPT.resolve().parents[2]
        current_hash = MODULE.json.loads(
            (repository / "docs" / "instrument.json").read_text(
                encoding="utf-8"
            )
        )["instrument_sha256"]
        with self.assertRaisesRegex(MODULE.GateError, "not a validated live"):
            MODULE.assert_local_live_release(repository, current_hash)

    def test_database_driver_is_loaded_only_inside_connector(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        before_connector = source.split("def connect_database", 1)[0]
        self.assertNotIn("import psycopg", before_connector)
        self.assertIn(
            "import psycopg",
            source.split("def connect_database", 1)[1],
        )

    def test_project_ref_defaults_to_link_and_explicit_must_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            linked_dir = repository / "supabase" / ".temp"
            linked_dir.mkdir(parents=True)
            ref = "a" * 20
            (linked_dir / "project-ref").write_text(
                ref + "\n", encoding="utf-8"
            )
            self.assertEqual(
                MODULE.resolve_project_ref(repository, None),
                ref,
            )
            self.assertEqual(
                MODULE.resolve_project_ref(repository, ref),
                ref,
            )
            with self.assertRaisesRegex(ValueError, "does not match"):
                MODULE.resolve_project_ref(repository, "b" * 20)

        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            with self.assertRaisesRegex(ValueError, "--project-ref"):
                MODULE.resolve_project_ref(repository, None)
            self.assertEqual(
                MODULE.resolve_project_ref(repository, "c" * 20),
                "c" * 20,
            )

    def test_database_url_is_pinned_without_leaking_rejected_value(self) -> None:
        ref = "d" * 20
        scheme = "postgres" + "ql://"
        password = "sec" + "ret"
        direct = (
            scheme
            + f"postgres:{password}@db.{ref}.supabase.co:5432/postgres"
        )
        pooled = (
            scheme
            + f"postgres.{ref}:{password}@aws-0-region.pooler.supabase.com:6543/postgres"
        )
        self.assertEqual(
            MODULE.required_database_url({"SUPABASE_DB_URL": direct}, ref),
            direct,
        )
        self.assertEqual(
            MODULE.required_database_url({"SUPABASE_DB_URL": pooled}, ref),
            pooled,
        )
        rejected = (
            scheme
            + f"postgres.{ref}:do-"
            + "not-print@attacker.example:5432/postgres"
        )
        with self.assertRaises(ValueError) as raised:
            MODULE.required_database_url(
                {"SUPABASE_DB_URL": rejected}, ref
            )
        self.assertNotIn(rejected, str(raised.exception))
        self.assertNotIn("do-not-print", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
