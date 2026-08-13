from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Condition
from typing import Any, Callable, Generic, TypeVar


T = TypeVar("T")


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
        with self._condition:
            state = self._sources.setdefault(source, _SourceState())

            if state.is_refreshing:
                if state.has_data and not force:
                    state.served_stale = True
                    return self._result_locked(source, state, copier, force_stale=True)

                while state.is_refreshing:
                    self._condition.wait()
                return self._result_locked(source, state, copier)

            if state.has_data and not force and not self._is_stale(state):
                state.served_stale = False
                return self._result_locked(source, state, copier)

            state.is_refreshing = True
            state.last_attempt_at = self._now()
            state.served_stale = False

        try:
            loaded = loader()
            cached = copier(loaded)
        except Exception as exc:
            error_code = self._safe_error_code(exc)
            with self._condition:
                state.is_refreshing = False
                state.error_code = error_code
                state.stale_after_error = state.has_data
                state.served_stale = state.has_data
                result = self._result_locked(source, state, copier)
                self._condition.notify_all()
                return result

        with self._condition:
            state.data = cached
            state.has_data = True
            state.loaded_at = self._now()
            state.is_refreshing = False
            state.stale_after_error = False
            state.served_stale = False
            state.error_code = None
            result = self._result_locked(source, state, copier)
            self._condition.notify_all()
            return result

    def status(self, source: str) -> RefreshStatus:
        with self._condition:
            state = self._sources.get(source)
            if state is None:
                return RefreshStatus(
                    source=source,
                    loaded_at=None,
                    last_attempt_at=None,
                    is_stale=False,
                    is_refreshing=False,
                    served_stale=False,
                    error_code=None,
                )
            return self._status_locked(source, state)

    def _result_locked(
        self,
        source: str,
        state: _SourceState,
        copier: Callable[[Any], Any],
        *,
        force_stale: bool = False,
    ) -> RefreshResult[Any]:
        data = copier(state.data) if state.has_data else None
        return RefreshResult(
            data=data,
            status=self._status_locked(source, state, force_stale=force_stale),
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
            return False
        return state.stale_after_error or self._now() - state.loaded_at >= self._ttl

    @staticmethod
    def _safe_error_code(exc: Exception) -> str:
        if isinstance(exc, PermissionError):
            return "permission_denied"
        if isinstance(exc, (TimeoutError, ConnectionError)):
            return "network"
        return "unexpected"
