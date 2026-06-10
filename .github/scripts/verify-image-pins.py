#!/usr/bin/env python3
"""Verify that container image references actually exist in their registry.

Catches the class of bug where a manifest pins an image to a tag or digest that
the registry does not publish (e.g. `teamarr:v2.6.0` when the published tag is
`2.6.0`), which only surfaces at runtime as ImagePullBackOff.

Two modes:

  # Check every `image:` introduced by a PR (added lines under kubernetes/)
  verify-image-pins.py --changed <base_sha>

  # Check explicit references (handy for local testing)
  verify-image-pins.py ghcr.io/pharaoh-labs/teamarr:2.6.0 linuxserver/radarr:6.2.1.10461-ls305

Exit status is non-zero if any reference cannot be resolved.

Existence is checked against the OCI/Docker registry HTTP API. Bearer-token
auth is negotiated generically from the WWW-Authenticate challenge, so any
standards-compliant registry works (ghcr.io, Docker Hub, quay.io, lscr.io, ...)
without per-registry special-casing.
"""

import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

DOCKER_HUB = "registry-1.docker.io"
# Hostnames that are really Docker Hub under a friendlier name.
DOCKER_HUB_ALIASES = {"docker.io", "index.docker.io", "registry.hub.docker.com"}

MANIFEST_ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)

# Matches `image: <ref>` allowing optional quotes; ignores commented lines.
IMAGE_LINE = re.compile(r"""^\s*image:\s*["']?([^"'\s#]+)["']?""")


def parse_ref(ref):
    """Split an image reference into (registry, repository, reference, is_digest)."""
    digest = None
    name = ref
    if "@" in name:
        name, digest = name.split("@", 1)

    if "/" in name:
        first, rest = name.split("/", 1)
        # A leading component is a registry only if it looks like a host.
        if "." in first or ":" in first or first == "localhost":
            registry, remainder = first, rest
        else:
            registry, remainder = DOCKER_HUB, name
    else:
        registry, remainder = DOCKER_HUB, name

    if registry in DOCKER_HUB_ALIASES:
        registry = DOCKER_HUB

    if ":" in remainder:
        repo, tag = remainder.rsplit(":", 1)
    else:
        repo, tag = remainder, "latest"

    # Docker Hub official images live under the implicit `library/` namespace.
    if registry == DOCKER_HUB and "/" not in repo:
        repo = "library/" + repo

    reference = digest if digest else tag
    return registry, repo, reference, digest is not None


def parse_www_authenticate(header):
    """Pull realm/service/scope out of a `Bearer realm="...",service="..."` challenge."""
    fields = dict(re.findall(r'(\w+)="([^"]*)"', header))
    return fields.get("realm"), fields.get("service"), fields.get("scope")


def fetch_token(realm, service, scope):
    url = realm
    params = []
    if service:
        params.append(f"service={urllib.parse.quote(service, safe='')}")
    if scope:
        params.append(f"scope={urllib.parse.quote(scope, safe='')}")
    if params:
        url = f"{realm}?{'&'.join(params)}"
    with urllib.request.urlopen(url, timeout=20) as resp:
        import json

        data = json.load(resp)
        return data.get("token") or data.get("access_token")


def manifest_request(registry, repo, reference, token=None):
    url = f"https://{registry}/v2/{repo}/manifests/{reference}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Accept", MANIFEST_ACCEPT)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    return urllib.request.urlopen(req, timeout=20)


def exists(registry, repo, reference):
    """Return (ok, detail) for whether the manifest resolves."""
    try:
        manifest_request(registry, repo, reference)
        return True, "200"
    except urllib.error.HTTPError as e:
        if e.code != 401:
            return False, f"HTTP {e.code}"
        challenge = e.headers.get("WWW-Authenticate", "")
    except urllib.error.URLError as e:
        return False, f"network error: {e.reason}"

    realm, service, scope = parse_www_authenticate(challenge)
    if not realm:
        return False, "HTTP 401 (no auth challenge to satisfy)"
    try:
        token = fetch_token(realm, service, scope)
        manifest_request(registry, repo, reference, token=token)
        return True, "200"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return False, f"network error: {e.reason}"


def refs_from_diff(base_sha):
    """Image references on lines added under kubernetes/ relative to base_sha."""
    diff = subprocess.run(
        ["git", "diff", f"{base_sha}...HEAD", "--", "kubernetes/**"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    refs = []
    for line in diff.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        m = IMAGE_LINE.match(line[1:])
        if m:
            refs.append(m.group(1))
    # De-dup while preserving order.
    return list(dict.fromkeys(refs))


def main(argv):
    if len(argv) >= 2 and argv[1] == "--changed":
        if len(argv) < 3:
            print("usage: verify-image-pins.py --changed <base_sha>", file=sys.stderr)
            return 2
        refs = refs_from_diff(argv[2])
    else:
        refs = argv[1:]

    if not refs:
        print("No image references to verify.")
        return 0

    failures = []
    for ref in refs:
        registry, repo, reference, is_digest = parse_ref(ref)
        ok, detail = exists(registry, repo, reference)
        kind = "digest" if is_digest else "tag"
        mark = "OK  " if ok else "FAIL"
        print(f"{mark}  {ref}  ({kind} -> {detail})")
        if not ok:
            failures.append((ref, detail))

    print()
    if failures:
        print(f"{len(failures)} image reference(s) do not resolve in their registry:")
        for ref, detail in failures:
            print(f"  - {ref}  ({detail})")
        print(
            "\nA pinned tag or digest that the registry does not publish will "
            "ImagePullBackOff at runtime. Fix the reference to match a published "
            "tag (check the registry's tag list — e.g. some repos publish `2.6.0`, "
            "not `v2.6.0`)."
        )
        return 1

    print(f"All {len(refs)} image reference(s) resolve. ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
