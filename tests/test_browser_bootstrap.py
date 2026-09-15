import importlib
import threading
import time
import unittest
from concurrent.futures import Future as RealFuture
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from unittest.mock import Mock


class BrowserBootstrapTests(unittest.TestCase):
    def _manager_types(self):
        try:
            module = importlib.import_module("bot.browser_bootstrap")
        except ModuleNotFoundError as exc:
            if exc.name == "bot.browser_bootstrap":
                self.fail("RuntimeManager is missing; failed preparation must be retryable")
            raise
        self.assertTrue(hasattr(module, "RuntimeManager"))
        self.assertTrue(hasattr(module, "RuntimeSetupError"))
        return module.RuntimeManager, module.RuntimeSetupError

    def test_failed_preparation_is_retried_on_next_request(self):
        RuntimeManager, RuntimeSetupError = self._manager_types()
        ready_handle = object()
        prepare = Mock(side_effect=[
            RuntimeSetupError("browser_install", "download_failed", retryable=True),
            ready_handle,
        ])
        manager = RuntimeManager(prepare=prepare, validate=lambda _handle, _deadline: True)

        with self.assertRaises(RuntimeSetupError):
            manager.ensure_ready("profile-a")

        self.assertIs(manager.ensure_ready("profile-a"), ready_handle)
        self.assertEqual(prepare.call_count, 2)

    def test_successful_handle_is_reused_while_valid(self):
        RuntimeManager, _RuntimeSetupError = self._manager_types()
        ready_handle = object()
        prepare = Mock(return_value=ready_handle)
        validate = Mock(return_value=True)
        manager = RuntimeManager(prepare=prepare, validate=validate)

        self.assertIs(manager.ensure_ready("profile-a"), ready_handle)
        self.assertIs(manager.ensure_ready("profile-a"), ready_handle)

        self.assertEqual(prepare.call_count, 1)
        self.assertEqual(validate.call_count, 1)

    def test_invalid_handle_is_prepared_again(self):
        RuntimeManager, _RuntimeSetupError = self._manager_types()
        first_handle, replacement_handle = object(), object()
        prepare = Mock(side_effect=[first_handle, replacement_handle])
        validate = Mock(return_value=False)
        manager = RuntimeManager(prepare=prepare, validate=validate)

        self.assertIs(manager.ensure_ready("profile-a"), first_handle)
        self.assertIs(manager.ensure_ready("profile-a"), replacement_handle)

        self.assertEqual(prepare.call_count, 2)
        self.assertEqual(validate.call_count, 1)

    def test_parallel_callers_share_one_preparation_and_result(self):
        RuntimeManager, _RuntimeSetupError = self._manager_types()
        preparation_started = threading.Event()
        release_preparation = threading.Event()
        ready_handle = object()
        prepare = Mock(side_effect=lambda _key, _deadline: (
            preparation_started.set(),
            release_preparation.wait(2),
            ready_handle,
        )[-1])
        manager = RuntimeManager(prepare=prepare, validate=lambda _handle, _deadline: True)

        class ObservedFuture(RealFuture):
            waiters = 0
            waiters_lock = threading.Lock()
            all_waiters_joined = threading.Event()

            def result(self, timeout=None):
                with self.waiters_lock:
                    self.waiters += 1
                    if self.waiters == 4:
                        self.all_waiters_joined.set()
                return super().result(timeout=timeout)

        with patch("bot.browser_bootstrap.Future", ObservedFuture):
            with ThreadPoolExecutor(max_workers=5) as pool:
                calls = [pool.submit(manager.ensure_ready, "profile-a") for _ in range(5)]
                self.assertTrue(preparation_started.wait(1), "leader did not begin preparation")
                self.assertTrue(ObservedFuture.all_waiters_joined.wait(1), "callers did not join the active flight")
                release_preparation.set()
                results = [call.result(timeout=1) for call in calls]

        self.assertEqual(prepare.call_count, 1)
        self.assertTrue(all(result is ready_handle for result in results))

    def test_parallel_failure_is_shared_but_next_explicit_call_retries(self):
        RuntimeManager, RuntimeSetupError = self._manager_types()
        preparation_started = threading.Event()
        release_preparation = threading.Event()
        ready_handle = object()
        failure = RuntimeSetupError("browser_install", "temporary_failure", retryable=True)

        def prepare(_key, _deadline):
            preparation_started.set()
            release_preparation.wait(2)
            if prepare.calls == 1:
                raise failure
            return ready_handle

        prepare.calls = 0

        def counted_prepare(key, deadline):
            prepare.calls += 1
            return prepare(key, deadline)

        manager = RuntimeManager(prepare=counted_prepare, validate=lambda _handle, _deadline: True)

        class ObservedFuture(RealFuture):
            waiters = 0
            waiters_lock = threading.Lock()
            all_waiters_joined = threading.Event()

            def result(self, timeout=None):
                with self.waiters_lock:
                    self.waiters += 1
                    if self.waiters == 3:
                        self.all_waiters_joined.set()
                return super().result(timeout=timeout)

        with patch("bot.browser_bootstrap.Future", ObservedFuture):
            with ThreadPoolExecutor(max_workers=4) as pool:
                calls = [pool.submit(manager.ensure_ready, "profile-a") for _ in range(4)]
                self.assertTrue(preparation_started.wait(1), "leader did not begin preparation")
                self.assertTrue(ObservedFuture.all_waiters_joined.wait(1), "callers did not join the active flight")
                release_preparation.set()
                errors = []
                for call in calls:
                    with self.assertRaises(RuntimeSetupError) as caught:
                        call.result(timeout=1)
                    errors.append(caught.exception)

        self.assertEqual({error.code for error in errors}, {"temporary_failure"})
        self.assertEqual(prepare.calls, 1)
        self.assertIs(manager.ensure_ready("profile-a"), ready_handle)
        self.assertEqual(prepare.calls, 2)

    def test_waiting_for_active_flight_respects_caller_deadline(self):
        RuntimeManager, RuntimeSetupError = self._manager_types()
        preparation_started = threading.Event()
        release_preparation = threading.Event()
        prepare = Mock(side_effect=lambda _key, _deadline: (
            preparation_started.set(), release_preparation.wait(2), object()
        )[-1])
        manager = RuntimeManager(prepare=prepare, validate=lambda _handle, _deadline: True, budget_seconds=0.5)
        leader_errors = []

        def run_leader():
            try:
                manager.ensure_ready("profile-a")
            except Exception as exc:  # surfaced to the parent thread below
                leader_errors.append(exc)

        leader = threading.Thread(target=run_leader)
        leader.start()
        self.assertTrue(preparation_started.wait(1), "leader did not begin preparation")
        started_at = time.monotonic()
        manager._budget = 0.05  # Give this waiter a shorter independent request budget.
        try:
            with self.assertRaises(RuntimeSetupError) as caught:
                manager.ensure_ready("profile-a")
        finally:
            manager._budget = 0.5
        elapsed = time.monotonic() - started_at
        release_preparation.set()
        leader.join(timeout=1)

        self.assertEqual(caught.exception.code, "wait_timeout")
        self.assertLess(elapsed, 0.5)
        self.assertFalse(leader.is_alive())
        self.assertEqual(leader_errors, [])

    def test_profile_change_does_not_reuse_handle_from_previous_profile(self):
        RuntimeManager, _RuntimeSetupError = self._manager_types()
        first_handle, second_handle = object(), object()
        prepare = Mock(side_effect=[first_handle, second_handle])
        manager = RuntimeManager(prepare=prepare, validate=lambda _handle, _deadline: True)

        self.assertIs(manager.ensure_ready("profile-a"), first_handle)
        self.assertIs(manager.ensure_ready("profile-b"), second_handle)

        self.assertEqual(prepare.call_count, 2)


if __name__ == "__main__":
    unittest.main()
