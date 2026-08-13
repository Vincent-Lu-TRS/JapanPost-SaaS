from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Condition
from typing import Any, Callable, Generic, TypeVar


T = TypeVar("T")


def may_apply_pending_snapshot(
    *,
    is_busy: bool,
    editor_dirty: bool,
    allow_dirty_reset: bool = False,
) -> bool:
    return not is_busy and (allow_dirty_reset or not editor_dirty)


@dataclass(frozen=True)
class RefreshStatus:
    source: str
    loaded_at: datetime | None
    last_attempt_at: datetime | None
    is_stale: bool
    is_refreshing: bool
    served_stale: bool
    error_code: str | None


@dataclass(frozen=True)
class RefreshResult(Generic[T]):
    data: T | None
    status: RefreshStatus


@dataclass
class _SourceState:
    data: Any = None
    has_data: bool = False
    loaded_at: datetime | None = None
    last_attempt_at: datetime | None = None
    is_refreshing: bool = False
    stale_after_error: bool = False
    served_stale: bool = False
    error_code: str | None = None


class SharedRefreshCoordinator:
    def __init__(
        self,
        ttl: timedelta = timedelta(minutes=20),
        *,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._ttl = ttl
        self._now = now
        self._condition = Condition()
        self._sources: dict[str, _SourceState] = {}

    def get(
        self,
        source: str,
        loader: Callable[[], T],
        *,
        force: bool = False,
        copier: Callable[[Any], Any] = copy.deepcopy,
    ) -> RefreshResult[T]:
        snapshot: tuple[bool, Any, RefreshStatus] | None = None
        with self._condition:
            state = self._sources.setdefault(source, _SourceState())

            if state.is_refreshing:
                if state.has_data and not force:
                    state.served_stale = True
                    snapshot = self._snapshot_locked(
                        source, state, force_stale=True
                    )
                else:
                    while state.is_refreshing:
                        self._condition.wait()
                    snapshot = self._snapshot_locked(source, state)
            elif state.has_data and not force and not self._is_stale(state):
                state.served_stale = False
                snapshot = self._snapshot_locked(source, state)
            else:
                state.is_refreshing = True
                state.last_attempt_at = self._now()
                state.served_stale = False

        if snapshot is not None:
            return self._copy_result(snapshot, copier)

        try:
            loaded = loader()
        except Exception as exc:
            error_code = self._safe_error_code(exc)
            with self._condition:
                state.error_code = error_code
                state.stale_after_error = state.has_data
                state.served_stale = state.has_data
                try:
                    state.is_refreshing = False
                    snapshot = self._snapshot_locked(source, state)
                finally:
                    state.is_refreshing = False
                    self._condition.notify_all()
            return self._copy_result(snapshot, copier)

        try:
            cached = copier(loaded)
        except Exception:
            with self._condition:
                state.error_code = "unexpected"
                state.stale_after_error = state.has_data
                state.served_stale = state.has_data
                try:
                    state.is_refreshing = False
                finally:
                    state.is_refreshing = False
                    self._condition.notify_all()
            raise

        with self._condition:
            try:
                state.data = cached
                state.has_data = True
                state.loaded_at = self._now()
                state.is_refreshing = False
                state.stale_after_error = False
                state.served_stale = False
                state.error_code = None
                snapshot = self._snapshot_locked(source, state)
            finally:
                state.is_refreshing = False
                self._condition.notify_all()
        return self._copy_result(snapshot, copier)

    def status(self, source: str) -> RefreshStatus:
        with self._condition:
            state = self._sources.get(source)
            if state is None:
                return RefreshStatus(
                    source=source,
                    loaded_at=None,
                    last_attempt_at=None,
                    is_stale=True,
                    is_refreshing=False,
                    served_stale=False,
                    error_code=None,
                )
            return self._status_locked(source, state)

    def _snapshot_locked(
        self,
        source: str,
        state: _SourceState,
        *,
        force_stale: bool = False,
    ) -> tuple[bool, Any, RefreshStatus]:
        return (
            state.has_data,
            state.data,
            self._status_locked(source, state, force_stale=force_stale),
        )

    @staticmethod
    def _copy_result(
        snapshot: tuple[bool, Any, RefreshStatus],
        copier: Callable[[Any], Any],
    ) -> RefreshResult[Any]:
        has_data, data, status = snapshot
        return RefreshResult(
            data=copier(data) if has_data else None,
            status=status,
        )

    def _status_locked(
        self,
        source: str,
        state: _SourceState,
        *,
        force_stale: bool = False,
    ) -> RefreshStatus:
        return RefreshStatus(
            source=source,
            loaded_at=state.loaded_at,
            last_attempt_at=state.last_attempt_at,
            is_stale=force_stale or self._is_stale(state),
            is_refreshing=state.is_refreshing,
            served_stale=state.served_stale,
            error_code=state.error_code,
        )

    def _is_stale(self, state: _SourceState) -> bool:
        if not state.has_data or state.loaded_at is None:
            return True
        return state.stale_after_error or self._now() - state.loaded_at >= self._ttl

    @staticmethod
    def _safe_error_code(exc: Exception) -> str:
        if isinstance(exc, PermissionError):
            return "permission_denied"
        if isinstance(exc, (TimeoutError, ConnectionError)):
            return "network"
        return "unexpected"
