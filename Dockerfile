FROM python:3.12-slim AS base
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir --upgrade pip

# ── dev target: includes dev deps, entire project mounted as volume ──────────
FROM base AS dev
COPY pyproject.toml .
# app/ must exist before pip install so hatchling registers it in the editable .pth
COPY app/ app/
RUN pip install --no-cache-dir -e ".[dev]"
# Source is bind-mounted at runtime; copy here only so the image is self-contained
COPY . .

# ── prod target: only runtime deps, minimal footprint ───────────────────────
FROM base AS prod
COPY pyproject.toml .
COPY app/ app/
RUN pip install --no-cache-dir -e "."
COPY resources/ resources/
