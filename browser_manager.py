FROM python:3.10-slim

# 安装系统依赖
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

WORKDIR /app

# ===== 关键修改：先创建用户，再以该用户身份安装浏览器 =====
# 创建非 root 用户（提前创建）
RUN useradd -m -u 1000 app_user
RUN mkdir -p /home/app_user/.cache && chown -R app_user:app_user /home/app_user/.cache

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade -r requirements.txt

# ===== 方法1：使用共享路径安装 Playwright 浏览器 =====
# 设置 Playwright 浏览器安装到一个所有用户都能访问的共享路径
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/browsers
RUN mkdir -p /opt/browsers && chmod 777 /opt/browsers

# 安装 Playwright Firefox 到共享路径
RUN python -m playwright install firefox
# 将 /opt/browsers 的所有权改为 app_user（使后续写入 .links 等操作可行）
RUN chown -R app_user:app_user /opt/browsers

# ===== 预安装 Camoufox 浏览器 =====
# 以 app_user 身份预下载 Camoufox 数据，避免运行时触发 GitHub API
RUN su app_user -c "python -c \"from camoufox.sync_api import CamoufoxSync; print('Camoufox pre-installed')\"" 2>/dev/null \
    || echo "⚠️ Camoufox pre-install skipped, will retry at runtime"

COPY . .

# 将应用目录的所有权给 app_user
RUN chown -R app_user:app_user /app

USER app_user

EXPOSE 7860

CMD ["python", "app.py"]
