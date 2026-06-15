# Phase 2 — Playbook Lookup

> [!IMPORTANT]
> **SKIPPED — pending playbook population.**

The playbook is empty and `scripts/rebuild-playbook-index.py` is a template, not a real index, so there is nothing to score yet. The orchestrator **skips this phase entirely** and proceeds straight from Phase 1 to Phase 3, minting hypothesis #1 directly from the Phase-1 evidence (failed-part fields + failed phase + error-code resolution + account/instrument anomalies).

When the playbook is populated and re-enabled, this phase will score `playbook/index.toon` entries against the Phase-1 evidence into a ranked hypothesis queue. That ranking only **biases** the orchestrator's hypothesis selection in Phase 3 — it does not change the investigation-loop structure; the orchestrator still formulates and tests one hypothesis per iteration.

Full Phase-2 prose is authored when the playbook is populated.
