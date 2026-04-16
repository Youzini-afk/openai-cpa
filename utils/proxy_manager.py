import os
import random
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests as std_requests
import yaml

CLASH_API_URL = ""
LOCAL_PROXY_URL = ""
ENABLE_NODE_SWITCH = False
POOL_MODE = False
FASTEST_MODE = False
PROXY_GROUP_NAME = "节点选择"
CLASH_SECRET = ""
NODE_BLACKLIST = []
_IS_IN_DOCKER = os.path.exists("/.dockerenv")
_global_switch_lock = threading.Lock()
_last_switch_time = 0
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(CURRENT_DIR)


def _cfg():
    from utils import config as runtime_cfg

    return runtime_cfg


def _embedded_manager():
    from utils.embedded_mihomo import get_embedded_mihomo_manager

    return get_embedded_mihomo_manager()


def format_docker_url(url: str) -> str:
    """智能检测：如果在 Docker 中运行，自动把 127.0.0.1 转为宿主机魔法地址"""
    if not url or not isinstance(url, str):
        return url
    if _IS_IN_DOCKER:
        if "127.0.0.1" in url:
            return url.replace("127.0.0.1", "host.docker.internal")
        if "localhost" in url:
            return url.replace("localhost", "host.docker.internal")
    return url


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def get_proxy_backend_mode() -> str:
    runtime_cfg = _cfg()
    mode = str(getattr(runtime_cfg, "PROXY_BACKEND_MODE", "external_clash") or "external_clash").strip().lower()
    if mode == "embedded_mihomo" and bool(getattr(runtime_cfg, "EMBEDDED_MIHOMO_ENABLE", False)):
        return "embedded_mihomo"
    return "external_clash"


def is_embedded_mode() -> bool:
    return get_proxy_backend_mode() == "embedded_mihomo"


def _safe_int(value, default: int) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default


def _normalize_qg_host_port(host_value: str, port_value) -> tuple[str, int]:
    host = str(host_value or "").strip()
    port = _safe_int(port_value, 12259)
    if not host:
        return "", port
    try:
        if "://" in host:
            parsed = urllib.parse.urlparse(host)
            host = parsed.hostname or host
            if parsed.port:
                port = parsed.port
        elif host.count(":") == 1:
            maybe_host, maybe_port = host.rsplit(":", 1)
            if maybe_host and maybe_port.isdigit():
                host = maybe_host
                port = int(maybe_port)
    except Exception:
        pass
    return host.strip(), port


def build_qg_dynamic_proxy_url(config_obj: dict = None, mask_password: bool = False) -> str:
    runtime_cfg = _cfg()
    if config_obj is None:
        config_obj = {
            "enable": bool(getattr(runtime_cfg, "QG_DYNAMIC_PROXY_ENABLE", False)),
            "host": str(getattr(runtime_cfg, "QG_DYNAMIC_PROXY_HOST", "") or "").strip(),
            "port": _safe_int(getattr(runtime_cfg, "QG_DYNAMIC_PROXY_PORT", 12259), 12259),
            "auth_key": str(getattr(runtime_cfg, "QG_DYNAMIC_PROXY_AUTH_KEY", "") or "").strip(),
            "auth_pwd": str(getattr(runtime_cfg, "QG_DYNAMIC_PROXY_AUTH_PWD", "") or "").strip(),
            "sticky_session": bool(getattr(runtime_cfg, "QG_DYNAMIC_PROXY_STICKY_SESSION", False)),
            "channel": str(getattr(runtime_cfg, "QG_DYNAMIC_PROXY_CHANNEL", "") or "").strip(),
            "session_seconds": _safe_int(getattr(runtime_cfg, "QG_DYNAMIC_PROXY_SESSION_SECONDS", 120), 120),
            "area_code": str(getattr(runtime_cfg, "QG_DYNAMIC_PROXY_AREA_CODE", "") or "").strip(),
        }

    if not bool(config_obj.get("enable", False)):
        return ""

    host, port = _normalize_qg_host_port(config_obj.get("host", ""), config_obj.get("port", 12259))
    auth_key = str(config_obj.get("auth_key", "") or "").strip()
    auth_pwd = str(config_obj.get("auth_pwd", "") or "").strip()
    sticky_session = bool(config_obj.get("sticky_session", False))
    channel = str(config_obj.get("channel", "") or "").strip()
    session_seconds = max(1, _safe_int(config_obj.get("session_seconds", 120), 120))
    area_code = str(config_obj.get("area_code", "") or "").strip()

    if not host or not port or not auth_key or not auth_pwd:
        return ""

    username = auth_key
    if sticky_session:
        if channel:
            username += f":C{channel}"
        if session_seconds > 0:
            username += f":T{session_seconds}"
        if area_code:
            username += f":A{area_code}"

    encoded_user = urllib.parse.quote(username, safe="")
    encoded_pwd = urllib.parse.quote(("******" if mask_password else auth_pwd), safe="")
    return f"http://{encoded_user}:{encoded_pwd}@{host}:{port}"


def _resolve_runtime_context(proxy_url: str = None) -> dict:
    runtime_cfg = _cfg()
    if is_embedded_mode():
        manager = _embedded_manager()
        endpoints = manager.get_runtime_endpoints()
        return {
            "backend_mode": "embedded_mihomo",
            "enable_switch": ENABLE_NODE_SWITCH,
            "pool_mode": False,
            "fastest_mode": FASTEST_MODE,
            "api_url": endpoints["controller_url"],
            "local_proxy_url": endpoints["mixed_proxy_url"],
            "group_name": str(getattr(runtime_cfg, "EMBEDDED_MIHOMO_GROUP_NAME", PROXY_GROUP_NAME) or PROXY_GROUP_NAME),
            "secret": endpoints["secret"],
            "blacklist": list(NODE_BLACKLIST),
            "display_name": "内置Mihomo",
        }

    current_api_url = CLASH_API_URL
    qg_proxy_url = build_qg_dynamic_proxy_url()
    resolved_local_proxy_url = qg_proxy_url or LOCAL_PROXY_URL
    qg_enabled = bool(qg_proxy_url)
    if POOL_MODE and proxy_url:
        try:
            parsed = urllib.parse.urlparse(proxy_url)
            port = parsed.port
            if port and 41000 < port <= 41050:
                api_port = port + 1000
                current_api_url = format_docker_url(f"http://{parsed.hostname}:{api_port}")
        except Exception:
            pass
    return {
        "backend_mode": "external_clash",
        "enable_switch": False if qg_enabled else ENABLE_NODE_SWITCH,
        "pool_mode": False if qg_enabled else POOL_MODE,
        "fastest_mode": False if qg_enabled else FASTEST_MODE,
        "api_url": "" if qg_enabled else current_api_url,
        "local_proxy_url": resolved_local_proxy_url,
        "group_name": PROXY_GROUP_NAME,
        "secret": "" if qg_enabled else CLASH_SECRET,
        "blacklist": list(NODE_BLACKLIST),
        "display_name": "青果动态代理" if qg_proxy_url and not proxy_url else get_display_name(proxy_url if proxy_url else resolved_local_proxy_url),
    }


def get_effective_default_proxy() -> str:
    runtime_cfg = _cfg()
    if is_embedded_mode():
        endpoints = _embedded_manager().get_runtime_endpoints()
        return endpoints["mixed_proxy_url"]
    qg_proxy_url = build_qg_dynamic_proxy_url()
    if qg_proxy_url:
        return format_docker_url(qg_proxy_url)
    return format_docker_url(getattr(runtime_cfg, "DEFAULT_PROXY", "") or "")


def get_effective_controller_url() -> str:
    return _resolve_runtime_context().get("api_url", "")


def reload_proxy_config():
    global CLASH_API_URL, LOCAL_PROXY_URL, ENABLE_NODE_SWITCH, POOL_MODE, FASTEST_MODE, PROXY_GROUP_NAME, CLASH_SECRET, NODE_BLACKLIST
    config_dir = os.path.join(BASE_DIR, "data")
    config_path = os.path.join(config_dir, "config.yaml")
    if not os.path.exists(config_path):
        print(f"[{ts()}] [WARNING] 配置文件 {config_path} 不存在，使用默认代理设置。")
        conf_data = {}
    else:
        with open(config_path, "r", encoding="utf-8") as handle:
            conf_data = yaml.safe_load(handle) or {}

    clash_conf = conf_data.get("clash_proxy_pool", {})
    ENABLE_NODE_SWITCH = bool(clash_conf.get("enable", False))
    POOL_MODE = bool(clash_conf.get("pool_mode", False))
    FASTEST_MODE = bool(clash_conf.get("fastest_mode", False))
    CLASH_API_URL = format_docker_url(clash_conf.get("api_url", "http://127.0.0.1:9090"))
    LOCAL_PROXY_URL = format_docker_url(clash_conf.get("test_proxy_url", "http://127.0.0.1:7890"))
    PROXY_GROUP_NAME = clash_conf.get("group_name", "节点选择")
    CLASH_SECRET = clash_conf.get("secret", "")
    NODE_BLACKLIST = clash_conf.get("blacklist", ["港", "HK", "台", "TW", "中国", "CN"])
    print(f"[{ts()}] [系统] 代理管理模块配置已同步更新。当前模式: {get_proxy_backend_mode()}")


def clean_for_log(text: str) -> str:
    emoji_pattern = re.compile(
        r"[\U0001F1E6-\U0001F1FF]"
        r"|[\U0001F300-\U0001F6FF]"
        r"|[\U0001F900-\U0001F9FF]"
        r"|[\U00002600-\U000027BF]"
        r"|[\uFE0F]"
    )
    return emoji_pattern.sub("", text).strip()


def get_display_name(proxy_url: str) -> str:
    if not proxy_url:
        return "全局单机"
    try:
        parsed = urllib.parse.urlparse(proxy_url)
        if parsed.port and 41000 < parsed.port <= 41050:
            return f"{parsed.port - 41000}号机"
        return f"端口:{parsed.port}"
    except Exception:
        return "未知通道"


def get_api_url_for_proxy(proxy_url: str) -> str:
    return _resolve_runtime_context(proxy_url).get("api_url", "")


def test_proxy_liveness(proxy_url=None):
    context = _resolve_runtime_context(proxy_url)
    raw_url = proxy_url if proxy_url else context.get("local_proxy_url", "")
    if not raw_url:
        print(f"[{ts()}] [代理测活] 未配置可用代理地址。")
        return False
    target_proxy = raw_url if context["backend_mode"] == "embedded_mihomo" else format_docker_url(raw_url)
    proxies = {"http": target_proxy, "https": target_proxy}
    display_name = context["display_name"] if context["backend_mode"] == "embedded_mihomo" else get_display_name(proxy_url if proxy_url else raw_url)

    try:
        res = std_requests.get("https://cloudflare.com/cdn-cgi/trace", proxies=proxies, timeout=5)
        if res.status_code == 200:
            loc = "UNKNOWN"
            for line in res.text.split("\n"):
                if line.startswith("loc="):
                    loc = line.split("=")[1].strip()

            blocked_regions = ["CN", "HK"]
            if loc in blocked_regions:
                print(f"[{ts()}] [代理测活] {display_name} 地区受限 ({loc})，弃用！")
                return False

            print(f"[{ts()}] [代理测活] {display_name} 成功！地区 ({loc})，延迟: {res.elapsed.total_seconds():.2f}s")
            return True
        return False
    except Exception:
        print(f"[{ts()}] [代理测活] {display_name} 链路中断或超时。")
        return False


def smart_switch_node(proxy_url=None):
    global _last_switch_time
    context = _resolve_runtime_context(proxy_url)
    if not context["enable_switch"]:
        return True

    if context["backend_mode"] == "external_clash" and context["pool_mode"] and proxy_url:
        return _do_smart_switch(proxy_url)

    with _global_switch_lock:
        if time.time() - _last_switch_time < 10:
            print(f"[{ts()}] [代理池] 其他线程刚完成切换，跳过本次请求...")
            return True

        success = _do_smart_switch(proxy_url)
        if success:
            _last_switch_time = time.time()
        return success


def _do_smart_switch(proxy_url=None):
    context = _resolve_runtime_context(proxy_url)
    if not context["enable_switch"]:
        return True

    if context["backend_mode"] == "embedded_mihomo" and not _embedded_manager().is_running():
        print(f"[{ts()}] [ERROR] 内置 Mihomo 未启动，无法执行节点切换。")
        return False

    current_api_url = context["api_url"]
    headers = {"Authorization": f"Bearer {context['secret']}"} if context["secret"] else {}
    display_name = context["display_name"]
    api_display = "内置Mihomo API" if context["backend_mode"] == "embedded_mihomo" else get_display_name(current_api_url).replace("号机", "号API")

    try:
        resp = std_requests.get(f"{current_api_url}/proxies", headers=headers, timeout=5)
        if resp.status_code != 200:
            print(f"[{ts()}] [ERROR] 无法连接 Clash API ({api_display})，请检查容器状态。")
            return False

        proxies_data = resp.json().get("proxies", {})

        actual_group_name = None
        for key in proxies_data.keys():
            if context["group_name"] in key and isinstance(proxies_data[key], dict) and "all" in proxies_data[key]:
                actual_group_name = key
                break

        if not actual_group_name:
            print(f"[{ts()}] [ERROR] {display_name} 找不到策略组关键词 '{context['group_name']}'")
            return False

        safe_group_name = urllib.parse.quote(actual_group_name, safe="")
        all_nodes = proxies_data[actual_group_name].get("all", [])
        valid_nodes = [node for node in all_nodes if not any(keyword.upper() in node.upper() for keyword in context["blacklist"])]

        if not valid_nodes:
            print(f"[{ts()}] [ERROR] {display_name} 过滤后无可用节点，请检查黑名单。")
            return False

        if context["fastest_mode"]:
            print(f"\n[{ts()}] [代理池] {display_name} 开启优选模式，并发测速 {len(valid_nodes)} 个节点...")
            session = std_requests.Session()

            def trigger_delay(name):
                encoded_name = urllib.parse.quote(name, safe="")
                try:
                    session.get(
                        f"{current_api_url}/proxies/{encoded_name}/delay?timeout=2000&url=https://www.gstatic.com/generate_204",
                        headers=headers,
                        timeout=2.5,
                    )
                except Exception:
                    pass

            thread_count = min(10, len(valid_nodes))
            with ThreadPoolExecutor(max_workers=thread_count) as executor:
                executor.map(trigger_delay, valid_nodes)
            session.close()
            time.sleep(1.5)

            try:
                resp2 = std_requests.get(f"{current_api_url}/proxies", headers=headers, timeout=5)
                if resp2.status_code == 200:
                    proxies_snapshot = resp2.json().get("proxies", {})
                    best_node = None
                    min_delay = float("inf")
                    for node in valid_nodes:
                        history = proxies_snapshot.get(node, {}).get("history", [])
                        if history:
                            delay = history[-1].get("delay", 0)
                            if 0 < delay < min_delay:
                                min_delay = delay
                                best_node = node

                    if best_node:
                        print(f"[{ts()}] [代理池] {display_name} 测速完成，最快节点: [{clean_for_log(best_node)}] ({min_delay}ms)")
                        switch_resp = std_requests.put(
                            f"{current_api_url}/proxies/{safe_group_name}",
                            headers=headers,
                            json={"name": best_node},
                            timeout=5,
                        )
                        if switch_resp.status_code == 204:
                            time.sleep(1)
                            if context["backend_mode"] == "embedded_mihomo":
                                _embedded_manager().select_proxy(actual_group_name, best_node)
                            if test_proxy_liveness(proxy_url):
                                return True
                            print(f"[{ts()}] [代理池] {display_name} 最快节点测活失败，回退到随机抽卡模式...")
                    else:
                        print(f"[{ts()}] [代理池] {display_name} 所有节点均超时，回退到随机抽卡模式...")
            except Exception as exc:
                print(f"[{ts()}] [代理池] {display_name} 优选模式异常: {exc}，回退到随机抽卡模式...")

        max_retries = 10
        for index in range(1, max_retries + 1):
            selected_node = random.choice(valid_nodes)
            print(f"\n[{ts()}] [代理池] {display_name} 尝试切换节点: [{clean_for_log(selected_node)}] ({index}/{max_retries})")
            switch_resp = std_requests.put(
                f"{current_api_url}/proxies/{safe_group_name}",
                headers=headers,
                json={"name": selected_node},
                timeout=5,
            )
            if switch_resp.status_code == 204:
                time.sleep(1.5)
                if context["backend_mode"] == "embedded_mihomo":
                    _embedded_manager().select_proxy(actual_group_name, selected_node)
                if test_proxy_liveness(proxy_url):
                    return True
                print(f"[{ts()}] [代理池] {display_name} 测活失败，重新抽卡...")
            else:
                print(f"[{ts()}] [代理池] {display_name} 指令下发失败 (HTTP {switch_resp.status_code})。")

        print(f"\n[{ts()}] [代理池] {display_name} 连续 10 次抽卡均不可用！")
        return False
    except Exception as exc:
        print(f"[{ts()}] [ERROR] {display_name} 切换节点异常: {exc}")
        return False


reload_proxy_config()
