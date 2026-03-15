FROM python:3.10-slim

# 安装系统依赖（包括浏览器运行所需库）
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
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libxkbcommon0 \
    libasound2 \
    libxss1 \
    libxtst6 \
    libxi6 \
    libnss3 \
    libxcursor1 \
    libgdk-pixbuf-2.0-0 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 创建非 root 用户
RUN useradd -m -u 1000 app_user

WORKDIR /app

# 复制 requirements.txt 并安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade -r requirements.txt

# ===== 核心：预安装 Playwright Firefox（默认引擎）=====
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/browsers
RUN python -m playwright install firefox \
    && python -m playwright install-deps firefox \
    && chown -R app_user:app_user /opt/browsers

# ===== 可选：尝试预下载 Camoufox（失败不影响启动）=====
RUN mkdir -p /home/app_user/.cache && chown -R app_user:app_user /home/app_user/.cache
USER app_user
ENV CAMOUFOX_NO_UPDATE_CHECK=1
RUN python -c "from camoufox.sync_api import Camoufox; print('Camoufox available')" 2>/dev/null \
    && python -c "import camoufox; camoufox.fetch()" 2>/dev/null \
    || echo "⚠️ Camoufox 预下载跳过，运行时将使用 Playwright Firefox"

# 切换回 root 复制应用代码
USER root
COPY --chown=app_user:app_user . .

# 设置默认环境变量
ENV API_SECRET_KEY=zxcvbnm
ENV HEADLESS=true

# 最终以 app_user 运行
USER app_user

EXPOSE 7860
CMD ["python", "app.py"]
