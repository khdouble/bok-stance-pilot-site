-- One-time R3 PI preview authorization; raw token is external only.
begin;
insert into private.pilot_invites (invite_id, token_hmac, instrument_sha256, assignment_code, expires_at, invite_purpose) values ('0769ea64-46b8-4c4f-8a0f-177f84cfd70c'::uuid, decode('db24a950d3f52e8ed1930a4228d1988b2bc3207d10b5deb0a171861c5e1fead7', 'hex'), '6cb5d63b5c4a764f9800bb1dd423f99b31b04d3a34854a011f0814a6550c5d41', 'PILOT_R01', '2026-09-08T10:28:23Z'::timestamptz, 'pi_preview');
commit;
