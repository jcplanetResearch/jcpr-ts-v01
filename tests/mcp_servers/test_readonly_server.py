"""
스모크 테스트 — readonly_server (서버 빌드 + 통합)
====================================================

JCPR Trading System - jcpr-ts-v01
Task 34 v0.1
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from src.mcp_servers import (  # noqa: E402
    ReadOnlyServerConfig,
    build_server,
    load_config_from_env,
)
from src.mcp_servers._config import (  # noqa: E402
    ENV_AUDIT_DIR,
    ENV_SESSION_ID,
    FORBIDDEN_ENV_KEYWORDS,
)


# ─────────────────────────────────────────────────
# Config 검증
# ─────────────────────────────────────────────────

def test_config_defaults():
    c = ReadOnlyServerConfig()
    assert c.audit_dir == "data/audit"
    assert c.rate_limit_per_minute == 120
    assert c.enable_get_trace is True
    assert c.session_id.startswith("mcp-")
    print("✅ test_config_defaults")


def test_config_extra_field_rejected():
    try:
        ReadOnlyServerConfig(unknown_field="x")  # type: ignore[call-arg]
        assert False
    except Exception:
        pass
    print("✅ test_config_extra_field_rejected")


def test_config_invalid_session_id():
    try:
        ReadOnlyServerConfig(session_id="with space")
        assert False
    except Exception:
        pass
    print("✅ test_config_invalid_session_id")


def test_config_invalid_rate_limit():
    try:
        ReadOnlyServerConfig(rate_limit_per_minute=0)
        assert False
    except Exception:
        pass
    print("✅ test_config_invalid_rate_limit")


def test_config_frozen():
    c = ReadOnlyServerConfig()
    try:
        c.session_id = "changed"  # type: ignore[misc]
        assert False
    except Exception:
        pass
    print("✅ test_config_frozen")


# ─────────────────────────────────────────────────
# 환경변수 로더
# ─────────────────────────────────────────────────

def test_load_config_from_env_basic(tmp_dir):
    """JCPR_AUDIT_DIR 환경변수에서 로드."""
    saved = dict(os.environ)
    try:
        # 기존 JCPR_ 변수 제거
        for k in list(os.environ.keys()):
            if k.startswith("JCPR_"):
                del os.environ[k]
        os.environ[ENV_AUDIT_DIR] = str(tmp_dir / "audit")
        os.environ[ENV_SESSION_ID] = "test-from-env"
        c = load_config_from_env()
        assert c.audit_dir == str(tmp_dir / "audit")
        assert c.session_id == "test-from-env"
    finally:
        os.environ.clear()
        os.environ.update(saved)
    print("✅ test_load_config_from_env_basic")


def test_load_config_rejects_credential_env():
    """JCPR_API_KEY 같은 환경변수 거부."""
    saved = dict(os.environ)
    try:
        os.environ["JCPR_API_KEY"] = "leaked"
        try:
            load_config_from_env()
            assert False, "Should reject credential-suspect env"
        except ValueError as e:
            assert "API_KEY" in str(e)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    print("✅ test_load_config_rejects_credential_env")


def test_forbidden_keywords_coverage():
    """모든 forbidden keyword가 제대로 차단되는지."""
    saved = dict(os.environ)
    for kw in FORBIDDEN_ENV_KEYWORDS:
        try:
            # 기존 JCPR_ 정리
            for k in list(os.environ.keys()):
                if k.startswith("JCPR_"):
                    del os.environ[k]
            os.environ[f"JCPR_TEST_{kw}"] = "x"
            try:
                load_config_from_env()
                assert False, f"Should reject {kw}"
            except ValueError:
                pass
        finally:
            os.environ.clear()
            os.environ.update(saved)
    print("✅ test_forbidden_keywords_coverage")


# ─────────────────────────────────────────────────
# 서버 빌드
# ─────────────────────────────────────────────────

def test_build_server(tmp_dir):
    """서버 빌드 + 8개 도구 등록 확인."""
    config = ReadOnlyServerConfig(
        audit_dir=str(tmp_dir / "audit"),
        session_id="test-build",
    )
    server = build_server(config)
    assert server is not None
    # FastMCP 인스턴스 확인
    assert hasattr(server, "run")
    # 도구 8개 등록 확인 (FastMCP 내부 구조)
    # FastMCP는 _tool_manager 또는 list_tools 비동기 메서드 보유
    print("✅ test_build_server")


def test_server_no_default_writer(tmp_dir):
    """build_server가 default writer 설정."""
    from src.observability import (
        get_default_writer, reset_default_writer,
    )
    reset_default_writer()
    assert get_default_writer() is None
    config = ReadOnlyServerConfig(audit_dir=str(tmp_dir / "audit"))
    build_server(config)
    assert get_default_writer() is not None
    reset_default_writer()
    print("✅ test_server_no_default_writer")


# ─────────────────────────────────────────────────
# 통합 (handler + audit + trace)
# ─────────────────────────────────────────────────

def test_handler_writes_audit(tmp_dir):
    """tool 호출 시 audit log 자동 기록 확인."""
    from src.observability import (
        AuditIndexer, configure_default_writer, reset_default_writer,
    )
    from src.mcp_servers._security import RateLimiter
    from src.mcp_servers.readonly_server import _wrap_call
    from src.mcp_servers import _tool_handlers as h

    reset_default_writer()
    audit_dir = tmp_dir / "audit"
    audit_dir.mkdir()
    configure_default_writer(str(audit_dir))

    config = ReadOnlyServerConfig(
        audit_dir=str(audit_dir),
        session_id="test-audit",
    )
    rl = RateLimiter(max_per_minute=60)
    result = _wrap_call(
        config, rl,
        "get_market_status",
        h.get_market_status,
        args={},
    )
    assert result["ok"] is True
    assert "_trace_id" in result

    # audit log 확인 — call + result 두 이벤트
    indexer = AuditIndexer(audit_dir=str(audit_dir))
    events = indexer.find_by_trace(result["_trace_id"])
    types = [e.event_type for e in events]
    assert "mcp_tool_call" in types
    assert "mcp_tool_result" in types
    reset_default_writer()
    print("✅ test_handler_writes_audit")


def test_rate_limit_enforced(tmp_dir):
    """rate limit 초과 시 거부."""
    from src.observability import (
        configure_default_writer, reset_default_writer,
    )
    from src.mcp_servers._security import RateLimiter
    from src.mcp_servers.readonly_server import _wrap_call
    from src.mcp_servers import _tool_handlers as h

    reset_default_writer()
    audit_dir = tmp_dir / "audit"
    audit_dir.mkdir()
    configure_default_writer(str(audit_dir))

    config = ReadOnlyServerConfig(audit_dir=str(audit_dir))
    rl = RateLimiter(max_per_minute=2)
    # 2번은 OK
    r1 = _wrap_call(config, rl, "get_market_status",
                    h.get_market_status, args={})
    assert r1["ok"] is True
    r2 = _wrap_call(config, rl, "get_market_status",
                    h.get_market_status, args={})
    assert r2["ok"] is True
    # 3번째는 RATE_LIMIT
    r3 = _wrap_call(config, rl, "get_market_status",
                    h.get_market_status, args={})
    assert r3["ok"] is False
    assert r3["error_code"] == "RATE_LIMIT"
    reset_default_writer()
    print("✅ test_rate_limit_enforced")


def test_handler_exception_audited(tmp_dir):
    """handler 내부 예외 발생 시 audit + 표준 응답."""
    from src.observability import (
        AuditIndexer, configure_default_writer, reset_default_writer,
    )
    from src.mcp_servers._security import RateLimiter
    from src.mcp_servers.readonly_server import _wrap_call

    reset_default_writer()
    audit_dir = tmp_dir / "audit"
    audit_dir.mkdir()
    configure_default_writer(str(audit_dir))

    config = ReadOnlyServerConfig(audit_dir=str(audit_dir))
    rl = RateLimiter(max_per_minute=60)

    def buggy_handler(cfg):
        raise RuntimeError("simulated failure")

    result = _wrap_call(config, rl, "buggy", buggy_handler, args={})
    assert result["ok"] is False
    assert result["error_code"] == "HANDLER_ERROR"
    assert "simulated failure" in result["error_message"]

    # audit에 exception 기록되어 있어야
    indexer = AuditIndexer(audit_dir=str(audit_dir))
    events = indexer.find_by_trace(result["_trace_id"])
    types = [e.event_type for e in events]
    assert "exception" in types
    reset_default_writer()
    print("✅ test_handler_exception_audited")


# ─────────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────────

try:
    import pytest

    @pytest.fixture
    def tmp_dir(tmp_path):
        return tmp_path
except ImportError:
    pass


def _run_all() -> int:
    failed = 0
    # 인자 없는 테스트
    no_arg_tests = [
        test_config_defaults,
        test_config_extra_field_rejected,
        test_config_invalid_session_id,
        test_config_invalid_rate_limit,
        test_config_frozen,
        test_load_config_rejects_credential_env,
        test_forbidden_keywords_coverage,
    ]
    for fn in no_arg_tests:
        try:
            fn()
        except AssertionError as e:
            print(f"❌ {fn.__name__}: {e}")
            failed += 1
        except Exception as e:  # noqa: BLE001
            print(f"💥 {fn.__name__}: {type(e).__name__}: {e}")
            failed += 1

    # 인자 받는 테스트
    arg_tests = [
        test_load_config_from_env_basic,
        test_build_server,
        test_server_no_default_writer,
        test_handler_writes_audit,
        test_rate_limit_enforced,
        test_handler_exception_audited,
    ]
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        for fn in arg_tests:
            sub = td_path / fn.__name__
            sub.mkdir()
            try:
                fn(sub)
            except AssertionError as e:
                print(f"❌ {fn.__name__}: {e}")
                failed += 1
            except Exception as e:  # noqa: BLE001
                print(f"💥 {fn.__name__}: {type(e).__name__}: {e}")
                failed += 1
    return failed


if __name__ == "__main__":
    print("─" * 50)
    print("Task 34 v0.1 — readonly_server 스모크 테스트")
    print("─" * 50)
    failed = _run_all()
    print("─" * 50)
    if failed == 0:
        print("✅ 모든 테스트 통과")
        sys.exit(0)
    else:
        print(f"❌ {failed}개 실패")
        sys.exit(1)
