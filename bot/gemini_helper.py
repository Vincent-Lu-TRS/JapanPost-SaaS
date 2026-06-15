"""
Gemini API 輔助模組
用途：根據商品名稱預測 6 碼 HS Code（供 EU 訂單使用）
"""
import os
import re
import logging
import streamlit as st

# 執行期 HS Code 快取（同一品名不重複呼叫 API）
_HS_CODE_CACHE: dict[str, str] = {}


def _get_gemini_key() -> str:
    try:
        return st.secrets["GEMINI_API_KEY"]
    except Exception:
        return os.environ.get("GEMINI_API_KEY", "")


def predict_hs_code(item_name: str, log_cb=None) -> str:
    """
    使用 Gemini API 預測 6 碼 HS Code。
    - log_cb: 可選的進度回呼函數 (str -> None)
    """
    def _log(msg):
        if log_cb:
            log_cb(msg)
        else:
            logging.info(msg)

    item_clean = str(item_name).strip()
    if not item_clean:
        return ""

    if item_clean in _HS_CODE_CACHE:
        _log(f"📦 HS Code 快取命中: {item_clean} → {_HS_CODE_CACHE[item_clean]}")
        return _HS_CODE_CACHE[item_clean]

    api_key = _get_gemini_key()
    if not api_key:
        _log("⚠️ 未設定 GEMINI_API_KEY，跳過 HS Code 預測")
        return ""

    try:
        from google import genai as google_genai
        client = google_genai.Client(api_key=api_key)
        prompt = (
            f"Predict a 6-digit HS Code for the shipping item: '{item_clean}'. "
            "Return ONLY the 6-digit number, nothing else."
        )
        _log(f"🤖 Gemini 預測 HS Code: {item_clean}")
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        text = response.text or ""

        # 優先抓取標準 6 碼
        match = re.search(r'\b\d{6}\b', text)
        if match:
            code = match.group(0)
        else:
            # 備案：移除非數字後取前 6 碼
            digits = re.sub(r'\D', '', text)
            code = digits[:6] if len(digits) >= 6 else ""

        if code:
            _log(f"✅ HS Code 預測成功: {item_clean} → {code}")
            _HS_CODE_CACHE[item_clean] = code
            return code
        else:
            _log(f"⚠️ 無法從 Gemini 回應解析 HS Code: {text!r}")
    except Exception as e:
        _log(f"❌ Gemini API 錯誤: {e}")

    return ""
