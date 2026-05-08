# Phase 1 — ApprovalStore 통합 변경사항 요약
## (Change Summary — Single-Store Unification)

**Phase**: 1 of 2
**작업 범위**: ApprovalStore 통합 + 50개 테스트
**파일 수**: 신규 4개, 수정 0개, 삭제 0개

---

## 1. 무엇이 바뀌었나 (What changed)

### 신규 파일 (Added)

| 파일 | 역할 | 줄 수 |
|---|---|---|
| `src/execution/approval_store.py` | 통합 ApprovalStore (Task 35 + Task 40 통합) | ~700 |
| `src/execution/__init__.py` | 패키지 export | ~50 |
| `tests/execution/test_approval_store.py` | 통합 store 테스트 (50개) | ~600 |
| `docs/MIGRATION.md` | 운영자 수동 삭제 절차 | ~250 |

### 변경 파일 (Modified)

**없음(None)** — Phase 1은 순수 추가만.

### 삭제 파일 (Deleted)

**없음(None)** — Phase 2에서 다음 파일 삭제 예정:
- `src/mcp_servers/_approval_store.py` (Task 35 작성)
- `src/execution/_approval_state.py` (Task 40 작성)

---

## 2. 운영자가 수정해야 할 부분 (What the operator must do)

| # | 항목 | 필수 여부 |
|---|---|---|
| 1 | `task_phase1_sync_to_local.sh` 실행 | ✅ 필수 |
| 2 | `python -m pytest tests/execution/test_approval_store.py` 실행 | ✅ 필수 |
| 3 | `docs/MIGRATION.md` §2.6 — 기존 DB 6 파일 수동 삭제 | ✅ 필수 |
| 4 | `.gitignore`에 `data/approvals.sqlite*` 추가 (없으면) | ✅ 필수 |
| 5 | `task_phase1_push_to_github.sh` 실행 | ⚠️ 선택 |

**상세 절차는 `docs/MIGRATION.md` 참조.**

---

## 3. Task 34-39와의 호환성 (Compatibility with prior tasks)

### 영향 없음 (No impact)

Phase 1은 **신규 파일만 추가**하므로 다음 시스템 동작에는 변화 없음:

| Task | 구성요소 | 변화 |
|---|---|---|
| Task 9 | KIS broker connection scripts | 변화 없음 |
| Task 34 | MCP read-only server (8 tools) | 변화 없음 |
| Task 35 | MCP restricted server | 기존 store 사용 (Phase 2에서 변경) |
| Task 36 | Agent prompt templates | 변화 없음 |
| Task 37 | market_agent | 변화 없음 |
| Task 38 | risk_agent | 변화 없음 |
| Task 39 | pnl_agent | 변화 없음 |
| Task 40 | execution_gateway, approve_cli | 기존 store 사용 (Phase 2에서 변경) |

### Phase 2에서의 영향 (Impact in Phase 2)

Phase 2에서 다음 파일들이 **신규 통합 store를 참조하도록 수정**됩니다:
- `src/mcp_servers/_write_handlers.py` — stub 제거 + ExecutionGateway 호출
- `src/mcp_servers/restricted_server.py` — 통합 store 주입
- `src/mcp_servers/_config.py` — `approval_db` 경로 통일
- `src/execution/execution_gateway.py` — 통합 store 사용
- `scripts/approve_cli.py` — 단일 파일로 통합

---

## 4. 보안 영향 (Security impact)

`<assumption>` 보안 요구사항 모두 유지 또는 강화:

| 항목 | 통합 전 | 통합 후 |
|---|---|---|
| 시크릿 GitHub 노출 차단 | ✅ | ✅ 변화 없음 |
| ApprovalStore 0600 권한 | ✅ (2 파일 각각) | ✅ (1 파일, 더 단순) |
| Self-approval 차단 | ✅ (양쪽 store) | ✅ (단일 지점) |
| Live 모드 진입 가드 | ⚠️ (양쪽 정책 다름) | ✅ 일관 — `LiveModeBlockedError` |
| TTL 정책 | 5min/60s | 5min/60s (kill_switch는 60s) |
| Single-use 실행 | ✅ | ✅ + EXECUTING 중간 상태 추가 |
| Decimal 정밀도 | ✅ (양쪽 string) | ✅ (단일 정책) |

**신규 보안 강화 포인트**:
- `EXECUTING` 중간 상태 도입 → APPROVED→EXECUTED 사이 동시성 충돌 방지 (테스트 검증)
- `LiveModeBlockedError` 명시적 예외 → 로그 식별 용이

**신규 보안 위험**: 없음(None).

---

## 5. 테스트 커버리지 (Test coverage)

### 50개 테스트 분류

| 카테고리 | 테스트 수 | 주요 검증 |
|---|---|---|
| Construction | 5 | DB 생성, 0600 권한, 부모 디렉터리, 스키마 버전 |
| create_request — 정상 | 6 | submit_order, kill_switch TTL, session/trace ID |
| create_request — 검증 | 7 | invalid action, empty requester, live mode block |
| Approve | 7 | 자기승인 차단, 재승인 차단, execute TTL 설정 |
| Reject | 3 | reason 필수, approve 후 거부 차단 |
| Cancel | 2 | requester 취소, approved 후 취소 차단 |
| Execution lifecycle | 7 | EXECUTING/EXECUTED/EXEC_FAILED, single-use |
| Expiration | 4 | TTL 자동 만료, terminal 상태 보호 |
| List queries | 5 | filter, limit 검증 |
| Concurrency | 2 | only one approve wins, only one executing wins |
| Serialization | 2 | datetime ISO, None 처리 |

**전체**: 50/50 통과 ✅

---

## 6. Phase 2 예고 (Phase 2 preview)

다음 응답에서 진행할 작업:

1. **`_write_handlers.py` 재작성**
   - `_execute_action_stub` 제거
   - `request_submit_order` → `OrderRequest` 변환 → `ExecutionGateway.propose_order()` 호출
   - `execute_approved_action` → `ExecutionGateway.execute()` 호출 (KIS 실 호출)

2. **`restricted_server.py` 수정**
   - 의존성 주입: `(ApprovalStore, ExecutionGateway)` 받음
   - 환경변수 `JCPR_APPROVAL_DB` 단일 경로

3. **`execution_gateway.py` 수정**
   - 통합 `ApprovalStore` 사용
   - `propose_order()` + `execute(approval_id)` 인터페이스 안정화
   - `KISExecutionAdapter` 의존성 주입

4. **`approve_cli.py` 단일화**
   - Task 35 + Task 40 버전 → 통합 1개 파일
   - 환경변수 `JCPR_APPROVAL_DB` 사용

5. **통합 테스트**
   - end-to-end: agent request → approve → execute → KIS mock
   - Phase 1+2 누적 약 80–100 테스트 예상

---

## 7. 롤백 가능성 (Rollback)

Phase 1은 **순수 추가**이므로 롤백 시 다음만 수행:

```bash
git revert <phase1-commit-sha>
rm -rf data/approvals.sqlite*  # 운영자 결정
```

기존 Task 35 + Task 40 코드는 영향받지 않으므로 **무중단 롤백 가능**.

---

**Phase 1 완료. 운영자 검토 + 승인 후 Phase 2 진행.**
