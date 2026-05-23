# institutional-altex-wrapper — Claude project instructions

This repository is a coding/debugging wrapper around the Altex product. It contains no service code. Its job is to give an agent enough cross-cutting context to work fluently across the four/five Altex service repositories — for routine coding tasks (cross-repo refactors, feature work, bug hunting, code review) as well as for the triage tooling that lives under `.claude/`.

The list of wrapped service repos is configured per developer via `CLAUDE_ADDITIONAL_DIR_N` env vars and synced into `.claude/settings.local.json` under `permissions.additionalDirectories`. Altex has two distinct repos for the frontend: V1 (sg-altonomy-settlement-engine-ui) and V2 (institutional-settlement-engine-frontend). Use only one in case you have to triage problems related to the frontend.

**Service repos stay product-and-triage-agnostic from this wrapper's perspective.** Do not push wrapper-specific tooling, triage hints, error-text maps, or playbook references into any service-repo `CLAUDE.md`. Cross-cutting knowledge lives here.

## Triage tooling

The wrapper hosts a slash command/skill — `/altex-triaging <task_id>` — that automates the manual triage work Middle-Office complaints used to require. The orchestrator skill lives at `.claude/skills/altex-triaging/SKILL.md` and is the only entrypoint an engineer invokes directly; everything it needs is documented inside that skill. Generic sub-agents (`mysql-master`, `loki-magician`, `codebase-locator`) live under `.claude/agents/` and load per-investigation skills under `.claude/skills/` to become context-aware.

If you are working on or extending the triage flow, read the orchestrator skill first plus `docs/agent-output-format.md`. The skill is the source of truth for the agent/skill contract; `docs/agent-output-format.md` is the source of truth for the canonical agent output JSON every sub-agent persists.

## Cross-cutting coding work

This wrapper is also the right place to drive multi-repo coding tasks across the four service repos — refactors that touch both backends, feature work that crosses the FastAPI/Next.js boundary, library bumps in `exchanges` that ripple into `transfer-engine`, etc. When doing that work:

- Treat each service repo as an independent codebase with its own conventions, test runner, and CI. Read its `CLAUDE.md` before editing.
- Run tests inside the relevant service repo, not from this wrapper root.
- Open PRs in the service repo where the change lives. The wrapper itself rarely needs commits for cross-repo work — only artefacts under `.claude/`, `playbook/`, or root docs belong here.

## Repo layout

```
.claude/
  agents/                  generic sub-agents (mysql-master, loki-magician, codebase-locator)
  skills/
    altex-triaging/        top-level orchestrator skill (/altex-triaging slash command)
    transfer-discovery/    mysql-master skill — Phase A
    account-discovery/     mysql-master skill — Phase B
    instrument-discovery/  mysql-master skill — Phase B
    log-digging/           loki-magician skill — Phase B
docs/
  agent-output-format.md   canonical agent output JSON contract (shared by all sub-agents)
playbook/                  accumulated triage knowledge, one entry per recurring root cause
  index.toon               auto-generated signature index
  README.md                entry schema + regeneration flow (read this before editing entries)
  <kebab-id>.md            one entry per recurring issue
runs/                      per-triage diagnosis reports + per-spawn agent output JSON (gitignored)
scripts/                   wrapper-local utility scripts
.mcp.json                  MCP server definitions (Grafana + three MySQL servers)
.envrc, .envrc.template    per-developer env (Grafana/MySQL creds, additional-dir paths)
README.md                  setup and prerequisites
```
