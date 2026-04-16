FROM python:3.11-slim

WORKDIR /app

ARG MIHOMO_VERSION=latest

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gzip \
    && rm -rf /var/lib/apt/lists/*

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
    download_url="$(python - <<'PY'
import json
import os
import urllib.error
import urllib.request

version = os.environ["MIHOMO_VERSION"].strip() or "latest"
arch = os.environ["MIHOMO_ARCH"].strip()

def fetch_release(target: str):
    if target == "latest":
        url = "https://api.github.com/repos/MetaCubeX/mihomo/releases/latest"
    else:
        url = f"https://api.github.com/repos/MetaCubeX/mihomo/releases/tags/{target}"
    with urllib.request.urlopen(url) as response:
        return json.load(response)

try:
    release = fetch_release(version)
except urllib.error.HTTPError:
    if version == "latest":
        raise
    release = fetch_release("latest")

assets = release.get("assets", [])
candidates = [
    asset for asset in assets
    if arch in (asset.get("name", "")).lower() and str(asset.get("name", "")).lower().endswith(".gz")
]
if not candidates:
    raise SystemExit(f"no mihomo asset found for {arch}")

def sort_key(asset):
    name = str(asset.get("name", "")).lower()
    if "compatible" in name:
        return (0, len(name))
    if "go120" in name:
        return (1, len(name))
    if "alpha" in name:
        return (2, len(name))
    return (3, len(name))

candidates.sort(key=sort_key)
print(candidates[0]["browser_download_url"])
PY
)"; \
    curl -fsSL "$download_url" | gzip -dc > /app/bin/mihomo; \
    chmod +x /app/bin/mihomo

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
