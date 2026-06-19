import pandas as pd


SHIPPING_COL = "郵局運送方式(複數商品請自行確認是否走小包)"
SHIPPING_OPTIONS = ["EMS", "國際小包", "ePacket"]
MAX_EDITOR_ITEMS = 5

PENDING_SUMMARY_COLUMNS = [
    "Order No.",
    "Name",
    "Country",
    "TransType",
    "TotalValue(USD)",
    "TotalValue(JPY)",
]

EDITABLE_PENDING_COLUMNS = [
    "注文番号(貼上原始資料)",
    "Shipping Name",
    "收件人國家",
    SHIPPING_COL,
    "郵局申告金額(USD)",
    "內容物1",
    "申告金額1",
    "數量1",
    "內容物2",
    "申告金額2",
    "數量2",
    "內容物3",
    "申告金額3",
    "數量3",
    "內容物4",
    "申告金額4",
    "數量4",
    "內容物5",
    "申告金額5",
    "數量5",
    "訂單合計申告金額(JPY)",
    "HSCode",
]


def _str_value(value) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip()


def _money_to_float(value) -> float:
    text = _str_value(value).replace(",", "")
    if not text:
        return 0.0
    try:
        return float(text)
    except Exception:
        return 0.0


def _quantity_to_float(value) -> float:
    qty = _money_to_float(value)
    return qty if qty > 0 else 0.0


def _format_usd(value: float) -> str:
    return f"{value:.2f}"


def _format_jpy(value: float) -> str:
    return str(int(round(value)))


def _content_col(index: int) -> str:
    return f"內容物{index}"


def _value_col(index: int) -> str:
    return f"申告金額{index}"


def _quantity_col(index: int) -> str:
    return f"數量{index}"


def calculate_total_value_usd(row: pd.Series, max_items: int = MAX_EDITOR_ITEMS) -> float:
    total = 0.0
    for index in range(1, max_items + 1):
        content = _str_value(row.get(_content_col(index), ""))
        value = _money_to_float(row.get(_value_col(index), ""))
        quantity = _quantity_to_float(row.get(_quantity_col(index), "1"))
        if content or value:
            total += value * (quantity or 1)
    return total


def has_zero_value_items(row: pd.Series, max_items: int = MAX_EDITOR_ITEMS) -> list[int]:
    zero_items: list[int] = []
    for index in range(1, max_items + 1):
        content = _str_value(row.get(_content_col(index), ""))
        if content and _money_to_float(row.get(_value_col(index), "")) <= 0:
            zero_items.append(index)
    return zero_items


def build_pending_summary_frame(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, str]] = []
    for _, row in df.iterrows():
        rows.append(
            {
                "Order No.": _str_value(row.get("注文番号(貼上原始資料)", "")),
                "Name": _str_value(row.get("Shipping Name", row.get("Shipping Name_1", ""))),
                "Country": _str_value(row.get("收件人國家", row.get("Country", ""))),
                "TransType": _str_value(row.get(SHIPPING_COL, "")),
                "TotalValue(USD)": _format_usd(calculate_total_value_usd(row)),
                "TotalValue(JPY)": _str_value(row.get("訂單合計申告金額(JPY)", "")),
            }
        )
    return pd.DataFrame(rows, columns=PENDING_SUMMARY_COLUMNS)


def build_pending_item_frame(
    row: pd.Series,
    hs_codes: dict[str, str] | None = None,
    max_items: int = MAX_EDITOR_ITEMS,
) -> pd.DataFrame:
    hs_codes = hs_codes or {}
    rows: list[dict[str, str | int]] = []
    for index in range(1, max_items + 1):
        content = _str_value(row.get(_content_col(index), ""))
        value = _str_value(row.get(_value_col(index), ""))
        quantity = _str_value(row.get(_quantity_col(index), ""))
        if not any([content, value, quantity, hs_codes.get(str(index), "")]):
            continue
        rows.append(
            {
                "Item": index,
                "Content": content,
                "HSCode": hs_codes.get(str(index), ""),
                "Value": value,
                "Quantity": quantity or "1",
            }
        )
    if not rows:
        rows.append({"Item": 1, "Content": "", "HSCode": "", "Value": "", "Quantity": "1"})
    return pd.DataFrame(rows, columns=["Item", "Content", "HSCode", "Value", "Quantity"])


def _summary_value(summary: pd.DataFrame, position: int, column: str) -> str:
    if column not in summary.columns or position >= len(summary):
        return ""
    return _str_value(summary.iloc[position].get(column, ""))


def apply_pending_order_editor_values(
    original: pd.DataFrame,
    edited_summary: pd.DataFrame,
    edited_items_by_position: dict[int, pd.DataFrame],
    usd_jpy_rate: float | None = None,
) -> pd.DataFrame:
    applied = original.copy()
    for position, source_index in enumerate(applied.index[:len(edited_summary)]):
        trans_type = _summary_value(edited_summary, position, "TransType")
        if trans_type:
            applied.at[source_index, SHIPPING_COL] = trans_type

        item_frame = edited_items_by_position.get(position)
        if item_frame is None:
            continue

        value_or_quantity_changed = False
        for _, item in item_frame.iterrows():
            try:
                item_index = int(float(item.get("Item", 0)))
            except Exception:
                continue
            if item_index < 1 or item_index > MAX_EDITOR_ITEMS:
                continue

            mappings = [
                (_content_col(item_index), "Content"),
                (_value_col(item_index), "Value"),
                (_quantity_col(item_index), "Quantity"),
            ]
            for target_col, edited_col in mappings:
                new_value = _str_value(item.get(edited_col, ""))
                if target_col not in applied.columns:
                    applied[target_col] = ""
                if _str_value(applied.at[source_index, target_col]) != new_value:
                    applied.at[source_index, target_col] = new_value
                    if edited_col in {"Value", "Quantity"}:
                        value_or_quantity_changed = True

        total_usd = calculate_total_value_usd(applied.loc[source_index])
        applied.at[source_index, "郵局申告金額(USD)"] = _format_usd(total_usd)
        if value_or_quantity_changed and usd_jpy_rate:
            applied.at[source_index, "訂單合計申告金額(JPY)"] = _format_jpy(total_usd * usd_jpy_rate)

    return applied


def build_pending_editor_frame(df: pd.DataFrame) -> pd.DataFrame:
    frame = pd.DataFrame()
    for column in EDITABLE_PENDING_COLUMNS:
        if column == "HSCode":
            frame[column] = ""
        elif column in df.columns:
            frame[column] = df[column].fillna("").astype(str)
        else:
            frame[column] = ""
    return frame


def coerce_pending_editor_values(df: pd.DataFrame) -> pd.DataFrame:
    coerced = df.copy()
    if SHIPPING_COL in coerced.columns:
        coerced[SHIPPING_COL] = coerced[SHIPPING_COL].fillna("").astype(str).str.strip()
    for column in [
        "郵局申告金額(USD)",
        "申告金額1",
        "申告金額2",
        "申告金額3",
        "申告金額4",
        "申告金額5",
        "訂單合計申告金額(JPY)",
    ]:
        if column in coerced.columns:
            coerced[column] = coerced[column].fillna("").astype(str).str.strip()
    return coerced


def apply_pending_editor_values(original: pd.DataFrame, edited: pd.DataFrame) -> pd.DataFrame:
    applied = original.copy()
    coerced = coerce_pending_editor_values(edited)
    target_index = applied.index[:len(coerced)]
    for column in EDITABLE_PENDING_COLUMNS:
        if column == "HSCode":
            continue
        if column in coerced.columns:
            applied.loc[target_index, column] = coerced[column].values
    return applied
