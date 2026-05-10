"""JCPR Trading System — Dashboard (Task 48).

User-monitoring dashboard for the JCPR (single-operator, local-only)
trading system. Reads from already-implemented modules:

    Task 19 — risk_gate.py
    Task 25 — position_ledger.py
    Task 26 — pnl_engine.py
    Task 27 — slippage.py
    Task 28 — reconciliation.py

and from ApprovalStore (sessions 1-15 of Stage 2-B).

Security model:
    - Bound to 127.0.0.1 only (single-operator, single host)
    - 0600 enforcement on all opened DB files
    - Read-only data adapters (no UPDATE/INSERT from this package)
    - Secret scrubbing on any rendered text
    - capacity.local.yaml is the single source of starting_capital_krw
      (gitignored, never committed)

This file marks the package; submodules expose the public API:
    _security  — layers 13-17 of the cumulative defense stack
    _config    — DashboardConfig + CapacityConfig (capacity.yaml parser)
    _data      — read-only adapters for each Task module
    _audit_reader — JSONL tail/since/aggregate readers
    _formatters — Decimal/datetime/mode display helpers
"""

__version__ = "0.1.0"  # Phase 2-B Task 48
