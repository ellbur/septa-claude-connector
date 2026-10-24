FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY septa_mcp ./septa_mcp
RUN pip install --no-cache-dir .

# The schedule is downloaded on startup and refreshed daily; mount a volume here
# to keep it across restarts.
ENV SEPTA_DATA_DIR=/data \
    MCP_TRANSPORT=http \
    PORT=8000
VOLUME /data
EXPOSE 8000

CMD ["septa-mcp"]
