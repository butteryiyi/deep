FROM python:3.11-slim

# 系统依赖（Camoufox/Firefox 需要的库）
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl gnupg \
    libgtk-3-0 libdbus-glib-1-2 libxt6 libx11-xcb1 \
    libasound2 libxcomposite1 libxdamage1 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libatk1.0-0 \
    libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
    libxshmfence1 fonts-noto-cjk xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先复制 requirements.txt 利用 Docker 缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ========== 关键：构建时就预装 Camoufox ==========
RUN python -c "from camoufox.pkgman import install_camoufox; install_camoufox()"

# 同时预装 Playwright Firefox 作为后备
RUN python -m playwright install firefox && python -m playwright install-deps firefox

# 复制应用代码
COPY . .

# Render 会设置 PORT 环境变量
ENV PORT=10000
EXPOSE 10000

# 启动命令
CMD ["python", "app.py"]
