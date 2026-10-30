# CodeAtlas backend: FastAPI + indexing + agents.  Build from the repo root:
#   docker build -f backend.Dockerfile -t codeatlas-backend .
FROM python:3.11.9-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_NATIVE_TLS=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

# git: cloning/blame (repos are never executed); ripgrep: grep_agent search
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ripgrep ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY docker/certs/ /usr/local/share/ca-certificates/extra/
RUN update-ca-certificates

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /bin/uv

WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock backend/.python-version ./
RUN uv sync --frozen --no-dev --extra tracing --no-install-project

COPY backend/app ./app
# Published eval results are served by GET /results
COPY eval/results /app/eval/results

RUN useradd --create-home --uid 10001 codeatlas && mkdir -p /data && chown codeatlas /data
USER codeatlas
ENV DATA_DIR=/data PATH="/app/backend/.venv/bin:$PATH"
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
