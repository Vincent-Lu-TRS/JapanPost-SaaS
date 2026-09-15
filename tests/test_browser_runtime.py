import hashlib
import io
import json
import threading
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bot.browser_bootstrap import RuntimeHandle, RuntimeSetupError
from bot.browser_runtime import (
    PROBE_PREFIX,
    _PROBE_SCRIPT,
    BrowserRuntimeBootstrap,
    OUTPUT_CAPTURE_MAX_BYTES,
    ProcessResult,
    ensure_browser_runtime,
    _run_bounded_process,
    _classify_install_failure,
    _release_digest,
    _terminate_process_tree,
    load_runtime_profile,
    runtime_environment_fingerprint,
    _runtime_environment_supported,
    probe_browser_runtime_fresh,
)


class BrowserRuntimeTests(unittest.TestCase):
    def test_disposable_probe_retries_only_explicit_target_closed_errors(self):
        self.assertIn('type(exc).__name__ == "TargetClosedError"', _PROBE_SCRIPT)
        self.assertNotIn('"targetclosederror",', _PROBE_SCRIPT.lower())

    def _supported_environment(self):
        return {
            "system": "linux", "os_id": "debian", "os_version": "12",
            "machine": "x86_64", "python_major": 3, "python_minor": 12,
            "libc_name": "glibc", "libc_version": "2.36",
        }

    def test_environment_fingerprint_reads_only_safe_os_release_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            release = Path(tmp) / "os-release"
            release.write_text(
                'ID=debian\nVERSION_ID="12"\nPRETTY_NAME="Debian 12 private host label"\n',
                encoding="utf-8",
            )
            with patch("bot.browser_runtime.platform.system", return_value="Linux"), patch(
                "bot.browser_runtime.platform.machine", return_value="AMD64"
            ), patch("bot.browser_runtime.platform.libc_ver", return_value=("glibc", "2.36")), patch(
                "bot.browser_runtime.sys.version_info", types.SimpleNamespace(major=3, minor=12)
            ):
                fingerprint = runtime_environment_fingerprint(os_release_path=release)

        self.assertEqual(
            fingerprint,
            {
                "system": "linux",
                "os_id": "debian",
                "os_version": "12",
                "machine": "x86_64",
                "python_major": 3,
                "python_minor": 12,
                "libc_name": "glibc",
                "libc_version": "2.36",
            },
        )
        self.assertNotIn(str(release), repr(fingerprint))
        self.assertNotIn("private host label", repr(fingerprint))

    def test_runtime_compatibility_requires_tested_linux_python_and_glibc(self):
        profile = load_runtime_profile()
        supported = {
            "system": "linux",
            "os_id": "debian",
            "os_version": "12",
            "machine": "x86_64",
            "python_major": 3,
            "python_minor": 12,
            "libc_name": "glibc",
            "libc_version": "2.36",
        }
        self.assertTrue(_runtime_environment_supported(supported, profile))
        for key, value in (
            ("os_version", "11"),
            ("python_minor", 13),
            ("libc_name", "musl"),
            ("libc_version", "2.31"),
            ("machine", "aarch64"),
        ):
            with self.subTest(key=key, value=value):
                unsupported = dict(supported)
                unsupported[key] = value
                self.assertFalse(_runtime_environment_supported(unsupported, profile))

    def test_fresh_cloud_probe_prepares_and_launches_instead_of_using_warm_cache(self):
        handle = RuntimeHandle("profile-key", Path("/tmp/chrome"), "r1", {})
        bootstrap = Mock()
        bootstrap.profile_key.return_value = "profile-key"
        bootstrap.prepare.return_value = handle
        profile = {"prepare_timeout_seconds": 90, "compatibility": {}}
        fingerprint = {
            "system": "linux", "os_id": "debian", "os_version": "12",
            "machine": "x86_64", "python_major": 3, "python_minor": 12,
            "libc_name": "glibc", "libc_version": "2.36",
        }

        with patch("bot.browser_runtime.BrowserRuntimeBootstrap", return_value=bootstrap), patch(
            "bot.browser_runtime.load_runtime_profile", return_value=profile
        ), patch("bot.browser_runtime.runtime_environment_fingerprint", return_value=fingerprint), patch(
            "bot.browser_runtime._runtime_environment_supported", return_value=True
        ), patch("bot.browser_runtime.time.monotonic", return_value=100.0), patch(
            "bot.browser_runtime.log_runtime_environment"
        ) as record_profile:
            result = probe_browser_runtime_fresh()

        self.assertIs(result, handle)
        bootstrap.prepare.assert_called_once_with("profile-key", 190.0)
        record_profile.assert_called_once()

    def test_prepare_stops_before_browser_or_package_work_on_unsupported_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime_preparer = Mock()
            bootstrap = self._bootstrap(root, lambda *_args, **_kwargs: ProcessResult(0))
            bootstrap.runtime_preparer = runtime_preparer
            unsupported = {
                "system": "linux", "os_id": "debian", "os_version": "11",
                "machine": "x86_64", "python_major": 3, "python_minor": 12,
                "libc_name": "glibc", "libc_version": "2.31",
            }
            with patch("bot.browser_runtime.runtime_environment_fingerprint", return_value=unsupported), patch(
                "bot.browser_runtime.sys.platform", "linux"
            ), patch("bot.browser_runtime.platform.machine", return_value="x86_64"):
                with self.assertRaises(RuntimeSetupError) as caught:
                    bootstrap.prepare_in_process("profile-key", __import__("time").monotonic() + 10)

            self.assertEqual(caught.exception.code, "profile_unsupported")
            runtime_preparer.assert_not_called()

    def test_release_digest_changes_when_runtime_fence_contents_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            release_files = (
                "app.py",
                "bot/automation.py",
                "bot/browser_bootstrap.py",
                "bot/browser_runtime.py",
                "bot/playwright_runtime.py",
                "bot/runtime_fence.py",
                "bot/runtime_profile.json",
                "requirements.txt",
            )
            for relative in release_files:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"initial:{relative}", encoding="utf-8")

            before = _release_digest(root)
            (root / "bot/runtime_fence.py").write_text("changed fence protocol", encoding="utf-8")

            self.assertNotEqual(before, _release_digest(root))

    def _profile_file(self, root: Path, *, attempts=3, backoff=(0, 0)) -> Path:
        source = Path(__file__).resolve().parents[1] / "bot" / "runtime_profile.json"
        profile = json.loads(source.read_text(encoding="utf-8"))
        profile["browser_root"] = str(root / "ms-playwright")
        profile["install_max_attempts"] = attempts
        profile["install_retry_backoff_seconds"] = list(backoff)
        path = root / "runtime_profile.json"
        path.write_text(json.dumps(profile), encoding="utf-8")
        return path

    def _bootstrap(self, root: Path, runner, *, attempts=3):
        profile_path = self._profile_file(root, attempts=attempts)
        asset_root = root / "assets"
        asset_root.mkdir()
        manifest = b'{"format":1,"platform":"linux-x86_64","packages":[]}'
        (asset_root / "manifest.json").write_bytes(manifest)
        return BrowserRuntimeBootstrap(
            profile_path=profile_path,
            project_root=Path(__file__).resolve().parents[1],
            asset_root=asset_root,
            runtime_preparer=lambda **_kwargs: type(
                "RuntimeResult", (), {"ok": True, "env": {"PATH": "/usr/bin"}, "manifest_digest": hashlib.sha256(manifest).hexdigest()}
            )(),
            process_runner=runner,
        )

    def test_profile_rejects_unbounded_retry_and_nonabsolute_browser_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self._profile_file(root)
            profile = json.loads(path.read_text(encoding="utf-8"))
            profile["install_max_attempts"] = 99
            path.write_text(json.dumps(profile), encoding="utf-8")
            with self.assertRaises(RuntimeSetupError):
                load_runtime_profile(path)
            profile["install_max_attempts"] = 3
            profile["browser_root"] = "relative/path"
            path.write_text(json.dumps(profile), encoding="utf-8")
            with self.assertRaises(RuntimeSetupError):
                load_runtime_profile(path)

    def test_runtime_identity_guard_rejects_replaced_handle(self):
        original_handle = RuntimeHandle("profile-key", Path("/mock/chrome"), "r1", {})
        replacement_handle = RuntimeHandle("profile-key", Path("/mock/chrome"), "r1", {})
        manager = Mock()
        manager.ensure_ready.return_value = replacement_handle

        with patch("bot.browser_runtime.get_browser_runtime_manager", return_value=manager), patch(
            "bot.browser_runtime.build_runtime_profile_key", return_value="profile-key"
        ), patch("bot.browser_runtime._runtime_environment_supported", return_value=True), self.assertRaises(
            RuntimeSetupError
        ) as caught:
            ensure_browser_runtime(expected_handle=original_handle)

        self.assertEqual(caught.exception.code, "runtime_changed")
        manager.ensure_ready.assert_called_once_with("profile-key")

    def test_installer_error_classification_retries_only_known_transient_failures(self):
        self.assertEqual(_classify_install_failure(ProcessResult(1, stderr="HTTP Error 503"))[:3], ("browser_download", 503, True))
        self.assertEqual(_classify_install_failure(ProcessResult(1, stderr="HTTP Error 404"))[:3], ("browser_install", 404, False))
        self.assertEqual(_classify_install_failure(ProcessResult(1, stderr="ENOTFOUND"))[:3], ("browser_download", None, True))

    def test_installer_classifies_playwright_cli_download_error_format(self):
        self.assertEqual(
            _classify_install_failure(
                ProcessResult(1, stderr="Download failed: server returned code 503 body Service Unavailable")
            )[:3],
            ("browser_download", 503, True),
        )
        self.assertEqual(
            _classify_install_failure(
                ProcessResult(1, stderr="Download failed: server returned code 404 body Not Found")
            )[:3],
            ("browser_install", 404, False),
        )
        self.assertEqual(
            _classify_install_failure(
                ProcessResult(1, stderr="Download failed: server returned code 429 body Too Many Requests\nRetry-After: 3")
            ),
            ("browser_download", 429, True, 3.0),
        )
        self.assertEqual(
            _classify_install_failure(
                ProcessResult(
                    1,
                    stderr=(
                        "Download failed: server returned code 404 body Not Found\n"
                        "Download failed: server returned code 503 body Unavailable"
                    ),
                )
            )[:3],
            ("browser_download", 503, True),
        )

    def test_terminated_parent_does_not_skip_killing_remaining_posix_process_group(self):
        import signal
        from unittest.mock import Mock, call, patch

        process = Mock()
        process.pid = 12345
        process.poll.return_value = 0
        process.communicate.return_value = (b"", b"")
        process.wait.return_value = 0
        process.returncode = None

        with patch("bot.browser_runtime.os.name", "posix"), patch(
            "bot.browser_runtime._linux_process_start_time", return_value="root-start"
        ), patch("bot.browser_runtime._linux_process_state", return_value="T"), patch(
            "bot.browser_runtime._linux_descendant_processes", return_value=[]
        ), patch("bot.browser_runtime.os.kill") as kill_pid, patch(
            "bot.browser_runtime.os.killpg", create=True
        ) as killpg, patch("bot.browser_runtime.signal.SIGKILL", 9, create=True), patch(
            "bot.browser_runtime.signal.SIGSTOP", 19, create=True
        ):
            _terminate_process_tree(process, root_start_time="root-start")

        self.assertEqual(
            killpg.call_args_list,
            [call(process.pid, 9)],
        )
        self.assertEqual(kill_pid.call_args_list, [call(process.pid, 19)])
        self.assertEqual(process.wait.call_args_list, [call(timeout=1)])

    def test_reaped_root_without_pidfd_never_kills_a_reused_process_group(self):
        import signal

        process = Mock()
        process.pid = 12345
        process.poll.return_value = 0
        process.returncode = 0
        process.wait.return_value = 0

        with patch("bot.browser_runtime.os.name", "posix"), patch(
            "bot.browser_runtime._linux_process_start_time", return_value=None
        ), patch("bot.browser_runtime._linux_descendant_processes", return_value=[]), patch(
            "bot.browser_runtime.os.kill"
        ) as kill_pid, patch("bot.browser_runtime.os.killpg", create=True) as killpg, patch(
            "bot.browser_runtime.signal.SIGKILL", 9, create=True
        ), patch("bot.browser_runtime.signal.SIGSTOP", 19, create=True):
            _terminate_process_tree(process)

        kill_pid.assert_not_called()
        killpg.assert_not_called()
        process.wait.assert_called_once_with(timeout=1)

    def test_detached_chromium_descendant_is_signaled_outside_original_group(self):
        import signal
        from unittest.mock import call

        process = Mock()
        process.pid = 45678
        process.poll.return_value = 0
        process.wait.return_value = 0
        process.returncode = None

        with patch("bot.browser_runtime.os.name", "posix"), patch(
            "bot.browser_runtime._linux_descendant_processes",
            return_value=[(78901, "child-start")],
        ), patch(
            "bot.browser_runtime._linux_process_start_time",
            side_effect=lambda pid: "root-start" if pid == process.pid else "child-start",
        ), patch("bot.browser_runtime._linux_process_state", return_value="T"
        ), patch("bot.browser_runtime.os.kill") as kill_pid, patch(
            "bot.browser_runtime.os.killpg", create=True
        ) as kill_group, patch("bot.browser_runtime.signal.SIGKILL", 9, create=True), patch(
            "bot.browser_runtime.signal.SIGSTOP", 19, create=True
        ):
            _terminate_process_tree(process, root_start_time="root-start")

        self.assertEqual(
            kill_pid.call_args_list,
            [
                call(process.pid, 19),
                call(78901, 19),
                call(78901, 9),
            ],
        )
        self.assertEqual(
            kill_group.call_args_list,
            [call(process.pid, 9)],
        )

    def test_process_output_capture_keeps_bounded_head_and_tail(self):
        payload = (
            b"Download failed: server returned code 503 body "
            + b"A" * (OUTPUT_CAPTURE_MAX_BYTES * 3)
            + b"FINAL-MARKER"
        )
        process = Mock()
        process.stdout = io.BytesIO(payload)
        process.stderr = io.BytesIO(payload)
        process.wait.return_value = 1
        process.returncode = 1
        process.pid = 23456

        with patch("bot.browser_runtime.os.name", "nt"), patch(
            "bot.browser_runtime.subprocess.Popen", return_value=process
        ):
            result = _run_bounded_process(["mock"], env={}, timeout=2)

        self.assertFalse(result.timed_out)
        self.assertLessEqual(len(result.stdout.encode()), OUTPUT_CAPTURE_MAX_BYTES)
        self.assertLessEqual(len(result.stderr.encode()), OUTPUT_CAPTURE_MAX_BYTES)
        self.assertEqual(
            _classify_install_failure(result)[:3],
            ("browser_download", 503, True),
        )

        retry_payload = (
            b"Download failed: server returned code 404 body Not Found\n"
            + b"C" * (OUTPUT_CAPTURE_MAX_BYTES * 2)
            + b"Download failed: server returned code 429 body Too Many Requests\nRetry-After: 7\n"
            + b"D" * (OUTPUT_CAPTURE_MAX_BYTES * 2)
            + b"Download failed: server returned code 404 body Not Found\n"
        )
        retry_process = Mock()
        retry_process.stdout = io.BytesIO(retry_payload)
        retry_process.stderr = io.BytesIO(retry_payload)
        retry_process.wait.return_value = 1
        retry_process.returncode = 1
        retry_process.pid = 23457
        with patch("bot.browser_runtime.os.name", "nt"), patch(
            "bot.browser_runtime.subprocess.Popen", return_value=retry_process
        ):
            retry_result = _run_bounded_process(["mock"], env={}, timeout=2)

        self.assertEqual(
            _classify_install_failure(retry_result),
            ("browser_download", 429, True, 7.0),
        )
        self.assertTrue(result.stdout.endswith("FINAL-MARKER"))
        self.assertTrue(result.stderr.endswith("FINAL-MARKER"))

    def test_middle_transient_status_survives_bounded_multi_mirror_output(self):
        payload = (
            b"Download failed: server returned code 404 body Not Found\\n"
            + b"A" * (OUTPUT_CAPTURE_MAX_BYTES * 2)
            + b"Download failed: server returned code 503 body Unavailable\\n"
            + b"B" * (OUTPUT_CAPTURE_MAX_BYTES * 2)
            + b"Download failed: server returned code 404 body Not Found\\n"
        )
        process = Mock()
        process.stdout = io.BytesIO(payload)
        process.stderr = io.BytesIO(payload)
        process.wait.return_value = 1
        process.returncode = 1
        process.pid = 23456

        with patch("bot.browser_runtime.os.name", "nt"), patch(
            "bot.browser_runtime.subprocess.Popen", return_value=process
        ):
            result = _run_bounded_process(["mock"], env={}, timeout=2)

        self.assertIn(503, result.observed_http_statuses)
        self.assertEqual(
            _classify_install_failure(result)[:3],
            ("browser_download", 503, True),
        )

    def test_retry_after_header_split_across_output_chunks_is_not_parsed_early(self):
        from bot.browser_runtime import _BoundedOutputCapture

        capture = _BoundedOutputCapture()
        capture.append(b"Download failed: server returned code 429 body Too Many Requests\nRetry-After: 7")
        capture.append(b"00\n")
        result = ProcessResult(
            1,
            stderr=capture.to_bytes().decode("utf-8"),
            observed_http_statuses=tuple(capture.observed_http_statuses),
            retry_after_values=tuple(capture.retry_after_values),
        )

        self.assertEqual(
            _classify_install_failure(result),
            ("browser_download", 429, True, 700.0),
        )

    def test_rate_limit_retry_after_wins_over_other_mirror_server_errors(self):
        for statuses in ((503, 429), (429, 503)):
            with self.subTest(statuses=statuses):
                result = ProcessResult(
                    1,
                    observed_http_statuses=statuses,
                    retry_after_values=("120",),
                )
                self.assertEqual(
                    _classify_install_failure(result),
                    ("browser_download", 429, True, 120.0),
                )

    def test_warm_handle_validation_rejects_same_size_same_mtime_executable_tampering(self):
        import os
        import time

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile_file = self._profile_file(root)
            profile = json.loads(profile_file.read_text(encoding="utf-8"))
            revision, version = "12345", "123.0.0.0"
            executable = Path(profile["browser_root"]) / f"chromium-{revision}" / "chrome-linux" / "chrome"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"verified executable")
            initial_stat = executable.stat()
            handle = RuntimeHandle(
                "profile-key",
                executable,
                revision,
                {"PLAYWRIGHT_BROWSERS_PATH": profile["browser_root"]},
                executable_sha256=hashlib.sha256(b"verified executable").hexdigest(),
                executable_size=initial_stat.st_size,
                executable_mtime_ns=initial_stat.st_mtime_ns,
                browser_version=version,
            )
            bootstrap = self._bootstrap(root, lambda *_args, **_kwargs: ProcessResult(0))

            with patch.object(bootstrap, "profile_key", return_value="profile-key"), patch.object(
                bootstrap, "_profile_parts", return_value=(profile, revision, version, "1.62.0")
            ), patch("bot.browser_runtime._load_verified_assets", return_value=("asset-digest", ())), patch(
                "bot.browser_runtime._manifest_digest", return_value="asset-digest"
            ), patch("bot.browser_runtime._vendor_libraries_ready", return_value=True), patch(
                "bot.browser_runtime._platform_fingerprint", return_value="linux-x86_64-glibc-test"
            ), patch("bot.browser_runtime._library_available", return_value=True), patch(
                "bot.browser_runtime._runtime_environment_supported", return_value=True
            ):
                self.assertTrue(bootstrap.validate_in_process(handle, time.monotonic() + 10))
                executable.write_bytes(b"tampered executable")
                os.utime(executable, ns=(initial_stat.st_atime_ns, initial_stat.st_mtime_ns))
                self.assertFalse(bootstrap.validate_in_process(handle, time.monotonic() + 10))

    def test_timed_out_child_pipes_trigger_process_group_cleanup_after_leader_exit(self):
        import signal
        import time
        from unittest.mock import call

        released = threading.Event()

        class MockPipe:
            def __init__(self, fd):
                self.fd = fd

            def fileno(self):
                return self.fd

            def close(self):
                pass

        class MockSelector:
            def __init__(self):
                self.registered = {}

            def register(self, stream, _event, data):
                self.registered[stream.fileno()] = (stream, data)

            def unregister(self, stream):
                self.registered.pop(stream.fileno(), None)

            def get_map(self):
                return self.registered

            def select(self, timeout):
                if not released.is_set():
                    time.sleep(min(timeout, 0.01))
                    return []
                return [
                    (types.SimpleNamespace(fileobj=stream, data=data), 1)
                    for stream, data in self.registered.values()
                ]

            def close(self):
                self.registered.clear()

        process = Mock()
        process.pid = 34567
        process.stdout = MockPipe(1)
        process.stderr = MockPipe(2)
        process.wait.return_value = 0
        process.poll.return_value = 0
        process.returncode = 0
        selector = MockSelector()

        def kill_group(_pid, _sig):
            released.set()

        with patch("bot.browser_runtime.subprocess.Popen", return_value=process), patch(
            "bot.browser_runtime.sys.platform", "linux"
        ), patch(
            "bot.browser_runtime.os.name", "posix"
        ), patch("bot.browser_runtime.selectors.DefaultSelector", return_value=selector), patch(
            "bot.browser_runtime.os.set_blocking"
        ), patch("bot.browser_runtime.os.read", side_effect=lambda _fd, _size: b"" if released.is_set() else BlockingIOError()), patch(
            "bot.browser_runtime._linux_descendant_processes", return_value=[]
        ), patch("bot.browser_runtime._linux_process_start_time", return_value="original-root"), patch(
            "bot.browser_runtime._open_linux_pidfd", return_value=45678, create=True
        ) as open_pidfd, patch("bot.browser_runtime.os.close") as close_fd, patch(
            "bot.browser_runtime.os.kill"
        ) as kill_pid, patch(
            "bot.browser_runtime.os.killpg", side_effect=kill_group, create=True
        ) as killpg, patch(
            "bot.browser_runtime.signal.SIGKILL", 9, create=True
        ), patch("bot.browser_runtime.signal.SIGSTOP", 19, create=True
        ):
            started = time.monotonic()
            result = _run_bounded_process(["mock"], env={}, timeout=0.03)
            elapsed = time.monotonic() - started

        self.assertTrue(result.timed_out)
        self.assertLess(elapsed, 1.0)
        open_pidfd.assert_called_once_with(process.pid, expected_start_time="original-root")
        close_fd.assert_called_once_with(45678)
        self.assertEqual(kill_pid.call_args_list, [call(process.pid, 19)])
        self.assertEqual(
            killpg.call_args_list,
            [call(process.pid, 9)],
        )

    def test_browser_install_retries_transient_http_failure_then_probes_explicit_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = []
            profile_file = self._profile_file(root)

            def runner(command, *, env, timeout):
                calls.append((command, env, timeout))
                if "install" in command:
                    if len([call for call in calls if "install" in call[0]]) == 1:
                        return ProcessResult(1, stderr="Download failed: server returned code 503 body Unavailable")
                    return ProcessResult(0)
                revision, version = BrowserRuntimeBootstrap()._profile_parts()[1:3]
                executable = Path(env["PLAYWRIGHT_BROWSERS_PATH"]) / f"chromium-{revision}" / "chrome-linux" / "chrome"
                executable.parent.mkdir(parents=True, exist_ok=True)
                executable.write_bytes(b"mock browser executable")
                output = PROBE_PREFIX + json.dumps({"executable": str(executable), "version": version})
                return ProcessResult(0, stdout=output)

            bootstrap = self._bootstrap(root, runner)
            with patch("bot.browser_runtime.sys.platform", "linux"), patch(
                "bot.browser_runtime.platform.machine", return_value="x86_64"
            ), patch(
                "bot.browser_runtime._runtime_environment_supported", return_value=True
            ):
                key = bootstrap.profile_key()
                handle = bootstrap.prepare_in_process(key, __import__("time").monotonic() + 10)

            install_calls = [call for call in calls if "install" in call[0]]
            probe_calls = [call for call in calls if "-c" in call[0]]
            self.assertEqual(len(install_calls), 2)
            self.assertEqual(len(probe_calls), 1)
            self.assertEqual(handle.executable_path.name, "chrome")
            self.assertEqual(handle.executable_sha256, hashlib.sha256(b"mock browser executable").hexdigest())
            self.assertEqual(handle.env["PLAYWRIGHT_BROWSERS_PATH"], json.loads(profile_file.read_text())["browser_root"])
            self.assertTrue(all(call[1]["PLAYWRIGHT_BROWSERS_PATH"] == handle.env["PLAYWRIGHT_BROWSERS_PATH"] for call in calls))

    def test_nontransient_http_failure_stops_without_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = []

            def runner(command, *, env, timeout):
                calls.append(command)
                return ProcessResult(1, stderr="Download failed: server returned code 404 body Not Found")

            bootstrap = self._bootstrap(root, runner)
            with patch("bot.browser_runtime.sys.platform", "linux"), patch(
                "bot.browser_runtime.platform.machine", return_value="x86_64"
            ), patch(
                "bot.browser_runtime._runtime_environment_supported", return_value=True
            ):
                with self.assertRaises(RuntimeSetupError) as caught:
                    bootstrap.prepare_in_process("profile-key", __import__("time").monotonic() + 10)

            self.assertEqual(caught.exception.http_status, 404)
            self.assertEqual(caught.exception.attempts, 1)
            self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
