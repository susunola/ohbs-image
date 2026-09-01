# Multi-stage image for running scripts/check_readme.py (the CI
# README-freshness guard) in a clean, reproducible Python environment.
#
# Why: contributors can validate that README.md stays in sync with the CLI
# (subcommands + OS profiles) without relying on their local Python state.
# The image installs ohbs-image from a freshly built wheel, so the check runs
# against the real package surface.
#
# Usage:
#   docker build -t ohbs-image:check-readme .
#   # build succeeds only if README.md documents every subcommand + profile
#   # re-run against a modified checkout:
#   docker run --rm -v "$(pwd):/app" ohbs-image:check-readme

# --- stage 1: build the wheel -----------------------------------------------
FROM python:3.11-slim AS build
WORKDIR /src
COPY pyproject.toml MANIFEST.in README.md ./
COPY ohbs_image ./ohbs_image
RUN pip install --no-cache-dir build \
 && python -m build --wheel

# --- stage 2: runtime with the installed package + check script --------------
FROM python:3.11-slim AS check-readme
WORKDIR /app
COPY --from=build /src/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl

# The source files the check reads at runtime. Keep the scripts/ layout so
# check_readme.py resolves its default README path (repo root) correctly. Copy
# the whole scripts/ directory, not just check_readme.py: the gate scripts
# share helpers (scripts/_cli_introspection.py) that must resolve at import.
COPY README.md /app/README.md
COPY scripts/ /app/scripts/
# check_readme.py also guards counts declared in pyproject.toml and the
# profile list in ohbs_image/__init__.py, so those source files have to be
# present next to the installed package (the wheel alone is not enough).
# NB: do NOT copy ohbs_image/ here. `python3 -c` from /app puts the working
# directory on sys.path, so a partial source tree would shadow the installed
# wheel and break the packaged-artifact checks below.
COPY pyproject.toml /app/pyproject.toml

ENTRYPOINT ["python", "scripts/check_readme.py"]

# --- stage 3: zero-cost demo (`ohbs-image try`) -------------------------------
# The same wheel, but the container's job is the offline demo: engine +
# catalog gates plus a sample single-page HTML compliance report, written
# to a bind-mounted directory so the host gets the files with no spend.
#
# Usage:
#   docker build --target try -t ohbs-image:try .
#   docker run --rm -v "$(pwd)/out:/demo/out" ohbs-image:try
#   # → ./out/demo-report.html + demo-audit.json + ohbs-image.toml
FROM python:3.11-slim AS try
WORKDIR /demo
COPY --from=build /src/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl \
 && mkdir -p /demo/out
ENTRYPOINT ["ohbs-image", "try", "--output", "/demo/out"]

# --- production control-plane server ---------------------------------------
FROM python:3.11-slim AS server
RUN groupadd --system --gid 10001 ohbs-image \
 && useradd --system --uid 10001 --gid 10001 --home /var/lib/ohbs-image ohbs-image \
 && mkdir -p /var/lib/ohbs-image \
 && chown ohbs-image:ohbs-image /var/lib/ohbs-image
COPY --from=build /src/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl
USER 10001:10001
ENV OHBS_IMAGE_STATE_DIR=/var/lib/ohbs-image PYTHONDONTWRITEBYTECODE=1
EXPOSE 8181
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8181/healthz', timeout=2)"
ENTRYPOINT ["ohbs-image"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8181", "--rbac", "/run/secrets/rbac.json"]
