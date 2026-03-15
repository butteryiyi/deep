FROM python:3.11-slim

# 系统依赖（合并 Camoufox + Playwright Firefox 所需的全部库）
# 注意：Debian Bookworm 中部分包名已变更
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl gnupg \
    # GTK / 显示相关
    libgtk-3-0 libdbus-glib-1-2 libxt6 libx11-xcb1 \
    libasound2 libxcomposite1 libxdamage1 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libatk1.0-0 \
    libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
    libxshmfence1 \
    # 图像处理（Bookworm 用 libgdk-pixbuf-2.0-0 替代旧包名）
    libgdk-pixbuf-2.0-0 libgdk-pixbuf-xlib-2.0-0 \
    # 字体（Bookworm 用 fonts-unifont 替代 ttf-unifont）
    fonts-noto-cjk fonts-unifont fonts-liberation \
    # Playwright Firefox 额外需要的
    libdbus-1-3 libfontconfig1 libfreetype6 \
    libharfbuzz0b libcairo-gobject2 libglib2.0-0 \
    libpangocairo-1.0-0 libpangoft2-1.0-0 \
    libxcb-shm0 libx11-6 libxext6 libxfixes3 libxcursor1 \
    libxi6 libxtst6 libpipewire-0.3-0 \
    # 虚拟显示（headless 运行需要）
    xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ========== 预装 Camoufox 浏览器（构建时下载，运行时零下载）==========
RUN python -m camoufox fetch

# ========== 预装 Playwright Firefox 作为后备 ==========
# 只下载浏览器二进制，不执行 install-deps（依赖已在上面手动装好）
RUN python -m playwright install firefox

COPY . .

ENV PORT=10000
EXPOSE 10000

CMD ["python", "app.py"]
