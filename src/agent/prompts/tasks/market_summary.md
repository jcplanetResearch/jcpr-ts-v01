---
template_id: market_analyst.market_summary
version: v1.0
role: user
target_agent: market_analyst
description: "User-level task: produce current market summary with positions and P&L."
required_variables:
  - starting_capital_krw
  - cash_krw
response_schema_path: schemas/market_analysis.json
---

Please produce a market summary for the JCPR operator right now.

Context:
- Starting capital this session: {{ starting_capital_krw }} KRW
- Current cash: {{ cash_krw }} KRW

Steps:
1. Call `get_market_status` to confirm market state.
2. Call `get_positions` to retrieve open positions.
3. Call `get_pnl_snapshot(starting_capital_krw="{{ starting_capital_krw }}", cash_krw="{{ cash_krw }}")` for P&L.
4. If anything looks unusual (large concentration, recent rejections), call `get_portfolio_risk` and/or `get_rejection_summary` to investigate.

Then respond in Korean per the response schema, with:
- A 2-3 sentence summary
- Key findings citing each tool you used
- Optional next_steps if relevant

Do not request any orders unless the operator asks. This is a read-only summary.
