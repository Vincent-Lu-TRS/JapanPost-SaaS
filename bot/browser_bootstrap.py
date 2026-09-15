"""Process-local coordination for preparing and validating the browser runtime.

This module intentionally contains no Streamlit calls. A manager may be used
from the UI thread or a background job worker, and only verified successful
handles are retained between explicit requests.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeout
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping


@dataclass(frozen=True)
class RuntimeHandle:
    """An immutable description of a browser executable that passed a probe."""

    profile_key: str
    executable_path: Path
    browser_revision: str
    env: Mapping[str, str]
    executable_sha256: str = ""
    executable_size: int = 0
    executable_mtime_ns: int = 0
    browser_flavor: str = "chromium"
    browser_version: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "executable_path", Path(self.executable_path))
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))


class RuntimeSetupError(RuntimeError):
    """Safe, structured failure suitable for user-facing classification."""

    def __init__(
        self,
        stage: str,
        code: str,
        *,
        retryable: bool = False,
        http_status: int | None = None,
    ) -> None:
        # Do not include URLs, raw exception text, or user/order data here.
        super().__init__(code)
        self.stage = stage
        self.code = code
        self.retryable = retryable
        self.http_status = http_status
        self.attempts = 0


class RuntimeManager:
    """Single-flight runtime preparation with success-only caching.

    ``prepare`` and ``validate`` receive the same absolute monotonic deadline.
    They must honor it for work they start; potentially blocking subprocess
    work should be isolated and terminated by the adapter rather than run in
    an unbounded thread.
    """

    def __init__(
        self,
        *,
        prepare: Callable[[str, float], object],
        validate: Callable[[object, float], bool],
        budget_seconds: float = 120,
    ) -> None:
        if budget_seconds <= 0:
            raise ValueError("budget_seconds must be positive")
        self._prepare = prepare
        self._validate = validate
        self._budget = float(budget_seconds)
        self._lock = threading.Lock()
        self._ready: object | None = None
        self._key: str | None = None
        self._flight: Future[object] | None = None
        self._flight_key: str | None = None

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeSetupError("bootstrap", "deadline_exceeded", retryable=True)
        return remaining

    def _acquire(self, deadline: float) -> None:
        if not self._lock.acquire(timeout=self._remaining(deadline)):
            raise RuntimeSetupError("bootstrap", "lock_timeout", retryable=True)

    def ensure_ready(self, key: str) -> object:
        """Return a validated handle, preparing once per concurrent flight."""

        if not key:
            raise RuntimeSetupError("bootstrap", "profile_key_missing")
        deadline = time.monotonic() + self._budget
        self._acquire(deadline)
        try:
            active_flight = self._flight is not None and not self._flight.done()
            if active_flight:
                if self._flight_key != key:
                    raise RuntimeSetupError(
                        "bootstrap", "profile_busy", retryable=True
                    )
                flight = self._flight
                leader = False
                candidate = None
            else:
                candidate = self._ready if self._key == key else None
                flight = Future()
                self._flight = flight
                self._flight_key = key
                self._ready = None
                self._key = None
                leader = True
        finally:
            self._lock.release()

        if not leader:
            try:
                return flight.result(timeout=self._remaining(deadline))
            except FutureTimeout as exc:
                raise RuntimeSetupError(
                    "bootstrap", "wait_timeout", retryable=True
                ) from exc
            except RuntimeSetupError:
                raise
            except Exception as exc:
                # Future callers should see a safe classification even if an
                # adapter unexpectedly raised an unstructured exception.
                raise RuntimeSetupError("bootstrap", "bootstrap_unknown") from exc

        try:
            valid = candidate is not None and self._validate(candidate, deadline)
            handle = candidate if valid else self._prepare(key, deadline)
            self._remaining(deadline)
            if handle is None or handle is False:
                raise RuntimeSetupError("bootstrap", "invalid_ready_result")
            if isinstance(handle, RuntimeHandle) and handle.profile_key != key:
                raise RuntimeSetupError("bootstrap", "profile_mismatch")

            self._acquire(deadline)
            try:
                self._ready = handle
                self._key = key
            finally:
                self._lock.release()
            flight.set_result(handle)
            return handle
        except BaseException as exc:
            # The failure is shared only with this flight's waiters. Since it
            # is not stored as ``_ready``, the next explicit call can retry.
            if not flight.done():
                flight.set_exception(exc)
            raise
