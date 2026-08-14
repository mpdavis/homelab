# Web-accessible coding agent: CloudCLI (claudecodeui) UI driving the claude and
# opencode CLIs, with the cluster/GitOps tooling (kubectl, flux, gh, git) the
# agent needs to debug the homelab. Built by .github/workflows/build-coding-agent.yml
# and deployed from kubernetes/apps/ai/coding-agent/.
FROM node:24-bookworm-slim

# renovate: datasource=npm depName=@cloudcli-ai/cloudcli
ARG CLOUDCLI_VERSION=1.37.1
# renovate: datasource=npm depName=@anthropic-ai/claude-code
ARG CLAUDE_CODE_VERSION=2.1.233
# renovate: datasource=npm depName=opencode-ai
ARG OPENCODE_VERSION=1.18.18
# renovate: datasource=github-releases depName=kubernetes/kubernetes
ARG KUBECTL_VERSION=v1.36.3
# renovate: datasource=github-releases depName=fluxcd/flux2
ARG FLUX_VERSION=v2.9.4
# renovate: datasource=github-releases depName=cli/cli
ARG GH_VERSION=v2.97.0

# python3/make/g++ serve double duty: node-gyp needs them when a native module
# (better-sqlite3, from cloudcli) has no prebuilt binary, and they're useful
# runtime tooling for a coding agent anyway.
RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl ca-certificates jq ripgrep less procps openssh-client \
      python3 make g++ \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
    && chmod +x /usr/local/bin/kubectl \
    && curl -fsSL "https://github.com/fluxcd/flux2/releases/download/${FLUX_VERSION}/flux_${FLUX_VERSION#v}_linux_amd64.tar.gz" \
       | tar -xz -C /usr/local/bin flux \
    && curl -fsSL "https://github.com/cli/cli/releases/download/${GH_VERSION}/gh_${GH_VERSION#v}_linux_amd64.tar.gz" \
       | tar -xz -C /tmp \
    && mv "/tmp/gh_${GH_VERSION#v}_linux_amd64/bin/gh" /usr/local/bin/gh \
    && rm -rf /tmp/gh_*

RUN npm install -g \
      "@cloudcli-ai/cloudcli@${CLOUDCLI_VERSION}" \
      "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
      "opencode-ai@${OPENCODE_VERSION}"

COPY entrypoint.sh /usr/local/bin/entrypoint.sh

# All state (~/.claude, opencode auth, repo clone, CloudCLI auth.db) lives in
# HOME, which is a PVC mount in the cluster. uid 1000 = the base image's "node"
# user; local-path provisions the volume world-writable and fsGroup covers the rest.
RUN chmod 0755 /usr/local/bin/entrypoint.sh \
    && mkdir -p /home/agent && chown 1000:1000 /home/agent
USER 1000:1000
ENV HOME=/home/agent \
    SERVER_PORT=3001 \
    HOST=0.0.0.0
WORKDIR /home/agent

EXPOSE 3001
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
