# institutional-altex-wrapper

AI coding/debugging wrapper for the Altex product. Points at local checkouts of Altex's repositories and provides cross-cutting tooling for working across them.

## What is a wrapper?

A wrapper is a separate repository that contains no service code. Instead, it gives an AI coding agent the context needed to work across multiple service repos at once — typically a frontend and its backend(s), or a set of services that belong to one product.

This repo is agnostic to the specific services it wraps: the list of service checkouts is configured via env vars (see [Setup](#setup)).

What lives here:

- `.mcp.json` — MCP server definitions (Grafana, MySQL). See the file for the full list.
- `.claude/settings.json` — committed Claude Code settings (enabled MCP servers, baseline permissions).
- `.envrc.template` — template for the per-developer env file that wires secrets and the list of service repos into the agent.
- `scripts/update-additional-dirs.sh` — syncs `CLAUDE_ADDITIONAL_DIR_N` env vars into `.claude/settings.local.json` under `permissions.additionalDirectories`.
- `scripts/get-alt-auth-token.sh` — performs the Altex login + OTP flow and prints (or exports) an `ALT_AUTH_TOKEN` JWT for calling Altex services. See [Altonomy auth token](#altonomy-auth-token).

## Prerequisites

Install on the host before setup:

- [`uv`](https://docs.astral.sh/uv/) — runs the Grafana MCP server via `uvx`, and runs `scripts/rebuild-playbook-index.py` (PEP 723 single-file script; uv provisions Python + `python-frontmatter` on first invocation).
- [Node.js](https://nodejs.org/) — provides `npm` and `npx`. `npx` fetches and runs the MySQL MCP server on demand.
- [`direnv`](https://direnv.net/) — auto-loads `.envrc` on `cd` into the repo.
- [`jq`](https://jqlang.org/) — required by `scripts/update-additional-dirs.sh` and `scripts/get-alt-auth-token.sh`.
- [`op`](https://developer.1password.com/docs/cli/get-started/) — the 1Password CLI, required by `scripts/get-alt-auth-token.sh` to generate the live TOTP code. Must be signed in (`op signin`) and have access to the vault holding the OTP.
- [`pre-commit`](https://pre-commit.com/) — runs the playbook-index validation hook on each commit. Install with `brew install pre-commit` (macOS), `pipx install pre-commit`, or `uv tool install pre-commit`.

## Setup

1. Make the scripts executable:

  ```sh
  chmod +x scripts/*.sh
  ```

2. Copy the env template and fill it in:

  ```sh
  cp .envrc.template .envrc
  ```

  Edit `.envrc` and set:

  - Grafana credentials (`GRAFANA_URL`, `GRAFANA_USERNAME`, `GRAFANA_PASSWORD`).
  - MySQL credentials (`MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`, `MYSQL_PASS`).
  - One `CLAUDE_ADDITIONAL_DIR_N` per service repo you want the agent to access (`N` = 1, 2, 3, …). Each value is an absolute path to a local checkout. The script stops at the first unset/empty var.
  - Altonomy auth secrets:
    - `ALT_USERNAME` — your Altonomy username used to log into Altex.
    - `ALT_PASSWORD` — your Altonomy password used to log into Altex.
    - `ALT_1PASSWORD_OTP_PATH` — 1Password secret reference to the field holding the Altex one-time password, in `op://<vault>/<item>/<field>` form (e.g. `op://Employee/Altono/one-time password`).

  `.envrc` is the only file you need to edit for local configuration.

3. Authorize direnv (one-time per `.envrc` change):

  ```sh
  direnv allow
  ```

  After this, simply `cd`ing into the repo exports the vars and runs `scripts/update-additional-dirs.sh`, which rewrites `.claude/settings.local.json` with the configured additional directories (`.claude/settings.local.json` will be created if it doesn't exist).

4. Install the pre-commit hooks (one-time, after `pre-commit` is installed on the host):

  ```sh
  pre-commit install
  ```

  This wires `.pre-commit-config.yaml` into `.git/hooks/pre-commit`. On every commit that touches `playbook/*.md`, the hook runs `scripts/rebuild-playbook-index.py`: if any frontmatter is malformed the commit aborts with the offending paths on stderr, and if the regenerated `playbook/index.toon` differs from the staged copy pre-commit reports "files were modified by this hook" — re-stage `playbook/index.toon` and commit again. The same validation runs in CI via `.github/workflows/playbook-index.yml`.

## Future work

- **Agent-provider agnostic** — currently Claude Code only. Generalize so the same wrapper works with other coding agents (Codex, Cursor, etc.).
- **Inline env vars in `.claude/settings.json`** — once Claude Code supports env var expansion inside `settings.json` (see [anthropics/claude-code#46889](https://github.com/anthropics/claude-code/issues/46889)), drop `scripts/update-additional-dirs.sh` and reference `${CLAUDE_ADDITIONAL_DIR_N}` directly in committed settings.
