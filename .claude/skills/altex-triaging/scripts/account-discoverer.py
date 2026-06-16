#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "requests>=2.31",
# ]
# ///
"""account-discoverer — read-only discovery of account configuration from the
Account REST API.

For each side (src, dest) it runs two dependent GETs against the logical-keyed
`/history/...` endpoints (the only ones keyed on the logical id), selecting the
active version client-side (`valid_to == null`):

  1. account_product/history/{account_product_id}  -> active account_product,
     read its `account_id`
  2. account/history/{account_id}                   -> active parent account

The two sides are independent: a failure on one does not stop the other. Pure
evidence — no interpretation. Writes the canonical agent-output envelope
(`../docs/agent-output-format.md`) to --output-path and prints that path.

Usage:
  uv run scripts/account-discoverer.py --output-path PATH \\
      --account-product-id-src SRC --account-product-id-dest DEST

Env: ALT_AUTH_TOKEN (required).
Exit: 0 once the envelope is written; non-zero only on setup failure.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _discoverer_common as common  # noqa: E402

AP_HISTORY = "/account_api/account_product/history/"
ACCT_HISTORY = "/account_api/account/history/"


def run_side(
    side: str,
    account_product_id: str,
    token: str,
    base: str,
    results: list,
    errors: list[str],
) -> None:
    """Append the `ap_<side>` then `acct_<side>` entries for one side."""
    ap_label = f"ap_{side}"
    acct_label = f"acct_{side}"

    # 1. account_product version history -> active row(s).
    ap_url = common.build_url(base, AP_HISTORY + common.path_seg(account_product_id))
    ap = common.http_get(ap_url, token)
    ap_active = common.pick_active(ap.json) if ap.ok else []
    results.append(
        {
            "label": ap_label,
            "errored": (not ap.ok) or len(ap_active) == 0,
            "rows": ap_active,
            "extra": {
                "url": ap_url,
                "http_status": ap.status,
                "rows_returned": len(ap_active),
            },
        }
    )
    if not ap.ok:
        errors.append(common.failure_clause(ap_label, ap))
    elif not ap_active:
        errors.append(f"{ap_label}: no active account_product (valid_to=null)")

    # 2. account version history, keyed on the active account_product's account_id.
    #    If there is no active account_product, this dependent call cannot run.
    account_id = ap_active[0].get("account_id") if ap_active else None
    if account_id is None:
        results.append(
            {
                "label": acct_label,
                "errored": True,
                "rows": [],
                "extra": {
                    "url": None,
                    "http_status": None,
                    "rows_returned": 0,
                    "note": f"skipped: no account_id from {ap_label}",
                },
            }
        )
        errors.append(f"{acct_label}: skipped, no account_id from {ap_label}")
        return

    acct_url = common.build_url(base, ACCT_HISTORY + common.path_seg(account_id))
    acct = common.http_get(acct_url, token)
    acct_active = common.pick_active(acct.json) if acct.ok else []
    results.append(
        {
            "label": acct_label,
            "errored": (not acct.ok) or len(acct_active) == 0,
            "rows": acct_active,
            "extra": {
                "url": acct_url,
                "http_status": acct.status,
                "rows_returned": len(acct_active),
            },
        }
    )
    if not acct.ok:
        errors.append(common.failure_clause(acct_label, acct))
    elif not acct_active:
        errors.append(f"{acct_label}: no active account (valid_to=null)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Account API discovery.")
    parser.add_argument(
        "--output-path", required=True, help="Absolute path to write the JSON envelope."
    )
    parser.add_argument(
        "--account-product-id-src",
        required=True,
        help="Logical account-product id, source side.",
    )
    parser.add_argument(
        "--account-product-id-dest",
        required=True,
        help="Logical account-product id, destination side.",
    )
    args = parser.parse_args()

    base = "https://optimus.altono.app"
    token = common.require_env()

    results: list = []
    errors: list[str] = []

    # Order: ap_src, acct_src, ap_dest, acct_dest.
    run_side("src", args.account_product_id_src, token, base, results, errors)
    run_side("dest", args.account_product_id_dest, token, base, results, errors)

    common.emit(args.output_path, "; ".join(errors), results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
