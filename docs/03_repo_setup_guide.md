# 03. GitHub 저장소 생성 안내서 (Repository Setup Guide)

| 항목 (Item) | 값 (Value) |
|---|---|
| 프로젝트명 (Project) | jcpr-ts-v01 |
| 문서 (Document) | docs/03_repo_setup_guide.md |
| 버전 (Version) | v0.1 |
| 작성일 (Date) | 2026-05-04 |
| 사용자 (User) | JCPR 전용 (JCPR sole use) |
| 관련 문서 (Related) | docs/00_target_operating_model.md, docs/09_production_rollout.md |

---

## 1. 문서 목적 (Document Purpose)

본 문서는 jcpr-ts-v01 시스템을 위한 **GitHub 저장소(repository)** 의 생성·초기화·디렉토리 골격(directory skeleton) 설정 절차를 안내한다. 모든 단계는 **사용자의 로컬 시스템(local system)** 에서 직접 수행되며, 컨테이너(container) 환경에서는 수행되지 않는다.

---

## 2. 사전 준비 (Prerequisites)

| 항목 (Item) | 요구사항 (Requirement) |
|---|---|
| Git | 버전 2.30 이상 권장 (≥ 2.30 recommended) |
| GitHub 계정 (account) | JCPR 운영자 계정 |
| 인증 방식 (auth method) | SSH 키 (SSH key) 또는 개인 액세스 토큰 (Personal Access Token, PAT) |
| 운영체제 (OS) | macOS / Linux / Windows (WSL 권장) |
| 에디터 (editor) | VSCode, Neovim 등 |
| Python | 3.11 이상 권장 (이후 Task에서 필요) |

### 2.1 인증 점검 (Authentication Check)

SSH 사용 시:
```bash
ssh -T git@github.com
# Hi <username>! You've successfully authenticated... 메시지 확인
```

PAT 사용 시:
- PAT는 `repo` 스코프(scope)만 부여 — 최소 권한 원칙(least privilege)
- PAT는 **로컬 시스템에만 보관**, 절대 코드·로그·채팅에 노출 금지

---

## 3. 저장소 생성 (Repository Creation)

### 3.1 가시성 결정 (Visibility Decision)

| 옵션 (Option) | 권장 여부 (Recommendation) | 사유 (Rationale) |
|---|---|---|
| **Private** | ✅ **강력 권장 (Strongly recommended)** | JCPR 전용 시스템, 외부 공개 이유 없음 |
| Public | ❌ 비권장 | 시스템 구조 노출, 사회공학 공격 표면 확대 |
| Internal (org only) | 가능 (org 환경 시) | 조직 내부 공유 필요 시 |

**본 안내서는 Private 저장소를 기준으로 작성된다.**

### 3.2 저장소 생성 방법 — A: GitHub 웹 (Web)

1. https://github.com/new 접속
2. Repository name: `jcpr-ts-v01`
3. Description: `Trading solution for JCPR (private)`
4. Visibility: **Private**
5. **Initialize 옵션은 모두 선택 해제 (uncheck all)**:
   - Add a README ❌
   - Add .gitignore ❌
   - Choose a license ❌
   (이 파일들은 컨테이너 산출물로 직접 추가)
6. Create repository 클릭

### 3.3 저장소 생성 방법 — B: gh CLI (선택)

GitHub CLI 가 설치되어 있는 경우:
```bash
gh auth login
gh repo create jcpr-ts-v01 --private --description "Trading solution for JCPR" --confirm
```

---

## 4. 로컬 클론 및 초기화 (Clone & Init)

### 4.1 작업 디렉토리 (Working Directory)

작업 디렉토리는 **사용자의 로컬 신뢰 영역(local trusted zone)** 에 위치해야 한다. 동기화 클라우드 폴더(예: 일부 클라우드 동기화 폴더 — 시크릿 위험)에는 두지 않는다.

권장 위치 예시:
- macOS/Linux: `~/Projects/jcpr-ts-v01`
- Windows (WSL): `~/projects/jcpr-ts-v01`

### 4.2 새 폴더에서 시작하는 경우 (Start from new folder)

```bash
mkdir -p ~/Projects/jcpr-ts-v01
cd ~/Projects/jcpr-ts-v01
git init -b main
git remote add origin git@github.com:<your-username>/jcpr-ts-v01.git
```

### 4.3 클론으로 시작하는 경우 (Clone existing)

```bash
cd ~/Projects
git clone git@github.com:<your-username>/jcpr-ts-v01.git
cd jcpr-ts-v01
```

---

## 5. 디렉토리 골격 (Directory Skeleton)

빌드 일정(build schedule) 50개 작업의 산출물이 위치할 디렉토리 구조를 미리 생성한다.

### 5.1 골격 트리 (Skeleton Tree)

```
jcpr-ts-v01/
├── .gitignore                      # Task 4
├── .env.example                    # Task 4
├── README.md                       # 추후 갱신
├── docs/                           # 문서 (Documentation)
│   ├── 00_target_operating_model.md       # Task 1 ✅
│   ├── 03_repo_setup_guide.md             # Task 3 ✅
│   ├── 09_production_rollout.md           # Task 2 ✅
│   └── final_operating_manual.md          # Task 50
├── configs/                        # 설정 (Config)
│   ├── capacity.example.yaml              # Task 5 (template)
│   ├── risk_limits.example.yaml           # Task 6 (template)
│   ├── live_limited.example.yaml          # Task 41 (template)
│   ├── strategy_registry.yaml             # Task 45
│   └── capacity_ladder.yaml               # Task 44
├── src/                            # 소스 코드 (Source)
│   ├── brokers/                           # Task 7-8
│   │   ├── base.py
│   │   └── <broker>_adapter.py
│   ├── data/                              # Task 10-13
│   │   ├── symbol_master.py
│   │   ├── calendar.py
│   │   ├── market_data.py
│   │   └── quotes.py
│   ├── signals/                           # Task 14-16
│   │   ├── schema.py
│   │   └── strategies/
│   │       └── momentum_v1.py
│   ├── execution/                         # Task 17-24, 40
│   │   ├── order_intent.py
│   │   ├── sizing.py
│   │   ├── execution_gateway.py
│   │   ├── idempotency.py
│   │   ├── order_state.py
│   │   ├── fills.py
│   │   └── approval.py
│   ├── risk/                              # Task 19-20, 29-31, 46-47
│   │   ├── risk_gate.py
│   │   ├── reports.py
│   │   ├── shutdown.py
│   │   ├── keyboard_stop.py
│   │   ├── kill_switch.py
│   │   ├── capital_allocation.py
│   │   └── portfolio_risk.py
│   ├── pnl/                               # Task 25-28
│   │   ├── position_ledger.py
│   │   ├── pnl_engine.py
│   │   ├── slippage.py
│   │   └── reconciliation.py
│   ├── mcp_servers/                       # Task 34-35
│   │   ├── readonly_server.py
│   │   └── restricted_server.py
│   ├── agents/                            # Task 36-39
│   │   ├── prompts/
│   │   ├── market_agent.py
│   │   ├── risk_agent.py
│   │   └── pnl_agent.py
│   └── dashboard/                         # Task 48
├── scripts/                        # 운영 스크립트 (Ops scripts)
│   ├── check_broker_connection.py         # Task 9
│   ├── show_positions.py                  # Task 9
│   ├── generate_signals.py                # Task 16
│   ├── run_paper_trading.py               # Task 32
│   └── run_live_limited.py                # Task 42
├── reports/                        # 보고서 (Reports — gitignored output)
│   ├── session_review.py                  # Task 43
│   └── daily_report.py                    # Task 49
├── data/                           # 데이터 (Data)
│   └── reference/                         # 참조 데이터만 커밋
│       └── symbol_master.csv              # Task 10
├── tests/                          # 테스트 (Tests)
└── runtime/                        # 런타임 상태 (NOT committed)
    └── .gitkeep
```

### 5.2 골격 일괄 생성 명령 (Bulk Skeleton Creation)

저장소 루트에서 다음 명령을 실행하면 빈 디렉토리 골격이 생성된다:

```bash
# 디렉토리 생성
mkdir -p docs configs src/brokers src/data src/signals/strategies \
         src/execution src/risk src/pnl src/mcp_servers \
         src/agents/prompts src/dashboard \
         scripts reports data/reference tests runtime

# Python 패키지 표시 (Mark as Python packages)
touch src/__init__.py \
      src/brokers/__init__.py \
      src/data/__init__.py \
      src/signals/__init__.py \
      src/signals/strategies/__init__.py \
      src/execution/__init__.py \
      src/risk/__init__.py \
      src/pnl/__init__.py \
      src/mcp_servers/__init__.py \
      src/agents/__init__.py \
      src/dashboard/__init__.py \
      tests/__init__.py

# 빈 디렉토리 보존 (Keep empty dirs in git)
touch runtime/.gitkeep reports/.gitkeep
```

---

## 6. 첫 커밋 (Initial Commit)

### 6.1 컨테이너 산출물 배치 (Place Container Artifacts)

다음 파일들을 컨테이너 출력물에서 다운로드하여 저장소 루트에 배치:

| 파일 (File) | 위치 (Path) | 출처 (Source) |
|---|---|---|
| 목표 운영 모델 | `docs/00_target_operating_model.md` | Task 1 |
| 저장소 안내서 | `docs/03_repo_setup_guide.md` | Task 3 (본 문서) |
| 프로덕션 등가 제한 용량 | `docs/09_production_rollout.md` | Task 2 |
| `.gitignore` | `.gitignore` | Task 4 |
| `.env.example` | `.env.example` | Task 4 |

### 6.2 푸시 (Push)

```bash
git add .gitignore .env.example docs/ configs/ src/ scripts/ reports/ data/ tests/ runtime/
git status            # 푸시될 항목 확인 (review what will be pushed)
git commit -m "chore: initial repo skeleton with docs and ignore rules (Task 1-4)"
git push -u origin main
```

### 6.3 푸시 후 검증 (Post-push Verification)

GitHub 웹에서 저장소를 열어 다음을 확인:

- [ ] `.env` 파일이 **없는지** 확인 (must NOT be present)
- [ ] `.env.example` 만 존재
- [ ] 어떤 파일에도 실제 API 키나 비밀번호가 없는지 확인
- [ ] 저장소가 **Private** 으로 설정되어 있는지 확인

---

## 7. 보안 사전 점검 (Security Pre-check)

### 7.1 시크릿 누출 검사 (Secret Leakage Check)

커밋 전에 항상 실행 권장:

```bash
# 간단 검사 — .env 가 staged 되어 있는지 확인
git status | grep -E '\.env$' && echo "⚠️ .env 가 staged 됨 — 즉시 unstage" || echo "✅ .env 미포함"

# 평문 시크릿 패턴 검사 (간단 grep)
git diff --cached | grep -iE 'api[_-]?key|secret|password|token' \
  | grep -vE '^\+\+\+|^---|example|placeholder|^\s*#' \
  && echo "⚠️ 시크릿 의심 — 검토 필요" \
  || echo "✅ 시크릿 패턴 없음"
```

### 7.2 권장 도구 (Recommended Tools, optional)

조직 정책에 따라 다음 도구를 도입할 수 있다 (모두 로컬 실행):

- **gitleaks** — 커밋 전 시크릿 스캔
- **pre-commit** — 커밋 훅 자동화
- **detect-secrets** — 베이스라인 기반 누출 감지

설치는 사용자 재량이며, **본 시스템의 핵심 안전장치는 `.gitignore` 와 사전 점검 절차** 다.

### 7.3 사고 대응 (Incident Response)

만약 실수로 시크릿을 푸시한 경우:

1. **즉시 해당 자격증명을 회전 (rotate immediately)** — 단순 삭제 커밋만으로는 부족
2. 브로커 API 키, GitHub PAT 등 모든 영향받는 키를 새로 발급
3. `git filter-repo` 또는 BFG Repo-Cleaner로 히스토리 정리 (선택)
4. 사고 기록 (incident record) 작성

---

## 8. 브랜치 전략 (Branch Strategy, Optional)

소규모 단일 운영자 환경(small single-operator environment)에서는 단순 전략을 권장:

```
main           — 안정 (stable, deployable)
└── feature/*  — 작업 분기 (per-task)
└── hotfix/*   — 긴급 수정 (emergency fix)
```

작업 흐름:
```bash
git checkout -b feature/task-05-capacity-config
# ... 작업 ...
git push origin feature/task-05-capacity-config
# 셀프 리뷰 후 main 으로 PR/머지
```

---

## 9. 체크리스트 (Checklist)

저장소 생성 후 다음 항목을 모두 확인:

- [ ] 저장소 가시성: **Private**
- [ ] `.gitignore` 푸시됨
- [ ] `.env.example` 푸시됨
- [ ] `.env` 푸시 **안** 됨
- [ ] `docs/00_target_operating_model.md` 푸시됨
- [ ] `docs/03_repo_setup_guide.md` 푸시됨
- [ ] `docs/09_production_rollout.md` 푸시됨
- [ ] 디렉토리 골격 생성됨
- [ ] 시크릿 패턴 검사 통과
- [ ] 첫 커밋 메시지: `chore: initial repo skeleton ...`

---

## 10. 변경 이력 (Change History)

| 버전 (Version) | 일자 (Date) | 변경 내용 (Change) | 작성자 (Author) |
|---|---|---|---|
| v0.1 | 2026-05-04 | 최초 작성 (Initial draft) | JCPR / jcpr-ts-v01 |

---

*본 문서의 모든 단계는 사용자의 로컬 시스템에서 직접 수행된다. 컨테이너 환경에는 어떠한 자격증명도 반입되지 않는다.*
