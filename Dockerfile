FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    gcc postgresql-client wget gnupg \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libdbus-1-3 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 libpango-1.0-0 libcairo2 \
    fonts-ubuntu fonts-unifont \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -g 1001 appuser && useradd -u 1001 -g appuser -d /app -s /usr/sbin/nologin appuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN if ! playwright install --with-deps chromium; then \
    echo "Playwright dependency install failed, retrying without --with-deps" >&2; \
    apt-get update && \
    apt-get install -y --no-install-recommends fonts-ubuntu fonts-unifont && \
    rm -rf /var/lib/apt/lists/* && \
    playwright install chromium; \
fi

COPY . .

RUN mkdir -p /app/logs /app/.cache /ms-playwright

RUN chown -R appuser:appuser /app /ms-playwright

ENV PYTHONUNBUFFERED=1
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=1
# если процесс под root — запускать без sandbox
ENV PW_LAUNCH_ARGS="--no-sandbox --disable-setuid-sandbox"

USER 1001:1001

CMD ["python", "-m", "bot.main"]
