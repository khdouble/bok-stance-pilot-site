begin;

create table private.pilot_withdrawal_events (
  withdrawal_event_id uuid primary key,
  instrument_sha256 text not null
    references research.pilot_instruments (instrument_sha256) on delete restrict,
  procedure_version text not null,
  selector_kind text not null,
  request_received_at timestamptz not null,
  completed_at timestamptz not null default now(),
  outcome text not null,
  deleted_invites smallint not null,
  deleted_identities smallint not null,
  deleted_submissions smallint not null,
  deleted_responses smallint not null,
  verification_passed boolean not null,
  constraint pilot_withdrawal_events_version_ck check (
    procedure_version ~ '^withdrawal-v[0-9]{4}-[0-9]{2}-[0-9]{2}-r[1-9][0-9]*$'
  ),
  constraint pilot_withdrawal_events_selector_ck check (
    selector_kind in ('invite_id', 'normalized_identity')
  ),
  constraint pilot_withdrawal_events_time_ck check (
    request_received_at <= completed_at
  ),
  constraint pilot_withdrawal_events_verified_ck check (verification_passed),
  constraint pilot_withdrawal_events_counts_ck check (
    (
      outcome = 'deleted_submission'
      and deleted_invites = 1
      and deleted_identities = 1
      and deleted_submissions = 1
      and deleted_responses = 12
    )
    or
    (
      outcome = 'deleted_unused_invite'
      and deleted_invites = 1
      and deleted_identities = 0
      and deleted_submissions = 0
      and deleted_responses = 0
    )
  )
);

comment on table private.pilot_withdrawal_events is
  'Non-identifying completion log for verified individual pilot withdrawals. Never store participant selectors or free text.';

alter table private.pilot_withdrawal_events enable row level security;
alter table private.pilot_withdrawal_events force row level security;

revoke all on table private.pilot_withdrawal_events
  from public, anon, authenticated, service_role;

commit;
