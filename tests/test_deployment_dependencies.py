import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PINNED_PRODUCTION_VERSIONS = {
    "streamlit": "1.56.0",
    "extra-streamlit-components": "0.1.81",
    "google-auth": "2.58.0",
    "google-auth-oauthlib": "1.4.1",
    "google-api-python-client": "2.200.0",
    "gspread": "6.2.1",
    "playwright": "1.62.0",
    "google-genai": "2.23.0",
    "numpy": "2.2.6",
    "pandas": "2.2.3",
    "pyarrow": "18.1.0",
    "openpyxl": "3.1.5",
    "reportlab": "4.4.3",
    "qrcode": "8.2",
    "pillow": "12.3.0",
    "pypdf": "6.18.1",
    "requests": "2.34.2",
    "python-dotenv": "1.2.3",
}


def _logical_requirement_lines(text):
    """Join pip-compile's backslash-continued hash lines into entries."""
    entries = []
    pending = ""
    for physical in text.splitlines():
        line = physical.strip()
        if not line or line.startswith("#"):
            continue
        if pending:
            continuation = line.endswith("\\")
            pending += " " + line.removesuffix("\\").strip()
            if not continuation:
                entries.append(pending)
                pending = ""
        else:
            continuation = line.endswith("\\")
            line = line.removesuffix("\\").strip()
            if continuation:
                pending = line
            else:
                entries.append(line)
    if pending:
        entries.append(pending)
    return [entry for entry in entries if not entry.startswith("--")]


def _pinned_versions(text):
    versions = {}
    for entry in _logical_requirement_lines(text):
        spec = entry.split()[0]
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_,.-]+\])?==([^;\s]+)", spec)
        if match:
            name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
            if name in versions:
                raise AssertionError(f"duplicate pinned package: {name}")
            versions[name] = match.group(2)
        else:
            raise AssertionError(f"dependency is not pinned with ==: {spec}")
    return versions


class DeploymentDependencyTests(unittest.TestCase):
    def test_dependency_input_matches_streamlit_cloud_python_312_resolution(self):
        source = (ROOT / "requirements.in").read_text(encoding="utf-8")
        pins = _pinned_versions(source)

        for name, expected in PINNED_PRODUCTION_VERSIONS.items():
            self.assertEqual(pins.get(name), expected, f"unexpected production pin for {name}")

    def test_native_dependencies_are_pinned_for_python_312(self):
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")

        self.assertIn("numpy==2.2.6", requirements)
        self.assertIn("pandas==2.2.3", requirements)
        self.assertIn("pyarrow==18.1.0", requirements)
        self.assertIn("reportlab==4.4.3", requirements)
        self.assertIn("playwright==1.62.0", requirements)

    def test_transitive_lock_is_exact_and_hash_pinned(self):
        source = _pinned_versions((ROOT / "requirements.in").read_text(encoding="utf-8"))
        lock_text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        entries = _logical_requirement_lines(lock_text)
        lock = _pinned_versions(lock_text)

        self.assertEqual(len(lock), 84, "lock must preserve the 80 Cloud packages plus four startup diagnostics")
        self.assertEqual(lock, source, "requirements.txt must preserve the recorded Cloud resolution")
        for entry in entries:
            hashes = re.findall(r"--hash=\S+", entry)
            self.assertTrue(hashes, f"requirement has no artifact hash: {entry.split()[0]}")
            self.assertTrue(
                all(re.fullmatch(r"--hash=sha256:[0-9a-f]{64}", value) for value in hashes),
                f"requirement contains a malformed artifact hash: {entry.split()[0]}",
            )

    def test_lock_targets_bookworm_compatible_linux_wheels_without_source_builds(self):
        source = (ROOT / "requirements.in").read_text(encoding="utf-8")
        lock_text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        workflow = (ROOT / ".github" / "workflows" / "runtime-validation.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("manylinux_2_28-compatible", source)
        self.assertIn("--python-platform x86_64-manylinux_2_28", lock_text.splitlines()[1])
        self.assertIn("--only-binary=:all:", workflow)

    def test_deployment_guide_requires_python_312_redeployment(self):
        guide = (ROOT / "DEPLOY_GUIDE.md").read_text(encoding="utf-8")

        self.assertIn("Python 3.12", guide)
        self.assertIn("刪除並重新部署", guide)

    def test_streamlit_cloud_apt_manifest_is_disabled_during_upstream_index_outage(self):
        """A stale Cloud apt index must not prevent the Python app from booting."""
        self.assertFalse(
            (ROOT / "packages.txt").exists(),
            "packages.txt re-enables the failing Cloud apt stage; restore it only after the upstream index is fixed",
        )

    def test_pinned_browser_runtime_is_prepared_before_preflight_without_apt_downloads(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        automation_source = (ROOT / "bot" / "automation.py").read_text(encoding="utf-8")
        runtime_source = (ROOT / "bot" / "playwright_runtime.py").read_text(encoding="utf-8")

        start_job_source = app_source.split("def _start_job(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("runtime_handle = ensure_browser_runtime()", start_job_source)
        self.assertLess(
            start_job_source.index("runtime_handle = ensure_browser_runtime()"),
            start_job_source.index("read_completion_authority()"),
        )
        self.assertIn("runtime_handle=runtime_handle", app_source)
        self.assertIn("from .browser_runtime import ensure_browser_runtime", automation_source)
        self.assertIn("executable_path=str(runtime_handle.executable_path)", automation_source)
        self.assertIn("env=runtime_env", automation_source)
        self.assertIn("DEFAULT_ASSET_ROOT", runtime_source)
        self.assertNotIn("urlopen", runtime_source)
        self.assertNotIn("_download_package", runtime_source)
        self.assertIn("PLAYWRIGHT_BROWSERS_PATH", app_source)

    def test_linux_ci_uses_immutable_container_and_runs_real_runtime_checks(self):
        workflow = (ROOT / ".github" / "workflows" / "runtime-validation.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("runs-on: ubuntu-24.04", workflow)
        self.assertNotIn("ubuntu-latest", workflow)
        self.assertIn(
            "python:3.12.14-slim-bookworm@sha256:"
            "9c47360a2a0355e2da18516d0b1c2126ec22c195d2185e97347c9d98398c5bef",
            workflow,
        )
        self.assertIn("sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254", workflow)
        actions = re.findall(r"(?m)^\s*uses:\s*([^\s]+)", workflow)
        self.assertTrue(actions, "the workflow should use the pinned checkout action")
        for action in actions:
            self.assertRegex(action, r"@([0-9a-f]{40})$", f"action is not pinned to a commit SHA: {action}")
        self.assertEqual(
            actions,
            ["actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683"],
        )

        for required in (
            "docker run",
            "--platform linux/amd64",
            '--volume "${GITHUB_WORKSPACE}:/workspace"',
            "--workdir /workspace",
            "python -m pip install --disable-pip-version-check --no-cache-dir --require-hashes --only-binary=:all: -r requirements.txt",
            "python -m pip check",
            "python -m unittest discover -s tests -v",
            "test_runtime_cold_start.py",
            "python scripts/check_browser_runtime.py",
        ):
            self.assertIn(required, workflow)
        self.assertNotIn("schedule:", workflow)
        self.assertNotIn("secrets.", workflow)
        self.assertNotRegex(workflow, r"(?im)^\s*(deploy|publish):")



if __name__ == "__main__":
    unittest.main()
