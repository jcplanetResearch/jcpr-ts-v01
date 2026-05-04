# 09. 프로덕션 등가 제한 용량 개념 (Production-Equivalent Limited-Capacity Concept)

| 항목 (Item) | 값 (Value) |
|---|---|
| 프로젝트명 (Project) | jcpr-ts-v01 |
| 문서 (Document) | docs/09_production_rollout.md |
| 버전 (Version) | v0.1 |
| 작성일 (Date) | 2026-05-04 |
| 사용자 (User) | JCPR 전용 (JCPR sole use) |
| 상태 (Status) | Draft — 승인 완료 후 초안 (Draft after approval) |
| 관련 문서 (Related) | docs/00_target_operating_model.md |

---

## 1. 문서 목적 (Document Purpose)

본 문서는 jcpr-ts-v01 시스템이 **종이 거래(paper trading)** 단계에서 **전면 프로덕션(full production)** 단계로 이행하기 전, 그 사이에 위치하는 **제한 라이브(limited live)** 단계 — 즉 **프로덕션 등가 제한 용량(production-equivalent limited-capacity)** 운영의 정의, 진입·종료 기준, 운영 프로토콜, 측정 지표, 위험 대응을 명시한다.

본 문서는 다음 산출물의 기준 문서(baseline)로 사용된다:

- `configs/capacity.yaml` (Task 5)
- `configs/live_limited.example.yaml` (Task 41)
- `scripts/run_live_limited.py` (Task 42)
- `configs/capacity_ladder.yaml` (Task 44)

---

## 2. 정의 (Definition)

**프로덕션 등가 제한 용량(Production-Equivalent Limited-Capacity)** 이란, 시스템이 **실제 브로커 계좌(real broker account)와 실제 자본(real capital)을 사용하면서도**, 자본 규모·리스크 한도·운영 범위를 **의도적으로 제한(intentionally constrained)** 하여, 프로덕션과 동일한 외부 조건(production-equivalent external conditions)에서 시스템을 검증하는 운영 단계를 의미한다.

### 2.1 핵심 속성 (Key Properties)

- **실제성 (Reality):** 종이 거래(paper trading)가 아닌 실제 체결(real fill), 실제 슬리피지(real slippage), 실제 수수료(real fee) 발생
- **제한성 (Limitation):** 손실 발생 시 사업 연속성에 미치는 영향이 통제 가능한 수준(loss-tolerable scale)
- **등가성 (Equivalence):** 코드 경로(code path), 인프라(infrastructure), 운영 절차(operational procedure)는 전면 프로덕션과 **동일 (identical)**
- **검증성 (Verifiability):** 실측 데이터(measured data) 기반으로 시스템·운영·심리적 준비도(system, operational, psychological readiness)를 평가 가능

### 2.2 본 단계가 검증하는 것 (What This Stage Verifies)

| 검증 영역 (Domain) | 검증 항목 (What is verified) |
|---|---|
| 시스템 (System) | 실제 API 응답 지연(latency), 부분 체결(partial fill), 거부(reject) 등 실측 동작 |
| 운영 (Operations) | 인간 트레이더의 모니터링·개입·비상 정지 절차 |
| 심리 (Psychology) | 실손실 발생 시 트레이더의 의사결정 안정성 |
| 보안 (Security) | 실제 자격증명(credentials) 운영 시 누출·오용 방지 |
| 정합성 (Reconciliation) | 실제 브로커 데이터와 내부 원장(ledger)의 일치 |
| 출력 (Output) | `<output>` 12개 항목의 실제 데이터 기반 산출 |

---

## 3. 단계 구분 (Stage Distinction)

| 구분 (Aspect) | 종이 거래 (Paper Trading) | 제한 라이브 (Limited Live) | 전면 프로덕션 (Full Production) |
|---|---|---|---|
| 자본 (Capital) | 가상 (virtual) | 실제, 소액 (real, small) | 실제, 정상 규모 (real, normal) |
| 브로커 계좌 (Broker account) | 페이퍼 계좌 (paper account) | 실거래 계좌 (live account) | 실거래 계좌 (live account) |
| 체결 (Fills) | 시뮬레이션 (simulated) | 실제 (real) | 실제 (real) |
| 슬리피지/수수료 (Slippage/Fees) | 모델링됨 (modeled) | 실측 (measured) | 실측 (measured) |
| 코드 경로 (Code path) | 실거래와 동일 (identical) | 실거래와 동일 (identical) | 실거래와 동일 (identical) |
| 인프라 (Infrastructure) | 동일 (identical) | 동일 (identical) | 동일 (identical) |
| 운영 절차 (Ops procedure) | 동일 (identical) | 동일 (identical) | 동일 (identical) |
| 정정·회귀 비용 (Cost of rollback) | 낮음 (low) | 중간 (medium) | 높음 (high) |
| 인간 모니터링 (Human monitoring) | 권장 (recommended) | **상주 (continuous)** | 정기 (scheduled) |
| 빌드 일정 연계 (Build linkage) | Task 32–33 | **Task 41–43** | Task 44 이후 + 사다리 |

### 3.1 등가성의 의미 (Meaning of Equivalence)

"프로덕션 등가(production-equivalent)" 라는 표현의 핵심은, **제한 라이브 단계에서 사용되는 코드와 인프라가 전면 프로덕션과 다르지 않다**는 것이다. 차이는 오직 **설정값(configuration value)** — 자본 한도, 리스크 한도, 심볼 범위, 주문 빈도 — 에서만 발생한다.

이 원칙은 다음을 보장한다:

- 제한 라이브에서 검증된 동작은 전면 프로덕션에서도 **동일하게 동작 (behaves identically)**
- "제한이 풀리면 새로운 코드가 동작하는" 식의 위험이 발생하지 않음
- 모든 설정 차이는 `configs/` 디렉토리의 YAML 파일에서만 제어

---

## 4. 제한 항목 (Limited Dimensions)

제한 라이브 단계에서는 다음 차원(dimensions)을 명시적으로 제한한다. 각 차원의 구체적 수치는 `configs/live_limited.example.yaml` 에서 정의되며, 본 문서는 그 **개념과 범주(category)** 만을 기술한다.

| 차원 (Dimension) | 제한 방식 (Limitation Method) | 설정 위치 (Config Location) |
|---|---|---|
| 자본 한도 (Capital cap) | 절대금액 또는 총 자본 대비 비율 (absolute or % of total) | `configs/capacity.yaml` |
| 포지션 한도 (Position cap) | 심볼당 최대 명목금액 (max notional per symbol) | `configs/risk_limits.yaml` |
| 일일 손실 한도 (Daily loss cap) | 절대금액 손실 시 정지 (stop on absolute loss) | `configs/risk_limits.yaml` |
| 심볼 범위 (Symbol scope) | 화이트리스트 (whitelist) | `data/reference/symbol_master.csv` |
| 주문 빈도 (Order frequency) | 단위 시간당 최대 주문 수 (max orders / unit time) | `configs/risk_limits.yaml` |
| 운영 시간 (Operating hours) | 시장 캘린더 + 추가 제한 (market calendar + window) | `src/data/calendar.py` 참조 |
| 전략 범위 (Strategy scope) | 검증 완료 전략만 활성화 (verified strategies only) | `configs/strategy_registry.yaml` |
| 단일 주문 규모 (Single-order size) | 최대 명목금액 (max notional per order) | `configs/risk_limits.yaml` |

### 4.1 제한 변경 절차 (Limitation Change Procedure)

- 제한 항목의 변경은 **자본 확장 사다리(capacity ladder, Task 44)** 의 단계 승급(stage promotion) 절차를 통해서만 가능
- 임의 상향(ad-hoc raise)은 금지
- 제한 하향(loosening downward to safer level)은 즉시 가능하며, 사후 기록(post-hoc logging)으로 충분

---

## 5. 진입 기준 (Entry Criteria)

종이 거래(Task 32–33) 단계에서 **제한 라이브(Task 41–42)** 로 승급되기 위해서는 다음 조건을 **모두** 충족해야 한다.

### 5.1 시스템 안정성 (System Stability)

| # | 항목 (Criterion) | 임계값 (Threshold, 권고) |
|---|---|---|
| 1 | 페이퍼 세션 연속 무사고 완주 | 5회 이상 (≥ 5 sessions) |
| 2 | 미회복 예외 발생 (unrecovered exception) | 0건 (zero) |
| 3 | 비상 정지 미작동 사례 (emergency stop failure) | 0건 (zero) |
| 4 | 평균 주문 지연 (avg order latency) | 사전 정의 임계값 이내 |

### 5.2 정합성 및 데이터 품질 (Reconciliation & Data Quality)

| # | 항목 (Criterion) | 임계값 (Threshold, 권고) |
|---|---|---|
| 5 | 페이퍼 단계 정합성 불일치율 | < 0.1% |
| 6 | 가격/잔고 결측(missing) | 0건 |
| 7 | 시그널 → 주문 변환 실패율 | < 0.5% |

### 5.3 리스크 게이트 (Risk Gate)

| # | 항목 (Criterion) | 임계값 (Threshold) |
|---|---|---|
| 8 | 거부 사유 모두 의도된 분류로 매핑 가능 | 100% |
| 9 | 우회(bypass) 사례 | 0건 |
| 10 | 한도 위반 후 정상 재개 검증 | 검증 완료 |

### 5.4 비상 정지 검증 (Emergency Stop Verification)

| # | 항목 (Criterion) | 임계값 |
|---|---|---|
| 11 | ESC 키 — 거래 즉시 종료 검증 | PASS |
| 12 | Ctrl-C 시그널 — 안전 종료 검증 | PASS |
| 13 | 킬 스위치 파일 — 신규 주문 차단 검증 | PASS |
| 14 | 정지 시 포지션 스냅샷 저장 | PASS |

### 5.5 출력 검증 (Output Verification)

| # | 항목 (Criterion) | 임계값 |
|---|---|---|
| 15 | `<output>` 12개 항목 자동 산출 | 100% |
| 16 | 수치 정합성 (P&L = 시작자본−종료자본 ± 일치) | PASS |

### 5.6 인간 준비도 (Human Readiness)

| # | 항목 (Criterion) | 확인 방식 |
|---|---|---|
| 17 | 트레이더 운영 매뉴얼 숙지 | 자가 확인 + 모의 점검 |
| 18 | 비상 절차 숙지 (emergency procedure) | 모의 비상 정지 훈련 완료 |
| 19 | 모니터링 가용 시간 확보 | 라이브 세션 동안 상주 가능 |

### 5.7 보안 검증 (Security Verification)

| # | 항목 (Criterion) | 임계값 |
|---|---|---|
| 20 | 실거래 자격증명(real credentials) 로컬 전용 보관 | PASS |
| 21 | `.env` 파일이 GitHub에 푸시되지 않음 | PASS (gitignore 검증) |
| 22 | 로그에 시크릿 평문 노출 없음 | PASS (자동 스캔) |

---

## 6. 운영 프로토콜 (Operating Protocol)

제한 라이브 세션의 표준 운영 절차(standard operating procedure).

### 6.1 일일 사이클 (Daily Cycle)

1. **세션 전 (Pre-session):**
   - 킬 스위치 파일 부재 확인 (verify no kill switch)
   - 자격증명 유효성 점검 (credential validity check)
   - `configs/live_limited.example.yaml` 로드 및 검증
   - 브로커 연결 시험 (broker connection test)
   - 시장 캘린더 확인 (market calendar check)
2. **세션 중 (In-session):**
   - 트레이더 상주 모니터링
   - 실시간 P&L 및 한도 사용률 관찰
   - 예외 발생 시 즉시 비상 정지 가능
3. **세션 후 (Post-session):**
   - `<output>` 12개 항목 자동 산출
   - 정합성 점검 (reconciliation check)
   - 예외·거부 사유 검토 (exception & rejection review)
   - 차세션 자본 권고(next-session capacity recommendation) 검토

### 6.2 모니터링 지표 (Monitoring Indicators)

세션 중 트레이더가 상시 관찰하는 지표:

- 현재 포지션 (current positions)
- 실시간 P&L (real-time P&L)
- 리스크 한도 사용률 (risk limit usage %)
- 거부 주문 카운터 (rejection counter)
- 브로커 연결 상태 (broker connection status)
- 예외 큐 (exception queue)

### 6.3 보고 (Reporting)

- 세션 종료 직후: 자동 일일 보고서 (Task 49 — daily report generator)
- 세션 검토 회의: 1–2주 단위 (Task 43 — post-session review)

---

## 7. 종료 / 회귀 기준 (Exit & Rollback Criteria)

제한 라이브 단계에서 **다음 사유 중 하나라도 발생 시 즉시 정지(immediate stop)** 하고, 페이퍼 단계로 회귀(rollback to paper)하거나 코드 수정 후 재진입(re-entry)한다.

### 7.1 즉시 정지 트리거 (Immediate Stop Triggers)

| 사유 (Trigger) | 대응 (Response) | 회귀 여부 (Rollback) |
|---|---|---|
| 정합성 불일치 발생 (reconciliation mismatch) | 즉시 정지 → 원인 분석 | 페이퍼 회귀 |
| 미허용 주문 발주 (unauthorized order) | 즉시 정지 → 보안 감사 | 코드 수정 후 재진입 |
| 비상 정지 미작동 (emergency stop failure) | 즉시 정지 → 정지 메커니즘 재검증 | 페이퍼 회귀 |
| 자본 한도 초과 (capacity breach) | 즉시 정지 → 사이징 로직 검토 | 코드 수정 후 재진입 |
| 리스크 한도 초과 (risk limit breach) | 즉시 정지 → 한도 조정 또는 회귀 | 상황별 결정 |
| 일일 손실 한도 도달 (daily loss cap hit) | 자동 정지 (정상 동작) | 익일 재개 가능 |
| 비상 시장 상황 (abnormal market) | 정지 → 시장 안정 후 재개 검토 | 시장 회복 후 결정 |
| 브로커 연결 단절 + 재연결 실패 | 정지 → 인프라 점검 | 인프라 복구 후 재진입 |
| 보안 사고 (security incident) | **즉시 정지 + 자격증명 회전** | 사고 원인 제거 후 재진입 |

### 7.2 회귀 절차 (Rollback Procedure)

1. 정지 사유 기록 (record stop reason)
2. 포지션 청산 또는 안전 보유 결정 (close or hold)
3. 모든 자격증명 회전 검토 (review credential rotation, 보안 사고 시 필수)
4. 원인 분석 및 수정 사항 정의
5. 페이퍼 단계로 회귀 또는 코드 수정
6. 재진입 시 §5의 진입 기준 전체 재검증

---

## 8. 자본 확장 사다리 연계 (Capacity Ladder Linkage)

제한 라이브 단계는 **자본 확장 사다리(capacity ladder, Task 44)** 의 가장 낮은 단(lowest rung)에 위치한다.

### 8.1 사다리 개념 (Ladder Concept)

```
[페이퍼 거래]
   ↓ (진입 기준 §5 충족)
[제한 라이브 — 1단계]   ← 본 문서의 범위
   ↓ (안정성·성과 기준 충족)
[제한 라이브 — 2단계]   ← 자본 단계적 확대
   ↓
   ...
   ↓
[전면 프로덕션]
```

### 8.2 단계 승급 기준 (Stage Promotion Criteria)

각 단계 사이의 승급 기준은 `configs/capacity_ladder.yaml` (Task 44)에서 구체적으로 정의되며, 본 문서는 그 **공통 골격(common skeleton)** 만을 기술한다:

- 최소 운영 기간 (minimum tenure at current stage)
- 정합성 무사고 (zero reconciliation issue)
- 리스크 한도 사용 분포 (risk limit usage distribution)
- 예외·거부 추세 (exception & rejection trend)
- 인간 운영자 신뢰도 (human operator confidence)

### 8.3 단계 강등 기준 (Stage Demotion Criteria)

다음 발생 시 자동으로 한 단계 하향:

- 정합성 불일치 발생
- 비상 정지 미작동
- 자본·리스크 한도 초과
- 보안 사고

---

## 9. 보안 및 운영 원칙 (Security & Operations Principles)

제한 라이브 단계는 **실제 자격증명(real credentials)** 을 사용하므로, 페이퍼 단계 대비 보안 요구가 강화된다.

### 9.1 자격증명 관리 (Credential Management)

- 실거래 API 키(live API key)는 `.env` 또는 OS 시크릿 저장소(OS secret store)에만 보관
- 코드 저장소·로그·채팅·외부 시스템에 절대 미반출
- 키 회전(key rotation) 주기 정의 (예: 90일)
- 보안 사고(security incident) 발생 시 **즉시 회전(immediate rotation)**

### 9.2 권한 최소화 (Least Privilege)

- API 키는 필요 최소 권한만 부여
- 출금(withdrawal) 권한은 가능한 경우 분리 또는 차단
- 읽기 전용 작업은 별도의 읽기 전용 토큰 사용

### 9.3 로그 및 감사 (Logging & Audit)

- 모든 주문·체결·거부·정지가 감사 로그(audit log)에 기록
- 감사 로그는 **로컬 관리자 명시적 승인 없이 삭제 불가** (target operating model §7.3 준수)
- 시크릿 평문이 로그에 기록되지 않도록 자동 마스킹

### 9.4 인간 개입 원칙 (Human Intervention)

- 제한 라이브 단계에서는 **인간 트레이더가 상주(continuous human presence)** 한다
- 무인 운영(unattended operation)은 본 단계에서 허용되지 않음

---

## 10. 측정 지표 (Measurement Metrics)

제한 라이브 단계에서 수집·평가하는 지표는 `<output>` 의 12개 항목과 직접 연결된다.

### 10.1 출력 12항목과의 매핑 (Mapping to 12 Output Items)

| # | 출력 항목 (Output Item) | 제한 라이브 단계의 의미 (Meaning at Limited Live) |
|---|---|---|
| 1 | 시작 자본 (Starting capital) | 세션 개시 시 실제 잔고 |
| 2 | 종료 자본 (Ending capital) | 세션 종료 시 실제 잔고 |
| 3 | 실현 손익 (Realized P&L) | 실제 체결 기반 |
| 4 | 미실현 손익 (Unrealized P&L) | 실제 보유 포지션 평가 |
| 5 | 수수료 / 슬리피지 (Fees/Slippage) | **실측치 (measured) — 본 단계의 핵심 검증 지표** |
| 6 | 전략별 기여도 (Strategy attribution) | 활성 전략별 분리 |
| 7 | 심볼별 기여도 (Symbol attribution) | 화이트리스트 심볼별 분리 |
| 8 | 거부된 주문 (Rejected orders) | 사전 리스크 게이트 거부 — 의도성 확인 |
| 9 | 리스크 한도 사용률 (Risk limit usage) | 한도별 % — 운영 한도 적정성 평가 |
| 10 | 정합성 상태 (Reconciliation status) | **본 단계의 핵심 검증 지표** |
| 11 | 예외 (Exceptions) | 모든 예외는 회귀 후보 사유 |
| 12 | 차세션 자본 권고 (Next-session capacity) | 사다리 단계 승급 입력 데이터 |

### 10.2 본 단계 고유 지표 (Stage-Specific Indicators)

위 12항목 외에, 제한 라이브 단계에서 추가로 추적하는 지표:

- 모델 슬리피지 vs 실측 슬리피지 차이 (model vs measured slippage gap)
- 부분 체결 비율 (partial fill ratio)
- 주문 거부 분포 (rejection distribution by reason)
- 평균/최악 응답 지연 (avg/worst latency)

---

## 11. 위험 시나리오 및 대응 (Risk Scenarios & Responses)

| 시나리오 (Scenario) | 발생 가능성 | 영향 (Impact) | 대응 (Response) |
|---|---|---|---|
| 브로커 API 장애 | 중간 | 거래 불가 | 정지 → 재연결 → 실패 시 페이퍼 회귀 |
| 자격증명 누출 의심 | 낮음 | 치명적 (critical) | 즉시 키 회전 + 세션 정지 + 사고 분석 |
| 정합성 불일치 | 낮음 | 높음 | 정지 → 원인 분석 → 페이퍼 회귀 |
| 시그널 폭주 (signal storm) | 중간 | 높음 | 주문 빈도 한도 자동 작동 → 정지 검토 |
| 비상 시장 (flash crash 등) | 낮음 | 매우 높음 | 일일 손실 한도 자동 정지 + 인간 판단 |
| 트레이더 부재 (operator absence) | 중간 | 높음 | 세션 시작 차단 또는 사전 정의된 보수 모드 진입 |
| 일일 손실 한도 도달 | 중간 | 통제됨 | 자동 정지 (정상 동작) → 익일 재개 검토 |
| 코드 회귀(regression) | 낮음 | 높음 | 페이퍼 회귀 → 수정 → 재진입 시 §5 재검증 |

---

## 12. 변경 이력 (Change History)

| 버전 (Version) | 일자 (Date) | 변경 내용 (Change) | 작성자 (Author) |
|---|---|---|---|
| v0.1 | 2026-05-04 | 최초 작성 (Initial draft) | JCPR / jcpr-ts-v01 |

---

## 부록 A. 관련 문서 및 산출물 (Related Documents & Artifacts)

- `docs/00_target_operating_model.md` — 목표 운영 모델 (Task 1)
- `configs/capacity.yaml` — 자본 한도 (Task 5)
- `configs/risk_limits.yaml` — 리스크 한도 (Task 6)
- `configs/live_limited.example.yaml` — 제한 라이브 설정 템플릿 (Task 41)
- `scripts/run_live_limited.py` — 제한 라이브 실행 스크립트 (Task 42)
- `reports/session_review.py` — 세션 검토 보고 (Task 43)
- `configs/capacity_ladder.yaml` — 자본 확장 사다리 (Task 44)

## 부록 B. 용어 보충 (Term Supplement)

| 한국어 (Korean) | 영문 (English) | 정의 (Definition) |
|---|---|---|
| 등가성 | Equivalence | 코드·인프라·운영 절차가 다른 단계와 동일함 |
| 사다리 | Ladder | 자본·권한을 단계적으로 확대하는 구조 |
| 회귀 | Rollback | 한 단계 아래(또는 페이퍼)로 되돌아가는 동작 |
| 강등 | Demotion | 사다리에서 한 단계 하향 |
| 승급 | Promotion | 사다리에서 한 단계 상향 |
| 무인 운영 | Unattended operation | 인간 트레이더 부재 상태에서의 운영 |

---

*본 문서는 jcpr-ts-v01 의 운영 단계(operating stage) 정의 문서이며, `<model>`, `<requirements>`, `<output>` 의 변경에 따라 갱신된다.*
