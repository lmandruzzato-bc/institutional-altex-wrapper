# Playbook

Accumulated triage knowledge for the Altex Middle-Office triage workflow. The `/altex-triaging` skill consults this directory after Phase A (failed-part / failed-phase identification) to short-circuit diagnosis when a known recurring issue matches.

Each entry is a self-contained Markdown file with YAML frontmatter (the machine-matchable signature) and a structured body (the human-readable diagnosis + fix recipe). `index.toon` is an auto-generated tabular projection of every entry's frontmatter, optimised for cheap scanning by the orchestrator.

## Entry schema

Every entry lives at `playbook/<kebab-case-id>.md`. The `id` in frontmatter must equal the filename without `.md`.

```markdown
---
id: <kebab-case-id, must equal filename without .md>
title: <short human-readable summary, ~80 chars max>
signature:
  phase: transfer | source_recon | dest_recon
  task_type: internal | external incoming | external outgoing | "*"
  transfer_method: <chain/rail string from transfer_task_part.transfer_method, or "*" for any. NOT a category — actual values are chain names like ERC20, TRC20, BTC, Solana, Signature, SIGNET, BCDC-*, or NULL for exchange-internal hops>
  exchange: <lowercased adapter name, or "*" for any>
  fix_category: code_bug | optimus_config | exchange_config | external_service
  error_patterns:                          # ordered list of distinctive substrings
    - "<literal substring 1>"
    - "<literal substring 2, e.g. -2015>"
last_seen: <YYYY-MM-DD>
example_task_ids: [<int>, ...]             # 2-5 historical task_ids for cross-reference
affected_repos: [<repo names from the four service repos>]
---

## Root cause

<One short paragraph: what is actually broken, in plain English.>

## Diagnostic steps

1. <Numbered, concrete: what to query/grep/inspect.>
2. <Each step references DB tables/columns or LogQL templates from LOGGING-STRATEGY.md.>
...

## Fix

- **Immediate (unblock the transfer):** <what the engineer or MO does to unblock this one task.>
- **Permanent (prevent recurrence):** <code change, Optimus config change, infra change, etc. Cite file:line where applicable.>

## References

- Code: <repo>/<path>:<line> — <one-line role>
- DB: <db>.<table>.<column> — <what it shows>
- Loki: <LogQL template with explicit time-window guidance>
- Related playbook entries: [[<other-id>]]
```

### Frontmatter field rules

- **`id`** — kebab-case, must equal the filename without `.md`. The index sorts on this.
- **`title`** — short human-readable summary, ~80 chars max.
- **`signature.phase`** — one of `transfer`, `source_recon`, `dest_recon`. Lowercase. Underscores (not hyphens) so it matches the failed-phase labels the triage skill uses.
- **`signature.task_type`** — the operation category from `transfer_task.task_type`: one of `internal`, `external incoming`, `external outgoing` (literal values with spaces). Use `"*"` for any. This is the real category dimension — withdraw-style vs internal-hop vs deposit-style.
- **`signature.transfer_method`** — the chain/rail value from `transfer_task_part.transfer_method` literally (`BTC`, `ERC20`, `TRC20`, `BEP20`, `Solana`, `TON`, `XRP`, `Silvergate SEN`, `Signature`, `SIGNET`, `BCDC-*` variants, …; ~64 distinct values, NULL for exchange-internal hops). **Not** a category enum. Use `"*"` when the issue is rail-agnostic; let `task_type` + `error_patterns` narrow the match.
- **`signature.exchange`** — lowercased adapter name (`binance`, `okx`, `bybit`, …). Use `"*"` for any.
- **`signature.fix_category`** — exactly one of `code_bug`, `optimus_config`, `exchange_config`, `external_service`. These are the buckets the triage report's `Suggested fix` uses.
- **`signature.error_patterns`** — ordered list of literal greppable substrings. **Not regexes.** The matcher uses these as `|=` filters in Loki and substring/`LIKE` matches in the DB log columns (`txn_log`, `recon_src_log`, `recon_dest_log`).
- **`last_seen`** — `YYYY-MM-DD`, date of the most recent confirmed occurrence.
- **`example_task_ids`** — 2–5 historical `task_id`s for cross-reference. Empty list allowed for seed entries.
- **`affected_repos`** — short names drawn from `transfer-engine`, `settlement-engine`, `exchanges`, `frontend`. Order by likelihood of relevance; the `codebase-locator` agent searches in that order.

## Index format

`playbook/index.toon` is auto-generated. TOON (Token-Oriented Object Notation) is a tabular header + rows:

```
[playbook_entries] -> id, title, phase, task_type, transfer_method, exchange, fix_category, error_patterns_joined, last_seen
binance-withdraw-ip-whitelist, Binance withdrawal IP not whitelisted, transfer, external outgoing, *, binance, exchange_config, "IP not in whitelist|-2015", 2026-04-12
onchain-recon-timeout-tron, On-chain recon timeout for TRX, dest_recon, external outgoing, TRC20, *, code_bug, "tx not found|timeout window expired", 2026-03-30
```

- One header row: `[playbook_entries] -> ` followed by the ordered column list (nine data columns).
- One row per entry. Columns in header order. Separator is `, ` (comma + space).
- `error_patterns_joined` = the entry's `error_patterns` joined with `|`, always double-quoted (patterns may contain commas, `|`, or unusual punctuation).
- `task_type` values contain spaces (`external outgoing`, `external incoming`); they are emitted unquoted because the field separator is `, ` (comma + space), so a bare space does not collide.
- Other fields are double-quoted only if they contain a comma or a `"`.
- Rows are sorted by `id` ascending (byte-collation, `LC_ALL=C`).
- File ends with one trailing newline.

## Adding an entry by hand

1. Write `playbook/<your-id>.md` following the schema above.
2. Run `./scripts/rebuild-playbook-index.py` from the repo root.
3. Inspect the resulting `playbook/index.toon` — your new entry should appear as a row in alphabetical position by `id`.
4. Commit the new entry **and** the updated `index.toon` together. The index is not gitignored — it is the cheap-to-scan projection the orchestrator reads first.

## How `/altex-triaging` proposes new entries

At the end of a triage run, if no existing playbook entry matched the failed task, the orchestrator prompts the engineer with a draft entry composed from the run's evidence:

- `phase`, `transfer_method`, `exchange`, `fix_category` derived from the verdict.
- `error_patterns` extracted from the most distinctive substrings in `txn_log` / Loki excerpts.
- Body sections (`Root cause`, `Diagnostic steps`, `Fix`, `References`) composed from the synthesis section of the report.

The orchestrator writes the draft to `playbook/<proposed-id>.md` and re-runs `rebuild-playbook-index.py` — but does **not** commit. The engineer reviews and edits before staging.

## Regeneration script

`scripts/rebuild-playbook-index.py` reads every `playbook/*.md` (excluding `README.md`), parses the YAML frontmatter via `python-frontmatter`, and rewrites `playbook/index.toon` in place. The script is idempotent (running it on an unchanged tree produces an identical file) and deterministic (rows sorted alphabetically by `id`).

Exit codes:
- `0` — index rebuilt (or unchanged).
- `1` — environment error (no write permission, `playbook/` not found).
- `2` — at least one entry has malformed frontmatter; the offending file paths are printed to stderr.

Dependencies: `uv`. The script is a [PEP 723](https://peps.python.org/pep-0723/) single-file script — its shebang `#!/usr/bin/env -S uv run --script` delegates Python version (`>=3.11`) and dependency (`python-frontmatter`) management to `uv`, which provisions an ephemeral cached venv on each run.
