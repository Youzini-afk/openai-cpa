FROM python:3.11-slim

WORKDIR /app

ARG MIHOMO_VERSION=latest

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gzip \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libglib2.0-0 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libx11-6 \
    libxcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    && rm -rf /var/lib/apt/lists/*

COPY tools/resolve_mihomo_asset.py /tmp/resolve_mihomo_asset.py

RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64) mihomo_arch="linux-amd64" ;; \
        arm64) mihomo_arch="linux-arm64" ;; \
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    mkdir -p /app/bin; \
    export MIHOMO_VERSION="$MIHOMO_VERSION"; \
    export MIHOMO_ARCH="$mihomo_arch"; \
    download_url="$(python /tmp/resolve_mihomo_asset.py)"; \
    curl -fsSL "$download_url" | gzip -dc > /app/bin/mihomo; \
    chmod +x /app/bin/mihomo; \
    rm -f /tmp/resolve_mihomo_asset.py

COPY requirements.txt .
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

RUN python -m playwright install chromium

COPY . .

RUN rm -rf utils/auth_core/*.py 2>/dev/null || true
RUN mkdir -p /app/data

EXPOSE 8000
ENV PYTHONUNBUFFERED=1 \
    MIHOMO_BINARY_PATH=/app/bin/mihomo \
    PORT=8000 \
    HOST=0.0.0.0

CMD ["python", "wfxl_openai_regst.py"]
