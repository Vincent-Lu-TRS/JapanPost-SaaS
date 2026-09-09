import os
import tempfile
import unittest
from pathlib import Path


class PlaywrightRuntimeTests(unittest.TestCase):
    def test_manifest_covers_full_chromium_shared_library_chain(self):
        from bot.playwright_runtime import (
            REQUIRED_RUNTIME_LIBRARY_NAMES,
            REQUIRED_RUNTIME_PACKAGES,
        )

        required_libraries = {
            "libglib-2.0.so.0",
            "libgobject-2.0.so.0",
            "libgio-2.0.so.0",
            "libnspr4.so",
            "libnss3.so",
            "libnssutil3.so",
            "libsmime3.so",
            "libatk-1.0.so.0",
            "libatk-bridge-2.0.so.0",
            "libdbus-1.so.3",
            "libX11.so.6",
            "libXcomposite.so.1",
            "libXdamage.so.1",
            "libXext.so.6",
            "libXfixes.so.3",
            "libXrandr.so.2",
            "libgbm.so.1",
            "libexpat.so.1",
            "libxcb.so.1",
            "libxkbcommon.so.0",
            "libudev.so.1",
            "libasound.so.2",
            "libatspi.so.0",
            "libXi.so.6",
            "libXrender.so.1",
            "libdrm.so.2",
            "libwayland-server.so.0",
        }
        package_names = {package.name for package in REQUIRED_RUNTIME_PACKAGES}

        self.assertTrue(required_libraries.issubset(set(REQUIRED_RUNTIME_LIBRARY_NAMES)))
        self.assertTrue(
            {
                "libglib2.0-0t64",
                "libgbm1",
                "libudev1",
                "libllvm19",
                "libz3-4",
            }.issubset(package_names)
        )

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

    def test_partial_system_runtime_does_not_skip_remaining_chromium_dependencies(self):
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

            original_three = {"libnspr4.so", "libnss3.so", "libasound.so.2"}
            result = prepare_playwright_runtime(
                runtime_root=runtime_root,
                platform_name="Linux",
                machine_name="x86_64",
                library_checker=lambda soname: soname in original_three,
                downloader=fake_downloader,
                extractor=fake_extractor,
                apply_to_process_env=False,
            )

            self.assertTrue(result.ok)
            self.assertEqual(
                downloaded,
                [package.name for package in REQUIRED_RUNTIME_PACKAGES],
                "three old libraries being present must not hide missing transitive libs",
            )

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
