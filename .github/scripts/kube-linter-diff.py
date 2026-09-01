#!/usr/bin/env python3
"""Report kube-linter findings this PR *introduces*, ignoring the pre-existing backlog.

kube-linter runs against the whole rendered tree, which is the only way to see inside
the ~20 third-party HelmReleases whose values schema nothing in this repo can model.
But the tree predates docs/manifest-conventions.md and currently carries a backlog of
findings, so a whole-tree gate would fail every unrelated PR.

So: render and lint twice — the PR and its merge base — and blame the PR only for
findings that appear in one and not the other. This is the same shape as the baseline
deploy-canary.yml takes against Gatus, for the same reason: report the transition, not
the state.

    kube-linter-diff.py base.json head.json > comment.md

Exit status is 1 if the PR introduces a finding, 0 otherwise.
"""

import json
import sys
from pathlib import Path

DOC_URL = (
    "https://github.com/mpdavis/homelab/blob/main/docs/manifest-conventions.md"
)


def findings(path):
    """{key: report} for one kube-linter JSON report.

    The key deliberately excludes Object.Metadata.FilePath: the base tree is rendered
    into a different directory than the PR tree, so every path differs and every
    finding would look new. Namespace/Kind/Name/message identify a finding across two
    renders of the same object.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8") or "{}")
    out = {}
    for r in data.get("Reports") or []:
        obj = (r.get("Object") or {}).get("K8sObject") or {}
        gvk = obj.get("GroupVersionKind") or {}
        key = (
            r.get("Check", ""),
            obj.get("Namespace", ""),
            gvk.get("Kind", ""),
            obj.get("Name", ""),
            (r.get("Diagnostic") or {}).get("Message", ""),
        )
        out[key] = r
    return out


def render(new, fixed):
    lines = []
    if new:
        lines.append("## ❌ New manifest hygiene findings\n")
        lines.append(
            f"This PR introduces {len(new)} finding(s) against the workload baseline in "
            f"[`docs/manifest-conventions.md`]({DOC_URL}). The tree's pre-existing "
            "findings are excluded — only what changed here is reported.\n"
        )
        by_check = {}
        for key in sorted(new):
            by_check.setdefault(key[0], []).append(key)
        for check, keys in sorted(by_check.items()):
            lines.append(f"### `{check}`\n")
            remediation = new[keys[0]].get("Remediation", "")
            for check_name, ns, kind, name, message in keys:
                where = f"{kind}/{name}" + (f" (ns `{ns}`)" if ns else "")
                lines.append(f"- **{where}** — {message}")
            if remediation:
                lines.append(f"\n> {remediation}\n")
        lines.append(
            "\nTo take a documented exception, annotate the object — the annotation "
            "survives Helm rendering, which a YAML comment does not:\n"
        )
        lines.append("```yaml")
        lines.append("metadata:")
        lines.append("  annotations:")
        lines.append(
            f'    ignore-check.kube-linter.io/{sorted(by_check)[0]}: '
            '"why this workload is different"'
        )
        lines.append("```")
    if fixed:
        lines.append(f"\n## ✅ Also fixed {len(fixed)} pre-existing finding(s)\n")
        for check, ns, kind, name, message in sorted(fixed):
            where = f"{kind}/{name}" + (f" (ns `{ns}`)" if ns else "")
            lines.append(f"- `{check}` — **{where}** — {message}")
    return "\n".join(lines)


def main():
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    base, head = findings(sys.argv[1]), findings(sys.argv[2])

    new_keys = head.keys() - base.keys()
    fixed_keys = base.keys() - head.keys()
    new = {k: head[k] for k in new_keys}

    body = render(new, fixed_keys)
    if body:
        print(body)

    print(
        f"base={len(base)} head={len(head)} new={len(new)} fixed={len(fixed_keys)}",
        file=sys.stderr,
    )
    return 1 if new else 0


if __name__ == "__main__":
    sys.exit(main())
