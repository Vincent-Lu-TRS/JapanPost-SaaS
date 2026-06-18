"""
HS/CN/TARIC code helpers.

Reference:
- Japan Post Europe notes:
  https://www.post.japanpost.jp/service/send/oversea/attention/region/europe.html
- Japan Post HS code examples:
  https://www.post.japanpost.jp/service/send/oversea/use/label/hscode/index.php?lang=_ja

Japan Post currently indicates:
- Ireland requires TARIC code, first 10 digits.
- France and listed French territories require CN code, first 8 digits.
- Other listed Europe destinations require HS code, first 6 digits.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Callable


TARIC_10_COUNTRIES = {
    "IRELAND",
    "アイルランド",
}

CN_8_COUNTRIES = {
    "FRANCE",
    "FRENCH GUIANA",
    "GUADELOUPE",
    "MARTINIQUE",
    "REUNION",
    "RÉUNION",
    "レユニオン",
    "フランス",
    "仏領ギアナ",
    "ガドループ",
    "グアドループ",
    "マルチニーク",
}

LOCAL_HS_CODE_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("facial mask", "no alcohol"), "3304990000"),
    (("face mask", "no alcohol"), "3304990000"),
    (("pillow",), "9404900000"),
    (("hair conditioner",), "3305900000"),
    (("toothbrush",), "9603210000"),
    (("spice grinder",), "8210000000"),
    (("spice grinders",), "8210000000"),
    (("frying pan",), "7323990000"),
    (("cooking stove",), "7321110000"),
    (("portable cooking stove",), "7321110000"),
)


def _normalize_country(country_raw: str) -> str:
    text = unicodedata.normalize("NFKC", str(country_raw or "")).strip()
    text = re.sub(r"\s+", " ", text)
    return text.upper()


def required_hs_code_length(country_raw: str, country_code: str = "") -> int:
    normalized = _normalize_country(country_raw)
    base_name = re.split(r"[\(（]", normalized, maxsplit=1)[0].strip()
    country_names = {normalized, base_name}
    if country_names & TARIC_10_COUNTRIES:
        return 10
    if country_names & CN_8_COUNTRIES:
        return 8
    if str(country_code or "").upper() == "EU":
        return 6
    return 0


def normalize_hs_code(raw_code: str, required_length: int) -> str:
    if required_length <= 0:
        return ""
    digits = re.sub(r"\D", "", str(raw_code or ""))
    if len(digits) < required_length:
        return ""
    return digits[:required_length]


def _normalize_item_name(item_name: str) -> str:
    text = unicodedata.normalize("NFKC", str(item_name or "")).casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def local_hs_code_lookup(item_name: str, required_length: int) -> str:
    normalized_name = _normalize_item_name(item_name)
    for needles, code in LOCAL_HS_CODE_RULES:
        if all(needle in normalized_name for needle in needles):
            return normalize_hs_code(code, required_length)
    return ""


Predictor = Callable[..., str]


def prepare_hs_codes_for_items(
    items: list[dict[str, str]],
    *,
    country_raw: str,
    country_code: str,
    predictor: Predictor,
    log_cb=None,
) -> dict[str, str]:
    required_length = required_hs_code_length(country_raw, country_code)
    if required_length <= 0:
        return {}

    resolved_by_pkg: dict[tuple[str, int, str], str] = {}
    codes_by_index: dict[str, str] = {}
    for item in items:
        pkg = str(item.get("pkg") or "").strip()
        item_index = str(item.get("index") or "").strip()
        if not pkg or not item_index:
            continue
        cache_key = (pkg.casefold(), required_length, _normalize_country(country_raw))
        if cache_key not in resolved_by_pkg:
            code = local_hs_code_lookup(pkg, required_length)
            if code and log_cb:
                log_cb(f"📚 HS Code 本地參照命中: {pkg} → {code} ({required_length}碼)")
            if not code:
                code = predictor(
                    pkg,
                    required_length=required_length,
                    country=country_raw,
                    country_code=country_code,
                    log_cb=log_cb,
                )
            normalized = normalize_hs_code(code, required_length)
            if log_cb:
                if normalized:
                    log_cb(f"✅ HS Code 預查完成: {pkg} → {normalized} ({required_length}碼)")
                else:
                    log_cb(f"⚠️ HS Code 預查失敗: {pkg}（需要 {required_length} 碼）")
            resolved_by_pkg[cache_key] = normalized
        if resolved_by_pkg[cache_key]:
            codes_by_index[item_index] = resolved_by_pkg[cache_key]
    return codes_by_index
