"""
日本郵政自動化製單 SaaS 平台 - 主程式
Streamlit Web UI + Google OAuth（限 @tkrjm.co.jp）
支援：30 天 Cookie Session
"""
import os
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/tmp/ms-playwright")

import html
from datetime import datetime
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
    compose_shipping_name,
    country_kind,
    expand_pending_orders_for_trans_types,
    has_zero_value_items,
    parse_shipping_name,
    sanitize_hscode,
)
from fx_rates import fetch_usd_jpy_rate

# ══════════════════════════════════════════════════════
# ★ set_page_config 必須在所有 st.* 呼叫之前
# ══════════════════════════════════════════════════════
st.set_page_config(
    page_title="JP Post 製單系統",
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


def _name_key_for(position: int, order_id: str, reset_version: int) -> str:
    return f"pending_name_{position}_{order_id}_{reset_version}"


def _prc_id_key_for(position: int, order_id: str, reset_version: int) -> str:
    return f"pending_prc_id_{position}_{order_id}_{reset_version}"


def _pccc_key_for(position: int, order_id: str, reset_version: int) -> str:
    return f"pending_pccc_{position}_{order_id}_{reset_version}"


def _selected_key_for(position: int, order_id: str, reset_version: int) -> str:
    return f"pending_selected_{position}_{order_id}_{reset_version}"


def _extra_trans_key_for(position: int, order_id: str, reset_version: int) -> str:
    return f"pending_extra_trans_{position}_{order_id}_{reset_version}"


def _sync_recipient_id_session_fields(name_key: str, prc_id_key: str, pccc_key: str) -> None:
    current_name = st.session_state.get(name_key)
    if not isinstance(current_name, str):
        return
    parsed = parse_shipping_name(current_name)
    if parsed["clean_name"] == current_name or not (parsed["prc_id"] or parsed["pccc"]):
        return
    st.session_state[name_key] = parsed["clean_name"]
    if parsed["prc_id"] and not st.session_state.get(prc_id_key):
        st.session_state[prc_id_key] = parsed["prc_id"]
    if parsed["pccc"] and not st.session_state.get(pccc_key):
        st.session_state[pccc_key] = parsed["pccc"]


def _required_id_warning_lines(df: pd.DataFrame) -> list[str]:
    warnings: list[str] = []
    for _, row in df.iterrows():
        order_id = str(row.get("注文番号(貼上原始資料)", "")).strip()
        country = str(row.get("收件人國家", row.get("Country", ""))).strip()
        parsed = parse_shipping_name(row.get("Shipping Name", row.get("Shipping Name_1", "")))
        kind = country_kind(country)
        if kind == "china" and not parsed["prc_id"]:
            warnings.append(f"{order_id}: 中國訂單需填入 PRC ID 才能製單")
        elif kind == "korea" and not parsed["pccc"]:
            warnings.append(f"{order_id}: 韓國訂單需填入 PCCC 才能製單")
    return warnings


def _format_short_rate(rate: float | None, rate_date: str) -> str:
    rate_text = f"{rate:.2f}" if rate else "N/A"
    date_text = ""
    if rate_date:
        try:
            date_text = datetime.strptime(rate_date, "%Y-%m-%d").strftime("%y/%m/%d")
        except Exception:
            date_text = str(rate_date)
    return f"USD/JPY {rate_text}" + (f"｜{date_text}" if date_text else "")


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
                if column == "HSCode":
                    value = sanitize_hscode(value)
                edited.at[edited.index[index], column] = value
    return edited


def _build_pending_run_frame_from_state(
    df_pending: pd.DataFrame,
    editable_count: int,
    usd_jpy_rate: float | None,
) -> pd.DataFrame:
    edited_summary_rows: list[dict[str, str]] = []
    edited_items_by_position: dict[int, pd.DataFrame] = {}
    for position in range(editable_count):
        row = df_pending.iloc[position]
        order_id = str(row.get("注文番号(貼上原始資料)", "")).strip() or f"row-{position + 1}"
        country = str(row.get("收件人國家", row.get("Country", ""))).strip()
        parsed_name = parse_shipping_name(row.get("Shipping Name", row.get("Shipping Name_1", "")))
        default_trans_type = str(row.get(SHIPPING_COL, "")).strip()
        reset_version = _reset_version(order_id)
        item_frame = build_pending_item_frame(row)
        item_key = f"pending_items_{position}_{order_id}_{reset_version}"
        trans_key = f"pending_trans_{position}_{order_id}_{reset_version}"
        name_key = _name_key_for(position, order_id, reset_version)
        prc_id_key = _prc_id_key_for(position, order_id, reset_version)
        pccc_key = _pccc_key_for(position, order_id, reset_version)
        _sync_recipient_id_session_fields(name_key, prc_id_key, pccc_key)
        edited_name = st.session_state.get(name_key, parsed_name["clean_name"])
        edited_prc_id = st.session_state.get(prc_id_key, parsed_name["prc_id"])
        edited_pccc = st.session_state.get(pccc_key, parsed_name["pccc"])
        shipping_name = compose_shipping_name(edited_name, country, edited_prc_id, edited_pccc)
        edited_summary_rows.append(
            {
                "Order No.": order_id,
                "Name": shipping_name,
                "Country": country,
                "TransType": st.session_state.get(trans_key, default_trans_type),
                "TotalValue(USD)": "",
                "TotalValue(JPY)": "",
            }
        )
        edited_items_by_position[position] = _apply_data_editor_state(item_frame, item_key)
    if not edited_summary_rows:
        return df_pending
    return apply_pending_order_editor_values(
        df_pending,
        pd.DataFrame(edited_summary_rows),
        edited_items_by_position,
        usd_jpy_rate=usd_jpy_rate,
    )


def _selected_source_indices_from_state(df_pending: pd.DataFrame, editable_count: int) -> list[object]:
    selected_indices: list[object] = []
    for position, source_index in enumerate(df_pending.index[:editable_count]):
        row = df_pending.iloc[position]
        order_id = str(row.get("瘜冽??芸(鞎潔???鞈?)", "")).strip() or f"row-{position + 1}"
        reset_version = _reset_version(order_id)
        selected_key = _selected_key_for(position, order_id, reset_version)
        if bool(st.session_state.get(selected_key, True)):
            selected_indices.append(source_index)
    selected_indices.extend(list(df_pending.index[editable_count:]))
    return selected_indices


def _extra_trans_types_by_index_from_state(df_pending: pd.DataFrame, editable_count: int) -> dict[object, list[str]]:
    extra_trans_types: dict[object, list[str]] = {}
    for position, source_index in enumerate(df_pending.index[:editable_count]):
        row = df_pending.iloc[position]
        order_id = str(row.get("瘜冽??芸(鞎潔???鞈?)", "")).strip() or f"row-{position + 1}"
        reset_version = _reset_version(order_id)
        extra_key = _extra_trans_key_for(position, order_id, reset_version)
        selected = st.session_state.get(extra_key, [])
        if isinstance(selected, (list, tuple)):
            extra_trans_types[source_index] = [str(value) for value in selected]
    return extra_trans_types


def _prepare_pending_run_frame_from_state(
    df_pending: pd.DataFrame,
    editable_count: int,
    usd_jpy_rate: float | None,
) -> pd.DataFrame:
    edited = _build_pending_run_frame_from_state(df_pending, editable_count, usd_jpy_rate)
    selected_indices = _selected_source_indices_from_state(df_pending, editable_count)
    if not selected_indices:
        return edited.iloc[0:0].copy()
    selected = edited.loc[selected_indices].copy()
    extra_trans_types = _extra_trans_types_by_index_from_state(df_pending, editable_count)
    return expand_pending_orders_for_trans_types(selected, extra_trans_types)


def _summary_cell(label: str, value: str) -> str:
    return (
        '<div class="summary-cell">'
        f'<div class="summary-label">{html.escape(label)}</div>'
        f'<div class="summary-value">{html.escape(str(value))}</div>'
        '</div>'
    )


def _native_info(label: str, value: str) -> str:
    label_class = "native-info-order" if label == "Order No." else "native-info-standard"
    return (
        f'<div class="native-info {label_class}">'
        f'<span class="native-info-label">{html.escape(label)}</span>'
        f'<span class="native-info-value">{html.escape(str(value))}</span>'
        '</div>'
    )


def _summary_label(label: str) -> str:
    return f'<div class="summary-label select-summary-label">{html.escape(label)}</div>'


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
        st.markdown('## 📮 <span class="brand-accent">JP Post</span> 製單系統', unsafe_allow_html=True)
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

    col1, col2, col3 = st.columns([5.7, 1.4, 0.65], vertical_alignment="center")
    with col1:
        st.markdown('<div class="app-header-title">JP Post 製單系統</div>', unsafe_allow_html=True)
    with col2:
        if picture:
            st.markdown(
                '<div class="app-header-user">'
                f'<img src="{picture}" width="28" style="border-radius:50%;'
                f'vertical-align:middle;margin-right:6px;">'
                f'<span>{name}</span>'
                '</div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(f'<div class="app-header-user"><span>{html.escape(name)}</span></div>', unsafe_allow_html=True)
    with col3:
        st.markdown('<span class="logout-button-marker"></span>', unsafe_allow_html=True)
        if st.button("登出", type="secondary"):
            logout(_cm)
            st.rerun()

    st.divider()

    st.markdown(
        """
        <style>
        :root {
            --erp-bg: #0b0d10;
            --erp-bg-warm: #15100c;
            --erp-surface: #15171b;
            --erp-surface-2: #1d2026;
            --erp-surface-3: #101722;
            --erp-border: rgba(251, 146, 60, 0.24);
            --erp-border-soft: rgba(148, 163, 184, 0.18);
            --erp-text: #f8fafc;
            --erp-muted: #cbd5e1;
            --erp-dim: #94a3b8;
            --erp-accent: #f59e0b;
            --erp-accent-2: #ea580c;
            --erp-danger: #ef4444;
            --control-h: 36px;
            --row-h: 38px;
            --control-radius: 10px;
            --control-pad-x: 12px;
            --row-gap: 8px;
        }
        * { box-sizing: border-box; }
        .stApp {
            background:
                radial-gradient(circle at 8% 4%, rgba(180, 83, 9, 0.24), transparent 30rem),
                linear-gradient(135deg, var(--erp-bg-warm) 0%, var(--erp-bg) 45%, #11100e 100%);
            color: var(--erp-text);
        }
        .block-container {
            padding-top: .72rem;
            padding-bottom: 2rem;
            max-width: 1580px;
        }
        div[data-testid="stHorizontalBlock"] { gap: var(--row-gap); }
        hr { margin: 0 0 .02rem 0; border-color: rgba(148, 163, 184, 0.12); }
        h1, h2, h3, h4, h5, h6 { color: var(--erp-text); letter-spacing: 0; }
        h3 { color: #fff7ed; margin-bottom: .18rem; }
        div[data-testid="stHeading"] { margin-bottom: .08rem; }
        p, label, .stMarkdown, [data-testid="stCaptionContainer"] { color: var(--erp-muted); }
        div[data-testid="stCaptionContainer"] { color: var(--erp-dim); }
        .app-header-title {
            color: var(--erp-accent);
            font-size: 1.66rem;
            font-weight: 850;
            line-height: 1.05;
            padding-top: 1.05rem;
            padding-bottom: .18rem;
        }
        .app-header-user {
            color: var(--erp-text);
            display: flex;
            align-items: center;
            min-height: var(--control-h);
            padding-top: 1.05rem;
            font-size: .9rem;
            white-space: nowrap;
        }
        div[data-testid="stVerticalBlock"]:has(.logout-button-marker) div[data-testid="stButton"] {
            padding-top: 1.05rem;
        }
        button {
            color: var(--erp-text) !important;
            border-radius: var(--control-radius) !important;
            min-height: var(--control-h);
            height: var(--control-h);
            padding-left: var(--control-pad-x) !important;
            padding-right: var(--control-pad-x) !important;
            white-space: nowrap !important;
        }
        button:disabled {
            color: #94a3b8 !important;
            opacity: .72;
        }
        .stButton > button {
            border-color: var(--erp-border-soft);
            background: rgba(24, 24, 27, 0.78);
        }
        .stButton > button:hover {
            border-color: rgba(245, 158, 11, 0.62);
            background: rgba(39, 39, 42, 0.95);
        }
        div[data-testid="stButton"],
        div[data-testid="stNumberInput"] {
            height: var(--control-h);
            min-height: var(--control-h);
            margin-bottom: 0;
            display: flex;
            align-items: center;
        }
        div[data-testid="stMetric"] {
            border: 1px solid var(--erp-border);
            border-radius: 10px;
            padding: 0.62rem 0.78rem;
            background: rgba(24, 24, 27, 0.86);
            color: var(--erp-text);
        }
        div[data-testid="stMetric"] label,
        div[data-testid="stMetric"] [data-testid="stMetricLabel"] {
            color: var(--erp-muted) !important;
        }
        div[data-testid="stMetric"] [data-testid="stMetricValue"] {
            color: var(--erp-text) !important;
        }
        .toolbar-title {
            height: var(--control-h);
            color: var(--erp-accent);
            display: flex;
            align-items: center;
            padding: 0;
            font-size: 1.52rem;
            font-weight: 850;
            line-height: 1;
            white-space: nowrap;
        }
        .toolbar-text {
            min-height: var(--control-h);
            display: flex;
            align-items: center;
            color: var(--erp-text);
            font-size: .9rem;
            font-weight: 700;
            white-space: nowrap;
        }
        .toolbar-text span {
            color: var(--erp-accent);
            font-size: .78rem;
            margin-right: .22rem;
        }
        .toolbar-muted {
            color: var(--erp-muted);
            font-size: .78rem;
            margin-left: .12rem;
        }
        .toolbar-count {
            display: inline-flex;
            align-items: baseline;
            gap: .28rem;
        }
        .toolbar-count strong {
            color: var(--erp-text);
            font-size: 1.52rem;
            font-weight: 900;
            line-height: 1;
        }
        .brand-title,
        .brand-title * {
            color: var(--erp-accent) !important;
        }
        .toolbar-chip,
        .field-inline-label {
            border: 1px solid rgba(251, 146, 60, 0.22);
            border-radius: var(--control-radius);
            background: rgba(15, 23, 42, 0.7);
            color: var(--erp-text);
            height: var(--control-h);
            min-height: var(--control-h);
            padding: 0 var(--control-pad-x);
            display: flex;
            align-items: center;
            box-sizing: border-box;
            white-space: nowrap;
        }
        .toolbar-chip {
            font-size: .8rem;
            font-weight: 700;
        }
        .toolbar-chip.toolbar-hint-chip {
            color: var(--erp-accent);
            font-size: .72rem;
            justify-content: center;
            padding-left: .55rem;
            padding-right: .55rem;
        }
        .toolbar-chip span {
            color: var(--erp-accent);
            font-size: .68rem;
            font-weight: 650;
            margin-right: .35rem;
        }
        .toolbar-inline-label {
            color: var(--erp-dim);
            font-size: .72rem;
            line-height: var(--control-h);
            height: var(--control-h);
            white-space: nowrap;
        }
        .toolbar-inline-label span {
            color: var(--erp-accent);
            margin-left: .25rem;
        }
        .field-inline-label { display: none; }
        .order-card-marker,
        .debug-log-marker {
            display: none;
        }
        div[data-testid="stExpander"] {
            border-radius: 12px;
            border-color: var(--erp-border-soft);
            background: rgba(24, 24, 27, 0.82);
            overflow: hidden;
        }
        div[data-testid="stExpander"] details > summary {
            background: rgba(39, 39, 42, 0.92);
            min-height: 2.35rem;
            color: var(--erp-text);
        }
        div[data-testid="stDataFrame"] {
            border-radius: 10px;
            overflow: hidden;
        }
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.order-card-marker) {
            border: 1px solid rgba(251, 146, 60, 0.17) !important;
            border-radius: 12px !important;
            background: rgba(19, 21, 25, 0.96);
            margin-bottom: .62rem;
            box-shadow: inset 0 1px 0 rgba(255, 255, 255, .025), 0 10px 24px rgba(0, 0, 0, .12);
            padding: .46rem .62rem .52rem .62rem !important;
        }
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.order-card-marker):hover {
            border-color: rgba(245, 158, 11, 0.42) !important;
            background: rgba(23, 25, 30, 0.96);
        }
        .order-card {
            border: 1px solid var(--erp-border-soft);
            border-radius: 12px;
            background: rgba(20, 22, 26, 0.92);
            margin: 0 0 .82rem 0;
            overflow: hidden;
            box-shadow: 0 10px 30px rgba(0, 0, 0, .18);
        }
        .order-card:hover {
            border-color: rgba(245, 158, 11, 0.42);
            background: rgba(23, 25, 30, 0.96);
        }
        .order-card-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: .75rem;
            padding: .52rem .72rem;
            background: rgba(39, 39, 42, 0.92);
            border-bottom: 1px solid var(--erp-border-soft);
        }
        .order-title {
            color: var(--erp-text);
            font-weight: 700;
            line-height: 1.25;
            overflow-wrap: anywhere;
            padding: 0;
        }
        .order-card-body {
            padding: .46rem .62rem .54rem .62rem;
        }
        .order-summary-grid {
            margin-bottom: .26rem;
        }
        .order-summary-grid div[data-testid="column"] {
            min-width: 0;
        }
        .summary-cell {
            border: 1px solid var(--erp-border);
            background: rgba(15, 23, 42, 0.72);
            border-radius: var(--control-radius);
            padding: .18rem var(--control-pad-x);
            height: var(--control-h);
            min-height: var(--control-h);
            display: flex;
            flex-direction: column;
            justify-content: center;
        }
        .summary-label {
            color: var(--erp-accent);
            font-size: .68rem;
            line-height: 1.1;
            font-weight: 650;
        }
        .summary-value {
            color: var(--erp-text);
            font-weight: 700;
            line-height: 1.08;
            white-space: normal;
            overflow-wrap: anywhere;
        }
        .native-info {
            min-height: var(--control-h);
            display: flex;
            align-items: center;
            gap: .34rem;
            white-space: nowrap;
            min-width: 0;
        }
        .native-info-label {
            color: var(--erp-accent);
            font-size: .8rem;
            font-weight: 700;
            line-height: var(--control-h);
        }
        .native-info-value {
            color: var(--erp-text);
            font-size: 1.13rem;
            font-weight: 850;
            line-height: var(--control-h);
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .native-info-order .native-info-value {
            font-size: 1.2rem;
            font-weight: 900;
        }
        .order-info-row {
            margin-bottom: .12rem;
        }
        .order-action-row {
            margin-bottom: .2rem;
        }
        .trans-select-cell {
            border: 1px solid var(--erp-border);
            background: rgba(15, 23, 42, 0.72);
            border-radius: 8px;
            padding: .24rem .42rem .34rem .42rem;
            min-height: 3.05rem;
        }
        .trans-select-cell div[data-baseweb="select"] > div {
            background: transparent;
            border: 0;
            min-height: 1.45rem;
            padding-left: 0;
            color: var(--erp-text);
        }
        .trans-select-cell [data-baseweb="select"] span,
        .trans-select-cell [data-baseweb="select"] div {
            color: var(--erp-text) !important;
            font-weight: 700;
        }
        .select-summary-label { margin-bottom: .02rem; }
        div[data-baseweb="select"] > div {
            background: rgba(15, 23, 42, 0.96);
            border-color: rgba(251, 146, 60, 0.26);
            min-height: var(--control-h);
            height: var(--control-h);
            border-radius: var(--control-radius);
        }
        div[data-baseweb="select"] span,
        div[data-baseweb="select"] div {
            color: var(--erp-text) !important;
            font-weight: 700;
        }
        .rate-caption {
            color: #fde68a;
            font-size: .78rem;
            text-align: right;
            padding-top: .42rem;
            white-space: nowrap;
        }
        .stButton > button[kind="primary"] {
            background: #c2410c;
            border-color: var(--erp-accent-2);
        }
        div[data-testid="stDataEditor"] {
            border-radius: 8px;
            overflow: hidden;
            border: 1px solid rgba(148, 163, 184, 0.16);
            background: #111827;
            max-width: 100%;
        }
        div[data-testid="stDataEditor"] [role="gridcell"],
        div[data-testid="stDataEditor"] [role="columnheader"] {
            line-height: 1.12;
            min-height: 1.56rem !important;
        }
        div[data-testid="stDataEditor"] [role="columnheader"] {
            background: #1f2937 !important;
            color: #cbd5e1 !important;
        }
        div[data-testid="stDataEditor"] [role="gridcell"] {
            background: #111827 !important;
            color: #e5e7eb !important;
            border-color: rgba(148, 163, 184, 0.14) !important;
        }
        div[data-testid="stDataEditor"] [role="row"]:hover [role="gridcell"] {
            background: #172033 !important;
        }
        div[data-testid="stNumberInput"] input {
            background: rgba(15, 23, 42, 0.96) !important;
            border-color: rgba(251, 146, 60, 0.26) !important;
            color: var(--erp-text) !important;
            min-height: var(--control-h);
            height: var(--control-h);
            border-radius: var(--control-radius);
            font-weight: 650;
        }
        div[data-testid="stNumberInput"] input::-webkit-outer-spin-button,
        div[data-testid="stNumberInput"] input::-webkit-inner-spin-button {
            -webkit-appearance: none;
            margin: 0;
        }
        div[data-testid="stNumberInput"] input[type=number] {
            -moz-appearance: textfield;
        }
        div[data-testid="stNumberInput"] button {
            display: none;
        }
        div[data-testid="stNumberInput"] {
            display: grid;
            grid-template-columns: auto 58px;
            align-items: center;
            gap: .5rem;
        }
        div[data-testid="stNumberInput"] label {
            color: var(--erp-accent) !important;
            font-size: .8rem !important;
            font-weight: 700 !important;
            min-height: var(--control-h);
            height: var(--control-h);
            display: flex;
            align-items: center;
            padding: 0;
            margin: 0;
            white-space: nowrap;
        }
        div[data-testid="stNumberInput"] label * {
            color: var(--erp-accent) !important;
            font-weight: 700 !important;
        }
        div[data-testid="stTextInput"] input {
            background: rgba(15, 23, 42, 0.96) !important;
            border-color: rgba(251, 146, 60, 0.26) !important;
            color: var(--erp-text) !important;
            min-height: var(--control-h);
            height: var(--control-h);
            border-radius: var(--control-radius);
            font-weight: 650;
        }
        div[data-testid="stTextInput"] label,
        div[data-testid="stSelectbox"] label {
            color: var(--erp-accent) !important;
            font-size: .75rem !important;
            font-weight: 650 !important;
            min-height: var(--control-h);
            height: var(--control-h);
            display: flex;
            align-items: center;
            padding: 0 .42rem 0 0;
            margin: 0;
        }
        div[data-testid="stTextInput"] label *,
        div[data-testid="stSelectbox"] label *,
        div[data-testid="stTextInput"] [data-testid="stWidgetLabel"] *,
        div[data-testid="stSelectbox"] [data-testid="stWidgetLabel"] * {
            color: var(--erp-accent) !important;
            font-weight: 650 !important;
        }
        div[data-testid="stTextInput"],
        div[data-testid="stSelectbox"] {
            display: grid;
            grid-template-columns: auto minmax(0, 1fr);
            align-items: center;
            gap: .5rem;
        }
        div[data-testid="stTextInput"] > div,
        div[data-testid="stSelectbox"] > div {
            min-width: 0;
        }
        .compact-actions div[data-testid="column"] {
            display: flex;
            align-items: stretch;
        }
        div[data-testid="stExpander"]:has(.debug-log-marker) {
            background: rgba(12, 16, 25, 0.9);
            border-color: rgba(148, 163, 184, 0.18);
        }
        div[data-testid="stExpander"]:has(.debug-log-marker) summary {
            background: rgba(17, 24, 39, 0.92) !important;
        }
        div[data-testid="stExpander"]:has(.debug-log-marker) pre,
        div[data-testid="stExpander"]:has(.debug-log-marker) code {
            max-height: 260px !important;
            overflow-y: auto !important;
            background: #0b1020 !important;
            color: #d1e7ff !important;
            border: 1px solid rgba(148, 163, 184, 0.18);
            border-radius: 10px;
            font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
            font-size: .82rem;
            line-height: 1.5;
        }
        .inline-static {
            min-height: var(--control-h);
            display: flex;
            align-items: center;
        }
        .sent-compact {
            border: 1px solid rgba(34, 197, 94, 0.2);
            border-radius: 8px;
            background: rgba(20, 83, 45, 0.16);
            color: #bbf7d0;
            font-size: .82rem;
            padding: .32rem .48rem;
            margin-top: .42rem;
            white-space: normal;
        }
        @media (max-width: 1200px) {
            .block-container {
                padding-left: 1.35rem;
                padding-right: 1.35rem;
            }
            .toolbar-title {
                font-size: 1.42rem;
            }
            .toolbar-count strong {
                font-size: 1.42rem;
            }
            .native-info-value {
                font-size: 1.04rem;
            }
            .native-info-order .native-info-value {
                font-size: 1.12rem;
            }
            div[data-testid="stVerticalBlockBorderWrapper"]:has(.order-card-marker) {
                padding: .42rem .54rem .5rem .54rem !important;
            }
        }
        @media (max-width: 900px) {
            .app-header-title {
                padding-top: .5rem;
            }
            .app-header-user,
            div[data-testid="stVerticalBlock"]:has(.logout-button-marker) div[data-testid="stButton"] {
                padding-top: .5rem;
            }
            .toolbar-title,
            .toolbar-text,
            .native-info {
                white-space: normal;
            }
            .toolbar-text {
                align-items: flex-start;
                line-height: 1.2;
                min-height: auto;
            }
            .toolbar-count {
                align-items: center;
            }
            .native-info {
                align-items: baseline;
                line-height: 1.2;
            }
            .native-info-label,
            .native-info-value {
                line-height: 1.2;
            }
        }
        @media (max-width: 640px) {
            .block-container {
                padding-left: .75rem;
                padding-right: .75rem;
            }
            .app-header-title {
                font-size: 1.34rem;
                padding-top: .25rem;
            }
            .app-header-user,
            div[data-testid="stVerticalBlock"]:has(.logout-button-marker) div[data-testid="stButton"] {
                padding-top: .1rem;
            }
            .toolbar-title {
                font-size: 1.28rem;
                min-height: 1.65rem;
            }
            .toolbar-count strong {
                font-size: 1.36rem;
            }
            button {
                min-height: 44px;
                height: 44px;
            }
            div[data-testid="stButton"],
            div[data-testid="stNumberInput"] {
                min-height: 44px;
                height: 44px;
            }
            div[data-testid="stNumberInput"] {
                grid-template-columns: auto 64px;
            }
            div[data-testid="stTextInput"],
            div[data-testid="stSelectbox"] {
                grid-template-columns: 4.5rem minmax(0, 1fr);
            }
            div[data-testid="stTextInput"] input,
            div[data-baseweb="select"] > div,
            div[data-testid="stNumberInput"] input {
                min-height: 44px;
                height: 44px;
            }
            .native-info-value {
                font-size: 1rem;
            }
            .native-info-order .native-info-value {
                font-size: 1.08rem;
            }
            div[data-testid="stVerticalBlockBorderWrapper"]:has(.order-card-marker) {
                padding: .5rem .5rem .55rem .5rem !important;
            }
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

    rate, rate_date, rate_source = _load_usd_jpy_rate() if not df_pending.empty else (None, "", "")
    editable_count = min(len(df_pending), 20)
    if is_running or df_pending.empty:
        df_pending_for_run = df_pending
        selected_count = pending_count
    else:
        selected_count = len(_selected_source_indices_from_state(df_pending, editable_count))
        df_pending_for_run = _prepare_pending_run_frame_from_state(df_pending, editable_count, rate)
    zero_value_warnings = _zero_value_warning_lines(df_pending_for_run)
    required_id_warnings = _required_id_warning_lines(df_pending_for_run)
    done = len(job["results"]) if job else 0

    toolbar_info_cols = st.columns([1.75, 1.45, .9, .95, 1.02], gap="small", vertical_alignment="center")
    with toolbar_info_cols[0]:
        st.markdown('<div class="toolbar-title">待打單預覽</div>', unsafe_allow_html=True)
    with toolbar_info_cols[1]:
        st.markdown(f'<div class="toolbar-text">{html.escape(_format_short_rate(rate, rate_date))}</div>', unsafe_allow_html=True)
    with toolbar_info_cols[2]:
        st.markdown(
            f'<div class="toolbar-text toolbar-count"><span>待製單</span><strong>{pending_count}</strong></div>',
            unsafe_allow_html=True,
        )
    with toolbar_info_cols[3]:
        st.markdown(
            f'<div class="toolbar-text toolbar-count"><span>已選取</span><strong>{selected_count}</strong></div>',
            unsafe_allow_html=True,
        )
    with toolbar_info_cols[4]:
        st.markdown(
            f'<div class="toolbar-text toolbar-count"><span>本次完成</span><strong>{done}</strong></div>',
            unsafe_allow_html=True,
        )
    st.markdown('<div style="height:.04rem"></div>', unsafe_allow_html=True)
    toolbar_action_cols = st.columns([.88, .45, 1.0, 1.0, 1.5, 1.12], gap="small", vertical_alignment="center")
    with toolbar_action_cols[0]:
        max_rows_input = st.number_input(
            "最大處理",
            min_value=0, max_value=500, value=20, step=1,
            disabled=is_running,
        )
    with toolbar_action_cols[1]:
        st.markdown('<div class="toolbar-text"><span class="toolbar-muted">(0=全部)</span></div>', unsafe_allow_html=True)
    max_rows_val: int | None = None if max_rows_input == 0 else int(max_rows_input)
    with toolbar_action_cols[2]:
        if is_running:
            if st.button("🔄 重新整理", width="stretch", key="refresh_running_top"):
                st.rerun()
        elif st.button("🔁 重新讀取", width="stretch", key="reload_pending_top"):
            st.session_state.pop("last_pending_df", None)
            st.session_state.pop("last_pending_logs", None)
            st.rerun()
    with toolbar_action_cols[3]:
        btn_label = "執行中…" if is_running else ("🚀 開始製單" if pending_count > 0 else "✅ 無待處理訂單")
        if st.button(btn_label, type="primary",
                     disabled=(is_running or pending_count == 0 or selected_count == 0 or bool(zero_value_warnings) or bool(required_id_warnings)), width="stretch"):
            if df_pending.empty:
                st.warning("沒有符合條件的待打單資料")
            elif df_pending_for_run.empty:
                st.warning("目前未選取任何訂單")
            else:
                ok, reason = _start_job(email, df_pending_for_run, max_rows_val)
                if ok:
                    if hasattr(st, "toast"):
                        st.toast("✅ 已啟動自動製單")
                    time.sleep(0.8)
                    st.rerun()
                elif reason == "batch_running":
                    st.error("同一批製單已在執行中，已阻止重複啟動。")
                else:
                    st.error("任務執行中，請稍候")
    with toolbar_action_cols[5]:
        reset_all_requested = st.button(
            "恢復全部預設",
            width="stretch",
            key="reset_all_pending",
            disabled=is_running or df_pending.empty,
        )

    if reset_all_requested and not df_pending.empty:
        _reset_all_order_editors(df_pending.head(editable_count))
        st.rerun()
    if not rate and not df_pending.empty:
        st.warning(f"暫時無法取得 USD/JPY 匯率；若編輯 Value 或 Quantity，TotalValue(JPY) 會保留來源預設值。{rate_source}")
    if zero_value_warnings:
        st.error("有品項 Value 為 0，請先修正：" + "；".join(zero_value_warnings[:5]))
    if required_id_warnings:
        st.error("；".join(required_id_warnings[:5]))

    if not df_pending.empty:
        if is_running:
            st.dataframe(build_pending_summary_frame(df_pending).head(20), hide_index=True, width="stretch")
        else:
            edited_summary_rows: list[dict[str, str]] = []
            edited_items_by_position: dict[int, pd.DataFrame] = {}
            job_order_by_id = {str(order.get("order_id", "")): order for order in (job or {}).get("orders", [])}
            for position in range(editable_count):
                row = df_pending.iloc[position]
                order_id = str(row.get("注文番号(貼上原始資料)", "")).strip() or f"row-{position + 1}"
                country = str(row.get("收件人國家", row.get("Country", ""))).strip()
                kind = country_kind(country)
                parsed_name = parse_shipping_name(row.get("Shipping Name", row.get("Shipping Name_1", "")))
                default_trans_type = str(row.get(SHIPPING_COL, "")).strip()
                reset_version = _reset_version(order_id)
                item_frame = build_pending_item_frame(row)
                item_key = f"pending_items_{position}_{order_id}_{reset_version}"
                summary_item_frame = _apply_data_editor_state(item_frame, item_key)
                trans_key = f"pending_trans_{position}_{order_id}_{reset_version}"
                name_key = _name_key_for(position, order_id, reset_version)
                prc_id_key = _prc_id_key_for(position, order_id, reset_version)
                pccc_key = _pccc_key_for(position, order_id, reset_version)
                selected_key = _selected_key_for(position, order_id, reset_version)
                extra_trans_key = _extra_trans_key_for(position, order_id, reset_version)
                _sync_recipient_id_session_fields(name_key, prc_id_key, pccc_key)
                pending_trans = st.session_state.get(trans_key, default_trans_type)
                pending_name = st.session_state.get(name_key, parsed_name["clean_name"])
                pending_prc_id = st.session_state.get(prc_id_key, parsed_name["prc_id"])
                pending_pccc = st.session_state.get(pccc_key, parsed_name["pccc"])
                composed_name_preview = compose_shipping_name(pending_name, country, pending_prc_id, pending_pccc)
                summary_preview = {
                    "Order No.": order_id,
                    "Name": composed_name_preview,
                    "Country": country,
                    "TransType": pending_trans,
                    "TotalValue(USD)": "",
                    "TotalValue(JPY)": "",
                }
                preview_df = apply_pending_order_editor_values(
                    df_pending.iloc[[position]],
                    pd.DataFrame([summary_preview]),
                    {0: summary_item_frame},
                    usd_jpy_rate=rate,
                )
                summary_row = build_pending_summary_frame(preview_df).iloc[0]

                with st.container(border=True):
                    st.markdown('<span class="order-card-marker"></span>', unsafe_allow_html=True)
                    st.markdown('<div class="order-info-row"></div>', unsafe_allow_html=True)
                    info_cols = st.columns([2.25, 1.55, .86, .86, 1.0], gap="small", vertical_alignment="center")
                    with info_cols[0]:
                        st.markdown(_native_info("Order No.", order_id), unsafe_allow_html=True)
                    with info_cols[1]:
                        st.markdown(_native_info("Country", summary_row["Country"]), unsafe_allow_html=True)
                    with info_cols[2]:
                        st.markdown(_native_info("USD", summary_row["TotalValue(USD)"]), unsafe_allow_html=True)
                    with info_cols[3]:
                        st.markdown(_native_info("JPY", summary_row["TotalValue(JPY)"]), unsafe_allow_html=True)

                    st.markdown('<div class="order-action-row"></div>', unsafe_allow_html=True)
                    if kind in {"china", "korea"}:
                        action_cols = st.columns([.55, 1.42, 1.2, 1.35, 1.45, .9], gap="small", vertical_alignment="center")
                    else:
                        action_cols = st.columns([.55, 1.42, 1.2, 1.45, 1.9, .9], gap="small", vertical_alignment="center")
                    with action_cols[0]:
                        st.checkbox(
                            "製單",
                            value=bool(st.session_state.get(selected_key, True)),
                            key=selected_key,
                        )
                    with action_cols[1]:
                        edited_name = st.text_input(
                            "Name",
                            value=pending_name,
                            key=name_key,
                        )
                    with action_cols[2]:
                        trans_type = st.selectbox(
                            "TransType",
                            options=SHIPPING_OPTIONS,
                            index=SHIPPING_OPTIONS.index(default_trans_type) if default_trans_type in SHIPPING_OPTIONS else 0,
                            key=trans_key,
                        )
                    with action_cols[3 if kind not in {"china", "korea"} else 4]:
                        st.multiselect(
                            "追加",
                            options=SHIPPING_OPTIONS,
                            default=st.session_state.get(extra_trans_key, []),
                            key=extra_trans_key,
                        )
                    edited_prc_id = pending_prc_id
                    edited_pccc = pending_pccc
                    if kind == "china":
                        with action_cols[3]:
                            edited_prc_id = st.text_input("PRC ID", value=pending_prc_id, key=prc_id_key)
                    elif kind == "korea":
                        with action_cols[3]:
                            edited_pccc = st.text_input("PCCC", value=pending_pccc, key=pccc_key)
                    with action_cols[-1]:
                        if st.button("恢復預設", key=f"reset_order_{position}_{order_id}", width="stretch"):
                            _reset_order_editor(order_id)
                            st.rerun()
                    composed_name = compose_shipping_name(edited_name, country, edited_prc_id, edited_pccc)

                    edited_summary_rows.append(
                        {
                            "Order No.": order_id,
                            "Name": composed_name,
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
                    edited_items_by_position[position] = st.data_editor(
                        item_frame,
                        hide_index=True,
                        width="stretch",
                        num_rows="fixed",
                        disabled=["Content"],
                        column_config={
                            "Content": st.column_config.TextColumn("Content", width=70),
                            "Description": st.column_config.TextColumn("Description", width="large"),
                            "HSCode": st.column_config.TextColumn("HSCode", width=120),
                            "Value": st.column_config.TextColumn("Value", width=100),
                            "Quantity": st.column_config.TextColumn("Quantity", width=90),
                        },
                        key=item_key,
                    )
                    sent_order = job_order_by_id.get(order_id)
                    if sent_order and sent_order.get("status") == "success":
                        hs_text = str(sent_order.get("hs_codes", "")).strip()
                        st.markdown(
                            '<div class="sent-compact">'
                            f'已製單｜Name {html.escape(edited_name)}'
                            + (f'｜PRC ID {html.escape(edited_prc_id)}' if kind == "china" and edited_prc_id else '')
                            + (f'｜PCCC {html.escape(edited_pccc)}' if kind == "korea" and edited_pccc else '')
                            + f'｜TransType {html.escape(trans_type)}'
                            f'｜HS {html.escape(hs_text)}｜USD {html.escape(str(summary_row["TotalValue(USD)"]))}'
                            f'｜JPY {html.escape(str(summary_row["TotalValue(JPY)"]))}'
                            '</div>',
                            unsafe_allow_html=True,
                        )

            edited_df = apply_pending_order_editor_values(
                df_pending,
                pd.DataFrame(edited_summary_rows),
                edited_items_by_position,
                usd_jpy_rate=rate,
            )
            selected_indices = _selected_source_indices_from_state(df_pending, editable_count)
            if selected_indices:
                df_pending_for_run = expand_pending_orders_for_trans_types(
                    edited_df.loc[selected_indices].copy(),
                    _extra_trans_types_by_index_from_state(df_pending, editable_count),
                )
            else:
                df_pending_for_run = edited_df.iloc[0:0].copy()
            if len(df_pending) > editable_count:
                st.caption(f"目前可編輯前 {editable_count} 筆；其餘訂單會保留來源表資料。")
        if pending_logs:
            with st.expander(f"待製單讀取診斷｜最終可打單 {pending_count} 筆", expanded=False):
                st.markdown('<span class="debug-log-marker"></span>', unsafe_allow_html=True)
                st.code("\n".join(pending_logs), language="text")
    elif pending_logs:
        st.info("目前沒有待製單資料。")
        with st.expander(f"待製單讀取診斷｜最終可打單 {pending_count} 筆", expanded=False):
            st.markdown('<span class="debug-log-marker"></span>', unsafe_allow_html=True)
            st.code("\n".join(pending_logs), language="text")
    else:
        st.info("目前沒有待製單資料。")

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
        order_lookup = {str(order.get("order_id", "")): order for order in job.get("orders", [])}
        for result in job["results"]:
            order_id = str(result.get("order_id", "")).strip()
            order_state = order_lookup.get(order_id, {})
            hs_text = str(order_state.get("hs_codes", "")).strip()
            result_name = str(result.get("name", ""))
            parsed_result_name = parse_shipping_name(result_name)
            result_country = str(order_state.get("country", ""))
            result_kind = country_kind(result_country)
            st.markdown(
                '<div class="sent-compact">'
                f'已製單｜Name {html.escape(parsed_result_name["clean_name"] or result_name)}'
                + (f'｜PRC ID {html.escape(parsed_result_name["prc_id"])}' if result_kind == "china" and parsed_result_name["prc_id"] else '')
                + (f'｜PCCC {html.escape(parsed_result_name["pccc"])}' if result_kind == "korea" and parsed_result_name["pccc"] else '')
                + (f'｜TransType {html.escape(str(order_state.get("trans_type", "")))}' if order_state.get("trans_type") else '')
                + f'｜Tracking {html.escape(str(result.get("tracking", "")))}'
                + (f'｜HS {html.escape(hs_text)}' if hs_text else '')
                + (f'｜USD {html.escape(str(order_state.get("total_usd", "")))}' if order_state.get("total_usd") else '')
                + (f'｜JPY {html.escape(str(order_state.get("total_jpy", "")))}' if order_state.get("total_jpy") else '')
                + '</div>',
                unsafe_allow_html=True,
            )

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
