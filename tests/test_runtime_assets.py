import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace


class RuntimeAssetBuilderTests(unittest.TestCase):
    def test_default_asset_root_points_to_versioned_bundle_directory(self):
        from bot.playwright_runtime import DEFAULT_ASSET_ROOT
        from scripts.vendor_playwright_runtime import DEFAULT_OUTPUT

        expected = (
            Path(__file__).resolve().parents[1]
            / "vendor"
            / "playwright-runtime-v1-linux-x86_64"
        )
        self.assertEqual(DEFAULT_ASSET_ROOT, expected)
        self.assertEqual(DEFAULT_OUTPUT, expected)

    def _packages(self, payloads):
        return [
            SimpleNamespace(
                name=f"pkg-{index}",
                filename=f"pkg-{index}.deb",
                url=f"https://deb.debian.org/package/{index}",
                sha256=hashlib.sha256(payload).hexdigest(),
            )
            for index, payload in enumerate(payloads)
        ]

    def test_builds_manifest_and_verified_asset_files(self):
        from scripts.vendor_playwright_runtime import build_asset_bundle

        payloads = [b"first deb payload", b"second deb payload"]
        packages = self._packages(payloads)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, output = root / "source", root / "bundle"
            source.mkdir()
            for package, payload in zip(packages, payloads):
                (source / package.filename).write_bytes(payload)

            digest = build_asset_bundle(source, output, packages=packages)

            manifest_path = output / "manifest.json"
            manifest_bytes = manifest_path.read_bytes()
            manifest = json.loads(manifest_bytes)
            self.assertEqual(digest, hashlib.sha256(manifest_bytes).hexdigest())
            self.assertEqual([item["filename"] for item in manifest["packages"]], [p.filename for p in packages])
            for package, payload, item in zip(packages, payloads, manifest["packages"]):
                self.assertEqual((output / package.filename).read_bytes(), payload)
                self.assertEqual(item["sha256"], hashlib.sha256(payload).hexdigest())
                self.assertEqual(item["size"], len(payload))

    def test_checksum_mismatch_does_not_publish_partial_bundle(self):
        from scripts.vendor_playwright_runtime import build_asset_bundle

        package = SimpleNamespace(name="pkg", filename="pkg.deb", url="https://deb.debian.org/pkg", sha256="0" * 64)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, output = root / "source", root / "bundle"
            source.mkdir()
            (source / package.filename).write_bytes(b"not the pinned package")

            with self.assertRaisesRegex(ValueError, "checksum"):
                build_asset_bundle(source, output, packages=[package])

            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob("bundle.staging-*")), [])

    def test_missing_input_does_not_publish_manifest(self):
        from scripts.vendor_playwright_runtime import build_asset_bundle

        package = SimpleNamespace(name="pkg", filename="pkg.deb", url="https://deb.debian.org/pkg", sha256="0" * 64)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, output = root / "source", root / "bundle"
            source.mkdir()

            with self.assertRaisesRegex(ValueError, "missing"):
                build_asset_bundle(source, output, packages=[package])

            self.assertFalse(output.exists())

    def test_atomic_publish_refuses_competing_empty_destination(self):
        from scripts import vendor_playwright_runtime

        payload = b"verified package"
        package = self._packages([payload])[0]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, output = root / "source", root / "bundle"
            source.mkdir()
            (source / package.filename).write_bytes(payload)

            original_publish = vendor_playwright_runtime._publish_directory_no_replace

            def competing_publisher(stage, destination):
                # Model another builder creating an empty output after the
                # initial existence check but immediately before publication.
                destination.mkdir()
                (destination / "kept.txt").write_text("another builder won", encoding="utf-8")
                return original_publish(stage, destination)

            with patch.object(
                vendor_playwright_runtime,
                "_publish_directory_no_replace",
                side_effect=competing_publisher,
            ):
                with self.assertRaises(OSError):
                    vendor_playwright_runtime.build_asset_bundle(source, output, packages=[package])

            self.assertEqual((output / "kept.txt").read_text(encoding="utf-8"), "another builder won")
            self.assertEqual(list(root.glob("bundle.staging-*")), [])


if __name__ == "__main__":
    unittest.main()
