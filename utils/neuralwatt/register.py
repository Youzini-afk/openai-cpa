import json
import os
import random
import re
import string
import time
from typing import Any, Dict, Optional, Tuple
from html import unescape

from curl_cffi import requests
from utils import config as cfg
from utils.email_providers.mail_service import mask_email
from utils.proxy_manager import pick_working_qg_short_proxy

_NW_USED_LUCKMAIL_PIDS = set()
_nw_luckmail_lock = __import__('threading').Lock()
_nw_thread_data = __import__('threading').local()


NW_PORTAL_BASE = "https://portal.neuralwatt.com"
NW_API_BASE = "https://api.neuralwatt.com"
NW_REGISTER_URL = f"{NW_PORTAL_BASE}/auth/register"
NW_LOGIN_URL = f"{NW_PORTAL_BASE}/auth/login"
NW_TURNSTILE_SITEKEY = "0x4AAAAAAC-gz0RNqkUp7irh"

FIRST_NAMES = [
    "James", "John", "Robert", "Michael", "William", "David", "Richard",
    "Joseph", "Thomas", "Charles", "Emma", "Olivia", "Ava", "Isabella",
    "Sophia", "Mia", "Charlotte", "Amelia", "Harper", "Evelyn",
]
LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez",
    "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
]


def _ssl_verify() -> bool:
    flag = os.getenv("OPENAI_SSL_VERIFY", "1").strip().lower()
    return flag not in {"0", "false", "no", "off"}


def _nw_get_email_and_token(proxies: Any = None) -> Tuple[Optional[str], Optional[str]]:
    if getattr(cfg, 'GLOBAL_STOP', False):
        return None, None

    mode = cfg.EMAIL_API_MODE
    mail_proxies = proxies if cfg.USE_PROXY_FOR_EMAIL else None
    base_url = cfg.GPTMAIL_BASE.rstrip("/")

    domain_list = [d.strip() for d in cfg.MAIL_DOMAINS.split(",") if d.strip()]
    if not domain_list:
        print(f"[{cfg.ts()}] [ERROR] 域名池配置为空，无法为 Neuralwatt 生成邮箱！")
        return None, None
    selected_domain = random.choice(domain_list)
    prefix = "".join(random.choices(string.ascii_lowercase, k=5)) + "".join(random.choices(string.digits, k=3))
    email_str = f"{prefix}@{selected_domain}"

    if mode == "cloudflare_temp_email":
        headers = {"x-admin-auth": cfg.ADMIN_AUTH, "Content-Type": "application/json"}
        body = {"enablePrefix": False, "name": prefix, "domain": selected_domain}
        for attempt in range(5):
            if getattr(cfg, 'GLOBAL_STOP', False):
                return None, None
            try:
                res = requests.post(
                    f"{base_url}/admin/new_address",
                    headers=headers, json=body,
                    proxies=mail_proxies, verify=_ssl_verify(), timeout=15,
                )
                res.raise_for_status()
                data = res.json()
                if data and data.get("address"):
                    email = data["address"].strip()
                    jwt = data.get("jwt", "").strip()
                    print(f"[{cfg.ts()}] [INFO] [NW] cloudflare_temp_email 成功获取: {mask_email(email)}")
                    return email, jwt
                print(f"[{cfg.ts()}] [WARNING] [NW] 邮箱申请失败 (尝试 {attempt + 1}/5)")
                time.sleep(1)
            except Exception as e:
                print(f"[{cfg.ts()}] [ERROR] [NW] 邮箱注册网络异常: {e}")
                time.sleep(2)
        return None, None

    elif mode == "freemail":
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {cfg.FREEMAIL_API_TOKEN}"}
        for attempt in range(5):
            if getattr(cfg, 'GLOBAL_STOP', False):
                return None, None
            try:
                res = requests.post(
                    f"{cfg.FREEMAIL_API_URL}/api/create",
                    json={"email": email_str}, headers=headers,
                    proxies=mail_proxies, verify=_ssl_verify(), timeout=15,
                )
                res.raise_for_status()
                print(f"[{cfg.ts()}] [INFO] [NW] Freemail 成功创建: {mask_email(email_str)}")
                return email_str, ""
            except Exception as e:
                print(f"[{cfg.ts()}] [ERROR] [NW] Freemail 异常: {e}")
                time.sleep(2)
        return None, None

    elif mode == "cloudmail":
        from utils.email_providers.mail_service import get_cm_token
        token = get_cm_token(mail_proxies)
        if not token:
            print(f"[{cfg.ts()}] [ERROR] [NW] CloudMail Token 获取失败")
            return None, None
        try:
            res = requests.post(
                f"{cfg.CM_API_URL}/api/public/addUser",
                headers={"Authorization": token},
                json={"list": [{"email": email_str}]},
                proxies=mail_proxies, timeout=15,
            )
            if res.json().get("code") == 200:
                print(f"[{cfg.ts()}] [INFO] [NW] CloudMail 成功创建: {mask_email(email_str)}")
                return email_str, ""
        except Exception as e:
            print(f"[{cfg.ts()}] [ERROR] [NW] CloudMail 异常: {e}")
        return None, None

    elif mode == "mail_curl":
        try:
            url = f"{cfg.MC_API_BASE}/api/remail?key={cfg.MC_KEY}"
            res = requests.post(url, proxies=mail_proxies, verify=_ssl_verify(), timeout=15)
            data = res.json()
            if data.get("email") and data.get("id"):
                email = data["email"]
                mailbox_id = data["id"]
                print(f"[{cfg.ts()}] [INFO] [NW] mail-curl 分配: {mask_email(email)}")
                return email, mailbox_id
        except Exception as e:
            print(f"[{cfg.ts()}] [ERROR] [NW] mail-curl 异常: {e}")
        return None, None

    elif mode == "luckmail":
        try:
            from utils.email_providers.luckmail_service import LuckMailService
            lm_service = LuckMailService(
                api_key=cfg.LUCKMAIL_API_KEY,
                preferred_domain=getattr(cfg, 'LUCKMAIL_PREFERRED_DOMAIN', ""),
                proxies=mail_proxies,
                email_type=getattr(cfg, 'LUCKMAIL_EMAIL_TYPE', "ms_graph"),
                variant_mode=getattr(cfg, 'LUCKMAIL_VARIANT_MODE', ""),
            )
            tag_id = getattr(cfg, 'LUCKMAIL_TAG_ID', None)

            if getattr(cfg, 'LUCKMAIL_REUSE_PURCHASED', False):
                with _nw_luckmail_lock:
                    email, token, p_id = lm_service.get_random_purchased_email(
                        tag_id=tag_id, local_used_pids=_NW_USED_LUCKMAIL_PIDS,
                    )
                    if p_id:
                        _NW_USED_LUCKMAIL_PIDS.add(p_id)
                if email and token:
                    print(f"[{cfg.ts()}] [INFO] [NW] LuckMail 复用历史邮箱: {mask_email(email)}")
                    return email, token
                print(f"[{cfg.ts()}] [INFO] [NW] 无可用历史邮箱，购买新号...")

            email, token, p_id = lm_service.get_email_and_token(auto_tag=False)
            if email and token:
                print(f"[{cfg.ts()}] [INFO] [NW] LuckMail 成功获取: {mask_email(email)}")
                return email, token
        except Exception as e:
            print(f"[{cfg.ts()}] [ERROR] [NW] LuckMail 异常: {e}")
        return None, None

    elif mode == "imap":
        print(f"[{cfg.ts()}] [INFO] [NW] IMAP 模式生成: {mask_email(email_str)}")
        return email_str, ""

    elif mode == "local_microsoft":
        from utils.email_providers.local_microsoft_service import LocalMicrosoftService
        from utils import db_manager
        ms_service = LocalMicrosoftService(proxies=mail_proxies)
        mailbox_info = ms_service.get_unused_mailbox()
        if not mailbox_info:
            cfg.POOL_EXHAUSTED = True
            print(f"[{cfg.ts()}] [WARNING] [NW] 微软邮箱库已耗尽")
            return None, None
        email = mailbox_info["email"]
        print(f"[{cfg.ts()}] [INFO] [NW] 微软库分配: {mask_email(email)}")
        return email, json.dumps(mailbox_info, ensure_ascii=False)

    else:
        print(f"[{cfg.ts()}] [WARNING] [NW] 邮箱模式 '{mode}' 暂不支持独立获取，回退到公共接口")
        try:
            from utils.email_providers import tempmail_service, tempmail_org, generator_email_service
        except Exception:
            pass
        try:
            if mode == "tempmail":
                from utils.email_providers.tempmail_service import TempmailService
                svc = TempmailService(proxies=mail_proxies)
                email, token = svc.create_email()
                if email and token:
                    print(f"[{cfg.ts()}] [INFO] [NW] Tempmail 成功: {mask_email(email)}")
                    return email, token
            elif mode == "tempmail_org":
                from utils.email_providers.tempmail_org import TempMailOrgService
                svc = TempMailOrgService(proxies=mail_proxies)
                email, token = svc.create_email()
                if email and token:
                    print(f"[{cfg.ts()}] [INFO] [NW] TempMail.org 成功: {mask_email(email)}")
                    return email, token
            elif mode == "generator_email":
                from utils.email_providers.generator_email_service import GeneratorEmailService
                svc = GeneratorEmailService(proxies=mail_proxies)
                email, token = svc.create_email()
                if email and token:
                    print(f"[{cfg.ts()}] [INFO] [NW] Generator 成功: {mask_email(email)}")
                    return email, token
            elif mode == "duckmail":
                from utils.email_providers.duckmail_service import DuckMailService
                svc = DuckMailService(
                    api_url=getattr(cfg, 'DUCKMAIL_API_URL', 'https://api.duckmail.com'),
                    domain=getattr(cfg, 'DUCKMAIL_DOMAIN', ''),
                    duck_api_token=getattr(cfg, 'DUCK_API_TOKEN', ''),
                    cookie=getattr(cfg, 'DUCK_COOKIE', ''),
                    proxies=mail_proxies,
                )
                email, jwt = svc.create_email()
                if email:
                    print(f"[{cfg.ts()}] [INFO] [NW] DuckMail 成功: {mask_email(email)}")
                    return email, jwt
            elif mode == "fvia":
                from utils.email_providers.fvia_service import FviaMailService
                svc = FviaMailService(token=getattr(cfg, 'FVIA_TOKEN', ''), proxies=mail_proxies)
                email, token = svc.create_email()
                if email and token:
                    print(f"[{cfg.ts()}] [INFO] [NW] Fvia 成功: {mask_email(email)}")
                    return email, token
            elif mode == "tmailor":
                from utils.email_providers.tmailor_service import TmailorService
                svc = TmailorService(token=getattr(cfg, 'TMAILOR_CURRENT_TOKEN', ''), proxies=mail_proxies)
                email, token = svc.create_email()
                if email and token:
                    print(f"[{cfg.ts()}] [INFO] [NW] Tmailor 成功: {mask_email(email)}")
                    return email, token
            elif mode == "temporam":
                from utils.email_providers.temporam_service import TemporamService
                svc = TemporamService(cookie=getattr(cfg, 'TEMPORAM_COOKIE', ''), proxies=mail_proxies)
                email, token = svc.create_email()
                if email and token:
                    print(f"[{cfg.ts()}] [INFO] [NW] Temporam 成功: {mask_email(email)}")
                    return email, token
            elif mode == "temporarymail":
                from utils.email_providers.temporarymail_service import TemporaryMailService
                svc = TemporaryMailService(proxies=mail_proxies)
                email, token = svc.create_email()
                if email and token:
                    print(f"[{cfg.ts()}] [INFO] [NW] TemporaryMail 成功: {mask_email(email)}")
                    return email, token
            elif mode == "inboxes":
                from utils.email_providers.inboxes_service import InboxesService
                svc = InboxesService(proxies=mail_proxies)
                email, token = svc.create_email()
                if email and token:
                    print(f"[{cfg.ts()}] [INFO] [NW] Inboxes 成功: {mask_email(email)}")
                    return email, token
        except Exception as e:
            print(f"[{cfg.ts()}] [ERROR] [NW] 邮箱获取异常: {e}")

        print(f"[{cfg.ts()}] [ERROR] [NW] 邮箱获取失败 (模式: {mode})")
        return None, None


def _generate_password(length: int = 16) -> str:
    upper = random.choices(string.ascii_uppercase, k=2)
    lower = random.choices(string.ascii_lowercase, k=2)
    digits = random.choices(string.digits, k=2)
    specials = random.choices("!@#$%&*", k=2)
    pool = string.ascii_letters + string.digits + "!@#$%&*"
    rest = random.choices(pool, k=length - 8)
    chars = upper + lower + digits + specials + rest
    random.shuffle(chars)
    return "".join(chars)


def _generate_name() -> str:
    return f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"


def _solve_turnstile_capsolver(api_key: str, site_url: str, sitekey: str, proxies: Any = None) -> str:
    try:
        create_payload = {
            "clientKey": api_key,
            "task": {
                "type": "NoCaptchaTaskProxyless",
                "websiteURL": site_url,
                "websiteKey": sitekey,
            },
        }
        resp = requests.post("https://api.capsolver.com/createTask", json=create_payload, timeout=30)
        data = resp.json()
        if data.get("errorId", 0) != 0:
            raise RuntimeError(f"CapSolver createTask error: {data.get('errorDescription', 'unknown')}")
        task_id = data.get("taskId")
        if not task_id:
            raise RuntimeError("CapSolver: no taskId returned")

        for _ in range(60):
            time.sleep(3)
            result_resp = requests.post(
                "https://api.capsolver.com/getTaskResult",
                json={"clientKey": api_key, "taskId": task_id},
                timeout=30,
            )
            result_data = result_resp.json()
            status = result_data.get("status", "")
            if status == "ready":
                token = (result_data.get("solution", {}) or {}).get("gRecaptchaResponse", "")
                if token:
                    return token
                raise RuntimeError("CapSolver: task ready but no token in solution")
            if status == "failed":
                raise RuntimeError(f"CapSolver task failed: {result_data.get('errorDescription', 'unknown')}")
        raise RuntimeError("CapSolver: task timeout after 180s")
    except Exception as e:
        print(f"[{cfg.ts()}] [ERROR] CapSolver Turnstile 解题异常: {e}")
        return ""


def _solve_turnstile_2captcha(api_key: str, site_url: str, sitekey: str, proxies: Any = None) -> str:
    try:
        resp = requests.post(
            "https://api.2captcha.com/in.php",
            data={
                "key": api_key,
                "method": "turnstile",
                "sitekey": sitekey,
                "pageurl": site_url,
                "json": 1,
            },
            timeout=30,
        )
        data = resp.json()
        if data.get("status") != 1:
            raise RuntimeError(f"2Captcha in.php error: {data.get('request', 'unknown')}")
        request_id = data.get("request")

        for _ in range(60):
            time.sleep(5)
            result_resp = requests.get(
                "https://api.2captcha.com/res.php",
                params={"key": api_key, "action": "get", "id": request_id, "json": 1},
                timeout=30,
            )
            result_data = result_resp.json()
            if result_data.get("status") == 1:
                return result_data.get("request", "")
            if result_data.get("request") != "CAPCHA_NOT_READY":
                raise RuntimeError(f"2Captcha error: {result_data.get('request', 'unknown')}")
        raise RuntimeError("2Captcha: task timeout after 300s")
    except Exception as e:
        print(f"[{cfg.ts()}] [ERROR] 2Captcha Turnstile 解题异常: {e}")
        return ""


def _solve_turnstile_playwright(site_url: str, sitekey: str, proxy: str = None) -> str:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(f"[{cfg.ts()}] [ERROR] Playwright 未安装，请执行: pip install playwright && playwright install chromium")
        return ""

    token = ""
    try:
        with sync_playwright() as p:
            launch_args = {"headless": True, "args": ["--no-sandbox", "--disable-gpu"]}
            if proxy:
                launch_args["proxy"] = {"server": proxy}
            browser = p.chromium.launch(**launch_args)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            )
            page = context.new_page()

            turnstile_callback_js = f"""
            () => {{
                const originalCallback = window.turnstile && window.turnstile.getResponse;
                if (originalCallback) {{
                    return originalCallback();
                }}
                const el = document.querySelector('[name="cf-turnstile-response"]');
                if (el && el.value) return el.value;
                const iframe = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
                if (!iframe) return '';
                return '';
            }}
            """

            page.goto(site_url, wait_until="networkidle", timeout=30000)
            time.sleep(5)

            for attempt in range(30):
                try:
                    result = page.evaluate(turnstile_callback_js)
                    if result and len(result) > 20:
                        token = result
                        break
                except Exception:
                    pass
                time.sleep(2)

            browser.close()
    except Exception as e:
        print(f"[{cfg.ts()}] [ERROR] Playwright Turnstile 解题异常: {e}")
    return token


def _solve_turnstile(proxy: str = None) -> str:
    nw_cfg = getattr(cfg, "_c", {}).get("neuralwatt_mode", {})
    turnstile_service = str(nw_cfg.get("turnstile_service", "")).strip().lower()
    turnstile_api_key = str(nw_cfg.get("turnstile_api_key", "")).strip()

    if turnstile_service and turnstile_api_key:
        if turnstile_service == "capsolver":
            print(f"[{cfg.ts()}] [INFO] 正在通过 CapSolver 解 Turnstile...")
            token = _solve_turnstile_capsolver(turnstile_api_key, NW_REGISTER_URL, NW_TURNSTILE_SITEKEY)
            if token:
                print(f"[{cfg.ts()}] [SUCCESS] CapSolver Turnstile 解题成功")
                return token
        elif turnstile_service == "2captcha":
            print(f"[{cfg.ts()}] [INFO] 正在通过 2Captcha 解 Turnstile...")
            token = _solve_turnstile_2captcha(turnstile_api_key, NW_REGISTER_URL, NW_TURNSTILE_SITEKEY)
            if token:
                print(f"[{cfg.ts()}] [SUCCESS] 2Captcha Turnstile 解题成功")
                return token

    print(f"[{cfg.ts()}] [INFO] 正在通过 Playwright 解 Turnstile...")
    token = _solve_turnstile_playwright(NW_REGISTER_URL, NW_TURNSTILE_SITEKEY, proxy)
    if token:
        print(f"[{cfg.ts()}] [SUCCESS] Playwright Turnstile 解题成功")
    else:
        print(f"[{cfg.ts()}] [ERROR] Turnstile 解题失败，所有方法均已尝试")
    return token


def get_verify_link_from_email(
        email: str,
        jwt: str = "",
        proxies: Any = None,
        processed_mail_ids: set = None,
        max_attempts: int = 30,
        link_patterns: list = None,
) -> str:
    if processed_mail_ids is None:
        processed_mail_ids = set()
    mail_proxies = proxies if cfg.USE_PROXY_FOR_EMAIL else None
    base_url = cfg.GPTMAIL_BASE.rstrip("/")
    mode = cfg.EMAIL_API_MODE

    if link_patterns is None:
        link_patterns = [
            r"https?://portal\.neuralwatt\.com/auth/verify[^\s\"'<>]+",
            r"https?://portal\.neuralwatt\.com/confirm[^\s\"'<>]+",
            r"https?://portal\.neuralwatt\.com/auth/confirm-email[^\s\"'<>]+",
            r"https?://portal\.neuralwatt\.com/auth/activate[^\s\"'<>]+",
        ]

    generic_link_patterns = [
        r"https?://[^\s\"'<>]*verify[^\s\"'<>]*",
        r"https?://[^\s\"'<>]*confirm[^\s\"'<>]*",
        r"https?://[^\s\"'<>]*activate[^\s\"'<>]*",
        r"https?://[^\s\"'<>]*click[^\s\"'<>]*",
    ]

    print(f"[{cfg.ts()}] [INFO] 等待接收验证链接 ({mask_email(email)})...")

    for attempt in range(max_attempts):
        if getattr(cfg, 'GLOBAL_STOP', False):
            return ""
        try:
            if mode == "cloudflare_temp_email" or not mode:
                if jwt:
                    res = requests.get(
                        f"{base_url}/api/mails",
                        params={"limit": 20, "offset": 0},
                        headers={"Authorization": f"Bearer {jwt}", "Content-Type": "application/json", "Accept": "application/json"},
                        proxies=mail_proxies, verify=_ssl_verify(), timeout=15,
                    )
                else:
                    res = requests.get(
                        f"{base_url}/admin/mails",
                        params={"limit": 20, "offset": 0, "address": email},
                        headers={"x-admin-auth": cfg.ADMIN_AUTH},
                        proxies=mail_proxies, verify=_ssl_verify(), timeout=15,
                    )
                if res.status_code != 200:
                    time.sleep(3)
                    continue
                results = res.json().get("results")
                if results:
                    for mail in results:
                        mail_id = mail.get("id")
                        if not mail_id or mail_id in processed_mail_ids:
                            continue
                        raw_html = str(mail.get("html") or mail.get("text") or mail.get("content") or mail.get("body") or "")
                        subject = str(mail.get("subject") or "")
                        sender = str(mail.get("source") or mail.get("from") or "")
                        if "neuralwatt" not in sender.lower() and "neuralwatt" not in raw_html.lower() and "neuralwatt" not in subject.lower():
                            continue
                        link = _extract_link_from_html(raw_html, link_patterns, generic_link_patterns)
                        if link:
                            processed_mail_ids.add(mail_id)
                            print(f"[{cfg.ts()}] [SUCCESS] ({mask_email(email)})验证链接提取成功")
                            return link

            elif mode == "freemail":
                headers = {"Content-Type": "application/json", "Authorization": f"Bearer {cfg.FREEMAIL_API_TOKEN}"}
                res = requests.get(f"{cfg.FREEMAIL_API_URL}/api/emails", params={"mailbox": email, "limit": 20}, headers=headers, proxies=mail_proxies, verify=_ssl_verify(), timeout=15)
                if res.status_code == 200:
                    raw_data = res.json()
                    emails_list = raw_data.get("data") or raw_data.get("emails") or raw_data.get("messages") or raw_data.get("results") or []
                    if not isinstance(emails_list, list):
                        emails_list = []
                    for mail in emails_list:
                        mail_id = str(mail.get("id") or mail.get("timestamp") or "")
                        if not mail_id or mail_id in processed_mail_ids:
                            continue
                        html_content = str(mail.get("html_content") or mail.get("html") or mail.get("content") or "")
                        if not html_content:
                            try:
                                dr = requests.get(f"{cfg.FREEMAIL_API_URL}/api/email/{mail_id}", headers=headers, proxies=mail_proxies, verify=_ssl_verify(), timeout=15)
                                if dr.status_code == 200:
                                    d = dr.json()
                                    html_content = str(d.get("html_content") or d.get("content") or "")
                            except Exception:
                                pass
                        if "neuralwatt" not in html_content.lower():
                            continue
                        link = _extract_link_from_html(html_content, link_patterns, generic_link_patterns)
                        if link:
                            processed_mail_ids.add(mail_id)
                            print(f"[{cfg.ts()}] [SUCCESS] freemail ({mask_email(email)})验证链接提取成功")
                            return link

            elif mode == "cloudmail":
                from utils.email_providers.mail_service import _CM_TOKEN_CACHE
                token = _get_cm_token(mail_proxies)
                if token:
                    res = requests.post(f"{cfg.CM_API_URL}/api/public/emailList", headers={"Authorization": token}, json={"toEmail": email, "timeSort": "desc", "size": 10}, proxies=mail_proxies, timeout=15)
                    if res.status_code == 200:
                        for m in res.json().get("data", []):
                            m_id = str(m.get("emailId"))
                            if m_id in processed_mail_ids:
                                continue
                            html_content = str(m.get("htmlContent") or m.get("text") or m.get("content") or "")
                            if "neuralwatt" not in html_content.lower() and "neuralwatt" not in str(m.get("sendEmail", "")).lower():
                                continue
                            link = _extract_link_from_html(html_content, link_patterns, generic_link_patterns)
                            if link:
                                processed_mail_ids.add(m_id)
                                print(f"[{cfg.ts()}] [SUCCESS] CloudMail ({mask_email(email)})验证链接提取成功")
                                return link

            elif mode == "imap":
                link = _get_link_via_imap(email, mail_proxies, processed_mail_ids, link_patterns, generic_link_patterns)
                if link:
                    return link

        except Exception as e:
            print(f"[{cfg.ts()}] [WARNING] 轮询验证链接异常: {e}")
        time.sleep(3)

    print(f"[{cfg.ts()}] [ERROR] ({mask_email(email)})验证链接接收超时")
    return ""


def _extract_link_from_html(html_content: str, specific_patterns: list, generic_patterns: list) -> str:
    if not html_content:
        return ""

    href_links = re.findall(r'href=["\']?(https?://[^"\'>\s]+)', html_content)
    all_urls = re.findall(r'https?://[^\s"\'<>\]]+', html_content)
    candidates = list(dict.fromkeys(href_links + all_urls))

    for pattern in specific_patterns:
        for url in candidates:
            if re.search(pattern, url, re.IGNORECASE):
                clean = url.rstrip(".'\",;)>")
                if clean:
                    return clean

    for url in candidates:
        url_lower = url.lower()
        if "neuralwatt" in url_lower and any(kw in url_lower for kw in ["verify", "confirm", "activate", "token=", "code="]):
            clean = url.rstrip(".'\",;)>")
            if clean:
                return clean

    for pattern in generic_patterns:
        for url in candidates:
            if re.search(pattern, url, re.IGNORECASE):
                clean = url.rstrip(".'\",;)>")
                if clean:
                    return clean

    return ""


def _get_link_via_imap(email: str, proxies: Any, processed_mail_ids: set, specific_patterns: list, generic_patterns: list) -> str:
    from utils.email_providers.mail_service import _create_imap_conn, _extract_body_from_message
    from email import message_from_string
    from email.policy import default as email_policy

    try:
        conn = _create_imap_conn(proxies)
        if not conn:
            return ""
        conn.select("INBOX")
        _, msg_ids = conn.search(None, "UNSEEN")
        if not msg_ids[0]:
            conn.logout()
            return ""
        for mid in msg_ids[0].split()[-10:]:
            mid_str = mid.decode() if isinstance(mid, bytes) else str(mid)
            if mid_str in processed_mail_ids:
                continue
            _, msg_data = conn.fetch(mid, "(RFC822)")
            if not msg_data or not msg_data[0]:
                continue
            raw_email = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
            if isinstance(raw_email, bytes):
                raw_email = raw_email.decode("utf-8", errors="replace")
            msg = message_from_string(raw_email, policy=email_policy)
            html_part = ""
            if msg.is_multipart():
                for part in msg.walk():
                    ct = (part.get_content_type() or "").lower()
                    if ct == "text/html":
                        try:
                            payload = part.get_payload(decode=True)
                            charset = part.get_content_charset() or "utf-8"
                            html_part = payload.decode(charset, errors="replace") if payload else ""
                        except Exception:
                            pass
                        break
            if not html_part:
                html_part = raw_email
            if "neuralwatt" not in html_part.lower():
                continue
            link = _extract_link_from_html(html_part, specific_patterns, generic_patterns)
            if link:
                processed_mail_ids.add(mid_str)
                conn.logout()
                return link
        conn.logout()
    except Exception as e:
        print(f"[{cfg.ts()}] [WARNING] IMAP 验证链接提取异常: {e}")
    return ""


def _get_cm_token(proxies: Any = None) -> str:
    from utils.email_providers.mail_service import _CM_TOKEN_CACHE
    if _CM_TOKEN_CACHE:
        return _CM_TOKEN_CACHE
    try:
        res = requests.post(
            f"{cfg.CM_API_URL}/api/public/login",
            json={"email": cfg.CM_ADMIN_EMAIL, "password": cfg.CM_ADMIN_PASS},
            proxies=proxies, verify=_ssl_verify(), timeout=15,
        )
        data = res.json()
        token = data.get("data", {}).get("token", "") if data.get("code") == 200 else ""
        if token:
            return token
    except Exception:
        pass
    return ""


def _do_register(
        email: str,
        password: str,
        name: str,
        turnstile_token: str,
        proxies: Any = None,
) -> Tuple[int, str]:
    form_data = {
        "name": name,
        "email": email,
        "password": password,
        "confirm_password": password,
        "company": "",
        "terms": "on",
        "cf-turnstile-response": turnstile_token,
        "posthog_distinct_id": "",
    }
    try:
        resp = requests.post(
            NW_REGISTER_URL,
            data=form_data,
            proxies=proxies,
            verify=_ssl_verify(),
            timeout=30,
            impersonate="chrome110",
            allow_redirects=False,
        )
        location = resp.headers.get("Location", "")
        if resp.status_code in (301, 302, 303, 307):
            if "verify" in location.lower() or "check-email" in location.lower() or "confirm" in location.lower():
                return 200, f"注册成功，需要邮箱验证: {location}"
            if "dashboard" in location.lower() or "/auth/login" in location.lower():
                return 200, "注册成功"
        if resp.status_code == 200:
            text = resp.text or ""
            if "verification" in text.lower() or "verify" in text.lower() or "check your email" in text.lower():
                return 200, "注册成功，需要邮箱验证"
            if "already" in text.lower() or "exists" in text.lower():
                return 409, "邮箱已注册"
            if "invalid" in text.lower() and "turnstile" in text.lower():
                return 403, "Turnstile 验证失败"
            return 200, "注册表单已提交"
        return resp.status_code, f"注册返回非预期状态码: {resp.status_code}"
    except Exception as e:
        return 0, f"注册请求异常: {e}"


def _do_login(email: str, password: str, proxies: Any = None) -> Tuple[bool, str]:
    try:
        session = requests.Session(proxies=proxies, impersonate="chrome110")
        resp = session.post(
            NW_LOGIN_URL,
            data={"email": email, "password": password},
            verify=_ssl_verify(),
            timeout=30,
            allow_redirects=False,
        )
        if resp.status_code in (301, 302, 303, 307):
            location = resp.headers.get("Location", "")
            if "dashboard" in location.lower() or "usage" in location.lower():
                return True, "登录成功"
            if "verify" in location.lower():
                return False, "需要邮箱验证后才能登录"
        if resp.status_code == 200:
            text = resp.text or ""
            if "invalid" in text.lower() or "incorrect" in text.lower():
                return False, "邮箱或密码错误"
            if "dashboard" in text.lower():
                return True, "登录成功"
        return False, f"登录返回非预期状态: {resp.status_code}"
    except Exception as e:
        return False, f"登录请求异常: {e}"


class NeuralwattClient:
    def __init__(self, email: str, password: str, proxies: Any = None):
        self.email = email
        self.password = password
        self.proxies = proxies
        self.session = requests.Session(proxies=proxies, impersonate="chrome110")
        self._logged_in = False

    def login(self) -> Tuple[bool, str]:
        try:
            resp = self.session.post(
                NW_LOGIN_URL,
                data={"email": self.email, "password": self.password},
                verify=_ssl_verify(),
                timeout=30,
                allow_redirects=True,
            )
            final_url = str(resp.url or "")
            if "dashboard" in final_url.lower() or resp.status_code == 200 and "api-key" in resp.text.lower():
                self._logged_in = True
                return True, "登录成功"
            if "verify" in final_url.lower():
                return False, "需要邮箱验证"
            return False, "登录失败"
        except Exception as e:
            return False, f"登录异常: {e}"

    def create_api_key(self, key_name: str = "auto-generated") -> Tuple[bool, str]:
        if not self._logged_in:
            ok, msg = self.login()
            if not ok:
                return False, msg
        try:
            dashboard_resp = self.session.get(f"{NW_PORTAL_BASE}/dashboard/keys", verify=_ssl_verify(), timeout=15)
            csrf_match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', dashboard_resp.text or "")
            csrf_token = csrf_match.group(1) if csrf_match else ""

            create_resp = self.session.post(
                f"{NW_PORTAL_BASE}/dashboard/keys/create",
                data={"name": key_name, "csrf_token": csrf_token} if csrf_token else {"name": key_name},
                verify=_ssl_verify(),
                timeout=15,
                allow_redirects=False,
            )
            if create_resp.status_code in (301, 302, 303, 307):
                return True, "API Key 创建请求已提交"

            text = create_resp.text or ""
            sk_match = re.search(r'(sk-[a-zA-Z0-9]{20,})', text)
            if sk_match:
                return True, sk_match.group(1)
            return True, "API Key 创建请求已发送"
        except Exception as e:
            return False, f"API Key 创建异常: {e}"

    def get_api_keys(self) -> list:
        if not self._logged_in:
            ok, _ = self.login()
            if not ok:
                return []
        try:
            resp = self.session.get(f"{NW_PORTAL_BASE}/dashboard/keys", verify=_ssl_verify(), timeout=15)
            text = resp.text or ""
            return re.findall(r'(sk-[a-zA-Z0-9]{20,})', text)
        except Exception:
            return []


def _verify_email_link(link: str, proxies: Any = None) -> bool:
    try:
        resp = requests.get(
            link,
            proxies=proxies,
            verify=_ssl_verify(),
            timeout=30,
            impersonate="chrome110",
            allow_redirects=True,
        )
        final_url = str(resp.url or "")
        text = resp.text or ""
        if resp.status_code == 200:
            if "verified" in text.lower() or "success" in text.lower() or "confirmed" in text.lower():
                return True
            if "dashboard" in final_url.lower() or "login" in final_url.lower():
                return True
            if "already" in text.lower() and "verified" in text.lower():
                return True
        return False
    except Exception as e:
        print(f"[{cfg.ts()}] [WARNING] 验证链接访问异常: {e}")
        return False


def run(proxy: Optional[str], run_ctx: dict = None) -> Tuple[Optional[str], Optional[str]]:
    if getattr(cfg, "QG_SHORT_PROXY_ENABLE", False):
        picked_proxy = pick_working_qg_short_proxy(force_refresh=False)
        if not picked_proxy:
            print(f"[{cfg.ts()}] [ERROR] 短效代理候选均不可用，已终止本轮 Neuralwatt 注册。")
            return None, None
        proxy = picked_proxy

    proxy = cfg.format_docker_url(proxy)
    if proxy and proxy.startswith("socks5://"):
        proxy = proxy.replace("socks5://", "socks5h://")
    proxies = {"http": proxy, "https": proxy} if proxy else None

    email, email_jwt = _nw_get_email_and_token(proxies)
    if not email:
        return None, None

    password = _generate_password()
    name = _generate_name()
    print(f"[{cfg.ts()}] [INFO] ({mask_email(email)}) Neuralwatt 注册开始 (名称: {name})")

    turnstile_token = _solve_turnstile(proxy)
    if not turnstile_token:
        print(f"[{cfg.ts()}] [ERROR] ({mask_email(email)}) Turnstile 解题失败，无法注册")
        if run_ctx is not None:
            run_ctx['turnstile_failed'] = True
        return None, None

    print(f"[{cfg.ts()}] [INFO] ({mask_email(email)}) 正在提交注册表单...")
    status, msg = _do_register(email, password, name, turnstile_token, proxies)

    if status == 409:
        print(f"[{cfg.ts()}] [WARNING] ({mask_email(email)}) 邮箱已注册: {msg}")
        return None, None
    if status == 403:
        print(f"[{cfg.ts()}] [ERROR] ({mask_email(email)}) Turnstile 验证失败: {msg}")
        if run_ctx is not None:
            run_ctx['turnstile_failed'] = True
        return None, None
    if status != 200:
        print(f"[{cfg.ts()}] [ERROR] ({mask_email(email)}) 注册失败: {msg}")
        return None, None

    print(f"[{cfg.ts()}] [INFO] ({mask_email(email)}) 注册表单已提交: {msg}")

    needs_verify = "验证" in msg or "verify" in msg.lower() or "check" in msg.lower()
    if needs_verify:
        print(f"[{cfg.ts()}] [INFO] ({mask_email(email)}) 需要邮箱验证，正在等待验证链接...")
        verify_link = get_verify_link_from_email(email, jwt=email_jwt, proxies=proxies)
        if not verify_link:
            print(f"[{cfg.ts()}] [ERROR] ({mask_email(email)}) 验证链接获取超时")
            return None, None

        print(f"[{cfg.ts()}] [INFO] ({mask_email(email)}) 正在访问验证链接...")
        verified = _verify_email_link(verify_link, proxies)
        if not verified:
            print(f"[{cfg.ts()}] [WARNING] ({mask_email(email)}) 验证链接访问可能未成功，继续尝试登录...")
        else:
            print(f"[{cfg.ts()}] [SUCCESS] ({mask_email(email)}) 邮箱验证成功")

    time.sleep(random.randint(3, 8))

    print(f"[{cfg.ts()}] [INFO] ({mask_email(email)}) 正在登录...")
    client = NeuralwattClient(email, password, proxies)
    login_ok, login_msg = client.login()
    if not login_ok:
        print(f"[{cfg.ts()}] [ERROR] ({mask_email(email)}) 登录失败: {login_msg}")
        token_data = {
            "email": email,
            "password": password,
            "type": "neuralwatt",
            "api_key": "",
            "login_status": "failed",
        }
        return json.dumps(token_data, ensure_ascii=False, separators=(",", ":")), password

    api_key = ""

    if getattr(cfg, "NW_AUTO_CREATE_API_KEY", True):
        print(f"[{cfg.ts()}] [INFO] ({mask_email(email)}) 正在创建 API Key...")
        key_ok, key_result = client.create_api_key("auto-reg")

        if key_ok and key_result.startswith("sk-"):
            api_key = key_result
            print(f"[{cfg.ts()}] [SUCCESS] ({mask_email(email)}) API Key 创建成功: {api_key[:8]}...")
        else:
            existing_keys = client.get_api_keys()
            if existing_keys:
                api_key = existing_keys[0]
                print(f"[{cfg.ts()}] [SUCCESS] ({mask_email(email)}) 使用现有 API Key: {api_key[:8]}...")
            else:
                print(f"[{cfg.ts()}] [WARNING] ({mask_email(email)}) API Key 创建结果: {key_result}")
    else:
        existing_keys = client.get_api_keys()
        if existing_keys:
            api_key = existing_keys[0]
            print(f"[{cfg.ts()}] [INFO] ({mask_email(email)}) 跳过创建，使用现有 API Key: {api_key[:8]}...")
        else:
            print(f"[{cfg.ts()}] [WARNING] ({mask_email(email)}) 跳过创建且无现有 API Key")

    nw_cfg = getattr(cfg, "_c", {}).get("neuralwatt_mode", {})
    test_model = str(nw_cfg.get("test_model", "meta-llama/Llama-3.3-70B-Instruct")).strip()

    token_data = {
        "email": email,
        "password": password,
        "type": "neuralwatt",
        "api_key": api_key,
        "api_base": f"{NW_API_BASE}/v1",
        "test_model": test_model,
        "login_status": "ok" if login_ok else "failed",
        "key_status": "ok" if api_key else "pending",
    }
    return json.dumps(token_data, ensure_ascii=False, separators=(",", ":")), password
