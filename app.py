"""
日本郵政自動化製單 SaaS 平台 - 主程式
Streamlit Web UI + Google OAuth（限 @tkrjm.co.jp）
支援：30 天 Cookie Session
"""
import os
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/tmp/ms-playwright")

import subprocess
import sys
import time
import threading
import streamlit as st
import pandas as pd

# ══════════════════════════════════════════════════════
# ★ set_page_config 必須在所有 st.* 呼叫之前
# ══════════════════════════════════════════════════════
st.set_page_config(
    page_title="JP Post 自動製單平台",
    page_icon="📮",
    layout="wide",
    initial_sidebar_state="collapsed",
)

from auth import (
    init_auth_state,
    handle_oauth_callback,
    get_login_url,
    logout,
    get_cookie_manager,
)

# ── Cookie Manager（必須在其他 UI 之前初始化）──────────
_cm = get_cookie_manager()


# ── Playwright 環境初始化（僅在第一次啟動時執行）────────
@st.cache_resource(show_spinner="正在安裝 Playwright Chromium 環境...")
def _install_playwright():
    """不加 --with-deps：系統相依套件已由 packages.txt 在建置時安裝。"""
    _env = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": "/tmp/ms-playwright"}
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True, text=True, timeout=300, env=_env,
        )
        print(f"[PLAYWRIGHT_INSTALL] returncode={result.returncode}", file=sys.stderr)
        if result.stdout:
            print(f"[PLAYWRIGHT_INSTALL stdout] {result.stdout[:500]}", file=sys.stderr)
        if result.stderr:
            print(f"[PLAYWRIGHT_INSTALL stderr] {result.stderr[:500]}", file=sys.stderr)
        return result.returncode == 0
    except Exception as e:
        print(f"[PLAYWRIGHT_INSTALL ERROR] {e}", file=sys.stderr)
        return False


# ── 全域任務追蹤器 ──────────────────────────────────────
if "_JOBS" not in globals():
    _JOBS: dict = {}


def _get_job(email: str) -> dict | None:
    return _JOBS.get(email)


def _start_job(email: str, df: pd.DataFrame, max_rows: int | None) -> bool:
    if email in _JOBS and _JOBS[email].get("status") == "running":
        return False

    job: dict = {
        "status": "running",
        "logs": [],
        "results": [],
        "started_at": time.strftime("%H:%M:%S"),
    }
    _JOBS[email] = job

    def _run():
        import traceback as tb

        def _log(msg: str):
            ts = time.strftime("%H:%M:%S")
            entry = f"[{ts}] {msg}"
            print(f"[BOT] {entry}", file=sys.stderr, flush=True)
            try:
                job["logs"].append(entry)
            except Exception as log_err:
                print(f"[LOG_ERR] {log_err}", file=sys.stderr, flush=True)

        try:
            _log("🚀 任務啟動，正在載入模組...")
            from bot.automation import run_automation
            from bot.sheets import backfill_results

            _log("✅ 模組載入成功，開始 Playwright 自動化...")
            results = run_automation(df, max_rows=max_rows, log_cb=_log, headless=True)
            job["results"] = results
            if results:
                _log(f"📋 正在回填 {len(results)} 筆至 Google Sheets...")
                backfill_results(results, log_cb=_log)
                _log(f"✅ 完成！共處理 {len(results)} 筆訂單。")
            else:
                _log("ℹ️ 自動化完成，無新增結果。")
            job["status"] = "completed"
        except BaseException as e:
            err_text = tb.format_exc()
            print(f"[BOT_ERROR] {err_text}", file=sys.stderr, flush=True)
            try:
                _log(f"❌ 例外：{type(e).__name__}: {e}")
                _log(f"詳細：{err_text}")
            except Exception:
                pass
            try:
                job["status"] = "error"
            except Exception:
                pass

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return True


# ══════════════════════════════════════════════════════
# 頁面渲染函數
# ══════════════════════════════════════════════════════

def _render_login_page():
    st.markdown(
        """
        <style>
        .block-container { padding-top: 4rem; }
        .google-login-btn {
            display: inline-block;
            padding: 0.55rem 1.4rem;
            background: #4285F4;
            color: white !important;
            text-decoration: none !important;
            border-radius: 6px;
            font-size: 1rem;
            font-weight: 500;
            cursor: pointer;
        }
        .google-login-btn:hover { background: #357ae8 !important; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    col_l, col_c, col_r = st.columns([1, 2, 1])
    with col_c:
        st.markdown("## 📮 JP Post 自動製單平台")
        st.markdown("**企業專屬 SaaS・免安裝・雲端全自動**")
        st.divider()
        st.markdown("請使用公司 Google 帳號登入（@tkrjm.co.jp）")

        _auth_error = st.session_state.pop("_auth_error", None)
        if _auth_error:
            st.error(_auth_error)

        auth_url, state = get_login_url()
        st.session_state.oauth_state = state

        if "client_id=" in auth_url and "client_id=&" not in auth_url:
            st.link_button(
                "🔑 使用 Google 帳號登入",
                auth_url,
                type="primary",
                use_container_width=False,
            )
        else:
            st.error("⚠️ GOOGLE_CLIENT_ID 未設定！請至 Streamlit Cloud Secrets 添加。")
        st.caption("僅限公司 @tkrjm.co.jp 帳號或已授權人員")


def _render_main_app():
    email = st.session_state.get("user_email", "")
    name = st.session_state.get("user_name", email)
    picture = st.session_state.get("user_picture", "")

    col1, col2, col3 = st.columns([6, 2, 1])
    with col1:
        st.markdown("### 📮 JP Post 自動製單平台")
    with col2:
        if picture:
            st.markdown(
                f'<img src="{picture}" width="28" style="border-radius:50%;'
                f'vertical-align:middle;margin-right:6px;">'
                f'<span style="font-size:0.9rem">{name}</span>',
                unsafe_allow_html=True,
            )
    with col3:
        if st.button("登出", type="secondary"):
            logout(_cm)
            st.rerun()

    st.divider()

    job = _get_job(email)
    is_running = job is not None and job.get("status") == "running"

    df_pending = pd.DataFrame()
    pending_count = 0
    with st.spinner("讀取 Google Sheets 待打單資料..."):
        try:
            from bot.sheets import get_pending_orders
            df_pending = get_pending_orders()
            pending_count = len(df_pending)
        except Exception as e:
            st.warning(f"無法讀取 Google Sheets：{e}")

    col_left, col_right = st.columns([1, 2])

    with col_left:
        st.subheader("📋 操作面板")

        m1, m2 = st.columns(2)
        with m1:
            st.metric("⏳ 待製單", pending_count)
        with m2:
            done = len(job["results"]) if job else 0
            st.metric("✅ 本次完成", done)

        st.divider()
        st.markdown("**執行設定**")
        max_rows_input = st.number_input(
            "最多處理筆數（0 = 全部）",
            min_value=0, max_value=500, value=10, step=1,
            disabled=is_running,
        )
        max_rows_val: int | None = None if max_rows_input == 0 else int(max_rows_input)

        if is_running:
            st.info("🔄 自動化進行中...")
            if st.button("🔄 重新整理", use_container_width=True):
                st.rerun()
        else:
            btn_label = "🚀 開始自動製單" if pending_count > 0 else "✅ 無待處理訂單"
            if st.button(btn_label, type="primary",
                         disabled=(pending_count == 0), use_container_width=True):
                if df_pending.empty:
                    st.warning("沒有符合條件的待打單資料")
                else:
                    ok = _start_job(email, df_pending, max_rows_val)
                    if ok:
                        st.success("✅ 已啟動！")
                        time.sleep(0.8)
                        st.rerun()
                    else:
                        st.error("任務執行中，請稍候")

        if job and job.get("status") in ("completed", "error"):
            st.divider()
            icon = "✅" if job["status"] == "completed" else "❌"
            st.markdown(f"**{icon} 上次：{job['status']}**")
            st.caption(f"啟動於 {job.get('started_at', '')}")
            if job.get("results"):
                st.caption(f"完成 {len(job['results'])} 筆")

    with col_right:
        st.subheader("📄 執行日誌")
        log_lines = job["logs"] if job else []
        log_text = "\n".join(log_lines) if log_lines else "（尚無日誌）"
        st.text_area("執行日誌內容", value=log_text, height=380,
                     disabled=True, key="log_area", label_visibility="hidden")

        if is_running:
            time.sleep(2)
            st.rerun()

        if job and job.get("results"):
            st.divider()
            st.subheader("✅ 本次製單結果")
            df_res = pd.DataFrame(job["results"])
            df_res.columns = ["收件人", "注文番号", "貨運單號", "國家（原始）", "日期"]
            st.dataframe(df_res, hide_index=True)

        if not df_pending.empty:
            with st.expander(f"📊 待打單預覽（共 {pending_count} 筆，顯示前 10）"):
                preview_cols = [c for c in [
                    "注文番号(貼上原始資料)", "Shipping Name", "收件人國家",
                    "郵局運送方式(複數商品請自行確認是否走小包)", "郵局申告金額(USD)",
                ] if c in df_pending.columns]
                if preview_cols:
                    st.dataframe(df_pending[preview_cols].head(10), hide_index=True)


# ══════════════════════════════════════════════════════
# 主程式入口
# ══════════════════════════════════════════════════════

_install_playwright()
init_auth_state(_cm)

if handle_oauth_callback(_cm):
    st.rerun()
elif st.session_state.get("authenticated"):
    _render_main_app()
else:
    _render_login_page()
