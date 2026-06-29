FROM python:3.11-slim

WORKDIR /app

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=60 \
    PIP_RETRIES=3 \
    PIP_NO_COMPILE=1

# Pinned Mihomo release. Bump deliberately after verifying asset names/checksums.
ARG MIHOMO_VERSION=v1.18.10
ARG TARGETARCH

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gzip \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    case "${TARGETARCH:-amd64}" in \
        amd64) mihomo_arch="amd64" ;; \
        arm64) mihomo_arch="arm64" ;; \
        *) echo "Unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/MetaCubeX/mihomo/releases/download/${MIHOMO_VERSION}/mihomo-linux-${mihomo_arch}-${MIHOMO_VERSION}.gz" -o /tmp/mihomo.gz; \
    gzip -d /tmp/mihomo.gz; \
    install -m 0755 /tmp/mihomo /usr/local/bin/mihomo; \
    rm -f /tmp/mihomo; \
    mihomo -v

COPY requirements.txt .
RUN python -m pip install --no-cache-dir --upgrade --prefer-binary --only-binary=:all: pip setuptools wheel && \
    python -m pip install --no-cache-dir --prefer-binary --only-binary=:all: -r requirements.txt

COPY . .

RUN rm -rf utils/auth_core/*.py 2>/dev/null || true

EXPOSE 8000
ENV PYTHONUNBUFFERED=1

CMD ["python", "wfxl_openai_regst.py"]
