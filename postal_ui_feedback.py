"""Small helpers for postal pending-order UI feedback."""
from __future__ import annotations

import re

import pandas as pd


_PENDING_ORDER_ID_COLUMNS = (
    "注文番号(貼上原始資料)",
    "注文番号(貼上原始資料)_1",
    "order_id",
    "Order No.",
)


def completed_order_ids(results: list[dict] | None) -> set[str]:
    """Return order IDs that were actually completed in a batch."""
    completed_statuses = {"success", "completed"}
    return {
        str(result.get("order_id") or "").strip()
        for result in results or []
        if str(result.get("status") or "").strip() in completed_statuses
        and str(result.get("order_id") or "").strip()
    }


def filter_pending_orders_after_batch(
    pending: pd.DataFrame,
    results: list[dict] | None,
) -> pd.DataFrame:
    """Hide successfully completed rows from the cached pending-order view.

    This is a presentation-layer filter only.  The input frame is not mutated,
    and failed/skipped rows remain visible so they can be reviewed or retried.
    """
    if not isinstance(pending, pd.DataFrame) or pending.empty:
        return pending

    completed_ids = completed_order_ids(results)
    if not completed_ids:
        return pending

    order_id_columns = [
        column for column in _PENDING_ORDER_ID_COLUMNS if column in pending.columns
    ]
    if not order_id_columns:
        return pending

    order_ids = pd.Series("", index=pending.index, dtype="object")
    for column in order_id_columns:
        values = pending[column].fillna("").astype(str).str.strip()
        order_ids = order_ids.mask(order_ids == "", values)

    return pending.loc[~order_ids.isin(completed_ids)].copy()


def summarize_batch_results(results: list[dict] | None) -> dict[str, object]:
    """Return authoritative counts and user-facing alerts for a completed job."""
    rows = list(results or [])
    completed_statuses = {"success", "completed"}
    failed_statuses = {"failed", "backfill_failed", "error", "skipped", "blocked"}
    completed_count = sum(
        1 for result in rows if str(result.get("status") or "").strip() in completed_statuses
    )
    failed_rows = [
        result for result in rows if str(result.get("status") or "").strip() in failed_statuses
    ]
    alerts = []
    for result in failed_rows:
        order_id = str(result.get("order_id") or "未指定").strip()
        reason = str(
            result.get("reason_text")
            or result.get("message")
            or result.get("reason_code")
            or "未知原因"
        ).strip()
        alerts.append(f"訂單編號 {order_id}：未製單（{reason}）")
    return {
        "total_count": len(rows),
        "completed_count": completed_count,
        "failed_count": sum(
            1 for result in failed_rows
            if str(result.get("status") or "").strip() in {"failed", "backfill_failed", "error"}
        ),
        "skipped_count": sum(
            1 for result in failed_rows
            if str(result.get("status") or "").strip() in {"skipped", "blocked"}
        ),
        "failure_alerts": alerts,
    }


def summarize_pending_read_logs(logs: list[str]) -> dict[str, str]:
    """Extract the useful pending-order read summary from diagnostic log lines."""
    summary = {
        "base_count": "-",
        "completed_filter": "-",
        "dedup_filter": "-",
        "final_count": "-",
        "elapsed": "-",
    }
    for line in logs or []:
        text = str(line)
        if "篩選後（未打單+必填）" in text:
            match = re.search(r"：(\d+)\s*筆", text)
            if match:
                summary["base_count"] = match.group(1)
        elif "雙重過濾" in text:
            match = re.search(r"：(\d+\s*→\s*\d+)\s*筆?", text)
            if match:
                summary["completed_filter"] = re.sub(r"\s*→\s*", " → ", match.group(1))
        elif "來源內同注文番号去重" in text:
            match = re.search(r"：(\d+\s*→\s*\d+)\s*筆?", text)
            if match:
                summary["dedup_filter"] = re.sub(r"\s*→\s*", " → ", match.group(1))
        elif "最終可打單" in text:
            count_match = re.search(r"最終可打單：(\d+)\s*筆", text)
            elapsed_match = re.search(r"總讀取耗時\s*([0-9.]+s)", text)
            if count_match:
                summary["final_count"] = count_match.group(1)
            if elapsed_match:
                summary["elapsed"] = elapsed_match.group(1)
    return summary
