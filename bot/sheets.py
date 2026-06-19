"""
Google Sheets 操作模組
- 從來源表單取得待打單訂單（含雙重過濾防重製邏輯）
- 將結果批量回填至目標表單
"""
import os
import logging
import time
import pandas as pd
import streamlit as st
import gspread
from google.oauth2.service_account import Credentials

# ── 固定常數（來自需求規格書）──────────────────────────
SOURCE_SHEET_ID = "1HDndg8GU35v6ft02pcOcfvABVt_J3rtCLfMuXWi14KM"
SOURCE_GID = "605188303"
TARGET_SHEET_ID = "1QJFFW7aWGpYX3W5nPW_HgUnVWk9AtggFvYow14BRW8U"
TARGET_GID = "465870894"

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


def _format_sample(values, limit: int = 8) -> str:
    sample = [str(v).strip() for v in values if str(v).strip()]
    if not sample:
        return "-"
    shown = sample[:limit]
    suffix = "" if len(sample) <= limit else f"...(+{len(sample) - limit})"
    return ", ".join(shown) + suffix


def _filter_pending_orders_dataframe(
    df: pd.DataFrame,
    completed_ids: set[str] | None = None,
    log_cb=None,
) -> pd.DataFrame:
    def _log(msg):
        if log_cb:
            log_cb(msg)
        else:
            logging.info(msg)

    if df.empty:
        return df

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

    base_mask = (
        (df[status_col] == "未打單")
        & (df[amount_col] != "")
        & (df[check_col].str.upper() != "TRUE")
        & (df[shipname_col] != "")
    )
    watched_mask = df[order_id_col].str.contains("WhoWhy", case=False, na=False)
    watched_rows = df[watched_mask]
    if not watched_rows.empty:
        _log(f"🧪 關注訂單診斷（WhoWhy*）：{len(watched_rows)} 筆")
        for idx, row in watched_rows.iterrows():
            result = "PASS" if bool(base_mask.loc[idx]) else "FAIL"
            _log(
                "   - 關注訂單 "
                f"{row.get(order_id_col, '')}: 基礎={result}; "
                f"狀態={row.get(status_col, '')!r}; "
                f"申告金額={row.get(amount_col, '')!r}; "
                f"製單檢核={row.get(check_col, '')!r}; "
                f"Shipping Name={row.get(shipname_col, '')!r}; "
                f"運送方式={row.get(shipping_col, '')!r}"
            )
    excluded = df[~base_mask]
    if not excluded.empty:
        _log(
            "🔎 基礎篩選排除 "
            f"{len(excluded)} 筆：{_format_sample(excluded[order_id_col].tolist())}"
        )
        reason_masks = [
            ("狀態不是未打單排除", df[status_col] != "未打單"),
            ("申告金額空白排除", df[amount_col] == ""),
            ("製單檢核 TRUE 排除", df[check_col].str.upper() == "TRUE"),
            ("Shipping Name 空白排除", df[shipname_col] == ""),
        ]
        for label, reason_mask in reason_masks:
            rows = df[reason_mask]
            if not rows.empty:
                _log(f"   - {label}：{_format_sample(rows[order_id_col].tolist())}")
    df_filtered = df[base_mask].copy()
    _log(f"📋 篩選後（未打單+必填）：{len(df_filtered)} 筆")

    completed_ids = completed_ids or set()
    if completed_ids:
        completed_mask = df_filtered[order_id_col].isin(completed_ids)
        completed_rows = df_filtered[completed_mask]
        if not completed_rows.empty:
            _log(
                "🔥 已在目標表完成而排除 "
                f"{len(completed_rows)} 筆：{_format_sample(completed_rows[order_id_col].tolist())}"
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


def _last_non_empty_row_sample(df: pd.DataFrame, order_id_col: str, limit: int = 5) -> str:
    if df.empty or order_id_col not in df.columns:
        return "-"
    ids = [str(v).strip() for v in df[order_id_col].tolist() if str(v).strip()]
    if not ids:
        return "-"
    return ", ".join(ids[-limit:])


def get_pending_orders(log_cb=None) -> pd.DataFrame:
    """
    從來源表單取得待打單清單，並執行雙重過濾防重製：
    1. 篩選狀態為「未打單」且必要欄位不為空
    2. 即時讀取目標表單 C 欄已完成單號集合，在記憶體中剔除重複
    3. 對來源本身的注文番号去重複

    回傳: pandas DataFrame，若無資料則為空 DataFrame
    """
    def _log(msg):
        if log_cb:
            log_cb(msg)
        else:
            logging.info(msg)

    try:
        started_at = time.perf_counter()
        client = _get_gspread_client()

        # ── 讀取來源表單 ────────────────────────────────
        source_started_at = time.perf_counter()
        sh_source = client.open_by_key(SOURCE_SHEET_ID)
        ws_source = _get_worksheet_by_gid(sh_source, SOURCE_GID)
        if not ws_source:
            _log(f"❌ 找不到來源表單 GID {SOURCE_GID}")
            return pd.DataFrame()

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
        _log(f"📊 來源原始筆數：{len(df)}")
        _log(
            "🧾 API 讀到的來源末端注文番号："
            f"{_last_non_empty_row_sample(df, '注文番号(貼上原始資料)')}"
        )

        # ── 🔥 雙重過濾：即時讀取目標表單已完成單號 ──────
        completed_ids: set[str] = set()
        try:
            target_started_at = time.perf_counter()
            sh_target = client.open_by_key(TARGET_SHEET_ID)
            ws_target = _get_worksheet_by_gid(sh_target, TARGET_GID)
            if ws_target:
                completed_col_c = ws_target.col_values(3)  # C 欄 = 注文番号
                completed_ids = {
                    str(v).strip()
                    for v in completed_col_c[1:]  # 跳過標題
                    if str(v).strip()
                }
                _log(
                    "⏱️ 目標表 C 欄讀取完成："
                    f"{len(completed_ids)} 個完成單號，耗時 {time.perf_counter() - target_started_at:.1f}s"
                )
        except Exception as e:
            _log(f"⚠️ 無法讀取目標表單（跳過雙重過濾）: {e}")

        df_filtered = _filter_pending_orders_dataframe(
            df,
            completed_ids=completed_ids,
            log_cb=log_cb,
        )
        _log(
            f"✅ 最終可打單：{len(df_filtered)} 筆，總讀取耗時 {time.perf_counter() - started_at:.1f}s"
        )

        return df_filtered

    except Exception as e:
        logging.error(f"❌ 取得待打單清單失敗: {e}")
        if log_cb:
            log_cb(f"❌ 取得待打單清單失敗: {e}")
        return pd.DataFrame()


def backfill_results(results: list[dict], log_cb=None):
    """
    將成功打單結果批量回填至目標表單 GID 465870894。
    每筆結果格式：{"name", "order_id", "tracking", "country_raw", "date"}
    """
    def _log(msg):
        if log_cb:
            log_cb(msg)
        else:
            logging.info(msg)

    if not results:
        _log("ℹ️ 無需回填（results 為空）")
        return

    try:
        client = _get_gspread_client()
        sh = client.open_by_key(TARGET_SHEET_ID)
        ws = next(
            (w for w in sh.worksheets() if str(w.id) == TARGET_GID), None
        )
        if not ws:
            _log(f"❌ 找不到目標表單 GID {TARGET_GID}")
            return

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
            country_code = COUNTRY_CODE_MAP.get(r.get("country_raw", ""), r.get("country_raw", ""))
            batch.append({
                "range": f"B{row_n}:D{row_n}",
                "values": [[r.get("name", ""), r.get("order_id", ""), r.get("tracking", "")]],
            })
            batch.append({
                "range": f"J{row_n}:J{row_n}",
                "values": [[country_code]],
            })

        ws.batch_update(batch, value_input_option="USER_ENTERED")
        _log(f"🚀 回填完成：{len(results)} 筆")

    except Exception as e:
        _log(f"❌ 回填失敗: {e}")
