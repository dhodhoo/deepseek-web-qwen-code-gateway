FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /opt/deepseek-qwen-gateway

COPY pyproject.toml README_START_HERE.md ./
COPY app ./app
COPY vendor ./vendor

RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin gateway \
    && mkdir -p /var/lib/deepseek-qwen-gateway/diagnostics \
    && chown -R gateway:gateway /opt/deepseek-qwen-gateway /var/lib/deepseek-qwen-gateway

USER gateway

EXPOSE 8000

CMD ["python", "-m", "app.main"]
