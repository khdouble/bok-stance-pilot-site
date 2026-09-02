from __future__ import annotations

import re
import unittest
from pathlib import Path


SUPABASE = Path(__file__).resolve().parents[1]
MIGRATION = SUPABASE / "migrations" / "202609030001_pilot_backend.sql"
EDGE = SUPABASE / "functions" / "pilot-api" / "index.ts"
CORE = SUPABASE / "functions" / "pilot-api" / "_shared" / "core.ts"
CONFIG = SUPABASE / "config.toml"


class BackendStaticTest(unittest.TestCase):
    def test_private_schemas_are_not_exposed_by_data_api(self) -> None:
        config = CONFIG.read_text(encoding="utf-8")
        self.assertIn('schemas = ["public", "storage", "graphql_public"]', config)
        self.assertNotIn('"private"', config)
        self.assertNotIn('"research"', config)
        self.assertIn("verify_jwt = false", config)

    def test_rls_and_no_browser_role_grants(self) -> None:
        migration = MIGRATION.read_text(encoding="utf-8").lower()
        self.assertGreaterEqual(migration.count("enable row level security"), 8)
        self.assertGreaterEqual(migration.count("force row level security"), 8)
        self.assertNotRegex(migration, r"grant\s+.+\s+to\s+(?:public|anon|authenticated)")
        self.assertIn("revoke all on schema private from public, anon, authenticated, service_role", migration)
        self.assertIn("revoke all on schema research from public, anon, authenticated, service_role", migration)

    def test_edge_is_only_network_data_boundary(self) -> None:
        edge = EDGE.read_text(encoding="utf-8")
        self.assertIn('const ALLOWED_ORIGIN = "https://khdouble.github.io"', CORE.read_text(encoding="utf-8"))
        self.assertIn('requiredEnv("SUPABASE_DB_URL")', edge)
        self.assertNotIn("SUPABASE_SERVICE_ROLE_KEY", edge)
        self.assertNotIn("/rest/v1/", edge)
        self.assertNotIn(".rpc(", edge)
        self.assertIn("db.begin", edge)
        self.assertIn("for update of v", edge.lower())
        self.assertIn("!invite.fielding_open", edge)
        self.assertIn("fielding_open boolean not null default false", MIGRATION.read_text(encoding="utf-8").lower())
        self.assertNotIn('headers.get("user-agent")', edge)
        self.assertNotIn("client_user_agent", CORE.read_text(encoding="utf-8"))

    def test_identity_is_encrypted_and_research_rows_are_pseudonymous(self) -> None:
        edge = EDGE.read_text(encoding="utf-8")
        migration = MIGRATION.read_text(encoding="utf-8")
        self.assertIn("encryptJson", edge)
        self.assertIn("AES-256-GCM", migration)
        submissions = migration.split("create table research.pilot_submissions", 1)[1].split(");", 1)[0]
        responses = migration.split("create table research.pilot_responses", 1)[1].split(");", 1)[0]
        for direct_identifier in ("name", "phone", "identity_ciphertext"):
            self.assertNotIn(direct_identifier, submissions)
            self.assertNotIn(direct_identifier, responses)

    def test_no_committed_raw_invite_or_environment_secret(self) -> None:
        files = [path for path in SUPABASE.rglob("*") if path.is_file() and "__pycache__" not in path.parts]
        token_pattern = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])")
        forbidden_assignments = re.compile(r"(?:SUPABASE_DB_URL|PII_ENCRYPTION_KEY_B64|INVITE_HMAC_SECRET_B64|IDENTITY_HMAC_SECRET_B64)\s*=\s*[^<_\s][^\r\n]*")
        for path in files:
            text = path.read_text(encoding="utf-8")
            if path.name not in {"core_test.ts", "test_provision_invites.py"}:
                self.assertIsNone(token_pattern.search(text), f"possible raw invite token in {path}")
            self.assertIsNone(forbidden_assignments.search(text), f"possible committed secret in {path}")


if __name__ == "__main__":
    unittest.main()
