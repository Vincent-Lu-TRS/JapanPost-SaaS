"""
日本郵政自動化製單 SaaS 平台 - 主程式
Streamlit Web UI + Google OAuth（限 @tkrjm.co.jp）
支援：30 天 Cookie Session
"""
import os
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/tmp/ms-playwright")

import html
import subprocess
import sys
import time
import threading
import streamlit as st
import pandas as pd
from job_control import (
    BatchJobRegistry,
    filter_key_log_lines,
    mark_results_completed,
    mark_unfinished_orders,
    update_order_status_from_log,
)
from pending_editor import (
    SHIPPING_COL,
    SHIPPING_OPTIONS,
    apply_pending_order_editor_values,
    build_pending_item_frame,
    build_pending_summary_frame,
    has_zero_value_items,
)
from fx_rates import fetch_usd_jpy_rate

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
    has_native_auth_config,
    login_with_native_auth,
    render_login_link,
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
if "_JOB_REGISTRY" not in globals():
    _JOB_REGISTRY = BatchJobRegistry()


def _get_job(email: str) -> dict | None:
    return _JOB_REGISTRY.get(email)


def _load_pending_orders() -> tuple[pd.DataFrame, list[str]]:
    from bot.sheets import get_pending_orders

    pending_logs: list[str] = []
    df_pending = get_pending_orders(log_cb=pending_logs.append)
    return df_pending, pending_logs


@st.cache_data(ttl=3600, show_spinner=False)
def _load_usd_jpy_rate() -> tuple[float | None, str, str]:
    rate, rate_date, source = fetch_usd_jpy_rate()
    if not rate:
        print(f"[FX] USDJPY fetch failed: {source}", file=sys.stderr, flush=True)
    return rate, rate_date, source


def _zero_value_warning_lines(df: pd.DataFrame) -> list[str]:
    warnings: list[str] = []
    for _, row in df.iterrows():
        zero_items = has_zero_value_items(row)
        if zero_items:
            order_id = str(row.get("注文番号(貼上原始資料)", "")).strip()
            warnings.append(f"{order_id}: item {', '.join(str(i) for i in zero_items)}")
    return warnings


def _reset_key_for(order_id: str) -> str:
    return f"pending_reset_{order_id}"


def _reset_version(order_id: str) -> int:
    return int(st.session_state.get(_reset_key_for(order_id), 0))


def _reset_order_editor(order_id: str) -> None:
    st.session_state[_reset_key_for(order_id)] = _reset_version(order_id) + 1


def _reset_all_order_editors(df: pd.DataFrame) -> None:
    for _, row in df.iterrows():
        order_id = str(row.get("注文番号(貼上原始資料)", "")).strip()
        if order_id:
            _reset_order_editor(order_id)


def _apply_data_editor_state(frame: pd.DataFrame, widget_key: str) -> pd.DataFrame:
    edited = frame.copy()
    state = st.session_state.get(widget_key)
    if not isinstance(state, dict):
        return edited
    edited_rows = state.get("edited_rows") or {}
    if not isinstance(edited_rows, dict):
        return edited
    for row_index, updates in edited_rows.items():
        if not isinstance(updates, dict):
            continue
        try:
            index = int(row_index)
        except Exception:
            continue
        if index < 0 or index >= len(edited):
            continue
        for column, value in updates.items():
            if column in edited.columns:
                edited.at[edited.index[index], column] = value
    return edited


def _summary_cell(label: str, value: str) -> str:
    return (
        '<div class="summary-cell">'
        f'<div class="summary-label">{html.escape(label)}</div>'
        f'<div class="summary-value">{html.escape(str(value))}</div>'
        '</div>'
    )


def _start_job(email: str, df: pd.DataFrame, max_rows: int | None) -> tuple[bool, str]:
    ok, job, reason = _JOB_REGISTRY.start(email, df, max_rows)
    if not ok or job is None:
        return False, reason

    def _run():
        import traceback as tb

        def _log(msg: str):
            ts = time.strftime("%H:%M:%S")
            entry = f"[{ts}] {msg}"
            print(f"[BOT] {entry}", file=sys.stderr, flush=True)
            try:
                job["logs"].append(entry)
                update_order_status_from_log(job, msg)
            except Exception as log_err:
                print(f"[LOG_ERR] {log_err}", file=sys.stderr, flush=True)

        try:
            _log("🚀 任務啟動，正在載入模組...")
            from bot.automation import AUTOMATION_BUILD_ID, _prepare_batch_hs_codes, run_automation
            from bot.sheets import COUNTRY_CODE_MAP, backfill_results
            _log(f"🧭 automation build: {AUTOMATION_BUILD_ID}")

            rows_for_run = df if max_rows is None else df.head(max_rows)
            _log("🔎 正在預查本批 HS Code...")
            hs_codes_by_order = _prepare_batch_hs_codes(
                rows_for_run,
                COUNTRY_CODE_MAP,
                log_cb=_log,
            )
            job["hs_codes_by_order"] = hs_codes_by_order
            for order in job.get("orders") or []:
                codes = hs_codes_by_order.get(order.get("order_id", ""), {})
                if codes:
                    order["hs_codes"] = ", ".join(
                        f"{idx}:{code}" for idx, code in sorted(codes.items(), key=lambda pair: int(pair[0]))
                    )
            _log("✅ 模組載入成功，開始 Playwright 自動化...")
            results = run_automation(
                df,
                max_rows=max_rows,
                log_cb=_log,
                headless=True,
                precomputed_hs_codes=hs_codes_by_order,
            )
            job["results"] = results
            mark_results_completed(job, results)
            if results:
                _log(f"📋 正在回填 {len(results)} 筆至 Google Sheets...")
                backfill_results(results, log_cb=_log)
                _log(f"✅ 完成！共處理 {len(results)} 筆訂單。")
                job["pending_refresh_needed"] = True
            else:
                _log("ℹ️ 自動化完成，無新增結果。")
                mark_unfinished_orders(job, "skipped", "無新增結果", "自動化完成但沒有產生新結果")
            _JOB_REGISTRY.finish(job, "completed")
        except BaseException as e:
            err_text = tb.format_exc()
            print(f"[BOT_ERROR] {err_text}", file=sys.stderr, flush=True)
            try:
                _log(f"❌ 例外：{type(e).__name__}: {e}")
                _log(f"詳細：{err_text}")
            except Exception:
                pass
            try:
                mark_unfinished_orders(job, "failed", "發生例外", f"{type(e).__name__}: {e}")
                _JOB_REGISTRY.finish(job, "error")
            except Exception:
                pass

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return True, ""


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

        if has_native_auth_config():
            if st.button("🔑 使用 Google 帳號登入", type="primary"):
                login_with_native_auth()
        else:
            auth_url, state = get_login_url()
            st.session_state.oauth_state = state

            if "client_id=" in auth_url and "client_id=&" not in auth_url:
                st.warning("目前使用舊版 OAuth 入口；若要避免新分頁，請設定 Streamlit 原生 [auth]。")
                st.markdown(
                    render_login_link(auth_url),
                    unsafe_allow_html=True,
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

    st.markdown(
        """
        <style>
        .stApp {
            background:
                radial-gradient(circle at top left, rgba(180, 83, 9, 0.12), transparent 30rem),
                linear-gradient(135deg, #161514 0%, #0f1115 45%, #17120e 100%);
        }
        .block-container { padding-top: 3.25rem; max-width: 1320px; }
        h3 { color: #f8fafc; }
        p, label, .stMarkdown, [data-testid="stCaptionContainer"] { color: #cbd5e1; }
        div[data-testid="stMetric"] {
            border: 1px solid rgba(251, 146, 60, 0.22);
            border-radius: 10px;
            padding: 0.75rem 0.9rem;
            background: rgba(24, 24, 27, 0.82);
        }
        div[data-testid="stExpander"] {
            border-radius: 12px;
            border-color: rgba(251, 146, 60, 0.18);
            background: rgba(24, 24, 27, 0.72);
            overflow: hidden;
        }
        div[data-testid="stExpander"] details > summary {
            background: rgba(39, 39, 42, 0.82);
            min-height: 2.5rem;
        }
        div[data-testid="stDataFrame"] {
            border-radius: 10px;
            overflow: hidden;
        }
        .order-summary-row {
            display: grid;
            grid-template-columns: minmax(9rem, 1.1fr) minmax(11rem, 1.5fr) minmax(10rem, 1.5fr) minmax(7rem, .8fr) minmax(7rem, .8fr);
            gap: .5rem;
            margin: .15rem 0 .45rem 0;
        }
        .summary-cell {
            border: 1px solid rgba(148, 163, 184, 0.18);
            background: rgba(15, 23, 42, 0.68);
            border-radius: 8px;
            padding: .45rem .55rem;
        }
        .summary-label {
            color: #94a3b8;
            font-size: .72rem;
            line-height: 1.1;
        }
        .summary-value {
            color: #f8fafc;
            font-weight: 650;
            line-height: 1.35;
            white-space: normal;
            overflow-wrap: anywhere;
        }
        .stButton > button[kind="primary"] {
            background: #c2410c;
            border-color: #ea580c;
        }
        @media (max-width: 900px) {
            .order-summary-row { grid-template-columns: 1fr 1fr; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    job = _get_job(email)
    is_running = job is not None and job.get("status") == "running"

    df_pending = pd.DataFrame()
    pending_count = 0
    pending_logs: list[str] = []
    if is_running:
        df_pending = st.session_state.get("last_pending_df", pd.DataFrame())
        pending_logs = st.session_state.get("last_pending_logs", [])
        pending_count = len(df_pending)
    else:
        if job and job.pop("pending_refresh_needed", False):
            st.session_state.pop("last_pending_df", None)
            st.session_state.pop("last_pending_logs", None)
        with st.spinner("讀取 Google Sheets 待打單資料..."):
            try:
                df_pending, pending_logs = _load_pending_orders()
                pending_count = len(df_pending)
                st.session_state.last_pending_df = df_pending
                st.session_state.last_pending_logs = pending_logs
            except Exception as e:
                st.warning(f"無法讀取 Google Sheets：{e}")

    df_pending_for_run = df_pending
    main_col, side_col = st.columns([3, 1])

    with main_col:
        preview_title_col, preview_button_col = st.columns([5, 1])
        with preview_title_col:
            st.subheader("📊 待打單預覽")
        with preview_button_col:
            if not is_running and st.button("🔁 重新讀取待製單", width="stretch", key="reload_pending_top"):
                st.session_state.pop("last_pending_df", None)
                st.session_state.pop("last_pending_logs", None)
                st.rerun()
        if not df_pending.empty:
            rate, rate_date, rate_source = _load_usd_jpy_rate()
            if rate:
                st.caption(f"USD/JPY rate: {rate:.4f} ({rate_date})")
            else:
                st.warning(f"暫時無法取得 USD/JPY 匯率；若編輯 Value 或 Quantity，TotalValue(JPY) 會保留來源預設值。{rate_source}")

            if is_running:
                st.dataframe(build_pending_summary_frame(df_pending).head(20), hide_index=True, width="stretch")
                df_pending_for_run = df_pending
            else:
                editable_count = min(len(df_pending), 20)
                if st.button("恢復全部預設值", width="stretch", key="reset_all_pending"):
                    _reset_all_order_editors(df_pending.head(editable_count))
                    st.rerun()
                edited_summary_rows: list[dict[str, str]] = []
                edited_items_by_position: dict[int, pd.DataFrame] = {}
                for position in range(editable_count):
                    row = df_pending.iloc[position]
                    order_id = str(row.get("注文番号(貼上原始資料)", "")).strip() or f"row-{position + 1}"
                    name = str(row.get("Shipping Name", "")).strip()
                    country = str(row.get("收件人國家", row.get("Country", ""))).strip()
                    default_trans_type = str(row.get(SHIPPING_COL, "")).strip()
                    with st.expander(f"{order_id} | {name}", expanded=True):
                        reset_version = _reset_version(order_id)
                        trans_col, spacer_col, reset_col = st.columns([1.25, 3.75, 1])
                        with trans_col:
                            trans_type = st.selectbox(
                                "TransType",
                                options=SHIPPING_OPTIONS,
                                index=SHIPPING_OPTIONS.index(default_trans_type) if default_trans_type in SHIPPING_OPTIONS else 0,
                                key=f"pending_trans_{position}_{order_id}_{reset_version}",
                            )
                        with spacer_col:
                            st.write("")
                        with reset_col:
                            st.write("")
                            if st.button("恢復預設", key=f"reset_order_{position}_{order_id}"):
                                _reset_order_editor(order_id)
                                st.rerun()
                        edited_summary_rows.append(
                            {
                                "Order No.": order_id,
                                "Name": name,
                                "Country": country,
                                "TransType": trans_type,
                                "TotalValue(USD)": "",
                                "TotalValue(JPY)": "",
                            }
                        )
                        zero_items = has_zero_value_items(row)
                        if zero_items:
                            st.error(
                                "Value is 0 for "
                                + ", ".join(f"Content{i}" for i in zero_items)
                                + ". Please edit before starting."
                            )
                        item_frame = build_pending_item_frame(row)
                        item_key = f"pending_items_{position}_{order_id}_{reset_version}"
                        summary_item_frame = _apply_data_editor_state(item_frame, item_key)
                        preview_df = apply_pending_order_editor_values(
                            df_pending.iloc[[position]],
                            pd.DataFrame([edited_summary_rows[-1]]),
                            {0: summary_item_frame},
                            usd_jpy_rate=rate,
                        )
                        summary_row = build_pending_summary_frame(preview_df).iloc[0]
                        st.markdown(
                            '<div class="order-summary-row">'
                            + _summary_cell("Order No.", summary_row["Order No."])
                            + _summary_cell("Name", summary_row["Name"])
                            + _summary_cell("Country", summary_row["Country"])
                            + _summary_cell("TotalValue(USD)", summary_row["TotalValue(USD)"])
                            + _summary_cell("TotalValue(JPY)", summary_row["TotalValue(JPY)"])
                            + "</div>",
                            unsafe_allow_html=True,
                        )
                        edited_items_by_position[position] = st.data_editor(
                            item_frame,
                            hide_index=True,
                            width="stretch",
                            num_rows="fixed",
                            disabled=["Content", "HSCode"],
                            column_config={
                                "Content": st.column_config.TextColumn("Content", width="small"),
                                "Description": st.column_config.TextColumn("Description", width="large"),
                                "HSCode": st.column_config.TextColumn("HSCode", width="small"),
                                "Value": st.column_config.TextColumn("Value", width="small"),
                                "Quantity": st.column_config.TextColumn("Quantity", width="small"),
                            },
                            key=item_key,
                        )

                df_pending_for_run = apply_pending_order_editor_values(
                    df_pending,
                    pd.DataFrame(edited_summary_rows),
                    edited_items_by_position,
                    usd_jpy_rate=rate,
                )
                if len(df_pending) > editable_count:
                    st.caption(f"目前可編輯前 {editable_count} 筆；其餘訂單會保留來源表資料。")
            if pending_logs:
                with st.expander("🔎 待製單讀取診斷", expanded=False):
                    st.code("\n".join(pending_logs), language="text")
        elif pending_logs:
            st.info("目前沒有待製單資料。")
            with st.expander("🔎 待製單讀取診斷", expanded=True):
                st.code("\n".join(pending_logs), language="text")
        else:
            st.info("目前沒有待製單資料。")

        zero_value_warnings = _zero_value_warning_lines(df_pending_for_run)

        if job and job.get("orders"):
            st.subheader("🧾 製單狀態")
            status_label = {
                "queued": "待機中",
                "running": "製單中",
                "success": "完成",
                "failed": "需排查",
                "skipped": "略過",
            }
            df_status = pd.DataFrame(job["orders"])
            df_status["status"] = df_status["status"].map(status_label).fillna(df_status["status"])
            df_status = df_status.rename(columns={
                "position": "#",
                "order_id": "注文番号",
                "recipient": "收件人",
                "country": "國家",
                "status": "狀態",
                "stage": "階段",
                "tracking_no": "貨運單號",
                "hs_codes": "HSCode",
                "message": "訊息",
            })
            if "HSCode" not in df_status.columns:
                df_status["HSCode"] = ""
            show_cols = ["#", "注文番号", "收件人", "國家", "狀態", "階段", "貨運單號", "HSCode", "訊息"]
            st.dataframe(df_status[show_cols], hide_index=True, width="stretch")

        if job and job.get("results"):
            st.divider()
            st.subheader("✅ 本次製單結果")
            df_res = pd.DataFrame(job["results"])
            df_res = df_res.rename(columns={
                "name": "收件人",
                "order_id": "注文番号",
                "tracking": "貨運單號",
                "country_raw": "國家（原始）",
                "date": "日期",
            })
            st.dataframe(df_res, hide_index=True)

    with side_col:
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
            if st.button("🔄 重新整理", width="stretch"):
                st.rerun()
        else:
            btn_label = "🚀 開始自動製單" if pending_count > 0 else "✅ 無待處理訂單"
            if zero_value_warnings:
                st.error("有品項 Value 為 0，請先修正：" + "；".join(zero_value_warnings[:5]))
            if st.button(btn_label, type="primary",
                         disabled=(pending_count == 0 or bool(zero_value_warnings)), width="stretch"):
                if df_pending.empty:
                    st.warning("沒有符合條件的待打單資料")
                else:
                    ok, reason = _start_job(email, df_pending_for_run, max_rows_val)
                    if ok:
                        st.success("✅ 已啟動！")
                        time.sleep(0.8)
                        st.rerun()
                    else:
                        if reason == "batch_running":
                            st.error("同一批製單已在執行中，已阻止重複啟動。")
                        else:
                            st.error("任務執行中，請稍候")

        if job and job.get("status") in ("completed", "error"):
            st.divider()
            icon = "✅" if job["status"] == "completed" else "❌"
            st.markdown(f"**{icon} 上次：{job['status']}**")
            st.caption(f"啟動於 {job.get('started_at', '')}")
            if job.get("results"):
                st.caption(f"完成 {len(job['results'])} 筆")

    if job and job.get("logs"):
        st.divider()
        st.subheader("📄 執行日誌")
        log_lines = job["logs"]
        key_lines = filter_key_log_lines(log_lines)
        log_text = "\n".join(key_lines) if key_lines else "\n".join(log_lines[-20:])
        st.text_area(
            "執行日誌內容",
            value=log_text,
            height=220,
            disabled=True,
            key="log_area",
            label_visibility="hidden",
        )
        if len(key_lines) != len(log_lines):
            with st.expander("🔧 詳細除錯日誌"):
                st.code("\n".join(log_lines[-200:]), language="text")

    if is_running:
        time.sleep(2)
        st.rerun()


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
