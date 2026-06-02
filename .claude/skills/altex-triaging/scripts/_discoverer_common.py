"""Shared helpers for the read-only Altex discoverer scripts.

This module is imported by the thin per-entity scripts (transfer-, account-,
instrument-discoverer). It is NOT runnable on its own: it has no shebang and no
PEP-723 dependency block. The importing script declares `requests` in its own
inline metadata, and because `uv run --script <file>` puts the script's own
directory on `sys.path[0]`, this sibling module resolves both as an import and
for its `import requests` (same ephemeral env).

It owns the parts every discoverer shares, so the agent-output contract
(`docs/agent-output-format.md`) lives in exactly one place:

  - env validation (`ALT_AUTH_TOKEN`),
  - a single-shot GET with status/network mapping (`http_get`),
  - the `valid_to == null` active-row pick (`pick_active`),
  - the canonical envelope write + bare-path print (`emit`),
  - hard-exit on setup failure (`die`).
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlencode

import requests

# (connect, read) seconds. Single shot — no retries, mirroring the agents'
# one-curl-per-call. A timeout or connection error is surfaced as a network
# failure (`http_status: 0`), never an exception that aborts the run.
TIMEOUT = (5, 30)

AUTH_HEADER = "Alt-Auth-Token"


@dataclass
class GetResult:
    """Outcome of one GET. `status == 0` means the call never reached the server
    (DNS/connect/read timeout); `ok` is true only on a 2xx. `json` is the parsed
    body, populated only on a 2xx with a JSON-decodable payload."""

    url: str
    status: int
    ok: bool
    json: object | None
    neterr: str | None


def die(msg: str, code: int) -> None:
    """Hard exit for setup failures (bad invocation / missing env / unwritable
    path). No envelope is written: the orchestrator reads 'no file at path' as a
    spawn-layer failure and aborts."""
    print(f"{Path(sys.argv[0]).name}: {msg}", file=sys.stderr)
    sys.exit(code)


def require_env() -> tuple[str, str]:
    """Return token or hard-exit 1 naming the missing var(s).
    Trailing slash is stripped from the base so path concatenation is clean."""
    token = os.environ.get("ALT_AUTH_TOKEN")
    if not token:
        die("missing required env var: ALT_AUTH_TOKEN", 1)
    return token


def build_url(base: str, path: str, params: dict | None = None) -> str:
    """Compose a full request URL. The result is recorded verbatim in
    `extra.url`, so it is built explicitly (not derived from the response)
    and is therefore present even when the call fails at the network layer."""
    url = base + path
    if params:
        url += "?" + urlencode(params)
    return url


def path_seg(value: object) -> str:
    """URL-encode an id for use as a path segment."""
    return quote(str(value), safe="")


def http_get(url: str, token: str) -> GetResult:
    """One GET with the auth header. Never raises: network/timeout failures map
    to `status=0, ok=False`. Body is parsed only on a 2xx."""
    try:
        resp = requests.get(url, headers={AUTH_HEADER: token}, timeout=TIMEOUT)
    except requests.RequestException as exc:
        return GetResult(
            url=url, status=0, ok=False, json=None, neterr=type(exc).__name__
        )

    ok = 200 <= resp.status_code < 300
    body: object | None = None
    if ok:
        try:
            body = resp.json()
        except ValueError:
            body = None
    return GetResult(url=url, status=resp.status_code, ok=ok, json=body, neterr=None)


def failure_clause(label: str, r: GetResult) -> str:
    """Human-readable clause for the `error` string when a call did not 2xx."""
    if r.status == 0:
        return f"{label} network error ({r.neterr})"
    return f"{label} returned {r.status}"


def pick_active(body: object) -> list:
    """From a version-history array, return every row whose `valid_to` is null
    (the active version(s)). A non-list body yields `[]`. Multiple actives are
    all returned — the orchestrator sees any duplication."""
    if not isinstance(body, list):
        return []
    return [
        row for row in body if isinstance(row, dict) and row.get("valid_to") is None
    ]


def emit(output_path: str, error: str, results: list) -> None:
    """Write the canonical envelope to `output_path` (pretty, 2-space, trailing
    newline, UTF-8), creating the parent dir if needed, then print the bare path
    the caller passed — the agent-output return-path contract. A write failure is
    a setup failure → hard exit (no partial file claimed as evidence)."""
    target = Path(output_path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="\n") as f:
            json.dump(
                {"error": error, "results": results}, f, indent=2, ensure_ascii=False
            )
            f.write("\n")
    except OSError as exc:
        die(f"failed to write {output_path}: {exc}", 1)
    print(output_path)
