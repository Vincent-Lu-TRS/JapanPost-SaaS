from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAFE_FIELDS = {
    "release_digest",
    "asset_digest",
    "playwright_version",
    "browser_revision",
    "platform",
    "stage",
    "error_code",
    "status",
    "elapsed_ms",
}
PROJECT_FILES = (
    "app.py",
    "requirements.txt",
    "bot/__init__.py",
    "bot/automation.py",
    "bot/browser_bootstrap.py",
    "bot/browser_runtime.py",
    "bot/playwright_runtime.py",
    "bot/runtime_fence.py",
    "bot/runtime_profile.json",
    "scripts/check_browser_runtime.py",
    "safe_logging.py",
)
ASSET_RELATIVE_PATH = Path("vendor") / "playwright-runtime-v1-linux-x86_64"


def _linux_x86_64() -> bool:
    return sys.platform.startswith("linux") and platform.machine().lower() in {
        "x86_64",
        "amd64",
    }


def _copy_runtime_project(destination: Path, *, include_assets: bool) -> Path:
    """Make an isolated checkout so cold-start never clears a user cache."""

    for relative in PROJECT_FILES:
        source = PROJECT_ROOT / relative
        if not source.is_file():
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

    assets = destination / ASSET_RELATIVE_PATH
    assets.parent.mkdir(parents=True, exist_ok=True)
    if include_assets:
        source_assets = PROJECT_ROOT / ASSET_RELATIVE_PATH
        if source_assets.is_dir():
            assets.symlink_to(source_assets, target_is_directory=True)
        else:
            assets.mkdir()
    else:
        assets.mkdir()

    profile_path = destination / "bot" / "runtime_profile.json"
    if profile_path.is_file():
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        profile["browser_root"] = str(destination / "browser-cache")
        profile_path.write_text(json.dumps(profile), encoding="utf-8")
    return destination


def _run_checker(project_root: Path, *, env: dict[str, str] | None = None, timeout: int = 300):
    return subprocess.run(
        [sys.executable, str(project_root / "scripts" / "check_browser_runtime.py")],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _json_payload(stdout: str) -> dict[str, object]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        raise AssertionError("checker stdout must contain only one safe JSON object") from None
    if not isinstance(payload, dict):
        raise AssertionError("checker stdout must contain a JSON object")
    return payload


def _assert_safe_payload(test: unittest.TestCase, payload: dict[str, object]) -> None:
    test.assertEqual(set(payload), SAFE_FIELDS)
    for field in (
        "release_digest",
        "asset_digest",
        "playwright_version",
        "browser_revision",
        "platform",
        "stage",
        "error_code",
        "status",
    ):
        test.assertIsInstance(payload[field], str)
    test.assertIsInstance(payload["elapsed_ms"], int)
    test.assertGreaterEqual(payload["elapsed_ms"], 0)


def _processes_using_path(path: Path) -> list[int]:
    """Return Linux PIDs whose command line mentions this isolated browser root."""
    if not Path("/proc").is_dir():
        return []
    needle = str(path).encode()
    matches = []
    for proc_entry in Path("/proc").iterdir():
        if not proc_entry.name.isdigit():
            continue
        try:
            command_line = (proc_entry / "cmdline").read_bytes()
        except OSError:
            continue
        if needle in command_line:
            matches.append(int(proc_entry.name))
    return matches


def _assert_no_browser_processes(test: unittest.TestCase, browser_root: Path) -> None:
    deadline = time.monotonic() + 5
    remaining = _processes_using_path(browser_root)
    while remaining and time.monotonic() < deadline:
        time.sleep(0.1)
        remaining = _processes_using_path(browser_root)
    test.assertFalse(remaining, f"cold-start left Chromium processes running: {remaining}")


class RuntimeColdStartTests(unittest.TestCase):
    @unittest.skipUnless(_linux_x86_64(), "real Chromium bootstrap is supported only on Linux x86_64")
    def test_checker_contract_includes_only_safe_fields_after_real_blank_page_launch(self):
        result = _run_checker(PROJECT_ROOT)
        self.assertEqual(
            result.returncode,
            0,
            "runtime checker must return success only after a real browser launch; "
            f"safe result={result.stdout.strip()[:1000]}",
        )

        payload = _json_payload(result.stdout)
        _assert_safe_payload(self, payload)
        self.assertEqual(payload["stage"], "complete")
        self.assertEqual(payload["error_code"], "none")
        self.assertEqual(payload["status"], "ready")

    def test_asset_verification_failure_emits_safe_json_and_returns_nonzero(self):
        with tempfile.TemporaryDirectory(prefix="jppost-runtime-assets-fail-") as temporary:
            project_root = Path(temporary) / "project"
            project_root.mkdir()
            _copy_runtime_project(project_root, include_assets=False)

            result = _run_checker(project_root, timeout=30)

        self.assertNotEqual(result.returncode, 0, "asset verification failure must fail the checker")
        payload = _json_payload(result.stdout)
        _assert_safe_payload(self, payload)
        self.assertEqual(payload["stage"], "asset_verification")
        self.assertEqual(payload["error_code"], "asset_missing")
        self.assertEqual(payload["status"], "error")

    @unittest.skipUnless(_linux_x86_64(), "fresh Chromium bootstrap is supported only on Linux x86_64")
    def test_real_cold_start_uses_an_empty_isolated_browser_cache(self):
        with tempfile.TemporaryDirectory(prefix="jppost-runtime-cold-start-") as temporary:
            temporary_root = Path(temporary)
            project_root = temporary_root / "project"
            project_root.mkdir()
            _copy_runtime_project(project_root, include_assets=True)

            profile = json.loads(
                (project_root / "bot" / "runtime_profile.json").read_text(encoding="utf-8")
            )
            browser_root = Path(profile["browser_root"])
            self.assertFalse(browser_root.exists(), "cold-start fixture must begin without an installed browser")

            isolated_tmp = temporary_root / "tmp"
            isolated_tmp.mkdir()
            native_runtime = isolated_tmp / "jppost-playwright-runtime"
            self.assertFalse(native_runtime.exists(), "cold-start native runtime fixture must begin empty")
            environment = os.environ.copy()
            environment["TMPDIR"] = str(isolated_tmp)
            result = _run_checker(project_root, env=environment, timeout=300)

            self.assertEqual(
                result.returncode,
                0,
                "cold-start checker must succeed with a real Chromium launch; "
                f"safe result={result.stdout.strip()[:1000]}",
            )
            payload = _json_payload(result.stdout)
            _assert_safe_payload(self, payload)
            self.assertEqual(payload["stage"], "complete")
            self.assertEqual(payload["error_code"], "none")
            self.assertEqual(payload["status"], "ready")
            self.assertTrue(browser_root.is_dir(), "cold-start must populate its isolated browser cache")
            self.assertTrue(any(browser_root.iterdir()), "cold-start must install the pinned Chromium runtime")

            # The production native-library preparer verifies and extracts the
            # checked-in Debian bundle only when at least one required library
            # is absent. This marker proves extraction occurred and prevents a
            # base image with preinstalled libraries from masking bundle issues.
            ready_marker = native_runtime / str(payload["asset_digest"]) / ".ready.json"
            self.assertTrue(
                ready_marker.is_file(),
                "cold-start succeeded without extracting the vendored Debian library bundle",
            )
            native_metadata = json.loads(ready_marker.read_text(encoding="utf-8"))
            self.assertEqual(native_metadata.get("manifest_digest"), payload["asset_digest"])
            self.assertTrue(native_metadata.get("libraries"), "bundle extraction marker has no libraries")
            from bot.playwright_runtime import REQUIRED_RUNTIME_LIBRARY_NAMES

            self.assertEqual(
                set(native_metadata["libraries"]),
                set(REQUIRED_RUNTIME_LIBRARY_NAMES),
            )

            _assert_no_browser_processes(self, browser_root)


if __name__ == "__main__":
    unittest.main()
