from __future__ import annotations

import time
import unittest
import copy
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event, Lock

import pandas as pd

from refresh_cache import SharedRefreshCoordinator


class MutableClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 8, 13, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


def wait_for_condition_waiters(
    coordinator: SharedRefreshCoordinator, count: int = 1
) -> None:
    deadline = time.monotonic() + 5
    condition = coordinator._condition
    while time.monotonic() < deadline:
        with condition:
            if len(condition._waiters) >= count:
                return
        time.sleep(0.005)
    raise AssertionError(f"expected at least {count} condition waiter(s)")


class SharedRefreshCoordinatorTests(unittest.TestCase):
    def test_first_load_returns_an_isolated_copy(self) -> None:
        clock = MutableClock()
        coordinator = SharedRefreshCoordinator(now=clock)
        loaded = {"rows": [1]}

        first = coordinator.get("shipments", lambda: loaded)
        first.data["rows"].append(2)
        loaded["rows"].append(3)

        second = coordinator.get("shipments", lambda: {"rows": [99]})

        self.assertEqual(second.data, {"rows": [1]})
        self.assertIsNot(second.data, first.data)
        self.assertEqual(second.status.source, "shipments")
        self.assertEqual(second.status.loaded_at, clock.current)
        self.assertIsNone(second.status.error_code)

    def test_reuses_snapshot_within_ttl(self) -> None:
        clock = MutableClock()
        coordinator = SharedRefreshCoordinator(
            ttl=timedelta(minutes=20), now=clock
        )
        calls = 0

        def load() -> str:
            nonlocal calls
            calls += 1
            return f"value-{calls}"

        first = coordinator.get("shipments", load)
        clock.advance(timedelta(minutes=19, seconds=59))
        second = coordinator.get("shipments", load)

        self.assertEqual(first.data, "value-1")
        self.assertEqual(second.data, "value-1")
        self.assertEqual(calls, 1)
        self.assertFalse(second.status.is_stale)

    def test_refreshes_snapshot_when_ttl_expires(self) -> None:
        clock = MutableClock()
        coordinator = SharedRefreshCoordinator(
            ttl=timedelta(minutes=20), now=clock
        )
        values = iter(("old", "new"))

        first = coordinator.get("shipments", lambda: next(values))
        clock.advance(timedelta(minutes=20))
        second = coordinator.get("shipments", lambda: next(values))

        self.assertEqual(first.data, "old")
        self.assertEqual(second.data, "new")
        self.assertEqual(second.status.loaded_at, clock.current)

    def test_force_refreshes_a_fresh_snapshot(self) -> None:
        clock = MutableClock()
        coordinator = SharedRefreshCoordinator(now=clock)
        calls = 0

        def load() -> int:
            nonlocal calls
            calls += 1
            return calls

        self.assertEqual(coordinator.get("shipments", load).data, 1)
        forced = coordinator.get("shipments", load, force=True)

        self.assertEqual(forced.data, 2)
        self.assertEqual(calls, 2)

    def test_refresh_failure_serves_stale_snapshot_with_safe_error(self) -> None:
        clock = MutableClock()
        coordinator = SharedRefreshCoordinator(
            ttl=timedelta(minutes=1), now=clock
        )
        coordinator.get("shipments", lambda: {"rows": [1]})
        clock.advance(timedelta(minutes=1))

        def fail() -> None:
            raise PermissionError("secret spreadsheet details")

        result = coordinator.get("shipments", fail)
        result.data["rows"].append(2)

        self.assertEqual(result.data, {"rows": [1, 2]})
        self.assertEqual(coordinator.get("shipments", fail).data, {"rows": [1]})
        self.assertTrue(result.status.served_stale)
        self.assertTrue(result.status.is_stale)
        self.assertEqual(result.status.error_code, "permission_denied")

    def test_first_failure_returns_stale_status_without_leaking_text(self) -> None:
        clock = MutableClock()
        coordinator = SharedRefreshCoordinator(now=clock)

        def fail() -> None:
            raise TimeoutError("token=do-not-expose")

        result = coordinator.get("shipments", fail)

        self.assertIsNone(result.data)
        self.assertEqual(result.status.error_code, "network")
        self.assertIsNone(result.status.loaded_at)
        self.assertEqual(result.status.last_attempt_at, clock.current)
        self.assertTrue(result.status.is_stale)
        self.assertNotIn("do-not-expose", repr(result.status))

    def test_unloaded_source_status_is_stale(self) -> None:
        coordinator = SharedRefreshCoordinator(now=MutableClock())

        status = coordinator.status("never-loaded")

        self.assertEqual(status.source, "never-loaded")
        self.assertIsNone(status.loaded_at)
        self.assertTrue(status.is_stale)
        self.assertFalse(status.is_refreshing)

    def test_dataframe_results_are_copied_on_write_and_read(self) -> None:
        clock = MutableClock()
        coordinator = SharedRefreshCoordinator(now=clock)
        source_frame = pd.DataFrame({"sku": ["A"]})

        first = coordinator.get("products", lambda: source_frame)
        source_frame.loc[0, "sku"] = "SOURCE-MUTATION"
        first.data.loc[0, "sku"] = "CALLER-MUTATION"
        second = coordinator.get("products", lambda: pd.DataFrame())

        self.assertEqual(second.data.to_dict("records"), [{"sku": "A"}])

    def test_sources_have_independent_ttl_and_failure_state(self) -> None:
        clock = MutableClock()
        coordinator = SharedRefreshCoordinator(
            ttl=timedelta(minutes=5), now=clock
        )

        coordinator.get("products", lambda: "products-v1")
        clock.advance(timedelta(minutes=3))
        coordinator.get("shipments", lambda: "shipments-v1")
        clock.advance(timedelta(minutes=2))

        products = coordinator.get("products", lambda: "products-v2")
        shipments = coordinator.get("shipments", lambda: "shipments-v2")

        self.assertEqual(products.data, "products-v2")
        self.assertEqual(shipments.data, "shipments-v1")
        self.assertEqual(coordinator.status("products").loaded_at, clock.current)
        self.assertEqual(
            coordinator.status("shipments").loaded_at,
            clock.current - timedelta(minutes=2),
        )

    def test_concurrent_force_callers_share_one_refresh_flight(self) -> None:
        clock = MutableClock()
        coordinator = SharedRefreshCoordinator(now=clock)
        self.assertEqual(coordinator.get("shipments", lambda: "old").data, "old")

        callers_ready = Barrier(3)
        loader_started = Event()
        release_loader = Event()
        call_lock = Lock()
        calls = 0

        def load() -> str:
            nonlocal calls
            with call_lock:
                calls += 1
            loader_started.set()
            self.assertTrue(release_loader.wait(timeout=5))
            return "new"

        def force_get():
            callers_ready.wait(timeout=5)
            return coordinator.get("shipments", load, force=True)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(force_get)
            second = pool.submit(force_get)
            callers_ready.wait(timeout=5)
            self.assertTrue(loader_started.wait(timeout=5))
            wait_for_condition_waiters(coordinator)

            stale = coordinator.get("shipments", load)
            self.assertEqual(stale.data, "old")
            self.assertTrue(stale.status.is_refreshing)
            self.assertTrue(stale.status.served_stale)
            release_loader.set()

            self.assertEqual(first.result(timeout=5).data, "new")
            self.assertEqual(second.result(timeout=5).data, "new")

        self.assertEqual(calls, 1)
        self.assertFalse(coordinator.status("shipments").is_refreshing)

    def test_concurrent_first_load_waits_for_the_same_flight(self) -> None:
        clock = MutableClock()
        coordinator = SharedRefreshCoordinator(now=clock)
        callers_ready = Barrier(3)
        loader_started = Event()
        release_loader = Event()
        call_lock = Lock()
        calls = 0

        def load() -> str:
            nonlocal calls
            with call_lock:
                calls += 1
            loader_started.set()
            self.assertTrue(release_loader.wait(timeout=5))
            return "loaded"

        def get():
            callers_ready.wait(timeout=5)
            return coordinator.get("products", load)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(get)
            second = pool.submit(get)
            callers_ready.wait(timeout=5)
            self.assertTrue(loader_started.wait(timeout=5))
            wait_for_condition_waiters(coordinator)
            release_loader.set()

            self.assertEqual(first.result(timeout=5).data, "loaded")
            self.assertEqual(second.result(timeout=5).data, "loaded")

        self.assertEqual(calls, 1)

    def test_copier_failure_cannot_strand_a_force_waiter(self) -> None:
        coordinator = SharedRefreshCoordinator(now=MutableClock())
        self.assertEqual(coordinator.get("shipments", lambda: "old").data, "old")

        loader_started = Event()
        release_loader = Event()
        copier_lock = Lock()
        copier_calls = 0

        def load() -> str:
            loader_started.set()
            self.assertTrue(release_loader.wait(timeout=5))
            return "new"

        def bad_result_copier(data):
            nonlocal copier_calls
            with copier_lock:
                copier_calls += 1
                call = copier_calls
            if call == 1:
                return copy.deepcopy(data)
            raise RuntimeError("copy failed")

        def owner_get():
            return coordinator.get(
                "shipments", load, force=True, copier=bad_result_copier
            )

        def waiter_get():
            return coordinator.get("shipments", load, force=True)

        pool = ThreadPoolExecutor(max_workers=2)
        owner = pool.submit(owner_get)
        try:
            self.assertTrue(loader_started.wait(timeout=5))
            waiter = pool.submit(waiter_get)
            wait_for_condition_waiters(coordinator)
            release_loader.set()

            with self.assertRaises(RuntimeError):
                owner.result(timeout=5)
            try:
                waited = waiter.result(timeout=1)
            except FutureTimeoutError:
                with coordinator._condition:
                    coordinator._condition.notify_all()
                waiter.result(timeout=5)
                self.fail("force waiter remained blocked after copier failure")

            self.assertEqual(waited.data, "new")
            self.assertIsNone(waited.status.error_code)
            self.assertFalse(waited.status.is_refreshing)
        finally:
            pool.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
