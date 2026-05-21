---
id: binance-withdraw-ip-whitelist
title: Binance withdrawal fails because transfer-engine egress IP not whitelisted
signature:
  phase: transfer
  task_type: external outgoing
  transfer_method: "*"
  exchange: binance
  fix_category: exchange_config
  error_patterns:
    - "IP not in whitelist"
    - "-2015"
last_seen: 2026-04-12
example_task_ids: []
affected_repos: [transfer-engine, exchanges]
---

## Root cause

Binance enforces a per-API-key IP allowlist. When transfer-engine pods rotate to a node pool with a different egress NAT IP, withdrawals start failing with Binance error code `-2015` ("IP not in whitelist"). Internal transfers continue to work because they use a different code path that does not trigger the same check.

## Diagnostic steps

1. Query `Altex.transfer_task_part` for the failed part and read `txn_log`. A confirmed match contains the substring `"-2015"` or `"IP not in whitelist"`.
2. In Loki, run `{server="w04.se1.altono.app", job="transfer-engine-logs"} |= "task_id=<id>" |= "/wapi/v3/withdraw"` over the affected window — the URL substring confirms the request that failed.
3. Look up the transfer-engine pods' current egress IP (out-of-band: ask MO or `kubectl ... -o wide`). Compare against the Binance API-key allowlist (only MO has access to the Binance console).

## Fix

- **Immediate:** MO adds the current egress IP to the affected Binance API key's IP allowlist. Engineer retries the part via the operator console.
- **Permanent:** Pin the transfer-engine egress IP at the infrastructure layer (out of scope for Altex repos).

## References

- Code: `sg-altonomy-exchanges/altonomy/exchanges/Binance.py:212` — request log line that surfaces the `-2015` error
- DB: `Altex.transfer_task_part.txn_log` — initial evidence
- Loki: `{server="w04.se1.altono.app", job="transfer-engine-logs"} |= "task_id=<id>" |= "-2015"`
