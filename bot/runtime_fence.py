"""Request-scoped process fence for browser runtime preparation and validation.

The Streamlit process starts this module with ``sys.executable -m`` for each
prepare/validate call. It is deliberately not a daemon: one request enters,
performs one synchronous runtime operation, closes/reaps operation children,
returns a small allowlisted JSON response, and exits.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import math
import os
import re
import signal
import sys
import time
from pathlib import Path
from typing import Callable, Mapping

from .browser_bootstrap import RuntimeHandle, RuntimeSetupError


PROTOCOL_VERSION = 1
MAX_IPC_BYTES = 16 * 1024
MAX_ENV_VALUE_CHARS = 4096
SUBREAPER_CLEANUP_SECONDS = 1.5
_CHILD_ENVIRONMENT_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
    }
)
_RUNTIME_HANDLE_ENV_KEYS = _CHILD_ENVIRONMENT_KEYS | frozenset(
    {"LD_LIBRARY_PATH", "PLAYWRIGHT_BROWSERS_PATH"}
)
_RUNTIME_HANDLE_FIELDS = frozenset(
    {
        "profile_key",
        "executable_path",
        "browser_revision",
        "env",
        "executable_sha256",
        "executable_size",
        "executable_mtime_ns",
        "browser_flavor",
        "browser_version",
    }
)
_SAFE_ERROR_CODES = frozenset(
    {
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
        "profile_key_missing",
        "profile_mismatch",
        "invalid_ready_result",
        "deadline_exceeded",
        "lock_timeout",
        "profile_busy",
        "wait_timeout",
        "runtime_changed",
    }
)
_SAFE_STAGES = frozenset(
    {
        "bootstrap",
        "browser_profile",
        "profile",
        "runtime_libraries",
        "browser_install",
        "browser_probe",
        "validate",
    }
)
_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")


def build_child_environment(source: Mapping[str, str]) -> dict[str, str]:
    """Return only non-secret runtime variables required by the child helper."""

    safe: dict[str, str] = {}
    for name in _CHILD_ENVIRONMENT_KEYS:
        value = source.get(name)
        if not isinstance(value, str) or not value or len(value) > MAX_ENV_VALUE_CHARS:
            continue
        if "\x00" in value:
            continue
        if name in {"SSL_CERT_FILE", "SSL_CERT_DIR"} and re.match(r"^[a-z][a-z0-9+.-]*://", value, re.I):
            continue
        safe[name] = value
    return safe


def _safe_runtime_handle_environment(source: Mapping[str, str]) -> dict[str, str]:
    safe: dict[str, str] = {}
    for name, value in source.items():
        if (
            name in _RUNTIME_HANDLE_ENV_KEYS
            and isinstance(value, str)
            and value
            and len(value) <= MAX_ENV_VALUE_CHARS
            and "\x00" not in value
            and "\n" not in value
            and "\r" not in value
        ):
            safe[name] = value
    return safe


def serialize_runtime_handle(handle: RuntimeHandle) -> dict[str, object]:
    """Serialize only the explicit, non-secret RuntimeHandle fields."""

    if not isinstance(handle, RuntimeHandle):
        raise ValueError("runtime handle is invalid")
    if not isinstance(handle.profile_key, str) or not _KEY_PATTERN.fullmatch(handle.profile_key):
        raise ValueError("runtime handle profile key is invalid")
    if not isinstance(handle.browser_revision, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", handle.browser_revision):
        raise ValueError("runtime handle browser revision is invalid")
    if not isinstance(handle.browser_flavor, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", handle.browser_flavor):
        raise ValueError("runtime handle browser flavor is invalid")
    if not isinstance(handle.browser_version, str) or not re.fullmatch(r"[A-Za-z0-9_.+-]{0,128}", handle.browser_version):
        raise ValueError("runtime handle browser version is invalid")
    if not isinstance(handle.executable_sha256, str) or not re.fullmatch(r"[A-Fa-f0-9]{0,64}", handle.executable_sha256):
        raise ValueError("runtime handle executable hash is invalid")
    if (
        not isinstance(handle.executable_size, int)
        or isinstance(handle.executable_size, bool)
        or handle.executable_size < 0
        or not isinstance(handle.executable_mtime_ns, int)
        or isinstance(handle.executable_mtime_ns, bool)
        or handle.executable_mtime_ns < 0
    ):
        raise ValueError("runtime handle executable metadata is invalid")
    path = str(handle.executable_path)
    if (
        not path
        or len(path) > 4096
        or "\x00" in path
        or "\n" in path
        or "\r" in path
        or "://" in path
        or not (Path(path).is_absolute() or path.startswith(("/", "\\")))
    ):
        raise ValueError("runtime handle executable path is invalid")
    return {
        "profile_key": handle.profile_key,
        "executable_path": path,
        "browser_revision": handle.browser_revision,
        "env": _safe_runtime_handle_environment(handle.env),
        "executable_sha256": handle.executable_sha256,
        "executable_size": handle.executable_size,
        "executable_mtime_ns": handle.executable_mtime_ns,
        "browser_flavor": handle.browser_flavor,
        "browser_version": handle.browser_version,
    }


def deserialize_runtime_handle(payload: object) -> RuntimeHandle:
    """Validate and reconstruct a RuntimeHandle received over private IPC."""

    if not isinstance(payload, dict) or set(payload) != _RUNTIME_HANDLE_FIELDS:
        raise ValueError("runtime handle fields are invalid")
    env = payload["env"]
    if not isinstance(env, dict) or any(
        not isinstance(name, str)
        or not isinstance(value, str)
        or name not in _RUNTIME_HANDLE_ENV_KEYS
        or not value
        or len(value) > MAX_ENV_VALUE_CHARS
        or "\x00" in value
        for name, value in env.items()
    ):
        raise ValueError("runtime handle environment is invalid")
    if not isinstance(payload["executable_path"], str):
        raise ValueError("runtime handle executable path is invalid")
    if not isinstance(payload["browser_revision"], str):
        raise ValueError("runtime handle browser revision is invalid")
    if not isinstance(payload["profile_key"], str):
        raise ValueError("runtime handle profile key is invalid")
    if not isinstance(payload["executable_sha256"], str):
        raise ValueError("runtime handle executable hash is invalid")
    if not isinstance(payload["browser_flavor"], str) or not isinstance(payload["browser_version"], str):
        raise ValueError("runtime handle browser metadata is invalid")
    if any(
        not isinstance(payload[field], int) or isinstance(payload[field], bool) or payload[field] < 0
        for field in ("executable_size", "executable_mtime_ns")
    ):
        raise ValueError("runtime handle executable metadata is invalid")
    handle = RuntimeHandle(
        profile_key=payload["profile_key"],
        executable_path=Path(payload["executable_path"]),
        browser_revision=payload["browser_revision"],
        env=env,
        executable_sha256=payload["executable_sha256"],
        executable_size=payload["executable_size"],
        executable_mtime_ns=payload["executable_mtime_ns"],
        browser_flavor=payload["browser_flavor"],
        browser_version=payload["browser_version"],
    )
    # Reuse the serializer's strict validation for the scalar metadata.
    serialize_runtime_handle(handle)
    return handle


def encode_fence_request(
    operation: str,
    payload: Mapping[str, object],
    *,
    deadline: float,
) -> bytes:
    if operation not in {"prepare", "validate"}:
        raise ValueError("runtime fence operation is invalid")
    key = payload.get("key")
    if not isinstance(key, str) or not _KEY_PATTERN.fullmatch(key):
        raise ValueError("runtime fence profile key is invalid")
    request: dict[str, object] = {
        "protocol": PROTOCOL_VERSION,
        "operation": operation,
        "deadline": float(deadline),
        "key": key,
    }
    if operation == "prepare":
        if set(payload) != {"key"}:
            raise ValueError("runtime fence request fields are invalid")
    else:
        if set(payload) != {"key", "handle"}:
            raise ValueError("runtime fence request fields are invalid")
        handle = payload["handle"]
        request["handle"] = serialize_runtime_handle(handle) if isinstance(handle, RuntimeHandle) else None
    if not math.isfinite(request["deadline"]):
        raise ValueError("runtime fence deadline is invalid")
    encoded = json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_IPC_BYTES:
        raise ValueError("runtime fence request is too large")
    return encoded


def _failure_response(
    stage: str,
    code: str,
    *,
    retryable: bool = False,
    http_status: int | None = None,
    attempts: int = 0,
) -> dict[str, object]:
    if stage not in _SAFE_STAGES:
        stage = "bootstrap"
    if code not in _SAFE_ERROR_CODES:
        code = "bootstrap_unknown"
    if not isinstance(http_status, int) or isinstance(http_status, bool) or not 100 <= http_status <= 599:
        http_status = None
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0 or attempts > 3:
        attempts = 0
    return {
        "protocol": PROTOCOL_VERSION,
        "ok": False,
        "error": {
            "stage": stage,
            "code": code,
            "retryable": bool(retryable),
            "http_status": http_status,
            "attempts": attempts,
        },
    }


def decode_fence_response(operation: str, result: object) -> object:
    """Parse a bounded ProcessResult without exposing child diagnostics."""

    if getattr(result, "timed_out", False):
        raise RuntimeSetupError("bootstrap", "bootstrap_timeout", retryable=True)
    stdout = getattr(result, "stdout", "")
    if not isinstance(stdout, str) or len(stdout.encode("utf-8", errors="replace")) > MAX_IPC_BYTES:
        raise RuntimeSetupError("bootstrap", "bootstrap_protocol_error")
    try:
        response = json.loads(stdout)
    except (json.JSONDecodeError, UnicodeError, TypeError):
        raise RuntimeSetupError("bootstrap", "bootstrap_protocol_error") from None
    if not isinstance(response, dict) or response.get("protocol") != PROTOCOL_VERSION or not isinstance(response.get("ok"), bool):
        raise RuntimeSetupError("bootstrap", "bootstrap_protocol_error")
    returncode = getattr(result, "returncode", 1)
    if response["ok"]:
        if returncode != 0 or set(response) != {"protocol", "ok", "result"}:
            raise RuntimeSetupError("bootstrap", "bootstrap_protocol_error")
        payload = response["result"]
        if not isinstance(payload, dict):
            raise RuntimeSetupError("bootstrap", "bootstrap_protocol_error")
        try:
            if operation == "prepare" and set(payload) == {"handle"}:
                return deserialize_runtime_handle(payload["handle"])
            if operation == "validate" and set(payload) == {"valid"} and isinstance(payload["valid"], bool):
                return payload["valid"]
        except (TypeError, ValueError, OSError):
            pass
        raise RuntimeSetupError("bootstrap", "bootstrap_protocol_error")
    if returncode == 0 or set(response) != {"protocol", "ok", "error"}:
        raise RuntimeSetupError("bootstrap", "bootstrap_protocol_error")
    error = response["error"]
    if not isinstance(error, dict) or set(error) != {"stage", "code", "retryable", "http_status", "attempts"}:
        raise RuntimeSetupError("bootstrap", "bootstrap_protocol_error")
    stage, code = error["stage"], error["code"]
    if stage not in _SAFE_STAGES or code not in _SAFE_ERROR_CODES or not isinstance(error["retryable"], bool):
        raise RuntimeSetupError("bootstrap", "bootstrap_protocol_error")
    status = error["http_status"]
    attempts = error["attempts"]
    if status is not None and (not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599):
        raise RuntimeSetupError("bootstrap", "bootstrap_protocol_error")
    if not isinstance(attempts, int) or isinstance(attempts, bool) or not 0 <= attempts <= 3:
        raise RuntimeSetupError("bootstrap", "bootstrap_protocol_error")
    exception = RuntimeSetupError(stage, code, retryable=error["retryable"], http_status=status)
    exception.attempts = attempts
    raise exception


def _enable_subreaper() -> None:
    """Enable Linux child-subreaper behavior or fail closed before work starts."""

    if not sys.platform.startswith("linux"):
        raise RuntimeSetupError("bootstrap", "subreaper_unavailable")
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        prctl.restype = ctypes.c_int
        status = prctl(36, 1, 0, 0, 0)  # PR_SET_CHILD_SUBREAPER
    except (AttributeError, OSError, TypeError, ValueError):
        raise RuntimeSetupError("bootstrap", "subreaper_unavailable") from None
    if status != 0:
        raise RuntimeSetupError("bootstrap", "subreaper_unavailable")


def _reap_available_children() -> None:
    while True:
        try:
            child_pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        except OSError:
            return
        if child_pid == 0:
            return


def _operation_children() -> list[tuple[int, str]]:
    from .browser_runtime import _linux_descendant_processes

    return _linux_descendant_processes(os.getpid())


def _cleanup_operation_children(*, deadline: float) -> tuple[bool, bool]:
    """Terminate and reap any process left by the synchronous operation.

    Returns ``(had_children, all_closed_and_reaped)``. A request that left any
    descendants is never considered ready, even if this cleanup succeeds.
    """

    try:
        from .browser_runtime import _signal_linux_descendants
    except Exception:
        return True, False
    _reap_available_children()
    descendants = _operation_children()
    had_children = bool(descendants)
    if not had_children:
        return False, True

    _signal_linux_descendants(descendants, signal.SIGTERM)
    known = {pid: identity for pid, identity in descendants}
    term_deadline = min(deadline, time.monotonic() + 0.35)
    while time.monotonic() < term_deadline:
        _reap_available_children()
        latest = _operation_children()
        known.update(latest)
        if not latest:
            return True, True
        time.sleep(min(0.025, max(0.0, term_deadline - time.monotonic())))

    latest = _operation_children()
    known.update(latest)
    _signal_linux_descendants(list(known.items()), signal.SIGKILL)
    kill_deadline = min(deadline, time.monotonic() + SUBREAPER_CLEANUP_SECONDS)
    while time.monotonic() < kill_deadline:
        _reap_available_children()
        latest = _operation_children()
        newly_discovered = [
            (pid, identity)
            for pid, identity in latest
            if known.get(pid) != identity
        ]
        known.update(latest)
        if newly_discovered:
            # A TERM-resistant process may fork after the first SIGKILL
            # snapshot. Keep killing identity-checked newcomers until the
            # tree is empty or the bounded cleanup budget expires.
            _signal_linux_descendants(newly_discovered, signal.SIGKILL)
        if not latest:
            return True, True
        time.sleep(min(0.025, max(0.0, kill_deadline - time.monotonic())))
    _reap_available_children()
    latest = _operation_children()
    newly_discovered = [
        (pid, identity)
        for pid, identity in latest
        if known.get(pid) != identity
    ]
    if newly_discovered:
        _signal_linux_descendants(newly_discovered, signal.SIGKILL)
    _reap_available_children()
    return True, not _operation_children()


class _DiscardOutput:
    """Sink incidental operation output instead of buffering secrets or logs."""

    encoding = "utf-8"

    def write(self, value: str) -> int:
        return len(value)

    def flush(self) -> None:
        return None


def _validate_request(request: object) -> tuple[str, str, float, RuntimeHandle | None]:
    if not isinstance(request, dict):
        raise ValueError("invalid request")
    operation = request.get("operation")
    expected_fields = {"protocol", "operation", "deadline", "key"}
    if operation == "validate":
        expected_fields.add("handle")
    if set(request) != expected_fields or request.get("protocol") != PROTOCOL_VERSION:
        raise ValueError("invalid request fields")
    key = request.get("key")
    if operation not in {"prepare", "validate"} or not isinstance(key, str) or not _KEY_PATTERN.fullmatch(key):
        raise ValueError("invalid operation key")
    deadline = request.get("deadline")
    if not isinstance(deadline, (int, float)) or isinstance(deadline, bool) or not math.isfinite(deadline):
        raise ValueError("invalid operation deadline")
    if deadline <= time.monotonic():
        raise RuntimeSetupError("bootstrap", "bootstrap_timeout", retryable=True)
    handle: RuntimeHandle | None = None
    if operation == "validate" and request["handle"] is not None:
        handle = deserialize_runtime_handle(request["handle"])
        if handle.profile_key != key:
            raise ValueError("invalid handle profile")
    return operation, key, float(deadline), handle


def execute_request(
    request: object,
    *,
    bootstrap_factory: Callable[..., object] | None = None,
    setup_subreaper: Callable[[], None] | None = None,
) -> dict[str, object]:
    """Execute one request and return only an allowlisted protocol response."""

    setup = setup_subreaper or _enable_subreaper
    try:
        setup()
    except RuntimeSetupError as exc:
        return _failure_response(exc.stage, exc.code, retryable=exc.retryable, http_status=exc.http_status, attempts=exc.attempts)
    except BaseException:
        return _failure_response("bootstrap", "subreaper_unavailable")

    operation = "prepare"
    deadline = time.monotonic()
    operation_error: RuntimeSetupError | None = None
    operation_result: object = None
    try:
        operation, key, deadline, handle = _validate_request(request)
    except RuntimeSetupError as exc:
        return _failure_response(exc.stage, exc.code, retryable=exc.retryable, http_status=exc.http_status, attempts=exc.attempts)
    except BaseException:
        return _failure_response("bootstrap", "bootstrap_protocol_error")

    try:
        with contextlib.redirect_stdout(_DiscardOutput()), contextlib.redirect_stderr(_DiscardOutput()):
            if time.monotonic() >= deadline:
                raise RuntimeSetupError("bootstrap", "bootstrap_timeout", retryable=True)
            if bootstrap_factory is None:
                from .browser_runtime import BrowserRuntimeBootstrap

                bootstrap_factory = BrowserRuntimeBootstrap
            bootstrap = bootstrap_factory()
            if operation == "prepare":
                operation_result = bootstrap.prepare_in_process(key, deadline)
            else:
                operation_result = bootstrap.validate_in_process(handle, deadline)
    except RuntimeSetupError as exc:
        operation_error = exc
    except BaseException:
        operation_error = RuntimeSetupError("bootstrap", "bootstrap_unknown")

    had_children, children_clean = _cleanup_operation_children(deadline=deadline)
    if not children_clean:
        return _failure_response("bootstrap", "operation_cleanup_failed", retryable=True)
    if had_children:
        return _failure_response("bootstrap", "operation_children_leaked", retryable=True)
    if operation_error is not None:
        return _failure_response(
            operation_error.stage,
            operation_error.code,
            retryable=operation_error.retryable,
            http_status=operation_error.http_status,
            attempts=operation_error.attempts,
        )

    try:
        if operation == "prepare":
            serialized = serialize_runtime_handle(operation_result)
            return {"protocol": PROTOCOL_VERSION, "ok": True, "result": {"handle": serialized}}
        if not isinstance(operation_result, bool):
            return _failure_response("validate", "bootstrap_unknown")
        return {"protocol": PROTOCOL_VERSION, "ok": True, "result": {"valid": operation_result}}
    except (TypeError, ValueError, OSError):
        return _failure_response("bootstrap", "bootstrap_unknown")


def main() -> int:
    try:
        raw_request = sys.stdin.buffer.read(MAX_IPC_BYTES + 1)
    except (AttributeError, OSError):
        raw_request = b""
    if len(raw_request) > MAX_IPC_BYTES:
        response = _failure_response("bootstrap", "bootstrap_protocol_error")
    else:
        try:
            request = json.loads(raw_request.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            request = None
        response = execute_request(request)
    try:
        encoded = json.dumps(response, sort_keys=True, separators=(",", ":"))
        output = encoded.encode("utf-8")
        if len(output) > MAX_IPC_BYTES:
            output = json.dumps(_failure_response("bootstrap", "bootstrap_protocol_error"), separators=(",", ":")).encode("utf-8")
        sys.__stdout__.buffer.write(output + b"\n")
        sys.__stdout__.buffer.flush()
    except (AttributeError, OSError, TypeError, ValueError):
        return 2
    return 0 if response.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
