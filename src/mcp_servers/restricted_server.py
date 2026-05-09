"""Restricted MCP server — Phase 2 (unified store + ExecutionGateway DI).

Exposes 8 write tools to LLM agents. Internal CLI handlers (approve_action,
reject_action) are intentionally NOT registered with the MCP transport.

Phase 2 changes:
  - ApprovalStore is constructed once and shared with ExecutionGateway.
  - ExecutionGateway is injected — no more stub.
  - JCPR_APPROVAL_DB env var (single path).
  - All handler exceptions are mapped to MCP-friendly error responses.
"""

from __future__ import annotations

import logging
import signal
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from src.execution.approval_store import ApprovalStore, ApprovalStoreError
from src.execution.execution_gateway import ExecutionGateway, GatewayError
from src.mcp_servers._config import (
    RestrictedServerConfig,
    load_restricted_config,
)
from src.mcp_servers._write_handlers import (
    WriteHandlerError,
    WriteHandlers,
    build_handlers,
)


__all__ = [
    "RestrictedMCPServer",
    "RestrictedServer",          # alias of RestrictedMCPServer
    "ToolResponse",              # call_tool response shape
    "build_server",
    "build_restricted_server",   # alias of build_server
    "main",
]


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Server class
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Tool response object
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ToolResponse:
    """Object response shape for RestrictedMCPServer.call_tool.

    Phase 2-B addition. Replaces the dict-shaped envelope used by the
    internal _safe_call helper for cases where tests/callers prefer
    attribute access (`r.ok`, `r.result["..."]`, `r.error_kind`) over
    dict subscription. Frozen so the response cannot be mutated after
    construction.

    Fields:
      ok          — True iff the underlying handler returned without
                    raising any of the recognized exception types.
      result      — Handler's return value when ok=True; None otherwise.
      error_kind  — One of "handler", "approval_store", "gateway",
                    "internal", "identity"; None when ok=True.
      message     — Human-readable error message; None when ok=True.

    The dict-shaped _safe_call envelope is preserved unchanged for the
    @mcp.tool() decorator path (LLM agent transport); ToolResponse is
    used only by the new call_tool dispatch.
    """
    ok: bool
    result: Optional[dict] = None
    error_kind: Optional[str] = None
    message: Optional[str] = None


# ---------------------------------------------------------------------------
# Server class
# ---------------------------------------------------------------------------

class RestrictedMCPServer:
    """Wraps FastMCP with the 8 exposed write tools.

    Constructed via build_server(). Lifecycle:
        - __init__:  wires store + gateway + handlers (no I/O yet)
        - register(): attaches @mcp.tool() decorators
        - run():     starts stdio transport (blocking)
        - close():   closes store, broker session, audit writer
    """

    def __init__(
        self,
        *,
        config: RestrictedServerConfig,
        store: ApprovalStore,
        gateway: ExecutionGateway,
        handlers: WriteHandlers,
        mcp_factory: Optional[Any] = None,
    ) -> None:
        self._config = config
        self._store = store
        self._gateway = gateway
        self._handlers = handlers
        self._interrupt_flag = False

        # Install ESC/Ctrl-C handler — must fire before any new trade.
        # The flag is queried by ExecutionGateway via interrupt_check.
        self._install_signal_handlers()

        # Build MCP server (FastMCP). Use injected factory for testing.
        self._mcp = self._build_mcp(mcp_factory)

    @property
    def config(self) -> RestrictedServerConfig:
        return self._config

    @property
    def handlers(self) -> WriteHandlers:
        return self._handlers

    @property
    def interrupt_fired(self) -> bool:
        return self._interrupt_flag

    # -- Signal handling ---------------------------------------------------

    def _install_signal_handlers(self) -> None:
        """Set ESC/Ctrl-C to flip interrupt flag immediately."""
        def _sig_handler(signum, frame):
            logger.warning(
                "RestrictedMCPServer: signal %s caught — interrupt fired", signum
            )
            self._interrupt_flag = True

        try:
            signal.signal(signal.SIGINT, _sig_handler)
            if hasattr(signal, "SIGTERM"):
                signal.signal(signal.SIGTERM, _sig_handler)
        except (ValueError, OSError):
            # Not in main thread (e.g. pytest); skip
            logger.debug("signal handlers not installed (non-main thread)")

    def check_interrupt(self) -> bool:
        """Called by ExecutionGateway to query interrupt state."""
        return self._interrupt_flag

    # -- MCP tool registration ---------------------------------------------

    def _build_mcp(self, mcp_factory: Optional[Any]) -> Any:
        """Build the FastMCP instance and register tools.

        If mcp_factory is provided (testing), use it; else import FastMCP.
        """
        if mcp_factory is not None:
            mcp = mcp_factory("jcpr-restricted")
        else:
            try:
                from mcp.server.fastmcp import FastMCP
                mcp = FastMCP("jcpr-restricted")
            except ImportError:
                logger.error(
                    "FastMCP (mcp package) not installed; install with "
                    "'pip install mcp' before running the restricted server"
                )
                raise

        self._register_tools(mcp)
        return mcp

    def _register_tools(self, mcp: Any) -> None:
        """Register all 8 exposed write tools — internal handlers excluded."""
        h = self._handlers

        # Track exposed tool names for status_snapshot(). The list is
        # populated below as each @mcp.tool() decorator runs; this is
        # the single source of truth for "what's reachable from the
        # outside" rather than hardcoding a count or list elsewhere.
        # Internal CLI handlers (approve_action, reject_action) are
        # intentionally NOT added here — they are not registered with
        # the MCP transport at all.
        self._tool_names: list[str] = []
        _expose = self._tool_names.append

        # Each tool wraps the handler with error-to-dict conversion. We
        # avoid re-raising so the LLM agent gets a structured error rather
        # than a raw Python traceback.

        @mcp.tool()
        def request_submit_order(
            symbol: str,
            side: str,
            quantity: str,
            order_type: str,
            requested_by: str,
            limit_price: Optional[str] = None,
            time_in_force: str = "DAY",
            client_order_id: Optional[str] = None,
            strategy_id: Optional[str] = None,
        ) -> dict[str, Any]:
            return self._safe_call(
                h.request_submit_order,
                symbol=symbol,
                side=side,
                quantity=quantity,
                order_type=order_type,
                limit_price=limit_price,
                time_in_force=time_in_force,
                client_order_id=client_order_id,
                strategy_id=strategy_id,
                requested_by=requested_by,
            )

        @mcp.tool()
        def request_cancel_order(
            broker_order_id: str,
            symbol: str,
            requested_by: str,
        ) -> dict[str, Any]:
            return self._safe_call(
                h.request_cancel_order,
                broker_order_id=broker_order_id,
                symbol=symbol,
                requested_by=requested_by,
            )

        @mcp.tool()
        def request_set_capacity(
            new_capacity_krw: str,
            rationale: str,
            requested_by: str,
        ) -> dict[str, Any]:
            return self._safe_call(
                h.request_set_capacity,
                new_capacity_krw=new_capacity_krw,
                rationale=rationale,
                requested_by=requested_by,
            )

        @mcp.tool()
        def request_kill_switch(
            reason: str,
            requested_by: str,
        ) -> dict[str, Any]:
            return self._safe_call(
                h.request_kill_switch,
                reason=reason,
                requested_by=requested_by,
            )

        @mcp.tool()
        def list_pending_approvals(
            requested_by: Optional[str] = None,
            limit: int = 50,
        ) -> dict[str, Any]:
            return self._safe_call(
                h.list_pending_approvals,
                requested_by=requested_by,
                limit=limit,
            )

        @mcp.tool()
        def get_approval_detail(approval_id: str) -> dict[str, Any]:
            return self._safe_call(h.get_approval_detail, approval_id=approval_id)

        @mcp.tool()
        def cancel_proposed_action(
            approval_id: str,
            actor: str,
            reason: str = "cancelled by requester",
        ) -> dict[str, Any]:
            return self._safe_call(
                h.cancel_proposed_action,
                approval_id=approval_id,
                actor=actor,
                reason=reason,
            )

        @mcp.tool()
        def execute_approved_action(
            approval_id: str,
            actor: str,
        ) -> dict[str, Any]:
            return self._safe_call(
                h.execute_approved_action,
                approval_id=approval_id,
                actor=actor,
            )

        # Record the 8 exposed tool names — single source for
        # status_snapshot().tools. Order matches registration order
        # above. Internal handlers (approve_action, reject_action)
        # are deliberately absent from this list and from the MCP
        # transport.
        for _name in (
            "request_submit_order",
            "request_cancel_order",
            "request_set_capacity",
            "request_kill_switch",
            "list_pending_approvals",
            "get_approval_detail",
            "cancel_proposed_action",
            "execute_approved_action",
        ):
            _expose(_name)

    # -- Status snapshot ---------------------------------------------------

    def status_snapshot(self) -> dict[str, Any]:
        """Read-only operational snapshot.

        Returns the server-level mode/allow_live, the interrupt flag,
        a nested gateway snapshot (broker class names + kill-switch
        state — no instances, no payloads), and the list of 8 exposed
        tool names. Does NOT include any secrets, approval payloads,
        order details, or broker credentials. Safe to log and to
        return to test harnesses.

        The `tools` field is sourced from `_tool_names`, which is
        populated by `_register_tools` — this is the single source of
        truth for what is reachable via the MCP transport.
        """
        gw_snap: Optional[dict] = None
        if hasattr(self._gateway, "status_snapshot"):
            try:
                gw_snap = self._gateway.status_snapshot()
            except Exception:  # pragma: no cover — defensive
                gw_snap = None

        return {
            "mode": self._config.mode,
            "allow_live": self._config.allow_live,
            "interrupt_fired": self._interrupt_flag,
            "gateway": gw_snap,
            "tools": list(getattr(self, "_tool_names", [])),
        }

    # -- call_tool dispatch (Phase 2-B) ------------------------------------

    # Tool name aliases — test-friendly names mapped to canonical
    # WriteHandlers method names. Single source of truth for the
    # public-facing tool vocabulary; keep in sync with _PAYLOAD_KEYS.
    # Internal CLI handlers (approve_action, reject_action) are NOT
    # aliased — call_tool cannot reach them.
    _TOOL_ALIASES: dict[str, str] = {
        # Phase 2-B test names → canonical WriteHandlers method names
        "propose_submit_order":   "request_submit_order",
        "propose_cancel_order":   "request_cancel_order",
        "propose_set_capacity":   "request_set_capacity",
        "propose_kill_switch":    "request_kill_switch",
        "list_pending":           "list_pending_approvals",
        "query_approval_status":  "get_approval_detail",
        "cancel_proposal":        "cancel_proposed_action",
        "execute_approved":       "execute_approved_action",
        # Identity mappings — canonical names also accepted
        "request_submit_order":   "request_submit_order",
        "request_cancel_order":   "request_cancel_order",
        "request_set_capacity":   "request_set_capacity",
        "request_kill_switch":    "request_kill_switch",
        "list_pending_approvals": "list_pending_approvals",
        "get_approval_detail":    "get_approval_detail",
        "cancel_proposed_action": "cancel_proposed_action",
        "execute_approved_action":"execute_approved_action",
    }

    # Per-tool whitelist of payload keys accepted via the `payload=`
    # argument to call_tool. Defense against silent key pass-through:
    # an unknown key yields error_kind='handler' rather than reaching
    # the underlying handler with surprising kwargs. Empty tuple means
    # "no payload-style invocation supported — use direct kwargs".
    _PAYLOAD_KEYS: dict[str, tuple[str, ...]] = {
        "propose_submit_order": (
            "symbol", "side", "quantity", "qty", "order_type",
            "limit_price", "time_in_force", "client_order_id", "strategy_id",
        ),
        "propose_cancel_order": ("broker_order_id", "symbol"),
        "propose_set_capacity": ("new_capacity_krw", "rationale"),
        "propose_kill_switch":  ("reason",),
    }

    def call_tool(self, name: str, *,
                  payload: Optional[dict] = None,
                  **kwargs) -> ToolResponse:
        """Dispatch a tool by name and return a ToolResponse object.

        This is a higher-level test/CLI entry point that runs alongside
        the @mcp.tool() decorator path used by the LLM agent transport.
        Both paths funnel through WriteHandlers and respect identical
        validation; only the response shape differs:
            - @mcp.tool() decorators return dict (LLM-consumable JSON)
            - call_tool() returns ToolResponse (attribute-access object)

        Parameters:
            name      — tool name (alias or canonical, see _TOOL_ALIASES)
            payload   — optional dict of fields, validated against
                        _PAYLOAD_KEYS for the named tool. Unknown keys
                        produce error_kind='handler' WITHOUT calling
                        the underlying handler.
            **kwargs  — direct keyword arguments (merged after payload).

        Special handling:
            - "qty" → "quantity" normalization (test convention)
            - "cancel_proposal": requires requested_by; performs an
              identity check against the original record.requested_by.
              Mismatch yields error_kind='identity'.

        Returns ToolResponse — never raises.
        """
        # Resolve alias
        target = self._TOOL_ALIASES.get(name)
        if target is None:
            return ToolResponse(
                ok=False, error_kind="handler",
                message=f"unknown tool: {name!r}"
            )

        # Merge payload into kwargs with whitelist enforcement
        if payload is not None:
            allowed = set(self._PAYLOAD_KEYS.get(name, ()))
            if not allowed:
                return ToolResponse(
                    ok=False, error_kind="handler",
                    message=f"tool {name!r} does not accept payload= invocation"
                )
            for k, v in payload.items():
                if k not in allowed:
                    return ToolResponse(
                        ok=False, error_kind="handler",
                        message=f"unknown payload key for {name!r}: {k!r}"
                    )
                # Test convention normalizations:
                if k == "qty":
                    # qty (int) → quantity (str expected by handler)
                    kwargs.setdefault("quantity", str(v))
                elif k == "limit_price" and v is not None:
                    kwargs.setdefault("limit_price", str(v))
                else:
                    kwargs.setdefault(k, v)

        # Identity check for cancel_proposal — paper_system fixture
        # requires that only the original requester can cancel.
        if name == "cancel_proposal":
            requested_by = kwargs.pop("requested_by", None)
            aid = kwargs.get("approval_id")
            if requested_by is None or aid is None:
                return ToolResponse(
                    ok=False, error_kind="handler",
                    message="cancel_proposal requires approval_id and requested_by"
                )
            try:
                rec = self._store.get(aid)
            except Exception as exc:
                return ToolResponse(
                    ok=False, error_kind="handler", message=str(exc)
                )
            if rec.requested_by != requested_by:
                # Identity violation — distinct error_kind so callers
                # can reliably distinguish authz failure from other
                # handler errors.
                return ToolResponse(
                    ok=False, error_kind="identity",
                    message=(
                        f"only original requester {rec.requested_by!r} "
                        f"may cancel; got {requested_by!r}"
                    )
                )
            kwargs["actor"] = requested_by
            kwargs.setdefault("reason", "cancelled by requester")

        # Dispatch via the corresponding handler method
        handler_method = getattr(self._handlers, target, None)
        if handler_method is None:
            return ToolResponse(
                ok=False, error_kind="handler",
                message=f"handler {target!r} not found on WriteHandlers"
            )
        return self._call_via_handler(handler_method, **kwargs)

    def _call_via_handler(self, func, **kwargs) -> ToolResponse:
        """Object-shaped sibling of _safe_call — same exception mapping.

        Kept separate from _safe_call so the @mcp.tool() decorator path
        (LLM transport) continues to receive dict envelopes unchanged.
        """
        try:
            return ToolResponse(ok=True, result=func(**kwargs))
        except WriteHandlerError as exc:
            return ToolResponse(
                ok=False, error_kind="handler", message=str(exc)
            )
        except ApprovalStoreError as exc:
            return ToolResponse(
                ok=False, error_kind="approval_store", message=str(exc)
            )
        except GatewayError as exc:
            return ToolResponse(
                ok=False, error_kind="gateway", message=str(exc)
            )
        except Exception as exc:  # last-resort safety
            logger.exception("unexpected error in handler %s", func.__name__)
            return ToolResponse(
                ok=False, error_kind="internal",
                message=f"{type(exc).__name__}: {exc}",
            )

    # -- Error mapping -----------------------------------------------------

    def _safe_call(self, func, **kwargs) -> dict[str, Any]:
        """Call handler and convert exceptions to structured error dicts."""
        try:
            return {"ok": True, "result": func(**kwargs)}
        except WriteHandlerError as exc:
            return {"ok": False, "error_kind": "handler", "message": str(exc)}
        except ApprovalStoreError as exc:
            return {"ok": False, "error_kind": "approval_store", "message": str(exc)}
        except GatewayError as exc:
            return {"ok": False, "error_kind": "gateway", "message": str(exc)}
        except Exception as exc:  # last-resort safety
            logger.exception("unexpected error in handler %s", func.__name__)
            return {
                "ok": False,
                "error_kind": "internal",
                "message": f"{type(exc).__name__}: {exc}",
            }

    # -- Lifecycle ---------------------------------------------------------

    def run(self) -> None:
        """Start the MCP stdio transport (blocking)."""
        logger.info(
            "RestrictedMCPServer starting: mode=%s db=%s allow_live=%s",
            self._config.mode,
            self._config.approval_db_path,
            self._config.allow_live,
        )
        self._mcp.run(transport="stdio")

    def close(self) -> None:
        """Release resources (store + broker session, if any)."""
        try:
            getattr(self._store, "close", lambda: None)()
        except Exception as exc:  # pragma: no cover
            logger.warning("store close failed: %s", exc)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

def build_server(
    *,
    config: Optional[RestrictedServerConfig] = None,
    broker: Optional[Any] = None,
    paper_broker: Optional[Any] = None,
    live_broker: Optional[Any] = None,
    audit_writer: Optional[Any] = None,
    mcp_factory: Optional[Any] = None,
) -> RestrictedMCPServer:
    """Build a fully wired RestrictedMCPServer.

    Caller must supply the broker(s). Two configurations are supported:
        (a) Legacy single-broker:   broker=...
        (b) Dual-broker routing:    paper_broker=..., live_broker=...

    For unit tests, inject mock broker(s) + mcp_factory.
    """
    cfg = config if config is not None else load_restricted_config()

    # Determine broker configuration:
    # - If neither single nor dual brokers given, build the real KIS
    #   adapter from .env (production path).
    # - If only single `broker=` given, pass through (legacy path).
    # - If `paper_broker`/`live_broker` given, use dual routing — the
    #   ExecutionGateway will reject the (broker, paper_broker) mix.
    has_dual = (paper_broker is not None) or (live_broker is not None)
    if not has_dual and broker is None:
        broker = _build_default_broker(cfg)

    # Build the unified ApprovalStore (Phase 1)
    store = ApprovalStore(db_path=cfg.approval_db_path)

    # Build the gateway, sharing the same store
    # interrupt_check is set later via server reference (chicken-and-egg)
    if has_dual:
        gateway = ExecutionGateway(
            store=store,
            paper_broker=paper_broker,
            live_broker=live_broker,
            mode=cfg.mode,
            allow_live=cfg.allow_live,
            audit_writer=audit_writer,
        )
    else:
        gateway = ExecutionGateway(
            approval_store=store,
            broker=broker,
            mode=cfg.mode,
            allow_live=cfg.allow_live,
            audit_writer=audit_writer,
        )

    # Build handlers
    handlers = build_handlers(
        store=store,
        gateway=gateway,
        operator_id=cfg.operator_id,
        proposal_ttl=cfg.proposal_ttl_seconds,
        execution_ttl=cfg.execution_ttl_seconds,
        kill_switch_ttl=cfg.kill_switch_ttl_seconds,
    )

    server = RestrictedMCPServer(
        config=cfg,
        store=store,
        gateway=gateway,
        handlers=handlers,
        mcp_factory=mcp_factory,
    )

    # Wire interrupt_check now that server exists
    gateway._interrupt_check = server.check_interrupt

    return server


def _build_default_broker(cfg: RestrictedServerConfig) -> Any:
    """Construct the KIS execution adapter from .env."""
    try:
        from src.brokers.kis_execution import KISExecutionAdapter
        from src.brokers._secrets import load_kis_secrets
    except ImportError as exc:
        raise RuntimeError(
            "KIS broker modules unavailable; cannot build default broker"
        ) from exc

    secrets = load_kis_secrets(mode=cfg.mode, allow_live=cfg.allow_live)
    return KISExecutionAdapter(secrets=secrets, mode=cfg.mode)


# ---------------------------------------------------------------------------
# Naming aliases
# ---------------------------------------------------------------------------

# Aliases for naming consistency. Both names refer to the same class /
# function — `RestrictedServer` and `build_restricted_server` are the
# simpler names preferred by integration tests and top-level wiring
# (e.g. tests/integration/test_phase2b_end_to_end.py); the canonical
# names `RestrictedMCPServer` and `build_server` are retained for
# backward compatibility and for callers that prefer the explicit MCP
# qualifier. Either may be imported, instantiated, or used in
# isinstance() checks — they are the same objects.
RestrictedServer = RestrictedMCPServer
build_restricted_server = build_server


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> int:
    """CLI entrypoint. Loads config from env, builds server, runs."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    server = None
    try:
        server = build_server()
        server.run()
        return 0
    except KeyboardInterrupt:
        logger.warning("KeyboardInterrupt — shutting down")
        return 130
    except Exception as exc:
        logger.exception("server failed: %s", exc)
        return 1
    finally:
        if server is not None:
            server.close()


if __name__ == "__main__":
    sys.exit(main())
