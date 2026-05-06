#!/usr/bin/env python3
"""
SignalRunner CLI 진입점 (CLI Entry Point)
==========================================

JCPR Trading System - jcpr-ts-v01
Task 16 v0.3

사용법 (Usage):
    # 단일 사이클 (테스트용)
    python scripts/run_signal_runner.py --once --watchlist 005930,000660

    # 무한 루프 (장중 운영)
    python scripts/run_signal_runner.py --watchlist 005930,000660 --interval 60

    # paper / live 환경 전환
    python scripts/run_signal_runner.py --once --env paper --watchlist 005930

⚠️ 보안 (Security):
- 자격증명은 .env 또는 OS 환경변수에서 로드
- 코드/CLI 인자에 절대 비밀 입력 금지
"""

from __future__ import annotations

import argparse
import logging
import signal as signal_module
import sys
import threading
from pathlib import Path

# Repo root를 path에 추가
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.brokers.kis import KISAdapter, KISEnv
from src.data.ohlcv_schema import Timeframe
from src.data.ohlcv_store import OHLCVStore
from src.data.quote_store import QuoteStore
from src.data.symbol_master import SymbolMaster
from src.execution.execution_record import ExecutionAuditLog
from src.execution.gateway import ExecutionGateway
from src.execution.shutdown_check import ShutdownChecker
from src.execution.sizing import CapacityConfig, OrderSizer
from src.execution.sizing_audit import SizingAuditLogger
from src.risk.gates import (
    DailyLossLimitGate, DuplicateOrderGate, ExposureGate,
    KillSwitchGate, MarketStateGate, OrderRateLimitGate,
    PositionLimitGate, PriceSanityGate,
)
from src.risk.risk_audit import RiskAuditLogger
from src.risk.risk_gate import RiskGateRunner
from src.signals.runner import RunnerConfig, SignalRunner
from src.signals.runner_audit import CycleAuditLog
from src.signals.strategies.momentum_v04 import MomentumStrategyV04


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="JCPR SignalRunner — Task 16 v0.3",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--watchlist", type=str, required=True,
        help="쉼표로 구분된 KRX 종목 코드 (예: 005930,000660)",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="단일 사이클만 실행 (기본은 run_forever)",
    )
    parser.add_argument(
        "--env", choices=["paper", "live"], default=None,
        help="KIS 환경 강제 전환 (.env의 KIS_ENV 무시)",
    )
    parser.add_argument(
        "--env-file", type=str, default=".env",
        help=".env 파일 경로",
    )
    parser.add_argument(
        "--interval", type=int, default=60,
        help="run_forever 사이클 간격 (초)",
    )
    parser.add_argument(
        "--min-symbol-interval", type=int, default=5,
        help="종목간 최소 간격 (초, ≥5)",
    )
    parser.add_argument(
        "--timeframe", choices=["1m", "5m", "15m", "60m", "1d"], default="1d",
        help="시그널 timeframe",
    )
    parser.add_argument(
        "--symbol-master-csv", type=str,
        default="data/reference/symbol_master.csv",
        help="Symbol Master CSV 경로",
    )
    parser.add_argument(
        "--ohlcv-db", type=str,
        default="data/local/market_data.sqlite",
        help="OHLCV SQLite 경로",
    )
    parser.add_argument(
        "--quote-db", type=str,
        default="data/local/quotes.sqlite",
        help="Quote SQLite 경로",
    )
    parser.add_argument(
        "--audit-dir", type=str,
        default="data/audit",
        help="audit log 디렉토리",
    )
    parser.add_argument(
        "--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO",
    )
    parser.add_argument(
        "--max-cycles", type=int, default=None,
        help="run_forever 최대 사이클 수 (테스트용)",
    )
    return parser.parse_args()


def _setup_signal_handlers(shutdown_event: threading.Event) -> None:
    """Ctrl-C (SIGINT) / SIGTERM 처리."""
    def handler(signum, frame):
        print(f"\n[runner] 종료 신호 수신: signal={signum}", file=sys.stderr)
        shutdown_event.set()
    signal_module.signal(signal_module.SIGINT, handler)
    try:
        signal_module.signal(signal_module.SIGTERM, handler)
    except (AttributeError, ValueError):
        # Windows 등 SIGTERM 미지원
        pass


def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger("run_signal_runner")

    # 1) Watchlist 파싱
    watchlist = tuple(s.strip() for s in args.watchlist.split(",") if s.strip())
    if not watchlist:
        logger.error("--watchlist 비어있음")
        return 1

    # 2) Shutdown event
    shutdown_event = threading.Event()
    _setup_signal_handlers(shutdown_event)

    # 3) Audit dir
    audit_dir = Path(args.audit_dir)
    audit_dir.mkdir(parents=True, exist_ok=True)

    # 4) KIS Adapter
    env_override = KISEnv(args.env) if args.env else None
    try:
        adapter = KISAdapter.from_env(
            env_file=args.env_file if Path(args.env_file).exists() else None,
            override_env=env_override,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("KIS Adapter 초기화 실패: %s", e)
        return 1
    logger.info("KIS Adapter: env=%s (orders dry-run=%s)",
                adapter.env.value, not adapter.dry_run_guard.live_enabled)

    # 5) Symbol Master
    sm = SymbolMaster.from_csv(args.symbol_master_csv)
    logger.info("Symbol Master: %d 종목 로드", len(sm))

    # 6) Stores (OHLCV / Quote)
    ohlcv_store = OHLCVStore(args.ohlcv_db)
    quote_store = QuoteStore(args.quote_db)

    # 7) Strategy (Task 14 v0.4)
    strategy = MomentumStrategyV04(
        ohlcv_store=ohlcv_store,
        quote_store=quote_store,
        symbol_master=sm,
    )

    # 8) Sizer (Task 18) — 보수적 기본값
    sizer = OrderSizer(
        capacity=CapacityConfig(
            max_per_order_krw=__decimal__("5000000"),
            max_per_day_krw=__decimal__("20000000"),
            max_pct_of_equity=__decimal__("0.05"),     # 5% 보수적
            min_per_order_krw=__decimal__("10000"),
        ),
        audit_logger=SizingAuditLogger(audit_dir / "sizing.jsonl"),
    )

    # 9) Risk Gates (Task 19 v0.4)
    risk_gates = [
        KillSwitchGate(kill_switch_path="runtime/KILL_SWITCH_ON"),
        MarketStateGate(),
        OrderRateLimitGate(global_min_interval_sec=5, per_symbol_cooldown_sec=30),
        DuplicateOrderGate(),
        PriceSanityGate(
            max_deviation_pct=__decimal__("0.05"),
            quote_store=quote_store,
            max_quote_age_sec=30,
        ),
        PositionLimitGate(max_positions=10),
        ExposureGate(max_pct_per_symbol=__decimal__("0.20")),
        DailyLossLimitGate(max_daily_loss_krw=__decimal__("500000")),
    ]
    risk_runner = RiskGateRunner(
        risk_gates,
        RiskAuditLogger(audit_dir / "risk.jsonl"),
    )

    # 10) Execution Gateway (Task 21)
    gateway = ExecutionGateway(
        adapter=adapter,
        symbol_master=sm,
        sizer=sizer,
        risk_runner=risk_runner,
        audit_log=ExecutionAuditLog(audit_dir / "executions.jsonl"),
    )

    # 11) Shutdown checker
    shutdown_checker = ShutdownChecker(
        kill_switch_path="runtime/KILL_SWITCH_ON",
        shutdown_event=shutdown_event,
    )

    # 12) SignalRunner (Task 16 v0.3)
    runner = SignalRunner(
        strategy=strategy,
        gateway=gateway,
        symbol_master=sm,
        shutdown=shutdown_checker,
        cycle_audit=CycleAuditLog(audit_dir / "cycles.jsonl"),
        config=RunnerConfig(
            timeframe=Timeframe(args.timeframe),
            watchlist_mode="explicit",
            explicit_watchlist=watchlist,
            min_symbol_interval_sec=args.min_symbol_interval,
            cycle_interval_sec=args.interval,
        ),
    )

    # 13) 실행
    logger.info("Watchlist: %s", list(watchlist))
    if args.once:
        result = runner.run_cycle()
        logger.info("Cycle 결과: %s", result.stats.as_dict())
        return 0 if not result.aborted else 2
    else:
        n = runner.run_forever(max_cycles=args.max_cycles)
        logger.info("run_forever 종료: %d 사이클 실행", n)
        return 0


def __decimal__(s: str):
    """Decimal lazy import (top-level import는 명확성 위해 main 안에서)."""
    from decimal import Decimal
    return Decimal(s)


if __name__ == "__main__":
    sys.exit(main())
