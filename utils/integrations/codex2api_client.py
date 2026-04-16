import json
import logging
from typing import Any, Dict, List, Tuple

from curl_cffi import requests as cffi_requests

logger = logging.getLogger(__name__)


class Codex2APIClient:
    def __init__(self, api_url: str, admin_key: str):
        self.api_url = api_url.rstrip("/")
        self.headers = {
            "Content-Type": "application/json",
            "X-Admin-Key": admin_key,
        }
        self.request_kwargs = {
            "timeout": 15,
            "impersonate": "chrome110",
        }

    def _handle_response(
        self,
        response: cffi_requests.Response,
        success_codes: Tuple[int, ...] = (200, 201, 204),
    ) -> Tuple[bool, Any]:
        if response.status_code in success_codes:
            try:
                return True, response.json() if response.text else {}
            except ValueError:
                return True, response.text

        error_msg = f"HTTP {response.status_code}"
        try:
            detail = response.json()
            if isinstance(detail, dict):
                error_msg = (
                    detail.get("message")
                    or detail.get("error")
                    or detail.get("detail")
                    or error_msg
                )
        except Exception:
            error_msg = f"{error_msg} - {response.text[:200]}"

        return False, error_msg

    def push_account(self, token_data: Dict[str, Any]) -> Tuple[bool, str]:
        url = f"{self.api_url}/api/admin/accounts/push-token"
        payload = {
            "name": str(token_data.get("email") or token_data.get("account_id") or "push-account")[:100],
            "email": token_data.get("email", ""),
            "proxy_url": token_data.get("proxy_url", "") or token_data.get("proxy", ""),
            "account_id": token_data.get("account_id", ""),
            "refresh_token": token_data.get("refresh_token", ""),
            "access_token": token_data.get("access_token", ""),
            "id_token": token_data.get("id_token", ""),
            "source": token_data.get("source", "register-oss"),
        }

        try:
            response = cffi_requests.post(
                url,
                json=payload,
                headers=self.headers,
                timeout=30,
                impersonate="chrome110",
                proxies=None,
            )
            ok, result = self._handle_response(response, success_codes=(200, 201))
            if not ok:
                return False, str(result)

            if isinstance(result, dict):
                return True, str(result.get("message") or "Codex2API account import succeeded")
            return True, "Codex2API account import succeeded"
        except Exception as exc:
            logger.error("Push Codex2API account failed: %s", exc)
            return False, f"Network request failed: {exc}"

    def get_accounts(self) -> Tuple[bool, Any]:
        url = f"{self.api_url}/api/admin/accounts"
        try:
            response = cffi_requests.get(url, headers=self.headers, **self.request_kwargs)
            return self._handle_response(response)
        except Exception as exc:
            logger.error("Get Codex2API accounts failed: %s", exc)
            return False, str(exc)

    def get_all_accounts(self) -> Tuple[bool, Any]:
        ok, data = self.get_accounts()
        if not ok:
            return False, data

        if isinstance(data, dict):
            if isinstance(data.get("accounts"), list):
                return True, data.get("accounts", [])
            if isinstance(data.get("data"), dict) and isinstance(data["data"].get("accounts"), list):
                return True, data["data"].get("accounts", [])

        return False, "Unexpected Codex2API account list response"

    def refresh_account(self, account_id: str) -> Tuple[bool, Any]:
        url = f"{self.api_url}/api/admin/accounts/{account_id}/refresh"
        try:
            response = cffi_requests.post(
                url,
                headers=self.headers,
                json={},
                timeout=30,
                impersonate="chrome110",
                proxies=None,
            )
            return self._handle_response(response)
        except Exception as exc:
            logger.warning("Refresh Codex2API account %s failed: %s", account_id, exc)
            return False, str(exc)

    def test_account(self, account_id: str) -> Tuple[str, str]:
        url = f"{self.api_url}/api/admin/accounts/{account_id}/test"

        for attempt in range(2):
            try:
                response = cffi_requests.get(
                    url,
                    headers=self.headers,
                    timeout=60,
                    impersonate="chrome110",
                    proxies=None,
                )
                if response.status_code != 200:
                    body_text = response.text[:500]
                    if attempt == 0 and response.status_code in (400, 404) and any(
                        keyword in body_text for keyword in ("Access Token", "先刷新", "运行时池")
                    ):
                        self.refresh_account(str(account_id))
                        continue
                    return _classify_sse_error(body_text or f"HTTP {response.status_code}")

                for line in response.text.splitlines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue

                    raw = line[5:].strip()
                    if not raw or raw == "[DONE]":
                        continue

                    try:
                        event = json.loads(raw)
                    except Exception:
                        continue

                    event_type = event.get("type", "")
                    if event_type == "test_complete":
                        if event.get("success"):
                            return "ok", "test completed"
                        err = str(event.get("error") or event.get("text") or "")
                        return _classify_sse_error(err)

                    if event_type == "error":
                        err = str(event.get("error") or event.get("text") or "")
                        return _classify_sse_error(err)

                return "ok", "no terminal SSE event, skipped"
            except Exception as exc:
                logger.warning("Codex2API test_account %s failed: %s", account_id, exc)
                if attempt == 0:
                    self.refresh_account(str(account_id))
                    continue
                return "ok", f"test error, skipped: {str(exc)}"

        return "ok", "skipped"

    def set_account_status(self, account_id: str, disabled: bool) -> bool:
        url = f"{self.api_url}/api/admin/accounts/{account_id}/lock"
        try:
            response = cffi_requests.post(
                url,
                json={"locked": disabled},
                headers=self.headers,
                timeout=30,
                impersonate="chrome110",
                proxies=None,
            )
            ok, _ = self._handle_response(response)
            if not ok:
                return False

            if not disabled:
                reset_url = f"{self.api_url}/api/admin/accounts/{account_id}/reset-status"
                try:
                    cffi_requests.post(
                        reset_url,
                        json={},
                        headers=self.headers,
                        timeout=30,
                        impersonate="chrome110",
                        proxies=None,
                    )
                except Exception as exc:
                    logger.warning("Reset Codex2API account %s status failed: %s", account_id, exc)
            return True
        except Exception as exc:
            logger.error("Set Codex2API account %s status failed: %s", account_id, exc)
            return False

    def delete_account(self, account_id: str) -> Tuple[bool, Any]:
        url = f"{self.api_url}/api/admin/accounts/{account_id}"
        try:
            response = cffi_requests.delete(url, headers=self.headers, **self.request_kwargs)
            return self._handle_response(response, success_codes=(200, 204))
        except Exception as exc:
            logger.error("Delete Codex2API account %s failed: %s", account_id, exc)
            return False, str(exc)


def _classify_sse_error(err_text: str) -> Tuple[str, str]:
    text = str(err_text or "").lower()
    if any(keyword in text for keyword in ("429", "rate_limit", "rate limit", "too many request")):
        return "quota", f"quota limited: {str(err_text)[:120]}"
    if text.strip():
        return "dead", f"test failed: {str(err_text)[:120]}"
    return "ok", "empty SSE error, skipped"
