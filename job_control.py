import hashlib
import json
import re
import threading
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import pandas as pd

if TYPE_CHECKING:
    from bot.sheets import CompletionAuthority


ORDER_ID_COLUMNS = ["注文番号(貼上原始資料)", "注文番号(貼上原始資料)_1", "order_id"]
RECIPIENT_COLUMNS = ["Shipping Name", "Shipping Name_1", "name"]
COUNTRY_COLUMNS = ["收件人國家", "Country", "country"]
TRANS_TYPE_COLUMNS = ["郵局運送方式(複數商品請自行確認是否走小包)", "TransType", "trans_type"]
TOTAL_USD_COLUMNS = ["郵局申告金額(USD)", "TotalValue(USD)", "total_usd"]
TOTAL_JPY_COLUMNS = ["訂單合計申告金額(JPY)", "TotalValue(JPY)", "total_jpy"]
SHIPMENT_ROLE_COLUMNS = ["_shipment_role", "shipment_role"]

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


def _row_value(row: pd.Series | Mapping[str, Any], columns: list[str], default: str = "") -> str:
    if not isinstance(row, (pd.Series, Mapping)):
        return default
    for column in columns:
        if column in row:
            value = row.get(column)
            if pd.notna(value) and str(value).strip():
                return str(value).strip()
    return default


def _shipment_role(row: pd.Series | Mapping[str, Any]) -> str:
    role = _row_value(row, SHIPMENT_ROLE_COLUMNS, "primary").lower()
    if role in {"primary", "additional"}:
        return role
    raise ValueError(f"invalid shipment role: {role}")


def shipment_package_key(
    row: pd.Series | Mapping[str, Any],
) -> tuple[str, str, str]:
    return (
        _row_value(row, ORDER_ID_COLUMNS),
        _row_value(row, TRANS_TYPE_COLUMNS),
        _shipment_role(row),
    )


def _shipment_state_id(package_key: tuple[str, str, str]) -> str:
    raw = json.dumps(package_key, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _selected_rows(df: pd.DataFrame, max_rows: int | None) -> pd.DataFrame:
    if max_rows is None or max_rows <= 0:
        return df.copy()
    return df.head(max_rows).copy()


def create_order_states(df: pd.DataFrame, max_rows: int | None) -> list[dict[str, Any]]:
    rows = _selected_rows(df, max_rows)
    states: list[dict[str, Any]] = []
    for position, (_, row) in enumerate(rows.iterrows(), start=1):
        order_id, trans_type, shipment_role = shipment_package_key(row)
        order_id = order_id or f"row-{position}"
        package_key = (order_id, trans_type, shipment_role)
        states.append(
            {
                "position": position,
                "state_id": _shipment_state_id(package_key),
                "order_id": order_id,
                "recipient": _row_value(row, RECIPIENT_COLUMNS),
                "country": _row_value(row, COUNTRY_COLUMNS),
                "trans_type": trans_type,
                "shipment_role": shipment_role,
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
            "shipment_role": state["shipment_role"],
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


def update_order_status_from_event(
    job: dict[str, Any],
    event: Mapping[str, Any],
) -> bool:
    orders = job.get("orders") or []
    if not orders or not isinstance(event, Mapping):
        return False

    event_type = str(event.get("event") or event.get("type") or "").strip().lower()
    if event_type not in {
        "order_started",
        "label_created",
        "order_failed",
        "writeback_pending",
        "writeback_verified",
    }:
        return False

    order_id = _row_value(event, ORDER_ID_COLUMNS)
    trans_type = _row_value(event, TRANS_TYPE_COLUMNS)
    if not order_id or not trans_type or not any(
        column in event for column in SHIPMENT_ROLE_COLUMNS
    ):
        return False
    try:
        shipment_role = _shipment_role(event)
    except ValueError:
        return False

    candidates = [
        order
        for order in orders
        if (
            str(order.get("order_id") or "").strip(),
            str(order.get("trans_type") or "").strip(),
            str(order.get("shipment_role") or "primary").strip().lower(),
        ) == (order_id, trans_type, shipment_role)
    ]
    if len(candidates) != 1:
        return False

    tracking = str(event.get("tracking") or event.get("tracking_no") or "").strip()
    if event_type == "writeback_verified":
        updates = {
            "status": "success",
            "stage": "已完成",
            "tracking_no": tracking,
            "message": "",
        }
    elif event_type == "order_failed":
        reason_code = str(event.get("reason_code") or "order_failed").strip()
        updates = {
            "status": "failed",
            "stage": "需排查",
            "message": (
                "已停止但未取得完整結果"
                if reason_code == "stopped"
                else reason_code
            ),
        }
    elif event_type == "label_created":
        updates = {
            "status": "running",
            "stage": "標籤已建立，等待回填確認",
            "tracking_no": tracking,
            "message": "",
        }
    elif event_type == "writeback_pending":
        updates = {
            "status": "running",
            "stage": "正在回填確認",
            "message": "",
        }
    else:
        updates = {
            "status": "running",
            "stage": "製單中",
            "message": "",
        }
    candidates[0].update(updates)
    return True


def _log_package_qualifiers(message: str) -> tuple[str, str]:
    trans_type_match = re.search(r"\btrans_type=([^\]\s]+)", message)
    shipment_role_match = re.search(r"\bshipment_role=([^\]\s]+)", message)
    trans_type = trans_type_match.group(1).strip() if trans_type_match else ""
    shipment_role = shipment_role_match.group(1).strip().lower() if shipment_role_match else ""
    if shipment_role and shipment_role not in {"primary", "additional"}:
        raise ValueError(f"invalid shipment role: {shipment_role}")
    return trans_type, shipment_role


def update_order_status_from_log(job: dict[str, Any], message: str) -> None:
    orders = job.get("orders") or []
    if not orders:
        return

    start_match = re.search(r"開始處理訂單：(.+?)（索引\s*(\d+)）", message)
    if start_match:
        index = int(start_match.group(2))
        if 0 <= index < len(orders):
            order = orders[index]
            update_order_status_from_event(
                job,
                {
                    "event": "order_started",
                    "order_id": order.get("order_id", ""),
                    "trans_type": order.get("trans_type", ""),
                    "shipment_role": order.get("shipment_role", "primary"),
                    "row_index": index,
                },
            )
        return

    done_match = re.search(r"訂單\s+(.+?)\s+完成，(?:貨運)?單號[:：]?\s*([A-Z]{2}\d{9}JP)", message)
    if done_match:
        trans_type, shipment_role = _log_package_qualifiers(message)
        order_id = done_match.group(1).strip()
        candidates = [order for order in orders if order.get("order_id") == order_id]
        if trans_type:
            candidates = [order for order in candidates if order.get("trans_type") == trans_type]
        if shipment_role:
            candidates = [order for order in candidates if order.get("shipment_role") == shipment_role]
        if len(candidates) == 1:
            order = candidates[0]
            update_order_status_from_event(
                job,
                {
                    "event": "label_created",
                    "order_id": order_id,
                    "tracking": done_match.group(2),
                    "trans_type": order.get("trans_type", ""),
                    "shipment_role": order.get("shipment_role", "primary"),
                },
            )
        return

    stopped_match = re.search(r"訂單\s+(.+?)\s+.*(已停止.*)", message)
    if stopped_match:
        trans_type, shipment_role = _log_package_qualifiers(message)
        order_id = stopped_match.group(1).strip()
        candidates = [order for order in orders if order.get("order_id") == order_id]
        if trans_type:
            candidates = [order for order in candidates if order.get("trans_type") == trans_type]
        if shipment_role:
            candidates = [order for order in candidates if order.get("shipment_role") == shipment_role]
        if len(candidates) == 1:
            order = candidates[0]
            update_order_status_from_event(
                job,
                {
                    "event": "order_failed",
                    "order_id": order_id,
                    "trans_type": order.get("trans_type", ""),
                    "shipment_role": order.get("shipment_role", "primary"),
                    "reason_code": "stopped",
                },
            )


def mark_results_completed(job: dict[str, Any], results: list[dict[str, Any]]) -> None:
    orders = job.get("orders") or []
    for result in results:
        order_id = str(result.get("order_id") or "").strip()
        tracking = str(result.get("tracking") or "").strip()
        trans_type = str(result.get("trans_type") or result.get("TransType") or "").strip()
        shipment_role = _shipment_role(result)
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
                shipment_role=shipment_role,
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
            shipment_role=_shipment_role(result),
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
    completion: "CompletionAuthority | set[str]",
) -> list[dict[str, Any]]:
    """Compare the selected snapshot with fresh source/target authority."""
    latest_by_order: dict[str, pd.Series] = {}
    if isinstance(latest_pending_df, pd.DataFrame):
        for _, row in latest_pending_df.iterrows():
            order_id = _row_value(row, ORDER_ID_COLUMNS)
            if order_id and order_id not in latest_by_order:
                latest_by_order[order_id] = row

    selected_primary_transport: dict[str, str] = {}
    if isinstance(selected_df, pd.DataFrame):
        for _, row in selected_df.iterrows():
            if _row_value(row, SHIPMENT_ROLE_COLUMNS, "primary").lower() != "primary":
                continue
            order_id = _row_value(row, ORDER_ID_COLUMNS)
            trans_type = _row_value(row, TRANS_TYPE_COLUMNS)
            if order_id and trans_type:
                selected_primary_transport.setdefault(order_id, trans_type)

    legacy_order_ids = set(getattr(completion, "legacy_order_ids", completion or set()))
    exact_pairs = set(getattr(completion, "exact_pairs", set()))
    checks: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    if not isinstance(selected_df, pd.DataFrame):
        return checks
    for row_index, (_, row) in enumerate(selected_df.iterrows()):
        order_id = _row_value(row, ORDER_ID_COLUMNS)
        trans_type = _row_value(row, TRANS_TYPE_COLUMNS)
        raw_role = _row_value(row, SHIPMENT_ROLE_COLUMNS, "primary").lower()
        shipment_role = raw_role if raw_role in {"primary", "additional"} else raw_role

        def _item(status: str, reason_code: str = "", reason_text: str = "") -> dict[str, Any]:
            return {
                "order_id": order_id,
                "trans_type": trans_type,
                "shipment_role": shipment_role,
                "row_index": row_index,
                "reason_code": reason_code,
                "reason_text": reason_text,
                "status": status,
            }

        try:
            shipment_role = _shipment_role(row)
        except ValueError:
            checks.append(_item("blocked", "invalid_shipment_role", "製單角色無效，請重新選擇主要或追加包裹。"))
            continue
        if not order_id or not trans_type:
            checks.append(_item("blocked", "missing_package_identity", "缺少訂單或運送方式，停止製單。"))
            continue
        package_key = (order_id, trans_type, shipment_role)
        if package_key in seen:
            checks.append(_item("blocked", "duplicate_package_request", "同一包裹在本批重複出現，停止製單。"))
            continue
        seen.add(package_key)
        selected_tracking = _row_value(row, ["tracking", "tracking_no", "貨運單號"])
        if (selected_tracking and (order_id, selected_tracking) in exact_pairs) or (
            shipment_role == "primary" and order_id in legacy_order_ids
        ):
            checks.append(_item("already_completed", "already_completed", "目標表已有完成紀錄"))
            continue
        latest = latest_by_order.get(order_id)
        if latest is None:
            source_status = _row_value(row, ["製單上傳狀態(請用[未打單]檢視模式)"])
            status_code = (
                "source_indicates_done_target_missing"
                if re.fullmatch(r"[A-Z]{2}\d{9}JP", source_status)
                else "source_changed"
            )
            checks.append(_item(
                status_code,
                status_code,
                (
                    "來源狀態已有 tracking、但目標表缺少完成證據，停止自動製單"
                    if status_code == "source_indicates_done_target_missing"
                    else "最新來源資料已不再是可製單狀態，停止以避免誤製"
                ),
            ))
            continue
        primary_trans_type = selected_primary_transport.get(order_id) or _row_value(
            latest,
            TRANS_TYPE_COLUMNS,
        )
        if shipment_role == "additional" and trans_type == primary_trans_type:
            checks.append(_item("blocked", "additional_transport_matches_primary", "追加包裹不得與主要包裹使用相同運送方式。"))
            continue
        selected_fingerprint = _row_value(row, ["_source_fingerprint"])
        latest_fingerprint = _row_value(latest, ["_source_fingerprint"])
        if selected_fingerprint and latest_fingerprint and selected_fingerprint != latest_fingerprint:
            checks.append(_item("source_changed", "source_changed", "來源資料在選取後已變更，停止以避免使用過期內容"))
            continue
        checks.append(_item("ready"))
    return checks


def partition_preflight_rows(
    selected_df: pd.DataFrame,
    checks: list[dict[str, Any]],
) -> tuple[pd.DataFrame, list[dict[str, Any]], list[dict[str, Any]]]:
    """Fail closed while separating executable, terminal, and blocked packages."""
    if not isinstance(selected_df, pd.DataFrame):
        return pd.DataFrame(), [], []
    by_position: dict[int, dict[str, Any]] = {}
    for check in checks or []:
        row_index = check.get("row_index")
        if isinstance(row_index, int) and 0 <= row_index < len(selected_df):
            by_position.setdefault(row_index, check)

    ready_positions: list[int] = []
    already_completed: list[dict[str, Any]] = []
    hard_blocked: list[dict[str, Any]] = []
    for row_index, (_, row) in enumerate(selected_df.iterrows()):
        check = by_position.get(row_index)
        if check is None:
            try:
                order_id, trans_type, shipment_role = shipment_package_key(row)
            except ValueError:
                order_id = _row_value(row, ORDER_ID_COLUMNS)
                trans_type = _row_value(row, TRANS_TYPE_COLUMNS)
                shipment_role = _row_value(row, SHIPMENT_ROLE_COLUMNS)
            hard_blocked.append({
                "order_id": order_id,
                "trans_type": trans_type,
                "shipment_role": shipment_role,
                "row_index": row_index,
                "reason_code": "missing_preflight_result",
                "reason_text": "製單前檢查缺少結果，停止製單。",
                "status": "blocked",
            })
        elif check.get("status") == "ready":
            ready_positions.append(row_index)
        elif check.get("status") == "already_completed":
            already_completed.append(check)
        else:
            hard_blocked.append(check)
    ready_df = selected_df.iloc[ready_positions].copy()
    return ready_df, already_completed, hard_blocked


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
    shipment_role: str = "",
) -> None:
    candidates = [order for order in orders if order.get("order_id") == order_id]
    if trans_type:
        candidates = [
            order
            for order in candidates
            if str(order.get("trans_type") or "").strip() == trans_type
        ]
    if shipment_role:
        if shipment_role not in {"primary", "additional"}:
            raise ValueError(f"invalid shipment role: {shipment_role}")
        candidates = [order for order in candidates if _shipment_role(order) == shipment_role]
    if len(candidates) == 1:
        candidates[0].update(updates)
