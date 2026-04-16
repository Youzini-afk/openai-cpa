import json
import os
import signal
import subprocess
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Optional

import requests as std_requests
import yaml

from utils import config as cfg

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
MIHOMO_DIR = os.path.join(DATA_DIR, "mihomo")
MIHOMO_PROVIDERS_DIR = os.path.join(MIHOMO_DIR, "providers")
MIHOMO_BIN_DIR = os.path.join(MIHOMO_DIR, "bin")
MIHOMO_CONFIG_PATH = os.path.join(MIHOMO_DIR, "config.yaml")
MIHOMO_STATE_PATH = os.path.join(MIHOMO_DIR, "state.json")
MIHOMO_LOG_PATH = os.path.join(MIHOMO_DIR, "mihomo.log")
MIHOMO_PID_PATH = os.path.join(MIHOMO_DIR, "mihomo.pid")


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default


def _tail_lines(path: str, limit: int) -> list[str]:
    if limit <= 0 or not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
        return [line.rstrip("\r\n") for line in lines[-limit:]]
    except Exception:
        return []


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


class EmbeddedMihomoManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._process: Optional[subprocess.Popen] = None
        self._log_handle = None
        self._auto_update_thread: Optional[threading.Thread] = None
        self._last_auto_check_at = 0.0
        self._runtime_state = {
            "last_error": "",
            "last_started_at": "",
            "last_stopped_at": "",
            "last_updated_at": "",
            "last_update_error": "",
            "current_group": "",
            "current_proxy": "",
            "subscription_url": "",
        }
        self._ensure_layout()
        self._load_state()

    def _ensure_layout(self) -> None:
        _ensure_dir(DATA_DIR)
        _ensure_dir(MIHOMO_DIR)
        _ensure_dir(MIHOMO_PROVIDERS_DIR)
        _ensure_dir(MIHOMO_BIN_DIR)

    def _load_state(self) -> None:
        if not os.path.exists(MIHOMO_STATE_PATH):
            return
        try:
            with open(MIHOMO_STATE_PATH, "r", encoding="utf-8") as handle:
                data = json.load(handle) or {}
            if isinstance(data, dict):
                self._runtime_state.update(data)
        except Exception:
            pass

    def _save_state(self) -> None:
        try:
            with open(MIHOMO_STATE_PATH, "w", encoding="utf-8") as handle:
                json.dump(self._runtime_state, handle, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _set_state(self, **kwargs) -> None:
        self._runtime_state.update(kwargs)
        self._save_state()

    def _read_pid(self) -> Optional[int]:
        if not os.path.exists(MIHOMO_PID_PATH):
            return None
        try:
            with open(MIHOMO_PID_PATH, "r", encoding="utf-8") as handle:
                return int(handle.read().strip())
        except Exception:
            return None

    def _write_pid(self, pid: int) -> None:
        try:
            with open(MIHOMO_PID_PATH, "w", encoding="utf-8") as handle:
                handle.write(str(pid))
        except Exception:
            pass

    def _clear_pid(self) -> None:
        try:
            if os.path.exists(MIHOMO_PID_PATH):
                os.remove(MIHOMO_PID_PATH)
        except Exception:
            pass

    def _pid_matches_mihomo(self, pid: int) -> bool:
        if pid <= 0:
            return False
        cmdline_path = f"/proc/{pid}/cmdline"
        if os.path.exists(cmdline_path):
            try:
                with open(cmdline_path, "rb") as handle:
                    cmdline = handle.read().decode("utf-8", errors="ignore")
                return "mihomo" in cmdline.lower()
            except Exception:
                return False
        try:
            os.kill(pid, 0)
            return True
        except Exception:
            return False

    def _is_process_running(self, pid: Optional[int] = None) -> bool:
        target_pid = pid if pid is not None else self._read_pid()
        if not target_pid:
            return False
        return self._pid_matches_mihomo(target_pid)

    def _cleanup_stale_pid(self) -> None:
        if self._is_process_running():
            return
        self._clear_pid()

    def get_binary_path(self) -> str:
        candidates = [
            os.getenv("MIHOMO_BINARY_PATH", "").strip(),
            os.path.join(MIHOMO_BIN_DIR, "mihomo"),
            "/usr/local/bin/mihomo",
            "/usr/bin/mihomo",
            "/app/bin/mihomo",
        ]
        for candidate in candidates:
            if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return ""

    def get_runtime_endpoints(self) -> dict[str, Any]:
        mixed_port = max(1, _safe_int(getattr(cfg, "EMBEDDED_MIHOMO_MIXED_PORT", 7897), 7897))
        controller_port = max(1, _safe_int(getattr(cfg, "EMBEDDED_MIHOMO_CONTROLLER_PORT", 9097), 9097))
        secret = str(getattr(cfg, "EMBEDDED_MIHOMO_SECRET", "") or "").strip()
        return {
            "mixed_port": mixed_port,
            "controller_port": controller_port,
            "mixed_proxy_url": f"http://127.0.0.1:{mixed_port}",
            "controller_url": f"http://127.0.0.1:{controller_port}",
            "secret": secret,
        }

    def _headers(self) -> dict[str, str]:
        secret = str(self.get_runtime_endpoints().get("secret") or "").strip()
        if not secret:
            return {}
        return {"Authorization": f"Bearer {secret}"}

    def _request_controller(self, method: str, path: str, **kwargs) -> Any:
        endpoints = self.get_runtime_endpoints()
        base_url = endpoints["controller_url"].rstrip("/")
        target = f"{base_url}/{path.lstrip('/')}"
        headers = kwargs.pop("headers", {}) or {}
        merged_headers = {}
        merged_headers.update(self._headers())
        merged_headers.update(headers)
        response = std_requests.request(method, target, headers=merged_headers, timeout=kwargs.pop("timeout", 10), **kwargs)
        response.raise_for_status()
        if not response.content:
            return {}
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            return response.json()
        return response.text

    def _current_subscription_url(self) -> str:
        runtime_url = str(self._runtime_state.get("subscription_url") or "").strip()
        config_url = str(getattr(cfg, "EMBEDDED_MIHOMO_SUBSCRIPTION_URL", "") or "").strip()
        return config_url or runtime_url

    def _build_config_payload(self, subscription_url: str) -> dict[str, Any]:
        endpoints = self.get_runtime_endpoints()
        provider_path = os.path.join(MIHOMO_PROVIDERS_DIR, "primary.yaml")
        group_name = str(getattr(cfg, "EMBEDDED_MIHOMO_GROUP_NAME", "节点选择") or "节点选择").strip() or "节点选择"
        test_url = str(getattr(cfg, "EMBEDDED_MIHOMO_TEST_URL", "https://www.gstatic.com/generate_204") or "").strip()
        if not test_url:
            test_url = "https://www.gstatic.com/generate_204"
        update_interval = max(5, _safe_int(getattr(cfg, "EMBEDDED_MIHOMO_UPDATE_INTERVAL_MINUTES", 60), 60))
        payload = {
            "mixed-port": endpoints["mixed_port"],
            "allow-lan": False,
            "bind-address": "*",
            "mode": "rule",
            "log-level": "info",
            "ipv6": True,
            "external-controller": f"127.0.0.1:{endpoints['controller_port']}",
            "secret": endpoints["secret"],
            "proxy-providers": {
                "primary": {
                    "type": "http",
                    "url": subscription_url,
                    "path": provider_path,
                    "interval": update_interval * 60,
                    "health-check": {
                        "enable": True,
                        "url": test_url,
                        "interval": 600,
                    },
                }
            },
            "proxy-groups": [
                {
                    "name": "自动选择",
                    "type": "url-test",
                    "use": ["primary"],
                    "url": test_url,
                    "interval": 300,
                    "tolerance": 150,
                },
                {
                    "name": group_name,
                    "type": "select",
                    "use": ["primary"],
                    "proxies": ["自动选择", "DIRECT"],
                },
            ],
            "rules": [f"MATCH,{group_name}"],
        }
        return payload

    def render_config(self, subscription_url: Optional[str] = None) -> str:
        active_url = str(subscription_url or self._current_subscription_url() or "").strip()
        if not active_url:
            raise ValueError("未配置 mihomo 订阅地址")
        payload = self._build_config_payload(active_url)
        return yaml.dump(payload, allow_unicode=True, sort_keys=False, default_flow_style=False)

    def write_config(self, subscription_url: Optional[str] = None) -> dict[str, Any]:
        self._ensure_layout()
        active_url = str(subscription_url or self._current_subscription_url() or "").strip()
        if not active_url:
            raise ValueError("未配置 mihomo 订阅地址")
        content = self.render_config(active_url)
        with open(MIHOMO_CONFIG_PATH, "w", encoding="utf-8") as handle:
            handle.write(content)
        self._set_state(subscription_url=active_url)
        return {
            "config_path": MIHOMO_CONFIG_PATH,
            "subscription_url": active_url,
        }

    def _wait_until_ready(self, timeout: int = 15) -> None:
        deadline = time.time() + timeout
        last_error = ""
        while time.time() < deadline:
            try:
                payload = self._request_controller("GET", "/version", timeout=3)
                if payload:
                    return
            except Exception as exc:
                last_error = str(exc)
            time.sleep(0.5)
        raise RuntimeError(last_error or "mihomo 启动超时")

    def start(self, subscription_url: Optional[str] = None) -> dict[str, Any]:
        with self._lock:
            self._cleanup_stale_pid()
            if self.is_running():
                return self.status()
            binary_path = self.get_binary_path()
            if not binary_path:
                raise RuntimeError("未找到 mihomo 可执行文件，请检查 Docker 镜像或 MIHOMO_BINARY_PATH")

            self.write_config(subscription_url)
            if self._log_handle:
                try:
                    self._log_handle.close()
                except Exception:
                    pass
                self._log_handle = None
            self._log_handle = open(MIHOMO_LOG_PATH, "ab")
            process = subprocess.Popen(
                [binary_path, "-d", MIHOMO_DIR, "-f", MIHOMO_CONFIG_PATH],
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
                cwd=MIHOMO_DIR,
            )
            self._process = process
            self._write_pid(process.pid)
            try:
                self._wait_until_ready()
                self._set_state(
                    last_error="",
                    last_started_at=now_iso(),
                )
                self._sync_current_group_selection()
                return self.status()
            except Exception as exc:
                try:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=3)
                except Exception:
                    try:
                        if process.poll() is None:
                            process.kill()
                    except Exception:
                        pass
                self._process = None
                self._clear_pid()
                self._set_state(last_error=str(exc))
                if self._log_handle:
                    try:
                        self._log_handle.close()
                    except Exception:
                        pass
                    self._log_handle = None
                raise

    def stop(self) -> dict[str, Any]:
        with self._lock:
            target_pid = None
            if self._process and self._process.poll() is None:
                target_pid = self._process.pid
            else:
                target_pid = self._read_pid()

            if target_pid and self._pid_matches_mihomo(target_pid):
                try:
                    os.kill(target_pid, signal.SIGTERM)
                except Exception:
                    pass
                deadline = time.time() + 10
                while time.time() < deadline and self._pid_matches_mihomo(target_pid):
                    time.sleep(0.2)
                if self._pid_matches_mihomo(target_pid):
                    try:
                        os.kill(target_pid, signal.SIGKILL)
                    except Exception:
                        pass

            if self._process:
                try:
                    self._process.wait(timeout=1)
                except Exception:
                    pass
            self._process = None
            self._clear_pid()
            if self._log_handle:
                try:
                    self._log_handle.close()
                except Exception:
                    pass
                self._log_handle = None
            self._set_state(last_stopped_at=now_iso(), current_proxy="")
            return self.status()

    def restart(self, subscription_url: Optional[str] = None) -> dict[str, Any]:
        with self._lock:
            self.stop()
            return self.start(subscription_url)

    def is_running(self) -> bool:
        if self._process and self._process.poll() is None:
            return True
        return self._is_process_running()

    def update_subscription(self, subscription_url: Optional[str] = None, restart_if_running: bool = True, reason: str = "manual") -> dict[str, Any]:
        with self._lock:
            active_url = str(subscription_url or self._current_subscription_url() or "").strip()
            if not active_url:
                raise ValueError("未配置 mihomo 订阅地址")
            self.write_config(active_url)
            self._set_state(
                last_updated_at=now_iso(),
                last_update_error="",
                subscription_url=active_url,
            )
            if restart_if_running and self.is_running():
                status = self.restart(active_url)
                status["update_reason"] = reason
                return status
            result = self.status()
            result["update_reason"] = reason
            return result

    def _sync_current_group_selection(self) -> None:
        try:
            groups_payload = self.get_groups()
            selected_group = groups_payload.get("selected_group") or ""
            current_proxy = ""
            if selected_group:
                for group in groups_payload.get("groups", []):
                    if group.get("name") == selected_group:
                        current_proxy = str(group.get("now") or "")
                        break
            self._set_state(current_group=selected_group, current_proxy=current_proxy)
        except Exception:
            pass

    def get_groups(self) -> dict[str, Any]:
        if not self.is_running():
            return {
                "running": False,
                "selected_group": str(getattr(cfg, "EMBEDDED_MIHOMO_GROUP_NAME", "节点选择") or "节点选择"),
                "groups": [],
            }
        payload = self._request_controller("GET", "/proxies")
        proxies_data = payload.get("proxies", {}) if isinstance(payload, dict) else {}
        groups = []
        preferred_group = str(getattr(cfg, "EMBEDDED_MIHOMO_GROUP_NAME", "节点选择") or "节点选择").strip() or "节点选择"
        for name, item in proxies_data.items():
            if not isinstance(item, dict):
                continue
            proxy_type = str(item.get("type") or "")
            if not any(marker in proxy_type.lower() for marker in ["selector", "urltest", "fallback", "loadbalance", "relay"]):
                continue
            groups.append({
                "name": name,
                "type": proxy_type,
                "now": item.get("now") or "",
                "all": item.get("all") or [],
                "alive": item.get("alive", True),
            })
        groups.sort(key=lambda each: each.get("name", ""))
        selected_group = preferred_group
        if groups and not any(group["name"] == preferred_group for group in groups):
            selected_group = groups[0]["name"]
        return {
            "running": True,
            "selected_group": selected_group,
            "groups": groups,
        }

    def select_proxy(self, group_name: str, proxy_name: Optional[str] = None) -> dict[str, Any]:
        if not self.is_running():
            raise RuntimeError("mihomo 尚未启动")
        group_name = str(group_name or "").strip()
        if not group_name:
            raise ValueError("缺少策略组名称")
        encoded_group = urllib.parse.quote(group_name, safe="")
        if proxy_name:
            std_requests.put(
                f"{self.get_runtime_endpoints()['controller_url']}/proxies/{encoded_group}",
                headers=self._headers() or {"Content-Type": "application/json"},
                json={"name": proxy_name},
                timeout=10,
            ).raise_for_status()
        self._set_state(current_group=group_name, current_proxy=str(proxy_name or self._runtime_state.get("current_proxy") or ""))
        return self.get_groups()

    def test_group_delays(self, group_name: Optional[str] = None) -> dict[str, Any]:
        groups_payload = self.get_groups()
        if not groups_payload.get("running"):
            return {"running": False, "group_name": group_name or "", "results": []}
        target_group = str(group_name or groups_payload.get("selected_group") or "").strip()
        group = next((item for item in groups_payload.get("groups", []) if item.get("name") == target_group), None)
        if not group:
            raise ValueError("未找到指定策略组")

        test_url = str(getattr(cfg, "EMBEDDED_MIHOMO_TEST_URL", "https://www.gstatic.com/generate_204") or "").strip()
        if not test_url:
            test_url = "https://www.gstatic.com/generate_204"
        candidates = [
            name for name in list(group.get("all") or [])
            if str(name).upper() not in {"DIRECT", "REJECT"}
        ]
        results = []
        base_url = self.get_runtime_endpoints()["controller_url"].rstrip("/")
        headers = self._headers()

        def run_delay_test(name: str) -> dict[str, Any]:
            encoded_name = urllib.parse.quote(name, safe="")
            delay = None
            error = ""
            try:
                response = std_requests.get(
                    f"{base_url}/proxies/{encoded_name}/delay",
                    headers=headers,
                    params={"timeout": 3000, "url": test_url},
                    timeout=5,
                )
                response.raise_for_status()
                payload = response.json()
                delay = payload.get("delay")
            except Exception as exc:
                error = str(exc)
            return {
                "name": name,
                "delay": delay if isinstance(delay, (int, float)) else None,
                "error": error,
            }

        max_workers = min(10, max(1, len(candidates)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {executor.submit(run_delay_test, name): name for name in candidates}
            for future in as_completed(future_map):
                try:
                    results.append(future.result())
                except Exception as exc:
                    results.append({"name": future_map[future], "delay": None, "error": str(exc)})

        results.sort(key=lambda each: (each["delay"] is None, each["delay"] if each["delay"] is not None else 999999))
        return {
            "running": True,
            "group_name": target_group,
            "test_url": test_url,
            "results": results,
        }

    def logs(self, limit: Optional[int] = None) -> dict[str, Any]:
        line_limit = limit if isinstance(limit, int) and limit > 0 else _safe_int(getattr(cfg, "EMBEDDED_MIHOMO_LOG_LINES", 200), 200)
        return {
            "running": self.is_running(),
            "path": MIHOMO_LOG_PATH,
            "lines": _tail_lines(MIHOMO_LOG_PATH, line_limit),
        }

    def status(self) -> dict[str, Any]:
        self._cleanup_stale_pid()
        endpoints = self.get_runtime_endpoints()
        groups_payload = {"groups": [], "selected_group": ""}
        try:
            groups_payload = self.get_groups()
            current_group = groups_payload.get("selected_group") or self._runtime_state.get("current_group") or ""
            current_proxy = self._runtime_state.get("current_proxy") or ""
            if current_group:
                for group in groups_payload.get("groups", []):
                    if group.get("name") == current_group:
                        current_proxy = str(group.get("now") or current_proxy or "")
                        break
            self._set_state(current_group=current_group, current_proxy=current_proxy)
        except Exception as exc:
            self._set_state(last_error=str(exc))
        return {
            "enabled": bool(getattr(cfg, "EMBEDDED_MIHOMO_ENABLE", False)),
            "mode": str(getattr(cfg, "PROXY_BACKEND_MODE", "external_clash") or "external_clash"),
            "running": self.is_running(),
            "subscription_configured": bool(self._current_subscription_url()),
            "subscription_url": self._current_subscription_url(),
            "binary_path": self.get_binary_path(),
            "data_dir": MIHOMO_DIR,
            "config_path": MIHOMO_CONFIG_PATH,
            "pid": self._read_pid(),
            "mixed_proxy_url": endpoints["mixed_proxy_url"],
            "controller_url": endpoints["controller_url"],
            "controller_secret": endpoints["secret"],
            "group_name": str(getattr(cfg, "EMBEDDED_MIHOMO_GROUP_NAME", "节点选择") or "节点选择"),
            "current_group": self._runtime_state.get("current_group") or groups_payload.get("selected_group") or "",
            "current_proxy": self._runtime_state.get("current_proxy") or "",
            "last_error": self._runtime_state.get("last_error") or "",
            "last_started_at": self._runtime_state.get("last_started_at") or "",
            "last_stopped_at": self._runtime_state.get("last_stopped_at") or "",
            "last_updated_at": self._runtime_state.get("last_updated_at") or "",
            "last_update_error": self._runtime_state.get("last_update_error") or "",
            "auto_update": bool(getattr(cfg, "EMBEDDED_MIHOMO_AUTO_UPDATE", False)),
            "update_interval_minutes": _safe_int(getattr(cfg, "EMBEDDED_MIHOMO_UPDATE_INTERVAL_MINUTES", 60), 60),
            "log_lines": _safe_int(getattr(cfg, "EMBEDDED_MIHOMO_LOG_LINES", 200), 200),
            "groups_count": len(groups_payload.get("groups", [])),
        }

    def _auto_update_loop(self) -> None:
        while True:
            try:
                if (
                    str(getattr(cfg, "PROXY_BACKEND_MODE", "") or "").strip().lower() == "embedded_mihomo"
                    and bool(getattr(cfg, "EMBEDDED_MIHOMO_ENABLE", False))
                    and bool(getattr(cfg, "EMBEDDED_MIHOMO_AUTO_UPDATE", False))
                    and self.is_running()
                    and self._current_subscription_url()
                ):
                    interval_minutes = max(5, _safe_int(getattr(cfg, "EMBEDDED_MIHOMO_UPDATE_INTERVAL_MINUTES", 60), 60))
                    last_updated_at = str(self._runtime_state.get("last_updated_at") or "").strip()
                    last_updated_ts = 0.0
                    if last_updated_at:
                        try:
                            last_updated_ts = datetime.fromisoformat(last_updated_at).timestamp()
                        except Exception:
                            last_updated_ts = 0.0
                    if time.time() - last_updated_ts >= interval_minutes * 60:
                        try:
                            self.update_subscription(restart_if_running=True, reason="auto")
                        except Exception as exc:
                            self._set_state(last_update_error=str(exc), last_error=str(exc))
                self._last_auto_check_at = time.time()
            except Exception:
                pass
            time.sleep(30)

    def ensure_background_worker(self) -> None:
        with self._lock:
            if self._auto_update_thread and self._auto_update_thread.is_alive():
                return
            self._auto_update_thread = threading.Thread(target=self._auto_update_loop, daemon=True, name="embedded-mihomo-auto-update")
            self._auto_update_thread.start()


_MANAGER: Optional[EmbeddedMihomoManager] = None
_MANAGER_LOCK = threading.Lock()


def get_embedded_mihomo_manager() -> EmbeddedMihomoManager:
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = EmbeddedMihomoManager()
            _MANAGER.ensure_background_worker()
        return _MANAGER
