import json
import os
import urllib.error
import urllib.request


def fetch_release(target: str) -> dict:
    if target == "latest":
        url = "https://api.github.com/repos/MetaCubeX/mihomo/releases/latest"
    else:
        url = f"https://api.github.com/repos/MetaCubeX/mihomo/releases/tags/{target}"
    with urllib.request.urlopen(url) as response:
        return json.load(response)


def sort_key(asset: dict) -> tuple[int, int]:
    name = str(asset.get("name", "")).lower()
    if "compatible" in name:
        return (0, len(name))
    if "go120" in name:
        return (1, len(name))
    if "alpha" in name:
        return (2, len(name))
    return (3, len(name))


def main() -> None:
    version = os.environ.get("MIHOMO_VERSION", "latest").strip() or "latest"
    arch = os.environ.get("MIHOMO_ARCH", "").strip()
    if not arch:
        raise SystemExit("MIHOMO_ARCH is required")

    try:
        release = fetch_release(version)
    except urllib.error.HTTPError:
        if version == "latest":
            raise
        release = fetch_release("latest")

    assets = release.get("assets", [])
    candidates = [
        asset for asset in assets
        if arch in str(asset.get("name", "")).lower() and str(asset.get("name", "")).lower().endswith(".gz")
    ]
    if not candidates:
        raise SystemExit(f"no mihomo asset found for {arch}")

    candidates.sort(key=sort_key)
    print(candidates[0]["browser_download_url"])


if __name__ == "__main__":
    main()
