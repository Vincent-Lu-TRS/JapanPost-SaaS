"""Deterministic Thai-address conversion for the Japan Post boundary.

The source export can contain Thai-only address fields.  Japan Post's label
form must receive Latin text, so common address terms and known Bangkok place
names are converted first; any remaining Thai text is romanized without
discarding digits or punctuation.  Unsupported Thai code points fail closed
instead of producing a partially blank label.
"""
from __future__ import annotations

import re


_THAI_RE = re.compile(r"[\u0e00-\u0e7f]")
_PUNCTUATION_MAP = str.maketrans({"\u201a": ",", "\uff0c": ","})

# Keep longer phrases before their component words.  These are address
# semantics, while place names are rendered using the common English spelling
# used by Bangkok's English-language public information pages.
_THAI_ADDRESS_TERMS = {
    "กรุงเทพมหานคร": "Bangkok",
    "ประเทศไทย": "Thailand",
    "ลาดพร้าว": "Lat Phrao",
    "จตุจักร": "Chatuchak",
    "จอมพล": "Chom Phon",
    "บางกะปิ": "Bang Kapi",
    "ห้วยขวาง": "Huai Khwang",
    "ปทุมวัน": "Pathum Wan",
    "ราชเทวี": "Ratchathewi",
    "ดินแดง": "Din Daeng",
    "บางนา": "Bang Na",
    "หมู่บ้าน": "Village",
    "แขวง": "Subdistrict",
    "ตำบล": "Subdistrict",
    "เขต": "District",
    "อำเภอ": "District",
    "จังหวัด": "Province",
    "อาคาร": "Building",
    "ชั้น": "Floor",
    "ห้อง": "Room",
    "เลขที่": "No.",
    "ซอย": "Soi",
    "ถนน": "Road",
    "แยก": "Yaek",
    "หมู่": "Moo",
    "บ้าน": "Ban",
    "กรุงเทพฯ": "Bangkok",
    "กรุงเทพ": "Bangkok",
}

# Fallback is intentionally conservative: it is a readable romanization for
# an unlisted proper name, not an invented address.  Address terms above take
# precedence, so the known order path produces the expected English wording.
_THAI_CHARACTER_ROMANIZATION = {
    "ก": "k", "ข": "kh", "ฃ": "kh", "ค": "kh", "ฅ": "kh", "ฆ": "kh",
    "ง": "ng", "จ": "ch", "ฉ": "ch", "ช": "ch", "ซ": "s", "ฌ": "ch",
    "ญ": "y", "ฎ": "d", "ฏ": "t", "ฐ": "th", "ฑ": "th", "ฒ": "th",
    "ณ": "n", "ด": "d", "ต": "t", "ถ": "th", "ท": "th", "ธ": "th",
    "น": "n", "บ": "b", "ป": "p", "ผ": "ph", "ฝ": "f", "พ": "ph",
    "ฟ": "f", "ภ": "ph", "ม": "m", "ย": "y", "ร": "r", "ล": "l",
    "ว": "w", "ศ": "s", "ษ": "s", "ส": "s", "ห": "h", "ฬ": "l",
    "อ": "o", "ฮ": "h",
    "ะ": "a", "ั": "a", "า": "a", "ำ": "am", "ิ": "i", "ี": "i",
    "ึ": "ue", "ื": "ue", "ุ": "u", "ู": "u", "เ": "e", "แ": "ae",
    "โ": "o", "ใ": "ai", "ไ": "ai", "ฤ": "rue", "ฦ": "lue", "ๅ": "a",
    "็": "", "่": "", "้": "", "๊": "", "๋": "", "์": "", "ํ": "",
    "ฺ": "", "ฯ": ".", "ๆ": "", "฿": "THB",
    "๐": "0", "๑": "1", "๒": "2", "๓": "3", "๔": "4",
    "๕": "5", "๖": "6", "๗": "7", "๘": "8", "๙": "9",
}


class ThaiAddressTranslationError(ValueError):
    """Raised when a Thai address cannot be converted safely."""

    reason_code = "address_invalid_character"


def _romanize_remaining_thai(value: str) -> str:
    unknown = {
        char
        for char in value
        if _THAI_RE.fullmatch(char) and char not in _THAI_CHARACTER_ROMANIZATION
    }
    if unknown:
        raise ThaiAddressTranslationError(
            "泰文地址含有未支援字元，已停止送出以避免產生空白地址"
        )
    return "".join(
        _THAI_CHARACTER_ROMANIZATION.get(char, char)
        for char in value
    )


def translate_thai_address(value: str) -> str:
    """Return a Latin-address value while preserving non-Thai input."""
    raw = str(value or "").translate(_PUNCTUATION_MAP)
    raw = " ".join(raw.split())
    if not raw or not _THAI_RE.search(raw):
        return raw

    translated = raw
    for thai_term, english_term in sorted(
        _THAI_ADDRESS_TERMS.items(), key=lambda item: len(item[0]), reverse=True
    ):
        translated = translated.replace(thai_term, english_term)
    translated = _romanize_remaining_thai(translated)
    translated = " ".join(translated.split())
    if _THAI_RE.search(translated):
        raise ThaiAddressTranslationError(
            "泰文地址轉換未完成，已停止送出以避免產生空白地址"
        )
    return translated
