"""스모크 테스트 (Smoke Test) — Task 24 v0.1 Fill Ingestion."""

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.execution.fills import Fill, FillSide, FillSource
from src.execution.fill_store import FillStore
from src.execution.fill_ingester import FillIngester, IngestionReport
from src.execution.kis_fill_source import KISFillSource
from src.brokers.kis import KISCredentials, KISEnv, KISClient
from src.brokers.kis.auth import KISAuth
from src.data.symbol_master import SymbolMaster


CSV_PATH = Path(__file__).parent / "data" / "reference" / "symbol_master.csv"


# ─────────────────────────────────────────────────
# Stub HTTP Session
# ─────────────────────────────────────────────────

class StubResponse:
    def __init__(self, status_code, json_data):
        self.status_code = status_code
        self._json = json_data
    def json(self):
        return self._json


class StubSession:
    def __init__(self, *, fills_response=None):
        self.calls = []
        self._fills_response = fills_response

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append({"method": "POST", "url": url})
        if "/oauth2/tokenP" in url:
            return StubResponse(200, {
                "access_token": "stub_token", "token_type": "Bearer", "expires_in": 86400,
            })
        return StubResponse(404, {"rt_cd": "1"})

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"method": "GET", "url": url, "params": dict(params or {})})
        if "inquire-daily-ccld" in url and self._fills_response is not None:
            return StubResponse(200, self._fills_response)
        return StubResponse(404, {"rt_cd": "1"})


# ─────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────

def _make_kis_client(*, fills_response=None) -> tuple[KISClient, StubSession]:
    creds = KISCredentials(
        env=KISEnv.PAPER, app_key="x"*20, app_secret="y"*20,
        account_no="12345678-01",
    )
    session = StubSession(fills_response=fills_response)
    auth = KISAuth(creds, http_session=session)
    return KISClient(auth, http_session=session), session


def _make_fill(
    *,
    fill_id="FILL-001",
    broker_order_no="ORD-001",
    client_order_id="exec-001",
    symbol="005930",
    side=FillSide.BUY,
    quantity=10,
    price="70000",
    fee_krw="200",
    tax_krw="0",
    filled_at=None,
    is_partial=False,
):
    return Fill(
        fill_id=fill_id,
        broker_order_no=broker_order_no,
        client_order_id=client_order_id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=Decimal(price),
        fee_krw=Decimal(fee_krw),
        tax_krw=Decimal(tax_krw),
        filled_at_utc=filled_at or datetime(2026, 5, 6, 9, 0, 0, tzinfo=timezone.utc),
        received_at_utc=datetime.now(timezone.utc),
        source="dummy",
        is_partial=is_partial,
    )


# ─────────────────────────────────────────────────
# Fill 데이터 모델 검증
# ─────────────────────────────────────────────────

def test_fill_validation():
    print("\n[1] Fill 검증")
    f = _make_fill()
    assert f.gross_amount_krw() == Decimal("700000")  # 10 * 70000
    assert f.net_amount_krw() == Decimal("-700200")   # 매수 → -gross - fee
    print(f"   ✅ 정상 매수: gross={f.gross_amount_krw()}, net={f.net_amount_krw()}")

    # 매도 — 거래세 포함
    sell = _make_fill(side=FillSide.SELL, fee_krw="200", tax_krw="1500")
    assert sell.net_amount_krw() == Decimal("700000") - Decimal("200") - Decimal("1500")
    print(f"   ✅ 매도 net={sell.net_amount_krw()}")


def test_fill_invalid_inputs():
    print("\n[2] Fill 잘못된 입력 거부")
    # 빈 fill_id
    try:
        _make_fill(fill_id="")
        assert False
    except ValueError as e:
        print(f"   ✅ 빈 fill_id 거부: {e}")

    # 음수 수량
    try:
        _make_fill(quantity=-5)
        assert False
    except ValueError as e:
        print(f"   ✅ 음수 quantity 거부")

    # 음수 가격
    try:
        _make_fill(price="-100")
        assert False
    except ValueError as e:
        print(f"   ✅ 음수 price 거부")

    # 매수에 거래세 (KRX 규정 위반)
    try:
        _make_fill(side=FillSide.BUY, tax_krw="500")
        assert False
    except ValueError as e:
        assert "매수" in str(e) and "거래세" in str(e)
        print(f"   ✅ 매수+거래세 거부: {e}")


def test_fill_tz_aware_required():
    print("\n[3] tz-naive 거부")
    naive_dt = datetime(2026, 5, 6, 9, 0, 0)  # naive
    try:
        Fill(
            fill_id="X", broker_order_no="X", client_order_id="X",
            symbol="005930", side=FillSide.BUY, quantity=1,
            price=Decimal("70000"), fee_krw=Decimal("0"), tax_krw=Decimal("0"),
            filled_at_utc=naive_dt,
            received_at_utc=datetime.now(timezone.utc),
            source="dummy",
        )
        assert False
    except ValueError as e:
        assert "tz-aware" in str(e)
        print(f"   ✅ tz-naive filled_at_utc 거부")


# ─────────────────────────────────────────────────
# FillStore
# ─────────────────────────────────────────────────

def test_fill_store_roundtrip():
    print("\n[4] FillStore — upsert + fetch 왕복")
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = f.name
    try:
        store = FillStore(db_path)
        fills = [
            _make_fill(fill_id="F1", filled_at=datetime(2026, 5, 6, 9, 0, 0, tzinfo=timezone.utc)),
            _make_fill(fill_id="F2", side=FillSide.SELL, price="70500",
                       fee_krw="210", tax_krw="1000",
                       filled_at=datetime(2026, 5, 6, 10, 0, 0, tzinfo=timezone.utc)),
        ]
        n = store.upsert_many(fills)
        assert n == 2
        assert store.count() == 2

        fetched = store.fetch_by_symbol(
            "005930",
            start_utc=datetime(2026, 5, 6, tzinfo=timezone.utc),
            end_utc=datetime(2026, 5, 7, tzinfo=timezone.utc),
        )
        assert len(fetched) == 2
        assert fetched[0].fill_id == "F1"
        assert fetched[1].fill_id == "F2"
        assert fetched[1].side == FillSide.SELL
        assert fetched[1].tax_krw == Decimal("1000")
        print(f"   ✅ {n}개 저장/조회 일치, 시간 오름차순 OK")
    finally:
        Path(db_path).unlink()


def test_fill_store_idempotency():
    print("\n[5] FillStore 멱등성 (같은 fill_id 재upsert)")
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = f.name
    try:
        store = FillStore(db_path)
        f1 = _make_fill(fill_id="F1")
        store.upsert(f1)
        store.upsert(f1)  # 재입력
        store.upsert(f1)
        assert store.count() == 1
        print(f"   ✅ 같은 fill_id 3번 upsert → 1개만 존재")
    finally:
        Path(db_path).unlink()


def test_fill_store_query_by_order():
    print("\n[6] FillStore — broker_order_no / client_order_id 조회")
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = f.name
    try:
        store = FillStore(db_path)
        # 한 주문에 부분 체결 2건
        fills = [
            _make_fill(fill_id="F1", broker_order_no="ORD-A", client_order_id="exec-A",
                       quantity=5, is_partial=True),
            _make_fill(fill_id="F2", broker_order_no="ORD-A", client_order_id="exec-A",
                       quantity=5, is_partial=False),
            _make_fill(fill_id="F3", broker_order_no="ORD-B", client_order_id="exec-B"),
        ]
        store.upsert_many(fills)

        order_a = store.fetch_by_order("ORD-A")
        assert len(order_a) == 2
        client_a = store.fetch_by_client_order_id("exec-A")
        assert len(client_a) == 2
        order_b = store.fetch_by_order("ORD-B")
        assert len(order_b) == 1
        print(f"   ✅ ORD-A: 2건, ORD-B: 1건, exec-A: 2건")
    finally:
        Path(db_path).unlink()


def test_fill_store_raw_preservation():
    print("\n[7] raw 응답 보존 (감사용)")
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = f.name
    try:
        store = FillStore(db_path)
        fill = Fill(
            fill_id="F-RAW", broker_order_no="ORD-X", client_order_id="exec-X",
            symbol="005930", side=FillSide.BUY, quantity=1,
            price=Decimal("70000"), fee_krw=Decimal("0"), tax_krw=Decimal("0"),
            filled_at_utc=datetime(2026, 5, 6, 9, 0, 0, tzinfo=timezone.utc),
            received_at_utc=datetime.now(timezone.utc),
            source="kis",
            raw={"odno": "ORD-X", "kis_field": "value", "한글": "OK"},
        )
        store.upsert(fill)
        fetched = store.fetch_by_symbol("005930")
        assert len(fetched) == 1
        assert fetched[0].raw["kis_field"] == "value"
        assert fetched[0].raw["한글"] == "OK"
        print(f"   ✅ raw 보존 + 한글 정상")
    finally:
        Path(db_path).unlink()


# ─────────────────────────────────────────────────
# KISFillSource (stub)
# ─────────────────────────────────────────────────

def test_kis_fill_source_parsing():
    print("\n[8] KISFillSource — 응답 파싱")
    fills_resp = {
        "rt_cd": "0", "msg_cd": "OK", "msg1": "",
        "output1": [
            {
                "ord_dt": "20260506", "ord_tmd": "090015",
                "odno": "0000123456",
                "sll_buy_dvsn_cd": "02",  # 매수
                "pdno": "005930",
                "tot_ccld_qty": "10",
                "ccld_unpr": "70100",
                "tot_ccld_amt": "701000",
                "cmsn_smtl": "200",
                "tlex_smtl": "200",
                "rmn_qty": "0",
                "ord_qty": "10",
            },
            {
                "ord_dt": "20260506", "ord_tmd": "100022",
                "odno": "0000123457",
                "sll_buy_dvsn_cd": "01",  # 매도
                "pdno": "005930",
                "tot_ccld_qty": "5",
                "ccld_unpr": "70500",
                "cmsn_smtl": "100",
                "tlex_smtl": "1100",  # 100 fee + 1000 tax
                "rmn_qty": "0",
            },
        ]
    }
    client, _ = _make_kis_client(fills_response=fills_resp)
    source = KISFillSource(client)
    assert source.is_live is True

    fills = source.fetch_fills_since(
        datetime(2026, 5, 6, tzinfo=timezone.utc)
    )
    assert len(fills) == 2

    f1, f2 = fills
    assert f1.symbol == "005930"
    assert f1.side == FillSide.BUY
    assert f1.quantity == 10
    assert f1.price == Decimal("70100")
    assert f1.fee_krw == Decimal("200")
    assert f1.tax_krw == Decimal("0")  # 매수 — 거래세 없음
    assert not f1.is_partial

    assert f2.side == FillSide.SELL
    assert f2.tax_krw == Decimal("1000")  # tlex - cmsn = 1100 - 100
    print(f"   ✅ 매수 fill: qty={f1.quantity}, price={f1.price}")
    print(f"   ✅ 매도 fill: tax={f2.tax_krw} (tlex - cmsn)")


def test_kis_fill_source_skip_no_fill():
    print("\n[9] KISFillSource — 미체결 응답 skip")
    fills_resp = {
        "rt_cd": "0", "msg_cd": "OK", "msg1": "",
        "output1": [
            {
                "ord_dt": "20260506", "ord_tmd": "090015",
                "odno": "0000123456", "sll_buy_dvsn_cd": "02", "pdno": "005930",
                "tot_ccld_qty": "0",  # 미체결
                "rmn_qty": "10", "ord_qty": "10",
            },
        ]
    }
    client, _ = _make_kis_client(fills_response=fills_resp)
    source = KISFillSource(client)
    fills = source.fetch_fills_since(datetime(2026, 5, 6, tzinfo=timezone.utc))
    assert len(fills) == 0
    print(f"   ✅ 미체결 0개")


def test_kis_fill_source_partial():
    print("\n[10] KISFillSource — 부분 체결")
    fills_resp = {
        "rt_cd": "0", "msg_cd": "OK", "msg1": "",
        "output1": [
            {
                "ord_dt": "20260506", "ord_tmd": "090015",
                "odno": "0000999", "sll_buy_dvsn_cd": "02", "pdno": "005930",
                "tot_ccld_qty": "5",
                "ccld_unpr": "70100",
                "cmsn_smtl": "100",
                "rmn_qty": "5",  # 5 미체결 → 부분
                "ord_qty": "10",
            },
        ]
    }
    client, _ = _make_kis_client(fills_response=fills_resp)
    source = KISFillSource(client)
    fills = source.fetch_fills_since(datetime(2026, 5, 6, tzinfo=timezone.utc))
    assert len(fills) == 1
    assert fills[0].is_partial is True
    print(f"   ✅ 부분체결 감지: rmn_qty>0 → is_partial=True")


# ─────────────────────────────────────────────────
# FillIngester
# ─────────────────────────────────────────────────

def test_ingester_basic():
    print("\n[11] FillIngester — 정상 수집")
    fills_resp = {
        "rt_cd": "0", "msg_cd": "OK", "msg1": "",
        "output1": [
            {
                "ord_dt": "20260506", "ord_tmd": "090015",
                "odno": "0000123", "sll_buy_dvsn_cd": "02", "pdno": "005930",
                "tot_ccld_qty": "10", "ccld_unpr": "70100",
                "cmsn_smtl": "200", "rmn_qty": "0",
            },
        ]
    }
    client, _ = _make_kis_client(fills_response=fills_resp)
    source = KISFillSource(client)

    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = f.name
    try:
        store = FillStore(db_path)
        sm = SymbolMaster.from_csv(CSV_PATH)
        ingester = FillIngester(source, store, symbol_master=sm)

        report = ingester.ingest_since(datetime(2026, 5, 6, tzinfo=timezone.utc))
        assert report.fetched == 1
        assert report.stored == 1
        assert report.duplicates == 0
        assert report.error is None
        print(f"   ✅ {report}")

        # 재실행 → 멱등 (duplicate 1)
        report2 = ingester.ingest_since(datetime(2026, 5, 6, tzinfo=timezone.utc))
        assert report2.stored == 0
        assert report2.duplicates == 1
        print(f"   ✅ 재실행 멱등: {report2}")
    finally:
        Path(db_path).unlink()


def test_ingester_unknown_symbol_rejected():
    print("\n[12] FillIngester — 미상장 종목 거부")
    fills_resp = {
        "rt_cd": "0", "msg_cd": "OK", "msg1": "",
        "output1": [
            {
                "ord_dt": "20260506", "ord_tmd": "090015",
                "odno": "0000123", "sll_buy_dvsn_cd": "02",
                "pdno": "999999",  # 미상장
                "tot_ccld_qty": "10", "ccld_unpr": "100",
                "cmsn_smtl": "0", "rmn_qty": "0",
            },
        ]
    }
    client, _ = _make_kis_client(fills_response=fills_resp)
    source = KISFillSource(client)

    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = f.name
    try:
        store = FillStore(db_path)
        sm = SymbolMaster.from_csv(CSV_PATH)
        ingester = FillIngester(source, store, symbol_master=sm)

        report = ingester.ingest_since(datetime(2026, 5, 6, tzinfo=timezone.utc))
        assert report.rejected == 1
        assert report.stored == 0
        print(f"   ✅ 미상장 종목 거부: {report}")
    finally:
        Path(db_path).unlink()


def test_ingester_error_handling():
    print("\n[13] FillIngester — 소스 예외 처리")

    class BrokenSource(FillSource):
        name = "broken"
        @property
        def is_live(self): return True
        def fetch_fills_since(self, since_utc):
            raise RuntimeError("network down")
        def fetch_fills_for_order(self, broker_order_no):
            raise RuntimeError("network down")

    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = f.name
    try:
        store = FillStore(db_path)
        ingester = FillIngester(BrokenSource(), store)
        report = ingester.ingest_since(datetime(2026, 5, 6, tzinfo=timezone.utc))
        assert report.error is not None
        assert "network" in report.error
        assert report.fetched == 0
        print(f"   ✅ 예외 처리: {report.error}")
    finally:
        Path(db_path).unlink()


def test_fill_store_fetch_since():
    print("\n[14] FillStore — fetch_since (전체 시간 범위)")
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = f.name
    try:
        store = FillStore(db_path)
        store.upsert_many([
            _make_fill(fill_id="F1", filled_at=datetime(2026, 5, 5, tzinfo=timezone.utc)),
            _make_fill(fill_id="F2", filled_at=datetime(2026, 5, 6, tzinfo=timezone.utc)),
            _make_fill(fill_id="F3", filled_at=datetime(2026, 5, 7, tzinfo=timezone.utc)),
        ])
        result = store.fetch_since(datetime(2026, 5, 6, tzinfo=timezone.utc))
        assert len(result) == 2  # F2, F3
        assert result[0].fill_id == "F2"
        print(f"   ✅ fetch_since: {len(result)}개")
    finally:
        Path(db_path).unlink()


if __name__ == "__main__":
    test_fill_validation()
    test_fill_invalid_inputs()
    test_fill_tz_aware_required()
    test_fill_store_roundtrip()
    test_fill_store_idempotency()
    test_fill_store_query_by_order()
    test_fill_store_raw_preservation()
    test_kis_fill_source_parsing()
    test_kis_fill_source_skip_no_fill()
    test_kis_fill_source_partial()
    test_ingester_basic()
    test_ingester_unknown_symbol_rejected()
    test_ingester_error_handling()
    test_fill_store_fetch_since()
    print("\n🎉 모든 스모크 테스트 통과 (All smoke tests passed)")
