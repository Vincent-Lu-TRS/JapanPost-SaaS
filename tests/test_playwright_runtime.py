import hashlib
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class PlaywrightRuntimeTests(unittest.TestCase):
    def test_manifest_covers_full_chromium_shared_library_chain(self):
        from bot.playwright_runtime import REQUIRED_RUNTIME_LIBRARY_NAMES, REQUIRED_RUNTIME_PACKAGES

        required_libraries = {
            "libglib-2.0.so.0", "libgobject-2.0.so.0", "libgio-2.0.so.0", "libnspr4.so",
            "libnss3.so", "libnssutil3.so", "libsmime3.so", "libatk-1.0.so.0",
            "libatk-bridge-2.0.so.0", "libdbus-1.so.3", "libX11.so.6", "libXcomposite.so.1",
            "libXdamage.so.1", "libXext.so.6", "libXfixes.so.3", "libXrandr.so.2",
            "libgbm.so.1", "libexpat.so.1", "libxcb.so.1", "libxkbcommon.so.0", "libudev.so.1",
            "libasound.so.2", "libatspi.so.0", "libXi.so.6", "libXrender.so.1", "libdrm.so.2",
            "libwayland-server.so.0",
        }
        package_names = {package.name for package in REQUIRED_RUNTIME_PACKAGES}

        self.assertTrue(required_libraries.issubset(set(REQUIRED_RUNTIME_LIBRARY_NAMES)))
        self.assertTrue(
            {
                "libcairo.so.2",
                "libcups.so.2",
                "libpango-1.0.so.0",
            }.issubset(set(REQUIRED_RUNTIME_LIBRARY_NAMES))
        )
        self.assertTrue(
            {"libcairo2", "libcups2", "libpango-1.0-0", "libglib2.0-0", "libllvm15"}.issubset(package_names)
        )
        self.assertFalse(any(name.endswith("t64") for name in package_names))

    def _asset_fixture(self, parent: Path):
        from bot import playwright_runtime as runtime

        package_payloads = (b"mock deb one", b"mock deb two")
        packages = tuple(
            runtime.RuntimePackage(
                name=f"mock-package-{index}",
                filename=f"mock-package-{index}.deb",
                url=f"https://deb.debian.org/mock/package-{index}.deb",
                sha256=hashlib.sha256(payload).hexdigest(),
            )
            for index, payload in enumerate(package_payloads)
        )
        asset_root = parent / "assets"
        asset_root.mkdir()
        entries = []
        for package, payload in zip(packages, package_payloads):
            (asset_root / package.filename).write_bytes(payload)
            entries.append(
                {
                    "name": package.name,
                    "filename": package.filename,
                    "url": package.url,
                    "size": len(payload),
                    "sha256": package.sha256,
                }
            )
        manifest = json.dumps(
            {"format": 1, "platform": "linux-x86_64", "packages": entries},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        (asset_root / "manifest.json").write_bytes(manifest)
        return runtime, packages, asset_root

    def _fake_extractor(self, calls):
        from bot.playwright_runtime import REQUIRED_RUNTIME_LIBRARY_NAMES

        def extract(package_path, destination):
            calls.append(package_path.name)
            library_dir = destination / "usr" / "lib" / "x86_64-linux-gnu"
            library_dir.mkdir(parents=True, exist_ok=True)
            for soname in REQUIRED_RUNTIME_LIBRARY_NAMES:
                (library_dir / soname).write_bytes(f"verified:{soname}".encode())

        return extract

    def test_verified_bundled_assets_prepare_offline_and_reuse_valid_runtime(self):
        from bot.playwright_runtime import prepare_playwright_runtime

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime, packages, asset_root = self._asset_fixture(root)
            extraction_calls = []
            runtime_root = root / "runtime"
            original_ld_path = os.environ.get("LD_LIBRARY_PATH")
            with (
                patch.object(
                    socket.socket,
                    "connect",
                    side_effect=AssertionError("offline asset preparation attempted a socket connection"),
                ),
                patch(
                    "urllib.request.urlopen",
                    side_effect=AssertionError("offline asset preparation attempted a URL open"),
                ),
                patch.object(runtime, "REQUIRED_RUNTIME_PACKAGES", packages),
            ):
                result = prepare_playwright_runtime(
                    env={"PATH": "/usr/bin", "LD_LIBRARY_PATH": "/system/lib"},
                    runtime_root=runtime_root,
                    asset_root=asset_root,
                    platform_name="Linux",
                    machine_name="x86_64",
                    library_checker=lambda _soname: False,
                    extractor=self._fake_extractor(extraction_calls),
                )
                self.assertTrue(result.ok, result.message)
                self.assertFalse(result.downloaded)
                self.assertFalse(result.reused)
                self.assertEqual(len(extraction_calls), len(packages))
                self.assertEqual(
                    result.env["LD_LIBRARY_PATH"],
                    f"{result.lib_dir}{os.pathsep}/system/lib",
                )
                self.assertEqual(os.environ.get("LD_LIBRARY_PATH"), original_ld_path)
                marker = result.lib_dir.parents[2] / ".ready.json"
                self.assertTrue(marker.is_file())

                second = prepare_playwright_runtime(
                    env={"PATH": "/usr/bin"},
                    runtime_root=runtime_root,
                    asset_root=asset_root,
                    platform_name="Linux",
                    machine_name="x86_64",
                    library_checker=lambda _soname: False,
                    extractor=lambda *_args: self.fail("verified runtime should be reused"),
                )

            self.assertTrue(second.ok, second.message)
            self.assertTrue(second.reused)
            self.assertEqual(second.manifest_digest, result.manifest_digest)

    def test_partial_system_libraries_are_not_mistaken_for_a_complete_runtime(self):
        from bot import playwright_runtime as runtime
        from bot.playwright_runtime import REQUIRED_RUNTIME_LIBRARY_NAMES, prepare_playwright_runtime

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime_module, packages, asset_root = self._asset_fixture(root)
            extraction_calls = []
            present = set(REQUIRED_RUNTIME_LIBRARY_NAMES[:3])
            with patch.object(runtime_module, "REQUIRED_RUNTIME_PACKAGES", packages):
                result = prepare_playwright_runtime(
                    runtime_root=root / "runtime",
                    asset_root=asset_root,
                    platform_name="Linux",
                    machine_name="x86_64",
                    library_checker=lambda soname: soname in present,
                    extractor=self._fake_extractor(extraction_calls),
                    apply_to_process_env=False,
                )

            self.assertTrue(result.ok, result.message)
            self.assertEqual(len(extraction_calls), len(packages))
            self.assertTrue(result.env["LD_LIBRARY_PATH"].startswith(str(result.lib_dir)))

    def test_missing_bundle_fails_closed_without_publishing_runtime(self):
        from bot.playwright_runtime import prepare_playwright_runtime

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = prepare_playwright_runtime(
                runtime_root=root / "runtime",
                asset_root=root / "missing-assets",
                platform_name="Linux",
                machine_name="x86_64",
                library_checker=lambda _soname: False,
                extractor=lambda *_args: self.fail("must not extract when the manifest is missing"),
            )

            self.assertFalse(result.ok)
            self.assertEqual(result.error_code, "asset_missing")
            self.assertFalse((root / "runtime").exists())

    def test_expired_runtime_deadline_fails_before_system_library_shortcut(self):
        from bot.playwright_runtime import prepare_playwright_runtime

        with tempfile.TemporaryDirectory() as tmp, patch(
            "bot.playwright_runtime._load_verified_assets", return_value=("manifest-digest", ())
        ) as load_assets:
            result = prepare_playwright_runtime(
                runtime_root=Path(tmp) / "runtime",
                asset_root=Path(tmp) / "assets",
                platform_name="Linux",
                machine_name="x86_64",
                library_checker=lambda _soname: True,
                deadline=0,
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "bootstrap_timeout")
        load_assets.assert_not_called()

    def test_package_checksum_mismatch_fails_before_extraction(self):
        from bot import playwright_runtime as runtime
        from bot.playwright_runtime import prepare_playwright_runtime

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime_module, packages, asset_root = self._asset_fixture(root)
            with (asset_root / packages[0].filename).open("ab") as stream:
                stream.write(b"tamper")
            with patch.object(runtime_module, "REQUIRED_RUNTIME_PACKAGES", packages):
                result = prepare_playwright_runtime(
                    runtime_root=root / "runtime",
                    asset_root=asset_root,
                    platform_name="Linux",
                    machine_name="x86_64",
                    library_checker=lambda _soname: False,
                    extractor=lambda *_args: self.fail("must not extract a bad asset"),
                )

            self.assertFalse(result.ok)
            self.assertEqual(result.error_code, "asset_checksum")
            self.assertFalse((root / "runtime").exists())

    def test_tampered_cached_library_is_rebuilt_before_reuse(self):
        from bot import playwright_runtime as runtime
        from bot.playwright_runtime import REQUIRED_RUNTIME_LIBRARY_NAMES, prepare_playwright_runtime

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime_module, packages, asset_root = self._asset_fixture(root)
            extraction_calls = []
            runtime_root = root / "runtime"
            with patch.object(runtime_module, "REQUIRED_RUNTIME_PACKAGES", packages):
                first = prepare_playwright_runtime(
                    runtime_root=runtime_root,
                    asset_root=asset_root,
                    platform_name="Linux",
                    machine_name="x86_64",
                    library_checker=lambda _soname: False,
                    extractor=self._fake_extractor(extraction_calls),
                )
                self.assertTrue(first.ok, first.message)
                (first.lib_dir / REQUIRED_RUNTIME_LIBRARY_NAMES[0]).write_bytes(b"tampered")
                second = prepare_playwright_runtime(
                    runtime_root=runtime_root,
                    asset_root=asset_root,
                    platform_name="Linux",
                    machine_name="x86_64",
                    library_checker=lambda _soname: False,
                    extractor=self._fake_extractor(extraction_calls),
                )

            self.assertTrue(second.ok, second.message)
            self.assertFalse(second.reused)
            self.assertEqual(len(extraction_calls), 2 * len(packages))
            self.assertFalse(list(runtime_root.glob("*.stale-*")))

    def test_malformed_json_ready_markers_are_rebuilt_without_sticking(self):
        from bot import playwright_runtime as runtime
        from bot.playwright_runtime import prepare_playwright_runtime

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime_module, packages, asset_root = self._asset_fixture(root)
            extraction_calls = []
            runtime_root = root / "runtime"
            with patch.object(runtime_module, "REQUIRED_RUNTIME_PACKAGES", packages):
                first = prepare_playwright_runtime(
                    runtime_root=runtime_root,
                    asset_root=asset_root,
                    platform_name="Linux",
                    machine_name="x86_64",
                    library_checker=lambda _soname: False,
                    extractor=self._fake_extractor(extraction_calls),
                )
                self.assertTrue(first.ok, first.message)
                marker = first.lib_dir.parents[2] / ".ready.json"

                for malformed in ("null", "[]", '"not-an-object"'):
                    marker.write_text(malformed, encoding="utf-8")
                    rebuilt = prepare_playwright_runtime(
                        runtime_root=runtime_root,
                        asset_root=asset_root,
                        platform_name="Linux",
                        machine_name="x86_64",
                        library_checker=lambda _soname: False,
                        extractor=self._fake_extractor(extraction_calls),
                    )
                    self.assertTrue(rebuilt.ok, rebuilt.message)
                    self.assertFalse(rebuilt.reused)
                    self.assertTrue(runtime._vendor_libraries_ready(
                        marker.parent,
                        manifest_digest=rebuilt.manifest_digest,
                        platform_fingerprint=runtime._platform_fingerprint("Linux", "x86_64"),
                    ))

            self.assertEqual(len(extraction_calls), 4 * len(packages))

    def test_extraction_failure_leaves_no_ready_directory_or_staging_tree(self):
        from bot import playwright_runtime as runtime
        from bot.playwright_runtime import prepare_playwright_runtime

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime_module, packages, asset_root = self._asset_fixture(root)
            runtime_root = root / "runtime"
            with patch.object(runtime_module, "REQUIRED_RUNTIME_PACKAGES", packages):
                result = prepare_playwright_runtime(
                    runtime_root=runtime_root,
                    asset_root=asset_root,
                    platform_name="Linux",
                    machine_name="x86_64",
                    library_checker=lambda _soname: False,
                    extractor=lambda *_args: (_ for _ in ()).throw(RuntimeError("raw failure text")),
                )

            self.assertFalse(result.ok)
            self.assertEqual(result.error_code, "asset_extract")
            self.assertNotIn("raw failure text", result.message)
            self.assertFalse(list(runtime_root.glob("*.staging-*")))
            self.assertFalse(list(runtime_root.glob("*/.ready.json")))

    def test_non_linux_runtime_does_not_require_linux_package_bundle(self):
        from bot.playwright_runtime import prepare_playwright_runtime

        with tempfile.TemporaryDirectory() as tmp:
            result = prepare_playwright_runtime(
                env={"PATH": "C:\\Windows\\System32"},
                runtime_root=Path(tmp) / "runtime",
                asset_root=Path(tmp) / "missing-assets",
                platform_name="Windows",
                machine_name="AMD64",
            )

            self.assertTrue(result.ok)
            self.assertFalse(result.downloaded)

if __name__ == "__main__":
    unittest.main()
