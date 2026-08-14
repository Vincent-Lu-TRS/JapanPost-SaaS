"""
日本郵政自動化製單 SaaS 平台 - 主程式
Streamlit Web UI + Google OAuth（限 @tkrjm.co.jp）
支援：30 天 Cookie Session
"""
import os
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/tmp/ms-playwright")

import hashlib
import html
import re
from datetime import datetime, timedelta
from pathlib import Path
import subprocess
import sys
import time
import threading
import tempfile
import streamlit as st
import pandas as pd
from app_imports import import_module_with_retry
from bot.sheets import backfill_results

_job_control = import_module_with_retry("job_control")
BatchJobRegistry = _job_control.BatchJobRegistry
apply_writeback_outcome = _job_control.apply_writeback_outcome
mark_results_completed = _job_control.mark_results_completed
mark_results_failed = _job_control.mark_results_failed
mark_unfinished_orders = _job_control.mark_unfinished_orders
partition_preflight_rows = _job_control.partition_preflight_rows
preflight_batch_orders = _job_control.preflight_batch_orders
summarize_job_results = _job_control.summarize_job_results
summarize_job_progress = _job_control.summarize_job_progress
update_order_status_from_log = _job_control.update_order_status_from_log
update_order_status_from_event = _job_control.update_order_status_from_event
writeback_retry_candidates = _job_control.writeback_retry_candidates
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
    pending_order_warning_lines,
    sanitize_hscode,
)
from fx_rates import fetch_usd_jpy_rate
from postal_ui_feedback import (
    completed_order_ids,
    filter_pending_orders_after_batch,
    fully_completed_order_ids,
    preserve_incomplete_submitted_orders,
    summarize_batch_results,
    summarize_pending_read_logs,
)
from postal_ui_v2 import (
    apply_batch_selection,
    build_v2_item_display_frame,
    format_secondary_rate_badge,
    restore_v2_item_frame,
)
from refresh_cache import (
    RefreshResult,
    SharedRefreshCoordinator,
    may_apply_pending_snapshot,
)
from refresh_payloads import (
    PendingPayload,
    copy_pending_payload,
    copy_picking_payload,
)
from safe_logging import redact_operational_log, safe_log_event
from features.picking_labels import apply_picking_payload, load_picking_payload
from local_time import JST, format_jst

# ══════════════════════════════════════════════════════
# ★ set_page_config 必須在所有 st.* 呼叫之前
# ══════════════════════════════════════════════════════
st.set_page_config(
    page_title="Cross-Border製單系統",
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


# ── 全域任務追蹤器（跨 Streamlit 重繪保留同一份結果）──────
_JOB_REGISTRY_CACHE_VERSION = "2026-08-05-postal-ui"


@st.cache_resource(show_spinner=False)
def _get_job_registry(cache_version: str = _JOB_REGISTRY_CACHE_VERSION) -> BatchJobRegistry:
    return BatchJobRegistry()


_JOB_REGISTRY = _get_job_registry(_JOB_REGISTRY_CACHE_VERSION)


@st.cache_resource(show_spinner=False)
def _get_refresh_coordinator(
    cache_version="2026-08-13-v1",
) -> SharedRefreshCoordinator:
    return SharedRefreshCoordinator(
        ttl=timedelta(minutes=20),
        now=lambda: datetime.now(JST),
    )


def _job_lock_path(email: str) -> Path:
    digest = hashlib.sha256((email or "anonymous").encode("utf-8")).hexdigest()[:24]
    return Path(tempfile.gettempdir()) / "jppost-job-locks" / f"{digest}.lock"


def _write_job_lock(email: str) -> None:
    path = _job_lock_path(email)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(time.time()), encoding="utf-8")


def _clear_job_lock(email: str) -> None:
    try:
        _job_lock_path(email).unlink(missing_ok=True)
    except Exception:
        pass


def _job_lock_is_active(email: str, max_age_seconds: int = 1800) -> bool:
    path = _job_lock_path(email)
    if not path.exists():
        return False
    try:
        age = time.time() - path.stat().st_mtime
        if age > max_age_seconds:
            path.unlink(missing_ok=True)
            return False
    except Exception:
        return False
    return True


def _get_job(email: str) -> dict | None:
    return _JOB_REGISTRY.get(email)


def _reset_preflight_job_view(job: dict | None) -> None:
    """Clear a terminal preflight stop after the user explicitly reloads."""
    if not isinstance(job, dict) or not job.get("preflight_reload_required"):
        return
    if job.get("status") == "running":
        return
    job["status"] = "idle"
    job["orders"] = []
    job["results"] = []
    job["logs"] = []
    job["preflight_checks"] = []
    job.pop("batch_preflight_blocked_count", None)
    job.pop("preflight_reload_required", None)
    job.pop("preflight_reload_message", None)


def _dataframe_sensitive_values(dataframe: pd.DataFrame | None) -> tuple[str, ...]:
    if not isinstance(dataframe, pd.DataFrame) or dataframe.empty:
        return ()
    values: list[str] = []
    seen: set[str] = set()
    sensitive_markers = (
        "注文番号",
        "order",
        "shipping name",
        "receiver",
        "recipient",
        "email",
        "mail",
        "tracking",
        "追跡",
        "運單",
        "address",
        "住所",
        "地址",
        "收件人",
        "phone",
        "電話",
        "pccc",
        "prc id",
    )
    for column in dataframe.columns:
        column_name = str(column).strip().lower()
        is_status_column = "製單上傳狀態" in column_name
        if not is_status_column and not any(
            marker in column_name for marker in sensitive_markers
        ):
            continue
        for value in dataframe[column].tolist():
            if value is None:
                continue
            try:
                if pd.isna(value):
                    continue
            except (TypeError, ValueError):
                pass
            text = str(value).strip()
            if is_status_column and not re.fullmatch(
                r"[A-Z]{2}\d{9}[A-Z]{2}",
                text,
                flags=re.IGNORECASE,
            ):
                continue
            lowered = text.lower()
            if len(text) < 4 or lowered in seen:
                continue
            seen.add(lowered)
            values.append(text)
    return tuple(values)


def _job_sensitive_values(
    job: dict | None,
    *,
    dataframe: pd.DataFrame | None = None,
    email: str = "",
) -> tuple[str, ...]:
    values = list(_dataframe_sensitive_values(dataframe))
    if email:
        values.append(email)

    sensitive_keys = {
        "order_id",
        "recipient",
        "receiver",
        "tracking",
        "tracking_no",
        "email",
        "name",
        "address",
        "phone",
        "pccc",
        "prc_id",
        "reason_text",
        "message",
        "error",
    }

    def _collect(value, *, include_scalar: bool = False) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                _collect(
                    nested,
                    include_scalar=str(key).strip().lower() in sensitive_keys,
                )
        elif isinstance(value, (list, tuple, set)):
            for nested in value:
                _collect(nested, include_scalar=include_scalar)
        elif include_scalar:
            if value is None:
                return
            try:
                if pd.isna(value):
                    return
            except (TypeError, ValueError):
                pass
            text = str(value).strip()
            if len(text) >= 4:
                values.append(text)

    if isinstance(job, dict):
        for key in ("orders", "results", "backfill_outcome"):
            _collect(job.get(key))

    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        lowered = value.lower()
        if lowered not in seen:
            seen.add(lowered)
            unique.append(value)
    return tuple(unique)


def _safe_operational_log_lines(
    logs,
    *,
    sensitive_values=(),
) -> list[str]:
    return [
        redact_operational_log(line, sensitive_values=sensitive_values)
        for line in logs or []
    ]


def _load_pending_orders(
    *,
    strict: bool = False,
    exclude_completed: bool = True,
) -> tuple[pd.DataFrame, list[str]]:
    from bot.sheets import get_pending_orders

    pending_logs: list[str] = []
    df_pending = get_pending_orders(
        log_cb=pending_logs.append,
        strict=strict,
        exclude_completed=exclude_completed,
    )
    safe_logs = _safe_operational_log_lines(
        pending_logs,
        sensitive_values=_dataframe_sensitive_values(df_pending),
    )
    return df_pending, safe_logs


def load_pending_payload() -> PendingPayload:
    dataframe, logs = _load_pending_orders(strict=True, exclude_completed=True)
    return PendingPayload(dataframe=dataframe, logs=tuple(logs))


def _refresh_source(source: str, *, force: bool):
    coordinator = _get_refresh_coordinator()
    if source == "pending":
        return coordinator.get(
            source,
            load_pending_payload,
            force=force,
            copier=copy_pending_payload,
        )
    if source == "picking":
        return coordinator.get(
            source,
            load_picking_payload,
            force=force,
            copier=copy_picking_payload,
        )
    raise ValueError(f"unknown refresh source: {source}")


def _mark_pending_editor_dirty() -> None:
    st.session_state["pending_editor_dirty"] = True


def _clear_pending_editor_keys() -> None:
    prefixes = (
        "pending_v2_name_",
        "pending_v2_trans_",
        "pending_v2_extra_trans_",
        "pending_v2_prc_id_",
        "pending_v2_pccc_",
        "pending_v2_items_",
    )
    for key in list(st.session_state):
        if str(key).startswith(prefixes):
            st.session_state.pop(key, None)


def _apply_pending_result(
    result: RefreshResult[PendingPayload],
    *,
    is_busy: bool,
    allow_dirty_reset: bool = False,
    job=None,
) -> bool:
    error_code = str(result.status.error_code or "").strip()
    if error_code:
        st.session_state["pending_refresh_error_code"] = error_code
    else:
        st.session_state.pop("pending_refresh_error_code", None)

    if result.data is None:
        st.session_state["pending_refresh_error_code"] = error_code or "unavailable"
        return False

    if not may_apply_pending_snapshot(
        is_busy=is_busy,
        editor_dirty=bool(st.session_state.get("pending_editor_dirty")),
        allow_dirty_reset=allow_dirty_reset,
    ):
        return False

    payload = copy_pending_payload(result.data)
    dataframe = payload.dataframe.copy(deep=True)
    if job:
        dataframe = preserve_incomplete_submitted_orders(
            existing=st.session_state.get("last_pending_df"),
            refreshed=dataframe,
            submitted_orders=job.get("orders") or [],
            results=job.get("results") or [],
        )
        if job.get("results"):
            dataframe = filter_pending_orders_after_batch(
                dataframe,
                job["results"],
                submitted_packages=job.get("orders") or [],
            )
    logs = _safe_operational_log_lines(
        payload.logs,
        sensitive_values=_dataframe_sensitive_values(dataframe),
    )
    st.session_state["last_pending_df"] = dataframe
    st.session_state["last_pending_logs"] = logs
    st.session_state["last_pending_loaded_at"] = result.status.loaded_at
    st.session_state["last_pending_read_summary"] = summarize_pending_read_logs(logs)
    st.session_state["last_pending_error"] = error_code
    return True


def _pending_refresh_warning_message() -> str:
    if not st.session_state.get("pending_refresh_error_code"):
        return ""
    if isinstance(st.session_state.get("last_pending_df"), pd.DataFrame):
        return "暫時無法取得最新資料，目前顯示上次成功讀取的內容。"
    return "目前無法取得待製郵便運單資料，請稍後重新讀取。"


if not hasattr(st, "fragment"):
    st.fragment = lambda **_kwargs: (lambda function: function)


@st.fragment(run_every="20m")
def _active_refresh_tick(*, is_busy: bool, job) -> None:
    before_pending = st.session_state.get("last_pending_loaded_at")
    before_picking = st.session_state.get("picking_snapshot_loaded_at")
    pending_applied = False
    picking_applied = False

    try:
        pending_result = _refresh_source("pending", force=False)
        pending_applied = _apply_pending_result(
            pending_result,
            is_busy=is_busy,
            allow_dirty_reset=False,
            job=job,
        )
    except Exception:
        pending_result = None
        st.session_state["pending_refresh_error_code"] = "unavailable"

    try:
        picking_result = _refresh_source("picking", force=False)
        if picking_result.data is not None:
            apply_picking_payload(
                picking_result.data,
                preserve_selection=True,
                loaded_at=picking_result.status.loaded_at,
            )
            st.session_state["picking_snapshot_loaded_at"] = picking_result.status.loaded_at
            picking_applied = True
    except Exception:
        picking_result = None

    changed = (
        pending_applied
        and pending_result is not None
        and pending_result.status.loaded_at != before_pending
    ) or (
        picking_applied
        and picking_result is not None
        and picking_result.status.loaded_at != before_picking
    )
    if changed:
        try:
            st.rerun(scope="app")
        except TypeError:
            st.rerun()


def _visible_pending_logs(logs: list[str], *, sensitive_values=()) -> list[str]:
    safe_logs = _safe_operational_log_lines(
        logs,
        sensitive_values=sensitive_values,
    )
    return [
        line
        for line in safe_logs
        if "關注訂單診斷（WhoWhy/WhoWht）" not in line
        and "關注訂單診斷(WhoWhy/WhoWht)" not in line
    ]


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


def _order_id_for_position(row: pd.Series, position: int) -> str:
    return str(row.get("注文番号(貼上原始資料)", row.get("Order No.", ""))).strip() or f"row-{position + 1}"


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


def _pending_data_warning_lines(df: pd.DataFrame) -> list[str]:
    warnings: list[str] = []
    if not isinstance(df, pd.DataFrame):
        return warnings
    for _, row in df.iterrows():
        warnings.extend(pending_order_warning_lines(row))
    return warnings


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


def _v2_reset_key_for(order_id: str) -> str:
    return f"pending_v2_reset_{order_id}"


def _v2_reset_version(order_id: str) -> int:
    return int(st.session_state.get(_v2_reset_key_for(order_id), 0))


def _v2_reset_order_editor(order_id: str) -> None:
    st.session_state[_v2_reset_key_for(order_id)] = _v2_reset_version(order_id) + 1
    _v2_selected_by_order_state()[order_id] = True


def _v2_reset_all_order_editors(df: pd.DataFrame, editable_count: int) -> None:
    for position, (_, row) in enumerate(df.iloc[:editable_count].iterrows()):
        order_id = _order_id_for_position(row, position)
        if order_id:
            _v2_reset_order_editor(order_id)


def _v2_name_key_for(position: int, order_id: str, reset_version: int) -> str:
    return f"pending_v2_name_{position}_{order_id}_{reset_version}"


def _v2_prc_id_key_for(position: int, order_id: str, reset_version: int) -> str:
    return f"pending_v2_prc_id_{position}_{order_id}_{reset_version}"


def _v2_pccc_key_for(position: int, order_id: str, reset_version: int) -> str:
    return f"pending_v2_pccc_{position}_{order_id}_{reset_version}"


def _v2_selected_key_for(position: int, order_id: str, reset_version: int) -> str:
    return f"pending_v2_selected_{position}_{order_id}_{reset_version}"


def _v2_item_key_for(position: int, order_id: str, reset_version: int) -> str:
    return f"pending_v2_items_{position}_{order_id}_{reset_version}"


def _v2_trans_key_for(position: int, order_id: str, reset_version: int) -> str:
    return f"pending_v2_trans_{position}_{order_id}_{reset_version}"


def _v2_extra_trans_key_for(position: int, order_id: str, reset_version: int) -> str:
    return f"pending_v2_extra_trans_{position}_{order_id}_{reset_version}"


def _v2_selected_by_order_state() -> dict[str, bool]:
    selected_by_order = st.session_state.get("pending_v2_selected_by_order")
    if not isinstance(selected_by_order, dict):
        selected_by_order = {}
        st.session_state["pending_v2_selected_by_order"] = selected_by_order
    return selected_by_order


def _v2_is_order_selected(order_id: str) -> bool:
    return bool(_v2_selected_by_order_state().get(order_id, True))


def _sync_v2_order_selected_from_widget(order_id: str, widget_key: str) -> None:
    _v2_selected_by_order_state()[order_id] = bool(st.session_state.get(widget_key, True))


def _initialize_v2_order_selected_widget(order_id: str, widget_key: str) -> None:
    selected_by_order = _v2_selected_by_order_state()
    if order_id not in selected_by_order:
        selected_by_order[order_id] = bool(st.session_state.get(widget_key, True))
    st.session_state[widget_key] = bool(selected_by_order.get(order_id, True))


def _sync_visible_v2_order_selection_from_widgets(df_pending: pd.DataFrame, editable_count: int) -> None:
    selected_by_order = _v2_selected_by_order_state()
    for position in range(editable_count):
        row = df_pending.iloc[position]
        order_id = _order_id_for_position(row, position)
        reset_version = _v2_reset_version(order_id)
        widget_key = _v2_selected_key_for(position, order_id, reset_version)
        if widget_key in st.session_state:
            selected_by_order[order_id] = bool(st.session_state.get(widget_key))


def _v2_order_ids(df_pending: pd.DataFrame) -> list[str]:
    return [
        _order_id_for_position(df_pending.iloc[position], position)
        for position in range(len(df_pending))
    ]


def _v2_selected_source_indices_from_state(df_pending: pd.DataFrame) -> list[object]:
    selected_indices: list[object] = []
    for position, source_index in enumerate(df_pending.index):
        row = df_pending.iloc[position]
        order_id = _order_id_for_position(row, position)
        if _v2_is_order_selected(order_id):
            selected_indices.append(source_index)
    return selected_indices


def _v2_extra_trans_types_by_index_from_state(
    df_pending: pd.DataFrame,
    editable_count: int,
) -> dict[object, list[str]]:
    extra_trans_types: dict[object, list[str]] = {}
    for position, source_index in enumerate(df_pending.index[:editable_count]):
        row = df_pending.iloc[position]
        order_id = _order_id_for_position(row, position)
        reset_version = _v2_reset_version(order_id)
        extra_key = _v2_extra_trans_key_for(position, order_id, reset_version)
        selected = st.session_state.get(extra_key, "無")
        if isinstance(selected, str):
            extra_trans_types[source_index] = [] if selected == "無" else [selected]
        elif isinstance(selected, (list, tuple)):
            extra_trans_types[source_index] = [str(value) for value in selected]
    return extra_trans_types


def _build_pending_run_frame_from_v2_state(
    df_pending: pd.DataFrame,
    editable_count: int,
    usd_jpy_rate: float | None,
) -> pd.DataFrame:
    edited_summary_rows: list[dict[str, str]] = []
    edited_items_by_position: dict[int, pd.DataFrame] = {}
    for position in range(editable_count):
        row = df_pending.iloc[position]
        order_id = _order_id_for_position(row, position)
        country = str(row.get("收件人國家", row.get("Country", ""))).strip()
        parsed_name = parse_shipping_name(row.get("Shipping Name", row.get("Shipping Name_1", "")))
        default_trans_type = str(row.get(SHIPPING_COL, "")).strip()
        reset_version = _v2_reset_version(order_id)
        item_key = _v2_item_key_for(position, order_id, reset_version)
        item_frame = build_v2_item_display_frame(build_pending_item_frame(row))
        edited_display_frame = _apply_data_editor_state(item_frame, item_key)
        edited_items_by_position[position] = restore_v2_item_frame(edited_display_frame)
        trans_key = _v2_trans_key_for(position, order_id, reset_version)
        name_key = _v2_name_key_for(position, order_id, reset_version)
        prc_id_key = _v2_prc_id_key_for(position, order_id, reset_version)
        pccc_key = _v2_pccc_key_for(position, order_id, reset_version)
        _sync_recipient_id_session_fields(name_key, prc_id_key, pccc_key)
        edited_name = st.session_state.get(name_key, parsed_name["clean_name"])
        edited_prc_id = st.session_state.get(prc_id_key, parsed_name["prc_id"])
        edited_pccc = st.session_state.get(pccc_key, parsed_name["pccc"])
        edited_summary_rows.append(
            {
                "Order No.": order_id,
                "Name": compose_shipping_name(edited_name, country, edited_prc_id, edited_pccc),
                "Country": country,
                "TransType": st.session_state.get(trans_key, default_trans_type),
                "TotalValue(USD)": "",
                "TotalValue(JPY)": "",
            }
        )
    if not edited_summary_rows:
        edited = df_pending.copy()
    else:
        edited = apply_pending_order_editor_values(
            df_pending,
            pd.DataFrame(edited_summary_rows),
            edited_items_by_position,
            usd_jpy_rate=usd_jpy_rate,
        )
    selected_indices = _v2_selected_source_indices_from_state(df_pending)
    if not selected_indices:
        return edited.iloc[0:0].copy()
    selected = edited.loc[selected_indices].copy()
    return expand_pending_orders_for_trans_types(
        selected,
        _v2_extra_trans_types_by_index_from_state(df_pending, editable_count),
    )


def _retry_writeback_results(job):
    candidates = writeback_retry_candidates((job or {}).get("results"))
    if not candidates:
        return {
            "ok": False,
            "message": "運單可能已產生但回填紀錄不足，請提供既有追跡番号後再回填。",
        }
    outcome = backfill_results(candidates)
    terminal_status = apply_writeback_outcome(job, candidates, outcome)
    _JOB_REGISTRY.finish(job, terminal_status)
    return {
        "ok": outcome["ok"],
        "message": "回填已完成" if outcome["ok"] else "仍有資料需要確認",
    }


def _render_postal_pending_v2(
    *,
    email: str,
    df_pending: pd.DataFrame,
    pending_logs: list[str],
    rate: float | None,
    rate_date: str,
    job: dict | None,
    is_running: bool,
    is_launching: bool,
    is_busy: bool,
    pending_count: int,
    done: int,
    batch_summary: dict,
) -> None:
    """Render the isolated v2 postal page using the existing job pipeline."""
    st.markdown('<span class="postal-v2-page-marker"></span>', unsafe_allow_html=True)
    editable_count = min(len(df_pending), 20)
    if not is_busy and not df_pending.empty:
        _sync_visible_v2_order_selection_from_widgets(df_pending, editable_count)

    if is_running and job:
        selected_order_ids = {
            str(order.get("order_id", "")).strip()
            for order in (job.get("orders") or [])
            if str(order.get("order_id", "")).strip()
        }
        selected_count = len(selected_order_ids)
        df_pending_for_run = df_pending
    elif df_pending.empty:
        selected_count = 0
        df_pending_for_run = df_pending
    else:
        selected_count = sum(_v2_is_order_selected(order_id) for order_id in _v2_order_ids(df_pending))
        df_pending_for_run = _build_pending_run_frame_from_v2_state(df_pending, editable_count, rate)

    zero_value_warnings = _zero_value_warning_lines(df_pending_for_run)
    required_id_warnings = _required_id_warning_lines(df_pending_for_run)
    pending_data_warnings = _pending_data_warning_lines(df_pending_for_run)

    with st.container():
        left_panel, right_panel = st.columns([3.05, 1.2], gap="medium", vertical_alignment="top")

        with right_panel:
            with st.container(border=True):
                st.markdown('<span class="postal-v2-operation-panel"></span>', unsafe_allow_html=True)
                rate_cols = st.columns([1.0, 1.35], gap="small", vertical_alignment="center")
                with rate_cols[0]:
                    st.markdown('<div class="postal-v2-panel-heading">匯率與進度</div>', unsafe_allow_html=True)
                with rate_cols[1]:
                    st.markdown(
                        '<div class="postal-v2-rate-badge">'
                        f'{html.escape(format_secondary_rate_badge(rate, rate_date))}'
                        '</div>',
                        unsafe_allow_html=True,
                    )

                metric_cols = st.columns(3, gap="small", vertical_alignment="center")
                metric_values = [
                    ("待製單", pending_count, "postal-v2-metric-pending"),
                    ("已選取", selected_count, "postal-v2-metric-selected"),
                    ("本次完成", done, "postal-v2-metric-completed"),
                ]
                for column, (label, value, class_name) in zip(metric_cols, metric_values):
                    with column:
                        st.markdown(
                            f'<div class="postal-v2-metric {class_name}">'
                            f'<span>{label}</span><strong>{value}</strong></div>',
                            unsafe_allow_html=True,
                        )

                st.markdown('<div class="postal-v2-section-heading">選取操作</div>', unsafe_allow_html=True)
                batch_action_cols = st.columns(2, gap="small")
                current_order_ids = _v2_order_ids(df_pending)
                with batch_action_cols[0]:
                    select_all_requested = st.button(
                        "選取全部",
                        type="primary",
                        key="postal_v2_select_all",
                        width="stretch",
                        disabled=is_busy or df_pending.empty,
                    )
                with batch_action_cols[1]:
                    clear_all_requested = st.button(
                        "清除全部",
                        key="postal_v2_clear_all",
                        width="stretch",
                        disabled=is_busy or df_pending.empty,
                    )
                if select_all_requested:
                    st.session_state["pending_v2_selected_by_order"] = apply_batch_selection(
                        _v2_selected_by_order_state(), current_order_ids, "select_all"
                    )
                    st.rerun()
                if clear_all_requested:
                    st.session_state["pending_v2_selected_by_order"] = apply_batch_selection(
                        _v2_selected_by_order_state(), current_order_ids, "clear_all"
                    )
                    st.rerun()
                st.markdown(
                    f'<div class="postal-v2-selection-count"><strong>{selected_count} / {pending_count}</strong> 筆已選取</div>',
                    unsafe_allow_html=True,
                )

                st.markdown('<div class="postal-v2-panel-divider"></div>', unsafe_allow_html=True)
                max_rows_input = st.number_input(
                    "最大處理",
                    min_value=0,
                    max_value=500,
                    value=20,
                    step=1,
                    disabled=is_busy,
                    key="postal_v2_max_rows",
                )
                max_rows_val: int | None = None if max_rows_input == 0 else int(max_rows_input)
                start_requested = st.button(
                    "開始製單",
                    type="primary",
                    width="stretch",
                    key="postal_v2_start_job",
                    disabled=(
                        is_busy
                        or bool(st.session_state.get("postal_batch_view_active"))
                        or pending_count == 0
                        or selected_count == 0
                        or bool(zero_value_warnings)
                        or bool(required_id_warnings)
                        or bool(pending_data_warnings)
                    ),
                )
                reload_requested = st.button(
                    "重新讀取",
                    width="stretch",
                    key="postal_v2_reload_pending",
                    disabled=is_busy,
                )
                reset_all_requested = st.button(
                    "全部恢復預設資料",
                    width="stretch",
                    key="postal_v2_reset_all_pending",
                    disabled=is_busy or df_pending.empty,
                )

                if reload_requested:
                    pending_result = _refresh_source("pending", force=True)
                    if _apply_pending_result(
                        pending_result,
                        is_busy=is_busy,
                        allow_dirty_reset=False,
                        job=job,
                    ):
                        _reset_preflight_job_view(job)
                        st.session_state.pop("postal_batch_view_active", None)
                        st.session_state.pop("pending_refresh_warning", None)
                    else:
                        st.session_state["pending_refresh_warning"] = (
                            "目前有尚未送出的編輯，已保留畫面資料。"
                        )
                    st.rerun()
                if reset_all_requested:
                    pending_result = _refresh_source("pending", force=True)
                    if _apply_pending_result(
                        pending_result,
                        is_busy=is_busy,
                        allow_dirty_reset=True,
                        job=job,
                    ):
                        _reset_preflight_job_view(job)
                        st.session_state.pop("postal_batch_view_active", None)
                        st.session_state["pending_editor_dirty"] = False
                        st.session_state.pop("pending_v2_selected_by_order", None)
                        _clear_pending_editor_keys()
                        st.rerun()

                if start_requested:
                    if df_pending.empty:
                        st.warning("沒有符合條件的待打單資料")
                    elif df_pending_for_run.empty:
                        st.warning("目前未選取任何訂單")
                    else:
                        ok, reason = _start_job(email, df_pending_for_run, max_rows_val)
                        if ok:
                            st.session_state["postal_batch_view_active"] = True
                            st.session_state["job_launching"] = True
                            st.session_state["job_launching_started_at"] = time.time()
                            if hasattr(st, "toast"):
                                st.toast("已啟動自動製單")
                            st.rerun()
                        elif reason == "batch_running":
                            st.error("同一批製單已在執行中，已阻止重複啟動。")
                        else:
                            st.error("任務執行中，請稍候")

                pending_loaded_at = st.session_state.get("last_pending_loaded_at")
                if isinstance(pending_loaded_at, datetime):
                    st.caption(f"資料更新於 {format_jst(pending_loaded_at, '%H:%M')}")
                refresh_error_message = _pending_refresh_warning_message()
                if refresh_error_message:
                    st.warning(refresh_error_message)
                elif st.session_state.get("pending_refresh_warning"):
                    st.warning(st.session_state["pending_refresh_warning"])
                if not rate and not df_pending.empty:
                    st.warning(
                        "暫時無法取得 USD/JPY 匯率；若編輯 Value 或 Quantity，"
                        "TotalValue(JPY) 會保留來源預設值。"
                    )
                if zero_value_warnings:
                    st.error("有品項 Value 為 0，請先修正：" + "；".join(zero_value_warnings[:5]))
                if required_id_warnings:
                    st.error("；".join(required_id_warnings[:5]))
                if pending_data_warnings:
                    st.error("資料需要先修正：" + "；".join(pending_data_warnings[:8]))

                st.markdown(
                    '<div class="postal-v2-legend">'
                    '<span class="postal-v2-legend-editable">藍框：可編輯</span>'
                    '<span class="postal-v2-legend-readonly">灰字：僅顯示／系統計算</span>'
                    '</div>',
                    unsafe_allow_html=True,
                )
                st.markdown(
                    '<div class="postal-v2-current-value-note">開始製單時採用目前畫面內容</div>',
                    unsafe_allow_html=True,
                )

        with left_panel:
            batch_view_active = bool(st.session_state.get("postal_batch_view_active"))
            show_editor_cards = not is_busy and not batch_view_active
            if df_pending.empty:
                st.info("目前沒有待製單資料。")
            elif is_busy:
                running_orders = pd.DataFrame((job or {}).get("orders") or [])
                if not running_orders.empty:
                    run_cols = [
                        "position",
                        "order_id",
                        "recipient",
                        "country",
                        "trans_type",
                        "total_usd",
                        "total_jpy",
                    ]
                    run_cols = [column for column in run_cols if column in running_orders.columns]
                    running_preview = running_orders[run_cols].rename(
                        columns={
                            "position": "#",
                            "order_id": "注文番号",
                            "recipient": "收件人",
                            "country": "國家",
                            "trans_type": "TransType",
                            "total_usd": "USD",
                            "total_jpy": "JPY",
                        }
                    )
                    st.caption("本次送出製單")
                    st.dataframe(running_preview, hide_index=True, width="stretch")
                else:
                    st.info("製單任務啟動中，正在建立執行狀態。")
                if job:
                    _render_running_progress(job)
            elif batch_view_active:
                submitted_orders = pd.DataFrame((job or {}).get("orders") or [])
                if not submitted_orders.empty:
                    run_cols = [
                        "position",
                        "order_id",
                        "recipient",
                        "country",
                        "trans_type",
                        "total_usd",
                        "total_jpy",
                    ]
                    run_cols = [column for column in run_cols if column in submitted_orders.columns]
                    submitted_preview = submitted_orders[run_cols].rename(
                        columns={
                            "position": "#",
                            "order_id": "注文番号",
                            "recipient": "收件人",
                            "country": "國家",
                            "trans_type": "TransType",
                            "total_usd": "USD",
                            "total_jpy": "JPY",
                        }
                    )
                    st.caption("本次送出製單")
                    st.dataframe(submitted_preview, hide_index=True, width="stretch")
                else:
                    st.info("本次製單列表正在整理。")
            elif show_editor_cards:
                edited_summary_rows: list[dict[str, str]] = []
                edited_items_by_position: dict[int, pd.DataFrame] = {}
                for position in range(editable_count):
                    row = df_pending.iloc[position]
                    order_id = _order_id_for_position(row, position)
                    country = str(row.get("收件人國家", row.get("Country", ""))).strip()
                    kind = country_kind(country)
                    parsed_name = parse_shipping_name(row.get("Shipping Name", row.get("Shipping Name_1", "")))
                    default_trans_type = str(row.get(SHIPPING_COL, "")).strip()
                    reset_version = _v2_reset_version(order_id)
                    item_key = _v2_item_key_for(position, order_id, reset_version)
                    item_frame = build_v2_item_display_frame(build_pending_item_frame(row))
                    summary_item_frame = restore_v2_item_frame(_apply_data_editor_state(item_frame, item_key))
                    trans_key = _v2_trans_key_for(position, order_id, reset_version)
                    name_key = _v2_name_key_for(position, order_id, reset_version)
                    prc_id_key = _v2_prc_id_key_for(position, order_id, reset_version)
                    pccc_key = _v2_pccc_key_for(position, order_id, reset_version)
                    selected_key = _v2_selected_key_for(position, order_id, reset_version)
                    extra_trans_key = _v2_extra_trans_key_for(position, order_id, reset_version)
                    _sync_recipient_id_session_fields(name_key, prc_id_key, pccc_key)
                    pending_trans = st.session_state.get(trans_key, default_trans_type)
                    pending_name = st.session_state.get(name_key, parsed_name["clean_name"])
                    pending_prc_id = st.session_state.get(prc_id_key, parsed_name["prc_id"])
                    pending_pccc = st.session_state.get(pccc_key, parsed_name["pccc"])
                    composed_name_preview = compose_shipping_name(
                        pending_name,
                        country,
                        pending_prc_id,
                        pending_pccc,
                    )
                    preview_df = apply_pending_order_editor_values(
                        df_pending.iloc[[position]],
                        pd.DataFrame(
                            [
                                {
                                    "Order No.": order_id,
                                    "Name": composed_name_preview,
                                    "Country": country,
                                    "TransType": pending_trans,
                                    "TotalValue(USD)": "",
                                    "TotalValue(JPY)": "",
                                }
                            ]
                        ),
                        {0: summary_item_frame},
                        usd_jpy_rate=rate,
                    )
                    summary_row = build_pending_summary_frame(preview_df).iloc[0]

                    with st.container(border=True):
                        st.markdown('<span class="postal-v2-card-marker"></span>', unsafe_allow_html=True)
                        info_cols = st.columns(
                            [.58, 1.75, 2.45, .86, .86, 1.0],
                            gap="small",
                            vertical_alignment="center",
                        )
                        with info_cols[0]:
                            _initialize_v2_order_selected_widget(order_id, selected_key)
                            st.checkbox(
                                "製單",
                                key=selected_key,
                                on_change=_sync_v2_order_selected_from_widget,
                                args=(order_id, selected_key),
                            )
                        with info_cols[1]:
                            st.markdown(_native_info("Order No.", order_id), unsafe_allow_html=True)
                        with info_cols[2]:
                            st.markdown(_native_info("Country", summary_row["Country"]), unsafe_allow_html=True)
                        with info_cols[3]:
                            st.markdown(_native_info("USD", summary_row["TotalValue(USD)"]), unsafe_allow_html=True)
                        with info_cols[4]:
                            st.markdown(_native_info("JPY", summary_row["TotalValue(JPY)"]), unsafe_allow_html=True)

                        if kind in {"china", "korea"}:
                            # Keep both select columns at 1.8 (1.5x the previous 1.2 ratio)
                            # while retaining room for country-specific ID fields.
                            action_cols = st.columns(
                                [1.2, 1.8, 1.8, 1.1, 1.2, .7],
                                gap="small",
                                vertical_alignment="center",
                            )
                        else:
                            action_cols = st.columns(
                                [1.42, 1.8, 1.8, 1.25, .9],
                                gap="small",
                                vertical_alignment="center",
                            )
                        with action_cols[0]:
                            edited_name = st.text_input(
                                "姓名",
                                value=pending_name,
                                key=name_key,
                                on_change=_mark_pending_editor_dirty,
                            )
                        with action_cols[1]:
                            trans_type = st.selectbox(
                                "寄送方式",
                                options=SHIPPING_OPTIONS,
                                index=SHIPPING_OPTIONS.index(default_trans_type)
                                if default_trans_type in SHIPPING_OPTIONS
                                else 0,
                                key=trans_key,
                                on_change=_mark_pending_editor_dirty,
                            )
                        extra_options = ["無"] + SHIPPING_OPTIONS
                        if st.session_state.get(extra_trans_key, "無") not in extra_options:
                            st.session_state[extra_trans_key] = "無"
                        if extra_trans_key not in st.session_state:
                            st.session_state[extra_trans_key] = "無"
                        with action_cols[2]:
                            st.selectbox(
                                "追加製作",
                                options=extra_options,
                                key=extra_trans_key,
                                on_change=_mark_pending_editor_dirty,
                            )
                        edited_prc_id = pending_prc_id
                        edited_pccc = pending_pccc
                        if kind == "china":
                            with action_cols[3]:
                                edited_prc_id = st.text_input(
                                    "PRC ID",
                                    value=pending_prc_id,
                                    key=prc_id_key,
                                    on_change=_mark_pending_editor_dirty,
                                )
                        elif kind == "korea":
                            with action_cols[3]:
                                edited_pccc = st.text_input(
                                    "PCCC",
                                    value=pending_pccc,
                                    key=pccc_key,
                                    on_change=_mark_pending_editor_dirty,
                                )
                        with action_cols[-1]:
                            if st.button(
                                "恢復預設",
                                key=f"postal_v2_reset_order_{position}_{order_id}_{reset_version}",
                                width="stretch",
                            ):
                                _v2_reset_order_editor(order_id)
                                st.rerun()

                        edited_summary_rows.append(
                            {
                                "Order No.": order_id,
                                "Name": compose_shipping_name(edited_name, country, edited_prc_id, edited_pccc),
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
                        edited_display_frame = st.data_editor(
                            item_frame,
                            hide_index=True,
                            width="stretch",
                            num_rows="fixed",
                            disabled=["No."],
                            column_config={
                                "No.": st.column_config.TextColumn("No.", width=54),
                                "Description": st.column_config.TextColumn("品名 / 描述", width="large"),
                                "HSCode": st.column_config.TextColumn("HS Code", width=120),
                                "Value": st.column_config.TextColumn("申報價值（USD）", width=120),
                                "Quantity": st.column_config.TextColumn("數量", width=90),
                            },
                            key=item_key,
                            on_change=_mark_pending_editor_dirty,
                        )
                        edited_items_by_position[position] = restore_v2_item_frame(edited_display_frame)

                if len(df_pending) > editable_count:
                    st.caption(f"目前可編輯前 {editable_count} 筆；其餘訂單會保留來源表資料。")

        if job and job.get("results") and not is_busy:
            retry_notice = st.session_state.pop("pending_v2_writeback_retry_notice", None)
            if retry_notice == "回填已完成":
                st.success(retry_notice)
            elif retry_notice:
                st.warning(retry_notice)

            if batch_summary["failure_groups"]:
                batch_abort_count = int((job or {}).get("batch_preflight_blocked_count") or 0)
                uncompleted_count = (
                    batch_summary["failed_count"]
                    + batch_summary["skipped_count"]
                    + batch_abort_count
                )
                if (job or {}).get("preflight_reload_required"):
                    st.warning(
                        (job or {}).get("preflight_reload_message")
                        or "本批未開始製單，請按「重新讀取」後重新選取。"
                    )
                else:
                    st.warning(
                        f"本批完成 {batch_summary['completed_count']} 筆，"
                        f"未完成 {uncompleted_count} 筆。"
                    )
                with st.expander(
                    f"查看未完成原因（{len(batch_summary['failure_alerts'])} 筆）",
                    expanded=False,
                ):
                    for alert in batch_summary["failure_groups"]:
                        st.error(alert)
            elif st.session_state.get("pending_refresh_notice"):
                st.success(
                    f"製單完成：本次完成 {batch_summary['completed_count']} 筆。"
                    "為避免 Google Sheets 讀取配額過高，目前沿用快取清單；需要最新待製單資料請按「重新讀取」。"
                )

            retry_candidates = writeback_retry_candidates(job.get("results"))
            missing_tracking_failures = [
                result
                for result in job.get("results") or []
                if str(result.get("status") or "").strip().lower() == "backfill_failed"
                and not str(result.get("tracking") or "").strip()
            ]
            if retry_candidates:
                if st.button("重新回填資料", key="pending_v2_retry_writeback"):
                    retry_result = _retry_writeback_results(job)
                    st.session_state["pending_v2_writeback_retry_notice"] = retry_result["message"]
                    st.rerun()
            if missing_tracking_failures:
                st.warning("運單可能已產生但回填紀錄不足，請提供既有追跡番号後再回填。")

        if job and job.get("orders"):
            st.markdown('<div class="postal-v2-status-heading">製單狀態</div>', unsafe_allow_html=True)
            status_label = {
                "queued": "待機中",
                "running": "製單中",
                "success": "完成",
                "completed": "完成",
                "failed": "需排查",
                "skipped": "略過",
                "blocked": "未製單",
            }
            df_status = pd.DataFrame(job["orders"])
            df_status["status"] = df_status["status"].map(status_label).fillna(df_status["status"])
            df_status = df_status.rename(
                columns={
                    "position": "#",
                    "order_id": "注文番号",
                    "recipient": "收件人",
                    "country": "國家",
                    "trans_type": "TransType",
                    "status": "狀態",
                    "stage": "階段",
                    "tracking_no": "貨運單號",
                    "hs_codes": "HSCode",
                    "message": "訊息",
                }
            )
            if "HSCode" not in df_status.columns:
                df_status["HSCode"] = ""
            show_cols = [
                "#",
                "注文番号",
                "收件人",
                "國家",
                "TransType",
                "狀態",
                "階段",
                "貨運單號",
                "HSCode",
                "訊息",
            ]
            st.dataframe(df_status[show_cols], hide_index=True, width="stretch")

        if job and job.get("logs") and batch_summary["failure_alerts"]:
            with st.expander("詳細除錯日誌", expanded=False):
                st.markdown('<span class="debug-log-marker"></span>', unsafe_allow_html=True)
                safe_job_logs = _safe_operational_log_lines(
                    job["logs"][-200:],
                    sensitive_values=_job_sensitive_values(job, email=email),
                )
                st.code("\n".join(safe_job_logs), language="text")


def _render_running_progress(job: dict) -> None:
    progress = summarize_job_progress(job)
    total = progress["total"]
    done = progress["done"]
    active_order = progress["active_order_id"] or "準備中"
    active_stage = progress["active_stage"] or "等待下一步"
    st.markdown(
        '<div class="running-panel">'
        f'<div class="running-title">製單執行中｜{done}/{total}</div>'
        f'<div class="running-detail">目前處理：{html.escape(active_order)}｜{html.escape(active_stage)}</div>'
        '</div>',
        unsafe_allow_html=True,
    )
    st.progress(float(progress["ratio"]), text=f"製單進度 {done}/{total}")
    latest_logs = (job.get("logs") or [])[-3:]
    if latest_logs:
        st.caption("最新狀態：" + "　".join(html.escape(line) for line in latest_logs))


def _render_blocking_running_guard(job: dict | None, launching: bool = False) -> None:
    progress = summarize_job_progress(job)
    total = progress["total"]
    done = progress["done"]
    active_order = progress["active_order_id"] or "準備中"
    active_stage = progress["active_stage"] or ("啟動製單任務" if launching else "建立製單任務")
    title = "製單啟動中" if launching and not job else "製單進行中"
    count_text = f"｜{done}/{total}" if total else ""
    st.markdown(
        '<div class="running-guard-overlay">'
        '<div class="running-guard-box">'
        f'<div class="running-guard-title">{title}{count_text}</div>'
        f'<div class="running-guard-text">目前處理：{html.escape(active_order)}｜{html.escape(active_stage)}</div>'
        '<div class="running-guard-sub">已鎖定操作以避免重複製單，畫面會自動更新。</div>'
        '</div></div>',
        unsafe_allow_html=True,
    )


def _native_info(label: str, value: str) -> str:
    if label == "Order No.":
        label_class = "native-info-order"
    elif label == "Country":
        label_class = "native-info-country"
    else:
        label_class = "native-info-standard"
    return (
        f'<div class="native-info {label_class}">'
        f'<span class="native-info-label">{html.escape(label)}</span>'
        f'<span class="native-info-value">{html.escape(str(value))}</span>'
        '</div>'
    )


def _start_job(email: str, df: pd.DataFrame, max_rows: int | None) -> tuple[bool, str]:
    ok, job, reason = _JOB_REGISTRY.start(email, df, max_rows)
    if not ok or job is None:
        return False, reason
    _write_job_lock(email)

    def _run():
        def _log(msg: str):
            try:
                update_order_status_from_log(job, msg)
                safe_message = redact_operational_log(
                    msg,
                    sensitive_values=_job_sensitive_values(
                        job,
                        dataframe=df,
                        email=email,
                    ),
                )
                ts = time.strftime("%H:%M:%S")
                entry = f"[{ts}] {safe_message}"
                print(f"[BOT] {entry}", file=sys.stderr, flush=True)
                job["logs"].append(entry)
            except Exception as log_err:
                print(
                    f"[LOG_ERR] {type(log_err).__name__}",
                    file=sys.stderr,
                    flush=True,
                )

        def _status(event):
            try:
                update_order_status_from_event(job, event)
            except Exception:
                safe_log_event(
                    _log,
                    "job_exception",
                    error_type="StatusEventError",
                )

        try:
            rows_for_run = df if max_rows is None else df.head(max_rows)
            from bot.sheets import (
                COUNTRY_CODE_MAP,
                get_pending_orders,
                read_completion_authority,
            )

            _log("🔐 製單前重新確認 Google Sheets 完成狀態與來源快照...")
            try:
                completion_authority = read_completion_authority()
            except Exception as preflight_error:
                reason_text = "無法確認完成狀態，這批未開始製單。"
                preflight_results = [
                    {
                        "order_id": str(order.get("order_id") or "").strip(),
                        "trans_type": str(order.get("trans_type") or "").strip(),
                        "shipment_role": str(order.get("shipment_role") or "primary").strip(),
                        "status": "blocked",
                        "reason_code": "target_read_error",
                        "reason_text": reason_text,
                        "message": reason_text,
                    }
                    for order in job.get("orders") or []
                ]
                job["results"] = preflight_results
                job["preflight_reload_required"] = True
                job["preflight_reload_message"] = (
                    "本批未開始製單，無法確認完成狀態；請按「重新讀取」後再試。"
                )
                mark_results_failed(job, preflight_results)
                safe_log_event(
                    _log,
                    "preflight_blocked",
                    error_type=type(preflight_error).__name__,
                )
                _JOB_REGISTRY.finish(job, "error")
                _clear_job_lock(email)
                return

            latest_pending_df = get_pending_orders(
                log_cb=_log,
                strict=True,
                exclude_completed=False,
            )
            preflight_checks = preflight_batch_orders(
                rows_for_run,
                latest_pending_df,
                completion_authority,
            )
            job["preflight_checks"] = preflight_checks
            ready_rows, already_completed_items, hard_blocked_items = partition_preflight_rows(
                rows_for_run,
                preflight_checks,
            )
            preflight_completed_results = [
                {
                    **check,
                    "status": "completed",
                    "message": check.get("reason_text", ""),
                }
                for check in already_completed_items
            ]
            preflight_blocked_results = [
                    {
                        **check,
                        "status": "blocked",
                        "reason_code": check.get("reason_code", "source_changed"),
                        "reason_text": check.get("reason_text", "製單前檢查未通過"),
                        "message": check.get("reason_text", "製單前檢查未通過"),
                    }
                    for check in hard_blocked_items
            ]
            initial_results = preflight_completed_results + preflight_blocked_results
            job["results"] = list(initial_results)
            if preflight_completed_results:
                mark_results_completed(job, preflight_completed_results)
            if preflight_blocked_results:
                mark_results_failed(job, preflight_blocked_results)
                job["preflight_reload_required"] = True
                job["preflight_reload_message"] = (
                    "本批未開始製單：選取後資料已更新或檢查未通過，"
                    "請按「重新讀取」後重新選取。"
                )
                safe_log_event(
                    _log,
                    "preflight_blocked",
                    count=len(preflight_blocked_results),
                )
                aborted_results = [
                    {
                        "order_id": str(row.get("order_id") or row.get("注文番号(貼上原始資料)") or "").strip(),
                        "trans_type": str(row.get("trans_type") or row.get("TransType") or row.get("郵局運送方式(複數商品請自行確認是否走小包)") or "").strip(),
                        "shipment_role": str(row.get("shipment_role") or row.get("_shipment_role") or "primary").strip(),
                        "status": "blocked",
                        "reason_code": "batch_preflight_blocked",
                        "reason_text": "本批含未通過製單前檢查的包裹，整批停止。",
                        "message": "本批含未通過製單前檢查的包裹，整批停止。",
                    }
                    for _, row in ready_rows.iterrows()
                ]
                # Keep the executable rows out of structured results. They were
                # never submitted; recording one synthetic result per row only
                # duplicates the same batch-level stop reason in the UI.
                job["batch_preflight_blocked_count"] = len(aborted_results)
                mark_results_failed(job, aborted_results)
                _JOB_REGISTRY.finish(job, "error")
                _clear_job_lock(email)
                return
            if ready_rows.empty:
                terminal_status = (
                    "completed" if preflight_completed_results and not preflight_blocked_results
                    else "partial_failure"
                )
                _JOB_REGISTRY.finish(job, terminal_status)
                if preflight_completed_results:
                    job["pending_refresh_needed"] = True
                _clear_job_lock(email)
                return

            rows_for_run = ready_rows

            _log("🚀 任務啟動，正在載入模組...")
            _log("🧰 正在準備 Playwright Chromium 環境...")
            _install_playwright()
            from bot.automation import AUTOMATION_BUILD_ID, _prepare_batch_hs_codes, run_automation
            _log(f"🧭 automation build: {AUTOMATION_BUILD_ID}")

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
                rows_for_run,
                max_rows=None,
                log_cb=_log,
                status_cb=_status,
                headless=True,
                precomputed_hs_codes=hs_codes_by_order,
            )
            if not results:
                _log("ℹ️ 自動化完成，無新增結果。")
                results = [
                    {
                        "order_id": str(order.get("order_id") or "").strip(),
                        "status": "skipped",
                        "reason_code": "no_result",
                        "reason_text": "自動化完成但沒有產生新結果",
                        "message": "自動化完成但沒有產生新結果",
                    }
                    for _, row in rows_for_run.iterrows()
                    for order in [
                        {
                            "order_id": str(row.get("order_id") or row.get("注文番号(貼上原始資料)") or "").strip(),
                            "trans_type": str(row.get("trans_type") or row.get("TransType") or row.get("郵局運送方式(複數商品請自行確認是否走小包)") or "").strip(),
                            "shipment_role": str(row.get("shipment_role") or row.get("_shipment_role") or "primary").strip(),
                        }
                    ]
                ]

            for result in results:
                if (
                    str(result.get("status") or "success").strip() in {"success", "completed"}
                    and not str(result.get("tracking") or "").strip()
                ):
                    result.update(
                        {
                            "status": "failed",
                            "reason_code": "invalid_writeback_identity",
                            "reason_text": "製單結果缺少貨運單號，未進行回填。",
                            "message": "製單結果缺少貨運單號，未進行回填。",
                        }
                    )
            successful_results = [
                result
                for result in results
                if str(result.get("status") or "success").strip() in {"success", "completed"}
                and str(result.get("tracking") or "").strip()
            ]
            failed_results = [
                result
                for result in results
                if str(result.get("status") or "success").strip() not in {"success", "completed"}
            ]
            results = initial_results + results
            job["results"] = results
            mark_results_failed(job, failed_results)
            if successful_results:
                writeback_candidates = successful_results
                for result in successful_results:
                    _status({"event": "writeback_pending", **result})
                _log(f"📋 正在回填 {len(writeback_candidates)} 筆至 Google Sheets...")
                backfill_outcome = backfill_results(writeback_candidates, log_cb=_log) or {
                    "ok": False,
                    "written": 0,
                    "failed": [str(result.get("order_id") or "") for result in writeback_candidates],
                    "error": "回填函式沒有回傳驗證結果",
                }
                terminal_status = apply_writeback_outcome(
                    job,
                    writeback_candidates,
                    backfill_outcome,
                )
                verified_results = [
                    result for result in writeback_candidates if result.get("status") == "completed"
                ]
                for result in verified_results:
                    _status({"event": "writeback_verified", **result})
                if backfill_outcome.get("ok") and verified_results:
                    _log(f"✅ 本次已確認 {len(verified_results)} 筆回填完成。")
                if len(verified_results) != len(writeback_candidates):
                    _log("❌ 部分運單已產生，但資料回填仍需確認。")
            else:
                job["backfill_outcome"] = {"ok": True, "written": 0, "failed": [], "error": ""}
                result_summary = summarize_job_results(results)
                terminal_status = "completed" if (
                    result_summary["completed"] == result_summary["total"]
                    and result_summary["total"] > 0
                ) else "partial_failure"
                job["pending_refresh_needed"] = terminal_status == "completed"
            _JOB_REGISTRY.finish(job, terminal_status)
            _clear_job_lock(email)
        except BaseException as e:
            print(
                f"[BOT_ERROR] {type(e).__name__}",
                file=sys.stderr,
                flush=True,
            )
            try:
                safe_log_event(
                    _log,
                    "job_exception",
                    error_type=type(e).__name__,
                )
            except Exception:
                pass
            try:
                reason_text = "製單流程發生錯誤，請查看安全診斷。"
                unfinished_results = [
                    {
                        "order_id": str(order.get("order_id") or "").strip(),
                        "status": "failed",
                        "reason_code": "job_exception",
                        "reason_text": reason_text,
                        "message": reason_text,
                    }
                    for order in (job.get("orders") or [])
                    if order.get("status") in {"queued", "running"}
                ]
                job.setdefault("results", []).extend(unfinished_results)
                mark_results_failed(job, unfinished_results)
                mark_unfinished_orders(job, "failed", "發生例外", reason_text)
                _JOB_REGISTRY.finish(job, "error")
            except Exception:
                pass
            _clear_job_lock(email)

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
        st.markdown('## <span class="brand-accent">Cross-Border</span>製單系統', unsafe_allow_html=True)
        st.markdown("**跨境出貨作業・揀貨單・製單輔助**")
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
    pending_initial_result = None
    picking_initial_result = None
    try:
        pending_initial_result = _refresh_source("pending", force=False)
    except Exception:
        st.session_state["pending_refresh_error_code"] = "unavailable"
    try:
        picking_initial_result = _refresh_source("picking", force=False)
    except Exception:
        pass

    email = st.session_state.get("user_email", "")
    name = st.session_state.get("user_name", email)
    picture = st.session_state.get("user_picture", "")

    col1, col2, col3 = st.columns([5.7, 1.4, 0.65], vertical_alignment="center")
    with col1:
        st.markdown('<div class="app-header-title">Cross-Border製單系統</div>', unsafe_allow_html=True)
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
            --erp-border: rgba(82, 117, 168, 0.34);
            --erp-border-soft: rgba(148, 163, 184, 0.18);
            --erp-text: #f8fafc;
            --erp-muted: #cbd5e1;
            --erp-dim: #94a3b8;
            --erp-accent: #5275A8;
            --erp-accent-2: #456A9F;
            --erp-danger: #ef4444;
            --control-h: 36px;
            --row-h: 38px;
            --control-radius: 10px;
            --control-pad-x: 12px;
            --row-gap: 8px;
        }
        * { box-sizing: border-box; }
        .stApp {
            background: var(--erp-bg);
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
        h3 { color: var(--erp-text); margin-bottom: .18rem; }
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
            border-color: rgba(82, 117, 168, 0.72);
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
            width: 100%;
            min-width: 0;
            display: flex;
            align-items: center;
            flex-wrap: wrap;
            gap: .25rem .36rem;
            color: var(--erp-text);
            font-size: 1.22rem;
            font-weight: 700;
            line-height: 1.1;
            white-space: nowrap;
            overflow-wrap: anywhere;
        }
        .toolbar-text span {
            color: var(--erp-accent);
            font-size: 1.05rem;
            font-weight: 800;
            margin-right: 0;
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
            width: 100%;
            min-width: 0;
            max-width: 100%;
            flex-wrap: wrap;
        }
        .toolbar-count strong {
            color: var(--erp-text);
            font-size: 2.1rem;
            font-weight: 900;
            line-height: 1;
            white-space: nowrap;
        }
        .toolbar-rate {
            display: inline-flex;
            align-items: baseline;
            gap: .36rem;
            width: 100%;
            min-width: 0;
            max-width: 100%;
            flex-wrap: wrap;
        }
        .toolbar-rate strong {
            color: var(--erp-text);
            font-size: 1.28rem;
            font-weight: 850;
            line-height: 1;
            white-space: nowrap;
        }
        .guide-note {
            border-left: 3px solid var(--erp-accent);
            background: rgba(82, 117, 168, 0.12);
            color: #b8caea;
            padding: .72rem .9rem;
            margin: .8rem 0 1.1rem;
            border-radius: 0 8px 8px 0;
            font-weight: 700;
        }
        .guide-subtle {
            color: #cbd5e1;
            font-size: .95rem;
        }
        .brand-title,
        .brand-title * {
            color: var(--erp-accent) !important;
        }
        .toolbar-chip,
        .field-inline-label {
            border: 1px solid rgba(82, 117, 168, 0.3);
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
            border: 1px solid rgba(82, 117, 168, 0.24) !important;
            border-radius: 12px !important;
            background: rgba(19, 21, 25, 0.96);
            margin-bottom: .62rem;
            box-shadow: inset 0 1px 0 rgba(255, 255, 255, .025), 0 10px 24px rgba(0, 0, 0, .12);
            padding: .46rem .62rem .52rem .62rem !important;
        }
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.order-card-marker):hover {
            border-color: rgba(82, 117, 168, 0.62) !important;
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
            border-color: rgba(82, 117, 168, 0.62);
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
        .running-panel {
            border: 1px solid rgba(82, 117, 168, 0.4);
            background: rgba(15, 23, 42, 0.72);
            border-radius: 8px;
            padding: .55rem .7rem;
            margin: .35rem 0 .55rem 0;
        }
        .running-title {
            color: #ffffff;
            font-weight: 850;
            font-size: 1rem;
            line-height: 1.2;
        }
        .running-detail {
            color: #9bb7df;
            font-weight: 700;
            font-size: .86rem;
            line-height: 1.25;
            margin-top: .18rem;
        }
        .running-guard-overlay {
            position: fixed;
            inset: 0;
            z-index: 999999;
            background: rgba(3, 7, 18, 0.48);
            backdrop-filter: blur(1.5px);
            display: flex;
            align-items: flex-start;
            justify-content: center;
            padding-top: 14.5rem;
            pointer-events: all;
        }
        .running-guard-box {
            width: min(560px, calc(100vw - 32px));
            border: 1px solid rgba(82, 117, 168, 0.58);
            background: rgba(15, 23, 42, 0.96);
            border-radius: 8px;
            padding: 1rem 1.25rem;
            box-shadow: 0 24px 80px rgba(0, 0, 0, 0.5);
        }
        .running-guard-title {
            color: #ffffff;
            font-size: 1.25rem;
            font-weight: 900;
            line-height: 1.2;
        }
        .running-guard-text {
            color: #9bb7df;
            font-size: .95rem;
            font-weight: 750;
            margin-top: .42rem;
        }
        .running-guard-sub {
            color: #cbd5e1;
            font-size: .82rem;
            margin-top: .35rem;
        }
        div[data-testid="stCheckbox"] {
            min-height: var(--control-h);
            display: flex;
            align-items: center;
            padding-top: .15rem;
        }
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.order-card-marker) div[data-testid="stCheckbox"] {
            padding-top: .36rem;
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
            border-color: rgba(82, 117, 168, 0.34);
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
            color: #b8caea;
            font-size: .78rem;
            text-align: right;
            padding-top: .42rem;
            white-space: nowrap;
        }
        .stButton > button[kind="primary"] {
            background: #5275A8;
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
            border-color: rgba(82, 117, 168, 0.34) !important;
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
            border-color: rgba(82, 117, 168, 0.34) !important;
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
                font-size: 1.86rem;
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
                align-items: center;
                line-height: 1.1;
                min-height: var(--control-h);
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
                font-size: 1.58rem;
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

        /* Isolated postal pending UI v2: flat dark surfaces and restrained blue edit cues. */
        .postal-v2-page-marker,
        .postal-v2-operation-panel,
        .postal-v2-card-marker {
            display: none;
        }
        .postal-v2-panel-heading,
        .postal-v2-section-heading,
        .postal-v2-list-heading,
        .postal-v2-status-heading {
            color: #e8eaf0;
            font-weight: 750;
            line-height: 1.2;
        }
        .postal-v2-panel-heading {
            font-size: .86rem;
        }
        .postal-v2-section-heading,
        .postal-v2-list-heading {
            font-size: 1rem;
            margin: .55rem 0 .42rem;
        }
        .postal-v2-status-heading {
            font-size: 1.08rem;
            margin: .9rem 0 .42rem;
        }
        .postal-v2-rate-badge {
            min-width: 0;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            text-align: right;
            color: #9bb7df;
            border: 1px solid #3a4152;
            background: #0A0D13;
            border-radius: 8px;
            padding: .32rem .48rem;
            font-size: .72rem;
            font-variant-numeric: tabular-nums;
        }
        .postal-v2-metric {
            min-height: 4.05rem;
            padding: .42rem .24rem .26rem;
            border-right: 1px solid rgba(58, 65, 82, .82);
            display: flex;
            flex-direction: column;
            justify-content: center;
            align-items: center;
            gap: .16rem;
        }
        .postal-v2-metric:last-child {
            border-right: 0;
        }
        .postal-v2-metric span {
            color: #8b93a7;
            font-size: .78rem;
            white-space: nowrap;
        }
        .postal-v2-metric strong {
            color: #5275A8;
            font-size: 1.7rem;
            font-weight: 850;
            line-height: 1;
            font-variant-numeric: tabular-nums;
        }
        .postal-v2-metric-pending strong { color: #6f8fbe; }
        .postal-v2-metric-selected strong { color: #5275A8; }
        .postal-v2-metric-completed strong { color: #5bbd80; }
        .postal-v2-selection-count {
            color: #8b93a7;
            font-size: .82rem;
            text-align: right;
            padding: .28rem .1rem .05rem;
            font-variant-numeric: tabular-nums;
        }
        .postal-v2-selection-count strong {
            color: #5275A8;
            font-size: 1.28rem;
            font-weight: 850;
        }
        .postal-v2-panel-divider {
            border-top: 1px solid #262b36;
            margin: .64rem 0 .56rem;
        }
        .postal-v2-legend {
            display: flex;
            flex-wrap: wrap;
            gap: .35rem .7rem;
            border-top: 1px solid #262b36;
            margin-top: .7rem;
            padding-top: .62rem;
            font-size: .76rem;
        }
        .postal-v2-legend-editable,
        .postal-v2-legend-readonly {
            white-space: nowrap;
        }
        .postal-v2-legend-editable { color: #5275A8; }
        .postal-v2-legend-readonly { color: #8b93a7; }
        .postal-v2-current-value-note {
            color: #8b93a7;
            font-size: .75rem;
            line-height: 1.35;
            margin-top: .56rem;
        }
        /* Streamlit exposes bordered containers as either wrapper test id depending on runtime version. */
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.postal-v2-operation-panel),
        div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] .postal-v2-operation-panel) {
            border: 1px solid #262b36 !important;
            border-radius: 12px !important;
            background: #0A0D13 !important;
            padding: .65rem .7rem 1.12rem !important;
            min-width: 0;
        }
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.postal-v2-card-marker) {
            border: 1px solid #262b36 !important;
            border-radius: 12px !important;
            background: #0A0D13 !important;
            margin-bottom: .62rem;
            padding: .46rem .62rem .52rem !important;
            box-shadow: inset 0 1px 0 rgba(255, 255, 255, .025), 0 10px 24px rgba(0, 0, 0, .16);
        }
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.postal-v2-card-marker):hover {
            border-color: #3f5f8f !important;
        }
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.postal-v2-operation-panel) .stButton > button[kind="primary"],
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.postal-v2-card-marker) .stButton > button[kind="primary"] {
            background: #5275A8 !important;
            border-color: #5275A8 !important;
            color: #f5f7f9 !important;
        }
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.postal-v2-operation-panel) .stButton > button:hover,
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.postal-v2-card-marker) .stButton > button:hover {
            border-color: #6f91c2 !important;
            background: #456A9F !important;
        }
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.postal-v2-operation-panel) div[data-testid="stNumberInput"] input,
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.postal-v2-card-marker) div[data-testid="stTextInput"] input,
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.postal-v2-card-marker) div[data-baseweb="select"] > div {
            background: #0A0D13 !important;
            border-color: #5275A8 !important;
            color: #e8eaf0 !important;
        }
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.postal-v2-card-marker) div[data-testid="stTextInput"] label,
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.postal-v2-card-marker) div[data-testid="stSelectbox"] label,
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.postal-v2-operation-panel) div[data-testid="stNumberInput"] label {
            color: #7f9bc7 !important;
        }
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.postal-v2-card-marker) div[data-testid="stDataEditor"] {
            border-color: #5275A8 !important;
            background: #0A0D13 !important;
        }
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.postal-v2-card-marker) div[data-testid="stDataEditor"] [role="columnheader"] {
            background: #171a21 !important;
            color: #a8b5ca !important;
            border-color: rgba(82, 117, 168, .42) !important;
        }
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.postal-v2-card-marker) div[data-testid="stDataEditor"] [role="gridcell"] {
            background: #0A0D13 !important;
            color: #e8eaf0 !important;
            border-color: rgba(82, 117, 168, .48) !important;
        }
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.postal-v2-card-marker) .native-info-label {
            color: #8b93a7 !important;
        }
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.postal-v2-card-marker) .native-info-value {
            color: #e8eaf0 !important;
        }
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.postal-v2-card-marker) .native-info-country .native-info-value,
        div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] .postal-v2-card-marker) .native-info-country .native-info-value {
            overflow: visible;
            text-overflow: clip;
        }
        div[data-testid="stTabs"] [data-baseweb="tab-highlight"] {
            background-color: #5275A8 !important;
        }
        div[data-testid="stTabs"] [data-baseweb="tab"][aria-selected="true"] {
            color: #5275A8 !important;
        }
        @media (max-width: 900px) {
            .postal-v2-rate-badge {
                font-size: .68rem;
            }
            .postal-v2-metric {
                min-height: 3.55rem;
            }
            .postal-v2-metric strong {
                font-size: 1.46rem;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    job = _get_job(email)
    is_running = job is not None and job.get("status") == "running"
    launch_lock_active = _job_lock_is_active(email)
    if is_running and st.session_state.get("job_launching"):
        st.session_state.pop("job_launching", None)
        st.session_state.pop("job_launching_started_at", None)
    if job is not None and not is_running and st.session_state.get("job_launching"):
        st.session_state.pop("job_launching", None)
        st.session_state.pop("job_launching_started_at", None)
    if job is None and st.session_state.get("job_launching") and not launch_lock_active:
        st.session_state.pop("job_launching", None)
        st.session_state.pop("job_launching_started_at", None)
    is_launching = bool(st.session_state.get("job_launching"))
    is_busy = is_running or is_launching

    if pending_initial_result is not None:
        _apply_pending_result(
            pending_initial_result,
            is_busy=is_busy,
            allow_dirty_reset=False,
            job=job,
        )
    if picking_initial_result is not None and picking_initial_result.data is not None:
        picking_was_loaded = "picking_orders" in st.session_state
        apply_picking_payload(
            picking_initial_result.data,
            preserve_selection=picking_was_loaded,
            loaded_at=picking_initial_result.status.loaded_at,
        )
        st.session_state["picking_snapshot_loaded_at"] = picking_initial_result.status.loaded_at

    refresh_notice = bool(job and job.pop("pending_refresh_needed", False))
    if refresh_notice and not is_busy:
        st.session_state["pending_refresh_notice"] = True
        completed_refresh = _refresh_source("pending", force=True)
        if _apply_pending_result(
            completed_refresh,
            is_busy=is_busy,
            allow_dirty_reset=True,
            job=job,
        ):
            st.session_state["pending_editor_dirty"] = False
            st.session_state.pop("postal_batch_view_active", None)
            _clear_pending_editor_keys()

    cached_pending = st.session_state.get("last_pending_df")
    if isinstance(cached_pending, pd.DataFrame):
        df_pending = cached_pending
        pending_logs = list(st.session_state.get("last_pending_logs", []))
    else:
        df_pending = pd.DataFrame()
        pending_logs = []
    pending_count = len(df_pending)

    if not is_busy and job and job.get("results"):
        filtered_pending = filter_pending_orders_after_batch(
            df_pending,
            job["results"],
            submitted_packages=job.get("orders") or [],
        )
        if len(filtered_pending) != len(df_pending):
            df_pending = filtered_pending
            st.session_state["last_pending_df"] = df_pending
            selected_by_order = st.session_state.get("pending_v2_selected_by_order")
            if isinstance(selected_by_order, dict):
                for order_id in fully_completed_order_ids(
                    job["results"],
                    job.get("orders") or [],
                ):
                    selected_by_order.pop(order_id, None)

    pending_count = len(df_pending)
    rate, rate_date, _ = _load_usd_jpy_rate()
    batch_summary = summarize_batch_results((job or {}).get("results") or [])
    done = int(batch_summary["completed_count"])

    _active_refresh_tick(is_busy=is_busy, job=job)

    picking_tab, postal_tab, guide_tab, diagnostics_tab = st.tabs(
        ["跨境揀貨單", "待製郵便運單", "使用說明", "讀取診斷"]
    )

    with picking_tab:
        picking_labels_module = import_module_with_retry("features.picking_labels")
        picking_labels_module.render_picking_label_tab(refresh_source=_refresh_source)

    with postal_tab:
        _render_postal_pending_v2(
            email=email,
            df_pending=df_pending,
            pending_logs=pending_logs,
            rate=rate,
            rate_date=rate_date,
            job=job,
            is_running=is_running,
            is_launching=is_launching,
            is_busy=is_busy,
            pending_count=pending_count,
            done=done,
            batch_summary=batch_summary,
        )

    with guide_tab:
        st.markdown("# Cross-Border製單系統使用說明")
        st.markdown(
            """
本系統是公司內部使用的跨境出貨製單工具。

使用者登入後，可以在瀏覽器中讀取跨境揀貨單與郵局待製單資料。系統可產生 100mm × 150mm 揀貨標籤 PDF，也可依待製單清單自動完成日本郵政製單、下載 PDF、上傳至指定 Google Drive 資料夾，最後將製單結果回填至指定 Google Sheets。
            """
        )
        st.markdown(
            '<div class="guide-note">重點：開始製單時，系統會使用目前畫面上顯示並確認送出的內容。</div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            """
## 一、登入與權限

本系統使用 Google 帳號登入。

原則上僅限公司網域 `@tkrjm.co.jp` 或已授權白名單人員使用。

登入後，系統會保留一段時間的登入狀態，避免每次操作都需要重新登入。如需切換帳號，可使用右上角「登出」。

## 二、主要操作流程

郵局製單基本操作流程如下：

1. 進入系統後，打開「待製郵便運單」頁籤
2. 檢查目前待製單訂單
3. 視需要調整 **Name、TransType、PRC ID、PCCC、HS Code、Value、Quantity** 等欄位
4. 勾選本批要製單的訂單
5. 設定最大處理筆數
6. 點擊「開始製單」
7. 系統自動製作日本郵政單據
8. 系統下載 PDF 並上傳至指定 Google Drive
9. 系統將 tracking number 與相關結果回填至指定 Google Sheets

跨境揀貨單基本操作流程如下：

1. 進入系統後，打開「跨境揀貨單」頁籤
2. 點擊「重新讀取」取得目前符合條件的待製單訂單
3. 檢查注文番号、注文日、訂單來源、國際物流方式與発送期限
4. 勾選本批要產生揀貨單的訂單
5. 點擊「產生揀貨單」
6. 系統產生揀貨標籤 PDF 並上傳至指定 Google Drive
7. 上傳成功後，系統才會將來源表 L 欄更新為「已製單」
8. 如需確認細節，可至「讀取診斷」頁籤查看跨境揀貨單診斷資訊

## 三、跨境揀貨單

「跨境揀貨單」頁籤會顯示目前可產生揀貨標籤的訂單。

主頁主要顯示日常操作所需資訊：

1. **待製單訂單**：目前符合條件的訂單數量
2. **最後讀取**：最近一次讀取來源表的時間
3. **注文番号**：揀貨單使用的訂單編號
4. **注文日**：訂單日期
5. **訂單來源**：例如 Official website、Shopee 或其他來源
6. **國際物流方式**：例如 郵便局、佐川、SLS、MLS
7. **発送期限**：顯示於揀貨標籤 Header 中的発送期限

操作按鈕包含：

1. **產生揀貨單**：產生 PDF、上傳 Google Drive，成功後寫回來源表
2. **重新讀取**：重新讀取來源 Google Sheets
3. **全選**：選取目前列表中的所有訂單
4. **取消全選**：取消目前列表中的所有選取

列印時請注意：PDF 檔尺寸為 `100mm × 150mm`，請使用對應 Label 大小輸出。

## 四、跨境揀貨單篩選與寫回規則

系統會從 `南巽出貨Label` 讀取跨境揀貨單資料。

候選訂單必須同時符合以下條件：

1. K 欄 `訂單狀態` 必須是「可出貨」
2. L 欄 `製單後勾選` 必須是未製單狀態
3. P 欄 `國際物流方式` 必須包含 `郵便局`、`佐川`、`MLS` 或 `SLS`

L 欄使用 Google Sheets checkbox / data validation 自訂值：

1. `未製單` 代表尚未產生揀貨單
2. `已製單` 代表已完成產生與寫回

正式產生揀貨單時，系統會先產生 PDF 並上傳至 Google Drive。只有在 Drive 上傳成功後，才會把來源表 L 欄寫回為「已製單」。如果 Drive 上傳失敗，系統不會更新 L 欄。

## 五、跨境揀貨標籤 PDF

揀貨標籤 PDF 採固定版型。

目前規則如下：

1. PDF 尺寸固定為 `100mm × 150mm`
2. 每頁固定 10 格商品列
3. 少於 10 個商品時，剩餘列會保留空白格線
4. 超過 10 個商品時，系統會每 10 個商品分頁
5. QR code 預設使用注文番号
6. `発送期限` 會顯示在注文番号下方
7. 商品名會自動換行，JAN 會保留在獨立一行
8. 商品名中的不可見方向控制字元會被清理，避免文字中出現異常大空白

PDF 檔名格式為：

`YYMMDD-N揀貨標籤.pdf`

例如：

1. `260629-1揀貨標籤.pdf`
2. `260629-2揀貨標籤.pdf`

系統會在上傳前檢查 Google Drive 目標資料夾，依照當日既有檔名自動使用下一個序號，避免覆蓋既有檔案。

## 六、待製郵便運單

「待製郵便運單」頁籤會顯示目前可製單的訂單。

每筆訂單會顯示以下資訊：

1. **Order No.**：訂單編號
2. **Country**：目的地國家
3. **USD**：訂單申告金額（美元）
4. **JPY**：依目前匯率換算後的日圓金額
5. **Name**：收件人姓名
6. **TransType**：運送類別，例如 EMS、國際小包、ePacket
7. **Content**：內容物項次
8. **Description**：內容物描述
9. **HS Code**：商品分類代碼
10. **Value**：內容物單價（USD）
11. **Quantity**：內容物數量

若目前沒有可製單資料，畫面會顯示「目前沒有待製單資料」。

## 七、待製單資料篩選規則

系統會從指定來源表讀取訂單資料，並自動篩選出目前可製單的訂單。

目前主要篩選條件包含：

1. 訂單狀態必須是「未打單」
2. 郵局申告金額不可空白
3. 製單檢核不可為 TRUE
4. Shipping Name 不可空白
5. 若同一注文番号在來源表中重複，系統會依運送方式優先度保留一筆
6. 已經完成回填的訂單會被排除，避免重複製單

如需確認某些訂單為什麼沒有出現在待製單清單中，可查看「讀取診斷」頁籤。

## 八、匯率與金額

Toolbar 會顯示目前 USD/JPY 匯率與日期，例如：

`USD/JPY 161.20｜26/06/20`

系統會依目前匯率換算 JPY 金額。

當商品 Value 或 Quantity 被修改時，系統會依目前資料重新計算：

1. TotalValue（USD）
2. TotalValue（JPY）

若匯率暫時無法取得，畫面會顯示 `USD/JPY N/A`。此時系統會盡量保留來源資料中既有的 JPY 金額。

## 九、批次選取與最大處理

每筆訂單左側都有「製單」勾選框，可選擇本批是否製作該訂單。

Toolbar 會顯示：

1. **待製單**：目前可製單訂單數量
2. **已選取**：目前已勾選訂單數量
3. **本次完成**：本次已完成製單數量

「最大處理」可限制本批最多處理幾筆訂單。

設定規則：

1. 預設值為 20
2. 設為 0 代表全部處理
3. 未勾選的訂單不會進入本次製單

## 十、可編輯欄位

目前可在前台調整的欄位包含：

1. Name
2. TransType
3. 追加 TransType
4. PRC ID
5. PCCC
6. 商品 Description
7. HS Code
8. Value
9. Quantity

開始製單時，系統會使用目前畫面上顯示的內容送出製單。

## 十一、Name、PRC ID、PCCC

部分國家的訂單需要額外證號。

### 中國訂單

來源資料可能會顯示為：

`zhuxiaomu (PRC ID:110108198309121213)`

前台會拆成：

`Name zhuxiaomu`

`PRC ID 110108198309121213`

製單送出時，系統會：

1. 將收件人姓名送為 `Name + 注文番号`
2. 將 PRC ID 放在地址最後方
3. 依日本郵政欄位長度自動拆分 Address 1 / Address 2 / Address 3

### 韓國訂單

來源資料可能會顯示為：

`Eunseo Ha (PCCC:P18026936191)`

前台會拆成：

`Name Eunseo Ha`

`PCCC P18026936191`

製單送出時，系統會：

1. 將收件人姓名送為 `Name + 注文番号`
2. 將 PCCC 放在地址最後方
3. 依日本郵政欄位長度自動拆分 Address 1 / Address 2 / Address 3

例如訂單 `imy2036430`：

`kim sang woo imy2036430`

地址會保留完整 Shipping Street，PCCC 會放在地址最後一行。

### 日本郵政送出格式範例

若 Shipping Street 為：

`3518, Changmil-ro, Miryang-si, Gyeongsangnam-do, Republic of Korea, e-Pyeonhansesang Nanovalley 103-2501`

且 PCCC 為：

`P210006411542`

系統送出時會將姓名與地址整理為：

`Name: kim sang woo imy2036430`

`Address 1: 3518, Changmil-ro, Miryang-si, Gyeongsangnam-do, Republic of Korea,`

`Address 2: e-Pyeonhansesang Nanovalley 103-2501`

`Address 3: PCCC:P210006411542`

### 顯示規則

1. 只有 CHINA 訂單會顯示 PRC ID 欄位
2. 只有 KOREA 訂單會顯示 PCCC 欄位
3. 其他國家不會顯示 PRC ID 或 PCCC 欄位
4. 日本郵政 Address 2 欄位上限為 80 字，Address 3 欄位上限為 36 字；系統會自動拆行，避免 Shipping Street 被截斷
5. PRC ID / PCCC 會從 Name 中移出，避免姓名欄過長，但輸出的 Name 仍會保留注文番号

## 十二、PRC ID / PCCC 必填規則

開始製單前，系統會檢查必要證號。

目前規則：

1. **CHINA 訂單若缺少 PRC ID，不可製單**
2. **KOREA 訂單若缺少 PCCC，不可製單**

缺少必要證號時，系統會提示：

`中國訂單需填入 PRC ID 才能製單`

或：

`韓國訂單需填入 PCCC 才能製單`

## 十三、TransType 與追加製單

每筆訂單可選擇主要 TransType。

目前支援：

1. EMS
2. 國際小包
3. ePacket

每筆訂單也有「追加」欄位，預設為「無」。

若選擇追加 TransType，系統會在同一批次中將同一筆訂單展開為多筆製單資料。

例如：

1. 國際小包 + ePacket
2. EMS + ePacket

系統會避免同一筆訂單重複加入相同 TransType。

## 十四、HS Code

商品表格可手動輸入 HS Code。

系統會自動清理 HS Code，只保留數字。

例如：

1. `9404.90` 會轉為 `940490`
2. `9404-90` 會轉為 `940490`
3. `HS:940490` 會轉為 `940490`

製單前，系統也會檢查本批訂單的 HS Code。對需要 HS Code 的國家或地區，系統會嘗試使用既有資料或系統推測結果補值。

## 十五、國家代碼與 EU 回填

系統會依目的地國家判斷回填用的國家代碼。

例如：

1. CHINA → CN
2. KOREA → KR
3. TAIWAN → TW
4. UNITED STATES → US
5. CANADA → CA
6. AUSTRALIA → AU

歐洲區域國家在回填至目標表時，會統一回填為 `EU`。

例如：

1. GERMANY → EU
2. FRANCE → EU
3. PORTUGAL → EU
4. BELGIUM → EU
5. GREECE → EU
6. CZECH → EU
7. ROMANIA → EU
8. CYPRUS → EU

## 十六、自動製單流程

點擊「開始製單」後，系統會開始處理本批訂單。

系統會自動執行：

1. 檢查本批訂單資料
2. 補齊必要 HS Code
3. 登入日本郵政製單系統
4. 依訂單資料填寫製單表單
5. 套用 TransType
6. 套用收件人、國家、商品、金額與 HS Code
7. 處理日本郵政頁面提示
8. 完成 shipment 登錄
9. 下載 PDF
10. 上傳 PDF 至指定 Google Drive
11. 回填 tracking number 與國家代碼至目標 Google Sheets

## 十七、PDF 上傳至 Google Drive

製單完成後，系統會下載日本郵政產生的 PDF。

PDF 會上傳至指定 Google Drive 資料夾。

系統會整理 PDF 檔名，避免檔名包含不可使用字元。

## 十八、製單結果回填

製單成功後，系統會將結果回填至目標 Google Sheets。

目前主要回填內容包含：

1. 收件人
2. 注文番号
3. tracking number
4. 運送國家代碼

歐洲國家會統一回填為 `EU`。

## 十九、重新讀取與資料暫存

為了降低 Google Sheets 讀取次數，系統會暫時保留已讀取的待製單清單與讀取診斷。

點擊「重新讀取」會重新讀取 Google Sheets 資料。

製單完成後，系統可能會暫時沿用既有清單，避免短時間內反覆讀取造成讀取限制。

## 二十、恢復預設

系統提供兩種恢復方式：

1. 單筆「恢復預設」
2. Toolbar 的「恢復全部預設」

恢復預設會將可編輯欄位恢復為來源資料解析出的原始值。

## 二十一、讀取診斷

「讀取診斷」頁籤會顯示待製單資料與跨境揀貨單資料的讀取與篩選結果。

內容包含：

1. 來源表名稱
2. 來源表讀取列數與耗時
3. 來源原始筆數
4. 來源末端注文番号
5. 目標表已完成單號讀取結果
6. 基礎篩選排除筆數
7. 排除原因
8. 同注文番号去重結果
9. 最終可打單筆數

此頁主要用於排查為什麼某些訂單沒有出現在待製單清單或跨境揀貨單清單中。

## 二十二、目前注意事項與後續改善項目

目前系統仍有以下注意事項：

1. 點擊「開始製單」後，畫面即時反應仍不夠明顯
2. 點擊製單後，畫面可能短暫看似回到原預設狀態
3. 雖然畫面可能看似回到預設狀態，但實際製單會依最後確認送出的內容執行
4. PRC ID / PCCC 的檢查邏輯仍會持續確認與優化
5. 執行中畫面未來可進一步固定顯示本次送出的訂單
6. 可進一步優化製單啟動提示、進度顯示與防止重複點擊
7. Google Sheets 讀取次數仍需注意，避免短時間內反覆重新讀取
            """
        )
        st.markdown(
            '<div class="guide-note">如遇到訂單未出現、製單結果異常或無法判斷原因，請先查看「讀取診斷」頁籤，再回報系統管理人員。</div>',
            unsafe_allow_html=True,
        )

    with diagnostics_tab:
        visible_pending_logs = _visible_pending_logs(
            pending_logs,
            sensitive_values=_dataframe_sensitive_values(df_pending),
        )
        if visible_pending_logs:
            with st.expander(f"待製單讀取診斷｜最終可打單 {pending_count} 筆", expanded=True):
                st.markdown('<span class="debug-log-marker"></span>', unsafe_allow_html=True)
                st.code("\n".join(visible_pending_logs), language="text")
        else:
            st.info("目前沒有待製單讀取診斷資料。")
        picking_labels_module = import_module_with_retry("features.picking_labels")
        picking_labels_module.render_picking_label_diagnostics_panel()

    if is_busy:
        time.sleep(2)
        st.rerun()


# ══════════════════════════════════════════════════════
# 主程式入口
# ══════════════════════════════════════════════════════

init_auth_state(_cm)

if handle_oauth_callback(_cm):
    st.rerun()
elif st.session_state.get("authenticated"):
    _render_main_app()
else:
    _render_login_page()
