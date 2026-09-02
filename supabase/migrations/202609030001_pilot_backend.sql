begin;

create schema if not exists private;
create schema if not exists research;

comment on schema private is 'Direct identifiers and invitation authentication material. Never expose through the Data API.';
comment on schema research is 'Pseudonymous pilot instruments and responses. Export only through an approved PI workflow.';

revoke all on schema private from public, anon, authenticated, service_role;
revoke all on schema research from public, anon, authenticated, service_role;

alter default privileges in schema private revoke all on tables from public, anon, authenticated, service_role;
alter default privileges in schema private revoke all on sequences from public, anon, authenticated, service_role;
alter default privileges in schema private revoke execute on functions from public, anon, authenticated, service_role;
alter default privileges in schema research revoke all on tables from public, anon, authenticated, service_role;
alter default privileges in schema research revoke all on sequences from public, anon, authenticated, service_role;
alter default privileges in schema research revoke execute on functions from public, anon, authenticated, service_role;

create table research.pilot_instruments (
  instrument_sha256 text primary key,
  instrument_version text not null unique,
  source_offline_sha256 text not null,
  expected_response_count smallint not null default 12,
  is_active boolean not null default false,
  fielding_open boolean not null default false,
  created_at timestamptz not null default now(),
  activated_at timestamptz,
  fielding_opened_at timestamptz,
  fielding_closed_at timestamptz,
  constraint pilot_instruments_hash_ck check (instrument_sha256 ~ '^[0-9a-f]{64}$'),
  constraint pilot_instruments_source_hash_ck check (source_offline_sha256 ~ '^[0-9a-f]{64}$'),
  constraint pilot_instruments_count_ck check (expected_response_count = 12),
  constraint pilot_instruments_activation_ck check ((is_active and activated_at is not null) or (not is_active)),
  constraint pilot_instruments_fielding_ck check (
    (fielding_open and is_active and fielding_opened_at is not null and fielding_closed_at is null)
    or not fielding_open
  )
);

create unique index pilot_instruments_one_active_idx
  on research.pilot_instruments (is_active)
  where is_active;

create table research.pilot_items (
  instrument_sha256 text not null references research.pilot_instruments (instrument_sha256) on delete restrict,
  pilot_item_id text not null,
  sentence_text text not null,
  created_at timestamptz not null default now(),
  primary key (instrument_sha256, pilot_item_id),
  constraint pilot_items_id_ck check (pilot_item_id ~ '^[A-Za-z0-9_-]{1,64}$'),
  constraint pilot_items_text_ck check (char_length(sentence_text) between 1 and 10000)
);

create table research.pilot_assignment_sets (
  instrument_sha256 text not null references research.pilot_instruments (instrument_sha256) on delete restrict,
  assignment_code text not null,
  expected_item_count smallint not null default 12,
  created_at timestamptz not null default now(),
  primary key (instrument_sha256, assignment_code),
  constraint pilot_assignment_sets_code_ck check (assignment_code ~ '^[A-Za-z0-9_-]{1,64}$'),
  constraint pilot_assignment_sets_count_ck check (expected_item_count = 12)
);

create table research.pilot_assignments (
  instrument_sha256 text not null,
  assignment_code text not null,
  display_position smallint not null,
  pilot_item_id text not null,
  assignment_id text not null,
  primary key (instrument_sha256, assignment_code, display_position),
  unique (instrument_sha256, assignment_code, pilot_item_id),
  unique (instrument_sha256, assignment_id),
  foreign key (instrument_sha256, assignment_code)
    references research.pilot_assignment_sets (instrument_sha256, assignment_code) on delete restrict,
  foreign key (instrument_sha256, pilot_item_id)
    references research.pilot_items (instrument_sha256, pilot_item_id) on delete restrict,
  constraint pilot_assignments_position_ck check (display_position between 1 and 12),
  constraint pilot_assignments_id_ck check (assignment_id ~ '^[A-Za-z0-9_-]{1,96}$')
);

create table private.pilot_invites (
  invite_id uuid primary key,
  token_hmac bytea not null unique,
  instrument_sha256 text not null,
  assignment_code text not null,
  expires_at timestamptz not null,
  revoked_at timestamptz,
  used_at timestamptz,
  submission_id uuid unique,
  created_at timestamptz not null default now(),
  foreign key (instrument_sha256, assignment_code)
    references research.pilot_assignment_sets (instrument_sha256, assignment_code) on delete restrict,
  constraint pilot_invites_hmac_ck check (octet_length(token_hmac) = 32),
  constraint pilot_invites_expiry_ck check (expires_at > created_at)
);

create table private.participant_identity (
  participant_id uuid primary key,
  invite_id uuid not null unique references private.pilot_invites (invite_id) on delete restrict,
  identity_ciphertext_b64 text not null,
  identity_iv_b64 text not null,
  identity_aad text not null,
  identity_hmac bytea not null,
  encryption_algorithm text not null default 'AES-256-GCM',
  encryption_key_id text not null,
  consent_version text not null,
  consent_accepted_at timestamptz not null,
  created_at timestamptz not null default now(),
  constraint participant_identity_ciphertext_ck check (char_length(identity_ciphertext_b64) between 24 and 8192),
  constraint participant_identity_iv_ck check (identity_iv_b64 ~ '^[A-Za-z0-9+/]{16}$'),
  constraint participant_identity_aad_ck check (char_length(identity_aad) between 1 and 512),
  constraint participant_identity_hmac_ck check (octet_length(identity_hmac) = 32),
  constraint participant_identity_algorithm_ck check (encryption_algorithm = 'AES-256-GCM'),
  constraint participant_identity_key_id_ck check (encryption_key_id ~ '^[A-Za-z0-9._-]{1,64}$'),
  constraint participant_identity_consent_ck check (char_length(consent_version) between 1 and 80)
);

create table research.pilot_submissions (
  submission_id uuid primary key,
  participant_id uuid not null unique,
  invite_id uuid not null unique,
  instrument_sha256 text not null,
  instrument_version text not null,
  assignment_code text not null,
  idempotency_key uuid not null unique,
  payload_sha256 text not null,
  session_started_at timestamptz not null,
  session_finished_at timestamptz not null,
  active_duration_seconds integer not null,
  fatigue_1to5 smallint not null,
  zero_vs_99_explanation text not null,
  change_vs_stance_explanation text not null,
  ui_error_note text not null default '',
  dataset_role text not null default 'synthetic_usability_pilot',
  excluded_from_analysis boolean not null default true,
  analysis_exclusion_reason text not null default 'synthetic_usability_only_never_analysis',
  submitted_at timestamptz not null default now(),
  foreign key (participant_id)
    references private.participant_identity (participant_id) on delete restrict,
  foreign key (invite_id)
    references private.pilot_invites (invite_id) on delete restrict,
  foreign key (instrument_sha256, assignment_code)
    references research.pilot_assignment_sets (instrument_sha256, assignment_code) on delete restrict,
  constraint pilot_submissions_version_ck check (char_length(instrument_version) between 1 and 80),
  constraint pilot_submissions_payload_hash_ck check (payload_sha256 ~ '^[0-9a-f]{64}$'),
  constraint pilot_submissions_timing_ck check (
    session_started_at < session_finished_at
    and active_duration_seconds between 1 and 21600
    and active_duration_seconds <= extract(epoch from (session_finished_at - session_started_at)) + 5
  ),
  constraint pilot_submissions_fatigue_ck check (fatigue_1to5 between 1 and 5),
  constraint pilot_submissions_feedback_ck check (
    char_length(zero_vs_99_explanation) between 1 and 2000
    and char_length(change_vs_stance_explanation) between 1 and 2000
    and char_length(ui_error_note) <= 2000
  ),
  constraint pilot_submissions_role_ck check (
    dataset_role = 'synthetic_usability_pilot'
    and excluded_from_analysis
    and analysis_exclusion_reason = 'synthetic_usability_only_never_analysis'
  )
);

alter table private.pilot_invites
  add constraint pilot_invites_submission_fk
  foreign key (submission_id)
  references research.pilot_submissions (submission_id)
  on delete restrict;

create table research.pilot_responses (
  submission_id uuid not null references research.pilot_submissions (submission_id) on delete restrict,
  instrument_sha256 text not null,
  display_position smallint not null,
  assignment_id text not null,
  pilot_item_id text not null,
  stance_label smallint,
  abstain boolean not null,
  reason_code text not null,
  confidence smallint not null,
  reason_note text not null default '',
  started_at timestamptz not null,
  finished_at timestamptz not null,
  active_duration_seconds integer not null,
  response_status text not null default 'COMPLETED',
  primary key (submission_id, display_position),
  unique (submission_id, pilot_item_id),
  foreign key (instrument_sha256, pilot_item_id)
    references research.pilot_items (instrument_sha256, pilot_item_id) on delete restrict,
  constraint pilot_responses_position_ck check (display_position between 1 and 12),
  constraint pilot_responses_label_ck check (
    (abstain and stance_label is null) or
    (not abstain and stance_label in (-2, -1, 0, 1, 2))
  ),
  constraint pilot_responses_reason_code_ck check (
    reason_code in ('NONE', 'IRRELEVANT', 'CONTEXT_NEEDED', 'MIXED_UNRESOLVED', 'TERMINOLOGY', 'OTHER')
    and (not abstain or reason_code <> 'NONE')
    and ((reason_code = 'OTHER' and char_length(reason_note) between 1 and 1000)
      or (reason_code <> 'OTHER' and reason_note = ''))
  ),
  constraint pilot_responses_confidence_ck check (confidence between 1 and 5),
  constraint pilot_responses_timing_ck check (
    started_at < finished_at
    and active_duration_seconds between 0 and 21600
    and active_duration_seconds <= extract(epoch from (finished_at - started_at)) + 1
  ),
  constraint pilot_responses_status_ck check (response_status = 'COMPLETED')
);

alter table research.pilot_instruments enable row level security;
alter table research.pilot_instruments force row level security;
alter table research.pilot_items enable row level security;
alter table research.pilot_items force row level security;
alter table research.pilot_assignment_sets enable row level security;
alter table research.pilot_assignment_sets force row level security;
alter table research.pilot_assignments enable row level security;
alter table research.pilot_assignments force row level security;
alter table private.pilot_invites enable row level security;
alter table private.pilot_invites force row level security;
alter table private.participant_identity enable row level security;
alter table private.participant_identity force row level security;
alter table research.pilot_submissions enable row level security;
alter table research.pilot_submissions force row level security;
alter table research.pilot_responses enable row level security;
alter table research.pilot_responses force row level security;

revoke all on all tables in schema private from public, anon, authenticated, service_role;
revoke all on all tables in schema research from public, anon, authenticated, service_role;
revoke all on all sequences in schema private from public, anon, authenticated, service_role;
revoke all on all sequences in schema research from public, anon, authenticated, service_role;

commit;
