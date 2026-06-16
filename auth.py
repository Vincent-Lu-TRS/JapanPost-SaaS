"""
Google OAuth 2.0 認證模組
限制：僅允許 @tkrjm.co.jp 網域或白名單人員登入
支援：30 天 Cookie Session（免重新登入）
"""
import os
import sys
import hmac
import hashlib
import secrets
import time
import base64
from html import escape
import requests
import streamlit as st
from urllib.parse import urlencode

# ── 劉全設定 ────────────────────────────────────────────
ALLOWED_DOMAIN = "tkrjm.co.jp"
SESSION_DURATION_DAYS = 30
SESSION_COOKIE_NAME = "jp_auth_v1"

# 手動白名單（非公司網域但需授權的外部帳號）
ALLOWED_WHITELIST: list[str] = [
    # "partner@example.com",
]

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

SCOPES = "openid email profile"


def _get_client_id() -> str:
    try:
        return st.secrets["GOOGLE_CLIENT_ID"]
    except Exception:
        return os.environ.get("GOOGLE_CLIENT_ID", "")


def _get_client_secret() -> str:
    try:
        return st.secrets["GOOGLE_CLIENT_SECRET"]
    except Exception:
        return os.environ.get("GOOGLE_CLIENT_SECRET", "")


def _get_redirect_uri() -> str:
    try:
        return st.secrets["OAUTH_REDIRECT_URI"]
    except Exception:
        pass
    try:
        host = st.context.headers.get("host", "")
        if host and "localhost" not in host and "127.0.0.1" not in host:
            return f"https://{host}/"
    except Exception:
        pass
    return os.environ.get("OAUTH_REDIRECT_URI", "http://localhost:8501/")


def is_authorized(email: str) -> bool:
    if not email:
        return False
    email_lower = email.lower().strip()
    if email_lower.endswith(f"@{ALLOWED_DOMAIN}"):
        return True
    if email_lower in [w.lower() for w in ALLOWED_WHITELIST]:
        return True
    return False


def _st_user_get(key: str, default=""):
    user = getattr(st, "user", None)
    if user is None:
        return default
    if isinstance(user, dict):
        return user.get(key, default)
    return getattr(user, key, default)


def has_native_auth_config() -> bool:
    """Return True when Streamlit's native OIDC auth is configured."""
    try:
        auth_cfg = st.secrets.get("auth", {})
    except Exception:
        return False
    required = (
        "redirect_uri",
        "cookie_secret",
        "client_id",
        "client_secret",
        "server_metadata_url",
    )
    return all(str(auth_cfg.get(k, "")).strip() for k in required)


def restore_native_auth_state() -> bool:
    """Mirror st.user into the existing app session_state contract."""
    if not bool(_st_user_get("is_logged_in", False)):
        return False

    email = str(_st_user_get("email", "")).strip()
    if not is_authorized(email):
        st.session_state._auth_error = (
            f"🚫 帳號 {email} 不在授權名單中。"
            f"請使用 @{ALLOWED_DOMAIN} 的公司帳號登入，或聯絡系統管理員。"
        )
        try:
            st.logout()
        except Exception:
            pass
        return False

    st.session_state.authenticated = True
    st.session_state.user_email = email
    st.session_state.user_name = _st_user_get("name", email) or email
    st.session_state.user_picture = _st_user_get("picture", "") or ""
    return True


def login_with_native_auth():
    """Start Streamlit native OIDC login."""
    st.login()


# ── Cookie Session 工具 ──────────────────────────────────
def get_cookie_manager():
    """
    回傳 CookieManager 實例。
    必須在每次 Streamlit rerun 開始時（其他 UI 之前）呼叫。
    若套件不相容，回傳 None（不影響基本登入功能）。
    """
    try:
        import extra_streamlit_components as stx
        return stx.CookieManager(key="jp_auth_cookie_manager")
    except Exception as e:
        print(f"[AUTH] CookieManager init failed: {e}", file=sys.stderr)
        return None


def _make_session_token(email: str, name: str, picture: str) -> str:
    """建立 HMAC 簽名的 session token"""
    expires = int(time.time()) + SESSION_DURATION_DAYS * 86400
    payload = f"{email}|{expires}|{name}^{picture}"
    secret = (_get_client_secret() or "jp-saas-fallback-secret").encode()
    sig = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{b64}.{sig}"


def _parse_session_token(token: str) -> dict | None:
    """驗證並解碼 session token，回傳 user dict 或 None"""
    if not token or "." not in token:
        return None
    try:
        b64, sig = token.rsplit(".", 1)
        padded = b64 + "=" * (4 - len(b64) % 4)
        payload = base64.urlsafe_b64decode(padded).decode()
        parts = payload.split("|", 2)
        if len(parts) != 3:
            return None
        email, expires_str, name_picture = parts
        name_parts = name_picture.split("^", 1)
        name = name_parts[0]
        picture = name_parts[1] if len(name_parts) > 1 else ""
        secret = (_get_client_secret() or "jp-saas-fallback-secret").encode()
        expected = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return None
        if int(time.time()) > int(expires_str):
            return None
        return {"email": email, "name": name, "picture": picture}
    except Exception as e:
        print(f"[AUTH] Token parse error: {e}", file=sys.stderr)
        return None


def _save_cookie(cookie_mgr, email: str, name: str, picture: str):
    """將認證 token 存入瀏覽器 cookie（30 天）"""
    if cookie_mgr is None:
        return
    try:
        from datetime import datetime, timedelta
        token = _make_session_token(email, name, picture)
        expires = datetime.now() + timedelta(days=SESSION_DURATION_DAYS)
        cookie_mgr.set(SESSION_COOKIE_NAME, token, expires_at=expires)
        print(f"[AUTH] Cookie saved for {email[:4]}***", file=sys.stderr)
    except Exception as e:
        print(f"[AUTH] Cookie save failed: {e}", file=sys.stderr)


def _clear_cookie(cookie_mgr):
    if cookie_mgr is None:
        return
    try:
        cookie_mgr.delete(SESSION_COOKIE_NAME)
    except Exception as e:
        print(f"[AUTH] Cookie clear failed: {e}", file=sys.stderr)


def _restore_from_cookie(cookie_mgr) -> bool:
    """嘗試從 cookie 恢復 session，成功回傳 True"""
    if cookie_mgr is None:
        return False
    try:
        token = cookie_mgr.get(SESSION_COOKIE_NAME)
        if not token:
            return False
        user = _parse_session_token(token)
        if not user:
            return False
        if not is_authorized(user["email"]):
            _clear_cookie(cookie_mgr)
            return False
        st.session_state.authenticated = True
        st.session_state.user_email = user["email"]
        st.session_state.user_name = user["name"]
        st.session_state.user_picture = user["picture"]
        print(f"[AUTH] Session restored from cookie: {user['email'][:4]}***", file=sys.stderr)
        return True
    except Exception as e:
        print(f"[AUTH] Cookie restore failed: {e}", file=sys.stderr)
        return False


# ── CSRF 防護 ─────────────────────────────────────────────
def _generate_state() -> str:
    timestamp = str(int(time.time()))
    secret = (_get_client_secret() or "fallback-secret").encode()
    sig = hmac.new(secret, timestamp.encode(), hashlib.sha256).hexdigest()
    return f"{timestamp}.{sig}"


def _verify_state(state: str) -> bool:
    if not state or "." not in state:
        return False
    try:
        timestamp_str, sig = state.rsplit(".", 1)
        ts = int(timestamp_str)
        if abs(int(time.time()) - ts) > 600:
            return False
        secret = (_get_client_secret() or "fallback-secret").encode()
        expected = hmac.new(secret, timestamp_str.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig)
    except Exception:
        return False


# ── OAuth 主流程 ──────────────────────────────────────────
def get_login_url() -> tuple[str, str]:
    state = _generate_state()
    params = {
        "client_id": _get_client_id(),
        "redirect_uri": _get_redirect_uri(),
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "access_type": "offline",
        "prompt": "select_account",
    }
    url = GOOGLE_AUTH_URL + "?" + urlencode(params)
    return url, state


def render_login_link(auth_url: str) -> str:
    """Render a same-tab Google OAuth link for Streamlit markdown."""
    safe_url = escape(auth_url, quote=True)
    return (
        '<a class="google-login-btn" href="'
        f'{safe_url}'
        '" target="_top">'
        '🔑 使用 Google 帳號登入</a>'
    )


def exchange_code_for_token(code: str) -> dict:
    resp = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "client_id": _get_client_id(),
            "client_secret": _get_client_secret(),
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": _get_redirect_uri(),
        },
        timeout=15,
    )
    if not resp.ok:
        raise RuntimeError(f"Token exchange 失敗: {resp.status_code} {resp.text}")
    return resp.json()


def get_user_info(access_token: str) -> dict:
    resp = requests.get(
        GOOGLE_USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    if not resp.ok:
        raise RuntimeError(f"取得使用者資訊失敗: {resp.status_code}")
    return resp.json()


# ── Streamlit Session 工具 ──────────────────────────────
def init_auth_state(cookie_mgr=None):
    """初始化 auth session_state，並嘗試從 cookie 恢復 30 天 session。"""
    defaults = {
        "authenticated": False,
        "user_email": None,
        "user_name": None,
        "user_picture": None,
        "oauth_state": None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

    if st.session_state.get("authenticated"):
        return

    if restore_native_auth_state():
        return

    _restore_from_cookie(cookie_mgr)


def handle_oauth_callback(cookie_mgr=None) -> bool:
    """
    處理 OAuth callback（URL 中的 code 參數）。
    認證成功後存入 30 天 cookie。
    """
    params = st.query_params
    code = params.get("code")
    state = params.get("state")

    if not code:
        return False

    if not _verify_state(state or ""):
        st.session_state._auth_error = "⚠️ 安全驗證失敗（state invalid），請重新登入。"
        st.query_params.clear()
        return True

    try:
        token_data = exchange_code_for_token(code)
        access_token = token_data.get("access_token")
        if not access_token:
            st.session_state._auth_error = f"❌ 無法取得 access token: {token_data}"
            st.query_params.clear()
            return True

        user_info = get_user_info(access_token)
        email = user_info.get("email", "")

        if not is_authorized(email):
            st.session_state._auth_error = (
                f"🚫 帳號 {email} 不在授權名單中。"
                f"請使用 @{ALLOWED_DOMAIN} 的公司帳號登入，或聯絡系統管理員。"
            )
            st.query_params.clear()
            return True

        name = user_info.get("name", email)
        picture = user_info.get("picture", "")
        st.session_state.authenticated = True
        st.session_state.user_email = email
        st.session_state.user_name = name
        st.session_state.user_picture = picture
        st.session_state._auth_error = None
        st.query_params.clear()

        _save_cookie(cookie_mgr, email, name, picture)

    except Exception as e:
        st.session_state._auth_error = f"❌ 認證過程發生錯誤：{e}"
        st.query_params.clear()

    return True


def logout(cookie_mgr=None):
    """清除 session 及 cookie，執行登出"""
    _clear_cookie(cookie_mgr)
    for key in ["authenticated", "user_email", "user_name", "user_picture", "oauth_state"]:
        st.session_state[key] = None
    st.session_state.authenticated = False
    if bool(_st_user_get("is_logged_in", False)):
        try:
            st.logout()
        except Exception as e:
            print(f"[AUTH] Native logout failed: {e}", file=sys.stderr)
