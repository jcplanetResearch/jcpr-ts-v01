---
template_id: common.readonly_tools
version: v1.0
role: tool_guide
target_agent: common
description: "Reference guide for the 8 read-only MCP tools (Task 34 server)."
required_variables: []
---

# JCPR Read-Only MCP Tools Reference

The read-only MCP server (`jcpr-readonly`, Task 34) provides 8 tools. None of these change system state. All return JSON dicts with an `ok` field.

Standard response shape:
```json
{
  "ok": true | false,
  "computed_at_utc": "2026-05-07T14:00:00+00:00",
  "_trace_id": "trc-20260507-...",
  "...": "tool-specific fields"
}
```

On error:
```json
{
  "ok": false,
  "error_code": "DB_NOT_FOUND" | "VALIDATION_ERROR" | "RATE_LIMIT" | ...,
  "error_message": "...",
  "_trace_id": "trc-..."
}
```

## 1. get_market_status
**No arguments**. Returns current KRX state.

```json
{
  "ok": true,
  "state": "open" | "closed" | "closed_weekend" | "pre_market" | "closed_post",
  "market": "KRX",
  "timezone": "Asia/Seoul",
  "kst_time": "14:53",
  "is_trading_day": true,
  "is_in_session": true
}
```

## 2. get_positions
**No arguments**. Returns currently open positions (qty > 0).

```json
{
  "ok": true,
  "positions": [{"symbol": "005930", "qty": 100, "avg_cost_krw": 70000, "market_value_krw": 7100000}, ...],
  "count": 2
}
```

## 3. get_pnl_snapshot
**Args:** `starting_capital_krw: str`, `cash_krw: str` (Decimal strings)

```json
{
  "ok": true,
  "starting_capital_krw": "10000000",
  "cash_krw": "500000",
  "position_value_krw": "13600000",
  "equity_krw": "14100000",
  "pnl_krw": "4100000",
  "pnl_pct": "0.41"
}
```

## 4. get_recent_fills
**Args:** `limit: int = 50` (max 500), `since_iso: str | None`

```json
{
  "ok": true,
  "fills": [{"fill_id": "F1", "symbol": "005930", "side": "buy", "qty": 100, "price_krw": 70000, "timestamp_utc": "..."}],
  "count": 2
}
```

## 5. get_rejection_summary
**Args:** `since_iso: str | None`

```json
{
  "ok": true,
  "total_decisions": 4,
  "rejections": 3,
  "approvals": 1,
  "by_reason": {"position_limit": 1, "system_paused": 2},
  "by_gate": {"exposure": 1, "kill_switch": 2}
}
```

## 6. get_portfolio_risk
**Args:** `sector_map: dict[str, str]`, `cash_krw: str`

```json
{
  "ok": true,
  "snapshot": {
    "total_exposure_krw": "13600000",
    "equity_krw": "14600000",
    "by_sector": {"tech": {"exposure_krw": "13600000", "pct": 1.0}},
    "hhi": 10000,
    "severity": "critical",
    "warnings": ["sector tech 100% > 50% limit"]
  }
}
```

## 7. get_strategy_registry
**No arguments**. Returns active/paper/live counts and strategy metadata.

```json
{
  "ok": true,
  "registry": {
    "total_strategies": 3,
    "active_count": 2,
    "by_strategy": {"momentum_v1": {...}, "...": {...}}
  }
}
```

## 8. get_trace
**Args:** `trace_id: str`, `include_tree: bool = true`

```json
{
  "ok": true,
  "trace_id": "trc-20260507-a1b2c3d4",
  "event_count": 4,
  "events": [...],
  "summary": {"event_count": 4, "duration_ms": 0.5, "...": "..."},
  "tree": {"span_id": "...", "event": {...}, "children": [...]}
}
```

## Usage Pattern (Common)

```
1. get_market_status — orient yourself
2. get_positions + get_pnl_snapshot — current state
3. (if anomaly) get_recent_fills + get_rejection_summary
4. (if specific question) get_trace(trace_id)
```

## Rate Limits & Size Caps

- 120 calls per minute (default)
- Max result size 256KB
- If you hit a rate limit (`error_code: "RATE_LIMIT"`), wait per the message and retry.
- Use `since_iso` to constrain time range when possible.
