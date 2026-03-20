FROM python:3.11-slim
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY mcp_server.py VERSION ./
COPY data/gps.db data/DATA_CATALOG.yaml data/

# OpenShift runs containers as arbitrary UID — ensure writable dirs
ENV UV_CACHE_DIR=/tmp/uv-cache
RUN chmod -R g+rwX /app

EXPOSE 8000
HEALTHCHECK CMD curl -f http://localhost:8000/health || exit 1
CMD ["uv", "run", "--script", "mcp_server.py", "--http", "--port", "8000"]
