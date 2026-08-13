from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event, Lock

import pandas as pd

from refresh_cache import SharedRefreshCoordinator


class MutableClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 8, 13, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


def test_first_load_returns_an_isolated_copy() -> None:
    clock = MutableClock()
    coordinator = SharedRefreshCoordinator(now=clock)
    loaded = {"rows": [1]}

    first = coordinator.get("shipments", lambda: loaded)
    first.data["rows"].append(2)
    loaded["rows"].append(3)

    second = coordinator.get("shipments", lambda: {"rows": [99]})

    assert second.data == {"rows": [1]}
    assert second.data is not first.data
    assert second.status.source == "shipments"
    assert second.status.loaded_at == clock.current
    assert second.status.error_code is None


def test_reuses_snapshot_within_ttl() -> None:
    clock = MutableClock()
    coordinator = SharedRefreshCoordinator(ttl=timedelta(minutes=20), now=clock)
    calls = 0

    def load() -> str:
        nonlocal calls
        calls += 1
        return f"value-{calls}"

    first = coordinator.get("shipments", load)
    clock.advance(timedelta(minutes=19, seconds=59))
    second = coordinator.get("shipments", load)

    assert first.data == second.data == "value-1"
    assert calls == 1
    assert second.status.is_stale is False


def test_refreshes_snapshot_when_ttl_expires() -> None:
    clock = MutableClock()
    coordinator = SharedRefreshCoordinator(ttl=timedelta(minutes=20), now=clock)
    values = iter(("old", "new"))

    first = coordinator.get("shipments", lambda: next(values))
    clock.advance(timedelta(minutes=20))
    second = coordinator.get("shipments", lambda: next(values))

    assert first.data == "old"
    assert second.data == "new"
    assert second.status.loaded_at == clock.current


def test_force_refreshes_a_fresh_snapshot() -> None:
    clock = MutableClock()
    coordinator = SharedRefreshCoordinator(now=clock)
    calls = 0

    def load() -> int:
        nonlocal calls
        calls += 1
        return calls

    assert coordinator.get("shipments", load).data == 1
    forced = coordinator.get("shipments", load, force=True)

    assert forced.data == 2
    assert calls == 2


def test_refresh_failure_serves_stale_snapshot_with_safe_error() -> None:
    clock = MutableClock()
    coordinator = SharedRefreshCoordinator(ttl=timedelta(minutes=1), now=clock)
    coordinator.get("shipments", lambda: {"rows": [1]})
    clock.advance(timedelta(minutes=1))

    def fail() -> None:
        raise PermissionError("secret spreadsheet details")

    result = coordinator.get("shipments", fail)
    result.data["rows"].append(2)

    assert result.data == {"rows": [1, 2]}
    assert coordinator.get("shipments", fail).data == {"rows": [1]}
    assert result.status.served_stale is True
    assert result.status.is_stale is True
    assert result.status.error_code == "permission_denied"


def test_first_failure_returns_no_data_and_does_not_leak_exception_text() -> None:
    clock = MutableClock()
    coordinator = SharedRefreshCoordinator(now=clock)

    def fail() -> None:
        raise TimeoutError("token=do-not-expose")

    result = coordinator.get("shipments", fail)

    assert result.data is None
    assert result.status.error_code == "network"
    assert result.status.loaded_at is None
    assert result.status.last_attempt_at == clock.current
    assert "do-not-expose" not in repr(result.status)


def test_dataframe_results_are_copied_on_write_and_read() -> None:
    clock = MutableClock()
    coordinator = SharedRefreshCoordinator(now=clock)
    source_frame = pd.DataFrame({"sku": ["A"]})

    first = coordinator.get("products", lambda: source_frame)
    source_frame.loc[0, "sku"] = "SOURCE-MUTATION"
    first.data.loc[0, "sku"] = "CALLER-MUTATION"
    second = coordinator.get("products", lambda: pd.DataFrame())

    assert second.data.to_dict("records") == [{"sku": "A"}]


def test_sources_have_independent_ttl_and_failure_state() -> None:
    clock = MutableClock()
    coordinator = SharedRefreshCoordinator(ttl=timedelta(minutes=5), now=clock)

    coordinator.get("products", lambda: "products-v1")
    clock.advance(timedelta(minutes=3))
    coordinator.get("shipments", lambda: "shipments-v1")
    clock.advance(timedelta(minutes=2))

    products = coordinator.get("products", lambda: "products-v2")
    shipments = coordinator.get("shipments", lambda: "shipments-v2")

    assert products.data == "products-v2"
    assert shipments.data == "shipments-v1"
    assert coordinator.status("products").loaded_at == clock.current
    assert coordinator.status("shipments").loaded_at == clock.current - timedelta(minutes=2)


def test_concurrent_force_callers_share_one_refresh_flight() -> None:
    clock = MutableClock()
    coordinator = SharedRefreshCoordinator(now=clock)
    assert coordinator.get("shipments", lambda: "old").data == "old"

    loader_started = Event()
    release_loader = Event()
    call_lock = Lock()
    calls = 0

    def load() -> str:
        nonlocal calls
        with call_lock:
            calls += 1
        loader_started.set()
        assert release_loader.wait(timeout=5)
        return "new"

    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(coordinator.get, "shipments", load, force=True)
        assert loader_started.wait(timeout=5)
        second = pool.submit(coordinator.get, "shipments", load, force=True)
        stale = coordinator.get("shipments", load)
        assert stale.data == "old"
        assert stale.status.is_refreshing is True
        assert stale.status.served_stale is True
        release_loader.set()

        assert first.result(timeout=5).data == "new"
        assert second.result(timeout=5).data == "new"

    assert calls == 1
    assert coordinator.status("shipments").is_refreshing is False


def test_concurrent_first_load_waits_for_the_same_flight() -> None:
    clock = MutableClock()
    coordinator = SharedRefreshCoordinator(now=clock)
    loader_started = Event()
    release_loader = Event()
    call_lock = Lock()
    calls = 0

    def load() -> str:
        nonlocal calls
        with call_lock:
            calls += 1
        loader_started.set()
        assert release_loader.wait(timeout=5)
        return "loaded"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(coordinator.get, "products", load)
        assert loader_started.wait(timeout=5)
        second = pool.submit(coordinator.get, "products", load)
        release_loader.set()

        assert first.result(timeout=5).data == "loaded"
        assert second.result(timeout=5).data == "loaded"

    assert calls == 1
