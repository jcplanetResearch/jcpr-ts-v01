---
template_id: pnl_explainer.pnl_attribution
version: v1.0
role: user
target_agent: pnl_explainer
description: "User-level task: attribute today's P&L to strategy and symbol."
required_variables:
  - starting_capital_krw
  - cash_krw
  - since_iso
response_schema_path: schemas/pnl_explanation.json
---

The operator wants to know how the trading session performed and what drove the P&L.

Context:
- Starting capital: {{ starting_capital_krw }} KRW
- Current cash: {{ cash_krw }} KRW
- Session start (UTC ISO): {{ since_iso }}

Steps:
1. Call `get_pnl_snapshot(starting_capital_krw="{{ starting_capital_krw }}", cash_krw="{{ cash_krw }}")` to get total P&L.
2. Call `get_recent_fills(limit=500, since_iso="{{ since_iso }}")` to fetch all session fills.
3. Call `get_positions` to capture unrealized state.
4. Call `get_strategy_registry` if you need strategy metadata.
5. Decompose:
   - Realized P&L = sum across closed trades from fills.
   - Unrealized P&L = sum across open positions (market_value - avg_cost * qty).
   - Per strategy: aggregate by `strategy_id` field on fills (if absent, mark "unattributed").
   - Per symbol: aggregate by `symbol`.
6. Estimate fees & slippage if data available (otherwise note as unavailable).
7. Reconcile: realized + unrealized + cash should ≈ equity. If not, set `reconciliation_ok: false` and add a note.

Respond in Korean per the response schema with all amounts as Decimal strings.

Be straightforward about losses. Do not soften the numbers.
