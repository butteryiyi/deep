FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl gnupg \
    libgtk-3-0 libdbus-glib-1-2 libxt6 libx11-xcb1 \
    libasound2 libxcomposite1 libxdamage1 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libatk1.0-0 \
    libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
    libxshmfence1 fonts-noto-cjk xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ===== 探测 camoufox 包结构 =====
RUN echo "===== camoufox package info =====" \
    && pip show camoufox \
    && echo "" \
    && echo "===== camoufox.pkgman functions =====" \
    && python -c "import camoufox.pkgman as m; print([x for x in dir(m) if not x.startswith('_')])" \
    && echo "" \
    && echo "===== camoufox top-level =====" \
    && python -c "import camoufox as m; print([x for x in dir(m) if not x.startswith('_')])" \
    && echo "" \
    && echo "===== camoufox CLI help =====" \
    && python -m camoufox --help 2>&1 || echo "(no CLI help)" \
    && echo "" \
    && echo "===== camoufox fetch =====" \
    && python -m camoufox fetch 2>&1 || echo "(fetch failed)"

# Playwright 后备
RUN python -m playwright install firefox \
    && python -m playwright install-deps firefox

COPY . .

ENV PORT=10000
EXPOSE 10000

CMD ["python", "app.py"]
