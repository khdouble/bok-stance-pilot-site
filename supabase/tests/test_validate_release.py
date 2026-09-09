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


class ActualTreeReleaseValidationTests(unittest.TestCase):
    def test_checked_in_r5_live_release_is_valid(self) -> None:
        result = validate_release.validate(REPOSITORY, expected_fielding="live")
        self.assertEqual(
            result.failures,
            [],
            json.dumps(result.failures, ensure_ascii=False, indent=2),
        )


if __name__ == "__main__":
    unittest.main()
