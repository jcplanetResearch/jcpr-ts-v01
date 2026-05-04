# 00. 목표 운영 모델 (Target Operating Model)

| 항목 (Item) | 값 (Value) |
|---|---|
| 프로젝트명 (Project) | jcpr-ts-v01 |
| 문서 (Document) | docs/00_target_operating_model.md |
| 버전 (Version) | v0.1 |
| 작성일 (Date) | 2026-05-04 |
| 사용자 (User) | JCPR 전용 (JCPR sole use) |
| 상태 (Status) | Draft — 승인 대기 (Pending approval) |

---

## 1. 문서 목적 (Document Purpose)

본 문서는 **jcpr-ts-v01** 트레이딩 솔루션(trading solution)의 목표 운영 모델(target operating model)을 정의한다. 시스템의 경계(boundaries), 데이터 흐름(data flow), 권한 모델(authority model), 비상 정지(emergency stop), 보안 원칙(security principles), 최종 출력 요구사항(final output requirements)을 명시하며, 이후 모든 빌드 단계(build stage)의 기준 문서로 사용된다.

본 문서가 변경될 경우, `<assumption>`에 따라 코드(code)는 이전 버전과의 비교 요약(comparison summary)을 포함하여 재생성된다.

---

## 2. 시스템 식별자 (System Identifier)

- **프로젝트명 (Project name):** jcpr-ts-v01
- **사용 주체 (Sole user):** JCPR
- **버전 (Version):** v0.1
- **저장소 (Repository):** GitHub (공개 가능 영역만 — public-safe content only)
- **운영 환경 (Operating environment):** 로컬 시스템 (Local system) 중심, 브로커 API(Brokerage API)와 통신하는 단일 사용자 시스템

---

## 3. 운영 모델 개요 (Operating Model Overview)

본 시스템의 거래(trade) 한 사이클(cycle)은 다음과 같이 흐른다:

1. **트레이더(trader)가 로컬 시스템(local system)에서 세션 개시 (session start)**
2. 로컬에서 **트레이딩 시그널 로직(signal logic)** 실행 → 시그널 생성 (signal generation)
3. 시그널을 **주문 의도(order intent)** 로 변환 → 사이징(sizing) → 사전 리스크 게이트(pre-trade risk gate) 통과
4. 통과된 주문은 **브로커리지 API(Brokerage API)** 로 라우팅 (routing)
5. 브로커리지로부터 **체결 결과(execution results), 잔고(cash balance), 상태(status)** 수신
6. 결과는 로컬에서 **검증(verify) 후 DB**에 기록 (record)
7. 세션 종료 시 **최종 출력(final output)** 자동 산출 — 본 문서 §8 참조

비상 정지(emergency stop)는 위 흐름의 어느 시점에서도 발동 가능하며, **신규 주문보다 우선한다 (prevails over new orders).**

---

## 4. 시스템 경계 및 책임 (System Boundaries & Responsibilities)

본 시스템은 세 개의 분리된 영역(three separated zones)으로 구성된다. 각 영역 간 데이터 흐름은 명시적으로 통제(explicitly controlled)되며, 신뢰 영역(trusted zone)을 벗어나는 비밀정보(secret)는 존재하지 않는다.

### 4.1 로컬 시스템 (Local System) — 신뢰 영역 (Trusted Zone)

- 주문 발주(order placement)의 **유일한 시작점 (sole entry point)**
- 개인키 및 비밀번호(private keys & passwords) 보관 — **공개 영역 반출 절대 금지 (never exported to public area)**
- 트레이딩 시그널 로직(signal logic) 생성 및 실행
- 사용자 인터페이스(user interface) 실행
- **비상 정지 입력장치(emergency stop input device)** 위치 — ESC, Ctrl-C
- 모든 결정 로그(decision log)의 마스터(master) 보관소

### 4.2 브로커리지 API (Brokerage API) — 외부 게이트웨이 (External Gateway)

- 주문 라우팅(order routing) 채널
- **체결 결과(execution results), 현금 잔고(cash balance), 거래 상태(trading status)** 수신
- 인증(authentication)은 로컬 시스템에서 발급·관리된 자격증명(credentials)으로만 수행
- 브로커 응답(broker response)은 로컬에서 **검증(verify) 후 DB에 기록**
- 브로커리지 자체는 **장기 보관소(long-term store)가 아니다** — 단순 통신 채널

### 4.3 DB 인터페이스 (DB Interface) — 보존 영역 (Persistence Zone)

- 트레이딩 출력(trading output) 저장 전용
- **로컬 관리자(local administrator)의 명시적 확인(explicit confirmation) 없이 삭제 불가 (no delete without firm confirmation)**
- 감사 로그(audit log)는 append-only 원칙 준수
- 백업(backup)은 로컬 관리자가 통제하는 영역 내에서만 수행

### 4.4 영역 간 데이터 흐름 요약 (Inter-Zone Data Flow Summary)

| From → To | 흐르는 데이터 (Data) | 흐르지 않는 데이터 (Forbidden) |
|---|---|---|
| Local → Brokerage | 주문 의도(order intent), 인증 토큰(auth token) | 시그널 로직 원본(raw signal logic), 개인키(private key) 본체 |
| Brokerage → Local | 체결(fill), 잔고(balance), 상태(status) | — |
| Local → DB | 검증된 결과(verified result), 감사 로그(audit log) | 평문 비밀번호(plaintext password), 평문 API 키(plaintext API key) |
| DB → Local | 조회 결과(query result) | — |
| Brokerage ↔ DB | **직접 연결 없음 (no direct connection)** | 모든 데이터 (all data) |

---

## 5. 자율 트레이딩 권한 모델 (Autonomous Trading Authority Model)

본 시스템은 `<model>`에 따라 **부여된 권한(granted permission) 범위 내에서 자율 트레이딩(autonomous trading)** 을 수행한다.

### 5.1 권한 부여 범위 (Granted Permission Scope)

- **자본 한도 (Capital limit):** `configs/capacity.yaml` 에 정의된 금액 이내
- **리스크 한도 (Risk limit):** `configs/risk_limits.yaml` 에 정의된 한도 이내
- **상품 범위 (Instrument scope):** `data/reference/symbol_master.csv` 에 등록된 심볼만
- **시간 범위 (Time scope):** 시장 캘린더(market calendar)상 거래 가능 시간만
- **세션 범위 (Session scope):** 트레이더가 명시적으로 시작한 세션(session) 동안만

### 5.2 권한 회수 트리거 (Revocation Triggers)

다음 사유 발생 시 자율 권한은 **즉시 회수(immediately revoked)** 된다:

1. ESC 키 또는 Ctrl-C 입력 (emergency stop input)
2. 킬 스위치 파일(kill switch file) 존재 — `runtime/KILL_SWITCH_ON`
3. 리스크 한도 초과 (risk limit breach)
4. 브로커 연결 단절(broker disconnection) 후 재연결 실패
5. 사전 리스크 게이트(pre-trade risk gate) 연속 거부 임계 초과
6. 자본 한도 초과 (capacity breach)

### 5.3 인간 승인 루프 (Human-in-the-Loop)

- 신규 전략(new strategy) 도입, 한도 상향(limit increase), 비정상 시장(abnormal market) 대응 등은 **반드시 인간 승인(human approval) 후 적용**
- 승인 워크플로우(approval workflow)는 Task 40에서 구현

---

## 6. 비상 정지 메커니즘 (Emergency Stop Mechanism)

본 시스템에는 비상 정지 입력(emergency stop inputs)이 다중화(redundant)되어 있으며, **정지 신호는 항상 신규 주문(new order)보다 우선(prevails)한다.**

### 6.1 정지 입력 (Stop Inputs)

| 입력 (Input) | 동작 (Action) | 우선순위 (Priority) | 구현 위치 (Module) |
|---|---|---|---|
| ESC 키 (ESC key) | 진행 중 거래 즉시 종료 (immediate termination) | 최우선 (Highest) | `src/risk/keyboard_stop.py` (Task 30) |
| Ctrl-C 시그널 (Ctrl-C signal) | 프로세스 종료 + 안전한 cleanup | 최우선 (Highest) | `src/risk/shutdown.py` (Task 29) |
| 킬 스위치 파일 (Kill switch file) | 신규 주문 영구 차단 — 파일 제거 시까지 | 최우선 (Highest) | `src/risk/kill_switch.py` (Task 31) |

### 6.2 정지 시 동작 순서 (Stop Sequence)

1. **신규 주문 차단 (block new orders)** — 가장 먼저 실행
2. 진행 중 주문 취소 시도 (attempt cancel of in-flight orders)
3. 포지션 상태 스냅샷 (snapshot positions to DB)
4. 브로커 연결 종료 (close broker connection gracefully)
5. 로그 flush 및 프로세스 종료 (flush logs and exit)

### 6.3 정지 우선 원칙 (Stop-First Principle)

`<model>` 에 명시된 바와 같이, **"ESC 또는 Ctrl-C는 신규 주문 발주에 우선한다 (ESC or Ctrl-C prevails before new trading is ordered)"**. 이 원칙은 다음과 같이 구현된다:

- 주문 발주 함수(order placement function)는 매 호출 시점에 정지 플래그(stop flag)를 검사한다.
- 정지 플래그가 설정된 이후에는 발주 함수가 즉시 반환(return immediately)하며, 어떠한 신규 주문도 브로커로 전송되지 않는다.
- 정지 플래그는 한 번 설정되면 **세션 종료 시까지 해제되지 않는다 (cannot be cleared within the session)**.

---

## 7. 보안 원칙 (Security Principles)

본 시스템은 다음 보안 원칙을 절대 원칙(absolute principles)으로 준수한다. `<assumption>` 에 따라, 이 원칙을 위반하는 어떠한 변경(change)도 허용되지 않는다.

### 7.1 개인키 로컬 전용 원칙 (Private Key Local-Only Principle)

모든 개인키(private key), API 시크릿(API secret), 비밀번호(password)는 **로컬 시스템에만 존재**하며, 다음 영역으로 어떠한 형태로도 업로드되지 않는다:

- GitHub 저장소 (repository) — public 또는 private 무관
- 클라우드 폴더 (cloud folder)
- 컨테이너 환경 (container environment)
- 채팅/메신저 (chat / messenger)
- 로그 파일 중 외부 반출되는 것 (any externally-shared log)

### 7.2 시크릿 파일 분리 원칙 (Secret File Separation)

- 자격증명은 `.env` 파일 또는 별도 시크릿 저장소(secret store)에만 보관
- 코드 저장소(code repository)에는 `.env.example` 템플릿만 존재
- `.gitignore` 에 `.env`, `secrets/`, `runtime/` 등이 명시적으로 등록

### 7.3 DB 무단 삭제 금지 원칙 (No Unauthorized DB Deletion)

- 트레이딩 출력 데이터(trading output)는 **로컬 관리자(local administrator)의 명시적 승인(explicit confirmation) 없이 삭제될 수 없다**
- 자동 정리 작업(automated cleanup)이라 할지라도 사전 승인된 보존 정책(retention policy)에 따라서만 동작
- 모든 삭제(delete) 동작은 감사 로그(audit log)에 기록

### 7.4 변경 시 보안 검증 원칙 (Change-Time Security Review)

`<model>`, `<requirements>`, `<output>` 이 재정의될 때:

1. 보안 영향 분석(security impact analysis)을 먼저 수행
2. 본 §7의 원칙을 위반하는 변경은 **거부 (rejected)**
3. 위반하지 않는 경우에만 코드 재생성(code regeneration) 진행
4. 재생성 결과에는 **이전 버전 대비 비교 요약(comparison summary vs previous version)** 포함

### 7.5 최소 권한 원칙 (Least Privilege)

- 브로커 API 토큰(API token)은 거래에 필요한 최소 권한(minimum scope)만 부여
- 읽기 전용 작업(read-only operation)에는 읽기 전용 토큰(read-only token) 사용
- MCP 서버(MCP server)는 읽기 전용(readonly)과 제한 권한(restricted) 두 종류로 분리 (Task 34, 35)

---

## 8. 최종 시스템 출력 요구사항 (Final System Output Requirements)

`<output>` 에 따라, 시스템은 세션 종료 시 다음 12개 항목을 **자동으로 산출(automatically produce)** 한다:

| # | 항목 (Item) | 단위 / 형식 (Unit / Format) | 출처 모듈 (Source Module) |
|---|---|---|---|
| 1 | 시작 자본 (Starting capital) | 통화 금액 (currency amount) | 세션 개시 시 잔고 (session-start balance) |
| 2 | 종료 자본 (Ending capital) | 통화 금액 | 세션 종료 시 잔고 (session-end balance) |
| 3 | 실현 손익 (Realized P&L) | 통화 금액 | `src/pnl/pnl_engine.py` |
| 4 | 미실현 손익 (Unrealized P&L) | 통화 금액 | `src/pnl/pnl_engine.py` |
| 5 | 수수료 / 슬리피지 (Fees / slippage) | 통화 금액 | `src/pnl/slippage.py` |
| 6 | 전략별 기여도 (Strategy attribution) | 전략명 → 손익 (strategy → P&L) | P&L engine + strategy registry |
| 7 | 심볼별 기여도 (Symbol attribution) | 심볼 → 손익 (symbol → P&L) | P&L engine + position ledger |
| 8 | 거부된 주문 (Rejected orders) | 주문 목록 + 사유 (orders + reasons) | `src/risk/reports.py` |
| 9 | 리스크 한도 사용률 (Risk-limit usage) | 한도별 사용률 (% per limit) | `src/risk/risk_gate.py` |
| 10 | 정합성 상태 (Reconciliation status) | OK / 불일치 + 차이 (mismatch + delta) | `src/pnl/reconciliation.py` |
| 11 | 예외 (Exceptions) | 예외 목록 + 컨텍스트 (list + context) | 감사 로그 (audit log) |
| 12 | 차세션 자본 권고 (Next-session capacity recommendation) | 권고 자본 + 근거 (suggested capacity + rationale) | `configs/capacity_ladder.yaml` + 성과 분석 |

### 8.1 출력 산출 트리거 (Output Generation Trigger)

다음 시점에 자동 산출:

- 정상 세션 종료 (normal session end)
- 비상 정지 후 안전 종료 (post-emergency-stop safe shutdown)
- 일일 보고 스케줄 (daily report schedule, Task 49)

### 8.2 출력 보관 (Output Storage)

- 형식: PDF/HTML/JSON 병행
- 위치: 로컬 `reports/` 디렉토리 + DB 보존 영역
- 외부 공유 시 시크릿(secret) 자동 마스킹

---

## 9. 가정사항 및 변경 관리 (Assumptions & Change Management)

### 9.1 핵심 가정 (Core Assumptions)

본 문서와 시스템은 다음 가정을 따른다:

1. **재생성 가정 (Regeneration assumption):** `<model>`, `<requirements>`, `<output>` 이 재정의되면 코드는 재생성되며, 이전 버전 대비 비교 요약이 함께 제공된다.
2. **보안 불가침 가정 (Security inviolability assumption):** 보안 또는 개인키 정보 유출을 야기하는 변경은 어떠한 경우에도 허용되지 않는다.
3. **승인 게이트 가정 (Approval gate assumption):** 코드 파일 생성(code file generation)은 승인 후에만 진행된다.
4. **언어 가정 (Language assumption):** 모든 응답은 한국어를 기본으로 하며 핵심 용어는 한국어(English) 형식으로 병기된다.

### 9.2 가정 변경 절차 (Assumption Change Procedure)

1. 변경 제안(change proposal) 작성
2. 보안 영향 분석(security impact analysis)
3. 운영 모델 영향 분석(operating model impact analysis)
4. 승인(approval)
5. 본 문서 §11(변경 이력) 갱신
6. 영향받는 코드 재생성

---

## 10. 승인 게이트 (Approval Gate)

`<requirement>` 에 따라:

- **코드 파일 생성(code file generation)은 항상 사용자 승인 이후에만 진행된다.**
- 각 빌드 작업(build task)은 다음 순서로 처리된다:
  1. 작업 범위 제시 (scope presentation)
  2. 산출물 미리보기 (deliverable preview)
  3. **승인 요청 (approval request)**
  4. 승인 시 파일 생성 (file creation upon approval)
  5. 산출물 전달 (deliverable handoff)
- 승인 없이 생성된 파일은 존재할 수 없다.

---

## 11. 용어집 (Glossary)

| 한국어 (Korean) | 영문 (English) | 정의 (Definition) |
|---|---|---|
| 트레이딩 솔루션 | Trading solution | 거래 의사결정·실행·기록을 자동화하는 시스템 |
| 시그널 | Signal | 전략 로직이 산출하는 매수/매도/보유 신호 |
| 주문 의도 | Order intent | 시그널을 실제 주문으로 변환하기 전 단계의 구조화된 의도 |
| 사전 리스크 게이트 | Pre-trade risk gate | 주문 발주 전 한도/잔고/상태를 검사하는 모듈 |
| 사이징 | Sizing | 주문 수량을 자본·리스크·캡 정책에 맞춰 결정 |
| 체결 | Fill | 브로커가 주문을 체결한 결과 |
| 정합성 | Reconciliation | 내부 원장과 브로커 스냅샷의 일치 여부 검증 |
| 비상 정지 | Emergency stop | ESC, Ctrl-C, 킬 스위치로 거래를 즉시 중단 |
| 킬 스위치 | Kill switch | 신규 주문을 영구 차단하는 파일 기반 차단 장치 |
| 자본 한도 | Capacity | 운용 가능한 자본의 상한 |
| 슬리피지 | Slippage | 도달 가격(arrival price) 대비 체결 가격 차이 |
| 정합성 상태 | Reconciliation status | 내부 원장과 외부 데이터의 일치 여부 |

---

## 12. 변경 이력 (Change History)

| 버전 (Version) | 일자 (Date) | 변경 내용 (Change) | 작성자 (Author) |
|---|---|---|---|
| v0.1 | 2026-05-04 | 최초 작성 (Initial draft) | JCPR / jcpr-ts-v01 |

---

## 부록 A. 관련 문서 (Related Documents)

- `docs/09_production_rollout.md` — 프로덕션 등가 제한 용량 개념 (Task 2)
- `configs/capacity.yaml` — 자본 한도 (Task 5)
- `configs/risk_limits.yaml` — 리스크 한도 (Task 6)
- `configs/strategy_registry.yaml` — 전략 레지스트리 (Task 45)
- `configs/capacity_ladder.yaml` — 자본 확장 사다리 (Task 44)
- `docs/final_operating_manual.md` — 최종 운영 매뉴얼 (Task 50)

---

*본 문서는 jcpr-ts-v01 의 기준 문서(baseline document)이며, `<model>`, `<requirements>`, `<output>` 의 변경에 따라 갱신된다.*
