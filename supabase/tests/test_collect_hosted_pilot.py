from __future__ import annotations

import copy
import csv
import hashlib
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "tools" / "collect_hosted_pilot.py"
SPEC = importlib.util.spec_from_file_location("collect_hosted_pilot", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def rehash(instrument: dict[str, object]) -> None:
    payload = dict(instrument)
    payload.pop("instrument_sha256", None)
    instrument["instrument_sha256"] = hashlib.sha256(MODULE.canonical_json(payload)).hexdigest()


class CollectHostedPilotTest(unittest.TestCase):
    def setUp(self) -> None:
        self.instrument_path = REPOSITORY / "docs" / "instrument.json"
        self.instrument_hash, self.assignments = MODULE.load_instrument(self.instrument_path)
        self.responses, self.feedback = self.make_collection(3)

    def make_collection(self, count: int) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        response_rows: list[dict[str, str]] = []
        feedback_rows: list[dict[str, str]] = []
        for participant_number in range(1, count + 1):
            assignment_code = f"PILOT_R{participant_number:02d}"
            submission_id = f"00000000-0000-4000-8000-{participant_number:012d}"
            payload_sha256 = format(participant_number, "x") * 64
            submitted_at = "2026-09-03T00:10:01.000Z"
            feedback_rows.append({
                "submission_id": submission_id,
                "assignment_code": assignment_code,
                "instrument_sha256": self.instrument_hash,
                "instrument_version": MODULE.HOSTED_VERSION,
                "fatigue_1to5": "2",
                "zero_vs_99_explanation": "0은 중립이고 99는 판단 유보입니다.",
                "change_vs_stance_explanation": "변화와 수준을 따로 판단했습니다.",
                "ui_error_note": "=1+1",
                "session_started_at": "2026-09-03T00:00:00.000Z",
                "session_finished_at": "2026-09-03T00:10:00.000Z",
                "active_duration_seconds": "60",
                "submitted_at": submitted_at,
                **MODULE.COMMON_FIXED,
                "payload_sha256": payload_sha256,
            })
            for expected in self.assignments[assignment_code]:
                response_rows.append({
                    "submission_id": submission_id,
                    "assignment_code": assignment_code,
                    "instrument_sha256": self.instrument_hash,
                    "instrument_version": MODULE.HOSTED_VERSION,
                    "display_position": str(expected["display_position"]),
                    "assignment_id": expected["assignment_id"],
                    "pilot_item_id": expected["pilot_item_id"],
                    "sentence_text": expected["sentence_text"],
                    "label5": "0",
                    "abstain": "false",
                    "reason_code": "NONE",
                    "confidence": "3",
                    "reason_note": "",
                    "started_at": "2026-09-03T00:00:10.000Z",
                    "finished_at": "2026-09-03T00:00:20.000Z",
                    "active_duration_seconds": "4",
                    "response_status": "COMPLETED",
                    **MODULE.COMMON_FIXED,
                    "submitted_at": submitted_at,
                    "payload_sha256": payload_sha256,
                })
        return response_rows, feedback_rows

    def validate(self, responses: list[dict[str, str]] | None = None,
                 feedback: list[dict[str, str]] | None = None) -> dict[str, object]:
        return MODULE.validate_collection(
            responses if responses is not None else self.responses,
            feedback if feedback is not None else self.feedback,
            self.instrument_hash,
            self.assignments,
            3,
            now=datetime(2026, 9, 3, 1, 0, tzinfo=timezone.utc),
        )

    def test_valid_collection_preserves_hosted_identity_and_escapes_formulas(self) -> None:
        validated = self.validate()
        self.assertEqual(validated["submission_count"], 3)
        self.assertEqual(validated["response_count"], 36)
        self.assertNotEqual(self.instrument_hash, MODULE.OFFLINE_PARENT_SHA256)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repo"
            repository.mkdir()
            response_input = root / "responses.csv"
            feedback_input = root / "feedback.csv"
            write_csv(response_input, MODULE.RESPONSE_COLUMNS, self.responses)
            write_csv(feedback_input, MODULE.FEEDBACK_COLUMNS, self.feedback)
            output = repository / "exports" / "hosted-run"
            manifest_path = MODULE.write_collection(
                output, repository, validated, response_input, feedback_input,
                self.instrument_path, self.instrument_hash,
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["instrument_sha256"], self.instrument_hash)
            self.assertEqual(manifest["instrument_version"], MODULE.HOSTED_VERSION)
            self.assertFalse(manifest["offline_collector_used"])
            self.assertTrue(manifest["instrument_identity_preserved"])
            self.assertEqual(manifest["response_count"], 36)
            output_feedback = MODULE.read_csv_exact(
                output / "hosted_feedback_validated.csv", MODULE.FEEDBACK_COLUMNS,
            )
            self.assertTrue(output_feedback[0]["ui_error_note"].startswith("'="))
            for forbidden in ("name", "phone", "participant_id", "invite_id"):
                self.assertNotIn(forbidden, MODULE.RESPONSE_COLUMNS)
                self.assertNotIn(forbidden, MODULE.FEEDBACK_COLUMNS)
            with self.assertRaises(FileExistsError):
                MODULE.write_collection(
                    output, repository, validated, response_input, feedback_input,
                    self.instrument_path, self.instrument_hash,
                )

    def test_instrument_hash_is_read_dynamically_but_offline_lineage_is_fixed(self) -> None:
        instrument = json.loads(self.instrument_path.read_text(encoding="utf-8"))
        instrument["release_source_hashes"]["public_app"] = "a" * 64
        rehash(instrument)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "instrument.json"
            path.write_text(json.dumps(instrument, ensure_ascii=False), encoding="utf-8")
            dynamic_hash, assignments = MODULE.load_instrument(path)
            self.assertEqual(dynamic_hash, instrument["instrument_sha256"])
            self.assertEqual(len(assignments), 5)
        instrument["source_offline_instrument_sha256"] = "f" * 64
        rehash(instrument)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "instrument.json"
            path.write_text(json.dumps(instrument, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "parent offline hash"):
                MODULE.load_instrument(path)

    def test_rejects_incomplete_or_tampered_assignments(self) -> None:
        with self.assertRaisesRegex(ValueError, "count times 12"):
            self.validate(responses=self.responses[:-1])
        tampered = copy.deepcopy(self.responses)
        tampered[0]["sentence_text"] += " changed"
        with self.assertRaisesRegex(ValueError, "sentence_text differs"):
            self.validate(responses=tampered)
        duplicate_position = copy.deepcopy(self.responses)
        duplicate_position[0]["display_position"] = "2"
        with self.assertRaisesRegex(ValueError, "positions must be exactly"):
            self.validate(responses=duplicate_position)

    def test_rejects_offline_or_changed_hosted_metadata(self) -> None:
        changed = copy.deepcopy(self.responses)
        changed[0]["instrument_sha256"] = MODULE.OFFLINE_PARENT_SHA256
        with self.assertRaisesRegex(ValueError, "hosted instrument hash mismatch"):
            self.validate(responses=changed)
        changed = copy.deepcopy(self.feedback)
        changed[0]["dataset_role"] = "analysis"
        with self.assertRaisesRegex(ValueError, "dataset_role mismatch"):
            self.validate(feedback=changed)

    def test_rejects_decimal_duration_bad_abstention_and_duplicate_assignment(self) -> None:
        changed = copy.deepcopy(self.responses)
        changed[0]["active_duration_seconds"] = "4.000"
        with self.assertRaisesRegex(ValueError, "exact integer"):
            self.validate(responses=changed)
        changed = copy.deepcopy(self.responses)
        changed[0]["abstain"] = "true"
        with self.assertRaisesRegex(ValueError, "invalid abstention"):
            self.validate(responses=changed)
        changed_responses = copy.deepcopy(self.responses)
        changed_feedback = copy.deepcopy(self.feedback)
        second_submission = changed_feedback[1]["submission_id"]
        changed_feedback[1]["assignment_code"] = "PILOT_R01"
        for row in changed_responses:
            if row["submission_id"] == second_submission:
                row["assignment_code"] = "PILOT_R01"
        with self.assertRaisesRegex(ValueError, "unique hosted assignment"):
            self.validate(changed_responses, changed_feedback)

    def test_exact_headers_reject_pii_and_short_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pii_path = root / "pii.csv"
            pii_path.write_text(",".join(MODULE.FEEDBACK_COLUMNS + ("name",)) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unexpected CSV header"):
                MODULE.read_csv_exact(pii_path, MODULE.FEEDBACK_COLUMNS)
            short_path = root / "short.csv"
            short_path.write_text(",".join(MODULE.FEEDBACK_COLUMNS) + "\nonly-one-value\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "malformed"):
                MODULE.read_csv_exact(short_path, MODULE.FEEDBACK_COLUMNS)

    def test_output_must_be_new_child_of_ignored_exports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repo"
            repository.mkdir()
            with self.assertRaisesRegex(ValueError, "below"):
                MODULE.ensure_output(repository / "outside", repository)
            with self.assertRaisesRegex(ValueError, "new collection directory"):
                MODULE.ensure_output(repository / "exports", repository)


if __name__ == "__main__":
    unittest.main()
