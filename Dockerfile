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

# ===== 关键修改：使用 camoufox fetch 预下载浏览器 =====
# 创建缓存目录并设置权限
RUN mkdir -p /home/app_user/.cache && chown -R app_user:app_user /home/app_user/.cache

# 切换到 app_user 运行 fetch 命令
USER app_user
ENV CAMOUFOX_NO_UPDATE_CHECK=1
# 运行 fetch 命令下载浏览器到 ~/.cache/camoufox
RUN python -c "from camoufox.sync_api import CamoufoxSync; CamoufoxSync.fetch()" || echo "Camoufox fetch failed, will retry at runtime"

# 切换回 root 复制应用代码
USER root
COPY --chown=app_user:app_user . .

# 预安装 Playwright Firefox 作为后备（可选）
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/browsers
RUN python -m playwright install firefox && chown -R app_user:app_user /opt/browsers

# 最终以 app_user 运行
USER app_user

EXPOSE 7860
CMD ["python", "app.py"]
