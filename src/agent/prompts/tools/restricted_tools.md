---
template_id: common.restricted_tools
version: v1.0
role: tool_guide
target_agent: common
description: "Reference for the 8 restricted (write) MCP tools — all require human approval."
required_variables: []
---

# JCPR Restricted MCP Tools Reference

The restricted MCP server (`jcpr-restricted`, Task 35) provides 8 tools that change state. **EVERY ACTION REQUIRES HUMAN APPROVAL.** You cannot bypass this.

## 3-Phase Workflow

```
1. AGENT calls request_xxx(...)
   → returns approval_id, status: "pending"

2. OPERATOR (human) approves via CLI:
   $ python scripts/approve_cli.py --approval-db ... \\
       --approval-id <id> --approve --decided-by <name>

3. AGENT calls execute_approved_action(approval_id)
   → runs the action (currently stub for Task 21)
```

## Hard Rules

1. **Self-approval is BLOCKED**: If you (`requested_by`) try to also approve, the system rejects (`SelfApprovalError`).
2. **Default mode is paper**: Set `mode='paper'` (or omit). `mode='live'` requires server config `allow_live=True`.
3. **TTLs are tight**: Approval TTL = 5 minutes (default). Execute TTL after approval = 60 seconds.
4. **Single-use**: An approval can only be executed once.
5. **All actions are audited**: `approval_request`, `approval_decision`, `mcp_tool_call`, `mcp_tool_result`.

## Tools

### request_submit_order
```
symbol: str       (e.g. "005930")
side: str         ("buy" or "sell")
qty: int          (positive)
order_type: str   ("market" | "limit", default "market")
price_krw: str?   (required if order_type="limit")
mode: str         ("paper" default)
strategy_id: str? (optional attribution)
client_order_id: str? (optional idempotency token)
requested_by: str (your agent name)
```

### request_cancel_order
```
order_id: str
reason: str
requested_by: str
```

### request_set_capacity
```
capacity_krw: str    (Decimal string)
target: str          ("total" or "per_strategy")
strategy_id: str?    (required if target=per_strategy)
reason: str
requested_by: str
```

### request_kill_switch
```
activate: bool
reason: str          (REQUIRED when activate=true)
requested_by: str
```
URGENT — TTL is 60 seconds (vs 5min for others).

### list_pending_approvals
```
limit: int = 20      (max 100)
```
Returns: `{pending_approvals: [...], count: N}`

### get_approval_status
```
approval_id: str     ("apv-YYYYMMDD-XXXXXXXX")
```
Returns full approval record including `status`, `decided_by`, `expires_in_seconds`.

### cancel_request
```
approval_id: str
reason: str
cancelled_by: str    (must match original requester)
```
Only YOU (the original requester) can cancel YOUR pending request. Cannot cancel approved/rejected/executed.

### execute_approved_action
```
approval_id: str
executed_by: str
```
Runs the approved action. Stub returns `{"executed": true, "stub": true, "note": "..."}` until Task 40.

## Standard Pattern

```
# 1. Request
res = request_submit_order(symbol="005930", side="buy", qty=10, requested_by="market_agent")
aid = res["approval_id"]
# tell operator: "I've requested approval (id={aid}), please review."

# 2. Operator decides via CLI (out-of-band)

# 3. Poll status
res = get_approval_status(approval_id=aid)
if res["status"] == "approved":
    # 4. Execute
    res = execute_approved_action(approval_id=aid, executed_by="market_agent")
elif res["status"] == "rejected":
    # explain to operator why it was rejected
elif res["status"] == "expired":
    # request again with current data
```

## Don'ts

- Don't request with `mode="live"` unless explicitly told the server allows it.
- Don't repeatedly resubmit on rejection — explain to operator first.
- Don't try to approve your own request — the store will refuse.
- Don't store `approval_id` outside the conversation; fetch fresh status each time.
