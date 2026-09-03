# Gridiron: college football betting research. Ingests game, drive and
# play-by-play history from CollegeFootballData plus live prices from The Odds
# API, builds hidden-yardage and prestige features into DuckDB, backtests
# theories walk-forward, and reports where a model disagrees with a book.
# Built by .github/workflows/build-gridiron.yml and deployed from
# kubernetes/apps/gridiron/gridiron/.
#
# Python 3.13 rather than 3.14: numpy, pandas and duckdb all publish manylinux
# wheels for it, so the image builds without a toolchain. On 3.14 at least one
# of the three falls back to a source build, which needs gcc in the image and
# turns a one-minute build into a fifteen-minute one.
FROM python:3.14-slim-bookworm

# ca-certificates is the only runtime OS dependency — every fetch is HTTPS.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    GRIDIRON_DATA_DIR=/data

WORKDIR /app

# Dependencies resolve from pyproject alone, so this layer survives edits to
# the package source.
COPY pyproject.toml README.md ./
COPY gridiron/__init__.py ./gridiron/
RUN pip install --no-cache-dir .

COPY gridiron/ ./gridiron/
RUN pip install --no-cache-dir --no-deps .

# The Deployment mounts a PVC at /data; running as a fixed non-root uid lets
# the pod's fsGroup make it writable. DuckDB writes its WAL alongside the
# database file, so the whole directory has to be writable, not just the file.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin gridiron \
    && mkdir -p /data \
    && chown -R 1000:1000 /data
USER 1000

VOLUME ["/data"]

EXPOSE 8080

# `serve` owns the database and runs ingest on an internal schedule. That is
# not a stylistic choice: DuckDB takes a single writer, so a separate ingest
# CronJob would be locked out of the file this process holds open. See
# gridiron/db.py and gridiron/scheduler.py.
ENTRYPOINT ["gridiron"]
CMD ["serve"]
