from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[2]
TOOLS = REPOSITORY / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from tools import build_deployment_manifest as manifest_module
from tools import build_public_instrument
from tools import render_instrument_transition as transition_module


SOURCE_PILOT = (
    REPOSITORY.parent / "latent_stance_pipeline" / "02_annotation" / "pilot"
)


class DeploymentManifestTests(unittest.TestCase):
    def prepared(self, directory: str) -> tuple[Path, Path, Path]:
        docs = Path(directory) / "docs"
        docs.mkdir()
        instrument_path = docs / "instrument.json"
        build_public_instrument.build(SOURCE_PILOT, instrument_path)
        for name in ("site-config.js", "privacy.html"):
            (docs / name).write_bytes((REPOSITORY / "docs" / name).read_bytes())
        config_path = docs / "site-config.js"
        instrument = json.loads(instrument_path.read_text(encoding="utf-8"))
        config = config_path.read_text(encoding="utf-8")
        config = config.replace(
            'hostedVersion: "v260911-r5-public-4"',
            f'hostedVersion: "{build_public_instrument.HOSTED_VERSION}"',
        ).replace(
            'de96c00e9f95a9035cfc54a1bf18cc0d24b3465f7b5edc05cd9669a3fbfa7dee',
            instrument["source_offline_instrument_sha256"],
        ).replace('fieldingEnabled: true', 'fieldingEnabled: false').replace(
            'directEntryEnabled: true', 'directEntryEnabled: false'
        )
        config_path.write_text(config, encoding="utf-8", newline="\n")
        transition = Path(directory) / "transition.sql"
        previous = transition_module.load_json(
            REPOSITORY / transition_module.PREVIOUS_INSTRUMENT_RELATIVE
        )
        transition_contract = transition_module.validate_transition(
            previous,
            instrument,
            REPOSITORY,
        )
        transition.write_text(
            transition_module.render_sql(
                transition_contract,
                transition_module.PREVIOUS_INSTRUMENT_RELATIVE,
                transition_module.CURRENT_INSTRUMENT_RELATIVE,
            ),
            encoding="utf-8",
            newline="\n",
        )
        return docs, transition, docs / "deployment-manifest.json"

    def patches(self, docs: Path, transition: Path):
        return (
            mock.patch.object(manifest_module, "DOCS", docs),
            mock.patch.object(
                manifest_module,
                "OPERATIONAL_FILES",
                {
                    "privacy_notice": docs / "privacy.html",
                    "site_config": docs / "site-config.js",
                },
            ),
            mock.patch.object(
                manifest_module,
                "DEPLOYMENT_SOURCE_FILES",
                {"database_instrument_transition": transition},
            ),
        )

    def test_build_binds_exact_transition_bytes_and_self_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            docs, transition, output = self.prepared(directory)
            first, second, third = self.patches(docs, transition)
            with first, second, third:
                result = manifest_module.build("staging", output)
            self.assertEqual(
                result["deployment_source_hashes"],
                {
                    "database_instrument_transition": hashlib.sha256(
                        transition.read_bytes()
                    ).hexdigest()
                },
            )
            basis = dict(result)
            declared = basis.pop("deployment_manifest_sha256")
            self.assertEqual(
                declared,
                manifest_module.sha256_bytes(manifest_module.canonical_json(basis)),
            )

    def test_builder_rejects_self_invalid_or_stale_instrument(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            docs, transition, output = self.prepared(directory)
            path = docs / "instrument.json"
            instrument = json.loads(path.read_text(encoding="utf-8"))
            instrument["instrument_sha256"] = "f" * 64
            path.write_text(json.dumps(instrument), encoding="utf-8")
            first, second, third = self.patches(docs, transition)
            with first, second, third, self.assertRaisesRegex(ValueError, "self-digest"):
                manifest_module.build("staging", output)

            build_public_instrument.build(SOURCE_PILOT, path)
            instrument = json.loads(path.read_text(encoding="utf-8"))
            instrument["release_source_hashes"]["public_admin"] = "f" * 64
            basis = dict(instrument)
            basis.pop("instrument_sha256")
            instrument["instrument_sha256"] = manifest_module.sha256_bytes(
                manifest_module.canonical_json(basis)
            )
            path.write_text(json.dumps(instrument), encoding="utf-8")
            first, second, third = self.patches(docs, transition)
            with first, second, third, self.assertRaisesRegex(ValueError, "stale"):
                manifest_module.build("staging", output)

    def test_builder_rejects_missing_extra_or_wrong_version_release_map(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            docs, transition, output = self.prepared(directory)
            path = docs / "instrument.json"
            for mutation, message in (
                (lambda hashes: hashes.pop("public_admin"), "key set"),
                (lambda hashes: hashes.update({"extra": "f" * 64}), "key set"),
            ):
                build_public_instrument.build(SOURCE_PILOT, path)
                instrument = json.loads(path.read_text(encoding="utf-8"))
                mutation(instrument["release_source_hashes"])
                basis = dict(instrument)
                basis.pop("instrument_sha256")
                instrument["instrument_sha256"] = manifest_module.sha256_bytes(
                    manifest_module.canonical_json(basis)
                )
                path.write_text(json.dumps(instrument), encoding="utf-8")
                first, second, third = self.patches(docs, transition)
                with first, second, third, self.assertRaisesRegex(ValueError, message):
                    manifest_module.build("staging", output)

            build_public_instrument.build(SOURCE_PILOT, path)
            instrument = json.loads(path.read_text(encoding="utf-8"))
            instrument["hosted_version"] = transition_module.PREVIOUS_HOSTED_VERSION
            basis = dict(instrument)
            basis.pop("instrument_sha256")
            instrument["instrument_sha256"] = manifest_module.sha256_bytes(
                manifest_module.canonical_json(basis)
            )
            path.write_text(json.dumps(instrument), encoding="utf-8")
            first, second, third = self.patches(docs, transition)
            with first, second, third, self.assertRaisesRegex(ValueError, "version"):
                manifest_module.build("staging", output)

    def test_builder_rejects_missing_or_mismatched_transition_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            docs, transition, output = self.prepared(directory)
            missing = Path(directory) / "missing.sql"
            first, second, third = self.patches(docs, missing)
            with first, second, third, self.assertRaisesRegex(ValueError, "unavailable"):
                manifest_module.build("staging", output)
            self.assertFalse(output.exists())

            transition.write_text("wrong", encoding="utf-8")
            first, second, third = self.patches(docs, transition)
            with first, second, third, self.assertRaisesRegex(ValueError, "does not match"):
                manifest_module.build("staging", output)
            self.assertFalse(output.exists())

    def test_exact_eleven_source_map_excludes_transition_and_state_is_checked(self) -> None:
        self.assertEqual(len(build_public_instrument.RELEASE_SOURCE_PATHS), 11)
        self.assertEqual(
            set(build_public_instrument.RELEASE_SOURCE_PATHS),
            {
                "public_admin",
                "public_admin_bridge",
                "public_app",
                "public_contract",
                "public_index",
                "public_styles",
                "supabase_config",
                "edge_function",
                "edge_contract",
                "database_schema",
                "database_pi_manual_test",
            },
        )
        self.assertNotIn(
            transition_module.TRANSITION_MIGRATION_RELATIVE,
            build_public_instrument.RELEASE_SOURCE_PATHS.values(),
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "instrument.json"
            with (
                mock.patch.object(
                    build_public_instrument,
                    "RELEASE_SOURCE_PATHS",
                    {"public_app": Path("docs/app.js")},
                ),
                self.assertRaisesRegex(RuntimeError, "exact 11"),
            ):
                build_public_instrument.build(SOURCE_PILOT, output)
            self.assertFalse(output.exists())
        with tempfile.TemporaryDirectory() as directory:
            docs, transition, output = self.prepared(directory)
            first, second, third = self.patches(docs, transition)
            with first, second, third, self.assertRaisesRegex(ValueError, "state"):
                manifest_module.build("other", output)


if __name__ == "__main__":
    unittest.main()
