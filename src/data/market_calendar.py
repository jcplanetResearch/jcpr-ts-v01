"""
KRX 시장 캘린더 (KRX Market Calendar)
=======================================

JCPR Trading System - jcpr-ts-v01
Task 11 v0.1

KRX(한국거래소) 거래일/거래시간 판정.
(KRX trading day/hour determination.)

원칙 (Principles):
- 입출력 datetime UTC tz-aware
- 내부 KST(Asia/Seoul) 변환
- v0.1: 정규 거래시간(09:00-15:30 KST)만 거래 가능
- 음력 휴일은 CSV에만 의존 (자동 계산 안 함)
- 양력 휴일은 자동 추가 (1/1, 3/1, 5/5, 6/6, 8/15, 10/3, 10/9, 12/25)
- 휴일 데이터 부재 시 fail-closed (UNKNOWN 반환)

KRX 거래시간 (Trading Hours):
- 정규: 09:00 ~ 15:30 KST (월~금)
- 시가 동시호가: 08:30 ~ 09:00 (v0.1 거래 불가)
- 종가 동시호가: 15:20 ~ 15:30 (정규에 포함)
- 시간외: 15:40 ~ 18:00 (v0.1 미지원)
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")

# 양력 고정 휴일 (자동 추가)
_FIXED_SOLAR_HOLIDAYS = {
    (1, 1): "신정",
    (3, 1): "삼일절",
    (5, 5): "어린이날",
    (6, 6): "현충일",
    (8, 15): "광복절",
    (10, 3): "개천절",
    (10, 9): "한글날",
    (12, 25): "성탄절",
}


class MarketState(str, Enum):
    REGULAR = "regular"                # 정규 거래시간 (09:00-15:30)
    PRE_AUCTION = "pre_auction"        # 시가 동시호가 (08:30-09:00) — v0.1 거래 불가
    CLOSED_HOLIDAY = "closed_holiday"  # 휴장일
    CLOSED_WEEKEND = "closed_weekend"  # 주말
    CLOSED_HOURS = "closed_hours"      # 평일 영업외 시간
    UNKNOWN = "unknown"                # 데이터 부재 — fail-closed


@dataclass(frozen=True)
class MarketStatus:
    """특정 시각의 KRX 시장 상태."""
    state: MarketState
    is_open_regular: bool              # 정규 거래 가능 여부
    is_trading_day: bool               # 오늘 거래일인지 (영업외 시간이어도 True)
    kst_datetime: datetime             # KST 변환된 시각
    next_open_utc: Optional[datetime] = None
    next_close_utc: Optional[datetime] = None
    holiday_name: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "is_open_regular": self.is_open_regular,
            "is_trading_day": self.is_trading_day,
            "kst_datetime": self.kst_datetime.isoformat(),
            "next_open_utc": (
                self.next_open_utc.isoformat() if self.next_open_utc else None
            ),
            "next_close_utc": (
                self.next_close_utc.isoformat() if self.next_close_utc else None
            ),
            "holiday_name": self.holiday_name,
        }


# ─────────────────────────────────────────────────
# KRX Calendar
# ─────────────────────────────────────────────────

class KRXCalendar:
    """
    KRX 거래일/거래시간 캘린더.

    Args:
        holidays_csv: 휴장일 CSV 경로 (없으면 양력 휴일만 자동 추가)
        regular_open_kst: 정규 개장 시각 (KST)
        regular_close_kst: 정규 마감 시각 (KST)
        pre_auction_open_kst: 시가 동시호가 시작 (KST)
        include_pre_auction: True면 시가 동시호가도 거래 가능으로 (v0.1: False)
        require_csv: True면 CSV 누락 시 ValueError
        fail_closed_unknown: True면 데이터 없는 미래 날짜를 휴장으로 가정
    """

    def __init__(
        self,
        holidays_csv: Optional[str | Path] = None,
        *,
        regular_open_kst: time = time(9, 0),
        regular_close_kst: time = time(15, 30),
        pre_auction_open_kst: time = time(8, 30),
        include_pre_auction: bool = False,
        require_csv: bool = False,
        fail_closed_unknown: bool = False,
    ):
        if regular_open_kst >= regular_close_kst:
            raise ValueError(
                f"regular_open_kst < regular_close_kst 필요: "
                f"{regular_open_kst} ~ {regular_close_kst}"
            )
        if pre_auction_open_kst >= regular_open_kst:
            raise ValueError(
                "pre_auction_open_kst < regular_open_kst 필요"
            )

        self._regular_open = regular_open_kst
        self._regular_close = regular_close_kst
        self._pre_auction_open = pre_auction_open_kst
        self._include_pre_auction = include_pre_auction
        self._fail_closed_unknown = fail_closed_unknown

        # 휴일 dict: date → name
        self._holidays: dict[date, str] = {}

        # CSV 로드
        if holidays_csv is not None:
            self._load_csv(Path(holidays_csv))
        elif require_csv:
            raise ValueError("holidays_csv 필수 (require_csv=True)")

        # CSV에서 로드된 연도 추적 — 양력 자동 휴일은 이 연도에만 추가
        self._csv_years: set[int] = set()
        for d in list(self._holidays.keys()):
            self._csv_years.add(d.year)

    # ------------------------------------------------------------------
    # 초기화 헬퍼
    # ------------------------------------------------------------------

    def _load_csv(self, path: Path) -> None:
        """CSV에서 휴장일 로드."""
        if not path.exists():
            logger.warning("KRX 휴장일 CSV 없음: %s", path)
            return
        try:
            with path.open("r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                if reader.fieldnames is None or "date" not in reader.fieldnames:
                    raise ValueError(
                        f"CSV 헤더에 'date' 컬럼 필수: {reader.fieldnames}"
                    )
                count = 0
                for row in reader:
                    date_str = (row.get("date") or "").strip()
                    if not date_str:
                        continue
                    try:
                        d = date.fromisoformat(date_str)
                    except ValueError as e:
                        logger.warning(
                            "CSV 라인 무효 — date 파싱 실패: %r — %s",
                            date_str, e,
                        )
                        continue
                    name = (row.get("name") or "").strip() or "휴장"
                    self._holidays[d] = name
                    count += 1
            logger.info("KRX 휴장일 %d건 로드: %s", count, path)
        except OSError as e:
            logger.warning("KRX 휴장일 CSV 로드 실패: %s — %s", path, e)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_holiday(self, kst_date: date, name: str) -> None:
        """동적 휴일 추가 (테스트/임시 공휴일)."""
        if not name or not name.strip():
            raise ValueError("휴일 사유 (name) 비어있음")
        self._holidays[kst_date] = name.strip()
        # 양력 자동 휴일을 적용하기 위해 csv_years에도 추가
        self._csv_years.add(kst_date.year)

    def is_holiday(self, kst_date: date) -> tuple[bool, Optional[str]]:
        """
        KST 날짜가 휴장일인지.
        Returns: (is_holiday, name)
        """
        # 1) CSV / 동적 추가
        if kst_date in self._holidays:
            return True, self._holidays[kst_date]

        # 2) 양력 자동 휴일 (CSV에 해당 연도 데이터가 있을 때만 적용)
        # CSV 데이터가 없으면 양력 휴일도 미상 처리 (음력 정보 없으므로)
        if kst_date.year in self._csv_years:
            key = (kst_date.month, kst_date.day)
            if key in _FIXED_SOLAR_HOLIDAYS:
                return True, _FIXED_SOLAR_HOLIDAYS[key]

        return False, None

    def is_weekend(self, kst_date: date) -> bool:
        """토(5) 또는 일(6)."""
        return kst_date.weekday() >= 5

    def is_trading_day(self, kst_date: date) -> bool:
        """
        KST 날짜가 거래일인지.

        주의: 미래 날짜이고 CSV 데이터가 없는 연도면:
            - fail_closed_unknown=True → False (보수적)
            - fail_closed_unknown=False → 평일이면 True (낙관적)
        """
        if self.is_weekend(kst_date):
            return False
        is_hol, _ = self.is_holiday(kst_date)
        if is_hol:
            return False

        # 데이터 미상 (CSV 연도에 없음)
        if (
            self._fail_closed_unknown
            and kst_date.year not in self._csv_years
        ):
            return False

        return True

    def get_status(self, now_utc: datetime) -> MarketStatus:
        """
        현재 시각 (UTC) 기준 KRX 시장 상태.
        """
        if now_utc.tzinfo is None:
            raise ValueError("now_utc tz-aware 필수")

        kst_dt = now_utc.astimezone(KST)
        kst_d = kst_dt.date()
        kst_t = kst_dt.time()

        # 1) 휴장 여부 판정
        if self.is_weekend(kst_d):
            return self._build_closed(
                MarketState.CLOSED_WEEKEND, kst_dt,
                holiday_name=None,
            )

        is_hol, hol_name = self.is_holiday(kst_d)
        if is_hol:
            return self._build_closed(
                MarketState.CLOSED_HOLIDAY, kst_dt,
                holiday_name=hol_name,
            )

        # 데이터 미상 + fail_closed_unknown
        if (
            self._fail_closed_unknown
            and kst_d.year not in self._csv_years
        ):
            return MarketStatus(
                state=MarketState.UNKNOWN,
                is_open_regular=False,
                is_trading_day=False,
                kst_datetime=kst_dt,
                next_open_utc=None,
                next_close_utc=None,
                holiday_name=f"{kst_d.year}년 휴장일 데이터 없음",
            )

        # 2) 평일 + 거래일 — 시간 판정
        if kst_t < self._pre_auction_open:
            # 새벽~08:30
            state = MarketState.CLOSED_HOURS
            is_open = False
        elif kst_t < self._regular_open:
            # 08:30~09:00 — 시가 동시호가
            state = MarketState.PRE_AUCTION
            is_open = self._include_pre_auction
        elif kst_t < self._regular_close:
            # 09:00~15:30 — 정규
            state = MarketState.REGULAR
            is_open = True
        else:
            # 15:30 이후 — 영업외
            state = MarketState.CLOSED_HOURS
            is_open = False

        return MarketStatus(
            state=state,
            is_open_regular=is_open,
            is_trading_day=True,
            kst_datetime=kst_dt,
            next_open_utc=self._compute_next_open(kst_dt),
            next_close_utc=self._compute_next_close(kst_dt, is_open),
            holiday_name=None,
        )

    def market_is_open(self, now_utc: datetime) -> bool:
        """
        간편 boolean 인터페이스.
        SignalRunner.market_is_open_provider 호환.
        """
        return self.get_status(now_utc).is_open_regular

    def next_open(self, from_utc: datetime) -> Optional[datetime]:
        """다음 정규 개장 시각 (UTC)."""
        if from_utc.tzinfo is None:
            raise ValueError("from_utc tz-aware 필수")
        kst_dt = from_utc.astimezone(KST)
        return self._compute_next_open(kst_dt)

    def next_close(self, from_utc: datetime) -> Optional[datetime]:
        """다음 정규 마감 시각 (UTC) — 현재 개장 중이면 오늘 종가, 아니면 다음 거래일 종가."""
        if from_utc.tzinfo is None:
            raise ValueError("from_utc tz-aware 필수")
        kst_dt = from_utc.astimezone(KST)
        # 일단 현재 시각 기준 — is_open 여부에 관계없이 다음 close 추적
        # _compute_next_close는 is_open=True를 가정하면 오늘 종가 우선
        is_open = self.get_status(from_utc).is_open_regular
        return self._compute_next_close(kst_dt, is_open)

    def trading_days_between(
        self,
        start_kst: date,
        end_kst: date,
    ) -> list[date]:
        """기간 내 거래일 (KST 날짜)."""
        if start_kst > end_kst:
            raise ValueError(f"start_kst > end_kst: {start_kst} > {end_kst}")
        days: list[date] = []
        cur = start_kst
        while cur <= end_kst:
            if self.is_trading_day(cur):
                days.append(cur)
            cur += timedelta(days=1)
        return days

    def all_holidays(self) -> dict[date, str]:
        """등록된 모든 휴일 (CSV + 동적 추가) — 양력 자동 제외."""
        return dict(self._holidays)

    # ------------------------------------------------------------------
    # 내부 — next_open / next_close
    # ------------------------------------------------------------------

    def _build_closed(
        self,
        state: MarketState,
        kst_dt: datetime,
        *,
        holiday_name: Optional[str],
    ) -> MarketStatus:
        return MarketStatus(
            state=state,
            is_open_regular=False,
            is_trading_day=False,
            kst_datetime=kst_dt,
            next_open_utc=self._compute_next_open(kst_dt),
            next_close_utc=None,
            holiday_name=holiday_name,
        )

    def _compute_next_open(self, kst_dt: datetime) -> Optional[datetime]:
        """다음 정규 개장 시각 (UTC)."""
        kst_d = kst_dt.date()
        kst_t = kst_dt.time()

        # 오늘이 거래일이고 아직 개장 전
        if self.is_trading_day(kst_d) and kst_t < self._regular_open:
            open_dt_kst = datetime.combine(kst_d, self._regular_open).replace(tzinfo=KST)
            return open_dt_kst.astimezone(timezone.utc)

        # 다음 거래일 검색 (최대 14일)
        next_d = kst_d + timedelta(days=1)
        for _ in range(14):
            if self.is_trading_day(next_d):
                open_dt_kst = datetime.combine(next_d, self._regular_open).replace(tzinfo=KST)
                return open_dt_kst.astimezone(timezone.utc)
            next_d += timedelta(days=1)
        # 14일 내 거래일 없음 (이상 — 데이터 부족)
        return None

    def _compute_next_close(
        self, kst_dt: datetime, currently_open: bool,
    ) -> Optional[datetime]:
        """다음 정규 마감 시각 (UTC)."""
        kst_d = kst_dt.date()
        kst_t = kst_dt.time()

        # 오늘 거래일이고 아직 마감 전
        if self.is_trading_day(kst_d) and kst_t < self._regular_close:
            close_kst = datetime.combine(kst_d, self._regular_close).replace(tzinfo=KST)
            return close_kst.astimezone(timezone.utc)

        # 다음 거래일의 마감
        next_d = kst_d + timedelta(days=1)
        for _ in range(14):
            if self.is_trading_day(next_d):
                close_kst = datetime.combine(next_d, self._regular_close).replace(tzinfo=KST)
                return close_kst.astimezone(timezone.utc)
            next_d += timedelta(days=1)
        return None
