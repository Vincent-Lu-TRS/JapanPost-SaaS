"""
日本郵政自動化打單核心模組（Server-side Headless Playwright）
完整繼承 pa_playwright.py 的防禦機制：
- 雙重 jQuery UI 彈窗防禦
- 歷史資料對話框強制重置
- ePacket / PostalParcel_Air 精確分流
- EU 訂單 Gemini HS Code 預測注入
- PDF 封包攔截（不依賴下載對話框）
- 標準化命名 + Google Drive 即時上傳
"""
import os
import re
import time
import logging
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin
from datetime import date
import pandas as pd

AUTOMATION_BUILD_ID = "2026-06-18-multi-item-priority-country"

from .drive import upload_pdf
from .gemini_helper import predict_hs_code

# ── 日本郵政登入憑證 ────────────────────────────────────
def _get_jp_post_creds() -> tuple[str, str]:
    import streamlit as st
    try:
        return st.secrets["JP_POST_USER"], st.secrets["JP_POST_PASS"]
    except Exception:
        return (
            os.environ.get("JP_POST_USER", ""),
            os.environ.get("JP_POST_PASS", ""),
        )


# ── 工具函數 ──────────────────────────────────────────
def _clean(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return str(v).strip()


def _sanitize_filename(s: str) -> str:
    return re.sub(r'[\\/*?:"<>|\r\n]', "_", s).strip()


def _get_excel_val(row: pd.Series, keys: list[str]) -> str:
    for k in keys:
        if k in row.index:
            v = _clean(row[k])
            if v:
                return v
    return ""


def _row_val(row, keys: list[str]) -> str:
    if hasattr(row, "index"):
        return _get_excel_val(row, keys)
    if hasattr(row, "get"):
        for key in keys:
            value = _clean(row.get(key, ""))
            if value:
                return value
    return ""


def _build_result_record(row, order_id: str, tracking: str) -> dict:
    country_raw = _row_val(row, ["收件人國家", "Country"])
    return {
        "name": _row_val(row, ["Shipping Name", "Shipping Name_1"]),
        "order_id": order_id,
        "tracking": tracking,
        "country": country_raw,
        "country_raw": country_raw,
        "date": time.strftime("%Y-%m-%d"),
    }


def _with_base_href(html: str, base_url: str) -> str:
    """Ensure response HTML resolves relative Japan Post URLs inside Playwright."""
    if re.search(r"<base\s", html, flags=re.IGNORECASE):
        return html
    base_tag = f'<base href="{base_url}">'
    if re.search(r"<head[^>]*>", html, flags=re.IGNORECASE):
        return re.sub(r"(<head[^>]*>)", r"\1" + base_tag, html, count=1, flags=re.IGNORECASE)
    return base_tag + html


# ── 主要自動化流程 ────────────────────────────────────
def _html_for_playwright_form(html: str) -> str:
    """Strip legacy site scripts but keep enough Struts helpers for button onclicks."""
    sanitized = re.sub(
        r"<script\b[^>]*>.*?</script\s*>",
        "",
        html or "",
        flags=re.IGNORECASE | re.DOTALL,
    )
    if "M060505_addrToBean_nam" in sanitized:
        forms = re.findall(
            r"<form\b[^>]*>.*?</form\s*>",
            sanitized,
            flags=re.IGNORECASE | re.DOTALL,
        )
        for form in forms:
            if "M060505_addrToBean_nam" in form:
                sanitized = f"<html><head><title>Japan Post Form</title></head><body>{form}</body></html>"
                break
    submit_stub = """
<script>
function setValue(name, value) {
  var el = document.getElementsByName(name)[0] || document.getElementById(name);
  if (el) el.value = value;
}
function submitCommand(command) {
  var form = document.forms[0];
  if (!form) return;
  var input = document.createElement("input");
  input.type = "hidden";
  input.name = "method:" + command;
  input.value = "";
  form.appendChild(input);
  form.submit();
}
function regist() { submitCommand("regist"); }
</script>
"""
    if re.search(r"</head\s*>", sanitized, flags=re.IGNORECASE):
        return re.sub(r"</head\s*>", submit_stub + r"</head>", sanitized, count=1, flags=re.IGNORECASE)
    return submit_stub + sanitized


def _set_value_assignments(script: str) -> dict[str, str]:
    assignments: dict[str, str] = {}
    for name, value in re.findall(
        r"setValue\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]*)['\"]\s*\)",
        script or "",
    ):
        assignments[name] = value
    return assignments


def _known_field_assignments(script: str) -> dict[str, str]:
    assignments = _set_value_assignments(script)
    known_fields = {
        "shippingBean.sendType",
        "shippingBean.transType",
        "shippingBean.pkgType",
    }
    for name, value in re.findall(
        r"['\"](shippingBean\.(?:sendType|transType|pkgType))['\"]\s*,\s*['\"]([^'\"]*)['\"]",
        script or "",
    ):
        if name in known_fields:
            assignments[name] = value
    send_type_match = re.search(r"chgSendTypeBtn\(\s*([0-9]+)\s*\)", script or "")
    if send_type_match:
        assignments["shippingBean.sendType"] = send_type_match.group(1)
    trans_type_match = re.search(r"chgTransTypeBtn\(\s*([0-9]+)\s*\)", script or "")
    if trans_type_match:
        assignments["shippingBean.transType"] = trans_type_match.group(1)
    return assignments


def _set_value_assignments_for_labels(html: str, labels: list[str]) -> dict[str, str]:
    assignments: dict[str, str] = {}
    label_norms = [" ".join(label.lower().split()) for label in labels if label]
    if not label_norms:
        return assignments
    for button_match in re.finditer(
        r"<button\b[^>]*>.*?</button\s*>",
        html or "",
        flags=re.IGNORECASE | re.DOTALL,
    ):
        button_text = unescape(button_match.group(0))
        button_norm = " ".join(button_text.split()).lower()
        attr_values = [
            " ".join(value.lower().split())
            for _, value in re.findall(r"""(\w+)\s*=\s*['"]([^'"]*)['"]""", button_text)
        ]
        if (
            any(label == attr_value for label in label_norms for attr_value in attr_values)
            or any(label in button_norm for label in label_norms)
        ):
            assignments.update(_known_field_assignments(button_text))
            if assignments:
                return assignments
    for tag_match in re.finditer(r"<[^>]+>", html or "", flags=re.IGNORECASE | re.DOTALL):
        tag_text = unescape(tag_match.group(0))
        tag_norm = " ".join(tag_text.split()).lower()
        attr_values = [
            " ".join(value.lower().split())
            for _, value in re.findall(r"""(\w+)\s*=\s*['"]([^'"]*)['"]""", tag_text)
        ]
        exact_attr_match = any(label == attr_value for label in label_norms for attr_value in attr_values)
        if exact_attr_match:
            assignments.update(_known_field_assignments(tag_text))
            if not assignments:
                start = max(0, tag_match.start() - 800)
                end = min(len(html or ""), tag_match.end() + 800)
                assignments.update(_known_field_assignments((html or "")[start:end]))
            if assignments:
                return assignments
        if any(len(label) > 3 and label in tag_norm for label in label_norms):
            assignments.update(_known_field_assignments(tag_text))
            if assignments:
                return assignments

    for label in label_norms:
        if len(label) <= 3:
            continue
        for label_match in re.finditer(re.escape(label), (html or "").lower()):
            start = max(0, label_match.start() - 800)
            end = min(len(html or ""), label_match.end() + 800)
            assignments.update(_known_field_assignments((html or "")[start:end]))
            if assignments:
                return assignments
    return assignments


def _html_context_for_labels(html: str, labels: list[str], max_chars: int = 700) -> str:
    contexts: list[str] = []
    text = html or ""
    lower_text = text.lower()
    for label in labels:
        if not label:
            continue
        label_norm = label.lower()
        idx = lower_text.find(label_norm)
        if idx < 0:
            continue
        start = max(0, idx - max_chars // 2)
        end = min(len(text), idx + len(label) + max_chars // 2)
        snippet = unescape(text[start:end])
        snippet = re.sub(r"csrfToken['\"]?\s+value=['\"][^'\"]+", "csrfToken value='[redacted]", snippet)
        snippet = re.sub(r"\s+", " ", snippet).strip()
        contexts.append(f"{label}=>{snippet[:max_chars]}")
    return " || ".join(contexts) or "no label context"


def _shipping_profile(row) -> str:
    shipping = _row_val(row, ["郵局運送方式(複數商品請自行確認是否走小包)"])
    normalized = shipping.lower()
    if "epacket" in normalized or "eパケット" in normalized:
        return "epacket_light"
    if (
        "國際小包" in shipping
        or "国際小包" in shipping
        or "postal parcel" in normalized
        or "international parcel" in normalized
    ):
        return "postal_parcel_air"
    return ""


class _StrutsFormParser(HTMLParser):
    def __init__(self, label: str):
        super().__init__(convert_charrefs=True)
        self.label = label.lower()
        self.in_first_form = False
        self.seen_form = False
        self.form_action = ""
        self.fields: dict[str, str] = {}
        self.href_stack: list[str] = []
        self.command = ""

    def handle_starttag(self, tag, attrs):
        attrs_d = {k.lower(): (v or "") for k, v in attrs}
        tag = tag.lower()
        if tag == "form" and not self.seen_form:
            self.seen_form = True
            self.in_first_form = True
            self.form_action = attrs_d.get("action", "")
        elif tag == "a":
            self.href_stack.append(attrs_d.get("href", ""))
        elif tag == "img" and self.href_stack:
            alt = attrs_d.get("alt", "").lower()
            if self.label and self.label in alt:
                self.command = self.command or _command_from_href(self.href_stack[-1])
        elif self.in_first_form and tag == "input":
            name = attrs_d.get("name", "")
            if name:
                input_type = attrs_d.get("type", "").lower()
                if input_type in ("radio", "checkbox"):
                    if "checked" in attrs_d:
                        self.fields[name] = attrs_d.get("value", "")
                        self.fields.update(_set_value_assignments(attrs_d.get("onclick", "")))
                    elif name not in self.fields:
                        self.fields[name] = ""
                else:
                    self.fields[name] = attrs_d.get("value", "")
            label_value = attrs_d.get("value", "").lower()
            if self.label and self.label in label_value:
                self.command = self.command or _command_from_href(attrs_d.get("onclick", ""))
        elif self.in_first_form and tag == "select":
            name = attrs_d.get("name", "")
            if name and name not in self.fields:
                self.fields[name] = attrs_d.get("value", "")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "form" and self.in_first_form:
            self.in_first_form = False
        elif tag == "a" and self.href_stack:
            self.href_stack.pop()

    def handle_data(self, data):
        if self.label and self.href_stack and self.label in data.lower():
            self.command = self.command or _command_from_href(self.href_stack[-1])


def _command_from_href(href: str) -> str:
    match = re.search(r"submitCommand\(['\"]([^'\"]+)['\"]\)", href or "")
    if match:
        return match.group(1)
    if re.search(r"\bregist\(\s*\)", href or ""):
        return "regist"
    return ""


def _extract_submit_command_for_label(html: str, label: str) -> str:
    parser = _StrutsFormParser(label)
    parser.feed(html or "")
    return parser.command


def _summarize_submit_commands(html: str) -> str:
    commands: list[str] = []
    for command in re.findall(r"submitCommand\(['\"]([^'\"]+)['\"]\)", html or ""):
        if command not in commands:
            commands.append(command)
    if re.search(r"\bregist\(\s*\)", html or "") and "regist" not in commands:
        commands.append("regist")
    return ", ".join(commands[:12])


def _extract_preferred_submit_command(html: str, preferred: list[str]) -> str:
    available = re.findall(r"submitCommand\(['\"]([^'\"]+)['\"]\)", html or "")
    if re.search(r"\bregist\(\s*\)", html or ""):
        available.append("regist")
    for command in preferred:
        if command in available:
            return command
    return ""


def _choose_label_flow_command(html: str, current_url: str) -> str:
    if "M060400" in (current_url or ""):
        preferred = ["directInput", "add", "regist"]
    elif "M060105" in (current_url or ""):
        preferred = ["addrSet", "regist", "directInput"]
    else:
        preferred = ["addrSet", "regist", "directInput"]
    return _extract_preferred_submit_command(html, preferred)


def _build_struts_submit(html: str, command: str, base_url: str) -> tuple[str, dict[str, str]]:
    parser = _StrutsFormParser("")
    parser.feed(html or "")
    action = urljoin(base_url, parser.form_action or "")
    payload = {
        name: value
        for name, value in parser.fields.items()
        if name != "command"
    }
    payload[f"method:{command}"] = ""
    return action, payload


class _HtmlFormParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms: list[dict] = []
        self._form: dict | None = None
        self._select_name: str | None = None
        self._option_value: str | None = None
        self._option_text: list[str] = []
        self._textarea_name: str | None = None
        self._textarea_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs_d = {k.lower(): (v or "") for k, v in attrs}
        tag = tag.lower()
        if tag == "form":
            self._form = {
                "action": attrs_d.get("action", ""),
                "method": attrs_d.get("method", "get").lower(),
                "fields": {},
                "selects": {},
            }
            return
        if self._form is None:
            return
        if tag == "input":
            name = attrs_d.get("name", "")
            if not name:
                return
            input_type = attrs_d.get("type", "text").lower()
            if input_type in {"button", "submit", "image", "reset"}:
                return
            if input_type == "radio":
                if "checked" in attrs_d or name not in self._form["fields"]:
                    self._form["fields"][name] = attrs_d.get("value", "")
                return
            self._form["fields"][name] = attrs_d.get("value", "")
        elif tag == "select":
            self._select_name = attrs_d.get("name", "")
            if self._select_name:
                self._form["selects"].setdefault(self._select_name, [])
                self._form["fields"].setdefault(self._select_name, "")
        elif tag == "option" and self._select_name:
            self._option_value = attrs_d.get("value", "")
            self._option_text = []
            if "selected" in attrs_d or not self._form["fields"].get(self._select_name):
                self._form["fields"][self._select_name] = self._option_value
        elif tag == "textarea":
            self._textarea_name = attrs_d.get("name", "")
            self._textarea_text = []

    def handle_data(self, data):
        if self._textarea_name:
            self._textarea_text.append(data)
        if self._select_name and self._option_value is not None:
            self._option_text.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "form" and self._form is not None:
            self.forms.append(self._form)
            self._form = None
        elif (
            tag == "option"
            and self._form is not None
            and self._select_name
            and self._option_value is not None
        ):
            self._form["selects"].setdefault(self._select_name, []).append({
                "value": self._option_value,
                "text": " ".join("".join(self._option_text).split()),
            })
            self._option_value = None
            self._option_text = []
        elif tag == "select":
            self._select_name = None
            self._option_value = None
            self._option_text = []
        elif tag == "textarea" and self._form is not None and self._textarea_name:
            self._form["fields"][self._textarea_name] = "".join(self._textarea_text)
            self._textarea_name = None
            self._textarea_text = []


def _parse_forms(html: str) -> list[dict]:
    parser = _HtmlFormParser()
    parser.feed(html or "")
    return parser.forms


def _pick_form(html: str, preferred_action: str = "", required_fields: list[str] | None = None) -> dict:
    forms = _parse_forms(html)
    required_fields = required_fields or []
    for form in forms:
        if preferred_action and preferred_action not in form.get("action", ""):
            continue
        if all(field in form["fields"] for field in required_fields):
            return form
    for form in forms:
        if all(field in form["fields"] for field in required_fields):
            return form
    if forms:
        return forms[0]
    raise RuntimeError("找不到可提交的日本郵政表單")


def _select_option_value(form: dict, field_name: str, label: str, fallback: str = "") -> str:
    label_norm = " ".join(_clean(label).split()).lower()
    for option in form.get("selects", {}).get(field_name, []):
        text_norm = " ".join(_clean(option.get("text", "")).split()).lower()
        value = _clean(option.get("value", ""))
        if label_norm and (label_norm == text_norm or label_norm in text_norm):
            return value
    return fallback


def _first_non_empty_option_value(form: dict, field_name: str, fallback: str = "") -> str:
    for option in form.get("selects", {}).get(field_name, []):
        value = _clean(option.get("value", ""))
        if value:
            return value
    return fallback


def _summarize_forms(html: str, max_forms: int = 4, max_fields: int = 8) -> str:
    parts: list[str] = []
    for idx, form in enumerate(_parse_forms(html)[:max_forms], start=1):
        fields = list(form.get("fields", {}).keys())
        selects = list(form.get("selects", {}).keys())
        field_summary = ",".join(fields[:max_fields])
        if len(fields) > max_fields:
            field_summary += ",..."
        select_summary = ",".join(selects[:max_fields])
        parts.append(
            f"#{idx} action={form.get('action', '')} "
            f"fields={field_summary or '-'} selects={select_summary or '-'}"
        )
    return " | ".join(parts) if parts else "(no forms)"


def _summarize_error_text(html: str, limit: int = 6) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html or "")
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</(p|div|li|td|th|tr|span)>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    lines = [
        " ".join(unescape(line).split())
        for line in text.splitlines()
    ]
    needles = (
        "error",
        "required",
        "please",
        "invalid",
        "入力",
        "選択",
        "確認",
        "必須",
        "未入力",
        "してください",
        "できません",
        "重量",
        "危険",
        "内容",
    )
    found: list[str] = []
    for line in lines:
        if not line or len(line) < 4:
            continue
        lowered = line.lower()
        if any(needle in lowered or needle in line for needle in needles):
            if line not in found:
                found.append(line[:180])
        if len(found) >= limit:
            break
    return " | ".join(found) if found else "-"


def _row_item_value(row, base_name: str, item_index: int) -> str:
    candidates = [f"{base_name}{item_index}"]
    if item_index == 1:
        fallbacks = {
            "內容物": ["郵局內容物"],
            "申告金額": ["郵局申告金額(USD)"],
            "數量": ["數量集合"],
        }
        candidates.extend(fallbacks.get(base_name, []))
    return _row_val(row, candidates)


def _iter_content_items(row, max_items: int = 10) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for item_index in range(1, max_items + 1):
        pkg = _row_item_value(row, "內容物", item_index)
        if not pkg:
            continue
        cost = _row_item_value(row, "申告金額", item_index) or "0"
        raw_num = _row_item_value(row, "數量", item_index) or "1"
        try:
            num = str(int(float(raw_num)))
        except Exception:
            num = "1"
        items.append({
            "index": str(item_index),
            "pkg": pkg,
            "cost": cost,
            "num": num,
        })
    return items


def _build_m060800_item_payload(
    html: str,
    page_url: str,
    row,
    is_eu: bool = False,
    hs_code: str = "",
    submit_command: str = "itemAdd2",
    item_index: int = 1,
) -> tuple[str, dict[str, str]]:
    form = _pick_form(
        html,
        preferred_action="M060800",
        required_fields=["itemBean.pkg"],
    )
    payload = dict(form["fields"])
    payload.pop("command", None)

    pkg = _row_item_value(row, "內容物", item_index)
    cost = _row_item_value(row, "申告金額", item_index) or "0"
    raw_num = _row_item_value(row, "數量", item_index) or "1"
    try:
        num = str(int(float(raw_num)))
    except Exception:
        num = "1"

    payload.update({
        "itemBean.pkg": pkg,
        "itemBean.cost.value": cost,
        "itemBean.num.value": num,
        "itemBean.curUnit": _select_option_value(
            form,
            "itemBean.curUnit",
            "USD",
            fallback=payload.get("itemBean.curUnit", "USD") or "USD",
        ),
    })
    profile = _shipping_profile(row)
    if profile == "postal_parcel_air":
        send_type_assignments = _set_value_assignments_for_labels(html, ["Postal Parcel", "POSTAL PARCEL"])
        if "shippingBean.sendType" in send_type_assignments:
            payload["shippingBean.sendType"] = send_type_assignments["shippingBean.sendType"]
        if "shippingBean.pkgType" in send_type_assignments:
            payload["shippingBean.pkgType"] = send_type_assignments["shippingBean.pkgType"]
        air_assignments = _set_value_assignments_for_labels(html, ["Air"])
        if "shippingBean.transType" in air_assignments:
            payload["shippingBean.transType"] = air_assignments["shippingBean.transType"]
        elif "chgTransTypeBtn" in html:
            payload["shippingBean.transType"] = "1"
        if payload.get("shippingBean.sendType", "") == "0" or not payload.get("shippingBean.transType", ""):
            raise RuntimeError(
                "Unable to resolve Postal Parcel/Air payload from M060800 HTML; "
                f"sendType={payload.get('shippingBean.sendType', '')}, "
                f"transType={payload.get('shippingBean.transType', '')}, "
                f"pkgType={payload.get('shippingBean.pkgType', '')}; "
                f"context={_html_context_for_labels(html, ['POSTAL PARCEL', 'Postal Parcel', 'AIR', 'Air'])}"
            )
    elif profile == "epacket_light":
        epacket_assignments = _set_value_assignments_for_labels(
            html,
            ["International ePacket light", "ePacket light", "EPACK_LITE", "Eパケットライト"],
        )
        if "shippingBean.sendType" in epacket_assignments:
            payload["shippingBean.sendType"] = epacket_assignments["shippingBean.sendType"]
        if payload.get("shippingBean.sendType", "") == "0":
            raise RuntimeError(
                "Unable to resolve ePacket payload from M060800 HTML; "
                f"sendType={payload.get('shippingBean.sendType', '')}, "
                f"transType={payload.get('shippingBean.transType', '')}, "
                f"pkgType={payload.get('shippingBean.pkgType', '')}; "
                f"context={_html_context_for_labels(html, ['International ePacket light', 'ePacket light', 'EPACK_LITE', 'Eパケットライト'])}"
            )
    total_jpy = _row_val(row, ["訂單合計申告金額(JPY)"])
    if total_jpy:
        payload["shippingBean.pkgTotalPrice.value"] = total_jpy
    if "ShippingBean.danger" in form["fields"]:
        payload["ShippingBean.danger"] = form["fields"].get("ShippingBean.danger") or "1"
    if "shippingBean.danger" in form["fields"]:
        payload["shippingBean.danger"] = form["fields"].get("shippingBean.danger") or "1"
    if is_eu and hs_code:
        for field_name in ("itemBean.hsCode", "itemBean.hsCode.value"):
            if field_name in form["fields"]:
                payload[field_name] = hs_code
                break
    payload[f"method:{submit_command}"] = ""
    return urljoin(page_url, form.get("action") or page_url), payload


def _build_m060800_next_payload(
    html: str,
    page_url: str,
    row,
) -> tuple[str, dict[str, str]]:
    form = _pick_form(
        html,
        preferred_action="M060800",
        required_fields=["shippingBean.sendType"],
    )
    payload = dict(form["fields"])
    payload.pop("command", None)
    total_jpy = _row_val(row, ["訂單合計申告金額(JPY)"])
    if total_jpy:
        payload["shippingBean.pkgTotalPrice.value"] = total_jpy
    if "ShippingBean.danger" in form["fields"]:
        payload["ShippingBean.danger"] = form["fields"].get("ShippingBean.danger") or "1"
    if "shippingBean.danger" in form["fields"]:
        payload["shippingBean.danger"] = form["fields"].get("shippingBean.danger") or "1"
    payload["method:regist"] = ""
    return urljoin(page_url, form.get("action") or page_url), payload


def _build_m060900_weight_payload(
    html: str,
    page_url: str,
    weight_grams: str = "100",
) -> tuple[str, dict[str, str]]:
    form = _pick_form(
        html,
        preferred_action="M060900",
        required_fields=["shippingBean.totalWeight.value"],
    )
    payload = dict(form["fields"])
    payload.pop("command", None)
    payload["shippingBean.totalWeight.value"] = _clean(weight_grams) or "100"
    if "shippingBean.invPrintNum.value" in form.get("selects", {}):
        payload["shippingBean.invPrintNum.value"] = (
            _first_non_empty_option_value(form, "shippingBean.invPrintNum.value", "1") or "1"
        )
    payload["method:regist"] = ""
    return urljoin(page_url, form.get("action") or page_url), payload


def _build_m061000_register_payload(
    html: str,
    page_url: str,
) -> tuple[str, dict[str, str]]:
    form = _pick_form(
        html,
        preferred_action="M061000",
        required_fields=["csrfToken"],
    )
    payload = dict(form["fields"])
    payload.pop("command", None)
    payload["method:regist"] = ""
    return urljoin(page_url, form.get("action") or page_url), payload


def _build_m061100_print_payload(
    html: str,
    page_url: str,
) -> tuple[str, dict[str, str]]:
    form = _pick_form(
        html,
        preferred_action="M061100",
        required_fields=["csrfToken"],
    )
    payload = dict(form["fields"])
    payload.pop("command", None)
    payload["method:print"] = ""
    return urljoin(page_url, form.get("action") or page_url), payload


def _extract_pdf_download_url(html: str, page_url: str) -> str:
    match = re.search(r"""["']([^"']*DOWNLOAD\?pdf=[^"']+)["']""", html or "", flags=re.IGNORECASE)
    if not match:
        match = re.search(r"""(DOWNLOAD\?pdf=[^\s"'<>]+)""", html or "", flags=re.IGNORECASE)
    if not match:
        return ""
    return urljoin(page_url, unescape(match.group(1)))


def _build_m061101_completed_payload(
    html: str,
    page_url: str,
) -> tuple[str, dict[str, str]]:
    form = _pick_form(
        html,
        preferred_action="M061101",
        required_fields=["csrfToken"],
    )
    payload = dict(form["fields"])
    payload.pop("command", None)
    payload["method:regist"] = ""
    return urljoin(page_url, form.get("action") or page_url), payload


def run_automation(
    df: pd.DataFrame,
    max_rows: int | None = None,
    log_cb=None,
    headless: bool = True,
) -> list[dict]:
    """
    執行日本郵政自動化打單。

    Parameters:
        df        : 待打單 DataFrame（來自 sheets.get_pending_orders）
        max_rows  : 最多處理幾筆（None = 全部）
        log_cb    : 進度回呼函數 (str -> None)
        headless  : 是否以 headless 模式執行（生產環境固定 True）

    Returns:
        成功結果清單，每筆為 dict {name, order_id, tracking, country_raw, date}
    """
    def _log(msg: str):
        if log_cb:
            log_cb(msg)
        logging.info(msg)

    from playwright.sync_api import sync_playwright

    rows = df if max_rows is None else df.head(max_rows)
    results: list[dict] = []
    user, pwd = _get_jp_post_creds()
    pw_cookies = []
    _log(f"🧭 automation build: {AUTOMATION_BUILD_ID}")

    if not user or not pwd:
        _log("❌ 未設定 JP_POST_USER / JP_POST_PASS，無法登入日本郵政")
        return results

    with sync_playwright() as p:
        chromium_args = [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--no-zygote",          # 容器環境必加：停用 zygote fork，避免 seccomp 限制殺掉進程
                "--disable-gpu",
                "--disable-software-rasterizer",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-default-apps",
                "--mute-audio",
                "--disable-features=site-per-process",
                "--blink-settings=imagesEnabled=false",
                "--disable-background-timer-throttling",
                "--disable-hang-monitor",
                "--disable-ipc-flooding-protection",
            ]

        def launch_browser():
            return p.chromium.launch(headless=headless, args=chromium_args)

        def new_context_with_cookies():
            new_context = browser.new_context(accept_downloads=True, ignore_https_errors=True)
            if pw_cookies:
                new_context.add_cookies(pw_cookies)
            return new_context

        browser = launch_browser()
        context = new_context_with_cookies()
        page = context.new_page()
        # 以 resource_type 攔截非必要資源（比副檔名更全面），大幅降低 Chromium 記憶體
        # stylesheet/image/font/media 全擋，保留 document/script/xhr/fetch（登入表單需要）
        def _abort_heavy(route):
            try:
                if route.request.resource_type in (
                    "image", "stylesheet", "font", "media", "ping", "eventsource", "other"
                ):
                    route.abort()
                else:
                    route.continue_()
            except Exception:
                pass  # route 可能已被解析
        page.route("**/*", _abort_heavy)

        def reset_playwright_page(reason: str):
            nonlocal browser, context, page
            _log(f"🔄 Playwright page 已關閉，重建頁面：{reason}")
            try:
                if not page.is_closed():
                    page.close()
            except Exception:
                pass
            try:
                page = context.new_page()
            except Exception:
                try:
                    context = new_context_with_cookies()
                    page = context.new_page()
                except Exception:
                    _log("🔄 Chromium browser process 已關閉，重新啟動")
                    browser = launch_browser()
                    context = new_context_with_cookies()
                    page = context.new_page()
            page.route("**/*", _abort_heavy)
            return page

        def ensure_playwright_page(reason: str):
            try:
                if page.is_closed():
                    reset_playwright_page(reason)
            except Exception:
                reset_playwright_page(reason)
            return page

        def set_content_from_requests(html: str):
            content = _with_base_href(
                _html_for_playwright_form(html),
                "https://www.int-mypage.post.japanpost.jp/mypage/",
            )
            last_exc = None
            for attempt in range(2):
                ensure_playwright_page(f"set_content attempt {attempt + 1}")
                try:
                    page.set_content(
                        content,
                        wait_until="domcontentloaded",
                        timeout=15000,
                    )
                    page.locator("body").count()
                    return
                except Exception as e:
                    last_exc = e
                    if "Target page, context or browser has been closed" not in str(e):
                        raise
                    reset_playwright_page("set_content target closed")
            raise last_exc

        def safe_page_url() -> str:
            try:
                return page.url
            except Exception:
                return "<closed>"

        # ── 診斷：驗證瀏覽器基礎導航能力 ────────────────
        try:
            _log(f"🔍 Chromium 版本: {browser.version}")
            page.goto("about:blank", wait_until="domcontentloaded", timeout=5000)
            _log("✅ 瀏覽器基礎導航測試通過")
        except Exception as _diag_e:
            _log(f"❌ 瀏覽器基礎導航失敗（可能缺少系統函式庫）: {_diag_e}")
            raise
        try:
            page.goto("https://example.com", wait_until="commit", timeout=15000)
            _log("✅ 外部網路連線測試通過")
        except Exception as _diag_e:
            _log(f"⚠️ 外部網路連線測試失敗（略過）: {_diag_e}")

        # ── 工具：重試包裝 ──────────────────────────
        def retry(fn, attempts=3, delay=1, name="action"):
            last_exc = None
            for i in range(attempts):
                try:
                    return fn()
                except Exception as e:
                    last_exc = e
                    time.sleep(delay)
            raise last_exc

        # ── 工具：安全點擊 ──────────────────────────
        def safe_click(sel: str, timeout=5000, label="click", critical=True):
            def _():
                loc = page.locator(sel)
                count = loc.count()
                target = None
                if count > 1:
                    for i in range(count):
                        c = loc.nth(i)
                        if c.is_visible() and c.is_enabled():
                            target = c
                            break
                target = target or loc.first
                target.wait_for(state="visible", timeout=timeout)
                target.click(timeout=timeout)
            try:
                retry(_, attempts=2, delay=1, name=label)
            except Exception as e:
                if critical:
                    _log(f"❌ 關鍵點擊失敗 [{label}]: {e}")
                    raise
                else:
                    _log(f"⚠️ 非關鍵點擊失敗 [{label}]，繼續執行")

        # ── 工具：安全填寫 ──────────────────────────
        def safe_fill(sel: str, value: str, timeout=5000, label="fill", critical=True):
            def _():
                loc = page.locator(sel).first
                loc.wait_for(state="visible", timeout=timeout)
                loc.fill(str(value), timeout=timeout)
            try:
                retry(_, attempts=2, delay=1, name=label)
            except Exception as e:
                if critical:
                    _log(f"❌ 填寫失敗 [{label}]: {e}")
                    raise
                else:
                    _log(f"⚠️ 填寫失敗 [{label}]，跳過")

        # ── 工具：安全下拉選單 ───────────────────────
        def safe_select(sel: str, label=None, value=None, timeout=5000):
            def _():
                page.wait_for_selector(sel, timeout=timeout)
                if label is not None:
                    page.select_option(sel, label=label)
                elif value is not None:
                    page.select_option(sel, value=value)
            retry(_, attempts=3, delay=1, name=f"select {sel}")

        # ── 工具：jQuery UI 雙重彈窗防禦 ─────────────
        def dismiss_dialogs(max_attempts=5):
            """
            偵測並關閉所有可見的 jQuery UI 警告對話框。
            包含 #dngrWarnDialog 危險物品警告及其他 .ui-dialog。
            """
            dismissed = 0
            for _ in range(max_attempts):
                result = page.evaluate("""() => {
                    // 1. 先嘗試 warningMsgOff 勾選 + OK 按鈕
                    const warnOff = document.getElementById('warningMsgOff');
                    if (warnOff && !warnOff.checked) {
                        warnOff.checked = true;
                    }
                    // 2. 點擊任何可見 ui-dialog 的按鈕
                    const buttons = document.querySelectorAll('.ui-dialog-buttonpane button');
                    for (const btn of buttons) {
                        const dialog = btn.closest('.ui-dialog');
                        if (dialog && dialog.style.display !== 'none' && dialog.offsetParent !== null) {
                            btn.click();
                            return true;
                        }
                    }
                    // 3. 針對 Yes/OK 類型確認窗
                    const okBtns = document.querySelectorAll('button[class*="yes"], button[class*="ok"]');
                    for (const b of okBtns) {
                        if (b.offsetParent !== null) {
                            b.click();
                            return true;
                        }
                    }
                    return false;
                }""")
                if result:
                    dismissed += 1
                    page.wait_for_timeout(400)
                else:
                    break
            if dismissed:
                _log(f"🛡️ 關閉 {dismissed} 個警告對話框")
            return dismissed > 0

        # ── 工具：處理「前次未完成」對話框 ──────────
        def handle_previous_label_dialog():
            try:
                btn = page.locator(
                    'button:has-text("Create a new label"), '
                    'input[value="Create a new label"]'
                )
                if btn.count() > 0 and btn.first.is_visible():
                    _log("🔄 偵測到前次未完成的資料，點擊『新製單』重置")
                    page.evaluate("""() => {
                        const btns = document.querySelectorAll('button, input[type=button]');
                        for (const b of btns) {
                            if (b.textContent.includes('Create a new label') ||
                                b.value === 'Create a new label') {
                                b.click(); break;
                            }
                        }
                    }""")
                    page.wait_for_timeout(800)
            except Exception:
                pass

        # ── 登入流程 ─────────────────────────────────
        def check_logged_in() -> bool:
            try:
                if page.locator('img[alt="Log out"], a:has-text("Log out")').count() > 0:
                    return True
                if "/mypage/M010001.do" in page.url or "/mypage/M06" in page.url:
                    return True
                if page.locator('img[alt="Create New Labels"]').count() > 0:
                    return True
            except Exception:
                pass
            return False

        def _login_via_requests():
            """
            用 requests HTTP POST 直接登入，繞過 Playwright 導航至登入頁（會 crash）
            回傳：(playwright_cookies, post_login_url, success_bool, response_html)
            """
            import requests as _req
            import re as _re

            base = "https://www.int-mypage.post.japanpost.jp"
            s = _req.Session()
            s.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            })

            # Step 1: GET 登入頁，取 CSRF token 與 session cookie
            _log("🌐 requests 登入：取得登入頁面...")
            r1 = s.get(f"{base}/mypage/M010000.do?request_locale=en", timeout=30)
            _log(f"  → {r1.status_code}, {len(r1.content)} bytes")

            csrf = ""
            m = _re.search(r'name="csrfToken"[^>]+value="([^"]+)"', r1.text)
            if not m:
                m = _re.search(r'value="([^"]+)"[^>]*name="csrfToken"', r1.text)
            if m:
                csrf = m.group(1)
                _log(f"  → CSRF: {csrf[:10]}...")
            else:
                _log("  ⚠️ 找不到 CSRF token")

            # Step 2: POST 登入表單
            # submitCommand('login') 的實際行為：把 command 欄位名改為 method:login
            # 所以 POST body 要用 method:login=（空值），而非 command=login
            _log("🌐 requests 登入：提交表單...")
            r2 = s.post(
                f"{base}/mypage/M010000.do",
                data={
                    "method:login": "",
                    "csrfToken": csrf,
                    "loginBean.id": user,
                    "loginBean.pw": pwd,
                    "request_locale": "en",
                    "localeSel": "en",
                },
                headers={
                    "Referer": f"{base}/mypage/M010000.do?request_locale=en",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=30,
                allow_redirects=True,
            )
            _log(f"  → {r2.status_code}, final URL: {r2.url}")
            # 回應 body 前 300 字（debug 用）
            _body_snip = r2.text[:300].replace('\n', ' ').replace('\r', '')
            _log(f"  → body[:300]: {_body_snip}")

            success = (
                "M010001.do" in r2.url
                or "/mypage/M06" in r2.url
                or "Log out" in r2.text
                or "Create New Labels" in r2.text
            )
            _log(f"  → {'✅ 登入成功' if success else '⚠️ 登入狀態不明'}")

            # 轉換為 Playwright cookie 格式
            pw_cookies = []
            for c in s.cookies:
                domain = c.domain
                if domain and not domain.startswith("."):
                    domain = "." + domain
                pw_cookies.append({
                    "name": c.name,
                    "value": c.value,
                    "domain": domain or ".int-mypage.post.japanpost.jp",
                    "path": c.path or "/",
                })
            _log(f"  → {len(pw_cookies)} 個 cookies 提取完成")
            return s, pw_cookies, r2.url, success, r2.text

        def attempt_login():
            _log(f"🔐 執行登入，帳號: {user[:3]}***")
            login_url = (
                "https://www.int-mypage.post.japanpost.jp/mypage/M010000.do"
                "?request_locale=en"
            )
            # ?request_locale=en 強制英文介面
            page.goto(login_url, wait_until="load", timeout=60000)

            # 填帳號密碼
            # 精確 ID（DOM 檢查確認）
            user_loc = page.locator('#M010000_loginBean_id, input[name="loginBean.id"]')
            pass_loc = page.locator('#M010000_loginBean_pw, input[name="loginBean.pw"]')
            if user_loc.count() > 0:
                user_loc.first.fill(user)
            else:
                _log("⚠️ 找不到帳號欄位")
            if pass_loc.count() > 0:
                pass_loc.first.fill(pwd)
            else:
                _log("⚠️ 找不到密碼欄位")

            page.wait_for_timeout(500)

            # 登入鈕結構：<a href="javascript:submitCommand('login')"><img alt="Log in"></a>
            # 必須點錨點（而非 img）才能觸發 javascript: href
            clicked = False
            for sel in [
                'a:has(img[alt="Log in"])',    # DOM 確認的結構
                'a[href*="submitCommand"]',     # 備用
                'img[alt="Log in"]',            # 備用（事件冒泡）
            ]:
                loc = page.locator(sel)
                if loc.count() > 0:
                    loc.first.click()
                    clicked = True
                    _log(f"✅ 點擊登入按鈕 ({sel})")
                    break
            if not clicked:
                _log("⚠️ 未找到登入按鈕，嘗試 JS submitCommand")
                page.evaluate("submitCommand('login')")

            page.wait_for_timeout(3000)

            if not check_logged_in():
                # 備案：JS submit
                try:
                    page.evaluate("() => { if(typeof submitCommand === 'function') submitCommand('login'); }")
                    page.wait_for_timeout(2000)
                except Exception:
                    pass

        # ── 執行登入：優先用 requests 繞過 Playwright 登入頁 crash ──────
        _login_ok = False
        req_session = None
        main_menu_html = ""
        main_menu_url = "https://www.int-mypage.post.japanpost.jp/mypage/"
        try:
            req_session, pw_cookies, post_url, req_ok, post_html = _login_via_requests()
            if pw_cookies:
                context.add_cookies(pw_cookies)
                _log("✅ Cookies 已注入 Playwright context")
            if req_ok:
                main_menu_html = post_html
                main_menu_url = post_url or main_menu_url
                # Struts login success is a server-side forward: the URL can remain M010000.do
                # while the response body already contains the logged-in main menu.
                create_count = 1 if (
                    "Create New Labels" in post_html
                    or _extract_submit_command_for_label(post_html, "Create New Labels")
                ) else 0
                _log(
                    "🧭 requests 已取得登入後主選單 HTML："
                    f"url={main_menu_url}, create_buttons={create_count}"
                )
                if create_count == 0:
                    body_snip = post_html[:300].replace("\n", " ").replace("\r", "")
                    _log(f"⚠️ 主選單 HTML 未找到 Create New Labels，body[:300]={body_snip!r}")
                _login_ok = True
                _log("✅ requests 登入成功，Cookies 與主選單 HTML 已就位；不回灌 Playwright HTML")
        except Exception as _re_err:
            _log(f"⚠️ requests 登入例外：{_re_err}")

        if not _login_ok:
            _log("❌ requests 登入失敗，請確認帳號密碼")
            raise RuntimeError("登入失敗：requests HTTP 登入未成功，請確認帳號密碼是否正確")

        if _login_ok:
            _log("✅ 登入成功")
        else:
            _log("⚠️ 登入狀態未確認，嘗試繼續...")

        # ── 逐筆處理訂單 ─────────────────────────────
        label_form_html = ""
        label_form_url = ""

        def return_to_main_menu_via_requests() -> bool:
            nonlocal main_menu_html, main_menu_url
            if not req_session or not main_menu_html:
                return False
            if (
                "Create New Labels" in main_menu_html
                or _extract_submit_command_for_label(main_menu_html, "Create New Labels")
            ):
                return True
            command = _extract_preferred_submit_command(main_menu_html, ["returnTop"])
            if not command:
                return False
            action, payload = _build_struts_submit(
                main_menu_html,
                command,
                "https://www.int-mypage.post.japanpost.jp/mypage/",
            )
            _log(f"↩️ requests 回主選單：command={command}, action={action}")
            r = req_session.post(
                action,
                data=payload,
                headers={
                    "Referer": main_menu_url,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=30,
                allow_redirects=True,
            )
            body_snip = r.text[:240].replace("\n", " ").replace("\r", "")
            _log(f"  → returnTop HTTP {r.status_code}, url={r.url}, body[:240]={body_snip}")
            if r.status_code >= 400:
                return False
            main_menu_html = r.text
            main_menu_url = r.url
            return True

        def open_create_label_form_via_requests() -> bool:
            nonlocal label_form_html, label_form_url, main_menu_html, main_menu_url
            if not req_session or not main_menu_html:
                return False
            return_to_main_menu_via_requests()
            current_html = main_menu_html
            referer_url = main_menu_url
            command_labels = [["Create New Labels"]] + [
                ["Enter the sender", "sender", "Next", "Register", "Select"]
                for _ in range(7)
            ]
            try:
                for step_idx, labels in enumerate(command_labels, start=1):
                    command = ""
                    if step_idx > 1:
                        command = _choose_label_flow_command(current_html, referer_url)
                    if not command:
                        for label in labels:
                            command = _extract_submit_command_for_label(current_html, label)
                            if command:
                                break
                    if not command:
                        _log(
                            "⚠️ requests 開啟打單頁：找不到下一步 command "
                            f"(step={step_idx}, labels={labels}, "
                            f"commands={_summarize_submit_commands(current_html)})"
                        )
                        raise RuntimeError("requests 無法找到下一步 command，停止以避免 Playwright crash")
                    action, payload = _build_struts_submit(
                        current_html,
                        command,
                        "https://www.int-mypage.post.japanpost.jp/mypage/",
                    )
                    _log(f"🌐 requests 開啟打單頁：step={step_idx}, command={command}, action={action}")
                    r = req_session.post(
                        action,
                        data=payload,
                        headers={
                            "Referer": referer_url,
                            "Content-Type": "application/x-www-form-urlencoded",
                        },
                        timeout=30,
                        allow_redirects=True,
                    )
                    body_snip = r.text[:240].replace("\n", " ").replace("\r", "")
                    _log(f"  → {r.status_code}, final URL: {r.url}, body[:240]: {body_snip}")
                    if r.status_code != 200:
                        return False
                    looks_like_sender_form = (
                        "M060505" in r.url
                        or "addrToBean" in r.text
                        or "#M060505_" in r.text
                    )
                    if looks_like_sender_form:
                        label_form_html = r.text
                        label_form_url = r.url
                        _log(
                            "✅ requests 已取得 M060505/addrToBean 表單 HTML；"
                            f"url={label_form_url}，不回灌 Playwright"
                        )
                        return True
                    current_html = r.text
                    referer_url = r.url
                _log(
                    "⚠️ requests 開啟打單頁：多步提交後仍未到寄件人表單，"
                    f"commands={_summarize_submit_commands(current_html)}"
                )
                raise RuntimeError("requests 多步提交後仍未到寄件人表單，停止以避免 Playwright crash")
            except Exception as e:
                _log(f"❌ requests 開啟打單頁例外：{e}")
                raise

        def submit_addr_to_bean_via_requests(row: pd.Series, order_id: str):
            if not req_session or not label_form_html:
                raise RuntimeError("尚未取得 M060505/addrToBean 表單 HTML，無法提交收件人 payload")

            from .sheets import COUNTRY_CODE_MAP

            country_raw = _get_excel_val(row, ["收件人國家", "Country"])
            country_code = COUNTRY_CODE_MAP.get(country_raw, "")
            name_val = _get_excel_val(row, ["Shipping Name", "Shipping Name_1"])
            final_name = f"{name_val} {order_id}".strip()

            form = _pick_form(
                label_form_html,
                preferred_action="M060505",
                required_fields=["addrToBean.nam"],
            )
            command = (
                _extract_submit_command_for_label(label_form_html, "Next")
                or _extract_preferred_submit_command(label_form_html, ["next", "regist", "addrToBean"])
                or "next"
            )
            data = dict(form["fields"])
            data.pop("command", None)
            country_value = _select_option_value(
                form,
                "addrToBean.couCode",
                country_raw,
                fallback=country_code if country_code != "EU" else data.get("addrToBean.couCode", ""),
            )
            data.update({
                f"method:{command}": "",
                "addrToBean.couCode": country_value,
                "addrToBean.nam": final_name,
                "addrToBean.add1": "",
                "addrToBean.add2": _get_excel_val(row, ["Shipping Street", "收件地址"]),
                "addrToBean.add3": _get_excel_val(row, ["Shipping City", "城市"]),
                "addrToBean.pref": _get_excel_val(row, ["收件人洲/省", "State"]),
                "addrToBean.postal": _get_excel_val(row, ["Shipping Zip", "郵遞區號"]),
                "addrToBean.tel": _get_excel_val(row, ["Shipping Phone", "電話"]),
            })
            post_target = urljoin(label_form_url or main_menu_url, form.get("action") or label_form_url)
            _log(
                "🌐 requests 提交 M060505/addrToBean 收件人 payload："
                f"command={command}, action={post_target}, name={final_name}, country={country_raw}"
            )
            resp = req_session.post(
                post_target,
                data=data,
                headers={
                    "Referer": label_form_url or main_menu_url,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=30,
                allow_redirects=True,
            )
            body_snip = resp.text[:240].replace("\n", " ").replace("\r", "")
            _log(f"  → addrToBean HTTP {resp.status_code}, url={resp.url}, body[:240]={body_snip}")
            marker_summary = ", ".join(
                marker
                for marker in [
                    "addrToBean",
                    "itemBean",
                    "shippingBean",
                    "M060505",
                    "M060800",
                    "International ePacket light",
                    "POSTAL PARCEL",
                ]
                if marker in resp.text
            ) or "-"
            _log(
                "🔎 addrToBean response diagnostics："
                f"commands={_summarize_submit_commands(resp.text) or '-'}; "
                f"markers={marker_summary}; "
                f"forms={_summarize_forms(resp.text)}"
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"M060505 addrToBean submit failed: HTTP {resp.status_code}")
            if "addrToBean" in resp.text and "error" in resp.text[:5000].lower():
                _log("⚠️ M060505 回應仍停留在收件人頁，可能有欄位驗證錯誤")
            return resp, country_raw, country_code

        def submit_m060800_item_via_requests(html: str, page_url: str, row: pd.Series, is_eu: bool):
            items = _iter_content_items(row)
            if not items:
                raise RuntimeError("M060800 payload has no content items")
            current_html = html
            current_url = page_url
            resp = None
            for pos, item in enumerate(items, start=1):
                hs_code = ""
                if is_eu and item["pkg"]:
                    hs_code = predict_hs_code(item["pkg"], log_cb=log_cb) or ""
                action, payload = _build_m060800_item_payload(
                    current_html,
                    current_url,
                    row,
                    is_eu=is_eu,
                    hs_code=hs_code,
                    submit_command="itemAdd2",
                    item_index=int(item["index"]),
                )
                _log(
                    "🌐 requests 提交 M060800 Confirm 內容物 payload："
                    f"item={pos}/{len(items)}, action={action}, "
                    f"pkg={payload.get('itemBean.pkg', '')}, "
                    f"cost={payload.get('itemBean.cost.value', '')}, "
                    f"num={payload.get('itemBean.num.value', '')}, "
                    f"profile={_shipping_profile(row) or '-'}, "
                    f"sendType={payload.get('shippingBean.sendType', '')}, "
                    f"transType={payload.get('shippingBean.transType', '')}, "
                    f"pkgType={payload.get('shippingBean.pkgType', '')}"
                )
                resp = req_session.post(
                    action,
                    data=payload,
                    headers={
                        "Referer": current_url,
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    timeout=30,
                    allow_redirects=True,
                )
                body_snip = resp.text[:240].replace("\n", " ").replace("\r", "")
                _log(f"  → M060800 HTTP {resp.status_code}, url={resp.url}, body[:240]={body_snip}")
                marker_summary = ", ".join(
                    marker
                    for marker in [
                        "addrToBean",
                        "itemBean",
                        "shippingBean",
                        "M060800",
                        "M060900",
                        "M061000",
                        "Register Shipment",
                        "totalWeight",
                        "DOWNLOAD?pdf=",
                    ]
                    if marker in resp.text
                ) or "-"
                _log(
                    "🔎 M060800 response diagnostics："
                    f"commands={_summarize_submit_commands(resp.text) or '-'}; "
                    f"markers={marker_summary}; "
                    f"forms={_summarize_forms(resp.text)}"
                )
                if resp.status_code >= 400:
                    raise RuntimeError(f"M060800 item submit failed: HTTP {resp.status_code}")
                current_html = resp.text
                current_url = resp.url

            if resp is None:
                raise RuntimeError("M060800 item submit did not produce a response")
            if "M060800" in resp.text and "M060900" not in resp.text:
                next_action, next_payload = _build_m060800_next_payload(resp.text, resp.url, row)
                _log(
                    "🌐 requests 提交 M060800 Next payload："
                    f"action={next_action}, "
                    f"sendType={next_payload.get('shippingBean.sendType', '')}, "
                    f"transType={next_payload.get('shippingBean.transType', '')}, "
                    f"pkgType={next_payload.get('shippingBean.pkgType', '')}, "
                    f"totalJpy={next_payload.get('shippingBean.pkgTotalPrice.value', '')}"
                )
                resp = req_session.post(
                    next_action,
                    data=next_payload,
                    headers={
                        "Referer": resp.url,
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    timeout=30,
                    allow_redirects=True,
                )
                body_snip = resp.text[:240].replace("\n", " ").replace("\r", "")
                _log(f"  → M060800 Next HTTP {resp.status_code}, url={resp.url}, body[:240]={body_snip}")
                marker_summary = ", ".join(
                    marker
                    for marker in [
                        "addrToBean",
                        "itemBean",
                        "shippingBean",
                        "M060800",
                        "M060900",
                        "M061000",
                        "Register Shipment",
                        "totalWeight",
                        "DOWNLOAD?pdf=",
                    ]
                    if marker in resp.text
                ) or "-"
                _log(
                    "🔎 M060800 Next response diagnostics："
                    f"commands={_summarize_submit_commands(resp.text) or '-'}; "
                    f"markers={marker_summary}; "
                    f"forms={_summarize_forms(resp.text)}"
                )
                if resp.status_code >= 400:
                    raise RuntimeError(f"M060800 next submit failed: HTTP {resp.status_code}")
            return resp

        def submit_m060900_weight_via_requests(html: str, page_url: str):
            action, payload = _build_m060900_weight_payload(
                html,
                page_url,
                weight_grams="100",
            )
            _log(
                "🌐 requests 提交 M060900 重量 payload："
                f"action={action}, weight={payload.get('shippingBean.totalWeight.value', '')}, "
                f"invPrintNum={payload.get('shippingBean.invPrintNum.value', '')}"
            )
            resp = req_session.post(
                action,
                data=payload,
                headers={
                    "Referer": page_url,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=30,
                allow_redirects=True,
            )
            body_snip = resp.text[:240].replace("\n", " ").replace("\r", "")
            _log(f"  → M060900 HTTP {resp.status_code}, url={resp.url}, body[:240]={body_snip}")
            marker_summary = ", ".join(
                marker
                for marker in [
                    "shippingBean",
                    "M060900",
                    "M061000",
                    "M061100",
                    "Register Shipment",
                    "Print after agreeing",
                    "DOWNLOAD?pdf=",
                    "totalWeight",
                ]
                if marker in resp.text
            ) or "-"
            _log(
                "🔎 M060900 response diagnostics："
                f"commands={_summarize_submit_commands(resp.text) or '-'}; "
                f"markers={marker_summary}; "
                f"forms={_summarize_forms(resp.text)}; "
                f"errors={_summarize_error_text(resp.text)}"
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"M060900 weight submit failed: HTTP {resp.status_code}")
            return resp

        def submit_m061000_register_via_requests(html: str, page_url: str):
            action, payload = _build_m061000_register_payload(
                html,
                page_url,
            )
            _log(f"🌐 requests 提交 M061000 Register Shipment payload：action={action}")
            resp = req_session.post(
                action,
                data=payload,
                headers={
                    "Referer": page_url,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=30,
                allow_redirects=True,
            )
            body_snip = resp.text[:240].replace("\n", " ").replace("\r", "")
            _log(f"  → M061000 HTTP {resp.status_code}, url={resp.url}, body[:240]={body_snip}")
            tracking_match = re.search(r"([A-Z]{2}\d{9}JP)", resp.text or "")
            marker_summary = ", ".join(
                marker
                for marker in [
                    "M061000",
                    "M061100",
                    "Print after agreeing",
                    "DOWNLOAD?pdf=",
                    "tracking",
                    "Completed",
                ]
                if marker in resp.text
            ) or "-"
            _log(
                "🔎 M061000 response diagnostics："
                f"commands={_summarize_submit_commands(resp.text) or '-'}; "
                f"markers={marker_summary}; "
                f"tracking={tracking_match.group(1) if tracking_match else '-'}; "
                f"forms={_summarize_forms(resp.text)}"
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"M061000 register submit failed: HTTP {resp.status_code}")
            return resp

        def submit_m061100_print_via_requests(html: str, page_url: str, row: pd.Series, order_id: str):
            action, payload = _build_m061100_print_payload(
                html,
                page_url,
            )
            _log(f"🌐 requests 提交 M061100 Print payload：action={action}")
            resp = req_session.post(
                action,
                data=payload,
                headers={
                    "Referer": page_url,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=30,
                allow_redirects=True,
            )
            content_type = resp.headers.get("Content-Type", "")
            pdf_bytes = resp.content if "%PDF" in resp.content[:16].decode("latin1", errors="ignore") else b""
            text = "" if pdf_bytes else (resp.text or "")
            body_snip = text[:240].replace("\n", " ").replace("\r", "") if text else "<binary>"
            tracking_match = re.search(r"([A-Z]{2}\d{9}JP)", text)
            _log(
                "  → M061100 HTTP "
                f"{resp.status_code}, url={resp.url}, content-type={content_type}, "
                f"pdf_bytes={len(pdf_bytes)}, body[:240]={body_snip}"
            )
            marker_summary = ", ".join(
                marker
                for marker in [
                    "M061100",
                    "DOWNLOAD?pdf=",
                    "Completed",
                    "Print Completed",
                    "returnTop",
                ]
                if marker in text
            ) or "-"
            _log(
                "🔎 M061100 response diagnostics："
                f"commands={_summarize_submit_commands(text) or '-'}; "
                f"markers={marker_summary}; "
                f"tracking={tracking_match.group(1) if tracking_match else '-'}; "
                f"forms={_summarize_forms(text)}"
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"M061100 print submit failed: HTTP {resp.status_code}")
            pdf_url = _extract_pdf_download_url(text, resp.url) if text else ""
            tracking_for_name = tracking_match.group(1) if tracking_match else "NO_TRACKING"
            if pdf_url and not pdf_bytes:
                _log(f"🌐 requests 下載 PDF：{pdf_url}")
                pdf_resp = req_session.get(
                    pdf_url,
                    headers={"Referer": resp.url},
                    timeout=30,
                    allow_redirects=True,
                )
                pdf_prefix = pdf_resp.content[:16].decode("latin1", errors="ignore")
                _log(
                    "  → PDF GET "
                    f"HTTP {pdf_resp.status_code}, content-type={pdf_resp.headers.get('Content-Type', '')}, "
                    f"bytes={len(pdf_resp.content)}, prefix={pdf_prefix!r}"
                )
                if pdf_resp.status_code < 400 and pdf_resp.content.startswith(b"%PDF"):
                    pdf_bytes = pdf_resp.content
                else:
                    _log("⚠️ PDF 下載回應不是 PDF，保留 diagnostics 後停止")
            pdf_uploaded = False
            if pdf_bytes:
                content_name = _get_excel_val(row, ["郵局內容物"]) or _get_excel_val(row, ["內容物1"]) or "Item"
                ship_name = _get_excel_val(row, ["Shipping Name", "Shipping Name_1"])
                fname = _sanitize_filename(
                    f"{content_name}_{order_id}_{tracking_for_name}_{ship_name}.pdf"
                )
                upload_pdf(pdf_bytes, fname, log_cb=log_cb)
                pdf_uploaded = True
                _log(f"✅ PDF 已透過 requests 取得並上傳：{fname}")
            completed = False
            completed_response = None
            if text and "M061101" in text:
                action, completed_payload = _build_m061101_completed_payload(text, resp.url)
                _log(f"🌐 requests 提交 M061101 Completed payload：action={action}")
                done_resp = req_session.post(
                    action,
                    data=completed_payload,
                    headers={
                        "Referer": resp.url,
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    timeout=30,
                    allow_redirects=True,
                )
                done_snip = (done_resp.text or "")[:240].replace("\n", " ").replace("\r", "")
                _log(
                    f"  → M061101 HTTP {done_resp.status_code}, url={done_resp.url}, "
                    f"body[:240]={done_snip}"
                )
                if done_resp.status_code >= 400:
                    raise RuntimeError(f"M061101 completed submit failed: HTTP {done_resp.status_code}")
                completed = True
                completed_response = done_resp
            return {
                "response": resp,
                "completed_response": completed_response,
                "tracking": tracking_for_name if tracking_for_name != "NO_TRACKING" else "",
                "pdf_uploaded": pdf_uploaded,
                "completed": completed,
            }

        for row_idx, row in rows.iterrows():
            order_id = _get_excel_val(row, ["注文番号(貼上原始資料)", "注文番号(貼上原始資料)_1"])
            _log(f"\n{'='*50}\n▶ 開始處理訂單：{order_id}（索引 {row_idx}）")

            tracking = "ERROR"
            results_before_order = len(results)
            try:
                # ── 防前次未完成對話框 ────────────────
                handle_previous_label_dialog()

                # ── 點擊「Create New Labels」────────────
                if not open_create_label_form_via_requests():
                    raise RuntimeError("requests 無法取得 M060505/addrToBean 表單，停止以避免 Playwright crash")

                # ── Step 2: M060505 收件人資訊改用純 requests payload submit ─────
                addr_resp, country_raw, country_code = submit_addr_to_bean_via_requests(row, order_id)
                main_menu_html = addr_resp.text
                main_menu_url = addr_resp.url
                _log("✅ M060505 收件人表單已用 requests payload submit；不回灌 Playwright HTML")
                is_eu = (country_code == "EU")
                if "itemBean" in addr_resp.text and "M060800" in addr_resp.text:
                    item_resp = submit_m060800_item_via_requests(
                        addr_resp.text,
                        addr_resp.url,
                        row,
                        is_eu,
                    )
                    main_menu_html = item_resp.text
                    main_menu_url = item_resp.url
                    _log("✅ M060800 內容物/運送表單已用 requests payload submit；不回灌 Playwright HTML")
                    if "shippingBean.totalWeight.value" in item_resp.text and "M060900" in item_resp.text:
                        weight_resp = submit_m060900_weight_via_requests(
                            item_resp.text,
                            item_resp.url,
                        )
                        main_menu_html = weight_resp.text
                        main_menu_url = weight_resp.url
                        _log("✅ M060900 重量表單已用 requests payload submit；不回灌 Playwright HTML")
                        if "M061000" in weight_resp.text and "Register Shipment" in weight_resp.text:
                            register_resp = submit_m061000_register_via_requests(
                                weight_resp.text,
                                weight_resp.url,
                            )
                            main_menu_html = register_resp.text
                            main_menu_url = register_resp.url
                            _log("✅ M061000 Register Shipment 已用 requests payload submit；不回灌 Playwright HTML")
                            if "M061100" in register_resp.text and "Print after agreeing" in register_resp.text:
                                print_result = submit_m061100_print_via_requests(
                                    register_resp.text,
                                    register_resp.url,
                                    row,
                                    order_id,
                                )
                                print_resp = print_result["response"]
                                completed_resp = print_result.get("completed_response")
                                if completed_resp is not None and "text/html" in completed_resp.headers.get("Content-Type", ""):
                                    main_menu_html = completed_resp.text
                                    main_menu_url = completed_resp.url
                                    _log(f"🔁 已更新下一筆起點為 Completed 後主選單：url={main_menu_url}")
                                elif "text/html" in print_resp.headers.get("Content-Type", ""):
                                    main_menu_html = print_resp.text
                                    main_menu_url = print_resp.url
                                _log("✅ M061100 Print 已用 requests payload submit；不回灌 Playwright HTML")
                                if print_result.get("tracking") and print_result.get("pdf_uploaded"):
                                    tracking = print_result["tracking"]
                                    results.append(_build_result_record(row, order_id, tracking))
                                    _log(f"📌 訂單 {order_id} 完成，貨運單號：{tracking}")
                if len(results) > results_before_order:
                    _log(f"✅ 訂單 {order_id} requests 打單流程已完成並回傳結果")
                else:
                    _log(
                        f"⏸️ 訂單 {order_id} requests 流程已停止但未取得完整結果；"
                        "請依最後一段 diagnostics 繼續排查"
                    )
                continue

                # ── Step 3: 運送方式分流 ──────────────
                shipping = _get_excel_val(row, ["郵局運送方式(複數商品請自行確認是否走小包)"])
                _log(f"📦 運送方式：{shipping}")
                is_eu = (country_code == "EU")

                if "ePacket" in shipping or "小包" in shipping:
                    # ── ePacket 子流程 ────────────────
                    _log("➡️ ePacket 子流程")
                    safe_click("img[alt='International ePacket light']", label="select_epacket")
                    page.wait_for_timeout(1000)
                    dismiss_dialogs()

                    for i in range(1, 5):
                        pkg = _clean(row.get(f"內容物{i}", ""))
                        if not pkg:
                            break
                        cost = _clean(row.get(f"申告金額{i}", "0"))
                        raw_num = row.get(f"數量{i}", 1)
                        try:
                            num = str(int(float(raw_num))) if raw_num != "" else "1"
                        except Exception:
                            num = "1"

                        safe_fill("input[name='itemBean.pkg']", pkg, label=f"pkg_{i}")
                        safe_fill("input[name='itemBean.cost.value']", cost, label=f"cost_{i}")
                        try:
                            page.select_option("select[name='itemBean.curUnit']", "USD")
                        except Exception:
                            pass
                        safe_fill("input[name='itemBean.num.value']", num, label=f"num_{i}")

                        # EU → Gemini HS Code
                        if is_eu:
                            hs = predict_hs_code(pkg, log_cb=log_cb)
                            if hs:
                                # 嘗試帶 .value 後綴變體
                                for hs_sel in [
                                    "input[name='itemBean.hsCode']",
                                    "input[name='itemBean.hsCode.value']",
                                ]:
                                    try:
                                        if page.locator(hs_sel).count() > 0:
                                            safe_fill(hs_sel, hs, label=f"hscode_{i}", critical=False)
                                            break
                                    except Exception:
                                        pass

                        safe_click("input[value='Confirm']", label=f"confirm_{i}")
                        page.wait_for_timeout(800)
                        dismiss_dialogs()

                        # 檢查數量上限警告
                        try:
                            limit_text = page.evaluate("""() => {
                                const ds = document.querySelectorAll('.ui-dialog[style*="display: block"]');
                                for (const d of ds) {
                                    if (d.innerText && (d.innerText.includes('allowable limit') ||
                                        d.innerText.includes('制限数を超えています'))) {
                                        return d.innerText;
                                    }
                                }
                                return null;
                            }""")
                            if limit_text:
                                _log(f"⚠️ 數量上限警告（第 {i} 項），停止添加")
                                dismiss_dialogs()
                                break
                        except Exception:
                            pass

                    # 填寫 JPY 合計金額
                    total_jpy = _get_excel_val(row, ["訂單合計申告金額(JPY)"])
                    if total_jpy:
                        safe_fill(
                            "input[name='shippingBean.pkgTotalPrice.value']",
                            total_jpy, label="total_jpy", critical=False,
                        )

                else:
                    # ── PostalParcel_Air 子流程 ────────
                    _log("➡️ PostalParcel Air 子流程")
                    safe_click("img[alt='POSTAL PARCEL']", label="select_postal")
                    page.wait_for_timeout(1000)
                    dismiss_dialogs()
                    safe_click("img[alt='AIR']", label="select_air")
                    page.wait_for_timeout(800)
                    dismiss_dialogs()

                    try:
                        page.check("input[name*='senderInstruction']")
                        page.check("input[name*='fwTransType']")
                    except Exception:
                        pass

                    pkg = _clean(row.get("內容物1", ""))
                    if pkg:
                        cost = _clean(row.get("申告金額1", "0"))
                        raw_num = row.get("數量1", 1)
                        try:
                            num = str(int(float(raw_num))) if raw_num != "" else "1"
                        except Exception:
                            num = "1"
                        safe_fill("input[name='itemBean.pkg']", pkg, label="parcel_pkg")
                        safe_fill("input[name='itemBean.cost.value']", cost, label="parcel_cost")
                        safe_fill("input[name='itemBean.num.value']", num, label="parcel_num")

                        if is_eu:
                            hs = predict_hs_code(pkg, log_cb=log_cb)
                            if hs:
                                for hs_sel in [
                                    "input[name='itemBean.hsCode']",
                                    "input[name='itemBean.hsCode.value']",
                                ]:
                                    try:
                                        if page.locator(hs_sel).count() > 0:
                                            safe_fill(hs_sel, hs, label="parcel_hscode", critical=False)
                                            break
                                    except Exception:
                                        pass

                        safe_click("input[value='Confirm']", label="parcel_confirm")
                        page.wait_for_timeout(800)
                        dismiss_dialogs()

                # ── Step 3: 危險物品聲明 + Next ────────
                for danger_sel in [
                    "#M060800_ShippingBean_danger",
                    "input[name='ShippingBean.danger']",
                ]:
                    try:
                        if page.locator(danger_sel).is_visible(timeout=1500):
                            page.locator(danger_sel).check()
                            _log("☑️ 勾選危險物品聲明")
                            break
                    except Exception:
                        pass

                safe_click("input[value='Next']", label="contents_next", critical=True)
                page.wait_for_timeout(1500)

                # ── Step 4: 重量頁（若存在）────────────
                weight_sel = "#M060900_shippingBean_totalWeight_value"
                if page.locator(weight_sel).is_visible(timeout=2000):
                    _log("⚖️ Step 4 重量頁")
                    try:
                        if page.locator("#M060900_ShippingBean_danger").is_visible(timeout=1000):
                            page.locator("#M060900_ShippingBean_danger").check()
                    except Exception:
                        pass
                    safe_fill(weight_sel, "100", label="weight")
                    safe_click("input[type='button'][value='Next']", label="weight_next", critical=True)
                    page.wait_for_timeout(1500)

                # ── Step 5: 確認 + Register Shipment ───
                safe_click("input[value='Register Shipment']", label="register", critical=True)
                page.wait_for_timeout(2000)

                # ── Step 6: PDF 封包攔截（M061000.do）──
                _log("📥 抵達 M061000.do，準備攔截 PDF...")
                page.wait_for_url("**/M061000.do*", timeout=10000)
                pdf_content = None

                try:
                    with page.expect_request(
                        lambda req: "DOWNLOAD?pdf=" in req.url, timeout=15000
                    ) as req_info:
                        page.locator(
                            "input[value*='Print after agreeing'][onclick*='print']"
                        ).evaluate("n => n.click()")

                    pdf_url = req_info.value.url
                    cookie_str = "; ".join(
                        [f"{c['name']}={c['value']}" for c in page.context.cookies()]
                    )
                    resp = page.request.get(pdf_url, headers={"Cookie": cookie_str})
                    if resp.ok:
                        pdf_content = resp.body()
                        _log(f"✅ PDF 攔截成功（{len(pdf_content)} bytes）")
                    else:
                        _log(f"⚠️ PDF 請求失敗: HTTP {resp.status}")
                except Exception as e:
                    _log(f"⚠️ PDF 攔截失敗: {e}")

                # ── Step 7: 擷取貨運單號（M061100.do）──
                _log("🔍 等待跳轉至 M061100.do 擷取貨運單號...")
                try:
                    page.wait_for_url("**/M061100.do*", timeout=6000)
                    page.wait_for_timeout(1500)
                except Exception:
                    _log("⚠️ 未偵測到自動跳轉，強制導航至完成頁")
                    page.goto(
                        "https://www.int-mypage.post.japanpost.jp/mypage/M061100.do"
                    )
                    page.wait_for_timeout(2000)

                page_text = page.locator("body").inner_text()
                match = re.search(r"([A-Z]{2}\d{9}JP)", page_text)
                if match:
                    tracking = match.group(1)
                    _log(f"🎉 貨運單號：{tracking}")
                else:
                    # 備案：精確 CSS 路徑
                    tracking_css = (
                        "#loaded > table:nth-child(4) > tbody > tr:nth-child(1) > "
                        "td > div > div:nth-child(3) > div > table > tbody > "
                        "tr:nth-child(2) > td:nth-child(1) > div > b"
                    )
                    try:
                        if page.locator(tracking_css).is_visible(timeout=2000):
                            tracking = page.locator(tracking_css).inner_text().strip()
                            _log(f"💡 備案 CSS 取得單號：{tracking}")
                    except Exception:
                        _log("⚠️ 無法擷取貨運單號")

                # ── Step 8: PDF 命名、存檔、上傳 Drive ─
                if pdf_content and tracking not in ("ERROR", "N/A", ""):
                    content_name = _get_excel_val(row, ["郵局內容物"]) or "Item"
                    ship_name = _get_excel_val(row, ["Shipping Name", "Shipping Name_1"])
                    fname = _sanitize_filename(
                        f"{content_name}_{order_id}_{tracking}_{ship_name}.pdf"
                    )
                    upload_pdf(pdf_content, fname, log_cb=log_cb)

                # ── Step 9: 點擊 Completed 返回首頁 ────
                try:
                    final_btn = page.locator(
                        "input[value='Print Completed'], input[value='Completed']"
                    )
                    if final_btn.count() > 0:
                        final_btn.first.click(timeout=5000)
                        page.wait_for_timeout(800)
                        _log("✅ 已點擊 Completed，返回首頁")
                    else:
                        _log("⚠️ 未找到 Completed 按鈕，略過")
                except Exception as e:
                    _log(f"⚠️ Step 9 點擊 Completed 失敗（略過）：{e}")

                # ── 收集結果 ────────────────────────────
                results.append({
                    "name": _get_excel_val(row, ["Shipping Name", "Shipping Name_1"]),
                    "order_id": order_id,
                    "tracking": tracking,
                    "country": _get_excel_val(row, ["收件人國家", "Country"]),
                    "date": time.strftime("%Y-%m-%d"),
                })
                _log(f"📌 訂單 {order_id} 完成，貨運單號：{tracking}")

            except Exception as e:
                import traceback as _tb
                _log(f"❌ 訂單 {order_id} 例外：{type(e).__name__}: {e}")
                _log(f"詳細：{_tb.format_exc()}")

    return results
