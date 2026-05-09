# Phase 2-B Changes — JCPR-ts-v01

**날짜(Date)**: 2026-05-08
**범위(Scope)**: MCP write handlers, restricted_server, approve_cli, agent prompt 통합
**전제(Prerequisite)**: Phase 2-A 적용 완료 — ExecutionGateway+통합 ApprovalStore, 43/43 테스트 통과

---

## 1. 한눈 비교 (Phase 2-A → Phase 2-B summary diff)

| 영역 | Phase 1 / Phase 2-A | Phase 2-B (현재) |
|---|---|---|
| MCP write 흐름 | `_execute_action_stub`이 직접 EXECUTED 마킹 (실 KIS 미호출) | propose만 수행, 운영자 승인 후 ExecutionGateway가 KIS API 호출 |
| ApprovalStore 의존성 | restricted_server에 Phase 1 분리 store 직접 import | ServerConfig+통합 ApprovalStore+ExecutionGateway+WriteHandlers DI 주입 |
| 핸들러 묶음 | 각 도구가 server 안에 흩어져 있음 | `WriteHandlers` 클래스로 통합 (8개 메서드) |
| 시크릿 누설 방어 | 없음 | server-side `_scan_for_secrets`가 응답 키 화이트리스트 검사 |
| `approve_cli` 파일 | Task 35용 + Task 40용 **두 개** | **단일 파일** (subcommand 7개) |
| agent prompt | Phase 1 stub 기반 표현 | Phase 2-B 워크플로우(propose-only)로 갱신 |
| `_approval_store.py` (Task 35 잔존물) | 존재 | **삭제** (sync 스크립트 자동 처리) |
| 테스트 수 | Phase 2-A 43개 (gateway+config) | Phase 2-B **82개** 추가 |
| 누적 (Phase 1+2A+2B) | 93 | **175 tests** |

---

## 2. 신규/수정/삭제 파일

### 신규 (NEW)
- `tests/mcp_servers/test_write_handlers.py` — 32 tests, 7 카테고리
- `tests/mcp_servers/test_restricted_server.py` — 19 tests, 6 카테고리
- `tests/integration/test_phase2b_end_to_end.py` — 18 tests, 12 카테고리 (e2e)
- `tests/scripts/test_approve_cli.py` — 13 tests, 6 카테고리
- `docs/PHASE2B_CHANGES.md` — 본 문서
- `task_phase2b_sync_to_local.sh`, `task_phase2b_push_to_github.sh`

### 수정 (MODIFIED)
- `src/mcp_servers/_write_handlers.py` — **재작성** (462 lines)
  - `_execute_action_stub` 완전 제거
  - `WriteHandlers` 클래스로 8개 도구 통합
  - payload validation (BUY/SELL, LIMIT/MARKET, qty>0, limit_price 등)
  - identity guard (`requested_by`가 'operator'로 시작 시 차단)
- `src/mcp_servers/restricted_server.py` — **재작성** (303 lines)
  - `ServerConfig` + `ApprovalStore` + `ExecutionGateway` + `WriteHandlers` DI
  - `ToolResult` envelope (ok / result / error / error_kind / elapsed_ms)
  - `_scan_for_secrets` defense-in-depth
  - `build_restricted_server` 팩토리
- `scripts/approve_cli.py` — **재작성, 단일 파일 통합** (438 lines)
  - 서브커맨드 7개: list, recent, show, approve, reject, cancel, execute
  - `--operator` 필수 (mutating commands)
  - `--prod` + `JCPR_ALLOW_LIVE=1` 이중 가드
  - `--broker-factory` 테스트용 mock 주입
- `src/agents/prompts/tools/restricted_tools.md` — **갱신** (126 lines)
  - propose-only 워크플로우 흐름도
  - 8개 도구 사용법 + 페이로드 스키마
  - error_kind 표 + agent 행동 규약

### 삭제 대상 (DELETION TARGETED, sync 스크립트 자동 처리)
- `src/mcp_servers/_approval_store.py` (Task 35 분리 store) — Phase 2-A에서 이미 단일 store로 통합됨
- `src/execution/_approval_state.py` (Task 40 분리 store) — Phase 2-A에서 이미 삭제 대상

---

## 3. 핵심 설계 결정 (Key design)

### 3.1 propose / execute 분리 (write path split)
agent의 도구 호출은 **propose만** 수행. ExecutionGateway는 별도 트리거(`approve_cli execute`)에서 호출됨. 이는 자동 실행 경로를 의도적으로 끊어 운영자의 명시적 승인을 강제하는 안전 장치 — `<requirement>` 태그 "코드 생성은 승인 후에만"의 거래 워크플로우 등가물.

### 3.2 시크릿 누설 다층 방어 (defense in depth)
- **계층 1**: `_write_handlers`에서 broker 어댑터 미접근 (시크릿 미접근)
- **계층 2**: `WriteHandlers._record_to_dict`가 화이트리스트 필드만 반환
- **계층 3**: `RestrictedServer._scan_for_secrets`가 응답 키를 재귀 검사하여 시크릿 의심 키 발견 시 응답 차단 + audit log
- **계층 4**: `gateway.status_snapshot()`도 시크릿 미포함 (Phase 2-A에서 보장)

### 3.3 Identity guard
`_validate_requested_by`가 `requested_by`가 빈 값 또는 'operator'로 시작하는 경우 거부. self-approval 차단의 1차 방어선 (2차는 store의 `requested_by != decided_by` 체크).

### 3.4 cancel_proposal — own only
agent가 자신이 propose한 PROPOSED 레코드만 취소 가능. 다른 agent의 제안은 `IdentityViolationError`. 이는 멀티 agent 환경에서 한 agent가 다른 agent의 작업을 방해하는 시나리오 차단.

### 3.5 list cap 강제
`list_pending_approvals`, `get_recent_decisions`에 `LIST_HARD_MAX = 200` 강제 — agent가 max_results=10000을 보내도 server가 200으로 캡. 응답 폭발(response explosion) 방지.

### 3.6 ToolResult envelope
모든 MCP 도구 호출 결과는 `ToolResult(ok, result, error, error_kind, elapsed_ms)`로 정규화. agent는 `error_kind`로 8가지 에러 유형(`not_found`, `validation`, `identity`, `transition`, `self_approval`, `live_blocked`, `unknown_tool`, `internal`)을 분류하여 적절히 대응 가능.

### 3.7 approve_cli — 단일 파일 통합
Task 35용 + Task 40용 두 파일을 하나로 통합. 운영자가 어느 CLI를 써야 하는지 혼란 제거. 서브커맨드 디자인으로 모든 워크플로우 단계를 한 도구에서 처리.

---

## 4. 보안 검증 (Security verification)

`<assumption>` 보안 요구사항 대조:

| 항목 | Phase 2-B 상태 |
|---|---|
| 시크릿 GitHub 노출 차단 | ✅ `.env` gitignored, sync/push 스크립트가 `.env`/`.key`/`tokens.json` 푸시 차단 |
| 시크릿 응답 누설 차단 | ✅ `_scan_for_secrets` 4계층 방어 |
| 평문 시크릿 할당 (정적 grep) | ✅ 0건 (`password|api_key|appsecret|access_token|private_key`) |
| Self-approval 차단 | ✅ identity guard + store 조건 이중 |
| Live 모드 가드 | ✅ Phase 2-A 3중 가드 + paper handler/live record 격리 |
| ESC/Ctrl-C 우선 | ✅ Phase 2-A KillSwitch Protocol 유지, e2e 테스트 검증 |
| DB 운영자 확인 후 삭제 | ✅ sync 스크립트가 `_approval_store.py`/`_approval_state.py`만 삭제, DB 미삭제 |
| Operator impersonation | ✅ `requested_by`가 'operator'로 시작 시 거부 |

---

## 5. 테스트 결과 (Test results)

```
$ pytest tests/ -v
============================== 125 passed in 2.15s ==============================
(Phase 2-A: 43 + Phase 2-B: 82)
```

### 5.1 카테고리별 분포

| 파일 | 테스트 수 |
|---|---|
| test_execution_gateway.py (Phase 2-A) | 25 |
| test_config.py (Phase 2-A) | 18 |
| test_write_handlers.py | 32 |
| test_restricted_server.py | 19 |
| test_phase2b_end_to_end.py | 18 |
| test_approve_cli.py | 13 |
| **합계** | **125** |

### 5.2 e2e 시나리오 12개
1. happy path (propose → approve → execute) ✅
2. reject 후 재propose ✅
3. cancel 흐름 ✅
4. self-approval 차단 ✅
5. live 모드 가드 (paper system rejects live record) ✅
6. live system routes to live broker ✅
7. kill switch 활성 시 모든 execute 차단 ✅
8. PROPOSED TTL 5분 만료 ✅
9. APPROVED TTL 60초 만료 ✅
10. EXEC_FAILED 후 재실행 차단 ✅
11. 동시 operator 2명 approve — 한 명만 성공 ✅
12. paper/live 모드 일치성 종합 ✅

### 5.3 동시성 안정성
20회 연속 실행 모두 통과 (concurrency stable across 20 runs).

### 5.4 누적 (Phase 1 + 2A + 2B)
- Phase 1: 50 (approval_store)
- Phase 2-A: +43 (gateway + config)
- Phase 2-B: +82 (write_handlers + restricted_server + e2e + approve_cli)
- **누적: 175 tests** — STATUS.md 예측 80–100개를 크게 상회

---

## 6. 운영자 적용 절차 (Operator application)

상세는 `task_phase2b_sync_to_local.sh` 참조. 요약:

1. 백업 (자동)
   ```bash
   ./task_phase2b_sync_to_local.sh --dry-run  # 미리보기
   ./task_phase2b_sync_to_local.sh             # 실제 적용
   ```

2. 잔존 모듈 삭제 확인
   ```bash
   ls src/mcp_servers/_approval_store.py 2>&1  # 없어야 함
   ls src/execution/_approval_state.py 2>&1    # 없어야 함
   ```

3. 테스트
   ```bash
   pytest tests/ -v
   # 기대: Phase 1 (50) + Phase 2-A (43) + Phase 2-B (82) = 175/175 PASSED
   ```

4. 잔존 import 검증
   ```bash
   grep -rn '_approval_store\|_approval_state\|_execute_action_stub' src/ tests/ scripts/
   # 0건이어야 함
   ```

5. GitHub 푸시
   ```bash
   ./task_phase2b_push_to_github.sh --dry-run
   ./task_phase2b_push_to_github.sh
   ```

---

## 7. 다음 단계 (Next steps)

Phase 2 통합이 완료되면 Phase 3로 진행할 작업:

| Task # | 작업 | 우선순위 |
|---|---|---|
| 32 | `scripts/run_paper_trading.py` paper-trading 통합 러너 | High |
| 33 | Paper 세션 실행 + 버그 수정 | High |
| 41-43 | live-limited 운영 (configs + 실 KIS paper account) | Medium |
| 44 | capacity expansion ladder | Medium |
| 49 | daily report generator (final output 12개 항목) | High |
| 50 | final operating manual | Low (마지막) |

운영자 승인 후 Phase 3 작업 범위 확정.

---

**문서 끝**
