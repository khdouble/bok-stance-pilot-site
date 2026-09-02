-- Parameterized, PII-free hosted response query used by export_hosted_research.py.
-- The two parameters are the canonical hash and version read from docs/instrument.json.
select
  s.submission_id,
  s.assignment_code,
  s.instrument_sha256,
  s.instrument_version,
  r.display_position,
  r.assignment_id,
  r.pilot_item_id,
  i.sentence_text,
  r.stance_label as label5,
  case when r.abstain then 'true' else 'false' end as abstain,
  r.reason_code,
  r.confidence,
  r.reason_note,
  to_char(r.started_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"') as started_at,
  to_char(r.finished_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"') as finished_at,
  r.active_duration_seconds,
  r.response_status,
  s.dataset_role,
  case when s.excluded_from_analysis then 'true' else 'false' end as excluded_from_analysis,
  s.analysis_exclusion_reason,
  to_char(s.submitted_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"') as submitted_at,
  s.payload_sha256
from research.pilot_responses as r
join research.pilot_submissions as s
  on s.submission_id = r.submission_id
join research.pilot_items as i
  on i.instrument_sha256 = r.instrument_sha256
 and i.pilot_item_id = r.pilot_item_id
where s.instrument_sha256 = %s
  and s.instrument_version = %s
order by s.submitted_at, s.submission_id, r.display_position;
