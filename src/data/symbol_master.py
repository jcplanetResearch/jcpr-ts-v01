"""
심볼 마스터 v0.1 (Symbol Master v0.1)
=====================================

JCPR Trading System - jcpr-ts-v01
Task 10 v0.1

KRX 종목 메타데이터의 단일 진실 원천 (Single Source of Truth for KRX symbol metadata).

기능 (Features):
- CSV 1차 저장 (Zone A — public reference)
- SQLite 옵션 (Zone D — local cache, 향후 갱신 시 사용)
- 메모리 dict 로드 + 읽기 전용 보장 (frozen dataclass)
- 코드/시장/상태 기반 조회
- 거래 가능 여부 판정 (fail-closed)

원칙 (Principles):
- 비밀 데이터 없음 (no secrets — all metadata is public)
- 읽기 전용 (read-only after load)
- 알 수 없는 코드/상태는 거부 (unknown = reject)

이전 Task와의 정합성 (Consistency with earlier tasks):
- InstrumentType: Task 18 tick_size.py와 호환
- TickPolicy: Task 18의 align_price_to_tick()로 위임
"""

from __future__ import annotations

import csv
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from .symbol_status import InstrumentType, Market, SymbolStatus, TickPolicy

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────
# 데이터 모델 (Data Model)
# ─────────────────────────────────────────────────

@dataclass(frozen=True)
class Symbol:
    """
    KRX 종목 한 건 (One KRX symbol).

    frozen=True: 로드 후 수정 불가 (immutable after load).
    """
    code: str                       # 6자리 KRX 코드 (e.g., "005930")
    name_kr: str                    # 한국어명
    name_en: str                    # 영문명 (없으면 빈 문자열)
    market: Market
    instrument_type: InstrumentType
    lot_size: int                   # 거래단위 (KRX 주식/ETF/ETN 모두 1)
    tick_policy: TickPolicy
    status: SymbolStatus
    currency: str = "KRW"
    listed_date: Optional[date] = None
    updated_at: Optional[datetime] = None

    def is_tradable(self) -> bool:
        """거래 가능 여부 — status가 ACTIVE인 경우만 (fail-closed)."""
        return self.status.is_tradable()


# ─────────────────────────────────────────────────
# 검증 (Validation)
# ─────────────────────────────────────────────────

class SymbolValidationError(ValueError):
    """심볼 데이터 검증 실패."""


def _validate_code(code: str) -> str:
    """KRX 종목 코드는 6자리 숫자 문자열."""
    if not isinstance(code, str):
        raise SymbolValidationError(f"코드 타입 오류 (code must be str): {type(code)}")
    code = code.strip()
    if len(code) != 6 or not code.isdigit():
        raise SymbolValidationError(
            f"잘못된 KRX 종목 코드 (invalid KRX code, must be 6 digits): {code!r}"
        )
    return code


def _parse_optional_date(s: str) -> Optional[date]:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError as e:
        raise SymbolValidationError(f"잘못된 날짜 형식 (invalid date): {s!r} ({e})")


def _parse_optional_datetime(s: str) -> Optional[datetime]:
    s = (s or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        # tz-aware 보장 (UTC 가정 if naive — 그러나 fail-closed로 거부 권장)
        if dt.tzinfo is None:
            raise SymbolValidationError(
                f"datetime은 tz-aware여야 함 (must be tz-aware): {s!r}"
            )
        return dt.astimezone(timezone.utc)
    except ValueError as e:
        raise SymbolValidationError(f"잘못된 datetime 형식: {s!r} ({e})")


def _row_to_symbol(row: dict[str, str], line_no: int) -> Symbol:
    """CSV 한 행을 Symbol로 변환. 검증 실패 시 line_no 포함하여 예외."""
    try:
        code = _validate_code(row["code"])

        market_raw = row["market"].strip().upper()
        try:
            market = Market(market_raw)
        except ValueError:
            raise SymbolValidationError(f"알 수 없는 시장 (unknown market): {market_raw}")

        instr_raw = row["instrument_type"].strip().lower()
        try:
            instrument = InstrumentType(instr_raw)
        except ValueError:
            raise SymbolValidationError(f"알 수 없는 상품유형: {instr_raw}")

        tick_raw = row["tick_policy"].strip().lower()
        try:
            tick = TickPolicy(tick_raw)
        except ValueError:
            raise SymbolValidationError(f"알 수 없는 호가정책: {tick_raw}")

        status_raw = row["status"].strip().lower()
        try:
            status = SymbolStatus(status_raw)
        except ValueError:
            raise SymbolValidationError(f"알 수 없는 상태: {status_raw}")

        try:
            lot_size = int(row["lot_size"])
        except (ValueError, KeyError):
            raise SymbolValidationError(f"lot_size 파싱 실패: {row.get('lot_size')!r}")
        if lot_size <= 0:
            raise SymbolValidationError(f"lot_size는 양수여야 함: {lot_size}")

        return Symbol(
            code=code,
            name_kr=row.get("name_kr", "").strip(),
            name_en=row.get("name_en", "").strip(),
            market=market,
            instrument_type=instrument,
            lot_size=lot_size,
            tick_policy=tick,
            status=status,
            currency=row.get("currency", "KRW").strip() or "KRW",
            listed_date=_parse_optional_date(row.get("listed_date", "")),
            updated_at=_parse_optional_datetime(row.get("updated_at", "")),
        )
    except KeyError as e:
        raise SymbolValidationError(
            f"line {line_no}: 필수 컬럼 누락 (missing column): {e}"
        )
    except SymbolValidationError as e:
        raise SymbolValidationError(f"line {line_no}: {e}")


# ─────────────────────────────────────────────────
# 심볼 마스터 본체 (Main)
# ─────────────────────────────────────────────────

EXPECTED_COLUMNS = {
    "code", "name_kr", "name_en", "market", "instrument_type",
    "lot_size", "tick_policy", "status", "currency",
    "listed_date", "updated_at",
}


@dataclass
class SymbolMaster:
    """
    심볼 마스터 — 메모리 로드 후 조회 인터페이스 제공.
    (In-memory symbol master with query interface.)
    """
    _by_code: dict[str, Symbol] = field(default_factory=dict)
    _source_path: Optional[Path] = None
    _loaded_at_utc: Optional[datetime] = None

    # ---------- 로딩 (Loading) ----------

    @classmethod
    def from_csv(cls, path: str | Path) -> "SymbolMaster":
        """
        CSV에서 로드. 검증 실패 시 즉시 예외 (fail-closed).

        Raises:
            FileNotFoundError, SymbolValidationError
        """
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"심볼 마스터 CSV 없음 (not found): {p}")

        master = cls(_source_path=p)
        with p.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                raise SymbolValidationError("CSV 헤더 없음 (missing header)")

            missing = EXPECTED_COLUMNS - set(reader.fieldnames)
            if missing:
                raise SymbolValidationError(f"필수 컬럼 누락: {sorted(missing)}")

            for line_no, row in enumerate(reader, start=2):  # 2 = header 다음 줄
                sym = _row_to_symbol(row, line_no)
                if sym.code in master._by_code:
                    raise SymbolValidationError(
                        f"line {line_no}: 중복 코드 (duplicate code): {sym.code}"
                    )
                master._by_code[sym.code] = sym

        master._loaded_at_utc = datetime.now(timezone.utc)
        logger.info(
            "심볼 마스터 로드 완료 (loaded): %d 종목 from %s",
            len(master._by_code), p.name,
        )
        return master

    @classmethod
    def from_sqlite(cls, db_path: str | Path, table: str = "symbol_master") -> "SymbolMaster":
        """SQLite에서 로드 (옵션 — 향후 자동 갱신 시 사용)."""
        p = Path(db_path)
        if not p.exists():
            raise FileNotFoundError(f"SQLite DB 없음: {p}")

        master = cls(_source_path=p)
        # 테이블명 보안: 식별자 화이트리스트 검증
        if not table.replace("_", "").isalnum():
            raise SymbolValidationError(f"잘못된 테이블명: {table!r}")

        conn = sqlite3.connect(p)
        try:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(f"SELECT * FROM {table}")  # noqa: S608 - validated above
            rows = cur.fetchall()
        finally:
            conn.close()

        for line_no, row in enumerate(rows, start=1):
            row_dict = {k: ("" if row[k] is None else str(row[k])) for k in row.keys()}
            sym = _row_to_symbol(row_dict, line_no)
            if sym.code in master._by_code:
                raise SymbolValidationError(f"중복 코드: {sym.code}")
            master._by_code[sym.code] = sym

        master._loaded_at_utc = datetime.now(timezone.utc)
        logger.info("심볼 마스터 로드 (sqlite): %d 종목", len(master._by_code))
        return master

    # ---------- 영속화 (Persistence) — SQLite 캐시 ----------

    def to_sqlite(self, db_path: str | Path, table: str = "symbol_master") -> None:
        """
        현재 메모리 상태를 SQLite로 저장 (Zone D — 로컬 캐시).
        실가동에서는 권한 600 디렉토리에 저장 권장.
        """
        if not table.replace("_", "").isalnum():
            raise SymbolValidationError(f"잘못된 테이블명: {table!r}")

        p = Path(db_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(p)
        try:
            cur = conn.cursor()
            cur.execute(f"DROP TABLE IF EXISTS {table}")
            cur.execute(f"""
                CREATE TABLE {table} (
                    code TEXT PRIMARY KEY,
                    name_kr TEXT,
                    name_en TEXT,
                    market TEXT,
                    instrument_type TEXT,
                    lot_size INTEGER,
                    tick_policy TEXT,
                    status TEXT,
                    currency TEXT,
                    listed_date TEXT,
                    updated_at TEXT
                )
            """)
            for sym in self._by_code.values():
                cur.execute(f"""
                    INSERT INTO {table} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    sym.code, sym.name_kr, sym.name_en,
                    sym.market.value, sym.instrument_type.value,
                    sym.lot_size, sym.tick_policy.value, sym.status.value,
                    sym.currency,
                    sym.listed_date.isoformat() if sym.listed_date else None,
                    sym.updated_at.isoformat() if sym.updated_at else None,
                ))
            conn.commit()
            logger.info("심볼 마스터 SQLite 저장 완료: %d 종목 → %s", len(self._by_code), p)
        finally:
            conn.close()

    # ---------- 조회 인터페이스 (Query Interface) ----------

    def __len__(self) -> int:
        return len(self._by_code)

    def __contains__(self, code: str) -> bool:
        return code in self._by_code

    def __iter__(self) -> Iterator[Symbol]:
        return iter(self._by_code.values())

    def get(self, code: str) -> Symbol:
        """
        코드로 조회. 없으면 KeyError (fail-closed — 호출자가 명시적 처리 필요).
        (Lookup by code. Raises KeyError if not found.)
        """
        try:
            return self._by_code[code]
        except KeyError:
            raise KeyError(f"심볼 마스터에 없는 코드 (unknown symbol code): {code!r}")

    def try_get(self, code: str) -> Optional[Symbol]:
        """없으면 None 반환 (호출자가 명시적으로 None 처리하는 경우만 사용)."""
        return self._by_code.get(code)

    def exists(self, code: str) -> bool:
        return code in self._by_code

    def is_tradable(self, code: str) -> bool:
        """
        거래 가능 여부 — 존재 + status==ACTIVE.
        (Tradable iff exists AND status is ACTIVE.)
        존재하지 않는 코드는 False (fail-closed).
        """
        sym = self._by_code.get(code)
        return sym is not None and sym.is_tradable()

    def filter_by_market(self, market: Market) -> list[Symbol]:
        return [s for s in self._by_code.values() if s.market == market]

    def filter_by_status(self, status: SymbolStatus) -> list[Symbol]:
        return [s for s in self._by_code.values() if s.status == status]

    def filter_by_instrument(self, instrument: InstrumentType) -> list[Symbol]:
        return [s for s in self._by_code.values() if s.instrument_type == instrument]

    def tradable_codes(self) -> list[str]:
        """거래 가능한 코드 목록."""
        return [c for c, s in self._by_code.items() if s.is_tradable()]

    # ---------- 메타 정보 (Meta) ----------

    @property
    def source_path(self) -> Optional[Path]:
        return self._source_path

    @property
    def loaded_at_utc(self) -> Optional[datetime]:
        return self._loaded_at_utc

    def summary(self) -> dict:
        """간단한 통계 (audit/dashboard 용)."""
        from collections import Counter
        markets = Counter(s.market.value for s in self._by_code.values())
        statuses = Counter(s.status.value for s in self._by_code.values())
        instruments = Counter(s.instrument_type.value for s in self._by_code.values())
        return {
            "total": len(self._by_code),
            "by_market": dict(markets),
            "by_status": dict(statuses),
            "by_instrument": dict(instruments),
            "tradable_count": len(self.tradable_codes()),
            "source": str(self._source_path) if self._source_path else None,
            "loaded_at_utc": self._loaded_at_utc.isoformat() if self._loaded_at_utc else None,
        }
