---
name: altex-triaging
description: Triage an issue raised by Middle-Office about an Altex transfer. Invoke as /altex-triaging <task_id> [optional developer context].
---

# altex-triaging

Top-level orchestrator for `/altex-triaging`. Given a `task_id` (and optionally free-form developer context), drives a four-phase investigation across the Settlement Engine, Account, and Instrument REST APIs/DBs and the two backend services' Loki logs, then writes a diagnosis report to `runs/<task_id>/diagnosis-report-<UTC-timestamp>.md` and prompts the engineer to capture novel findings into the playbook.

## When to invoke

Engineer types `/altex-triaging <task_id> [optional developer context]` after receiving an issue raised by Middle-Office. The skill produces a diagnosis report. `task_id` is the logical id from `Altex.transfer_task.task_id` — the integer the operator quotes. Any trailing text is provided as developer context.

## Workflow

Preamble (every run):

- Read `docs/agent-output-format.md`.
- Confirm `ALT_AUTH_TOKEN` is set; if it is missing/empty, abort per the `## Abort protocol` (missing-credential).
- `mkdir -p runs/<task_id>`. Use `test -n "$ALT_AUTH_TOKEN" && echo "ALT_AUTH_TOKEN set (len=${#ALT_AUTH_TOKEN})" || echo "ALT_AUTH_TOKEN MISSING"` (do NOT concatenate other commands).
- If a developer-context argument was provided, write it to `runs/<task_id>/developer-context.txt`.

The investigation proceeds through four phases. Read the linked phase file before executing that phase.

1. **Evidence Collection** — `./phases/1-evidence-collection.md`. Orchestrator runs the `transfer-discoverer` script to fetch `transfer/tasks` (live + historical) for the transfer record, reads its output to reconcile the matching group and pin the single failed part, then fans out four always-on collectors — the `account-discoverer` and `instrument-discoverer` scripts plus the `settlement-engine-log-digger` and `transfer-engine-log-digger` sub-agents — plus a conditional `error-code-resolver` sub-agent when the failed phase's log carries an exchange error code.
2. **Playbook Lookup** — `./phases/2-playbook-lookup.md`. Orchestrator-internal. Score `playbook/index.toon` entries against Phase 1 evidence into a ranked hypothesis queue.
3. **Investigation Loop** — `./phases/3-investigation-loop.md`. Up to `MAX_ITER = 5` iterations. Each spawns `altex-investigator` to test one hypothesis under fixed tool budgets. Exit on a proven hypothesis (`verdict == "proven"` AND `confidence >= 0.8`) or the cap.
4. **Issue Reporting** — `./phases/4-issue-reporting.md`. Write the report per `./templates/success-report-template.md`, print the chat summary, and prompt for a playbook entry if Phase 2 found no match.

## Rules

Global invariants the orchestrator must respect across every phase.

1. **Collector pattern.** Each Phase 1/3 capability is a self-contained collector — this system has no actual triage skills. Collectors come in two kinds:
   - **Scripts** (`transfer-discoverer`, `account-discoverer`, `instrument-discoverer`) — deterministic Python under `./scripts/`, run via Bash from filled `./prompts/<name>.txt` command templates. The orchestrator treats each as a black box behind its CLI flags.
   - **Sub-agents** (`settlement-engine-log-digger`, `transfer-engine-log-digger`, `error-code-resolver`, `altex-investigator`) — spawned by `subagent_type` with a bare JSON object (filled `./prompts/<name>.json`) as the prompt. The orchestrator does NOT read the agent files; each agent owns its own procedure (which DB/logs/queries to run and the result shape it returns).
2. **Fail loud on infra failure.** If any collector fails — a sub-agent spawn times out, a script exits non-zero, or a collector persists an agent output JSON with **empty `results`** — abort synthesis and follow the `## Abort protocol`. Do NOT prompt for a playbook entry on an aborted run.
3. **Tone.** The report and any drafted playbook entry are written in normal concise prose. The chat summary is short but plain English — the eventual "caveman mode" is for chat ephemera, not artifacts.
4. **Canonical agent output.** Every collector persists its output as JSON to a path the orchestrator pre-computes (`runs/<task_id>/<collector>-<ts>.json`) and surfaces that path — a sub-agent returns it as its chat reply; a script prints it to stdout (and signals a setup failure with a non-zero exit, writing no file). The shared shape is documented in `docs/agent-output-format.md` — evidence collectors carry two top-level keys (`error` plain string, `results[]` structured); `altex-investigator` adds a third (`take` string). Plus the empty-`results` failure rule and persistence + return-path rules.

## Abort protocol

Fail loud rather than synthesize from incomplete evidence. The orchestrator owns this — it is the only component that knows a step failed, decides to stop, and writes the failure report.

**Triggers.** Abort when any of:

- A sub-agent spawn times out, a script exits non-zero (setup failure — no file written), or no usable file lands at the path the collector was given.
- A collector persists an agent output JSON with **empty `results`** — no usable unit (e.g. a malformed spawn prompt, or every attempted unit errored). The file's `error` string carries the cause.
- The Phase 1 transfer-record reconciliation fails (orchestrator reading `transfer-discoverer` output): `ALT_AUTH_TOKEN` unset (missing-credential, checked in the preamble), a network error or any non-2xx other than 401/403 (incl. 5xx) on either group, or a `401`/`403` (token missing/expired/under-scoped).

**Action.** On any trigger:

1. **Stop.** Do not derive a verdict from partial evidence.
2. **Write a failure report** to `runs/<task_id>/diagnosis-report-<UTC-timestamp>.md` using `./templates/failure-report-template.md`.
3. **Print to chat:** `[<task_id>] ABORTED at Phase <X> (<component> unreachable). Partial evidence at runs/<task_id>/.`
4. **Do NOT prompt for a playbook entry.** An aborted run reached no verdict, so there is nothing novel to capture — fix the failing component and re-run.
