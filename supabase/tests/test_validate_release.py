from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
TOOLS = REPOSITORY / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from tools import validate_release
from tools.render_instrument_transition import TRANSITION_MIGRATION_RELATIVE


SOURCE_PILOT = (
    REPOSITORY.parent / "latent_stance_pipeline" / "02_annotation" / "pilot"
)


class ActualTreeReleaseValidationTests(unittest.TestCase):
    def test_checked_in_tree_is_fail_closed_and_activates_with_transition(self) -> None:
        self.assertTrue(
            (SOURCE_PILOT / "response_template.csv").is_file(),
            "frozen source-pilot fixture is unavailable",
        )
        result = validate_release.validate(REPOSITORY, SOURCE_PILOT, "staging")
        transition = REPOSITORY / TRANSITION_MIGRATION_RELATIVE
        if not transition.is_file():
            self.assertEqual(
                [failure["check"] for failure in result.failures],
                ["required_public_files"],
                "before 005 exists, the checked-in tree must fail only at the "
                "deliberately missing required transition",
            )
            return
        self.assertEqual(
            result.failures,
            [],
            json.dumps(result.failures, ensure_ascii=False, indent=2),
        )


if __name__ == "__main__":
    unittest.main()
