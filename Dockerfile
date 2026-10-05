FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY prompts ./prompts
COPY config ./config
COPY evals ./evals
# Editable install keeps prompts/ and config/ resolvable from /app.
RUN pip install -e .

# Optional: headless Chromium for the browser fallback (adds ~400 MB).
ARG WITH_BROWSER=0
RUN if [ "$WITH_BROWSER" = "1" ]; then pip install "playwright>=1.47" && playwright install --with-deps chromium; fi

RUN useradd --create-home --uid 10001 app && mkdir -p /app/var /app/evals/candidates && chown -R app /app
USER app

ENV PORT=8000 \
    TICKETPILOT_DB=/app/var/ticketpilot.db \
    TICKETPILOT_PUBLIC_DEMO=true \
    TICKETPILOT_BROWSER_ENGINE=http
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import os,urllib.request;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz')"
# The no-API carrier portal is served by this same app, so the browser fallback targets loopback.
CMD ["sh", "-c", "TICKETPILOT_PORTAL_BASE_URL=http://127.0.0.1:${PORT} exec uvicorn ticketpilot.web.app:create_app --factory --host 0.0.0.0 --port ${PORT} --proxy-headers"]
