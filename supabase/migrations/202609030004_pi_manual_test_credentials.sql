begin transaction isolation level read committed;

-- This append-only migration is deliberately non-idempotent. It may be
-- applied exactly once, and only after the closed pre-production database has
-- been cleaned completely. Without this guard, the temporary default below
-- would silently relabel a historical disposable E2E invite as a participant
-- invite, destroying the authentication-purpose provenance.
do $migration_guard$
begin
  if to_regclass('private.pilot_withdrawal_events') is null then
    raise exception
      'migration 004 requires the expected migration 003 schema';
  end if;

  if exists (
    select 1
    from information_schema.columns
    where table_schema = 'private'
      and table_name = 'pilot_invites'
      and column_name in ('invite_purpose', 'admin_id_hmac')
  ) then
    raise exception
      'migration 004 must be applied exactly once to the expected migration 003 schema';
  end if;

end
$migration_guard$;

-- Acquire the same canonical lock order as the instrument transition before
-- checking the clean pre-production boundary. In particular, this prevents a
-- concurrent invite INSERT from slipping between the provenance guard and the
-- ALTER TABLE temporary default below. READ COMMITTED is intentional: the
-- post-lock guard must see a writer that committed while this migration waited
-- for these locks, rather than reuse the earlier catalog-check snapshot.
lock table
  research.pilot_instruments,
  research.pilot_items,
  research.pilot_assignment_sets,
  research.pilot_assignments,
  private.pilot_invites,
  private.participant_identity,
  research.pilot_submissions,
  research.pilot_responses
in access exclusive mode;

do $clean_boundary_guard$
begin
  if exists (
    select 1
    from information_schema.columns
    where table_schema = 'private'
      and table_name = 'pilot_invites'
      and column_name in ('invite_purpose', 'admin_id_hmac')
  ) then
    raise exception
      'migration 004 must be applied exactly once to the expected migration 003 schema';
  end if;

  if exists (select 1 from private.pilot_invites)
     or exists (select 1 from private.participant_identity)
     or exists (select 1 from research.pilot_submissions)
     or exists (select 1 from research.pilot_responses) then
    raise exception
      'migration 004 requires empty invite, identity, submission, and response tables; classify or remove historical rows before a reviewed forward migration';
  end if;
end
$clean_boundary_guard$;

alter table private.pilot_invites
  add column invite_purpose text not null default 'participant';

alter table private.pilot_invites
  alter column invite_purpose drop default;

alter table private.pilot_invites
  add column admin_id_hmac bytea;

alter table private.pilot_invites
  add constraint pilot_invites_purpose_ck check (
    invite_purpose in ('participant', 'disposable_e2e', 'pi_manual_test')
  );

alter table private.pilot_invites
  add constraint pilot_invites_admin_auth_ck check (
    (
      invite_purpose = 'pi_manual_test'
      and admin_id_hmac is not null
      and octet_length(admin_id_hmac) = 32
      and admin_id_hmac <> token_hmac
      and assignment_code = 'PILOT_R01'
    )
    or
    (
      invite_purpose in ('participant', 'disposable_e2e')
      and admin_id_hmac is null
    )
  );

comment on column private.pilot_invites.invite_purpose is
  'Server-enforced authentication purpose. pi_manual_test credentials are valid only while production fielding is closed.';

comment on column private.pilot_invites.admin_id_hmac is
  'Domain-separated HMAC of a PI manual-test account ID. Raw account IDs and passwords are never stored.';

create unique index pilot_invites_admin_id_hmac_idx
  on private.pilot_invites (admin_id_hmac)
  where admin_id_hmac is not null;

create unique index pilot_invites_one_outstanding_pi_manual_test_idx
  on private.pilot_invites (instrument_sha256)
  where invite_purpose = 'pi_manual_test'
    and revoked_at is null
    and used_at is null;

alter table research.pilot_submissions
  drop constraint pilot_submissions_role_ck;

alter table research.pilot_submissions
  add constraint pilot_submissions_role_ck check (
    (
      dataset_role = 'synthetic_usability_pilot'
      and excluded_from_analysis
      and analysis_exclusion_reason = 'synthetic_usability_only_never_analysis'
    )
    or
    (
      dataset_role = 'synthetic_pi_manual_test'
      and excluded_from_analysis
      and analysis_exclusion_reason = 'pi_manual_test_never_analysis'
    )
  );

commit;
