# institutional-altex-wrapper — Claude project instructions

This repository is a coding/debugging wrapper around the Altex product. It contains no service code. Its job is to give an agent enough cross-cutting context to work fluently across the 4/5 Altex service repositories — for routine coding tasks (cross-repo refactors, feature work, bug hunting, code review) as well as for the triage tooling that lives under `.claude/`.

The list of wrapped service repos is configured per developer via `CLAUDE_ADDITIONAL_DIR_N` env vars and synced into `.claude/settings.local.json` under `permissions.additionalDirectories`. Altex has 2 distinct repos for the frontend: V1 (sg-altonomy-settlement-engine-ui) and V2 (institutional-settlement-engine-frontend). Use only one in case you have to triage problems related to the frontend.

**Service repos stay product-and-triage-agnostic from this wrapper's perspective.** Do not push wrapper-specific tooling, triage hints, error-text maps, or playbook references into any service-repo `CLAUDE.md`. Cross-cutting knowledge lives here.
