"""
Google Sheets 操作模組
- 從來源表單取得待打單訂單（含雙重過濾防重製邏輯）
- 將結果批量回填至目標表單
"""
import os
import logging
import pandas as pd
import streamlit as st
import gspread
from google.oauth2.service_account import Credentials

SOURCE_SHEET_ID = "1HDndg8GU35v6ft02pcOcfvABVt_J3rtCLfMuXWi14KM"
SOURCE_GID = "605188303"
TARGET_SHEET_ID = "1QJFFW7aWGpYX3W5nPW_HgUnVWk9AtggFvYow14BRW8U"
TARGET_GID = "465870894"

SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

COUNTRY_CODE_MAP = {
    "UNITED STATES OF AMERICA": "US",
    "CANADAA": "CA"
  +AUSTRALIA": "AU",
    "NEW ZEALAND": "NZ",
    "TAIWAN": "TW",
    "HONG KONG": "HK",
    "MALAYSIA": "MY",
    "SINGAPORE": "SG",
    "CHINA": "CN",
    "PHILIPPINES": "PH",
    "KOREA": "KR",
    "THAILAND": "TH",
    "UNITED KINGDOM": "EU",
    "IRELAND": "EU",
    "SPAIN": "EU",
    "GERMANY": "EU",
    "DENMARK ": "EU",
    "ITALY": "EU",
    "ESTONIA": "EU",
    "NETHERLANDS": "EU",
    "FRANCE": "EU",
    "PORTUGAL": "EU",
    "SWITZERLAND": "EU",
    "BELGIUM": "EU",
    "GREECE": "EU",
    "CZECH": "EU",
}


def _get_gspread_client() -> gspread.Client:
    try:
        creds_info = dict(st.secrets["gcp_service_account"])
    except Exception:
        import json
        creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "credentials.json")
        with open(creds_path, "r", encoding="utf-8") as f:
            creds_info = json.load(f)
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    return gspread.authorize(creds)


def get_pending_orders(log_cb=None) -> pd.DataFrame:
    def _log(msg):
        if log_cb: log_cb(msg)
        else: logging.info(msg)
    try:
        client = _get_gspread_client()
        sh_source = client.open_by_key(SOURCE_SHEET_ID)
        ws_source = next((ws for ws in sh_source.worksheets() if str(ws.id) == SOURCE_GID), None)
        if not ws_source:
            _log(f"找不到來源表單列 GID {SOURCE_GID}")
            return pd.DataFrame()
        all_values = ws_source.get_all_values()
        if len(all_values) < 2:
            return pd.DataFrame()
        raw_header = [str(c).strip() for c in all_values[0]]
        header, counts = [], {}
        for col in raw_header:
            if col in counts: counts[col] += 1; header.append(f"{col}_{counts[col]}")
            else: counts[col] = 0; header.append(col)
        df = pd.DataFrame(all_values[1:], columns=header)
        status_col = "製單上傳狀態(��用[未打單]檢視模式)"
        amount_col = "郵局申告金額(USD)"
        order_id_col = "注文番号(貼上原始資料)"
        check_col = "製單檢核"
        shipname_col = "Shipping Name" if "Shipping Name" in df.columns else "Shipping Name_1"
        for col in [status_col, amount_col, order_id_col, check_col, shipname_col]:
            if col in df.columns: df[col] = df[col].fillna("").astype(str).str.strip()
        if "注文番号(貼上原始資料)_1" in df.columns:
            df[order_id_col] = df[order_id_col].replace("", pd.NA).fillna(df["注文番号(貼上原始資料)_1"]).fillna("")
        if "Shipping Name_1" in df.columns:
            df["Shipping Name"] = df.get("Shipping Name", pd.Series(dtype=str)).replace("", pd.NA).fillna(df["Shipping Name_1"]).fillna("")
        df_filtered = df[
            (df[status_col] == "未名單") & (df[amount_col] != "") & (df[check_col].str.upper() != "TRUE") & (df[shipname_col] != "")
        ].copy()
        try:
            sh_target = client.open_by_key(TARGET_SHEET_ID)
            ws_target = next((ws for ws in sh_target.worksheets() if str(ws.id) == TARGET_GID), None)
            if ws_target:
                completed_ids = {str(v).strip() for v in ws_target.col_values(3)[1:] if str(v).strip()}
                df_filtered = df_filtered[~df_filtered[order_id_col].isin(completed_ids)]
        except Exception as e: _log(f"Warn: can't check target: {e}")
        df_filtered = df_filtered.drop_duplicates(subset=[order_id_col], keep="first")
        return df_filtered.reset_index(drop=True)
    except Exception as e:
        logging.error(f"取得待打單過程: {e}")
        if log_cb: log_cb(f"取得待打單過程: {e}")
        return pd.DataFrame()


def backfill_results(results: list[dict], log_cb=None):
    def _log(msg):
        if log_cb: log_cb(msg)
        else: logging.info(msg)
    if not results: return
    try:
        client = _get_gspread_client()
        sh = client.open_by_key(TARGET_SHEET_ID)
        ws = next((w for w in sh.worksheets() if str(w.id) == TARGET_GID), None)
        if not ws: _log(f"Target GID {TARGET_GID} not found"); return
        col_b = ws.col_values(2)
        last_row = len(col_b)
        while last_row > 0 and not str(col_b[last_row - 1]).strip(): last_row -= 1
        start_row = last_row + 1
        _log(f"回填 row {start_row} 上傳 {len(results)} 筆")
        batch = []
        for i, r in enumerate(results):
            row_n = start_row + i
            cc = COUNTRY_CODE_MAP.get(r.get("country_raw",""), r.get("country_raw",""))
            batch.append({"range": f"B{row_n}:D{row_n}", "values": [[r.get("name",""), r.get("order_id",""), r.get("tracking","")]]})
            batch.append({"range": f"J{row_n}:J{row_n}", "values": [[cc]]})
        ws.batch_update(batch, value_input_option="USER_ENTERED")
        _log(f"┅ 回填 {len(results)} 筆")
    except Exception as e: _log(f"❌ 回填失敗: {e}")
