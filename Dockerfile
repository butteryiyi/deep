FROM python:3.10-slim

# 安装系统依赖（只安装 Playwright 官方推荐的 Firefox 依赖）
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    curl \
    gnupg \
    procps \
    xvfb \
    # Firefox 运行时依赖（来自 Playwright 文档）
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
    # 额外可能需要的依赖
    libgdk-pixbuf-2.0-0 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade -r requirements.txt

# 安装 Playwright 浏览器（Firefox）—— 只下载浏览器，不尝试安装系统依赖
RUN python -m playwright install firefox

# 安装 Camoufox（会触发下载其自定义浏览器）
RUN python -c "import camoufox; camoufox.sync_playwright()" 2>/dev/null || true
RUN python -c "from camoufox.sync_api import CamoufoxSync; print('Camoufox ready')" 2>/dev/null || echo "Will download on first run"

COPY . .

# 创建非 root 用户
RUN useradd -m -u 1000 app_user && chown -R app_user:app_user /app
USER app_user

EXPOSE 7860

CMD ["python", "app.py"]
