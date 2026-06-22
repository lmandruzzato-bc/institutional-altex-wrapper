---
id: binance-withdraw-recvwindow-timeout
title: Binance withdraw -1021 recvWindow timeout from proxy latency, no re-sign retry
signature:
  phase: transfer
  task_type: "*"
  transfer_method: "*"
  exchange: binance
  fix_category: code_bug
  error_patterns:
    - "-1021"
    - "Timestamp for this request is outside of the recvWindow"
    - "/sapi/v1/capital/withdraw/apply"
    - "3105"
last_seen: 2026-06-22
example_task_ids: [262849]
affected_repos: [exchanges, transfer-engine, settlement-engine]
---

## Root cause

A Binance withdraw apply (`/sapi/v1/capital/withdraw/apply`) signs its `timestamp` exactly once, with a hard-coded `recvWindow=60000` (60s). When that single request is delayed in flight — observed as a ~113s stall on the account-configured forward proxy `proxy.altono.app:3128` while sibling legs on both sides were sub-second — the signed timestamp is already stale by the time Binance evaluates it, and Binance returns native `-1021` "Timestamp for this request is outside of the recvWindow." (HTTP 400). The Altex code path turns this transient timing rejection into a **permanent** leg failure: `API._request` only retries `5xx≠504`/`429` (and even then re-sends the same already-signed payload without re-signing), so a `-1021` is never retried; and `make_withdrawal` catches the resulting `TypeError` (on `None['id']`) into a traceback and returns `_format_withdrawal(success=False)` = `{id:null,tx_id:null,message:'',success:false}`. The transfer-engine surfaces that as an HTTP 400 detail and the settlement-engine stores it as the `txn_log` `external_api_response`. The empty message is therefore an Altex artifact — Binance's real `-1021` text (encoded to local `3105`) survives only in Loki. No `tx_id` is minted, so nothing is broadcast on-chain.

## Diagnostic steps

1. From the transfer record, confirm the failed part is in the `transfer` phase with `status=failed`, `txn_id=null`, `internal_id=null`, and a `txn_log` of `{"external_api_response":"{\"detail\":{\"id\":null,\"tx_id\":null,\"message\":\"\",\"success\":false}}"}` — the empty `success:false` body is the tell that the venue error was swallowed.
2. The stored `txn_log` carries no venue detail, so go to Loki for the real response. Query the transfer-engine logs on the executing server for the withdraw apply response: `{server="<seN-host>"} |~ "received post|exchange=BINANCE|withdraw/apply"` over a ±5 min window around the part's `start_time`. Look for the verbatim `{"code":-1021,"msg":"Timestamp for this request is outside of the recvWindow."}` and the paired `_handle_exchange_response` line encoding it to local `3105`.
3. Confirm the timing mechanism: find the `_sign` line (`|= "/sapi/v1/capital/withdraw/apply"`) and compare its body-time/ingest-time against the `received` line for the same leg. A sign→receive gap exceeding ~60s past the signed `timestamp` (with `recvWindow=60000`) is the trigger. Note the `proxies={'http'/'https':'...proxy.altono.app:3128'}` kwarg on the request.
4. Rule out a sustained outage by checking sibling withdraw applies in the same batch — if neighboring legs (incl. ones sent seconds later) completed in ~0.5s and minted `{"id":"..."}`, the latency was isolated to the one stalled request, not a venue/proxy outage.
5. Confirm terminality: `count_over_time` the BCH/asset-specific apply over a multi-hour span — exactly one apply sent and one received (the `-1021`), and the recon listener hits `breaking!!!` shortly after, with no re-arm and no minted id.

## Fix

- **Immediate (unblock the transfer):** re-trigger the withdrawal. No funds moved (no `tx_id`), and the latency is a one-off, so a fresh sign with a current timestamp almost always succeeds. Verify sibling/neighbor legs on the same account are completing sub-second before resubmitting.
- **Permanent (prevent recurrence):** make `-1021` self-healing in `sg-altonomy-exchanges`. (1) Treat `-1021` as retryable with a **fresh re-sign** — retry `post_withdrawal` after re-signing the timestamp, or fix `API._request` so its retry branch re-enters the nonce/sign block instead of re-sending stale signed kwargs (`Binance.py:221-227`, `:231`). (2) Stop `make_withdrawal` (`Binance.py:1719-1729`) from collapsing the venue error into an empty `_format_withdrawal(success=False)` — surface the real `-1021`/local-`3105` reason into the response so `txn_log` is self-diagnosing without a Loki dig. Separately, infra should investigate `proxy.altono.app:3128` tail latency (one request stalled ~113s while neighbors were sub-second).

## References

- Code: `sg-altonomy-exchanges/altonomy/exchanges/Binance.py:173-237` — `API._request`: signs timestamp once (181-182); retry gate only fires for 5xx≠504/429 and re-sends stale signed data, so a `-1021` (400) is never retried/re-signed. Proxy injected at 208-209.
- Code: `sg-altonomy-exchanges/altonomy/exchanges/Binance.py:553-580,1719-1729` — `post_withdrawal` hard-codes `recvWindow=60000`; `make_withdrawal` calls it once, swallows the `None`-response `TypeError` into a traceback, returns `_format_withdrawal(success=False)` (the empty `txn_log`).
- Code: `sg-altonomy-exchanges/altonomy/exchanges/CoinMarket.py:346-358` — `_handle_exchange_response`: encodes the Binance error reason to a local 4-digit code (`3105`), logs it, returns `None`.
- Code: `sg-altonomy-transfer-engine/altonomy/txengine/endpoints/transfer.py:43-62` — `start_withdraw`: raises `HTTPException(400, detail=result)` on a falsy id/tx_id/success; settlement-engine wraps non-200 as `{"external_api_response": <body>}` into `txn_log`.
- Loki: `{server="<seN-host>"} |~ "received post|exchange=BINANCE|withdraw/apply"` and `{server="<seN-host>"} |= "/sapi/v1/capital/withdraw/apply"` — window ±5 min around the failed part's `start_time` (UTC); compares per-leg sign→receive latency and surfaces the verbatim `-1021` + `3105` encode.
</content>
