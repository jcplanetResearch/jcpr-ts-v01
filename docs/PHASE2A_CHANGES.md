# Phase 2-A Changes — JCPR-ts-v01

**날짜(Date)**: 2026-05-08
**범위(Scope)**: ExecutionGateway + 통합 ApprovalStore 결합, MCP/Gateway 공유 config 통합
**전제(Prerequisite)**: Phase 1 완료 — `src/execution/approval_store.py` 1016줄, 50/50 테스트 통과, 운영자 로컬 적용 및 GitHub 푸시 완료

---

## 1. 한눈 비교 (Phase 1 → Phase 2-A summary diff)

| 영역(Area) | Phase 1 (이전) | Phase 2-A (현재) |
|---|---|---|
| ApprovalStore 개수 | 2개 분리 (`_approval_store.py` MCP용 + `_approval_state.py` Exec용) | **1개 통합** (`approval_store.py`) |
| 환경변수 | `JCPR_APPROVAL_DB_MCP`, `JCPR_APPROVAL_DB_EXEC` | **`JCPR_APPROVAL_DB` 단일** |
| SQLite 파일 | `data/approvals_mcp.sqlite`, `data/approvals_exec.sqlite` | **`data/approvals.sqlite` 단일** |
| ExecutionGateway 의존성 | `_approval_state.py` 직접 import | **DI로 ApprovalStore 주입** |
| Live 모드 가드 | gateway 단독 검증 | **3중 가드** — `--prod` + `JCPR_ALLOW_LIVE=1` + record.mode='live' |
| Kill switch | gateway 내부 변수 | **Protocol 주입** (NoOp 기본값) |
| 모드 검증 | record.mode 검증 누락 | **paper 게이트웨이는 live 레코드 거부** (`ModeViolationError`) |
| 어댑터 mode 검증 | 없음 | **`broker.mode` 속성 일치 검증** in `__init__` |
| 폐기 환경변수 처리 | 해당 없음 | **자동 검출 + stderr 경고** |
| 테스트 수 | 50개 (store만) | **43개 추가** (gateway 25 + config 18) → 누적 93개 |

---

## 2. 신규/수정/삭제 파일 (File-by-file)

### 신규 (NEW)
- `tests/execution/test_execution_gateway.py` — 25 tests, 9 카테고리
- `tests/mcp_servers/test_config.py` — 18 tests, 5 카테고리
- `docs/PHASE2A_CHANGES.md` — 본 문서
- `task_phase2a_sync_to_local.sh`, `task_phase2a_push_to_github.sh`

### 수정 (MODIFIED)
- `src/execution/execution_gateway.py` — **재작성**
  - 통합 ApprovalStore를 DI로 받음
  - 상태 전이 강제 + 9종 예외 클래스
  - kill switch / mode / live 이중가드 통합
- `src/execution/__init__.py` — 통합 export 갱신
- `src/mcp_servers/_config.py` — 단일 환경변수 정책 + `ServerConfig` 추가

### 삭제 예정 (DELETION TARGETED, by operator)
- `src/execution/_approval_state.py` — 통합 store로 대체
- 운영자가 Phase 2-B 진입 전 `git rm`으로 삭제 (`task_phase2a_sync_to_local.sh`에 자동화됨)

---

## 3. 핵심 설계 결정 (Key design decisions)

### 3.1 ExecutionGateway는 무상태(stateless)
모든 상태는 ApprovalStore SQLite에 있음. 게이트웨이는 인-메모리 캐시 또는 멤버 변수로 진행 상태를 보관하지 않음 — 재시작/멀티 워커 안전성 확보.

### 3.2 상태 전이 매트릭스 (state transition matrix)

| 시작 상태 | execute() 결과 |
|---|---|
| PROPOSED | `NotApprovedError` |
| APPROVED | EXECUTING → EXECUTED 또는 EXEC_FAILED |
| EXECUTING | `AlreadyExecutingError` |
| EXECUTED | `AlreadyExecutedError` |
| EXEC_FAILED | `AlreadyExecutedError` (terminal로 간주) |
| REJECTED | `NotApprovedError` |
| EXPIRED | `ExpiredApprovalError` |
| CANCELLED | `NotApprovedError` |

### 3.3 Live 모드 3중 가드
```
gateway.execute() 진입 조건:
  config.mode == 'live'
  AND config.allow_live == True       (env: JCPR_ALLOW_LIVE=1)
  AND record.mode == 'live'           (propose 시 명시)
  AND CLI flag --prod                 (config 로딩 시 검증)
```
하나라도 빠지면 `ModeViolationError` 또는 paper로 강제 다운그레이드.

### 3.4 Kill switch는 모든 검사보다 우선
신규 거래 직전 가장 먼저 확인. record가 존재하지 않아도 `KillSwitchActiveError`가 우선 발생 — `<model>` 태그 요구사항(ESC/Ctrl-C는 신규 거래보다 우선) 준수.

### 3.5 action_kind 분리
- `submit_order`, `cancel_order` → 게이트웨이 처리
- `set_capacity`, `kill_switch` → restricted_server에서 직접 처리 (Phase 2-B에서 정의). 게이트웨이로 흘러오면 명시적 오류.

---

## 4. 보안 검증 (Security verification)

`<assumption>` 태그 보안 요구사항 대조:

| 항목 | Phase 2-A 상태 |
|---|---|
| 시크릿 노출 차단 | ✅ 정적 grep `password\|api_key\|secret\|token`: 0건 |
| ApprovalStore 0600 | ✅ Phase 1 store 책임 (재확인 완료) |
| TLS 1.2+ | ✅ KIS 어댑터 책임 (게이트웨이 비관여) |
| Self-approval 차단 | ✅ Phase 1 store 단일 검증 지점 |
| Live 모드 이중 가드 | ✅ 3중 가드 강화 |
| ESC/Ctrl-C 즉시 종료 | ✅ KillSwitch Protocol + KIS adapter signal |
| DB 운영자 확인 후 삭제 | ✅ 본 단계에선 신규 DB 미생성 — Phase 1 store 재사용 |
| 시크릿 평문 storage | ✅ 게이트웨이는 시크릿 미접근 (어댑터 위임) |

---

## 5. 테스트 결과 (Test results)

```
$ pytest tests/ -v
============================== 43 passed in 0.58s ==============================
```

### 5.1 카테고리별 분포

| 카테고리 | 파일 | 테스트 수 |
|---|---|---|
| Happy path | test_execution_gateway.py | 3 |
| State transitions | test_execution_gateway.py | 5 |
| Kill switch | test_execution_gateway.py | 2 |
| Mode guards | test_execution_gateway.py | 6 |
| Broker failures | test_execution_gateway.py | 2 |
| Invalid action_kind | test_execution_gateway.py | 2 |
| Diagnostics | test_execution_gateway.py | 3 |
| Concurrency | test_execution_gateway.py | 1 |
| TTL | test_execution_gateway.py | 1 |
| Defaults | test_config.py | 2 |
| Env overrides | test_config.py | 2 |
| Live mode guards | test_config.py | 6 |
| Deprecated env detection | test_config.py | 4 |
| ServerConfig invariants | test_config.py | 4 |
| **합계** | | **43** |

### 5.2 누적 테스트 수
- Phase 1: 50 (approval_store)
- Phase 2-A: +43 (gateway + config)
- **누적: 93 tests**
- Phase 2-B 예정 추가: ~30개 (MCP write handlers + e2e)
- 최종 예상: ~120개 (STATUS.md "Phase 1+2 합산 80–100개" 상회)

### 5.3 정적 검사
- `grep -E "(password|api[_-]?key|secret|token)\s*="` → 0건
- `bash -n` (sync/push 스크립트) → OK
- 라인 수: 코드 772줄 + 테스트 736줄 = 1508줄

---

## 6. 운영자 적용 절차 (Operator application steps)

상세는 `task_phase2a_sync_to_local.sh` 참조. 요약:

1. 백업
   ```bash
   cp -r src/execution src/execution.phase1.bak
   cp src/mcp_servers/_config.py src/mcp_servers/_config.py.phase1.bak
   ```

2. 스크립트 실행
   ```bash
   ./task_phase2a_sync_to_local.sh --dry-run
   ./task_phase2a_sync_to_local.sh
   ```

3. 테스트
   ```bash
   pytest tests/execution/ tests/mcp_servers/ -v
   # 기대: 50 (Phase 1) + 43 (Phase 2-A) = 93/93 통과
   ```

4. `_approval_state.py` 삭제 확인
   ```bash
   git rm src/execution/_approval_state.py  # 스크립트가 자동 처리
   git status
   ```

5. `.env` 정리 — Phase 1의 분리 환경변수 제거
   ```bash
   sed -i.bak '/JCPR_APPROVAL_DB_MCP/d;/JCPR_APPROVAL_DB_EXEC/d' .env
   # 새 키 추가 (선택, 기본값 사용 시 불필요)
   echo "JCPR_APPROVAL_DB=data/approvals.sqlite" >> .env
   ```

6. GitHub 푸시
   ```bash
   ./task_phase2a_push_to_github.sh --dry-run
   ./task_phase2a_push_to_github.sh
   ```

---

## 7. Phase 2-B 예고 (Phase 2-B preview)

다음 단계에서 작업 예정:

1. `src/mcp_servers/_write_handlers.py` 재작성 — `_execute_action_stub` 제거, ExecutionGateway 직결
2. `src/mcp_servers/restricted_server.py` — DI로 통합 store + ExecutionGateway 주입
3. `scripts/approve_cli.py` — Task 35 + Task 40 통합 단일 파일
4. `src/agents/prompts/tools/restricted_tools.md` — 워크플로우 표현 갱신
5. End-to-end 통합 테스트 — agent request → operator approve → KIS mock execute (~30개)

운영자가 Phase 2-A 검증 + GitHub 푸시 완료 후 Phase 2-B 진행 승인 요청.

---

**문서 끝**
