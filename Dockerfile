FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORTFOLIO_TRACKER_HOME=/data

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY portfolio_tracker ./portfolio_tracker
COPY scripts ./scripts

RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data

USER appuser

VOLUME ["/data"]
EXPOSE 5055

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5055/health', timeout=3)" || exit 1

CMD ["python", "-c", "from portfolio_tracker.web import main; main(host='0.0.0.0', port=5055)"]
