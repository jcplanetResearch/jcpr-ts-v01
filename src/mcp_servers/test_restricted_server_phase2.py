"""Stage 2A unit tests for restricted_server + config — operator-env version.
Phase 1 진짜 approval_store 직접 사용.
"""
from __future__ import annotations
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import pytest

from src.execution.approval_store import ApprovalStore
from src.execution.execution_gateway import ExecutionGateway
from src.mcp_servers._config import ConfigError, RestrictedServerConfig, load_restricted_config
from src.mcp_servers._write_handlers import build_handlers
from src.mcp_servers.restricted_server import RestrictedMCPServer, build_server


class _FakeOrderResponse:
    def __init__(self):
        self.accepted = True; self.broker_order_id = "B-fake"
        self.client_order_id = "c1"; self.filled_quantity = Decimal("1")
        self.average_price = Decimal("75000"); self.error_code = None
        self.error_message = None; self.submitted_at_utc = datetime.now(timezone.utc)

class _MockBroker:
    def __init__(self):
        self.place_order_calls = []; self.cancel_order_calls = []
    def place_order(self, req, *, approval_id):
        self.place_order_calls.append(approval_id)
        return _FakeOrderResponse()
    def cancel_order(self, *, broker_order_id, symbol, approval_id):
        self.cancel_order_calls.append(broker_order_id)
        return {"cancelled": True}


class _FakeMCP:
    def __init__(self, name):
        self.name = name; self.registered_tools = []
    def tool(self):
        def decorator(func):
            self.registered_tools.append(func.__name__)
            return func
        return decorator
    def run(self, transport="stdio"): pass


def _fake_factory(name): return _FakeMCP(name)


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------
class TestRestrictedServerConfig:
    def test_paper_mode_ok(self, tmp_path):
        cfg = RestrictedServerConfig(
            approval_db_path=tmp_path / "approvals.sqlite",
            audit_dir=tmp_path / "audit",
        )
        assert cfg.mode == "paper"

    def test_live_without_allow_live_rejected(self, tmp_path):
        with pytest.raises(ConfigError, match="allow_live"):
            RestrictedServerConfig(
                approval_db_path=tmp_path / "approvals.sqlite",
                audit_dir=tmp_path / "audit",
                mode="live", allow_live=False,
            )

    def test_live_with_allow_live_ok(self, tmp_path):
        cfg = RestrictedServerConfig(
            approval_db_path=tmp_path / "approvals.sqlite",
            audit_dir=tmp_path / "audit",
            mode="live", allow_live=True,
        )
        assert cfg.allow_live is True

    def test_invalid_mode_rejected(self, tmp_path):
        with pytest.raises(ConfigError):
            RestrictedServerConfig(
                approval_db_path=tmp_path / "approvals.sqlite",
                audit_dir=tmp_path / "audit",
                mode="testnet",
            )

    def test_empty_db_path_rejected(self, tmp_path):
        with pytest.raises(ConfigError):
            RestrictedServerConfig(
                approval_db_path=Path(""),
                audit_dir=tmp_path / "audit",
            )


class TestLoadRestrictedConfig:
    def test_loads_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JCPR_APPROVAL_DB", str(tmp_path / "approvals.sqlite"))
        monkeypatch.setenv("JCPR_AUDIT_DIR", str(tmp_path / "audit"))
        monkeypatch.delenv("JCPR_MCP_APPROVAL_DB", raising=False)
        monkeypatch.delenv("JCPR_EXEC_APPROVAL_DB", raising=False)
        cfg = load_restricted_config()
        assert cfg.mode == "paper"

    def test_missing_approval_db_rejected(self, monkeypatch):
        monkeypatch.delenv("JCPR_APPROVAL_DB", raising=False)
        with pytest.raises(ConfigError, match="JCPR_APPROVAL_DB"):
            load_restricted_config()

    def test_legacy_mcp_var_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JCPR_APPROVAL_DB", str(tmp_path / "approvals.sqlite"))
        monkeypatch.setenv("JCPR_AUDIT_DIR", str(tmp_path / "audit"))
        monkeypatch.setenv("JCPR_MCP_APPROVAL_DB", str(tmp_path / "old.sqlite"))
        with pytest.raises(ConfigError, match="JCPR_MCP_APPROVAL_DB"):
            load_restricted_config()

    def test_legacy_exec_var_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JCPR_APPROVAL_DB", str(tmp_path / "approvals.sqlite"))
        monkeypatch.setenv("JCPR_AUDIT_DIR", str(tmp_path / "audit"))
        monkeypatch.setenv("JCPR_EXEC_APPROVAL_DB", str(tmp_path / "old.sqlite"))
        with pytest.raises(ConfigError, match="JCPR_EXEC_APPROVAL_DB"):
            load_restricted_config()

    def test_live_mode_requires_allow_live_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JCPR_APPROVAL_DB", str(tmp_path / "approvals.sqlite"))
        monkeypatch.setenv("JCPR_AUDIT_DIR", str(tmp_path / "audit"))
        monkeypatch.setenv("JCPR_MODE", "live")
        monkeypatch.delenv("JCPR_ALLOW_LIVE", raising=False)
        monkeypatch.delenv("JCPR_MCP_APPROVAL_DB", raising=False)
        monkeypatch.delenv("JCPR_EXEC_APPROVAL_DB", raising=False)
        with pytest.raises(ConfigError, match="allow_live"):
            load_restricted_config()

    def test_live_mode_with_allow_live_ok(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JCPR_APPROVAL_DB", str(tmp_path / "approvals.sqlite"))
        monkeypatch.setenv("JCPR_AUDIT_DIR", str(tmp_path / "audit"))
        monkeypatch.setenv("JCPR_MODE", "live")
        monkeypatch.setenv("JCPR_ALLOW_LIVE", "1")
        monkeypatch.delenv("JCPR_MCP_APPROVAL_DB", raising=False)
        monkeypatch.delenv("JCPR_EXEC_APPROVAL_DB", raising=False)
        cfg = load_restricted_config()
        assert cfg.mode == "live" and cfg.allow_live is True


# ---------------------------------------------------------------------------
# Server build tests
# ---------------------------------------------------------------------------
class TestBuildServer:
    @pytest.fixture
    def cfg(self, tmp_path):
        return RestrictedServerConfig(
            approval_db_path=tmp_path / "approvals.sqlite",
            audit_dir=tmp_path / "audit",
        )

    def test_build_registers_8_tools(self, cfg):
        server = build_server(config=cfg, broker=_MockBroker(), mcp_factory=_fake_factory)
        assert len(server._mcp.registered_tools) == 8

    def test_exposed_tool_names(self, cfg):
        server = build_server(config=cfg, broker=_MockBroker(), mcp_factory=_fake_factory)
        expected = {
            "request_submit_order", "request_cancel_order", "request_set_capacity",
            "request_kill_switch", "list_pending_approvals", "get_approval_detail",
            "cancel_proposed_action", "execute_approved_action",
        }
        assert set(server._mcp.registered_tools) == expected

    def test_internal_handlers_not_exposed(self, cfg):
        server = build_server(config=cfg, broker=_MockBroker(), mcp_factory=_fake_factory)
        assert "approve_action" not in server._mcp.registered_tools
        assert "reject_action" not in server._mcp.registered_tools

    def test_safe_call_wraps_handler_error(self, cfg):
        server = build_server(config=cfg, broker=_MockBroker(), mcp_factory=_fake_factory)
        def bad():
            from src.mcp_servers._write_handlers import WriteHandlerError
            raise WriteHandlerError("bad")
        r = server._safe_call(bad)
        assert r["ok"] is False and r["error_kind"] == "handler"

    def test_safe_call_wraps_unexpected(self, cfg):
        server = build_server(config=cfg, broker=_MockBroker(), mcp_factory=_fake_factory)
        r = server._safe_call(lambda: (_ for _ in ()).throw(RuntimeError("kaboom")))
        assert r["ok"] is False and r["error_kind"] == "internal"

    def test_safe_call_happy_path(self, cfg):
        server = build_server(config=cfg, broker=_MockBroker(), mcp_factory=_fake_factory)
        r = server._safe_call(lambda: {"approval_id": "apv-test"})
        assert r["ok"] is True

    def test_interrupt_check_callable(self, cfg):
        server = build_server(config=cfg, broker=_MockBroker(), mcp_factory=_fake_factory)
        assert server.check_interrupt() is False
        server._interrupt_flag = True
        assert server.check_interrupt() is True

    def test_gateway_wired_to_server_interrupt_check(self, cfg):
        server = build_server(config=cfg, broker=_MockBroker(), mcp_factory=_fake_factory)
        server._interrupt_flag = False
        assert server._gateway._interrupt_check() is False
        server._interrupt_flag = True
        assert server._gateway._interrupt_check() is True

    def test_close_releases_store(self, cfg):
        server = build_server(config=cfg, broker=_MockBroker(), mcp_factory=_fake_factory)
        server.close()
        assert server._store._closed is True
