from __future__ import annotations

import tempfile
import sys
import unittest
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPOSITORY_ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from tools import build_deployment_manifest
from tools import build_public_instrument
from tools import render_instrument_transition


SOURCE_PILOT = (
    REPOSITORY_ROOT.parent
    / "latent_stance_pipeline"
    / "02_annotation"
    / "pilot"
)


class PublicArtifactLineEndingTests(unittest.TestCase):
    def test_official_builders_emit_lf_only_public_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            docs = Path(directory) / "docs"
            docs.mkdir()
            instrument_path = docs / "instrument.json"
            hash_path = docs / "instrument-hash.js"
            manifest_path = docs / "deployment-manifest.json"
            config_path = docs / "site-config.js"
            privacy_path = docs / "privacy.html"

            instrument = build_public_instrument.build(
                SOURCE_PILOT, instrument_path
            )
            config_path.write_bytes(
                (REPOSITORY_ROOT / "docs" / "site-config.js").read_bytes()
            )
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace(
                    'hostedVersion: "v260903-pilot-hosted-1"',
                    f'hostedVersion: "{build_public_instrument.HOSTED_VERSION}"',
                ),
                encoding="utf-8",
                newline="\n",
            )
            privacy_path.write_bytes(
                (REPOSITORY_ROOT / "docs" / "privacy.html").read_bytes()
            )
            previous = render_instrument_transition.load_json(
                REPOSITORY_ROOT
                / render_instrument_transition.PREVIOUS_INSTRUMENT_RELATIVE
            )
            transition_contract = render_instrument_transition.validate_transition(
                previous,
                instrument,
                REPOSITORY_ROOT,
            )
            transition_path = Path(directory) / "transition.sql"
            transition_path.write_text(
                render_instrument_transition.render_sql(
                    transition_contract,
                    render_instrument_transition.PREVIOUS_INSTRUMENT_RELATIVE,
                    render_instrument_transition.CURRENT_INSTRUMENT_RELATIVE,
                ),
                encoding="utf-8",
                newline="\n",
            )
            with (
                mock.patch.object(build_deployment_manifest, "DOCS", docs),
                mock.patch.object(
                    build_deployment_manifest,
                    "OPERATIONAL_FILES",
                    {
                        "privacy_notice": privacy_path,
                        "site_config": config_path,
                    },
                ),
                mock.patch.object(
                    build_deployment_manifest,
                    "DEPLOYMENT_SOURCE_FILES",
                    {"database_instrument_transition": transition_path},
                ),
            ):
                build_deployment_manifest.build("staging", manifest_path)

            for artifact in (instrument_path, hash_path, manifest_path):
                with self.subTest(artifact=artifact.name):
                    encoded = artifact.read_bytes()
                    self.assertNotIn(b"\r\n", encoded)
                    self.assertTrue(encoded.endswith(b"\n"))


if __name__ == "__main__":
    unittest.main()
