# Remote mode image: `gchat-mcp serve` (Streamable HTTP + OAuth). Configuration comes from env; see docs/remote.md.
FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --no-dev --extra remote
RUN useradd --create-home --uid 10001 app
USER app
ENV PATH="/app/.venv/bin:$PATH" PORT=8080
EXPOSE 8080
CMD ["gchat-mcp", "serve"]
