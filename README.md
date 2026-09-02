# BOK stance hosted pilot

사전에 연락한 5명의 참가자가 합성 통화정책 문장 12개를 평가하는 온라인
사용성 파일럿입니다. 이 저장소는 공개 GitHub Pages 화면과 Supabase
백엔드의 재현 가능한 배포 원본을 함께 보관합니다.

현재 상태는 STAGING / FIELDING HOLD입니다. 사이트를 공개해도
fieldingEnabled=false와 PENDING_PI 게이트 때문에 실제 설문은 열리지
않습니다. 개인정보 안내, 보유·파기 규칙, 기관 절차 및 실서버 E2E가 끝나기
전에는 이 게이트를 열지 마십시오.

## Supabase 프로젝트란

Supabase 프로젝트는 이 설문의 서버와 데이터베이스입니다. 제공된
mebisrsvasrzwkmsodsw.supabase.co 주소는 프로젝트 API 주소이지 관리 화면이나
비밀키가 아닙니다.

- GitHub Pages: 참가자가 보는 정적 HTML/CSS/JavaScript
- Supabase Edge Function: 초대 검증, 입력 검증, 암호화, 원자적 제출
- private schema: 암호화된 성명·전화번호, 동의기록, 초대 HMAC
- research schema: 가명 submission ID, 문항응답, 시간, 사용성 피드백

성명과 전화번호는 사전 지정 참가자를 응답자료에서 구별하고 제출자료를
확인하기 위한 정보입니다. 온라인 본인인증이나 신원 보증 기능은 아닙니다.

## 고정된 연구 경계

- Hosted version: v260903-pilot-hosted-1
- Hosted instrument SHA-256:
  4a07da2785bb2228787f2dd4e57339bd5c132111d693681a12cb62baf64978e7
- Parent offline instrument SHA-256:
  b594a196eb7be720e57d974f4b5c6e4437b697e6ae20f01013e830af35707a51
- Dataset role: synthetic_usability_pilot
- excluded_from_analysis: true

Hosted 파일럿은 기존 offline instrument의 후속 배포형식이지만 해시와
collector가 다릅니다. Hosted 결과를 기존 b594... collector에 맞추려고
해시나 버전을 바꾸지 마십시오.

## 폴더

- docs/: GitHub Pages 공개 파일
- supabase/migrations/: DB schema와 동결 instrument seed
- supabase/functions/pilot-api/: 공개 브라우저와 DB 사이의 유일한 API
- supabase/admin/: 초대, PII 복호화, research export, 보유기간 삭제 도구
- tools/: instrument/manifest builder, release validator, hosted collector, tests

## 로컬 미리보기와 검증

저장소 루트에서 다음을 실행합니다.

    python -m http.server 8765 --bind 127.0.0.1 --directory docs

그다음 로컬 브라우저에서 아래 주소를 엽니다. 이 모드는 DB에 제출하지
않습니다.

    http://127.0.0.1:8765/?preview=1

전체 정적·백엔드 검증:

    python -X utf8 tools/validate_release.py --source-pilot ../latent_stance_pipeline/02_annotation/pilot --expect-fielding staging
    python -X utf8 -m unittest discover -s supabase/tests -v

Node가 PATH에 없을 때 이 컴퓨터의 VS Code bundled Node로 실행:

    powershell -ExecutionPolicy Bypass -File tools/run_node_tests.ps1

이 wrapper는 두 child process를 각각 기다리고 실제 exit code가 0이 아니면
즉시 실패합니다.

## GitHub Pages 배포

1. main branch에 이 저장소를 push합니다.
2. GitHub 저장소의 Settings → Pages로 이동합니다.
3. Source를 Deploy from a branch로 선택합니다.
4. Branch는 main, folder는 /docs로 선택하고 Save를 누릅니다.
5. 배포 주소는 https://khdouble.github.io/bok-stance-pilot-site/ 입니다.

공개 저장소와 docs에는 비밀키, DB 비밀번호, raw 초대 토큰, 참가자 명단,
응답 export를 절대 넣지 마십시오.

## Supabase 배포 순서

프로젝트 dashboard는 다음 주소입니다.

    https://supabase.com/dashboard/project/mebisrsvasrzwkmsodsw

Windows에서는 공식 안내에 따라 Supabase CLI를 설치한 뒤, 저장소 루트에서
로그인과 연결을 수행합니다. 로그인 토큰과 DB 비밀번호는 이 대화나 GitHub에
붙여 넣지 말고 CLI의 로컬 입력창에만 입력합니다.

    supabase login
    supabase link --project-ref mebisrsvasrzwkmsodsw
    supabase db push --dry-run
    supabase db push

Dashboard의 Data API integration 화면에서 Enable Data API가 꺼져 있는지도
확인합니다. 이 프로젝트는 REST/GraphQL Data API를 사용하지 않습니다.

다음으로 supabase/.env.example을 참고해 Git/Google Drive 저장소 밖의
OS 로컬 보호 디렉터리에 pilot-function.env를 만듭니다. Windows 기본 운영
경로는 %LOCALAPPDATA%\bok-stance-pilot입니다. 세 HMAC/암호화 키는 각각
서로 다른 32-byte 무작위 값이어야 합니다. 저장소의 무시된 .private도
동기화될 수 있으므로 실운영 비밀정보를 넣지 마십시오.

    $pilotPrivate = Join-Path $env:LOCALAPPDATA "bok-stance-pilot"
    New-Item -ItemType Directory -Force -Path $pilotPrivate
    $pilotEnv = Join-Path $pilotPrivate "pilot-function.env"
    if (Test-Path -LiteralPath $pilotEnv) { throw "Refusing to overwrite $pilotEnv" }
    Copy-Item -LiteralPath "supabase/.env.example" -Destination $pilotEnv
    $env:BOK_PILOT_PRIVATE_DIR = $pilotPrivate
    supabase secrets set --env-file $pilotEnv
    supabase functions deploy pilot-api --no-verify-jwt

config.toml도 로컬 Data API를 꺼 두었고, 브라우저에는
anon/publishable/service-role/DB key가 하나도 들어가지 않습니다.

## 초대와 실서버 점검

실제 참가자 초대 전에 disposable 초대 1개로 다음을 확인합니다.

- 허용 origin CORS와 다른 origin 차단
- 유효·만료·폐기·재사용 invite
- 정확히 12개 배정과 변조 거부
- 제출 성공, 응답 유실 뒤 동일 idempotent retry
- private identity 암호화·로컬 복호화
- PII-free research export와 hosted collector
- cutoff dry-run과 보유기간 삭제
- Chrome/Edge 및 모바일 화면

raw 초대 링크는 저장소 밖 BOK_PILOT_PRIVATE_DIR 아래에만 생성되고 DB에는
HMAC만 들어갑니다. 관리자 도구는 저장소 내부 .private 경로도 거부합니다.
링크의 #invite fragment는 화면이 즉시 지우며 브라우저 저장소에는 보관하지
않습니다.

## 결과 export와 삭제

DB URI를 현재 프로세스의 SUPABASE_DB_URL에만 둔 뒤, read-only repeatable
snapshot exporter를 실행합니다. 내부 SQL은 instrument.json에서 읽은
최종 hash/version을 parameter binding하며 private schema나 식별자와
join하지 않습니다.

    python -m pip install "psycopg[binary]"
    python -X utf8 supabase/admin/export_hosted_research.py --expected-submissions 5 --output exports/pilot_YYYYMMDD

exporter는 hosted 전용 strict collector를 같은 실행에서 통과시킨 뒤에만
PII-free CSV 두 개와 provenance manifest를 만듭니다. 기존 offline
collector로 relabel하지 않습니다.

보유기간에 따른 전체 일괄 파기에는 먼저 read-only dry-run을 수행합니다.
도구가 출력한 exact confirmation phrase를 확인한 뒤에만 같은 cutoff로
삭제를 실행합니다.

이 cutoff 도구는 특정 개인의 철회 요청용이 아닙니다. 한 참가자의 철회·삭제
요청을 안전하게 식별하고 다른 참가자 자료를 건드리지 않는 절차는 fielding
전에 별도로 확정·시험해야 합니다.

    python -X utf8 supabase/admin/delete_retained_pilot_data.py --instrument-sha256 4a07da2785bb2228787f2dd4e57339bd5c132111d693681a12cb62baf64978e7 --cutoff 2026-12-31T23:59:59+09:00 --dry-run

## FIELDING HOLD 해제에 필요한 PI 확정값

- 개인정보 처리자/연구책임자 표시명(`dataController`)
- 실제 참가자 문의·철회 이메일(`contactEmail`)
- 고정 보유 종료일(`retentionEndDate`, `YYYY-MM-DD`)과 그 날짜 및
  파기 문구가 포함된 안내(`retentionNotice`)
- 승인된 동의문 버전(`consent-vYYYY-MM-DD-rN`)
- Supabase dashboard에 표시된 실제 저장 region code
- 기관 IRB/면제/비대상 판단(`approved`, `exempt`, `not_required`)과
  동일 prefix 및 날짜·문서번호가 있는 증빙 참조
  (예: `exempt:IRB-2026-001 exemption record`)
- 개별 철회 대상의 식별·삭제·검증 절차와 버전
  (`withdrawal-vYYYY-MM-DD-rN`)
- 최종 동의문·철회 절차를 반영한 실제 배포 뒤 disposable-invite E2E
  완료 UTC 시각(`YYYY-MM-DDTHH:MM:SSZ`)

위 값이 확정되면 privacy.html과 site-config.js를 수정하고 live deployment
manifest를 다시 만든 뒤 validator의 --expect-fielding live를 통과해야 합니다.
빈값, 예시값, 임의 문자열, 지난 보유 종료일, 미래 E2E 시각은 live gate를
통과하지 못합니다. DB의 fielding_open은 마지막에 별도로 여는 이중
게이트입니다.
