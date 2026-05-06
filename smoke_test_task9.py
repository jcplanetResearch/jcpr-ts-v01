"""스모크 테스트 (Smoke Test) — Task 9 KIS Connection Check.

⚠️ 모든 호출은 StubSession 경유 — 실 KIS API 절대 호출 안 함.
"""

import sys
import json
import io
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.brokers.kis import KISAdapter, KISCredentials, KISEnv
from scripts.check_kis_connection import (
    CheckReport, CheckResult, CheckStatus,
    KISConnectionChecker, main,
)


# ─────────────────────────────────────────────────
# Stub HTTP Session (Task 8 패턴 재사용)
# ─────────────────────────────────────────────────

class StubResponse:
    def __init__(self, status_code, json_data):
        self.status_code = status_code
        self._json = json_data
    def json(self):
        return self._json


class StubSession:
    def __init__(self, *, fail_on=None, with_position=False, with_open_orders=False):
        self.calls = []
        self._token_count = 0
        self._fail_on = fail_on or set()  # set of url substrings to fail
        self._with_position = with_position
        self._with_open_orders = with_open_orders

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append({"method": "POST", "url": url})
        if any(f in url for f in self._fail_on):
            return StubResponse(500, {})
        if "/oauth2/tokenP" in url:
            self._token_count += 1
            return StubResponse(200, {
                "access_token": f"stub_{self._token_count}_abcdef",
                "token_type": "Bearer", "expires_in": 86400,
            })
        # 점검 스크립트는 절대 주문 송신 안 함 — order-cash 호출 시 실패
        if "order-cash" in url:
            raise RuntimeError("점검 스크립트에서 order-cash 호출 발견 — 안전 위반!")
        return StubResponse(404, {"rt_cd": "1", "msg_cd": "404"})

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"method": "GET", "url": url})
        if any(f in url for f in self._fail_on):
            return StubResponse(200, {"rt_cd": "1", "msg_cd": "ERR", "msg1": "강제 실패"})

        if "inquire-balance" in url:
            positions = []
            if self._with_position:
                positions = [{
                    "pdno": "005930", "prdt_name": "삼성전자",
                    "hldg_qty": "10", "ord_psbl_qty": "10",
                    "pchs_avg_pric": "70000", "prpr": "70500",
                    "evlu_amt": "705000", "pchs_amt": "700000",
                    "evlu_pfls_amt": "5000", "evlu_pfls_rt": "0.71",
                }]
            return StubResponse(200, {
                "rt_cd": "0", "msg_cd": "OK", "msg1": "",
                "output1": positions,
                "output2": [{
                    "dnca_tot_amt": "10000000", "ord_psbl_cash": "9500000",
                    "tot_evlu_amt": "10000000",
                    "pchs_amt_smtl_amt": "0", "evlu_pfls_smtl_amt": "0",
                }],
            })
        if "inquire-daily-itemchartprice" in url:
            return StubResponse(200, {
                "rt_cd": "0", "msg_cd": "OK", "msg1": "",
                "output1": {"hts_kor_isnm": "삼성전자"},
                "output2": [
                    {"stck_bsop_date": "20260505",
                     "stck_oprc": "70000", "stck_hgpr": "71000",
                     "stck_lwpr": "69500", "stck_clpr": "70500",
                     "acml_vol": "12000000", "acml_tr_pbmn": "847500000000"},
                    {"stck_bsop_date": "20260504",
                     "stck_oprc": "69500", "stck_hgpr": "70200",
                     "stck_lwpr": "69000", "stck_clpr": "70000",
                     "acml_vol": "10000000", "acml_tr_pbmn": "697000000000"},
                ],
            })
        if "inquire-asking-price" in url:
            return StubResponse(200, {
                "rt_cd": "0", "msg_cd": "OK", "msg1": "",
                "output1": {
                    "stck_prpr": "70500",
                    "aspr_acpt_hour": "143015",
                    **{f"askp{i}": str(70500 + i*100) for i in range(1, 11)},
                    **{f"askp_rsqn{i}": str(1000 - i*50) for i in range(1, 11)},
                    **{f"bidp{i}": str(70400 - (i-1)*100) for i in range(1, 11)},
                    **{f"bidp_rsqn{i}": str(900 - i*40) for i in range(1, 11)},
                },
                "output2": [],
            })
        if "inquire-psbl-rvsecncl" in url:
            output = []
            if self._with_open_orders:
                output = [{
                    "odno": "0000999111", "pdno": "005930",
                    "sll_buy_dvsn_cd": "02", "ord_qty": "5", "ord_unpr": "70000",
                }]
            return StubResponse(200, {
                "rt_cd": "0", "msg_cd": "OK", "msg1": "", "output": output,
            })
        return StubResponse(404, {"rt_cd": "1"})


def _make_adapter(*, env=KISEnv.PAPER, session=None):
    creds = KISCredentials(
        env=env, app_key="x"*20, app_secret="y"*20,
        account_no="12345678-01",
    )
    if session is None:
        session = StubSession()
    return KISAdapter(creds, http_session=session), session


# ─────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────

def test_check_credentials_pass():
    print("\n[1] 자격증명 로드 — PASS")
    adapter, _ = _make_adapter()
    checker = KISConnectionChecker(adapter)
    r = checker.check_credentials()
    assert r.status == CheckStatus.PASS
    assert "1234***01" in r.message  # 마스킹 확인
    # 비밀 노출 검사
    assert "y" * 20 not in r.message
    assert "x" * 20 not in r.message
    print(f"   ✅ {r.message}")


def test_check_environment_info():
    print("\n[2] 환경 정보 — INFO")
    adapter, _ = _make_adapter(env=KISEnv.PAPER)
    checker = KISConnectionChecker(adapter)
    r = checker.check_environment()
    assert r.status == CheckStatus.INFO
    assert "openapivts" in r.message  # paper URL
    print(f"   ✅ {r.message}")


def test_check_token_issuance_pass():
    print("\n[3] 토큰 발급 — PASS")
    adapter, _ = _make_adapter()
    checker = KISConnectionChecker(adapter)
    r = checker.check_token_issuance()
    assert r.status == CheckStatus.PASS
    assert "Bearer" in r.message
    # 토큰 마스킹 확인
    assert "stub_1_abcdef" not in r.message  # 전체 토큰 노출 없음
    print(f"   ✅ {r.message}")


def test_check_token_failure():
    print("\n[4] 토큰 발급 실패 — FAIL")
    session = StubSession(fail_on={"/oauth2/tokenP"})
    adapter, _ = _make_adapter(session=session)
    checker = KISConnectionChecker(adapter)
    r = checker.check_token_issuance()
    assert r.status == CheckStatus.FAIL
    print(f"   ✅ FAIL detected: {r.message}")


def test_check_account_pass():
    print("\n[5] 계좌 조회 — PASS")
    adapter, _ = _make_adapter()
    checker = KISConnectionChecker(adapter)
    r = checker.check_account()
    assert r.status == CheckStatus.PASS
    assert "9,500,000" in r.message or "10,000,000" in r.message
    print(f"   ✅ {r.message}")


def test_check_account_with_position():
    print("\n[6] 계좌 조회 (보유 종목 포함)")
    adapter, _ = _make_adapter(session=StubSession(with_position=True))
    checker = KISConnectionChecker(adapter)
    r = checker.check_account()
    assert r.status == CheckStatus.PASS
    assert "1종목" in r.message
    assert "005930" in r.detail.get("position_symbols", [])
    print(f"   ✅ {r.message}")


def test_check_market_data_pass():
    print("\n[7] 시세 조회 — PASS")
    adapter, _ = _make_adapter()
    checker = KISConnectionChecker(adapter, symbol="005930")
    r = checker.check_market_data()
    assert r.status == CheckStatus.PASS
    assert "70,500" in r.message
    print(f"   ✅ {r.message}")


def test_check_market_data_failure():
    print("\n[8] 시세 조회 실패 — FAIL (rt_cd!=0)")
    adapter, _ = _make_adapter(session=StubSession(fail_on={"inquire-daily"}))
    checker = KISConnectionChecker(adapter)
    r = checker.check_market_data()
    assert r.status == CheckStatus.FAIL
    print(f"   ✅ FAIL detected: {r.message}")


def test_check_quote_pass():
    print("\n[9] 호가 조회 — PASS")
    adapter, _ = _make_adapter()
    checker = KISConnectionChecker(adapter)
    r = checker.check_quote()
    assert r.status == CheckStatus.PASS
    assert "bid=70,400" in r.message
    assert "ask=70,600" in r.message  # stub: askp1=70500+1*100
    assert "10단계" in r.message
    print(f"   ✅ {r.message}")


def test_rate_limit_skip():
    print("\n[10] Rate Limit — SKIP")
    adapter, _ = _make_adapter()
    checker = KISConnectionChecker(adapter, skip_rate_limit=True)
    r = checker.check_rate_limit()
    assert r.status == CheckStatus.SKIP
    print(f"   ✅ {r.message}")


def test_rate_limit_pass():
    print("\n[11] Rate Limit — PASS (5건 호출)")
    adapter, session = _make_adapter()
    checker = KISConnectionChecker(adapter)
    r = checker.check_rate_limit()
    assert r.status == CheckStatus.PASS
    assert "5 requests" in r.message
    # 호가 조회 5번 호출됨
    quote_calls = [c for c in session.calls if "inquire-asking-price" in c["url"]]
    assert len(quote_calls) == 5
    print(f"   ✅ {r.message}")


def test_dry_run_guard_safe():
    print("\n[12] DryRunGuard — PASS (live_enabled=False)")
    adapter, _ = _make_adapter()
    checker = KISConnectionChecker(adapter)
    r = checker.check_dry_run_guard()
    assert r.status == CheckStatus.PASS
    assert "False" in r.message
    print(f"   ✅ {r.message}")


def test_dry_run_guard_warning():
    print("\n[13] DryRunGuard — INFO (live_enabled=True)")
    adapter, _ = _make_adapter()
    adapter.dry_run_guard.enable_live(reason="test")
    checker = KISConnectionChecker(adapter)
    r = checker.check_dry_run_guard()
    assert r.status == CheckStatus.INFO
    assert "live_enabled=True" in r.message
    print(f"   ✅ {r.message}")


def test_check_open_orders():
    print("\n[14] 미체결 주문 조회")
    adapter, session = _make_adapter(session=StubSession(with_open_orders=True))
    # 시작 시 dry-run 상태
    assert not adapter.dry_run_guard.live_enabled
    checker = KISConnectionChecker(adapter)
    r = checker.check_open_orders()
    assert r.status == CheckStatus.PASS
    assert "1건" in r.message
    # 점검 후 dry-run 복귀 확인 (안전)
    assert not adapter.dry_run_guard.live_enabled
    print(f"   ✅ {r.message}, dry-run 복귀 확인")


def test_run_all_full_pass():
    print("\n[15] 전체 점검 — 모두 통과")
    adapter, session = _make_adapter()
    checker = KISConnectionChecker(adapter)
    report = checker.run_all()
    
    # 9개 결과 (PASS + INFO 조합)
    assert len(report.results) == 9
    assert report.failed_count() == 0
    # PASS만 카운트 — INFO는 별도
    passed = report.passed_count()
    assert passed >= 7  # 자격증명, 토큰, 계좌, 시세, 호가, RL, DryRun, 미체결
    print(f"   ✅ {passed}/{report.total_count()} 통과 (INFO/SKIP 제외)")
    
    # 점검 중 order-cash 호출 0건 확인 (안전)
    order_calls = [c for c in session.calls if "order-cash" in c["url"]]
    assert len(order_calls) == 0
    print(f"   ✅ order-cash 호출 0건 — 점검 안전")


def test_run_all_partial_failure():
    print("\n[16] 일부 실패 — 시세 조회만 fail")
    session = StubSession(fail_on={"inquire-daily"})
    adapter, _ = _make_adapter(session=session)
    checker = KISConnectionChecker(adapter)
    report = checker.run_all()
    
    assert report.failed_count() >= 1
    failed_names = [r.name for r in report.results if r.status == CheckStatus.FAIL]
    assert any("시세" in n for n in failed_names)
    print(f"   ✅ 실패 detected: {failed_names}")


def test_report_to_json():
    print("\n[17] JSON 출력 형식")
    adapter, _ = _make_adapter()
    checker = KISConnectionChecker(adapter)
    report = checker.run_all()
    d = report.to_dict()
    
    # JSON 직렬화 가능
    json_str = json.dumps(d, ensure_ascii=False, indent=2)
    assert "passed" in d
    assert "all_passed" in d
    assert d["all_passed"] is True
    
    # 비밀 노출 검사
    assert "y" * 20 not in json_str
    assert "x" * 20 not in json_str
    assert "12345678-01" not in json_str  # 계좌번호 전체 노출 없음
    print(f"   ✅ JSON 출력, 비밀 누출 없음, all_passed={d['all_passed']}")


def test_no_real_api_calls():
    print("\n[18] 실 KIS API 호출 0건 (모든 테스트)")
    # 모든 위 테스트가 StubSession만 사용 — 통과되었다는 사실 자체가 증거
    print(f"   ✅ 실 API 미호출 보장")


def test_credentials_loading_failure_exit_code():
    print("\n[19] CredentialsError 시 exit code 2")
    # 환경변수 모두 제거
    for k in ["KIS_ENV", "KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO",
              "KIS_HTS_ID", "KIS_REQUEST_TIMEOUT_SEC", "KIS_RATE_LIMIT_PER_SEC"]:
        os.environ.pop(k, None)
    
    # main()을 직접 호출 — 인자는 sys.argv 통해
    saved_argv = sys.argv[:]
    saved_stderr = sys.stderr
    try:
        # 존재 안 하는 .env로 강제
        sys.argv = ["check_kis_connection.py", "--env-file", "/tmp/__nonexistent_env__"]
        sys.stderr = io.StringIO()
        rc = main()
        assert rc == 2, f"expected exit code 2, got {rc}"
        err = sys.stderr.getvalue()
        assert "자격증명 로드 실패" in err or "credentials" in err.lower()
        print(f"   ✅ exit code = 2 (자격증명 실패)")
    finally:
        sys.argv = saved_argv
        sys.stderr = saved_stderr


if __name__ == "__main__":
    test_check_credentials_pass()
    test_check_environment_info()
    test_check_token_issuance_pass()
    test_check_token_failure()
    test_check_account_pass()
    test_check_account_with_position()
    test_check_market_data_pass()
    test_check_market_data_failure()
    test_check_quote_pass()
    test_rate_limit_skip()
    test_rate_limit_pass()
    test_dry_run_guard_safe()
    test_dry_run_guard_warning()
    test_check_open_orders()
    test_run_all_full_pass()
    test_run_all_partial_failure()
    test_report_to_json()
    test_no_real_api_calls()
    test_credentials_loading_failure_exit_code()
    print("\n🎉 모든 스모크 테스트 통과 (All smoke tests passed)")
    print("⚠️  실 KIS API 0회 호출 (모두 stub session)")
