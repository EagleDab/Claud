# ===== 1) ВАРИАНТ ПО УМОЛЧАНИЮ: Playwright base image =====
# Браузеры уже предустановлены, ничего дополнительно качать не нужно.
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy AS bot-playwright

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Полезные ENV для запуска в контейнере (root / no-sandbox и пр.)
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=1 \
    PW_LAUNCH_ARGS="--no-sandbox --disable-setuid-sandbox"

COPY . .
# CMD/ENTRYPOINT оставь как было в твоём проекте (compose его переопределяет)


# ===== 2) АЛЬТЕРНАТИВНЫЙ ВАРИАНТ: Ubuntu/Debian slim + ручная установка =====
# Если будешь деплоить на свой хостинг без готового playwright-образа.
FROM python:3.11-slim AS bot-ubuntu

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Системные зависимости для Chromium/Playwright под Debian/Ubuntu
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates wget curl \
    libnss3 libnspr4 libx11-6 libx11-xcb1 libxcb1 \
    libxcomposite1 libxcursor1 libxdamage1 libxi6 libxtst6 libcups2 \
    libxrandr2 libgbm1 libpango-1.0-0 libpangocairo-1.0-0 \
    libasound2 libatk1.0-0 libatk-bridge2.0-0 libgtk-3-0 \
    libdrm2 libxfixes3 libxext6 libxrender1 libxshmfence1 \
    fonts-liberation fonts-unifont fonts-ubuntu \
 && rm -rf /var/lib/apt/lists/*

# Ставим ТОЛЬКО браузеры (без --with-deps, т.к. deps уже поставили выше)
RUN playwright install chromium

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=1 \
    PW_LAUNCH_ARGS="--no-sandbox --disable-setuid-sandbox"

COPY . .