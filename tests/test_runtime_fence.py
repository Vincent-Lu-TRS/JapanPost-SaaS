import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bot.browser_bootstrap import RuntimeHandle, RuntimeSetupError
from bot.browser_runtime import BrowserRuntimeBootstrap, ProcessResult, _run_bounded_process


class RuntimeFenceTests(unittest.TestCase):
    def _runtime_fence(self):
        try:
            import bot.runtime_fence as runtime_fence
        except ModuleNotFoundError as exc:
            if exc.name == "bot.runtime_fence":
                self.fail("request-scoped runtime fence helper is missing")
            raise
        return runtime_fence

    def test_manager_fences_both_prepare_and_validate(self):
        calls = []
        ready_handle = RuntimeHandle("profile-key", Path("/tmp/ms-playwright/chrome"), "r1", {})

        def fence_runner(operation, payload, *, deadline):
            calls.append((operation, payload, deadline))
            return ready_handle if operation == "prepare" else True

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = root / "runtime_profile.json"
            bundled_profile = Path(__file__).resolve().parents[1] / "bot" / "runtime_profile.json"
            profile_data = json.loads(bundled_profile.read_text(encoding="utf-8"))
            profile_data["browser_root"] = "/tmp/ms-playwright"
            profile.write_text(json.dumps(profile_data), encoding="utf-8")
            bootstrap = BrowserRuntimeBootstrap(profile_path=profile, fence_runner=fence_runner)
            manager = bootstrap.create_manager()

            self.assertIs(manager.ensure_ready("profile-key"), ready_handle)
            self.assertIs(manager.ensure_ready("profile-key"), ready_handle)

        self.assertEqual([call[0] for call in calls], ["prepare", "validate"])
        self.assertEqual(calls[0][1], {"key": "profile-key"})
        self.assertIs(calls[1][1]["handle"], ready_handle)
        self.assertTrue(all(call[2] > time.monotonic() for call in calls))

    def test_runtime_fence_invokes_sys_executable_as_module_and_bounds_timeout(self):
        self._runtime_fence()
        from bot.browser_runtime import _run_runtime_fence

        response = {"protocol": 1, "ok": True, "result": {"handle": {
            "profile_key": "profile-key",
            "executable_path": "/tmp/ms-playwright/chrome",
            "browser_revision": "r1",
            "env": {},
            "executable_sha256": "",
            "executable_size": 0,
            "executable_mtime_ns": 0,
            "browser_flavor": "chromium",
            "browser_version": "",
        }}}
        runner = Mock(return_value=ProcessResult(0, stdout=json.dumps(response)))
        deadline = time.monotonic() + 15

        with patch("bot.browser_runtime.sys.executable", "/cloud/venv/bin/python"), patch(
            "bot.browser_runtime._run_bounded_process", runner
        ):
            result = _run_runtime_fence("prepare", {"key": "profile-key"}, deadline=deadline)

        self.assertEqual(result.executable_path, Path("/tmp/ms-playwright/chrome"))
        command = runner.call_args.args[0]
        self.assertEqual(command, ["/cloud/venv/bin/python", "-m", "bot.runtime_fence"])
        self.assertLessEqual(runner.call_args.kwargs["timeout"], 10)
        self.assertGreater(runner.call_args.kwargs["timeout"], 0)
        self.assertEqual(runner.call_args.kwargs["cwd"], Path(__file__).resolve().parents[1])

    def test_runtime_fence_timeout_is_bounded_and_classified(self):
        self._runtime_fence()
        from bot.browser_runtime import _run_runtime_fence

        runner = Mock(return_value=ProcessResult(124, timed_out=True))
        with patch("bot.browser_runtime._run_bounded_process", runner):
            started = time.monotonic()
            with self.assertRaises(RuntimeSetupError) as caught:
                _run_runtime_fence("prepare", {"key": "profile-key"}, deadline=started + 10)

        self.assertEqual(caught.exception.stage, "bootstrap")
        self.assertEqual(caught.exception.code, "bootstrap_timeout")
        self.assertTrue(caught.exception.retryable)
        self.assertLess(time.monotonic() - started, 1)
        self.assertLessEqual(runner.call_args.kwargs["timeout"], 5)

    def test_ipc_and_child_environment_exclude_credentials_and_secrets(self):
        runtime_fence = self._runtime_fence()
        from bot.browser_runtime import _run_runtime_fence

        handle = RuntimeHandle(
            "profile-key",
            Path("/tmp/ms-playwright/chrome"),
            "r1",
            {
                "PATH": "/usr/bin",
                "LD_LIBRARY_PATH": "/tmp/runtime/lib",
                "PLAYWRIGHT_BROWSERS_PATH": "/tmp/ms-playwright",
                "GOOGLE_APPLICATION_CREDENTIALS": "do-not-forward-this",
                "GEMINI_API_KEY": "also-secret",
            },
        )
        request_seen = {}
        safe_response = {"protocol": 1, "ok": True, "result": {"valid": True}}

        def runner(_command, *, env, timeout, input_data, deadline, cwd):
            request_seen["env"] = env
            request_seen["request"] = json.loads(input_data.decode("utf-8"))
            request_seen["cwd"] = cwd
            request_seen["deadline"] = deadline
            return ProcessResult(0, stdout=json.dumps(safe_response))

        source_env = {
            "PATH": "/usr/bin",
            "HOME": "/home/app",
            "TMPDIR": "/tmp",
            "SSL_CERT_FILE": "/etc/ssl/cert.pem",
            "GOOGLE_APPLICATION_CREDENTIALS": "parent-secret.json",
            "GEMINI_API_KEY": "parent-secret-key",
            "STREAMLIT_SERVER_COOKIE_SECRET": "cookie-secret",
        }
        with patch("bot.browser_runtime.os.environ", source_env), patch(
            "bot.browser_runtime._run_bounded_process", runner
        ):
            self.assertTrue(_run_runtime_fence("validate", {"key": "profile-key", "handle": handle}, deadline=time.monotonic() + 10))

        serialized = json.dumps(request_seen["request"])
        environment = json.dumps(request_seen["env"])
        for secret in ("do-not-forward-this", "also-secret", "parent-secret.json", "parent-secret-key", "cookie-secret"):
            self.assertNotIn(secret, serialized)
            self.assertNotIn(secret, environment)
        self.assertEqual(set(request_seen["env"]), {"PATH", "HOME", "TMPDIR", "SSL_CERT_FILE"})
        self.assertEqual(
            set(request_seen["request"]["handle"]["env"]),
            {"PATH", "LD_LIBRARY_PATH", "PLAYWRIGHT_BROWSERS_PATH"},
        )
        self.assertEqual(set(request_seen["request"]), {"protocol", "operation", "deadline", "key", "handle"})

    def test_handle_serialization_round_trips_allowlisted_runtime_fields(self):
        runtime_fence = self._runtime_fence()
        handle = RuntimeHandle(
            "profile-key",
            Path("/tmp/ms-playwright/chromium-123/chrome"),
            "123",
            {"PATH": "/usr/bin", "LD_LIBRARY_PATH": "/tmp/runtime/lib"},
            executable_sha256="abc123",
            executable_size=42,
            executable_mtime_ns=99,
            browser_flavor="chromium",
            browser_version="123.0.0.0",
        )

        encoded = runtime_fence.serialize_runtime_handle(handle)
        decoded = runtime_fence.deserialize_runtime_handle(encoded)

        self.assertEqual(decoded, handle)
        self.assertEqual(dict(decoded.env), dict(handle.env))

    def test_subreaper_setup_failure_fails_closed_before_running_operation(self):
        runtime_fence = self._runtime_fence()
        operation = Mock(return_value=RuntimeHandle("profile-key", Path("/tmp/chrome"), "r1", {}))
        bootstrap = Mock()
        bootstrap.prepare_in_process = operation

        result = runtime_fence.execute_request(
            {"protocol": 1, "operation": "prepare", "deadline": time.monotonic() + 5, "key": "profile-key"},
            bootstrap_factory=lambda **_kwargs: bootstrap,
            setup_subreaper=lambda: (_ for _ in ()).throw(RuntimeSetupError("bootstrap", "subreaper_unavailable")),
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "subreaper_unavailable")
        operation.assert_not_called()

    def test_cleanup_kills_descendants_discovered_after_sigkill_snapshot(self):
        runtime_fence = self._runtime_fence()
        clock = [0.0]
        snapshots = iter((
            [(101, "parent")],
            [(101, "parent")],
            [(101, "parent")],
            [(101, "parent"), (202, "late-child")],
            [(101, "parent"), (202, "late-child"), (303, "later-child")],
            [],
        ))
        signal_calls = []

        def advance_clock(_seconds):
            clock[0] += 0.5

        def record_signal(descendants, signal_number):
            signal_calls.append((tuple(descendants), signal_number))

        with patch.object(runtime_fence, "_operation_children", side_effect=lambda: next(snapshots)), patch.object(
            runtime_fence, "_reap_available_children"
        ), patch.object(runtime_fence.time, "monotonic", side_effect=lambda: clock[0]), patch.object(
            runtime_fence.time, "sleep", side_effect=advance_clock
        ), patch.object(runtime_fence.signal, "SIGKILL", 9, create=True), patch(
            "bot.browser_runtime._signal_linux_descendants", side_effect=record_signal
        ):
            had_children, children_clean = runtime_fence._cleanup_operation_children(deadline=10.0)

        self.assertTrue(had_children)
        self.assertTrue(children_clean)
        for late_descendant in ((202, "late-child"), (303, "later-child")):
            self.assertTrue(
                any(
                    signal_number == 9 and late_descendant in descendants
                    for descendants, signal_number in signal_calls
                ),
                f"new descendant {late_descendant} must receive SIGKILL",
            )


@unittest.skipUnless(sys.platform.startswith("linux"), "requires Linux /proc, setsid and PR_SET_CHILD_SUBREAPER")
class RuntimeFenceLinuxIntegrationTests(unittest.TestCase):
    def _scenario_command(self, scenario: str, marker: Path) -> list[str]:
        helper = Path(__file__).with_name("runtime_fence_scenario.py")
        return [sys.executable, str(helper), scenario, str(marker)]

    def _assert_not_running(self, marker: Path) -> None:
        pid = int(marker.read_text(encoding="ascii"))
        proc_stat = Path(f"/proc/{pid}/stat")
        deadline = time.monotonic() + 3
        while proc_stat.exists() and time.monotonic() < deadline:
            try:
                state = proc_stat.read_text(encoding="ascii").split(")", 1)[1].split()[0]
            except (OSError, IndexError):
                return
            if state == "Z":
                return
            time.sleep(0.02)
        self.assertFalse(proc_stat.exists(), f"detached grandchild {pid} remained alive")

    def _assert_reaped(self, marker: Path) -> None:
        pid = int(marker.read_text(encoding="ascii"))
        proc_stat = Path(f"/proc/{pid}/stat")
        deadline = time.monotonic() + 3
        while proc_stat.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(proc_stat.exists(), f"detached grandchild {pid} was not reaped")

    def _cleanup_marked_process(self, marker: Path) -> None:
        if not marker.exists():
            return
        try:
            pid = int(marker.read_text(encoding="ascii"))
            os.kill(pid, signal.SIGKILL)
        except (OSError, ValueError):
            return

    def test_exited_helper_does_not_report_ready_with_adopted_setsid_grandchild(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "grandchild.pid"
            try:
                result = subprocess.run(self._scenario_command("exit", marker), capture_output=True, timeout=8, check=False)
                self.assertNotEqual(result.returncode, 0)
                payload = json.loads(result.stdout.decode("utf-8"))
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["error"]["code"], "operation_children_leaked")
                self._assert_not_running(marker)
            finally:
                self._cleanup_marked_process(marker)

    def test_crashed_operation_reaps_adopted_setsid_grandchild_without_traceback(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "grandchild.pid"
            try:
                result = subprocess.run(self._scenario_command("crash", marker), capture_output=True, timeout=8, check=False)
                self.assertNotEqual(result.returncode, 0)
                payload = json.loads(result.stdout.decode("utf-8"))
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["error"]["code"], "bootstrap_unknown")
                self.assertNotIn("secret", result.stdout.decode("utf-8").lower())
                self.assertNotIn("Traceback", result.stderr.decode("utf-8", errors="replace"))
                self._assert_not_running(marker)
            finally:
                self._cleanup_marked_process(marker)

    def test_kills_and_reaps_descendant_created_after_initial_sigkill_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "late-grandchild.pid"
            try:
                result = subprocess.run(
                    self._scenario_command("late-descendant", marker),
                    capture_output=True,
                    timeout=8,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                payload = json.loads(result.stdout.decode("utf-8"))
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["error"]["code"], "operation_children_leaked")
                self._assert_reaped(marker)
            finally:
                # Keep a broken implementation from leaking a fixture process
                # if the assertion above is what fails on Linux CI.
                if marker.exists():
                    try:
                        os.kill(int(marker.read_text(encoding="ascii")), signal.SIGKILL)
                    except (OSError, ValueError):
                        pass

    def test_timed_out_synchronous_extract_kills_setsid_grandchild_within_cleanup_reserve(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "grandchild.pid"
            started = time.monotonic()
            try:
                result = _run_bounded_process(
                    self._scenario_command("timeout", marker),
                    env={"PATH": os.environ.get("PATH", ""), "HOME": temporary, "TMPDIR": temporary},
                    timeout=0.5,
                )
                elapsed = time.monotonic() - started
                self.assertTrue(result.timed_out)
                self.assertLess(elapsed, 5)
                self._assert_not_running(marker)
            finally:
                self._cleanup_marked_process(marker)


if __name__ == "__main__":
    unittest.main()
