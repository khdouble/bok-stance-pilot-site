-- Run in the Supabase SQL editor as the project owner, then download the result as CSV.
-- The result remains encrypted private material: download it only to the external
-- BOK_PILOT_PRIVATE_DIR, never anywhere inside this repository.
select
  p.participant_id,
  s.submission_id,
  s.assignment_code,
  p.identity_ciphertext_b64,
  p.identity_iv_b64,
  p.identity_aad,
  p.encryption_key_id,
  p.consent_version,
  p.consent_accepted_at,
  s.submitted_at
from private.participant_identity as p
join research.pilot_submissions as s
  on s.participant_id = p.participant_id
order by s.submitted_at, s.assignment_code;
