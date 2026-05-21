#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "python-frontmatter>=1.1",
# ]
# ///
"""Rebuild playbook/index.toon from all playbook/*.md frontmatter.

Exit codes:
  0 - index rebuilt (or unchanged)
  1 - environment error (no playbook dir, write failure)
  2 - one or more entries have malformed frontmatter
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import frontmatter

REPO_ROOT = Path(__file__).resolve().parent.parent
PLAYBOOK_DIR = REPO_ROOT / "playbook"
INDEX_FILE = PLAYBOOK_DIR / "index.toon"
HEADER = (
    "[playbook_entries] -> id, title, phase, task_type, transfer_method, "
    "exchange, fix_category, error_patterns_joined, last_seen"
)

REQUIRED_TOP = ("id", "title", "last_seen")
REQUIRED_SIG = ("phase", "task_type", "transfer_method", "exchange", "fix_category")


def qcsv(s: str) -> str:
    """Quote a column value only if it contains `,` or `"`."""
    if "," in s or '"' in s:
        return '"' + s.replace('"', '""') + '"'
    return s


def qalways(s: str) -> str:
    """Always-quote a column value (for error_patterns_joined)."""
    return '"' + s.replace('"', '""') + '"'


def validate(meta: dict) -> str | None:
    """Return a human-readable reason string if invalid, else None."""
    missing: list[str] = []
    for k in REQUIRED_TOP:
        v = meta.get(k)
        if v is None or (isinstance(v, str) and not v.strip()):
            missing.append(k)
    sig = meta.get("signature")
    if not isinstance(sig, dict):
        missing.append("signature")
    else:
        for k in REQUIRED_SIG:
            v = sig.get(k)
            if v is None or (isinstance(v, str) and not v.strip()):
                missing.append(f"signature.{k}")
    if missing:
        return "missing fields: " + " ".join(missing)
    return None


def row_for(meta: dict) -> str:
    sig = meta["signature"]
    patterns = sig.get("error_patterns") or []
    if not isinstance(patterns, list):
        patterns = []
    joined = "|".join(str(p) for p in patterns)
    last_seen = meta["last_seen"]
    # YAML may parse YYYY-MM-DD into a date object; stringify deterministically.
    last_seen_str = (
        last_seen.isoformat() if hasattr(last_seen, "isoformat") else str(last_seen)
    )
    return ", ".join(
        [
            qcsv(str(meta["id"])),
            qcsv(str(meta["title"])),
            qcsv(str(sig["phase"])),
            qcsv(str(sig["task_type"])),
            qcsv(str(sig["transfer_method"])),
            qcsv(str(sig["exchange"])),
            qcsv(str(sig["fix_category"])),
            qalways(joined),
            qcsv(last_seen_str),
        ]
    )


def main() -> int:
    if not PLAYBOOK_DIR.is_dir():
        print(f"rebuild-playbook-index: {PLAYBOOK_DIR} not found", file=sys.stderr)
        return 1

    rows: list[str] = []
    malformed: list[str] = []

    for md in sorted(PLAYBOOK_DIR.glob("*.md")):
        if md.name == "README.md":
            continue
        try:
            post = frontmatter.load(md)
        except Exception as e:
            malformed.append(f"MALFORMED: {md} — frontmatter parse error: {e}")
            continue
        if not post.metadata:
            malformed.append(f"MALFORMED: {md} — frontmatter missing or unterminated")
            continue
        reason = validate(post.metadata)
        if reason:
            malformed.append(f"MALFORMED: {md} — {reason}")
            continue
        rows.append(row_for(post.metadata))

    if malformed:
        for line in malformed:
            print(line, file=sys.stderr)
        return 2

    rows.sort()

    try:
        fd, tmp_path = tempfile.mkstemp(prefix="playbook-index.", dir=str(PLAYBOOK_DIR))
    except OSError as e:
        print(f"rebuild-playbook-index: tempfile creation failed: {e}", file=sys.stderr)
        return 1

    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(HEADER + "\n")
            for r in rows:
                f.write(r + "\n")
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, INDEX_FILE)
    except OSError as e:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        print(f"rebuild-playbook-index: write failed: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
