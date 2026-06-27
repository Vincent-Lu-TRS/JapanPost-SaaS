"""Cross-border picking label parsing and pagination."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import ceil
from pathlib import Path
import re
from typing import Any

PICKING_SOURCE_SPREADSHEET_ID = "1KiyftkJveKrhd54a7yWvl-a1UuLJbzo7c5zJVquVa0s"
PICKING_SOURCE_SHEET_NAME = "南巽出貨Label"
PICKING_OUTPUT_DRIVE_FOLDER_ID = "1_JYIwmtpKQ7FjWY2zplofLGe0GHaEMvw"
SHIPPING_STATUS_SPREADSHEET_ID = "1QJFFW7aWGpYX3W5nPW_HgUnVWk9AtggFvYow14BRW8U"
SHIPPING_STATUS_SHEET_NAME = "南巽出貨狀態一覽"
ITEMS_PER_PAGE = 10
STATUS_COL = 10
DONE_COL = 11
ORDER_DATE_COL = 12
ORDER_SOURCE_COL = 13
ORDER_NO_COL = 14
LOGISTICS_COL = 15
ITEM_START_COL = 16
ITEM_GROUP_WIDTH = 5
ALLOWED_LOGISTICS_KEYWORDS = ["郵便局", "佐川", "MLS", "SLS"]


@dataclass
class PickingItem:
    sku: str
    name: str
    jan: str
    quantity: str
    progress: str


@dataclass
class PickingOrder:
    source_row_number: int
    order_date: str
    order_source: str
    order_no: str
    logistics_method: str
    items: list[PickingItem]
    qr_content: str = ""
    shipping_deadline: str = ""


@dataclass
class PickingPdfPage:
    order: PickingOrder
    page_index: int
    total_pages: int
    items: list[PickingItem]

    @property
    def order_no(self) -> str:
        return self.order.order_no


@dataclass
class RenderResult:
    local_path: str
    total_orders: int
    total_pages: int
    warnings: list[str]


@dataclass
class PickingTransactionResult:
    success: bool
    local_path: str
    filename: str
    marked_rows: list[int]
    drive_file: dict | None = None
    error: str = ""


@dataclass(frozen=True)
class PickingSourceSchema:
    status_idx: int
    done_idx: int
    order_date_idx: int
    order_source_idx: int
    order_no_idx: int
    logistics_idx: int
    item_groups: dict[int, dict[str, int]]
    anchored: bool


def _cell(row: list[Any], index: int | None) -> str:
    if index is None or index < 0 or index >= len(row):
        return ""
    value = row[index]
    if value is None:
        return ""
    return str(value).strip()


def _normalize_cell_value(value: Any) -> str:
    text = "" if value is None else str(value)
    for token in ["　", " ", "\n", "\r", "\t"]:
        text = text.replace(token, "")
    return text.strip()


def normalize_status_value(value: Any) -> str:
    return _normalize_cell_value(value)


def normalize_logistics_method(value: Any) -> str:
    return _normalize_cell_value(value)


def is_allowed_picking_logistics(value: Any) -> bool:
    normalized = normalize_logistics_method(value)
    if not normalized:
        return False
    upper_text = normalized.upper()
    return any(
        keyword in normalized or keyword in upper_text
        for keyword in ALLOWED_LOGISTICS_KEYWORDS
    )


def normalize_done_state(value: Any) -> str:
    if value is False:
        return "NOT_DONE"
    if value is True:
        return "DONE"
    text = _normalize_cell_value(value)
    upper_text = text.upper()
    if text in {"", "未製單"} or upper_text in {"FALSE", "0", "NO", "N"}:
        return "NOT_DONE"
    if text == "已製單" or upper_text in {"TRUE", "1", "YES", "Y"}:
        return "DONE"
    return "UNKNOWN"


def _is_unchecked(value: Any) -> bool:
    return normalize_done_state(value) == "NOT_DONE"


def _normalize_header_name(value: Any) -> str:
    text = "" if value is None else str(value)
    replacements = {
        "　": "",
        " ": "",
        "\n": "",
        "\r": "",
        "\t": "",
        "－": "-",
        "ー": "-",
        "―": "-",
        "‐": "-",
        "–": "-",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text.strip()


def _header_map(header: list[Any]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for idx, value in enumerate(header):
        name = _normalize_header_name(value)
        if name and name not in mapping:
            mapping[name] = idx
    return mapping


def _header_positions(header: list[Any]) -> dict[str, list[int]]:
    positions: dict[str, list[int]] = {}
    for idx, value in enumerate(header):
        name = _normalize_header_name(value)
        if name:
            positions.setdefault(name, []).append(idx)
    return positions


def _column_letter(index: int | None) -> str:
    if index is None or index < 0:
        return ""
    result = ""
    number = index + 1
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _find_item_groups(header: list[Any]) -> dict[int, dict[str, int]]:
    patterns = {
        "sku": re.compile(r"^商品SKU(\d+)$"),
        "name": re.compile(r"^商品名(\d+)$"),
        "jan": re.compile(r"^JAN-(\d+)$"),
        "quantity": re.compile(r"^數量(\d+)$"),
        "progress": re.compile(r"^入荷進捗(\d+)$"),
    }
    groups: dict[int, dict[str, int]] = {}
    for idx, raw_name in enumerate(header):
        name = _normalize_header_name(raw_name)
        for field, pattern in patterns.items():
            match = pattern.match(name)
            if match:
                item_index = int(match.group(1))
                groups.setdefault(item_index, {})[field] = idx
                break
    return groups


def _has_anchored_picking_schema(header: list[Any]) -> bool:
    expected = {
        STATUS_COL: "訂單狀態",
        DONE_COL: "製單後勾選",
        ORDER_DATE_COL: "注文日",
        ORDER_SOURCE_COL: "訂單來源",
        ORDER_NO_COL: "注文番号",
        LOGISTICS_COL: "國際物流方式",
    }
    for idx, name in expected.items():
        if idx >= len(header) or _normalize_header_name(header[idx]) != name:
            return False
    return len(header) > ITEM_START_COL and _normalize_header_name(header[ITEM_START_COL]) == "商品SKU1"


def _anchored_item_groups(header: list[Any]) -> dict[int, dict[str, int]]:
    fields = [
        ("sku", "商品SKU{idx}"),
        ("name", "商品名{idx}"),
        ("jan", "JAN-{idx}"),
        ("quantity", "數量{idx}"),
        ("progress", "入荷進捗{idx}"),
    ]
    groups: dict[int, dict[str, int]] = {}
    for item_index in range(1, ITEMS_PER_PAGE + 1):
        start = ITEM_START_COL + (item_index - 1) * ITEM_GROUP_WIDTH
        group: dict[str, int] = {}
        for offset, (field, template) in enumerate(fields):
            idx = start + offset
            if idx < len(header) and _normalize_header_name(header[idx]) == template.format(idx=item_index):
                group[field] = idx
        if group:
            groups[item_index] = group
    return groups


def _build_picking_source_schema(header: list[Any]) -> PickingSourceSchema:
    headers = _header_map(header)
    if _has_anchored_picking_schema(header):
        return PickingSourceSchema(
            status_idx=STATUS_COL,
            done_idx=DONE_COL,
            order_date_idx=ORDER_DATE_COL,
            order_source_idx=ORDER_SOURCE_COL,
            order_no_idx=ORDER_NO_COL,
            logistics_idx=LOGISTICS_COL,
            item_groups=_anchored_item_groups(header),
            anchored=True,
        )
    return PickingSourceSchema(
        status_idx=headers.get("訂單狀態", STATUS_COL),
        done_idx=headers.get("製單後勾選", DONE_COL),
        order_date_idx=headers.get("注文日", ORDER_DATE_COL),
        order_source_idx=headers.get("訂單來源", ORDER_SOURCE_COL),
        order_no_idx=headers.get("注文番号", ORDER_NO_COL),
        logistics_idx=headers.get("國際物流方式", LOGISTICS_COL),
        item_groups=_find_item_groups(header),
        anchored=False,
    )


def _duplicate_header_diagnostics(header: list[Any], schema: PickingSourceSchema) -> list[dict[str, Any]]:
    positions = _header_positions(header)
    used_by_header = {
        "注文日": schema.order_date_idx,
        "訂單來源": schema.order_source_idx,
        "注文番号": schema.order_no_idx,
        "國際物流方式": schema.logistics_idx,
    }
    diagnostics: list[dict[str, Any]] = []
    for name, used_idx in used_by_header.items():
        all_positions = positions.get(name, [])
        ignored = [idx for idx in all_positions if idx != used_idx]
        if ignored or len(all_positions) > 1:
            diagnostics.append(
                {
                    "header": name,
                    "ignored_columns": [_column_letter(idx) for idx in ignored],
                    "used_column": _column_letter(used_idx),
                }
            )
    return diagnostics


def _items_from_row(row: list[Any], item_groups: dict[int, dict[str, int]]) -> list[PickingItem]:
    items: list[PickingItem] = []
    for item_index in sorted(item_groups):
        group = item_groups[item_index]
        item = PickingItem(
            sku=_cell(row, group.get("sku")),
            name=_cell(row, group.get("name")),
            jan=_cell(row, group.get("jan")),
            quantity=_cell(row, group.get("quantity")),
            progress=_cell(row, group.get("progress")),
        )
        if any([item.sku, item.name, item.jan, item.quantity, item.progress]):
            items.append(item)
    return items


def parse_picking_label_candidates(
    values: list[list[Any]],
    shipping_deadlines: dict[str, str] | None = None,
) -> tuple[list[PickingOrder], list[str]]:
    """Parse shippable, not-yet-generated picking orders from raw sheet values."""
    if len(values) < 2:
        return [], []

    header = values[0]
    schema = _build_picking_source_schema(header)
    headers = _header_map(header)
    qr_idx = (
        headers.get("QR內容")
        or headers.get("QR Code")
        or headers.get("QR")
        or headers.get("QRCode")
    )
    item_groups = schema.item_groups

    orders: list[PickingOrder] = []
    for source_row_number, row in enumerate(values[1:], start=2):
        if normalize_status_value(_cell(row, schema.status_idx)) != "可出貨":
            continue
        if normalize_done_state(row[schema.done_idx] if schema.done_idx < len(row) else "") != "NOT_DONE":
            continue
        if not is_allowed_picking_logistics(_cell(row, schema.logistics_idx)):
            continue
        order_no = _cell(row, schema.order_no_idx)
        if not order_no:
            continue

        items = _items_from_row(row, item_groups)
        if not items:
            continue

        qr_content = _cell(row, qr_idx) or order_no
        orders.append(
            PickingOrder(
                source_row_number=source_row_number,
                order_date=_cell(row, schema.order_date_idx),
                order_source=_cell(row, schema.order_source_idx),
                order_no=order_no,
                logistics_method=_cell(row, schema.logistics_idx),
                items=items,
                qr_content=qr_content,
                shipping_deadline=(shipping_deadlines or {}).get(order_no, ""),
            )
        )

    warnings: list[str] = []
    max_item_index = max(item_groups.keys(), default=0)
    if 0 < max_item_index < ITEMS_PER_PAGE:
        warnings.append(
            f"目前來源表只提供至商品SKU{max_item_index}，因此 App 目前只能讀到 {max_item_index} 個商品。"
            "若要完整支援 10 個以上商品，請先擴充來源表欄位或改接 normalized 訂單商品資料來源。"
        )
    return orders, warnings


def build_picking_pdf_pages(orders: list[PickingOrder]) -> list[PickingPdfPage]:
    pages: list[PickingPdfPage] = []
    for order in orders:
        chunks = [
            order.items[index:index + ITEMS_PER_PAGE]
            for index in range(0, max(len(order.items), 1), ITEMS_PER_PAGE)
        ]
        total_pages = len(chunks)
        for page_index, chunk in enumerate(chunks, start=1):
            pages.append(PickingPdfPage(order, page_index, total_pages, chunk))
    return pages


def estimate_total_pages(orders: list[PickingOrder]) -> int:
    return sum(max(1, ceil(len(order.items) / ITEMS_PER_PAGE)) for order in orders)


def summarize_items(order: PickingOrder, limit: int = 4) -> str:
    parts = []
    for item in order.items[:limit]:
        label = item.sku or item.name or "商品"
        quantity = item.quantity or "1"
        parts.append(f"{label} x {quantity}")
    if len(order.items) > limit:
        parts.append("...")
    return " / ".join(parts)


def filter_orders_by_rows(orders: list[PickingOrder], row_numbers: set[int]) -> list[PickingOrder]:
    return [order for order in orders if order.source_row_number in row_numbers]


def resolve_picking_done_row_numbers(values: list[list[Any]], orders: list[PickingOrder]) -> list[int]:
    """Resolve source rows for writeback, revalidating row numbers by order number."""
    if not orders:
        return []
    if len(values) < 2:
        raise ValueError("來源表沒有可供回寫的資料列。")

    schema = _build_picking_source_schema(values[0])
    order_no_idx = schema.order_no_idx
    row_by_order_no: dict[str, int] = {}
    for row_number, row in enumerate(values[1:], start=2):
        order_no = _cell(row, order_no_idx)
        if order_no and order_no not in row_by_order_no:
            row_by_order_no[order_no] = row_number

    resolved: list[int] = []
    missing: list[str] = []
    for order in orders:
        fast_path_order_no = ""
        fast_path_index = order.source_row_number - 1
        if 0 < fast_path_index < len(values):
            fast_path_order_no = _cell(values[fast_path_index], order_no_idx)
        if fast_path_order_no == order.order_no:
            resolved.append(order.source_row_number)
            continue
        fallback_row = row_by_order_no.get(order.order_no)
        if fallback_row:
            resolved.append(fallback_row)
        else:
            missing.append(order.order_no)

    if missing:
        raise ValueError(f"找不到以下注文番号的來源列，已中止 L 欄回寫：{', '.join(missing)}")
    return sorted(set(resolved))


def build_picking_source_diagnostics(
    values: list[list[Any]],
    orders: list[PickingOrder],
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    header = values[0] if values else []
    schema = _build_picking_source_schema(header)
    item_groups = schema.item_groups
    status_idx = schema.status_idx
    done_idx = schema.done_idx
    order_no_idx = schema.order_no_idx
    max_item_group = max(item_groups.keys(), default=0)
    missing_headers: list[str] = []
    fields = [
        ("sku", "商品SKU{idx}"),
        ("name", "商品名{idx}"),
        ("jan", "JAN-{idx}"),
        ("quantity", "數量{idx}"),
        ("progress", "入荷進捗{idx}"),
    ]
    for idx in range(1, ITEMS_PER_PAGE + 1):
        group = item_groups.get(idx, {})
        for field, template in fields:
            if field not in group:
                missing_headers.append(template.format(idx=idx))

    data_row_count = max(len(values) - 1, 0)
    excluded_because_status = 0
    excluded_because_done = 0
    excluded_because_order_no_missing = 0
    excluded_because_item_data_missing = 0
    excluded_because_logistics_not_allowed = 0
    parser_unknown_exclusion_count = 0
    near_candidate_exclusions: list[dict[str, Any]] = []
    logistics_filter_exclusions: list[dict[str, Any]] = []
    done_samples: list[str] = []
    included_candidate_samples: list[dict[str, Any]] = []

    for source_row_number, row in enumerate(values[1:], start=2):
        raw_status = _cell(row, status_idx)
        normalized_status = normalize_status_value(raw_status)
        raw_done_value = row[done_idx] if done_idx < len(row) else ""
        raw_done = _cell(row, done_idx)
        done_state = normalize_done_state(raw_done_value)
        order_no = _cell(row, order_no_idx)
        raw_logistics = _cell(row, schema.logistics_idx)
        normalized_logistics = normalize_logistics_method(raw_logistics)
        item_count = len(_items_from_row(row, item_groups))
        if raw_done not in done_samples:
            done_samples.append(raw_done)

        reason = ""
        if normalized_status != "可出貨":
            excluded_because_status += 1
            reason = "K status != 可出貨"
        elif done_state == "DONE":
            excluded_because_done += 1
            reason = "L indicates done"
        elif done_state != "NOT_DONE":
            parser_unknown_exclusion_count += 1
            reason = "L state unknown"
        elif not order_no:
            excluded_because_order_no_missing += 1
            reason = "注文番号 missing"
        elif item_count == 0:
            excluded_because_item_data_missing += 1
            reason = "item data missing"
        elif not is_allowed_picking_logistics(raw_logistics):
            excluded_because_logistics_not_allowed += 1
            reason = "P logistics not allowed"
            if len(logistics_filter_exclusions) < 20:
                logistics_filter_exclusions.append(
                    {
                        "source_row_number": source_row_number,
                        "o_order_no": order_no,
                        "m_order_date": _cell(row, schema.order_date_idx),
                        "raw_p_logistics_method": raw_logistics,
                        "normalized_p_logistics_method": normalized_logistics,
                        "raw_k_value": raw_status,
                        "normalized_k_value": normalized_status,
                        "raw_l_value": raw_done,
                        "normalized_l_state": done_state,
                    }
                )
        elif len(included_candidate_samples) < 20:
            included_candidate_samples.append(
                {
                    "source_row_number": source_row_number,
                    "m_order_date": _cell(row, schema.order_date_idx),
                    "n_order_source": _cell(row, schema.order_source_idx),
                    "o_order_no": order_no,
                    "raw_p_logistics_method": raw_logistics,
                    "normalized_p_logistics_method": normalized_logistics,
                    "raw_k_value": raw_status,
                    "normalized_k_value": normalized_status,
                    "raw_l_value": raw_done,
                    "normalized_l_state": done_state,
                    "item_count": item_count,
                }
            )

        if reason and len(near_candidate_exclusions) < 5:
            near_candidate_exclusions.append(
                {
                    "source_row_number": source_row_number,
                    "raw_k_value": raw_status,
                    "normalized_k_value": normalized_status,
                    "raw_l_value": raw_done,
                    "normalized_l_state": done_state,
                    "order_no": order_no,
                    "detected_item_count": item_count,
                    "exclusion_reason": reason,
                }
            )

    return {
        "source_sheet": PICKING_SOURCE_SHEET_NAME,
        "filter_condition": "K 訂單狀態 = 可出貨，且 L 製單後勾選 != TRUE",
        "status_column": _column_letter(schema.status_idx),
        "done_column": _column_letter(schema.done_idx),
        "order_date_column": _column_letter(schema.order_date_idx),
        "order_source_column": _column_letter(schema.order_source_idx),
        "order_no_column": _column_letter(schema.order_no_idx),
        "logistics_column": _column_letter(schema.logistics_idx),
        "item_start_column": _column_letter(ITEM_START_COL) if schema.anchored else "",
        "anchored_schema": schema.anchored,
        "duplicate_header_diagnostics": _duplicate_header_diagnostics(header, schema),
        "total_source_rows": data_row_count,
        "detected_item_groups": sorted(item_groups.keys()),
        "max_item_group": max_item_group,
        "missing_item_headers": missing_headers if max_item_group < ITEMS_PER_PAGE else [],
        "candidate_order_count": len(orders),
        "excluded_count": max(data_row_count - len(orders), 0),
        "excluded_because_status": excluded_because_status,
        "excluded_because_done": excluded_because_done,
        "excluded_because_order_no_missing": excluded_because_order_no_missing,
        "excluded_because_item_data_missing": excluded_because_item_data_missing,
        "excluded_because_logistics_not_allowed": excluded_because_logistics_not_allowed,
        "allowed_logistics_keywords": ALLOWED_LOGISTICS_KEYWORDS,
        "parser_unknown_exclusion_count": parser_unknown_exclusion_count,
        "done_raw_values_sample": done_samples[:12],
        "near_candidate_exclusions": near_candidate_exclusions,
        "logistics_filter_exclusions": logistics_filter_exclusions,
        "included_candidate_samples": included_candidate_samples,
        "actual_detected_headers": [_normalize_header_name(value) for value in header if _normalize_header_name(value)],
        "warnings": warnings or [],
        "qr_contents": [order.qr_content or order.order_no for order in orders],
    }


def build_picking_label_summary(orders: list[PickingOrder]) -> dict[str, Any]:
    return {
        "source_sheet": PICKING_SOURCE_SHEET_NAME,
        "filter_condition": "K 訂單狀態 = 可出貨，且 L 製單後勾選 != TRUE",
        "order_count": len(orders),
        "item_count": sum(len(order.items) for order in orders),
        "estimated_pdf_pages": estimate_total_pages(orders),
        "qr_contents": [order.qr_content or order.order_no for order in orders],
    }


def format_shipping_deadline(value: Any) -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        return ""
    text = text.replace("-", "/")
    match = re.search(r"(\d{4}/\d{1,2}/\d{1,2})(?:\s+(\d{1,2}):(\d{1,2}))?", text)
    if not match:
        return text.split()[0]
    year, month, day = match.group(1).split("/")
    hour = int(match.group(2) or 0)
    minute = int(match.group(3) or 0)
    return f"{int(year):04d}/{int(month):02d}/{int(day):02d} {hour:02d}:{minute:02d}"


def build_shipping_deadline_lookup(values: list[list[Any]]) -> dict[str, str]:
    """Build order_no -> earliest non-empty shipping deadline from status sheet values."""
    lookup: dict[str, str] = {}
    if len(values) < 2:
        return lookup
    header = _header_map(values[0])
    order_idx = header.get("訂單編號", 2)
    deadline_idx = header.get("発送期限", 10)
    candidates: dict[str, list[str]] = {}
    for row in values[1:]:
        order_no = _cell(row, order_idx)
        deadline = format_shipping_deadline(_cell(row, deadline_idx))
        if order_no and deadline:
            candidates.setdefault(order_no, []).append(deadline)
    for order_no, deadlines in candidates.items():
        lookup[order_no] = sorted(deadlines)[0]
    return lookup


def generate_picking_labels_transaction(
    orders: list[PickingOrder],
    output_dir: str,
    list_files,
    upload_file,
    mark_done,
    now: str | None = None,
) -> PickingTransactionResult:
    """Render, upload, then mark source rows after upload succeeds."""
    from bot.drive import choose_safe_picking_filename
    from bot.picking_pdf import render_picking_labels_pdf

    dt = datetime.now() if now is None else datetime.strptime(now, "%Y-%m-%d %H:%M:%S")
    prefix = dt.strftime("%y%m%d-")
    today = dt.strftime("%Y-%m-%d")
    timestamp = dt.strftime("%H%M%S")
    initial_files = list_files(prefix)
    candidate = choose_safe_picking_filename(
        initial_files=initial_files,
        rechecked_files=list_files(prefix),
        today=today,
        timestamp=timestamp,
    )
    output_path = Path(output_dir) / candidate
    render_picking_labels_pdf(orders, str(output_path))

    try:
        drive_file = upload_file(str(output_path))
    except Exception as exc:
        return PickingTransactionResult(
            success=False,
            local_path=str(output_path),
            filename=candidate,
            marked_rows=[],
            error=str(exc),
        )

    rows = [order.source_row_number for order in orders]
    try:
        marked_rows = mark_done(rows)
    except Exception as exc:
        return PickingTransactionResult(
            success=False,
            local_path=str(output_path),
            filename=candidate,
            marked_rows=[],
            drive_file=drive_file,
            error=str(exc),
        )
    return PickingTransactionResult(
        success=True,
        local_path=str(output_path),
        filename=candidate,
        marked_rows=marked_rows if isinstance(marked_rows, list) else rows,
        drive_file=drive_file,
    )
