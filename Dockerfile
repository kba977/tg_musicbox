FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.7.17 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# 先只装依赖，代码变化时可以复用这一层缓存
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY musicbox ./musicbox

RUN useradd --create-home --uid 1000 musicbox && mkdir -p /app/data && chown musicbox /app/data
USER musicbox

CMD ["python", "-m", "musicbox"]
