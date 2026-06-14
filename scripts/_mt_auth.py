#!/usr/bin/env python3
"""Shared auth + config for the memory-toast-make-card skill.

Credential store: ``~/.memory-toast/credentials.json`` (chmod 600), holding the
rotating 7-day refresh token — never the password. Imported by ``mt_login.py``
and ``upload_pack.py``. Zero third-party deps (Python 3.9+ stdlib only).
"""

import json
import os
import stat
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_API_URL = "https://memory-toast-api.smallseven-87b.workers.dev"
CONFIG_DIR = Path.home() / ".memory-toast"
CRED_PATH = CONFIG_DIR / "credentials.json"
# Cloudflare blocks the default Python-urllib User-Agent (error 1010).
USER_AGENT = "memory-toast-make-card/1.0"


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def load_credentials() -> dict:
    """Return the stored credentials dict, or {} if not logged in."""
    if not CRED_PATH.is_file():
        return {}
    try:
        return json.loads(CRED_PATH.read_text())
    except json.JSONDecodeError:
        fail(f"{CRED_PATH} is corrupted — run: python3 scripts/mt_login.py logout, then log in again")


def save_credentials(data: dict) -> None:
    """Write the credential store, owner read/write only (0600)."""
    CONFIG_DIR.mkdir(mode=0o700, exist_ok=True)
    CRED_PATH.write_text(json.dumps(data, indent=2))
    os.chmod(CRED_PATH, stat.S_IRUSR | stat.S_IWUSR)


def clear_credentials() -> bool:
    """Delete the credential store. Returns True if a file was removed."""
    if CRED_PATH.is_file():
        CRED_PATH.unlink()
        return True
    return False


def resolve_api_url(cli_arg: str = None) -> str:
    """Resolve the API base URL: --api flag > env > stored > built-in default."""
    creds = load_credentials()
    url = (cli_arg or os.environ.get("MEMORY_TOAST_API_URL")
           or creds.get("apiUrl") or DEFAULT_API_URL)
    return url.rstrip("/")


def api_call(method, url, body=None, token=None, raw=None, timeout=300):
    """Minimal JSON/binary HTTP call against the Memory Toast API.

    Returns (status_code, parsed_json_or_dict). Never raises on HTTP errors.
    """
    headers = {"User-Agent": USER_AGENT}
    data = None
    if raw is not None:
        data = raw
        headers["Content-Type"] = "application/zip"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode() or "{}"
            return resp.status, (json.loads(text) if text.strip().startswith("{") else {})
    except urllib.error.HTTPError as e:
        text = e.read().decode()
        try:
            return e.code, json.loads(text)
        except json.JSONDecodeError:
            return e.code, {"raw": text}


def login(api: str, email: str, password: str) -> dict:
    """POST /auth/login. Returns the parsed response {user, accessToken, refreshToken}."""
    status, res = api_call("POST", f"{api}/api/v1/auth/login",
                           {"email": email, "password": password})
    if status != 200:
        msg = res.get("message") or res.get("error") or res
        fail(f"login failed ({status}): {msg}")
    if "refreshToken" not in res:
        fail(f"login response missing refreshToken: {res}")
    return res


def get_access_token(api: str) -> str:
    """Mint a fresh access token from the stored refresh token.

    Persists the rotated refresh token (slides the 7-day idle window forward).
    Exits with a friendly message if not logged in or the session has expired.
    """
    creds = load_credentials()
    refresh = creds.get("refreshToken")
    if not refresh:
        fail("not logged in — run: python3 scripts/mt_login.py")
    status, res = api_call("POST", f"{api}/api/v1/auth/refresh",
                           {"refreshToken": refresh})
    if status == 401:
        fail("session expired (refresh token older than 7 days or invalid) — "
             "run: python3 scripts/mt_login.py")
    if status != 200 or "accessToken" not in res:
        msg = res.get("message") or res.get("error") or res
        fail(f"token refresh failed ({status}): {msg}")
    # Persist the rotated refresh token; keep the stored apiUrl untouched so a
    # one-off --api override does not clobber the user's configured server.
    if res.get("refreshToken"):
        creds["refreshToken"] = res["refreshToken"]
        email = res.get("user", {}).get("email")
        if email:
            creds["email"] = email
        save_credentials(creds)
    return res["accessToken"]


def store_refresh_token(api: str, refresh_token: str) -> str:
    """Validate a pasted refresh token via /auth/refresh and store it.

    For Google/Facebook (social-login) users who have no password: they copy this
    token from the app (Settings → Copy upload token) and paste it here. Returns
    the account email on success; exits with a friendly message otherwise.
    """
    status, res = api_call("POST", f"{api}/api/v1/auth/refresh",
                           {"refreshToken": refresh_token})
    if status == 401:
        fail("that token is invalid or expired — copy a fresh one from the app "
             "(Settings → Copy upload token)")
    if status != 200 or "refreshToken" not in res:
        msg = res.get("message") or res.get("error") or res
        fail(f"token validation failed ({status}): {msg}")
    email = res.get("user", {}).get("email", "(unknown)")
    save_credentials({
        "apiUrl": api,
        "email": email,
        "refreshToken": res["refreshToken"],  # store the freshly-rotated token
    })
    return email
