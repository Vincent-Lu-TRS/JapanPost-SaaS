"""Small helpers for postal pending-order UI feedback."""
from __future__ import annotations

import re

import pandas as pd
from job_control import shipment_package_key


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


def _package_key(result: dict) -> tuple[str, str, str] | None:
    try:
        order_id, trans_type, shipment_role = shipment_package_key(result)
    except ValueError:
        return None
    if not order_id or not trans_type:
        return None
    return order_id, trans_type, shipment_role


def completed_package_keys(results: list[dict] | None) -> set[tuple[str, str, str]]:
    """Return package keys with terminal, verified writeback success."""
    return {
        key
        for result in results or []
        for key in [_package_key(result)]
        if key is not None
        and str(result.get("status") or "").strip() in {"completed", "writeback_verified"}
    }


def fully_completed_order_ids(
    results: list[dict] | None,
    submitted_packages: list[dict] | None = None,
) -> set[str]:
    """Return orders whose every submitted package has verified completion."""
    rows = list(results or [])
    submitted_rows = list(submitted_packages or rows)
    submitted_keys = {
        key for row in submitted_rows for key in [_package_key(row)] if key is not None
    }
    if not submitted_keys:
        return completed_order_ids(rows)
    completed_keys = completed_package_keys(rows)
    order_ids = {key[0] for key in submitted_keys}
    return {
        order_id
        for order_id in order_ids
        if {key for key in submitted_keys if key[0] == order_id}
        <= completed_keys
    }


def _pending_order_ids(frame: pd.DataFrame) -> pd.Series:
    order_ids = pd.Series("", index=frame.index, dtype="object")
    for column in _PENDING_ORDER_ID_COLUMNS:
        if column not in frame.columns:
            continue
        values = frame[column].fillna("").astype(str).str.strip()
        order_ids = order_ids.mask(order_ids == "", values)
    return order_ids


def preserve_incomplete_submitted_orders(
    existing: pd.DataFrame | None,
    refreshed: pd.DataFrame,
    submitted_orders: list[dict] | None,
    results: list[dict] | None,
) -> pd.DataFrame:
    """Restore omitted cached rows until every submitted package is complete."""
    if not isinstance(refreshed, pd.DataFrame):
        return refreshed
    if not isinstance(existing, pd.DataFrame) or existing.empty:
        return refreshed.copy()

    submitted_keys = {
        key
        for row in submitted_orders or []
        for key in [_package_key(row)]
        if key is not None
    }
    if not submitted_keys:
        return refreshed.copy()

    completed_keys = completed_package_keys(results)
    incomplete_order_ids = {
        order_id
        for order_id in {key[0] for key in submitted_keys}
        if not {
            key for key in submitted_keys if key[0] == order_id
        } <= completed_keys
    }
    if not incomplete_order_ids:
        return refreshed.copy()

    refreshed_ids = set(_pending_order_ids(refreshed)) - {""}
    missing_ids = incomplete_order_ids - refreshed_ids
    if not missing_ids:
        return refreshed.copy()

    existing_ids = _pending_order_ids(existing)
    preserved = existing.loc[existing_ids.isin(missing_ids)].copy()
    if preserved.empty:
        return refreshed.copy()
    return pd.concat([refreshed.copy(), preserved], ignore_index=True, sort=False)


def filter_pending_orders_after_batch(
    pending: pd.DataFrame,
    results: list[dict] | None,
    *,
    submitted_packages: list[dict] | None = None,
) -> pd.DataFrame:
    """Hide successfully completed rows from the cached pending-order view.

    This is a presentation-layer filter only.  The input frame is not mutated,
    and failed/skipped rows remain visible so they can be reviewed or retried.
    """
    if not isinstance(pending, pd.DataFrame) or pending.empty:
        return pending

    completed_ids = fully_completed_order_ids(results, submitted_packages)
    if not completed_ids:
        return pending

    order_id_columns = [
        column for column in _PENDING_ORDER_ID_COLUMNS if column in pending.columns
    ]
    if not order_id_columns:
        return pending

    order_ids = _pending_order_ids(pending)

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
        status = str(result.get("status") or "").strip().lower()
        reason = str(
            result.get("reason_text")
            or result.get("message")
            or result.get("reason_code")
            or "未知原因"
        ).strip()
        if status == "backfill_failed":
            alerts.append(f"訂單編號 {order_id}：運單已產生，但資料回填未完成（{reason}）")
        else:
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
