"""
tests/mcp_servers/test_write_handlers.py — JCPR-ts-v01 (Phase 2-B)
==================================================================

WriteHandlers의 8개 메서드에 대한 단위 테스트.

검증 카테고리:
  1. propose_* 정상 흐름 (4개)
  2. payload validation (BUY/SELL, LIMIT/MARKET, qty>0, limit_price 등)
  3. requested_by 검증 (operator 위장 차단, 빈 값)
  4. cancel_proposal — 본인 제안만 취소 가능
  5. query / list / get_recent
  6. mode 격리 (paper handler는 paper record만 생성)
  7. invariants — 시크릿 키 미반환, list cap 강제
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.execution.approval_store import (
    ApprovalNotFoundError,
    ApprovalStatus,
    ApprovalStore,
    InvalidTransitionError,
)
from src.mcp_servers._write_handlers import (
    LIST_HARD_MAX,
    IdentityViolationError,
    PayloadValidationError,
    WriteHandlers,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path: Path) -> ApprovalStore:
    return ApprovalStore(db_path=tmp_path / "approvals.sqlite")


@pytest.fixture
def handlers(store) -> WriteHandlers:
    return WriteHandlers(store=store, mode="paper")


@pytest.fixture
def live_handlers(store) -> WriteHandlers:
    return WriteHandlers(store=store, mode="live")


# ===========================================================================
# 1. propose_* 정상 흐름
# ===========================================================================

class TestProposeHappy:
    def test_submit_order_limit(self, handlers, store):
        out = handlers.propose_submit_order(
            payload={
                "symbol": "005930", "side": "buy", "qty": 10,
                "order_type": "limit", "limit_price": 70000,
            },
            requested_by="market_agent",
        )
        assert out["status"] == "PROPOSED"
        assert out["action_kind"] == "submit_order"
        assert out["mode"] == "paper"
        aid = out["approval_id"]
        assert aid.startswith("apv-")

        # 정규화 검증 — uppercase 변환됨
        rec = store.get(aid)
        assert rec.payload["side"] == "BUY"
        assert rec.payload["order_type"] == "LIMIT"
        assert rec.payload["limit_price"] == 70000.0

    def test_submit_order_market(self, handlers, store):
        out = handlers.propose_submit_order(
            payload={"symbol": "005930", "side": "SELL", "qty": 5,
                     "order_type": "MARKET"},
            requested_by="agent",
        )
        rec = store.get(out["approval_id"])
        assert "limit_price" not in rec.payload

    def test_cancel_order(self, handlers):
        out = handlers.propose_cancel_order(
            payload={"broker_order_id": "BRK-000123"},
            requested_by="agent",
        )
        assert out["status"] == "PROPOSED"
        assert out["action_kind"] == "cancel_order"

    def test_set_capacity(self, handlers):
        out = handlers.propose_set_capacity(
            payload={"strategy": "momentum_v1", "capacity_krw": 10_000_000},
            requested_by="quant_agent",
        )
        assert out["status"] == "PROPOSED"

    def test_kill_switch(self, handlers, store):
        out = handlers.propose_kill_switch(
            payload={"action": "activate", "reason": "manual halt"},
            requested_by="risk_agent",
        )
        rec = store.get(out["approval_id"])
        assert rec.payload["action"] == "activate"
        assert rec.payload["reason"] == "manual halt"


# ===========================================================================
# 2. Payload validation
# ===========================================================================

class TestPayloadValidation:
    def test_submit_missing_symbol(self, handlers):
        with pytest.raises(PayloadValidationError, match="missing"):
            handlers.propose_submit_order(
                payload={"side": "BUY", "qty": 1, "order_type": "MARKET"},
                requested_by="agent",
            )

    def test_submit_invalid_side(self, handlers):
        with pytest.raises(PayloadValidationError, match="BUY/SELL"):
            handlers.propose_submit_order(
                payload={"symbol": "005930", "side": "HOLD",
                         "qty": 1, "order_type": "MARKET"},
                requested_by="agent",
            )

    def test_submit_negative_qty(self, handlers):
        with pytest.raises(PayloadValidationError, match="positive"):
            handlers.propose_submit_order(
                payload={"symbol": "005930", "side": "BUY",
                         "qty": -5, "order_type": "MARKET"},
                requested_by="agent",
            )

    def test_submit_zero_qty(self, handlers):
        with pytest.raises(PayloadValidationError, match="positive"):
            handlers.propose_submit_order(
                payload={"symbol": "005930", "side": "BUY",
                         "qty": 0, "order_type": "MARKET"},
                requested_by="agent",
            )

    def test_submit_limit_missing_price(self, handlers):
        with pytest.raises(PayloadValidationError, match="limit_price"):
            handlers.propose_submit_order(
                payload={"symbol": "005930", "side": "BUY",
                         "qty": 1, "order_type": "LIMIT"},
                requested_by="agent",
            )

    def test_submit_limit_negative_price(self, handlers):
        with pytest.raises(PayloadValidationError, match="positive"):
            handlers.propose_submit_order(
                payload={"symbol": "005930", "side": "BUY", "qty": 1,
                         "order_type": "LIMIT", "limit_price": -1},
                requested_by="agent",
            )

    def test_submit_invalid_order_type(self, handlers):
        with pytest.raises(PayloadValidationError, match="LIMIT/MARKET"):
            handlers.propose_submit_order(
                payload={"symbol": "005930", "side": "BUY",
                         "qty": 1, "order_type": "ICEBERG"},
                requested_by="agent",
            )

    def test_cancel_missing_broker_id(self, handlers):
        with pytest.raises(PayloadValidationError, match="broker_order_id"):
            handlers.propose_cancel_order(payload={}, requested_by="agent")

    def test_capacity_negative(self, handlers):
        with pytest.raises(PayloadValidationError, match="non-negative"):
            handlers.propose_set_capacity(
                payload={"strategy": "x", "capacity_krw": -1},
                requested_by="agent",
            )

    def test_kill_switch_invalid_action(self, handlers):
        with pytest.raises(PayloadValidationError, match="activate/deactivate"):
            handlers.propose_kill_switch(
                payload={"action": "pause"},
                requested_by="agent",
            )

    def test_payload_not_dict(self, handlers):
        with pytest.raises(PayloadValidationError, match="dict"):
            handlers.propose_submit_order(
                payload="not a dict",  # type: ignore[arg-type]
                requested_by="agent",
            )


# ===========================================================================
# 3. requested_by 검증
# ===========================================================================

class TestIdentity:
    def test_empty_requested_by(self, handlers):
        with pytest.raises(IdentityViolationError, match="required"):
            handlers.propose_submit_order(
                payload={"symbol": "005930", "side": "BUY",
                         "qty": 1, "order_type": "MARKET"},
                requested_by="",
            )

    def test_operator_impersonation_blocked(self, handlers):
        with pytest.raises(IdentityViolationError, match="operator"):
            handlers.propose_submit_order(
                payload={"symbol": "005930", "side": "BUY",
                         "qty": 1, "order_type": "MARKET"},
                requested_by="operator_alice",
            )

    def test_operator_uppercase_blocked(self, handlers):
        with pytest.raises(IdentityViolationError):
            handlers.propose_submit_order(
                payload={"symbol": "005930", "side": "BUY",
                         "qty": 1, "order_type": "MARKET"},
                requested_by="Operator",
            )


# ===========================================================================
# 4. cancel_proposal
# ===========================================================================

class TestCancelProposal:
    def test_cancel_own_proposal(self, handlers, store):
        out = handlers.propose_submit_order(
            payload={"symbol": "005930", "side": "BUY",
                     "qty": 1, "order_type": "MARKET"},
            requested_by="agent_a",
        )
        result = handlers.cancel_proposal(
            approval_id=out["approval_id"], requested_by="agent_a"
        )
        assert result["status"] == "CANCELLED"
        rec = store.get(out["approval_id"])
        assert rec.status == ApprovalStatus.CANCELLED

    def test_cannot_cancel_other_agents_proposal(self, handlers):
        out = handlers.propose_submit_order(
            payload={"symbol": "005930", "side": "BUY",
                     "qty": 1, "order_type": "MARKET"},
            requested_by="agent_a",
        )
        with pytest.raises(IdentityViolationError, match="own"):
            handlers.cancel_proposal(
                approval_id=out["approval_id"], requested_by="agent_b"
            )

    def test_cancel_unknown_id(self, handlers):
        with pytest.raises(ApprovalNotFoundError):
            handlers.cancel_proposal(
                approval_id="apv-19700101-deadbeefcafebabe",
                requested_by="agent",
            )


# ===========================================================================
# 5. query / list / recent
# ===========================================================================

class TestQueryAndList:
    def test_query_existing(self, handlers):
        out = handlers.propose_submit_order(
            payload={"symbol": "005930", "side": "BUY",
                     "qty": 1, "order_type": "MARKET"},
            requested_by="agent",
        )
        info = handlers.query_approval_status(approval_id=out["approval_id"])
        assert info["approval_id"] == out["approval_id"]
        assert info["status"] == "PROPOSED"
        assert info["payload"]["symbol"] == "005930"

    def test_query_unknown_raises(self, handlers):
        with pytest.raises(ApprovalNotFoundError):
            handlers.query_approval_status(
                approval_id="apv-19700101-deadbeefcafebabe"
            )

    def test_list_pending_returns_only_proposed_approved(
        self, handlers, store
    ):
        # 3개 propose
        ids = []
        for i in range(3):
            out = handlers.propose_submit_order(
                payload={"symbol": f"00593{i}", "side": "BUY",
                         "qty": 1, "order_type": "MARKET"},
                requested_by="agent",
            )
            ids.append(out["approval_id"])
        # 1개 거부
        store.reject(ids[0], decided_by="operator", reason="test")
        # 1개 승인
        store.approve(ids[1], decided_by="operator")

        result = handlers.list_pending_approvals()
        statuses = {r["status"] for r in result["records"]}
        assert "REJECTED" not in statuses
        assert "PROPOSED" in statuses or "APPROVED" in statuses

    def test_list_cap_enforced(self, handlers):
        result = handlers.list_pending_approvals(max_results=10_000)
        assert result["max_results_applied"] == LIST_HARD_MAX

    def test_recent_returns_only_terminal(self, handlers, store):
        out = handlers.propose_submit_order(
            payload={"symbol": "005930", "side": "BUY",
                     "qty": 1, "order_type": "MARKET"},
            requested_by="agent",
        )
        store.reject(out["approval_id"], decided_by="operator", reason="r")
        result = handlers.get_recent_decisions()
        assert result["count"] >= 1
        assert all(
            r["status"] in {"REJECTED", "EXECUTED", "EXEC_FAILED",
                            "EXPIRED", "CANCELLED"}
            for r in result["records"]
        )


# ===========================================================================
# 6. mode 격리
# ===========================================================================

class TestModeIsolation:
    def test_paper_handler_creates_paper_records(self, handlers, store):
        out = handlers.propose_submit_order(
            payload={"symbol": "005930", "side": "BUY",
                     "qty": 1, "order_type": "MARKET"},
            requested_by="agent",
        )
        rec = store.get(out["approval_id"])
        assert rec.mode == "paper"

    def test_live_handler_creates_live_records(self, live_handlers, store):
        out = live_handlers.propose_submit_order(
            payload={"symbol": "005930", "side": "BUY",
                     "qty": 1, "order_type": "MARKET"},
            requested_by="agent",
        )
        rec = store.get(out["approval_id"])
        assert rec.mode == "live"

    def test_invalid_mode_raises(self, store):
        with pytest.raises(ValueError, match="paper.*live"):
            WriteHandlers(store=store, mode="dryrun")


# ===========================================================================
# 7. Invariants — 시크릿 키 미반환
# ===========================================================================

class TestInvariants:
    def test_no_secret_keys_in_responses(self, handlers, store):
        out = handlers.propose_submit_order(
            payload={"symbol": "005930", "side": "BUY",
                     "qty": 1, "order_type": "MARKET"},
            requested_by="agent",
        )
        info = handlers.query_approval_status(approval_id=out["approval_id"])
        # 응답 dict 키에 시크릿 키워드 없음
        for k in info.keys():
            kl = k.lower()
            for forbidden in ("password", "secret", "token", "api_key",
                              "apikey", "appkey", "appsecret"):
                assert forbidden not in kl, f"forbidden key: {k}"

    def test_list_tools(self, handlers):
        tools = handlers.list_tools()
        assert len(tools) == 8
        assert "propose_submit_order" in tools
        assert "query_approval_status" in tools
