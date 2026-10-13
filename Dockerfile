FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install dependencies using uv caching
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Copy source code
COPY src/ ./src/

ENV DAEMON_MODE=true \
    SYNC_TIME=06:30 \
    TZ=Europe/Amsterdam

CMD ["uv", "run", "python", "src/sync.py"]
