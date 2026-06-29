import os
from typing import Optional

import yaml
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from global_state import verify_token
from utils import config as cfg
from utils.config import reload_all_configs
from utils.embedded_mihomo import EmbeddedMihomoError, get_manager, normalize_settings, validate_subscription_url


router = APIRouter()


class SubscriptionUpdateReq(BaseModel):
    subscription_url: str
    restart: bool = True


class SelectReq(BaseModel):
    group: Optional[str] = ""
    proxy: str
    persist: bool = True


class TestReq(BaseModel):
    group: Optional[str] = ""
    proxy: Optional[str] = ""
    url: Optional[str] = ""


def _settings() -> dict:
    return normalize_settings(getattr(cfg, "EMBEDDED_MIHOMO", {}) or {})


def _manager():
    return get_manager(_settings())


def _save_embedded_config(changes: dict) -> dict:
    with cfg.CONFIG_FILE_LOCK:
        try:
            with open(cfg.CONFIG_PATH, "r", encoding="utf-8") as handle:
                config_data = yaml.safe_load(handle) or {}
        except FileNotFoundError:
            config_data = getattr(cfg, "_c", {}).copy()
        embedded = config_data.get("embedded_mihomo")
        if not isinstance(embedded, dict):
            embedded = {}
        embedded.update(changes)
        config_data["embedded_mihomo"] = embedded
        os.makedirs(os.path.dirname(cfg.CONFIG_PATH), exist_ok=True)
        with open(cfg.CONFIG_PATH, "w", encoding="utf-8") as handle:
            yaml.safe_dump(config_data, handle, allow_unicode=True, sort_keys=False)
    reload_all_configs(new_config_dict=config_data)
    return getattr(cfg, "EMBEDDED_MIHOMO", {}) or {}


def _ok(message: str, data: Optional[dict] = None) -> dict:
    return {"status": "success", "message": message, "data": data or {}}


def _err(exc: Exception) -> dict:
    return {"status": "error", "message": str(exc)}


@router.get("/api/mihomo/status")
async def mihomo_status(token: str = Depends(verify_token)):
    try:
        return _ok("Mihomo 状态已读取", _manager().status())
    except Exception as exc:
        return _err(exc)


@router.post("/api/mihomo/start")
async def mihomo_start(token: str = Depends(verify_token)):
    try:
        return _ok("Mihomo 已启动", _manager().start())
    except Exception as exc:
        return _err(exc)


@router.post("/api/mihomo/stop")
async def mihomo_stop(token: str = Depends(verify_token)):
    try:
        return _ok("Mihomo 已停止", _manager().stop())
    except Exception as exc:
        return _err(exc)


@router.post("/api/mihomo/restart")
async def mihomo_restart(token: str = Depends(verify_token)):
    try:
        return _ok("Mihomo 已重启", _manager().restart())
    except Exception as exc:
        return _err(exc)


@router.post("/api/mihomo/subscription/update")
async def mihomo_subscription_update(req: SubscriptionUpdateReq, token: str = Depends(verify_token)):
    try:
        normalized_url = validate_subscription_url(req.subscription_url)
        settings = _save_embedded_config({"subscription_url": normalized_url})
        manager = get_manager(settings)
        manager.write_config()
        data = manager.restart() if req.restart and settings.get("enable") else manager.status()
        return _ok("Mihomo 订阅已更新", data)
    except Exception as exc:
        return _err(exc)


@router.get("/api/mihomo/groups")
async def mihomo_groups(token: str = Depends(verify_token)):
    try:
        return _ok("Mihomo 策略组已读取", _manager().groups())
    except Exception as exc:
        return _err(exc)


@router.post("/api/mihomo/select")
async def mihomo_select(req: SelectReq, token: str = Depends(verify_token)):
    try:
        group = str(req.group or _settings().get("group_name") or "").strip()
        data = _manager().select_proxy(group, req.proxy)
        if req.persist:
            _save_embedded_config({"selected_group": group, "selected_proxy": req.proxy})
        return _ok("Mihomo 节点已切换", data)
    except Exception as exc:
        return _err(exc)


@router.post("/api/mihomo/test")
async def mihomo_test(req: TestReq, token: str = Depends(verify_token)):
    try:
        data = _manager().test_delay(group=req.group, proxy=req.proxy, url=req.url)
        return _ok("Mihomo 测速完成", data)
    except Exception as exc:
        return _err(exc)


@router.get("/api/mihomo/logs")
async def mihomo_logs(token: str = Depends(verify_token)):
    try:
        return _ok("Mihomo 日志已读取", _manager().logs())
    except EmbeddedMihomoError as exc:
        return _err(exc)
    except Exception as exc:
        return _err(exc)
