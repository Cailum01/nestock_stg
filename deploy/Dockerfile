# Nestock backend image. Build context = a copy of backend/ (see deploy-aca.sh).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
# read_agent.py resolves <repo-root>/prompts via parents[3]; in the image that is /prompts.
COPY prompts /prompts
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1

EXPOSE 8000
# External upload URLs come only from the fixed NESTOCK_STAGING_BASE_URL.
# Ignore client-controlled forwarded headers; rate limits use authenticated
# install and endpoint-global scopes, not a proxy-derived peer address.
# Single worker: approvals and staging uploads are in-memory (single replica only).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--no-proxy-headers"]
