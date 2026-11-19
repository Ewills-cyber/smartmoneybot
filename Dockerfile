# syntax=docker/dockerfile:1
FROM python:3.12-bookworm AS builder
WORKDIR /app
ENV PIP_NO_CACHE_DIR=1
COPY requirements.txt .
RUN pip wheel -w /wheels -r requirements.txt

FROM python:3.12-slim-bookworm AS runner
WORKDIR /app
ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SMARTMONEY_DB=/tmp/smartmoney.db \
    PORT=10000
RUN useradd --create-home --uid 1001 appuser
COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels
COPY --chown=appuser:appuser . .
USER appuser
EXPOSE 10000
CMD ["python", "web.py"]
