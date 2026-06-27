"""Streamlit UI for cross-border picking labels."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import tempfile
import os

import pandas as pd
import streamlit as st

from bot.drive import list_drive_files, upload_file_to_drive
from bot.picking_labels import (
    PICKING_OUTPUT_DRIVE_FOLDER_ID,
    PICKING_SOURCE_SHEET_NAME,
    PICKING_SOURCE_SPREADSHEET_ID,
    SHIPPING_STATUS_SHEET_NAME,
    SHIPPING_STATUS_SPREADSHEET_ID,
    PickingOrder,
    build_shipping_deadline_lookup,
    build_picking_label_summary,
    build_picking_source_diagnostics,
    filter_orders_by_rows,
    generate_picking_labels_transaction,
    parse_picking_label_candidates,
    resolve_picking_done_row_numbers,
)
from bot.picking_pdf import render_picking_labels_pdf
from bot.sheets import batch_mark_picking_done, load_sheet_values


def _config_value(name: str, default: str) -> str:
    try:
        value = st.secrets.get(name, "")
    except Exception:
        value = ""
    return str(value or os.environ.get(name, "") or default)


def _picking_source_spreadsheet_id() -> str:
    return _config_value("PICKING_SOURCE_SPREADSHEET_ID", PICKING_SOURCE_SPREADSHEET_ID)


def _picking_source_sheet_name() -> str:
    return _config_value("PICKING_SOURCE_SHEET_NAME", PICKING_SOURCE_SHEET_NAME)


def _shipping_status_spreadsheet_id() -> str:
    return _config_value("SHIPPING_STATUS_SPREADSHEET_ID", SHIPPING_STATUS_SPREADSHEET_ID)


def _shipping_status_sheet_name() -> str:
    return _config_value("SHIPPING_STATUS_SHEET_NAME", SHIPPING_STATUS_SHEET_NAME)


def _picking_output_drive_folder_id() -> str:
    return _config_value("PICKING_OUTPUT_DRIVE_FOLDER_ID", PICKING_OUTPUT_DRIVE_FOLDER_ID)


def _load_orders() -> None:
    values = load_sheet_values(_picking_source_spreadsheet_id(), _picking_source_sheet_name())
    status_values = load_sheet_values(_shipping_status_spreadsheet_id(), _shipping_status_sheet_name())
    shipping_deadlines = build_shipping_deadline_lookup(status_values)
    orders, warnings = parse_picking_label_candidates(values, shipping_deadlines=shipping_deadlines)
    st.session_state["picking_orders"] = orders
    st.session_state["picking_warnings"] = warnings
    diagnostics = build_picking_source_diagnostics(values, orders, warnings)
    diagnostics["source_spreadsheet_id"] = _picking_source_spreadsheet_id()
    diagnostics["source_sheet"] = _picking_source_sheet_name()
    diagnostics["shipping_status_spreadsheet_id"] = _shipping_status_spreadsheet_id()
    diagnostics["shipping_status_sheet"] = _shipping_status_sheet_name()
    st.session_state["picking_diagnostics"] = diagnostics
    st.session_state["picking_loaded_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    st.session_state["picking_selected_rows"] = set()


def _orders_to_dataframe(orders: list[PickingOrder], selected_rows: set[int]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "選取": order.source_row_number in selected_rows,
                "注文番号": order.order_no,
                "注文日": order.order_date,
                "訂單來源": order.order_source,
                "國際物流方式": order.logistics_method,
                "発送期限": order.shipping_deadline or "-",
            }
            for order in orders
        ]
    )


def _selected_orders_from_editor(orders: list[PickingOrder], edited_df: pd.DataFrame) -> list[PickingOrder]:
    if edited_df.empty:
        return []
    selected_rows = {
        orders[position].source_row_number
        for position, (_, row) in enumerate(edited_df.iterrows())
        if position < len(orders) and bool(row.get("選取"))
    }
    st.session_state["picking_selected_rows"] = selected_rows
    return filter_orders_by_rows(orders, selected_rows)


def _preview_pdf(selected_orders: list[PickingOrder]) -> None:
    if not selected_orders:
        st.warning("請先選取至少一筆訂單。")
        return
    preview_path = Path(tempfile.gettempdir()) / "jppost-picking-preview.pdf"
    result = render_picking_labels_pdf(selected_orders, str(preview_path))
    st.session_state["picking_preview_path"] = str(preview_path)
    st.success(f"預覽 PDF 已產生：{result.total_orders} 筆訂單，{result.total_pages} 頁。")
    st.download_button(
        "下載預覽 PDF",
        data=preview_path.read_bytes(),
        file_name="preview-揀貨標籤.pdf",
        mime="application/pdf",
        use_container_width=True,
    )
    with st.expander("預覽 / debug 摘要"):
        st.json(build_picking_label_summary(selected_orders))


def _generate_and_upload(selected_orders: list[PickingOrder]) -> None:
    if not selected_orders:
        st.warning("請先選取至少一筆訂單。")
        return

    row_numbers = {order.source_row_number for order in selected_orders}
    with st.spinner("重新確認來源表狀態..."):
        values = load_sheet_values(_picking_source_spreadsheet_id(), _picking_source_sheet_name())
        status_values = load_sheet_values(_shipping_status_spreadsheet_id(), _shipping_status_sheet_name())
        shipping_deadlines = build_shipping_deadline_lookup(status_values)
        fresh_orders, _warnings = parse_picking_label_candidates(values, shipping_deadlines=shipping_deadlines)
        fresh_selected = filter_orders_by_rows(fresh_orders, row_numbers)

    if len(fresh_selected) != len(selected_orders):
        st.error("部分訂單已不符合可製作條件，請重新讀取後再試。")
        return

    def _mark_done_after_revalidation(_rows: list[int]) -> list[int]:
        resolved_rows = resolve_picking_done_row_numbers(
            load_sheet_values(_picking_source_spreadsheet_id(), _picking_source_sheet_name()),
            fresh_selected,
        )
        batch_mark_picking_done(
            _picking_source_spreadsheet_id(),
            _picking_source_sheet_name(),
            resolved_rows,
        )
        return resolved_rows

    try:
        result = generate_picking_labels_transaction(
            orders=fresh_selected,
            output_dir=tempfile.gettempdir(),
            list_files=lambda prefix: list_drive_files(_picking_output_drive_folder_id(), prefix),
            upload_file=lambda path: upload_file_to_drive(path, _picking_output_drive_folder_id(), "application/pdf"),
            mark_done=_mark_done_after_revalidation,
            now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
    except Exception as exc:
        st.error(f"正式產生前檢查 Google Drive 檔名失敗，已中止：{exc}")
        return

    output_path = Path(result.local_path)
    if not result.success and not result.drive_file:
        st.error(f"PDF 已產生但上傳 Google Drive 失敗：{result.error}")
        st.download_button(
            "下載本機產生的 PDF",
            data=output_path.read_bytes(),
            file_name=result.filename,
            mime="application/pdf",
            use_container_width=True,
        )
        return

    if not result.success and result.drive_file:
        st.error(
            "高風險警告：PDF 已成功上傳，但來源表未成功標記製單完成，"
            f"請手動確認 L 欄，避免下次重複製作。錯誤：{result.error}"
        )
        if result.drive_file.get("webViewLink"):
            st.link_button("開啟已上傳 PDF", result.drive_file["webViewLink"])
        return

    st.success(f"完成：{result.filename}，已標記來源列：{', '.join(str(row) for row in result.marked_rows)}")
    if result.drive_file and result.drive_file.get("webViewLink"):
        st.link_button("開啟 Google Drive PDF", result.drive_file["webViewLink"])
    _load_orders()


def render_picking_label_tab() -> None:
    st.info("列印設定：PDF檔尺寸為 100mm × 150mm，請使用對應Label大小輸出。")

    if "picking_orders" not in st.session_state:
        try:
            _load_orders()
        except Exception as exc:
            st.error(f"無法讀取跨境揀貨單來源表：{exc}")
            return

    orders: list[PickingOrder] = st.session_state.get("picking_orders", [])
    selected_rows: set[int] = set(st.session_state.get("picking_selected_rows", set()))
    selected_orders = filter_orders_by_rows(orders, selected_rows)

    summary = build_picking_label_summary(orders)
    selected_summary = build_picking_label_summary(selected_orders)
    st.markdown(
        """
        <style>
        .picking-status-row {
            display: grid;
            grid-template-columns: minmax(180px, 0.45fr) minmax(260px, 1fr);
            gap: 12px;
            margin: 0.2rem 0 0.75rem;
        }
        .picking-count-panel {
            border: 1px solid rgba(251, 146, 60, 0.32);
            border-radius: 10px;
            background: rgba(24, 24, 27, 0.86);
            padding: 0.62rem 0.78rem;
        }
        .picking-count-label {
            color: #f59e0b;
            font-size: 0.82rem;
            font-weight: 800;
            line-height: 1.1;
        }
        .picking-count-value {
            color: #f8fafc;
            font-size: 2.1rem;
            font-weight: 900;
            line-height: 1;
            margin-top: 0.2rem;
        }
        .picking-loaded-panel {
            min-height: 58px;
            display: flex;
            align-items: center;
            gap: 0.5rem;
            color: #cbd5e1;
            font-size: 0.82rem;
        }
        .picking-loaded-label {
            color: #f59e0b;
            font-size: 0.82rem;
            font-weight: 800;
        }
        .picking-loaded-panel strong {
            color: #f8fafc;
            font-size: 1rem;
            font-weight: 700;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(
        f"""
        <div class="picking-status-row">
            <div class="picking-count-panel">
                <div class="picking-count-label">待製單訂單</div>
                <div class="picking-count-value">{summary["order_count"]}</div>
            </div>
            <div class="picking-loaded-panel">
                <span class="picking-loaded-label">最後讀取</span>
                <strong>{st.session_state.get("picking_loaded_at", "-")}</strong>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    has_selection = bool(selected_orders)
    actions = st.columns([1.35, 1, 1, 1], vertical_alignment="bottom")
    generate_type = "primary" if has_selection else "secondary"
    if actions[0].button(
        "產生揀貨單",
        type=generate_type,
        use_container_width=True,
        disabled=not has_selection,
    ):
        _generate_and_upload(selected_orders)
    if actions[1].button("重新讀取", use_container_width=True):
        try:
            _load_orders()
            st.rerun()
        except Exception as exc:
            st.error(f"重新讀取失敗：{exc}")
    if actions[2].button("全選", use_container_width=True, disabled=not orders):
        st.session_state["picking_selected_rows"] = {order.source_row_number for order in orders}
        st.rerun()
    if actions[3].button("取消全選", use_container_width=True, disabled=not orders):
        st.session_state["picking_selected_rows"] = set()
        st.rerun()
    st.caption("成功上傳雲端資料夾後，才會勾選來源表製單檢核欄。")

    if not orders:
        st.info("目前沒有符合條件的待製單訂單。")
    else:
        df = _orders_to_dataframe(orders, selected_rows)
        edited_df = st.data_editor(
            df,
            hide_index=True,
            use_container_width=True,
            disabled=[col for col in df.columns if col != "選取"],
            column_config={"選取": st.column_config.CheckboxColumn("選取")},
            key="picking_order_editor",
        )
        selected_orders = _selected_orders_from_editor(orders, edited_df)


def render_picking_label_diagnostics_panel() -> None:
    st.markdown("### 跨境揀貨單")
    diagnostics = st.session_state.get("picking_diagnostics")
    if not diagnostics:
        st.info("尚未讀取跨境揀貨單資料。請先打開「跨境揀貨單」頁籤或按「重新讀取」。")
        return

    meta_rows = {
        "source sheet name": diagnostics.get("source_sheet", ""),
        "source spreadsheet id": diagnostics.get("source_spreadsheet_id", ""),
        "shipping status sheet": diagnostics.get("shipping_status_sheet", ""),
        "detected K status column": diagnostics.get("status_column", ""),
        "detected L done column": diagnostics.get("done_column", ""),
        "used M 注文日 column": diagnostics.get("order_date_column", ""),
        "used N 訂單來源 column": diagnostics.get("order_source_column", ""),
        "used O 注文番号 column": diagnostics.get("order_no_column", ""),
        "used P 國際物流方式 column": diagnostics.get("logistics_column", ""),
        "anchored picking schema": diagnostics.get("anchored_schema", False),
        "total source rows": diagnostics.get("total_source_rows", 0),
        "detected item groups": diagnostics.get("detected_item_groups", []),
        "max item group": diagnostics.get("max_item_group", 0),
        "candidate order count": diagnostics.get("candidate_order_count", 0),
        "excluded count": diagnostics.get("excluded_count", 0),
        "excluded because K status != 可出貨": diagnostics.get("excluded_because_status", 0),
        "excluded because L indicates done / 已製單": diagnostics.get("excluded_because_done", 0),
        "excluded because 注文番号 missing": diagnostics.get("excluded_because_order_no_missing", 0),
        "excluded because item data missing": diagnostics.get("excluded_because_item_data_missing", 0),
        "parser / unknown exclusion count": diagnostics.get("parser_unknown_exclusion_count", 0),
        "actual detected L raw values sample": diagnostics.get("done_raw_values_sample", []),
        "filter condition": diagnostics.get("filter_condition", ""),
    }
    st.json(meta_rows)

    duplicate_headers = diagnostics.get("duplicate_header_diagnostics", [])
    if duplicate_headers:
        with st.expander("重複欄名錨定狀態", expanded=False):
            st.dataframe(pd.DataFrame(duplicate_headers), hide_index=True, use_container_width=True)

    included = diagnostics.get("included_candidate_samples", [])
    if included:
        with st.expander("前 20 筆已納入候選訂單（M:P / K:L）", expanded=False):
            st.dataframe(pd.DataFrame(included), hide_index=True, use_container_width=True)

    exclusions = diagnostics.get("near_candidate_exclusions", [])
    if exclusions:
        with st.expander("前 5 筆接近條件但被排除的來源列", expanded=False):
            st.dataframe(pd.DataFrame(exclusions), hide_index=True, use_container_width=True)

    warnings = diagnostics.get("warnings", [])
    if warnings:
        with st.expander("來源欄位 / 系統限制警告", expanded=False):
            for warning in warnings:
                st.warning(warning)

    missing_headers = diagnostics.get("missing_item_headers", [])
    if missing_headers:
        with st.expander("缺少的 10 組商品欄位", expanded=False):
            st.write(missing_headers)

    with st.expander("QR / debug summary", expanded=False):
        st.json(
            {
                "qr_contents": diagnostics.get("qr_contents", []),
                "actual_detected_headers": diagnostics.get("actual_detected_headers", []),
            }
        )
