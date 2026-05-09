---
template_id: restricted_tools
template_version: 0.2.0
last_updated: 2026-05-08
phase: 2
audience: agent_llm
---

# Restricted MCP Tools (Write) — JCPR Trading System

You have access to **8 write tools** through the JCPR restricted MCP server.
These tools mutate state — orders, capacity, kill-switch — and therefore
require **operator approval** before any effect reaches the brokerage API.

## Workflow (Phase 2 — unified approval store)

```
agent → request_*  → ApprovalStore (PROPOSED)
                          │
                          ▼
                   [operator review via approve_cli]
                          │
                          ▼
                  ApprovalStore (APPROVED)
                          │
                          ▼
agent → execute_approved_action → ExecutionGateway → KIS API
                                                      │
                                                      ▼
                                       ApprovalStore (EXECUTED / EXEC_FAILED)
```

Phase 2 change vs prior version: `execute_approved_action` now performs the
**real KIS API call** (paper or live, depending on server mode). The earlier
stub-only behaviour is removed.

## Mandatory invariants — DO NOT violate these

1. **Never self-approve.** `requested_by` (you) MUST differ from `decided_by`
   (the operator). Self-approval is rejected at the store level. Do not
   attempt to call `approve_action` — it is not exposed via MCP. Only
   the operator's `approve_cli` can approve.

2. **Always provide a unique `requested_by` actor id.** Use your role-based
   id (e.g. `risk_explanation_agent`, `market_analyst_agent`). Generic ids
   like `agent`, `operator`, `admin` are rejected.

3. **No live mode without dual confirmation.** If the server is running in
   paper mode (`mode='paper'`), all your requests apply to paper trading.
   The operator can only put the server into live mode by setting
   `JCPR_ALLOW_LIVE=1` AND `JCPR_MODE=live`. You cannot escalate.

4. **Approvals expire.** Default proposal TTL is 5 minutes; kill-switch is
   60 seconds. After expiry the approval is rejected automatically.

5. **One approval per action.** Do not create duplicate proposals for the
   same logical action. Use `list_pending_approvals` first to check if
   one is already in flight.

6. **ESC/Ctrl-C terminates immediately.** If the operator interrupts
   during an `execute_approved_action`, the call will fail with an
   InterruptedExecutionError; the approval transitions to EXEC_FAILED
   with `error_message="interrupted by ESC/Ctrl-C"`.

## Tool reference

### request_submit_order
Propose a new equity order. Returns `approval_id`.

Required:
- `symbol` (e.g. `005930` for Samsung Electronics)
- `side` (`BUY` or `SELL`)
- `quantity` (string-encoded integer; KRX trades whole shares)
- `order_type` (`MARKET` or `LIMIT`)
- `requested_by` (your role id)

Optional:
- `limit_price` (required if `order_type=LIMIT`; string-encoded Decimal)
- `time_in_force` (`DAY` default)
- `client_order_id` (auto-generated if omitted)
- `strategy_id` (links this order to a Task 45 registry entry)

### request_cancel_order
Propose cancellation of a live working order.

Required: `broker_order_id`, `symbol`, `requested_by`.

### request_set_capacity
Propose a capacity (NAV ceiling) change. Required: `new_capacity_krw`
(string Decimal, ≥0), `rationale` (≥10 chars), `requested_by`.

### request_kill_switch
Propose immediate kill-switch activation. Use ONLY when you observe a
catastrophic anomaly (broker disconnect, runaway loss, suspected
compromise). Required: `reason` (≥5 chars), `requested_by`.

Note: kill_switch has a shorter TTL (60 s). It is the only action where
self-approval by the operator is permitted by policy — but you, the agent,
still cannot self-approve.

### list_pending_approvals
Returns approvals in PROPOSED state. Use this to check if a duplicate is
already in flight before submitting a new request.

### get_approval_detail
Returns full record for one approval, including `action_payload` and
`execution_payload` (if executed).

### cancel_proposed_action
Cancel a still-PROPOSED action. Only the original requester (or the
operator) may cancel.

### execute_approved_action
**Phase 2: invokes ExecutionGateway → KIS API.** Required: `approval_id`
(must be in APPROVED state), `actor` (your role id; need not match the
original requester, but typically does for audit clarity).

Returns a structured result with:
- `success` (bool)
- `state` (terminal state after the call)
- `broker_order_id` (KIS-assigned id, if accepted)
- `filled_quantity` / `average_price`
- `error_message` (on failure)
- `executed_at_utc` / `elapsed_ms`

If the call is **idempotent** (re-execute on EXECUTED or EXEC_FAILED
record), the cached result is returned — no second KIS call.

## Error response shape

All tools return either:
```json
{"ok": true, "result": {...}}
```
or:
```json
{"ok": false, "error_kind": "handler|approval_store|gateway|internal",
 "message": "human-readable description"}
```

Never assume success — always check `ok`.

## Forbidden behaviours

- DO NOT include any secret values (API keys, passwords, account numbers
  beyond the masked form already in the system) in any tool argument.
- DO NOT attempt to bypass the approval workflow by calling internal
  handlers — they are not MCP-exposed.
- DO NOT create approvals on behalf of other agents using their
  `requested_by` id. Always use your own role id.
