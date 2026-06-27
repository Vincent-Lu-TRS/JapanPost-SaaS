"""ReportLab PDF renderer for cross-border picking labels."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
import re
import textwrap

import qrcode
from reportlab.lib import colors
from reportlab.lib.pagesizes import portrait
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from bot.picking_labels import (
    PickingItem,
    PickingPdfPage,
    PickingOrder,
    RenderResult,
    build_picking_pdf_pages,
)

PAGE_SIZE = portrait((100 * mm, 150 * mm))
FONT_NAME = "PickingLabelCJK"
FALLBACK_FONT = "HeiseiKakuGo-W5"
FONT_BOLD = FONT_NAME
LATIN_BOLD = "Helvetica-Bold"
CJK_NORMAL_SOURCE = ""
CJK_BOLD_SOURCE = ""
CJK_FONT_INFO: dict[str, str | bool] = {}


NORMAL_FONT_CANDIDATES = [
    {"path": "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "source_type": "system-noto-cjk", "subfont_index": 0},
    {"path": "/usr/share/fonts/opentype/noto/NotoSansCJKjp-Regular.otf", "source_type": "system-noto-cjk", "subfont_index": 0},
    {"path": "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc", "source_type": "system-noto-cjk", "subfont_index": 0},
    {"path": "/usr/share/fonts/truetype/noto/NotoSansJP-Regular.ttf", "source_type": "system-noto-jp", "subfont_index": 0},
    {"path": "/usr/share/fonts/truetype/noto/NotoSansTC-Regular.ttf", "source_type": "system-noto-tc", "subfont_index": 0},
    {"path": "C:/Windows/Fonts/NotoSansTC-Regular.ttf", "source_type": "windows-noto-tc", "subfont_index": 0},
    {"path": "C:/Windows/Fonts/NotoSansCJK-Regular.ttc", "source_type": "windows-noto-cjk", "subfont_index": 0},
    {"path": "C:/Windows/Fonts/meiryo.ttc", "source_type": "windows-meiryo", "subfont_index": 0},
    {"path": "C:/Windows/Fonts/YuGothM.ttc", "source_type": "windows-yugothic", "subfont_index": 0},
    {"path": "C:/Windows/Fonts/YuGothR.ttc", "source_type": "windows-yugothic", "subfont_index": 0},
]

BOLD_FONT_CANDIDATES = [
    {"path": "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", "source_type": "system-noto-cjk", "subfont_index": 0},
    {"path": "/usr/share/fonts/opentype/noto/NotoSansCJKjp-Bold.otf", "source_type": "system-noto-cjk", "subfont_index": 0},
    {"path": "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc", "source_type": "system-noto-cjk", "subfont_index": 0},
    {"path": "/usr/share/fonts/truetype/noto/NotoSansJP-Bold.ttf", "source_type": "system-noto-jp", "subfont_index": 0},
    {"path": "/usr/share/fonts/truetype/noto/NotoSansTC-Bold.ttf", "source_type": "system-noto-tc", "subfont_index": 0},
    {"path": "C:/Windows/Fonts/NotoSansTC-Bold.ttf", "source_type": "windows-noto-tc", "subfont_index": 0},
    {"path": "C:/Windows/Fonts/NotoSansCJK-Bold.ttc", "source_type": "windows-noto-cjk", "subfont_index": 0},
    {"path": "C:/Windows/Fonts/meiryob.ttc", "source_type": "windows-meiryo", "subfont_index": 0},
    {"path": "C:/Windows/Fonts/YuGothB.ttc", "source_type": "windows-yugothic", "subfont_index": 0},
]


def select_cjk_font_candidate(candidates: list[dict], exists=None) -> dict[str, str | int | bool]:
    exists = exists or (lambda path: Path(str(path)).exists())
    for candidate in candidates:
        path = str(candidate.get("path", ""))
        if path and exists(path):
            return {
                "path": path,
                "source_type": str(candidate.get("source_type", "file")),
                "subfont_index": int(candidate.get("subfont_index", 0)),
                "embedded": True,
                "fallback_reason": "",
            }
    return {
        "path": FALLBACK_FONT,
        "source_type": "reportlab-cid",
        "subfont_index": 0,
        "embedded": False,
        "fallback_reason": "No preferred CJK TrueType/OpenType font was available; using ReportLab CID fallback.",
    }


def _register_ttfont(font_name: str, candidate: dict) -> bool:
    try:
        pdfmetrics.registerFont(
            TTFont(
                font_name,
                str(candidate["path"]),
                subfontIndex=int(candidate.get("subfont_index", 0)),
            )
        )
        return True
    except Exception:
        return False


def _register_font_from_candidates(font_name: str, candidates: list[dict]) -> dict[str, str | int | bool]:
    remaining = list(candidates)
    failures: list[str] = []
    while remaining:
        selected = select_cjk_font_candidate(remaining)
        if not selected["embedded"]:
            break
        if _register_ttfont(font_name, selected):
            return selected
        failures.append(str(selected["path"]))
        remaining = [candidate for candidate in remaining if str(candidate.get("path", "")) != selected["path"]]
    selected = select_cjk_font_candidate([])
    if failures:
        selected["fallback_reason"] = (
            "Preferred CJK font files were found but ReportLab could not register them: "
            + ", ".join(failures)
            + "; using ReportLab CID fallback."
        )
    return selected


def _register_fonts() -> None:
    global FONT_NAME, FONT_BOLD, CJK_NORMAL_SOURCE, CJK_BOLD_SOURCE, CJK_FONT_INFO
    if FONT_NAME in pdfmetrics.getRegisteredFontNames():
        return
    normal = _register_font_from_candidates(FONT_NAME, NORMAL_FONT_CANDIDATES)
    if normal["embedded"]:
        CJK_NORMAL_SOURCE = str(normal["path"])
    else:
        pdfmetrics.registerFont(UnicodeCIDFont(FALLBACK_FONT))
        FONT_NAME = FALLBACK_FONT
        CJK_NORMAL_SOURCE = FALLBACK_FONT

    bold = _register_font_from_candidates("PickingLabelCJKBold", BOLD_FONT_CANDIDATES)
    if bold["embedded"]:
        FONT_BOLD = "PickingLabelCJKBold"
        CJK_BOLD_SOURCE = str(bold["path"])
    else:
        FONT_BOLD = FONT_NAME
        CJK_BOLD_SOURCE = f"faux-bold:{CJK_NORMAL_SOURCE}"
    fallback_reason = "; ".join(
        reason for reason in [str(normal.get("fallback_reason", "")), str(bold.get("fallback_reason", ""))] if reason
    )
    CJK_FONT_INFO = {
        "normal_font": FONT_NAME,
        "bold_font": FONT_BOLD,
        "normal_source": CJK_NORMAL_SOURCE,
        "bold_source": CJK_BOLD_SOURCE,
        "normal_source_type": str(normal.get("source_type", "reportlab-cid")),
        "bold_source_type": str(bold.get("source_type", "faux-bold" if not bold.get("embedded") else "file")),
        "normal_embedded": bool(normal.get("embedded", False)),
        "bold_embedded": bool(bold.get("embedded", False)),
        "embedded": bool(normal.get("embedded", False)),
        "fallback_reason": fallback_reason,
    }


def get_registered_cjk_font_info() -> dict[str, str]:
    _register_fonts()
    return {key: str(value) if not isinstance(value, bool) else value for key, value in CJK_FONT_INFO.items()}


def _qr_image(value: str) -> ImageReader:
    qr = qrcode.QRCode(border=1, box_size=6)
    qr.add_data(value or "")
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    return ImageReader(buffer)


def _fit_lines(text: str, max_chars: int, max_lines: int) -> list[str]:
    normalized = " ".join(str(text or "").split())
    if not normalized:
        return [""]
    lines = textwrap.wrap(
        normalized,
        width=max(1, max_chars),
        break_long_words=True,
        replace_whitespace=False,
    )
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        if len(lines[-1]) > 1:
            lines[-1] = lines[-1][:-1] + "..."
    return lines or [""]


def _draw_centered(c: canvas.Canvas, text: str, x: float, y: float, width: float, font_size: float, font: str | None = None) -> None:
    font = font or FONT_NAME
    c.setFont(font, font_size)
    c.drawCentredString(x + width / 2, y, text or "")


def _draw_faux_bold_text(c: canvas.Canvas, text: str, x: float, y: float, font: str, font_size: float, centered_width: float | None = None) -> None:
    c.setFont(font, font_size)
    draw_x = x
    if centered_width is not None:
        draw_x = x + (centered_width - pdfmetrics.stringWidth(text or "", font, font_size)) / 2
    for dx, dy in [(0, 0), (0.18, 0), (0, 0.18)]:
        c.drawString(draw_x + dx, y + dy, text or "")


def _draw_regular_weight_text(c: canvas.Canvas, text: str, x: float, y: float, font: str, font_size: float, centered_width: float | None = None) -> None:
    c.setFont(font, font_size)
    draw_x = x
    if centered_width is not None:
        draw_x = x + (centered_width - pdfmetrics.stringWidth(text or "", font, font_size)) / 2
    for dx in [0, 0.08]:
        c.drawString(draw_x + dx, y, text or "")


def _is_latinish(text: str) -> bool:
    return all(ord(ch) < 128 for ch in str(text or ""))


def _font_size_to_fit(text: str, font: str, max_width: float, preferred: float, minimum: float) -> float:
    size = preferred
    while size > minimum and pdfmetrics.stringWidth(text or "", font, size) > max_width:
        size -= 0.5
    return size


def _truncate_to_width(text: str, font: str, size: float, max_width: float) -> str:
    text = str(text or "")
    if pdfmetrics.stringWidth(text, font, size) <= max_width:
        return text
    ellipsis = "…"
    while text and pdfmetrics.stringWidth(text + ellipsis, font, size) > max_width:
        text = text[:-1]
    return text + ellipsis if text else ellipsis


def _source_header_base_text(source: str) -> str:
    source = (source or "Official website - imy Shop").strip()
    lower = source.lower()
    if lower.startswith("official website") and "japan" not in lower and "international" not in lower:
        return f"{source} Japan"
    return source


def plan_source_header_text(source: str, width_points: float) -> dict:
    _register_fonts()
    text = _source_header_base_text(source)
    font = FONT_NAME if not _is_latinish(text) else "Helvetica"
    preferred = 8.8
    minimum = 6.0
    font_size = _font_size_to_fit(text, font, width_points, preferred, minimum)
    truncated = False
    if pdfmetrics.stringWidth(text, font, font_size) > width_points:
        text = _truncate_to_width(text, font, font_size, width_points)
        truncated = True
    return {
        "text": text,
        "font": font,
        "font_size": font_size,
        "truncated": truncated,
    }


def plan_logistics_header_text(logistics_method: str) -> dict:
    _register_fonts()
    text = (logistics_method or "郵便局").strip()
    normalized = re.sub(r"\s+", "", text)
    match = re.match(r"^(佐川)[\-\u2010-\u2015ー]?(SLS|MLS)$", normalized, re.IGNORECASE)
    if match:
        return {"lines": [f"{match.group(1)} -", match.group(2).upper()], "font_size": 10.5}
    return {"lines": [text], "font_size": 10.5}


_TOKEN_PATTERN = re.compile(
    r"[A-Za-z]+(?:[-&/][A-Za-z0-9]+)*|[A-Z]{1,6}(?:-[A-Z0-9]+)+|[A-Z]{1,6}\d{2,}[A-Z0-9]*|\d+(?:\.\d+)?(?:L|ml|ML|cm|mm|kg|g)?|\S"
)


def _tokenize_for_wrap(text: str) -> list[str]:
    tokens = []
    for part in str(text or "").split():
        tokens.extend(_TOKEN_PATTERN.findall(part))
        tokens.append(" ")
    if tokens and tokens[-1] == " ":
        tokens.pop()
    return tokens


def _wrap_to_width(text: str, font: str, size: float, max_width: float, max_lines: int) -> list[str]:
    normalized = " ".join(str(text or "").split())
    if not normalized:
        return [""]
    lines: list[str] = []
    current = ""
    truncated = False
    tokens = _tokenize_for_wrap(normalized)
    for token_index, token in enumerate(tokens):
        if token == " " and not current:
            continue
        candidate = current + token
        if current and pdfmetrics.stringWidth(candidate, font, size) > max_width:
            lines.append(current)
            current = "" if token == " " else token.lstrip()
            if len(lines) == max_lines:
                truncated = token_index < len(tokens)
                break
        else:
            current = candidate
    if len(lines) < max_lines and current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
    if len(lines) == max_lines:
        lines[-1] = _truncate_to_width(lines[-1], font, size, max_width)
        if truncated and not lines[-1].endswith("…"):
            lines[-1] = _truncate_to_width(lines[-1] + "…", font, size, max_width)
    lines = [line.rstrip(" -") for line in lines]
    return lines or [""]


def plan_item_text_layout(item: PickingItem, row_height_points: float, name_width_points: float) -> dict:
    """Plan row text sizes and wrapping without drawing, for layout tests."""
    _register_fonts()
    name_font_size = 10 if row_height_points >= 70 else 8.4 if row_height_points >= 45 else 7.0
    minimum = 6.5
    max_name_lines = 3 if row_height_points >= 70 else 2
    while name_font_size > minimum:
        lines = _wrap_to_width(item.name, FONT_NAME, name_font_size, name_width_points - 6, max_name_lines)
        total_text_height = len(lines) * (name_font_size + 1.5) + 7
        if total_text_height <= row_height_points - 6:
            break
        name_font_size -= 0.5
    lines = _wrap_to_width(item.name, FONT_NAME, max(name_font_size, minimum), name_width_points - 6, max_name_lines)
    return {
        "sku_text": item.sku,
        "name_lines": lines,
        "name_font_size": max(name_font_size, minimum),
        "jan_font_size": 7.2 if row_height_points >= 70 else 6.5 if row_height_points >= 45 else 6.0,
        "jan": item.jan,
        "quantity": item.quantity,
        "quantity_font_size": 25 if row_height_points >= 70 else 20 if row_height_points >= 45 else 14.0,
        "progress": item.progress,
        "progress_font_size": 8 if row_height_points >= 70 else 7.2 if row_height_points >= 45 else 6.5,
        "sku_font_size": 11 if row_height_points >= 70 else 9.5 if row_height_points >= 45 else 8.0,
    }


def can_fit_items_on_page(items: list[PickingItem]) -> bool:
    """Dry-check whether the given item chunk can render at readable minimums."""
    if len(items) > 10:
        return False
    if len(items) <= 2:
        row_count = max(2, len(items))
    elif len(items) <= 5:
        row_count = 5
    else:
        row_count = len(items)
    header_h = 21 * mm
    table_header_h = 7 * mm
    table_gap = 0.5 * mm
    vertical_space = PAGE_SIZE[1] - (2 * mm) - header_h - table_gap - table_header_h - (2 * mm)
    row_h = vertical_space / row_count
    name_w = PAGE_SIZE[0] - (2 * 2 * mm) - (16 * mm) - (16 * mm)
    for item in items:
        layout = plan_item_text_layout(item, row_h, name_w)
        if layout["name_font_size"] < 6.5:
            return False
        if layout["jan_font_size"] < 6.0:
            return False
        if layout["sku_font_size"] < 8.0 and len(items) >= 6:
            return False
        if layout["quantity_font_size"] < 14.0:
            return False
        if layout["progress_font_size"] < 6.5:
            return False
        name_block = len(layout["name_lines"]) * (layout["name_font_size"] + 1.6)
        jan_block = layout["jan_font_size"] + 3 if item.jan else 0
        if name_block + jan_block > row_h - 4:
            return False
    return True


def plan_page_grid(items: list[PickingItem]) -> dict:
    return {
        "row_count": 10,
        "filled_rows": len(items),
        "blank_rows": max(0, 10 - len(items)),
        "layout_mode": "dense",
    }


def _draw_header(c: canvas.Canvas, page: PickingPdfPage) -> float:
    width, height = PAGE_SIZE
    margin = 2 * mm
    top = height - margin
    header_h = 21 * mm
    bottom = top - header_h

    c.setStrokeColor(colors.black)
    c.setLineWidth(1.4)
    c.line(margin, top, width - margin, top)
    c.line(margin, bottom, width - margin, bottom)
    c.setLineWidth(0.45)

    date_w = 16 * mm
    qr_w = 20 * mm
    method_w = 15 * mm
    x_date = margin
    x_main = x_date + date_w
    x_qr = width - margin - method_w - qr_w
    x_method = width - margin - method_w

    for x in [x_main, x_qr, x_method]:
        c.line(x, top, x, bottom)

    _draw_centered(c, page.order.order_date, x_date, top - 12 * mm, date_w, 8)

    source_box_w = x_qr - x_main - 3 * mm
    source_box_x = x_main + 1.5 * mm
    source_box_y = top - 6.2 * mm
    source_layout = plan_source_header_text(page.order.order_source, source_box_w - 1.4 * mm)
    c.setLineWidth(0.75)
    c.rect(source_box_x, source_box_y, source_box_w, 4.5 * mm, stroke=1, fill=0)
    _draw_centered(
        c,
        source_layout["text"],
        source_box_x,
        source_box_y + 1.15 * mm,
        source_box_w,
        source_layout["font_size"],
        source_layout["font"],
    )

    label = "注文番号："
    c.setFont(FONT_NAME, 8)
    order_y = top - 15.8 * mm
    label_x = x_main + 2.5 * mm
    c.drawString(label_x, order_y, label)
    order_no_x = label_x + pdfmetrics.stringWidth(label, FONT_NAME, 8)
    order_no_max_w = x_qr - order_no_x - 1.5 * mm
    order_font = LATIN_BOLD if _is_latinish(page.order.order_no) else FONT_BOLD
    order_font_size = _font_size_to_fit(page.order.order_no, order_font, order_no_max_w, 15, 9)
    if order_font == LATIN_BOLD:
        c.setFont(order_font, order_font_size)
        c.drawString(order_no_x, order_y - 0.8 * mm, page.order.order_no)
    else:
        _draw_faux_bold_text(c, page.order.order_no, order_no_x, order_y - 0.8 * mm, order_font, order_font_size)
    if page.total_pages > 1:
        c.setFont(FONT_NAME, 6)
        c.drawRightString(x_qr - 2 * mm, bottom + 1.0 * mm, f"{page.page_index}/{page.total_pages}")

    if page.order.shipping_deadline:
        c.setFont(FONT_NAME, 6.4)
        c.drawString(label_x, bottom + 1.5 * mm, f"発送期限：{page.order.shipping_deadline}")

    c.drawImage(_qr_image(page.order.qr_content or page.order.order_no), x_qr + 1.5 * mm, bottom + 2.0 * mm, width=18 * mm, height=18 * mm)
    logistics = plan_logistics_header_text(page.order.logistics_method)
    if len(logistics["lines"]) == 1:
        _draw_regular_weight_text(c, logistics["lines"][0], x_method, top - 12 * mm, FONT_BOLD, logistics["font_size"], centered_width=method_w)
    else:
        first_y = top - 9.8 * mm
        for offset, line in enumerate(logistics["lines"][:2]):
            _draw_regular_weight_text(c, line, x_method, first_y - offset * 5.7 * mm, FONT_BOLD, logistics["font_size"], centered_width=method_w)
    return bottom


def _draw_table_header(c: canvas.Canvas, y_top: float) -> float:
    width, _height = PAGE_SIZE
    margin = 2 * mm
    header_h = 7 * mm
    y_bottom = y_top - header_h
    sku_w = 16 * mm
    qty_w = 16 * mm
    name_w = width - 2 * margin - sku_w - qty_w

    c.setLineWidth(0.9)
    c.line(margin, y_top, width - margin, y_top)
    c.line(margin, y_bottom, width - margin, y_bottom)
    c.setLineWidth(0.45)
    c.line(margin + sku_w, y_top, margin + sku_w, y_bottom)
    c.line(margin + sku_w + name_w, y_top, margin + sku_w + name_w, y_bottom)
    _draw_regular_weight_text(c, "商品 SKU", margin, y_bottom + 2 * mm, FONT_BOLD, 8.2, centered_width=sku_w)
    _draw_regular_weight_text(c, "商品名", margin + sku_w, y_bottom + 2 * mm, FONT_BOLD, 8.2, centered_width=name_w)
    _draw_regular_weight_text(c, "數量", margin + sku_w + name_w, y_bottom + 2 * mm, FONT_BOLD, 8.2, centered_width=qty_w)
    return y_bottom


def _draw_item_row(c: canvas.Canvas, item: PickingItem, y_top: float, row_h: float, layout_mode: str) -> None:
    width, _height = PAGE_SIZE
    margin = 2 * mm
    sku_w = 16 * mm
    qty_w = 16 * mm
    name_w = width - 2 * margin - sku_w - qty_w
    x_sku = margin
    x_name = x_sku + sku_w
    x_qty = x_name + name_w
    y_bottom = y_top - row_h

    c.setLineWidth(0.32)
    c.line(margin, y_bottom, width - margin, y_bottom)
    c.line(x_name, y_top, x_name, y_bottom)
    c.line(x_qty, y_top, x_qty, y_bottom)

    if not any([item.sku, item.name, item.jan, item.quantity, item.progress]):
        return

    sku_size = 11 if layout_mode == "large" else 9.5 if layout_mode == "medium" else 8.4
    sku_size = _font_size_to_fit(item.sku, LATIN_BOLD if _is_latinish(item.sku) else FONT_BOLD, sku_w - 2 * mm, sku_size, 6.2)
    sku_font = LATIN_BOLD if _is_latinish(item.sku) else FONT_BOLD
    sku_text = _truncate_to_width(item.sku, sku_font, sku_size, sku_w - 2 * mm)
    if sku_font == LATIN_BOLD:
        _draw_centered(c, sku_text, x_sku, y_bottom + row_h / 2 - 1.5 * mm, sku_w, sku_size, sku_font)
    else:
        _draw_faux_bold_text(c, sku_text, x_sku, y_bottom + row_h / 2 - 1.5 * mm, sku_font, sku_size, centered_width=sku_w)

    text_layout = plan_item_text_layout(item, row_h, name_w)
    name_size = max(text_layout["name_font_size"], 7.0 if layout_mode == "dense" else text_layout["name_font_size"])
    name_lines = text_layout["name_lines"]
    jan_size = text_layout["jan_font_size"]
    block_h = len(name_lines) * (name_size + 1.6) + (jan_size + 2 if item.jan else 0)
    line_y = y_bottom + row_h / 2 + block_h / 2 - name_size
    for line in name_lines:
        _draw_regular_weight_text(c, line, x_name, line_y, FONT_NAME, name_size, centered_width=name_w)
        line_y -= (name_size + 1.6)
    if item.jan:
        c.setFont(LATIN_BOLD if _is_latinish(item.jan) else FONT_BOLD, jan_size)
        c.drawCentredString(x_name + name_w / 2, line_y - 1.0, item.jan)

    qty_size = text_layout["quantity_font_size"]
    qty_size = _font_size_to_fit(item.quantity, LATIN_BOLD if _is_latinish(item.quantity) else FONT_BOLD, qty_w - 2 * mm, qty_size, 14)
    progress_lines = _fit_lines(item.progress, max_chars=5, max_lines=2) if item.progress else []
    progress_size = text_layout["progress_font_size"]
    progress_line_gap = progress_size + 1.2
    progress_block_h = len(progress_lines) * progress_line_gap
    group_gap = 1.4 * mm if progress_lines else 0
    group_h = qty_size + group_gap + progress_block_h
    group_top = y_bottom + row_h / 2 + group_h / 2
    qty_y = group_top - qty_size
    _draw_centered(c, item.quantity, x_qty, qty_y, qty_w, qty_size, LATIN_BOLD if _is_latinish(item.quantity) else FONT_BOLD)
    if item.progress:
        progress_y = qty_y - group_gap - progress_size
        for line in progress_lines:
            _draw_regular_weight_text(c, line, x_qty, progress_y, FONT_BOLD, progress_size, centered_width=qty_w)
            progress_y -= progress_line_gap


def _layout_row_count(item_count: int) -> tuple[int, str]:
    return 10, "dense"


def _draw_page(c: canvas.Canvas, page: PickingPdfPage) -> None:
    width, height = PAGE_SIZE
    margin = 2 * mm
    header_bottom = _draw_header(c, page)
    table_top = header_bottom - 0.5 * mm
    table_header_bottom = _draw_table_header(c, table_top)
    table_bottom = margin
    row_count, layout_mode = _layout_row_count(len(page.items))
    row_h = (table_header_bottom - table_bottom) / row_count

    y = table_header_bottom
    for item in page.items:
        _draw_item_row(c, item, y, row_h, layout_mode)
        y -= row_h
    while y > table_bottom + 0.1:
        _draw_item_row(c, PickingItem("", "", "", "", ""), y, row_h, layout_mode)
        y -= row_h

    c.setLineWidth(1.2)
    c.line(margin, table_bottom, width - margin, table_bottom)
    c.showPage()


def render_picking_labels_pdf(orders: list[PickingOrder], output_path: str) -> RenderResult:
    _register_fonts()
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pages = build_picking_pdf_pages(orders)
    pdf = canvas.Canvas(str(path), pagesize=PAGE_SIZE)
    pdf.setTitle("Cross-border picking labels")
    for page in pages:
        _draw_page(pdf, page)
    pdf.save()
    return RenderResult(
        local_path=str(path),
        total_orders=len(orders),
        total_pages=len(pages),
        warnings=[],
    )
