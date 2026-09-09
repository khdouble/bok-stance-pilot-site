from __future__ import annotations

import copy
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock


ADMIN = Path(__file__).resolve().parents[1] / "admin"
if str(ADMIN) not in sys.path:
    sys.path.insert(0, str(ADMIN))
SCRIPT = ADMIN / "manage_fielding_gate.py"
TOOLS = SCRIPT.resolve().parents[2] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
import pi_config

SPEC = importlib.util.spec_from_file_location("manage_fielding_gate", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
EXPECTED_REF = "mebisrsvasrzwkmsodsw"


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

    def _seal_manifest(self, manifest: dict[str, object]) -> None:
        payload = {
            key: value
            for key, value in manifest.items()
            if key != "deployment_manifest_sha256"
        }
        manifest["deployment_manifest_sha256"] = MODULE.hashlib.sha256(
            MODULE._canonical_json(payload)
        ).hexdigest()

    def _write_live_release(
        self, repository: Path
    ) -> tuple[dict[str, object], Path, Path]:
        docs = repository / "docs"
        docs.mkdir(parents=True, exist_ok=True)
        transition = repository / MODULE.TRANSITION_MIGRATION_RELATIVE
        transition.parent.mkdir(parents=True, exist_ok=True)
        config_path = docs / "site-config.js"
        privacy_path = docs / "privacy.html"
        manifest_path = docs / "deployment-manifest.json"
        config_path.write_text(
            "fieldingEnabled: true,\n", encoding="utf-8", newline="\n"
        )
        privacy_path.write_text(
            "approved privacy notice\n",
            encoding="utf-8",
            newline="\n",
        )
        transition.write_text(
            "generated transition\n", encoding="utf-8", newline="\n"
        )
        (docs / "instrument.json").write_text(
            json.dumps(
                {
                    "instrument_sha256": self.digest,
                    "hosted_version": MODULE.EXPECTED_HOSTED_VERSION,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        manifest: dict[str, object] = {
            "deployment_manifest_sha256": "",
            "schema_version": "1.0",
            "deployment_state": "live",
            "site_url": MODULE.EXPECTED_SITE_URL,
            "api_url": MODULE.EXPECTED_API_URL,
            "hosted_version": MODULE.EXPECTED_HOSTED_VERSION,
            "instrument_sha256": self.digest,
            "operational_file_hashes": {
                "privacy_notice": MODULE._sha256_file(privacy_path),
                "site_config": MODULE._sha256_file(config_path),
            },
            "deployment_source_hashes": {
                "database_instrument_transition": MODULE._sha256_file(
                    transition
                )
            },
        }
        self._seal_manifest(manifest)
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return manifest, manifest_path, transition

    def _assert_live_release(self, repository: Path) -> None:
        with mock.patch.object(
            pi_config, "boolean_value", return_value=True
        ), mock.patch.object(
            pi_config, "has_placeholder", return_value=False
        ), mock.patch.object(
            pi_config, "validate_live_config", return_value=[]
        ):
            MODULE.assert_local_live_release(repository, self.digest)

    def test_parser_defaults_to_direct_backend(self) -> None:
        args = MODULE.parser().parse_args(
            [
                "status",
                "--instrument-sha256",
                self.digest,
            ]
        )
        self.assertEqual(args.db_backend, MODULE.DIRECT_BACKEND)

    def test_linked_backend_routes_status_open_and_close_without_db_url(
        self,
    ) -> None:
        class FakeLinkedBackend:
            def __init__(self) -> None:
                self.calls: list[tuple[object, ...]] = []

            def read_status(self, digest: str) -> dict[str, object]:
                self.calls.append(("status", digest))
                return {
                    "instrument_sha256": digest,
                    "instrument_version": "v260903-pilot-hosted-1",
                    "is_active": True,
                    "fielding_open": False,
                    "fielding_opened_at": "",
                    "fielding_closed_at": "",
                    "invite_count": 1,
                    "unrevoked_invite_count": 1,
                    "eligible_unused_invite_count": 1,
                    "used_invite_count": 0,
                    "submission_count": 0,
                    "identity_count": 0,
                    "response_count": 0,
                }

            def open_e2e(
                self, digest: str, version: str, invite_id: str
            ) -> None:
                self.calls.append(
                    ("open-e2e", digest, version, invite_id)
                )

            def close_e2e(self, digest: str, invite_id: str) -> None:
                self.calls.append(("close-e2e", digest, invite_id))

        backend = FakeLinkedBackend()
        cases = (
            ("status", None),
            ("open-e2e", self.invite_id),
            ("close-e2e", self.invite_id),
        )
        for action, invite_id in cases:
            with self.subTest(action=action):
                arguments = [
                    action,
                    "--db-backend",
                    MODULE.LINKED_CLI_BACKEND,
                    "--instrument-sha256",
                    self.digest,
                ]
                if invite_id is not None:
                    arguments.extend(["--invite-id", invite_id])
                    arguments.extend(
                        [
                            "--confirm",
                            MODULE.confirmation_phrase(
                                action, self.digest, invite_id
                            ),
                        ]
                    )
                with mock.patch.object(
                    MODULE, "LinkedCliBackend", return_value=backend
                ), mock.patch.object(
                    MODULE,
                    "resolve_project_ref",
                    return_value=EXPECTED_REF,
                ), mock.patch.object(
                    MODULE,
                    "local_instrument_version",
                    return_value="v260903-pilot-hosted-1",
                ), mock.patch.object(
                    MODULE, "required_database_url"
                ) as database_url, mock.patch.object(
                    MODULE, "connect_database"
                ) as connect:
                    result = MODULE.main(arguments)
                self.assertEqual(result, 0)
                database_url.assert_not_called()
                connect.assert_not_called()
        self.assertEqual(
            backend.calls,
            [
                ("status", self.digest),
                (
                    "open-e2e",
                    self.digest,
                    "v260903-pilot-hosted-1",
                    self.invite_id,
                ),
                ("close-e2e", self.digest, self.invite_id),
            ],
        )

    def test_linked_backend_rejects_unsupported_mutations_before_access(
        self,
    ) -> None:
        for action in ("open-production", "close"):
            with self.subTest(action=action):
                arguments = [
                    action,
                    "--db-backend",
                    MODULE.LINKED_CLI_BACKEND,
                    "--instrument-sha256",
                    self.digest,
                    "--confirm",
                    MODULE.confirmation_phrase(action, self.digest),
                ]
                with mock.patch.object(
                    MODULE, "LinkedCliBackend"
                ) as backend, mock.patch.object(
                    MODULE, "connect_database"
                ) as connect:
                    result = MODULE.main(arguments)
                self.assertEqual(result, 2)
                backend.assert_not_called()
                connect.assert_not_called()

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
                        (
                            self.invite_id,
                            "PILOT_R01",
                            True,
                            True,
                            "disposable_e2e",
                        ),
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
                        (
                            self.invite_id,
                            "PILOT_R01",
                            True,
                            True,
                            "disposable_e2e",
                        ),
                        (
                            extra_id,
                            "PILOT_R02",
                            True,
                            True,
                            "disposable_e2e",
                        ),
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
                "participant",
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

    def test_direct_open_rejects_credentials_with_wrong_purpose(self) -> None:
        for action, rows, invite_id in (
            (
                "open-e2e",
                [(
                    self.invite_id,
                    "PILOT_R01",
                    True,
                    True,
                    "pi_manual_test",
                )],
                self.invite_id,
            ),
            (
                "open-production",
                [
                    (
                        str(uuid.uuid4()),
                        assignment,
                        True,
                        True,
                        (
                            "pi_manual_test"
                            if assignment == "PILOT_R01"
                            else "participant"
                        ),
                    )
                    for assignment in MODULE.EXPECTED_ASSIGNMENTS
                ],
                None,
            ),
        ):
            with self.subTest(action=action):
                connection = FakeConnection([
                    {
                        "contains":
                            "set transaction isolation level serializable"
                    },
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
                ])
                with self.assertRaises(MODULE.GateError):
                    MODULE.mutate_gate(
                        connection,
                        action,
                        self.digest,
                        invite_id,
                    )
                self.assertEqual(connection.commits, 0)
                self.assertEqual(connection.rollbacks, 1)

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
        rendered = " ".join(statements)
        self.assertGreaterEqual(
            rendered.count("invite_purpose = 'disposable_e2e'"),
            2,
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

    def test_current_live_release_is_accepted_for_production(self) -> None:
        repository = SCRIPT.resolve().parents[2]
        current_hash = MODULE.json.loads(
            (repository / "docs" / "instrument.json").read_text(
                encoding="utf-8"
            )
        )["instrument_sha256"]
        MODULE.assert_local_live_release(repository, current_hash)

    def test_exact_live_release_manifest_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            self._write_live_release(repository)
            self._assert_live_release(repository)

    def test_live_release_manifest_contract_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            original, manifest_path, _ = self._write_live_release(repository)
            cases: list[tuple[str, dict[str, object], str | None]] = [
                ("schema version", {"schema_version": "2.0"}, None),
                ("deployment state", {"deployment_state": "staging"}, None),
                (
                    "site URL",
                    {"site_url": "https://attacker.invalid/"},
                    None,
                ),
                (
                    "API URL",
                    {"api_url": "https://attacker.invalid/pilot-api"},
                    None,
                ),
                (
                    "hosted version",
                    {"hosted_version": "v260903-pilot-hosted-3"},
                    None,
                ),
                ("instrument hash", {"instrument_sha256": "b" * 64}, None),
                (
                    "operational hashes",
                    {"operational_file_hashes": {"privacy_notice": "c" * 64}},
                    None,
                ),
                (
                    "transition hash",
                    {
                        "deployment_source_hashes": {
                            "database_instrument_transition": "d" * 64
                        }
                    },
                    None,
                ),
                (
                    "extra transition source",
                    {
                        "deployment_source_hashes": {
                            "database_instrument_transition": (
                                original["deployment_source_hashes"][
                                    "database_instrument_transition"
                                ]
                            ),
                            "unexpected": "e" * 64,
                        }
                    },
                    None,
                ),
                ("missing top-level key", {}, "api_url"),
                ("extra top-level key", {"unexpected": True}, None),
            ]
            for name, changes, removed_key in cases:
                with self.subTest(name=name):
                    candidate = copy.deepcopy(original)
                    if removed_key is not None:
                        candidate.pop(removed_key)
                    candidate.update(changes)
                    self._seal_manifest(candidate)
                    manifest_path.write_text(
                        json.dumps(candidate, indent=2) + "\n",
                        encoding="utf-8",
                        newline="\n",
                    )
                    with self.assertRaises(MODULE.GateError):
                        self._assert_live_release(repository)

    def test_live_release_rejects_stale_transition_and_self_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            manifest, manifest_path, transition = self._write_live_release(
                repository
            )
            transition.write_text(
                "shape-preserving but unbound transition\n",
                encoding="utf-8",
                newline="\n",
            )
            with self.assertRaises(MODULE.GateError):
                self._assert_live_release(repository)

            manifest, manifest_path, _ = self._write_live_release(repository)
            manifest["deployment_manifest_sha256"] = "f" * 64
            manifest_path.write_text(
                json.dumps(manifest, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            with self.assertRaises(MODULE.GateError):
                self._assert_live_release(repository)

    def test_live_release_rejects_duplicate_manifest_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            manifest, manifest_path, _ = self._write_live_release(repository)
            encoded = json.dumps(manifest, indent=2)
            encoded = encoded.replace(
                '  "schema_version": "1.0",',
                (
                    '  "schema_version": "1.0",\n'
                    '  "schema_version": "1.0",'
                ),
                1,
            )
            manifest_path.write_text(
                encoded + "\n", encoding="utf-8", newline="\n"
            )
            with self.assertRaises(MODULE.GateError) as raised:
                self._assert_live_release(repository)
            self.assertIn(
                "duplicate JSON key", str(raised.exception.__cause__)
            )

    def test_live_release_rejects_non_v2_local_instrument(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            self._write_live_release(repository)
            (repository / "docs" / "instrument.json").write_text(
                json.dumps(
                    {
                        "instrument_sha256": self.digest,
                        "hosted_version": "v260903-pilot-hosted-3",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
                newline="\n",
            )
            with self.assertRaises(MODULE.GateError):
                self._assert_live_release(repository)

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
