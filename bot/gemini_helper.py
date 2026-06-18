"""
Gemini API 輔助模組
用途：根據商品名稱與目的國需求預測 HS/CN/TARIC Code（供 EU 訂單使用）
"""
import os
import logging
import streamlit as st

from .hs_codes import normalize_hs_code

# 執行期 HS Code 快取（同一品名不重複呼叫 API）
_HS_CODE_CACHE: dict[tuple[str, int, str, str], str] = {}


def _get_gemini_key() -> str:
    try:
        return st.secrets["GEMINI_API_KEY"]
    except Exception:
        return os.environ.get("GEMINI_API_KEY", "")


def predict_hs_code(
    item_name: str,
    log_cb=None,
    *,
    required_length: int = 6,
    country: str = "",
    country_code: str = "",
) -> str:
    """
    使用 Gemini API 預測指定碼數的 HS/CN/TARIC Code。
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

    required_length = int(required_length or 6)
    cache_key = (
        item_clean.casefold(),
        required_length,
        str(country or "").strip().casefold(),
        str(country_code or "").strip().upper(),
    )
    if cache_key in _HS_CODE_CACHE:
        _log(f"📦 HS Code 快取命中: {item_clean} → {_HS_CODE_CACHE[cache_key]}")
        return _HS_CODE_CACHE[cache_key]

    api_key = _get_gemini_key()
    if not api_key:
        _log("⚠️ 未設定 GEMINI_API_KEY，跳過 HS Code 預測")
        return ""

    try:
        from google import genai as google_genai
        client = google_genai.Client(api_key=api_key)
        prompt = (
            f"Predict a {required_length}-digit HS/CN/TARIC code for the shipping item: "
            f"'{item_clean}'. Destination country: {country or country_code or 'unknown'}. "
            f"Return ONLY the first {required_length} digits, nothing else."
        )
        _log(f"🤖 Gemini 預測 HS Code: {item_clean}（需要 {required_length} 碼）")
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        text = response.text or ""

        code = normalize_hs_code(text, required_length)

        if code:
            _log(f"✅ HS Code 預測成功: {item_clean} → {code}")
            _HS_CODE_CACHE[cache_key] = code
            return code
        else:
            _log(f"⚠️ 無法從 Gemini 回應解析 HS Code: {text!r}")
    except Exception as e:
        _log(f"❌ Gemini API 錯誤: {e}")

    return ""
