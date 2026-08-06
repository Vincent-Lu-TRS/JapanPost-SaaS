"""Pure helpers for the isolated postal pending-order UI v2 preview."""

from __future__ import annotations

from datetime import datetime

import pandas as pd


V2_FIELD_CONTRACT = {
    "editable": [
        "Name",
        "TransType",
        "追加",
        "PRC ID/PCCC",
        "Description",
        "HSCode",
        "Value",
        "Quantity",
    ],
    "display_only": ["製單", "Order No.", "Country", "No."],
    "system_calculated": ["TotalValue(USD)", "TotalValue(JPY)"],
}


def apply_batch_selection(
    selected_by_order: dict[str, bool],
    current_order_ids: list[str],
    action: str,
) -> dict[str, bool]:
    """Return a copied selection map with an action applied to current orders."""
    target_value = {"select_all": True, "clear_all": False}[action]
    updated = dict(selected_by_order)
    for order_id in current_order_ids:
        updated[str(order_id)] = target_value
    return updated


def format_secondary_rate_badge(rate: float | None, rate_date: str) -> str:
    """Format the compact, single-line v2 exchange-rate badge."""
    rate_text = f"{rate:.2f}" if rate else "N/A"
    date_text = ""
    if rate_date:
        try:
            date_text = datetime.strptime(rate_date, "%Y-%m-%d").strftime("%y/%m/%d")
        except ValueError:
            date_text = str(rate_date)
    return f"USD/JPY {rate_text}" + (f" · {date_text}" if date_text else "")


def v2_field_contract() -> dict[str, list[str]]:
    """Return a defensive copy of the v2 display/editability contract."""
    return {key: list(value) for key, value in V2_FIELD_CONTRACT.items()}


def build_v2_item_display_frame(item_frame: pd.DataFrame) -> pd.DataFrame:
    """Expose the internal Content index as the read-only No. column."""
    display = item_frame.copy()
    if "Content" not in display.columns:
        raise KeyError("v2 item frame requires the internal Content column")
    return display.rename(columns={"Content": "No."})[
        ["No.", "Description", "HSCode", "Value", "Quantity"]
    ]


def restore_v2_item_frame(display_frame: pd.DataFrame) -> pd.DataFrame:
    """Restore the internal Content index before using existing editor logic."""
    restored = display_frame.copy()
    if "No." not in restored.columns:
        raise KeyError("v2 display frame requires the No. column")
    restored = restored.rename(columns={"No.": "Content"})
    return restored[["Content", "Description", "HSCode", "Value", "Quantity"]]
