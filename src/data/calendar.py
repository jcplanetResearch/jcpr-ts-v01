"""시장 캘린더 (Market Calendar) — KRX 권위 모듈.

본 모듈은 시장 개·폐장 판단의 단일 권위(single authority)다.
KIS 어댑터(Task 8)의 is_market_open 은 NotImplementedError 를 던지므로,
모든 호출자(특히 risk_gate, Task 19)는 본 모듈을 직접 사용한다.

설계 원칙 (Design principles):
1. UTC 내부 처리 + KST 비교 — 모든 입력은 UTC tz-aware 강제, 내부에서 KST 변환
2. tz-naive 거부 — 시간대 혼동으로 인한 거래 사고 방지
3. fail-closed — 휴장일 데이터 누락 시 is_open=False (보수 기본값)
4. 명시적 영역(phase) 매핑 — risk_limits.open_close_guard 와 직접 연동
5. 외부 라이브러리 의존 최소화 — 자체 구현 (zoneinfo + yaml만 사용)

관련 모듈 (Related):
- src/risk/risk_gate.py            — Task 19, get_phase 결과로 신규 주문 허용/거부
- src/brokers/base.py              — Task 7, is_market_open NotImplementedError fallback
- configs/risk_limits.example.yaml — Task 6 §8.3 open_close_guard 와 동기화
- data/reference/krx_holidays.yaml — 휴장일 데이터 (사용자 갱신)
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from datetime import date, datetime, time, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

import yaml


# ============================================================
# 1. 영역 (Phase) 열거형
# ============================================================
class MarketPhase(str, Enum):
    """시장 영역 — risk_limits.open_close_guard 의 입력으로 사용.

    - CLOSED       : 휴장일 또는 정규장 시간 외 (전체 차단)
    - PRE_OPEN     : 정규장 시작 직전 N분 (Task 6 avoid_first_minutes 와 의미 다름;
                     여기서는 시작 N분 전을 의미. 일반적으로 0분 권장)
    - REGULAR      : 정규장 — 본 시스템 거래 가능 영역
    - NEAR_CLOSE   : 정규장 종료 직전 N분 (avoid_last_minutes 정합)
    - AFTER_HOURS  : 정규장 종료 후 (Phase 1 미지원)
    """
    CLOSED = "CLOSED"
    PRE_OPEN = "PRE_OPEN"
    REGULAR = "REGULAR"
    NEAR_CLOSE = "NEAR_CLOSE"
    AFTER_HOURS = "AFTER_HOURS"


# ============================================================
# 2. 시간대 헬퍼 (Timezone helpers)
# ============================================================
KST = ZoneInfo("Asia/Seoul")
UTC = ZoneInfo("UTC")


def require_utc_aware(ts: datetime) -> None:
    """입력이 UTC tz-aware 인지 검증. 그렇지 않으면 ValueError.

    docs/08a §9.3 시간대 정책: 시스템 내부는 UTC tz-aware 만 허용.
    tz-naive datetime 또는 UTC 가 아닌 timezone 의 datetime 을 거부함으로써
    시간대 혼동으로 인한 거래 사고를 차단한다.
    """
    if ts.tzinfo is None:
        raise ValueError(
            "datetime must be tz-aware UTC (got tz-naive). "
            "Per docs/08a §9.3, system internals must use UTC tz-aware datetimes."
        )
    # tzinfo 가 UTC 와 동등한지 확인 (utcoffset() == 0)
    offset = ts.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise ValueError(
            f"datetime must be in UTC (got tzinfo={ts.tzinfo}, offset={offset}). "
            "Convert to UTC before passing to calendar API."
        )


def utc_to_kst(ts: datetime) -> datetime:
    """UTC tz-aware datetime 을 KST tz-aware 로 변환."""
    require_utc_aware(ts)
    return ts.astimezone(KST)


def kst_to_utc(ts_kst: datetime) -> datetime:
    """KST tz-aware datetime 을 UTC 로 변환.

    내부 헬퍼 — 외부 호출자는 일반적으로 UTC 입력만 사용한다.
    """
    if ts_kst.tzinfo is None:
        raise ValueError("ts_kst must be tz-aware (in KST)")
    return ts_kst.astimezone(UTC)


# ============================================================
# 3. 추상 베이스 (Abstract base)
# ============================================================
class MarketCalendar(ABC):
    """시장 캘린더 추상 베이스. 추후 다른 거래소 추가 시 본 클래스 상속."""

    @property
    @abstractmethod
    def name(self) -> str:
        """거래소명 — 'krx', 'nyse' 등 소문자 단일 토큰."""

    @abstractmethod
    def is_open(self, ts_utc: datetime) -> bool:
        """주어진 UTC 시각에 시장이 정규장 운영 중인가?"""

    @abstractmethod
    def get_phase(self, ts_utc: datetime) -> MarketPhase:
        """주어진 UTC 시각의 시장 영역."""

    @abstractmethod
    def is_holiday(self, date_kst: date) -> bool:
        """주어진 KST 날짜가 휴장일인가? (주말 포함)"""

    @abstractmethod
    def session_bounds(
        self, date_kst: date
    ) -> Optional[Tuple[datetime, datetime]]:
        """주어진 KST 날짜의 정규장 (start, end) UTC 시각.

        휴장일이면 None 반환.
        """

    @abstractmethod
    def next_open(self, ts_utc: datetime) -> datetime:
        """주어진 UTC 시각 이후 다음 정규장 개장 시각 (UTC 반환)."""


# ============================================================
# 4. KRX 구현 (KRX implementation)
# ============================================================
class KrxCalendar(MarketCalendar):
    """KRX 캘린더 — krx_holidays.yaml 로드.

    Args:
        holidays_yaml_path: 휴장일 yaml 경로.
        pre_open_minutes:   정규장 시작 직전 PRE_OPEN 영역으로 분류할 분.
                            기본 0 (사용 안 함). risk_limits 와 정합 필요.
        near_close_minutes: 정규장 종료 직전 NEAR_CLOSE 영역으로 분류할 분.
                            기본 5. risk_limits.open_close_guard.avoid_last_minutes 와 정합.

    fail-closed 원칙:
        - yaml 파일이 없거나 손상된 경우 RuntimeError 를 즉시 던진다.
        - 해당 연도의 휴장일 데이터가 없으면 is_open 은 False 반환 (휴장 가정).
    """

    def __init__(
        self,
        holidays_yaml_path: str | Path,
        *,
        pre_open_minutes: int = 0,
        near_close_minutes: int = 5,
    ) -> None:
        if pre_open_minutes < 0 or near_close_minutes < 0:
            raise ValueError("pre/near minutes must be non-negative")

        self._yaml_path = Path(holidays_yaml_path)
        self._pre_open_minutes = pre_open_minutes
        self._near_close_minutes = near_close_minutes

        if not self._yaml_path.exists():
            raise RuntimeError(
                f"holidays yaml not found: {self._yaml_path}. "
                "Per fail-closed principle, calendar refuses to start."
            )

        with open(self._yaml_path, "r", encoding="utf-8") as f:
            self._raw = yaml.safe_load(f)

        if self._raw.get("exchange") != "KRX":
            raise RuntimeError(
                f"yaml exchange mismatch: expected 'KRX', got {self._raw.get('exchange')!r}"
            )

        # 시장 시간 파싱
        mh = self._raw["market_hours"]
        self._regular_start = self._parse_hhmm(mh["regular_session"]["start"])
        self._regular_end = self._parse_hhmm(mh["regular_session"]["end"])
        if self._regular_start >= self._regular_end:
            raise RuntimeError(
                f"invalid regular session: start {self._regular_start} >= end {self._regular_end}"
            )

        # 휴장일 파싱: {연도(str): set[date]}
        self._holidays_by_year: dict[str, set[date]] = {}
        for year_str, items in (self._raw.get("holidays") or {}).items():
            self._holidays_by_year[str(year_str)] = {
                date.fromisoformat(item["date"]) for item in items
            }

    @staticmethod
    def _parse_hhmm(s: str) -> time:
        """'09:00' 형식의 문자열을 time 객체로."""
        h, m = s.split(":")
        return time(int(h), int(m))

    @property
    def name(self) -> str:
        return "krx"

    # ------------------------------------------------------------
    # 휴장일 / 시장 시간 판별
    # ------------------------------------------------------------
    def is_holiday(self, date_kst: date) -> bool:
        """주말 또는 yaml 등재 휴장일인가?

        해당 연도 데이터가 yaml 에 없으면 fail-closed 로 True (휴장) 반환.
        """
        # 주말
        if date_kst.weekday() >= 5:  # 5=Saturday, 6=Sunday
            return True
        year_key = str(date_kst.year)
        if year_key not in self._holidays_by_year:
            # 데이터 없는 연도: 보수적으로 휴장 가정
            return True
        return date_kst in self._holidays_by_year[year_key]

    def session_bounds(
        self, date_kst: date
    ) -> Optional[Tuple[datetime, datetime]]:
        """해당 KST 날짜 정규장 (start, end) UTC 시각."""
        if self.is_holiday(date_kst):
            return None
        start_kst = datetime.combine(date_kst, self._regular_start, tzinfo=KST)
        end_kst = datetime.combine(date_kst, self._regular_end, tzinfo=KST)
        return (start_kst.astimezone(UTC), end_kst.astimezone(UTC))

    def is_open(self, ts_utc: datetime) -> bool:
        """주어진 UTC 시각에 정규장이 운영 중인가?"""
        require_utc_aware(ts_utc)
        ts_kst = ts_utc.astimezone(KST)
        bounds = self.session_bounds(ts_kst.date())
        if bounds is None:
            return False
        start_utc, end_utc = bounds
        return start_utc <= ts_utc < end_utc

    def get_phase(self, ts_utc: datetime) -> MarketPhase:
        """시장 영역 반환.

        영역 결정 순서:
        1. 휴장일 또는 정규장 시간 전(이른 시각) → CLOSED
        2. 정규장 시작 직전 N분 → PRE_OPEN (pre_open_minutes > 0 일 때만)
        3. 정규장 시작 ~ 종료 N분 전 → REGULAR
        4. 정규장 종료 N분 전 ~ 종료 → NEAR_CLOSE
        5. 정규장 종료 후 → AFTER_HOURS
        """
        require_utc_aware(ts_utc)
        ts_kst = ts_utc.astimezone(KST)
        bounds = self.session_bounds(ts_kst.date())
        if bounds is None:
            return MarketPhase.CLOSED

        start_utc, end_utc = bounds

        # 정규장 이전
        if ts_utc < start_utc:
            if self._pre_open_minutes > 0 and \
               ts_utc >= start_utc - timedelta(minutes=self._pre_open_minutes):
                return MarketPhase.PRE_OPEN
            return MarketPhase.CLOSED

        # 정규장 종료 후
        if ts_utc >= end_utc:
            return MarketPhase.AFTER_HOURS

        # 정규장 내 — NEAR_CLOSE 검사
        if self._near_close_minutes > 0 and \
           ts_utc >= end_utc - timedelta(minutes=self._near_close_minutes):
            return MarketPhase.NEAR_CLOSE

        return MarketPhase.REGULAR

    def next_open(self, ts_utc: datetime) -> datetime:
        """ts_utc 이후 다음 정규장 개장 UTC 시각."""
        require_utc_aware(ts_utc)
        ts_kst = ts_utc.astimezone(KST)
        candidate_date = ts_kst.date()

        # 오늘이 거래일이면서 아직 개장 전이면 오늘 개장 시각 반환
        bounds = self.session_bounds(candidate_date)
        if bounds is not None and ts_utc < bounds[0]:
            return bounds[0]

        # 그 외 — 다음 거래일 탐색 (최대 30일)
        for _ in range(30):
            candidate_date += timedelta(days=1)
            bounds = self.session_bounds(candidate_date)
            if bounds is not None:
                return bounds[0]

        raise RuntimeError(
            f"no trading day found within 30 days of {ts_kst.date()}; "
            "check holiday yaml data"
        )

    # ------------------------------------------------------------
    # repr (시크릿 미포함)
    # ------------------------------------------------------------
    def __repr__(self) -> str:
        years = sorted(self._holidays_by_year.keys())
        return (
            f"<KrxCalendar yaml={self._yaml_path.name} "
            f"years={years} pre_open={self._pre_open_minutes}m "
            f"near_close={self._near_close_minutes}m>"
        )
