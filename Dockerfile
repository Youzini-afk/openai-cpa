FROM python:3.11-slim

WORKDIR /app

ARG MIHOMO_VERSION=latest

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gzip \
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
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

RUN rm -rf utils/auth_core/*.py 2>/dev/null || true
RUN mkdir -p /app/data

EXPOSE 8000
ENV PYTHONUNBUFFERED=1 \
    MIHOMO_BINARY_PATH=/app/bin/mihomo \
    PORT=8000 \
    HOST=0.0.0.0

CMD ["python", "wfxl_openai_regst.py"]
