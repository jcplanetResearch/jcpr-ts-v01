"""스모크 테스트 (Smoke Test) — Task 40 v0.1 Human Approval Workflow."""

import io
import json
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.execution.approval import (
    ApprovalDecision, ApprovalProvider, ApprovalRequest,
    AutoApproveProvider, DenyAllProvider,
)
from src.execution.approval_audit import ApprovalAuditLog
from src.execution.approval_cli import CLIApprovalProvider
from src.execution.approval_composite import CompositeApprovalProvider
from src.execution.approval_preapproval import (
    ApprovalWindow, PreApprovalManager, PreApprovalProvider,
)


def _make_request(
    *,
    execution_id="exec-001",
    symbol="005930",
    side="buy",
    quantity=10,
    price="70500",
    is_live_env=False,
    is_dry_run=True,
):
    p = Decimal(price)
    return ApprovalRequest(
        execution_id=execution_id,
        signal_id="sig-001",
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=p,
        estimated_cost_krw=p * Decimal(quantity),
        is_dry_run=is_dry_run,
        is_live_env=is_live_env,
        requested_at_utc=datetime.now(timezone.utc),
    )


# ─────────────────────────────────────────────────
# CLIApprovalProvider Tests
# ─────────────────────────────────────────────────

def test_cli_approve_explicit():
    print("\n[1] CLI 명시적 'approve' 승인")
    output = io.StringIO()
    provider = CLIApprovalProvider(
        timeout_sec=5.0,
        require_explicit_yes=True,
        input_provider=lambda prompt: "approve",
        output_writer=output,
        use_color=False,
    )
    decision = provider.request_approval(_make_request())
    assert decision.approved is True
    assert "approve" in decision.reason
    assert decision.approver == "cli_human"
    print(f"   ✅ 'approve' → 승인")


def test_cli_approve_yes():
    print("\n[2] CLI 'yes' 승인")
    provider = CLIApprovalProvider(
        require_explicit_yes=True,
        input_provider=lambda p: "yes",
        output_writer=io.StringIO(),
        use_color=False,
    )
    decision = provider.request_approval(_make_request())
    assert decision.approved is True
    print(f"   ✅ 'yes' → 승인")


def test_cli_strict_mode_rejects_y():
    print("\n[3] strict 모드 — 'y' 단축은 거부")
    provider = CLIApprovalProvider(
        require_explicit_yes=True,
        input_provider=lambda p: "y",
        output_writer=io.StringIO(),
        use_color=False,
    )
    decision = provider.request_approval(_make_request())
    assert decision.approved is False
    assert "shortcut not allowed" in decision.reason or "explicit" in decision.reason.lower()
    print(f"   ✅ strict + 'y' → 거부")


def test_cli_loose_mode_accepts_y():
    print("\n[4] loose 모드 — 'y' 단축 허용")
    provider = CLIApprovalProvider(
        require_explicit_yes=False,
        input_provider=lambda p: "y",
        output_writer=io.StringIO(),
        use_color=False,
    )
    decision = provider.request_approval(_make_request())
    assert decision.approved is True
    print(f"   ✅ loose + 'y' → 승인")


def test_cli_reject():
    print("\n[5] CLI 거부 응답")
    for resp in ["reject", "no", "n", "deny", "r"]:
        provider = CLIApprovalProvider(
            input_provider=lambda p, r=resp: r,
            output_writer=io.StringIO(),
            use_color=False,
        )
        decision = provider.request_approval(_make_request())
        assert decision.approved is False, f"{resp!r} should reject"
    print(f"   ✅ reject/no/n/deny/r 모두 거부")


def test_cli_quit():
    print("\n[6] CLI 'quit' — 거부 + 종료 의도")
    provider = CLIApprovalProvider(
        input_provider=lambda p: "quit",
        output_writer=io.StringIO(),
        use_color=False,
    )
    decision = provider.request_approval(_make_request())
    assert decision.approved is False
    assert "quit" in decision.reason.lower()
    print(f"   ✅ quit → 거부 + reason에 quit 표시")


def test_cli_empty_input_rejected():
    print("\n[7] 빈 입력 거부 (fail-closed)")
    provider = CLIApprovalProvider(
        input_provider=lambda p: "",
        output_writer=io.StringIO(),
        use_color=False,
    )
    decision = provider.request_approval(_make_request())
    assert decision.approved is False
    assert "empty" in decision.reason
    print(f"   ✅ 빈 입력 → 거부")


def test_cli_unknown_response():
    print("\n[8] 알 수 없는 응답 거부")
    provider = CLIApprovalProvider(
        input_provider=lambda p: "maybe",
        output_writer=io.StringIO(),
        use_color=False,
    )
    decision = provider.request_approval(_make_request())
    assert decision.approved is False
    assert "unknown" in decision.reason
    print(f"   ✅ 'maybe' → 거부 (unknown)")


def test_cli_timeout_real():
    print("\n[9] 실제 타임아웃 — fail-closed")
    # 테스트 환경에서는 input_provider가 None일 때 stdin 데몬 스레드가
    # 인터프리터 종료 시 lock 문제를 일으킬 수 있음.
    # input_provider로 _InputTimeout을 시뮬레이션하여 동일 코드 경로 검증.
    from src.execution.approval_cli import _InputTimeout
    
    def timeout_simulator(prompt):
        raise _InputTimeout()
    
    output = io.StringIO()
    provider = CLIApprovalProvider(
        timeout_sec=0.3,
        input_provider=timeout_simulator,
        output_writer=output,
        use_color=False,
    )
    decision = provider.request_approval(_make_request())
    assert decision.approved is False
    assert "timeout" in decision.reason.lower()
    print(f"   ✅ 타임아웃 → 거부 (reason: {decision.reason})")


def test_cli_audit_log_written():
    print("\n[10] CLI audit log 기록")
    audit_path = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
    try:
        audit = ApprovalAuditLog(audit_path)
        provider = CLIApprovalProvider(
            input_provider=lambda p: "approve",
            output_writer=io.StringIO(),
            audit_log=audit,
            use_color=False,
        )
        provider.request_approval(_make_request())

        with open(audit_path, "r", encoding="utf-8") as f:
            lines = [l for l in f.readlines() if l.strip()]
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["approved"] is True
        assert rec["approver"] == "cli_human"
        assert "response_time_sec" in rec
        # 비밀 누출 검사
        raw = "\n".join(lines)
        assert "secret" not in raw.lower()
        assert "token" not in raw.lower()
        print(f"   ✅ audit 1건 기록, 비밀 누출 없음")
    finally:
        Path(audit_path).unlink()


# ─────────────────────────────────────────────────
# PreApprovalManager / Provider Tests
# ─────────────────────────────────────────────────

def test_preapproval_basic_match():
    print("\n[11] 사전 승인 — 기본 매칭")
    mgr = PreApprovalManager()
    wid = mgr.add_window(
        symbol="005930", side="buy",
        max_quantity_per_order=100,
        max_orders=5,
        valid_for_seconds=3600,
        reason="JCPR test",
    )
    provider = PreApprovalProvider(mgr)
    decision = provider.request_approval(_make_request())
    assert decision.approved is True
    assert wid in decision.reason
    print(f"   ✅ 매칭 윈도우 → 자동 승인")


def test_preapproval_symbol_mismatch():
    print("\n[12] 사전 승인 — 종목 불일치")
    mgr = PreApprovalManager()
    mgr.add_window(
        symbol="000660",  # 다른 종목
        max_orders=5,
        valid_for_seconds=3600,
        reason="test",
    )
    provider = PreApprovalProvider(mgr)
    decision = provider.request_approval(_make_request(symbol="005930"))
    assert decision.approved is False
    assert "no matching" in decision.reason.lower()
    print(f"   ✅ 종목 불일치 → 거부")


def test_preapproval_quantity_exceeds():
    print("\n[13] 사전 승인 — 수량 초과")
    mgr = PreApprovalManager()
    mgr.add_window(
        symbol="005930",
        max_quantity_per_order=5,  # 5주 한도
        max_orders=5,
        valid_for_seconds=3600,
        reason="test",
    )
    provider = PreApprovalProvider(mgr)
    decision = provider.request_approval(_make_request(quantity=10))  # 10주 요청
    assert decision.approved is False
    print(f"   ✅ 수량 10>5 → 거부")


def test_preapproval_cost_exceeds():
    print("\n[14] 사전 승인 — 비용 초과")
    mgr = PreApprovalManager()
    mgr.add_window(
        max_total_cost_per_order_krw=Decimal("500000"),
        max_orders=5,
        valid_for_seconds=3600,
        reason="test",
    )
    provider = PreApprovalProvider(mgr)
    # 10주 * 70500 = 705,000 > 500,000
    decision = provider.request_approval(_make_request())
    assert decision.approved is False
    print(f"   ✅ 비용 초과 → 거부")


def test_preapproval_window_exhaustion():
    print("\n[15] 사전 승인 — 윈도우 소진")
    mgr = PreApprovalManager()
    mgr.add_window(
        symbol="005930", max_orders=2,
        valid_for_seconds=3600,
        reason="test",
    )
    provider = PreApprovalProvider(mgr)
    # 첫 두 번 승인
    assert provider.request_approval(_make_request()).approved is True
    assert provider.request_approval(_make_request()).approved is True
    # 세 번째 거부 (max_orders=2 소진)
    d3 = provider.request_approval(_make_request())
    assert d3.approved is False
    print(f"   ✅ 2/2 사용 후 3번째 거부")


def test_preapproval_expiration():
    print("\n[16] 사전 승인 — 유효기간 만료")
    mgr = PreApprovalManager()
    mgr.add_window(
        symbol="005930", max_orders=5,
        valid_for_seconds=1,  # 1초
        reason="test",
    )
    time.sleep(1.2)  # 1.2초 대기
    provider = PreApprovalProvider(mgr)
    decision = provider.request_approval(_make_request())
    assert decision.approved is False
    print(f"   ✅ 1초 후 만료")


def test_preapproval_revoke():
    print("\n[17] 사전 승인 — Revoke")
    mgr = PreApprovalManager()
    wid = mgr.add_window(
        symbol="005930", max_orders=10,
        valid_for_seconds=3600, reason="test",
    )
    provider = PreApprovalProvider(mgr)
    # 정상 승인
    assert provider.request_approval(_make_request()).approved is True
    # Revoke
    revoked = mgr.revoke(wid)
    assert revoked is True
    # 이후 거부
    decision = provider.request_approval(_make_request())
    assert decision.approved is False
    print(f"   ✅ revoke 후 자동 거부")


def test_preapproval_live_env_blocked():
    print("\n[18] 사전 승인 — 실거래 + LIVE는 allow_live_env=False면 거부")
    mgr = PreApprovalManager()
    mgr.add_window(
        symbol="005930", max_orders=5,
        valid_for_seconds=3600, reason="test",
        allow_live_env=False,  # 기본 — 실거래 금지
    )
    provider = PreApprovalProvider(mgr)
    decision = provider.request_approval(_make_request(
        is_live_env=True, is_dry_run=False,
    ))
    assert decision.approved is False
    # matches()가 False 반환 → find_matching_window가 None 반환 → "no matching"
    assert "no matching" in decision.reason.lower() or "live" in decision.reason.lower()
    print(f"   ✅ allow_live_env=False + LIVE orders → 거부 (reason: {decision.reason})")


def test_preapproval_live_env_allowed():
    print("\n[19] 사전 승인 — allow_live_env=True 시 실거래 통과")
    mgr = PreApprovalManager()
    mgr.add_window(
        symbol="005930", max_orders=5,
        valid_for_seconds=3600, reason="JCPR live trading approved",
        allow_live_env=True,
    )
    provider = PreApprovalProvider(mgr)
    decision = provider.request_approval(_make_request(
        is_live_env=True, is_dry_run=False,
    ))
    assert decision.approved is True
    print(f"   ✅ allow_live_env=True → 실거래 자동 승인 가능")


def test_preapproval_revoke_all():
    print("\n[20] revoke_all — 전체 무효화")
    mgr = PreApprovalManager()
    mgr.add_window(symbol="005930", max_orders=5, valid_for_seconds=3600, reason="test1")
    mgr.add_window(symbol="000660", max_orders=5, valid_for_seconds=3600, reason="test2")
    count = mgr.revoke_all()
    assert count == 2
    active = mgr.get_all(only_active=True)
    assert len(active) == 0
    print(f"   ✅ {count}개 윈도우 모두 revoked")


def test_preapproval_specificity_priority():
    print("\n[21] 가장 좁은 윈도우 우선")
    mgr = PreApprovalManager()
    # 일반 (모든 종목 buy)
    mgr.add_window(
        side="buy", max_orders=5, valid_for_seconds=3600,
        reason="generic buy",
    )
    # 좁은 (특정 종목 + side + qty)
    specific_id = mgr.add_window(
        symbol="005930", side="buy",
        max_quantity_per_order=100,
        max_orders=5, valid_for_seconds=3600,
        reason="specific 005930",
    )
    provider = PreApprovalProvider(mgr)
    decision = provider.request_approval(_make_request(symbol="005930"))
    # 더 좁은(specific) 윈도우가 우선 사용됨
    assert decision.approved is True
    assert specific_id in decision.reason
    print(f"   ✅ specific 윈도우 우선 매칭")


# ─────────────────────────────────────────────────
# CompositeApprovalProvider Tests
# ─────────────────────────────────────────────────

def test_composite_first_approves():
    print("\n[22] Composite — 첫 Provider 승인 시 즉시 종료")
    cli_called = {"called": False}

    class TrackedCLI(CLIApprovalProvider):
        def request_approval(self, req):
            cli_called["called"] = True
            return super().request_approval(req)

    mgr = PreApprovalManager()
    mgr.add_window(symbol="005930", max_orders=5, valid_for_seconds=3600, reason="test")

    composite = CompositeApprovalProvider([
        PreApprovalProvider(mgr),
        TrackedCLI(input_provider=lambda p: "approve", output_writer=io.StringIO(), use_color=False),
    ])
    decision = composite.request_approval(_make_request())
    assert decision.approved is True
    assert cli_called["called"] is False  # short-circuit — CLI 호출 안 됨
    assert "chain" in decision.metadata
    print(f"   ✅ Pre-approval 통과 → CLI 호출 안 됨, chain={decision.metadata['chain']}")


def test_composite_fallback_to_cli():
    print("\n[23] Composite — 첫 Provider 거부 시 다음 Provider 시도")
    mgr = PreApprovalManager()  # 빈 manager — 매칭 없음
    composite = CompositeApprovalProvider([
        PreApprovalProvider(mgr),
        CLIApprovalProvider(input_provider=lambda p: "approve", output_writer=io.StringIO(), use_color=False),
    ])
    decision = composite.request_approval(_make_request())
    assert decision.approved is True
    chain = decision.metadata.get("chain", [])
    assert len(chain) == 2
    assert "rejected" in chain[0]
    assert "approved" in chain[1]
    print(f"   ✅ Pre-approval 거부 → CLI 호출 → 승인, chain={chain}")


def test_composite_all_reject():
    print("\n[24] Composite — 모두 거부")
    composite = CompositeApprovalProvider([
        DenyAllProvider(),
        CLIApprovalProvider(input_provider=lambda p: "no", output_writer=io.StringIO(), use_color=False),
    ])
    decision = composite.request_approval(_make_request())
    assert decision.approved is False
    chain = decision.metadata.get("chain", [])
    assert len(chain) == 2
    print(f"   ✅ 모두 거부, chain={chain}")


def test_composite_provider_exception_continues():
    print("\n[25] Composite — Provider 예외 시 다음으로 진행")
    class BrokenProvider(ApprovalProvider):
        name = "broken"
        def request_approval(self, req):
            raise RuntimeError("simulated failure")

    composite = CompositeApprovalProvider([
        BrokenProvider(),
        CLIApprovalProvider(input_provider=lambda p: "approve", output_writer=io.StringIO(), use_color=False),
    ])
    decision = composite.request_approval(_make_request())
    assert decision.approved is True
    chain = decision.metadata.get("chain", [])
    assert any("error" in c for c in chain)
    print(f"   ✅ 예외 후 다음 Provider, chain={chain}")


def test_composite_all_exceptions_fail_closed():
    print("\n[26] Composite — 모두 예외 → fail-closed")
    class BrokenProvider(ApprovalProvider):
        name = "broken"
        def request_approval(self, req):
            raise RuntimeError("fail")

    composite = CompositeApprovalProvider([BrokenProvider(), BrokenProvider()])
    decision = composite.request_approval(_make_request())
    assert decision.approved is False
    assert "all providers failed" in decision.reason
    print(f"   ✅ 모두 예외 → fail-closed")


def test_composite_empty_providers_rejected():
    print("\n[27] Composite — 빈 providers 거부")
    try:
        CompositeApprovalProvider([])
        assert False
    except ValueError as e:
        assert "비어있음" in str(e) or "empty" in str(e).lower()
        print(f"   ✅ 빈 providers 거부")


# ─────────────────────────────────────────────────
# Validation Tests
# ─────────────────────────────────────────────────

def test_window_invalid_inputs():
    print("\n[28] 윈도우 검증")
    mgr = PreApprovalManager()
    # 빈 reason
    try:
        mgr.add_window(max_orders=5, valid_for_seconds=3600, reason="")
        assert False
    except ValueError:
        print(f"   ✅ 빈 reason 거부")
    # max_orders=0
    try:
        mgr.add_window(max_orders=0, valid_for_seconds=3600, reason="x")
        assert False
    except ValueError:
        print(f"   ✅ max_orders=0 거부")
    # 잘못된 side
    try:
        mgr.add_window(side="hold", max_orders=5, valid_for_seconds=3600, reason="x")
        assert False
    except ValueError:
        print(f"   ✅ 잘못된 side 거부")


def test_cli_timeout_validation():
    print("\n[29] CLI timeout 검증")
    try:
        CLIApprovalProvider(timeout_sec=-5)
        assert False
    except ValueError:
        print(f"   ✅ 음수 timeout 거부")


def test_audit_log_secret_filtering():
    print("\n[30] Audit log — 비밀 키워드 필터링")
    audit_path = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
    try:
        audit = ApprovalAuditLog(audit_path)
        request = _make_request()
        decision = ApprovalDecision(
            approved=True, reason="test", approver="test",
            decided_at_utc=datetime.now(timezone.utc),
        )
        # extra에 secret 키 시도
        audit.write(request, decision, extra={
            "safe_field": "ok",
            "app_secret": "should_be_filtered",
            "auth_token": "should_be_filtered",
            "account_no": "should_be_filtered",
        })
        with open(audit_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "should_be_filtered" not in content
        assert "safe_field" in content
        print(f"   ✅ secret/token/account_no 자동 필터링")
    finally:
        Path(audit_path).unlink()


if __name__ == "__main__":
    test_cli_approve_explicit()
    test_cli_approve_yes()
    test_cli_strict_mode_rejects_y()
    test_cli_loose_mode_accepts_y()
    test_cli_reject()
    test_cli_quit()
    test_cli_empty_input_rejected()
    test_cli_unknown_response()
    test_cli_timeout_real()
    test_cli_audit_log_written()
    test_preapproval_basic_match()
    test_preapproval_symbol_mismatch()
    test_preapproval_quantity_exceeds()
    test_preapproval_cost_exceeds()
    test_preapproval_window_exhaustion()
    test_preapproval_expiration()
    test_preapproval_revoke()
    test_preapproval_live_env_blocked()
    test_preapproval_live_env_allowed()
    test_preapproval_revoke_all()
    test_preapproval_specificity_priority()
    test_composite_first_approves()
    test_composite_fallback_to_cli()
    test_composite_all_reject()
    test_composite_provider_exception_continues()
    test_composite_all_exceptions_fail_closed()
    test_composite_empty_providers_rejected()
    test_window_invalid_inputs()
    test_cli_timeout_validation()
    test_audit_log_secret_filtering()
    print("\n🎉 모든 스모크 테스트 통과 (All smoke tests passed)")
