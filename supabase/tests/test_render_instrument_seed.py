from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "tools" / "render_instrument_seed.py"
SPEC = importlib.util.spec_from_file_location("render_instrument_seed", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

FROZEN_INSTRUMENT_SHA256 = (
    "4a07da2785bb2228787f2dd4e57339bd5c132111d693681a12cb62baf64978e7"
)
FROZEN_SEED_SHA256 = (
    "c0a8116c0bc8551a4ac0fb77bf776385bc002ad6f26d8b575e83adbac353ebd2"
)
FROZEN_RELEASE_SOURCE_HASHES = {
    "database_schema": "ac214e9a6d08b02cad8d14d97c6a049374c610b0f7ba54a6d0e1bb09796e895d",
    "edge_contract": "8f40b7c744647a956951a71b60ff940ce03d564c7caf1eb1a2983a2e5a297c76",
    "edge_function": "fecb6f47dcc88dae595b3acf2207ed5d9c61374989cf2d445e77f29e8fe93cb7",
    "public_app": "1c8a65e24b39e05e2363daa4f68b79735f7d3c4276675610ebd937a470c3e299",
    "public_contract": "5359b283c4d90bdbb9dd716c7a2be892e9f362df3949ebc8aa567da3c3043086",
    "public_index": "250a521cba58b081ef774354ede7274dade952abf9729db8d6f68fc9d0bd329d",
    "public_styles": "ea23e22cf0f2adf2e0707fa84e2e2f92efe73d231ea6ebc6d379b37813a80580",
    "supabase_config": "62e27b64065d1802c260244e55f068856025ee4dc65a307ca67827ffe8308add",
}


def rehash(instrument: dict[str, object]) -> None:
    payload = dict(instrument)
    payload.pop("instrument_sha256", None)
    instrument["instrument_sha256"] = hashlib.sha256(MODULE.canonical_json(payload)).hexdigest()


class RenderInstrumentSeedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.instrument = MODULE.load_json(REPOSITORY / "docs" / "instrument.json")
        self.instrument["hosted_version"] = MODULE.EXPECTED_HOSTED_VERSION
        self.instrument["release_source_hashes"] = copy.deepcopy(
            FROZEN_RELEASE_SOURCE_HASHES
        )
        rehash(self.instrument)
        self.assertEqual(
            self.instrument["instrument_sha256"],
            FROZEN_INSTRUMENT_SHA256,
            "current research payload has drifted from the immutable v1 baseline",
        )

    def test_current_instrument_renders_expected_immutable_rows(self) -> None:
        validated = MODULE.validate_instrument(copy.deepcopy(self.instrument))
        sql = MODULE.render_sql(validated, Path("docs/instrument.json"))
        self.assertEqual(len(validated["items"]), 12)
        self.assertEqual(len(validated["assignments"]), 5)
        self.assertEqual(sum(map(len, validated["assignments"].values())), 60)
        self.assertIn("fielding_open,", sql)
        self.assertIn("  false,", sql)
        self.assertNotIn("on conflict", sql.lower())
        self.assertNotRegex(sql, MODULE.PLACEHOLDER_RE)

    def test_committed_seed_exactly_matches_current_instrument(self) -> None:
        validated = MODULE.validate_instrument(copy.deepcopy(self.instrument))
        expected = MODULE.render_sql(validated, Path("docs/instrument.json"))
        actual_path = REPOSITORY / "supabase" / "migrations" / "202609030002_seed_pilot_instrument.sql"
        self.assertEqual(actual_path.read_text(encoding="utf-8"), expected)
        self.assertEqual(
            hashlib.sha256(actual_path.read_bytes()).hexdigest(),
            FROZEN_SEED_SHA256,
        )

    def test_writes_only_explicit_output_and_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            docs = repository / "docs"
            migrations = repository / "supabase" / "migrations"
            docs.mkdir(parents=True)
            migrations.mkdir(parents=True)
            source = docs / "instrument.json"
            source.write_text(json.dumps(self.instrument, ensure_ascii=False), encoding="utf-8")
            output = migrations / "202609030002_seed_pilot_instrument.sql"
            before = set(repository.rglob("*"))
            counts = MODULE.render_to_path(source, output, repository)
            after = set(repository.rglob("*"))
            self.assertEqual(after - before, {output})
            self.assertEqual(counts["instruments"], 1)
            self.assertEqual(counts["items"], 12)
            self.assertEqual(counts["assignment_sets"], 5)
            self.assertEqual(counts["assignments"], 60)
            with self.assertRaises(FileExistsError):
                MODULE.render_to_path(source, output, repository)

    def test_sql_quote_escaping(self) -> None:
        changed = copy.deepcopy(self.instrument)
        for rows in changed["assignments"].values():
            for row in rows:
                if row["pilot_item_id"] == "USP_001":
                    row["sentence_text"] += " O'Brien"
        rehash(changed)
        sql = MODULE.render_sql(MODULE.validate_instrument(changed), Path("docs/instrument.json"))
        self.assertIn("O''Brien", sql)
        self.assertNotIn("O'Brien", sql)

    def test_rejects_wrong_count_even_with_valid_hash(self) -> None:
        changed = copy.deepcopy(self.instrument)
        changed["assignments"]["PILOT_R01"].pop()
        rehash(changed)
        with self.assertRaisesRegex(ValueError, "exactly 12 rows"):
            MODULE.validate_instrument(changed)

    def test_rejects_wrong_version_even_with_valid_hash(self) -> None:
        changed = copy.deepcopy(self.instrument)
        changed["hosted_version"] = "v999999-wrong"
        rehash(changed)
        with self.assertRaisesRegex(ValueError, "hosted_version"):
            MODULE.validate_instrument(changed)

    def test_rejects_hash_mismatch_and_placeholder(self) -> None:
        changed = copy.deepcopy(self.instrument)
        changed["assignments"]["PILOT_R01"][0]["sentence_text"] += " changed"
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            MODULE.validate_instrument(changed)
        placeholder = copy.deepcopy(self.instrument)
        placeholder["source_hashes"]["parent_manifest"] = "__PILOT_HASH__"
        rehash(placeholder)
        with self.assertRaisesRegex(ValueError, "placeholder"):
            MODULE.validate_instrument(placeholder)

    def test_rejects_output_outside_migrations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            docs = repository / "docs"
            docs.mkdir()
            source = docs / "instrument.json"
            source.write_text(json.dumps(self.instrument, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "inside"):
                MODULE.ensure_paths(source, repository / "seed.sql", repository)


if __name__ == "__main__":
    unittest.main()
