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
    estimate_total_pages,
    filter_orders_by_rows,
    generate_picking_labels_transaction,
    parse_picking_label_candidates,
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
    st.session_state["picking_loaded_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    st.session_state["picking_selected_rows"] = {order.source_row_number for order in orders}


def _orders_to_dataframe(orders: list[PickingOrder], selected_rows: set[int]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "選取": order.source_row_number in selected_rows,
                "注文番号": order.order_no,
                "注文日": order.order_date,
                "訂單來源": order.order_source,
                "國際物流方式": order.logistics_method,
                "商品數": len(order.items),
                "PDF 頁數": estimate_total_pages([order]),
                "提醒": "無商品資料" if not order.items else "",
                "QR內容": order.qr_content or order.order_no,
                "來源列號": order.source_row_number,
            }
            for order in orders
        ]
    )


def _selected_orders_from_editor(orders: list[PickingOrder], edited_df: pd.DataFrame) -> list[PickingOrder]:
    if edited_df.empty:
        return []
    selected_rows = {
        int(row["來源列號"])
        for _, row in edited_df.iterrows()
        if bool(row.get("選取"))
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

    try:
        result = generate_picking_labels_transaction(
            orders=fresh_selected,
            output_dir=tempfile.gettempdir(),
            list_files=lambda prefix: list_drive_files(_picking_output_drive_folder_id(), prefix),
            upload_file=lambda path: upload_file_to_drive(path, _picking_output_drive_folder_id(), "application/pdf"),
            mark_done=lambda rows: batch_mark_picking_done(_picking_source_spreadsheet_id(), _picking_source_sheet_name(), rows),
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
    st.subheader("跨境揀貨單")
    st.caption("成功產生並上傳 Drive 後，會將來源表 L 欄「製單後勾選」改為 TRUE，避免下次重複製作。")
    st.info("列印設定：PDF 為 100mm × 150mm。請用實際大小 / 100% 列印，勿選 fit-to-page；正式使用前請掃描 QR code 確認可讀。")

    if "picking_orders" not in st.session_state:
        try:
            _load_orders()
        except Exception as exc:
            st.error(f"無法讀取跨境揀貨單來源表：{exc}")
            return

    orders: list[PickingOrder] = st.session_state.get("picking_orders", [])
    warnings: list[str] = st.session_state.get("picking_warnings", [])
    selected_rows: set[int] = set(st.session_state.get("picking_selected_rows", set()))
    selected_orders = filter_orders_by_rows(orders, selected_rows)

    summary = build_picking_label_summary(orders)
    selected_summary = build_picking_label_summary(selected_orders)
    st.caption(f"來源表：{summary['source_sheet']}")
    metric_cols = st.columns(4)
    metric_cols[0].metric("待製單訂單", summary["order_count"])
    metric_cols[1].metric("商品總數", summary["item_count"])
    metric_cols[2].metric("預估 PDF 頁數", selected_summary["estimated_pdf_pages"])
    metric_cols[3].metric("最後讀取", st.session_state.get("picking_loaded_at", "-"))
    st.caption("篩選條件：K 訂單狀態 = 可出貨，且 L 製單後勾選 != TRUE")
    with st.expander("QR內容 / debug summary", expanded=False):
        st.json(selected_summary)

    if warnings:
        with st.expander("系統限制與來源欄位狀態", expanded=False):
            for warning in warnings:
                st.warning(warning)

    actions = st.columns(3)
    if actions[0].button("重新讀取", use_container_width=True):
        try:
            _load_orders()
            st.rerun()
        except Exception as exc:
            st.error(f"重新讀取失敗：{exc}")
    if actions[1].button("全選", use_container_width=True, disabled=not orders):
        st.session_state["picking_selected_rows"] = {order.source_row_number for order in orders}
        st.rerun()
    if actions[2].button("取消全選", use_container_width=True, disabled=not orders):
        st.session_state["picking_selected_rows"] = set()
        st.rerun()

    if not orders:
        st.info(
            "目前沒有符合條件的待製單訂單。\n\n"
            "條件：K 訂單狀態 = 可出貨，且 L 製單後勾選 != TRUE。"
        )
        with st.expander("可能原因", expanded=False):
            st.markdown(
                "- 來源表目前沒有可出貨訂單\n"
                "- 已製單訂單已被 L 欄 TRUE 排除\n"
                "- Streamlit secrets 指向錯誤來源表\n"
                "- service account 權限不足"
            )
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

    pdf_actions = st.columns(2)
    has_selection = bool(selected_orders)
    if pdf_actions[0].button("預覽 PDF", use_container_width=True, disabled=not has_selection):
        _preview_pdf(selected_orders)

    with pdf_actions[1]:
        st.caption("成功上傳 Drive 後，才會將來源表 L 欄改為 TRUE。")
    generate_type = "primary" if has_selection else "secondary"
    if pdf_actions[1].button(
        "產生揀貨單並儲存到 Google Drive",
        type=generate_type,
        use_container_width=True,
        disabled=not has_selection,
    ):
        _generate_and_upload(selected_orders)
