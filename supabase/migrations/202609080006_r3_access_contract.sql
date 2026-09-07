-- R3 access and pilot-only response contract.  This migration is append-only:
-- it preserves the already-excluded H2 PI manual test and does not activate H3.
begin;

lock table research.pilot_instruments in share row exclusive mode;
lock table private.pilot_invites in share row exclusive mode;
lock table private.participant_identity in share row exclusive mode;
lock table research.pilot_submissions in share row exclusive mode;
lock table research.pilot_responses in share row exclusive mode;

do $r3_closed_gate_guard$
begin
  if exists (select 1 from research.pilot_instruments where fielding_open) then
    raise exception 'R3 access contract requires every fielding gate to be closed';
  end if;
end;
$r3_closed_gate_guard$;

alter table private.pilot_invites
  add column registration_identity_hmac bytea;

alter table private.pilot_invites
  add constraint pilot_invites_registration_identity_hmac_ck check (
    (invite_purpose = 'participant_direct'
      and registration_identity_hmac is not null
      and octet_length(registration_identity_hmac) = 32)
    or
    (invite_purpose <> 'participant_direct'
      and registration_identity_hmac is null)
  );

alter table private.pilot_invites
  drop constraint pilot_invites_purpose_ck;

alter table private.pilot_invites
  add constraint pilot_invites_purpose_ck check (
    invite_purpose in (
      'participant',
      'disposable_e2e',
      'pi_manual_test',
      'pi_preview',
      'participant_direct'
    )
  );

alter table private.pilot_invites
  drop constraint pilot_invites_admin_auth_ck;

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
      invite_purpose in ('participant', 'disposable_e2e', 'pi_preview', 'participant_direct')
      and admin_id_hmac is null
    )
  );

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
    or
    (
      dataset_role = 'r3_content_response_pilot'
      and excluded_from_analysis
      and analysis_exclusion_reason = 'r3_repilot_never_analysis'
    )
    or
    (
      dataset_role = 'r3_pi_preview'
      and excluded_from_analysis
      and analysis_exclusion_reason = 'r3_pi_preview_never_analysis'
    )
  );

alter table research.pilot_responses
  add column item_quality_code text not null default 'NONE',
  add column item_quality_note text not null default '';

alter table research.pilot_responses
  add constraint pilot_responses_item_quality_ck check (
    item_quality_code in (
      'NONE',
      'TOO_OBVIOUS',
      'UNNATURAL_OR_IMPOSSIBLE',
      'POLICY_INSTRUMENT_AMBIGUITY',
      'CONTEXT_REFERENCE_AMBIGUITY',
      'UI_PROBLEM',
      'OTHER'
    )
    and char_length(item_quality_note) <= 1000
    and ((item_quality_code = 'OTHER' and char_length(item_quality_note) >= 1)
      or (item_quality_code <> 'OTHER' and item_quality_note = ''))
  );

create index pilot_invites_direct_registration_idx
  on private.pilot_invites (instrument_sha256, registration_identity_hmac)
  where invite_purpose = 'participant_direct'
    and registration_identity_hmac is not null;

comment on column private.pilot_invites.registration_identity_hmac is
  'Domain-separated HMAC of normalized name and phone for a direct-entry session. Raw identity is never stored until final consented submission.';

comment on column research.pilot_responses.item_quality_code is
  'Pilot item-quality diagnostic. It is not a stance response, score, or analysis outcome.';

commit;
