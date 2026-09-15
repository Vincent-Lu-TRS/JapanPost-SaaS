"""PII-safe operational logging helpers."""
from __future__ import annotations

import logging
import math
import re
from typing import Callable, Iterable, Mapping


_ALLOWED_FIELDS = {
    "count",
    "seconds",
    "reason",
    "error_type",
    "first_row",
    "last_row",
    "stage",
    "code",
    "retryable",
    "attempts",
    "http_status",
    "system",
    "os_id",
    "os_version",
    "machine",
    "python_minor",
    "libc_name",
    "libc_version",
}

_ALLOWED_EVENTS = {
    "pending_read_failed",
    "preflight_blocked",
    "job_exception",
    "writeback_initialization_failed",
    "writeback_batch_finished",
    "browser_runtime_failed",
    "browser_runtime_profile",
}

_SAFE_RUNTIME_STAGES = {
    "profile",
    "runtime_libraries",
    "browser_profile",
    "browser_install",
    "browser_probe",
    "bootstrap",
}

_SAFE_RUNTIME_CODES = {
    "asset_missing",
    "asset_checksum",
    "asset_manifest_invalid",
    "asset_extract",
    "profile_unsupported",
    "browser_install",
    "browser_download",
    "browser_install_unknown",
    "browser_install_timeout",
    "browser_probe_timeout",
    "browser_launch",
    "bootstrap_timeout",
    "bootstrap_unknown",
    "bootstrap_protocol_error",
    "subreaper_unavailable",
    "operation_children_leaked",
    "operation_cleanup_failed",
    "deadline_exceeded",
    "lock_timeout",
    "wait_timeout",
    "profile_busy",
    "runtime_changed",
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
_PLATFORM_VALUE_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
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
    if field == "stage":
        if not isinstance(value, str) or value not in _SAFE_RUNTIME_STAGES:
            raise ValueError("unsafe field value: stage")
        return value
    if field == "code":
        if not isinstance(value, str) or value not in _SAFE_RUNTIME_CODES:
            raise ValueError("unsafe field value: code")
        return value
    if field == "retryable":
        if not isinstance(value, bool):
            raise ValueError("unsafe field value: retryable")
        return "true" if value else "false"
    if field == "attempts":
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 3:
            raise ValueError("unsafe field value: attempts")
        return str(value)
    if field == "http_status":
        if isinstance(value, bool) or not isinstance(value, int) or not 100 <= value <= 599:
            raise ValueError("unsafe field value: http_status")
        return str(value)
    if field in {"system", "os_id", "os_version", "machine", "python_minor", "libc_name", "libc_version"}:
        if not isinstance(value, str) or not _PLATFORM_VALUE_RE.fullmatch(value):
            raise ValueError(f"unsafe field value: {field}")
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
    if event == "browser_runtime_failed" and not {"stage", "code", "retryable"}.issubset(fields):
        raise ValueError("browser runtime event requires safe classification fields")
    if event == "browser_runtime_profile" and set(fields) != {
        "system", "os_id", "os_version", "machine", "python_minor", "libc_name", "libc_version"
    }:
        raise ValueError("browser runtime profile requires the safe environment fingerprint")
    parts = [event]
    for field, value in fields.items():
        parts.append(f"{field}={_safe_field_value(field, value)}")
    if log_cb is not None:
        log_cb(" ".join(parts))


def log_runtime_failure(log_cb: Callable[[str], object] | None, error: object) -> None:
    """Log a runtime error's enum classification, never its exception text."""

    stage = getattr(error, "stage", "bootstrap")
    code = getattr(error, "code", "bootstrap_unknown")
    if stage not in _SAFE_RUNTIME_STAGES:
        stage = "bootstrap"
    if code not in _SAFE_RUNTIME_CODES:
        code = "bootstrap_unknown"
    try:
        attempts = int(getattr(error, "attempts", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        attempts = 0
    fields: dict[str, object] = {
        "stage": stage,
        "code": code,
        "retryable": bool(getattr(error, "retryable", False)),
        "attempts": min(max(attempts, 0), 3),
    }
    status = getattr(error, "http_status", None)
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        fields["http_status"] = status
    safe_log_event(log_cb, "browser_runtime_failed", **fields)


def log_runtime_environment(
    log_cb: Callable[[str], object] | None,
    environment: Mapping[str, object],
) -> None:
    """Log only the small, allowlisted platform fingerprint used for compatibility checks."""

    if not isinstance(environment, Mapping):
        raise ValueError("runtime environment fingerprint is invalid")
    expected = {
        "system", "os_id", "os_version", "machine", "python_major", "python_minor",
        "libc_name", "libc_version",
    }
    if set(environment) != expected:
        raise ValueError("runtime environment fingerprint fields are invalid")
    major, minor = environment["python_major"], environment["python_minor"]
    if (
        isinstance(major, bool) or not isinstance(major, int) or not 0 <= major <= 99
        or isinstance(minor, bool) or not isinstance(minor, int) or not 0 <= minor <= 99
    ):
        raise ValueError("runtime environment Python version is invalid")
    fields = {
        "system": environment["system"],
        "os_id": environment["os_id"],
        "os_version": environment["os_version"],
        "machine": environment["machine"],
        "python_minor": f"{major}.{minor}",
        "libc_name": environment["libc_name"],
        "libc_version": environment["libc_version"],
    }
    safe_log_event(log_cb, "browser_runtime_profile", **fields)


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
