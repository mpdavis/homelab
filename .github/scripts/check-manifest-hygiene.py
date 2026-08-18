#!/usr/bin/env python3
"""Enforce the workload conventions in docs/manifest-conventions.md.

These are the findings the LLM reviewer kept re-discovering one new service at a
time: containers with no uid/gid pinned, `PUID`/`PGID` set on an image that never
reads them, missing container hardening, missing resource requests, floating image
tags, and a new hostname that never got registered with Gatus or Homepage. All of
them are mechanically decidable, so they should not cost a review round-trip.

Two modes:

  # Check manifests a PR ADDS (the new-service case) -- what CI runs
  check-manifest-hygiene.py --changed <base_sha>

  # Audit specific files, or the whole tree, locally
  check-manifest-hygiene.py kubernetes/apps/media/listenarr/helmrelease.yaml
  check-manifest-hygiene.py --all

CI deliberately scopes to *added* files. The tree predates these conventions and a
whole-tree gate would fail every unrelated PR; the rules apply going forward, and
`--all` is there for when you want to work the backlog down on purpose.

Exit status is non-zero if any error-level finding survives exemptions.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

import yaml

DOC = "docs/manifest-conventions.md"
REPO_ROOT = Path(__file__).resolve().parents[2]

GATUS = "kubernetes/infrastructure/controllers/gatus.yaml"
HOMEPAGE_SERVICES = "kubernetes/apps/homepage/homepage/config/services.yaml"
DOMAIN = "mpdavis.com"
AUTH_MIDDLEWARE = "authentik-forward-auth"

# Tags that move under you. A digest-pinned reference is fine even with one of
# these as the tag, because the digest is what actually gets deployed.
FLOATING_TAGS = {
    "latest", "edge", "stable", "main", "master",
    "develop", "development", "nightly", "rolling", "release",
}

LINUXSERVER = re.compile(r"(^|/)(lscr\.io/)?linuxserver/", re.IGNORECASE)

# `# hygiene-exempt: <rule-id> <reason>` -- the reason is mandatory. An exemption
# without one is just a silent omission wearing a comment.
EXEMPT = re.compile(r"#\s*hygiene-exempt:\s*([a-z0-9-]+)[ \t]+(\S.*)$", re.MULTILINE)
EXEMPT_BARE = re.compile(r"#\s*hygiene-exempt:\s*([a-z0-9-]+)\s*$", re.MULTILINE)

HOST_RULE = re.compile(r"Host\(`([^`]+)`\)")

POD_TEMPLATE_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "ReplicaSet"}

# Rule id -> the section of docs/manifest-conventions.md that explains it.
RULE_DOC = {
    "floating-tag": "4. Image pinning",
    "runtime-identity": "1. Runtime identity",
    "puid-noop": "1. Runtime identity",
    "linuxserver-conflict": "1. Runtime identity",
    "container-hardening": "2. Container hardening",
    "readonly-rootfs": "2. Container hardening",
    "resources": "3. Resource requests and limits",
    "gatus-endpoint": "6. Monitoring registration",
    "gatus-hostalias": "6. Monitoring registration",
    "gatus-group": "6. Monitoring registration",
    "homepage-tile": "6. Monitoring registration",
}


class Finding:
    def __init__(self, level, rule, path, where, message, fix=None):
        self.level = level  # "error" | "warn"
        self.rule = rule
        self.path = path
        self.where = where
        self.message = message
        self.fix = fix

    def render(self):
        mark = "ERROR" if self.level == "error" else "warn "
        out = [f"{mark}  [{self.rule}]  {self.path}"]
        out.append(f"         {self.where}: {self.message}")
        if self.fix:
            for line in self.fix.splitlines():
                out.append(f"         {line}")
        return "\n".join(out)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def load_yaml_docs(path):
    """Every YAML document in a file, plus the raw text (for exemption markers)."""
    text = path.read_text(encoding="utf-8")
    try:
        docs = [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]
    except yaml.YAMLError as e:
        return None, text, e
    return docs, text, None


def exemptions(text):
    """Rule ids exempted in this file, and bare markers that were rejected."""
    granted = {rule for rule, _reason in EXEMPT.findall(text)}
    bare = {r for r in EXEMPT_BARE.findall(text)} - granted
    return granted, bare


def parse_image(ref):
    """(repository, tag, has_digest) from an image string."""
    if not isinstance(ref, str):
        return None, None, False
    has_digest = "@sha256:" in ref
    name = ref.split("@", 1)[0]
    # A colon in the final path segment is the tag; a colon earlier is a port.
    if ":" in name.rsplit("/", 1)[-1]:
        repo, tag = name.rsplit(":", 1)
    else:
        repo, tag = name, None
    return repo, tag, has_digest


def env_names(env):
    """Env var names from either the app-template map form or the k8s list form."""
    if isinstance(env, dict):
        return set(env.keys())
    if isinstance(env, list):
        return {e.get("name") for e in env if isinstance(e, dict)}
    return set()


class Container:
    def __init__(self, name, spec, image_ref):
        self.name = name
        self.spec = spec or {}
        repo, tag, digest = parse_image(image_ref)
        self.repo, self.tag, self.has_digest = repo, tag, digest
        self.raw_image = image_ref

    @property
    def is_linuxserver(self):
        return bool(self.repo and LINUXSERVER.search(self.repo))

    @property
    def security_context(self):
        return self.spec.get("securityContext") or {}

    @property
    def resources(self):
        return self.spec.get("resources") or {}

    @property
    def env(self):
        return env_names(self.spec.get("env"))


class Workload:
    """A pod spec plus its containers, normalized across manifest shapes."""

    def __init__(self, where, pod_security_context, containers, chart_managed=False):
        self.where = where
        self.pod_sc = pod_security_context or {}
        self.containers = containers
        self.chart_managed = chart_managed


def workloads_from_doc(doc, path):
    """Every workload a manifest document describes, or [] if it describes none."""
    kind = doc.get("kind")
    name = (doc.get("metadata") or {}).get("name", "?")

    if kind in POD_TEMPLATE_KINDS:
        pod = ((doc.get("spec") or {}).get("template") or {}).get("spec") or {}
        return [_from_pod_spec(f"{kind}/{name}", pod)]

    if kind == "CronJob":
        job = (doc.get("spec") or {}).get("jobTemplate") or {}
        pod = ((job.get("spec") or {}).get("template") or {}).get("spec") or {}
        return [_from_pod_spec(f"{kind}/{name}", pod)]

    if kind == "Pod":
        return [_from_pod_spec(f"Pod/{name}", doc.get("spec") or {})]

    if kind == "HelmRelease":
        return _from_helmrelease(doc, name)

    return []


def _from_pod_spec(where, pod):
    containers = []
    for key in ("initContainers", "containers"):
        for c in pod.get(key) or []:
            if not isinstance(c, dict):
                continue
            label = c.get("name", "?")
            if key == "initContainers":
                label = f"init:{label}"
            containers.append(Container(label, c, c.get("image")))
    return Workload(where, pod.get("securityContext"), containers)


def _from_helmrelease(doc, name):
    spec = doc.get("spec") or {}
    chart = ((spec.get("chart") or {}).get("spec") or {}).get("chart")
    values = spec.get("values") or {}

    if chart != "app-template":
        # A third-party chart's values schema is its own; we cannot reason about
        # where its securityContext lives. Surface the images we can see (so the
        # floating-tag rule still applies) and skip the structural rules.
        return [
            Workload(
                f"HelmRelease/{name} (chart: {chart})",
                None,
                _chart_images(values),
                chart_managed=True,
            )
        ]

    default_pod = values.get("defaultPodOptions") or {}
    out = []
    for cname, controller in (values.get("controllers") or {}).items():
        if not isinstance(controller, dict):
            continue
        # A per-controller `pod:` block overrides defaultPodOptions.
        pod_opts = controller.get("pod") or {}
        pod_sc = pod_opts.get("securityContext", default_pod.get("securityContext"))

        containers = []
        for kname, c in (controller.get("containers") or {}).items():
            if not isinstance(c, dict):
                continue
            img = c.get("image") or {}
            ref = None
            if isinstance(img, dict) and img.get("repository"):
                tag = img.get("tag")
                ref = f"{img['repository']}:{tag}" if tag else str(img["repository"])
            elif isinstance(img, str):
                ref = img
            containers.append(Container(f"{cname}/{kname}", c, ref))
        out.append(Workload(f"HelmRelease/{name} controller {cname}", pod_sc, containers))
    return out


def _chart_images(node, prefix="values"):
    """Best-effort image references inside an arbitrary chart's values."""
    found = []
    if isinstance(node, dict):
        img = node.get("image")
        if isinstance(img, dict) and img.get("repository"):
            tag = img.get("tag")
            ref = f"{img['repository']}:{tag}" if tag else str(img["repository"])
            found.append(Container(prefix, {}, ref))
        elif isinstance(img, str):
            found.append(Container(prefix, {}, img))
        for k, v in node.items():
            if k != "image":
                found.extend(_chart_images(v, f"{prefix}.{k}"))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            found.extend(_chart_images(v, f"{prefix}[{i}]"))
    return found


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------


def check_workload(w, rel, granted, findings):
    for c in w.containers:
        _check_image(c, w, rel, granted, findings)
        if w.chart_managed:
            continue
        _check_identity(c, w, rel, granted, findings)
        _check_hardening(c, w, rel, granted, findings)
        _check_resources(c, w, rel, granted, findings)


def _add(findings, granted, level, rule, rel, where, message, fix=None):
    if rule in granted:
        return
    findings.append(Finding(level, rule, rel, where, message, fix))


def _check_image(c, w, rel, granted, findings):
    if not c.repo:
        return
    if c.has_digest:
        return
    if c.tag is None:
        _add(findings, granted, "error", "floating-tag", rel, f"{w.where} / {c.name}",
             f"`{c.raw_image}` has no tag, so it resolves to :latest.",
             "Pin an immutable tag or a digest.")
        return
    if c.tag.lower() in FLOATING_TAGS:
        _add(findings, granted, "error", "floating-tag", rel, f"{w.where} / {c.name}",
             f"`{c.raw_image}` is pinned to the floating tag `{c.tag}`.",
             "Pin a version tag, or append a digest (`:latest@sha256:...`) so "
             "Renovate can propose a reviewable bump.")


def _check_identity(c, w, rel, granted, findings):
    pod_sc = w.pod_sc
    has_puid = bool({"PUID", "PGID"} & c.env)
    pins_user = "runAsUser" in pod_sc or "runAsUser" in c.security_context
    drops_all = _drops_all(c.security_context) or _drops_all(pod_sc)

    if c.is_linuxserver:
        if not has_puid:
            _add(findings, granted, "error", "runtime-identity", rel,
                 f"{w.where} / {c.name}",
                 "LinuxServer image with no PUID/PGID -- s6 will run the app as root.",
                 'Add `PUID: "1000"` and `PGID: "1000"` to env.')
        if pins_user:
            _add(findings, granted, "error", "linuxserver-conflict", rel,
                 f"{w.where} / {c.name}",
                 "LinuxServer image with runAsUser set -- s6-overlay must start as "
                 "root and will fail its init scripts.",
                 "Drop runAsUser and let PUID/PGID do the de-escalation.")
        if drops_all:
            _add(findings, granted, "error", "linuxserver-conflict", rel,
                 f"{w.where} / {c.name}",
                 "LinuxServer image with capabilities.drop: [ALL] -- breaks s6 init.",
                 "Keep `allowPrivilegeEscalation: false`; drop the capabilities block.")
        return

    if has_puid:
        _add(findings, granted, "error", "puid-noop", rel, f"{w.where} / {c.name}",
             f"PUID/PGID set on `{c.repo}`, which is not a LinuxServer image -- "
             "nothing reads them, so the container silently runs as the image's uid.",
             "Remove them and pin the uid via the pod securityContext instead:\n"
             "  defaultPodOptions.securityContext: {runAsUser: 1000, "
             "runAsGroup: 1000, fsGroup: 1000}")

    if not pins_user and not pod_sc.get("runAsNonRoot"):
        _add(findings, granted, "error", "runtime-identity", rel,
             f"{w.where} / {c.name}",
             "No pod-level runAsUser/runAsNonRoot -- the container runs as whatever "
             "uid the image baked in, often root.",
             "Add a pod securityContext with runAsUser/runAsGroup (and fsGroup for "
             "an RWO volume, or supplementalGroups for NFS / read-only mounts).")


def _drops_all(sc):
    caps = (sc or {}).get("capabilities") or {}
    drop = caps.get("drop") or []
    return any(str(d).upper() == "ALL" for d in drop)


def _check_hardening(c, w, rel, granted, findings):
    sc = c.security_context
    if sc.get("allowPrivilegeEscalation") is not False:
        _add(findings, granted, "error", "container-hardening", rel,
             f"{w.where} / {c.name}",
             "Container securityContext is missing `allowPrivilegeEscalation: false`.",
             "Safe on every image family, including LinuxServer.")
    if not c.is_linuxserver and not _drops_all(sc):
        _add(findings, granted, "error", "container-hardening", rel,
             f"{w.where} / {c.name}",
             "Container securityContext is missing `capabilities.drop: [ALL]`.",
             "Add an explicit `add:` alongside it if the app genuinely needs a "
             "capability (see gluetun/NET_ADMIN).")
    if sc.get("readOnlyRootFilesystem") is not True:
        _add(findings, granted, "warn", "readonly-rootfs", rel,
             f"{w.where} / {c.name}",
             "No `readOnlyRootFilesystem: true`.",
             "Most apps only need an emptyDir at /tmp. If this one writes into its "
             "install tree, say so with a `# hygiene-exempt: readonly-rootfs <reason>` "
             "comment.")


def _check_resources(c, w, rel, granted, findings):
    res = c.resources
    req = res.get("requests") or {}
    lim = res.get("limits") or {}
    missing = [f"requests.{k}" for k in ("cpu", "memory") if k not in req]
    if "memory" not in lim:
        missing.append("limits.memory")
    if missing:
        _add(findings, granted, "error", "resources", rel, f"{w.where} / {c.name}",
             f"Missing {', '.join(missing)}.",
             "Homelab-scale numbers are fine -- a declared 10m/32Mi is the point. "
             "Skip limits.cpu unless you want a deliberate ceiling.")


# --------------------------------------------------------------------------
# Monitoring registration
# --------------------------------------------------------------------------


def load_monitoring():
    """Hostnames Gatus probes, hostnames it can resolve, and Homepage tiles."""
    gatus = REPO_ROOT / GATUS
    endpoints, aliases, groups = set(), set(), {}
    if gatus.exists():
        text = gatus.read_text(encoding="utf-8")
        # The endpoint list and the hostAliases patch live in the same file, one
        # as values and one inside a kustomize patch string -- match on text so
        # both are covered without modelling the patch.
        for host in re.findall(r"https://([a-z0-9.-]+\." + re.escape(DOMAIN) + r")", text):
            endpoints.add(host)
        for host in re.findall(r"-\s+([a-z0-9-]+\." + re.escape(DOMAIN) + r")\s*$",
                               text, re.MULTILINE):
            aliases.add(host)
        try:
            doc = yaml.safe_load(text) or {}
            cfg = (((doc.get("spec") or {}).get("values") or {}).get("config") or {})
            for ep in cfg.get("endpoints") or []:
                m = re.match(r"https://([a-z0-9.-]+)", str(ep.get("url", "")))
                if m:
                    groups[m.group(1)] = ep.get("group")
        except yaml.YAMLError:
            pass

    tiles = set()
    hp = REPO_ROOT / HOMEPAGE_SERVICES
    if hp.exists():
        text = hp.read_text(encoding="utf-8")
        for host in re.findall(r"https://([a-z0-9-]+)\.\$\{DOMAIN\}", text):
            tiles.add(f"{host}.{DOMAIN}")
        for host in re.findall(r"https://([a-z0-9-]+\." + re.escape(DOMAIN) + r")", text):
            tiles.add(host)
    return endpoints, aliases, groups, tiles


def check_ingressroute(doc, rel, granted, findings, monitoring):
    endpoints, aliases, groups, tiles = monitoring
    spec = doc.get("spec") or {}
    for route in spec.get("routes") or []:
        if not isinstance(route, dict):
            continue
        behind_auth = any(
            (m or {}).get("name") == AUTH_MIDDLEWARE
            for m in route.get("middlewares") or []
            if isinstance(m, dict)
        )
        for host in HOST_RULE.findall(str(route.get("match", ""))):
            host = host.replace("${DOMAIN}", DOMAIN)
            if not host.endswith(DOMAIN):
                continue
            where = f"Host({host})"
            if host not in endpoints:
                _add(findings, granted, "error", "gatus-endpoint", rel, where,
                     f"No Gatus endpoint probes {host}.",
                     f"Add a `config.endpoints` entry in {GATUS}. Without it the "
                     "service is invisible to deploy-canary and GatusEndpointDown.")
            if host not in aliases:
                _add(findings, granted, "error", "gatus-hostalias", rel, where,
                     f"{host} is not in the Gatus `hostAliases` patch.",
                     f"Add it to the postRenderers patch in {GATUS} -- in-cluster "
                     "probes resolve *.mpdavis.com via the Traefik VIP, not DNS.")
            expected = "external-auth" if behind_auth else "external-open"
            actual = groups.get(host)
            if actual and actual != expected:
                _add(findings, granted, "error", "gatus-group", rel, where,
                     f"IngressRoute is {'behind' if behind_auth else 'not behind'} "
                     f"{AUTH_MIDDLEWARE}, so the Gatus group should be `{expected}`, "
                     f"not `{actual}`.",
                     "An open service asserts 200; a protected one asserts a 302 to "
                     "iam.mpdavis.com. The wrong group hides a missing middleware.")
            if host not in tiles:
                _add(findings, granted, "warn", "homepage-tile", rel, where,
                     f"No Homepage tile links to {host}.",
                     f"Add one to {HOMEPAGE_SERVICES}. Exempt with "
                     "`# hygiene-exempt: homepage-tile <reason>` if the service is "
                     "machine-facing.")


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------


def added_files(base_sha):
    out = subprocess.run(
        ["git", "diff", "--diff-filter=A", "--name-only", f"{base_sha}...HEAD",
         "--", "kubernetes/"],
        capture_output=True, text=True, check=True,
    ).stdout
    return [p for p in out.splitlines() if p.endswith((".yaml", ".yml"))]


def all_files():
    return sorted(
        str(p.relative_to(REPO_ROOT))
        for p in (REPO_ROOT / "kubernetes").rglob("*.y*ml")
    )


def check_file(rel, monitoring, findings):
    path = REPO_ROOT / rel
    if not path.exists():
        return
    docs, text, err = load_yaml_docs(path)
    if err is not None:
        findings.append(Finding("error", "parse", rel, "file",
                                f"YAML does not parse: {err}"))
        return
    granted, bare = exemptions(text)
    for rule in sorted(bare):
        findings.append(Finding(
            "error", "exempt-no-reason", rel, "file",
            f"`hygiene-exempt: {rule}` has no reason after it.",
            "An exemption is a decision on the record -- write down why."))
    for rule in sorted(granted):
        if rule not in RULE_DOC:
            findings.append(Finding(
                "warn", "exempt-unknown", rel, "file",
                f"`hygiene-exempt: {rule}` names no known rule (typo?)."))
    for doc in docs:
        if doc.get("kind") == "IngressRoute":
            check_ingressroute(doc, rel, granted, findings, monitoring)
        for w in workloads_from_doc(doc, rel):
            check_workload(w, rel, granted, findings)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--changed", metavar="BASE_SHA",
                    help="check manifests added relative to BASE_SHA")
    ap.add_argument("--all", action="store_true",
                    help="audit every manifest under kubernetes/")
    ap.add_argument("--strict", action="store_true",
                    help="treat warnings as errors")
    ap.add_argument("files", nargs="*", help="explicit files to check")
    args = ap.parse_args(argv)

    if args.changed:
        files = added_files(args.changed)
    elif args.all:
        files = all_files()
    else:
        files = args.files
    if not files:
        print("No added manifests to check.")
        return 0

    monitoring = load_monitoring()
    findings = []
    for rel in files:
        check_file(rel, monitoring, findings)

    errors = [f for f in findings if f.level == "error"]
    warns = [f for f in findings if f.level == "warn"]
    if args.strict:
        errors, warns = errors + warns, []

    print(f"Checked {len(files)} manifest(s) against {DOC}.\n")
    for f in errors + warns:
        print(f.render())
        print()

    if errors:
        rules = sorted({f.rule for f in errors})
        print(f"{len(errors)} error(s) across rule(s): {', '.join(rules)}")
        for r in rules:
            if r in RULE_DOC:
                print(f"  {r:22} -> {DOC} section {RULE_DOC[r]}")
        print("\nEvery rule has an escape hatch. If one genuinely does not apply, "
              "add a\n  # hygiene-exempt: <rule-id> <reason>\ncomment to the "
              "manifest and the check will honor it.")
        return 1

    if warns:
        print(f"{len(warns)} warning(s), no errors. ✅")
    else:
        print("All checked manifests meet the conventions. ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
