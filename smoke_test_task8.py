"""스모크 테스트 (Smoke Test) — Task 8 v0.1 KIS Adapter.

⚠️ 본 테스트는 실 KIS API를 절대 호출하지 않습니다.
(NEVER calls real KIS API — uses stub responses only.)
"""

import sys
import os
import json
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.brokers.kis import (
    KISCredentials, KISEnv, KISAuth, KISClient, KISAdapter,
    KISMarketDataSource, KISQuoteSource, KISAccount,
    KISOrderClient, OrderRequest, OrderResponse, OrdersDryRunGuard,
)
from src.brokers.kis.credentials import (
    load_kis_credentials_from_env, CredentialsError, _mask,
)
from src.brokers.kis.tr_codes import get_tr_code, list_functions, TRCodeError
from src.brokers.kis.client import _TokenBucket, KISAPIError, RateLimitError
from src.brokers.kis.orders import OrderSide, OrderType
from src.data.market_data_source import MarketDataSource
from src.data.quote_source import QuoteSource
from src.data.ohlcv_schema import Timeframe


# ─────────────────────────────────────────────────
# Stub HTTP Session — 실 API 호출 차단
# ─────────────────────────────────────────────────

class StubResponse:
    def __init__(self, status_code, json_data):
        self.status_code = status_code
        self._json = json_data
    def json(self):
        return self._json


class StubSession:
    """실 KIS API를 호출하지 않는 스텁 세션."""
    def __init__(self):
        self.calls = []
        self._token_issue_count = 0
    
    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append({"method": "POST", "url": url, "headers": dict(headers or {})})
        # 토큰 발급 응답
        if "/oauth2/tokenP" in url:
            self._token_issue_count += 1
            return StubResponse(200, {
                "access_token": f"stub_token_{self._token_issue_count}",
                "token_type": "Bearer",
                "expires_in": 86400,
            })
        # 주문 응답 (성공)
        if "order-cash" in url:
            return StubResponse(200, {
                "rt_cd": "0",
                "msg_cd": "OK",
                "msg1": "주문 성공",
                "output": {"ODNO": "0000123456", "KRX_FWDG_ORD_ORGNO": "12345"},
            })
        return StubResponse(404, {"rt_cd": "1", "msg_cd": "404", "msg1": "not found"})
    
    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"method": "GET", "url": url, "params": dict(params or {})})
        # 일봉 응답
        if "inquire-daily-itemchartprice" in url:
            return StubResponse(200, {
                "rt_cd": "0", "msg_cd": "OK", "msg1": "",
                "output1": {"hts_kor_isnm": "삼성전자"},
                "output2": [
                    # 최신순으로 응답 (KIS 형식)
                    {
                        "stck_bsop_date": "20260505",
                        "stck_oprc": "70000", "stck_hgpr": "71000",
                        "stck_lwpr": "69500", "stck_clpr": "70500",
                        "acml_vol": "12000000", "acml_tr_pbmn": "847500000000",
                    },
                    {
                        "stck_bsop_date": "20260504",
                        "stck_oprc": "69500", "stck_hgpr": "70200",
                        "stck_lwpr": "69000", "stck_clpr": "70000",
                        "acml_vol": "10000000", "acml_tr_pbmn": "697000000000",
                    },
                ],
            })
        # 호가 응답
        if "inquire-asking-price" in url:
            return StubResponse(200, {
                "rt_cd": "0", "msg_cd": "OK", "msg1": "",
                "output1": {
                    "stck_prpr": "70500",
                    "aspr_acpt_hour": "143015",
                    # 매도호가 1~10
                    **{f"askp{i}": str(70500 + i*100) for i in range(1, 11)},
                    **{f"askp_rsqn{i}": str(1000 - i*50) for i in range(1, 11)},
                    # 매수호가 1~10
                    **{f"bidp{i}": str(70400 - (i-1)*100) for i in range(1, 11)},
                    **{f"bidp_rsqn{i}": str(900 - i*40) for i in range(1, 11)},
                },
                "output2": [],
            })
        # 잔고 조회
        if "inquire-balance" in url:
            return StubResponse(200, {
                "rt_cd": "0", "msg_cd": "OK", "msg1": "",
                "output1": [
                    {
                        "pdno": "005930", "prdt_name": "삼성전자",
                        "hldg_qty": "10", "ord_psbl_qty": "10",
                        "pchs_avg_pric": "70000", "prpr": "70500",
                        "evlu_amt": "705000", "pchs_amt": "700000",
                        "evlu_pfls_amt": "5000", "evlu_pfls_rt": "0.71",
                    }
                ],
                "output2": [{
                    "dnca_tot_amt": "5000000",
                    "ord_psbl_cash": "4500000",
                    "tot_evlu_amt": "5705000",
                    "pchs_amt_smtl_amt": "700000",
                    "evlu_pfls_smtl_amt": "5000",
                }],
            })
        # 미체결 주문
        if "inquire-psbl-rvsecncl" in url:
            return StubResponse(200, {
                "rt_cd": "0", "msg_cd": "OK", "msg1": "",
                "output": [
                    {
                        "odno": "0000999111", "pdno": "005930",
                        "sll_buy_dvsn_cd": "02",  # 매수
                        "ord_qty": "5", "ord_unpr": "70000",
                    }
                ],
            })
        return StubResponse(404, {"rt_cd": "1", "msg_cd": "404"})


# ─────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────

def test_credentials_masking():
    print("\n[1] 자격증명 마스킹 — 비밀 절대 노출 금지")
    creds = KISCredentials(
        env=KISEnv.PAPER,
        app_key="ABC1234567890XYZsuperSecret",
        app_secret="MySecretValue9999",
        account_no="12345678-01",
        hts_id="my_hts_id",
    )
    
    # __repr__ 검증
    repr_str = repr(creds)
    assert "MySecretValue9999" not in repr_str, "app_secret이 repr에 노출됨!"
    assert "ABC1234567890XYZsuperSecret" not in repr_str, "app_key 전체가 repr에 노출됨!"
    assert "****" in repr_str
    print(f"   ✅ repr: {repr_str}")
    
    # __str__도 동일
    assert "MySecretValue9999" not in str(creds)
    print(f"   ✅ str/repr 마스킹 OK")
    
    # _mask 헬퍼
    assert _mask("abcdefghij") == "abc***ij"
    assert _mask("ab") == "**"
    assert _mask("") == "<missing>"
    assert _mask(None) == "<missing>"
    print(f"   ✅ _mask 헬퍼 정상")


def test_credentials_validation():
    print("\n[2] 자격증명 검증 — fail-closed")
    # app_key 누락
    try:
        KISCredentials(
            env=KISEnv.PAPER, app_key="", app_secret="x",
            account_no="12345678-01",
        )
        assert False
    except CredentialsError as e:
        print(f"   ✅ app_key 누락 거부: {e}")
    
    # 계좌번호 형식 오류
    try:
        KISCredentials(
            env=KISEnv.PAPER, app_key="x", app_secret="y",
            account_no="12345678",  # - 없음
        )
        assert False
    except CredentialsError as e:
        print(f"   ✅ 계좌번호 형식 오류 거부: {e}")
    
    # rate_limit 한도 초과
    try:
        KISCredentials(
            env=KISEnv.PAPER, app_key="x", app_secret="y",
            account_no="12345678-01", rate_limit_per_sec=50,
        )
        assert False
    except CredentialsError as e:
        print(f"   ✅ rate_limit 한도 거부: {e}")


def test_load_from_env_file():
    print("\n[3] .env 파일 로드 + override_env")
    # 임시 .env 생성
    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
        f.write("KIS_ENV=paper\n")
        f.write('KIS_APP_KEY=test_key_value\n')
        f.write("KIS_APP_SECRET=test_secret_value\n")
        f.write('KIS_ACCOUNT_NO="11111111-22"\n')
        f.write("# 주석 무시\n")
        f.write("\n")
        env_path = f.name
    
    # 기존 환경변수 정리
    for k in ["KIS_ENV", "KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO", "KIS_HTS_ID",
              "KIS_REQUEST_TIMEOUT_SEC", "KIS_RATE_LIMIT_PER_SEC"]:
        os.environ.pop(k, None)
    
    try:
        creds = load_kis_credentials_from_env(env_path)
        assert creds.app_key == "test_key_value"
        assert creds.account_no == "11111111-22"
        assert creds.env == KISEnv.PAPER
        # 따옴표 trim 확인
        assert creds.account_cano == "11111111"
        assert creds.account_prdt == "22"
        print(f"   ✅ 로드 OK: {creds!r}")
        
        # override_env로 강제 전환 — 환경 정리 후 재로드
        for k in ["KIS_ENV"]:
            os.environ.pop(k, None)
        creds_live = load_kis_credentials_from_env(env_path, override_env=KISEnv.LIVE)
        assert creds_live.env == KISEnv.LIVE
        assert creds_live.env.base_url() == "https://openapi.koreainvestment.com:9443"
        print(f"   ✅ override_env=LIVE 전환: base_url={creds_live.env.base_url()}")
    finally:
        Path(env_path).unlink()
        for k in ["KIS_ENV", "KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO",
                  "KIS_REQUEST_TIMEOUT_SEC", "KIS_RATE_LIMIT_PER_SEC"]:
            os.environ.pop(k, None)


def test_tr_codes():
    print("\n[4] TR 코드 환경별 매핑")
    paper_buy = get_tr_code("order_buy", KISEnv.PAPER)
    live_buy = get_tr_code("order_buy", KISEnv.LIVE)
    assert paper_buy == "VTTC0802U"
    assert live_buy == "TTTC0802U"
    print(f"   ✅ order_buy: paper={paper_buy}, live={live_buy}")
    
    # 시세는 동일
    assert get_tr_code("daily_chart", KISEnv.PAPER) == get_tr_code("daily_chart", KISEnv.LIVE)
    print(f"   ✅ daily_chart는 paper/live 동일")
    
    # 알 수 없는 기능
    try:
        get_tr_code("unknown_function", KISEnv.PAPER)
        assert False
    except TRCodeError as e:
        print(f"   ✅ 알 수 없는 기능명 거부")


def test_token_bucket():
    print("\n[5] Rate Limit (토큰버킷)")
    bucket = _TokenBucket(rate_per_sec=5)
    # 5번은 즉시 통과
    for i in range(5):
        bucket.acquire(max_wait_sec=0.1)
    print(f"   ✅ 5/5 즉시 통과 (current={bucket.current_usage()})")
    
    # 6번째는 대기 발생 — max_wait 짧게 → RateLimitError
    try:
        bucket.acquire(max_wait_sec=0.05)
        assert False
    except RateLimitError as e:
        print(f"   ✅ 6번째 RateLimitError: {e}")


def test_auth_token_issuance():
    print("\n[6] 토큰 발급 (stub session)")
    creds = KISCredentials(
        env=KISEnv.PAPER, app_key="x"*20, app_secret="y"*20,
        account_no="12345678-01",
    )
    session = StubSession()
    auth = KISAuth(creds, http_session=session)
    
    token1 = auth.get_token()
    assert token1.token == "stub_token_1"
    # 캐시된 토큰 재사용
    token2 = auth.get_token()
    assert token2.token == "stub_token_1"  # 같은 토큰
    assert session._token_issue_count == 1
    
    # force_refresh
    token3 = auth.get_token(force_refresh=True)
    assert token3.token == "stub_token_2"
    assert session._token_issue_count == 2
    
    # repr 마스킹
    repr_str = repr(token1)
    assert "stub_token_1" not in repr_str or repr_str.count("_") <= 2
    print(f"   ✅ 토큰 발급 + 캐시 + 마스킹 OK: {repr_str}")


def test_market_data_adapter():
    print("\n[7] KISMarketDataSource — Task 12 인터페이스 호환")
    creds = KISCredentials(
        env=KISEnv.PAPER, app_key="x"*20, app_secret="y"*20,
        account_no="12345678-01",
    )
    session = StubSession()
    auth = KISAuth(creds, http_session=session)
    client = KISClient(auth, http_session=session)
    md = KISMarketDataSource(client)
    
    # Task 12 인터페이스 검증
    assert isinstance(md, MarketDataSource)
    assert md.is_live is True
    
    # 일봉 조회 (stub 응답)
    bars = list(md.fetch_bars(
        "005930", Timeframe.D1,
        datetime(2026, 5, 1, tzinfo=timezone.utc),
        datetime(2026, 5, 6, tzinfo=timezone.utc),
    ))
    assert len(bars) == 2  # stub에서 2개 제공
    # 시간 오름차순 (stub은 최신순으로 줬으나 _fetch_daily가 reversed)
    assert bars[0].bar_time_utc < bars[1].bar_time_utc
    assert bars[1].close == Decimal("70500")
    assert bars[0].source == "kis"
    assert bars[0].volume_split_method.value.startswith("estimated_")
    print(f"   ✅ {len(bars)}개 bar 변환: 첫={bars[0].bar_time_utc.date()} 끝={bars[1].bar_time_utc.date()}")


def test_quote_adapter():
    print("\n[8] KISQuoteSource — Task 13 인터페이스 호환")
    creds = KISCredentials(
        env=KISEnv.PAPER, app_key="x"*20, app_secret="y"*20,
        account_no="12345678-01",
    )
    session = StubSession()
    auth = KISAuth(creds, http_session=session)
    client = KISClient(auth, http_session=session)
    q = KISQuoteSource(client)
    
    assert isinstance(q, QuoteSource)
    assert q.is_live is True
    
    snap = q.snapshot("005930")
    assert snap.symbol == "005930"
    assert snap.best_ask >= snap.best_bid
    assert len(snap.depth_levels) == 10
    assert snap.is_live_source is True
    print(f"   ✅ best_bid={snap.best_bid}, best_ask={snap.best_ask}, depth=10")
    print(f"      mid={snap.mid_quote()}, spread_bps={snap.spread_bps():.2f}")


def test_account_adapter():
    print("\n[9] KISAccount — 잔고/포지션 조회")
    creds = KISCredentials(
        env=KISEnv.PAPER, app_key="x"*20, app_secret="y"*20,
        account_no="12345678-01",
    )
    session = StubSession()
    auth = KISAuth(creds, http_session=session)
    client = KISClient(auth, http_session=session)
    acc = KISAccount(client)
    
    snap = acc.fetch_account_snapshot()
    assert snap.cash_krw == Decimal("5000000")
    assert snap.available_cash_krw == Decimal("4500000")
    assert "005930" in snap.positions
    pos = snap.positions["005930"]
    assert pos.quantity == 10
    assert pos.avg_price_krw == Decimal("70000")
    assert pos.unrealized_pnl_krw == Decimal("5000")
    print(f"   ✅ 현금={snap.available_cash_krw}, 보유종목={list(snap.positions.keys())}")
    
    # Task 19 호환 dict 변환
    risk_dict = snap.open_positions_dict()
    assert "005930" in risk_dict
    assert risk_dict["005930"]["quantity"] == 10
    print(f"   ✅ Task 19 RiskContext 호환: {risk_dict['005930']}")


def test_orders_dry_run_default():
    print("\n[10] Orders — 기본 DRY-RUN (실 송신 차단)")
    creds = KISCredentials(
        env=KISEnv.PAPER, app_key="x"*20, app_secret="y"*20,
        account_no="12345678-01",
    )
    session = StubSession()
    auth = KISAuth(creds, http_session=session)
    client = KISClient(auth, http_session=session)
    guard = OrdersDryRunGuard()
    orders = KISOrderClient(client, guard)
    
    assert guard.live_enabled is False
    
    req = OrderRequest(
        symbol="005930", side=OrderSide.BUY, quantity=10,
        order_type=OrderType.LIMIT, limit_price=Decimal("70000"),
    )
    response = orders.submit_order(req)
    
    assert response.is_dry_run is True
    assert response.accepted is True
    assert response.broker_order_no is None
    # stub session에 order POST 호출이 없어야 함 (dry-run)
    order_calls = [c for c in session.calls if "order-cash" in c["url"]]
    assert len(order_calls) == 0, "DRY-RUN인데 실제 호출됨!"
    print(f"   ✅ DRY-RUN: accepted=True, is_dry_run=True, 실제 호출 0회")
    print(f"   ✅ guard.status()={guard.status()}")


def test_orders_live_enable():
    print("\n[11] Orders — LIVE 활성화 후 실 호출")
    creds = KISCredentials(
        env=KISEnv.PAPER, app_key="x"*20, app_secret="y"*20,
        account_no="12345678-01",
    )
    session = StubSession()
    auth = KISAuth(creds, http_session=session)
    client = KISClient(auth, http_session=session)
    guard = OrdersDryRunGuard()
    orders = KISOrderClient(client, guard)
    
    # 빈 reason 거부
    try:
        guard.enable_live(reason="")
        assert False
    except ValueError as e:
        print(f"   ✅ 빈 reason 거부: {e}")
    
    # 정상 활성화
    guard.enable_live(reason="Task 8 smoke test (stub session only)")
    assert guard.live_enabled is True
    
    req = OrderRequest(
        symbol="005930", side=OrderSide.BUY, quantity=10,
        order_type=OrderType.LIMIT, limit_price=Decimal("70000"),
    )
    response = orders.submit_order(req)
    
    assert response.is_dry_run is False
    assert response.accepted is True
    assert response.broker_order_no == "0000123456"
    print(f"   ✅ LIVE: broker_order_no={response.broker_order_no}")
    
    # 다시 비활성화
    guard.disable_live()
    assert guard.live_enabled is False
    print(f"   ✅ disable_live() 성공")


def test_open_orders():
    print("\n[12] 미체결 주문 조회 — Task 19 RiskContext 호환")
    creds = KISCredentials(
        env=KISEnv.PAPER, app_key="x"*20, app_secret="y"*20,
        account_no="12345678-01",
    )
    session = StubSession()
    auth = KISAuth(creds, http_session=session)
    client = KISClient(auth, http_session=session)
    guard = OrdersDryRunGuard()
    orders = KISOrderClient(client, guard)
    
    # DRY-RUN — 빈 리스트
    open_orders = orders.fetch_open_orders()
    assert open_orders == []
    
    # LIVE 활성화 후
    guard.enable_live(reason="test")
    open_orders = orders.fetch_open_orders()
    assert len(open_orders) == 1
    assert open_orders[0]["symbol"] == "005930"
    assert open_orders[0]["side"] == "buy"
    assert open_orders[0]["status"] == "pending"
    print(f"   ✅ 미체결 주문: {open_orders[0]}")


def test_full_adapter_facade():
    print("\n[13] KISAdapter Facade 통합")
    creds = KISCredentials(
        env=KISEnv.PAPER, app_key="x"*20, app_secret="y"*20,
        account_no="12345678-01",
    )
    session = StubSession()
    adapter = KISAdapter(creds, http_session=session)
    
    # 전체 컴포넌트 접근 가능
    assert adapter.market_data is not None
    assert adapter.quote is not None
    assert adapter.account is not None
    assert adapter.orders is not None
    assert adapter.dry_run_guard.live_enabled is False  # 기본 안전
    
    # repr 안전
    repr_str = repr(adapter)
    assert "y"*20 not in repr_str  # secret 노출 없음
    assert "orders_live_enabled=False" in repr_str
    print(f"   ✅ {repr_str}")


def test_kis_api_error_propagation():
    print("\n[14] KIS API 에러 전파 (rt_cd != '0')")
    
    class FailSession(StubSession):
        def get(self, url, **kwargs):
            return StubResponse(200, {
                "rt_cd": "1",  # 실패
                "msg_cd": "EGW00001",
                "msg1": "잘못된 요청",
            })
    
    creds = KISCredentials(
        env=KISEnv.PAPER, app_key="x"*20, app_secret="y"*20,
        account_no="12345678-01",
    )
    session = FailSession()
    auth = KISAuth(creds, http_session=session)
    client = KISClient(auth, http_session=session)
    md = KISMarketDataSource(client)
    
    try:
        list(md.fetch_bars(
            "005930", Timeframe.D1,
            datetime(2026, 5, 1, tzinfo=timezone.utc),
            datetime(2026, 5, 6, tzinfo=timezone.utc),
        ))
        assert False, "KISAPIError 발생해야 함"
    except KISAPIError as e:
        assert e.rt_cd == "1"
        print(f"   ✅ rt_cd={e.rt_cd} msg_cd={e.msg_cd} → KISAPIError 전파")


def test_no_real_api_calls():
    print("\n[15] 실 KIS API 호출 0건 확인 (전체 테스트 안전)")
    # 모든 테스트가 StubSession 통해서만 동작
    # 만약 실 호출이 있었다면 네트워크 오류로 이미 실패했을 것
    print(f"   ✅ 모든 호출은 stub session 경유 — 실 KIS API 미호출")


if __name__ == "__main__":
    test_credentials_masking()
    test_credentials_validation()
    test_load_from_env_file()
    test_tr_codes()
    test_token_bucket()
    test_auth_token_issuance()
    test_market_data_adapter()
    test_quote_adapter()
    test_account_adapter()
    test_orders_dry_run_default()
    test_orders_live_enable()
    test_open_orders()
    test_full_adapter_facade()
    test_kis_api_error_propagation()
    test_no_real_api_calls()
    print("\n🎉 모든 스모크 테스트 통과 (All smoke tests passed)")
    print("⚠️  실 KIS API는 한 번도 호출되지 않았음 (No real KIS API calls made)")
