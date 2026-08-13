"""
Google Sheets 操作模組
- 從來源表單取得待打單訂單（含雙重過濾防重製邏輯）
- 將結果批量回填至目標表單
"""
import os
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
import pandas as pd
import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
from safe_logging import redact_operational_log, safe_log_event

from .countries import resolve_country_code

# ── 固定常數（來自需求規格書）──────────────────────────
SOURCE_SHEET_ID = "1HDndg8GU35v6ft02pcOcfvABVt_J3rtCLfMuXWi14KM"
SOURCE_GID = "605188303"
TARGET_SHEET_ID = "1QJFFW7aWGpYX3W5nPW_HgUnVWk9AtggFvYow14BRW8U"
TARGET_GID = "465870894"


@dataclass(frozen=True)
class CompletionAuthority:
    legacy_order_ids: frozenset[str]
    exact_pairs: frozenset[tuple[str, str]]

SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

# 國家代碼對照表（完整繼承自 pa_playwright.py）
COUNTRY_CODE_MAP = {
    "UNITED STATES OF AMERICA（アメリカ合衆国）": "US",
    "CANADA（カナダ）": "CA",
    "AUSTRALIA（オーストラリア）": "AU",
    "NEW ZEALAND（ニュージーランド）": "NZ",
    "TAIWAN（台湾）": "TW",
    "HONG KONG（香港）": "HK",
    "MALAYSIA（マレーシア）": "MY",
    "SINGAPORE（シンガポール）": "SG",
    "CHINA（中国）": "CN",
    "PHILIPPINES（フィリピン）": "PH",
    "KOREA（韓国）": "KR",
    "THAILAND（タイ）": "TH",
    "UNITED KINGDOM（英国）": "EU",
    "IRELAND（アイルランド）": "EU",
    "SPAIN（西班牙）": "EU",
    "GERMANY（德國）": "EU",
    "GERMANY（ドイツ）": "EU",
    "DENMARK（丹麥）": "EU",
    "ITALY（義大利）": "EU",
    "ITALY（イタリア）": "EU",
    "ESTONIA（愛沙尼亞）": "EU",
    "NETHERLANDS（荷蘭）": "EU",
    "FRANCE（法國）": "EU",
    "PORTUGAL（葡萄牙）": "EU",
    "SWITZERLAND（瑞士）": "EU",
    "BELGIUM（比利時）": "EU",
    "BELGIUM（ベルギー）": "EU",
    "GREECE（希臘）": "EU",
    "GREECE（ギリシャ）": "EU",
    "CZECH（捷克）": "EU",
    "CZECH（チェコ）": "EU",
    "ROMANIA（ルーマニア）": "EU",
    "CYPRUS（キプロス）": "EU",
    "INDONESIA（インドネシア）": "ID",
}


def _shipping_priority(value: str) -> int:
    text = str(value or "").strip()
    lowered = text.lower()
    if "ems" in lowered:
        return 30
    if "國際小包" in text or "国際小包" in text or "postal parcel" in lowered:
        return 20
    if "epacket" in lowered or "eパケット" in lowered:
        return 10
    return 0


_ITEM_COLUMN_LIMIT = 10


def _clean_cell(value) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _source_row_fingerprint(row: pd.Series) -> str:
    payload = {
        str(column): _clean_cell(value)
        for column, value in row.items()
        if not str(column).startswith("_")
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _prefer_shipping_method_rows(
    df: pd.DataFrame,
    order_id_col: str,
    shipping_col: str,
) -> pd.DataFrame:
    if df.empty or order_id_col not in df.columns:
        return df
    if shipping_col not in df.columns:
        return df.drop_duplicates(subset=[order_id_col], keep="first")

    ranked = df.copy()
    ranked["_source_order"] = range(len(ranked))
    ranked["_shipping_priority"] = ranked[shipping_col].map(_shipping_priority)
    max_priority = ranked.groupby(order_id_col)["_shipping_priority"].transform("max")
    ranked = ranked[ranked["_shipping_priority"] == max_priority]
    ranked = ranked.sort_values("_source_order").drop_duplicates(
        subset=[order_id_col],
        keep="first",
    )
    return ranked.drop(columns=["_source_order", "_shipping_priority"])


def _dataframe_sensitive_log_values(df: pd.DataFrame) -> tuple[str, ...]:
    if not isinstance(df, pd.DataFrame) or df.empty:
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
    for column in df.columns:
        column_name = str(column).strip().lower()
        is_status_column = "製單上傳狀態" in column_name
        if not is_status_column and not any(
            marker in column_name for marker in sensitive_markers
        ):
            continue
        for value in df[column].tolist():
            text = _clean_cell(value)
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


def _looks_like_jp_tracking(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Z]{2}\d{9}JP", str(value or "").strip()))


def _filter_pending_orders_dataframe(
    df: pd.DataFrame,
    completed_ids: set[str] | None = None,
    log_cb=None,
) -> pd.DataFrame:
    sensitive_values = _dataframe_sensitive_log_values(df)

    def _log(msg):
        safe_message = redact_operational_log(
            msg,
            sensitive_values=sensitive_values,
        )
        if log_cb:
            log_cb(safe_message)
        else:
            logging.info(safe_message)

    if df.empty:
        return df

    df = df.copy()
    if "_source_row_number" not in df.columns:
        df["_source_row_number"] = [str(index) for index in range(len(df))]

    status_col = "製單上傳狀態(請用[未打單]檢視模式)"
    amount_col = "郵局申告金額(USD)"
    order_id_col = "注文番号(貼上原始資料)"
    backup_order_id_col = "注文番号(貼上原始資料)_1"
    check_col = "製單檢核"
    shipping_col = "郵局運送方式(複數商品請自行確認是否走小包)"
    shipname_col = "Shipping Name" if "Shipping Name" in df.columns else "Shipping Name_1"

    for col in [status_col, amount_col, order_id_col, check_col, shipname_col]:
        if col in df.columns:
            df[col] = df[col].fillna("").astype(str).str.strip()

    if backup_order_id_col in df.columns:
        df[order_id_col] = (
            df[order_id_col].replace("", pd.NA)
            .fillna(df[backup_order_id_col])
            .fillna("")
        )
    if "Shipping Name_1" in df.columns:
        df["Shipping Name"] = (
            df.get("Shipping Name", pd.Series(dtype=str))
            .replace("", pd.NA)
            .fillna(df["Shipping Name_1"])
            .fillna("")
        )
        shipname_col = "Shipping Name"

    has_target_authority = completed_ids is not None
    completed_id_set = completed_ids or set()
    stale_source_status_mask = (
        df[status_col].map(_looks_like_jp_tracking)
        & has_target_authority
        & ~df[order_id_col].isin(completed_id_set)
    )
    if stale_source_status_mask.any():
        stale_rows = df[stale_source_status_mask]
        _log(
            "🛑 來源狀態疑似快取過期，目標表缺少完成證據，"
            f"阻擋自動製單 {len(stale_rows)} 筆"
        )

    status_pending_mask = (df[status_col] == "未打單")
    amount_present_mask = df[amount_col] != ""
    for index in range(1, _ITEM_COLUMN_LIMIT + 1):
        item_amount_col = f"申告金額{index}"
        if item_amount_col in df.columns:
            amount_present_mask = amount_present_mask | (
                df[item_amount_col].fillna("").astype(str).str.strip() != ""
            )
    base_mask = (
        status_pending_mask
        & amount_present_mask
        & (df[check_col].str.upper() != "TRUE")
        & (df[shipname_col] != "")
    )
    watched_mask = df[order_id_col].str.contains("WhoWhy|WhoWht", case=False, na=False, regex=True)
    watched_rows = df[watched_mask]
    if not watched_rows.empty:
        watched_pass_count = int(base_mask[watched_mask].sum())
        watched_fail_count = len(watched_rows) - watched_pass_count
        _log(
            f"🧪 關注訂單診斷（WhoWhy/WhoWht）："
            f"{len(watched_rows)} 筆，PASS={watched_pass_count}，FAIL={watched_fail_count}"
        )
    excluded = df[~base_mask]
    if not excluded.empty:
        _log(
            f"🔎 基礎篩選排除：{len(excluded)} 筆"
        )
        reason_masks = [
            ("狀態不是未打單排除", ~status_pending_mask),
            ("來源 tracking 但目標缺少完成證據，阻擋", stale_source_status_mask),
            ("申告金額空白排除", ~amount_present_mask),
            ("製單檢核 TRUE 排除", df[check_col].str.upper() == "TRUE"),
            ("Shipping Name 空白排除", df[shipname_col] == ""),
        ]
        for label, reason_mask in reason_masks:
            rows = df[reason_mask]
            if not rows.empty:
                _log(f"   - {label}：{len(rows)} 筆")
    df_filtered = df[base_mask].copy()
    _log(f"📋 篩選後（未打單+必填）：{len(df_filtered)} 筆")

    if completed_id_set:
        completed_mask = df_filtered[order_id_col].isin(completed_id_set)
        completed_rows = df_filtered[completed_mask]
        if not completed_rows.empty:
            _log(
                "🔥 已在目標表完成而排除 "
                f"{len(completed_rows)} 筆"
            )
        before_completed = len(df_filtered)
        df_filtered = df_filtered[~completed_mask]
        _log(
            f"🔥 雙重過濾（已完成 {len(completed_ids)} 筆）："
            f"{before_completed} → {len(df_filtered)} 筆"
        )

    before_dedup = len(df_filtered)
    df_filtered = _prefer_shipping_method_rows(
        df_filtered,
        order_id_col=order_id_col,
        shipping_col=shipping_col,
    )
    _log(
        f"✅ 來源內同注文番号去重：{before_dedup} → {len(df_filtered)} 筆"
    )
    return df_filtered.reset_index(drop=True)


def _get_gspread_client() -> gspread.Client:
    """建立 gspread 客戶端（從 Streamlit secrets 讀取服務帳號）"""
    try:
        creds_info = dict(st.secrets["gcp_service_account"])
    except Exception:
        # 備案：從環境變數或本地 credentials.json
        import json
        creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "credentials.json")
        with open(creds_path, "r", encoding="utf-8") as f:
            creds_info = json.load(f)

    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    return gspread.authorize(creds)


def _get_worksheet_by_gid(spreadsheet, gid: str):
    try:
        return spreadsheet.get_worksheet_by_id(int(str(gid).strip()))
    except Exception:
        return None


def read_completion_authority(
    client: gspread.Client | None = None,
) -> CompletionAuthority:
    """Read legacy order IDs and verified order/tracking pairs from target C:D."""
    client = client or _get_gspread_client()
    spreadsheet = client.open_by_key(TARGET_SHEET_ID)
    worksheet = _get_worksheet_by_gid(spreadsheet, TARGET_GID)
    if worksheet is None:
        raise RuntimeError("target worksheet unavailable")
    values = worksheet.get("C:D")
    legacy_order_ids: set[str] = set()
    exact_pairs: set[tuple[str, str]] = set()
    for row in values[1:]:
        order_id = _clean_cell(row[0]) if row else ""
        tracking = _clean_cell(row[1]) if len(row) > 1 else ""
        if order_id:
            legacy_order_ids.add(order_id)
        if order_id and tracking:
            exact_pairs.add((order_id, tracking))
    return CompletionAuthority(
        legacy_order_ids=frozenset(legacy_order_ids),
        exact_pairs=frozenset(exact_pairs),
    )


def read_completed_order_ids(client: gspread.Client | None = None) -> set[str]:
    """Compatibility view of the legacy order-level completion authority."""
    return set(read_completion_authority(client).legacy_order_ids)


def _pending_read_failure(exc: Exception, log_cb, *, strict: bool) -> pd.DataFrame:
    safe_log_event(
        log_cb or logging.error,
        "pending_read_failed",
        error_type=type(exc).__name__,
    )
    if strict:
        if isinstance(exc, PermissionError):
            code = "pending_read_permission_denied"
        elif isinstance(exc, (TimeoutError, ConnectionError)):
            code = "pending_read_network"
        else:
            error_name = type(exc).__name__.lower()
            if any(token in error_name for token in ("permission", "forbidden", "unauthorized")):
                code = "pending_read_permission_denied"
            elif any(token in error_name for token in ("timeout", "connection", "network")):
                code = "pending_read_network"
            else:
                code = "pending_read_failed"
        raise RuntimeError(code) from None
    return pd.DataFrame()


def get_pending_orders(
    log_cb=None,
    *,
    strict: bool = False,
    exclude_completed: bool = True,
) -> pd.DataFrame:
    """
    從來源表單取得待打單清單，並執行雙重過濾防重製：
    1. 篩選狀態為「未打單」且必要欄位不為空
    2. 即時讀取目標表單 C 欄已完成單號集合，在記憶體中剔除重複
    3. 對來源本身的注文番号去重複

    回傳: pandas DataFrame，若無資料則為空 DataFrame
    """
    sensitive_values: list[str] = []

    def _log(msg):
        safe_message = redact_operational_log(
            msg,
            sensitive_values=sensitive_values,
        )
        if log_cb:
            log_cb(safe_message)
        else:
            logging.info(safe_message)

    try:
        started_at = time.perf_counter()
        client = _get_gspread_client()

        # ── 讀取來源表單 ────────────────────────────────
        source_started_at = time.perf_counter()
        sh_source = client.open_by_key(SOURCE_SHEET_ID)
        ws_source = _get_worksheet_by_gid(sh_source, SOURCE_GID)
        if not ws_source:
            raise RuntimeError("source worksheet unavailable")

        _log(f"🌐 讀取來源表單：{sh_source.title}")
        all_values = ws_source.get_all_values()
        _log(
            "⏱️ 來源表 API 讀取完成："
            f"{len(all_values)} 列，耗時 {time.perf_counter() - source_started_at:.1f}s"
        )
        if len(all_values) < 2:
            _log("ℹ️ 來源表單無資料列")
            return pd.DataFrame()

        # 處理標題（去空格、處理重複欄名）
        raw_header = [str(c).strip() for c in all_values[0]]
        header, counts = [], {}
        for col in raw_header:
            if col in counts:
                counts[col] += 1
                header.append(f"{col}_{counts[col]}")
            else:
                counts[col] = 0
                header.append(col)

        df = pd.DataFrame(all_values[1:], columns=header)
        df["_source_row_number"] = [str(row_number) for row_number in range(2, len(df) + 2)]
        df["_source_fingerprint"] = df.apply(_source_row_fingerprint, axis=1)
        sensitive_values.extend(_dataframe_sensitive_log_values(df))
        _log(f"📊 來源原始筆數：{len(df)}")
        order_id_col = "注文番号(貼上原始資料)"
        source_order_count = 0
        if order_id_col in df.columns:
            source_order_count = int(df[order_id_col].fillna("").astype(str).str.strip().ne("").sum())
        _log(f"🧾 來源有效注文番号：{source_order_count} 筆")

        # ── 🔥 雙重過濾：即時讀取目標表單已完成單號 ──────
        completed_ids: set[str] = set()
        if exclude_completed:
            try:
                target_started_at = time.perf_counter()
                completed_ids = read_completed_order_ids(client)
                _log(
                    "⏱️ 目標表 C 欄讀取完成："
                    f"{len(completed_ids)} 個完成單號，耗時 {time.perf_counter() - target_started_at:.1f}s"
                )
            except Exception as exc:
                if strict:
                    raise
                return _pending_read_failure(exc, log_cb, strict=strict)

        df_filtered = _filter_pending_orders_dataframe(
            df,
            completed_ids=completed_ids,
            log_cb=_log,
        )
        _log(
            f"✅ 最終可打單：{len(df_filtered)} 筆，總讀取耗時 {time.perf_counter() - started_at:.1f}s"
        )

        return df_filtered

    except Exception as exc:
        return _pending_read_failure(exc, log_cb, strict=strict)


def backfill_results(results: list[dict], log_cb=None):
    """
    將成功打單結果批量回填至目標表單 GID 465870894。
    每筆結果格式：{"name", "order_id", "tracking", "country_raw", "date"}
    """
    sensitive_values = tuple(
        value
        for result in results or []
        for value in (
            result.get("order_id"),
            result.get("tracking"),
            result.get("name"),
            result.get("country_raw"),
        )
        if _clean_cell(value)
    )

    def _log(msg):
        safe_message = redact_operational_log(msg, sensitive_values=sensitive_values)
        if log_cb:
            log_cb(safe_message)
        else:
            logging.info("%s", safe_message)

    if not results:
        _log("ℹ️ 無需回填（results 為空）")
        return {"ok": True, "written": 0, "failed": [], "error": ""}

    try:
        client = _get_gspread_client()
        sh = client.open_by_key(TARGET_SHEET_ID)
        ws = next(
            (w for w in sh.worksheets() if str(w.id) == TARGET_GID), None
        )
        if not ws:
            safe_log_event(
                log_cb or (lambda message: logging.error("%s", message)),
                "writeback_initialization_failed",
                reason="writeback_api_error",
            )
            return {
                "ok": False,
                "written": 0,
                "failed": [str(r.get("order_id") or "") for r in results],
                "error": "writeback_api_error",
            }

        # 找最後一列
        col_b = ws.col_values(2)
        last_row = len(col_b)
        while last_row > 0 and not str(col_b[last_row - 1]).strip():
            last_row -= 1
        start_row = last_row + 1
        _log(f"📍 從第 {start_row} 列開始回填 {len(results)} 筆")

        batch = []
        for i, r in enumerate(results):
            row_n = start_row + i
            country_raw = r.get("country_raw", "")
            country_code = resolve_country_code(country_raw, COUNTRY_CODE_MAP) or country_raw
            batch.append({
                "range": f"B{row_n}:D{row_n}",
                "values": [[r.get("name", ""), r.get("order_id", ""), r.get("tracking", "")]],
            })
            batch.append({
                "range": f"J{row_n}:J{row_n}",
                "values": [[country_code]],
            })

        ws.batch_update(batch, value_input_option="USER_ENTERED")
        written_order_ids = ws.col_values(3)
        written_tracking = ws.col_values(4)
        written_pairs = {
            (_clean_cell(order_id), _clean_cell(tracking))
            for order_id, tracking in zip(written_order_ids[1:], written_tracking[1:])
            if _clean_cell(order_id)
        }
        missing = [
            str(result.get("order_id") or "")
            for result in results
            if (_clean_cell(result.get("order_id")), _clean_cell(result.get("tracking"))) not in written_pairs
        ]
        if missing:
            safe_log_event(
                log_cb or (lambda message: logging.error("%s", message)),
                "writeback_batch_finished",
                count=len(results),
                reason="partial_write",
            )
            return {
                "ok": False,
                "written": len(results),
                "failed": missing,
                "error": "writeback_readback_failed",
            }
        _log(f"🚀 回填完成並驗證：{len(results)} 筆")
        return {"ok": True, "written": len(results), "failed": [], "error": ""}

    except Exception as exc:
        if isinstance(exc, PermissionError):
            reason = "writeback_permission_denied"
        elif isinstance(exc, (TimeoutError, ConnectionError)):
            reason = "writeback_network_error"
        else:
            reason = "writeback_api_error"
        safe_log_event(
            log_cb or (lambda message: logging.error("%s", message)),
            "writeback_initialization_failed",
            reason=reason,
            error_type=type(exc).__name__,
        )
        return {
            "ok": False,
            "written": 0,
            "failed": [str(r.get("order_id") or "") for r in results],
            "error": reason,
        }


def load_sheet_values(spreadsheet_id: str, sheet_name: str) -> list[list[str]]:
    """Load all values from a named worksheet."""
    client = _get_gspread_client()
    sh = client.open_by_key(spreadsheet_id)
    ws = sh.worksheet(sheet_name)
    return ws.get_all_values()


def build_picking_done_updates(row_numbers: list[int], checked_value: str = "已製單") -> list[dict]:
    """Build L-column updates for picking-label completion checkboxes."""
    return [
        {"range": f"L{int(row_number)}:L{int(row_number)}", "values": [[checked_value]]}
        for row_number in sorted(set(row_numbers))
    ]


def batch_mark_picking_done(spreadsheet_id: str, sheet_name: str, row_numbers: list[int], checked_value: str = "已製單") -> None:
    """Set L column 製單後勾選 to the checked value for the given original sheet rows."""
    if not row_numbers:
        return
    client = _get_gspread_client()
    sh = client.open_by_key(spreadsheet_id)
    ws = sh.worksheet(sheet_name)
    updates = build_picking_done_updates(row_numbers, checked_value=checked_value)
    ws.batch_update(updates, value_input_option="USER_ENTERED")
