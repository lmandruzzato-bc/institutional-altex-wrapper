# Logging Strategy — Altex Backend Services

Reference cheat-sheet for log digging across the two Altex backend services consumed by the institutional-altex-wrapper. Built to support the Middle-Office Loki triage workflow. Engineers can also read it as a standalone document.

The two services in scope:

- **`sg-altonomy-settlement-engine`** — owns the transfer state-machine, settlement APIs, and the `process_transfer` long-running driver. Talks to all other services over HTTP. Runs in Kubernetes (`prod` env).
- **`sg-altonomy-transfer-engine`** — owns exchange-call proxying (`/transfer/*`) and reconciliation listeners (`/txrecon/*`). Embeds the `sg-altonomy-exchanges` library in-process, so all exchange-vendor request/response logs originate from that library.

Scope notes:
- For settlement-engine, only the `settlement-engine-api` (`uvicorn altonomy.settlement_engine.main:app`) and `settlement-engine-long-running` (`python -m altonomy.settlement_engine.process_transfer`) entrypoints are covered. Cron jobs, deal subscribers, sync workers, and client-report are out of scope.
- For sg-altonomy-exchanges, only the `CoinMarket` base class and the `Binance` concrete adapter are characterised. Other exchange adapters follow similar patterns but are not enumerated here.

---

## 1. settlement-engine

### 1.1 Logger setup

The repo has three coexisting logger flavours. There is **no JSON formatter and no structured logging library**; everything is stdlib `logging`. Log lines are plain text written to stdout (k8s collects via Loki).

| Logger | Where it is built | Used by |
|:---|:---|:---|
| `logging.getLogger('long_running')` | `altonomy/settlement_engine/process_transfer.py:22` — `setLevel(DEBUG)` + bare `StreamHandler` (no formatter, so default `%(levelname)s:%(name)s:%(message)s`) | Only the per-phase loop log lines in `process_transfer.py` (`logger.info("Begin Loop")` etc.) and the four phase-level `logger.error(...)` fallbacks (lines 83, 99, 104, 118, 131). |
| `fastapi.logger.logger` (loguru-flavoured uvicorn logger) — re-exported as `fastapi_logger` | Default kwarg `logger=fastapi_logger` in `transfer_ctrl.TransferCtrl.__init__` (line 43), `settlement_v2_ctrl.SettlementV2Ctrl.__init__` (line 61), `client_activity_report_ctrl.ClientActivityReportCtrl.__init__` (line 49) | Every `self.logger.{info,error,exception,warning,debug}` call site inside the ctrl layer. Both API requests AND the long-running loop end up logging through `fastapi.logger.logger` because `process_transfer.py` constructs `TransferLongRunningCtrl(session, token)` without passing `logger=`, so the default kicks in. The bare uvicorn formatter is used (no key=value, no JSON). |
| `logging.getLogger('settlement_v2_api')` + `StreamHandler` (no formatter), level `DEBUG` | `altonomy/settlement_engine/endpoints/settlements_v2_api.py:42-45` | Two `log.error(err)` calls (lines 94, 105) — both feed `Invalid Counterparty Ref [...]` and `Invalid POrtfolio Numbers [...]` errors. The same file also uses `fastapi.logger.logger` (imported as `logger`) for the four `logger.error(f"Failed to get exchange txns: {e}")` / `logger.error(f"Failed to get blockchain txns: {e}")` calls (lines 181, 190, 388, 397). |
| `altonomy.loggers.log_utils.get_simple_logger()` (root logger, custom format) | `altonomy/settlement_engine/common/api_utils.py:12` and `common/cache_manager.py:7` | One usage in scope: `api_utils.log.debug(...)` is invoked from `settlement_v2_ctrl.py:1397/1406/1409` via `from altonomy.settlement_engine.common import api_utils`. Format: `<<--YYYY-MM-DD HH:MM:SS.mmm-->> LEVEL [filename:funcName:lineno] message` (see `venv/.../altonomy/loggers/log_utils.py`). Only this code path uses the structured-ish format; everything else is plain. |

Practical consequence for grepping: **format is unreliable across lines in the same service**. Match on the message body, not on the prefix.

### 1.2 Long-running loop — `process_transfer.py` (entrypoint: `settlement-engine-long-running`)

Loop is the heart of the transfer state machine. `main()` runs forever, logging into Optimus every 30 min and looping every 10s through four phases.

| File:line | Level | Message format | When |
|:---|:---|:---|:---|
| `process_transfer.py:55` | INFO | `Begin Loop` | Start of each 10s iteration |
| `process_transfer.py:59` | INFO | `Process Waiting Part` | Before `process_part_waiting` |
| `process_transfer.py:62` | INFO | `Process Transfer` | Before `process_transfer` |
| `process_transfer.py:65` | INFO | `Process Recon` | Before `process_recon` |
| `process_transfer.py:68` | INFO | `Resolve Dest Recon` | Before `resolve_dest_recon` |
| `process_transfer.py:72` | INFO | `End Lopp` *(sic — typo, "Lopp" not "Loop")* | End of each iteration |
| `process_transfer.py:83-86` | ERROR | `Start running error: {str(e)}\n{traceback}` | Unhandled exception in `process_part_waiting` |
| `process_transfer.py:99-102` | ERROR | `Start transfer error: {str(e)} {model_to_dict(part)}\n{traceback}` | Per-part exception inside `process_transfer` — embeds the full part dict |
| `process_transfer.py:104-107` | ERROR | `Process transfer unhandled error: {str(e)}\n{traceback}` | Outer exception in `process_transfer` |
| `process_transfer.py:118-121` | ERROR | `Recon error: {str(e)}\n{traceback}` | Exception inside `process_recon` |
| `process_transfer.py:131-134` | ERROR | `Resolve Dest recon error: {str(e)}\n{traceback}` | Exception inside `resolve_dest_recon` |

These lines go through `getLogger('long_running')`, not the ctrl `fastapi.logger`, so they appear bare (no asctime, no level prefix beyond what `StreamHandler` defaults add — typically `LEVEL:long_running:<msg>`).

### 1.3 Long-running ctrl — `transfer_long_running_ctrl.py`

All driven by `self.logger`, which defaults to `fastapi.logger.logger`. Identifier substrings here are the most useful for triage.

| File:line | Level | Message format (load-bearing identifiers in bold) | Lifecycle event |
|:---|:---|:---|:---|
| `transfer_long_running_ctrl.py:81` | EXCEPTION | `Failed to parse recon src log {recon_src_log} to extract fee` | Fee extraction failure when reading raw exchange response |
| `transfer_long_running_ctrl.py:163-166` | INFO | `Request to start recon for task_id={task_id} part_id={part_id} direction={direction} recon_id={recon_id} succeeded with response {resp}.` | **Source recon start success** (`direction=withdraw`) |
| `transfer_long_running_ctrl.py:176-180` | ERROR | `Request to start recon for task_id={task_id} part_id={part_id} direction={direction} recon_id={recon_id} failed with status code {status_src} and response {resp}. Request will not be retried.` | Source recon start — terminal 400 |
| `transfer_long_running_ctrl.py:193-197` | ERROR | `Request to start recon for task_id={...} part_id={...} direction={...} recon_id={...} failed with status code {...} and response {...}. Request will be retried.` | Source recon start — retryable failure |
| `transfer_long_running_ctrl.py:263-266` | INFO | Same `Request to start recon for task_id=... part_id=... direction=deposit recon_id=... succeeded ...` pattern | **Settlement (off-exchange) destination recon start** |
| `transfer_long_running_ctrl.py:276-280` | ERROR | Same pattern + `Request will not be retried.` | Settlement-dest recon — terminal 400 |
| `transfer_long_running_ctrl.py:293-297` | ERROR | Same pattern + `Request will be retried.` | Settlement-dest recon — retryable |
| `transfer_long_running_ctrl.py:408-411` | INFO | Same `Request to start recon for task_id=... part_id=... direction=deposit recon_id=... succeeded ...` pattern | **Standard destination recon start** |
| `transfer_long_running_ctrl.py:421-425` | ERROR | Same pattern + `Request will not be retried.` | Standard dest recon — terminal 400 |
| `transfer_long_running_ctrl.py:438-442` | ERROR | Same pattern + `Request will be retried.` | Standard dest recon — retryable |

**Identifier substring shapes that show up in this file** — these are the literal `|=` matches to use in Loki:

- `task_id={int}` — e.g. `task_id=12345`
- `part_id={int}` — e.g. `part_id=1`
- `direction={withdraw|deposit}`
- `recon_id={uuid5-as-int}` — generated by `_generate_recon_id` at line 705-706 as `uuid.uuid5(RECON_NAMESPACE, "{task_id}-{part_id}-{direction}").int` (so it is a stringified integer, not a hex UUID)
- `response {dict-repr}` — the full Recon-service response is inlined, including its own `recon_id` key

There is **no `tx_id=` or `internal_id=` printed** by the long-running ctrl directly — those are visible only via the embedded `response {...}` dict (`response.get("tx_id")`, `response.get("internal_id")`) populated when calling `do_external_transfer`/`do_internal_transfer` (see `start_transfer_part` at `transfer_long_running_ctrl.py:568-658`). `start_transfer_part` itself does NOT log; failures fall through silently to `set_part_failed` with a DB write.

### 1.4 API ctrl — `transfer_api_ctrl.py` (entrypoint: `settlement-engine-api`)

`TransferApiCtrl` subclasses `TransferCtrl`. All sites use `self.logger` (default `fastapi.logger.logger`).

| File:line | Level | Message format | Trigger |
|:---|:---|:---|:---|
| `transfer_api_ctrl.py:838` | INFO | `Creating QMS transfers {request}` | QMS bulk-transfer entry |
| `transfer_api_ctrl.py:877-879` | EXCEPTION | `Failed to get balance for account {source_account_product_id}` | Balance fetch error during QMS bulk |
| `transfer_api_ctrl.py:883-886` | WARNING | `Insufficient balance for {asset} in account {source_account_product_id}: available={...}, required={...}. Publishing to NATS for retry.` | QMS task hits balance shortfall |
| `transfer_api_ctrl.py:913-915` | EXCEPTION | `Failed to create QMS transfer {task} tag {task.settlement_id}` | Per-task create failure |
| `transfer_api_ctrl.py:929-931` | EXCEPTION | `Failed to publish insufficient balance tasks {insufficient_balance_tasks} to NATS` | NATS publish failure |
| `transfer_api_ctrl.py:937-939` | INFO | `Will send QMS bulk transfer summary in {delay_seconds} seconds` | Pre-summary delay log |
| `transfer_api_ctrl.py:948` | EXCEPTION | `Failed to send QMS bulk transfer summary` | Summary send failure |
| `transfer_api_ctrl.py:950` | INFO | `Finished processing QMS bulk transfer request` | QMS bulk complete |
| `transfer_api_ctrl.py:955` | INFO | `Publishing insufficient balance tasks` | NATS publish start |
| `transfer_api_ctrl.py:970-972` | INFO | `Published insufficient balance task {task} to NATS subject {subject}, ack: {pub_ack}` | NATS per-task ack |
| `transfer_api_ctrl.py:974-976` | EXCEPTION | `Failed to publish insufficient balance task {task} to NATS subject {subject}` | NATS publish error |
| `transfer_api_ctrl.py:1091` | ERROR | `Unexpected task {task}` | Bulk-transfer router can't classify task |
| `transfer_api_ctrl.py:1100-1102` | INFO | `Creating internal transfer between {task.source_account} and {task.destination_account}` | Single internal-transfer create |
| `transfer_api_ctrl.py:1125-1127` | EXCEPTION | `Failed to create internal transfer between {task.source_account} and {task.destination_account}` | Internal create failure |
| `transfer_api_ctrl.py:1136-1138` | INFO | `Creating external transfer between {task.source_account} and {task.destination_address}` | Single external-transfer create |
| `transfer_api_ctrl.py:1161-1163` | EXCEPTION | `Failed to create external transfer between {task.source_account} and {task.destination_address}` | External create failure |
| `transfer_api_ctrl.py:1428-1430` | INFO | `cancelling transfer task part {part_id} of the transfer task {task_id}` | Force-cancel a task part |
| `transfer_api_ctrl.py:1687-1689` | WARNING | `Cannot find account with account_product_id={account_product_id}` | Account lookup miss |
| `transfer_api_ctrl.py:1694` | WARNING | `[BALANCE] Missing nitro or exchange for {src_info}` | Account record missing fields |
| `transfer_api_ctrl.py:1716-1718` | WARNING | `[BALANCE] No info for account_id={account_product_id}` | Async balance lookup miss |
| `transfer_api_ctrl.py:1725` | WARNING | `[BALANCE] Missing nitro or exchange for {src_info}` | Async — missing fields |

**Identifier substring shapes seen in this file**:

- `task_id={int}` (e.g. `cancelling transfer task part {part_id} of the transfer task {task_id}` — note the word "task" appears literally, not `task_id=`)
- `part_id={int}` (same caveat — appears as `transfer task part {part_id}`)
- `account_product_id={int}` and bare `account {source_account_product_id}` and `account_id={...}` and `nitro_account_id` (only inside `src_info` dict-repr)
- `[BALANCE]` literal bracketed tag — useful for filtering balance-related issues
- `tag {task.settlement_id}` — QMS settlement-id substring
- `task.settlement_id` and `task.source_account` / `task.destination_account` / `task.destination_address` get printed via Pydantic `repr`

### 1.5 Settlement v2 ctrl — `settlement_v2_ctrl.py`

Used by the settlements_v2 API. Same `self.logger` (default `fastapi.logger.logger`). Notable lines:

| File:line | Level | Message format | Trigger |
|:---|:---|:---|:---|
| `settlement_v2_ctrl.py:665-668` | ERROR | `Failed to match settlement id={settlement_id_self} with id={settlement_id_other} due to different assets. {asset_a} != {asset_b}` | Deprecated `match()` — asset mismatch |
| `settlement_v2_ctrl.py:671-674` | ERROR | `Failed to match settlement id=... with id=... due to different counterparty_ref. {a} != {b}` | Deprecated `match()` — counterparty mismatch |
| `settlement_v2_ctrl.py:676-678` | INFO | `Matching id={settlement_id_self} against id={settlement_id_other} for {amount}` | Deprecated `match()` start |
| `settlement_v2_ctrl.py:702-707` | DEBUG | `Outstanding after matching (self): id={settlement_id_self} outstanding={outstanding_self}` (and `(other)` variant) | Post-match outstanding |
| `settlement_v2_ctrl.py:728-741` | ERROR/INFO | Same asset/counterparty-mismatch and `Matching id=...` triplet — duplicated in `get_match()` | Live matching path |
| `settlement_v2_ctrl.py:765-768` | DEBUG | `Outstanding after matching (self): id=... outstanding=...` (and `(other)`) | Live matching outstanding |
| `settlement_v2_ctrl.py:1040` | DEBUG | `Settle Preview: {filled_map}` | Pre-settle preview |
| `settlement_v2_ctrl.py:1133` | ERROR | `Exception occurred for settlement match for counterparty_ref = {counterparty_ref} asset = {asset} settlement_ids={settlement_ids} | {e}` | Settlement-match failure (note `key = value` with spaces, and `|` separator before exception) |
| `settlement_v2_ctrl.py:1136` | ERROR | Same shape, different except branch | redis.lock branch |
| `settlement_v2_ctrl.py:1148` | INFO | `Unsettling Deal for settlement id={settlement_id}` | Manual unsettle |
| `settlement_v2_ctrl.py:1159` | ERROR | `Exception occured when unsettling deal | {e}` (note typo "occured") | Unsettle failure |
| `settlement_v2_ctrl.py:1160` | DEBUG | `traceback.format_exc()` raw | Unsettle traceback |
| `settlement_v2_ctrl.py:1170` | INFO | `Settling Deal for settlement id={settlement_id}` | Manual settle (single) |
| `settlement_v2_ctrl.py:1181` | ERROR | `Exception occured when settling deal | {e}` | Settle failure |
| `settlement_v2_ctrl.py:1201` | INFO | `Settling Deal for settlement ids={settlement_ids}` | Manual settle (bulk) |
| `settlement_v2_ctrl.py:1206` | ERROR | `Exception occured when settling deal | {e}` | Settle bulk failure |
| `settlement_v2_ctrl.py:1252` | ERROR | `Exception (redis.lock) occurred for Clear Residual for counterparty_ref = {counterparty_ref} asset = {asset} | {e}` | Clear-residual redis-lock failure |
| `settlement_v2_ctrl.py:1255` | ERROR | `Exception (general) occurred for Clear Residual for counterparty_ref = {counterparty_ref} asset = {asset} | {e}` | Clear-residual general failure |
| `settlement_v2_ctrl.py:1397/1406/1409` | DEBUG | `ak_test|get_aging_settlement|settlements: {settlements}` / `...|deal_settlements: ...` / `...|deal_date_map: ...` | Aging-settlement debug (uses `api_utils.log` — the only place the structured `<<--...-->> LEVEL [file:func:line] message` format appears) |

**Identifier substring shapes here**:

- `settlement id={int}` (mind the space — it's `id=`, not `settlement_id=`, when speaking of a single settlement)
- `settlement_ids={[list]}` (with underscore + plural when bulk)
- `counterparty_ref = {string}` (with spaces around `=`)
- `asset = {symbol}` (with spaces around `=`)
- `ak_test|` prefix — appears in three aging-settlement debug lines and is the only easily-greppable namespace marker
- `[BALANCE]` (in transfer_api_ctrl, not here)

### 1.6 Settlements v2 API — `settlements_v2_api.py`

Six log call sites — two via `log = getLogger('settlement_v2_api')`, four via `fastapi.logger.logger`. Both formats are bare. Identifier shapes already covered above; messages: `Invalid Counterparty Ref {list}`, `Invalid POrtfolio Numbers {list}` *(sic — "POrtfolio" misspelled)*, `Failed to get exchange txns: {e}`, `Failed to get blockchain txns: {e}`.

### 1.7 External clients

`altonomy/settlement_engine/external/{exchanges,optimus_client,account,nitro_client,txn_client,external_util,xalpha_ctrl,optimus_ctrl,s3_ctrl,comply_adv_client}.py` — **no log calls in scope**. These modules silently consume errors via `try/except` and return tuples; any HTTP-level errors are surfaced only through the response payloads embedded in upstream `logger.error` calls (the `response {resp}` substrings in `transfer_long_running_ctrl.py`).

This is a gap (see Section 4).

---

## 2. transfer-engine

### 2.1 Logger setup — `Logger.py`

Single source of truth: `altonomy/txengine/Logger.py` configures **loguru** with three sinks:

1. `sys.stderr` — receives all records **where `extra.recon_id` is NOT set** (i.e. the FastAPI app and any non-listener thread).
2. `~/logs/txengine/altonomy.log` — same filter (no `recon_id`). DEBUG level, 500 MB rotation, 7-day retention, lzma-compressed.
3. `~/logs/txengine/recon_listeners.log` — only records where `extra.recon_id` **IS** set. DEBUG level, 500 MB rotation, 7-day retention, lzma-compressed.

Format (for both file sinks; stderr uses loguru default):

```
{extra} <green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>
```

ANSI color tags are present in the format string. Loguru's `{extra}` is the leading dict literal (e.g. `{'recon_id': '12345...'}`) — this is **the only structured field that appears in the log line**.

Helper `recon_listener_logger(recon_id)` at `Logger.py:33-35` returns `logger.bind(recon_id=recon_id)`. Every `IReconListener` subclass holds a logger bound this way (see `IReconListener.py:24`).

The user-described log-shipping split:

- `job="transfer-engine-logs"` — collects the stderr/`altonomy.log` sink (app, endpoint, exchange-library logs, on-chain helpers).
- `job="transfer-engine-recon"` — collects `recon_listeners.log` (per-recon listener thread output).

Important: every `extra` field bound by `logger.bind(recon_id=...)` appears in **the rendered file line** as a dict literal at the start: `{'recon_id': '12345...'}`. This is the substring to match for cross-referencing recon flows.

### 2.2 Endpoints — `endpoints/transfer.py` and `endpoints/tx_recon.py`

Both files use the module-level loguru `logger` imported from `..Logger`.

`endpoints/transfer.py`:

| File:line | Level | Message format | Endpoint |
|:---|:---|:---|:---|
| `transfer.py:29` | EXCEPTION | `Exception during transfer: bad altonomy-exchanges code` | `POST /transfer/internal_transfer` failure |
| `transfer.py:33` | DEBUG | `{result}` (bare dict from exchange lib) | `POST /transfer/internal_transfer` success result |
| `transfer.py:51` | EXCEPTION | `Exception during withdrawal: bad altonomy-exchanges code` | `POST /transfer/withdraw_funds` failure |
| `transfer.py:55` | DEBUG | `{result}` (bare dict) | `POST /transfer/withdraw_funds` success |
| `transfer.py:73` | EXCEPTION | `Exception getting withdrawal fees: {e}` | `GET /transfer/withdrawal_fees` |
| `transfer.py:95-97` | EXCEPTION | `Exception during get_account_balance: bad altonomy-exchanges code` | `GET /transfer/account_balance` |
| `transfer.py:101` | DEBUG | `{bal}` | `GET /transfer/account_balance` success |
| `transfer.py:117-119` | EXCEPTION | `Exception during get_deposit_addresses: bad altonomy-exchanges code` | `GET /transfer/deposit_addresses` |
| `transfer.py:123` | DEBUG | `{addrs}` | `GET /transfer/deposit_addresses` success |
| `transfer.py:139-141` | EXCEPTION | `Exception during get_deposit_addresses: bad altonomy-exchanges code` *(sic — message says deposit_addresses but the endpoint is `transfer_history`)* | `GET /transfer/transfer_history` failure |
| `transfer.py:145` | DEBUG | `{hist}` | `GET /transfer/transfer_history` success |
| `transfer.py:160-162` | EXCEPTION | `Exception getting account_uid for {exchange_name} {account_id}` | `GET /transfer/account_uid` |

Identifier substring shapes:
- `account_uid for {exchange_name} {account_id}` — bare space-separated, no `=` sign
- All other endpoint logs are exception-only and embed nothing structured beyond what the exception itself stringifies

`endpoints/tx_recon.py`:

| File:line | Level | Message format | Endpoint |
|:---|:---|:---|:---|
| `tx_recon.py:57` | DEBUG | `{status}` (bare `TxRecon` model with `recon_id`, `status`, etc.) | `GET /txrecon/status?recon_id={id}` |
| `tx_recon.py:67` | DEBUG | `{status}` | `POST /txrecon/cancel` |

These endpoints do not log the incoming `recon_id` themselves; the listener thread logs the `recon_id` via `extra` binding.

### 2.3 Recon listener base — `IReconListener.py`

All listener-thread logs are bound with `extra.recon_id`, so they land in `recon_listeners.log` and `{job="transfer-engine-recon"}`.

| File:line | Level | Message | When |
|:---|:---|:---|:---|
| `IReconListener.py:44` | INFO | `Thread for transaction listener started` | Thread spawn |
| `IReconListener.py:52` | ERROR | `{traceback}` (raw traceback string) | Exception in `check_tx_history_rest()` |
| `IReconListener.py:68` | INFO | `breaking!!!` | Hit terminal status (fail/canceled/success) |
| `IReconListener.py:69` | INFO | `{self.status}` (bare dict-repr — `{'recon_id': ..., 'status': ..., 'error': ..., 'error_msg': ..., 'timestamp': ..., 'tx_id': ..., 'amount': ..., 'data': ...}`) | Status snapshot at break |
| `IReconListener.py:70` | INFO | `{self.status["status"]}` (just the enum string) | Status value at break |
| `IReconListener.py:76` | INFO | `ending!!!` | End-of-window timeout |
| `IReconListener.py:77` | INFO | `{self.status}` | Status snapshot at timeout |
| `IReconListener.py:78-81` | INFO | `ending transaction listener, transaction not found between {start} and {end}` | Window-expired explanation |
| `IReconListener.py:90` | INFO | `ending transaction listener (canceled)` | Cancel-driven exit |
| `IReconListener.py:126` | DEBUG | `updated tx recon status to {self.status}` | Every status mutation — high-volume |

The bound `extra.recon_id` appears as a leading `{'recon_id': '12345...'}` literal in the rendered file line.

### 2.4 Concrete listeners

All four listeners share the same shape — call `self.exchange.get_account_transactions(...)`, log the raw response, walk it for matches.

| File:line | Level | Message | Listener type |
|:---|:---|:---|:---|
| `InternalReconListener.py:35` | DEBUG | `received txs: {txs}` | Internal recon |
| `InternalReconListener.py:49-52` | WARNING | `external_id provided by {exchange_class_name} is not in str format, fix at exchange level (converting)` | Internal recon — type-coercion |
| `WithdrawReconListener.py:27` | DEBUG | `received txs: {txs}` | Withdraw recon |
| `DepositReconListener.py:27` | DEBUG | `received txs: {txs}` | Deposit (exchange-side) recon |
| `OnChainReconListener.py:146` | ERROR | `{traceback}` | On-chain source raised |
| `OnChainReconListener.py:147` | ERROR | `{s.name}` + positional `{self.source_responses.get(s.name)}` (note: passed as 2-arg `.error(s.name, dict)` — loguru renders the first as the message and the second is dropped unless format includes positional binding; this is effectively a bug, see Section 4) | Per-source response when on-chain source raised |

Identifier substrings that show up in listener output:

- `recon_id` — only via the `{'recon_id': '...'}` prefix supplied by the loguru `extra`
- `tx_id` and `amount` — only as keys inside `received txs: [...]` raw exchange response, OR inside `self.status` dict snapshots
- `external_id` and `transaction_ref` — exchange-side normalised TX identifiers, appearing in `received txs: [...]`
- For internal recons, `external_id provided by {ExchangeClass} is not in str format` — searchable phrase if you want to find type-coercion incidents

OnChain status updates additionally produce these error strings (logged via `self.update_status(...)` which calls `self.logger.debug(f"updated tx recon status to {self.status}")` at the base class):

- `tx from addr {tx.from_address} and to addr {tx.to_address} do not match recon address {self.address}` — emitted as `error_msg` key when address mismatch (`OnChainReconListener.py:183-184`)
- `tx with hash {self.tx_id} has amount {tx.amount} not matching recon amount {self.amount} with fee threshold {self.fee_threshold}` — amount mismatch (`OnChainReconListener.py:200-201`)
- `tx with hash {self.tx_id} has timestamp {tx.timestamp} not between {start} and {end}` — timestamp mismatch (`OnChainReconListener.py:210-211`)

### 2.5 On-chain sources — `recon_sources/`

Two patterns coexist:

- **Bound (preferred)**: `OnChainSource.__init__` accepts `logger=`, falling back to the module-level loguru `logger`. `OnChainReconListener` always passes `self.logger` (the recon-bound one) at construction time (`OnChainReconListener.py:128`). Sources that use `self.logger` (e.g. `Ada`, `Solana`, `Avax`, `Optimism`, `Polygon`, `RippleAPI`, `Theta`, `Ton`, `Hiro`, `Seiscan`, `Near`) **do** carry the `recon_id` binding.
- **Unbound (anti-pattern, see Section 4)**: `Berascan.py`, `Blockchair.py`, `Taostats.py` directly `from loguru import logger` and call `logger.exception(...)` / `logger.error(...)`. These lines lose the `recon_id` binding and land in the app log, not `recon_listeners.log`.

Common substrings emitted by `OnChainSource._resp_data` (base, `OnChainSource.py:37`) for all sources:

```
recv -- {resp.text} request params -- {kwargs}
```

This is the cheapest line to grep when you want to see "what did chain X return for tx Y" — it carries the URL kwargs (which include `tx_id`/address) and the raw response body.

Per-source error phrases (the `logger.error`/`logger.exception` strings) are descriptive enough to be self-explanatory; high-value ones:

- `blockchair._find_eth_tx - failed to get data for {tx_id} | data={data} | e={e}` (`Blockchair.py:55-56`)
- `berascan - tx {tx_id} failed to get timeStamp {tx_data} - {e}` (`Berascan.py:69-71`)
- `berascan - tx {tx_id} failed to get conformations or block number ...` *(sic — "conformations")* (`Berascan.py:78-79`)
- `berascan - tx {tx_id} found but confirmations below threshold confirmations={n}` (`Berascan.py:84-85`)
- `Failed to parse the response {data}` — common phrase across Ada, Solana, RippleAPI, Optimism, Hiro, Seiscan, Theta, Near, Avax
- `Failed to parse TON transaction response: {data}` (`Ton.py:67`)
- `Transaction {tx_id} not confirmed yet` (`Theta.py:39`) — INFO level

### 2.6 Exchange manager — `exchange_manager.py`

Module-level loguru `logger`.

| File:line | Level | Message |
|:---|:---|:---|
| `exchange_manager.py:35` | EXCEPTION | `failed to get account {account_id} secret from vault` |
| `exchange_manager.py:53` | EXCEPTION | `cannot initiate exchange {exchange_name}` |

Critical: `exchange_manager.get(...)` passes `logger=logger` (loguru) into `exchanges.Exchange(...)`, so all subsequent exchange-library logs flow through loguru. When the call originates from a recon listener, `IReconListener.__init__` then overwrites `self.exchange.logger = self.logger` (the bound one — `InternalReconListener.py:28`, `WithdrawReconListener.py:21`, `DepositReconListener.py:21`), so exchange-vendor request/response logs **acquire the `recon_id` binding** for the duration of that listener's lifecycle. This is a key triage hook: filtering on `recon_id` will surface the actual exchange HTTP traffic too.

### 2.7 Utility / HTTP retry — `utils.py`

`utils.request_with_retry` is a generic HTTP wrapper used by various recon helpers. Module-level loguru `logger`:

| File:line | Level | Message |
|:---|:---|:---|
| `utils.py:82` | DEBUG | `sending [{rc}] {method} {url} {kwargs}` |
| `utils.py:84-86` | DEBUG | `received [{rc}] {method} {url} {kwargs} {status_code}|{response.text}` |
| `utils.py:95-97` | EXCEPTION | `exception on failure [{rc}] {method} {url} {kwargs}|e={e}` |

Identifier shape: `[{retry_count}]` bracketed integer, `|e={...}` exception-tail separator.

### 2.8 Config — `config.py`

`config.py:22` — single ERROR log when config loading fails. Out of scope for runtime triage; mentioned for completeness.

---

## 3. sg-altonomy-exchanges library — in-process logging from transfer-engine

This library is loaded into the transfer-engine process. It does **not** ship to Loki on its own; its output flows through whatever `logger` object the host service injected at `exchanges.Exchange(...)` construction time. From transfer-engine, that is the loguru module logger — and inside a recon listener, it is the `recon_id`-bound child of it.

Scope: only `CoinMarket` (base) and `Binance` (concrete) — other adapters follow the same patterns.

### 3.1 CoinMarket base — `CoinMarket.py`

Default logger fallback: `_logger(self.exchange_name())` where `_logger = altonomy.core.logger` → `altonomy.loggers.logger.Logger`. This custom queue-backed logger is **only used if no `logger=` was passed** to the constructor. In transfer-engine, loguru is always passed, so this fallback never engages in practice.

If it ever did engage, the format would be:
```
%(levelname)s %(asctime)-15s %(_server_ip)s %(_port)s [%(process)d-%(thread)d] %(_filename)s:%(_function)s:%(_line)d %(message)s
```
…with file output to `${LOGGINGPATH}${LOGGINGFILE}` and/or stdout per env. Mention only as a fallback — don't chase this format in Loki under normal conditions.

Notable log call sites (all `self.logger` — i.e. loguru when called from transfer-engine):

| File:line | Level | Message |
|:---|:---|:---|
| `CoinMarket.py:104` | DEBUG | `retrieved '{path}{exchange_name}{account_id}' from redis` (inside `@RedisFallback` decorator) |
| `CoinMarket.py:262` | ERROR | `HTTP 4XX/5XX error detected - {self.last_error_message}` |
| `CoinMarket.py:289-290` | ERROR | `cannot get external ip address` then `{traceback}` |
| `CoinMarket.py:356` | ERROR | `{errstr}, exchange={self.exchange_name()}, data={kwargs.get('data')}, params={kwargs.get('params')}` |
| `CoinMarket.py:367` | ERROR | `{traceback}` |
| `CoinMarket.py:375` | ERROR | `{errstr} {exchange_name} args({kwargs})` |
| `CoinMarket.py:388` | CRITICAL | `{errstr} {exchange_name} args({kwargs})` (legacy `_handle_response`) |
| `CoinMarket.py:531` | ERROR | `Failed to get client tradablecoins due to {e}` |
| `CoinMarket.py:980` | (uses `self.logger.port`) | — service ID lookup, not a log call |
| `CoinMarket.py:2238-2256` | ERROR | `Unexpected response format: {resp}` and traceback, around `send_order` |
| `CoinMarket.py:2283` | ERROR | `error in send order: {error}` |
| **`CoinMarket.py:2738`** | DEBUG | `sending {method} {url} {kwargs}` — inside `CoinMarket.API.request_with_retry` |
| **`CoinMarket.py:2740-2742`** | DEBUG | `response from {method} {url} {kwargs} -- {response.text}` |
| `CoinMarket.py:2747` | DEBUG | `retrying` |
| `CoinMarket.py:2753-2754` | ERROR | `{e} occured while {method} {url} {str(kwargs)}` then `{traceback}` *(sic — "occured")* |
| `CoinMarket.py:2788-2790` | DEBUG | `remote signing @ {sign_url} with {json.dumps(sign_data)}` (only when `remote_sign=True`) |
| `CoinMarket.py:2803` | DEBUG | `received {resp_json} from signature engine` |
| `CoinMarket.py:2806-2807` | ERROR | `remote sign failed` then `{traceback}` |

Identifier shapes the library produces:

- `sending {METHOD} {URL} {KWARGS}` — the most useful grep for vendor traffic; `{KWARGS}` includes path-and-query params, signed headers (masked or not depending on subclass), and body
- `response from {METHOD} {URL} {KWARGS} -- {response.text}`
- `HTTP 4XX/5XX error detected - {errstr}` — every 4xx/5xx from any vendor lands here
- `{errstr}, exchange={NAME}, data={...}, params={...}` — exchange-specific error parsed by `_handle_exchange_response`
- `error in send order: {error}` — order-rejection summary

There is no `account_id=` prefix in these messages — the binding only exists because the call originated from a recon listener that bound `recon_id`. To correlate with a specific account, you need to cross-reference via `recon_id` → recon-start log → `account_id={...}` argument visible in the settlement-engine `Request to start recon for ... account_id=...` (wait, settlement-engine doesn't print `account_id` either — it goes into the body of the HTTP request only). Practical consequence: **the only reliable cross-ref between settlement-engine and exchange traffic is `recon_id`**.

### 3.2 Binance concrete — `Binance.py`

Binance overrides `_request` directly (does not use `CoinMarket.API.request_with_retry`). The duplicated logs at `Binance.py:212-217` and `230-234` therefore live in a parallel codepath:

| File:line | Level | Message |
|:---|:---|:---|
| `Binance.py:212` | DEBUG | `sending {method} {url} {kwargs}` |
| `Binance.py:215` | DEBUG | `received {method} {url} {kwargs} {response.text}` (skipped if URL ends with `/exchangeInfo` — avoids spam) |
| `Binance.py:217` | INFO | `error %s occur while %s %s %s` *(sic — "occur" not "occurred", `%s` percent-formatted)* — note `INFO`, not `ERROR` |
| `Binance.py:230` | DEBUG | `{url}, {method}, {kwargs}` (retry line) |
| `Binance.py:233` | INFO | `error %s occur while retry %s %s %s` |
| `Binance.py:473` | ERROR | `{traceback}` (around send_order parse) |
| `Binance.py:1013` | DEBUG | `The account of BINANCE is locked!` (string concatenation, hard-coded) |
| `Binance.py:1015` | ERROR | `failed_to_fetch_spot_balances|{traceback}` |
| `Binance.py:1039` | ERROR | `order not sent, {self.last_error_message}` |
| `Binance.py:1075` | DEBUG | `Cannot get the orderbook of trading pair {pair}!` |
| `Binance.py:1343` | DEBUG | `failed to retrieve candle data for {pair}` |
| `Binance.py:1496-1498` | WARNING / DEBUG | `limit of {limit} reached for {symbol}, {startTime} to {endTime}` / `new records = {n}` |
| `Binance.py:1603` | DEBUG | `sending message - {_message}` (websocket) |
| `Binance.py:1617-1903` | mixed | Three WebSocket loops with `started`/`loaded configuration`/`{name} for {pair} started`/`received websocket data {result}`/`{name} for {pair} terminated` patterns |
| `Binance.py:1934-1967` | ERROR | `error in new_future_account_transfer : {e}`, `error in get_future_account_transactions : {e}`, `error in transfer_between_spot_margin : {e}` |
| `Binance.py:2035` | ERROR | `Failed to fetch Binance withdrawal fees: {e}` |
| `Binance.py:2051` | DEBUG | `earn balances total page: {n}` |
| `Binance.py:2069` | WARNING | `Failed to fetch earn balances page {n}, returning partial data` |
| `Binance.py:2071` | DEBUG | `Binance Earn balances: {result}` |
| `Binance.py:2572` / `2603` | ERROR | `{traceback}` around `get_account_transactions` deposit/withdrawal loops |

Identifier substring shapes specific to Binance:

- `sending {method} {url} {kwargs}` and `received {method} {url} {kwargs} {response.text}` — same shape as base, but emitted from a different line. Grep on the URL path (e.g. `/api/v3/account`, `/sapi/v1/capital/deposit/hisrec`, `/wapi/v3/withdraw.html`) to identify which Binance endpoint was hit.
- `error %s occur while ...` — Python `%`-formatting leaves `%s` template literal in some search tools; use `error` + verb-form match.
- `failed_to_fetch_spot_balances|` — pipe-separated tag, useful as a unique grep.
- `BINANCE` (uppercase, from `exchange_name()`) appears in `_handle_exchange_response` errors via `exchange={exchange_name}`.

Other exchange adapters are out of scope per the brief but follow the same `self.logger.debug(f"sending ... {url}")` / `self.logger.debug(f"received ...")` shape, with adapter-specific `errstr` formats inside `_handle_exchange_response`.

---

## 4. Gaps and anti-patterns

- **No structured logging anywhere.** Both services emit plain text. Identifiers are embedded in the message body, sometimes as `key=value`, sometimes as `key={value}` (Pydantic repr), sometimes bare (`tx_id` without label). Loki parsers cannot rely on label extraction — use `|=` substring matching.
- **Two different identifier-substring conventions in settlement-engine.** Some lines use `task_id={n} part_id={n} recon_id={n}` (clean key=value), others use `transfer task part {n} of the transfer task {n}` (English prose, no key). When in doubt, search both shapes.
- **Typos in log messages are load-bearing.** Several lines have stable typos: `End Lopp`, `occured`, `POrtfolio`, `conformations`. Match exact strings — they are stable and high-precision filters.
- **`process_transfer.py` uses a separate logger.** The `getLogger('long_running')` lines (`Begin Loop`, `Process Waiting Part`, `Process Transfer`, `Process Recon`, `Resolve Dest Recon`, `End Lopp`, plus the 5 phase-level error logs) do not share the formatter of the ctrl logs. They appear as `LEVEL:long_running:<msg>` rather than the FastAPI/uvicorn format used by `self.logger.*`.
- **Three on-chain sources lose the `recon_id` binding.** `Berascan.py`, `Blockchair.py`, `Taostats.py` use module-level loguru `logger` instead of `self.logger`. Their lines land in `altonomy.log` / `{job="transfer-engine-logs"}`, not `recon_listeners.log` / `{job="transfer-engine-recon"}`. To trace a recon involving these chains, search BOTH job labels for the `tx_id` value (or the chain-name marker, e.g. `berascan -`).
- **`OnChainReconListener.py:147` is buggy.** `self.logger.error(s.name, self.source_responses.get(s.name))` passes two positional arguments to loguru `.error()`. Loguru treats the first as message; the second is silently dropped. The per-source response dict is therefore NOT in the log line — only `s.name`. If you need to see the response, find the preceding `recv -- {resp.text} request params -- {kwargs}` from `OnChainSource._resp_data`.
- **Binance logs vendor HTTP errors at INFO, not ERROR.** `Binance.py:217` and `:233` use `self.logger.info("error %s occur while ...")`. A Loki alert on "exchange error" must include `level=INFO` for Binance, or it will miss most of them.
- **No `account_id` / `nitro_account_id` printing on the request path.** When grepping for a specific account, the only deterministic anchor is the `recon_id` (long-running ctrl) or the `account_product_id`/`source_account` field embedded inside `src_info` / Pydantic-repr dicts. Plain `account_id=` text is rare and inconsistent (mostly in transfer-engine `Exception getting account_uid for {exchange_name} {account_id}` — no `=`, just whitespace).
- **External clients in settlement-engine are log-silent.** `external/exchanges.py`, `external/optimus_client.py`, `external/nitro_client.py`, `external/txn_client.py`, etc. — none log. Failures surface only through the response dict embedded in upstream `logger.error(...)` lines. If you suspect an external HTTP issue, look for the `response {resp}` substring in `Request to start recon for ...` lines.
- **`api_utils.log` is a different formatter again.** Lines emitted via `api_utils.log.debug(f"ak_test|get_aging_settlement|...")` (3 sites in `settlement_v2_ctrl.py`) use the `<<--YYYY-MM-DD HH:mm:ss.mmm-->> LEVEL [file:func:line] message` format. Useful only because the `ak_test|` prefix is unique. Don't generalise from this format to other lines.

---

## 5. How to query (Loki templates)

### 5.1 Base selectors (as provided by the team)

```
# transfer-engine — combined app + recon listener (single VM, both files shipped)
{server="w04.se1.altono.app"}

# transfer-engine — granular
{server="w04.se1.altono.app", job="transfer-engine-logs"}      # stderr/altonomy.log (app, endpoints, exchange lib, unbound on-chain helpers)
{server="w04.se1.altono.app", job="transfer-engine-recon"}     # recon_listeners.log (only records bound with extra.recon_id)

# settlement-engine — both api and long-running pods
{pod=~".*(settlement-engine-api|settlement-engine-long).*", env="prod"}
```

### 5.2 Templates per identifier

Wrap each of the below in your time window of choice. Default windows: `1h` for live triage, `24h` for investigations, `7d` is the retention horizon on transfer-engine file sinks.

#### task_id (settlement-engine)

```logql
# All log lines for a given transfer task
{pod=~".*(settlement-engine-api|settlement-engine-long).*", env="prod"}
  |= "task_id=12345"

# Only the long-running loop view (phase errors + recon-start lifecycle)
{pod=~".*settlement-engine-long.*", env="prod"} |= "task_id=12345"

# Recon-start lifecycle only (succeeded / failed / will be retried)
{pod=~".*settlement-engine-long.*", env="prod"}
  |= "task_id=12345" |= "Request to start recon"
```

Alternate substring shapes (try if `task_id=` returns nothing):
- `|= "transfer task 12345"` (matches `transfer task part {n} of the transfer task {task_id}`)
- `|= "tag 12345"` (matches QMS `tag {task.settlement_id}` — only when settlement_id == task_id, NOT a general substitute)

#### part_id (settlement-engine)

```logql
{pod=~".*settlement-engine-long.*", env="prod"}
  |= "task_id=12345" |= "part_id=1"
```

#### recon_id (both services — the highest-value cross-service identifier)

```logql
# Settlement-engine view: recon-start request + outcome
{pod=~".*settlement-engine-long.*", env="prod"} |= "recon_id=12345678901234567890"

# Transfer-engine view: recon-listener thread, bound via loguru extra
{server="w04.se1.altono.app", job="transfer-engine-recon"}
  |= "'recon_id': '12345678901234567890'"

# Transfer-engine view including unbound on-chain helpers
# (Berascan/Blockchair/Taostats lose the recon_id binding — fall back to tx_id)
{server="w04.se1.altono.app"} |= "12345678901234567890"
```

Note the **integer-stringified UUID5** shape (e.g. `12345678901234567890`, not `abc-...`). Generated by `_generate_recon_id` (`transfer_long_running_ctrl.py:705-706`).

#### tx_id (transfer-engine, on-chain)

```logql
# All recon listener output mentioning this hash
{server="w04.se1.altono.app", job="transfer-engine-recon"} |= "0xabc..."

# Including unbound on-chain source logs (Berascan/Blockchair/Taostats)
{server="w04.se1.altono.app"} |= "0xabc..."

# On-chain mismatch reason
{server="w04.se1.altono.app", job="transfer-engine-recon"}
  |= "0xabc..." |~ "(AMOUNT MISMATCH|ADDRESS MISMATCH|TIMESTAMP MISMATCH)"
```

#### internal_id / external_id (transfer-engine, exchange-side)

```logql
# Internal/Withdraw recon listener match candidates
{server="w04.se1.altono.app", job="transfer-engine-recon"}
  |= "'recon_id': '12345...'" |= "external_id"

# Type-coercion warning (string-vs-int from a vendor)
{server="w04.se1.altono.app", job="transfer-engine-recon"}
  |= "external_id provided by"
```

#### account_id / account_product_id (settlement-engine)

```logql
# Balance-related warnings on a specific account
{pod=~".*settlement-engine-api.*", env="prod"}
  |= "[BALANCE]" |= "account_id=42"

# Account-lookup miss
{pod=~".*settlement-engine-api.*", env="prod"}
  |= "Cannot find account with account_product_id=42"
```

#### exchange endpoint URL (transfer-engine + exchanges lib)

```logql
# All Binance sapi/v1 traffic
{server="w04.se1.altono.app", job="transfer-engine-logs"} |= "/sapi/v1/"

# Failed Binance withdrawals (logged at INFO, not ERROR — see Section 4)
{server="w04.se1.altono.app", job="transfer-engine-logs"}
  |= "error" |= "occur while" |= "/wapi/v3/withdraw"

# HTTP 4xx/5xx detected anywhere in the exchanges library
{server="w04.se1.altono.app", job="transfer-engine-logs"}
  |= "HTTP 4XX/5XX error detected"

# Specific exchange + error format
{server="w04.se1.altono.app", job="transfer-engine-logs"}
  |= "exchange=BINANCE" |= "data=" |= "params="
```

#### Lifecycle phrases (high-precision substrings)

```logql
# Long-running loop heartbeat (10s cadence)
{pod=~".*settlement-engine-long.*", env="prod"} |= "Begin Loop"

# End of each loop iteration (typo, exact match)
{pod=~".*settlement-engine-long.*", env="prod"} |= "End Lopp"

# Recon-listener thread spawn
{server="w04.se1.altono.app", job="transfer-engine-recon"}
  |= "Thread for transaction listener started"

# Recon-listener terminal break
{server="w04.se1.altono.app", job="transfer-engine-recon"} |= "breaking!!!"

# QMS bulk transfer
{pod=~".*settlement-engine-api.*", env="prod"} |= "Creating QMS transfers"

# Settlement-match exceptions
{pod=~".*settlement-engine-api.*", env="prod"}
  |= "Exception occurred for settlement match"
```

#### Counterparty-scoped triage

```logql
{pod=~".*settlement-engine-api.*", env="prod"}
  |= "counterparty_ref = CPTY001"
```

Note the spaces around `=` — they are load-bearing in `settlement_v2_ctrl.py`.

### 5.3 Time-window patterns

- **Active incident**: 5–15 min window aligned on the user-reported timestamp. Long-running loop heartbeat = 10s, so 5 min gives ~30 iterations of phase markers.
- **Post-mortem for one task**: query by `task_id` over 1–7 days. Settlement-engine retention is k8s log retention (Loki tenant default). Transfer-engine recon files retain 7 days on-disk; if Loki shipped them, the same applies.
- **Cross-service flow**: start with `recon_id` (long-running ctrl `Request to start recon ...` line gives you both the integer-UUID and a timestamp), then pivot to transfer-engine with the same `recon_id` substring. Allow ±5 min around the settlement-engine log time for clock skew + listener start latency.
- **Vendor outage detection**: `{server="w04.se1.altono.app", job="transfer-engine-logs"} |= "HTTP 4XX/5XX error detected"` over 1 h, group/rate by `exchange={NAME}` substring.
