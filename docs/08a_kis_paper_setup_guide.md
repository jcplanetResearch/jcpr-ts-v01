# 08a. KIS 모의투자 발급 및 OpenAPI 설정 안내서
# (KIS Paper Account & OpenAPI Setup Guide)

| 항목 (Item) | 값 (Value) |
|---|---|
| 프로젝트명 (Project) | jcpr-ts-v01 |
| 문서 (Document) | docs/08a_kis_paper_setup_guide.md |
| 버전 (Version) | v0.1 |
| 작성일 (Date) | 2026-05-04 |
| 사용자 (User) | JCPR 전용 (JCPR sole use) |
| 관련 문서 (Related) | docs/00_target_operating_model.md, docs/09_production_rollout.md |
| 관련 산출물 (Related artifacts) | `.env.example` (Task 4), `src/brokers/kis_adapter.py` (Task 8 — 본 안내서 완료 후 진행) |

---

## ⚠️ 본 안내서 사용 시 주의 (Important Notice)

본 안내서는 **개념적 가이드(conceptual guide)** 입니다. KIS의 실제 웹사이트 UI, 메뉴 명칭, 신청 양식, 정확한 URL 등은 KIS 측에서 변경될 수 있습니다. 따라서 절차를 진행하실 때는 **반드시 KIS 공식 사이트의 최신 안내를 함께 참조**하시기 바랍니다.

본 안내서에서 `[verify]` 표기가 된 항목은 사용자께서 KIS 공식 사이트에서 직접 확인 후 채워 넣어야 하는 부분입니다.

**공식 참조 사이트 (Official references):**
- 한국투자증권: https://www.koreainvestment.com
- KIS Developers: https://apiportal.koreainvestment.com

---

## 1. 본 문서의 목적과 완료 기준 (Purpose & Done-Criteria)

### 1.1 목적 (Purpose)

본 안내서는 **사용자(JCPR 운영자)가 한국투자증권 모의투자 OpenAPI 사용을 위해 필요한 외부 절차** 를 안내한다. 모든 절차는 사용자의 로컬 환경 또는 KIS 사이트에서 직접 수행되며, 컨테이너 환경에서는 수행되지 않는다.

### 1.2 완료 기준 (Done-Criteria)

다음 항목이 **모두 ✅ 상태**가 되면 Task 8 (Phase 1 코드) 진입 가능:

- [ ] 한국투자증권 모의투자 계좌 발급 완료
- [ ] KIS Developers 가입 및 OpenAPI 사용 신청 완료
- [ ] 모의투자용 앱키(App Key) 및 앱시크릿(App Secret) 수령
- [ ] 운영 환경 IP 등록 완료 (필요한 경우)
- [ ] `.env.local` 파일 작성 (저장소 외부 또는 `.gitignore` 보호 영역)
- [ ] `.env` 파일이 `git status` 에서 보이지 않음 (보안 검증)
- [ ] curl 으로 토큰 발급 1회 성공 (검증)

---

## 2. 사전 준비 확인 (Prerequisites)

| 항목 (Item) | 상태 (Status) |
|---|---|
| 한국투자증권 (실거래) 계정 | ✅ 보유 (사용자 확인) |
| 본인 명의 휴대폰 | 본인 인증용 |
| 공동인증서 또는 모바일 인증 수단 | KIS 가입·로그인 시 필요 |
| 운영 환경 (로컬 PC) IP 주소 | KIS Developers 등록 시 필요할 수 있음 |
| Python 3.11+ + `requests` | Task 8 코드 실행 환경 |

---

## 3. 모의투자 계좌 신청 (Paper Account Issuance)

### 3.1 일반 절차 (General Procedure)

KIS 모의투자 계좌는 일반적으로 다음 경로로 신청한다 (UI 변경 가능 — `[verify]`):

1. 한국투자증권 홈페이지 또는 모바일 앱 로그인
2. 메뉴: `[verify] 트레이딩 → 모의투자` 또는 유사 경로
3. 모의투자 신청
4. 모의투자 계좌번호 수령 (실거래 계좌와 **별도 번호**)

### 3.2 핵심 사실 (Key Facts)

- 모의투자 계좌는 **실거래 계좌와 별도 번호**다. 본 시스템은 모의투자 계좌만 사용한다.
- 모의투자 계좌에는 **가상 자본금**이 부여된다 (KIS 측 정책).
- 모의투자 계좌는 **주기적으로 초기화**될 수 있다 — 운영 중 잔고가 갑자기 리셋되면 KIS 측 일정 확인.
- 모의투자 운영 시간은 실시장 시간과 다를 수 있다 — `[verify]` KIS 공지.

### 3.3 사용자 작성란 (User Fill-in)

발급 완료 후 다음을 메모해 두십시오 (본 문서에 직접 적지 마십시오 — `.env.local` 에만):

```
[모의투자 계좌번호 종합]:  ___-________ (8자리-2자리 형식 등 KIS 정책에 따름)
[발급일자]:              YYYY-MM-DD
[가상 자본금]:            ___________원
```

---

## 4. KIS Developers 가입 및 OpenAPI 사용 신청 (Developer Portal)

### 4.1 가입 (Registration)

1. https://apiportal.koreainvestment.com 접속
2. 회원가입 (실거래 계정과 연동될 수 있음)
3. 약관 동의 — 특히 **시세 데이터 사용 약관** 주의 깊게 검토
4. OpenAPI 사용 신청

### 4.2 앱(App) 등록 (App Registration)

KIS Developers 에서 "앱" 단위로 앱키·앱시크릿을 발급받는다.

- **모의투자용 앱**과 **실거래용 앱**은 **별도로 등록**된다.
- 본 시스템은 현재 **모의투자용 앱키만** 사용한다.
- 앱 등록 시 입력 항목 (예시 — `[verify]`):
  - 앱 이름 (e.g. "jcpr-ts-v01-paper")
  - 앱 설명
  - 사용 목적 (자율 트레이딩 시스템 검증)
  - **콜백 URL 또는 IP 등록** (해당 시)

### 4.3 IP 등록 (IP Whitelist, if applicable)

KIS는 보안상 **호출 IP를 등록**받는 경우가 있다 — `[verify]`:

- 운영 환경의 **공인 IP** 등록
- 가정용 인터넷의 IP는 변경될 수 있으므로 주기적 확인 필요
- IP 변경 후 첫 호출에서 권한 거부 발생 시 IP 등록부터 점검

### 4.4 앱키 / 앱시크릿 수령 (Receiving Credentials)

발급 완료 후 사용자에게 노출되는 정보:

- **App Key** (영숫자 30자 이상)
- **App Secret** (영숫자 100자 이상)

🚨 **보안 절대 원칙 (Absolute Security Rule):**
- 본 두 값은 **사용자의 로컬 환경에만** 보관한다.
- 채팅·이메일·메신저·외부 시스템·GitHub 어떠한 곳에도 전송·복사·붙여넣기 금지.
- 컨테이너 환경, 클라우드 폴더, 동기화되는 폴더에 저장 금지.
- 앱시크릿이 노출됐다고 의심되면 **즉시 KIS Developers 에서 재발급(rotation)**.

---

## 5. `.env.local` 파일 작성 (Local Env Setup)

### 5.1 사전 검증 (Pre-check)

작성 전, 저장소가 안전한 상태인지 확인:

```bash
# 저장소 루트에서
cd ~/Projects/jcpr-ts-v01

# .env 가 .gitignore 에 있는지 확인 (Task 4)
grep -E "^\.env$" .gitignore && echo "✅ .env 차단됨" || echo "⚠️ .env 미차단 — 즉시 수정"

# 현재 .env 가 staged 되어 있지 않은지 확인
git status | grep -E "^\s*\.env$" && echo "⚠️ .env 가 staged — 즉시 unstage" || echo "✅ .env 미staged"
```

### 5.2 `.env.local` 작성 (Create .env.local)

`.env.example` (Task 4) 을 복사하여 실제 값으로 채운다:

```bash
cp .env.example .env.local
chmod 600 .env.local        # 본인만 읽기/쓰기 (Linux/macOS)
```

### 5.3 KIS 관련 변수 매핑 (KIS Variable Mapping)

`.env.local` 에 다음 항목을 채운다 (값은 모두 사용자의 로컬에만):

```bash
# ====== 운영 모드 ======
OPERATING_MODE=paper                  # paper 로 시작 (절대 권장)
SESSION_OWNER=JCPR

# ====== KIS 자격증명 (모의투자) ======
BROKER_NAME=kis
BROKER_API_KEY=<KIS App Key>          # KIS Developers 에서 발급된 모의투자용 App Key
BROKER_API_SECRET=<KIS App Secret>    # KIS Developers 에서 발급된 모의투자용 App Secret

# Base URL — 모의투자 / 실거래 가 다르며 정확한 URL 은 KIS 공식 문서 참조 [verify]
BROKER_BASE_URL=<KIS_PAPER_BASE_URL>
# 일반적으로 알려진 형식:
#   모의투자: https://openapivts.koreainvestment.com:29443
#   실거래:   https://openapi.koreainvestment.com:9443
# 단, 정확한 URL 은 KIS Developers 공식 문서로 [verify]

BROKER_ACCOUNT_ID=<모의투자 계좌번호>   # §3.3 에서 받은 번호 (전체)

# ====== 그 외는 .env.example 기본값 유지 ======
```

### 5.4 보안 즉시 검증 (Immediate Security Check)

```bash
# .env.local 이 git 에 보이지 않는지
git status | grep -E "\.env\.local" && echo "⚠️ 차단 실패" || echo "✅ 정상 차단"

# .env.local 권한 확인 (Linux/macOS)
ls -l .env.local
# 출력에서 -rw------- (600) 인지 확인
```

---

## 6. 보안 점검 체크리스트 (Security Checklist)

`.env.local` 작성 후 다음을 모두 확인:

- [ ] `.env.local` 이 `git status` 출력에 나타나지 않음
- [ ] `git diff --cached` 에 App Key 나 App Secret 의 일부 문자열도 노출되지 않음
- [ ] `.env.local` 의 OS 권한이 600 (또는 사용자 본인만 접근 가능)
- [ ] App Key/Secret 을 채팅·이메일·메신저로 전송한 이력 없음
- [ ] 클립보드에 잔류한 시크릿 제거 (다른 텍스트 복사로 덮어쓰기)
- [ ] 클라우드 동기화 폴더(예: 일부 드라이브 폴더)가 저장소 경로와 겹치지 않음

---

## 7. 토큰 발급 검증 (Token Issuance Verification)

KIS는 OpenAPI 호출 전 **접근 토큰(access token)** 을 먼저 발급받아야 한다. 본 시스템 구현 전에, **단순 curl 테스트**로 발급이 성공하는지 검증한다.

### 7.1 환경변수 로드 (Load Env)

```bash
cd ~/Projects/jcpr-ts-v01

# .env.local 을 현재 셸에 로드
set -a; source .env.local; set +a

# 핵심 변수가 비어있지 않은지 (값 미출력, 길이만 확인)
echo "App Key 길이: ${#BROKER_API_KEY}"
echo "App Secret 길이: ${#BROKER_API_SECRET}"
echo "Base URL: $BROKER_BASE_URL"
```

### 7.2 토큰 발급 호출 예시 (Example — verify with KIS docs)

KIS의 토큰 발급 엔드포인트는 일반적으로 `/oauth2/tokenP` 형태이지만, **정확한 경로와 페이로드 형식은 KIS Developers 공식 문서로 `[verify]`** 하십시오.

일반적 형식 (verify with official docs):

```bash
# ⚠️ 아래는 일반적 형식이며, 정확한 필드명·경로는 KIS 공식 문서 확인 필요
curl -X POST "$BROKER_BASE_URL/oauth2/tokenP" \
  -H "Content-Type: application/json" \
  -d "{
    \"grant_type\": \"client_credentials\",
    \"appkey\": \"$BROKER_API_KEY\",
    \"appsecret\": \"$BROKER_API_SECRET\"
  }"
```

### 7.3 성공 응답 형태 (Expected Successful Response)

성공 시 응답에는 일반적으로 다음 필드가 포함된다 (`[verify]`):

- `access_token` — 영숫자 토큰
- `token_type` — 일반적으로 `Bearer`
- `expires_in` — 토큰 수명 (초 단위, 일반적으로 약 86400 = 24시간)

### 7.4 실패 시 일반적 원인 (Common Failure Causes)

| 증상 (Symptom) | 가능한 원인 (Likely Cause) |
|---|---|
| 401 Unauthorized | App Key 또는 App Secret 오타 |
| 403 Forbidden | IP 미등록 또는 OpenAPI 사용 권한 미승인 |
| 권한 거부 | 모의투자용 앱키를 실거래 URL 에 사용 (또는 그 반대) |
| Connection refused | Base URL 의 포트 누락 또는 오타 |
| 응답 없음 | 방화벽 또는 프록시 차단 |

### 7.5 검증 후 즉시 정리 (Post-verification Cleanup)

```bash
# 환경변수를 현재 셸에서 제거 (다른 프로세스에 새지 않도록)
unset BROKER_API_KEY BROKER_API_SECRET

# 셸 히스토리에서 토큰이나 시크릿이 들어간 명령 삭제 (선택)
history | grep -E "(token|secret|key)" -n | head
# 의심되는 줄이 있으면 history -d <line> 로 삭제 (Bash)
```

---

## 8. 자격증명 회전 정책 (Credential Rotation Policy)

`docs/00_target_operating_model.md` §7.5 (최소 권한 원칙) 의 일환으로 다음 정책을 따른다:

| 시나리오 (Scenario) | 회전 시점 (When to Rotate) |
|---|---|
| 정기 회전 (Routine) | 90일 주기 권장 |
| 발급 후 첫 운영 | 모의투자 → 실거래 전환 시점에 모의투자 키는 폐기 |
| 보안 사고 의심 | **즉시** (0일) — 의심만으로도 회전 |
| 신뢰할 수 없는 환경에서 사용 의심 | **즉시** |
| 채팅/메시지/외부 시스템에 노출 | **즉시** |
| IP 환경 변경 | 회전은 선택, 단 IP 재등록 필수 |

회전 절차:

1. KIS Developers 에서 새 App Key/Secret 발급
2. 새 값을 `.env.local` 에 입력 (이전 값 덮어쓰기)
3. 새 토큰 발급 검증 (§7)
4. KIS Developers 에서 이전 키 폐기
5. 회전 일자·사유 기록 (감사 로그)

---

## 9. 트러블슈팅 (Troubleshooting)

### 9.1 흔한 오류 (Common Errors)

| 오류 (Error) | 해석 / 대응 (Interpretation / Action) |
|---|---|
| `EGW00121` 또는 유사 | 일반적으로 인증 실패 — 키 오타·만료 확인 (`[verify]` KIS 코드표) |
| `40400000` 류 4xx | 클라이언트 오류 — 페이로드 형식·필드명 확인 |
| `5xxxxxxx` 류 5xx | KIS 서버 일시적 오류 — 백오프 후 재시도 |
| `Connection timeout` | 방화벽/프록시 또는 IP 미등록 |

### 9.2 모의투자 영업시간 (Paper Market Hours)

모의투자 환경은 실시장과 운영시간·서버 점검 일정이 다를 수 있다 — `[verify]` KIS 공지. 새벽 시간대 점검으로 예상치 못한 503 응답이 발생할 수 있다.

### 9.3 시간대 (Timezone)

본 시스템은 **내부 처리는 UTC, 운영자 표시는 KST** 정책을 따른다 (Task 8 설계 결정 §6 참조). KIS 응답의 timestamp 가 KST 인 경우, 코드(Phase 1)에서 수신 즉시 UTC 로 정규화한다. 사용자가 직접 검증할 때는 KST 기준으로 판독해도 무방하다.

### 9.4 모의투자 잔고 리셋 (Paper Balance Reset)

모의투자는 KIS 측 정책에 따라 주기적으로 잔고가 초기화될 수 있다. 본 시스템의 P&L 정합성 검증(reconciliation, Task 28) 단계에서 갑작스러운 잔고 변화가 감지되면, 우선 KIS 모의투자 공지를 확인한다.

---

## 10. 완료 체크리스트 (Completion Checklist)

본 안내서 작업이 완료되어 **Task 8 Phase 1 코드 진입이 가능**한 시점:

- [ ] §3 모의투자 계좌 발급 완료
- [ ] §4 KIS Developers 가입 + 모의투자용 앱키/앱시크릿 발급 완료
- [ ] §4.3 IP 등록 (필요한 경우) 완료
- [ ] §5 `.env.local` 작성 완료
- [ ] §6 보안 체크리스트 모두 통과
- [ ] §7 토큰 발급 curl 검증 1회 성공
- [ ] §8 회전 정책 인지

이 모든 항목이 ✅ 인 시점에 다음 단계 (Task 8 Phase 1 — Read-only 어댑터 코드 생성) 으로 진입한다.

---

## 11. 변경 이력 (Change History)

| 버전 (Version) | 일자 (Date) | 변경 내용 (Change) | 작성자 (Author) |
|---|---|---|---|
| v0.1 | 2026-05-04 | 최초 작성 (Initial draft) | JCPR / jcpr-ts-v01 |

---

## 부록 A. `[verify]` 항목 체크리스트 (Items to Verify with KIS Docs)

본 안내서에서 KIS 측 변경 가능성으로 인해 사용자 확인이 필요한 항목 모음:

- [ ] §3.1 모의투자 신청 메뉴 정확한 경로
- [ ] §3.2 모의투자 운영 시간
- [ ] §4.2 앱 등록 입력 양식의 정확한 필드
- [ ] §4.3 IP 등록 필요 여부 및 절차
- [ ] §5.3 모의투자 / 실거래 Base URL 정확한 형식
- [ ] §7.2 토큰 발급 엔드포인트 경로 및 페이로드 형식
- [ ] §7.3 응답 필드명 및 토큰 수명
- [ ] §9.1 KIS 오류 코드표

이 항목들은 Task 8 Phase 1 코드 작성 시 정확한 값으로 확정된다 (`src/brokers/_kis_endpoints.py`).

---

## 부록 B. 보안 원칙 재확인 (Security Principles Reaffirmed)

본 안내서는 `<assumption>` 의 절대 보안 원칙을 따른다:

1. 사용자의 App Key, App Secret, 계좌번호, 토큰은 **로컬 시스템에만** 존재한다.
2. 본 안내서에는 **어떠한 실제 시크릿도 포함되지 않는다** — 변수명과 개념적 경로만 안내.
3. 본 안내서 자체는 GitHub 푸시 가능하다 (시크릿 미포함).
4. 사용자가 본 안내서에 따라 작성하는 `.env.local` 은 `.gitignore` (Task 4) 에 의해 자동 차단된다.

---

*본 안내서는 jcpr-ts-v01 의 KIS OpenAPI 사용 사전 절차 문서이며, KIS 측 정책·UI 변경에 따라 갱신될 수 있다.*
