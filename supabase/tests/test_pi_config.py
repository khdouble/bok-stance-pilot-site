from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path


TOOLS = Path(__file__).resolve().parents[2] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import pi_config as MODULE


VALID_VALUES = {
    "privacyNoticeVersion": "consent-v2026-09-03-r1",
    "dataController": "Pilot Research Office",
    "contactEmail": "pilot-contact@institution.ac.kr",
    "retentionNotice": "2030-12-31까지 보관 후 지체 없이 파기",
    "retentionEndDate": "2030-12-31",
    "dataRegion": "ap-northeast-2",
    "ethicsDisposition": "exempt",
    "ethicsReference": "exempt:IRB-2026-001 exemption record",
    "withdrawalProcedureVersion": "withdrawal-v2026-09-03-r1",
    "remoteE2eVerifiedAt": "2026-09-03T01:02:03Z",
    "identityPurpose": MODULE.EXPECTED_IDENTITY_PURPOSE,
}


def config_text(values: dict[str, str], enabled: bool = True) -> str:
    rows = [
        "window.PILOT_SITE_CONFIG = Object.freeze({",
        f"  fieldingEnabled: {str(enabled).lower()},",
    ]
    rows.extend(f'  {key}: "{value}",' for key, value in values.items())
    rows.append("});")
    return "\n".join(rows)


def privacy_text(values: dict[str, str]) -> str:
    displayed = (
        "privacyNoticeVersion",
        "dataController",
        "contactEmail",
        "retentionEndDate",
        "retentionNotice",
        "dataRegion",
        "ethicsReference",
        "withdrawalProcedureVersion",
    )
    return "<html>" + " ".join(values[key] for key in displayed) + "</html>"


class PiConfigTest(unittest.TestCase):
    def validate(self, values: dict[str, str]) -> list[str]:
        return MODULE.validate_live_config(
            config_text(values),
            privacy_text(values),
            today=date(2026, 9, 3),
            now=datetime(2026, 9, 3, 2, 0, tzinfo=timezone.utc),
        )

    def test_complete_meaningful_live_values_pass(self) -> None:
        self.assertEqual(self.validate(VALID_VALUES.copy()), [])
        self.assertTrue(
            MODULE.boolean_value(config_text(VALID_VALUES), "fieldingEnabled")
        )

    def test_empty_and_arbitrary_replacements_do_not_clear_gate(self) -> None:
        for field, invalid in (
            ("privacyNoticeVersion", ""),
            ("dataController", "x"),
            ("contactEmail", "anything"),
            ("retentionNotice", "done"),
            ("retentionEndDate", "2099-99-99"),
            ("dataRegion", "xx-fake-1"),
            ("ethicsDisposition", "done"),
            ("ethicsReference", "exempt:abcdefgh"),
            ("withdrawalProcedureVersion", "anything"),
            ("remoteE2eVerifiedAt", "anything"),
        ):
            with self.subTest(field=field):
                values = VALID_VALUES.copy()
                values[field] = invalid
                self.assertTrue(self.validate(values))

    def test_expired_retention_and_future_e2e_are_rejected(self) -> None:
        values = VALID_VALUES.copy()
        values["retentionEndDate"] = "2026-09-02"
        values["retentionNotice"] = "2026-09-02까지 보관 후 지체 없이 파기"
        values["remoteE2eVerifiedAt"] = "2026-09-03T03:00:00Z"
        errors = self.validate(values)
        self.assertTrue(any("retentionEndDate" in error for error in errors))
        self.assertTrue(any("remoteE2eVerifiedAt" in error for error in errors))

    def test_e2e_must_follow_final_governance_versions(self) -> None:
        values = VALID_VALUES.copy()
        values["privacyNoticeVersion"] = "consent-v2026-09-04-r1"
        values["withdrawalProcedureVersion"] = "withdrawal-v2026-09-05-r1"
        errors = MODULE.validate_live_config(
            config_text(values),
            privacy_text(values),
            today=date(2026, 9, 3),
            now=datetime(2026, 9, 6, 2, 0, tzinfo=timezone.utc),
        )
        self.assertTrue(
            any("must follow consent and withdrawal" in error for error in errors)
        )

    def test_ethics_reference_matches_disposition_and_record_shape(self) -> None:
        for invalid in (
            "approved:IRB-2026-001 record",
            "exempt:abcdefgh",
            "exempt:12345678",
        ):
            with self.subTest(invalid=invalid):
                values = VALID_VALUES.copy()
                values["ethicsReference"] = invalid
                self.assertTrue(self.validate(values))

    def test_privacy_displays_values(self) -> None:
        privacy = privacy_text(VALID_VALUES).replace(
            VALID_VALUES["dataRegion"], "region omitted"
        )
        errors = MODULE.validate_live_config(
            config_text(VALID_VALUES),
            privacy,
            today=date(2026, 9, 3),
            now=datetime(2026, 9, 3, 2, 0, tzinfo=timezone.utc),
        )
        self.assertIn("privacy.html does not display dataRegion", errors)


if __name__ == "__main__":
    unittest.main()
