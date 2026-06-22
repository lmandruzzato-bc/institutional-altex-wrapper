# Timezones and log time — Altex triage

How time is represented across the Altex systems, and the 2 places a local-timezone
offset silently leaks in and makes a correct query return **zero results**. Read this
before building any time-filtered Loki query or comparing an API/DB timestamp to a log line.

## TL;DR

- **Every machine-readable instant in Altex is UTC**: Settlement-Engine API epoch fields, Loki
  ingest timestamps, the timestamp printed inside the log line body, and the stored value of
  every altex-DB `datetime` column.
- **The Loki MCP (`query_loki_logs`) is pure UTC.** Pass `startRfc3339` / `endRfc3339` with a
  `Z` suffix (no offset ⇒ UTC). No conversion, no offset.
- **2 surfaces leak a host-local offset** (observed `UTC+1` / BST at authoring time). Both
  are display/serialization artifacts — the underlying stored instant is still UTC:
  1. **The human Grafana / Explore UI** renders and *accepts* times in the browser timezone.
     A hand-typed window is off by the local offset → "0 results" trap.
  2. **The altex-DB MCP driver** serializes `datetime` columns shifted by the host offset and
     mislabels them `Z`. A bare `SELECT some_time_col` comes back **1 h early**, tagged `Z`.
- **Agents:** always work in UTC. Use the orchestrator-supplied RFC3339 window for Loki, and
  read DB times server-side (`UNIX_TIMESTAMP()` / `DATE_FORMAT(... 'Z')`), never the bare column.

## Verified surfaces (task `260914`, recon `170269563309436692429298798615875585369`)

`task_create_time` epoch `1781180267.859191`; the matching recon log lines land at
`12:26:13 UTC` (~8 min later).

| Surface | What you get | TZ of the value | Notes |
|:---|:---|:---|:---|
| API epoch field (`task_create_time`, `start_time`, `transfer_time`, …) | `1781180267.859191` | UTC (epoch is absolute) | = `2026-06-11 12:17:47 UTC` |
| Loki ingest timestamp | ns-epoch string `1781180773717888269` | UTC | `/1e9` → `2026-06-11 12:26:13 UTC` |
| Loki log-line **body** timestamp | `2026-06-11 12:26:13.611 \| DEBUG …` | UTC | loguru/stdlib print UTC |
| Loki MCP query bounds (`startRfc3339`/`endRfc3339`) | you supply RFC3339 | **interpreted as UTC** | `12:15:00Z–13:00:00Z` returned the `12:26Z` event |
| altex-DB stored instant (`datetime(6)` col) | — | UTC | `UNIX_TIMESTAMP(task_create_time)` = `1781180267.859191` ✓ |
| altex-DB col via **MCP driver** (bare `SELECT col`) | `2026-06-11T11:17:47.859Z` | **shifted −1 h, falsely `Z`** | driver renders host-local-as-UTC |
| Grafana human UI picker/display | local clock | browser TZ (`UTC+1`) | event shows at `13:26`, not `12:26` |

The session/global DB timezone is `UTC` (`@@time_zone = UTC`, `NOW() == UTC_TIMESTAMP()`); the
−1 h on a bare column is purely the **MCP's MySQL driver** serializing the `datetime` as
host-local then ISO-rendering it as `…Z` — the stored instant is correct.

## API epoch → UTC RFC3339 (the conversion)

Settlement-Engine API time fields are **Unix epoch seconds (float)**. Epoch is timezone-absolute;
format it as UTC. For a Loki query, hand `query_loki_logs` an RFC3339-UTC string with `Z`:

```
epoch 1781180267.859191  →  2026-06-11T12:17:47Z
```

The orchestrator has a shell; the investigator does **not** (no Bash / code execution — see
`.claude/agents/altex-investigator.md` tool surface). **The orchestrator converts; the investigator
consumes.** Do not ask the investigator to do epoch math in its head.

```bash
# orchestrator (Bash) — epoch → RFC3339 UTC, with a clock-skew pad
python3 - <<'PY'
from datetime import datetime, timezone, timedelta
start, end = 1781180267.859191, 1781180267.859191   # the 2 window-bound instants (epoch secs); which part fields + pad → logging-and-loki §7
pad = timedelta(minutes=5)                            # skew + listener-start latency, per logging-and-loki §7
f = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
print(f(start - pad.total_seconds()), f(end + pad.total_seconds()))
PY
```

Window **span** (which instants to bracket, and how much to pad for recon-listener lifetime)
is owned by `logging-and-loki.md` § 7 — this doc owns only the *timezone* of the bounds.

## altex-DB: read times UTC-safe

A bare `SELECT <datetime_col>` over the MCP comes back **host-offset-shifted and mislabeled `Z`**
(see table). When the exact instant matters — building a Loki window, comparing to an API epoch —
force a server-side rendering the driver passes through untouched:

```sql
-- UTC-safe time reads for a part (driver does NOT re-shift strings / numbers)
SELECT UNIX_TIMESTAMP(transfer_time)                     AS transfer_epoch,   -- absolute, = API epoch
       DATE_FORMAT(transfer_time, '%Y-%m-%dT%H:%i:%sZ')  AS transfer_utc,     -- correct UTC string
       CAST(transfer_time AS CHAR)                        AS transfer_raw      -- raw stored value, still UTC
FROM transfer_task_part WHERE task_id = :task_id AND part_id = :part_id AND end_time IS NULL;

-- task_create_time lives on transfer_task (parent header, constant across parts), NOT the part:
SELECT UNIX_TIMESTAMP(task_create_time)                    AS create_epoch,
       DATE_FORMAT(task_create_time, '%Y-%m-%dT%H:%i:%sZ') AS create_utc
FROM transfer_task WHERE task_id = :task_id AND end_time IS NULL;
```

`FROM_UNIXTIME(...)`, `CONVERT_TZ(...)` and any other expression that yields a `datetime`
get re-shifted by the driver on the way out — keep the result a **string or number**.

## Grafana human UI (when you query by hand, not via MCP)

The UI renders and accepts times in the **browser/org timezone** (`UTC+1` here), so a window typed
as "12:15–13:00" actually means `11:15–12:00 UTC` and misses a `12:26 UTC` event — exactly the
symptom on task `260914` (results only appeared at "13:15–14:00", i.e. `12:26 UTC + 1 h`). Fix:
set the time picker to UTC (time range → *Change time settings* → **UTC**), or add the local offset.
Agents using the MCP are unaffected — the MCP is UTC.

See also: `logging-and-loki.md` § 7 (time-window patterns), `altex-db-schema.md` § 5 (cookbook).
