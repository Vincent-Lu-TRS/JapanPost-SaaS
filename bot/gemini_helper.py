"""
Gemini API 輔助模組
用途：根據商品名稱預測 6 碼 HS Code（供 EU 訂單使用）
"""
import os
import re
import logging
import streamlit as st

_HS_CODE_CACHE = {}


def _get_gemini_key() -> str:
    try:
        return st.secrets["GEMINI_API_KEY"]
    except Exception:
        return os.environ.get("GEMINI_API_KEY", "")


def predict_hs_code(item_name: str, log_cb=None) -> str:
    def _log(msg):
        if log_cb:
            log_cb(msg)
        else:
            logging.info(msg)

    item_clean = str(item_name).strip()
    if not item_clean:
        return ""

    if item_clean in _HS_CODE_CACHE:
        return _HS_CODE_CACHE[item_clean]

    api_key = _get_gemini_key()
    if not api_key:
        _log("⚠️ GEMINI_API_KEY not set, skipping HS Code")
        return ""

    try:
        from google import genai as google_genai
        client = google_genai.Client(api_key=api_key)
        prompt = (
            f"Predict a 6-digit HS Code for the shipping item: '{item_clean}'. "
            "Return ONLY the 6-digit number, nothing else."
        )
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        text = response.text or ""
        match = re.search(r'\b\d{6}\b', text)
        if match:
            code = match.group(0)
        else:
            digits = re.sub(r'\D', '', text)
            code = digits[:6] if len(digits) >= 6 else ""
        if code:
            _HS_CODE_CACHE[item_clean] = code
        return code
    except Exception as e:
        _log(f"❌ Gemini API error: {e}")
    return ""
