"""스모크 테스트 (Smoke Test) — Task 11 v0.1 KRX Market Calendar."""

import sys
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))

from src.data.market_calendar import (
    KRXCalendar, MarketState, MarketStatus,
)

KST = ZoneInfo("Asia/Seoul")
CSV_PATH = Path(__file__).parent / "data" / "reference" / "krx_holidays.csv"


def _kst_to_utc(year, month, day, hour=9, minute=0):
    """KST datetime → UTC."""
    return datetime(year, month, day, hour, minute, tzinfo=KST).astimezone(timezone.utc)


# ─────────────────────────────────────────────────
# Tests — CSV 로드 + 양력 자동
# ─────────────────────────────────────────────────

def test_csv_loaded():
    print("\n[1] CSV 로드 — 2026 휴장일")
    cal = KRXCalendar(CSV_PATH)
    holidays = cal.all_holidays()
    assert len(holidays) > 0
    # 핵심 휴일 검증
    assert date(2026, 1, 1) in holidays  # 신정
    assert date(2026, 2, 17) in holidays  # 설날
    assert date(2026, 5, 24) in holidays  # 부처님오신날
    assert date(2026, 9, 25) in holidays  # 추석
    assert date(2026, 12, 31) in holidays  # 연말 휴장
    print(f"   ✅ {len(holidays)}건 휴일 로드, 핵심 휴일 확인")


def test_solar_holidays_auto():
    print("\n[2] 양력 자동 휴일 — CSV 연도 내")
    cal = KRXCalendar(CSV_PATH)
    # 양력 휴일 (CSV에도 있어 동일)
    is_h, name = cal.is_holiday(date(2026, 5, 5))  # 어린이날
    assert is_h
    print(f"   ✅ 어린이날(2026-05-05): {name}")

    # 양력 자동만 적용 — CSV에 명시 없는 가상 시나리오
    # (현재 CSV에는 다 들어가있으나, 기본 로직 검증)
    cal2 = KRXCalendar()
    # CSV 없으면 csv_years 비어있어 양력 자동도 적용 안 됨
    is_h2, _ = cal2.is_holiday(date(2026, 1, 1))
    assert is_h2 is False
    print(f"   ✅ CSV 없으면 양력 자동도 미적용 (안전)")


def test_weekend():
    print("\n[3] 주말 판정")
    cal = KRXCalendar(CSV_PATH)
    # 2026-05-09 (토)
    assert cal.is_weekend(date(2026, 5, 9))
    # 2026-05-10 (일)
    assert cal.is_weekend(date(2026, 5, 10))
    # 2026-05-11 (월)
    assert not cal.is_weekend(date(2026, 5, 11))
    print(f"   ✅ 토/일 → 주말, 평일 → 거래일")


def test_is_trading_day():
    print("\n[4] is_trading_day")
    cal = KRXCalendar(CSV_PATH)
    # 2026-05-08 (금) — 평일, 휴일 아님
    assert cal.is_trading_day(date(2026, 5, 8))
    # 2026-05-05 (화) — 어린이날
    assert not cal.is_trading_day(date(2026, 5, 5))
    # 2026-05-09 (토)
    assert not cal.is_trading_day(date(2026, 5, 9))
    # 2026-12-31 — 연말 휴장
    assert not cal.is_trading_day(date(2026, 12, 31))
    print(f"   ✅ 평일 거래일 / 휴일 / 주말 / 연말휴장 모두 정확")


def test_market_status_regular_hours():
    print("\n[5] 정규 거래시간 — REGULAR")
    cal = KRXCalendar(CSV_PATH)
    # 2026-05-08 (금) 10:00 KST
    now_utc = _kst_to_utc(2026, 5, 8, 10, 0)
    status = cal.get_status(now_utc)
    assert status.state == MarketState.REGULAR
    assert status.is_open_regular is True
    assert status.is_trading_day is True
    assert status.holiday_name is None
    # 마감 시각 확인 — 같은 날 15:30 KST
    expected_close = _kst_to_utc(2026, 5, 8, 15, 30)
    assert status.next_close_utc == expected_close
    print(f"   ✅ 10:00 KST → REGULAR, next_close={status.next_close_utc.astimezone(KST).strftime('%H:%M %Z')}")


def test_market_status_pre_auction():
    print("\n[6] 시가 동시호가 — PRE_AUCTION (v0.1: 거래 불가)")
    cal = KRXCalendar(CSV_PATH)
    # 2026-05-08 (금) 08:45 KST
    now_utc = _kst_to_utc(2026, 5, 8, 8, 45)
    status = cal.get_status(now_utc)
    assert status.state == MarketState.PRE_AUCTION
    assert status.is_open_regular is False  # v0.1 거래 불가
    assert status.is_trading_day is True
    print(f"   ✅ 08:45 → PRE_AUCTION, is_open_regular=False")


def test_market_status_pre_auction_included():
    print("\n[7] PRE_AUCTION 포함 옵션")
    cal = KRXCalendar(CSV_PATH, include_pre_auction=True)
    now_utc = _kst_to_utc(2026, 5, 8, 8, 45)
    status = cal.get_status(now_utc)
    assert status.state == MarketState.PRE_AUCTION
    assert status.is_open_regular is True  # 옵션으로 거래 가능
    print(f"   ✅ include_pre_auction=True → 08:45도 거래 가능")


def test_market_status_after_close():
    print("\n[8] 마감 후 — CLOSED_HOURS")
    cal = KRXCalendar(CSV_PATH)
    # 2026-05-08 (금) 16:00 KST
    now_utc = _kst_to_utc(2026, 5, 8, 16, 0)
    status = cal.get_status(now_utc)
    assert status.state == MarketState.CLOSED_HOURS
    assert status.is_open_regular is False
    assert status.is_trading_day is True  # 거래일이지만 영업외
    print(f"   ✅ 16:00 → CLOSED_HOURS, trading_day=True")


def test_market_status_weekend():
    print("\n[9] 주말 — CLOSED_WEEKEND")
    cal = KRXCalendar(CSV_PATH)
    # 2026-05-09 (토) 10:00 KST
    now_utc = _kst_to_utc(2026, 5, 9, 10, 0)
    status = cal.get_status(now_utc)
    assert status.state == MarketState.CLOSED_WEEKEND
    assert status.is_open_regular is False
    assert status.is_trading_day is False
    print(f"   ✅ 토 10:00 → CLOSED_WEEKEND")


def test_market_status_holiday():
    print("\n[10] 공휴일 — CLOSED_HOLIDAY")
    cal = KRXCalendar(CSV_PATH)
    # 2026-01-01 (목) — 신정
    now_utc = _kst_to_utc(2026, 1, 1, 10, 0)
    status = cal.get_status(now_utc)
    assert status.state == MarketState.CLOSED_HOLIDAY
    assert status.is_open_regular is False
    assert status.is_trading_day is False
    assert "신정" in status.holiday_name
    print(f"   ✅ 신정 → CLOSED_HOLIDAY, name={status.holiday_name}")


def test_market_status_lunar_holiday():
    print("\n[11] 음력 공휴일 (CSV 의존) — 설날")
    cal = KRXCalendar(CSV_PATH)
    # 2026-02-17 (화) — 설날
    now_utc = _kst_to_utc(2026, 2, 17, 12, 0)
    status = cal.get_status(now_utc)
    assert status.state == MarketState.CLOSED_HOLIDAY
    assert "설날" in status.holiday_name
    print(f"   ✅ 설날(2026-02-17) → {status.holiday_name}")


def test_market_is_open_callback():
    print("\n[12] market_is_open() — SignalRunner 호환")
    cal = KRXCalendar(CSV_PATH)
    # 정규 시간
    assert cal.market_is_open(_kst_to_utc(2026, 5, 8, 10, 0)) is True
    # 마감 후
    assert cal.market_is_open(_kst_to_utc(2026, 5, 8, 16, 0)) is False
    # 주말
    assert cal.market_is_open(_kst_to_utc(2026, 5, 9, 10, 0)) is False
    # 휴일
    assert cal.market_is_open(_kst_to_utc(2026, 1, 1, 10, 0)) is False
    print(f"   ✅ boolean 인터페이스 정상")


def test_next_open_during_closed_hours():
    print("\n[13] next_open — 마감 후")
    cal = KRXCalendar(CSV_PATH)
    # 금 16:00 → 다음 개장은 월 09:00 (5/8 금 → 5/11 월)
    now_utc = _kst_to_utc(2026, 5, 8, 16, 0)
    next_open = cal.next_open(now_utc)
    expected = _kst_to_utc(2026, 5, 11, 9, 0)
    assert next_open == expected
    print(f"   ✅ 금 16:00 → 다음 개장: {next_open.astimezone(KST)}")


def test_next_open_during_holiday():
    print("\n[14] next_open — 신정 휴일 중")
    cal = KRXCalendar(CSV_PATH)
    # 2026-01-01 (목) 10:00 → 다음 개장은 1/2 (금)
    now_utc = _kst_to_utc(2026, 1, 1, 10, 0)
    next_open = cal.next_open(now_utc)
    expected = _kst_to_utc(2026, 1, 2, 9, 0)
    assert next_open == expected
    print(f"   ✅ 신정(1/1) → 다음 개장: {next_open.astimezone(KST).date()}")


def test_next_open_during_long_holiday():
    print("\n[15] next_open — 설날 연휴")
    cal = KRXCalendar(CSV_PATH)
    # 2026-02-16 (월) — 설날 연휴 시작
    now_utc = _kst_to_utc(2026, 2, 16, 10, 0)
    next_open = cal.next_open(now_utc)
    # 2/16(연휴), 2/17(설날), 2/18(연휴) → 2/19(목) 개장
    expected = _kst_to_utc(2026, 2, 19, 9, 0)
    assert next_open == expected
    print(f"   ✅ 설날 연휴 → 다음 개장: {next_open.astimezone(KST).date()}")


def test_next_close_during_open():
    print("\n[16] next_close — 개장 중")
    cal = KRXCalendar(CSV_PATH)
    now_utc = _kst_to_utc(2026, 5, 8, 10, 0)  # 금 10:00
    next_close = cal.next_close(now_utc)
    expected = _kst_to_utc(2026, 5, 8, 15, 30)  # 같은 날 15:30
    assert next_close == expected
    print(f"   ✅ 금 10:00 → 마감: 같은 날 15:30")


def test_trading_days_between():
    print("\n[17] trading_days_between — 5월 1주")
    cal = KRXCalendar(CSV_PATH)
    days = cal.trading_days_between(date(2026, 5, 4), date(2026, 5, 10))
    # 5/4 (월), 5/5 (화 어린이날 휴일), 5/6 (수), 5/7 (목), 5/8 (금), 5/9 (토), 5/10 (일)
    # → 거래일: 5/4, 5/6, 5/7, 5/8 = 4일
    assert len(days) == 4
    assert date(2026, 5, 5) not in days  # 어린이날
    assert date(2026, 5, 4) in days
    assert date(2026, 5, 6) in days
    print(f"   ✅ 4일: {[d.isoformat() for d in days]}")


def test_add_holiday_dynamic():
    print("\n[18] add_holiday — 동적 추가")
    cal = KRXCalendar(CSV_PATH)
    # 임시 공휴일 (예: 시스템 점검)
    cal.add_holiday(date(2026, 6, 1), "거래소 임시 휴장")
    assert not cal.is_trading_day(date(2026, 6, 1))
    is_h, name = cal.is_holiday(date(2026, 6, 1))
    assert is_h
    assert "임시" in name
    print(f"   ✅ 6/1 추가: {name}")


def test_csv_required_missing():
    print("\n[19] require_csv=True + 누락 → ValueError")
    try:
        KRXCalendar(holidays_csv=None, require_csv=True)
        assert False
    except ValueError as e:
        assert "필수" in str(e) or "require" in str(e).lower()
        print(f"   ✅ require_csv=True 검증")


def test_fail_closed_unknown_year():
    print("\n[20] fail_closed_unknown — 데이터 없는 연도 보수적 처리")
    cal = KRXCalendar(CSV_PATH, fail_closed_unknown=True)
    # 2030-05-13 (월) — CSV에 없음 + 평일
    assert not cal.is_trading_day(date(2030, 5, 13))
    # 상태도 UNKNOWN
    now_utc = datetime(2030, 5, 13, 10, 0, tzinfo=KST).astimezone(timezone.utc)
    status = cal.get_status(now_utc)
    assert status.state == MarketState.UNKNOWN
    assert status.is_open_regular is False
    assert "데이터 없음" in (status.holiday_name or "")
    print(f"   ✅ 2030년 평일 → UNKNOWN (보수적)")


def test_fail_closed_unknown_disabled():
    print("\n[21] fail_closed_unknown=False — 평일 낙관적 처리")
    cal = KRXCalendar(CSV_PATH, fail_closed_unknown=False)
    # 2030년 평일 — CSV에 없지만 평일 → True
    assert cal.is_trading_day(date(2030, 5, 13))  # 월
    print(f"   ✅ 2030년 평일 → True (낙관적, 기본)")


def test_dst_no_issue_korea():
    print("\n[22] 한국은 DST 없음 — KST 일관")
    cal = KRXCalendar(CSV_PATH)
    # 여름/겨울 동일하게 KST UTC+9
    summer = _kst_to_utc(2026, 7, 15, 10, 0)
    winter = _kst_to_utc(2026, 1, 15, 10, 0)
    s_summer = cal.get_status(summer)
    s_winter = cal.get_status(winter)
    # 둘 다 평일 정규 시간
    assert s_summer.state == MarketState.REGULAR
    # 1/15 — 평일이고 휴일 아님 (확인: 2026-01-15는 목요일, 휴일 아님)
    assert s_winter.state == MarketState.REGULAR
    print(f"   ✅ 7/15 + 1/15 모두 KST 09:00-15:30")


def test_invalid_inputs():
    print("\n[23] 잘못된 입력 거부")
    cal = KRXCalendar(CSV_PATH)
    # tz-naive
    try:
        cal.get_status(datetime(2026, 5, 8, 10))
        assert False
    except ValueError as e:
        assert "tz-aware" in str(e)
        print(f"   ✅ tz-naive 거부")

    # 빈 휴일 이름
    try:
        cal.add_holiday(date(2026, 7, 1), "")
        assert False
    except ValueError:
        print(f"   ✅ 빈 휴일 이름 거부")

    # trading_days_between start > end
    try:
        cal.trading_days_between(date(2026, 5, 10), date(2026, 5, 1))
        assert False
    except ValueError:
        print(f"   ✅ start > end 거부")


def test_to_dict_serializable():
    print("\n[24] MarketStatus.to_dict — JSON 직렬화")
    cal = KRXCalendar(CSV_PATH)
    status = cal.get_status(_kst_to_utc(2026, 5, 8, 10, 0))
    d = status.to_dict()
    import json
    json_str = json.dumps(d, ensure_ascii=False)
    assert "regular" in json_str
    assert "kst_datetime" in d
    print(f"   ✅ JSON 직렬화 OK")


def test_init_validation():
    print("\n[25] 초기화 검증")
    # open >= close
    try:
        KRXCalendar(
            CSV_PATH,
            regular_open_kst=time(15, 30),
            regular_close_kst=time(9, 0),
        )
        assert False
    except ValueError:
        print(f"   ✅ open >= close 거부")

    # pre_auction >= regular_open
    try:
        KRXCalendar(
            CSV_PATH,
            pre_auction_open_kst=time(10, 0),
            regular_open_kst=time(9, 0),
        )
        assert False
    except ValueError:
        print(f"   ✅ pre_auction >= regular_open 거부")


if __name__ == "__main__":
    test_csv_loaded()
    test_solar_holidays_auto()
    test_weekend()
    test_is_trading_day()
    test_market_status_regular_hours()
    test_market_status_pre_auction()
    test_market_status_pre_auction_included()
    test_market_status_after_close()
    test_market_status_weekend()
    test_market_status_holiday()
    test_market_status_lunar_holiday()
    test_market_is_open_callback()
    test_next_open_during_closed_hours()
    test_next_open_during_holiday()
    test_next_open_during_long_holiday()
    test_next_close_during_open()
    test_trading_days_between()
    test_add_holiday_dynamic()
    test_csv_required_missing()
    test_fail_closed_unknown_year()
    test_fail_closed_unknown_disabled()
    test_dst_no_issue_korea()
    test_invalid_inputs()
    test_to_dict_serializable()
    test_init_validation()
    print("\n🎉 모든 스모크 테스트 통과 (All smoke tests passed)")
