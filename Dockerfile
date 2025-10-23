# ===== 1) ВАРИАНТ ПО УМОЛЧАНИЮ: Playwright base image =====
# (браузеры уже предустановлены)
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy AS bot-playwright

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=1 \
    PW_LAUNCH_ARGS="--no-sandbox --disable-setuid-sandbox"

COPY . .
# CMD/ENTRYPOINT как у тебя (compose обычно переопределяет)


# ===== 2) АЛЬТЕРНАТИВНЫЙ ВАРИАНТ: Debian/Ubuntu slim + ручная установка =====
# ВАЖНО: без fonts-ubuntu (его нет в trixie). Ставим кросс-дефолтные шрифты.
FROM python:3.11-slim AS bot-ubuntu
# (при желании можно зафиксировать на bookworm: FROM python:3.11-slim-bookworm)

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# системные зависимости для Chromium/Playwright под Debian/Ubuntu
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates wget curl \
    # базовые X/GTK/аудио/рендер либы
    libnss3 libnspr4 libx11-6 libx11-xcb1 libxcb1 \
    libxcomposite1 libxcursor1 libxdamage1 libxi6 libxtst6 libcups2 \
    libxrandr2 libgbm1 libpango-1.0-0 libpangocairo-1.0-0 \
    libasound2 libatk1.0-0 libatk-bridge2.0-0 libgtk-3-0 \
    libdrm2 libxfixes3 libxext6 libxrender1 libxshmfence1 \
    libxkbcommon0 \
    # шрифты, которые точно есть в Debian
    fonts-liberation fonts-unifont fonts-dejavu-core fonts-noto-color-emoji \
 && rm -rf /var/lib/apt/lists/*

# ставим только браузер (deps уже установлены выше)
RUN playwright install chromium

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=1 \
    PW_LAUNCH_ARGS="--no-sandbox --disable-setuid-sandbox"

COPY . .
