"""Stage 2A tests — restricted_server + config with real Phase 1 ApprovalStore."""
from __future__ import annotations
from pathlib import Path
import pytest

from src.mcp_servers._config import ConfigError, RestrictedServerConfig, load_restricted_config
from src.mcp_servers.restricted_server import build_server
from tests._stubs import MockBroker


class _FakeMCP:
    def __init__(self, name):
        self.name = name; self.registered_tools = []
    def tool(self):
        def d(func):
            self.registered_tools.append(func.__name__)
            return func
        return d
    def run(self, transport="stdio"): pass

def _fmcp(name): return _FakeMCP(name)


class TestRestrictedServerConfig:
    def test_paper_mode_ok(self, tmp_path):
        cfg = RestrictedServerConfig(
            approval_db_path=tmp_path / "a.sqlite", audit_dir=tmp_path / "a")
        assert cfg.mode == "paper"

    def test_live_without_allow_live_rejected(self, tmp_path):
        with pytest.raises(ConfigError, match="allow_live"):
            RestrictedServerConfig(
                approval_db_path=tmp_path / "a.sqlite", audit_dir=tmp_path / "a",
                mode="live", allow_live=False)

    def test_live_with_allow_live_ok(self, tmp_path):
        cfg = RestrictedServerConfig(
            approval_db_path=tmp_path / "a.sqlite", audit_dir=tmp_path / "a",
            mode="live", allow_live=True)
        assert cfg.allow_live is True

    def test_invalid_mode_rejected(self, tmp_path):
        with pytest.raises(ConfigError):
            RestrictedServerConfig(
                approval_db_path=tmp_path / "a.sqlite", audit_dir=tmp_path / "a",
                mode="testnet")

    def test_empty_db_path_rejected(self, tmp_path):
        with pytest.raises(ConfigError):
            RestrictedServerConfig(approval_db_path=Path(""), audit_dir=tmp_path / "a")


class TestLoadRestrictedConfig:
    def test_loads_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JCPR_APPROVAL_DB", str(tmp_path / "a.sqlite"))
        monkeypatch.setenv("JCPR_AUDIT_DIR", str(tmp_path / "a"))
        monkeypatch.delenv("JCPR_MCP_APPROVAL_DB", raising=False)
        monkeypatch.delenv("JCPR_EXEC_APPROVAL_DB", raising=False)
        assert load_restricted_config().mode == "paper"

    def test_missing_approval_db_rejected(self, monkeypatch):
        monkeypatch.delenv("JCPR_APPROVAL_DB", raising=False)
        with pytest.raises(ConfigError, match="JCPR_APPROVAL_DB"):
            load_restricted_config()

    def test_legacy_mcp_var_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JCPR_APPROVAL_DB", str(tmp_path / "a.sqlite"))
        monkeypatch.setenv("JCPR_AUDIT_DIR", str(tmp_path / "a"))
        monkeypatch.setenv("JCPR_MCP_APPROVAL_DB", str(tmp_path / "old.sqlite"))
        with pytest.raises(ConfigError, match="JCPR_MCP_APPROVAL_DB"):
            load_restricted_config()

    def test_legacy_exec_var_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JCPR_APPROVAL_DB", str(tmp_path / "a.sqlite"))
        monkeypatch.setenv("JCPR_AUDIT_DIR", str(tmp_path / "a"))
        monkeypatch.setenv("JCPR_EXEC_APPROVAL_DB", str(tmp_path / "old.sqlite"))
        with pytest.raises(ConfigError, match="JCPR_EXEC_APPROVAL_DB"):
            load_restricted_config()

    def test_live_mode_requires_allow_live(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JCPR_APPROVAL_DB", str(tmp_path / "a.sqlite"))
        monkeypatch.setenv("JCPR_AUDIT_DIR", str(tmp_path / "a"))
        monkeypatch.setenv("JCPR_MODE", "live")
        monkeypatch.delenv("JCPR_ALLOW_LIVE", raising=False)
        monkeypatch.delenv("JCPR_MCP_APPROVAL_DB", raising=False)
        monkeypatch.delenv("JCPR_EXEC_APPROVAL_DB", raising=False)
        with pytest.raises(ConfigError, match="allow_live"):
            load_restricted_config()

    def test_live_mode_with_allow_live_ok(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JCPR_APPROVAL_DB", str(tmp_path / "a.sqlite"))
        monkeypatch.setenv("JCPR_AUDIT_DIR", str(tmp_path / "a"))
        monkeypatch.setenv("JCPR_MODE", "live")
        monkeypatch.setenv("JCPR_ALLOW_LIVE", "1")
        monkeypatch.delenv("JCPR_MCP_APPROVAL_DB", raising=False)
        monkeypatch.delenv("JCPR_EXEC_APPROVAL_DB", raising=False)
        cfg = load_restricted_config()
        assert cfg.mode == "live" and cfg.allow_live is True


class TestBuildServer:
    @pytest.fixture
    def cfg(self, tmp_path):
        return RestrictedServerConfig(
            approval_db_path=tmp_path / "a.sqlite", audit_dir=tmp_path / "a")

    def test_build_registers_8_tools(self, cfg):
        s = build_server(config=cfg, broker=MockBroker(), mcp_factory=_fmcp)
        assert len(s._mcp.registered_tools) == 8

    def test_exposed_tool_names(self, cfg):
        s = build_server(config=cfg, broker=MockBroker(), mcp_factory=_fmcp)
        assert set(s._mcp.registered_tools) == {
            "request_submit_order", "request_cancel_order", "request_set_capacity",
            "request_kill_switch", "list_pending_approvals", "get_approval_detail",
            "cancel_proposed_action", "execute_approved_action",
        }

    def test_internal_handlers_not_exposed(self, cfg):
        s = build_server(config=cfg, broker=MockBroker(), mcp_factory=_fmcp)
        assert "approve_action" not in s._mcp.registered_tools
        assert "reject_action" not in s._mcp.registered_tools

    def test_safe_call_wraps_handler_error(self, cfg):
        s = build_server(config=cfg, broker=MockBroker(), mcp_factory=_fmcp)
        def bad():
            from src.mcp_servers._write_handlers import WriteHandlerError
            raise WriteHandlerError("bad")
        r = s._safe_call(bad)
        assert r["ok"] is False and r["error_kind"] == "handler"

    def test_safe_call_wraps_unexpected(self, cfg):
        s = build_server(config=cfg, broker=MockBroker(), mcp_factory=_fmcp)
        r = s._safe_call(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert r["ok"] is False and r["error_kind"] == "internal"

    def test_safe_call_happy_path(self, cfg):
        s = build_server(config=cfg, broker=MockBroker(), mcp_factory=_fmcp)
        r = s._safe_call(lambda: {"ok": True})
        assert r["ok"] is True

    def test_interrupt_check_callable(self, cfg):
        s = build_server(config=cfg, broker=MockBroker(), mcp_factory=_fmcp)
        assert s.check_interrupt() is False
        s._interrupt_flag = True
        assert s.check_interrupt() is True

    def test_gateway_wired_to_server_interrupt_check(self, cfg):
        s = build_server(config=cfg, broker=MockBroker(), mcp_factory=_fmcp)
        s._interrupt_flag = False
        assert s._gateway._interrupt_check() is False
        s._interrupt_flag = True
        assert s._gateway._interrupt_check() is True

    def test_close_does_not_raise(self, cfg):
        # Phase 1 ApprovalStore에 close() 없어도 에러 없이 완료
        s = build_server(config=cfg, broker=MockBroker(), mcp_factory=_fmcp)
        s.close()  # must not raise
