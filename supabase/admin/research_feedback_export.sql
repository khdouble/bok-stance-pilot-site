-- Parameterized, PII-free hosted feedback query used by export_hosted_research.py.
-- The two parameters are the canonical hash and version read from docs/instrument.json.
select
  s.submission_id,
  s.assignment_code,
  s.instrument_sha256,
  s.instrument_version,
  s.fatigue_1to5,
  s.zero_vs_99_explanation,
  s.change_vs_stance_explanation,
  s.ui_error_note,
  to_char(s.session_started_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"') as session_started_at,
  to_char(s.session_finished_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"') as session_finished_at,
  s.active_duration_seconds,
  to_char(s.submitted_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"') as submitted_at,
  s.dataset_role,
  case when s.excluded_from_analysis then 'true' else 'false' end as excluded_from_analysis,
  s.analysis_exclusion_reason,
  s.payload_sha256
from research.pilot_submissions as s
where s.instrument_sha256 = %s
  and s.instrument_version = %s
order by s.submitted_at, s.submission_id;
