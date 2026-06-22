#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "requests>=2.31",
# ]
# ///
"""collect_instrument_evidence — read-only discovery of a token's configuration from
the Instrument REST API.

There is no symbol-keyed endpoint: GET the instrument list and filter
client-side by `asset_code` (exact match). The list is a bare JSON array — not
paginated. Among matches, the active row(s) are those with `valid_to == null`;
if more than one is active, keep them all. Zero matches after a 2xx is NOT an
error — it is the evidence that the symbol is unknown to the platform. Pure
evidence — no interpretation (no parsing `blockchain_network`, no chain-support
derivation). Writes the canonical agent-output envelope
(`.claude/skills/altex-triaging/docs/agent-output-format.md`) to --output-path and
prints that path.

Usage:
  uv run scripts/collect_instrument_evidence.py --output-path PATH --asset SYMBOL

Env: ALT_AUTH_TOKEN (required).
Exit: 0 once the envelope is written; non-zero only on setup failure.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _collector_common as common  # noqa: E402

INSTRUMENT_LIST = "/instrument_api/instrument/list"


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Instrument API discovery.")
    common.add_output_arg(parser)
    parser.add_argument(
        "--asset",
        required=True,
        help="Token symbol to look up (matched against asset_code).",
    )
    args = parser.parse_args()

    base = "https://optimus.altono.app"
    token = common.require_env()

    url = common.build_url(base, INSTRUMENT_LIST)
    r = common.http_get(url, token)

    # On 2xx: filter the array to asset_code == asset, then keep the active
    # match(es). [] on a non-list body or zero matches (zero matches != error).
    rows: list = []
    if r.ok and isinstance(r.json, list):
        matches = [
            x
            for x in r.json
            if isinstance(x, dict) and x.get("asset_code") == args.asset
        ]
        rows = [x for x in matches if x.get("valid_to") is None]

    results = [
        {
            "label": "instrument",
            "errored": not r.ok,
            "rows": rows,
            "extra": {"url": url, "http_status": r.status, "rows_returned": len(rows)},
        }
    ]
    error = "" if r.ok else common.failure_clause("instrument", r)

    common.emit(args.output_path, error, results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
