#!/usr/bin/env python3
"""
KIS 연결 점검 스크립트 (KIS Connection Check)
==============================================

JCPR Trading System - jcpr-ts-v01
Task 9 v0.1

⚠️ 안전 원칙 (Safety Principles):
- 주문 송신 절대 안 함 (NEVER submits orders) — read-only 점검만
- 자격증명은 마스킹 출력
- 모든 호출은 read-only API

사용법 (Usage):
    # 기본 (.env 사용, paper 환경)
    python scripts/check_kis_connection.py

    # 환경 강제 전환
    python scripts/check_kis_connection.py --env paper

    # 다른 종목으로 시세 점검
    python scripts/check_kis_connection.py --symbol 000660

    # JSON 출력 (CI/스크립트용)
    python scripts/check_kis_connection.py --output json

    # Rate limit 점검 skip
    python scripts/check_kis_connection.py --skip-rate-limit

종료 코드 (Exit codes):
    0 = 모든 점검 통과
    1 = 1개 이상 점검 실패
    2 = 자격증명 로드 실패 (.env 누락 등)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Optional

# Repo root를 path에 추가
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.brokers.kis import KISAdapter, KISEnv
from src.brokers.kis.client import KISAPIError
from src.brokers.kis.credentials import (
    CredentialsError, load_kis_credentials_from_env,
)
from src.data.ohlcv_schema import Timeframe


# ─────────────────────────────────────────────────
# 점검 결과 모델
# ─────────────────────────────────────────────────

class CheckStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    INFO = "info"
    SKIP = "skip"


@dataclass
class CheckResult:
    name: str
    status: CheckStatus
    message: str
    detail: dict[str, Any] = field(default_factory=dict)
    duration_ms: Optional[int] = None

    def is_failure(self) -> bool:
        return self.status == CheckStatus.FAIL

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status.value,
            "message": self.message,
            "detail": self.detail,
            "duration_ms": self.duration_ms,
        }


@dataclass
class CheckReport:
    started_at_utc: datetime
    completed_at_utc: datetime
    env: str
    base_url: str
    results: list[CheckResult] = field(default_factory=list)

    def passed_count(self) -> int:
        return sum(1 for r in self.results if r.status == CheckStatus.PASS)

    def failed_count(self) -> int:
        return sum(1 for r in self.results if r.status == CheckStatus.FAIL)

    def total_count(self) -> int:
        # INFO/SKIP은 카운트에서 제외 (PASS+FAIL 만)
        return sum(1 for r in self.results if r.status in (CheckStatus.PASS, CheckStatus.FAIL))

    def to_dict(self) -> dict:
        return {
            "started_at_utc": self.started_at_utc.isoformat(),
            "completed_at_utc": self.completed_at_utc.isoformat(),
            "env": self.env,
            "base_url": self.base_url,
            "passed": self.passed_count(),
            "total": self.total_count(),
            "all_passed": self.failed_count() == 0,
            "results": [r.to_dict() for r in self.results],
        }


# ─────────────────────────────────────────────────
# 출력 포맷
# ─────────────────────────────────────────────────

# ANSI 색상 (terminal)
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
CYAN = "\033[96m"
GRAY = "\033[90m"
BOLD = "\033[1m"
RESET = "\033[0m"


def _icon(status: CheckStatus) -> str:
    return {
        CheckStatus.PASS: f"{GREEN}✅{RESET}",
        CheckStatus.FAIL: f"{RED}❌{RESET}",
        CheckStatus.INFO: f"{BLUE}ℹ️ {RESET}",
        CheckStatus.SKIP: f"{GRAY}⏭️ {RESET}",
    }[status]


def _print_text_report(report: CheckReport, total_planned: int) -> None:
    """텍스트 형식 출력 (사람 친화적)."""
    print()
    print(f"{BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RESET}")
    print(f"{BOLD}  JCPR — KIS Connection Check (Task 9 v0.1){RESET}")
    print(f"{BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RESET}")
    print(f"  환경 (env): {CYAN}{report.env}{RESET}")
    print(f"  Base URL:   {report.base_url}")
    print(f"  시작:       {report.started_at_utc.isoformat()}")
    print()

    for i, r in enumerate(report.results, start=1):
        prefix = f"[{i}/{total_planned}] {r.name:<28}"
        duration = f"({r.duration_ms}ms)" if r.duration_ms is not None else ""
        line = f"{prefix} {_icon(r.status)} {r.message} {GRAY}{duration}{RESET}"
        print(line)
        # 상세 정보 (verbose)
        if r.detail:
            for k, v in r.detail.items():
                print(f"    {GRAY}└─ {k}: {v}{RESET}")

    print()
    print(f"{BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RESET}")
    elapsed = (report.completed_at_utc - report.started_at_utc).total_seconds()
    if report.failed_count() == 0:
        print(f"  {GREEN}{BOLD}✅ 전체 통과: {report.passed_count()}/{report.total_count()} ({elapsed:.2f}s){RESET}")
    else:
        print(f"  {RED}{BOLD}❌ 실패 {report.failed_count()}건 / 전체 {report.total_count()}건 ({elapsed:.2f}s){RESET}")
    print(f"{BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RESET}")
    print()


# ─────────────────────────────────────────────────
# 개별 점검 함수
# ─────────────────────────────────────────────────

class KISConnectionChecker:
    """
    KIS 연결 점검 실행기.
    각 check_*() 메서드는 CheckResult 반환.
    """

    def __init__(
        self,
        adapter: KISAdapter,
        *,
        symbol: str = "005930",
        skip_rate_limit: bool = False,
    ):
        self._adapter = adapter
        self._symbol = symbol
        self._skip_rate_limit = skip_rate_limit

    # ---------- 1. 자격증명 ----------
    def check_credentials(self) -> CheckResult:
        start = time.perf_counter()
        try:
            creds = self._adapter.credentials
            return CheckResult(
                name="자격증명 로드",
                status=CheckStatus.PASS,
                message=f"env={creds.env.value}, account={creds.account_no[:4]}***{creds.account_no[-2:]}",
                detail={
                    "env": creds.env.value,
                    "rate_limit_per_sec": creds.rate_limit_per_sec,
                    "request_timeout_sec": creds.request_timeout_sec,
                },
                duration_ms=int((time.perf_counter() - start) * 1000),
            )
        except Exception as e:  # noqa: BLE001
            return CheckResult(
                name="자격증명 로드",
                status=CheckStatus.FAIL,
                message=f"{type(e).__name__}: {e}",
                duration_ms=int((time.perf_counter() - start) * 1000),
            )

    # ---------- 2. 환경 정보 ----------
    def check_environment(self) -> CheckResult:
        creds = self._adapter.credentials
        return CheckResult(
            name="환경 정보",
            status=CheckStatus.INFO,
            message=f"base_url={creds.env.base_url()}",
            detail={
                "env": creds.env.value,
                "is_paper": creds.env == KISEnv.PAPER,
                "is_live": creds.env == KISEnv.LIVE,
            },
        )

    # ---------- 3. 토큰 발급 ----------
    def check_token_issuance(self) -> CheckResult:
        start = time.perf_counter()
        try:
            token = self._adapter.auth.get_token()
            duration = int((time.perf_counter() - start) * 1000)
            ttl = token.expires_at_utc - datetime.now(timezone.utc)
            return CheckResult(
                name="토큰 발급",
                status=CheckStatus.PASS,
                message=f"{token.token_type} ****{token.token[-4:]}, TTL={int(ttl.total_seconds()/3600)}h",
                detail={
                    "token_type": token.token_type,
                    "expires_at_utc": token.expires_at_utc.isoformat(),
                    "ttl_seconds": int(ttl.total_seconds()),
                },
                duration_ms=duration,
            )
        except Exception as e:  # noqa: BLE001
            return CheckResult(
                name="토큰 발급",
                status=CheckStatus.FAIL,
                message=f"{type(e).__name__}: {str(e)[:120]}",
                duration_ms=int((time.perf_counter() - start) * 1000),
            )

    # ---------- 4. 계좌 조회 ----------
    def check_account(self) -> CheckResult:
        start = time.perf_counter()
        try:
            snap = self._adapter.account.fetch_account_snapshot()
            return CheckResult(
                name="계좌 조회",
                status=CheckStatus.PASS,
                message=(
                    f"가용현금={int(snap.available_cash_krw):,} KRW, "
                    f"평가액={int(snap.total_evaluation_krw):,} KRW, "
                    f"보유 {len(snap.positions)}종목"
                ),
                detail={
                    "cash_krw": str(snap.cash_krw),
                    "available_cash_krw": str(snap.available_cash_krw),
                    "total_evaluation_krw": str(snap.total_evaluation_krw),
                    "total_purchase_krw": str(snap.total_purchase_krw),
                    "total_unrealized_pnl_krw": str(snap.total_unrealized_pnl_krw),
                    "position_count": len(snap.positions),
                    "position_symbols": list(snap.positions.keys()),
                },
                duration_ms=int((time.perf_counter() - start) * 1000),
            )
        except KISAPIError as e:
            return CheckResult(
                name="계좌 조회",
                status=CheckStatus.FAIL,
                message=f"KIS API 오류: rt_cd={e.rt_cd} msg={e.msg}",
                duration_ms=int((time.perf_counter() - start) * 1000),
            )
        except Exception as e:  # noqa: BLE001
            return CheckResult(
                name="계좌 조회",
                status=CheckStatus.FAIL,
                message=f"{type(e).__name__}: {str(e)[:120]}",
                duration_ms=int((time.perf_counter() - start) * 1000),
            )

    # ---------- 5. 시세 조회 (일봉) ----------
    def check_market_data(self) -> CheckResult:
        start = time.perf_counter()
        try:
            end = datetime.now(timezone.utc)
            startd = end - timedelta(days=14)
            bars = list(self._adapter.market_data.fetch_bars(
                self._symbol, Timeframe.D1, startd, end,
            ))
            duration = int((time.perf_counter() - start) * 1000)
            if not bars:
                return CheckResult(
                    name=f"시세 조회 ({self._symbol} 일봉)",
                    status=CheckStatus.FAIL,
                    message="0 bars 반환됨 (휴장 또는 종목 코드 확인)",
                    duration_ms=duration,
                )
            latest = bars[-1]
            return CheckResult(
                name=f"시세 조회 ({self._symbol} 일봉)",
                status=CheckStatus.PASS,
                message=f"{len(bars)} bars, 최근 종가={int(latest.close):,} KRW ({latest.bar_time_utc.date()})",
                detail={
                    "bar_count": len(bars),
                    "latest_close_krw": str(latest.close),
                    "latest_volume": latest.volume,
                    "latest_bar_date": latest.bar_time_utc.date().isoformat(),
                    "split_method": latest.volume_split_method.value,
                },
                duration_ms=duration,
            )
        except KISAPIError as e:
            return CheckResult(
                name=f"시세 조회 ({self._symbol} 일봉)",
                status=CheckStatus.FAIL,
                message=f"KIS API 오류: rt_cd={e.rt_cd} msg={e.msg}",
                duration_ms=int((time.perf_counter() - start) * 1000),
            )
        except Exception as e:  # noqa: BLE001
            return CheckResult(
                name=f"시세 조회 ({self._symbol} 일봉)",
                status=CheckStatus.FAIL,
                message=f"{type(e).__name__}: {str(e)[:120]}",
                duration_ms=int((time.perf_counter() - start) * 1000),
            )

    # ---------- 6. 호가 조회 ----------
    def check_quote(self) -> CheckResult:
        start = time.perf_counter()
        try:
            snap = self._adapter.quote.snapshot(self._symbol)
            duration = int((time.perf_counter() - start) * 1000)
            spread_bps = snap.spread_bps()
            return CheckResult(
                name=f"호가 조회 ({self._symbol})",
                status=CheckStatus.PASS,
                message=(
                    f"bid={int(snap.best_bid):,}, ask={int(snap.best_ask):,}, "
                    f"spread={int(snap.spread()):,} ({spread_bps:.1f}bps), depth={len(snap.depth_levels)}단계"
                ),
                detail={
                    "best_bid": str(snap.best_bid),
                    "best_ask": str(snap.best_ask),
                    "best_bid_size": snap.best_bid_size,
                    "best_ask_size": snap.best_ask_size,
                    "mid_quote": str(snap.mid_quote()),
                    "spread_bps": str(spread_bps) if spread_bps else None,
                    "depth_levels": len(snap.depth_levels),
                },
                duration_ms=duration,
            )
        except KISAPIError as e:
            return CheckResult(
                name=f"호가 조회 ({self._symbol})",
                status=CheckStatus.FAIL,
                message=f"KIS API 오류: rt_cd={e.rt_cd} msg={e.msg}",
                duration_ms=int((time.perf_counter() - start) * 1000),
            )
        except Exception as e:  # noqa: BLE001
            return CheckResult(
                name=f"호가 조회 ({self._symbol})",
                status=CheckStatus.FAIL,
                message=f"{type(e).__name__}: {str(e)[:120]}",
                duration_ms=int((time.perf_counter() - start) * 1000),
            )

    # ---------- 7. Rate Limit ----------
    def check_rate_limit(self) -> CheckResult:
        if self._skip_rate_limit:
            return CheckResult(
                name="Rate Limit",
                status=CheckStatus.SKIP,
                message="--skip-rate-limit",
            )
        start = time.perf_counter()
        try:
            # 5번의 가벼운 호출 — 호가 조회 반복
            for _ in range(5):
                self._adapter.quote.snapshot(self._symbol)
            duration = int((time.perf_counter() - start) * 1000)
            usage = self._adapter.client.rate_limiter.current_usage()
            return CheckResult(
                name="Rate Limit",
                status=CheckStatus.PASS,
                message=f"5 requests in {duration/1000:.2f}s, current usage={usage}",
                detail={
                    "requests": 5,
                    "rate_limit_per_sec": self._adapter.credentials.rate_limit_per_sec,
                    "current_usage": usage,
                },
                duration_ms=duration,
            )
        except Exception as e:  # noqa: BLE001
            return CheckResult(
                name="Rate Limit",
                status=CheckStatus.FAIL,
                message=f"{type(e).__name__}: {str(e)[:120]}",
                duration_ms=int((time.perf_counter() - start) * 1000),
            )

    # ---------- 8. DryRunGuard ----------
    def check_dry_run_guard(self) -> CheckResult:
        guard = self._adapter.dry_run_guard
        live = guard.live_enabled
        if not live:
            return CheckResult(
                name="DryRunGuard",
                status=CheckStatus.PASS,
                message="live_enabled=False (안전 — 주문 dry-run)",
                detail=guard.status(),
            )
        # 활성화되어 있으면 경고 (점검 자체는 통과)
        return CheckResult(
            name="DryRunGuard",
            status=CheckStatus.INFO,
            message="⚠️  live_enabled=True — 실제 주문 송신 가능 상태",
            detail=guard.status(),
        )

    # ---------- 9. 미체결 주문 ----------
    def check_open_orders(self) -> CheckResult:
        start = time.perf_counter()
        try:
            # DryRunGuard가 False면 fetch_open_orders도 빈 리스트 반환
            # 실 점검을 위해 잠깐만 활성화 해도 되지만, 보수적으로 그냥 그대로 호출
            # (orders 모듈의 fetch_open_orders는 dry-run 모드에서 빈 리스트 반환)
            # 진짜 점검은 dry-run을 잠깐 우회 → 호출 → 복귀
            guard = self._adapter.dry_run_guard
            was_live = guard.live_enabled
            if not was_live:
                guard.enable_live(reason="task9_check_open_orders_temp")
            try:
                open_orders = self._adapter.orders.fetch_open_orders()
            finally:
                if not was_live:
                    guard.disable_live()
            duration = int((time.perf_counter() - start) * 1000)
            return CheckResult(
                name="미체결 주문 조회",
                status=CheckStatus.PASS,
                message=f"{len(open_orders)}건",
                detail={
                    "open_order_count": len(open_orders),
                    "symbols": [o.get("symbol") for o in open_orders if o.get("symbol")],
                },
                duration_ms=duration,
            )
        except KISAPIError as e:
            return CheckResult(
                name="미체결 주문 조회",
                status=CheckStatus.FAIL,
                message=f"KIS API 오류: rt_cd={e.rt_cd} msg={e.msg}",
                duration_ms=int((time.perf_counter() - start) * 1000),
            )
        except Exception as e:  # noqa: BLE001
            return CheckResult(
                name="미체결 주문 조회",
                status=CheckStatus.FAIL,
                message=f"{type(e).__name__}: {str(e)[:120]}",
                duration_ms=int((time.perf_counter() - start) * 1000),
            )

    # ---------- 통합 실행 ----------
    def run_all(self) -> CheckReport:
        started_at = datetime.now(timezone.utc)
        creds = self._adapter.credentials
        report = CheckReport(
            started_at_utc=started_at,
            completed_at_utc=started_at,  # 일단 같은 값, 끝에 갱신
            env=creds.env.value,
            base_url=creds.env.base_url(),
        )

        # 순서대로 실행
        report.results.append(self.check_credentials())
        report.results.append(self.check_environment())
        report.results.append(self.check_token_issuance())
        # 토큰 실패하면 후속 점검 무의미 — 그래도 시도
        report.results.append(self.check_account())
        report.results.append(self.check_market_data())
        report.results.append(self.check_quote())
        report.results.append(self.check_rate_limit())
        report.results.append(self.check_dry_run_guard())
        report.results.append(self.check_open_orders())

        report.completed_at_utc = datetime.now(timezone.utc)
        return report


# ─────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="JCPR KIS Connection Check — Task 9 v0.1 (read-only)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--env-file", type=str, default=".env",
        help=".env 파일 경로",
    )
    parser.add_argument(
        "--env", choices=["paper", "live"], default=None,
        help="KIS 환경 강제 전환 (.env의 KIS_ENV 무시)",
    )
    parser.add_argument(
        "--symbol", type=str, default="005930",
        help="시세/호가 점검 종목 코드",
    )
    parser.add_argument(
        "--skip-rate-limit", action="store_true",
        help="Rate Limit 점검 skip",
    )
    parser.add_argument(
        "--output", choices=["text", "json"], default="text",
        help="출력 형식",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="상세 로그",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    if args.verbose:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )
    else:
        logging.basicConfig(level=logging.WARNING)

    # 1) 자격증명 로드 (어댑터 생성 전 — 실패 시 명확한 에러)
    env_override = KISEnv(args.env) if args.env else None
    env_file = Path(args.env_file) if Path(args.env_file).exists() else None

    try:
        # KISAdapter.from_env가 .env 로드 + 어댑터 생성
        adapter = KISAdapter.from_env(
            env_file=env_file,
            override_env=env_override,
        )
    except CredentialsError as e:
        print(f"\n{RED}❌ 자격증명 로드 실패: {e}{RESET}", file=sys.stderr)
        print(f"{YELLOW}  해결 방법:{RESET}", file=sys.stderr)
        print(f"    1. cp .env.example .env", file=sys.stderr)
        print(f"    2. .env 파일에 KIS_APP_KEY, KIS_APP_SECRET, KIS_ACCOUNT_NO 입력", file=sys.stderr)
        print(f"    3. chmod 600 .env (macOS/Linux)", file=sys.stderr)
        print(file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"\n{RED}❌ 어댑터 초기화 실패: {type(e).__name__}: {e}{RESET}", file=sys.stderr)
        return 2

    # 2) 점검 실행
    checker = KISConnectionChecker(
        adapter=adapter,
        symbol=args.symbol,
        skip_rate_limit=args.skip_rate_limit,
    )
    report = checker.run_all()

    # 3) 출력
    if args.output == "json":
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        _print_text_report(report, total_planned=9)

    # 4) 종료 코드
    return 0 if report.failed_count() == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
