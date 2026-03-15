# 使用 Python 3.10 轻量镜像
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

# 设置工作目录
WORKDIR /app

# 提前创建缓存目录并设置权限
RUN mkdir -p /home/app_user/.cache /opt/browsers \
    && chown -R app_user:app_user /home/app_user/.cache /opt/browsers

# 复制依赖文件
COPY requirements.txt .

# 安装 Python 依赖（以 root 身份安装到系统）
RUN pip install --no-cache-dir --upgrade -r requirements.txt

# 设置环境变量，禁止 Camoufox 运行时检查更新
ENV CAMOUFOX_NO_UPDATE_CHECK=1

# 设置 Playwright 浏览器安装路径（全局共享）
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/browsers

# 预安装 Playwright Firefox（以 root 身份，以便所有用户可用）
RUN python -m playwright install firefox \
    && chown -R app_user:app_user /opt/browsers

# 切换到 app_user 进行后续操作
USER app_user

# 预下载 Camoufox 浏览器到用户缓存目录
# 使用 sync_api 触发下载，如果失败（例如网络问题）则忽略，运行时再尝试
RUN python -c "from camoufox.sync_api import CamoufoxSync; CamoufoxSync()" 2>/dev/null || echo "Camoufox pre-download failed, will retry at runtime"

# 切换回 root 以复制应用代码
USER root

# 复制应用代码（注意保留权限）
COPY --chown=app_user:app_user . .

# 再次确保缓存目录权限正确
RUN chown -R app_user:app_user /home/app_user/.cache /app

# 最终以 app_user 运行
USER app_user

# 暴露端口（Render 默认使用 7860）
EXPOSE 7860

# 启动命令
CMD ["python", "app.py"]
