# 使用 Python 3.10 轻量镜像
FROM python:3.10-slim

# 安装系统依赖（浏览器运行所需库和工具）
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    curl \
    gnupg \
    procps \
    xvfb \
    unzip \
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

# 创建非 root 用户（与 Render 环境兼容）
RUN useradd -m -u 1000 app_user

# 设置工作目录
WORKDIR /app

# 提前创建必要的目录并设置权限
RUN mkdir -p /home/app_user/.cache /opt/browsers \
    && chown -R app_user:app_user /home/app_user/.cache /opt/browsers

# 复制依赖文件
COPY requirements.txt .

# 安装 Python 依赖（以 root 身份安装到系统）
RUN pip install --no-cache-dir --upgrade -r requirements.txt

# 设置环境变量
ENV CAMOUFOX_NO_UPDATE_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/browsers

# 预安装 Playwright Firefox（备选）
RUN python -m playwright install firefox \
    && chown -R app_user:app_user /opt/browsers

# ===== 方案A：手动下载 Camoufox 浏览器到缓存目录 =====
# 定义 Camoufox 版本（与日志中的版本一致）
ENV CAMOUFOX_VERSION=v135.0.1-beta.24
ENV CAMOUFOX_URL=https://github.com/daijro/camoufox/releases/download/${CAMOUFOX_VERSION}/camoufox-${CAMOUFOX_VERSION}-lin.x86_64.zip

# 下载并解压到缓存目录
RUN wget -q -O /tmp/camoufox.zip ${CAMOUFOX_URL} \
    && unzip /tmp/camoufox.zip -d /home/app_user/.cache/camoufox/ \
    && rm /tmp/camoufox.zip \
    && chown -R app_user:app_user /home/app_user/.cache/camoufox \
    && echo "Camoufox 预下载完成，缓存目录: /home/app_user/.cache/camoufox"

# 可选：验证解压后的文件
RUN ls -la /home/app_user/.cache/camoufox

# ===== 复制应用代码 =====
COPY --chown=app_user:app_user . .

# 最终以 app_user 运行
USER app_user

# 暴露端口（Render 默认使用 7860）
EXPOSE 7860

# 启动命令
CMD ["python", "app.py"]
