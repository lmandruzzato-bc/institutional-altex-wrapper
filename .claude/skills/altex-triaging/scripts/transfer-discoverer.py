#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "requests>=2.31",
# ]
# ///
"""transfer-discoverer — read-only fetch of a transfer task from the Settlement
Engine REST API.

A failed task_id can surface in either the `live` group or the `historical`
group, and we do not know which. Fetch BOTH, dump each response verbatim, and
make no decision about which group wins, whether the task exists, or whether an
HTTP error is fatal — the orchestrator owns all of that. Writes the canonical
agent-output envelope (`../docs/agent-output-format.md`) to --output-path and
prints that path.

Usage:
  uv run scripts/transfer-discoverer.py --output-path PATH --task-id ID

Env: ALT_AUTH_TOKEN (required).
Exit: 0 once the envelope is written (any HTTP/network outcome rides inside it);
      non-zero only on setup failure (bad args, missing env, unwritable path).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _discoverer_common as common  # noqa: E402

TASKS_PATH = "/settlement_engine_api/transfer/tasks"
GROUPS = ("live", "historical")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Settlement Engine transfer fetch."
    )
    parser.add_argument(
        "--output-path", required=True, help="Absolute path to write the JSON envelope."
    )
    parser.add_argument(
        "--task-id", required=True, help="Logical transfer task id to fetch."
    )
    args = parser.parse_args()

    base = "https://altex.altono.app"
    token = common.require_env()

    results: list = []
    errors: list[str] = []

    # Two entries, in this fixed order: live, then historical.
    for group in GROUPS:
        url = common.build_url(
            base,
            TASKS_PATH,
            {"group": group, "include_parts": "true", "task_ids": args.task_id},
        )
        r = common.http_get(url, token)

        # Success body shape: {"list": [<task objects, each with "parts">], ...}.
        # rows is that `list` verbatim; [] on error or a body without a `list`.
        rows: list = []
        if r.ok and isinstance(r.json, dict) and isinstance(r.json.get("list"), list):
            rows = r.json["list"]

        results.append(
            {
                "label": group,
                "errored": not r.ok,
                "rows": rows,
                "extra": {"url": url, "http_status": r.status},
            }
        )
        if not r.ok:
            errors.append(common.failure_clause(group, r))

    common.emit(args.output_path, "; ".join(errors), results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
