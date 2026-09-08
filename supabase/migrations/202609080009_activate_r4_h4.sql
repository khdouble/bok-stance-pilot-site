-- Generated from the locked R4 public instrument.
-- Append-only H3 -> H4 transition. H2 manual-test and H3 PI-preview rows remain preserved and permanently analysis-excluded.
begin;
set local standard_conforming_strings = on;

lock table research.pilot_instruments in share row exclusive mode;
lock table research.pilot_items in share row exclusive mode;
lock table research.pilot_assignment_sets in share row exclusive mode;
lock table research.pilot_assignments in share row exclusive mode;
lock table private.pilot_invites in share row exclusive mode;
lock table private.participant_identity in share row exclusive mode;
lock table research.pilot_submissions in share row exclusive mode;
lock table research.pilot_responses in share row exclusive mode;

do $r4_transition_guard$
begin
  if exists (select 1 from research.pilot_instruments where fielding_open) then
    raise exception 'H4 activation requires every fielding gate to be closed';
  end if;
  if (select count(*) from research.pilot_instruments where is_active) <> 1
     or not exists (select 1 from research.pilot_instruments where instrument_sha256 = '6cb5d63b5c4a764f9800bb1dd423f99b31b04d3a34854a011f0814a6550c5d41' and is_active and not fielding_open) then
    raise exception 'H4 activation requires exactly active closed H3 baseline';
  end if;
  if exists (select 1 from research.pilot_instruments where instrument_sha256 = '4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153') then
    raise exception 'H4 instrument already exists';
  end if;
  if exists (
    select 1 from research.pilot_submissions
    where instrument_sha256 = '6cb5d63b5c4a764f9800bb1dd423f99b31b04d3a34854a011f0814a6550c5d41'
      and (dataset_role <> 'r3_pi_preview' or not excluded_from_analysis
           or analysis_exclusion_reason <> 'r3_pi_preview_never_analysis')
  ) then
    raise exception 'H3 history is not the expected permanently excluded PI preview';
  end if;
  if exists (
    select 1 from private.pilot_invites
    where instrument_sha256 = '6cb5d63b5c4a764f9800bb1dd423f99b31b04d3a34854a011f0814a6550c5d41'
      and (invite_purpose <> 'pi_preview' or used_at is null or submission_id is null)
  ) then
    raise exception 'H3 must contain only finalized PI-preview access before H4 activation';
  end if;
end;
$r4_transition_guard$;
insert into research.pilot_instruments (
  instrument_sha256, instrument_version, source_offline_sha256,
  expected_response_count, is_active, fielding_open
) values (
  '4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'v260908-r4-public-1',
  'de96c00e9f95a9035cfc54a1bf18cc0d24b3465f7b5edc05cd9669a3fbfa7dee',
  12, false, false
);

insert into research.pilot_items (instrument_sha256, pilot_item_id, sentence_text) values
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', '19990304_S002', '다만 엔화 약세 등으로 인한 수출부진 가능성, 기업 구조조정과정에서의 노사갈등 우려 등 대내외 불안요인이 잠재해 있음'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', '20010508_S005', '앞으로도 물가 및 경기, 금융시장 상황, 대외여건 변화 등을 종합적으로 감안하여 경기회복을 뒷받침하면서도 수요면에서 물가압력이 발생하지 않도록 통화정책을 운용할 것이며 특히 최근의 물가상승세가 일반의 인플레기대심리 확산으로 이어지지 않도록 유의할 것임'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', '20040812_S002', '물가면에서는 국제유가 급등 등의 영향으로 상승압력이 높아지고 있으나 내수 저조로 수요압력이 미약하여 근원인플레이션율은 목표범위 내에서 유지되고 있음'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', '20060309_S001', '실물경제는 건설투자의 증가가 미약하나 수출이 견실한 증가세를 유지하고 민간소비가 회복세를 지속하는 가운데 설비투자도 증가세를 이어가고 있음'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', '20071207_S004', '소비자물가는 고유가의 영향 등으로 상승세가 확대되는 모습을 보이고 있으며 부동산가격은 오름세가 제한되고 있음'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', '20090409_S006', '앞으로 통화정책은 당분간 금융완화기조를 유지하면서 경기의 과도한 위축을 방지하고 금융시장 안정을 도모하는 데 주안점을 두고 운용해 나갈 것임'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', '20111013_S008', '앞으로 농산물가격 하락 및 전년도로부터의 기저효과 등이 물가상승률을 낮추는 요인으로 작용하겠으나, 계속 높게 유지되고 있는 인플레이션 기대심리 등으로 물가상승률의 하락 속도는 완만할 것으로 예상된다.'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', '20120608_S005', '국내경제를 보면, 수출이 대체로 전년도 수준을 유지하는 가운데 소비와 설비투자가 소폭 증가로 전환하면서 미약하나마 성장세는 이어지고 있다.'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', '20150409_S004', '국내경제를 보면, 경제주체들의 심리가 뚜렷이 회복되지 못한 가운데 수출이 석유제품 등의 단가하락 등에 기인하여 감소세를 지속하였으나 소비, 투자 등 내수는 개선되는 모습을 나타내었다.'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', '20240412_S018', '국내경제는 성장세가 개선 흐름을 지속하는 가운데 근원물가 상승률의 둔화 추세가 이어질 것으로 예상되지만 소비자물가 전망과 관련한 불확실성이 높기 때문에 물가가 목표수준으로 수렴할 것으로 확신하기는 아직 이른 상황이다.'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', '20250116_S019', '국내경제는 물가상승률이 안정적인 흐름을 이어가고 있으나 정치적 리스크 확대로 성장의 하방위험이 증대되고 경제전망의 불확실성도 커진 상황이다.'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', '20260410_S016', '향후 물가경로는 국제유가 및 환율 움직임, 정부 물가안정 대책의 효과, 비용상승의 파급 정도 등과 관련한 불확실성이 매우 높은 상황이다.');

insert into research.pilot_assignment_sets (instrument_sha256, assignment_code, expected_item_count) values
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R01', 12),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R02', 12),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R03', 12),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R04', 12),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R05', 12);

insert into research.pilot_assignments (
  instrument_sha256, assignment_code, display_position, pilot_item_id, assignment_id
) values
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R01', 1, '19990304_S002', 'H4_PILOT_R01_01'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R01', 2, '20010508_S005', 'H4_PILOT_R01_02'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R01', 3, '20040812_S002', 'H4_PILOT_R01_03'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R01', 4, '20060309_S001', 'H4_PILOT_R01_04'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R01', 5, '20071207_S004', 'H4_PILOT_R01_05'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R01', 6, '20090409_S006', 'H4_PILOT_R01_06'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R01', 7, '20111013_S008', 'H4_PILOT_R01_07'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R01', 8, '20120608_S005', 'H4_PILOT_R01_08'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R01', 9, '20150409_S004', 'H4_PILOT_R01_09'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R01', 10, '20250116_S019', 'H4_PILOT_R01_10'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R01', 11, '20240412_S018', 'H4_PILOT_R01_11'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R01', 12, '20260410_S016', 'H4_PILOT_R01_12'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R02', 1, '20060309_S001', 'H4_PILOT_R02_01'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R02', 2, '20250116_S019', 'H4_PILOT_R02_02'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R02', 3, '20010508_S005', 'H4_PILOT_R02_03'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R02', 4, '20240412_S018', 'H4_PILOT_R02_04'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R02', 5, '20120608_S005', 'H4_PILOT_R02_05'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R02', 6, '19990304_S002', 'H4_PILOT_R02_06'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R02', 7, '20260410_S016', 'H4_PILOT_R02_07'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R02', 8, '20040812_S002', 'H4_PILOT_R02_08'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R02', 9, '20150409_S004', 'H4_PILOT_R02_09'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R02', 10, '20071207_S004', 'H4_PILOT_R02_10'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R02', 11, '20111013_S008', 'H4_PILOT_R02_11'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R02', 12, '20090409_S006', 'H4_PILOT_R02_12'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R03', 1, '20111013_S008', 'H4_PILOT_R03_01'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R03', 2, '19990304_S002', 'H4_PILOT_R03_02'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R03', 3, '20240412_S018', 'H4_PILOT_R03_03'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R03', 4, '20071207_S004', 'H4_PILOT_R03_04'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R03', 5, '20120608_S005', 'H4_PILOT_R03_05'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R03', 6, '20040812_S002', 'H4_PILOT_R03_06'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R03', 7, '20260410_S016', 'H4_PILOT_R03_07'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R03', 8, '20010508_S005', 'H4_PILOT_R03_08'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R03', 9, '20250116_S019', 'H4_PILOT_R03_09'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R03', 10, '20060309_S001', 'H4_PILOT_R03_10'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R03', 11, '20090409_S006', 'H4_PILOT_R03_11'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R03', 12, '20150409_S004', 'H4_PILOT_R03_12'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R04', 1, '20150409_S004', 'H4_PILOT_R04_01'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R04', 2, '20090409_S006', 'H4_PILOT_R04_02'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R04', 3, '20060309_S001', 'H4_PILOT_R04_03'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R04', 4, '20260410_S016', 'H4_PILOT_R04_04'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R04', 5, '20040812_S002', 'H4_PILOT_R04_05'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R04', 6, '20111013_S008', 'H4_PILOT_R04_06'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R04', 7, '19990304_S002', 'H4_PILOT_R04_07'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R04', 8, '20250116_S019', 'H4_PILOT_R04_08'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R04', 9, '20071207_S004', 'H4_PILOT_R04_09'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R04', 10, '20240412_S018', 'H4_PILOT_R04_10'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R04', 11, '20010508_S005', 'H4_PILOT_R04_11'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R04', 12, '20120608_S005', 'H4_PILOT_R04_12'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R05', 1, '20240412_S018', 'H4_PILOT_R05_01'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R05', 2, '20120608_S005', 'H4_PILOT_R05_02'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R05', 3, '20071207_S004', 'H4_PILOT_R05_03'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R05', 4, '19990304_S002', 'H4_PILOT_R05_04'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R05', 5, '20250116_S019', 'H4_PILOT_R05_05'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R05', 6, '20010508_S005', 'H4_PILOT_R05_06'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R05', 7, '20090409_S006', 'H4_PILOT_R05_07'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R05', 8, '20111013_S008', 'H4_PILOT_R05_08'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R05', 9, '20060309_S001', 'H4_PILOT_R05_09'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R05', 10, '20150409_S004', 'H4_PILOT_R05_10'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R05', 11, '20260410_S016', 'H4_PILOT_R05_11'),
  ('4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153', 'PILOT_R05', 12, '20040812_S002', 'H4_PILOT_R05_12');

do $r4_seed_validation$
begin
  if (select count(*) from research.pilot_items where instrument_sha256 = '4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153') <> 12
     or (select count(*) from research.pilot_assignment_sets where instrument_sha256 = '4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153') <> 5
     or (select count(*) from research.pilot_assignments where instrument_sha256 = '4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153') <> 60 then
    raise exception 'H4 seed counts are invalid';
  end if;
end;
$r4_seed_validation$;

update research.pilot_instruments
set is_active = false,
    fielding_open = false,
    fielding_closed_at = coalesce(fielding_closed_at, now())
where instrument_sha256 = '6cb5d63b5c4a764f9800bb1dd423f99b31b04d3a34854a011f0814a6550c5d41' and is_active;

update research.pilot_instruments
set is_active = true, activated_at = now(), fielding_open = false
where instrument_sha256 = '4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153';

do $r4_activation_validation$
begin
  if (select count(*) from research.pilot_instruments where is_active) <> 1
     or not exists (select 1 from research.pilot_instruments where instrument_sha256 = '4a8d23b10cc1f6e5a2234b2487ffc4c738b43301677d89302408be463e80f153' and is_active and not fielding_open) then
    raise exception 'H4 did not become the sole active closed-gate instrument';
  end if;
end;
$r4_activation_validation$;

commit;
