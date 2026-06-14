"""
日本郵政自動化製單 SaaS 平台 - 主程式
Streamlit Web UI + Google OAuth（限 @tkrjm.co.jp）
"""
import subprocess
import sys
import time
import threading
import streamlit as st
import pandas as pd

from auth import (
    init_auth_state,
    handle_oauth_callback,
    get_login_url,
    logout,
)

# ── Playwright 安裝管理（只在需要時在雲端安裝）────────────
@st.cache_resource(show_spinner="正在安裝 Playwright Chromium 瀏覽器...")
def _install_playwright():
    """在雲端環境首次啟動時安裝 Playwright 瀏覽器"""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium", "--with-deps"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        return result.returncode == 0
    except Exception:
        return False


# ── 全局任務狀態（避免 Streamlit rerun 重置中斷）──────
_JOBS: dict[str, dict] = {}


def _get_job(email: str) -> dict | None:
    return _JOBS.get(email)


def _start_job(email: str, df: pd.DataFrame, max_rows: int | None) -> bool:
    """在背景執行緒啟動自動化任務"""
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
        from bot.automation import run_automation
        from bot.sheets import backfill_results

        def _log(msg: str):
            job["logs"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")

        try:
            results = run_automation(df, max_rows=max_rows, log_cb=_log, headless=True)
            job["results"] = results
            if results:
                _log(f"📋 正在回寫 {len(results)} 筆到 Google Sheets...")
                backfill_results(results, log_cb=_log)
            job["status"] = "completed"
        except Exception as e:
            job["logs"].append(f"[{time.strftime('%H:%M:%S')}] ❌ 發生錯誤：{e}")
            job["status"] = "error"

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return True


# ╔══════════════════════════════════════════════════════╗
# ║  頁面渲染函式（根據登入狀態切換不同介面）            ║
# ╚══════════════════════════════════════════════════════╝

def _render_login_page():
    """渲染 Google 登入頁面"""
    st.markdown(
        """
        <style>
        .block-container { padding-top: 4rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    col_l, col_c, col_r = st.columns([1, 2, 1])
    with col_c:
        st.markdown("## 📦 JP Post 自動化製單平台")
        st.markdown("**企業版 SaaS 系統 - 快速製單自動化**")
        st.divider()
        st.markdown("請使用公司 Google 帳號登入（@tkrjm.co.jp）")

        # 生成 OAuth URL 並儲存 state
        auth_url, state = get_login_url()
        st.session_state.oauth_state = state

        st.markdown(
            f'<a href="{auth_url}" target="_self" style="'
            'display:inline-block;background:#fff;border:1px solid #dadce0;'
            'border-radius:4px;padding:10px 20px;font-size:1rem;font-weight:500;'
            'color:#3c4043;text-decoration:none;'
            '">🔑 使用 Google 帳號登入</a>',
            unsafe_allow_html=True,
        )
        st.caption("僅限 @tkrjm.co.jp 帳號或白名單人員")


def _render_main_app():
    """主應用介面（登入後渲染）"""
    email = st.session_state.get("user_email", "")
    name = st.session_state.get("user_name", email)
    picture = st.session_state.get("user_picture", "")

    # ── 頂部用戶資訊列 ─────────────────────────────────
    col1, col2, col3 = st.columns([6, 2, 1])
    with col1:
        st.markdown("### 📦 JP Post 自動化製單平台")
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
            logout()
            st.rerun()

    st.divider()

    job = _get_job(email)
    is_running = job is not None and job.get("status") == "running"

    # ── 讀取待處理訂單數量 ─────────────────────────
    df_pending = pd.DataFrame()
    pending_count = 0
    with st.spinner("讀取 Google Sheets 待處理訂單中..."):
        try:
            from bot.sheets import get_pending_orders
            df_pending = get_pending_orders()
            pending_count = len(df_pending)
        except Exception as e:
            st.warning(f"無法讀取 Google Sheets：{e}")

    # ── 主要操作區 ───────────────────────────────────
    col_left, col_right = st.columns([1, 2])

    with col_left:
        st.subheader("📊 任務控制面板")

        # 數字指標
        m1, m2 = st.columns(2)
        with m1:
            st.metric("⏳ 待處理訂單", pending_count)
        with m2:
            done = len(job["results"]) if job else 0
            st.metric("✅ 完成製單數", done)

        st.divider()

        # 執行控制
        st.markdown("**執行控制**")
        max_rows_input = st.number_input(
            "最多處理幾筆（0 = 全部）",
            min_value=0,
            max_value=500,
            value=10,
            step=1,
            disabled=is_running,
        )
        max_rows_val: int | None = None if max_rows_input == 0 else int(max_rows_input)

        # 啟動 / 刷新狀態
        if is_running:
            st.info("🔄 自動化任務執行中...")
            if st.button("🔄 更新狀態", use_container_width=True):
                st.rerun()
        else:
            btn_label = "🚀 開始自動化製單" if pending_count > 0 else "✅ 暫無待處理訂單"
            if st.button(
                btn_label,
                type="primary",
                disabled=(pending_count == 0),
                use_container_width=True,
            ):
                if df_pending.empty:
                    st.warning("無法讀取到待處理的訂單資料")
                else:
                    ok = _start_job(email, df_pending, max_rows_val)
                    if ok:
                        st.success("✅ 已啟動！")
                        time.sleep(0.8)
                        st.rerun()
                    else:
                        st.error("任務已在執行中，請稍候")

        # 上次結果摘要
        if job and job.get("status") in ("completed", "error"):
            st.divider()
            icon = "✅" if job["status"] == "completed" else "❌"
            st.markdown(f"**{icon} 上次：{job['status']}**")
            st.caption(f"啟動於 {job.get('started_at', '')}")
            if job["results"]:
                st.caption(f"完成 {len(job['results'])} 筆")

    with col_right:
        # 即時執行日誌
        st.subheader("📄 即時執行日誌")
        log_lines = job["logs"] if job else []
        log_text = "\n".join(log_lines) if log_lines else "（尚無執行日誌）"
        st.text_area("", value=log_text, height=380, disabled=True, key="log_area")

        # 任務執行中 → 每 2 秒自動刷新
        if is_running:
            time.sleep(2)
            st.rerun()

        # 完成結果預覽
        if job and job.get("results"):
            st.divider()
            st.subheader("✅ 完成訂單預覽")
            df_res = pd.DataFrame(job["results"])
            df_res.columns = ["訂單編號", "收件姓名", "追蹤單號", "狀態（原始）", "時間戳"]
            st.dataframe(df_res, use_container_width=True, hide_index=True)

        # 待處理訂單預覽（可折疊）
        if not df_pending.empty:
            with st.expander(f"📋 待處理訂單預覽（共 {pending_count} 筆，只顯示 10）"):
                preview_cols = [
                    c for c in [
                        "收件姓名(收件人所在地各種語言的收件人名稱)",
                        "Shipping Name",
                        "訂單編號狀態",
                        "商品內容品名(以收件國適當語言填寫收件姓名是否確認一致)",
                        "商品內容申告價格(USD)",
                    ] if c in df_pending.columns
                ]
                if preview_cols:
                    st.dataframe(
                        df_pending[preview_cols].head(10),
                        use_container_width=True,
                        hide_index=True,
                    )


# ╔══════════════════════════════════════════════════════╗
# ║  主程式入口                                          ║
# ╚══════════════════════════════════════════════════════╝

st.set_page_config(
    page_title="JP Post 自動化製單平台",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# 預裝 Playwright（首次啟動在雲端安裝瀏覽器）
_install_playwright()

# 初始化 Auth session state
init_auth_state()

# 處理 OAuth callback（Google 回傳 ?code=...）
if handle_oauth_callback():
    st.rerun()

# 路由：未登入 → 登入頁；已登入 → 主介面
if not st.session_state.get("authenticated"):
    _render_login_page()
else:
    _render_main_app()
