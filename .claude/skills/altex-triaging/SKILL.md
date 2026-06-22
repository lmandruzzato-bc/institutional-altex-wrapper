---
name: altex-triaging
description: Triage an issue about an Altex transfer. Invoke as /altex-triaging <task_id> [optional user context].
---

# altex-triaging

Top-level orchestrator for `/altex-triaging`. Given a `task_id` (and optionally free-form user context), drives a four-phase investigation, then writes a diagnosis report to `runs/<task_id>/diagnosis-report-<UTC-timestamp>.md` and prompts the user to capture novel findings into the playbook.

## When to invoke

User types `/altex-triaging <task_id> [optional user context]`. The skill produces a diagnosis report. `task_id` is the logical ID from `Altex.transfer_task.task_id` — the integer the operator quotes. Any trailing text is provided as user context.

## Workflow

Preamble (every run):

- **Work from the repo root.** Run every command and spawn every sub-agent from the `institutional-altex-wrapper` repo root. All `runs/<task_id>/…` paths in this skill are relative to that root; `runs/` is the git-ignored top-level dir there.
- Read `.claude/skills/altex-triaging/docs/agent-output-format.md`.
- Confirm `ALT_AUTH_TOKEN` is set; if it is missing/empty, abort per the `## Abort protocol` (missing-credential). Use `test -n "$ALT_AUTH_TOKEN" && echo "ALT_AUTH_TOKEN set (len=${#ALT_AUTH_TOKEN})" || echo "ALT_AUTH_TOKEN MISSING"` (do NOT concatenate other commands).
- `mkdir -p runs/<task_id>`.
- If a user-context argument was provided, write it to `runs/<task_id>/user-context.txt`.

The investigation proceeds through 4 phases. Read the linked phase file before executing that phase.

1. **Evidence Collection** — `.claude/skills/altex-triaging/phases/1-evidence-collection.md`. Orchestrator runs the `collect_transfer_evidence` script to fetch `transfer/tasks` (live + historical) for the transfer record, reads its output to reconcile the matching group and pin the single failed part, then fans out 2 always-on evidence collectors — the `collect_account_evidence` and `collect_instrument_evidence` scripts — plus a conditional `error-code-resolver` sub-agent when the failed phase's log carries an exchange error code. *(The `settlement-engine-log-digger` and `transfer-engine-log-digger` sub-agents are **SKIPPED — pending logging-framework maturity**.)*
2. **Playbook Lookup** — `.claude/skills/altex-triaging/phases/2-playbook-lookup.md`. **SKIPPED (pending: playbook population).** The orchestrator skips playbook scoring and proceeds directly to Phase 3, minting hypothesis #1 from Phase-1 evidence. When re-enabled, scoring `playbook/index.toon` entries against Phase 1 evidence will *bias* hypothesis selection — the loop structure is unchanged.
3. **Investigation Loop** — `.claude/skills/altex-triaging/phases/3-investigation-loop.md`. Up to `MAX_ITER = 5` iterations. The orchestrator formulates each hypothesis (#1 from Phase-1 evidence, #N from the prior iteration's findings) and spawns `altex-investigator` agents to test that one hypothesis, pulling Loki / altex-DB / code / web on demand under its own tool budgets. Exit on a proven hypothesis (`verdict == "proven"` AND `confidence >= 0.8`); otherwise stop at the cap or on hypothesis exhaustion and report inconclusive. Spawn-layer failure or an empty-`results` file aborts (`## Abort protocol`); a valid `inconclusive` does not.
4. **Issue Reporting** — `.claude/skills/altex-triaging/phases/4-issue-reporting.md`. Reached only on a Phase-3 `proven` or `inconclusive` verdict (aborts and Phase-1 clean-stops never get here). Write the report per `.claude/skills/altex-triaging/templates/success-report-template.md`, print the chat summary, and — only when the run **proved a root cause** — prompt for a playbook entry. *(On an `inconclusive` verdict there is nothing confirmed to capture, so no prompt.)*

## Rules

Global invariants the orchestrator must respect across every phase.

1. **Collector pattern.** Each collector has no actual triage skills. They just collect evidences.
2. **Fail loud on infra failure.** If any collector fails — a sub-agent spawn times out, a script exits non-zero, or a collector persists an agent output JSON with **empty `results`** — abort synthesis and follow the `## Abort protocol`.
3. **Tone.** The report and any drafted playbook entry are written in normal concise prose. The chat summary is short but plain English — the eventual "caveman mode" is for chat ephemera, not artifacts.
4. **Canonical agent output.** Every collector persists its output as JSON to a path the orchestrator pre-computes (`runs/<task_id>/<collector>-<ts>.json`) and surfaces that path — a sub-agent returns it as its chat reply; a script prints it to stdout (and signals a setup failure with a non-zero exit, writing no file). Details at `.claude/skills/altex-triaging/docs/agent-output-format.md`.

## Abort protocol

Fail loud rather than synthesize from incomplete evidence. The orchestrator owns this — it is the only component that knows a step failed, decides to stop, and writes the failure report.

**Triggers.** Abort when any of:

- A sub-agent spawn times out, a script exits non-zero (setup failure — no file written), or no usable file lands at the path the collector was given.
- A collector persists an agent output JSON with **empty `results`** — no usable unit (e.g., a malformed spawn prompt, or every attempted unit errored). The file's `error` string carries the cause.
- The Phase 1 transfer-record reconciliation fails (orchestrator reading `collect_transfer_evidence` output): `ALT_AUTH_TOKEN` unset (missing-credential, checked in the preamble), a network error or any non-2xx other than 401/403 (incl. 5xx) on either group, or a `401`/`403` (token missing/expired/under-scoped).

**Action.** On any trigger:

1. **Stop.** Do not derive a verdict from partial evidence.
2. **Write a failure report** to `runs/<task_id>/diagnosis-report-<UTC-timestamp>.md` using `.claude/skills/altex-triaging/templates/failure-report-template.md`.
3. **Print to chat:** `[<task_id>] ABORTED at Phase <X> (<component> unreachable). Partial evidence at runs/<task_id>/.`
4. **Do NOT prompt for a playbook entry.** An aborted run reached no verdict, so there is nothing novel to capture — fix the failing component and re-run.
