# Phase 1 — Evidence Collection

Gather every piece of evidence the rest of the triage will reason over. The orchestrator first runs the `transfer-discoverer` script to fetch the transfer record, reads that output to reconcile which group carries the row and pin the single failed part, then fans out four parallel collectors (and one conditional collector) to enrich that part with account, instrument, log, and exchange-error-code context.

Collectors are of two kinds: deterministic **scripts** run via Bash (`transfer-discoverer`, `account-discoverer`, `instrument-discoverer`) and **sub-agents** spawned by `subagent_type` (`settlement-engine-log-digger`, `transfer-engine-log-digger`, `error-code-resolver`). Both write the same canonical envelope (`docs/agent-output-format.md`); they differ only in how they are invoked and how a setup failure surfaces.

This phase is the prerequisite for everything downstream: Phase 2 scores the playbook against the evidence collected here, and Phase 3 investigates hypotheses seeded by it.

## Step 1 — Fetch the transfer record (`transfer-discoverer`)

Run `transfer-discoverer` **first, alone**, before any fan-out. It fetches the transfer task from both the `live` and `historical` groups and dumps both responses verbatim — it does not pick a winner or judge the result.

Run it per the canonical contract (`docs/agent-output-format.md`): the orchestrator pre-computes `output_path`, reads `../prompts/transfer-discoverer.txt`, fills `output_path` and the `<task_id>` placeholder, and runs the resulting command via the Bash tool. The script writes the envelope to `runs/<task_id>/transfer-discoverer-<ts>.json` and prints that path. **A non-zero exit (bad args, missing env, unwritable path) means no file was written — treat it as a spawn-layer failure and abort** (`../SKILL.md` § Abort protocol).

`transfer-discoverer`'s output is the one Phase 1 evidence file the orchestrator reads to drive control flow (Steps 1-reconcile through 3).

### Reconcile (orchestrator-inline)

A failed `task_id` can surface in either the `live` group (status ∈ `Running`/`Paused`/`Failed`) or the `historical` group (status ∈ `PartiallyCompleted`/`Completed`/`Failed`/`Cancelled`). The script fetched both; the orchestrator decides what the two `results[]` entries (`live`, `historical`) mean. Each entry carries `extra.http_status` and `rows` (the response `list` of task objects).

| Condition (across the `live` + `historical` entries) | Handling |
|:---|:---|
| Network error (`http_status == 0`), or any non-2xx other than 401/403 (incl. 5xx) on either entry | **Infra failure** → abort (`../SKILL.md` § Abort protocol). |
| `401` / `403` on either entry | **Infra failure**, with the hint that `ALT_AUTH_TOKEN` is missing/expired/under-scoped. |
| Both entries are 2xx with empty `rows` | **Not infra.** Task not found. Abort cleanly: print `[<task_id>] No transfer task found in live or historical — check the id.` No fan-out, no report, no playbook prompt. |
| Exactly one entry is 2xx with non-empty `rows` | Use it; discard the empty one. Record which group matched (signal: `live` = in-flight, `historical` = terminal). |

The matched entry's `rows` holds the transfer task object (with its `parts`). That is the input to Step 2.

> [!NOTE]
> A non-zero exit from the `transfer-discoverer` script (malformed invocation / missing `--task-id`, or missing env) writes no file and is the generic spawn-layer abort signal per `../SKILL.md` § Abort protocol. On a clean (exit 0) run the envelope always carries both the `live` and `historical` entries.

## Step 2 — Identify the failed part (orchestrator-inline)

**Sequential invariant.** Within a `TransferTask`, parts execute strictly in `part_id` order; within a part, the three phases (`transfer`, `source_recon`, `dest_recon`) also execute in order. At any moment there is **exactly one failed part with exactly one failed phase**. There is no fan-out across parts or phases.

Walk the matched task's `parts[]` in ascending `part_id` order and select the first row whose `(status, recon_src, recon_dest)` triple matches one of these patterns:

| `status` | `recon_src` | `recon_dest` | Failed phase | Error log field |
|:---|:---|:---|:---|:---|
| `failed` | `pending` | `pending` | `transfer` | `txn_log` |
| (any) | `failed` | (any) | `source_recon` | `recon_src_log` |
| (any) | `recon confirmed` OR `manual confirmed` | `failed` | `dest_recon` | `recon_dest_log` |

Note the literal single space in `recon confirmed`, `manual confirmed`, `transfer initiated`.

If no part matches any pattern (e.g. the task is fully `completed`): **not infra.** Abort cleanly: print `[<task_id>] No failed part (task status=<x>) — nothing to triage.` Hard-stop even when developer context is present. No fan-out, no report, no playbook prompt.

The matched row is **the failed part**; its failed phase pins the single **error log field** to inspect. Extract the failed part's key fields for seeding the fan-out: `part_id`, `transfer_method`, `asset`, `amount`, `account_src`, `account_dest`, `address_src`, `address_dest`, `recon_id_src`, `recon_id_dest`, `internal_id`, `txn_id`, `start_time`, `transfer_time`, plus the failed phase and its error log field.

Dump the failed part (error log field included) in a file called `failed-part.json` in the `runs/<task_id>` directory.

## Step 3 — Detect an exchange error code (orchestrator-inline)

> [!NOTE]
> Run this step if and only if the failed phase is the `transfer` phase.

Read the failed phase's error log field (Step 2 table). The error string may be:

- a **local 4-digit internal code** minted by `sg-altonomy-exchanges`, or
- a **raw exchange-native string** still embedding `code=-NNNN` (the common case), or
- a recon/internal failure with **no exchange code at all**.

Scan the failed-phase log's error field for an error code — a local 4-digit code or a raw exchange-native code. Also try to infer the venue the error came from.

- **Error code found** → mark the `error-code-resolver` spawn (Step 4) as enabled and capture the raw error code and the `exchange_name`.
- **No error code** → skip the resolver spawn.

## Step 4 — Fan-out

Once the failed part and phase are pinned, launch the enrichment collectors **concurrently — in a single orchestrator message with parallel tool calls** (Bash for the scripts, Agent spawns for the sub-agents). Four are always-on; the fifth is conditional on Step 3.

| Collector | Kind | Seed (from the failed part) | Persisted output |
|:---|:---|:---|:---|
| `account-discoverer` | script | `account_src`, `account_dest` | `runs/<task_id>/account-discoverer-<ts>.json` |
| `instrument-discoverer` | script | `asset` | `runs/<task_id>/instrument-discoverer-<ts>.json` |
| `settlement-engine-log-digger` | agent | `task_id`, `part_id`, `internal_id`, `recon_id_src`, `recon_id_dest`, time window `[start_time, now]` | `runs/<task_id>/settlement-engine-log-digger-<ts>.json` |
| `transfer-engine-log-digger` | agent | `internal_id`, `txn_id`, `recon_id_src`, `recon_id_dest`, time window `[start_time, now]` | `runs/<task_id>/transfer-engine-log-digger-<ts>.json` |
| `error-code-resolver` *(only if Step 3 found an error code)* | agent | raw error string, `exchange_name` | `runs/<task_id>/error-code-resolver-<ts>.json` |

All write the canonical agent-output envelope (`docs/agent-output-format.md`) to a path the orchestrator pre-computes, and all surface that same path. The two kinds differ only in invocation and in how a setup failure surfaces:

- **Scripts** (`account-discoverer`, `instrument-discoverer`) — the orchestrator fills the `../prompts/<name>.txt` command template and runs it via the Bash tool; the script prints the path and signals a setup failure with a **non-zero exit** (no file written).
- **Sub-agents** (`settlement-engine-log-digger`, `transfer-engine-log-digger`, `error-code-resolver`) — the orchestrator spawns them by `subagent_type` with the filled `../prompts/<name>.json` object as the bare prompt; the agent returns the bare path.

The orchestrator does not read the agent definition files (Rule 1) and treats each script as a black box behind its flags, so the per-collector input keys and their failed-part source mappings live in the prompt templates under `../prompts/`. Each template carries `<...>` placeholders. For each collector the orchestrator reads the template, fills `output_path` with the value it pre-computed per the canonical contract, and substitutes every `<...>` placeholder with the failed-part value (using JSON `null` where the source field is absent — **except `account-discoverer`'s `<failed_part.account_dest>`, which is always present on a failed part; the script requires it**).

- `account-discoverer` → `../prompts/account-discoverer.txt` *(script)*
- `instrument-discoverer` → `../prompts/instrument-discoverer.txt` *(script)*
- `settlement-engine-log-digger` → `../prompts/settlement-engine-log-digger.json` *(agent)*
- `transfer-engine-log-digger` → `../prompts/transfer-engine-log-digger.json` *(agent)*
- `error-code-resolver` → `../prompts/error-code-resolver.json` *(agent)*

## Output / handoff to Phase 2

On success this phase yields, under `runs/<task_id>/`:

- `transfer-discoverer-<ts>.json` — Settlement Engine API responses (live + historical), canonical agent-output envelope.
- `account-discoverer-<ts>.json`
- `instrument-discoverer-<ts>.json`
- `settlement-engine-log-digger-<ts>.json`
- `transfer-engine-log-digger-<ts>.json`
- `error-code-resolver-<ts>.json` *(only if an error code was detected)*.

The orchestrator holds the extracted failed-part fields + failed phase in memory for Phase 2 scoring. Any collector failure — a sub-agent spawn that times out, a script that exits non-zero, or any collector that persists a file with empty `results` — triggers the abort protocol (`../SKILL.md` § Abort protocol).
