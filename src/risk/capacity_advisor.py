"""Capacity advisor — `<output>` #12 자동 권장 (Phase 2 A2-1).

Ladder step v1 알고리즘으로 다음 세션 capacity를 권장한다.
read-only 자문 모듈이며 주문 실행 권한은 없다.

알고리즘 결정 (Algorithm Decisions):
    #1 알고리즘   : (d) Ladder step 단독
    #2 입력 데이터: (a) 단일 세션 P&L + reconciliation (현재) / (b) N일 history (옵션, 차기)
    #3 출력 형식  : (c) 구조화 + 근거(rationale list)
    #4 ladder 출처: capacity.local.yaml capital_caps.ladder

상승/하강 규칙 (Transition Rules):
    +PnL  + recon OK  + exception=0   →  up   (1 단계 상승)
    flat  + recon OK                   →  hold
    -PnL  OR exception>0               →  down (1 단계 하강)
    recon major                        →  floor (최하단 강제)
    recon missing                      →  상승 무효화 (이미 결정된 up → hold)
    history.consecutive_loss_days>=3   →  추가 1 단계 하강 (history 제공 시)

보안 영향 (Security Impact):
    - 시크릿 / 자격증명 미접촉 (P&L 숫자 + severity 만 처리)
    - read-only 자문, 거래 미발생
    - <model> ESC/Ctrl-C 영향 없음
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Mapping

# Public algorithm identifier — 다음 버전(history 통합) 시 v2로 증가
ALGORITHM_ID: str = "ladder_step_v1"

# 평탄(flat) 판정 임계값 (default ±0.5% of starting capital)
DEFAULT_FLAT_THRESHOLD_RATIO: Decimal = Decimal("0.005")

# 연속 손실일 추가 하향 임계값 (history 제공 시 적용)
DEFAULT_CONSECUTIVE_LOSS_THRESHOLD: int = 3


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


SeverityLiteral = Literal["ok", "minor", "major", "missing"]
DirectionLiteral = Literal["up", "hold", "down", "floor"]
ReasonLiteral = Literal[
    "computed_v1",
    "no_ladder",
    "invalid_capital",
    "manual",
]


@dataclass(frozen=True)
class SessionSignals:
    """단일 세션 입력 신호 (single-session inputs).

    모든 금액은 Decimal(KRW)로 표현. float 사용 금지(정밀도 보존).
    """

    realized_pnl_krw: Decimal
    unrealized_pnl_krw: Decimal
    starting_capital_krw: Decimal
    reconciliation_severity: SeverityLiteral
    exception_count: int

    def __post_init__(self) -> None:
        # 타입 검증 (Decimal 강제)
        for field_name in (
            "realized_pnl_krw",
            "unrealized_pnl_krw",
            "starting_capital_krw",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, Decimal):
                raise TypeError(
                    f"SessionSignals.{field_name} must be Decimal, "
                    f"got {type(value).__name__}"
                )
        if self.starting_capital_krw <= 0:
            raise ValueError(
                f"starting_capital_krw must be positive, got {self.starting_capital_krw}"
            )
        if self.exception_count < 0:
            raise ValueError(
                f"exception_count must be non-negative, got {self.exception_count}"
            )
        if self.reconciliation_severity not in ("ok", "minor", "major", "missing"):
            raise ValueError(
                f"invalid reconciliation_severity: {self.reconciliation_severity!r}"
            )

    @property
    def total_pnl_krw(self) -> Decimal:
        return self.realized_pnl_krw + self.unrealized_pnl_krw


@dataclass(frozen=True)
class HistoryStats:
    """N일 history 통계 (옵션, 차기 사이클 활성화).

    현 사이클에서 capacity_advisor는 history=None 으로 호출됨.
    A2-2 사이클에서 session jsonl 저장 모듈 도입 후 활성화.
    """

    sessions_count: int
    cumulative_realized_pnl_krw: Decimal
    max_drawdown_krw: Decimal
    consecutive_loss_days: int

    def __post_init__(self) -> None:
        if self.sessions_count < 0:
            raise ValueError("sessions_count must be non-negative")
        if self.consecutive_loss_days < 0:
            raise ValueError("consecutive_loss_days must be non-negative")
        if self.max_drawdown_krw < 0:
            raise ValueError("max_drawdown_krw must be non-negative (magnitude)")


@dataclass(frozen=True)
class CapacityRecommendation:
    """`<output>` #12 구조화 권장 (immutable).

    available=False 인 경우 reason 만 채워지고 나머지 필드는 None.
    available=True 인 경우 모든 권장 필드가 채워짐.
    """

    available: bool
    reason: ReasonLiteral
    current_capacity_krw: Decimal | None
    recommended_capacity_krw: Decimal | None
    direction: DirectionLiteral | None
    ladder_step_from: int | None
    ladder_step_to: int | None
    rationale: tuple[str, ...]
    triggers: Mapping[str, Any]
    computed_at: datetime
    algorithm: str = ALGORITHM_ID

    def __post_init__(self) -> None:
        # frozen + Mapping immutability
        if not isinstance(self.rationale, tuple):
            object.__setattr__(self, "rationale", tuple(self.rationale))
        if self.computed_at.tzinfo is None:
            raise ValueError("computed_at must be timezone-aware")

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form (Decimal → str, datetime → isoformat)."""
        return {
            "available": self.available,
            "reason": self.reason,
            "current_capacity_krw": (
                str(self.current_capacity_krw)
                if self.current_capacity_krw is not None
                else None
            ),
            "recommended_capacity_krw": (
                str(self.recommended_capacity_krw)
                if self.recommended_capacity_krw is not None
                else None
            ),
            "direction": self.direction,
            "ladder_step_from": self.ladder_step_from,
            "ladder_step_to": self.ladder_step_to,
            "rationale": list(self.rationale),
            "triggers": dict(self.triggers),
            "computed_at": self.computed_at.isoformat(),
            "algorithm": self.algorithm,
        }


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CapacityAdvisorError(Exception):
    """capacity_advisor 모듈 베이스 예외."""


class InvalidLadderError(CapacityAdvisorError):
    """ladder가 알고리즘 입력 요건을 위반 (정렬되지 않음, 음수, 빈 값 등)."""


# ---------------------------------------------------------------------------
# CapacityAdvisor
# ---------------------------------------------------------------------------


class CapacityAdvisor:
    """Ladder step v1 알고리즘 권장 엔진.

    인스턴스는 ladder + 임계값으로 고정. recommend() 호출 시 입력 신호로
    CapacityRecommendation을 산출한다. 부작용 없음(side-effect free).
    """

    def __init__(
        self,
        ladder: tuple[Decimal, ...] | list[Decimal],
        flat_threshold_ratio: Decimal = DEFAULT_FLAT_THRESHOLD_RATIO,
        consecutive_loss_threshold: int = DEFAULT_CONSECUTIVE_LOSS_THRESHOLD,
    ) -> None:
        # ladder 검증 + 정규화
        ladder_tuple = self._validate_ladder(ladder)
        self._ladder: tuple[Decimal, ...] = ladder_tuple
        # 평탄 임계 검증
        if flat_threshold_ratio < 0:
            raise ValueError(
                f"flat_threshold_ratio must be non-negative, got {flat_threshold_ratio}"
            )
        self._flat_threshold_ratio: Decimal = flat_threshold_ratio
        # 연속 손실 임계 검증
        if consecutive_loss_threshold < 1:
            raise ValueError(
                f"consecutive_loss_threshold must be >= 1, "
                f"got {consecutive_loss_threshold}"
            )
        self._consecutive_loss_threshold: int = consecutive_loss_threshold

    # -- public ----------------------------------------------------------

    @property
    def ladder(self) -> tuple[Decimal, ...]:
        return self._ladder

    def recommend(
        self,
        session: SessionSignals,
        history: HistoryStats | None = None,
        *,
        now: datetime | None = None,
    ) -> CapacityRecommendation:
        """단일 세션 신호 + (옵션) history 로 다음 세션 capacity 권장."""
        computed_at = now if now is not None else datetime.now(timezone.utc)

        # 1. 빈 ladder 가드 (생성 시 막혔지만 방어적 재검증)
        if not self._ladder:
            return self._make_unavailable(
                reason="no_ladder",
                rationale=("ladder가 정의되지 않음 — 운영자 설정 필요",),
                triggers={"ladder_size": 0},
                computed_at=computed_at,
            )

        # 2. 현재 step 인덱스 결정 (가장 가까운 ladder 단계)
        try:
            step_from, current_capacity, normalized = self._locate_step(
                session.starting_capital_krw
            )
        except InvalidLadderError as exc:
            return self._make_unavailable(
                reason="invalid_capital",
                rationale=(f"starting_capital 이상: {exc}",),
                triggers={"starting_capital_krw": str(session.starting_capital_krw)},
                computed_at=computed_at,
            )

        # 3. P&L 부호 분류
        flat_threshold = (
            session.starting_capital_krw * self._flat_threshold_ratio
        )
        total_pnl = session.total_pnl_krw
        if total_pnl > flat_threshold:
            pnl_signal = "positive"
        elif total_pnl < -flat_threshold:
            pnl_signal = "negative"
        else:
            pnl_signal = "flat"

        # 4. rationale + triggers 누적
        rationale: list[str] = []
        triggers: dict[str, Any] = {
            "pnl_signal": pnl_signal,
            "total_pnl_krw": str(total_pnl),
            "starting_capital_krw": str(session.starting_capital_krw),
            "reconciliation_severity": session.reconciliation_severity,
            "exception_count": session.exception_count,
            "ladder_size": len(self._ladder),
        }

        if normalized:
            rationale.append(
                f"starting_capital이 ladder에 정확히 일치하지 않아 "
                f"가장 가까운 단계({current_capacity:,} KRW)로 정규화"
            )
            triggers["normalized"] = True

        # 5. 강제 floor 검사 — major 정합성 위반은 모든 다른 신호를 우선
        if session.reconciliation_severity == "major":
            step_to = 0
            direction: DirectionLiteral = "floor"
            rationale.append("정합성 점검(reconciliation) major 위반 — 최하단 강제")
            return self._build_recommendation(
                step_from=step_from,
                step_to=step_to,
                direction=direction,
                rationale=rationale,
                triggers=triggers,
                computed_at=computed_at,
            )

        # 6. 기본 방향 결정
        if pnl_signal == "negative" or session.exception_count > 0:
            step_to = max(step_from - 1, 0)
            direction = "down" if step_to < step_from else "hold"
            if pnl_signal == "negative":
                pct = (
                    total_pnl / session.starting_capital_krw * Decimal("100")
                ).quantize(Decimal("0.01"))
                rationale.append(f"세션 P&L {pct}% — 하강 권장")
            if session.exception_count > 0:
                rationale.append(
                    f"실행 예외(EXEC_FAILED) {session.exception_count}건 — 하강 권장"
                )
            if step_to == step_from:
                rationale.append("이미 최하단 도달 — 하강 불가, 유지")

        elif (
            pnl_signal == "positive"
            and session.reconciliation_severity == "ok"
            and session.exception_count == 0
        ):
            step_to = min(step_from + 1, len(self._ladder) - 1)
            direction = "up" if step_to > step_from else "hold"
            pct = (
                total_pnl / session.starting_capital_krw * Decimal("100")
            ).quantize(Decimal("0.01"))
            rationale.append(
                f"세션 P&L +{pct}%, 정합성 OK, 예외 없음 — 상승 권장"
            )
            if step_to == step_from:
                rationale.append("이미 최상단 도달 — 상승 불가, 유지")

        else:
            # flat OR (positive 인데 recon != ok)
            step_to = step_from
            direction = "hold"
            if pnl_signal == "flat":
                rationale.append(
                    f"세션 P&L 평탄 (±{self._flat_threshold_ratio * 100}% 이내) — 유지 권장"
                )
            elif pnl_signal == "positive":
                if session.reconciliation_severity == "minor":
                    rationale.append(
                        "P&L 양호하나 정합성 점검(minor) 경고 — 유지 (보수적)"
                    )
                elif session.reconciliation_severity == "missing":
                    rationale.append(
                        "P&L 양호하나 정합성 점검(missing) 미실행 — 유지 (보수적)"
                    )
                else:
                    rationale.append(
                        "P&L 양호하나 정합성/예외 신호 미충족 — 유지 (보수적)"
                    )

        # 7. recon missing 시 상승 무효화 (보수화)
        if session.reconciliation_severity == "missing" and direction == "up":
            step_to = step_from
            direction = "hold"
            rationale.append("정합성 점검(reconciliation/missing) 미실행 — 상승 보류")
        elif session.reconciliation_severity == "missing" and direction != "floor":
            rationale.append("정합성 점검(missing) 미실행 — 권장 신뢰도 저하")

        # 8. minor 정합성 — 별도 보수화 (상승 무효화)
        if session.reconciliation_severity == "minor" and direction == "up":
            step_to = step_from
            direction = "hold"
            rationale.append("정합성 점검(reconciliation/minor) 경고 — 상승 보류")

        # 9. history 기반 추가 하향 (제공 시)
        if history is not None:
            triggers["history_sessions_count"] = history.sessions_count
            triggers["history_consecutive_loss_days"] = history.consecutive_loss_days
            triggers["history_max_drawdown_krw"] = str(history.max_drawdown_krw)
            if history.consecutive_loss_days >= self._consecutive_loss_threshold:
                old_step = step_to
                step_to = max(step_to - 1, 0)
                if step_to < old_step:
                    direction = "down"
                    rationale.append(
                        f"연속 손실 {history.consecutive_loss_days}일 — 추가 하향"
                    )
                else:
                    rationale.append(
                        f"연속 손실 {history.consecutive_loss_days}일 — "
                        f"이미 최하단으로 추가 하향 불가"
                    )

        return self._build_recommendation(
            step_from=step_from,
            step_to=step_to,
            direction=direction,
            rationale=rationale,
            triggers=triggers,
            computed_at=computed_at,
        )

    # -- internal --------------------------------------------------------

    @staticmethod
    def _validate_ladder(
        ladder: tuple[Decimal, ...] | list[Decimal],
    ) -> tuple[Decimal, ...]:
        """ladder 검증. 빈 ladder는 InvalidLadderError 대신 허용 (recommend()에서 처리)."""
        if not ladder:
            return ()
        ladder_list = list(ladder)
        # 모든 요소가 Decimal + 양수
        for i, v in enumerate(ladder_list):
            if not isinstance(v, Decimal):
                raise InvalidLadderError(
                    f"ladder[{i}] must be Decimal, got {type(v).__name__}"
                )
            if v <= 0:
                raise InvalidLadderError(
                    f"ladder[{i}] must be positive, got {v}"
                )
        # strictly increasing 검증
        for i in range(1, len(ladder_list)):
            if ladder_list[i] <= ladder_list[i - 1]:
                raise InvalidLadderError(
                    f"ladder must be strictly increasing: "
                    f"ladder[{i - 1}]={ladder_list[i - 1]} >= ladder[{i}]={ladder_list[i]}"
                )
        return tuple(ladder_list)

    def _locate_step(
        self, starting_capital: Decimal
    ) -> tuple[int, Decimal, bool]:
        """ladder에서 starting_capital과 가장 가까운 단계 인덱스 반환.

        Returns:
            (step_index, ladder_value_at_step, normalized_flag)
            normalized_flag=True 인 경우 starting_capital이 ladder 값과 정확히 일치하지 않음.
        """
        if starting_capital <= 0:
            raise InvalidLadderError(
                f"starting_capital must be positive, got {starting_capital}"
            )
        # 정확히 일치하는 단계 우선
        for i, v in enumerate(self._ladder):
            if v == starting_capital:
                return i, v, False
        # 가장 가까운 단계 (절대 차이 최소)
        best_i = 0
        best_diff: Decimal | None = None
        for i, v in enumerate(self._ladder):
            diff = abs(v - starting_capital)
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best_i = i
        return best_i, self._ladder[best_i], True

    def _build_recommendation(
        self,
        *,
        step_from: int,
        step_to: int,
        direction: DirectionLiteral,
        rationale: list[str],
        triggers: dict[str, Any],
        computed_at: datetime,
    ) -> CapacityRecommendation:
        recommended = self._ladder[step_to]
        current = self._ladder[step_from]
        triggers["recommended_capacity_krw"] = str(recommended)
        triggers["current_capacity_krw"] = str(current)
        return CapacityRecommendation(
            available=True,
            reason="computed_v1",
            current_capacity_krw=current,
            recommended_capacity_krw=recommended,
            direction=direction,
            ladder_step_from=step_from,
            ladder_step_to=step_to,
            rationale=tuple(rationale),
            triggers=dict(triggers),
            computed_at=computed_at,
            algorithm=ALGORITHM_ID,
        )

    @staticmethod
    def _make_unavailable(
        *,
        reason: ReasonLiteral,
        rationale: tuple[str, ...],
        triggers: Mapping[str, Any],
        computed_at: datetime,
    ) -> CapacityRecommendation:
        return CapacityRecommendation(
            available=False,
            reason=reason,
            current_capacity_krw=None,
            recommended_capacity_krw=None,
            direction=None,
            ladder_step_from=None,
            ladder_step_to=None,
            rationale=rationale,
            triggers=dict(triggers),
            computed_at=computed_at,
            algorithm=ALGORITHM_ID,
        )


__all__ = (
    "ALGORITHM_ID",
    "DEFAULT_FLAT_THRESHOLD_RATIO",
    "DEFAULT_CONSECUTIVE_LOSS_THRESHOLD",
    "SessionSignals",
    "HistoryStats",
    "CapacityRecommendation",
    "CapacityAdvisor",
    "CapacityAdvisorError",
    "InvalidLadderError",
)
