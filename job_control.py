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
        if order_id:
            _mark_order(
                orders,
                order_id,
                {
                    "status": "success",
                    "stage": "已完成",
                    "tracking_no": tracking,
                    "message": "",
                },
            )


def mark_unfinished_orders(job: dict[str, Any], status: str, stage: str, message: str) -> None:
    for order in job.get("orders") or []:
        if order.get("status") in {"queued", "running"}:
            order.update({"status": status, "stage": stage, "message": message})


def _mark_order(orders: list[dict[str, Any]], order_id: str, updates: dict[str, Any]) -> None:
    for order in orders:
        if order.get("order_id") == order_id:
            order.update(updates)
            return
