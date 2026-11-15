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
# --proxy-headers: staging upload URLs are built from X-Forwarded-Proto/Host.
# Single worker: approvals and staging uploads are in-memory (single replica only).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
