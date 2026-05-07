---
template_id: risk_explainer.risk_breach_explain
version: v1.0
role: user
target_agent: risk_explainer
description: "User-level task: explain a specific risk gate rejection by trace_id."
required_variables:
  - trace_id
response_schema_path: schemas/risk_explanation.json
---

The operator wants to understand a risk gate rejection.

Trace to investigate: `{{ trace_id }}`

Steps:
1. Call `get_trace(trace_id="{{ trace_id }}", include_tree=true)` and inspect events.
2. Identify the `risk_evaluation` event(s) in the trace tree. Note the gate name, decision, reason, and any thresholds.
3. If the rejection was due to portfolio concentration, call `get_portfolio_risk` with the relevant sector_map to confirm current state.
4. If multiple rejections occurred recently, optionally call `get_rejection_summary(since_iso=...)` for context.

Then respond in Korean per the response schema:
- `summary_ko`: 2-3 sentence explanation in Korean of WHY the rejection happened
- `severity`: ok / warning / critical
- `breakdown`: which factors mattered (sector_concentration, kill_switch_state, etc.) with values and limits
- `evidence`: cite each tool call you made
- `recommended_actions` (only if appropriate): what the operator could change. Include `requires_approval: true` for any state-changing action.

Be factual. Do not advocate for overriding the rule.
