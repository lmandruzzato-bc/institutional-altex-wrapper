# Agent output format

Every collector run by `/altex-triaging` (sub-agent or script) writes its result as a JSON file to a path the orchestrator pre-computes, then surfaces only that absolute path. The orchestrator reads the file from disk and parses it. This document is the canonical contract for that JSON.

## File on disk

- **Path pattern.** `runs/<task_id>/<agent>-<UTC-timestamp>.json` (e.g. `runs/4827193/account-discoverer-2026-05-23T143211Z.json`). The orchestrator computes this path per spawn and passes it as `output_path` in the spawn prompt; the agent writes there verbatim.
- **Timestamp.** UTC ISO-8601 with `Z` suffix, no colons in the filename portion (e.g. `2026-05-23T143211Z`). It lives only inside the filename — there is no separate `ts` field anywhere.
- **Format.** Pretty-printed JSON, 2-space indent, UTF-8, trailing newline.
- **Layout.** Flat under `runs/<task_id>/`.

### Return-path contract

After writing the file, the agent's chat reply MUST be:

- A single line.
- The bare absolute path the orchestrator passed as `output_path`.
- No prefix, no preamble, no narration, no markdown, no code-fence.

> [!CRITICAL]
> The orchestrator pre-creates `runs/<task_id>/` and supplies `output_path`, so the write does not fail in practice — there is no write-failure reply. If a file is somehow absent at the returned path, the orchestrator treats the spawn as having returned no usable evidence and aborts (see `## Failure model`).

### Scripted collectors

Three collectors are deterministic scripts (`transfer-discoverer`, `account-discoverer`, `instrument-discoverer`), not sub-agents. They write the **identical envelope** to `output_path`, but the return channel differs:

- The path goes to **stdout**, not a chat reply.
- A setup failure (bad args, missing env, unwritable path) is signalled by a **non-zero exit with no file written** — not by an empty-`results` envelope.

The orchestrator reads a non-zero exit as a spawn-layer failure and aborts; on exit 0 it reads the pre-computed file and applies the `## Failure model` below exactly as for a sub-agent.

## Top-level shape

Evidence-collection agents have exactly two top-level keys, in this order:

```json
{
  "error":   "<plain string, may be empty>",
  "results": [ /* see ## `results[]` */ ]
}
```

`altex-investigator` (Phase 3) additionally carries a third key, `take`, after `results` (see `## take`). Phase 1 agents collect evidence only and omit it.

## `error`

A single plain string. Free prose, not JSON, not an object, not an array.

- `""` (empty) means nothing errored. Also `null` is treated as empty.
- Otherwise it names every failure the agent hit, in plain English — e.g. `"dest query timed out; src + address-book queries ok"` or `"asked to INSERT, refused; ran the read-only queries instead"`.
- It is **never itself an abort trigger.** The orchestrator decides abort-vs-continue from `results` (see below), not from this string. The string is for the human reading the report.
- Which specific unit failed is also visible structurally: the matching `results[]` entry carries `errored: true`.

## `results[]`

Zero or more objects, one per attempted unit (query / filter / grep op), in **chronological attempt order**. This is the only structured part of the output. Universal fields on every entry:

| Field | Value |
|:---|:---|
| `label` | Stable identifier for the attempted unit (the query label, recipe filter label, or per-grep short name). |
| `errored` | `true` \| `false`. `true` means this unit failed; `rows` is empty and `error` describes the cause. |
| `rows` | Array of row objects (possibly empty). Row schema is agent-specific. |
| `extra` | Object of arbitrary additional agent-specific fields (e.g. `url`, `http_status`, `rows_returned`). |

Each agent documents its own `rows` and `extra` shapes.

## `take`

A bare JSON string (NOT an object). Present **only on `altex-investigator`** (Phase 3) — forming an interpretation is its job. Phase 1 evidence-collection agents (`transfer-discoverer`, `account-discoverer`, `instrument-discoverer`, `settlement-engine-log-digger`, `transfer-engine-log-digger`, `error-code-resolver`) collect raw data and omit this key entirely; their interpretation, where any exists, lives in structured `results[]` fields, not in prose.

- 2–3 sentences of plain-English interpretation grounded in the rows actually returned.
- Never an object. The orchestrator does NOT consume `take` as structured data. The `take` is interpretation, not handoff.

## Failure model

How the orchestrator reads an agent's outcome:

- **Spawn-layer failure** (collector never returned a usable file — a sub-agent spawn timed out, a script exited non-zero, or no file landed at the returned path) → orchestrator aborts (no synthesis from partial evidence).
- **Empty `results`** (file returned, but the agent produced no usable unit — e.g. a malformed spawn prompt, or every attempted unit errored) → orchestrator aborts. `error` carries the cause.
- **Non-empty `results`** (at least one unit returned, even with some `errored: true` entries and a non-empty `error`) → continue. Survivors are synthesized; `error` rides along into the report.
