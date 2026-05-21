# Altex — Product Summary

This document describes the Altex product across the four repositories wrapped by this directory:

- `sg-altonomy-exchanges` — exchange/protocol abstraction library
- `sg-altonomy-transfer-engine` — fund-movement and reconciliation service
- `sg-altonomy-settlement-engine` — orchestration, persistence, and business workflow
- `institutional-settlement-engine-frontend` — operator UI

It covers the business context, the system architecture, the end-to-end fund-movement workflow, and the role of each repository within it.

---

## 1. Business Context

Altex is the post-trade fund-movement and settlement platform that sits behind Altonomy's institutional OTC trading desk. Its purpose is to operationalise everything that happens *after* a trade is agreed — i.e. once a deal exists between the desk and an external counterparty, Altex is responsible for actually moving the assets so that the books reconcile and the counterparty receives what it is owed (and vice versa).

In practice, two distinct categories of fund movement are handled:

- **Internal rebalancing.** Traders and Middle Office (MO) constantly need to move assets between the desk's own accounts — between sub-accounts and main accounts on the same exchange, between exchanges, between spot/margin/futures wallets, and between exchanges and bank accounts. These moves keep collateral, liquidity, and trading capacity where they need to be. They are operationally critical but invisible to the counterparty.

- **External OTC settlement.** When a deal is booked against an external OTC client, Altex executes the on-chain transfer (or bank wire) required to deliver the asset to the client's wallet/account, and recognises the corresponding incoming leg from the counterparty. Each deal can settle in multiple legs (base, quote, fee, premium, margin), and a single settlement may require chaining several internal moves before the final external withdrawal can be initiated.

The users of Altex are **internal Altonomy staff** — traders, MO, settlement ops, compliance — not external counterparties. The frontend exists for them; the counterparty only ever sees the resulting on-chain transaction or bank credit. This shapes everything about the system: it optimises for operator visibility, manual override, audit trails, and recoverability rather than for end-customer self-service.

Because every fund movement touches real assets on real venues, Altex's core responsibilities are:

1. **Routing** — given a source, a destination, an asset and an amount, compute a path of one or more legs (e.g. *Binance sub → Binance main → on-chain withdrawal → counterparty wallet*).
2. **Execution** — call the right exchange/blockchain API for each leg with the right credentials and parameters.
3. **Reconciliation** — independently verify that funds actually left the source and actually arrived at the destination, by querying exchange transaction histories and on-chain explorers. This is the part that distinguishes "we initiated a transfer" from "the transfer completed".
4. **Settlement matching** — tie executed transfers back to deals booked in the upstream deal-management system (XAlpha / Optimus) so the firm's books mark trades as settled.
5. **Reporting and audit** — produce client-facing settlement reports, internal activity reports, and a per-settlement audit trail of every exchange and on-chain transaction involved.

Adjacent systems consumed (but not owned) by Altex:

- **Optimus** — master data for accounts, counterparties, portfolios, settlement methods, and blockchain networks. Altex never owns this data; it syncs it.
- **XAlpha** — deal management. Deals are streamed into Altex via Redis as they are booked, and Altex flags them as settled once the corresponding fund movement is matched.
- **Vault (HashiCorp)** — holds exchange API keys, scoped per account.
- **ComplyAdv** — compliance screening for counterparty transactions.

---

## 2. Architecture Overview

Altex is a small constellation of Python services backed by MySQL and Redis, fronted by a Next.js operator console. All persistent business state — transfer tasks, settlements, fees, reports — lives in the settlement engine's database. The transfer engine is deliberately **stateless** (in-memory caches only), and the exchanges package is a **library**, not a service.

```
                                  ┌───────────────────────────────────────────┐
                                  │  institutional-settlement-engine-frontend │
                                  │   (Next.js 16, React 19, RTK Query)       │
                                  └──────────────┬────────────────────────────┘
                                                 │ JWT cookie + alt-auth-token
                                                 │ (proxied via Next.js route handlers)
                                                 ▼
   ┌────────────────────────────────────────────────────────────────────────────────┐
   │                       sg-altonomy-settlement-engine                            │
   │                                                                                │
   │   ┌──────────────────────────────┐    ┌──────────────────────────────────┐     │
   │   │ settlement-engine-api        │    │ settlement-engine-long-running   │     │
   │   │  FastAPI (port 8031)         │    │  Polling loop, 10s tick          │     │
   │   │  /transfer, /settlements,    │    │  Drives TransferTaskPart state   │     │
   │   │  /optimus, /transactions     │    │  machine end-to-end              │     │
   │   └──────────────┬───────────────┘    └──────────────┬───────────────────┘     │
   │                  │                                   │                         │
   │                  └──────────────┬────────────────────┘                         │
   │                                 ▼                                              │
   │            MySQL (TransferTask, TransferTaskPart, Settlement, …)               │
   │            Redis (XAlpha deal stream)                                          │
   └──────────────┬─────────────────────────────────────────────────────────────────┘
                  │ HTTP (alt-auth-token)
                  ▼
   ┌────────────────────────────────────────────────────────────────────────────────┐
   │                       sg-altonomy-transfer-engine                              │
   │                       FastAPI (port 8766)                                      │
   │                                                                                │
   │   /transfer/internal_transfer    /transfer/withdraw_funds                      │
   │   /transfer/balances             /transfer/deposit_addresses                   │
   │   /txrecon/internal              /txrecon/withdraw                             │
   │   /txrecon/onchain               /txrecon/deposit                              │
   │                                                                                │
   │   ExchangeManager  ←─ HashiCorp Vault (per-account credentials)                │
   │   ReconManager     →  background listener threads per recon job                │
   └──────────────┬──────────────────────────────────┬──────────────────────────────┘
                  │                                  │
                  ▼                                  ▼
   ┌──────────────────────────────────┐   ┌───────────────────────────────────────┐
   │  sg-altonomy-exchanges (library) │   │  21 on-chain data sources             │
   │   ~78 exchange adapters          │   │   Blockchair, Blockscout, Solana,     │
   │   CoinMarket abstract base       │   │   TronGrid, Near, Algo, Ada, Dot,     │
   │   Binance, Bybit, OKX, Coinbase, │   │   Ripple, Ton, Story, Theta, Monad,   │
   │   Kraken, Bitfinex, Huobi, …     │   │   Hiro, Berascan, Taostats, Seiscan,  │
   │   IWithdrawal /                  │   │   …                                   │
   │   ITransferSubAccount /          │   │                                       │
   │   ITransactionHistory interfaces │   │                                       │
   └──────────────────────────────────┘   └───────────────────────────────────────┘
```

### Layering rationale

The split between settlement engine and transfer engine reflects a deliberate separation between **business workflow** and **mechanical execution**:

- The **settlement engine** owns *what should happen and why*: tasks, parts, recon state, settlement matching, deal linkage, OTP gates, audit. It is the system of record and the only service that talks to the database.
- The **transfer engine** owns *how it actually happens*: pick the right exchange adapter, present the right credentials, call the right endpoint, and verify the result. It is intentionally stateless so it can be restarted, scaled horizontally, or replaced without data-loss concerns.
- The **exchanges package** owns *vendor-specific quirks*: signing, rate limits, response shapes, error codes, symbol normalisation. By keeping this as a library consumed by transfer engine, the same adapter code can be reused (and has been, in other services) without forcing a network hop.

This layering also explains why the settlement engine *never* talks directly to an exchange or blockchain — every venue interaction goes through the transfer engine, which centralises credential handling and reconciliation logic.

---

## 3. The Core Workflow: Transfer Task Lifecycle

The heart of Altex is the **TransferTask** state machine. Every fund movement — internal rebalance or external settlement — is modelled as a Task composed of one or more **Parts** (legs).

### 3.1 Task creation (settlement-engine-api)

A task originates from one of several entry points:

- An operator filling out `/new-transfer` in the frontend (`POST /transfer/create`).
- A bulk CSV upload (`POST /transfer/bulk_create`).
- An internal-only move from MO (`POST /transfer/create_internal`).
- A recon-only record for a transfer that was executed out-of-band (`POST /transfer/create_external_recon_task`).
- A deal arriving on the XAlpha Redis stream, triggering automatic settlement creation.

When the task carries a settlement (`is_settlement=true`), the API first computes a path via `POST /transfer/path` or `/transfer/path_settlement`, which routes from source account to either an internal destination or an external OTC wallet. The resulting path becomes the ordered list of `TransferTaskPart` rows. Each part has its own source/destination accounts and addresses, asset, amount, and `transfer_method` (exchange-internal, on-chain withdrawal, bank wire, exchange cross-account, etc.).

The task lands in state `running` (or `paused` if approval is required); each part starts in `pending`.

### 3.2 Part execution (settlement-engine-long-running)

`settlement-engine-long-running` runs a single loop every 10 seconds. On each tick it advances every active part through four stages:

| Stage | Method |
|:-------|:--------|
| 1. `process_part_waiting()` | Check that the parent task is `running` and prerequisites are met, then transition `WAITING → RUNNING`. |
| 2. `process_transfer()` | Either skip (incoming-only parts have nothing to push) or call the transfer engine — `do_internal_transfer()` for exchange cross-account moves, `do_external_transfer()` for on-chain withdrawals. Record the returned `txn_id` / `internal_id`. Transition `RUNNING → TRANSFER_INITIATED`. |
| 3. `process_recon()` | Start (or poll) **source reconciliation**: prove the transfer actually left the source. POST to the transfer engine's `/txrecon/withdraw` or `/txrecon/internal` with a deterministic `recon_id = uuid5("{task_id}::{part_id}::{direction}")`. Poll for `ConfirmedRecon` or `Failed`. |
| 4. `resolve_dest_recon()` | Start (or poll) **destination reconciliation**: prove the transfer arrived at the destination. For on-chain destinations this hits `/txrecon/deposit` or `/txrecon/onchain`. Once confirmed, transition the part `TRANSFER_INITIATED → COMPLETED`, and if all parts of the task are complete, mark the task `completed`. |

Parts can fail at any stage. Operators can `pause`, `resume`, `cancel`, `skip`, `force-cancel`, or `restart` parts via the API — every state transition is logged for audit. Internal-exchange parts bypass on-chain recon (no chain involvement); bank wires and certain exchange-internal moves bypass dest recon entirely (configurable allow-list).

### 3.3 Reconciliation (transfer-engine)

Reconciliation is the part of Altex that makes the system trustworthy. Initiating a transfer is easy; *proving* it landed is the actual product. The transfer engine implements four recon flavours, each as a long-running listener thread managed by a singleton `ReconManager`:

- **Internal recon** — polls exchange transaction history looking for a record matching the expected currency, amount, and time window.
- **Withdraw recon** — matches by withdrawal `external_id` returned at execution time.
- **Deposit recon** — matches incoming deposits on the destination exchange by transaction reference.
- **On-chain recon** — fans out to up to **21 different blockchain data sources** (Blockchair, Blockscout, Solana, TronGrid, Near, Algo, Ada, Dot, Ripple, Ton, Story, Theta, Monad, Hiro, Berascan, Taostats, Seiscan, and others). Each source is checked for a transaction matching the expected from/to addresses, amount (within a fee threshold), and timestamp window. First successful match wins.

Recon IDs are deterministic (`uuid5` of task/part/direction), which means a recon poll is **idempotent and re-entrant** — restarting the long-running service does not lose state because the next tick simply queries the same recon ID and resumes where it left off.

### 3.4 Settlement matching

For settlement-type tasks, completion does not end the workflow. The settlement engine still has to mark the corresponding deal as settled in the OTC books:

1. The `Settlement` row (created when the deal arrived from XAlpha) is matched against the completed `TransferTask` (linked via `settle_id`, `x_deal_ref`, and `task_id`).
2. `Settlement.filled` is flipped to true.
3. `XAlphaCtrl.settle_deal_by_deal_ref()` pushes the settlement status back to XAlpha so the trading system reflects reality.
4. The audit endpoint (`GET /settlements/audit/{settlement_id}`) becomes able to render the full chain: deal → settlement → task → parts → exchange txns + on-chain txns (looked up via the external `TxnClient` service).

Operators can manually settle or un-settle via `/settlements/settle` and `/settlements/unsettled/{id}`, which is how mismatches get resolved.

---

## 4. Repository Breakdown

### 4.1 `sg-altonomy-exchanges` — Exchange Abstraction Library

**Type:** Python library (Poetry-packaged, version 1.0.260). Not a service; consumed by other services.

**Surface area:** Roughly **78 exchange adapters** (Binance + variants for spot/margin/USDM/COINM, Bybit, OKX v3/v5, Coinbase, Kraken, Bitfinex, Huobi + futures variants, Kucoin, Gate.io, MXC, Deribit, Poloniex, Bitget, …) plus OTC and crypto-protocol adapters (Paradigm, Paxos, Circle, Cumberland).

**Core abstractions:**

- `CoinMarket` (2,800 LOC) — the abstract base class that every exchange adapter extends. Defines the contract for market data, balances, orders, signing, error handling, rate limiting, response caching, and Redis fallback.
- Three additional interfaces in `Wallet.py` — `ITransactionHistory`, `ITransferSubAccount`, and `IWithdrawal` — that capture the operations Altex actually needs from a venue: pull transaction history, move funds between sub-accounts, and initiate on-chain withdrawals.
- A factory function `exchanges.Exchange(name, keys, …)` that dynamically imports the right concrete class by name.

**Why it matters to Altex:** the transfer engine treats every venue uniformly. When a part says `transfer_method=EXCHANGE_WITHDRAW` on Binance, the transfer engine calls `Binance().make_withdrawal(...)`. When the same field says `EXCHANGE_CROSS_ACCOUNT_TX` on OKX, it calls `Okex().transfer_funds(...)`. Adding a new venue is purely a matter of writing a new adapter against the four interfaces — no settlement-engine or transfer-engine code changes.

**Representative concrete adapter — Binance:** ~3,000 LOC covering spot/margin/futures, sub-account listing and transfer history, universal transfer between wallet types, on-chain withdrawal (multi-chain), deposit addresses, withdrawal-fee discovery, order execution, market data, and a WebSocket user-data stream for live account events. Rate limits are enforced per IP and per API key, with HMAC-SHA256 signing on every authenticated request.

### 4.2 `sg-altonomy-transfer-engine` — Fund Movement & Reconciliation Service

**Type:** Stateless FastAPI service (Python 3.9, port 8766). Uses uvicorn, no database.

**Responsibilities:**

- **Credential resolution.** `ExchangeManager` pulls per-account API keys from HashiCorp Vault on demand and caches adapter instances in an LRU keyed by `(exchange_name, account_id)`.
- **Transfer execution.** Routes `POST /transfer/internal_transfer` and `POST /transfer/withdraw_funds` to the right `exchanges.Exchange(...).transfer_funds()` / `.make_withdrawal()` call.
- **Read APIs.** Withdrawal fees, account balances, deposit addresses, transfer history, account UIDs — all proxied through the appropriate adapter.
- **Reconciliation.** Four listener types (`InternalReconListener`, `WithdrawReconListener`, `DepositReconListener`, `OnChainReconListener`) inheriting from a common `IReconListener` base that owns the polling loop, time window, and backoff. Listeners run as `threading.Thread` instances tracked by a process-wide `recon_manager`.
- **On-chain coverage.** 21 chain data sources behind a common `OnChainSource` abstract class so adding a new chain is a matter of dropping in another implementation.

**Authentication.** Every call validates `alt_auth_token` against the central auth service (`/auth_api/auth/verify`) and checks one of three RBAC scopes: `altex_admin_read`, `altex_admin_create`, `altex_admin_update`.

**Configuration hierarchy.** Four tiers — environment variables → centralised `altonomy.core.client()` lookup → `~/.altonomy/config.ini` → hardcoded defaults — so the same binary runs in dev, staging, and production with no code changes.

### 4.3 `sg-altonomy-settlement-engine` — Business Workflow & Persistence

**Type:** Python 3.9 / FastAPI 0.99 / SQLAlchemy 1.4 / MySQL 8 / Redis 7. Multiple processes share one codebase; for this product description only two are in scope.

#### `settlement-engine-api` (port 8031)

The REST surface exposed to the frontend. Five route groups under `/settlement_engine_api/`:

- **`/transfer`** — the operator-facing transfer-task CRUD: create (single, internal, bulk, QMS, external-recon), read (filters, pagination, live vs. historical, single task, part lists, per-part balance), update (pause, resume, cancel, skip, force-cancel, restart), and a path-computation endpoint. Roughly 30 routes; the bulk endpoints execute on threaded workers because they can fan out to dozens of underlying tasks.
- **`/settlements`** — settlement listing with filters (counterparty, asset, deal-ref, filled status), audit trail with full transaction lookup, manual settle / unsettle, and bulk creation. This is where the deal-management linkage lives.
- **`/optimus`** — read-through proxy of the upstream master-data system (instruments, prices, accounts, counterparties, portfolios, settlement methods, blockchain networks). Includes a `force_sync` endpoint and bulk-settlement-creation back to Optimus.
- **`/bookmark`** — operator UI bookmarks (saved filters).
- **`/transactions`** — historical ledger queries.

All routes are gated by the same `altex_admin_*` scopes as the transfer engine.

#### `settlement-engine-long-running`

A single-process polling loop, 10-second tick, that drives every active `TransferTaskPart` through its state machine:

```
pending → waiting → running → transfer_initiated → completed
   ↓        ↓        ↓             ↓
       (any failure state) ───────→ failed
       (operator action)  ───────→ cancelled
```

On each tick it calls, in order:

1. `process_part_waiting()` — promote parts whose prerequisites are now satisfied.
2. `process_transfer()` — execute the actual transfer via the transfer engine.
3. `process_recon()` — start or poll source reconciliation.
4. `resolve_dest_recon()` — start or poll destination reconciliation; on success, propagate to task-level status.

Authentication is handled once at startup via JWT login against Optimus; the token is refreshed as needed and presented as `alt-auth-token` on every downstream call.

#### Core domain models

| Model | Purpose |
|-------|---------|
| `TransferTask` | Header row for a single user-requested fund movement. Tracks status, total amount, source/destination accounts, optional `settle_id` linking to a settlement, maker (creator), and `x_deal_ref`. |
| `TransferTaskPart` | One leg in the multi-leg execution. Tracks `transfer_method`, addresses, amount, `txn_id`, `internal_id`, source and destination recon status, recon IDs, recon logs, and the per-part state machine. |
| `Settlement` | An OTC deal or a manual settlement record. Carries direction (incoming/outgoing/exec_fee/premium/margin), counterparty, asset, amount, `filled` flag, and the list of `tx_id`s that proved the settlement. |
| `Fee` | Recorded transfer fees, looked up at recon time and surfaced in reporting. |

#### External integrations

| System | Direction | Purpose |
|--------|-----------|---------|
| sg-altonomy-transfer-engine | outbound | Execute transfers, run recon |
| Optimus | bidirectional | Master data (in), settlement creation (out) |
| XAlpha (Redis stream) | inbound | Deal events → settlement creation |
| XAlpha (HTTP) | outbound | Flag deals as settled |
| Txn service | outbound | Look up exchange + blockchain txns for audit trail |
| ComplyAdv | outbound | Compliance screening (skipped — out of scope) |
| S3 | outbound | Client report exports (skipped — out of scope) |

### 4.4 `institutional-settlement-engine-frontend` — Operator Console

**Type:** Next.js 16 + React 19 application using the App Router. SSR/RSC for the page shell, Redux Toolkit + RTK Query for client-side data, AG Grid Enterprise for the dense tabular views, Ant Design + Tailwind for the rest of the UI.

**Layout.** Two route groups:

- `(login)` — a single `/login` page that handles username/password and, when the user profile requires it, a six-digit OTP.
- `(base)` — every authenticated screen, sharing a common navigation shell.

**Key pages.**

| Route | Purpose |
|-------|---------|
| `/outstanding` (home) | All active settlement obligations. The operator's dashboard. |
| `/new-transfer` | Create a single transfer (picks source/destination accounts, asset, amount, settlement method). |
| `/bulk-transfer` | CSV-driven bulk creation. |
| `/transfers` | Live and historical transfer requests with state, recon status, manual-action buttons (pause/resume/cancel/skip/restart). |
| `/txn-validation` | Validate pending on-chain transactions before they execute. |
| `/balances` | Live balances across exchange and bank accounts. |
| `/manage`, `/settlement-report`, `/client-activity-report`, `/option-settlement-report` | Reporting and back-office settlement management. |
| `/txn-statement` | Historical transaction ledger. |
| `/whitelisting` | Withdrawal address whitelisting. |

**Authentication.** Login posts to Optimus's `auth_api`. The resulting JWT is stored in an `httpOnly`, `Secure` cookie. Every browser request to `/api/*` is intercepted by a Next.js route handler that strips the cookie, forwards it as the `alt-auth-token` header, and proxies to either the settlement engine (`settlement_engine_api`), the account/auth services (`account_api`, `auth_api`), or the client-report API. The browser itself never talks directly to the Python services.

**Data fetching.** Each backend domain has its own RTK Query slice (`settlementsApi`, `transferApi`, `outstandingsApi`, `balancesApi`, `userApi`, `transactionsApi`, `clientActivityReportApi`, `bookmarksApi`, `settlementMethodsApi`). This gives normalised cache, request deduping, and tag-based invalidation for free, with each user action (approve transfer, settle, unsettle, …) re-fetching only the slices it touched.

**Users.** Internal Altonomy staff only — traders, MO, settlement ops, compliance, admins. The page copy ("Outstanding Settlements", "Manage Settlements", "Settlement Report", "Whitelisting"), the OTP-gated approval flow, the bulk CSV import, and the cross-counterparty visibility all point unambiguously at back-office personas rather than end customers. External OTC counterparties are referenced as data (`counterparty_ref`), never as users.

---

## 5. End-to-End Example: An External OTC Settlement

To make the architecture concrete, here is what happens when a desk books a sale of 100 BTC to an external OTC counterparty, with the counterparty's BTC settlement to be delivered from a Binance sub-account.

1. **Deal arrives.** The trade is booked in XAlpha. XAlpha publishes the deal to `STREAM:XALPHA:SETTLEMENT` on Redis.
2. **Settlement created.** The settlement engine's deal subscriber reads the stream and inserts a `Settlement` row (direction `outgoing`, asset `BTC`, amount `100`, counterparty `<client_ref>`, `filled=false`).
3. **Operator creates the transfer.** A settlement-ops user opens `/outstanding` in the frontend, sees the unsettled deal, and clicks "Settle". The frontend calls `POST /transfer/path_settlement` to compute the route — say, *Binance-sub → Binance-main → on-chain withdrawal to client wallet*. The operator confirms, supplying an OTP if required.
4. **Task persisted.** `settlement-engine-api` writes one `TransferTask` and three `TransferTaskPart` rows (or two, depending on the path). The task starts in `running`; parts in `pending`.
5. **Long-running picks it up.** On the next 10-second tick, `settlement-engine-long-running`:
   - Promotes part 1 (sub → main) to `RUNNING`.
   - Calls the transfer engine's `POST /transfer/internal_transfer` with the Binance account ID.
   - The transfer engine pulls Binance credentials from Vault, instantiates the `Binance` adapter, calls `universal_transfer()`, records the resulting `internal_id`.
   - The part moves to `TRANSFER_INITIATED`. The long-running service kicks off internal recon (`POST /txrecon/internal`).
   - The transfer engine's `InternalReconListener` polls Binance's transfer history every few seconds until it finds a matching entry, then resolves the recon to `ConfirmedRecon`.
   - On the next long-running tick, source recon is `ConfirmedRecon`, destination recon for an internal sub→main hop is bypassed, and the part is marked `COMPLETED`.
6. **Repeat for part 2** (Binance main → external client wallet, via on-chain withdrawal). This time the transfer is an on-chain withdrawal via `POST /transfer/withdraw_funds`, source recon is `withdraw` recon, and destination recon is `OnChainReconListener` querying Blockchair (and 20 other sources) for a Bitcoin transaction matching the expected address, amount, and time window.
7. **Task completes.** All parts done. The task is marked `completed`.
8. **Settlement filled.** The settlement engine matches the completed task against the `Settlement` row, sets `filled=true`, and calls XAlpha's `settle_deal_by_deal_ref` so the trading desk sees the deal as settled.
9. **Audit available.** The operator (or compliance) can hit `GET /settlements/audit/{settlement_id}` to retrieve the full chain: deal → settlement → task → both parts → the Binance internal transfer record → the on-chain BTC transaction (via the `TxnClient`).

The same flow, with different `transfer_method` values, handles internal-only rebalances (no on-chain leg), bank wires (no recon at all for the bank leg), exchange-to-exchange moves, and multi-asset settlements where base and quote settle independently.

---

## 6. Cross-Cutting Concerns

**Idempotency.** Recon IDs are deterministic UUIDs derived from `task_id`, `part_id`, and direction. Restarting any service does not duplicate work: the next call to `start_recon()` is treated as a status query for the existing recon job.

**Authentication.** A single auth service (Optimus's `auth_api`) mints JWTs that flow as `alt-auth-token` through every service. RBAC is uniform: three scopes — `altex_admin_read`, `altex_admin_create`, `altex_admin_update` — gate every API route.

**Credentials.** Exchange API keys are never embedded in code or environment. The transfer engine fetches them from HashiCorp Vault on demand, scoped per `account_id`, and caches the resulting adapter instance in memory. Rotating a key is a Vault operation, no deploy required.

**Observability.** The transfer engine writes dual log streams (a general log plus a recon-listener-specific log) with 7-day retention and LZMA compression. The settlement engine writes per-part `recon_src_log` and `recon_dest_log` JSON blobs into MySQL, so every state transition is retrievable via the audit endpoint.

**Manual overrides.** Every automated action has a manual counterpart: pause, resume, cancel, skip part, force-cancel part, restart part, manually settle, manually unsettle, `create_external_recon_task` for transfers executed out-of-band. This is deliberate — Altex assumes operators will sometimes know more than the system does, and it must let them say so.

**Scalability boundaries.** The settlement engine is the single source of truth and is not horizontally sharded; the long-running process is a single instance to avoid double-driving the state machine. The transfer engine is stateless and can run as many replicas as needed. The exchanges library has no shared state.

---

## 7. Glossary

- **Part / TransferTaskPart** — one leg of a multi-leg fund movement.
- **Task / TransferTask** — a single operator-requested or deal-triggered fund movement, composed of one or more Parts.
- **Settlement** — an obligation tied to a booked deal (or a manually created ledger item). Becomes `filled` once a matching completed Task exists.
- **Recon** — independent verification, by polling either an exchange API or a blockchain explorer, that a transfer actually happened. Distinct from "initiated".
- **MO** — Middle Office. Internal operations team that requests rebalancing.
- **OTC** — Over-the-counter. The desk's external counterparties.
- **XAlpha** — upstream deal-management system. Source of settlement obligations.
- **Optimus** — upstream master-data system. Source of accounts, counterparties, settlement methods.
- **Nitro Account** — Altonomy's internal account identifier; mapped to per-venue UIDs by the transfer engine.
