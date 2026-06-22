---
id: bybit-withdraw-address-chain-mismatch
title: Bybit withdrawal rejected — destination address chain not whitelisted (131002)
signature:
  phase: transfer
  task_type: internal
  transfer_method: MEGAETH
  exchange: bybit
  fix_category: exchange_config
  error_patterns:
    - "Withdraw address chain or destination tag are not equal"
    - "131002"
    - "/v5/asset/withdraw/create"
last_seen: 2026-06-22
example_task_ids: [262809]
affected_repos: [exchanges, transfer-engine]
---

## Root cause

A Bybit on-chain withdrawal leg fails in the `transfer` phase because Bybit rejects the request pre-broadcast with native error `retCode 131002` / `"Withdraw address chain or destination tag are not equal"`. Bybit requires the chain supplied in the withdraw request to equal the chain under which the destination address was whitelisted in Bybit's withdrawal address book. When Altex sends `chain=<X>` (here `MEGAETH`) but the destination address is whitelisted under a different chain (e.g. `ERC20`/`ETH`) — or that chain is not a valid withdraw network for the coin at Bybit — Bybit rejects before broadcasting. The Altonomy address book is internally consistent (the leg's `address_dest` matches the destination account's channel address); the mismatch lives entirely on the Bybit side, so this is a venue config issue, not a code defect. The Bybit adapter forwards the chain unchanged and performs no address-book validation, so it cannot pre-empt the rejection. The error surfaces as a raw venue-native string (no local 4-digit Altonomy code).

Symptom shape: failed part has `(status=failed, recon_src=pending, recon_dest=pending)`, null `txn_id` / null `internal_id` / null `transfer_time`, and `txn_log.external_api_response` carrying the verbatim Bybit message. Bybit's `result:{}` is empty and no withdraw id is returned — confirming nothing broadcast.

## Diagnostic steps

1. Confirm the failed phase is `transfer` and read `txn_log.external_api_response` — look for `"Withdraw address chain or destination tag are not equal"`.
2. Confirm the source `account_product`'s exchange is **Bybit** (collect_account_evidence `ap_src` row, `exchange: Bybit`). The withdrawal executes on the source venue, so Bybit's address-book registration of the *destination* address is what matters.
3. Loki — pull the outbound request + Bybit response to see the exact `chain` param sent: `{server="<transfer-engine host>", job="transfer-engine-logs"} |= "/v5/asset/withdraw/create"`, windowed to the part's `start_time` ±5 min (UTC). Confirm `retCode:131002` and which `chain` was sent.
4. Cross-check the instrument's `deployments[]` (collect_instrument_evidence) — if the coin is deployed on more than one chain, the destination address is likely whitelisted at Bybit under the *other* chain.

## Fix

- **Immediate (unblock the transfer):** have the Bybit account operator add/confirm the destination address in Bybit's withdrawal address book under the requested chain (`MEGAETH`), then re-run the transfer. Altonomy cannot query or modify Bybit's address book. If the coin is not in fact a supported withdraw network on that chain at Bybit, re-route the leg over a chain the coin *is* deployed on and the address *is* whitelisted under (e.g. `ERC20`).
- **Permanent (prevent recurrence):** `exchange_config` — no code defect; the adapter correctly forwards the chain. Operationally, reconcile each venue's withdrawal address book against the chains Altex routes over before initiating large multi-hop transfers, so an unwhitelisted chain is caught before the leg fires (rather than after a partial-failure mid-chain). A future adapter-side enhancement could surface 131002 with an explicit "address not whitelisted on chain X" hint, but it cannot prevent the rejection.

## References

- Code: `sg-altonomy-exchanges/altonomy/exchanges/Bybit.py:700-757` — `make_withdrawal` maps `transfer_method`/chain → Bybit chain code via a literal dict (`'MEGAETH': 'MEGAETH'` at line 745); no address-book validation.
- Code: `sg-altonomy-exchanges/altonomy/exchanges/Bybit.py:1521-1541` — `_create_withdraw` posts `{coin, chain, address, tag, amount, accountType, feeType}` to `/v5/asset/withdraw/create`; logs `failed_withdrawal_request` and returns `success=False` on `retCode != 0`.
- Loki: `{server="<transfer-engine host>", job="transfer-engine-logs"} |= "/v5/asset/withdraw/create"` and `{server="<host>"} |= "Withdraw address chain or destination tag are not equal"`, windowed to the part's `start_time` ±5 min (UTC).
- Web: Bybit V5 error code 131002 — `https://bybit-exchange.github.io/docs/v5/error`.
