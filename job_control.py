import hashlib
import json
import re
import threading
import time
from typing import Any

import pandas as pd


ORDER_ID_COLUMNS = ["注文番号(貼上原始資料)", "注文番号(貼上原始資料)_1", "order_id"]
RECIPIENT_COLUMNS = ["Shipping Name", "Shipping Name_1", "name"]
COUNTRY_COLUMNS = ["收件人國家", "Country", "country"]
TRANS_TYPE_COLUMNS = ["郵局運送方式(複數商品請自行確認是否走小包)", "TransType", "trans_type"]
TOTAL_USD_COLUMNS = ["郵局申告金額(USD)", "TotalValue(USD)", "total_usd"]
TOTAL_JPY_COLUMNS = ["訂單合計申告金額(JPY)", "TotalValue(JPY)", "total_jpy"]

KEY_LOG_MARKERS = (
    "任務啟動",
    "開始處理訂單",
    "自動化完成",
    "正在回填",
    "完成！",
    "完成，貨運單號",
    "已停止",
    "例外",
    "錯誤",
    "失敗",
)


def _row_value(row: pd.Series, columns: list[str], default: str = "") -> str:
    for column in columns:
        if column in row.index:
            value = row.get(column)
            if pd.notna(value) and str(value).strip():
                return str(value).strip()
    return default


def _selected_rows(df: pd.DataFrame, max_rows: int | None) -> pd.DataFrame:
    if max_rows is None or max_rows <= 0:
        return df.copy()
    return df.head(max_rows).copy()


def create_order_states(df: pd.DataFrame, max_rows: int | None) -> list[dict[str, Any]]:
    rows = _selected_rows(df, max_rows)
    states: list[dict[str, Any]] = []
    for position, (_, row) in enumerate(rows.iterrows(), start=1):
        states.append(
            {
                "position": position,
                "order_id": _row_value(row, ORDER_ID_COLUMNS, f"row-{position}"),
                "recipient": _row_value(row, RECIPIENT_COLUMNS),
                "country": _row_value(row, COUNTRY_COLUMNS),
                "trans_type": _row_value(row, TRANS_TYPE_COLUMNS),
                "total_usd": _row_value(row, TOTAL_USD_COLUMNS),
                "total_jpy": _row_value(row, TOTAL_JPY_COLUMNS),
                "status": "queued",
                "stage": "待機中",
                "tracking_no": "",
                "message": "",
            }
        )
    return states


def build_batch_fingerprint(df: pd.DataFrame, max_rows: int | None) -> str:
    states = create_order_states(df, max_rows)
    payload = [
        {
            "order_id": state["order_id"],
            "recipient": state["recipient"],
            "country": state["country"],
            "trans_type": state["trans_type"],
        }
        for state in states
    ]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class BatchJobRegistry:
    def __init__(self):
        self._lock = threading.RLock()
        self._jobs: dict[str, dict[str, Any]] = {}

    def get(self, email: str) -> dict[str, Any] | None:
        with self._lock:
            return self._jobs.get(email)

    def start(
        self,
        email: str,
        df: pd.DataFrame,
        max_rows: int | None,
    ) -> tuple[bool, dict[str, Any] | None, str]:
        fingerprint = build_batch_fingerprint(df, max_rows)
        with self._lock:
            existing = self._jobs.get(email)
            if existing and existing.get("status") == "running":
                return False, existing, "user_running"

            for job in self._jobs.values():
                if (
                    job.get("status") == "running"
                    and job.get("batch_fingerprint") == fingerprint
                ):
                    return False, job, "batch_running"

            job: dict[str, Any] = {
                "status": "running",
                "logs": [],
                "results": [],
                "orders": create_order_states(df, max_rows),
                "batch_fingerprint": fingerprint,
                "started_at": time.strftime("%H:%M:%S"),
            }
            self._jobs[email] = job
            return True, job, ""

    def finish(self, job: dict[str, Any], status: str) -> None:
        with self._lock:
            job["status"] = status


def filter_key_log_lines(logs: list[str], limit: int = 80) -> list[str]:
    key_lines = [
        line
        for line in logs
        if any(marker in line for marker in KEY_LOG_MARKERS)
        and "response diagnostics" not in line
        and "payload" not in line
    ]
    return key_lines[-limit:]


def update_order_status_from_log(job: dict[str, Any], message: str) -> None:
    orders = job.get("orders") or []
    if not orders:
        return

    start_match = re.search(r"開始處理訂單：(.+?)（索引\s*(\d+)）", message)
    if start_match:
        index = int(start_match.group(2))
        if 0 <= index < len(orders):
            orders[index].update({"status": "running", "stage": "製單中", "message": ""})
        return

    done_match = re.search(r"訂單\s+(.+?)\s+完成，(?:貨運)?單號[:：]?\s*([A-Z]{2}\d{9}JP)", message)
    if done_match:
        _mark_order(
            orders,
            done_match.group(1).strip(),
            {
                "status": "success",
                "stage": "已完成",
                "tracking_no": done_match.group(2),
                "message": "",
            },
        )
        return

    stopped_match = re.search(r"訂單\s+(.+?)\s+.*(已停止.*)", message)
    if stopped_match:
        _mark_order(
            orders,
            stopped_match.group(1).strip(),
            {
                "status": "failed",
                "stage": "需排查",
                "message": stopped_match.group(2).strip(),
            },
        )


def mark_results_completed(job: dict[str, Any], results: list[dict[str, Any]]) -> None:
    orders = job.get("orders") or []
    for result in results:
        order_id = str(result.get("order_id") or "").strip()
        tracking = str(result.get("tracking") or "").strip()
        trans_type = str(result.get("trans_type") or result.get("TransType") or "").strip()
        if order_id:
            result["status"] = "completed"
            _mark_order(
                orders,
                order_id,
                {
                    "status": "success",
                    "stage": "已完成",
                    "tracking_no": tracking,
                    "message": "",
                },
                trans_type=trans_type,
            )


def mark_results_failed(job: dict[str, Any], results: list[dict[str, Any]]) -> None:
    orders = job.get("orders") or []
    for result in results:
        order_id = str(result.get("order_id") or "").strip()
        if not order_id:
            continue
        reason_code = str(result.get("reason_code") or "unknown").strip()
        reason_text = str(result.get("reason_text") or result.get("message") or reason_code).strip()
        status = str(result.get("status") or "failed").strip()
        _mark_order(
            orders,
            order_id,
            {
                "status": "skipped" if status in {"skipped", "blocked"} else "failed",
                "stage": "未製單" if status in {"skipped", "blocked"} else "需排查",
                "reason_code": reason_code,
                "message": reason_text,
            },
            trans_type=str(result.get("trans_type") or result.get("TransType") or "").strip(),
        )


def summarize_job_results(results: list[dict[str, Any]] | None) -> dict[str, Any]:
    rows = list(results or [])
    completed_statuses = {"success", "completed"}
    failure_statuses = {"failed", "backfill_failed", "error"}
    skipped_statuses = {"skipped", "blocked"}
    failures = [
        result
        for result in rows
        if str(result.get("status") or "").strip() in failure_statuses | skipped_statuses
    ]
    return {
        "total": len(rows),
        "completed": sum(1 for result in rows if str(result.get("status") or "").strip() in completed_statuses),
        "failed": sum(1 for result in rows if str(result.get("status") or "").strip() in failure_statuses),
        "skipped": sum(1 for result in rows if str(result.get("status") or "").strip() in skipped_statuses),
        "failures": failures,
    }


def preflight_batch_orders(
    selected_df: pd.DataFrame,
    latest_pending_df: pd.DataFrame,
    completed_ids: set[str],
) -> list[dict[str, str]]:
    """Compare the selected snapshot with fresh source/target authority."""
    latest_by_order: dict[str, pd.Series] = {}
    if isinstance(latest_pending_df, pd.DataFrame):
        for _, row in latest_pending_df.iterrows():
            order_id = _row_value(row, ORDER_ID_COLUMNS)
            if order_id and order_id not in latest_by_order:
                latest_by_order[order_id] = row

    checks: list[dict[str, str]] = []
    seen: set[str] = set()
    if not isinstance(selected_df, pd.DataFrame):
        return checks
    for _, row in selected_df.iterrows():
        order_id = _row_value(row, ORDER_ID_COLUMNS)
        if not order_id or order_id in seen:
            continue
        seen.add(order_id)
        if order_id in completed_ids:
            checks.append({
                "order_id": order_id,
                "status": "already_completed",
                "reason_code": "already_completed",
                "reason_text": "目標表已有完成紀錄",
            })
            continue
        latest = latest_by_order.get(order_id)
        if latest is None:
            source_status = _row_value(row, ["製單上傳狀態(請用[未打單]檢視模式)"])
            status_code = (
                "source_indicates_done_target_missing"
                if re.fullmatch(r"[A-Z]{2}\d{9}JP", source_status)
                else "source_changed"
            )
            checks.append({
                "order_id": order_id,
                "status": status_code,
                "reason_code": status_code,
                "reason_text": (
                    "來源狀態已有 tracking、但目標表缺少完成證據，停止自動製單"
                    if status_code == "source_indicates_done_target_missing"
                    else "最新來源資料已不再是可製單狀態，停止以避免誤製"
                ),
            })
            continue
        selected_fingerprint = _row_value(row, ["_source_fingerprint"])
        latest_fingerprint = _row_value(latest, ["_source_fingerprint"])
        if selected_fingerprint and latest_fingerprint and selected_fingerprint != latest_fingerprint:
            checks.append({
                "order_id": order_id,
                "status": "source_changed",
                "reason_code": "source_changed",
                "reason_text": "來源資料在選取後已變更，停止以避免使用過期內容",
            })
            continue
        checks.append({
            "order_id": order_id,
            "status": "ready",
            "reason_code": "",
            "reason_text": "",
        })
    return checks


def mark_unfinished_orders(job: dict[str, Any], status: str, stage: str, message: str) -> None:
    for order in job.get("orders") or []:
        if order.get("status") in {"queued", "running"}:
            order.update({"status": status, "stage": stage, "message": message})


def summarize_job_progress(job: dict[str, Any] | None) -> dict[str, Any]:
    orders = (job or {}).get("orders") or []
    total = len(orders)
    done_statuses = {"success", "completed", "failed", "skipped", "blocked"}
    done = sum(1 for order in orders if order.get("status") in done_statuses)
    active = next((order for order in orders if order.get("status") == "running"), None)
    if active is None:
        active = next((order for order in orders if order.get("status") == "queued"), None)
    return {
        "total": total,
        "done": done,
        "remaining": max(total - done, 0),
        "ratio": (done / total) if total else 0.0,
        "active_order_id": str((active or {}).get("order_id", "") or ""),
        "active_stage": str((active or {}).get("stage", "") or ""),
    }


def _mark_order(
    orders: list[dict[str, Any]],
    order_id: str,
    updates: dict[str, Any],
    trans_type: str = "",
) -> None:
    for order in orders:
        if order.get("order_id") == order_id and (
            not trans_type or str(order.get("trans_type") or "").strip() == trans_type
        ):
            order.update(updates)
            return
