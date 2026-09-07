from __future__ import annotations

import re
import unittest
from pathlib import Path


SUPABASE = Path(__file__).resolve().parents[1]
MIGRATION = SUPABASE / "migrations" / "202609030001_pilot_backend.sql"
WITHDRAWAL_MIGRATION = (
    SUPABASE / "migrations" / "202609030003_pilot_withdrawal_audit.sql"
)
PI_MIGRATION = (
    SUPABASE
    / 'migrations'
    / '202609030004_pi_manual_test_credentials.sql'
)
EDGE = SUPABASE / "functions" / "pilot-api" / "index.ts"
CORE = SUPABASE / "functions" / "pilot-api" / "_shared" / "core.ts"
CONFIG = SUPABASE / "config.toml"
RUNBOOK = SUPABASE / "README.md"


class BackendStaticTest(unittest.TestCase):
    def test_pi_manual_test_auth_is_purpose_bound_and_nonidentifying(
        self,
    ) -> None:
        migration = PI_MIGRATION.read_text(encoding='utf-8').lower()
        edge = EDGE.read_text(encoding='utf-8')
        self.assertIn('admin_id_hmac bytea', migration)
        self.assertIn(
            "invite_purpose in ('participant', 'disposable_e2e', "
            "'pi_manual_test')",
            migration,
        )
        self.assertIn('octet_length(admin_id_hmac) = 32', migration)
        self.assertIn('admin_id_hmac is not null', migration)
        self.assertIn('admin_id_hmac <> token_hmac', migration)
        self.assertIn("invite_purpose text not null default 'participant'", migration)
        self.assertIn('alter column invite_purpose drop default', migration)
        self.assertIn("and assignment_code = 'pilot_r01'", migration)
        self.assertNotRegex(
            migration,
            r'\n\s*(?:admin_id|admin_password)\s+(?:text|varchar)',
        )
        outstanding_index = migration.split(
            'create unique index pilot_invites_one_outstanding_pi_manual_test_idx',
            1,
        )[1].split(';', 1)[0]
        self.assertIn("invite_purpose = 'pi_manual_test'", outstanding_index)
        self.assertIn('revoked_at is null', outstanding_index)
        self.assertIn('used_at is null', outstanding_index)
        self.assertIn('synthetic_pi_manual_test', migration)
        self.assertIn('pi_manual_test_never_analysis', migration)
        self.assertIn('drop constraint pilot_submissions_role_ck', migration)
        self.assertNotRegex(
            migration,
            r'grant\s+.+\s+to\s+(?:public|anon|authenticated)',
        )
        self.assertIn('bok-pilot-admin-id-v1', edge)
        self.assertIn('bok-pilot-admin-password-v1', edge)
        self.assertIn('v.admin_id_hmac = decode', edge)
        self.assertIn('v.token_hmac = decode', edge)
        self.assertIn(
            "new Set(['participant', 'disposable_e2e'])",
            edge,
        )
        self.assertIn("invite.invite_purpose === PI_MANUAL_TEST_PURPOSE", edge)
        self.assertIn('invite.fielding_open !== false', edge)
        self.assertGreaterEqual(edge.count('fielding_open !== false'), 2)
        self.assertIn("'ADMIN_CREDENTIAL_ALREADY_USED'", edge)
        self.assertIn("'synthetic_pi_manual_test'", edge)
        self.assertIn("'pi_manual_test_never_analysis'", edge)
        self.assertIn(
            "invite_purpose = ${authMode === 'admin' ? "
            "PI_MANUAL_TEST_PURPOSE : invite.invite_purpose}",
            edge,
        )

    def test_pi_migration_refuses_nonempty_or_replayed_schema(self) -> None:
        migration = PI_MIGRATION.read_text(encoding="utf-8").lower()
        first_alter = migration.index("alter table private.pilot_invites")
        guard = migration[:first_alter]
        self.assertIn("do $migration_guard$", guard)
        self.assertIn("to_regclass('private.pilot_withdrawal_events')", guard)
        self.assertIn("requires the expected migration 003 schema", guard)
        self.assertIn("information_schema.columns", guard)
        self.assertIn("column_name in ('invite_purpose', 'admin_id_hmac')", guard)
        self.assertIn("must be applied exactly once", guard)

    def test_pi_migration_locks_clean_boundary_before_check_and_ddl(self) -> None:
        migration = PI_MIGRATION.read_text(encoding="utf-8").lower()
        prerequisite_guard = migration.index("do $migration_guard$")
        prerequisite_guard_end = migration.index(
            "$migration_guard$;", prerequisite_guard
        )
        lock_start = migration.index("lock table", prerequisite_guard_end)
        clean_guard = migration.index("do $clean_boundary_guard$", lock_start)
        first_alter = migration.index("alter table private.pilot_invites", clean_guard)

        self.assertTrue(
            migration.startswith("begin transaction isolation level read committed;")
        )
        self.assertLess(prerequisite_guard_end, lock_start)
        self.assertLess(lock_start, clean_guard)
        self.assertLess(clean_guard, first_alter)

        locked = migration[lock_start:clean_guard]
        expected_order = (
            "research.pilot_instruments",
            "research.pilot_items",
            "research.pilot_assignment_sets",
            "research.pilot_assignments",
            "private.pilot_invites",
            "private.participant_identity",
            "research.pilot_submissions",
            "research.pilot_responses",
        )
        positions = [locked.index(table) for table in expected_order]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("in access exclusive mode", locked)

        clean = migration[clean_guard:first_alter]
        self.assertIn("information_schema.columns", clean)
        self.assertIn("must be applied exactly once", clean)
        for table in expected_order[4:]:
            self.assertIn(f"exists (select 1 from {table})", clean)
        self.assertNotIn(
            "exists (select 1 from private.pilot_invites)",
            migration[:lock_start],
        )

    def test_pi_manual_release_runbook_is_fail_closed(self) -> None:
        runbook = RUNBOOK.read_text(encoding="utf-8")
        required = (
            "migration 004 -> migration 005 -> Edge Function -> GitHub Pages",
            "fielding_open=false",
            "invite_count=0",
            "identity_count=0",
            "submission_count=0",
            "response_count=0",
            "Do not retry either migration",
            "forward-fix",
            "exact 11-source map",
            "deployment_source_hashes.database_instrument_transition",
            "--expected-purpose pi_manual_test",
            "--mode pi-manual-test",
            "synthetic_pi_manual_test",
            "Never open production while a PI credential",
        )
        for phrase in required:
            self.assertIn(phrase, runbook)

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
        self.assertIn("invite.fielding_open !== true", edge)
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

    def test_withdrawal_audit_is_private_and_contains_no_target_identifier(self) -> None:
        migration = WITHDRAWAL_MIGRATION.read_text(encoding="utf-8").lower()
        table = migration.split(
            "create table private.pilot_withdrawal_events", 1
        )[1].split(");", 1)[0]
        self.assertIn("enable row level security", migration)
        self.assertIn("force row level security", migration)
        self.assertIn(
            "revoke all on table private.pilot_withdrawal_events", migration
        )
        self.assertNotRegex(
            migration,
            r"grant\s+.+\s+to\s+(?:public|anon|authenticated|service_role)",
        )
        for forbidden_column in (
            "name",
            "phone",
            "invite_id",
            "participant_id",
            "submission_id",
            "identity_hmac",
            "token_hmac",
            "ciphertext",
            "free_text",
            "operator_note",
        ):
            self.assertNotRegex(
                table,
                rf"\n\s*{forbidden_column}\s+",
            )

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
