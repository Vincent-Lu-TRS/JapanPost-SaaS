"""
Google OAuth 2.0 認證模組
限制：僅允許 @tkrjm.co.jp 網域或白名單人員登入
"""
import os
import hmac
import hashlib
import secrets
import time
import requests
import streamlit as st
from urllib.parse import urlencode

# ── 安全設定 ────────────────────────────────────────────
ALLOWED_DOMAIN = "tkrjm.co.jp"

# 手動白名單（非公司網域但需授權的外部帳號）
ALLOWED_WHITELIST: list[str] = [
    # "partner@example.com",  # 範例：加入外部合作夥伴
]

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

SCOPES = "openid email profile"


def _get_client_id() -> str:
    """從 Streamlit secrets 或環境變數取得 Client ID"""
    try:
        return st.secrets["GOOGLE_CLIENT_ID"]
    except Exception:
        return os.environ.get("GOOGLE_CLIENT_ID", "")


def _get_client_secret() -> str:
    """從 Streamlit secrets 或環境變數取得 Client Secret"""
    try:
        return st.secrets["GOOGLE_CLIENT_SECRET"]
    except Exception:
        return os.environ.get("GOOGLE_CLIENT_SECRET", "")


def _get_redirect_uri() -> str:
    """
    取得 OAuth 回呼 URI。
    優先使用 secrets 中的設定（生產環境），否則用 localhost（開發環境）。
    """
    try:
        return st.secrets["OAUTH_REDIRECT_URI"]
    except Exception:
        return os.environ.get("OAUTH_REDIRECT_URI", "http://localhost:8501/")


def is_authorized(email: str) -> bool:
    """驗證使用者電子郵件是否有登入權限"""
    if not email:
        return False
    email_lower = email.lower().strip()
    if email_lower.endswith(f"@{ALLOWED_DOMAIN}"):
        return True
    if email_lower in [w.lower() for w in ALLOWED_WHITELIST]:
        return True
    return False


def _generate_state() -> str:
    """
    產生自我驗證的 HMAC state token（無需伺服器端儲存）。
    格式：{timestamp}.{hmac_hex}
    """
    timestamp = str(int(time.time()))
    secret = (_get_client_secret() or "fallback-secret").encode()
    sig = hmac.new(secret, timestamp.encode(), hashlib.sha256).hexdigest()
    return f"{timestamp}.{sig}"


def _verify_state(state: str) -> bool:
    """
    驗證 HMAC state token。
    允許 10 分鐘內的 state（防止 CSRF，同時不依賴 session 儲存）。
    """
    if not state or "." not in state:
        return False
    try:
        timestamp_str, sig = state.rsplit(".", 1)
        ts = int(timestamp_str)
        # 時間視窗：10 分鐘
        if abs(int(time.time()) - ts) > 600:
            return False
        secret = (_get_client_secret() or "fallback-secret").encode()
        expected = hmac.new(secret, timestamp_str.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig)
    except Exception:
        return False


def get_login_url() -> tuple[str, str]:
    """
    產生 Google OAuth 授權 URL 及 state 防 CSRF 值。
    回傳 (auth_url, state)
    """
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


def exchange_code_for_token(code: str) -> dict:
    """使用 authorization code 換取 access_token"""
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
    """使用 access_token 取得使用者資訊"""
    resp = requests.get(
        GOOGLE_USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    if not resp.ok:
        raise RuntimeError(f"取得使用者資訊失敗: {resp.status_code}")
    return resp.json()


# ── Streamlit Session 工具 ──────────────────────────────
def init_auth_state():
    """初始化 auth 相關的 session_state"""
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


def handle_oauth_callback() -> bool:
    """
    檢查 URL 參數中是否有 OAuth callback code。
    若有則完成認證流程，回傳 True 表示處理成功（不論成功或失敗）。
    """
    params = st.query_params
    code = params.get("code")
    state = params.get("state")

    if not code:
        return False

    # CSRF 驗證（HMAC 自我驗證，不依賴 session_state）
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

        # 認證成功
        st.session_state.authenticated = True
        st.session_state.user_email = email
        st.session_state.user_name = user_info.get("name", email)
        st.session_state.user_picture = user_info.get("picture", "")
        st.session_state._auth_error = None
        st.query_params.clear()

    except Exception as e:
        st.session_state._auth_error = f"❌ 認證過程發生錯誤：{e}"
        st.query_params.clear()

    return True


def logout():
    """清除 session 狀態，執行登出"""
    for key in ["authenticated", "user_email", "user_name", "user_picture", "oauth_state"]:
        st.session_state[key] = None
    st.session_state.authenticated = False
