# Altex triage — task <task_id> — ABORTED

Generated <UTC iso8601> by /altex-triaging.

The run aborted before reaching a verdict. No synthesis was attempted on partial evidence.

## Failure

Aborted at **Phase <N> (<phase name>)**. `<component>` did not return usable evidence.

`<component>` is the orchestrator's `curl` to the Settlement Engine API, or the named sub-agent that failed (`collect_account_evidence`, `collect_instrument_evidence`, a log-digger, `error-code-resolver`, or `altex-investigator`).

## Evidence collected before abort

Whatever artifacts landed under `runs/<task_id>/` before the abort. Open any for detail — a failing sub-agent's own JSON, when one was written, carries its `error` string.

- `runs/<task_id>/<file>`
- …

(If nothing completed, state "none" — the failure was at the first step.)

## Next step

Fix `<component>`, then re-run `/altex-triaging <task_id>`.
