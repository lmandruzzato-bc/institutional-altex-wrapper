# Logging and Loki — Altex backend services

Loki playbook for transfer-triage. Given identifier from failed transfer — `task_id`, `part_id`, `recon_id`, `tx_id`/`txn_id`, `internal_id`/`external_id`, account id, or exchange endpoint URL — gives exact LogQL selector + substring filters that find truth, plus gotchas that make naive greps miss.

Neither backend emit structured logs. Identifiers live inside message body — sometimes `key=value`, sometimes Pydantic/loguru dict literal, sometimes bare English prose. **Loki label extraction not work; match on substrings (`|=` / `|~`).**

Cross-references:

- `error-codes.md` — decode exchange/local error strings these logs surface (`HTTP 4XX/5XX error detected`, `error in send order`, reason fields).
- `altex-db-schema.md` — `recon_id`, `task_id`, `txn_id` live as columns there; `recon_id` = cross-service log anchor.
- `altex-overview.md` — service split / architecture.
- `timezones.md` — **everything below is UTC.** All log times (ingest + line body) are UTC; the Loki MCP query bounds (`startRfc3339`/`endRfc3339`) are interpreted as UTC. Convert API epoch fields to RFC3339-UTC (`…Z`) before windowing. The human Grafana UI renders in browser-local time — the "0 results" trap.

Two services in scope:

- **`sg-altonomy-settlement-engine`** — owns transfer state-machine, settlement APIs, `process_transfer` long-running driver. Two entrypoints in scope: `settlement-engine-api` (`uvicorn altonomy.settlement_engine.main:app`) and `settlement-engine-long-running` (`python -m altonomy.settlement_engine.process_transfer`). Runs in Kubernetes (`env="prod"`). Cron jobs, deal subscribers, sync workers, client-report out of scope.
- **`sg-altonomy-transfer-engine`** — owns exchange-call proxying (`/transfer/*`) and reconciliation listeners (`/txrecon/*`). Embeds `sg-altonomy-exchanges` library in-process, so all exchange-vendor request/response logs originate from that library, flow through transfer-engine's loguru sinks. Runs on single VM (`server="w04.se1.altono.app"`). For `sg-altonomy-exchanges`, only `CoinMarket` base class + `Binance` concrete adapter characterised; other adapters follow same shapes.

---

## 1. Quick reference: identifier → LogQL

Pick base selector for service, add substring filter(s), wrap in time window (Section 7). All selectors **environment-specific** (`server=`/`pod=~`/`job=` values came as provided by team, confirmed against live prod Loki at authoring time — see Section 6 for verification notes).

| You have | Service / sink | Selector + filter |
|:---|:---|:---|
| `task_id` | settlement-engine (both pods) | `{pod=~".*(settlement-engine-api\|settlement-engine-long).*", env="prod"} \|= "task_id=<id>"` |
| `task_id`, recon lifecycle only | settlement-engine long-running | `{pod=~".*settlement-engine-long.*", env="prod"} \|= "task_id=<id>" \|= "Request to start recon"` |
| `part_id` | settlement-engine long-running | `… \|= "task_id=<id>" \|= "part_id=<n>"` |
| `recon_id` (settlement side) | settlement-engine long-running | `{pod=~".*settlement-engine-long.*", env="prod"} \|= "recon_id=<id>"` |
| `recon_id` (transfer side) | transfer-engine recon sink | `{server="w04.se1.altono.app", job="transfer-engine-recon-logs"} \|= "'recon_id': '<id>'"` |
| `recon_id`, incl. unbound on-chain helpers | transfer-engine combined | `{server="w04.se1.altono.app"} \|= "<id>"` |
| `tx_id` / `txn_id` (on-chain) | transfer-engine recon sink | `{server="w04.se1.altono.app", job="transfer-engine-recon-logs"} \|= "<hash>"` |
| `tx_id` / `txn_id`, incl. unbound sources | transfer-engine combined | `{server="w04.se1.altono.app"} \|= "<hash>"` |
| `internal_id` / `external_id` | transfer-engine recon sink | `{server="w04.se1.altono.app", job="transfer-engine-recon-logs"} \|= "'recon_id': '<id>'" \|= "external_id"` |
| `account_product_id` | settlement-engine api | `{pod=~".*settlement-engine-api.*", env="prod"} \|= "account_product_id=<id>"` |
| exchange endpoint URL | transfer-engine app sink | `{server="w04.se1.altono.app", job="transfer-engine-logs"} \|= "/sapi/v1/…"` |
| counterparty | settlement-engine api | `{pod=~".*settlement-engine-api.*", env="prod"} \|= "counterparty_ref = <ref>"` (spaces load-bearing) |

`recon_id` = single reliable cross-service anchor: settlement-engine prints as `recon_id=<int>`; transfer-engine prints inside loguru `extra` dict literal `{'recon_id': '<int>'}`. Same value crosses both services. See Section 5.

---

## 2. Base selectors (environment-specific)

```logql
# settlement-engine — both api and long-running pods
{pod=~".*(settlement-engine-api|settlement-engine-long).*", env="prod"}

# settlement-engine — narrow to one entrypoint
{pod=~".*settlement-engine-api.*", env="prod"}      # FastAPI app
{pod=~".*settlement-engine-long.*", env="prod"}     # process_transfer driver

# transfer-engine — combined (single VM, both files shipped)
{server="w04.se1.altono.app"}

# transfer-engine — granular
{server="w04.se1.altono.app", job="transfer-engine-logs"}        # stderr/altonomy.log (app, endpoints, exchange lib, unbound on-chain helpers)
{server="w04.se1.altono.app", job="transfer-engine-recon-logs"}  # recon_listeners.log (only records bound with extra.recon_id)
```

> Transfer-engine recon sink ships under `job="transfer-engine-recon-logs"` — note trailing `-logs`. App sink = `job="transfer-engine-logs"`. (Common mistake: query `job="transfer-engine-recon"`, matches nothing.) On settlement-engine side, Kubernetes-collector `job` label = `optimus/settlement-engine`, but `pod=~…` + `env="prod"` selector above = right handle, resolves both pods.

Pod ownership for settlement-engine narrow selectors:

- `settlement-engine-long` — `process_transfer.py` loop heartbeat, all `transfer_long_running_ctrl.py` recon-init lifecycle.
- `settlement-engine-api` — `transfer_api_ctrl.py` (QMS bulk, balance checks, single internal/external transfer creates), `settlement_v2_ctrl.py` (settle/unsettle/match), `settlements_v2_api.py`, and `api_utils.log` debug lines.

---

## 3. settlement-engine

### 3.1 Logger setup

Four coexisting logger flavours; **no JSON formatter, no structured-logging library.** Everything stdlib `logging` or loguru-flavoured uvicorn logger, written to stdout (k8s collects via Loki). Practical consequence: **line-prefix format unreliable across lines in same service — match on message body, not prefix.**

| Logger | Built in | Used by |
|:---|:---|:---|
| `logging.getLogger('long_running')` | module level of `process_transfer.py` | Per-phase loop heartbeat lines in `process_transfer.py` (`logger.info("Begin Loop")`, etc.) + five phase-level `logger.error(...)` fallbacks. |
| `fastapi.logger.logger` (loguru-flavoured uvicorn logger, imported as `fastapi_logger`) | default `logger=` kwarg in ctrl `__init__`s — `TransferCtrl`, `SettlementV2Ctrl`, `ClientActivityReportCtrl` | Every `self.logger.{info,error,exception,warning,debug}` call in ctrl layer. Both API requests AND long-running loop log through this: `process_transfer.py` constructs `TransferLongRunningCtrl(session, token)` without passing `logger=`, so `TransferCtrl`'s default kicks in. |
| `logging.getLogger('settlement_v2_api')` (exposed as `log`) | module level of `settlements_v2_api.py` | Two `log.error(err)` calls feeding `Invalid Counterparty Ref …` and `Invalid POrtfolio Numbers …`. |
| `altonomy.loggers.log_utils.get_simple_logger()` (root logger, custom format) — exposed as `api_utils.log` | `common/api_utils.py` | Three `ak_test\|…` aging-settlement debug lines in `settlement_v2_ctrl.py`. Renders `<<--YYYY-MM-DD HH:MM:SS.mmm-->> LEVEL [filename:funcName:lineno] message` format. |

> **Observed in prod:** `getLogger('long_running')` lines do NOT render bare. Ship with same `<<--YYYY-MM-DD HH:mm:ss.mmm-->> LEVEL [process_transfer.py:loop:NN] <msg>` structured prefix as `api_utils.log` lines, often appear duplicated (one bare line + one prefixed line on `stdout`/`stderr` respectively). Don't rely on prefix to distinguish loggers — filter on message body.

### 3.2 Long-running loop — `process_transfer.py`

Entrypoint `settlement-engine-long-running`. `main()` runs forever; re-logs into Optimus roughly every 30 min, loops every ~10s (`sleep(10)`) through five phases. Heartbeat lines useful as time anchors — at 10s cadence, 5-minute window gives ~30 iterations.

These lines emitted by loop driver (`loop()`) through `getLogger('long_running')`:

| Level | Message | When |
|:---|:---|:---|
| INFO | `Begin Loop` | Start of each iteration |
| INFO | `Process Waiting Part` | Before `process_part_waiting` |
| INFO | `Process Transfer` | Before `process_transfer` |
| INFO | `Process Recon` | Before `process_recon` |
| INFO | `Resolve Dest Recon` | Before `resolve_dest_recon` |
| INFO | `End Lopp` *(sic — "Lopp", not "Loop")* | End of each iteration |
| ERROR | `Start running error: {e}` + traceback | Unhandled exception in `process_part_waiting` |
| ERROR | `Start transfer error: {e} {model_to_dict(part)}` + traceback | Per-part exception inside `process_transfer` (embeds full part dict) |
| ERROR | `Process transfer unhandled error: {e}` + traceback | Outer exception in `process_transfer` |
| ERROR | `Recon error: {e}` + traceback | Exception inside `process_recon` |
| ERROR | `Resolve Dest recon error: {e}` + traceback | Exception inside `resolve_dest_recon` (note lowercase "recon", unlike title-case heartbeat above) |

### 3.3 Long-running ctrl — `transfer_long_running_ctrl.py`

All driven by `self.logger` (default `fastapi.logger.logger`). Recon-init lifecycle here carries most useful triage identifiers.

| Level | Message format | Lifecycle event |
|:---|:---|:---|
| EXCEPTION | `Failed to parse recon src log {recon_src_log} to extract fee` | Fee-extraction failure when reading raw exchange response (`_parse_recon_src_log`-style path) |
| INFO | `Request to start recon for task_id={task_id} part_id={part_id} direction={direction} recon_id={recon_id} succeeded with response {resp}.` | Recon-start success — emitted by `start_or_check_source_recon` (`direction=withdraw`) + two dest variants in `start_or_check_dest_recon` (`direction=deposit`) |
| ERROR | Same prefix + `failed with status code {status} and response {resp}. Request will not be retried.` | Recon-start terminal failure (typically 400) |
| ERROR | Same prefix + `failed with status code {status} and response {resp}. Request will be retried.` | Recon-start retryable failure |

`succeeded` / `not be retried` / `be retried` triplet repeats three times in file (source-side, settlement-style destination, standard destination). `direction=withdraw` ⇒ source recon; `direction=deposit` ⇒ destination recon.

Real prod line (verbatim) for shape reference:

```
Request to start recon for task_id=261498 part_id=1 direction=deposit recon_id=237223966514380682460847610448910946406 succeeded with response {'recon_id': '237223966514380682460847610448910946406', 'status': None, ... 'internal_id': None, 'tx_id': None, 'amount': None, 'params': None}.
```

Identifier substring shapes:

- `task_id={int}` — e.g. `task_id=261498`.
- `part_id={int}` — e.g. `part_id=1`.
- `direction=withdraw` / `direction=deposit` — splits source vs destination recon.
- `recon_id={int}` — **stringified integer UUID5**, generated by `_generate_recon_id` as `str(uuid.uuid5(self.RECON_NAMESPACE, f"{task_id}-{part_id}-{direction}").int)`. Long decimal string (~39 digits), **not** hex `abc-…` UUID. Namespace config-driven (`config.RECON_NAMESPACE`).
- `response {dict-repr}` — full Recon-service response inlined, including own `recon_id`, `internal_id`, `tx_id` keys.

**No `tx_id=` or `internal_id=` printed by long-running ctrl directly** — appear only inside embedded `response {…}` dict. `start_transfer_part` itself does NOT log; transfer failures fall through silently to `set_part_failed` DB write (see Section 3.7).

### 3.4 API ctrl — `transfer_api_ctrl.py`

Entrypoint `settlement-engine-api`. `TransferApiCtrl` subclasses `TransferCtrl`; all sites use `self.logger` (default `fastapi.logger.logger`).

| Level | Message format | Trigger |
|:---|:---|:---|
| INFO | `Creating QMS transfers {request}` | QMS bulk-transfer entry |
| EXCEPTION | `Failed to get balance for account {source_account_product_id}` | Balance fetch error during QMS bulk |
| WARNING | `Insufficient balance for {asset} in account {source_account_product_id}: available=…, required=…. Publishing to NATS for retry.` | QMS task hits balance shortfall |
| EXCEPTION | `Failed to create QMS transfer {task} tag {task.settlement_id}` | Per-task create failure |
| EXCEPTION | `Failed to publish insufficient balance tasks {…} to NATS` | NATS publish failure |
| INFO | `Will send QMS bulk transfer summary in {delay_seconds} seconds` | Pre-summary delay log |
| EXCEPTION | `Failed to send QMS bulk transfer summary` | Summary send failure |
| INFO | `Finished processing QMS bulk transfer request` | QMS bulk complete |
| INFO | `Publishing insufficient balance tasks` | NATS publish start |
| INFO | `Published insufficient balance task {task} to NATS subject {subject}, ack: {pub_ack}` | NATS per-task ack |
| EXCEPTION | `Failed to publish insufficient balance task {task} to NATS subject {subject}` | NATS publish error |
| ERROR | `Unexpected task {task}` | Bulk-transfer router can't classify task |
| INFO | `Creating internal transfer between {task.source_account} and {task.destination_account}` | Single internal-transfer create |
| EXCEPTION | `Failed to create internal transfer between {…} and {…}` | Internal create failure |
| INFO | `Creating external transfer between {task.source_account} and {task.destination_address}` | Single external-transfer create |
| EXCEPTION | `Failed to create external transfer between {…} and {…}` | External create failure |
| INFO | `cancelling transfer task part {part_id} of the transfer task {task_id}` | Force-cancel a task part |
| WARNING | `Cannot find account with account_product_id={account_product_id}` | Account-lookup miss |
| WARNING | `[BALANCE] Missing nitro or exchange for {src_info}` | Account record missing fields |
| WARNING | `[BALANCE] No info for account_id={account_product_id}` | Async balance-lookup miss |

Identifier substring shapes:

- `task_id={int}` — recon-init lines use this clean form.
- `cancelling transfer task part {part_id} of the transfer task {task_id}` — English prose, word "task" appears literally, **no `task_id=`**. Use `transfer task {n}` as fallback substring when `task_id=` returns nothing.
- `account_product_id={int}` — balance/account-lookup paths; also appears bare as `account {source_account_product_id}` in QMS paths and as `account_id={…}` inside `[BALANCE] No info for account_id=…` and inside `src_info` Pydantic-repr dicts.
- `[BALANCE]` — bracketed tag; filters balance-related warnings.
- `tag {task.settlement_id}` — QMS settlement-id substring. Equal to `task_id` only when `task.settlement_id == task_id`; not general substitute.

### 3.5 Settlement v2 ctrl — `settlement_v2_ctrl.py`

`settlement-engine-api`. `self.logger` (default `fastapi.logger.logger`), plus three `api_utils.log` debug lines.

| Level | Message format | Trigger |
|:---|:---|:---|
| ERROR | `Failed to match settlement id={a} with id={b} due to different assets. {asset_a} != {asset_b}` | Deprecated `match()` — asset mismatch |
| ERROR | `Failed to match settlement id=… with id=… due to different counterparty_ref. {a} != {b}` | Deprecated `match()` — counterparty mismatch |
| INFO | `Matching id={a} against id={b} for {amount}` | `match()` / `get_match()` start |
| DEBUG | `Outstanding after matching (self): id=… outstanding=…` (and `(other)` variant) | Post-match outstanding |
| DEBUG | `Settle Preview: {filled_map}` | Pre-settle preview |
| ERROR | `Exception occurred for settlement match for counterparty_ref = {counterparty_ref} asset = {asset} settlement_ids={settlement_ids} \| {e}` | Settlement-match failure (note `key = value` with spaces, and `\|` before exception) |
| INFO | `Unsettling Deal for settlement id={settlement_id}` | Manual unsettle |
| ERROR | `Exception occured when unsettling deal \| {e}` *(sic — "occured")* | Unsettle failure |
| INFO | `Settling Deal for settlement id={settlement_id}` | Manual settle (single) |
| INFO | `Settling Deal for settlement ids={settlement_ids}` | Manual settle (bulk) |
| ERROR | `Exception occured when settling deal \| {e}` *(sic — "occured")* | Settle failure (single + bulk) |
| ERROR | `Exception (redis.lock) occurred for Clear Residual for counterparty_ref = {…} asset = {…} \| {e}` | Clear-residual redis-lock failure |
| ERROR | `Exception (general) occurred for Clear Residual for counterparty_ref = {…} asset = {…} \| {e}` | Clear-residual general failure |
| DEBUG | `ak_test\|get_aging_settlement\|settlements: {…}` / `…\|deal_settlements: …` / `…\|deal_date_map: …` | Aging-settlement debug (only place `<<--…-->>` structured format appears) |

> **Spelling trap:** settlement-**match** exception spells `occurred` correctly (`Exception occurred for settlement match …`). Only unsettle/settle-deal exceptions carry `occured` typo. Filter expecting `occured` on match path will miss it; use `Exception occurred for settlement match` there.

Identifier substring shapes:

- `settlement id={int}` — single-settlement form (mind space: `id=`, not `settlement_id=`).
- `settlement_ids={[list]}` — bulk form (underscore + plural).
- `counterparty_ref = {string}` and `asset = {symbol}` — **spaces around `=` load-bearing**.
- `ak_test|` — pipe-separated namespace marker, only easily-greppable handle for `api_utils.log` debug lines.

### 3.6 Settlements v2 API — `settlements_v2_api.py`

`settlement-engine-api`. Six call sites — two via `getLogger('settlement_v2_api')`, four via `fastapi.logger.logger`. Both formats bare.

| Level | Message format |
|:---|:---|
| ERROR | `Invalid Counterparty Ref {list}` |
| ERROR | `Invalid POrtfolio Numbers {list}` *(sic — "POrtfolio")* |
| ERROR | `Failed to get exchange txns: {e}` |
| ERROR | `Failed to get blockchain txns: {e}` |

### 3.7 External clients — log-silent

`altonomy/settlement_engine/external/{exchanges,optimus_client,account,nitro_client,txn_client,external_util,xalpha_ctrl,optimus_ctrl,s3_ctrl,comply_adv_client}.py` have **no log calls in scope**. Consume errors via `try/except`, return tuples; HTTP-level errors surface only through response payloads embedded in upstream `logger.error(...)` lines. Suspect external HTTP issue → look for `response {…}` substring inside `Request to start recon for …` lines — embedded dict carries failing payload.

---

## 4. transfer-engine

### 4.1 Logger setup + sink split

Service uses **loguru** with three sinks (configured in `logger.py`). No JSON formatter; identifiers live in message body.

1. `sys.stderr` — records where `extra.recon_id` **NOT** set (FastAPI app, endpoints, exchange-lib calls outside listener thread, unbound on-chain helpers). Filter: `lambda r: not r["extra"].get("recon_id")`.
2. `~/logs/txengine/altonomy.log` — same no-`recon_id` filter. DEBUG, 500 MB rotation, 7-day retention, lzma-compressed.
3. `~/logs/txengine/recon_listeners.log` — only records where `extra.recon_id` **IS** set (per-recon listener threads). Filter: `lambda r: r["extra"].get("recon_id")`. Same DEBUG/rotation/retention.

Loki ship-split:

- `{job="transfer-engine-logs"}` — stderr + `altonomy.log` (app, endpoints, exchange lib, unbound on-chain helpers).
- `{job="transfer-engine-recon-logs"}` — `recon_listeners.log` (per-recon listener threads only).

File-sink render format (verbatim):

```
{extra} <green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>
```

Loguru's `{extra}` renders as leading dict literal. **Only structured field that appears in line** — and the recon-flow cross-reference handle. Real recon-sink line:

```
{'recon_id': '237223966514380682460847610448910946406'} 2026-06-15 13:19:46.482 | INFO     | altonomy.txengine.listeners.base_recon_listener:run:73 - breaking!!!
```

Records with no bound `recon_id` render prefix as empty dict `{}`.

`recon_listener_logger(recon_id)` helper (in `logger.py`) returns `logger.bind(recon_id=recon_id)`. `BaseReconListener.__init__` sets `self.logger = recon_listener_logger(recon_id)`, so every listener subclass inherits logger bound this way → output lands in recon sink.

**Exchange-library logs acquire `recon_id` binding when called from listener.** Concrete exchange-based listeners (`InternalReconListener`, `WithdrawReconListener`, `DepositReconListener`) overwrite `self.exchange.logger = self.logger` (bound one) in `__init__`, so exchange-vendor HTTP traffic lands in recon sink for that listener's lifecycle. Key triage hook: filtering on `recon_id` surfaces actual exchange HTTP traffic too. Outside listener (e.g. FastAPI endpoint, where `exchange_manager.get(...)` injects unbound module-level loguru `logger`), exchange-lib logs land unbound in app sink.

### 4.2 Endpoints — `endpoints/transfer.py` + `endpoints/tx_recon.py`

Module-level loguru `logger`. Endpoint failures fire before any listener spawned → land in `{job="transfer-engine-logs"}` unbound.

| Level | Message format | Endpoint / when |
|:---|:---|:---|
| EXCEPTION | `Exception during transfer: bad altonomy-exchanges code` | `POST /transfer/internal_transfer` failure (`start_internal_transfer`) |
| EXCEPTION | `Exception during withdrawal: bad altonomy-exchanges code` | `POST /transfer/withdraw_funds` failure (`start_withdraw`) |
| EXCEPTION | `Exception during get_account_balance: bad altonomy-exchanges code` | `GET /transfer/account_balance` (`get_account_balance`) |
| EXCEPTION | `Exception during get_deposit_addresses: bad altonomy-exchanges code` | `GET /transfer/deposit_addresses` (`get_deposit_addresses`) |
| EXCEPTION | `Exception during get_deposit_addresses: bad altonomy-exchanges code` *(sic — message says deposit_addresses but emitter is transfer_history handler)* | `GET /transfer/transfer_history` failure (`get_transfer_history`) |
| EXCEPTION | `Exception getting withdrawal fees: {e}` | `GET /transfer/withdrawal_fees` (`get_withdrawal_fee`) |
| EXCEPTION | `Exception getting account_uid for {exchange_name} {account_id}` | `GET /transfer/account_uid` failure (`get_account_uid`) |
| DEBUG | `{result}` / `{bal}` / `{addrs}` / `{hist}` (bare dicts from exchange lib) | Endpoint success paths |
| DEBUG | `{status}` (bare `TxRecon` model with `recon_id`, `status`, …) | `GET /txrecon/status` / `POST /txrecon/cancel` |

Identifier shapes:

- `account_uid for {exchange_name} {account_id}` — bare space-separated, no `=` sign. Only place `account_id` shows in transfer-engine.
- `/txrecon/status` and `/txrecon/cancel` do **not** log incoming `recon_id` themselves — listener thread does, via `extra` binding.
- Success DEBUG lines (`{result}`, `{addrs}`, …) bare dicts: useful for post-mortem reconstruction, near-useless for direct grep.

### 4.3 Recon-listener base — `listeners/base_recon_listener.py`

`BaseReconListener`. All listener-thread logs bound with `extra.recon_id` → land in `{job="transfer-engine-recon-logs"}`.

| Level | Message format | When (method) |
|:---|:---|:---|
| INFO | `Thread for transaction listener started` | Thread spawn (`run`) |
| ERROR | `{traceback}` | Exception caught in `run` |
| INFO | `breaking!!!` | Hit terminal status (fail/canceled/success) (`run`) |
| INFO | `{self.status}` — dict-repr (`{'recon_id': …, 'status': …, 'error': …, 'error_msg': …, 'timestamp': …, 'tx_id': …, 'amount': …, 'data': …}`) | Status snapshot at break (`run`) |
| INFO | `{self.status["status"]}` (just enum string) | Status value at break (`run`) |
| INFO | `ending!!!` | End-of-window timeout (`run`) |
| INFO | `ending transaction listener, transaction not found between {start} and {end}` | Window-expired explanation (`run`) |
| INFO | `ending transaction listener (canceled)` | Cancel-driven exit (`cancel`) |
| ERROR | `too many consecutive errors ({n}), marking as failed` | Repeated source errors trip failure threshold (`run`); precedes `fail` status with `error="OTHER"` |
| INFO | `consecutive errors: {n}` | Per-error counter bump (`run`) |
| DEBUG | `updated tx recon status to {self.status}` | Every status mutation, high-volume (`update_status`) |

Bound `extra.recon_id` appears as leading `{'recon_id': '<int>'}` literal on every one of these lines.

> `error` / `error_msg` carried by recon outcome **not their own log lines** — keys inside `self.status` dict. Reach Loki via INFO `breaking!!!` + `{self.status}` snapshot (reliable) and DEBUG `updated tx recon status to {self.status}` line (only if DEBUG shipped). To find specific failure reason, grep for `error_msg` substring; surfaces inside INFO status snapshot.

### 4.4 Concrete listeners — `listeners/{internal,withdraw,deposit,on_chain}_recon_listener.py`

Three exchange-based listeners (`InternalReconListener`, `WithdrawReconListener`, `DepositReconListener`) share same shape — call `self.exchange.get_account_transactions(...)`, log raw response, walk it for matches.

| Level | Message format | Listener (method) |
|:---|:---|:---|
| DEBUG | `received txs: {txs}` | Internal / Withdraw / Deposit (`check_tx_history_rest`) |
| WARNING | `external_id provided by {exchange_class_name} is not in str format, fix at exchange level (converting)` | Internal — type-coercion (`check_tx_history_rest`) |

On-chain recon (`OnChainReconListener.check_tx_history_rest`) **structural**, not field-by-field: each source's `find_tx` returns list of `(amount, to_address)` transfers; listener filters to transfers matching destination address, then branches on count. Amount recorded (for downstream settlement), not reconciled; per-tx timestamp no longer checked (only global window in base class). Outcome strings = `error`/`error_msg` fields on status dict (see note in Section 4.3), not standalone log lines:

| count | `error` field | `error_msg` field |
|:---|:---|:---|
| 0 (no transfer to dest) | `TRANSFER TO DEST NOT FOUND` | `Transaction with hash {tx_id} has no transfer to {address}. Something went wrong check the transaction !!!` |
| >1 (ambiguous) | `TRANSFER NEEDS TO BE CHECKED MANUALLY` | `Please check using an {chain} explorer the transaction with hash {tx_id} contains a transfer to {address} with the expected amount.\nIf so, approve this step manually.` |

When *every* source errored, `OnChainReconListener` raises `Exception(json.dumps(self.source_responses))`; `run` catches it, logs traceback at ERROR, sets `error="OTHER"`, `error_msg=<traceback>`. Total source-failure surfaces as ERROR traceback line containing JSON dump of every source's raw HTTP error body + status code.

High-precision on-chain substrings: `has no transfer to`, `TRANSFER TO DEST NOT FOUND`, `TRANSFER NEEDS TO BE CHECKED MANUALLY`, `transaction not found between` (window expiry).

Identifier substrings in listener output:

- `recon_id` — via `{'recon_id': '<int>'}` loguru `extra` prefix.
- `tx_id` / `amount` — inside `received txs: [...]` raw exchange responses and inside `self.status` snapshots. (DB column = `txn_id`; body literal may be `tx_id` — match on value, not key.)
- `external_id` / `transaction_ref` — exchange-side normalised TX identifiers, inside `received txs: [...]`.
- `external_id provided by {ExchangeClass}` — type-coercion warning phrase.

### 4.5 On-chain sources — `recon_sources/`

Two binding patterns coexist:

- **Bound (preferred).** `OnChainSource.__init__` accepts `logger=`, falling back to module-level loguru; `OnChainReconListener` passes `self.logger` (recon-bound one) at construction. Most sources carry binding — but in practice only `self.logger` call in whole source layer = shared base line in `OnChainSource._resp_data`; individual chain subclasses rarely log directly.
- **Unbound (anti-pattern).** `berascan.py` and `blockchair.py` directly `from loguru import logger` and log via module-level (unbound) logger. Lines lose `recon_id` binding, land in `{job="transfer-engine-logs"}`, **not** recon sink. To trace recon involving these chains, search **both** job labels for `tx_id`/`txn_id` value (or chain-name marker, e.g. `berascan -`). *(No `Taostats` source in current codebase.)*

Cheapest line to grep for "what did chain X return for tx Y" — emitted by `OnChainSource._resp_data` (base, DEBUG) for all sources, carrying URL kwargs (incl. `tx_id`/address) and raw response body:

```
recv -- {resp.text} request params -- {kwargs}
```

`_resp_data` also records non-OK HTTP responses into `self.source_responses[source]` as `error=<status code>` (e.g. `"429"`, `"500"`) + `error_msg=<raw body>`.

Per-source phrases that still exist (high-value):

| Level | Message format | Source |
|:---|:---|:---|
| DEBUG | `berascan - tx {tx_id} found but confirmations below threshold confirmations={n}` | Berascan (`_find_bera_tx`) — module-level loguru, unbound |
| WARNING | `Blockchair API key is not set. Requests may be rate limited. Set the BLOCKCHAIR_API_KEY config value to increase rate limits.` | Blockchair (`_get_with_key`) — module-level loguru, unbound |

> Older on-chain phrases — `tx from addr … do not match recon address`, `… not matching recon amount … with fee threshold`, `… has timestamp … not between …`, `Failed to parse the response`, `Failed to parse TON transaction response:`, Theta's `Transaction … not confirmed yet`, and Berascan `conformations` / `failed to get timeStamp` / `failed to get conformations or block number` typo lines — **no longer exist**. Removed when on-chain recon moved to structural count-based model. Don't use as filters.

### 4.6 Exchange manager + HTTP retry

`exchange_manager.py` (module-level loguru):

| Level | Message format | Method |
|:---|:---|:---|
| EXCEPTION | `failed to get account {account_id} secret from vault` | `get` |
| EXCEPTION | `cannot initiate exchange {exchange_name}` | `get` |

`exchange_manager.get(...)` constructs `exchanges.Exchange(..., logger=logger, ...)` with **unbound** module-level loguru logger; listeners later overwrite `self.exchange.logger` with bound one (Section 4.1).

Bracketed-retry-count HTTP wrapper (`utils.request_with_retry` with `sending [{rc}] {method} {url}` / `received [{rc}] …` / `exception on failure [{rc}] …`) described in older notes **no longer exists** in transfer-engine. Live request/response logging = exchanges library's `request_with_retry` (Section 5) — `sending {method} {url} {kwargs}` etc., no bracketed retry count.

---

## 5. sg-altonomy-exchanges — in-process logging from transfer-engine

Library loaded into transfer-engine process. Does **not** ship to Loki on its own; output flows through whatever `logger` injected at `exchanges.Exchange(...)` construction — loguru module logger by default, or `recon_id`-bound child when construction happened inside recon listener. Default fallback when no `logger=` passed (`altonomy.core.logger` → queue-backed `altonomy.loggers.logger.Logger`) never engages from transfer-engine; don't chase its format in Loki.

Scope: `CoinMarket` (base) + `Binance` (concrete). Other adapters follow same shapes.

### 5.1 CoinMarket base — `CoinMarket.py`

All `self.logger` (loguru when called from transfer-engine).

| Level | Message format | Method |
|:---|:---|:---|
| DEBUG | `retrieved '{path}{exchange_name}{account_id}' from redis` | `RedisFallback` decorator |
| ERROR | `HTTP 4XX/5XX error detected - {self.last_error_message}` | `_retry_request` |
| ERROR | `cannot get external ip address` + traceback | `broker_external_ip` |
| ERROR | `{errstr}, exchange={exchange_name}, data={kwargs.get('data')}, params={kwargs.get('params')}` | `_handle_exchange_response` |
| ERROR | `Failed to get client tradablecoins due to {e}` | `get_tradablecoins` |
| ERROR | `Unexpected response format: {resp}` + traceback | `_handle_unexpected_market_history` (around `send_order`) |
| ERROR | `error in send order: {error}` | `_format_send_order` (called by send-order paths) |
| DEBUG | `sending {method} {url} {kwargs}` | `API.request_with_retry` |
| DEBUG | `response from {method} {url} {kwargs} -- {response.text}` | `API.request_with_retry` |
| DEBUG | `retrying` | `API.request_with_retry` |
| ERROR | `{e} occured while {method} {url} {str(kwargs)}` + traceback *(sic — "occured")* | `API.request_with_retry` |
| DEBUG | `remote signing @ {sign_url} with {json.dumps(sign_data)}` | `API._remote_sign` (only when `remote_sign=True`) |
| DEBUG | `received {resp_json} from signature engine` | `API._remote_sign` |
| ERROR | `remote sign failed` + traceback | `API._remote_sign` |

Identifier shapes:

- `sending {METHOD} {URL} {KWARGS}` — most useful grep for vendor traffic; `{KWARGS}` includes path-and-query params, signed headers, body.
- `response from {METHOD} {URL} {KWARGS} -- {response.text}` — response side (note ` -- ` separator).
- `HTTP 4XX/5XX error detected - {errstr}` — every 4xx/5xx from any vendor (exact casing `4XX/5XX`).
- `{errstr}, exchange={NAME}, data={…}, params={…}` — exchange-specific error from `_handle_exchange_response`.
- `error in send order: {error}` — order-rejection summary.

**No `account_id=` prefix** in library messages. Only reliable cross-ref between settlement-engine and exchange traffic = `recon_id` (present only when call originated inside recon listener). Settlement-engine does not print `account_id=` on request path either — account ids live inside HTTP request body, not log line.

### 5.2 Binance concrete — `Binance.py`

Binance overrides `_request` directly (does NOT use `CoinMarket.API.request_with_retry`), so request/response logs live on parallel codepath. `exchange_name()` returns uppercase `BINANCE`.

| Level | Message format | Method |
|:---|:---|:---|
| DEBUG | `sending {method} {url} {kwargs}` | `_request` |
| DEBUG | `received {method} {url} {kwargs} {response.text}` (skipped when URL ends with `/exchangeInfo` — avoids spam) | `_request` |
| INFO | `error %s occur while %s %s %s` *(sic — "occur"; `%s`-formatted; **INFO, not ERROR**)* | `_request` |
| DEBUG | `{url}, {method}, {kwargs}` | `_request` retry line |
| INFO | `error %s occur while retry %s %s %s` | `_request` retry path |
| ERROR | `{traceback}` | around `send_order` parse |
| DEBUG | `The account of BINANCE is locked!` (string-concat) | `get_account_balance` |
| ERROR | `failed_to_fetch_spot_balances|{traceback}` | `get_account_balance` |
| ERROR | `order not sent, {self.last_error_message}` | `send_order` |
| DEBUG | `Cannot get the orderbook of trading pair {pair}!` (string-concat) | `get_all_orderbook` |
| DEBUG | `failed to retrieve candle data for {pair}` | `get_standard_candles` |
| ERROR | `error in new_future_account_transfer : {e}` / `error in get_future_account_transactions : {e}` / `error in transfer_between_spot_margin : {e}` (note space before colon) | `transfer_funds` |
| ERROR | `Failed to fetch Binance withdrawal fees: {e}` | `get_withdrawal_fees` |
| WARNING | `Failed to fetch earn balances page {n}, returning partial data` | `get_earn_balances` |
| DEBUG | `Binance Earn balances: {result}` | `get_earn_balances` |

Binance-specific identifier substrings:

- URL fragments — `/api/v3/`, `/sapi/v1/`. Grep URL path to identify which endpoint hit. **Withdrawals use `/sapi/v1/capital/withdraw/apply`; withdrawal history uses `/sapi/v1/capital/withdraw/history`.** (Older `/wapi/v3/withdraw.html` path no longer in use — `/wapi/` survives only as guard inside `_request`.)
- `error %s occur while …` — Python `%`-formatting leaves `%s` template literals in some search tools; match on `error` + `occur while`. **Logged at INFO** — see Section 6.
- `failed_to_fetch_spot_balances|` — pipe-separated tag, unique grep.
- `exchange=BINANCE` (uppercase) — appears in `_handle_exchange_response` errors.

---

## 6. System facts that make naive greps miss

Properties of system, not to-do list. Investigator should keep in mind whenever "should be there" line fails to show up.

- **No structured logging anywhere.** Both services emit plain text; identifiers embedded in message body (`key=value`, `key={value}` Pydantic/loguru repr, or bare English prose). Loki label extraction does not apply — use `|=` / `|~` substring matching.
- **Two identifier conventions in settlement-engine.** Some lines use `task_id={n} part_id={n} recon_id={n}` (clean `key=value`); others use `transfer task part {n} of the transfer task {n}` (English prose, no key). When `task_id=` returns nothing, try `transfer task {n}`.
- **Stable typos load-bearing — match exactly, don't autocorrect.** Confirmed present verbatim: `End Lopp` (settlement-engine loop end), `occured` (settlement-engine unsettle/settle-deal exceptions, and exchanges `request_with_retry`), `POrtfolio` (`Invalid POrtfolio Numbers`). Note `Exception occurred for settlement match …` spells `occurred` correctly. Berascan `conformations` typo **fixed** to `confirmations` — don't filter on `conformations` any more.
- **`long_running` logger not bare in prod.** Despite bare `StreamHandler` in code, `getLogger('long_running')` lines ship with `<<--…-->> LEVEL [process_transfer.py:loop:NN] <msg>` structured prefix (often duplicated with bare copy). Filter on message body, never prefix.
- **Recon sink job label has trailing `-logs`.** Recon-listener output = `{job="transfer-engine-recon-logs"}`; app sink = `{job="transfer-engine-logs"}`. Querying `transfer-engine-recon` (no `-logs`) returns nothing.
- **Two on-chain sources lose `recon_id` binding.** `berascan.py` and `blockchair.py` use module-level (unbound) loguru logger, so lines land in `{job="transfer-engine-logs"}`, not recon sink. For recon involving these chains, search both job labels for `tx_id`/`txn_id` value.
- **`OnChainReconListener` has swallowed-argument log call.** Calls `self.logger.error(s.name, self.source_responses.get(s.name))` — two positional args to loguru `.error()`. First becomes message; second (per-source response dict) silently dropped. To see response, find preceding `recv -- {resp.text} request params -- {kwargs}` line from `OnChainSource._resp_data`.
- **Binance logs vendor HTTP errors at INFO, not ERROR.** `error %s occur while …` emitted via `.info(...)`. Loki alert on "level=ERROR exchange error" will miss most Binance failures — filter on message text, not level. Several other useful lines (`The account of BINANCE is locked!`, `Cannot get the orderbook …`, `failed to retrieve candle data …`, `Binance Earn balances: …`) DEBUG and only present if DEBUG shipped.
- **No `account_id` on request path.** Plain `account_id=` text rare. In settlement-engine use `account_product_id={n}` or embedded `src_info` Pydantic-repr dict; in transfer-engine only `account_id` occurrence bare in `account_uid for {exchange_name} {account_id}` (no `=`). Deterministic cross-service anchor = `recon_id`.
- **External clients in settlement-engine log-silent** (Section 3.7). HTTP failures surface only through `response {…}` dict embedded in `Request to start recon for …` lines.
- **`api_utils.log` separate formatter.** Three `ak_test|…` debug lines use `<<--YYYY-MM-DD HH:mm:ss.mmm-->> LEVEL [file:func:line] message` format. Useful only because `ak_test|` prefix unique — don't generalise format to other lines.
- **Recon outcome reasons = dict fields, not log lines.** Transfer-engine recon's `error` / `error_msg` reach Loki only inside `{self.status}` snapshot (INFO, after `breaking!!!`) and high-volume `updated tx recon status to {…}` DEBUG line. Grep for reason substring (e.g. `has no transfer to`), not standalone reason line.

---

## 7. Query templates and time windows

Wrap each template in time window. Default windows: `1h` for live triage, `24h` for investigations; transfer-engine file sinks retain 7 days on-disk.

> **All times are UTC.** Log ingest timestamps, the timestamp inside the line body, and the Loki MCP query bounds (`startRfc3339`/`endRfc3339`) are all UTC — pass bounds as RFC3339 with a `Z` suffix. API epoch fields are absolute; format as UTC before windowing. The human Grafana UI renders in browser-local time, so a hand-typed window can be off by the local offset (the "0 results" trap). Full conversion recipe + the altex-DB driver-shift gotcha: `timezones.md`.

### task_id (settlement-engine)

```logql
# All log lines for a transfer task (both pods)
{pod=~".*(settlement-engine-api|settlement-engine-long).*", env="prod"} |= "task_id=12345"

# Long-running loop view (phase errors + recon-start lifecycle)
{pod=~".*settlement-engine-long.*", env="prod"} |= "task_id=12345"

# Recon-start lifecycle only
{pod=~".*settlement-engine-long.*", env="prod"} |= "task_id=12345" |= "Request to start recon"
```

Alternate substrings if `task_id=` returns nothing:

```logql
# English-prose form (cancellation, etc.)
{pod=~".*settlement-engine-api.*", env="prod"} |= "transfer task 12345"

# QMS path — only when settlement_id == task_id
{pod=~".*settlement-engine-api.*", env="prod"} |= "tag 12345"
```

### part_id (settlement-engine)

```logql
{pod=~".*settlement-engine-long.*", env="prod"} |= "task_id=12345" |= "part_id=1"
```

### recon_id (both services — the highest-value cross-service identifier)

```logql
# Settlement-engine: recon-start request + outcome
{pod=~".*settlement-engine-long.*", env="prod"} |= "recon_id=237223966514380682460847610448910946406"

# Transfer-engine: recon-listener thread (bound via loguru extra)
{server="w04.se1.altono.app", job="transfer-engine-recon-logs"} |= "'recon_id': '237223966514380682460847610448910946406'"

# Transfer-engine incl. unbound on-chain helpers (berascan/blockchair lose the binding → fall back to tx_id)
{server="w04.se1.altono.app"} |= "237223966514380682460847610448910946406"
```

`recon_id` = stringified integer form of `uuid.uuid5(NAMESPACE, "{task_id}-{part_id}-{direction}")` — long decimal string, not hex.

### tx_id / txn_id (transfer-engine, on-chain)

```logql
# All recon-listener output mentioning the hash
{server="w04.se1.altono.app", job="transfer-engine-recon-logs"} |= "0xabc..."

# Including unbound on-chain source logs (berascan/blockchair)
{server="w04.se1.altono.app"} |= "0xabc..."

# On-chain failure reasons (dest-address miss / ambiguous)
{server="w04.se1.altono.app", job="transfer-engine-recon-logs"}
  |= "0xabc..." |~ "has no transfer to|TRANSFER NEEDS TO BE CHECKED MANUALLY"
```

### internal_id / external_id (transfer-engine, exchange-side)

```logql
# Match candidates inside the recon listener
{server="w04.se1.altono.app", job="transfer-engine-recon-logs"}
  |= "'recon_id': '237223...'" |= "external_id"

# Type-coercion warning (string-vs-int from a vendor)
{server="w04.se1.altono.app", job="transfer-engine-recon-logs"} |= "external_id provided by"
```

### account_product_id (settlement-engine)

```logql
# Balance-related warnings on a specific account
{pod=~".*settlement-engine-api.*", env="prod"} |= "[BALANCE]" |= "account_id=42"

# Account-lookup miss
{pod=~".*settlement-engine-api.*", env="prod"} |= "Cannot find account with account_product_id=42"
```

### Exchange endpoint URL (transfer-engine + exchanges lib)

```logql
# All Binance sapi/v1 traffic
{server="w04.se1.altono.app", job="transfer-engine-logs"} |= "/sapi/v1/"

# Failed Binance withdrawals (INFO, not ERROR — see Section 6)
{server="w04.se1.altono.app", job="transfer-engine-logs"}
  |= "error" |= "occur while" |= "/sapi/v1/capital/withdraw"

# Any vendor 4xx/5xx
{server="w04.se1.altono.app", job="transfer-engine-logs"} |= "HTTP 4XX/5XX error detected"

# Exchange-specific error envelope
{server="w04.se1.altono.app", job="transfer-engine-logs"} |= "exchange=BINANCE" |= "data=" |= "params="
```

### Lifecycle phrases (high-precision substrings)

```logql
# Long-running loop heartbeat (10s cadence)
{pod=~".*settlement-engine-long.*", env="prod"} |= "Begin Loop"

# End of each loop iteration (typo, exact match)
{pod=~".*settlement-engine-long.*", env="prod"} |= "End Lopp"

# QMS bulk transfer
{pod=~".*settlement-engine-api.*", env="prod"} |= "Creating QMS transfers"

# Settlement-match exceptions (note: "occurred", correctly spelled)
{pod=~".*settlement-engine-api.*", env="prod"} |= "Exception occurred for settlement match"

# Recon-listener thread spawn / terminal break / window expiry
{server="w04.se1.altono.app", job="transfer-engine-recon-logs"} |= "Thread for transaction listener started"
{server="w04.se1.altono.app", job="transfer-engine-recon-logs"} |= "breaking!!!"
{server="w04.se1.altono.app", job="transfer-engine-recon-logs"} |= "ending transaction listener"
```

### Counterparty-scoped triage

```logql
{pod=~".*settlement-engine-api.*", env="prod"} |= "counterparty_ref = CPTY001"
```

Spaces around `=` load-bearing in `settlement_v2_ctrl.py`.

### Time-window patterns

- **Active incident**: 5–15 min window aligned on user-reported timestamp. Long-running loop heartbeat = 10s, so 5 min ≈ 30 phase-marker iterations.
- **Post-mortem for one task**: query by `task_id` over 1–7 days. Settlement-engine retention = Loki tenant default; transfer-engine recon files retain 7 days on-disk.
- **Cross-service flow**: start with `recon_id` (settlement-engine `Request to start recon …` line gives both integer-UUID and timestamp), then pivot to transfer-engine with same `recon_id`. Allow ±5 min around settlement-engine log time for clock skew + listener-start latency.
- **Recon-phase failure window** (`source_recon` / `dest_recon`): anchor the window on the failed part's own timestamps, **not** on `now`. Open at `transfer_time − 5 min` (the leg executes, then the recon listener spawns just after — first line `Thread for transaction listener started`) and close at `start_time + 5 min`. The part's **`start_time` is the version-write instant of the failed row ≈ when recon *resolved*** (the terminal status persisted), hours after the listener's first lines — so `start_time ≥ transfer_time` always, and the `transfer_time → start_time` bracket spans the full recon-listener lifetime. **Do not open the window at `start_time`**: it lands near the *end* of the recon thread, so the early lines fall outside and the query returns empty (the trap that bit the 260914 triage — a `start_time`-anchored window opened ~2 h after the recon began). A listener's own max lifetime is `config.LISTENER_DEFAULT_MAX_DURATION` (1 h, transfer-engine `config.py`), but settlement-engine re-arms recon across `process_transfer` loop iterations, so one recon's lines can span several listener lifetimes — the `start_time`-bounded close covers the whole respawn chain without an arbitrary fixed offset.
- **Vendor-outage detection**: `{server="w04.se1.altono.app", job="transfer-engine-logs"} |= "HTTP 4XX/5XX error detected"` over 1h, grouped by `exchange={NAME}` substring.