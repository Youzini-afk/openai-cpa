import os
import time
import json
import secrets
import re
import asyncio
import traceback
import threading
import sys
import subprocess
import yaml
import urllib.parse
import httpx
from fastapi import APIRouter, Depends, Header, Query, Request, WebSocket, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from typing import List, Optional, Any
from cloudflare import Cloudflare
from utils import core_engine, db_manager
from utils.config import reload_all_configs
from utils.embedded_mihomo import get_embedded_mihomo_manager
from utils.integrations.codex2api_client import Codex2APIClient
from utils.integrations.sub2api_client import Sub2APIClient
from utils.integrations.tg_notifier import send_tg_msg_async
from utils.email_providers.gmail_oauth_handler import GmailOAuthHandler
from utils.proxy_manager import (
    build_qg_dynamic_proxy_url,
    get_effective_controller_url,
    get_effective_default_proxy,
    get_proxy_backend_mode,
    get_qg_short_proxy_status,
    is_embedded_mode,
)
from curl_cffi import requests as cffi_requests
from global_state import VALID_TOKENS, CLUSTER_NODES, NODE_COMMANDS, cluster_lock, log_history, engine, verify_token, worker_status
import utils.config as cfg

router = APIRouter()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "data", "config.yaml")
GMAIL_CLIENT_SECRETS = os.path.join(BASE_DIR, "data", "credentials.json")
GMAIL_TOKEN_PATH = os.path.join(BASE_DIR, "data", "token.json")
GMAIL_VERIFIER_PATH = os.path.join(BASE_DIR, "data", "temp_verifier.txt")

class DummyArgs:
    def __init__(self, proxy=None, once=False):
        self.proxy = proxy
        self.once = once


class ExportReq(BaseModel): emails: list[str]


class DeleteReq(BaseModel): emails: list[str]


class LoginData(BaseModel): password: str


class CFSyncExistingReq(BaseModel): sub_domains: str; api_email: str; api_key: str


class LuckMailBulkBuyReq(BaseModel): quantity: int; auto_tag: bool; config: dict


class SMSPriceReq(BaseModel): service: str = "openai"


class GmailExchangeReq(BaseModel): code: str


class CloudAccountItem(BaseModel): id: str; type: str


class CloudActionReq(BaseModel): accounts: List[CloudAccountItem]; action: str


class ClusterUploadAccountsReq(BaseModel): node_name: str; secret: str; accounts: list


class ClusterReportReq(BaseModel): node_name: str; secret: str; stats: dict; logs: list


class ClusterControlReq(BaseModel): node_name: str; action: str

class ExtResultReq(BaseModel):
    status: str
    task_id: Optional[str] = ""
    email: Optional[str] = ""
    password: Optional[str] = ""
    error_msg: Optional[str] = ""
    token_data: Optional[str] = ""
    callback_url: Optional[str] = ""
    code_verifier: Optional[str] = ""
    expected_state: Optional[str] = ""
    error_type: Optional[str] = "failed"

class ImportMailboxReq(BaseModel):
    raw_text: str

class DeleteMailboxReq(BaseModel):
    ids: list[Any]

class OutlookAuthUrlReq(BaseModel):
    client_id: str

class OutlookExchangeReq(BaseModel):
    email: str
    client_id: str
    code_or_url: str

class UpdateMailboxStatusReq(BaseModel):
    emails: list[str]
    status: int


class ProxySubscriptionReq(BaseModel):
    subscription_url: str


class ProxySelectReq(BaseModel):
    group_name: str
    proxy_name: Optional[str] = None


class ProxyDelayTestReq(BaseModel):
    group_name: Optional[str] = ""
# ==========================================
# 辅助函数
# ==========================================
def get_web_password():
    env_password = str(
        os.getenv("WEB_PASSWORD")
        or os.getenv("WENFXL_WEB_PASSWORD")
        or ""
    ).strip()
    if env_password:
        return env_password
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                c = yaml.safe_load(f) or {}
                return str(c.get("web_password", "admin")).strip()
    except Exception:
        pass
    return "admin"


def load_config_data() -> dict:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}
    return {}


def ensure_proxy_config_defaults(config_data: dict) -> dict:
    def safe_int(value, default):
        try:
            return int(str(value).strip())
        except Exception:
            return default

    if "qg_short_proxy" not in config_data or not isinstance(config_data.get("qg_short_proxy"), dict):
        config_data["qg_short_proxy"] = {}
    qg_short_proxy = config_data["qg_short_proxy"]
    qg_short_proxy.setdefault("enable", False)
    qg_short_proxy.setdefault("extract_url", "")
    qg_short_proxy.setdefault("auth_username", "")
    qg_short_proxy.setdefault("auth_password", "")
    qg_short_proxy.setdefault("refresh_before_expire_seconds", 5)
    qg_short_proxy.setdefault("request_timeout_seconds", 10)
    qg_short_proxy.setdefault("max_retry_candidates", 3)
    qg_short_proxy.setdefault("probe_timeout_seconds", 10)

    if "cpa_mode" not in config_data or not isinstance(config_data.get("cpa_mode"), dict):
        config_data["cpa_mode"] = {}
    cpa_mode = config_data["cpa_mode"]
    cpa_mode.setdefault("enable", False)
    cpa_mode.setdefault("auto_check", True)
    cpa_mode.setdefault("save_to_local", True)
    cpa_mode.setdefault("api_url", "")
    cpa_mode.setdefault("api_token", "")
    cpa_mode.setdefault("min_accounts_threshold", 30)
    cpa_mode.setdefault("batch_reg_count", 1)
    cpa_mode.setdefault("min_remaining_weekly_percent", 80)
    cpa_mode.setdefault("remove_on_limit_reached", False)
    cpa_mode.setdefault("remove_dead_accounts", False)
    cpa_mode.setdefault("enable_token_revive", False)
    cpa_mode.setdefault("check_interval_minutes", 60)
    cpa_mode.setdefault("threads", 10)

    if "sub2api_mode" not in config_data or not isinstance(config_data.get("sub2api_mode"), dict):
        config_data["sub2api_mode"] = {}
    sub2api_mode = config_data["sub2api_mode"]
    sub2api_mode.setdefault("enable", False)
    sub2api_mode.setdefault("auto_check", True)
    sub2api_mode.setdefault("save_to_local", True)
    sub2api_mode.setdefault("api_url", "")
    sub2api_mode.setdefault("api_key", "")
    sub2api_mode.setdefault("min_accounts_threshold", 20)
    sub2api_mode.setdefault("batch_reg_count", 1)
    sub2api_mode.setdefault("remove_on_limit_reached", True)
    sub2api_mode.setdefault("remove_dead_accounts", True)
    sub2api_mode.setdefault("enable_token_revive", False)
    sub2api_mode.setdefault("check_interval_minutes", 60)
    sub2api_mode.setdefault("threads", 10)
    sub2api_mode.setdefault("account_concurrency", 10)
    sub2api_mode.setdefault("account_load_factor", 10)
    sub2api_mode.setdefault("account_priority", 1)
    sub2api_mode.setdefault("account_rate_multiplier", 1.0)
    sub2api_mode.setdefault("account_group_ids", "")
    sub2api_mode.setdefault("enable_ws_mode", True)
    sub2api_mode.setdefault("test_model", "GPT-5.2")

    if "codex2api_mode" not in config_data or not isinstance(config_data.get("codex2api_mode"), dict):
        config_data["codex2api_mode"] = {}
    codex2api_mode = config_data["codex2api_mode"]
    codex2api_mode.setdefault("enable", False)
    codex2api_mode.setdefault("api_url", "")
    codex2api_mode.setdefault("admin_key", "")
    codex2api_mode.setdefault("push_source", "register-oss")
    codex2api_mode.setdefault("threads", 10)

    if "neuralwatt_mode" not in config_data or not isinstance(config_data.get("neuralwatt_mode"), dict):
        config_data["neuralwatt_mode"] = {}
    nw_mode = config_data["neuralwatt_mode"]
    nw_mode.setdefault("enable", False)
    nw_mode.setdefault("turnstile_service", "")
    nw_mode.setdefault("turnstile_api_key", "")
    nw_mode.setdefault("auto_create_api_key", True)
    nw_mode.setdefault("test_model", "meta-llama/Llama-3.3-70B-Instruct")
    nw_mode.setdefault("verify_max_attempts", 30)
    nw_mode.setdefault("threads", 10)
    nw_mode.setdefault("check_interval_minutes", 60)
    nw_mode.setdefault("min_accounts_threshold", 20)
    nw_mode.setdefault("batch_reg_count", 1)

    if "auto_push" not in config_data or not isinstance(config_data.get("auto_push"), dict):
        config_data["auto_push"] = {}
    auto_push = config_data["auto_push"]
    auto_push.setdefault("cpa", False)
    auto_push.setdefault("sub2api", False)
    auto_push.setdefault("codex2api", False)
    auto_push.setdefault("neuralwatt", False)

    if "qg_dynamic_proxy" not in config_data or not isinstance(config_data.get("qg_dynamic_proxy"), dict):
        config_data["qg_dynamic_proxy"] = {}
    qg_proxy = config_data["qg_dynamic_proxy"]
    qg_proxy.setdefault("enable", False)
    qg_proxy.setdefault("host", "")
    qg_proxy.setdefault("port", 12259)
    qg_proxy.setdefault("auth_key", "")
    qg_proxy.setdefault("auth_pwd", "")
    qg_proxy.setdefault("sticky_session", False)
    qg_proxy.setdefault("channel", "")
    qg_proxy.setdefault("session_seconds", 120)
    qg_proxy.setdefault("area_code", "")

    if "proxy_backend" not in config_data or not isinstance(config_data.get("proxy_backend"), dict):
        config_data["proxy_backend"] = {"mode": "external_clash"}
    config_data["proxy_backend"]["mode"] = str(config_data["proxy_backend"].get("mode", "external_clash") or "external_clash")

    if "embedded_mihomo" not in config_data or not isinstance(config_data.get("embedded_mihomo"), dict):
        config_data["embedded_mihomo"] = {}
    embedded = config_data["embedded_mihomo"]
    embedded.setdefault("enable", False)
    embedded.setdefault("subscription_url", "")
    embedded.setdefault("auto_update", False)
    embedded.setdefault("update_interval_minutes", 60)
    embedded.setdefault("mixed_port", 7897)
    embedded.setdefault("controller_port", 9097)
    embedded.setdefault("secret", "openai-cpa-mihomo")
    embedded.setdefault("group_name", "节点选择")
    embedded.setdefault("test_url", "https://www.gstatic.com/generate_204")
    embedded.setdefault("log_lines", 200)

    if "clash_proxy_pool" not in config_data or not isinstance(config_data.get("clash_proxy_pool"), dict):
        config_data["clash_proxy_pool"] = {}
    clash_pool = config_data["clash_proxy_pool"]
    clash_pool.setdefault("enable", False)
    clash_pool.setdefault("pool_mode", False)
    clash_pool.setdefault("fastest_mode", False)
    clash_pool.setdefault("api_url", "http://127.0.0.1:9097")
    clash_pool.setdefault("group_name", "节点选择")
    clash_pool.setdefault("secret", "")
    clash_pool.setdefault("test_proxy_url", "")
    clash_pool.setdefault("blacklist", ["港", "HK", "台", "TW", "中国", "CN"])
    return config_data


def save_config_data(new_config: dict) -> None:
    with core_engine.cfg.CONFIG_FILE_LOCK:
        with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
            yaml.dump(new_config, handle, allow_unicode=True, sort_keys=False, default_flow_style=False)


def update_proxy_config(mutator):
    config_data = ensure_proxy_config_defaults(load_config_data())
    mutator(config_data)
    save_config_data(config_data)
    reload_all_configs()
    return config_data


def ensure_embedded_proxy_ready():
    if not is_embedded_mode():
        return None
    manager = get_embedded_mihomo_manager()
    if manager.is_running():
        return manager
    manager.start()
    return manager


def get_proxy_controller_secret() -> str:
    if is_embedded_mode():
        return str(get_embedded_mihomo_manager().get_runtime_endpoints().get("secret") or "").strip()
    clash_conf = (getattr(cfg, "_c", {}) or {}).get("clash_proxy_pool", {}) if hasattr(cfg, "_c") else {}
    return str(clash_conf.get("secret", "") or "").strip()


def request_proxy_controller(method: str, path: str, **kwargs):
    controller_url = str(get_effective_controller_url() or "").strip().rstrip("/")
    if not controller_url:
        raise RuntimeError("未配置可用的代理控制器地址")
    url = f"{controller_url}/{path.lstrip('/')}"
    headers = dict(kwargs.pop("headers", {}) or {})
    secret = get_proxy_controller_secret()
    if secret and "Authorization" not in headers:
        headers["Authorization"] = f"Bearer {secret}"
    response = httpx.request(method, url, headers=headers, timeout=kwargs.pop("timeout", 10), **kwargs)
    response.raise_for_status()
    if not response.content:
        return {}
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        return response.json()
    return response.text


def parse_proxy_groups_from_payload(payload: dict) -> dict:
    proxies_map = payload.get("proxies", {}) if isinstance(payload, dict) else {}
    groups = []
    selected_group = ""
    preferred_group = ""
    if is_embedded_mode():
        preferred_group = str(getattr(cfg, "EMBEDDED_MIHOMO_GROUP_NAME", "节点选择") or "节点选择").strip()
    else:
        clash_conf = (getattr(cfg, "_c", {}) or {}).get("clash_proxy_pool", {}) if hasattr(cfg, "_c") else {}
        preferred_group = str(clash_conf.get("group_name", "节点选择") or "节点选择").strip()

    for name, item in proxies_map.items():
        if not isinstance(item, dict):
            continue
        proxy_type = str(item.get("type") or "")
        if not any(flag in proxy_type.lower() for flag in ["selector", "urltest", "fallback", "loadbalance", "relay"]):
            continue
        groups.append({
            "name": name,
            "type": proxy_type,
            "now": item.get("now") or "",
            "all": item.get("all") or [],
            "alive": item.get("alive", True),
        })
    groups.sort(key=lambda each: each.get("name", ""))
    if groups:
        selected_group = next((group["name"] for group in groups if preferred_group and preferred_group in group["name"]), groups[0]["name"])
    return {
        "groups": groups,
        "selected_group": selected_group,
    }

def parse_cpa_usage_to_details(raw_usage: dict) -> dict:
    details = {"is_cpa": True}
    try:
        payload = raw_usage
        if "body" in raw_usage and isinstance(raw_usage["body"], str):
            try:
                payload = json.loads(raw_usage["body"])
            except:
                pass
        details["cpa_plan_type"] = str(payload.get("plan_type", "未知")).upper()
        total = payload.get("total_granted") or payload.get("hard_limit_usd") or payload.get("total")
        used = payload.get("total_used") or payload.get("total_usage") or payload.get("used")
        if total is not None and used is not None:
            total_val = float(total)
            used_val = float(used)
            details["cpa_total"] = f"${total_val:.2f}"
            details["cpa_remaining"] = f"${max(0.0, total_val - used_val):.2f}"
        else:
            details["cpa_total"] = "100%"
            details["cpa_remaining"] = "未知"

        rate_limit = payload.get("rate_limit", {})
        if isinstance(rate_limit, dict):
            primary = rate_limit.get("primary_window", {})
            if primary:
                p_remain = primary.get("remaining_percent")
                if p_remain is None and primary.get("used_percent") is not None:
                    p_remain = 100.0 - float(primary.get("used_percent"))
                details["cpa_primary_remain_pct"] = round(float(p_remain if p_remain is not None else 100.0), 1)

        code_review = payload.get("code_review_rate_limit", {})
        if isinstance(code_review, dict):
            c_primary = code_review.get("primary_window", {})
            if c_primary:
                c_remain = c_primary.get("remaining_percent")
                if c_remain is None and c_primary.get("used_percent") is not None:
                    c_remain = 100.0 - float(c_primary.get("used_percent"))
                details["cpa_codex_remain_pct"] = round(float(c_remain if c_remain is not None else 100.0), 1)

        details["cpa_used_percent"] = round(100.0 - details.get("cpa_primary_remain_pct", 100.0), 1)
        return details
    except Exception as e:
        print(f"[DEBUG] 解析CPA用量异常: {e}")
    details["cpa_total"] = "0.00";
    details["cpa_remaining"] = "0.00";
    details["cpa_used_percent"] = 0.0;
    details["cpa_plan_type"] = "未知"
    return details


def normalize_cloud_time(raw_time: Any) -> str:
    raw = str(raw_time or "").strip()
    if not raw:
        return "-"
    try:
        return raw.split(".")[0].replace("T", " ")
    except Exception:
        return raw


def map_codex2api_status(item: dict) -> str:
    if bool(item.get("locked")):
        return "disabled"

    raw_status = str(item.get("status") or "").strip().lower()
    if raw_status in ("", "active", "ready", "ok"):
        return "active"
    return "dead"


def build_codex2api_details(item: dict) -> dict:
    def as_percent(value: Any) -> float:
        try:
            return round(float(value or 0), 1)
        except Exception:
            return 0.0

    return {
        "plan_type": item.get("plan_type", "未知"),
        "health_tier": item.get("health_tier", ""),
        "proxy_url": item.get("proxy_url", ""),
        "at_only": bool(item.get("at_only")),
        "locked": bool(item.get("locked")),
        "codex_5h_used_percent": as_percent(item.get("usage_percent_5h")),
        "codex_7d_used_percent": as_percent(item.get("usage_percent_7d")),
        "codex_5h_reset_at": item.get("reset_5h_at", ""),
        "codex_7d_reset_at": item.get("reset_7d_at", ""),
    }

@router.get("/")
async def get_dashboard():
    version = "1.0.0"
    js_path = os.path.join(BASE_DIR, "static", "js", "app.js")
    try:
        if os.path.exists(js_path):
            with open(js_path, "r", encoding="utf-8") as f:
                match = re.search(r"appVersion:\s*['\"]([^'\"]+)['\"]", f.read())
                if match: version = match.group(1)
    except Exception:
        pass

    html_path = os.path.join(BASE_DIR, "index.html")
    if not os.path.exists(html_path): return HTMLResponse(content="<h1>找不到 index.html</h1>", status_code=404)

    with open(html_path, "r", encoding="utf-8") as f:
        html_content = f.read()
    return HTMLResponse(content=html_content.replace("__VER__", version),
                        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


@router.post("/api/login")
async def login(data: LoginData):
    if data.password == get_web_password():
        token = secrets.token_hex(16)
        VALID_TOKENS.add(token)
        return {"status": "success", "token": token}
    return {"status": "error", "message": "密码错误"}


@router.get("/api/status")
async def get_status(token: str = Depends(verify_token)):
    return {"is_running": engine.is_running()}

@router.post("/api/start")
async def start_task(token: str = Depends(verify_token)):
    if engine.is_running(): return {"status": "error", "message": "任务已经在运行中！"}
    try:
        reload_all_configs()
    except Exception as e:
        print(f"[{core_engine.ts()}] [警告] 启动重载提示: {e}")

    try:
        ensure_embedded_proxy_ready()
    except Exception as e:
        return {"status": "error", "message": f"内置 Mihomo 启动失败: {e}"}

    default_proxy = get_effective_default_proxy()
    args = DummyArgs(proxy=default_proxy if default_proxy else None)
    core_engine.run_stats.update({"success": 0, "failed": 0, "retries": 0, "pwd_blocked": 0, "phone_verify": 0, "start_time": time.time()})
    if getattr(core_engine.cfg, 'ENABLE_CPA_MODE', False):
        core_engine.run_stats["target"] = 0
        engine.start_cpa(args)
        return {"status": "success", "message": "启动成功：已自动识别并开启 [CPA 智能仓管模式]"}
    elif getattr(core_engine.cfg, 'ENABLE_SUB2API_MODE', False):
        engine.start_sub2api(args)
        return {"status": "success", "message": "启动成功：已自动识别并开启 [Sub2API 仓管模式]"}
    elif getattr(core_engine.cfg, 'ENABLE_NEURALWATT_MODE', False):
        core_engine.run_stats["target"] = 0
        engine.start_neuralwatt(args)
        return {"status": "success", "message": "启动成功：已自动识别并开启 [Neuralwatt 仓管模式]"}
    else:
        core_engine.run_stats["target"] = core_engine.cfg.NORMAL_TARGET_COUNT
        engine.start_normal(args)
        return {"status": "success", "message": "启动成功：已自动识别并开启 [常规量产模式]"}


@router.post("/api/stop")
async def stop_task(token: str = Depends(verify_token)):
    if not engine.is_running(): return {"status": "warning", "message": "当前没有运行的任务"}
    stats = core_engine.run_stats
    elapsed_time = round(time.time() - stats["start_time"], 1) if stats["start_time"] > 0 else 0
    total_attempts = stats["success"] + stats["failed"]
    success_rate = round((stats["success"] / total_attempts * 100), 2) if total_attempts > 0 else 0.0
    avg_time = round(elapsed_time / stats["success"], 1) if stats["success"] > 0 else 0.0
    target_str = stats["target"] if stats["target"] > 0 else "∞"
    template_str = getattr(core_engine.cfg, 'TG_BOT', {}).get("template_stop", "🛑 停止：成功 {success}/{target}")
    pwd_blocked = stats["pwd_blocked"] if stats["pwd_blocked"] > 0 else 0
    phone_blocked = stats["phone_verify"] if stats["phone_verify"] > 0 else 0

    try:
        msg = template_str.format(success_rate=success_rate, success=stats['success'], target=target_str,
                                  failed=stats['failed'], retries=stats['retries'], elapsed_time=elapsed_time,
                                  pwd_blocked=pwd_blocked,phone_verify=phone_blocked,avg_time=avg_time)
    except Exception:
        msg = f"⚠️ TG 模板渲染出错：未知的变量格式。\n请检查配置面板中的模板变量是否正确填写。"

    asyncio.create_task(send_tg_msg_async(msg))
    engine.stop()
    return {"status": "success", "message": "已发送停止指令，正在安全退出..."}


@router.get("/api/stats")
async def get_stats(token: str = Depends(verify_token)):
    stats = core_engine.run_stats
    is_running = engine.is_running()
    current_reg_mode = getattr(core_engine.cfg, 'REG_MODE', 'protocol')

    if current_reg_mode == 'extension':
        is_running = stats.get("ext_is_running", False)
    else:
        is_running = engine.is_running()

    if is_running or (current_reg_mode == 'extension' and stats["start_time"] > 0):
        elapsed = round(time.time() - stats["start_time"], 1) if stats.get("start_time", 0) > 0 else 0
        stats["_frozen_elapsed"] = elapsed
    else:
        elapsed = stats.get("_frozen_elapsed", 0)

    total_attempts = stats["success"] + stats["failed"]
    success_rate = round((stats["success"] / total_attempts * 100), 2) if total_attempts > 0 else 0.0
    avg_time = round(elapsed / stats["success"], 1) if stats["success"] > 0 else 0.0

    progress_pct = 0
    if stats["target"] > 0:
        progress_pct = min(100, round((stats["success"] / stats["target"]) * 100, 1))
    elif stats["success"] > 0:
        progress_pct = 100
    if current_reg_mode == 'extension':
        current_mode = "插件托管 (古法)"
    else:
        current_mode = "CPA 仓管" if getattr(core_engine.cfg, 'ENABLE_CPA_MODE', False) else (
            "Sub2Api 仓管" if getattr(core_engine.cfg, 'ENABLE_SUB2API_MODE', False) else (
                "Neuralwatt 仓管" if getattr(core_engine.cfg, 'ENABLE_NEURALWATT_MODE', False) else "常规量产"))

    return {
        "success": stats["success"], "failed": stats["failed"], "retries": stats["retries"],
        "pwd_blocked": stats.get("pwd_blocked", 0), "phone_verify": stats.get("phone_verify", 0),
        "total": total_attempts, "target": stats["target"] if stats["target"] > 0 else "∞",
        "success_rate": f"{success_rate}%", "elapsed": f"{elapsed}s", "avg_time": f"{avg_time}s",
        "progress_pct": f"{progress_pct}%", "is_running": is_running, "mode": current_mode
    }


@router.post("/api/start_check")
async def start_check_api(token: str = Depends(verify_token)):
    if engine.is_running(): return {"code": 400, "message": "系统正在运行中，请先停止主任务！"}
    try:
        ensure_embedded_proxy_ready()
    except Exception as e:
        return {"code": 400, "message": f"内置 Mihomo 启动失败: {e}"}
    default_proxy = get_effective_default_proxy()
    engine.start_check(DummyArgs(proxy=default_proxy if default_proxy else None))
    return {"code": 200, "message": "独立测活指令已下发！"}


@router.post("/api/system/restart")
async def restart_system(token: str = Depends(verify_token)):
    try:
        if engine.is_running(): engine.stop()

        def _do_restart():
            time.sleep(1.5)
            print(f"[{core_engine.ts()}] [系统] 🔄 正在执行重启命令...")
            try:
                sys.stdout.flush()
                sys.stderr.flush()
                subprocess.Popen([sys.executable] + sys.argv)
                os._exit(0)
            except Exception as e:
                print(f"[{core_engine.ts()}] [系统] ❌ 重启失败: {e}")
                os._exit(1)

        threading.Thread(target=_do_restart, daemon=True).start()
        return {"status": "success", "message": "指令已下发，系统即将重启..."}
    except Exception as e:
        return {"status": "error", "message": f"重启异常: {str(e)}"}

@router.get("/api/config")
async def get_config(token: str = Depends(verify_token)):
    config_data = ensure_proxy_config_defaults(load_config_data())
    if isinstance(config_data.get("sub2api_mode"), dict):
        config_data["sub2api_mode"].pop("min_remaining_weekly_percent", None)
    config_data["web_password"] = get_web_password()
    if "local_microsoft" not in config_data:
        config_data["local_microsoft"] = {
            "enable_fission": False,
            "master_email": "",
            "client_id": "",
            "refresh_token": ""
        }


    return config_data


@router.post("/api/config")
async def save_config(new_config: dict, token: str = Depends(verify_token)):
    try:
        current_config = ensure_proxy_config_defaults(load_config_data())
        if isinstance(new_config.get("sub2api_mode"), dict):
            new_config["sub2api_mode"].pop("min_remaining_weekly_percent", None)
        new_config = ensure_proxy_config_defaults(new_config)
        save_config_data(new_config)
        reload_all_configs()
        proxy_changed = (
            current_config.get("proxy_backend") != new_config.get("proxy_backend")
            or current_config.get("embedded_mihomo") != new_config.get("embedded_mihomo")
        )
        if proxy_changed:
            manager = get_embedded_mihomo_manager()
            if is_embedded_mode():
                if manager.is_running():
                    manager.restart()
            elif manager.is_running():
                manager.stop()
        return {"status": "success", "message": "✅ 配置已成功保存！"}
    except Exception as e:
        return {"status": "error", "message": f"❌ 保存失败: {str(e)}"}


@router.get("/api/proxy/status")
async def get_proxy_status(token: str = Depends(verify_token)):
    def safe_int(value, default):
        try:
            return int(str(value).strip())
        except Exception:
            return default

    mode = get_proxy_backend_mode()
    embedded_status = get_embedded_mihomo_manager().status()
    qg_short_conf = ((getattr(cfg, "_c", {}) or {}).get("qg_short_proxy", {}) or {})
    qg_conf = ((getattr(cfg, "_c", {}) or {}).get("qg_dynamic_proxy", {}) or {})
    qg_short_status = get_qg_short_proxy_status()
    return {
        "status": "success",
        "mode": mode,
        "effective_default_proxy": get_effective_default_proxy(),
        "effective_controller_url": get_effective_controller_url(),
        "embedded": embedded_status,
        "external": {
            "controller_url": str(((getattr(cfg, "_c", {}) or {}).get("clash_proxy_pool", {}) or {}).get("api_url", "")).strip(),
            "group_name": str(((getattr(cfg, "_c", {}) or {}).get("clash_proxy_pool", {}) or {}).get("group_name", "节点选择")).strip(),
            "enable_switch": bool(((getattr(cfg, "_c", {}) or {}).get("clash_proxy_pool", {}) or {}).get("enable", False)),
        },
        "qg_short_proxy": {
            "enabled": bool(qg_short_conf.get("enable", False)),
            "extract_url": str(qg_short_conf.get("extract_url", "") or "").strip(),
            "auth_username": str(qg_short_conf.get("auth_username", "") or "").strip(),
            "refresh_before_expire_seconds": safe_int(qg_short_conf.get("refresh_before_expire_seconds", 5), 5),
            "request_timeout_seconds": safe_int(qg_short_conf.get("request_timeout_seconds", 10), 10),
            "max_retry_candidates": safe_int(qg_short_conf.get("max_retry_candidates", 3), 3),
            "probe_timeout_seconds": safe_int(qg_short_conf.get("probe_timeout_seconds", 10), 10),
            "effective_proxy": qg_short_status.get("effective_proxy", ""),
            "server": qg_short_status.get("server", ""),
            "proxy_ip": qg_short_status.get("proxy_ip", ""),
            "area": qg_short_status.get("area", ""),
            "isp": qg_short_status.get("isp", ""),
            "deadline": qg_short_status.get("deadline", ""),
            "request_id": qg_short_status.get("request_id", ""),
            "error": qg_short_status.get("error", ""),
            "cached": bool(qg_short_status.get("cached", False)),
            "last_probe_ok": bool(qg_short_status.get("last_probe_ok", False)),
            "last_probe_error": qg_short_status.get("last_probe_error", ""),
            "last_probe_loc": qg_short_status.get("last_probe_loc", ""),
            "last_probe_elapsed_ms": safe_int(qg_short_status.get("last_probe_elapsed_ms", 0), 0),
            "last_probe_at": qg_short_status.get("last_probe_at", 0),
        },
        "qg_dynamic_proxy": {
            "enabled": bool(qg_conf.get("enable", False)),
            "host": str(qg_conf.get("host", "") or "").strip(),
            "port": safe_int(qg_conf.get("port", 12259), 12259),
            "sticky_session": bool(qg_conf.get("sticky_session", False)),
            "channel": str(qg_conf.get("channel", "") or "").strip(),
            "session_seconds": safe_int(qg_conf.get("session_seconds", 120), 120),
            "area_code": str(qg_conf.get("area_code", "") or "").strip(),
            "effective_proxy": build_qg_dynamic_proxy_url(),
        },
    }


@router.post("/api/proxy/core/start")
async def proxy_core_start(token: str = Depends(verify_token)):
    if not is_embedded_mode():
        return {"status": "error", "message": "当前不是内置 Mihomo 模式"}
    try:
        status = get_embedded_mihomo_manager().start()
        return {"status": "success", "message": "内置 Mihomo 已启动", "data": status}
    except Exception as e:
        return {"status": "error", "message": f"启动失败: {e}"}


@router.post("/api/proxy/core/stop")
async def proxy_core_stop(token: str = Depends(verify_token)):
    try:
        status = get_embedded_mihomo_manager().stop()
        return {"status": "success", "message": "内置 Mihomo 已停止", "data": status}
    except Exception as e:
        return {"status": "error", "message": f"停止失败: {e}"}


@router.post("/api/proxy/core/restart")
async def proxy_core_restart(token: str = Depends(verify_token)):
    if not is_embedded_mode():
        return {"status": "error", "message": "当前不是内置 Mihomo 模式"}
    try:
        status = get_embedded_mihomo_manager().restart()
        return {"status": "success", "message": "内置 Mihomo 已重启", "data": status}
    except Exception as e:
        return {"status": "error", "message": f"重启失败: {e}"}


@router.post("/api/proxy/subscription/import")
async def proxy_subscription_import(req: ProxySubscriptionReq, token: str = Depends(verify_token)):
    try:
        subscription_url = str(req.subscription_url or "").strip()
        if not subscription_url.startswith("http://") and not subscription_url.startswith("https://"):
            return {"status": "error", "message": "订阅地址必须以 http:// 或 https:// 开头"}

        def apply_subscription(conf):
            conf["proxy_backend"]["mode"] = "embedded_mihomo"
            conf["embedded_mihomo"].update({
                "subscription_url": subscription_url,
                "enable": True,
            })

        update_proxy_config(apply_subscription)
        manager = get_embedded_mihomo_manager()
        data = manager.update_subscription(subscription_url=subscription_url, restart_if_running=manager.is_running(), reason="import")
        return {"status": "success", "message": "订阅地址已保存", "data": data}
    except Exception as e:
        return {"status": "error", "message": f"导入订阅失败: {e}"}


@router.post("/api/proxy/subscription/update")
async def proxy_subscription_update(token: str = Depends(verify_token)):
    if not is_embedded_mode():
        return {"status": "error", "message": "当前不是内置 Mihomo 模式"}
    try:
        data = get_embedded_mihomo_manager().update_subscription(restart_if_running=True, reason="manual")
        return {"status": "success", "message": "订阅已更新", "data": data}
    except Exception as e:
        return {"status": "error", "message": f"更新订阅失败: {e}"}


@router.get("/api/proxy/groups")
async def proxy_groups(token: str = Depends(verify_token)):
    try:
        if is_embedded_mode():
            payload = get_embedded_mihomo_manager().get_groups()
        else:
            payload = parse_proxy_groups_from_payload(request_proxy_controller("GET", "/proxies"))
            payload["running"] = True
        return {"status": "success", "data": payload}
    except Exception as e:
        return {"status": "error", "message": f"获取代理组失败: {e}", "data": {"groups": [], "selected_group": ""}}


@router.post("/api/proxy/select")
async def proxy_select(req: ProxySelectReq, token: str = Depends(verify_token)):
    try:
        group_name = str(req.group_name or "").strip()
        proxy_name = str(req.proxy_name or "").strip()
        if not group_name:
            return {"status": "error", "message": "缺少策略组名称"}

        if is_embedded_mode():
            update_proxy_config(lambda conf: conf["embedded_mihomo"].update({"group_name": group_name}))
            data = get_embedded_mihomo_manager().select_proxy(group_name, proxy_name or None)
        else:
            if proxy_name:
                encoded_group = urllib.parse.quote(group_name, safe="")
                request_proxy_controller("PUT", f"/proxies/{encoded_group}", json={"name": proxy_name})
            data = parse_proxy_groups_from_payload(request_proxy_controller("GET", "/proxies"))
        return {"status": "success", "message": "代理节点已更新", "data": data}
    except Exception as e:
        return {"status": "error", "message": f"切换节点失败: {e}"}


@router.post("/api/proxy/delay-test")
async def proxy_delay_test(req: ProxyDelayTestReq, token: str = Depends(verify_token)):
    try:
        group_name = str(req.group_name or "").strip()
        if is_embedded_mode():
            data = get_embedded_mihomo_manager().test_group_delays(group_name or None)
        else:
            group_payload = parse_proxy_groups_from_payload(request_proxy_controller("GET", "/proxies"))
            target_group = group_name or group_payload.get("selected_group") or ""
            target = next((item for item in group_payload.get("groups", []) if item.get("name") == target_group), None)
            if not target:
                return {"status": "error", "message": "未找到指定策略组"}
            test_url = "https://www.gstatic.com/generate_204"
            headers = {}
            secret = get_proxy_controller_secret()
            if secret:
                headers["Authorization"] = f"Bearer {secret}"
            controller_url = get_effective_controller_url().rstrip("/")
            results = []
            for proxy_name in [item for item in list(target.get("all") or []) if str(item).upper() not in {"DIRECT", "REJECT"}]:
                encoded_name = urllib.parse.quote(proxy_name, safe="")
                delay = None
                error = ""
                try:
                    response = httpx.get(f"{controller_url}/proxies/{encoded_name}/delay", headers=headers, params={"timeout": 3000, "url": test_url}, timeout=5)
                    response.raise_for_status()
                    payload = response.json()
                    delay = payload.get("delay")
                except Exception as exc:
                    error = str(exc)
                results.append({"name": proxy_name, "delay": delay if isinstance(delay, (int, float)) else None, "error": error})
            results.sort(key=lambda each: (each["delay"] is None, each["delay"] if each["delay"] is not None else 999999))
            data = {"running": True, "group_name": target_group, "test_url": test_url, "results": results}
        return {"status": "success", "data": data}
    except Exception as e:
        return {"status": "error", "message": f"延迟测试失败: {e}"}


@router.get("/api/proxy/logs")
async def proxy_logs(limit: int = Query(200), token: str = Depends(verify_token)):
    try:
        if is_embedded_mode():
            data = get_embedded_mihomo_manager().logs(limit=limit)
        else:
            data = {"running": False, "path": "", "lines": []}
        return {"status": "success", "data": data}
    except Exception as e:
        return {"status": "error", "message": f"读取代理日志失败: {e}", "data": {"running": False, "path": "", "lines": []}}


@router.post("/api/config/add_wildcard_dns")
async def add_wildcard_dns(req: CFSyncExistingReq, token: str = Depends(verify_token)):
    try:
        main_list = [d.strip() for d in req.sub_domains.split(",") if d.strip()]
        if not main_list: return {"status": "error", "message": "❌ 没有找到有效的主域名"}

        proxy_url = get_effective_default_proxy()
        headers = {"X-Auth-Email": req.api_email, "X-Auth-Key": req.api_key, "Content-Type": "application/json"}
        client_kwargs = {"timeout": 30.0}
        if proxy_url: client_kwargs["proxy"] = proxy_url

        semaphore = asyncio.Semaphore(2)

        async def process_single_domain(client, domain):
            async with semaphore:
                try:
                    zone_resp = await client.get(f"https://api.cloudflare.com/client/v4/zones?name={domain}",
                                                 headers=headers)
                    zone_data = zone_resp.json()
                    if not zone_data.get("success") or not zone_data.get("result"): return False
                    zone_id = zone_data["result"][0]["id"]

                    records = [
                        {"type": "MX", "name": "*", "content": "route3.mx.cloudflare.net", "priority": 36},
                        {"type": "MX", "name": "*", "content": "route2.mx.cloudflare.net", "priority": 25},
                        {"type": "MX", "name": "*", "content": "route1.mx.cloudflare.net", "priority": 51},
                        {"type": "TXT", "name": "*", "content": '"v=spf1 include:_spf.mx.cloudflare.net ~all"'}
                    ]
                    for rec in records:
                        rec_resp = await client.post(
                            f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records", headers=headers,
                            json=rec)
                        rec_data = rec_resp.json()
                        if not rec_data.get("success"):
                            errors = rec_data.get("errors", [])
                            is_quota_exceeded = any(err.get("code") == 81045 for err in errors)
                            is_exist = any(err.get("code") in {81057, 81058} for err in errors)

                            if is_quota_exceeded:
                                print(f"[{core_engine.ts()}] [CF] [{domain}] 记录配额已超出，无法继续创建，请手动去cf官网清除记录后在推送。")
                                continue
                            elif is_exist:
                                print(f"[{core_engine.ts()}] [CF] [{domain}] 记录已存在无需重复创建。")
                                continue

                            print(f"[{core_engine.ts()}] [ERROR] [{domain}] 记录创建报错: {errors}")
                        print(f"[{core_engine.ts()}] [SUCCESS] [{domain}] 创建成功")
                        await asyncio.sleep(0.5)
                    print(f"[{core_engine.ts()}] [CF] ✅ [{domain}] 解析处理成功，防止遗漏，请等待日志输出完毕后，重新点击推送！")
                    return True
                except:
                    return False
                finally:
                    await asyncio.sleep(0.5)

        async with httpx.AsyncClient(**client_kwargs) as client:
            tasks = [process_single_domain(client, dom) for dom in main_list]
            results = await asyncio.gather(*tasks)

        success_count = sum(1 for r in results if r)
        return {"status": "success", "message": f"成功处理 {success_count}/{len(main_list)} 个域名。"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/api/config/cf_global_status")
def get_cf_global_status(main_domain: str, token: str = Depends(verify_token)):
    try:
        cf_cfg = getattr(core_engine.cfg, '_c', {})
        api_email, api_key = cf_cfg.get("cf_api_email"), cf_cfg.get("cf_api_key")
        if not api_email or not api_key: return {"status": "error", "message": "未配置 CF 账号信息"}

        cf = Cloudflare(api_email=api_email, api_key=api_key)
        domains = [d.strip() for d in main_domain.split(",") if d.strip()]
        results = []

        for dom in domains:
            zones = cf.zones.list(name=dom)
            if not zones.result:
                results.append({"domain": dom, "is_enabled": False, "dns_status": "not_found"})
                continue
            zone_id = zones.result[0].id
            routing_info = cf.email_routing.get(zone_id=zone_id)

            def safe_get(obj, attr, default=None):
                val = getattr(obj, attr, None)
                if val is None and hasattr(obj, 'result'): val = getattr(obj.result, attr, None)
                return val if val is not None else default

            raw_status, raw_synced = safe_get(routing_info, 'status', 'unknown'), safe_get(routing_info, 'synced',
                                                                                           False)
            results.append({"domain": dom, "is_enabled": (raw_status == 'ready' and raw_synced is True),
                            "dns_status": "active" if raw_synced else "pending"})

        return {"status": "success", "data": results}
    except Exception as e:
        return {"status": "error", "message": f"状态同步失败: {str(e)}"}

@router.get("/api/accounts")
async def get_accounts(
    page: int = Query(1),
    page_size: int = Query(50),
    push_platform: str = Query("all"),
    push_state: str = Query("all"),
    token: str = Depends(verify_token),
):
    result = db_manager.get_accounts_page(page, page_size, push_platform=push_platform, push_state=push_state)
    return {"status": "success", "data": result["data"], "total": result["total"], "page": page, "page_size": page_size}


@router.get("/api/nw_keys")
async def get_nw_keys(
    page: int = Query(1),
    page_size: int = Query(50),
    status: str = Query("all"),
    token: str = Depends(verify_token),
):
    result = db_manager.get_nw_keys_page(page, page_size, status=status)
    return {"status": "success", "data": result["data"], "total": result["total"], "page": page, "page_size": page_size}


@router.post("/api/nw_keys/delete")
async def delete_nw_keys(req: DeleteReq, token: str = Depends(verify_token)):
    if not req.emails:
        return {"status": "error", "message": "未收到任何要删除的 key"}
    deleted = 0
    for key_id_str in req.emails:
        try:
            if db_manager.delete_nw_key(int(key_id_str)):
                deleted += 1
        except (ValueError, TypeError):
            pass
    return {"status": "success", "message": f"成功删除 {deleted} 个 Neuralwatt key"}


@router.post("/api/nw_keys/export")
async def export_nw_keys(req: ExportReq, token: str = Depends(verify_token)):
    if not req.emails:
        return {"status": "error", "message": "未收到任何要导出的 key"}
    keys = db_manager.get_nw_keys_page(1, 10000, status="all")["data"]
    export_data = [k for k in keys if str(k.get("id", "")) in req.emails or k.get("api_key", "") in req.emails]
    return {"status": "success", "data": export_data}


@router.post("/api/accounts/export_selected")
async def export_selected_accounts(req: ExportReq, token: str = Depends(verify_token)):
    if not req.emails: return {"status": "error", "message": "未收到任何要导出的账号"}
    tokens = db_manager.get_tokens_by_emails(req.emails)
    return {"status": "success", "data": tokens} if tokens else {"status": "error", "message": "未能提取到选中账号的有效 Token"}


@router.post("/api/accounts/delete")
async def delete_selected_accounts(req: DeleteReq, token: str = Depends(verify_token)):
    if not req.emails: return {"status": "error", "message": "未收到任何要删除的账号"}
    return {"status": "success", "message": f"成功删除 {len(req.emails)} 个账号"} if db_manager.delete_accounts_by_emails(
        req.emails) else {"status": "error", "message": "删除操作失败"}


@router.post("/api/account/action")
def account_action(data: dict, token: str = Depends(verify_token)):
    try:
        email, action = data.get("email"), data.get("action")
        config = getattr(core_engine.cfg, '_c', {})
        token_data = db_manager.get_token_by_email(email)
        if not token_data: return {"status": "error", "message": f"未找到 {email} 的 Token。"}

        if action == "push":
            if not config.get("cpa_mode", {}).get("enable", False): return {"status": "error",
                                                                            "message": "🚫 推送失败：未开启 CPA 模式！"}
            success, msg = core_engine.upload_to_cpa_integrated(token_data,
                                                                config.get("cpa_mode", {}).get("api_url", ""),
                                                                config.get("cpa_mode", {}).get("api_token", ""))
            if success:
                db_manager.mark_account_pushed(email, "cpa")
            return {"status": "success", "message": f"账号 {email} 已成功推送到 CPA！"} if success else {"status": "error",
                                                                                                "message": f"CPA 推送失败: {msg}"}

        elif action == "push_sub2api":
            if not getattr(core_engine.cfg, 'ENABLE_SUB2API_MODE', False): return {"status": "error",
                                                                                   "message": "🚫 推送失败：未开启 Sub2API 模式！"}
            client = Sub2APIClient(api_url=getattr(core_engine.cfg, 'SUB2API_URL', ''),
                                   api_key=getattr(core_engine.cfg, 'SUB2API_KEY', ''))
            success, resp = client.add_account(token_data)
            if success:
                db_manager.mark_account_pushed(email, "sub2api")
            return {"status": "success", "message": f"账号 {email} 已同步至 Sub2API！"} if success else {"status": "error",
                                                                                                  "message": f"Sub2API 推送失败: {resp}"}
        elif action == "push_codex2api":
            if not getattr(core_engine.cfg, 'ENABLE_CODEX2API_MODE', False):
                return {"status": "error", "message": "🚫 推送失败：未开启 Codex2API 模式！"}
            client = Codex2APIClient(
                api_url=getattr(core_engine.cfg, 'CODEX2API_URL', ''),
                admin_key=getattr(core_engine.cfg, 'CODEX2API_ADMIN_KEY', ''),
            )
            push_payload = dict(token_data or {})
            push_payload["source"] = config.get("codex2api_mode", {}).get("push_source", "register-oss")
            success, resp = client.push_account(push_payload)
            if success:
                db_manager.mark_account_pushed(email, "codex2api")
            return {"status": "success", "message": f"账号 {email} 已同步至 Codex2API！"} if success else {
                "status": "error",
                "message": f"Codex2API 推送失败: {resp}",
            }
        elif action == "push_neuralwatt":
            api_key_val = (token_data or {}).get("api_key", "")
            if not api_key_val:
                return {"status": "error", "message": "🚫 推送失败：该账号无 Neuralwatt API Key！"}
            db_manager.mark_account_pushed(email, "neuralwatt")
            return {"status": "success", "message": f"账号 {email} Neuralwatt API Key 已标记推送！"}
        return {"status": "error", "message": f"不支持的操作: {action}"}
    except Exception as e:
        return {"status": "error", "message": f"后端推送异常: {str(e)}"}


@router.post("/api/accounts/export_sub2api")
async def export_sub2api_accounts(req: ExportReq, token: str = Depends(verify_token)):
    from datetime import datetime, timezone
    try:
        tokens = db_manager.get_tokens_by_emails(req.emails)
        if not tokens: return {"status": "error", "message": "未提取到Token"}

        sub2api_settings = getattr(core_engine.cfg, '_c', {}).get("sub2api_mode", {})
        accounts_list = []
        for td in tokens:
            accounts_list.append({
                "name": str(td.get("email", "unknown"))[:64],
                "platform": "openai", "type": "oauth",
                "credentials": {"refresh_token": td.get("refresh_token", "")},
                "concurrency": int(sub2api_settings.get("account_concurrency", 10)),
                "priority": int(sub2api_settings.get("account_priority", 1)),
                "rate_multiplier": float(sub2api_settings.get("account_rate_multiplier", 1.0)),
                "extra": {"load_factor": int(sub2api_settings.get("account_load_factor", 10))}
            })
        return {"status": "success",
                "data": {"exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "proxies": [],
                         "accounts": accounts_list}}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/api/cloud/accounts")
def get_cloud_accounts(types: str = "sub2api,cpa,codex2api", page: int = Query(1), page_size: int = Query(50),
                       token: str = Depends(verify_token)):
    type_list = types.split(",")
    combined_data = []
    try:
        if "sub2api" in type_list and getattr(cfg, 'SUB2API_URL', None) and getattr(cfg, 'SUB2API_KEY', None):
            client = Sub2APIClient(api_url=cfg.SUB2API_URL, api_key=cfg.SUB2API_KEY)
            success, raw_sub2_data = client.get_all_accounts()
            if success:
                for item in raw_sub2_data:
                    extra = item.get("extra", {})
                    combined_data.append({
                        "id": str(item.get("id", "")), "account_type": "sub2api",
                        "credential": item.get("name", "未知账号"),
                        "status": "disabled" if item.get("status") == "inactive" else (
                            "active" if item.get("status") == "active" else "dead"),
                        "last_check": normalize_cloud_time(item.get("updated_at", "-")),
                        "details": {"plan_type": item.get("credentials", {}).get("plan_type", "未知"),
                                    "codex_5h_used_percent": extra.get("codex_5h_used_percent", 0),
                                    "codex_7d_used_percent": extra.get("codex_7d_used_percent", 0)}
                    })

        if "codex2api" in type_list and getattr(cfg, 'CODEX2API_URL', None) and getattr(cfg, 'CODEX2API_ADMIN_KEY', None):
            client = Codex2APIClient(api_url=cfg.CODEX2API_URL, admin_key=cfg.CODEX2API_ADMIN_KEY)
            success, raw_codex_data = client.get_all_accounts()
            if success:
                for item in raw_codex_data:
                    combined_data.append({
                        "id": str(item.get("id", "")),
                        "account_type": "codex2api",
                        "credential": item.get("email") or item.get("name") or f"account-{item.get('id', '')}",
                        "status": map_codex2api_status(item),
                        "last_check": normalize_cloud_time(item.get("updated_at") or item.get("last_used_at") or "-"),
                        "details": build_codex2api_details(item),
                    })

        if "cpa" in type_list and getattr(cfg, 'CPA_API_URL', None) and getattr(cfg, 'CPA_API_TOKEN', None):
            from curl_cffi import requests
            res = requests.get(core_engine._normalize_cpa_auth_files_url(cfg.CPA_API_URL),
                               headers={"Authorization": f"Bearer {cfg.CPA_API_TOKEN}"}, timeout=20,
                               impersonate="chrome110")
            if res.status_code == 200:
                for item in [f for f in res.json().get("files", []) if
                             "codex" in str(f.get("type", "")).lower() or "codex" in str(
                                     f.get("provider", "")).lower()]:
                    combined_data.append({"id": item.get("name", ""), "account_type": "cpa",
                                          "credential": item.get("name", "").replace(".json", ""),
                                          "status": "disabled" if item.get("disabled", False) else "active",
                                          "details": {}, "last_check": "-"})

        return {"status": "success", "data": combined_data[(page - 1) * page_size: page * page_size],
                "total": len(combined_data)}
    except Exception as e:
        return {"status": "error", "message": f"拉取远端数据失败: {str(e)}"}


@router.post("/api/cloud/action")
def process_cloud_action(req: CloudActionReq, token: str = Depends(verify_token)):
    from curl_cffi import requests
    from concurrent.futures import ThreadPoolExecutor

    success_count, fail_count, updated_details_map = 0, 0, {}
    sub2api_client = Sub2APIClient(api_url=cfg.SUB2API_URL, api_key=cfg.SUB2API_KEY) if getattr(cfg, 'SUB2API_URL',
                                                                                                None) and getattr(cfg,
                                                                                                                  'SUB2API_KEY',
                                                                                                                  None) else None
    codex2api_client = Codex2APIClient(api_url=cfg.CODEX2API_URL, admin_key=cfg.CODEX2API_ADMIN_KEY) if getattr(
        cfg, 'CODEX2API_URL', None
    ) and getattr(cfg, 'CODEX2API_ADMIN_KEY', None) else None

    cpa_files_map = {}
    if any(a.type == "cpa" for a in req.accounts) and req.action == "check" and getattr(cfg, 'CPA_API_URL', None):
        try:
            res = requests.get(core_engine._normalize_cpa_auth_files_url(cfg.CPA_API_URL),
                               headers={"Authorization": f"Bearer {cfg.CPA_API_TOKEN}"}, timeout=15,
                               impersonate="chrome110")
            if res.status_code == 200: cpa_files_map = {f.get("name"): f for f in res.json().get("files", [])}
        except:
            pass

    def _worker(acc: CloudAccountItem):
        is_success, details = False, None
        cache_key = f"{acc.type}:{acc.id}"
        try:
            if acc.type == "sub2api" and sub2api_client:
                if req.action == "check":
                    result, _ = sub2api_client.test_account(acc.id)
                    is_success = (result == "ok")
                    if not is_success: sub2api_client.set_account_status(acc.id, disabled=True)
                elif req.action in ["enable", "disable"]:
                    is_success = sub2api_client.set_account_status(acc.id, disabled=(req.action == "disable"))
                elif req.action == "delete":
                    is_success, _ = sub2api_client.delete_account(acc.id)

            elif acc.type == "codex2api" and codex2api_client:
                if req.action == "check":
                    result, _ = codex2api_client.test_account(acc.id)
                    is_success = (result == "ok")
                    if not is_success:
                        codex2api_client.set_account_status(acc.id, disabled=True)
                elif req.action in ["enable", "disable"]:
                    is_success = codex2api_client.set_account_status(acc.id, disabled=(req.action == "disable"))
                elif req.action == "delete":
                    is_success, _ = codex2api_client.delete_account(acc.id)

            elif acc.type == "cpa" and getattr(cfg, 'CPA_API_URL', None):
                if req.action == "check":
                    item = cpa_files_map.get(acc.id, {"name": acc.id, "disabled": False})
                    is_success, _ = core_engine.test_cliproxy_auth_file(item, cfg.CPA_API_URL, cfg.CPA_API_TOKEN)
                    if '_raw_usage' in item: details = parse_cpa_usage_to_details(item['_raw_usage'])
                    if not is_success: core_engine.set_cpa_auth_file_status(cfg.CPA_API_URL, cfg.CPA_API_TOKEN, acc.id,
                                                                            disabled=True)
                elif req.action in ["enable", "disable"]:
                    is_success = core_engine.set_cpa_auth_file_status(cfg.CPA_API_URL, cfg.CPA_API_TOKEN, acc.id,
                                                                      disabled=(req.action == "disable"))
                elif req.action == "delete":
                    is_success = requests.delete(core_engine._normalize_cpa_auth_files_url(cfg.CPA_API_URL),
                                                 headers={"Authorization": f"Bearer {cfg.CPA_API_TOKEN}"},
                                                 params={"name": acc.id}, impersonate="chrome110").status_code in (
                                 200, 204)
        except:
            pass
        return (is_success, cache_key, details)

    target_threads = 5
    if any(a.type == "cpa" for a in req.accounts): target_threads = max(target_threads, int(
        getattr(cfg, '_c', {}).get('cpa_mode', {}).get('threads', 10)))
    if any(a.type == "sub2api" for a in req.accounts): target_threads = max(target_threads, int(
        getattr(cfg, '_c', {}).get('sub2api_mode', {}).get('threads', 10)))
    if any(a.type == "codex2api" for a in req.accounts): target_threads = max(target_threads, int(
        getattr(cfg, '_c', {}).get('codex2api_mode', {}).get('threads', 10)))

    with ThreadPoolExecutor(max_workers=max(1, min(target_threads, 50))) as executor:
        for is_success, acc_id, details in executor.map(_worker, req.accounts):
            if is_success:
                success_count += 1
            else:
                fail_count += 1
            if details: updated_details_map[acc_id] = details

    msg = f"测活完毕 | 存活: {success_count} 个 | 失效并已自动禁用: {fail_count} 个" if req.action == "check" else f"指令已下发 | 成功: {success_count} 个 | 失败: {fail_count} 个"
    return {"status": "success" if fail_count == 0 else "warning", "message": msg,
            "updated_details": updated_details_map}

@router.get('/api/sms/balance')
def api_get_sms_balance(token: str = Depends(verify_token)):
    from utils.integrations.hero_sms import hero_sms_get_balance
    proxy_url = get_effective_default_proxy()
    balance, err = hero_sms_get_balance(proxies={"http": proxy_url, "https": proxy_url} if proxy_url else None)
    return {"status": "success", "balance": f"{balance:.2f}"} if balance >= 0 else {"status": "error", "message": err}


@router.post('/api/sms/prices')
def api_get_sms_prices(req: SMSPriceReq, token: str = Depends(verify_token)):
    from utils.integrations.hero_sms import _hero_sms_prices_by_service
    proxy_url = get_effective_default_proxy()
    rows = _hero_sms_prices_by_service(req.service,
                                       proxies={"http": proxy_url, "https": proxy_url} if proxy_url else None)
    return {"status": "success", "prices": rows} if rows else {"status": "error", "message": "无法获取价格或当前服务无库存"}


@router.post("/api/luckmail/bulk_buy")
def api_luckmail_bulk_buy(req: LuckMailBulkBuyReq, token: str = Depends(verify_token)):
    try:
        from utils.email_providers.luckmail_service import LuckMailService
        lm_service = LuckMailService(api_key=req.config.get("api_key"),
                                     preferred_domain=req.config.get("preferred_domain", ""),
                                     email_type=req.config.get("email_type", "ms_graph"),
                                     variant_mode=req.config.get("variant_mode", ""))
        tag_id = req.config.get("tag_id") or lm_service.get_or_create_tag_id("已使用")
        results = lm_service.bulk_purchase(quantity=req.quantity, auto_tag=req.auto_tag, tag_id=tag_id)
        return {"status": "success", "message": f"成功购买 {len(results)} 个邮箱！", "data": results}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/api/gmail/auth_url")
async def get_gmail_auth_url(token: str = Depends(verify_token)):
    if not os.path.exists(GMAIL_CLIENT_SECRETS): return {"status": "error",
                                                         "message": f"❌ 未找到凭证文件！请上传至: {GMAIL_CLIENT_SECRETS}"}
    try:
        url, verifier = GmailOAuthHandler.get_authorization_url(GMAIL_CLIENT_SECRETS)
        with open(GMAIL_VERIFIER_PATH, "w") as f:
            f.write(verifier)
        return {"status": "success", "url": url}
    except Exception as e:
        return {"status": "error", "message": f"生成链接失败: {str(e)}"}


@router.post("/api/gmail/exchange_code")
async def exchange_gmail_code(req: GmailExchangeReq, token: str = Depends(verify_token)):
    if not req.code: return {"status": "error", "message": "授权码不能为空"}
    try:
        if not os.path.exists(GMAIL_VERIFIER_PATH): return {"status": "error", "message": "会话已过期，请重新生成链接"}
        with open(GMAIL_VERIFIER_PATH, "r") as f:
            stored_verifier = f.read().strip()
        success, msg = GmailOAuthHandler.save_token_from_code(GMAIL_CLIENT_SECRETS, req.code, GMAIL_TOKEN_PATH,
                                                              code_verifier=stored_verifier,
                                                              proxy=get_effective_default_proxy() or None)
        if success and os.path.exists(GMAIL_VERIFIER_PATH):
            os.remove(GMAIL_VERIFIER_PATH)
            return {"status": "success", "message": "✨ 授权成功！token.json 已保存在 data 目录。"}
        return {"status": "error", "message": msg}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/api/sub2api/groups")
def get_sub2api_groups(token: str = Depends(verify_token)):
    from curl_cffi import requests as cffi_requests
    sub2api_url = getattr(core_engine.cfg, "SUB2API_URL", "").strip()
    sub2api_key = getattr(core_engine.cfg, "SUB2API_KEY", "").strip()
    if not sub2api_url or not sub2api_key: return {"status": "error",
                                                   "message": "Please save the Sub2API URL and API key first."}
    try:
        response = cffi_requests.get(f"{sub2api_url.rstrip('/')}/api/v1/admin/groups/all",
                                     headers={"x-api-key": sub2api_key, "Content-Type": "application/json"}, timeout=10,
                                     impersonate="chrome110")
        if response.status_code != 200: return {"status": "error",
                                                "message": f"HTTP {response.status_code}: {response.text[:200]}"}
        return {"status": "success", "data": response.json().get("data", [])}
    except Exception as exc:
        return {"status": "error", "message": f"Failed to fetch Sub2API groups: {exc}"}


@router.get("/api/system/check_update")
async def check_update(current_version: str, token: str = Depends(verify_token)):
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get("https://api.github.com/repos/wenfxl/openai-cpa/releases/latest",
                                    headers={"Accept": "application/vnd.github.v3+json"})
            if resp.status_code != 200: return {"status": "error",
                                                "message": f"无法获取更新数据 (GitHub API 返回 HTTP {resp.status_code})"}
        data = resp.json()
        remote_version = data.get("tag_name", "")

        def _parse(v):
            return [int(x) for x in re.findall(r'\d+', str(v))]

        has_update = _parse(remote_version) > _parse(current_version) if remote_version else False
        assets = data.get("assets")
        download_url = assets[0].get("browser_download_url", "") if assets else data.get("zipball_url", "")
        return {"status": "success", "has_update": has_update, "remote_version": remote_version,
                "changelog": data.get("body", "无更新日志"), "download_url": download_url,
                "html_url": data.get("html_url", "")}
    except Exception as e:
        return {"status": "error", "message": f"检查更新发生未知异常: {str(e)}"}

@router.post("/api/logs/clear")
async def clear_backend_logs(token: str = Depends(verify_token)):
    log_history.clear()
    return {"status": "success"}


@router.get("/api/logs/stream")
async def stream_logs(request: Request, token: str = Query(None)):
    if token not in VALID_TOKENS: raise HTTPException(status_code=401, detail="Unauthorized")

    async def log_generator():
        current_snapshot = list(log_history)
        for old_msg in current_snapshot:
            yield f"data: {old_msg}\n\n"
        last_sent_msg = current_snapshot[-1] if current_snapshot else None
        idle_loops = 0

        try:
            while True:
                if await request.is_disconnected():
                    break
                snap = list(log_history)
                if snap and snap[-1] != last_sent_msg:
                    start_idx = 0
                    for i in range(len(snap) - 1, -1, -1):
                        if snap[i] == last_sent_msg:
                            start_idx = i + 1
                            break
                    for i in range(start_idx, len(snap)):
                        yield f"data: {snap[i]}\n\n"
                    last_sent_msg = snap[-1]
                    idle_loops = 0
                else:
                    idle_loops += 1
                    if idle_loops >= 50:
                        yield ": keepalive\n\n"
                        idle_loops = 0

                await asyncio.sleep(0.3)
        except Exception:
            pass

    return StreamingResponse(log_generator(), media_type="text/event-stream")

@router.post("/api/cluster/control")
async def cluster_control(req: ClusterControlReq, token: str = Depends(verify_token)):
    if req.action not in ["start", "stop", "restart", "export_accounts"]: return {"status": "error",
                                                                                  "message": "不支持的指令"}
    with cluster_lock: NODE_COMMANDS[req.node_name] = req.action
    return {"status": "success", "message": f"指令 [{req.action}] 已排队"}


@router.get("/api/cluster/view")
async def cluster_view(token: str = Depends(verify_token)):
    global CLUSTER_NODES
    now = time.time()
    with cluster_lock:
        CLUSTER_NODES = {k: v for k, v in CLUSTER_NODES.items() if now - v["last_seen"] < 20}
        return {"status": "success", "nodes": CLUSTER_NODES}


@router.post("/api/cluster/report")
async def cluster_report(req: ClusterReportReq):
    cf_dict = getattr(core_engine.cfg, '_c', {})
    if req.secret != str(cf_dict.get("cluster_secret", "wenfxl666")).strip(): return {"status": "error",
                                                                                      "message": "密钥错误"}

    target_cmd = NODE_COMMANDS.get(req.node_name, "none")
    node_is_running = req.stats.get("is_running", False)

    if target_cmd in ["restart", "export_accounts"]:
        NODE_COMMANDS[req.node_name] = "none"
    elif (target_cmd == "start" and node_is_running) or (target_cmd == "stop" and not node_is_running):
        NODE_COMMANDS[req.node_name] = "none"
        target_cmd = "none"

    with cluster_lock:
        CLUSTER_NODES[req.node_name] = {
            "stats": req.stats, "logs": req.logs, "last_seen": time.time(),
            "join_time": CLUSTER_NODES.get(req.node_name, {}).get("join_time", time.time())
        }
    return {"status": "success", "command": target_cmd}


@router.websocket("/api/cluster/report_ws")
async def ws_cluster_report(websocket: WebSocket, node_name: str, secret: str):
    await websocket.accept()
    if secret != str(getattr(core_engine.cfg, '_c', {}).get("cluster_secret", "wenfxl666")).strip():
        await websocket.close(code=1008, reason="Secret Mismatch")
        return
    try:
        while True:
            data = await websocket.receive_json()
            target_cmd = NODE_COMMANDS.get(node_name, "none")
            node_is_running = data.get("stats", {}).get("is_running", False)
            if target_cmd in ["restart", "export_accounts"]:
                NODE_COMMANDS[node_name] = "none"
            elif (target_cmd == "start" and node_is_running) or (target_cmd == "stop" and not node_is_running):
                NODE_COMMANDS[node_name] = "none"
                target_cmd = "none"
            with cluster_lock:
                CLUSTER_NODES[node_name] = {
                    "stats": data.get("stats", {}), "logs": data.get("logs", []), "last_seen": time.time(),
                    "join_time": CLUSTER_NODES.get(node_name, {}).get("join_time", time.time())
                }
            await websocket.send_json({"command": target_cmd})
    except Exception:
        pass


@router.websocket("/api/cluster/view_ws")
async def cluster_view_ws(websocket: WebSocket, token: str = Query(None)):
    if token not in VALID_TOKENS:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    try:
        while True:
            global CLUSTER_NODES
            now = time.time()
            with cluster_lock:
                CLUSTER_NODES = {k: v for k, v in CLUSTER_NODES.items() if now - v["last_seen"] < 20}
                await websocket.send_json({"status": "success", "nodes": CLUSTER_NODES})
            await asyncio.sleep(0.5)
    except Exception:
        pass

@router.post("/api/cluster/upload_accounts")
def cluster_upload_accounts(req: ClusterUploadAccountsReq):
    if req.secret != str(getattr(core_engine.cfg, '_c', {}).get("cluster_secret", "wenfxl666")).strip(): return {
        "status": "error", "message": "密钥错误"}
    success_count = 0
    for acc in req.accounts:
        if acc.get("email") and acc.get("token_data"):
            if db_manager.save_account_to_db(acc.get("email"), acc.get("password"),
                                             acc.get("token_data")): success_count += 1

    msg = f"[{core_engine.ts()}] [系统] 📦 成功从子控 [{req.node_name}] 提取并完美入库 {success_count} 个账号！"
    print(msg)
    try:
        log_history.append(msg)
    except:
        pass
    return {"status": "success", "message": f"成功接收 {success_count} 个账号"}

#模式二注册
@router.get("/api/ext/generate_task")
def ext_generate_task(token: str = Depends(verify_token)):
    from utils.email_providers.mail_service import mask_email, get_email_and_token, clear_sticky_domain
    from utils.register import _generate_password, generate_random_user_info, generate_oauth_url
    import utils.config as cfg
    import time
    print(f"[{cfg.ts()}] [INFO] 正在进行插件古法注册模式，请稍后...")
    try:
        cfg.GLOBAL_STOP = False
        clear_sticky_domain()

        email = None
        email_jwt = None
        for attempt in range(3):
            print(f"[{cfg.ts()}] [INFO] 正在进行邮箱创建...")
            email, email_jwt = get_email_and_token(proxies=None)
            if email:
                break
            time.sleep(1.5)

        if not email:
            return {"status": "error", "message": "邮箱获取超时或暂无库存，请稍候"}

        user_info = generate_random_user_info()
        password = _generate_password()

        oauth_reg = generate_oauth_url()

        print(f"[{cfg.ts()}] [INFO] （{mask_email(email)}）下发任务数据 (昵称: {user_info['name']}) (密码: {password}) (生日: {user_info['birthdate']})...")

        name_parts = user_info['name'].split(' ')
        return {
            "status": "success",
            "task_data": {
                "email": email,
                "email_jwt": email_jwt,
                "password": password,
                "firstName": name_parts[0] if len(name_parts) > 0 else "John",
                "lastName": name_parts[1] if len(name_parts) > 1 else "Doe",
                "birthday": user_info['birthdate'],
                "registerUrl": oauth_reg.auth_url,
                "code_verifier": oauth_reg.code_verifier,
                "expected_state": oauth_reg.state
            }
        }
    except Exception as e:
        return {"status": "error", "message": f"任务生成失败: {str(e)}"}

@router.get("/api/ext/get_mail_code")
def ext_get_mail_code(email: str, email_jwt: str = "", type: str = "signup", max_attempts: int = 20, token: str = Depends(verify_token)):
    from utils.email_providers.mail_service import get_oai_code
    try:
        code = get_oai_code(email, jwt=email_jwt, proxies=None, max_attempts=max_attempts)
        if code:
            return {"status": "success", "code": code}
        return {"status": "pending"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.post("/api/ext/submit_result")
def ext_submit_result(req: ExtResultReq, token: str = Depends(verify_token)):
    from utils import core_engine
    from utils.register import submit_callback_url

    if req.status == "success":
        token_json = req.token_data
        if not token_json and req.callback_url:
            try:
                token_json = submit_callback_url(
                    callback_url=req.callback_url,
                    expected_state=req.expected_state,
                    code_verifier=req.code_verifier
                )
            except Exception as e:
                print(f"换取 Token 失败: {e}")
                return {"status": "error", "message": "Token 换取失败"}
        db_manager.save_account_to_db(req.email, req.password, token_json)
        core_engine.run_stats['success'] = core_engine.run_stats.get('success', 0) + 1

        return {"status": "success", "message": "战利品已入库"}
    else:
        core_engine.run_stats['failed'] = core_engine.run_stats.get('failed', 0) + 1
        is_dead_account = False
        if req.error_type == 'phone_verify':
            core_engine.run_stats['phone_verify'] = core_engine.run_stats.get('phone_verify', 0) + 1
            is_dead_account = True
        elif req.error_type == 'pwd_blocked':
            core_engine.run_stats['pwd_blocked'] = core_engine.run_stats.get('pwd_blocked', 0) + 1
        if is_dead_account and getattr(cfg, "EMAIL_API_MODE", "") == "local_microsoft" and req.email:
            db_manager.update_local_mailbox_status(req.email, 3)
            print(f"[{cfg.ts()}] [WARNING] 插件上报邮箱不可用，已将邮箱标记为死号: {req.email}")
        return {"status": "success", "message": "异常统计已录入看板"}


@router.post("/api/ext/heartbeat")
def ext_heartbeat(worker_id: str, token: str = Depends(verify_token)):
    worker_status[worker_id] = time.time()
    return {"status": "success", "message": "ok"}


@router.get("/api/ext/check_node")
def check_node_status(worker_id: str, token: str = Depends(verify_token)):
    last_seen = worker_status.get(worker_id)
    if not last_seen:
        return {"status": "success", "online": False, "reason": "never_connected"}
    is_online = (time.time() - last_seen) < 15
    return {
        "status": "success",
        "online": is_online,
        "last_seen": last_seen
    }

@router.post("/api/ext/reset_stats")
def ext_reset_stats(token: str = Depends(verify_token)):
    from utils import core_engine
    import time
    core_engine.run_stats.update({
        "success": 0, "failed": 0, "retries": 0,
        "pwd_blocked": 0, "phone_verify": 0,
        "start_time": time.time(),
        "target": getattr(core_engine.cfg, 'NORMAL_TARGET_COUNT', 0),
        "ext_is_running": True
    })
    return {"status": "success"}

@router.post("/api/ext/stop")
def ext_stop(token: str = Depends(verify_token)):
    from utils import core_engine
    core_engine.run_stats["ext_is_running"] = False
    return {"status": "success"}

@router.get("/api/mailboxes")
async def get_mailboxes(page: int = Query(1), page_size: int = Query(50), token: str = Depends(verify_token)):
    result = db_manager.get_local_mailboxes_page(page, page_size)
    return {"status": "success", "data": result["data"], "total": result["total"], "page": page, "page_size": page_size}


@router.post("/api/mailboxes/import")
async def import_mailboxes(req: ImportMailboxReq, token: str = Depends(verify_token)):
    if not req.raw_text: return {"status": "error", "message": "内容为空"}

    parsed_mailboxes = []
    lines = req.raw_text.strip().split("\n")
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#"): continue
        parts = [p.strip() for p in text.split("----")]
        if len(parts) >= 2 and "@" in parts[0]:
            parsed_mailboxes.append({
                "email": parts[0],
                "password": parts[1],
                "client_id": parts[2] if len(parts) >= 3 else "",
                "refresh_token": parts[3] if len(parts) >= 4 else ""
            })

    if not parsed_mailboxes: return {"status": "error", "message": "未能识别出有效数据"}
    count = db_manager.import_local_mailboxes(parsed_mailboxes)
    return {"status": "success", "count": count}


@router.post("/api/mailboxes/delete")
async def delete_mailboxes(req: DeleteMailboxReq, token: str = Depends(verify_token)):
    if not req.ids: return {"status": "error", "message": "未收到任何要删除的ID"}
    if db_manager.delete_local_mailboxes(req.ids):
        return {"status": "success", "message": "删除成功"}
    return {"status": "error", "message": "删除操作失败"}


@router.post("/api/mailboxes/oauth_url")
async def get_outlook_oauth_url(req: OutlookAuthUrlReq, token: str = Depends(verify_token)):
    if not req.client_id:
        return {"status": "error", "message": "缺少 Client ID"}

    redirect_uri = "http://localhost"
    scope_str = urllib.parse.quote("offline_access https://graph.microsoft.com/Mail.Read")
    auth_url = (
        f"https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
        f"?client_id={req.client_id}"
        f"&response_type=code"
        f"&redirect_uri={urllib.parse.quote(redirect_uri)}"
        f"&response_mode=query"
        f"&scope={scope_str}"
    )
    return {"status": "success", "url": auth_url}


@router.post("/api/mailboxes/oauth_exchange")
async def exchange_outlook_oauth_code(req: OutlookExchangeReq, token: str = Depends(verify_token)):
    try:
        auth_code = req.code_or_url.strip()
        if "http" in auth_code or "code=" in auth_code:
            parsed_url = urllib.parse.urlparse(auth_code)
            query_params = urllib.parse.parse_qs(parsed_url.query)
            extracted = query_params.get("code", [None])[0]
            if extracted:
                auth_code = extracted
            else:
                return {"status": "error", "message": "无法从网址中提取 code 参数，请确保复制了完整的网址。"}

        token_url = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
        payload = {
            "client_id": req.client_id,
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": "http://localhost"
        }

        proxy_url = get_effective_default_proxy()
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

        response = cffi_requests.post(token_url, data=payload, proxies=proxies, timeout=15, impersonate="chrome110")
        data = response.json()

        if response.status_code == 200:
            refresh_token = data.get("refresh_token")
            import sqlite3
            from utils.db_manager import DB_PATH
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                conn.execute(
                    "UPDATE local_mailboxes SET client_id = ?, refresh_token = ?, status = 0 WHERE email = ?",
                    (req.client_id, refresh_token, req.email)
                )
                conn.commit()
            return {"status": "success", "message": f"授权成功！已为 {req.email} 绑定永久 Token。", "refresh_token": refresh_token}
        else:
            return {"status": "error", "message": f"获取失败: {data.get('error_description', data)}"}

    except Exception as e:
        return {"status": "error", "message": f"处理异常: {str(e)}"}


@router.post("/api/mailboxes/update_status")
async def update_mailboxes_status(req: UpdateMailboxStatusReq, token: str = Depends(verify_token)):
    if not req.emails:
        return {"status": "error", "message": "未收到任何邮箱"}

    success_count = 0
    for email in req.emails:
        try:
            db_manager.update_local_mailbox_status(email, req.status)
            db_manager.clear_retry_master_status(email)
            success_count += 1
        except Exception as e:
            pass

    return {"status": "success", "message": f"成功将 {success_count} 个邮箱状态重置！"}
