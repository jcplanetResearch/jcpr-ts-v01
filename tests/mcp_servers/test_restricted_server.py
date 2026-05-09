"""
tests/mcp_servers/test_restricted_server.py — JCPR-ts-v01 (Phase 2-B)
======================================================================

RestrictedServer 단위 테스트.

검증 카테고리:
  1. tool dispatch — 8개 도구 모두 list/call 동작
  2. 에러 분류 (error_kind) — 각 에러 카테고리 매핑
  3. 시크릿 누설 차단 — 응답에 시크릿 의심 키 발견 시 internal로 차단
  4. mode 정합성 — gateway/handlers/config 불일치 시 init 실패
  5. status_snapshot — 시크릿 없는 진단 정보
  6. build_restricted_server 팩토리
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.execution.approval_store import ApprovalStore
from src.execution.execution_gateway import ExecutionGateway
from src.mcp_servers._config import ServerConfig
from src.mcp_servers._write_handlers import WriteHandlers
from src.mcp_servers.restricted_server import (
    RestrictedServer,
    ToolResult,
    _scan_for_secrets,
    build_restricted_server,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeBroker:
    def __init__(self, mode: str = "paper") -> None:
        self.mode = mode
    def submit_order(self, payload):
        return {"broker_order_id": "FAKE-1", "status": "ACCEPTED"}
    def cancel_order(self, payload):
        return {"status": "CANCELLED"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def cfg(tmp_path) -> ServerConfig:
    return ServerConfig(
        approval_db_path=tmp_path / "approvals.sqlite",
        mode="paper",
        allow_live=False,
        project_root=tmp_path,
    )

@pytest.fixture
def store(cfg) -> ApprovalStore:
    return ApprovalStore(db_path=cfg.approval_db_path)

@pytest.fixture
def server(cfg, store) -> RestrictedServer:
    pb = FakeBroker(mode="paper")
    gw = ExecutionGateway(store=store, paper_broker=pb, mode="paper")
    handlers = WriteHandlers(store=store, mode="paper")
    return RestrictedServer(
        config=cfg, store=store, gateway=gw, handlers=handlers,
    )


# ===========================================================================
# 1. Tool dispatch
# ===========================================================================

class TestToolDispatch:
    def test_list_tools_returns_8(self, server):
        tools = server.list_tools()
        assert len(tools) == 8
        assert "propose_submit_order" in tools

    def test_call_propose_submit_order(self, server):
        result = server.call_tool(
            "propose_submit_order",
            payload={"symbol": "005930", "side": "BUY",
                     "qty": 10, "order_type": "MARKET"},
            requested_by="agent",
        )
        assert isinstance(result, ToolResult)
        assert result.ok is True
        assert result.error is None
        assert result.result["status"] == "PROPOSED"
        assert result.elapsed_ms >= 0

    def test_call_query(self, server):
        # propose 후 query
        r1 = server.call_tool(
            "propose_submit_order",
            payload={"symbol": "005930", "side": "BUY",
                     "qty": 1, "order_type": "MARKET"},
            requested_by="agent",
        )
        r2 = server.call_tool(
            "query_approval_status",
            approval_id=r1.result["approval_id"],
        )
        assert r2.ok is True
        assert r2.result["status"] == "PROPOSED"

    def test_call_list_pending(self, server):
        server.call_tool(
            "propose_submit_order",
            payload={"symbol": "005930", "side": "BUY",
                     "qty": 1, "order_type": "MARKET"},
            requested_by="agent",
        )
        r = server.call_tool("list_pending_approvals")
        assert r.ok is True
        assert r.result["count"] >= 1

    def test_call_get_recent(self, server):
        r = server.call_tool("get_recent_decisions")
        assert r.ok is True


# ===========================================================================
# 2. Error classification
# ===========================================================================

class TestErrorClassification:
    def test_unknown_tool(self, server):
        r = server.call_tool("nonexistent_tool")
        assert r.ok is False
        assert r.error_kind == "unknown_tool"

    def test_validation_error(self, server):
        r = server.call_tool(
            "propose_submit_order",
            payload={"symbol": "005930"},  # missing side, qty, order_type
            requested_by="agent",
        )
        assert r.ok is False
        assert r.error_kind == "validation"

    def test_identity_error(self, server):
        r = server.call_tool(
            "propose_submit_order",
            payload={"symbol": "005930", "side": "BUY",
                     "qty": 1, "order_type": "MARKET"},
            requested_by="operator_alice",
        )
        assert r.ok is False
        assert r.error_kind == "identity"

    def test_not_found(self, server):
        r = server.call_tool(
            "query_approval_status",
            approval_id="apv-19700101-deadbeefcafebabe",
        )
        assert r.ok is False
        assert r.error_kind == "not_found"

    def test_typeerror_classified_as_validation(self, server):
        # 잘못된 키워드 인자
        r = server.call_tool(
            "propose_submit_order",
            wrong_kwarg="x",
        )
        assert r.ok is False
        assert r.error_kind == "validation"


# ===========================================================================
# 3. 시크릿 누설 차단
# ===========================================================================

class TestSecretLeakProtection:
    def test_scan_finds_password_key(self):
        findings = _scan_for_secrets({"data": {"password": "x"}})
        assert findings
        assert "data.password" in findings

    def test_scan_finds_api_key(self):
        findings = _scan_for_secrets({"api_key": "x"})
        assert "api_key" in findings

    def test_scan_finds_nested_token(self):
        findings = _scan_for_secrets({"a": {"b": [{"access_token": "x"}]}})
        assert any("access_token" in f for f in findings)

    def test_scan_clean_dict(self):
        findings = _scan_for_secrets({
            "approval_id": "apv-...", "status": "PROPOSED",
            "broker_order_id": "BRK-1",
        })
        assert findings == []

    def test_scan_does_not_check_values(self):
        # 값에 'password' 문자열이 있어도 키가 정상이면 OK
        findings = _scan_for_secrets({"reason": "wrong password input"})
        assert findings == []

    def test_server_blocks_secret_leak(self, server, monkeypatch):
        """핸들러가 시크릿 키를 포함한 응답을 반환하면 server가 차단."""
        from src.mcp_servers._write_handlers import WriteHandlers
        # 더러운(malicious) 핸들러 시뮬레이션
        def bad_handler(**kwargs):
            return {"approval_id": "apv-x", "api_key": "leaked"}
        monkeypatch.setitem(
            server._tool_registry, "propose_submit_order", bad_handler
        )
        r = server.call_tool(
            "propose_submit_order",
            payload={"symbol": "005930"}, requested_by="agent",
        )
        assert r.ok is False
        assert r.error_kind == "internal"
        assert "secret" in r.error.lower() or "차단" in r.error


# ===========================================================================
# 4. Mode 정합성
# ===========================================================================

class TestModeConsistency:
    def test_gateway_mode_mismatch_raises(self, cfg, store):
        live_pb = FakeBroker(mode="paper")
        live_lb = FakeBroker(mode="live")
        gw = ExecutionGateway(
            store=store, paper_broker=live_pb, live_broker=live_lb,
            mode="live", allow_live=True,
        )
        handlers = WriteHandlers(store=store, mode="paper")
        # cfg.mode='paper' but gateway.mode='live'
        with pytest.raises(ValueError, match="mode mismatch"):
            RestrictedServer(
                config=cfg, store=store, gateway=gw, handlers=handlers,
            )

    def test_handlers_mode_mismatch_raises(self, cfg, store):
        pb = FakeBroker(mode="paper")
        gw = ExecutionGateway(store=store, paper_broker=pb, mode="paper")
        handlers = WriteHandlers(store=store, mode="live")  # 불일치
        # 메시지는 한국어 "불일치" 또는 영어 "mismatch" 어느 쪽이든 OK
        with pytest.raises(ValueError, match="mismatch|불일치"):
            RestrictedServer(
                config=cfg, store=store, gateway=gw, handlers=handlers,
            )


# ===========================================================================
# 5. status_snapshot
# ===========================================================================

class TestStatusSnapshot:
    def test_snapshot_no_secrets(self, server):
        snap = server.status_snapshot()
        assert snap["mode"] == "paper"
        assert snap["allow_live"] is False
        assert len(snap["tools"]) == 8
        assert "gateway" in snap
        assert "approval_db_path" in snap
        # 시크릿 키워드 없음 — DB 경로는 시크릿 정보가 아니므로 제외
        # (테스트의 임시 디렉터리 이름이 우연히 'secret'을 포함할 수 있음)
        snap_no_path = {k: v for k, v in snap.items() if k != "approval_db_path"}
        s = str(snap_no_path).lower()
        for forbidden in ("password", "appsecret", "appkey", "private_key",
                          "api_key", "apikey"):
            assert forbidden not in s, f"forbidden keyword in snapshot: {forbidden}"


# ===========================================================================
# 6. build_restricted_server 팩토리
# ===========================================================================

class TestFactory:
    def test_factory_assembles_consistent_server(self, cfg):
        pb = FakeBroker(mode="paper")
        server = build_restricted_server(config=cfg, paper_broker=pb)
        assert isinstance(server, RestrictedServer)
        assert server.list_tools() == [
            "propose_submit_order",
            "propose_cancel_order",
            "propose_set_capacity",
            "propose_kill_switch",
            "cancel_proposal",
            "query_approval_status",
            "list_pending_approvals",
            "get_recent_decisions",
        ]

    def test_factory_live_requires_live_broker(self, tmp_path):
        live_cfg = ServerConfig(
            approval_db_path=tmp_path / "x.sqlite",
            mode="live", allow_live=True, project_root=tmp_path,
        )
        pb = FakeBroker(mode="paper")
        with pytest.raises(ValueError, match="live_broker"):
            build_restricted_server(config=live_cfg, paper_broker=pb)
