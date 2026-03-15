FROM python:3.10-slim

# 安装系统依赖（Playwright Firefox 运行所需）
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    curl \
    gnupg \
    procps \
    xvfb \
    libdbus-glib-1-2 \
    libgtk-3-0 \
    libx11-xcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libxkbcommon0 \
    libasound2t64 \
    libxss1 \
    libxtst6 \
    libxi6 \
    libnss3 \
    libnspr4 \
    libxcursor1 \
    libxfixes3 \
    libxrender1 \
    libfontconfig1 \
    libfreetype6 \
    libharfbuzz0b \
    libgdk-pixbuf-2.0-0 \
    fonts-unifont \
    fonts-noto-color-emoji \
    fonts-liberation \
    fonts-ipafont-gothic \
    fonts-wqy-zenhei \
    libdrm2 \
    libxshmfence1 \
    libglib2.0-0 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 app_user

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade -r requirements.txt

# 预安装 Playwright Firefox
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/browsers
RUN python -m playwright install firefox \
    && chown -R app_user:app_user /opt/browsers

COPY --chown=app_user:app_user . .

ENV HEADLESS=true

USER app_user

EXPOSE 7860
CMD ["python", "app.py"]
