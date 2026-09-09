from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
TOOLS = REPOSITORY / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from tools import render_instrument_transition as module


def rehash(instrument: dict[str, object]) -> None:
    basis = dict(instrument)
    basis.pop("instrument_sha256", None)
    instrument["instrument_sha256"] = hashlib.sha256(
        module.canonical_json(basis)
    ).hexdigest()


def current_v2(repository: Path = REPOSITORY) -> dict[str, object]:
    instrument = json.loads(
        (REPOSITORY / "docs" / "instrument.json").read_text(encoding="utf-8")
    )
    instrument["hosted_version"] = module.NEXT_HOSTED_VERSION
    instrument["release_source_hashes"] = {
        name: hashlib.sha256((repository / relative).read_bytes()).hexdigest()
        for name, relative in module.RELEASE_SOURCE_PATHS.items()
    }
    rehash(instrument)
    return instrument


def prepare_repository(
    repository: Path,
    previous: dict[str, object],
) -> tuple[Path, Path, Path, Path]:
    for relative in module.RELEASE_SOURCE_PATHS.values():
        target = repository / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((REPOSITORY / relative).read_bytes())
    baseline_path = repository / module.BASELINE_SEED_RELATIVE
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_bytes(
        (REPOSITORY / module.BASELINE_SEED_RELATIVE).read_bytes()
    )
    previous_path = repository / module.PREVIOUS_INSTRUMENT_RELATIVE
    current_path = repository / module.CURRENT_INSTRUMENT_RELATIVE
    output_path = repository / module.TRANSITION_MIGRATION_RELATIVE
    previous_path.parent.mkdir(parents=True, exist_ok=True)
    current_path.parent.mkdir(parents=True, exist_ok=True)
    previous_path.write_text(
        json.dumps(previous, ensure_ascii=False), encoding="utf-8"
    )
    current_path.write_text(
        json.dumps(current_v2(repository), ensure_ascii=False), encoding="utf-8"
    )
    return previous_path, current_path, output_path, baseline_path


def bidirectional_except(
    expected: tuple[tuple[object, ...], ...],
    actual: tuple[tuple[object, ...], ...],
) -> tuple[set[tuple[object, ...]], set[tuple[object, ...]]]:
    return set(actual) - set(expected), set(expected) - set(actual)


@unittest.skip(
    "The H1-to-H2 transition renderer is a frozen historical artifact; active R5/H6 release coverage is exercised by the R5 builders and deployment tests."
)
class RenderInstrumentTransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous = module.load_json(
            REPOSITORY / module.PREVIOUS_INSTRUMENT_RELATIVE
        )
        self.current = current_v2()

    def test_valid_transition_is_deterministic_and_has_exact_shape(self) -> None:
        validated = module.validate_transition(
            copy.deepcopy(self.previous), copy.deepcopy(self.current), REPOSITORY
        )
        first = module.render_sql(validated)
        second = module.render_sql(validated)
        self.assertEqual(first, second)
        self.assertTrue(first.endswith("\n"))
        self.assertEqual(len(validated["current"]["items"]), 12)
        self.assertEqual(len(validated["current"]["assignments"]), 5)
        self.assertEqual(
            sum(len(rows) for rows in validated["current"]["assignments"].values()),
            60,
        )

    def test_sql_is_serializable_locked_transactional_and_append_only(self) -> None:
        sql = module.render_sql(
            module.validate_transition(self.previous, self.current, REPOSITORY)
        )
        lowered = sql.lower()
        self.assertEqual(lowered.count("begin;"), 1)
        self.assertEqual(lowered.count("commit;"), 1)
        self.assertIn("set transaction isolation level serializable;", lowered)
        self.assertIn("in access exclusive mode;", lowered)
        for table in (
            "research.pilot_instruments",
            "research.pilot_items",
            "research.pilot_assignment_sets",
            "research.pilot_assignments",
            *module.MUTABLE_TABLES,
        ):
            self.assertIn(table, lowered)
        precheck = lowered.index("$instrument_transition_precheck$")
        first_insert = lowered.index("insert into research.pilot_instruments")
        seed_check = lowered.index("$instrument_transition_seed_check$")
        activation = lowered.index("$instrument_transition_activation$")
        old_off = lowered.index("set is_active = false", activation)
        new_on = lowered.index("set is_active = true", old_off)
        postcheck = lowered.index("$instrument_transition_postcheck$", new_on)
        commit = lowered.rindex("commit;")
        self.assertLess(precheck, first_insert)
        self.assertLess(first_insert, seed_check)
        self.assertLess(seed_check, old_off)
        self.assertLess(old_off, new_on)
        self.assertLess(new_on, postcheck)
        self.assertLess(postcheck, commit)
        self.assertIn("12,\n  false,\n  false", lowered)
        self.assertEqual(lowered.count("get diagnostics"), 2)
        self.assertEqual(lowered.count("row_count"), 2)
        self.assertNotRegex(lowered, r"(?m)^\s*(?:delete|truncate)\s")
        self.assertNotRegex(lowered, r"\bon\s+conflict\b")
        self.assertNotRegex(
            lowered,
            r"update\s+research\.pilot_(?:items|assignment_sets|assignments)",
        )

    def test_exact_migration_004_contract_is_guarded_before_permanent_dml(self) -> None:
        sql = module.render_sql(
            module.validate_transition(self.previous, self.current, REPOSITORY)
        ).lower()
        lock_position = sql.index("lock table")
        contract_position = sql.index(
            "create temporary table _pilot_expected_invite_004_contract"
        )
        precheck_position = sql.index("do $instrument_transition_precheck$")
        first_insert = sql.index("insert into research.pilot_instruments")
        self.assertLess(lock_position, contract_position)
        self.assertLess(contract_position, precheck_position)
        self.assertLess(precheck_position, first_insert)
        self.assertEqual(sql.count("on commit drop"), 2)
        for column in ("invite_purpose", "admin_id_hmac"):
            self.assertIn(f"attname = '{column}'", sql)
        self.assertIn(
            "attname in ('invite_purpose', 'admin_id_hmac')", sql
        )
        for constraint in (
            "pilot_invites_purpose_ck",
            "pilot_invites_admin_auth_ck",
            "pilot_submissions_role_ck",
        ):
            self.assertIn(f"actual.conname = '{constraint}'", sql)
        for index in (
            "pilot_invites_admin_id_hmac_idx",
            "pilot_invites_one_outstanding_pi_manual_test_idx",
        ):
            self.assertIn(index, sql)
        for catalog_assertion in (
            "actual.convalidated",
            "actual.indisunique",
            "actual.indisvalid",
            "actual.indisready",
            "actual.indislive",
            "pg_get_expr(actual.conbin",
            "pg_get_indexdef(actual.indexrelid, 1, true)",
            "pg_get_expr(actual.indpred",
        ):
            self.assertIn(catalog_assertion, sql)
        for error in ("columns", "checks", "indexes"):
            self.assertIn(
                f"instrument transition requires exact migration 004 {error}",
                sql,
            )

    def test_replay_and_all_zero_guards_precede_every_mutation(self) -> None:
        sql = module.render_sql(
            module.validate_transition(self.previous, self.current, REPOSITORY)
        ).lower()
        first_insert = sql.index("insert into research.pilot_instruments")
        replay = sql.index("instrument transition target already exists")
        zero = sql.index("instrument transition requires all mutable pilot tables")
        singleton = sql.index("requires exactly one baseline instrument")
        self.assertLess(singleton, first_insert)
        self.assertLess(replay, first_insert)
        self.assertLess(zero, first_insert)
        for table in module.MUTABLE_TABLES:
            self.assertGreaterEqual(sql.count(f"select count(*) from {table}"), 2)
        self.assertIn(
            "select count(*) from research.pilot_instruments) <> 2", sql
        )

    def test_exact_h1_h2_identity_closed_state_and_shape_are_rendered(self) -> None:
        sql = module.render_sql(
            module.validate_transition(self.previous, self.current, REPOSITORY)
        )
        lowered = sql.lower()
        self.assertGreaterEqual(
            lowered.count(module.PREVIOUS_INSTRUMENT_SHA256), 5
        )
        self.assertGreaterEqual(
            lowered.count(str(self.current["instrument_sha256"])), 8
        )
        self.assertGreaterEqual(
            lowered.count(module.PREVIOUS_HOSTED_VERSION), 3
        )
        self.assertGreaterEqual(lowered.count(module.NEXT_HOSTED_VERSION), 4)
        self.assertIn(
            "assignment_code in ('pilot_r01', 'pilot_r02', 'pilot_r03', "
            "'pilot_r04', 'pilot_r05')",
            lowered,
        )
        self.assertIn("and expected_item_count = 12", lowered)
        self.assertGreaterEqual(lowered.count("fielding_opened_at is null"), 3)
        self.assertGreaterEqual(lowered.count("fielding_closed_at is null"), 3)
        self.assertGreaterEqual(lowered.count("activated_at is not null"), 3)
        self.assertIn("activated_at is null", lowered)
        self.assertIn("and created_at is not null", lowered)
        self.assertIn("and activated_at = created_at", lowered)

    def test_h1_closed_lifecycle_truth_table_and_h2_starts_never_opened(
        self,
    ) -> None:
        sql = module.render_sql(
            module.validate_transition(self.previous, self.current, REPOSITORY)
        )
        normalize = lambda value: " ".join(value.lower().split())
        normalized_sql = normalize(sql)
        normalized_predicate = normalize(
            module.H1_CLOSED_LIFECYCLE_PREDICATE
        )
        self.assertEqual(normalized_sql.count(normalized_predicate), 2)

        cases = (
            ("never opened", None, None, True),
            ("completed cycle", 1, 2, True),
            ("zero-duration completed cycle", 1, 1, True),
            ("closed without open", None, 2, False),
            ("open without close", 1, None, False),
            ("reversed cycle", 2, 1, False),
        )
        with sqlite3.connect(":memory:") as connection:
            query = (
                "select "
                + module.H1_CLOSED_LIFECYCLE_PREDICATE
                + " from (select ? as fielding_opened_at, "
                + "? as fielding_closed_at)"
            )
            for name, opened_at, closed_at, expected in cases:
                with self.subTest(name=name):
                    row = connection.execute(
                        query, (opened_at, closed_at)
                    ).fetchone()
                    self.assertIsNotNone(row)
                    self.assertIs(bool(row[0]), expected)

        seed_start = normalized_sql.index(
            "do $instrument_transition_seed_check$"
        )
        activation_start = normalized_sql.index(
            "do $instrument_transition_activation$", seed_start
        )
        target_seed = normalized_sql[seed_start:activation_start]
        self.assertIn("and fielding_open is false", target_seed)
        self.assertIn(
            "and activated_at is null "
            "and fielding_opened_at is null "
            "and fielding_closed_at is null",
            target_seed,
        )

    def test_exact_h1_rows_use_bidirectional_except_before_h2_insert(self) -> None:
        validated = module.validate_transition(
            self.previous, self.current, REPOSITORY
        )
        sql = module.render_sql(validated)
        lowered = sql.lower()
        lock_position = lowered.index("lock table")
        contract_position = lowered.index("with expected_h1_items")
        first_insert = lowered.index("insert into research.pilot_instruments")
        self.assertLess(lock_position, contract_position)
        self.assertLess(contract_position, first_insert)
        self.assertEqual(lowered.count("\n      except\n"), 6)
        for contract_name in (
            "expected_h1_items",
            "actual_h1_items",
            "expected_h1_assignment_sets",
            "actual_h1_assignment_sets",
            "expected_h1_assignments",
            "actual_h1_assignments",
        ):
            self.assertIn(contract_name, lowered)
        for error in (
            "baseline item rows drifted",
            "baseline assignment-set rows drifted",
            "baseline assignment rows drifted",
        ):
            self.assertIn(error, lowered)
        self.assertGreaterEqual(
            lowered.count("created_at = instrument.created_at"), 2
        )

        contract = module.baseline_semantic_contract(validated["current"])
        item_id, sentence = contract["items"][0]
        self.assertIn(
            f"({module.sql_text(item_id)}, {module.sql_text(sentence)}, true)",
            sql,
        )
        code, position, item_id, assignment_id = contract["assignments"][0]
        self.assertIn(
            "("
            + ", ".join(
                (
                    module.sql_text(code),
                    f"{position}::smallint",
                    module.sql_text(item_id),
                    module.sql_text(assignment_id),
                )
            )
            + ")",
            sql,
        )

    def test_shape_preserving_h1_sentence_drift_fails_exact_contract(self) -> None:
        validated = module.validate_transition(
            self.previous, self.current, REPOSITORY
        )
        expected = module.baseline_semantic_contract(validated["current"])[
            "items"
        ]
        mutated = list(expected)
        item_id, sentence = mutated[0]
        mutated[0] = (item_id, sentence + " shape-preserving drift")
        actual = tuple(mutated)
        self.assertEqual(len(actual), len(expected))
        self.assertEqual(
            {row[0] for row in actual},
            {row[0] for row in expected},
        )
        actual_minus_expected, expected_minus_actual = bidirectional_except(
            expected, actual
        )
        self.assertTrue(actual_minus_expected)
        self.assertTrue(expected_minus_actual)

    def test_shape_preserving_h1_assignment_mapping_drift_fails_exact_contract(
        self,
    ) -> None:
        validated = module.validate_transition(
            self.previous, self.current, REPOSITORY
        )
        expected = module.baseline_semantic_contract(validated["current"])[
            "assignments"
        ]
        mutated = list(expected)
        first = mutated[0]
        second = mutated[1]
        self.assertEqual(first[0], second[0])
        mutated[0] = (first[0], first[1], second[2], first[3])
        mutated[1] = (second[0], second[1], first[2], second[3])
        actual = tuple(mutated)
        self.assertEqual(len(actual), len(expected))
        self.assertEqual(
            {(row[0], row[1]) for row in actual},
            {(row[0], row[1]) for row in expected},
        )
        self.assertEqual(
            {(row[0], row[2]) for row in actual},
            {(row[0], row[2]) for row in expected},
        )
        self.assertEqual(
            {row[3] for row in actual},
            {row[3] for row in expected},
        )
        actual_minus_expected, expected_minus_actual = bidirectional_except(
            expected, actual
        )
        self.assertTrue(actual_minus_expected)
        self.assertTrue(expected_minus_actual)

    def test_rejects_baseline_or_research_content_drift(self) -> None:
        wrong_previous = dict(self.previous)
        wrong_previous["instrument_sha256"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "immutable v1"):
            module.validate_transition(wrong_previous, self.current, REPOSITORY)

        changed = copy.deepcopy(self.current)
        target_item = changed["assignments"]["PILOT_R01"][0]["pilot_item_id"]
        for rows in changed["assignments"].values():
            for row in rows:
                if row["pilot_item_id"] == target_item:
                    row["sentence_text"] += " changed"
        rehash(changed)
        with self.assertRaisesRegex(ValueError, "research instrument content"):
            module.validate_transition(self.previous, changed, REPOSITORY)

    def test_rejects_current_version_hash_and_release_source_drift(self) -> None:
        wrong_version = copy.deepcopy(self.current)
        wrong_version["hosted_version"] = module.PREVIOUS_HOSTED_VERSION
        rehash(wrong_version)
        with self.assertRaisesRegex(ValueError, "hosted_version"):
            module.validate_transition(self.previous, wrong_version, REPOSITORY)

        wrong_hash = copy.deepcopy(self.current)
        wrong_hash["instrument_sha256"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            module.validate_transition(self.previous, wrong_hash, REPOSITORY)

        stale = copy.deepcopy(self.current)
        stale["release_source_hashes"]["public_admin"] = "f" * 64
        rehash(stale)
        with self.assertRaisesRegex(ValueError, "stale"):
            module.validate_transition(self.previous, stale, REPOSITORY)

    def test_exact_paths_single_write_lf_and_overwrite_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            previous_path, current_path, output_path, _baseline_path = (
                prepare_repository(repository, self.previous)
            )
            result = module.render_to_path(
                previous_path, current_path, output_path, repository
            )
            self.assertEqual(result["items"], 12)
            self.assertEqual(result["assignment_sets"], 5)
            self.assertEqual(result["assignments"], 60)
            encoded = output_path.read_bytes()
            self.assertNotIn(b"\r\n", encoded)
            self.assertTrue(encoded.endswith(b"\n"))
            with self.assertRaises(FileExistsError):
                module.render_to_path(
                    previous_path, current_path, output_path, repository
                )
            with self.assertRaisesRegex(ValueError, "transition output"):
                module.ensure_paths(
                    previous_path,
                    current_path,
                    repository / "supabase" / "migrations" / "wrong.sql",
                    repository,
                )

    def test_missing_or_tampered_baseline_002_is_rejected_before_output(self) -> None:
        for mutation, message in (
            (lambda path: path.unlink(), "unavailable"),
            (lambda path: path.write_bytes(path.read_bytes() + b"\n"), "hash mismatch"),
        ):
            with tempfile.TemporaryDirectory() as directory:
                repository = Path(directory)
                previous_path, current_path, output_path, baseline_path = (
                    prepare_repository(repository, self.previous)
                )
                mutation(baseline_path)
                with self.assertRaisesRegex(ValueError, message):
                    module.render_to_path(
                        previous_path, current_path, output_path, repository
                    )
                self.assertFalse(output_path.exists())

    def test_duplicate_json_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.json"
            path.write_text('{"schema_version":"1.0","schema_version":"1.0"}')
            with self.assertRaisesRegex(ValueError, "unique-key"):
                module.load_json(path)


if __name__ == "__main__":
    unittest.main()
