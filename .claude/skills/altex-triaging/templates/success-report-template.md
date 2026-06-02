# Altex triage — task <task_id>

Generated <UTC iso8601> by /altex-triaging.

## Verdict

**<one-sentence root cause OR "Inconclusive — iteration cap reached with no hypothesis above the acceptance threshold">** (confidence: <high|medium|low|inconclusive>; fix category: <code_bug|optimus_config|exchange_config|external_service|n/a>)

## Hypotheses tested

(one entry per investigation-loop iteration, in chronological order)

1. <iter 1 hypothesis> — verdict: <proven|disproven|inconclusive>, confidence: <0.0-1.0>, evidence: <pointer>
2. <iter 2 hypothesis> — verdict: <proven|disproven|inconclusive>, confidence: <0.0-1.0>, evidence: <pointer>

## Evidence

### Failed part

<compact key/value block from Phase A: part_id, transfer_method, asset, amount, account_src, account_dest, address_src, address_dest, recon_id_src, recon_id_dest, internal_id, txn_id, failed_phase, status, recon_src, recon_dest, start_time, transfer_time>

**Failed part summary:** <orchestrator-written 1-2 sentence interpretation of the failed part + failed phase, grounded in the `transfer-discoverer` API response (which group matched, the failed-phase triple, what the error log field shows)>

### Part logs

- `txn_log`: <truncate to ≤500 chars if longer, with [...truncated] marker>
- `recon_src_log`: <ditto>
- `recon_dest_log`: <ditto>

### Exchange error code

(omit this section if Phase A detected no exchange error code in the failed-phase log)

- **Raw error code:** <verbatim error string pulled from the failed-phase log field>
- **Classification:** <local | native>
- **Native exchange code:** <`code=…` extracted from the decoded/raw reason, or n/a>
- **Resolved meaning:** <decoded reason + the venue's authoritative meaning>
- **Citation(s):** <`altonomy/core/exceptions.py` for the decode, and/or web source URL>

### Account context

<orchestrator reading of the `account-discoverer` rows (the active `account_product` + `account` objects per side), focused on fields that look anomalous (exchange_status / internal_status not normal, missing exchange_uid / api_key_name on an exchange-backed product)>

### Instrument context

<orchestrator reading of the `instrument-discoverer` row (the active instrument object), focused on fields that look anomalous (status inactive, unexpected asset_type, decimal_convention out of range, symbol unknown to the platform)>

### Loki excerpts

<for each log-digger (`settlement-engine-log-digger` / `transfer-engine-log-digger`) results entry that returned ≥1 hit AND informed the verdict, include the filter label, the LogQL line, and up to 5 representative log lines>

## Code references

(omit this section if no investigation-loop iteration surfaced relevant code)

- `<repo>/<path>:<line>` — <one-line role>
  ```<lang>
  <±5 lines of context>
  ```

## Raw agent outputs

Per-spawn JSON files that fed the synthesis above. Click into any for full per-query rows / per-filter log lines / per-grep file:line hits.

- `runs/<task_id>/transfer-discoverer-<ts>.json` (Settlement Engine API responses — live + historical)
- `runs/<task_id>/account-discoverer-<ts>.json`
- `runs/<task_id>/instrument-discoverer-<ts>.json`
- `runs/<task_id>/settlement-engine-log-digger-<ts>.json`
- `runs/<task_id>/transfer-engine-log-digger-<ts>.json`
- `runs/<task_id>/error-code-resolver-<ts>.json` (present only when an error code was detected)
- `runs/<task_id>/altex-investigator-<ts>.json` (one per investigation-loop iteration)

## Playbook match

<one of:>
- Matched: `<entry_id>` — see `playbook/<entry_id>.md`.
- No exact match. Signature candidates considered: <list of entry_ids> (none matched on error_patterns).
- No playbook entry matched. Consider adding one — see prompt below.

## Suggested fix

<pull from the matched playbook entry's "Fix" section if matched; otherwise compose one paragraph from the synthesis. Distinguish immediate unblock vs permanent fix when applicable.>

## Open questions

(required when verdict is inconclusive; otherwise omit if confidence=high and no gaps)

- <question 1>
- <question 2>

## Queries run (relevant)

- Settlement Engine API: `transfer/tasks` (live + historical; which group matched + failed `part_id`/phase)
- Account API: <one-line summary of each `account_product`/`account` history fetch that informed the verdict>
- Instrument API: <one-line summary of the `instrument/list` fetch + `asset_code` filter that informed the verdict>
- Loki (`settlement-engine`): <each `|=` filter that returned ≥1 hit AND informed the verdict>
- Loki (`transfer-engine`): <ditto>
- Error code lookup: <`altonomy.core` decode + web searches run> (omit if no code detected)
- Investigation loop: <per-iter summary — hypothesis label + queries/searches/reads used + verdict>
