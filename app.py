"""
日本郵政自動化製單 SaaS 平台 - 主程式
Streamlit Web UI + Google OAuth（限 @tkrjm.co.jp）
"""
import subprocess
import sys
import time
import threading
import streamlit as st
import streamlit.components.v1 as components
import pandas as pd

from auth import (
    init_auth_state,
    handle_oauth_callback,
    get_login_url,
    logout,
)

# ── Playwright 環境初始化（僅在第一次啟動時執行）────────
@st.cache_resource(show_spinner="正在安裝 Playwright Chromium 環境...")
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


# ── 全域任務追蹤器（跨 Streamlit rerun 保持狀態）──────
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
                _log(f"📋 正在回填 {len(results)} 筆至 Google Sheets...")
                backfill_results(results, log_cb=_log)
            job["status"] = "completed"
        except Exception as e:
            job["logs"].append(f"[{time.strftime('%H:%M:%S')}] ❌ 系統例外：{e}")
            job["status"] = "error"

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return True


# ══════════════════════════════════════════════════════
# 頁面渲染函數（必須在呼叫前定義）
# ══════════════════════════════════════════════════════

def _render_login_page():
    """顯示 Google 登入頁面"""
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
        st.markdown("## 📮 JP Post 自動製單平台")
        st.markdown("**企業專屬 SaaS・免安裝・雲端全自動**")
        st.divider()
        st.markdown("請使用公司 Google 帳號登入（@tkrjm.co.jp）")

        # 顯示並清除上次的認證錯誤
        _auth_error = st.session_state.pop("_auth_error", None)
        if _auth_error:
            st.error(_auth_error)

        # 產生 OAuth URL 並儲存 state
        auth_url, state = get_login_url()
        st.session_state.oauth_state = state

        # 用 components.html 在子 iframe 中執行 JS 導航，
        # 繞過 Streamlit React 攔截點擊事件的問題
        components.html(
            f"""
            <style>
              body {{ margin: 0; padding: 0; }}
              button {{
                background: #fff;
                border: 1px solid #dadce0;
                border-radius: 4px;
                padding: 10px 24px;
                font-size: 1rem;
                font-weight: 500;
                color: #3c4043;
                cursor: pointer;
                font-family: sans-serif;
              }}
              button:hover {{ background: #f8f9fa; border-color: #c6cacd; }}
            </style>
            <button onclick="window.top.location.href = '{auth_url}'">
              🔑 使用 Google 帳號登入
            </button>
            """,
            height=60,
        )
        st.caption("僅限公司 @tkrjm.co.jp 帳號或已授權人員")


def _render_main_app():
    """主應用介面（登入後顯示）"""
    email = st.session_state.get("user_email", "")
    name = st.session_state.get("user_name", email)
    picture = st.session_state.get("user_picture", "")

    # ── 頂部導覽列 ──────────────────────────────────
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
            logout()
            st.rerun()

    st.divider()

    job = _get_job(email)
    is_running = job is not None and job.get("status") == "running"

    # ── 讀取待打單清單 ──────────────────────────────
    df_pending = pd.DataFrame()
    pending_count = 0
    with st.spinner("讀取 Google Sheets 待打單資料..."):
        try:
            from bot.sheets import get_pending_orders
            df_pending = get_pending_orders()
            pending_count = len(df_pending)
        except Exception as e:
            st.warning(f"無法讀取 Google Sheets：{e}")

    # ── 雙欄佈局 ────────────────────────────────────
    col_left, col_right = st.columns([1, 2])

    with col_left:
        st.subheader("📋 操作面板")

        # 統計卡片
        m1, m2 = st.columns(2)
        with m1:
            st.metric("⏳ 待製單", pending_count)
        with m2:
            done = len(job["results"]) if job else 0
            st.metric("✅ 本次完成", done)

        st.divider()

        # 執行設定
        st.markdown("**執行設定**")
        max_rows_input = st.number_input(
            "最多處理筆數（0 = 全部）",
            min_value=0,
            max_value=500,
            value=10,
            step=1,
            disabled=is_running,
        )
        max_rows_val: int | None = None if max_rows_input == 0 else int(max_rows_input)

        # 啟動 / 刷新按鈕
        if is_running:
            st.info("🔄 自動化進行中...")
            if st.button("🔄 重新整理", use_container_width=True):
                st.rerun()
        else:
            btn_label = "🚀 開始自動製單" if pending_count > 0 else "✅ 無待處理訂單"
            if st.button(
                btn_label,
                type="primary",
                disabled=(pending_count == 0),
                use_container_width=True,
            ):
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

        # 上次任務狀態
        if job and job.get("status") in ("completed", "error"):
            st.divider()
            icon = "✅" if job["status"] == "completed" else "❌"
            st.markdown(f"**{icon} 上次：{job['status']}**")
            st.caption(f"啟動於 {job.get('started_at', '')}")
            if job["results"]:
                st.caption(f"完成 {len(job['results'])} 筆")

    with col_right:
        # 日誌面板
        st.subheader("📄 執行日誌")
        log_lines = job["logs"] if job else []
        log_text = "\n".join(log_lines) if log_lines else "（尚無日誌）"
        st.text_area("", value=log_text, height=380, disabled=True, key="log_area")

        # 任務進行中 → 每 2 秒自動刷新
        if is_running:
            time.sleep(2)
            st.rerun()

        # 本次結果表格
        if job and job.get("results"):
            st.divider()
            st.subheader("✅ 本次製單結果")
            df_res = pd.DataFrame(job["results"])
            df_res.columns = ["收件人", "注文番号", "貨運單號", "國家（原始）", "日期"]
            st.dataframe(df_res, use_container_width=True, hide_index=True)

        # 待打單預覽（可折疊）
        if not df_pending.empty:
            with st.expander(f"📊 待打單預覽（共 {pending_count} 筆，顯示前 10）"):
                preview_cols = [
                    c for c in [
                        "注文番号(貼上原始資料)",
                        "Shipping Name",
                        "收件人國家",
                        "郵局運送方式(複數商品請自行確認是否走小包)",
                        "郵局申告金額(USD)",
                    ] if c in df_pending.columns
                ]
                if preview_cols:
                    st.dataframe(
                        df_pending[preview_cols].head(10),
                        use_container_width=True,
                        hide_index=True,
                    )


# ══════════════════════════════════════════════════════
# 主程式入口
# ══════════════════════════════════════════════════════

st.set_page_config(
    page_title="JP Post 自動製單平台",
    page_icon="📮",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# 背景安裝 Playwright（雲端環境需要）
_install_playwright()

# 初始化 Auth session state
init_auth_state()

# 處理 OAuth callback（Google 回調帶 ?code=...）
if handle_oauth_callback():
    st.rerun()

# 路由：未登入 → 登入頁；已登入 → 主介面
if not st.session_state.get("authenticated"):
    _render_login_page()
else:
    _render_main_app()
