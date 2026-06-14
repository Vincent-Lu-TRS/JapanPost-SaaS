"""
æ—¥æœ¬éƒµæ”¿è‡ªå‹•åŒ–è£½å–® SaaS å¹³å° - ä¸»ç¨‹å¼
Streamlit Web UI + Google OAuthï¼ˆé™ @tkrjm.co.jpï¼‰
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

# â”€â”€ Playwright ç’°å¢ƒåˆå§‹åŒ–ï¼ˆåƒ…åœ¨ç¬¬$¸€æ¬¡å•Ÿå‹•æ™‚åŸ·è¡Œï¼‰â”€â”€â”€â”€â”€â”€â”€â”€
@st.cache_resource(show_spinner="æ­£åœ¨å®‰è£ Playwright Chromium ç’°å¢ƒ...")
def _install_playwright():
    """åœ¨é›²ç«¯ç’°å¢ƒé¦–æ¬¡å•Ÿå‹•æ™‚å®‰è£ Playwright ç€è¦½å™¨"""
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


# â”€â”€ å…¨åŸŸä»»å‹™è¿½è¹¤å™¨ï¼ˆè·¨ Streamlit rerun ä¿æŒç‹€æ…‹ï¼‰â”€â”€â”€â”€â”€â”€
_JOBS: dict[str, dict] = {}


def _get_job(email: str) -> dict | None:
    return _JOBS.get(email)


def _start_job(email: str, df: pd.DataFrame, max_rows: int | None) -> bool:
    """åœ¨èƒŒæ™¯åŸ·è¡Œç·’å•Ÿå‹•è‡ªå‹•åŒ–ä»»å‹™"""
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
                _log(f"ðŸ“‹ æ­£åœ¨å›žå¡« {len(results)} ç­†è‡³ Google Sheets...")
                backfill_results(results, log_cb=_log)
            job["status"] = "completed"
        except Exception as e:
            job["logs"].append(f"[{time.strftime('%H:%M:%S')}] âŒ ç³»çµ±ä¾‹å¤–ï¼š{e}")
            job["status"] = "error"

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return True


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# é é¢æ¸²æŸ“å‡½æ•¸ï¼ˆå¿…é ˆåœ¨å‘¼å«å‰å®šç¾©ï¼‰
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def _render_login_page():
    """é¡¯ç¤º Google ç™»å…¥é é¢"""
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
        st.markdown("## ðŸ“® JP Post è‡ªå‹•è£½å–®å¹³å°")
        st.markdown("**ä¼æ¥­å°ˆå±¬ SaaSãƒ»å…å®‰è£ãƒ»é›²ç«¯å…¨è‡ªå‹•**")
        st.divider()
        st.markdown("è«‹ä½¿ç”¨å…¬å¸ Google å¸³è™Ÿç™»å…¥ï¼ˆ@tkrjm.co.jpï¼‰")

        # é¡¯ç¤ºä¸¦æ¸…é™¤ä¸Šæ¬¡çš„èªè­‰éŒ¯èª¤
        _auth_error = st.session_state.pop("_auth_error", None)
        if _auth_error:
            st.error(_auth_error)

        # ç”¢ç”Ÿ OAuth URL ä¸¦å„²å­˜ state
        auth_url, state = get_login_url()
        st.session_state.oauth_state = state

        st.markdown(
            f'<a href="{auth_url}" target="_top" style="'
            'display:inline-block;background:#fff;border:1px solid #dadce0;'
            'border-radius:4px;padding:10px 20px;font-size:1rem;font-weight:500;'
            'color:#3c4043;text-decoration:none;'
            '">ðŸ”‘ ä½¿ç”¨ Google å¸³è™Ÿç™»å…¥</a>',
            unsafe_allow_html=True,
        )
        st.caption("åƒ…é™å…¬å¸ @tkrjm.co.jp å¸³è™Ÿæˆ–å·²æŽˆæ¬Šäººå“¡")


def _render_main_app():
    """ä¸»æ‡‰ç”¨ä»‹é¢ï¼ˆç™»å…¥å¾Œé¡¯ç¤ºï¼‰"""
    email = st.session_state.get("user_email", "")
    name = st.session_state.get("user_name", email)
    picture = st.session_state.get("user_picture", "")

    # â”€â”€ é ‚éƒ¨å°Žè¦½åˆ— â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    col1, col2, col3 = st.columns([6, 2, 1])
    with col1:
        st.markdown("### ðŸ“® JP Post è‡ªå‹•è£½å–®å¹³å°")
    with col2:
        if picture:
            st.markdown(
                f'<img src="{picture}" width="28" style="border-radius:50%;'
                f'vertical-align:middle;margin-right:6px;">'
                f'<span style="font-size:0.9rem">{name}</span>',
                unsafe_allow_html=True,
            )
    with col3:
        if st.button("ç™»å‡º", type="secondary"):
            logout()
            st.rerun()

    st.divider()

    job = _get_job(email)
    is_running = job is not None and job.get("status") == "running"

    # â”€â”€ è®€å–å¾…æ‰“å–®æ¸…å–® â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    df_pending = pd.DataFrame()
    pending_count = 0
    with st.spinner("è®€å– Google Sheets å¾…æ‰“å–®è³‡æ–™..."):
        try:
            from bot.sheets import get_pending_orders
            df_pending = get_pending_orders()
            pending_count = len(df_pending)
        except Exception as e:
            st.warning(f"ç„¡æ³•è®€å– Google Sheetsï¼š{e}")

    # â”€â”€ é›™æ¬„ä½ˆå±€ â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    col_left, col_right = st.columns([1, 2])

    with col_left:
        st.subheader("ðŸ“‹ æ“ä½œé¢æ¿")

        # çµ±è¨ˆå¡ç‰‡
        m1, m2 = st.columns(2)
        with m1:
            st.metric("â³ å¾…è£½å–®", pending_count)
        with m2:
            done = len(job["results"]) if job else 0
            st.metric("âœ… æœ¬æ¬¡å®Œæˆ", done)

        st.divider()

        # åŸ·è¡Œè¨­å®š
        st.markdown("**åŸ·è¡Œè¨­å®š**")
        max_rows_input = st.number_input(
            "æœ€å¤šè™•ç†ç­†æ•¸ï¼ˆ0 = å…¨éƒ¨ï¼‰",
            min_value=0,
            max_value=500,
            value=10,
            step=1,
            disabled=is_running,
        )
        max_rows_val: int | None = None if max_rows_input == 0 else int(max_rows_input)

        # å•Ÿå‹• / åˆ·æ–°
        if is_running:
            st.info("ðŸ”„ è‡ªå‹•åŒ–é€²è¡Œä¸­...")
            if st.button("ðŸ”„ é‡æ–°æ•´ç†", use_container_width=True):
                st.rerun()
        else:
            btn_label = "ðŸš€ é–‹å§‹è‡ªå‹•è£½å–®" if pending_count > 0 else "âœ… ç„¡å¾…è™•ç†è¨‚å–®"
            if st.button(
                btn_label,
                type="primary",
                disabled=(pending_count == 0),
                use_container_width=True,
            ):
                if df_pending.empty:
                    st.warning("æ²’æœ‰ç¬¦åˆæ¢ä»¶çš„å¾…æ‰“å–®è³‡æ–™")
                else:
                    ok = _start_job(email, df_pending, max_rows_val)
                    if ok:
                        st.success("âœ… å·²å•Ÿå‹•ï¼")
                        time.sleep(0.8)
                        st.rerun()
                    else:
                        st.error("ä»»å‹™åŸ·è¡Œä¸­ï¼Œè«‹ç¨å€™")

        # ä¸Šæ¬¡ä»»å‹™ç‹€æ…‹
        if job and job.get("status") in ("completed", "error"):
            st.divider()
            icon = "âœ…" if job["status"] == "completed" else "âŒ"
            st.markdown(f"**{icon} ä¸Šæ¬¡ï¼š{job['status']}**")
            st.caption(f"å•Ÿå‹•æ–¼ {job.get('started_at', '')}")
            if job["results"]:
                st.caption(f"å®Œæˆ {len(job['results'])} ç­†")

    with col_right:
        # æ—¥èªŒé¢æ¿
        st.subheader("ðŸ“„ åŸ·è¡Œæ—¥èªŒ")
        log_lines = job["logs"] if job else []
        log_text = "\n".join(log_lines) if log_lines else "ï¼ˆå°šç„¡æ—¥èªŒï¼‰"
        st.text_area("", value=log_text, height=380, disabled=True, key="log_area")

        # ä»»å‹™é€²è¡Œä¸­ â†’ æ¯ 2 ç§’è‡ªå‹•åˆ·æ–°
        if is_running:
            time.sleep(2)
            st.rerun()

        # æœ¬æ¬¡çµæžœè¡¨æ ¼
        if job and job.get("results"):
            st.divider()
            st.subheader("âœ… æœ¬æ¬¡è£½å–®çµæžœ")
            df_res = pd.DataFrame(job["results"])
            df_res.columns = ["æ”¶ä»¶äºº", "æ³¨æ–‡ç•ªå·", "è²¨é‹å–®è™Ÿ", "åœ‹å®¶ï¼ˆåŽŸå§‹ï¼‰", "æ—¥æœŸ"]
            st.dataframe(df_res, use_container_width=True, hide_index=True)

        # å¾…æ‰“å–®é è¦½ï¼ˆå¯æŠ˜ç–Šï¼‰
        if not df_pending.empty:
            with st.expander(f"ðŸ“Š å¾…æ‰“å–®é è¦½ï¼ˆå…± {pending_count} ç­†ï¼Œé¡¯ç¤ºå‰ 10ï¼‰"):
                preview_cols = [
                    c for c in [
                        "æ³¨æ–‡ç•ªå·(è²¼ä¸ŠåŽŸå§‹è³‡æ–™)",
                        "Shipping Name",
                        "æ”¶ä»¶äººåœ‹å®¶",
                        "éƒµå±€é‹é€æ–¹å¼(è¤‡æ•¸å•†å“è«‹è‡ªè¡Œç¢ºèªæ˜¯å¦èµ°å°åŒ…)",
                        "éƒµå±€ç”³å‘Šé‡‘é¡(USD)",
                    ] if c in df_pending.columns
                ]
                if preview_cols:
                    st.dataframe(
                        df_pending[preview_cols].head(10),
                        use_container_width=True,
                        hide_index=True,
                    )


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# ä¸»ç¨‹å¼å…¥å£
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

st.set_page_config(
    page_title="JP Post è‡ªå‹•è£½å–®å¹³å°",
    page_icon="ðŸ“®",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# èƒŒæ™¯å®‰è£ Playwrightï¼ˆé›²ç«¯ç’°å¢ƒéœ€è¦ï¼‰
_install_playwright()

# åˆå§‹åŒ– Auth session state
init_auth_state()

# è™•ç† OAuth callbackï¼ˆGoogle å›žèª¿å¸¶ ?code=...ï¼‰
if handle_oauth_callback():
    st.rerun()

# è·¯ç”±ï¼šæœªç™»å…¥ â†’ ç™»å…¥é ï¼›å·²ç™»å…¥ â†’ ä¸»ä»‹é¢
if not st.session_state.get("authenticated"):
    _render_login_page()
else:
    _render_main_app()
