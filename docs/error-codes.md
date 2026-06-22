# Altex error codes

Error string pulled off log line (see [`logging-and-loki.md`](./logging-and-loki.md)) or from transfer's `transfer_task_part.txn_log` JSON column (see [`altex-db-schema.md`](./altex-db-schema.md)) is one of 2 things:

- a **local 4-digit Altonomy code** (`1xxx`–`4xxx`) — venue error collapsed into Altonomy's canonical vocabulary, or
- a **raw exchange-native string** — venue's own code/message, passed through unchanged.

Raw is common case. Only curated set of well-known errors gets local code; everything else falls through verbatim.

## The decision

1. **Bare 4-digit number** (`1001`, `2141`, `3023`, `4180`)? Then **local code**. Decode to underlying reason string via `code_reason` map (below), read embedded exchange status/code, then web-look-up that native code's meaning at venue.
2. **Otherwise raw exchange string**, usually already embeds venue's own code. 3 shapes recur from how built (see "How a reason string is built"):
   - `ApiError(status {status} code={code}): {message}` — `{code}` is **exchange's own error code**, `{status}` the HTTP status.
   - `RequestError(status {status}): {reason}`
   - `SystemError(status 555): ...` / `ClientError(status 444): ...` — Altonomy-internal, not from venue.
   Pull `code=` / `status` out, web-look-up against venue's API docs.
3. If neither 4-digit code nor those shapes, it's whatever venue returned (or transport error) — search verbatim.

## Where the codes live

`ErrorCode` (plus `ClientRequestException` / `InternalSystemException` wrappers) defined in **`altonomy-core`** dependency at `altonomy/core/exceptions.py`. `sg-altonomy-exchanges` repo does not define it — `altonomy/exchanges/exceptions.py` re-exports `ErrorCode`, `ClientRequestException`, and `InternalSystemException` from `altonomy.core.exceptions`, and every adapter reaches them via `from altonomy.exchanges import exceptions` (e.g. `CoinMarket.py` does `from . import Order, config, exceptions, utils`), never by importing `altonomy.core` directly. See [`altex-overview.md`](./altex-overview.md) for library's place in architecture.

`altonomy/exchanges/exceptions.py` adds HTTP-facing wrappers `ExchangeApiException`, `ExchangeRequestException`, and `ExchangeWebsocketException`; these build the reason strings.

## When codes are produced

Only on a **failed** exchange response. Successful response returned as parsed JSON, never touches `ErrorCode`.

Pipeline lives in `CoinMarket._handle_exchange_response`:

1. Status/body check decides `error`. `error = True` when HTTP status non-`2xx`, or — for venues returning `200` with error in body — when per-exchange `elif` finds venue's error field set (e.g. HUOBI futures `status != 'ok'`, KUCOIN2 `code != 200000`, OKEX `code > 0`).
2. On error, reason string built from one of wrappers' `__str__` (see next section), stored via `_set_error_message`, which sets `self.last_error_message` (and caches under `request_id` in `error_cache`). Method returns `None`.
3. Stored string surfaced in `reason` field of order/transfer results. `_format_send_order` returns `[order_id, {'state': ..., 'reason': error}]` where `error` defaults to `self.last_error_message`, and logs `error in send order: {error}` on failure.

Transport-level failures take different branch: `_retry_request` builds `ExchangeRequestException` string, logs `HTTP 4XX/5XX error detected - {message}`. (`_handle_response` is legacy path superseded by `_handle_exchange_response`.)

## How a reason string is built

Wrappers' `__str__` methods produce string and run encoding on it:

- `ExchangeApiException.__str__` → `ErrorCode.encode('ApiError(status {status_code} code={code}): {message}', exchange_name)`. `{code}`/`{message}` parsed from venue's JSON body in `__init__` (big per-exchange `if/elif` on `exchange_name` — each venue puts code/message under different keys).
- `ExchangeRequestException.__str__` → `ErrorCode.encode('RequestError(status {status_code}): {reason}')` (no exchange arg, so no per-exchange override layer applies).
- `InternalSystemException.__str__` → encodes `SystemError(status 555): {message}` (or message as-is if already contains `Error(status`; bare 4-char message returned unchanged).
- `ClientRequestException.__str__` → encodes `ClientError(status 444): {message}`.

Encoding is **lazy**: happens inside `__str__()` when string being built, not when exception constructed/raised.

## Encoding: how a raw string becomes a local code

`ErrorCode.encode(reason, exchange=None)` returns **local 4-digit code only when reason string matches**; otherwise returns raw reason string unchanged. Fallthrough at end:

```python
return ErrorCode.reason_code.get(reason, reason)   # altonomy/core/exceptions.py
```

3 match layers; first that hits wins:

| Layer | Trigger |
|:------|:--------|
| Substring rules | Checked first, exchange-independent. Substring found anywhere in reason. |
| Per-exchange overrides | Only when `exchange` one of `BINANCE`, `HUOBI`, `ABCC`, `GEMINI`. |
| Exact `reason_code` dict | Reason string equals dict **key** exactly. |

No layer matches → **raw string passes through** unchanged.

### Substring rules (full list, in order)

| Substring in reason | Local code |
|:--------------------|----------:|
| `SystemError(status 555): order_ref not found for order_id` | 1001 |
| `when calling remote method` | 4002 |
| `status 500` | 3011 |
| `status 502` | 3012 |
| `status 503` | 3013 |
| `status 504` | 3014 |
| `Read timed out` | 3023 |
| `Connection broken: IncompleteRead` | 3024 |
| `mismatch min notional` | 1002 |

### Per-exchange overrides

- `BINANCE`: `ApiError(status 418 code=-1003): Way too many requests; IP banned until` → 3103; `Illegal characters found in parameter` → 1104.
- `HUOBI`: `ApiError(status 200 code=account-frozen-balance-insufficient-error): trade account balance is not enough, left` → 2111.
- `ABCC`: `ApiError(status 400 code=2002): Failed to create order. Reason: cannot lock funds (amount:` → 2122; `ApiError(status 401 code=2006)` → 4121.
- `GEMINI`: maps by HTTP status prefix — `ApiError(status {N}` for N in {406→2186, 400→3180, 403→3183, 404→3184, 429→3189, 500→4180, 502→4182, 503→4183}.
- `KUCOIN2`, `COINBASE`: branches exist but no-ops (`pass`).

### Exact `reason_code` dict

~40 entries. Each key is **full literal reason string**; exact equality required. Examples: `ApiError(status 400 code=-2010): Account has insufficient balance for requested action.` → 2101; `RequestError(status 400): Bad Request` → 4141; `ClientError(status 444): unsupported instrument` → 4001.

## The 4 numeric ranges

Source: `altonomy-core` `altonomy/core/exceptions.py` (`ErrorCode.code_reason` / `reason_code`). **Confirmed at source** — dependency present and inspectable. Ranges are loose convention, not strict partitions: few codes sit outside nominal band (e.g. `4121` is auth/tonce error, `4141` generic 400).

| Range | Meaning |
|:------|:--------|
| 1xxx | Order-state: not found, already done, in settlement |
| 2xxx | Insufficient balance / funds / margin |
| 3xxx | Rate limiting, dropped connections, auth/signature failures |
| 4xxx | Server 5xx, unsupported instrument, trading disabled |

## Decoding a local code → reason string

To go other way (have 4-digit code, want underlying venue meaning), use inverse map:

```python
ErrorCode.decode(errorCode)   # returns ErrorCode.code_reason.get(errorCode, errorCode)
```

`code_reason` (~55 entries) maps each local code to canonical reason string embedding original venue `status`/`code`. From there, read `code=`/`status` and web-look-up venue's authoritative meaning. Examples:

| Local code | Reason string |
|:-----------|:--------------|
| 1001 | `SystemError(status 555): order_ref not found for order_id XX` |
| 1161 | `ApiError(status 200 code=1056): In settlement. Your order can't be placed/withdrew currently.` (HUOBI) |
| 2101 | `ApiError(status 400 code=-2010): Account has insufficient balance for requested action.` (Binance) |
| 3023 | `SystemError(status 555): HTTPConnectionPool(host=XX, port=80): Read timed out.` |
| 4101 | `ApiError(status 400 code=-2010): Rest API trading is not enabled.` |

Slots like `XX`/`AA`/`PP`/`NN` are placeholders filled with live values at runtime — stored code maps back to template, not original concrete text.

## Why exact matching is fragile

`reason_code` exact dict requires byte-for-byte equality against full literal string. If venue changes wording, adds/removes whitespace, or message contained filled-in templated slot (`XX`/`AA`/etc.), exact match fails and string falls through to raw. Such templated/variable errors only get local code when **substring rule or per-exchange override** happens to cover them. This is why most real-world reasons are raw, and why treat raw string as expected outcome rather than bug.

Note one source inconsistency: GEMINI `406` insufficient-funds error encodes to **2186** (via override and `code_reason`), but exact `reason_code` dict maps same literal string to **2181**. If you see either, they refer to same Gemini condition.