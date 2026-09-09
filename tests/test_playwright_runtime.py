import os
import tempfile
import unittest
from pathlib import Path


class PlaywrightRuntimeTests(unittest.TestCase):
    def test_missing_linux_runtime_libraries_are_downloaded_and_added_to_child_env(self):
        from bot.playwright_runtime import (
            REQUIRED_RUNTIME_LIBRARY_NAMES,
            REQUIRED_RUNTIME_PACKAGES,
            prepare_playwright_runtime,
        )

        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp) / "runtime"
            downloaded = []

            def fake_downloader(package, destination):
                downloaded.append(package.name)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"mock deb package")

            def fake_extractor(_package_path, destination):
                library_dir = destination / "usr" / "lib" / "x86_64-linux-gnu"
                library_dir.mkdir(parents=True, exist_ok=True)
                for soname in REQUIRED_RUNTIME_LIBRARY_NAMES:
                    (library_dir / soname).write_bytes(b"mock shared library")

            result = prepare_playwright_runtime(
                env={"PATH": "/usr/bin", "LD_LIBRARY_PATH": "/system/lib"},
                runtime_root=runtime_root,
                platform_name="Linux",
                machine_name="x86_64",
                library_checker=lambda _soname: False,
                downloader=fake_downloader,
                extractor=fake_extractor,
                apply_to_process_env=False,
            )

            self.assertTrue(result.ok)
            self.assertEqual(
                downloaded,
                [package.name for package in REQUIRED_RUNTIME_PACKAGES],
            )
            self.assertEqual(
                result.env["LD_LIBRARY_PATH"],
                f"{result.lib_dir}{os.pathsep}/system/lib",
            )
            self.assertTrue((runtime_root / ".ready").exists())

    def test_ready_runtime_is_reused_without_a_second_download(self):
        from bot.playwright_runtime import (
            REQUIRED_RUNTIME_LIBRARY_NAMES,
            RUNTIME_VERSION,
            prepare_playwright_runtime,
        )

        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp) / "runtime"
            library_dir = runtime_root / "usr" / "lib" / "x86_64-linux-gnu"
            library_dir.mkdir(parents=True)
            for soname in REQUIRED_RUNTIME_LIBRARY_NAMES:
                (library_dir / soname).write_bytes(b"ready")
            (runtime_root / ".ready").write_text(RUNTIME_VERSION, encoding="utf-8")

            def unexpected_download(*_args):
                raise AssertionError("a ready runtime must not be downloaded again")

            result = prepare_playwright_runtime(
                env={"PATH": "/usr/bin"},
                runtime_root=runtime_root,
                platform_name="Linux",
                machine_name="x86_64",
                library_checker=lambda _soname: False,
                downloader=unexpected_download,
                apply_to_process_env=False,
            )

            self.assertTrue(result.ok)
            self.assertTrue(result.reused)
            self.assertEqual(result.env["LD_LIBRARY_PATH"], str(result.lib_dir))

    def test_non_linux_runtime_does_not_download_linux_packages(self):
        from bot.playwright_runtime import prepare_playwright_runtime

        with tempfile.TemporaryDirectory() as tmp:
            result = prepare_playwright_runtime(
                env={"PATH": "C:\\Windows\\System32"},
                runtime_root=Path(tmp) / "runtime",
                platform_name="Windows",
                machine_name="AMD64",
                downloader=lambda *_args: self.fail("Windows must not download Linux libraries"),
                apply_to_process_env=False,
            )

            self.assertTrue(result.ok)
            self.assertFalse(result.downloaded)


if __name__ == "__main__":
    unittest.main()
