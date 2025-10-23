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


