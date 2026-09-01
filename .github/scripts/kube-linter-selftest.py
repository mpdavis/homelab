#!/usr/bin/env python3
"""Assert every check configured in .kube-linter.yaml actually fires.

kube-linter exits 0 with `Reports: null` when it resolves no objects, and it drops a
renamed or mistyped check from the config without complaining. Either failure turns the
hygiene gate into something that passes everything while looking healthy — the worst
possible state for a check, because it is trusted.

So before trusting a clean run, lint a fixture that violates every configured rule and
assert the findings cover them all:

    kube-linter lint --config .kube-linter.yaml --format json \
        .github/tests/kube-linter-canary.yaml > canary.json
    kube-linter-selftest.py .kube-linter.yaml canary.json

Exit status is 1 if any configured check failed to fire.
"""

import json
import sys
from pathlib import Path

import yaml


def configured_checks(config_path):
    """Check names the config turns on: `checks.include` plus every custom check."""
    cfg = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    names = set((cfg.get("checks") or {}).get("include") or [])
    names |= {
        c["name"] for c in (cfg.get("customChecks") or []) if isinstance(c, dict) and c.get("name")
    }
    return names


def fired_checks(report_path):
    data = json.loads(Path(report_path).read_text(encoding="utf-8") or "{}")
    return {r.get("Check") for r in (data.get("Reports") or [])}


def main():
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2

    configured = configured_checks(sys.argv[1])
    fired = fired_checks(sys.argv[2])

    if not configured:
        print("FAIL: .kube-linter.yaml enables no checks at all.", file=sys.stderr)
        return 1

    silent = configured - fired
    unexpected = fired - configured

    for name in sorted(configured & fired):
        print(f"  ok      {name}")
    for name in sorted(silent):
        print(f"  SILENT  {name}", file=sys.stderr)
    for name in sorted(unexpected):
        # Not fatal: kube-linter reporting a check the config did not ask for would be
        # surprising, but it is not the failure this guard exists to catch.
        print(f"  extra   {name} (fired but not in the config)", file=sys.stderr)

    if silent:
        print(
            f"\nFAIL: {len(silent)} configured check(s) produced no finding against "
            f"{sys.argv[2]}.\nEither the canary fixture no longer violates them, or the "
            "check name in .kube-linter.yaml is wrong and kube-linter silently ignored it.",
            file=sys.stderr,
        )
        return 1

    print(f"\nAll {len(configured)} configured checks fired.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
