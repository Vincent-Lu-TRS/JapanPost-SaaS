"""Pinned browser profile, bounded installer and disposable Chromium probe."""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import logging
import os
import platform
import re
import selectors
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, Mapping

from .browser_bootstrap import RuntimeHandle, RuntimeManager, RuntimeSetupError
from .runtime_fence import (
    MAX_IPC_BYTES,
    build_child_environment,
    decode_fence_response,
    encode_fence_request,
)
from .playwright_runtime import (
    DEFAULT_ASSET_ROOT,
    REQUIRED_RUNTIME_LIBRARY_NAMES,
    _file_sha256,
    _library_available,
    _load_verified_assets,
    _platform_fingerprint,
    _vendor_libraries_ready,
    prepare_playwright_runtime,
)
from safe_logging import log_runtime_environment


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = Path(__file__).with_name("runtime_profile.json")
PROBE_PREFIX = "JPPOST_BROWSER_PROBE="
OUTPUT_CAPTURE_MAX_BYTES = 256 * 1024
_OUTPUT_CAPTURE_HEAD_BYTES = 32 * 1024
_OUTPUT_CAPTURE_MARKER = b"\n...[output truncated]...\n"
_OUTPUT_READ_CHUNK_BYTES = 64 * 1024
_ALLOWED_RUNTIME_CODES = {
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
}

_PROBE_SCRIPT = r'''
import ctypes, json, os, sys
if sys.platform.startswith("linux"):
    try:
        # If the Playwright Node driver exits unexpectedly, keep its detached
        # Chromium child adopted by this disposable probe process so timeout
        # cleanup can still discover and terminate it.
        ctypes.CDLL(None).prctl(36, 1, 0, 0, 0)  # PR_SET_CHILD_SUBREAPER
    except Exception:
        pass
from pathlib import Path
from playwright.sync_api import sync_playwright

def close_quietly(resource):
    if resource is not None:
        try:
            resource.close()
        except Exception:
            pass

try:
    browser = context = page = None
    with sync_playwright() as p:
        root = Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"]).resolve()
        executable = Path(p.chromium.executable_path).resolve()
        if not executable.is_relative_to(root) or not executable.is_file():
            raise RuntimeError("browser executable profile mismatch")
        launch_args = json.loads(os.environ["JPPOST_PROBE_LAUNCH_ARGS"])
        fallback_args = json.loads(os.environ["JPPOST_PROBE_FALLBACK_ARGS"])
        expected_version = os.environ["JPPOST_PROBE_EXPECTED_VERSION"]
        expected_title = os.environ["JPPOST_PROBE_TITLE"]
        last_error = None
        for args in (launch_args, list(dict.fromkeys(launch_args + fallback_args))):
            try:
                browser = p.chromium.launch(
                    headless=True,
                    args=args,
                    env=dict(os.environ),
                    executable_path=str(executable),
                )
                context = browser.new_context()
                page = context.new_page()
                page.set_content("<html><head><title>JPPOST runtime probe</title></head><body>ready</body></html>")
                if page.title() != expected_title or browser.version != expected_version:
                    raise RuntimeError("browser health check did not match pinned profile")
                print("JPPOST_BROWSER_PROBE=" + json.dumps({
                    "executable": str(executable),
                    "version": browser.version,
                }, separators=(",", ":")))
                raise SystemExit(0)
            except SystemExit:
                raise
            except Exception as exc:
                last_error = exc
                text = str(exc).lower()
                retryable = type(exc).__name__ == "TargetClosedError" or any(marker in text for marker in (
                    "target page, context or browser has been closed",
                    "browser has been closed", "browser process closed",
                ))
                close_quietly(page); page = None
                close_quietly(context); context = None
                close_quietly(browser); browser = None
                if not retryable or args is not launch_args:
                    break
        if last_error is not None:
            raise SystemExit(41)
        raise SystemExit(42)
except SystemExit:
    raise
except Exception:
    raise SystemExit(43)
finally:
    close_quietly(page)
    close_quietly(context)
    close_quietly(browser)
'''


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    observed_http_statuses: tuple[int, ...] = ()
    retry_after_values: tuple[str, ...] = ()


def _decode_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _linux_process_start_time(pid: int) -> str | None:
    """Return /proc identity used to avoid signaling a recycled PID."""

    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        fields_after_comm = stat.rsplit(")", 1)[1].split()
        return fields_after_comm[19]
    except (OSError, UnicodeError, IndexError):
        return None


def _linux_process_state(pid: int) -> str | None:
    """Read the process state used to confirm a stopped cleanup snapshot."""

    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        return stat.rsplit(")", 1)[1].split()[0]
    except (OSError, UnicodeError, IndexError):
        return None


def _open_linux_pidfd(pid: int, *, expected_start_time: str | None) -> int | None:
    """Pin a Linux process identity so its PID cannot be recycled during cleanup."""

    if not sys.platform.startswith("linux") or expected_start_time is None:
        return None
    pidfd_open = getattr(os, "pidfd_open", None)
    if not callable(pidfd_open):
        return None
    try:
        pidfd = pidfd_open(pid, 0)
    except OSError:
        return None
    if _linux_process_start_time(pid) == expected_start_time:
        return pidfd
    try:
        os.close(pidfd)
    except OSError:
        pass
    return None


def _linux_descendant_processes(root_pid: int) -> list[tuple[int, str]]:
    """Snapshot recursive child PIDs, including detached Playwright browsers."""

    pending = [root_pid]
    seen = {root_pid}
    descendants: list[tuple[int, str]] = []
    while pending:
        parent_pid = pending.pop()
        children_path = Path(f"/proc/{parent_pid}/task/{parent_pid}/children")
        try:
            child_pids = [int(value) for value in children_path.read_text(encoding="ascii").split()]
        except (OSError, UnicodeError, ValueError):
            continue
        for child_pid in child_pids:
            if child_pid in seen:
                continue
            seen.add(child_pid)
            start_time = _linux_process_start_time(child_pid)
            if start_time is None:
                continue
            descendants.append((child_pid, start_time))
            pending.append(child_pid)
    return descendants


def _signal_linux_descendants(
    descendants: list[tuple[int, str]],
    signal_number: int,
) -> None:
    for pid, start_time in reversed(descendants):
        if _linux_process_start_time(pid) != start_time:
            continue
        try:
            os.kill(pid, signal_number)
        except (ProcessLookupError, PermissionError):
            pass


class _BoundedOutputCapture:
    """Keep output prefix and suffix so status lines survive huge response bodies."""

    def __init__(self) -> None:
        self.head = bytearray()
        self.tail = bytearray()
        self.truncated = False
        self.observed_http_statuses: list[int] = []
        self.retry_after_values: list[str] = []
        self._classifier_tail = ""
        self._retry_after_line_tail = ""
        self.tail_limit = (
            OUTPUT_CAPTURE_MAX_BYTES
            - _OUTPUT_CAPTURE_HEAD_BYTES
            - len(_OUTPUT_CAPTURE_MARKER)
        )

    def append(self, chunk: bytes) -> None:
        classifier_text = self._classifier_tail + chunk.decode("utf-8", errors="replace")
        status_pattern = (
            r"\bserver returned code\s*[=: ]\s*(\d{3})\b"
            r"|\bHTTP(?:\s+Error)?\s*[: ]\s*(\d{3})\b"
            r"|\bstatus\s*[=: ]\s*(\d{3})\b"
        )
        for match in re.finditer(status_pattern, classifier_text, re.I):
            status = int(next(value for value in match.groups() if value))
            if status not in self.observed_http_statuses and len(self.observed_http_statuses) < 32:
                self.observed_http_statuses.append(status)
        self._classifier_tail = classifier_text[-512:]
        retry_after_text = self._retry_after_line_tail + chunk.decode("utf-8", errors="replace")
        lines = retry_after_text.splitlines(keepends=True)
        self._retry_after_line_tail = ""
        for index, line in enumerate(lines):
            if line.endswith(("\n", "\r")):
                self._remember_retry_after_line(line.rstrip("\r\n"))
            elif index == len(lines) - 1:
                # A header split between pipe reads is incomplete. Do not
                # classify it until the newline or EOF arrives.
                if len(line) <= 512:
                    self._retry_after_line_tail = line

        head_room = _OUTPUT_CAPTURE_HEAD_BYTES - len(self.head)
        if head_room > 0:
            head_chunk, chunk = chunk[:head_room], chunk[head_room:]
            self.head.extend(head_chunk)
        if not chunk:
            return
        overflow = len(self.tail) + len(chunk) - self.tail_limit
        if overflow > 0:
            self.truncated = True
            del self.tail[:min(overflow, len(self.tail))]
            if len(chunk) > self.tail_limit:
                chunk = chunk[-self.tail_limit:]
        self.tail.extend(chunk)

    def _remember_retry_after_line(self, line: str) -> None:
        match = re.match(r"(?i)^\s*retry-after\s*:\s*(.*?)\s*$", line)
        if not match:
            return
        value = match.group(1).strip()[:256]
        if value and value not in self.retry_after_values and len(self.retry_after_values) < 8:
            self.retry_after_values.append(value)

    def finalize_classifier_metadata(self) -> None:
        """Process a final unterminated header only after its stream is closed."""

        if self._retry_after_line_tail:
            self._remember_retry_after_line(self._retry_after_line_tail)
            self._retry_after_line_tail = ""

    def to_bytes(self) -> bytes:
        if not self.truncated:
            return bytes(self.head + self.tail)
        return bytes(self.head) + _OUTPUT_CAPTURE_MARKER + bytes(self.tail)


def _terminate_process_tree(
    process: subprocess.Popen,
    *,
    root_start_time: str | None = None,
    root_pidfd: int | None = None,
) -> None:
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
    else:
        root_start_time = root_start_time or _linux_process_start_time(process.pid)

        def root_identity_is_pinned() -> bool:
            if root_pidfd is not None:
                return True
            return (
                root_start_time is not None
                and getattr(process, "returncode", None) is None
                and _linux_process_start_time(process.pid) == root_start_time
            )

        def root_is_stopped_or_gone() -> bool:
            if getattr(process, "returncode", None) is not None:
                return True
            if root_start_time is None or _linux_process_start_time(process.pid) != root_start_time:
                return True
            return _linux_process_state(process.pid) in {"T", "t", "Z"}

        # Stop the operation root first, then repeatedly discover and stop its
        # descendants before killing anything. This closes the fork-after-first-
        # snapshot race for detached Playwright/browser children. Known child
        # identities are retained so PID reuse cannot redirect cleanup signals.
        # A pidfd pins the process-group leader's numeric ID across poll()/wait();
        # without one, only signal or inspect a still-unreaped child whose /proc
        # start time matches the identity recorded immediately after Popen.
        known: dict[int, str] = {}
        if root_identity_is_pinned():
            try:
                os.kill(process.pid, signal.SIGSTOP)
            except ProcessLookupError:
                pass
            except OSError:
                pass
        stop_deadline = time.monotonic() + 0.25
        while True:
            latest = _linux_descendant_processes(process.pid) if root_identity_is_pinned() else []
            new_descendants = [item for item in latest if known.get(item[0]) != item[1]]
            known.update(latest)
            _signal_linux_descendants(new_descendants, signal.SIGSTOP)

            root_stopped = root_is_stopped_or_gone()
            descendants_stopped = all(
                _linux_process_start_time(pid) != identity
                or _linux_process_state(pid) in {"T", "t", "Z"}
                for pid, identity in known.items()
            )
            if root_stopped and descendants_stopped and not new_descendants:
                break
            if time.monotonic() >= stop_deadline:
                break
            time.sleep(0.01)

        # Include one final descendant snapshot before killing the root. Any
        # process still alive in the known tree receives SIGKILL before its
        # subreaper/root, including children that called setsid().
        latest_descendants = _linux_descendant_processes(process.pid) if root_identity_is_pinned() else []
        known.update(latest_descendants)
        _signal_linux_descendants(list(known.items()), signal.SIGKILL)
        if root_identity_is_pinned():
            try:
                # The pidfd keeps the original PGID number reserved even if
                # process.poll() already reaped its leader. Without a pidfd,
                # the matching unreaped child pins that number until this call.
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                if getattr(process, "returncode", None) is None and root_identity_is_pinned():
                    try:
                        process.kill()
                    except OSError:
                        pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        except (OSError, ChildProcessError):
            pass


def _drain_bounded(stream, destination: _BoundedOutputCapture) -> None:
    """Drain a child pipe continuously while retaining a bounded head and tail."""

    try:
        while True:
            chunk = stream.read(_OUTPUT_READ_CHUNK_BYTES)
            if not chunk:
                return
            destination.append(chunk)
    except (OSError, ValueError):
        # The process/pipe can close while timeout cleanup is in progress.
        return


def _close_process_pipes(process: subprocess.Popen) -> None:
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def _captured_process_result(
    returncode: int,
    captures: Mapping[str, _BoundedOutputCapture],
    *,
    timed_out: bool = False,
    stderr_override: str | None = None,
) -> ProcessResult:
    for capture in captures.values():
        capture.finalize_classifier_metadata()
    statuses = tuple(dict.fromkeys(
        status
        for capture in captures.values()
        for status in capture.observed_http_statuses
    ))
    retry_after_values = tuple(dict.fromkeys(
        value
        for capture in captures.values()
        for value in capture.retry_after_values
    ))
    return ProcessResult(
        returncode=returncode,
        stdout=_decode_output(captures["stdout"].to_bytes()),
        stderr=stderr_override if stderr_override is not None else _decode_output(captures["stderr"].to_bytes()),
        timed_out=timed_out,
        observed_http_statuses=statuses,
        retry_after_values=retry_after_values,
    )


def _run_bounded_process_posix(
    process: subprocess.Popen,
    command: list[str],
    timeout: float,
    *,
    deadline: float | None = None,
    root_start_time: str | None = None,
    root_pidfd: int | None = None,
) -> ProcessResult:
    captures = {
        "stdout": _BoundedOutputCapture(),
        "stderr": _BoundedOutputCapture(),
    }
    selector = selectors.DefaultSelector()
    try:
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        deadline = deadline if deadline is not None else time.monotonic() + timeout
        while True:
            returncode = process.poll()
            if returncode is not None and not selector.get_map():
                return _captured_process_result(returncode or 0, captures)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            if selector.get_map():
                events = selector.select(min(remaining, 0.1))
                for key, _mask in events:
                    try:
                        chunk = os.read(key.fileobj.fileno(), _OUTPUT_READ_CHUNK_BYTES)
                    except BlockingIOError:
                        continue
                    except OSError:
                        chunk = b""
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        captures[key.data].append(chunk)
            else:
                try:
                    process.wait(timeout=min(remaining, 0.1))
                except subprocess.TimeoutExpired:
                    pass
    except subprocess.TimeoutExpired:
        _terminate_process_tree(
            process,
            root_start_time=root_start_time,
            root_pidfd=root_pidfd,
        )
        cleanup_deadline = time.monotonic() + 2.0
        while selector.get_map() and time.monotonic() < cleanup_deadline:
            for key, _mask in selector.select(min(0.1, cleanup_deadline - time.monotonic())):
                try:
                    chunk = os.read(key.fileobj.fileno(), _OUTPUT_READ_CHUNK_BYTES)
                except (BlockingIOError, OSError):
                    chunk = b""
                if not chunk:
                    selector.unregister(key.fileobj)
                else:
                    captures[key.data].append(chunk)
        return _captured_process_result(124, captures, timed_out=True)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        _terminate_process_tree(
            process,
            root_start_time=root_start_time,
            root_pidfd=root_pidfd,
        )
        return _captured_process_result(127, captures, stderr_override=type(exc).__name__)
    finally:
        selector.close()
        _close_process_pipes(process)


def _run_bounded_process_threaded(
    process: subprocess.Popen,
    command: list[str],
    timeout: float,
    *,
    deadline: float | None = None,
    root_start_time: str | None = None,
    root_pidfd: int | None = None,
) -> ProcessResult:
    captures = {"stdout": _BoundedOutputCapture(), "stderr": _BoundedOutputCapture()}
    readers = [
        threading.Thread(target=_drain_bounded, args=(process.stdout, captures["stdout"]), daemon=True),
        threading.Thread(target=_drain_bounded, args=(process.stderr, captures["stderr"]), daemon=True),
    ]
    started_readers = []
    try:
        for reader in readers:
            reader.start()
            started_readers.append(reader)
    except RuntimeError as exc:
        _terminate_process_tree(
            process,
            root_start_time=root_start_time,
            root_pidfd=root_pidfd,
        )
        for reader in started_readers:
            reader.join(timeout=2)
        if all(not reader.is_alive() for reader in started_readers):
            _close_process_pipes(process)
        return ProcessResult(127, stderr=type(exc).__name__)

    deadline = deadline if deadline is not None else time.monotonic() + timeout
    try:
        try:
            returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
            for reader in readers:
                reader.join(timeout=max(0.0, deadline - time.monotonic()))
            if any(reader.is_alive() for reader in readers):
                raise subprocess.TimeoutExpired(command, timeout)
            return _captured_process_result(returncode or 0, captures)
        except subprocess.TimeoutExpired:
            _terminate_process_tree(
                process,
                root_start_time=root_start_time,
                root_pidfd=root_pidfd,
            )
            for reader in readers:
                reader.join(timeout=2)
            return _captured_process_result(124, captures, timed_out=True)
        except (OSError, subprocess.SubprocessError) as exc:
            _terminate_process_tree(
                process,
                root_start_time=root_start_time,
                root_pidfd=root_pidfd,
            )
            for reader in readers:
                reader.join(timeout=2)
            return _captured_process_result(127, captures, stderr_override=type(exc).__name__)
    finally:
        # Closing a BufferedReader from another thread may block on its lock.
        # If a descendant still owns the pipe, let its daemon reader close the
        # handle naturally instead of breaching this subprocess deadline.
        if all(not reader.is_alive() for reader in readers):
            _close_process_pipes(process)


def _run_bounded_process(
    command: list[str],
    *,
    env: Mapping[str, str],
    timeout: float,
    input_data: bytes | None = None,
    deadline: float | None = None,
    cwd: str | Path | None = None,
) -> ProcessResult:
    process_deadline = deadline if deadline is not None else time.monotonic() + timeout
    if timeout <= 0 or process_deadline <= time.monotonic():
        return ProcessResult(124, timed_out=True)
    if input_data is not None and len(input_data) > MAX_IPC_BYTES:
        return ProcessResult(127, stderr="input_too_large")
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(env),
            cwd=str(cwd) if cwd is not None else None,
            text=False,
            bufsize=0,
            start_new_session=os.name != "nt",
            creationflags=creationflags,
        )
    except OSError as exc:
        return ProcessResult(127, stderr=type(exc).__name__)

    root_start_time = _linux_process_start_time(process.pid) if sys.platform.startswith("linux") else None
    root_pidfd = _open_linux_pidfd(
        process.pid,
        expected_start_time=root_start_time,
    )
    try:
        return _run_bounded_process_started(
            process,
            command,
            timeout=timeout,
            input_data=input_data,
            deadline=process_deadline,
            root_start_time=root_start_time,
            root_pidfd=root_pidfd,
        )
    finally:
        if root_pidfd is not None:
            try:
                os.close(root_pidfd)
            except OSError:
                pass


def _run_bounded_process_started(
    process: subprocess.Popen,
    command: list[str],
    *,
    timeout: float,
    input_data: bytes | None,
    deadline: float,
    root_start_time: str | None,
    root_pidfd: int | None,
) -> ProcessResult:
    if input_data is not None:
        try:
            if process.stdin is not None:
                if os.name != "nt":
                    os.set_blocking(process.stdin.fileno(), False)
                    selector = selectors.DefaultSelector()
                    try:
                        selector.register(process.stdin, selectors.EVENT_WRITE)
                        offset = 0
                        while offset < len(input_data):
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                _terminate_process_tree(
                                    process,
                                    root_start_time=root_start_time,
                                    root_pidfd=root_pidfd,
                                )
                                _close_process_pipes(process)
                                return ProcessResult(124, timed_out=True)
                            if not selector.select(min(remaining, 0.1)):
                                continue
                            try:
                                offset += os.write(process.stdin.fileno(), input_data[offset:])
                            except BlockingIOError:
                                continue
                    finally:
                        selector.close()
                else:
                    process.stdin.write(input_data)
                    process.stdin.flush()
        except (BrokenPipeError, OSError):
            # A child that exits before consuming the small request body will
            # be classified from its bounded protocol response below.
            pass
        finally:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        _terminate_process_tree(
            process,
            root_start_time=root_start_time,
            root_pidfd=root_pidfd,
        )
        _close_process_pipes(process)
        return ProcessResult(124, timed_out=True)
    if os.name == "nt":
        return _run_bounded_process_threaded(
            process,
            command,
            remaining,
            deadline=deadline,
            root_start_time=root_start_time,
            root_pidfd=root_pidfd,
        )
    return _run_bounded_process_posix(
        process,
        command,
        remaining,
        deadline=deadline,
        root_start_time=root_start_time,
        root_pidfd=root_pidfd,
    )


_RUNTIME_FENCE_CLEANUP_RESERVE_SECONDS = 5.0


def _run_runtime_fence(
    operation: str,
    payload: Mapping[str, object],
    *,
    deadline: float,
) -> object:
    """Run one prepare/validate operation in an isolated request child."""

    try:
        request = encode_fence_request(operation, payload, deadline=deadline)
    except (TypeError, ValueError, OverflowError):
        raise RuntimeSetupError("bootstrap", "bootstrap_protocol_error") from None
    process_deadline = deadline - _RUNTIME_FENCE_CLEANUP_RESERVE_SECONDS
    timeout = min(
        120.0,
        process_deadline - time.monotonic(),
    )
    if timeout <= 0:
        raise RuntimeSetupError("bootstrap", "bootstrap_timeout", retryable=True)
    try:
        result = _run_bounded_process(
            [sys.executable, "-m", "bot.runtime_fence"],
            env=build_child_environment(os.environ),
            timeout=timeout,
            input_data=request,
            deadline=process_deadline,
            cwd=PROJECT_ROOT,
        )
    except BaseException:
        raise RuntimeSetupError("bootstrap", "bootstrap_unknown") from None
    return decode_fence_response(operation, result)


def load_runtime_profile(path: str | Path = PROFILE_PATH) -> dict[str, object]:
    profile_path = Path(path)
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeSetupError("profile", "profile_unsupported") from exc
    if not isinstance(profile, dict) or profile.get("format") != 1:
        raise RuntimeSetupError("profile", "profile_unsupported")
    if profile.get("browser") != "chromium" or profile.get("headless") is not True:
        raise RuntimeSetupError("profile", "profile_unsupported")
    browser_root = profile.get("browser_root")
    if (
        not isinstance(browser_root, str)
        or not (Path(browser_root).is_absolute() or browser_root.startswith("/"))
        or ".." in Path(browser_root).parts
    ):
        raise RuntimeSetupError("profile", "profile_unsupported")
    for field, minimum, maximum in (
        ("prepare_timeout_seconds", 10, 120),
        ("install_attempt_timeout_seconds", 1, 45),
        ("install_max_attempts", 1, 3),
        ("probe_timeout_seconds", 1, 60),
    ):
        value = profile.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
            raise RuntimeSetupError("profile", "profile_unsupported")
    backoff = profile.get("install_retry_backoff_seconds")
    if not isinstance(backoff, list) or len(backoff) != 2 or any(
        not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 10
        for value in backoff
    ):
        raise RuntimeSetupError("profile", "profile_unsupported")
    for field in ("launch_args", "fallback_args"):
        value = profile.get(field)
        if not isinstance(value, list) or any(not isinstance(item, str) or not item.startswith("-") for item in value):
            raise RuntimeSetupError("profile", "profile_unsupported")
    for field in ("probe_title", "launch_profile"):
        if not isinstance(profile.get(field), str) or not profile[field]:
            raise RuntimeSetupError("profile", "profile_unsupported")
    compatibility = profile.get("compatibility")
    supported_os = {
        "debian": ["12", "13"],
        "ubuntu": ["22.04", "24.04", "26.04"],
    }
    if (
        not isinstance(compatibility, dict)
        or compatibility.get("system") != "linux"
        or compatibility.get("machine") != "x86_64"
        or compatibility.get("python_major") != 3
        or compatibility.get("python_minor") != 12
        or compatibility.get("supported_os") != supported_os
        or compatibility.get("libc_name") != "glibc"
        or compatibility.get("minimum_libc_version") != "2.35"
    ):
        raise RuntimeSetupError("profile", "profile_unsupported")
    return profile


def _playwright_browser_metadata() -> tuple[str, str]:
    spec = importlib.util.find_spec("playwright")
    if spec is None or spec.origin is None:
        raise RuntimeSetupError("browser_profile", "browser_install")
    browsers_file = Path(spec.origin).parent / "driver" / "package" / "browsers.json"
    try:
        data = json.loads(browsers_file.read_text(encoding="utf-8"))
        entry = next(
            item for item in data["browsers"]
            if item.get("name") == "chromium" and item.get("browserVersion")
        )
        return str(entry["revision"]), str(entry["browserVersion"])
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, StopIteration, TypeError) as exc:
        raise RuntimeSetupError("browser_profile", "browser_install") from exc


def _release_digest(project_root: Path) -> str:
    digest = hashlib.sha256()
    for relative in (
        "app.py",
        "bot/automation.py",
        "bot/browser_bootstrap.py",
        "bot/browser_runtime.py",
        "bot/playwright_runtime.py",
        "bot/runtime_fence.py",
        "bot/runtime_profile.json",
        "requirements.txt",
    ):
        path = project_root / relative
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise RuntimeSetupError("profile", "profile_unsupported") from exc
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(content).digest())
    return digest.hexdigest()


def _manifest_digest(asset_root: Path) -> str:
    try:
        return hashlib.sha256((asset_root / "manifest.json").read_bytes()).hexdigest()
    except OSError:
        return "missing"


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _http_status(text: str) -> int | None:
    pattern = (
        r"\bserver returned code\s*[=: ]\s*(\d{3})\b"
        r"|\bHTTP(?:\s+Error)?\s*[: ]\s*(\d{3})\b"
        r"|\bstatus\s*[=: ]\s*(\d{3})\b"
    )
    matches = re.findall(pattern, text, re.I)
    statuses = [int(value) for match in matches for value in match if value]
    # Playwright may report multiple mirror/redirect failures in one message.
    # Prefer a retryable response if any endpoint reported one.
    return next(
        (status for status in statuses if status == 429 or status == 408 or 500 <= status <= 599),
        statuses[0] if statuses else None,
    )


def _retry_after_seconds(text: str) -> float | None:
    match = re.search(r"(?im)^\s*retry-after\s*:\s*([^\r\n]+)", text)
    if not match:
        return None
    value = match.group(1).strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _classify_install_failure(result: ProcessResult) -> tuple[str, int | None, bool, float | None]:
    output = f"{result.stdout}\n{result.stderr}"
    status_candidates = list(result.observed_http_statuses)
    parsed_status = _http_status(output)
    if not status_candidates and parsed_status is not None:
        status_candidates.append(parsed_status)
    status = (
        429
        if 429 in status_candidates
        else next(
            (value for value in status_candidates if value == 408 or 500 <= value <= 599),
            status_candidates[0] if status_candidates else None,
        )
    )
    if status == 429:
        retry_after_candidates = [
            _retry_after_seconds(f"Retry-After: {value}")
            for value in result.retry_after_values
        ]
        retry_after = max(
            (value for value in retry_after_candidates if value is not None),
            default=_retry_after_seconds(output),
        )
        return "browser_download", status, True, retry_after
    if status == 408 or status is not None and 500 <= status <= 599:
        return "browser_download", status, True, None
    if status in {401, 403, 404}:
        return "browser_install", status, False, None
    if result.timed_out or any(
        marker in output.lower()
        for marker in ("econnreset", "etimedout", "enotfound", "eai_again", "temporary failure", "socket hang up")
    ):
        return "browser_download", status, True, None
    if result.returncode == 127:
        return "browser_install", status, False, None
    return "browser_install_unknown", status, False, None


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeSetupError("bootstrap", "bootstrap_timeout", retryable=True)
    return remaining


_SAFE_PLATFORM_LABEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")


def _os_release_fields(path: str | Path = "/etc/os-release") -> dict[str, str]:
    """Read only ID/VERSION_ID from the standard OS label file."""

    try:
        with Path(path).open("r", encoding="utf-8", errors="replace") as stream:
            content = stream.read(64 * 1024)
    except OSError:
        return {"os_id": "unknown", "os_version": "unknown"}

    values: dict[str, str] = {}
    for line in content.splitlines():
        name, separator, raw_value = line.partition("=")
        if not separator or name not in {"ID", "VERSION_ID"}:
            continue
        value = raw_value.strip().strip("\"'").lower()
        if _SAFE_PLATFORM_LABEL_RE.fullmatch(value):
            values["os_id" if name == "ID" else "os_version"] = value
    return {
        "os_id": values.get("os_id", "unknown"),
        "os_version": values.get("os_version", "unknown"),
    }


def runtime_environment_fingerprint(
    *, os_release_path: str | Path = "/etc/os-release",
) -> dict[str, object]:
    """Return safe, bounded environment labels for compatibility checks/logs."""

    system = platform.system().lower()
    machine = platform.machine().lower()
    if machine == "amd64":
        machine = "x86_64"
    libc_name, libc_version = platform.libc_ver()
    libc_name = (libc_name or "unknown").lower()
    libc_version = (libc_version or "unknown").lower()
    if not _SAFE_PLATFORM_LABEL_RE.fullmatch(system):
        system = "unknown"
    if not _SAFE_PLATFORM_LABEL_RE.fullmatch(machine):
        machine = "unknown"
    if not _SAFE_PLATFORM_LABEL_RE.fullmatch(libc_name):
        libc_name = "unknown"
    if not _SAFE_PLATFORM_LABEL_RE.fullmatch(libc_version):
        libc_version = "unknown"
    return {
        "system": system,
        **_os_release_fields(os_release_path),
        "machine": machine,
        "python_major": int(sys.version_info.major),
        "python_minor": int(sys.version_info.minor),
        "libc_name": libc_name,
        "libc_version": libc_version,
    }


def _version_tuple(value: object) -> tuple[int, ...]:
    if not isinstance(value, str):
        return ()
    match = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?$", value)
    if match is None:
        return ()
    return tuple(int(part or 0) for part in match.groups())


def _runtime_environment_supported(
    environment: Mapping[str, object],
    profile: Mapping[str, object],
) -> bool:
    compatibility = profile.get("compatibility")
    if not isinstance(compatibility, Mapping):
        return False
    supported_os = compatibility.get("supported_os")
    if not isinstance(supported_os, Mapping):
        return False
    os_id, os_version = environment.get("os_id"), environment.get("os_version")
    allowed_versions = supported_os.get(os_id)
    if not isinstance(allowed_versions, list) or os_version not in allowed_versions:
        return False
    try:
        python_major = int(environment.get("python_major", -1))
        python_minor = int(environment.get("python_minor", -1))
    except (TypeError, ValueError, OverflowError):
        return False
    minimum_libc = _version_tuple(compatibility.get("minimum_libc_version"))
    current_libc = _version_tuple(environment.get("libc_version"))
    return bool(
        environment.get("system") == compatibility.get("system")
        and environment.get("machine") == compatibility.get("machine")
        and python_major == compatibility.get("python_major")
        and python_minor == compatibility.get("python_minor")
        and environment.get("libc_name") == compatibility.get("libc_name")
        and minimum_libc
        and current_libc
        and current_libc >= minimum_libc
    )


def _record_runtime_environment(environment: Mapping[str, object]) -> None:
    logger = logging.getLogger("jppost.runtime")
    log_runtime_environment(logger.info, environment)


class BrowserRuntimeBootstrap:
    """Production adapter used by both Streamlit and non-UI entry points."""

    def __init__(
        self,
        *,
        profile_path: str | Path = PROFILE_PATH,
        project_root: str | Path = PROJECT_ROOT,
        asset_root: str | Path = DEFAULT_ASSET_ROOT,
        runtime_preparer: Callable[..., object] = prepare_playwright_runtime,
        process_runner: Callable[..., ProcessResult] = _run_bounded_process,
        fence_runner: Callable[..., object] | None = None,
    ) -> None:
        self.profile_path = Path(profile_path)
        self.project_root = Path(project_root)
        self.asset_root = Path(asset_root)
        self.runtime_preparer = runtime_preparer
        self.process_runner = process_runner
        self.fence_runner = fence_runner or _run_runtime_fence

    def _profile_parts(self) -> tuple[dict[str, object], str, str, str]:
        profile = load_runtime_profile(self.profile_path)
        revision, browser_version = _playwright_browser_metadata()
        try:
            playwright_version = importlib.metadata.version("playwright")
        except importlib.metadata.PackageNotFoundError as exc:
            raise RuntimeSetupError("browser_profile", "browser_install") from exc
        return profile, revision, browser_version, playwright_version

    def profile_key(self) -> str:
        profile, revision, browser_version, playwright_version = self._profile_parts()
        identity = {
            "release_digest": _release_digest(self.project_root),
            "asset_digest": _manifest_digest(self.asset_root),
            "environment": runtime_environment_fingerprint(),
            "playwright_version": playwright_version,
            "browser_revision": revision,
            "browser_version": browser_version,
            "platform": {
                "system": platform.system().lower(),
                "machine": platform.machine().lower(),
                "libc": platform.libc_ver(),
            },
            "launch_profile": profile,
        }
        body = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(body).hexdigest()

    def create_manager(self) -> RuntimeManager:
        profile = load_runtime_profile(self.profile_path)
        return RuntimeManager(
            prepare=self.prepare,
            validate=self.validate,
            budget_seconds=float(profile["prepare_timeout_seconds"]),
        )

    def _install_browser(self, profile: dict[str, object], env: dict[str, str], deadline: float) -> None:
        max_attempts = int(profile["install_max_attempts"])
        backoffs = list(profile["install_retry_backoff_seconds"])
        last_code, last_status, last_retryable = "browser_install_unknown", None, False
        unknown_retry_used = False
        for attempt in range(1, max_attempts + 1):
            remaining = _remaining(deadline)
            timeout = min(float(profile["install_attempt_timeout_seconds"]), remaining - 5.0)
            if timeout <= 0:
                raise RuntimeSetupError("browser_install", "bootstrap_timeout", retryable=True)
            result = self.process_runner(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                env=env,
                timeout=timeout,
            )
            if result.returncode == 0 and not result.timed_out:
                return
            code, status, retryable, retry_after = _classify_install_failure(result)
            last_code, last_status, last_retryable = code, status, retryable
            retry_unknown_once = code == "browser_install_unknown" and not unknown_retry_used
            if retry_unknown_once:
                unknown_retry_used = True
            if attempt >= max_attempts or not (retryable or retry_unknown_once):
                error = RuntimeSetupError(
                    "browser_install",
                    "browser_install_timeout" if result.timed_out else last_code,
                    retryable=last_retryable,
                    http_status=last_status,
                )
                error.attempts = attempt
                raise error
            if retry_after is not None and status == 429:
                delay = retry_after
            else:
                delay = float(backoffs[min(attempt - 1, len(backoffs) - 1)])
            if delay >= _remaining(deadline) - 5.0:
                error = RuntimeSetupError("browser_install", "bootstrap_timeout", retryable=True, http_status=status)
                error.attempts = attempt
                raise error
            time.sleep(delay)
        error = RuntimeSetupError("browser_install", last_code, retryable=last_retryable, http_status=last_status)
        error.attempts = max_attempts
        raise error

    def _probe_browser(
        self,
        profile: dict[str, object],
        env: dict[str, str],
        deadline: float,
        *,
        revision: str,
        browser_version: str,
    ) -> tuple[Path, str]:
        env = dict(env)
        env.update(
            {
                "JPPOST_PROBE_LAUNCH_ARGS": json.dumps(profile["launch_args"], separators=(",", ":")),
                "JPPOST_PROBE_FALLBACK_ARGS": json.dumps(profile["fallback_args"], separators=(",", ":")),
                "JPPOST_PROBE_EXPECTED_VERSION": browser_version,
                "JPPOST_PROBE_TITLE": str(profile["probe_title"]),
            }
        )
        timeout = min(float(profile["probe_timeout_seconds"]), _remaining(deadline) - 5.0)
        if timeout <= 0:
            raise RuntimeSetupError("browser_probe", "bootstrap_timeout", retryable=True)
        result = self.process_runner(
            [sys.executable, "-c", _PROBE_SCRIPT],
            env=env,
            timeout=timeout,
        )
        if result.timed_out or result.returncode != 0:
            raise RuntimeSetupError(
                "browser_probe",
                "browser_probe_timeout" if result.timed_out else "browser_launch",
                retryable=result.timed_out,
            )
        line = next((line for line in result.stdout.splitlines() if line.startswith(PROBE_PREFIX)), None)
        if line is None:
            raise RuntimeSetupError("browser_probe", "browser_launch")
        try:
            payload = json.loads(line[len(PROBE_PREFIX):])
            executable = Path(payload["executable"]).resolve()
            actual_version = str(payload["version"])
        except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
            raise RuntimeSetupError("browser_probe", "browser_launch") from exc
        browser_root = Path(str(profile["browser_root"])).resolve()
        if not _path_is_within(executable, browser_root):
            raise RuntimeSetupError("browser_probe", "profile_unsupported")
        relative = executable.relative_to(browser_root)
        if f"chromium-{revision}" not in relative.parts and f"chromium_headless_shell-{revision}" not in relative.parts:
            raise RuntimeSetupError("browser_probe", "profile_unsupported")
        if actual_version != browser_version or not executable.is_file():
            raise RuntimeSetupError("browser_probe", "browser_launch")
        try:
            executable_digest = _file_sha256(executable, deadline=deadline)
        except TimeoutError as exc:
            raise RuntimeSetupError("browser_probe", "bootstrap_timeout", retryable=True) from exc
        return executable, executable_digest

    def prepare(self, key: str, deadline: float) -> RuntimeHandle:
        """Prepare in a request-scoped process fence used by RuntimeManager."""

        result = self.fence_runner("prepare", {"key": key}, deadline=deadline)
        if not isinstance(result, RuntimeHandle):
            raise RuntimeSetupError("bootstrap", "bootstrap_protocol_error")
        return result

    def prepare_in_process(self, key: str, deadline: float) -> RuntimeHandle:
        """Synchronous operation body executed only inside the fence child."""

        profile = load_runtime_profile(self.profile_path)
        if not _runtime_environment_supported(runtime_environment_fingerprint(), profile):
            raise RuntimeSetupError("profile", "profile_unsupported")
        profile, revision, browser_version, _playwright_version = self._profile_parts()
        runtime = self.runtime_preparer(
            asset_root=self.asset_root,
            apply_to_process_env=False,
            deadline=deadline,
        )
        if not getattr(runtime, "ok", False):
            code = str(getattr(runtime, "error_code", "bootstrap_unknown"))
            if code not in _ALLOWED_RUNTIME_CODES:
                code = "bootstrap_unknown"
            raise RuntimeSetupError("runtime_libraries", code, retryable=code in {"bootstrap_timeout"})
        env = dict(runtime.env)
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(profile["browser_root"])
        self._install_browser(profile, env, deadline)
        executable, executable_digest = self._probe_browser(
            profile,
            env,
            deadline,
            revision=revision,
            browser_version=browser_version,
        )
        _remaining(deadline)
        stat = executable.stat()
        return RuntimeHandle(
            profile_key=key,
            executable_path=executable,
            browser_revision=revision,
            env=env,
            executable_sha256=executable_digest,
            executable_size=stat.st_size,
            executable_mtime_ns=stat.st_mtime_ns,
            browser_flavor="chromium",
            browser_version=browser_version,
        )

    def validate(self, handle: object, deadline: float) -> bool:
        """Validate through the same hard process fence as cold preparation."""

        key = handle.profile_key if isinstance(handle, RuntimeHandle) else "invalid-handle"
        result = self.fence_runner(
            "validate",
            {"key": key, "handle": handle},
            deadline=deadline,
        )
        if not isinstance(result, bool):
            raise RuntimeSetupError("bootstrap", "bootstrap_protocol_error")
        return result

    def validate_in_process(self, handle: object, deadline: float) -> bool:
        """Synchronous validation body executed only inside the fence child."""

        if time.monotonic() >= deadline:
            return False
        if not isinstance(handle, RuntimeHandle):
            return False
        profile = load_runtime_profile(self.profile_path)
        if not _runtime_environment_supported(runtime_environment_fingerprint(), profile):
            return False
        if handle.profile_key != self.profile_key():
            return False
        profile, revision, browser_version, _playwright_version = self._profile_parts()
        if handle.browser_revision != revision or handle.browser_flavor != "chromium":
            return False
        if handle.browser_version != browser_version:
            return False
        browser_root = Path(str(profile["browser_root"])).resolve()
        if not _path_is_within(handle.executable_path, browser_root):
            return False
        relative = handle.executable_path.resolve().relative_to(browser_root)
        if f"chromium-{revision}" not in relative.parts and f"chromium_headless_shell-{revision}" not in relative.parts:
            return False
        try:
            stat = handle.executable_path.stat()
        except OSError:
            return False
        if stat.st_size != handle.executable_size or stat.st_mtime_ns != handle.executable_mtime_ns:
            return False
        try:
            if (
                not handle.executable_sha256
                or _file_sha256(handle.executable_path, deadline=deadline).lower()
                != handle.executable_sha256.lower()
            ):
                return False
        except (OSError, TimeoutError):
            return False
        if handle.env.get("PLAYWRIGHT_BROWSERS_PATH") != str(profile["browser_root"]):
            return False
        try:
            asset_digest, _assets = _load_verified_assets(self.asset_root, deadline=deadline)
        except Exception:
            return False
        if _manifest_digest(self.asset_root) != asset_digest:
            return False
        ld_library_path = handle.env.get("LD_LIBRARY_PATH", "")
        if ld_library_path:
            library_path = Path(ld_library_path.split(os.pathsep)[0])
            runtime_root = library_path.parents[2]
            try:
                libraries_ready = _vendor_libraries_ready(
                    runtime_root,
                    manifest_digest=asset_digest,
                    platform_fingerprint=_platform_fingerprint(platform.system(), platform.machine()),
                    deadline=deadline,
                )
            except (OSError, TimeoutError):
                return False
            if not libraries_ready:
                return False
        elif not all(_library_available(name) for name in REQUIRED_RUNTIME_LIBRARY_NAMES):
            return False
        return time.monotonic() < deadline


_MANAGER_LOCK = threading.Lock()
_MANAGER: RuntimeManager | None = None


def get_browser_runtime_manager() -> RuntimeManager:
    """Return one process-local single-flight manager shared by app and worker."""

    global _MANAGER
    if _MANAGER is None:
        with _MANAGER_LOCK:
            if _MANAGER is None:
                _MANAGER = BrowserRuntimeBootstrap().create_manager()
    return _MANAGER


def build_runtime_profile_key() -> str:
    return BrowserRuntimeBootstrap().profile_key()


def probe_browser_runtime_fresh() -> RuntimeHandle:
    """Always prepare and launch a disposable blank-page browser probe.

    This bypasses the warm-handle cache intentionally. It is for the explicit
    no-order cloud diagnostic; normal job startup continues to use the shared
    RuntimeManager.
    """

    bootstrap = BrowserRuntimeBootstrap()
    environment = runtime_environment_fingerprint()
    _record_runtime_environment(environment)
    profile = load_runtime_profile(bootstrap.profile_path)
    if not _runtime_environment_supported(environment, profile):
        raise RuntimeSetupError("profile", "profile_unsupported")
    key = bootstrap.profile_key()
    deadline = time.monotonic() + float(profile["prepare_timeout_seconds"])
    handle = bootstrap.prepare(key, deadline)
    if not isinstance(handle, RuntimeHandle) or handle.profile_key != key:
        raise RuntimeSetupError("bootstrap", "bootstrap_protocol_error")
    return handle


def ensure_browser_runtime(expected_handle: RuntimeHandle | None = None) -> RuntimeHandle:
    """Ensure one validated handle; never silently replace a caller's snapshot."""

    environment = runtime_environment_fingerprint()
    _record_runtime_environment(environment)
    profile = load_runtime_profile()
    if not _runtime_environment_supported(environment, profile):
        raise RuntimeSetupError("profile", "profile_unsupported")
    handle = get_browser_runtime_manager().ensure_ready(build_runtime_profile_key())
    if not isinstance(handle, RuntimeHandle):
        raise RuntimeSetupError("bootstrap", "bootstrap_unknown")
    if expected_handle is not None and handle is not expected_handle:
        raise RuntimeSetupError("bootstrap", "runtime_changed", retryable=True)
    return handle
