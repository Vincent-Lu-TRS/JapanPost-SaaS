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

@st.cache_resource(show_spinner="正在安裝 Playwright Chromium 環境...")
def _install_playwright():
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium", "--with-deps"],
            capture_output=True, text=True, timeout=300,
        )
        return result.returncode == 0
    except Exception:
        return False


_JOBS: dict[str, dict] = {}


def _get_job(email: str) -> dict | None:
    return _JOBS.get(email)


def _start_job(email: str, df: pd.DataFrame, max_rows: int | None) -> bool:
    if email in _JOBS and _JOBS[email].get("status") == "running":
        return False
    job: dict = {"status": "running", "logs": [], "results": [], "started_at": time.strftime("%H:%M:%S")}
    _JOBS[email] = job
    def _run():
        from bot.automation import run_automation
        from bot.sheets import backfill_results
        def _log(msg): job["logs"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        try:
            results = run_automation(df, max_rows=max_rows, log_cb=_log, headless=True)
            job["results"] = results
            if results:
                _log(f"📋 当在回填 {len(results)} 筆 Google Sheets...")
                backfill_results(results, log_cb=_log)
            job["status"] = "completed"
        except Exception as e:
            job["logs"].append(f"[{time.strftime('%H:%M:%S')}] ❌ {e}")
            job["status"] = "error"
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return True


def _render_login_page():
    st.markdown('<style>.block-container{padding-top:4rem}</style>', unsafe_allow_html=True)
    _, col_c, _ = st.columns([1, 2, 1])
    with col_c:
        st.markdown("## 📮 JP Post 自動製單平台")
        st.markdown("**企業專屬 SaaS免安裝雲端全自動**")
        st.divider()
        st.markdown("被使用公司 Google 帳號登入 (@tkrjm.co.jp)")
        auth_url, state = get_login_url()
        st.session_state.oauth_state = state
        st.markdown(f'<a href="{auth_url}" target="_self" style="display:inline-block;background:#fff;border:1px solid #dadce0;border-radius:4px;padding:10px 20px;font-size:1rem;font-weight:500;color:#3c4043;text-decoration:none;">🔔 Google 帳號登入</a>', unsafe_allow_html=True)
        st.caption("@tkrjm.co.jp or approved whitelist only")


def _render_main_app():
    email = st.session_state.get("user_email", "")
    name = st.session_state.get("user_name", email)
    picture = st.session_state.get("user_picture", "")
    col1, col2, col3 = st.columns([6, 2, 1])
    with col1: st.markdown("### 📮 JP Post 自動製單平台")
    with col2:
        if picture: st.markdown(f'<img src="{picture}" width="28" style="border-radius:50%;vertical-align:middle;margin-right:6px;"><span style="font-size:0.9rem">{name}</span>', unsafe_allow_html=True)
    with col3:
        if st.button("登出", type="secondary"):
            logout(); st.rerun()
    st.divider()
    job = _get_job(email)
    is_running = job is not None and job.get("status") == "running"
    df_pending = pd.DataFrame()
    pending_count = 0
    with st.spinner("讀取 Google Sheets..."):
        try:
            from bot.sheets import get_pending_orders
            df_pending = get_pending_orders()
            pending_count = len(df_pending)
        except Exception as e: st.warning(f"Google Sheets error: {e}")
    col_left, col_right = st.columns([1, 2])
    with col_left:
        st.subheader("📋 交文娙")
        m1, m2 = st.columns(2)
        with m1: st.metric("待製單", pending_count)
        with m2: st.metric("本次完成", len(job["results"]) if job else 0)
        st.divider()
        max_rows_val = st.number_input("最多處理（0 = 全部）", min_value=0, max_value=500, value=10, disabled=is_running)
        max_rows = None if max_rows_val == 0 else int(max_rows_val)
        if is_running:
            st.info("🔄 進行中跳過...")
            if st.button("🔄 随界結比", use_container_width=True): st.rerun()
        else:
            btn_label = "🚀 開始自動製單" if pending_count > 0 else "✅ 待處理訂單"
            if st.button(btn_label, type="primary", disabled=(pending_count==0), use_container_width=True):
                if not df_pending.empty:
                    ok = _start_job(email, df_pending, max_rows)
                    if ok: st.success("✅ 已啟動！"); time.sleep(0.8); st.rerun()
                    else: st.error("任務喨回中")
        if job and job.get("status") in ("completed", "error"):
            st.divider()
            icon = "✅" if job["status"] == "completed" else "❌"
            st.markdown(f"**{icon} {job['status']}**")
    with col_right:
        st.subheader("📄 �ߡ�日誌")
        log_text = "\n".join(job["logs"]) if job else "(⺒版斜啬在说）"
        st.text_area("", value=log_text, height=380, disabled=True, key="log_area")
        if is_running: time.sleep(2); st.rerun()
        if job and job.get("results"):
            st.divider()
            df_res = pd.DataFrame(job["results"])
            st.dataframe(df_res, use_container_width=True, hide_index=True)


st.set_page_config(page_title="JP Post Automation", page_icon="📮", layout="wide", initial_sidebar_state="collapsed")
_install_playwright()
init_auth_state()
if handle_oauth_callback(): st.rerun()
if not st.session_state.get("authenticated"):
    _render_login_page()
else:
    _render_main_app()
