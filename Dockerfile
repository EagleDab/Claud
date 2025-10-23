FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    gcc postgresql-client wget gnupg \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libdbus-1-3 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 libpango-1.0-0 libcairo2 \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -g 1001 appuser && useradd -u 1001 -g appuser -d /app -s /usr/sbin/nologin appuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium

COPY . .

RUN mkdir -p /app/logs /app/.cache

RUN chown -R appuser:appuser /app

ENV PYTHONUNBUFFERED=1
ENV PLAYWRIGHT_BROWSERS_PATH=/app/.cache/ms-playwright

USER 1001:1001

CMD ["python", "-m", "bot.main"]
