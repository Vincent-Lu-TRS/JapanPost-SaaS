"""PII-safe operational logging helpers."""
from __future__ import annotations

import logging
import math
import re
from typing import Callable, Iterable


_ALLOWED_FIELDS = {
    "count",
    "seconds",
    "reason",
    "error_type",
    "first_row",
    "last_row",
}

_ALLOWED_EVENTS = {
    "pending_read_failed",
    "preflight_blocked",
    "job_exception",
    "writeback_initialization_failed",
    "writeback_batch_finished",
}

_SAFE_REASONS = {
    "source_changed",
    "source_indicates_done_target_missing",
    "already_completed",
    "invalid_shipment_role",
    "missing_package_identity",
    "duplicate_package_request",
    "additional_transport_matches_primary",
    "writeback_permission_denied",
    "writeback_network_error",
    "writeback_api_error",
    "writeback_readback_failed",
    "partial_write",
    "tracking_owned_by_other_order",
    "unexpected_second_tracking",
    "incomplete_existing_row",
    "duplicate_batch_pair",
    "invalid_writeback_identity",
}

_ERROR_TYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{0,63}$")
_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_TRACKING_RE = re.compile(r"(?i)\b[A-Z]{2}\d{9}[A-Z]{2}\b")
_LONG_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])\d{6,}(?![A-Za-z0-9])")
_IDENTIFIER_FIELD_RE = re.compile(
    r"(?i)\b(order(?:_id|\s+no\.?|\s+number)?|receiver|tracking(?:_no|\s+no\.?)?)"
    r"\s*([:=])\s*([^\s,;]+)"
)


def _safe_field_value(field: str, value: object) -> str:
    if field in {"count", "first_row", "last_row"}:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"unsafe field value: {field}")
        return str(value)
    if field == "seconds":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("unsafe field value: seconds")
        number = float(value)
        if not math.isfinite(number) or number < 0:
            raise ValueError("unsafe field value: seconds")
        return str(value)
    if field == "reason":
        if not isinstance(value, str) or value not in _SAFE_REASONS:
            raise ValueError("unsafe field value: reason")
        return value
    if field == "error_type":
        if not isinstance(value, str) or not _ERROR_TYPE_RE.fullmatch(value):
            raise ValueError("unsafe field value: error_type")
        return value
    raise ValueError(f"unknown log field: {field}")


def safe_log_event(
    log_cb: Callable[[str], object] | None,
    event: str,
    **fields: object,
) -> None:
    """Emit one allowlisted event containing aggregate-safe fields only."""
    if event not in _ALLOWED_EVENTS:
        raise ValueError("unknown log event")
    unknown = set(fields) - _ALLOWED_FIELDS
    if unknown:
        raise ValueError("unknown log field")
    parts = [event]
    for field, value in fields.items():
        parts.append(f"{field}={_safe_field_value(field, value)}")
    if log_cb is not None:
        log_cb(" ".join(parts))


def build_safe_automation_logger(
    log_cb: Callable[[str], object] | None = None,
    *,
    sensitive_values: Iterable[object] = (),
    logger: logging.Logger | None = None,
) -> Callable[[object], None]:
    """Build a boundary that redacts before callbacks and Python logging."""
    python_logger = logger or logging.getLogger("jppost.automation")
    sensitive_tokens = tuple(sensitive_values)

    def emit(message: object) -> None:
        safe_message = redact_operational_log(
            message,
            sensitive_values=sensitive_tokens,
        )
        if log_cb is not None:
            log_cb(safe_message)
        python_logger.info("%s", safe_message)

    return emit


def redact_operational_log(
    message: object,
    sensitive_values: Iterable[object] = (),
) -> str:
    """Normalize a diagnostic message and redact common operational identifiers."""
    text = " ".join(str(message).splitlines())
    for value in sensitive_values:
        token = str(value or "")
        if token:
            text = re.sub(re.escape(token), "[REDACTED]", text, flags=re.IGNORECASE)
    text = _IDENTIFIER_FIELD_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
        text,
    )
    text = _EMAIL_RE.sub("[REDACTED]", text)
    text = _TRACKING_RE.sub("[REDACTED]", text)
    text = _LONG_NUMBER_RE.sub("[REDACTED]", text)
    return " ".join(text.split())
