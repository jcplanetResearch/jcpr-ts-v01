# MIGRATION — Phase 1: ApprovalStore 통합 절차
## (Single-Store Unification — Manual DB Cleanup Procedure)

**대상 운영자(Target operator)**: JCPR
**Phase**: 1 of 2 — ApprovalStore 단일화
**작업 일자**: 2026-05-07 이후 적용
**예상 소요 시간**: 약 5–10분

---

## 0. 이 문서의 목적 (Purpose)

옵션 A 통합 리팩터의 **첫 번째 단계**입니다. Task 35가 사용하던 MCP 전용 ApprovalStore와 Task 40이 사용하던 ExecutionGateway 전용 ApprovalState를 **단일 SQLite DB**로 합칩니다.

`<user>` 조항에 따라 **기존 DB는 운영자 본인이 수동 삭제**합니다. 본 절차는 그 삭제 전후 단계를 명시합니다.

---

## 1. 사전 조건 (Pre-conditions)

본 절차를 실행하기 전 다음을 모두 충족해야 합니다:

| 항목 | 확인 방법 |
|---|---|
| Task 9 + Task 40 작업이 로컬에 적용 완료 | `ls src/execution/_approval_state.py` → 파일 존재 |
| Task 35 작업이 로컬에 적용 완료 | `ls src/mcp_servers/_approval_store.py` → 파일 존재 |
| 진행 중인 거래 세션 없음 | `ps aux \| grep run_paper_trading` → 결과 없음 |
| 진행 중인 MCP 서버 없음 | `ps aux \| grep mcp_servers` → 결과 없음 |
| 진행 중인 approve_cli 프로세스 없음 | `ps aux \| grep approve_cli` → 결과 없음 |

**모두 충족되지 않으면 진행하지 말 것.**

---

## 2. 단계별 절차 (Step-by-Step Procedure)

### Step 2.1 — 기존 DB 파일 위치 확인

먼저 어떤 DB 파일이 어디에 있는지 확인합니다.

```bash
cd /path/to/jcpr-ts-v01

# Task 35 MCP store가 만든 DB
find . -name "approvals_mcp*.sqlite*" -not -path "./node_modules/*" 2>/dev/null

# Task 40 ExecutionGateway가 만든 DB
find . -name "approvals_exec*.sqlite*" -not -path "./node_modules/*" 2>/dev/null

# 기타 approvals*.sqlite (일반 패턴)
find . -name "approvals*.sqlite*" -not -path "./node_modules/*" 2>/dev/null
```

**예상 결과**:
```
./data/approvals_mcp.sqlite
./data/approvals_mcp.sqlite-shm        # WAL shared memory
./data/approvals_mcp.sqlite-wal        # WAL log
./data/approvals_exec.sqlite
./data/approvals_exec.sqlite-shm
./data/approvals_exec.sqlite-wal
```

파일 경로를 메모해 두십시오.

---

### Step 2.2 — 미실행 승인 레코드 확인

이전 운영 중 **승인됐지만 아직 실행되지 않은(approved이지만 executed가 아닌) 레코드**가 있는지 확인합니다. 있으면 통합 후 손실됩니다.

```bash
# Task 35 MCP store
sqlite3 ./data/approvals_mcp.sqlite \
  "SELECT approval_id, state, action_kind, requested_by, decided_at \
   FROM approvals WHERE state = 'approved' \
     OR state = 'pending' OR state = 'proposed';"

# Task 40 store
sqlite3 ./data/approvals_exec.sqlite \
  "SELECT approval_id, state, action_kind, requested_by, decided_at \
   FROM approvals WHERE state = 'approved' \
     OR state = 'pending' OR state = 'proposed';"
```

**결과 해석**:
- 출력이 **빈 결과(0 rows)**: 안전. 다음 단계로 진행.
- 출력에 **레코드 존재**: 매우 중요. 다음 중 하나 선택:
  - **(권장)** 해당 승인을 운영자가 거부(reject) 또는 실행(execute) 완료 후 본 절차 재시작
  - **(주의)** 해당 데이터 손실 인지 후 진행 — Phase 1 후에는 해당 승인 ID가 사라짐

---

### Step 2.3 — 백업 (안전망)

DB 삭제 전 백업을 권장합니다 (`<user>` 조항: 운영자 확인 없이 삭제 금지).

```bash
# 백업 디렉터리 생성
BACKUP_DIR="./data/_backup_pre_phase1_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

# 백업 (WAL 파일 포함)
cp ./data/approvals_mcp.sqlite*  "$BACKUP_DIR/" 2>/dev/null || true
cp ./data/approvals_exec.sqlite* "$BACKUP_DIR/" 2>/dev/null || true

ls -la "$BACKUP_DIR"
```

**검증**: `ls -la "$BACKUP_DIR"` 결과에 6개 파일(.sqlite + .sqlite-shm + .sqlite-wal × 2 store)이 보이면 OK.

---

### Step 2.4 — Phase 1 신규 코드 적용

Phase 1 산출물을 로컬에 동기화합니다 (`task_phase1_sync_to_local.sh` 사용).

```bash
chmod +x task_phase1_sync_to_local.sh
./task_phase1_sync_to_local.sh --dry-run    # 미리보기
./task_phase1_sync_to_local.sh              # 적용
```

이 스크립트는 다음 파일을 추가/수정합니다:
- `src/execution/approval_store.py` (신규 — 통합 store)
- `src/execution/__init__.py` (수정 — export 추가)
- `tests/execution/test_approval_store.py` (신규 — 50개 테스트)
- `docs/MIGRATION.md` (이 문서)

**기존 파일은 변경하지 않음**:
- `src/mcp_servers/_approval_store.py` ← Phase 2에서 삭제 예정
- `src/execution/_approval_state.py` ← Phase 2에서 삭제 예정
- `src/execution/execution_gateway.py` ← Phase 2에서 의존성 변경

---

### Step 2.5 — Phase 1 테스트 실행

신규 통합 store가 정상 동작하는지 확인합니다.

```bash
python -m pytest tests/execution/test_approval_store.py -v
```

**기대 결과**: `50 passed`. 실패가 있으면 진행 중단하고 보고.

---

### Step 2.6 — 기존 DB 파일 수동 삭제 (운영자 결정)

**여기부터는 운영자 본인이 결정하고 실행해야 합니다.** Claude는 자동 삭제하지 않습니다.

`<user>` 조항: "DB interface: store the trading output and would not be deleted without any firm confirmation from the local administrator"

다음 명령을 한 줄씩 수동 실행하십시오. **각 줄을 실행하기 전 파일 경로가 본인이 의도한 것이 맞는지 확인**하십시오.

```bash
# 1) Task 35 MCP store 삭제
rm -i ./data/approvals_mcp.sqlite        # -i: 삭제 전 y/n 확인
rm -i ./data/approvals_mcp.sqlite-shm
rm -i ./data/approvals_mcp.sqlite-wal

# 2) Task 40 store 삭제
rm -i ./data/approvals_exec.sqlite
rm -i ./data/approvals_exec.sqlite-shm
rm -i ./data/approvals_exec.sqlite-wal
```

`-i` 플래그는 매 파일마다 **y/n 확인**을 받습니다. 본인이 직접 `y`를 타이핑한 후에만 삭제됩니다.

**검증**:
```bash
ls -la ./data/approvals_*.sqlite* 2>&1
# 기대: "No such file or directory" 또는 빈 결과
```

---

### Step 2.7 — 새 통합 DB 생성 확인

다음 MCP 서버 또는 ExecutionGateway 실행 시 자동으로 `data/approvals.sqlite`가 생성됩니다. 사전 확인용 빠른 테스트:

```bash
python -c "
from src.execution import ApprovalStore
import os

store = ApprovalStore(db_path='./data/approvals.sqlite')
print(f'DB created: {os.path.exists(\"./data/approvals.sqlite\")}')
print(f'Mode: {oct(os.stat(\"./data/approvals.sqlite\").st_mode & 0o777)}')
"
```

**기대 결과**:
```
DB created: True
Mode: 0o600
```

---

### Step 2.8 — `.gitignore` 확인

새 DB 파일이 GitHub에 노출되지 않는지 확인:

```bash
grep -E "approvals.*sqlite" .gitignore
```

**기대 결과**: 다음 패턴 중 하나가 존재해야 함:
```
data/approvals*.sqlite*
*.sqlite
*.sqlite-shm
*.sqlite-wal
```

없으면 추가:
```bash
cat >> .gitignore <<'EOF'

# Approval store (Phase 1 unified)
data/approvals.sqlite
data/approvals.sqlite-shm
data/approvals.sqlite-wal
data/_backup_pre_phase1_*/
EOF
```

검증:
```bash
git check-ignore data/approvals.sqlite
# 기대 출력: data/approvals.sqlite
```

---

## 3. 완료 확인 체크리스트 (Completion Checklist)

다음 항목을 모두 ✅ 한 후 Phase 2 진행:

- [ ] Step 2.1 — 기존 DB 파일 위치 확인 완료
- [ ] Step 2.2 — 미실행 승인 레코드 확인 (없거나, 손실 동의)
- [ ] Step 2.3 — 백업 디렉터리 생성 + 6 파일 복사 완료
- [ ] Step 2.4 — Phase 1 신규 코드 적용 완료
- [ ] Step 2.5 — `pytest tests/execution/test_approval_store.py` → 50 passed
- [ ] Step 2.6 — 기존 DB 6개 파일 수동 삭제 완료
- [ ] Step 2.7 — 새 `data/approvals.sqlite` 생성 + 0600 권한 확인
- [ ] Step 2.8 — `.gitignore`에 새 DB 패턴 포함 확인

---

## 4. 롤백 절차 (Rollback Procedure)

Phase 1 적용 후 문제 발견 시 다음으로 복구:

```bash
# 1) 새 통합 DB 삭제
rm -i ./data/approvals.sqlite*

# 2) 백업에서 복원
BACKUP_DIR="./data/_backup_pre_phase1_<timestamp>"  # 실제 경로 입력
cp "$BACKUP_DIR"/approvals_mcp.sqlite*  ./data/
cp "$BACKUP_DIR"/approvals_exec.sqlite* ./data/

# 3) Phase 1 코드 되돌리기 (git)
git checkout HEAD~1 -- src/execution/approval_store.py \
                        src/execution/__init__.py \
                        tests/execution/test_approval_store.py
```

---

## 5. Phase 2 진행 조건 (Pre-conditions for Phase 2)

Phase 2(MCP write handlers ↔ ExecutionGateway 직결)는 다음 조건이 모두 충족된 후 진행:

1. 본 문서 §3 체크리스트 8개 항목 모두 ✅
2. 운영자가 새 `data/approvals.sqlite`로 최소 1회 paper-mode end-to-end 동작 확인
3. Phase 2 설계안 검토 + 승인

---

## 6. 보안 재확인 (Security Re-verification)

Phase 1 후 다음을 재확인:

| 항목 | 검증 명령 | 기대 |
|---|---|---|
| 새 DB 권한 | `stat -c "%a" data/approvals.sqlite` (Linux) / `stat -f "%Lp" data/approvals.sqlite` (macOS) | `600` |
| 백업 디렉터리 권한 | `stat -c "%a" data/_backup_pre_phase1_*` | `700` |
| `.env` 권한 | `stat -c "%a" .env` | `600` |
| Git 상태 | `git status --porcelain \| grep -E "\.(sqlite\|env)$"` | 빈 결과 |
| Stage 시 시크릿 미포함 | `git diff --cached --name-only \| grep -E "\.env$"` | 빈 결과 |

하나라도 실패 시 보고.

---

## 부록 A — 자주 묻는 질문 (FAQ)

**Q1. 기존 DB를 삭제하지 않고 그대로 두면 어떻게 되나요?**
A. 시스템은 새 `data/approvals.sqlite`만 사용하므로 동작에 영향 없음. 다만 디스크 공간과 혼동 가능성 때문에 정리 권장.

**Q2. 백업 디렉터리는 언제 삭제하나요?**
A. 운영자 본인 판단. 권장: Phase 2 완료 + 1주 paper-trading 안정 운영 후.

**Q3. 새 DB 경로를 변경하고 싶습니다.**
A. `ApprovalStore(db_path=...)` 생성자 인자 또는 환경변수 `JCPR_APPROVAL_DB`로 설정. Phase 2에서 환경변수 지원 추가 예정.

**Q4. `data/approvals.sqlite-shm`, `-wal` 파일은 무엇인가요?**
A. SQLite WAL(Write-Ahead Logging) 모드의 보조 파일. 정상 종료 시 자동 정리. 절대 수동 편집 금지.

---

**문서 끝.**
