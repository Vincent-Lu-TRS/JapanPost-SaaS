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
SESSION_COOKIE_NAME = "jp_auth_v2"
OAUTH_STATE_COOKIE_NAME = "jp_oauth_state_v2"
DIAGNOSTICS_ADMIN = "admin@tkrjm.co.jp"

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
    if _st_user_get("email_verified", False) is not True or not is_authorized(email):
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
    st.session_state["_verified_auth_email"] = email.lower().strip()
    return True


def can_view_diagnostics() -> bool:
    """Authorize from the verified provider identity, never a display/widget email."""
    if has_native_auth_config():
        return (
            bool(_st_user_get("is_logged_in", False))
            and _st_user_get("email_verified", False) is True
            and str(_st_user_get("email", "")).strip().lower() == DIAGNOSTICS_ADMIN
        )
    verified_email = st.session_state.get("_verified_auth_email", "")
    return (
        st.session_state.get("authenticated") is True
        and verified_email == DIAGNOSTICS_ADMIN
        and str(st.session_state.get("user_email", "")).strip().lower() == verified_email
    )


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
    # Version the signed payload as well as the cookie name: pre-hardening
    # cookies did not require Google's email_verified claim.
    payload = f"v2|{email}|{expires}|{name}^{picture}"
    configured_secret = _get_client_secret()
    if not configured_secret:
        raise ValueError("legacy authentication secret is not configured")
    secret = configured_secret.encode()
    sig = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{b64}.{sig}"


def _parse_session_token(token: str) -> dict | None:
    """驗證並解碼 session token，回傳 user dict 或 None"""
    if not token or "." not in token:
        return None
    configured_secret = _get_client_secret()
    if not configured_secret or has_native_auth_config():
        return None
    try:
        b64, sig = token.rsplit(".", 1)
        padded = b64 + "=" * (4 - len(b64) % 4)
        payload = base64.urlsafe_b64decode(padded).decode()
        parts = payload.split("|", 3)
        if len(parts) != 4 or parts[0] != "v2":
            return None
        _, email, expires_str, name_picture = parts
        name_parts = name_picture.split("^", 1)
        name = name_parts[0]
        picture = name_parts[1] if len(name_parts) > 1 else ""
        secret = configured_secret.encode()
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
        cookie_mgr.set(
            SESSION_COOKIE_NAME, token, expires_at=expires,
            key="auth_session_set", same_site="lax",
            secure=_get_redirect_uri().startswith("https://"),
        )
        print(f"[AUTH] Cookie saved for {email[:4]}***", file=sys.stderr)
    except Exception as e:
        print(f"[AUTH] Cookie save failed: {e}", file=sys.stderr)


def _clear_cookie(cookie_mgr):
    if cookie_mgr is None:
        return
    try:
        cookie_mgr.delete(SESSION_COOKIE_NAME, key="auth_session_delete")
    except Exception as e:
        print(f"[AUTH] Cookie clear failed: {e}", file=sys.stderr)


def _restore_from_cookie(cookie_mgr) -> bool:
    """嘗試從 cookie 恢復 session，成功回傳 True"""
    if has_native_auth_config() or st.session_state.get("_legacy_logged_out"):
        return False
    try:
        token = _request_cookie(SESSION_COOKIE_NAME)
        if not token and cookie_mgr is not None:
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
        st.session_state["_verified_auth_email"] = user["email"].lower().strip()
        print(f"[AUTH] Session restored from cookie: {user['email'][:4]}***", file=sys.stderr)
        return True
    except Exception as e:
        print(f"[AUTH] Cookie restore failed: {e}", file=sys.stderr)
        return False


# ── CSRF 防護 ─────────────────────────────────────────────
def _request_cookie(name: str):
    """Read the request's cookies before the JS CookieManager hydrates."""
    try:
        return st.context.cookies.get(name)
    except Exception:
        return None


def _generate_state() -> str:
    configured_secret = _get_client_secret()
    if not configured_secret:
        return ""
    timestamp = str(int(time.time()))
    payload = f"{timestamp}.{secrets.token_urlsafe(32)}"
    sig = hmac.new(configured_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _verify_state(state: str, cookie_mgr=None) -> bool:
    if not state or "." not in state:
        return False
    try:
        state_hash = hashlib.sha256(state.encode()).hexdigest()
        consumed = st.session_state.get("_consumed_oauth_states", set())
        if state_hash in consumed:
            return False
        expected_states = [
            st.session_state.pop("oauth_state", None),
            _request_cookie(OAUTH_STATE_COOKIE_NAME),
            cookie_mgr.get(OAUTH_STATE_COOKIE_NAME) if cookie_mgr is not None else None,
        ]
        if cookie_mgr is not None:
            try:
                cookie_mgr.delete(OAUTH_STATE_COOKIE_NAME, key="oauth_state_consume")
            except Exception:
                # The component can schedule deletion before its local cookie
                # dictionary hydrates, then raise KeyError. Validation uses
                # the server request cookie and must not lose that callback.
                pass
        # Same-browser cookie survives the full-page OAuth redirect; session is
        # useful when the callback does not reconnect the Streamlit websocket.
        if not any(expected and hmac.compare_digest(state, str(expected)) for expected in expected_states):
            return False
        payload, sig = state.rsplit(".", 1)
        timestamp_str, nonce = payload.split(".", 1)
        if not nonce:
            return False
        ts = int(timestamp_str)
        if not 0 <= int(time.time()) - ts <= 600:
            return False
        configured_secret = _get_client_secret()
        if not configured_secret:
            return False
        expected = hmac.new(configured_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return False
        # st.context.cookies is a websocket snapshot, so deleting a browser
        # cookie alone does not prevent replay in this Streamlit session.
        st.session_state["_consumed_oauth_states"] = set(consumed) | {state_hash}
        return True
    except Exception:
        return False


# ── OAuth 主流程 ──────────────────────────────────────────
def get_login_url(cookie_mgr=None) -> tuple[str, str]:
    state = _generate_state()
    if not state or not _get_client_id():
        return "", ""
    st.session_state["oauth_state"] = state
    if cookie_mgr is not None:
        from datetime import datetime, timedelta
        cookie_mgr.set(
            OAUTH_STATE_COOKIE_NAME, state,
            expires_at=datetime.now() + timedelta(minutes=10),
            key="oauth_state_set", same_site="lax",
            secure=_get_redirect_uri().startswith("https://"),
        )
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
        raise RuntimeError(f"Token exchange 失敗: {resp.status_code}")
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

    if has_native_auth_config():
        st.session_state.authenticated = False
        st.session_state.pop("_verified_auth_email", None)
        restore_native_auth_state()
        return

    verified_email = st.session_state.get("_verified_auth_email")
    if (
        st.session_state.get("authenticated") is True
        and verified_email
        and verified_email == str(st.session_state.get("user_email", "")).strip().lower()
        and is_authorized(verified_email)
    ):
        return
    st.session_state.authenticated = False
    st.session_state.pop("_verified_auth_email", None)
    _restore_from_cookie(cookie_mgr)


def handle_oauth_callback(cookie_mgr=None) -> bool:
    """
    處理 OAuth callback（URL 中的 code 參數）。
    認證成功後存入 30 天 cookie。
    """
    if has_native_auth_config():
        return False
    params = st.query_params
    code = params.get("code")
    state = params.get("state")

    if not code:
        return False

    if not _verify_state(state or "", cookie_mgr):
        st.session_state._auth_error = "⚠️ 安全驗證失敗（state invalid），請重新登入。"
        st.query_params.clear()
        return True

    try:
        token_data = exchange_code_for_token(code)
        access_token = token_data.get("access_token")
        if not access_token:
            st.session_state._auth_error = "❌ 無法取得登入憑證，請重新登入。"
            st.query_params.clear()
            return True

        user_info = get_user_info(access_token)
        email = user_info.get("email", "")

        if user_info.get("email_verified") is not True or not is_authorized(email):
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
        st.session_state["_verified_auth_email"] = email.lower().strip()
        st.session_state.pop("_legacy_logged_out", None)
        st.session_state._auth_error = None
        st.query_params.clear()

        _save_cookie(cookie_mgr, email, name, picture)

    except Exception:
        st.session_state._auth_error = "❌ 認證過程發生錯誤，請重新登入或聯絡系統管理員。"
        st.query_params.clear()

    return True


def logout(cookie_mgr=None):
    """清除 session 及 cookie，執行登出"""
    _clear_cookie(cookie_mgr)
    # Request cookies are a websocket snapshot. A deleted browser cookie can
    # still be present there until reconnect, so do not restore after logout.
    st.session_state["_legacy_logged_out"] = True
    for key in ["authenticated", "user_email", "user_name", "user_picture", "oauth_state"]:
        st.session_state[key] = None
    st.session_state.authenticated = False
    st.session_state.pop("_verified_auth_email", None)
    if cookie_mgr is not None:
        try:
            cookie_mgr.delete(OAUTH_STATE_COOKIE_NAME, key="oauth_state_logout")
        except Exception:
            pass
    if bool(_st_user_get("is_logged_in", False)):
        try:
            st.logout()
        except Exception as e:
            print(f"[AUTH] Native logout failed: {e}", file=sys.stderr)
