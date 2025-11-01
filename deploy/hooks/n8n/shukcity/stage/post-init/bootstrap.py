"""
n8n post-init bootstrap script

Purpose:
- Detect if the n8n instance owner is already set up; if not, create it
  using environment variables. Designed to be idempotent and safe to re-run.

Inputs (via environment):
- N8N_URL: Base URL to reach n8n (e.g., http://n8n:5678). Defaults to http://localhost:5678
- N8N_ALT_URLS: Comma-separated fallback base URLs to try if N8N_URL fails
- N8N_BOOTSTRAP_WAIT_SECONDS: Total time to wait across all URLs (default: 120)
- N8N_OWNER_EMAIL: Owner email (default: owner@example.com)
- N8N_OWNER_PASSWORD: Owner password (default: owner)
- N8N_OWNER_FIRST_NAME: Owner first name (default: Owner)
- N8N_OWNER_LAST_NAME: Owner last name (default: User)

Notes:
- This script uses urllib only (no third-party dependencies).
- It polls /rest/settings until n8n is ready, then checks userManagement flags.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Tuple


def _http_json_get(url: str, timeout: float = 6.0) -> Tuple[int, Dict[str, Any]]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        status = getattr(resp, "status", resp.getcode())
        raw = resp.read()
        data = json.loads(raw.decode("utf-8") or "{}")
        return status, data


def _http_json_post(url: str, payload: Dict[str, Any], timeout: float = 10.0) -> Tuple[int, Dict[str, Any]]:
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json",
               "Accept": "application/json"}
    req = urllib.request.Request(
        url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        status = getattr(resp, "status", resp.getcode())
        raw = resp.read()
        body = json.loads(raw.decode("utf-8") or "{}")
        return status, body


def _is_dns_error(message: str) -> bool:
    msg = (message or "").lower()
    return (
        "temporary failure in name resolution" in msg
        or "name or service not known" in msg
        or "nodename nor servname provided" in msg
        or "getaddrinfo failed" in msg
        or "[errno -3]" in msg
        or "[errno -2]" in msg
    )


def _wait_for_ready(base_url: str, timeout_seconds: float = 60.0, abort_on_dns_failure: bool = False) -> Dict[str, Any]:
    deadline = time.time() + timeout_seconds
    last_err: str = ""
    settings_url = f"{base_url}/rest/settings"
    while time.time() < deadline:
        try:
            status, data = _http_json_get(settings_url, timeout=5.0)
            if status in (200, 204) and isinstance(data, dict):
                return data
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            if abort_on_dns_failure and _is_dns_error(last_err):
                raise RuntimeError(f"DNS error for {base_url}: {last_err}")
        time.sleep(2.0)
    raise RuntimeError(f"n8n not ready at {settings_url}: {last_err}")


def _candidate_base_urls() -> list[str]:
    primary = (os.environ.get("N8N_URL") or "http://n8n:5678").strip()
    alts_raw = (os.environ.get("N8N_ALT_URLS") or "").strip()
    candidates: list[str] = []
    if primary:
        candidates.append(primary)
    if alts_raw:
        for part in alts_raw.split(","):
            url = part.strip()
            if url:
                candidates.append(url)
    # Common fallbacks for in-cluster vs. self calls
    candidates.extend([
        "http://n8n:5678",
        "http://localhost:5678",
        "http://127.0.0.1:5678",
    ])
    # Deduplicate while preserving order
    seen = set()
    unique: list[str] = []
    for u in candidates:
        u2 = u.rstrip("/")
        if u2 not in seen:
            seen.add(u2)
            unique.append(u2)
    return unique


def main() -> int:
    candidates = _candidate_base_urls()
    try:
        total_wait = int(
            (os.environ.get("N8N_BOOTSTRAP_WAIT_SECONDS") or "120").strip())
    except Exception:
        total_wait = 120
    deadline = time.time() + max(10, total_wait)
    base_url = ""
    settings: Dict[str, Any] = {}
    last_error: Exception | None = None
    while time.time() < deadline and not base_url:
        for candidate in candidates:
            if base_url or time.time() >= deadline:
                break
            remain = max(5, int(deadline - time.time()))
            per_try = min(12, remain)
            try:
                settings = _wait_for_ready(candidate.rstrip(
                    "/"), timeout_seconds=per_try, abort_on_dns_failure=True)
                base_url = candidate.rstrip("/")
                break
            except Exception as e:  # noqa: BLE001
                last_error = e
                continue
        if not base_url:
            time.sleep(1.0)
    if not base_url:
        print(f"n8n bootstrap: settings wait failed: {
              last_error}", file=sys.stderr)
        return 1

    # Read intended owner credentials from env (with safe defaults)
    owner_email = os.environ.get(
        "N8N_OWNER_EMAIL", "owner@example.com").strip()
    owner_password = os.environ.get("N8N_OWNER_PASSWORD", "owner").strip()
    owner_first = os.environ.get("N8N_OWNER_FIRST_NAME", "Owner").strip()
    owner_last = os.environ.get("N8N_OWNER_LAST_NAME", "User").strip()

    try:
        settings = _wait_for_ready(base_url, timeout_seconds=75.0)
    except Exception as e:  # noqa: BLE001
        print(f"n8n bootstrap: settings wait failed: {e}", file=sys.stderr)
        return 1

    # Unwrap settings payload shape used by newer n8n versions
    if isinstance(settings.get("data"), dict):
        settings = settings.get("data")
    # Detect if instance owner already set up (support legacy and new flags)
    um = settings.get("userManagement") or {}
    is_owner_setup = bool(um.get("isInstanceOwnerSetUp")) or bool(
        um.get("isInstanceOwnerInitialized"))
    # Newer versions expose a boolean to show the setup screen on first load
    # Treat showSetupOnFirstLoad == False as owner already set up
    if "showSetupOnFirstLoad" in um:
        try:
            is_owner_setup = is_owner_setup or (
                um.get("showSetupOnFirstLoad") is False)
        except Exception:  # noqa: BLE001
            pass
    if is_owner_setup:
        print("n8n bootstrap: owner already set up; nothing to do")
        return 0

    # Determine REST base prefix from settings when available (fallback to /rest)
    rest_prefix = "/" + str((settings.get("endpoint")
                            or settings.get("restEndpoint") or "rest").strip("/"))

    # Attempt initial setup
    payload = {
        "email": owner_email,
        "password": owner_password,
        "firstName": owner_first,
        "lastName": owner_last,
    }
    try:
        # Newer n8n versions expose owner setup at /owner/setup (skipAuth)
        status, body = _http_json_post(
            f"{base_url}{rest_prefix}/owner/setup", payload, timeout=15.0)
        if status in (200, 201):
            print(f"n8n bootstrap: owner created ({owner_email})")
            return 0
        # Some versions may respond 409/400 if already initialized between checks; treat as success
        if status in (400, 409):
            msg = (body or {}).get("message", "")
            if "already" in msg.lower() or "initialized" in msg.lower():
                print(
                    "n8n bootstrap: owner appears to be already initialized (race condition)")
                return 0
        # Try alternative endpoints used by some versions
        status2, body2 = _http_json_post(
            f"{base_url}{rest_prefix}/setup", payload, timeout=15.0)
        if status2 in (200, 201):
            print(f"n8n bootstrap: owner created via setup ({owner_email})")
            return 0
        if status2 in (400, 409):
            msg2 = (body2 or {}).get("message", "")
            if "already" in msg2.lower() or "initialized" in msg2.lower():
                print("n8n bootstrap: owner appears to be already initialized (setup)")
                return 0
        # Another legacy path
        status3, body3 = _http_json_post(
            f"{base_url}{rest_prefix}/user-management/setup", payload, timeout=15.0)
        if status3 in (200, 201):
            print(
                f"n8n bootstrap: owner created via user-management ({owner_email})")
            return 0
        if status3 in (400, 409):
            msg3 = (body3 or {}).get("message", "")
            if "already" in msg3.lower() or "initialized" in msg3.lower():
                print(
                    "n8n bootstrap: owner appears to be already initialized (user-management)")
                return 0
        print(f"n8n bootstrap: setup failed (HTTP {
              status}/{status2}/{status3}): {body or body2 or body3}", file=sys.stderr)
        return 1
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8")
        except Exception:  # noqa: BLE001
            detail = ""
        # Accept 'already initialized' as success; on 404 treat as success for versions without setup endpoint
        if e.code in (400, 409):
            # 404: endpoint not available in this version; re-check settings and treat as success if owner now set
            try:
                settings2 = _wait_for_ready(base_url, timeout_seconds=10.0)
                if isinstance(settings2.get("data"), dict):
                    settings2 = settings2.get("data")
                um2 = (settings2 or {}).get("userManagement") or {}
                post_init = bool(um2.get("isInstanceOwnerSetUp")) or bool(
                    um2.get("isInstanceOwnerInitialized"))
                if "showSetupOnFirstLoad" in um2:
                    try:
                        post_init = post_init or (
                            um2.get("showSetupOnFirstLoad") is False)
                    except Exception:  # noqa: BLE001
                        pass
                if post_init:
                    print(
                        "n8n bootstrap: owner appears to be initialized (post-setup check)")
                    return 0
            except Exception:
                pass
            if e.code in (400, 409) and ("already" in detail.lower() or "initialized" in detail.lower()):
                print(
                    "n8n bootstrap: owner appears to be already initialized (HTTP error)")
                return 0
        if e.code in (404, 401, 403):
            # Try alternative known setup endpoints before giving up
            candidates = [
                f"{rest_prefix}/owner/setup",
                f"{rest_prefix}/user-management/setup",
                f"{rest_prefix}/setup",
                f"{rest_prefix}/users",
                f"{rest_prefix}/user-management/users",
                "/owner/setup",
                "/user-management/setup",
                "/setup",
                "/users",
                "/user-management/users",
                "/api/v1/owner/setup",
                "/api/v1/setup",
                "/api/v1/user-management/setup",
            ]
            # Deduplicate while preserving order
            seen = set()
            paths = []
            for p in candidates:
                if p not in seen:
                    seen.add(p)
                    paths.append(p)
            for path in paths:
                try:
                    status_fallback, body_fallback = _http_json_post(
                        f"{base_url}{path}", payload, timeout=15.0)
                    if status_fallback in (200, 201):
                        print(f"n8n bootstrap: owner created via {
                              path} ({owner_email})")
                        return 0
                except urllib.error.HTTPError as e2:
                    try:
                        detail2 = e2.read().decode("utf-8")
                    except Exception:  # noqa: BLE001
                        detail2 = ""
                    if e2.code in (400, 409) and ("already" in detail2.lower() or "initialized" in detail2.lower()):
                        print(
                            "n8n bootstrap: owner appears to be already initialized (HTTP error via fallback)")
                        return 0
                    # 401/403 likely means endpoint requires auth (not usable for first-run); try next
                    if e2.code in (401, 403):
                        continue
                    if e2.code not in (404,):
                        print(f"n8n bootstrap: HTTP error {e2.code} on {
                              path}: {detail2}", file=sys.stderr)
                        return 1
                    # else continue to next path
                except Exception as e2:  # noqa: BLE001
                    print(f"n8n bootstrap: setup error via fallback {
                          path}: {e2}", file=sys.stderr)
                    return 1
            # Final check: if owner became initialized during attempts, accept success
            try:
                settings2 = _wait_for_ready(base_url, timeout_seconds=10.0)
                if isinstance(settings2.get("data"), dict):
                    settings2 = settings2.get("data")
                um2 = (settings2 or {}).get("userManagement") or {}
                post_init = bool(um2.get("isInstanceOwnerSetUp")) or bool(
                    um2.get("isInstanceOwnerInitialized"))
                if "showSetupOnFirstLoad" in um2:
                    try:
                        post_init = post_init or (
                            um2.get("showSetupOnFirstLoad") is False)
                    except Exception:  # noqa: BLE001
                        pass
                if post_init:
                    print(
                        "n8n bootstrap: owner appears to be initialized (post-fallback check)")
                    return 0
            except Exception:
                pass
            print(
                "n8n bootstrap: setup endpoints not found; owner not created", file=sys.stderr)
            return 1
        print(f"n8n bootstrap: HTTP error {e.code}: {detail}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"n8n bootstrap: setup error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
