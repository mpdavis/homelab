# Ames city council digest: pulls agendas and council packets from the city's
# public Laserfiche WebLink repository, summarizes them through an
# Anthropic-compatible model gateway (opencode zen by default), and emits a
# short digest. Built by .github/workflows/build-ames-council-digest.yml and
# deployed as a CronJob from kubernetes/apps/civic/ames-council-digest/.
FROM python:3.13-slim-bookworm

# ca-certificates is the only runtime OS dependency — every fetch is HTTPS.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies resolve from pyproject alone, so this layer survives edits to
# the package source.
COPY pyproject.toml README.md ./
COPY ames_digest/__init__.py ./ames_digest/
RUN pip install --no-cache-dir .

COPY ames_digest/ ./ames_digest/
RUN pip install --no-cache-dir --no-deps .

# The CronJob mounts a PVC at /data; running as a fixed non-root uid lets the
# pod's fsGroup make it writable.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin digest \
    && mkdir -p /data/state /data/digests \
    && chown -R 1000:1000 /data
USER 1000

VOLUME ["/data"]

ENTRYPOINT ["ames-digest"]
CMD ["run"]
