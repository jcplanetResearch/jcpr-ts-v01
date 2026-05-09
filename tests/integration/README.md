# JCPR-TS-V01 Integration Tests

**Stage 2B Deliverable 1** — 30 integration tests covering SQLite WAL store,
KIS paper-trading e2e, kill-switch behavior, recovery, and secret-leak
regression.

## Quick start

```bash
# 1. Install test deps (KIS official Python SDK is assumed already installed)
pip install -r requirements-test.txt

# 2. Run the whole suite
pytest tests/integration/ -v

# 3. Run a single category by marker
pytest -m wal             # 8 SQLite WAL tests
pytest -m kis_happy       # 6 happy-path e2e
pytest -m kis_reject      # 5 pre-trade rejection
pytest -m kill_switch     # 5 ESC/Ctrl-C kill switch
pytest -m recovery        # 4 restart consistency
pytest -m secret_leak     # 2 secret-leak regression
```

## Coverage matrix

| File | Tests | Build-schedule tasks covered |
|------|------:|------------------------------|
| `test_wal_store.py` | 8 | 25 (position ledger), 28 (reconciliation) |
| `test_kis_paper_happy_path.py` | 6 | 14, 19, 21, 24, 25, 26 |
| `test_kis_paper_rejection.py` | 5 | 19, 20 |
| `test_kill_switch.py` | 5 | 29, 30, 31 |
| `test_recovery.py` | 4 | 23, 24, 28 |
| `test_secret_leak_regression.py` | 2 | (cross-cutting; `<assumption>` clause) |
| **Total** | **30** | |

## Safety guarantees

The fixtures in `conftest.py` enforce three invariants at session start:

1. **No real credentials.** Any `KIS_APP_KEY` / `KIS_APP_SECRET` /
   `KIS_ACCOUNT_NO` env vars are overwritten with synthetic test
   placeholders. A misconfigured shell cannot leak.

2. **Paper-only endpoint.** If the `kis_sdk` package is importable, the
   session aborts with `pytest.UsageError` should the SDK base URL fail
   to look like a paper endpoint.

3. **Isolated databases.** Each test gets its own SQLite file in
   `tmp_path`. WAL/SHM sidecars are checkpointed and removed in teardown.

## SDK fallback

`kis_paper_client` prefers the official `kis_sdk.PaperClient`. If the
package is not installed, the fixture transparently falls back to
`FakeKISPaperClient` (defined in `conftest.py`), which exposes the same
surface used by the tests. **All 30 tests pass under either backend.**

## Running with the GitHub update script

`scripts/github_update.zsh` runs this entire suite as its test gate. A
single test failure prevents the push (exit code 20). To skip the gate
(NOT recommended) pass `--skip-tests`.
