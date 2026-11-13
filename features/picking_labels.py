"""Streamlit UI for cross-border picking labels."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import tempfile
import os
import sys

import pandas as pd
import streamlit as st
import auth

from bot.drive import list_drive_files, reserve_picking_file_id, upload_reserved_picking_pdf, get_reserved_picking_pdf
from bot.picking_labels import (
    PICKING_OUTPUT_DRIVE_FOLDER_ID,
    PICKING_SOURCE_SHEET_NAME,
    PICKING_SOURCE_SPREADSHEET_ID,
    PICKING_SOURCE_WORKSHEET_ID,
    picking_order_key,
    picking_render_fingerprint,
    SHIPPING_STATUS_SHEET_NAME,
    SHIPPING_STATUS_SPREADSHEET_ID,
    PickingOrder,
    build_shipping_deadline_lookup,
    build_picking_label_summary,
    build_picking_source_diagnostics,
    parse_picking_label_candidates,
    _has_anchored_picking_schema,
)
from bot.picking_pdf import get_registered_cjk_font_info, render_picking_labels_pdf
from bot.sheets import load_sheet_values, load_picking_sheet_values, mark_picking_sheet_rows, mark_picking_done_verified
from bot.picking_journal import PickingJournal, picking_source_snapshot, classify_picking_snapshot, execute_picking_batch, PickingValidationError
from bot.shipment_ledger import configured_ledger, global_writer_lock, LedgerUnavailable
from refresh_payloads import PickingPayload, copy_picking_payload
from local_time import JST, format_jst


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


def _source_snapshot():
    if _picking_source_spreadsheet_id() != PICKING_SOURCE_SPREADSHEET_ID:
        raise PickingValidationError("揀貨來源 spreadsheet 設定與已綁定來源不符，已停止。")
    values = load_picking_sheet_values(PICKING_SOURCE_SPREADSHEET_ID, PICKING_SOURCE_WORKSHEET_ID)
    if not values or not _has_anchored_picking_schema(values[0]):
        raise PickingValidationError("來源表 K:Q 必需欄位已改名或移位，已停止。請管理員核對來源欄位，不可改用前方重複欄位。")
    statuses = load_sheet_values(_shipping_status_spreadsheet_id(), _shipping_status_sheet_name())
    return values, picking_source_snapshot(values, build_shipping_deadline_lookup(statuses))


def load_picking_payload() -> PickingPayload:
    values, snapshot = _source_snapshot()
    _candidates, warnings = parse_picking_label_candidates(values)
    diagnostics = build_picking_source_diagnostics(values, _candidates, warnings)
    try:
        classified = classify_picking_snapshot(snapshot, PickingJournal(configured_ledger()))
        orders = [entry['order'] for entry in classified['pending']]
        diagnostics['picking_recovery'] = classified['mirror_pending']
        diagnostics['picking_review'] = classified['review']
    except LedgerUnavailable:
        orders = []
        diagnostics['picking_blocked'] = (
            "揀貨製作紀錄無法讀取，已停止產生與補標。請管理員確認既有 "
            "SHIPMENT_LEDGER_SECRET 或 OAuth cookie secret（至少 32 字元），並保留原密鑰。")
    diagnostics.update(source_spreadsheet_id=PICKING_SOURCE_SPREADSHEET_ID,
                       source_sheet=_picking_source_sheet_name(),
                       source_worksheet_id=PICKING_SOURCE_WORKSHEET_ID,
                       shipping_status_spreadsheet_id=_shipping_status_spreadsheet_id(),
                       shipping_status_sheet=_shipping_status_sheet_name(),
                       pdf_cjk_font=get_registered_cjk_font_info())
    return PickingPayload(tuple(orders), tuple(warnings), diagnostics)


def apply_picking_payload(
    payload: PickingPayload,
    *,
    preserve_selection: bool,
    loaded_at: datetime | None = None,
) -> None:
    copied = copy_picking_payload(payload)
    old_orders = st.session_state.get("picking_orders", ())
    old_fingerprints = {picking_order_key(order): picking_render_fingerprint(order) for order in old_orders}
    current = {picking_order_key(order): order for order in copied.orders}
    old_manifest = [(picking_order_key(order), picking_render_fingerprint(order), order.source_row_number) for order in old_orders]
    new_manifest = [(picking_order_key(order), picking_render_fingerprint(order), order.source_row_number) for order in copied.orders]
    if old_manifest != new_manifest:
        st.session_state["picking_editor_epoch"] = st.session_state.get("picking_editor_epoch", 0) + 1
    if preserve_selection:
        selected_keys = set(st.session_state.get("picking_selected_keys", set()))
        selected_keys = {key for key in selected_keys if key in current
                         and old_fingerprints.get(key) == picking_render_fingerprint(current[key])}
    else:
        selected_keys = set(current)
    st.session_state["picking_orders"] = copied.orders
    st.session_state["picking_warnings"] = copied.warnings
    st.session_state["picking_diagnostics"] = copied.diagnostics
    st.session_state["picking_selected_keys"] = selected_keys
    # Row set is retained for display/legacy diagnostics only.
    st.session_state["picking_selected_rows"] = {current[key].source_row_number for key in selected_keys}
    if loaded_at is not None:
        st.session_state["picking_loaded_at"] = format_jst(loaded_at)


def _load_orders() -> None:
    payload = load_picking_payload()
    apply_picking_payload(
        payload,
        preserve_selection="picking_orders" in st.session_state,
        loaded_at=datetime.now(JST),
    )


def _orders_to_dataframe(orders: list[PickingOrder], selected_keys: set[str]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "選取": picking_order_key(order) in selected_keys,
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
    selected_keys = {
        picking_order_key(orders[position])
        for position, (_, row) in enumerate(edited_df.iterrows())
        if position < len(orders) and bool(row.get("選取"))
    }
    st.session_state["picking_selected_keys"] = selected_keys
    selected = [order for order in orders if picking_order_key(order) in selected_keys]
    st.session_state["picking_selected_rows"] = {order.source_row_number for order in selected}
    return selected


def _preview_pdf(selected_orders: list[PickingOrder]) -> None:
    if not selected_orders:
        st.warning("請先選取至少一筆訂單。")
        return
    try:
        with tempfile.TemporaryDirectory(prefix="jppost-picking-preview-") as preview_dir:
            preview_path = Path(preview_dir) / "preview.pdf"
            result = render_picking_labels_pdf(selected_orders, str(preview_path))
            preview_bytes = preview_path.read_bytes()
    except Exception:
        st.error("無法產生預覽 PDF，請聯絡系統管理員。")
        return
    st.success(f"預覽 PDF 已產生：{result.total_orders} 筆訂單，{result.total_pages} 頁。")
    st.download_button(
        "下載預覽 PDF",
        data=preview_bytes,
        file_name="preview-揀貨標籤.pdf",
        mime="application/pdf",
        width="stretch",
    )
    if auth.can_view_diagnostics():
        with st.expander("預覽 / debug 摘要"):
            st.json(build_picking_label_summary(selected_orders))


def _generate_and_upload(selected_orders: list[PickingOrder]) -> bool:
    if not selected_orders:
        st.warning("請先選取至少一筆訂單。")
        return False
    try:
        lock = global_writer_lock()
        journal = PickingJournal(configured_ledger(lock))
        def source_loader():
            return _source_snapshot()[1]
        def mark_done(orders):
            return mark_picking_done_verified(
                orders, source_loader=source_loader,
                write_rows=lambda rows: mark_picking_sheet_rows(PICKING_SOURCE_SPREADSHEET_ID, rows, PICKING_SOURCE_WORKSHEET_ID),
            )
        with tempfile.TemporaryDirectory(prefix="jppost-picking-output-") as output_dir:
            result = execute_picking_batch(
                selected_orders, output_dir=output_dir, source_loader=source_loader,
                journal=journal, lock=lock,
                list_files=lambda prefix: list_drive_files(_picking_output_drive_folder_id(), prefix),
                reserve_file_id=reserve_picking_file_id,
                upload_file=lambda path, record: upload_reserved_picking_pdf(path, _picking_output_drive_folder_id(), record),
                get_file=get_reserved_picking_pdf, mark_done=mark_done,
                folder_id=_picking_output_drive_folder_id(), now=datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
            )
    except LedgerUnavailable as exc:
        code = getattr(exc, "reason_code", "") or "unknown"
        print(f"[PICKING_ERROR] LedgerUnavailable reason={code}", file=sys.stderr)
        st.error(
            f"揀貨製作紀錄或共用鎖無法確認，已停止（代碼：{code}）。"
            "請先按「重新讀取」確認是否已產生 PDF；已產生者只能補標，勿重新產生。"
            "若確認無人製單而鎖仍卡住，可展開頁面上方「製單卡住了？」解除共用鎖。")
        return False
    except PickingValidationError as exc:
        st.error(str(exc))
        return False
    except Exception as exc:
        print(f"[PICKING_ERROR] unexpected type={type(exc).__name__}", file=sys.stderr)
        st.error("揀貨製作未能確認，請重新讀取製作紀錄；已存在的 PDF 只會補標。")
        return False
    if not result.success:
        if result.drive_file:
            st.link_button("開啟原 PDF／重印", result.drive_file["webViewLink"])
        st.error(result.error)
        return False
    # 成功說明存入 session，讓畫面自動重整（已製單訂單移出清單）後仍保留顯示。
    st.session_state["picking_last_batch"] = {
        "filename": result.filename,
        "link": (result.drive_file or {}).get("webViewLink", ""),
        "orders": [(o.order_source, o.order_no) for o in selected_orders],
        "finished_at": datetime.now(JST).strftime("%H:%M:%S"),
    }
    return True


def _render_last_batch() -> None:
    batch = st.session_state.get("picking_last_batch")
    if not batch:
        return
    count = len(batch["orders"])
    st.success(
        f"最近完成批次：{batch['filename']}｜{count} 筆訂單｜{batch['finished_at']}｜"
        "PDF 已上傳雲端資料夾，來源表已讀回確認為已製單。")
    if batch.get("link"):
        st.link_button("開啟原 PDF／重印", batch["link"], key="picking_last_batch_link")
    with st.expander(f"本批訂單明細（{count} 筆）", expanded=False):
        st.dataframe(
            pd.DataFrame([{"訂單來源": src, "注文番号": no} for src, no in batch["orders"]]),
            hide_index=True, width="stretch")


def _is_source_identity_review(item: dict) -> bool:
    """Keep unaddressable source identity rows in diagnostics, preserving receipts."""
    order = item['order']
    return not item.get('file_id') and (not order.order_source.strip() or not order.order_no.strip())


def _is_quiet_review(item: dict) -> bool:
    """無 PDF 紀錄的核對項目不逐筆佔用前台；只在讀取診斷中列出。

    有 PDF 檔案 ID 的項目仍須在前台提供原檔連結，故不屬於此類。
    """
    return not item.get('file_id') and not _is_source_identity_review(item)


def render_picking_label_tab(refresh_source=None) -> None:
    st.info("列印設定：PDF檔尺寸為 100mm × 150mm，請使用對應Label大小輸出。")

    has_loaded_orders = "picking_orders" in st.session_state

    orders: list[PickingOrder] = st.session_state.get("picking_orders", [])
    selected_keys = set(st.session_state.get("picking_selected_keys", set()))
    selected_orders = [order for order in orders if picking_order_key(order) in selected_keys]

    summary = build_picking_label_summary(orders)
    st.markdown(
        """
        <style>
        .picking-status-row {
            display: grid;
            grid-template-columns: minmax(180px, 0.35fr) minmax(260px, 1fr);
            gap: 12px;
            align-items: center;
            margin: 0.1rem 0 0.6rem;
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
            display: flex;
            align-items: center;
            gap: 0.5rem;
            color: #cbd5e1;
            font-size: 0.9rem;
            line-height: 1.2;
            min-height: 28px;
        }
        .picking-loaded-label {
            color: #f59e0b;
            font-size: 0.88rem;
            font-weight: 800;
        }
        .picking-loaded-panel strong {
            color: #f8fafc;
            font-size: 0.94rem;
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

    # 警示與補標操作含一般按鈕，不能放進表單，因此放在表單上方。
    diagnostics = st.session_state.get("picking_diagnostics", {})
    if diagnostics.get("picking_blocked"):
        st.error(diagnostics["picking_blocked"])
    quiet_blocked = [
        item for item in diagnostics.get("picking_review", [])
        if _is_quiet_review(item) and item.get("done") != "DONE"
    ]
    if quiet_blocked:
        # 只給一行摘要，不逐筆刷版；明細在「讀取診斷」。
        st.caption(f"另有 {len(quiet_blocked)} 筆未製單訂單因資料與歷史紀錄不一致已排除，明細見「讀取診斷」。")
    for item in diagnostics.get("picking_review", []):
        if _is_quiet_review(item) or _is_source_identity_review(item):
            continue
        st.warning(f"{item['order'].order_source} / {item['order'].order_no}：{item['review']}")
        if item.get('file_id'):
            st.link_button("開啟原 PDF／重印", f"https://drive.google.com/file/d/{item['file_id']}/view", key=f"picking_review_{item['key']}")
    for item in diagnostics.get("picking_recovery", []):
        check_upload = item.get('recovery_kind') == 'check_upload'
        if check_upload:
            st.warning(f"{item['order'].order_no}：上傳結果未確認。只會核對原預留檔案；不會重新產生或上傳 PDF。")
        else:
            st.warning(f"{item['order'].order_no}：PDF 已存在。L 未確認或被重設，可開原檔重印；補標不會產生新 PDF。")
            st.link_button("開啟原 PDF／重印", f"https://drive.google.com/file/d/{item['file_id']}/view", key=f"picking_original_{item['key']}")
        repair_label = "核對原上傳結果／補標" if check_upload else "只補標此訂單"
        if st.button(repair_label, key=f"picking_repair_{item['key']}"):
            if _generate_and_upload([item['order']]):
                _load_orders()
                st.rerun()

    # 勾選與四個按鈕放在同一個表單：勾選變動不會觸發重新整理，
    # 按下任一按鈕時才一次送出畫面上最後的勾選狀態（與郵便頁一致）。
    # 製作結果與錯誤訊息顯示在按鈕列上方，避免被長表格擠到畫面外。
    result_slot = st.container()
    with result_slot:
        _render_last_batch()
    with st.form("picking_selection_form", border=False):
        action_slot = st.container()
        st.caption("成功上傳雲端資料夾後，才會勾選來源表製單檢核欄。")
        table_slot = st.container()
        selected_orders: list[PickingOrder] = []
        with table_slot:
            if not has_loaded_orders:
                st.info("尚未讀取跨境揀貨單資料。請按「重新讀取」取得目前待製單訂單。")
            elif not orders:
                st.info("目前沒有符合條件的待製單訂單。")
            else:
                df = _orders_to_dataframe(orders, selected_keys)
                edited_df = st.data_editor(
                    df,
                    hide_index=True,
                    width="stretch",
                    disabled=[col for col in df.columns if col != "選取"],
                    column_config={"選取": st.column_config.CheckboxColumn("選取")},
                    key=f"picking_order_editor_{st.session_state.get('picking_editor_epoch', 0)}",
                )
                selected_orders = _selected_orders_from_editor(orders, edited_df)
        with action_slot:
            actions = st.columns([1.35, 1, 1, 1], vertical_alignment="bottom")
            generate_clicked = actions[0].form_submit_button(
                "產生揀貨單",
                type="primary",
                width="stretch",
                disabled=not orders,
            )
            reload_clicked = actions[1].form_submit_button("重新讀取", width="stretch")
            select_all_clicked = actions[2].form_submit_button("全選", width="stretch", disabled=not orders)
            clear_all_clicked = actions[3].form_submit_button("取消全選", width="stretch", disabled=not orders)

    if generate_clicked:
        st.session_state.pop("picking_last_batch", None)
        with result_slot:
            generated = _generate_and_upload(selected_orders)
        if generated:
            # 回讀來源後自動重整：已製單訂單移出清單，只留下未製的揀貨單。
            refreshed_ok = False
            try:
                if refresh_source is None:
                    _load_orders()
                else:
                    refresh_result = refresh_source("picking", force=True)
                    if refresh_result.data is not None:
                        apply_picking_payload(
                            refresh_result.data,
                            preserve_selection=True,
                            loaded_at=refresh_result.status.loaded_at,
                        )
                        st.session_state["picking_snapshot_loaded_at"] = refresh_result.status.loaded_at
                refreshed_ok = True
            except Exception:
                with result_slot:
                    st.warning("已製單完成，但自動重新整理失敗，請按「重新讀取」。")
            if refreshed_ok:
                st.rerun()
    if reload_clicked:
        try:
            if refresh_source is None:
                _load_orders()
            else:
                refresh_result = refresh_source("picking", force=True)
                if refresh_result.data is None:
                    raise RuntimeError("picking_refresh_failed")
                apply_picking_payload(
                    refresh_result.data,
                    preserve_selection=True,
                    loaded_at=refresh_result.status.loaded_at,
                )
                st.session_state["picking_snapshot_loaded_at"] = refresh_result.status.loaded_at
            st.rerun()
        except Exception:
            with result_slot:
                st.error("重新讀取失敗，請稍後再試。")
    if select_all_clicked:
        st.session_state["picking_selected_keys"] = {picking_order_key(order) for order in orders}
        st.session_state["picking_selected_rows"] = {order.source_row_number for order in orders}
        st.rerun()
    if clear_all_clicked:
        st.session_state["picking_selected_keys"] = set()
        st.session_state["picking_selected_rows"] = set()
        st.rerun()


def render_picking_label_diagnostics_panel() -> None:
    if not auth.can_view_diagnostics():
        return
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
        "excluded because P logistics not allowed": diagnostics.get("excluded_because_logistics_not_allowed", 0),
        "allowed logistics keywords": diagnostics.get("allowed_logistics_keywords", []),
        "parser / unknown exclusion count": diagnostics.get("parser_unknown_exclusion_count", 0),
        "actual detected L raw values sample": diagnostics.get("done_raw_values_sample", []),
        "filter condition": diagnostics.get("filter_condition", ""),
    }
    st.json(meta_rows)

    identity_reviews = [item for item in diagnostics.get("picking_review", []) if _is_source_identity_review(item)]
    if identity_reviews:
        with st.expander(f"來源身份不完整｜已排除 {len(identity_reviews)} 筆", expanded=False):
            st.dataframe(pd.DataFrame([
                {"來源列": item.get('row', item['order'].source_row_number),
                 "訂單來源": item['order'].order_source,
                 "注文番号": item['order'].order_no,
                 "原因": item['review']}
                for item in identity_reviews
            ]), hide_index=True, width="stretch")

    quiet_reviews = [item for item in diagnostics.get("picking_review", []) if _is_quiet_review(item)]
    if quiet_reviews:
        with st.expander(f"資料不一致核對｜{len(quiet_reviews)} 筆（已製單者不影響製單）", expanded=False):
            st.dataframe(pd.DataFrame([
                {"來源列": item.get('row', item['order'].source_row_number),
                 "訂單來源": item['order'].order_source,
                 "注文番号": item['order'].order_no,
                 "L 狀態": "已製單" if item.get('done') == 'DONE' else "未製單（已排除）",
                 "原因": item['review']}
                for item in quiet_reviews
            ]), hide_index=True, width="stretch")

    font_info = diagnostics.get("pdf_cjk_font", {})
    if font_info:
        with st.expander("PDF CJK 字型診斷", expanded=False):
            st.json(font_info)

    duplicate_headers = diagnostics.get("duplicate_header_diagnostics", [])
    if duplicate_headers:
        with st.expander("重複欄名錨定狀態", expanded=False):
            st.dataframe(pd.DataFrame(duplicate_headers), hide_index=True, width="stretch")

    included = diagnostics.get("included_candidate_samples", [])
    if included:
        with st.expander("前 20 筆已納入候選訂單（M:P / K:L）", expanded=False):
            st.dataframe(pd.DataFrame(included), hide_index=True, width="stretch")

    logistics_exclusions = diagnostics.get("logistics_filter_exclusions", [])
    if logistics_exclusions:
        with st.expander("前 20 筆因物流方式被排除的來源列", expanded=False):
            st.dataframe(pd.DataFrame(logistics_exclusions), hide_index=True, width="stretch")

    exclusions = diagnostics.get("near_candidate_exclusions", [])
    if exclusions:
        with st.expander("前 5 筆接近條件但被排除的來源列", expanded=False):
            st.dataframe(pd.DataFrame(exclusions), hide_index=True, width="stretch")

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
