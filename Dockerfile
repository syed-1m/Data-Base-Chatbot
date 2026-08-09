# =============================================================================
# DB-ChatBot — Dockerfile
# Multi-stage build for a lean, secure production image.
#
# Stages:
#   builder  : Installs Python dependencies into an isolated virtual env.
#   runtime  : Copies only the venv + source; no compilers or build tools.
#
# Build:  docker build -t db-chatbot .
# Run:    docker run -p 8000:8000 --env-file .env db-chatbot
# =============================================================================

# ---------------------------------------------------------------------------
# Stage 1 — Builder
# Installs all Python dependencies into /opt/venv
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

# Prevent Python from writing .pyc files and enable unbuffered stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

# Install system build dependencies needed for psycopg2 / asyncpg compilation
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Create isolated virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy and install Python dependencies
# Copy requirements first to leverage Docker layer caching —
# dependencies are only reinstalled when requirements.txt changes.
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt


# ---------------------------------------------------------------------------
# Stage 2 — Runtime
# Minimal image: base Python + the compiled venv + application source only.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Activate the virtual environment from the builder stage
    PATH="/opt/venv/bin:$PATH" \
    # Ensure Python finds our app package
    PYTHONPATH="/app"

WORKDIR /app

# Install only the runtime system libraries (libpq for psycopg2/asyncpg)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy the virtual environment from the builder stage
COPY --from=builder /opt/venv /opt/venv

# Create a non-root user for security
# Running as root inside a container is a security risk.
RUN groupadd --system appgroup && \
    useradd --system --gid appgroup --no-create-home appuser

# Copy application source code
COPY --chown=appuser:appgroup app/ ./app/
COPY --chown=appuser:appgroup alembic/ ./alembic/
COPY --chown=appuser:appgroup alembic.ini .

# Switch to non-root user
USER appuser

# Expose the application port
EXPOSE 8000

# Health check — Docker will mark the container unhealthy if this fails.
# Waits 30s before first check to allow DB connection to establish.
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/v1/health/')" \
    || exit 1

# ---------------------------------------------------------------------------
# Entrypoint
# Production: 2 uvicorn workers, no reload.
# Override CMD in docker-compose for development (--reload, 1 worker).
# ---------------------------------------------------------------------------
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "2", \
     "--loop", "uvloop", \
     "--http", "httptools", \
     "--access-log", \
     "--log-level", "info"]
