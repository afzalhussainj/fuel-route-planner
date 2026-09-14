FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:/usr/local/bin:${PATH}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mv /root/.local/bin/uv /usr/local/bin/uv \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin app

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .
# Host trees must never replace the Linux venv created above.
RUN rm -rf .venv \
    && uv sync --frozen --no-dev \
    && chmod +x scripts/entrypoint.sh \
    && mkdir -p staticfiles \
    && chown -R app:app /app

USER app

EXPOSE 8000

ENTRYPOINT ["./scripts/entrypoint.sh"]
CMD ["uv", "run", "--no-sync", "python", "manage.py", "runserver", "0.0.0.0:8000"]
