# Phase 2 Stage 2A — 변경 내역 (Change Summary)

**작성일:** 2026-05-08
**범위:** Phase 2 Stage 2A — 코어 결선 (Core wiring)
**상태:** Stage 2A 완료, Stage 2B 대기

---

## 1. 작업 요약 (Summary)

Task 35의 MCP restricted server에서 `_execute_action_stub`을 제거하고,
`ExecutionGateway`를 통해 KIS API에 도달하도록 결선 완료.

Phase 1에서 도입된 단일 `ApprovalStore`를 두 코드 경로(MCP / Gateway)가
공유하도록 의존성 주입(DI) 구조를 갱신.

---

## 2. 수정된 파일 (6개)

| 경로 | 변경 |
|---|---|
| `src/execution/execution_gateway.py` | 통합 ApprovalStore 사용, ExecutionResult dataclass 추가, idempotency, mode 일관성 검사 |
| `src/mcp_servers/_config.py` | `JCPR_APPROVAL_DB` 단일 환경변수 통일, 레거시 변수 거부 (마이그레이션 보조) |
| `src/mcp_servers/_write_handlers.py` | `_execute_action_stub` 제거 → `ExecutionGateway.execute_approved()` 호출, action_kind별 dispatch |
| `src/mcp_servers/restricted_server.py` | DI 갱신, ESC/SIGTERM 핸들러, gateway interrupt_check 결선 |
| `src/agents/prompts/tools/restricted_tools.md` | template_version 0.2.0, Phase 2 워크플로우 ASCII 다이어그램, 6개 invariant 명시 |
| `scripts/approve_cli.py` | Task 35 + Task 40 두 CLI 통합, 6개 subcommand (list/show/approve/reject/cancel/history) |

---

## 3. 삭제 대상 파일 (2개) — 운영자 수동 처리

| 경로 | 사유 |
|---|---|
| `src/mcp_servers/_approval_store.py` | Phase 1 통합 store로 대체 (Task 35 자체 store 제거) |
| `src/execution/_approval_state.py` | Phase 1 통합 store로 대체 (Task 40 자체 store 제거) |

**운영자 적용 절차** (Stage 2B에서 `MIGRATION_PHASE2.md`로 상세 안내 예정):
1. 새 6개 파일 sync_to_local 실행
2. 위 2개 파일 수동 삭제 (DB 데이터는 별도 처리, 자동 삭제 금지)
3. `pytest` 회귀 검증

---

## 4. 핵심 동작 비교 (Before / After)

### 4.1 ApprovalStore 인스턴스 수
| | Phase 1 완료 | Phase 2 Stage 2A |
|---|---|---|
| 코드 경로 | 2개 (Task 35 + Task 40) | **1개 (통합)** |
| DB 파일 | 1개 (data/approvals.sqlite) | 1개 (동일) |
| 중복 가능성 | 일부 잔존 | **완전 제거** |

### 4.2 MCP `execute_approved_action` 흐름
| | Phase 1 완료 | Phase 2 Stage 2A |
|---|---|---|
| 호출 대상 | `_execute_action_stub` (mock 응답) | **`ExecutionGateway.execute_approved()`** |
| KIS API 도달 | ❌ 불가 | ✅ 가능 (paper 모드 기본) |
| Idempotency | 없음 | ✅ EXECUTED/EXEC_FAILED 재호출 시 캐시 반환 |

### 4.3 Action kind 라우팅
| action_kind | 실행 경로 |
|---|---|
| `submit_order` | `gateway.execute_approved()` → `broker.place_order()` |
| `cancel_order` | `broker.cancel_order()` 직접 호출 |
| `set_capacity` | store에 EXECUTED 마킹만 (out-of-band 적용) |
| `kill_switch` | store에 EXECUTED 마킹 + 런타임 플래그 (out-of-band) |

### 4.4 Live 모드 가드 (이중 조건)
| 조건 | 위치 |
|---|---|
| `JCPR_ALLOW_LIVE=1` env var | `RestrictedServerConfig` 검증 |
| `JCPR_MODE=live` | `RestrictedServerConfig` 검증 |
| `gateway.allow_live=True` | `ExecutionGateway` 생성자 검증 |
| `record.mode='live'` | `execute_approved()` mode 일관성 검사 |
| `--yes-i-mean-live` 플래그 | `approve_cli` 인터랙티브 |

5중 가드. Phase 1 대비 추가된 부분: 게이트웨이 mode 일관성 검사
(record.mode != gateway.mode 차단), CLI 명시적 확인 플래그.

### 4.5 ESC/Ctrl-C 즉시 종료
| 위치 | 동작 |
|---|---|
| `RestrictedMCPServer` | SIGINT/SIGTERM → `_interrupt_flag = True` |
| `ExecutionGateway` | 모든 외부 경계에서 `_check_interrupt()` 호출 |
| 진입 직후 인터럽트 | 상태 변화 없음, raise만 (record는 APPROVED 유지) |
| broker call 직전 인터럽트 | `mark_executing` 후 → EXEC_FAILED 마킹 + raise |

---

## 5. 신규 기능 (New behaviors)

### 5.1 Idempotency
`execute_approved_action`을 같은 `approval_id`로 두 번 호출하면:
- 첫 호출: 실제 broker 호출 + EXECUTED 마킹
- 두 번째 호출: store의 `execution_payload`에서 캐시된 결과 반환,
  broker 재호출 없음

이는 중복 실행 방지 (네트워크 retry 등)에 필수.

### 5.2 Mode 일관성 검사
`record.mode = 'paper'`인 approval을 live mode gateway로 실행 시도하면
`GatewayError("approval mode='paper' != gateway mode='live'")`로 거부.
운영자 실수 방어선.

### 5.3 Generic actor id 거부
`requested_by="agent"`, `"operator"`, `"admin"`, `"root"` 같은 generic
이름은 거부됨. role+name 형태(예: `market_agent`, `risk_explanation_agent`)
강제. 자가 승인 우회 공격 방어.

### 5.4 통합 CLI subcommand
| subcommand | 용도 | 종료 코드 |
|---|---|---|
| `list` | PROPOSED 목록 | 0 |
| `show <id>` | 상세 (JSON) | 0 / 2 (not found) |
| `approve <id>` | 승인 | 0 / 2 / 3 / 4 / 5 / 6 / 7 / 8 |
| `reject <id>` | 거절 | 0 / 2 / 3 / 8 |
| `cancel <id>` | 취소 (요청자 측) | 0 / 2 / 3 / 8 |
| `history` | 최근 결정 목록 | 0 |

종료 코드 의미: 2=not found, 3=wrong state, 4=live without confirm,
5=user abort, 6=self-approval blocked, 7=expired, 8=store error.

---

## 6. 테스트 결과 (Stage 2A)

| 파일 | 테스트 수 | 결과 |
|---|---:|---|
| `tests/execution/test_execution_gateway_phase2.py` | 26 | ✅ |
| `tests/mcp_servers/test_write_handlers_phase2.py` | 39 | ✅ |
| `tests/mcp_servers/test_restricted_server_phase2.py` | 21 | ✅ |
| `tests/test_approve_cli_phase2.py` | 18 | ✅ |
| **Stage 2A 합계** | **104** | **✅ 104/104 통과** |

Phase 1 (50) + Stage 2A (104) = 누적 154개. Stage 2B에서 통합 테스트
~30개 추가 예정 → 최종 누계 ~184개 (목표 80~100을 초과 달성 예상).

테스트 실행 시간: 0.21초 (단위 테스트만, 외부 I/O 없음).

---

## 7. 보안 재확인 (Security re-verification)

`<assumption>` 태그 정책 준수 여부:

| 항목 | 보장 방법 | 상태 |
|---|---|---|
| 시크릿 미노출 | `_secrets.py` 변경 없음, _config.py에서 secret 키워드 거부 | ✅ |
| GitHub 노출 차단 | `.gitignore` 패턴 유지 (Stage 2B에서 push 스크립트로 검증) | ⏳ Stage 2B |
| DB 자동 삭제 금지 | 마이그레이션 시 코드 파일만 삭제, DB는 운영자 수동 처리 | ✅ |
| 토큰 캐시 0600 | KIS adapter 변경 없음 | ✅ |
| Live 모드 5중 가드 | env + config + gateway + record + CLI 플래그 | ✅ |
| ESC/Ctrl-C 즉시 종료 | 모든 외부 경계에서 interrupt_check | ✅ |
| 자가 승인 차단 | store + handler 이중 검증 | ✅ |
| Generic actor id 거부 | handler에서 거부 | ✅ |

---

## 8. Stage 2B 작업 항목 (다음 세션 또는 후속 진행)

- [ ] 통합 테스트 ~30개 (e2e order, MCP→Gateway dispatch, kill_switch during exec, approval expiry, self-approval block)
- [ ] `task_phase2_sync_to_local.py` (Python 크로스플랫폼)
- [ ] `task_phase2_push_to_github.py` (시크릿 스캐너 포함)
- [ ] `docs/MIGRATION_PHASE2.md` (운영자 적용 절차)
- [ ] `STATUS.md` 갱신

---

## 9. 호환성 (Compatibility)

| 코드 | 변경 여부 |
|---|---|
| Task 9 (read-only `BrokerInterface`) | 변경 없음 |
| Task 34 readonly MCP server | 변경 없음 |
| KIS adapter (`kis_adapter.py`, `kis_execution.py`) | 변경 없음 |
| Tasks 36-39 agent code | 변경 없음 (MCP tool 시그니처 동일) |
| Phase 1 ApprovalStore | 변경 없음 (Phase 1에서 이미 통합됨) |

운영자 측 Stage 2A 적용은 **6개 파일 교체 + 2개 파일 삭제**만 필요.
다른 모듈 회귀 위험 없음.

---

## 10. 알려진 한계 (Known limitations) — Stage 2B에서 해결

1. 단위 테스트는 in-memory `ApprovalStore` stub 사용. 실제 SQLite WAL
   동작은 Stage 2B 통합 테스트에서 검증.
2. KIS adapter mock 사용. 실제 paper API 콜 검증은 운영자 환경에서
   수행 (Stage 2B `task_phase2_sync_to_local.py` 후).
3. set_capacity의 capacity.yaml 실제 갱신은 out-of-band (Task 44에서 처리).
4. kill_switch의 KILL_SWITCH_ON 파일 드롭은 paper trading runner
   (Tasks 32-33)에서 처리.

---

**문서 끝.** Stage 2A 산출물 검토 후 Stage 2B 진행 여부를 결정.
