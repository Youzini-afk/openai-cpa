import json
import os
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

import yaml


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data", "mihomo")
CONFIG_PATH = os.path.join(DATA_DIR, "config.yaml")
PROVIDERS_DIR = os.path.join(DATA_DIR, "providers")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
PID_PATH = os.path.join(DATA_DIR, "mihomo.pid")
LOG_PATH = os.path.join(DATA_DIR, "mihomo.log")
STATE_PATH = os.path.join(DATA_DIR, "state.json")


class EmbeddedMihomoError(RuntimeError):
    pass


def mask_subscription_url(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    parsed = urllib.parse.urlparse(text)
    if not parsed.scheme or not parsed.netloc:
        return "***"
    query = "" if not parsed.query else "?***"
    path = parsed.path or ""
    if len(path) > 16:
        path = path[:8] + "..." + path[-4:]
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "", query, ""))


def validate_subscription_url(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    if len(text) > 2048:
        raise EmbeddedMihomoError("订阅链接过长")
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise EmbeddedMihomoError("订阅链接必须是 http/https URL")
    host = (parsed.hostname or "").strip().lower().strip("[]")
    if host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}:
        raise EmbeddedMihomoError("订阅链接不允许指向本机地址")
    if host.startswith("127.") or host.startswith("169.254."):
        raise EmbeddedMihomoError("订阅链接不允许指向本机/元数据地址")
    return text


def validate_port(value: Any, name: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise EmbeddedMihomoError(f"{name} 必须是有效端口")
    if port < 1024 or port > 65535:
        raise EmbeddedMihomoError(f"{name} 端口范围必须是 1024-65535")
    if port == 8000:
        raise EmbeddedMihomoError(f"{name} 不能使用应用端口 8000")
    return port


def normalize_settings(settings: Optional[dict]) -> dict:
    raw = settings if isinstance(settings, dict) else {}
    mixed_port = validate_port(raw.get("mixed_port", 7890), "mixed_port")
    controller_port = validate_port(raw.get("controller_port", 9090), "controller_port")
    if mixed_port == controller_port:
        raise EmbeddedMihomoError("mixed_port 与 controller_port 不能相同")
    return {
        "enable": bool(raw.get("enable", False)),
        "auto_start": bool(raw.get("auto_start", True)),
        "use_as_default_proxy": bool(raw.get("use_as_default_proxy", False)),
        "subscription_url": validate_subscription_url(raw.get("subscription_url", "")),
        "mixed_port": mixed_port,
        "controller_port": controller_port,
        "controller_secret": str(raw.get("controller_secret", "") or "").strip(),
        "group_name": str(raw.get("group_name", "OpenAI") or "OpenAI").strip()[:120],
        "selected_group": str(raw.get("selected_group", "") or "").strip()[:120],
        "selected_proxy": str(raw.get("selected_proxy", "") or "").strip()[:200],
        "test_url": str(raw.get("test_url", "https://api.openai.com/cdn-cgi/trace") or "https://api.openai.com/cdn-cgi/trace").strip()[:2048],
        "update_interval_minutes": max(1, int(raw.get("update_interval_minutes", 60) or 60)),
        "log_lines": min(5000, max(10, int(raw.get("log_lines", 200) or 200))),
    }


class EmbeddedMihomoManager:
    def __init__(self, settings: Optional[dict] = None):
        self.settings = normalize_settings(settings or {})
        self._lock = threading.Lock()
        self._process: Optional[subprocess.Popen] = None
        self._started_by_app = False

    def update_settings(self, settings: dict) -> None:
        self.settings = normalize_settings(settings)

    @property
    def controller_base(self) -> str:
        return f"http://127.0.0.1:{self.settings['controller_port']}"

    def _headers(self) -> dict:
        secret = self.settings.get("controller_secret") or ""
        return {"Authorization": f"Bearer {secret}"} if secret else {}

    def ensure_dirs(self) -> None:
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(PROVIDERS_DIR, exist_ok=True)
        os.makedirs(CACHE_DIR, exist_ok=True)

    def write_config(self) -> str:
        self.ensure_dirs()
        settings = self.settings
        if not settings.get("subscription_url"):
            raise EmbeddedMihomoError("请先配置 Mihomo 订阅链接")
        provider_path = os.path.join(PROVIDERS_DIR, "subscription.yaml")
        config = {
            "mixed-port": settings["mixed_port"],
            "allow-lan": False,
            "bind-address": "127.0.0.1",
            "mode": "rule",
            "log-level": "info",
            "external-controller": f"127.0.0.1:{settings['controller_port']}",
            "secret": settings.get("controller_secret", ""),
            "profile": {"store-selected": True, "store-fake-ip": True},
            "proxies": [],
            "proxy-providers": {
                "subscription": {
                    "type": "http",
                    "url": settings["subscription_url"],
                    "interval": settings["update_interval_minutes"] * 60,
                    "path": provider_path,
                    "health-check": {
                        "enable": True,
                        "interval": 600,
                        "url": settings["test_url"],
                    },
                }
            },
            "proxy-groups": [
                {
                    "name": settings["group_name"],
                    "type": "select",
                    "use": ["subscription"],
                }
            ],
            "rules": [f"MATCH,{settings['group_name']}"],
        }
        tmp_path = CONFIG_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            yaml.safe_dump(config, handle, allow_unicode=True, sort_keys=False)
        os.replace(tmp_path, CONFIG_PATH)
        return CONFIG_PATH

    def _read_pid(self) -> Optional[int]:
        try:
            with open(PID_PATH, "r", encoding="utf-8") as handle:
                return int(handle.read().strip())
        except Exception:
            return None

    def _pid_running(self, pid: Optional[int]) -> bool:
        if not pid or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def is_running(self) -> bool:
        if self._process and self._process.poll() is None:
            return True
        return self._pid_running(self._read_pid())

    def start(self, managed: bool = False) -> dict:
        with self._lock:
            if self.is_running():
                if managed:
                    self._started_by_app = True
                return self.status()
            config_path = self.write_config()
            log_handle = open(LOG_PATH, "ab", buffering=0)
            try:
                process = subprocess.Popen(
                    ["mihomo", "-d", DATA_DIR, "-f", config_path],
                    cwd=DATA_DIR,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                )
            except FileNotFoundError as exc:
                log_handle.close()
                raise EmbeddedMihomoError("未找到 mihomo 二进制，请确认 Docker 镜像已内置 Mihomo") from exc
            except Exception:
                log_handle.close()
                raise
            self._process = process
            if managed:
                self._started_by_app = True
            with open(PID_PATH, "w", encoding="utf-8") as handle:
                handle.write(str(process.pid))
            self._write_state({"last_start": int(time.time()), "managed": self._started_by_app})
        time.sleep(0.8)
        if self.settings.get("selected_proxy"):
            try:
                self.select_proxy(
                    str(self.settings.get("selected_group") or self.settings.get("group_name") or ""),
                    str(self.settings.get("selected_proxy") or ""),
                )
            except Exception:
                pass
        return self.status()

    def stop(self, only_if_managed: bool = False) -> dict:
        with self._lock:
            if only_if_managed and not self._started_by_app:
                return self.status()
            pid = self._process.pid if self._process and self._process.poll() is None else self._read_pid()
            if pid and self._pid_running(pid):
                try:
                    os.kill(pid, signal.SIGTERM)
                except Exception:
                    pass
                deadline = time.time() + 5
                while time.time() < deadline and self._pid_running(pid):
                    time.sleep(0.2)
                if self._pid_running(pid):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except Exception:
                        pass
            self._process = None
            self._started_by_app = False
            try:
                if os.path.exists(PID_PATH):
                    os.remove(PID_PATH)
            except Exception:
                pass
            self._write_state({"last_stop": int(time.time()), "managed": False})
        return self.status()

    def restart(self) -> dict:
        self.stop()
        return self.start()

    def _write_state(self, payload: dict) -> None:
        self.ensure_dirs()
        current = {}
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as handle:
                current = json.load(handle) or {}
        except Exception:
            current = {}
        current.update(payload)
        with open(STATE_PATH, "w", encoding="utf-8") as handle:
            json.dump(current, handle, ensure_ascii=False, indent=2)

    def controller_request(self, method: str, path: str, data: Optional[dict] = None, timeout: float = 5.0) -> Any:
        url = self.controller_base + path
        body = None if data is None else json.dumps(data).encode("utf-8")
        headers = {"Content-Type": "application/json", **self._headers()}
        req = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise EmbeddedMihomoError(f"Mihomo 控制器请求失败 HTTP {exc.code}: {detail}") from exc
        except Exception as exc:
            raise EmbeddedMihomoError(f"Mihomo 控制器不可用: {exc}") from exc
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except Exception:
            return {"raw": raw}

    def status(self) -> dict:
        running = self.is_running()
        version = None
        controller_ok = False
        error = ""
        if running:
            try:
                version = self.controller_request("GET", "/version", timeout=2.0)
                controller_ok = True
            except Exception as exc:
                error = str(exc)
        data = {
            "enabled": bool(self.settings.get("enable")),
            "running": running,
            "controller_ok": controller_ok,
            "pid": self._process.pid if self._process and self._process.poll() is None else self._read_pid(),
            "mixed_port": self.settings["mixed_port"],
            "controller_port": self.settings["controller_port"],
            "subscription_url": mask_subscription_url(self.settings.get("subscription_url", "")),
            "version": version,
            "error": error,
        }
        return data

    def logs(self, lines: Optional[int] = None) -> dict:
        limit = int(lines or self.settings.get("log_lines") or 200)
        if not os.path.exists(LOG_PATH):
            return {"lines": []}
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read().splitlines()
        masked_url = mask_subscription_url(self.settings.get("subscription_url", ""))
        raw_url = self.settings.get("subscription_url", "")
        selected = content[-max(1, limit):]
        if raw_url:
            selected = [line.replace(raw_url, masked_url) for line in selected]
        return {"lines": selected}

    def groups(self) -> dict:
        return self.controller_request("GET", "/proxies")

    def select_proxy(self, group: str, proxy: str) -> dict:
        group_name = str(group or self.settings.get("selected_group") or self.settings.get("group_name") or "").strip()
        proxy_name = str(proxy or "").strip()
        if not group_name or not proxy_name:
            raise EmbeddedMihomoError("请选择策略组和节点")
        encoded = urllib.parse.quote(group_name, safe="")
        return self.controller_request("PUT", f"/proxies/{encoded}", {"name": proxy_name})

    def test_delay(self, group: Optional[str] = None, proxy: Optional[str] = None, url: Optional[str] = None) -> dict:
        name = str(proxy or group or self.settings.get("selected_proxy") or self.settings.get("group_name") or "").strip()
        if not name:
            raise EmbeddedMihomoError("请选择需要测速的策略组或节点")
        query = urllib.parse.urlencode({"timeout": 5000, "url": url or self.settings.get("test_url")})
        encoded = urllib.parse.quote(name, safe="")
        return self.controller_request("GET", f"/proxies/{encoded}/delay?{query}", timeout=8.0)


_manager: Optional[EmbeddedMihomoManager] = None
_manager_lock = threading.Lock()


def get_manager(settings: Optional[dict] = None) -> EmbeddedMihomoManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = EmbeddedMihomoManager(settings or {})
        elif settings is not None:
            _manager.update_settings(settings)
        return _manager
