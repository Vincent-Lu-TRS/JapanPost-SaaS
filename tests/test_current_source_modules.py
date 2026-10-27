"""Code-only updates must not mix current app calls with cached legacy helpers."""
import ast
import importlib
import inspect
import os
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import job_control
import safe_logging
import test_postal_worker_runtime as worker_fixture

ROOT = Path(__file__).resolve().parents[1]
try:
    import current_source_modules as snapshots
except ImportError:
    snapshots = None


def legacy_module(name):
    """Actual archived function bodies, sharing only their unchanged dependencies."""
    current = {"job_control": job_control, "safe_logging": safe_logging}[name]
    module = types.ModuleType(name)
    module.__dict__.update(current.__dict__)
    source = (ROOT / "tests/fixtures" / f"pre_pr15_{name}.txt").read_text(encoding="utf-8")
    exec(compile(source, f"archived_{name}.py", "exec"), module.__dict__)
    return module


def app_bindings(*, include_registry=False):
    """Execute the real app imports/bindings, without auth, browser or Sheets startup."""
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    nodes = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.ImportFrom) and node.module in {
            "app_imports", "current_source_modules", "safe_logging"
        }:
            nodes.append(node)
        elif isinstance(node, ast.Assign):
            names = {n.id for n in node.targets if isinstance(n, ast.Name)}
            if names & {"_job_control", "_safe_logging"} or (
                isinstance(node.value, ast.Attribute)
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id in {"_job_control", "_safe_logging"}
            ):
                nodes.append(node)
            elif include_registry and names & {"_JOB_REGISTRY_CACHE_VERSION", "_JOB_REGISTRY"}:
                nodes.append(node)
        elif include_registry and isinstance(node, ast.FunctionDef) and node.name == "_get_job_registry":
            nodes.append(node)
    scope = {"__name__": "offline_app_binding_test", "st": importlib.import_module("streamlit")}
    exec(compile(ast.Module(nodes, type_ignores=[]), str(ROOT / "app.py"), "exec"), scope)
    return scope


def invoke_bound_worker(bindings, **kwargs):
    # Keep the existing external browser/Sheets fakes, but use the exact helper
    # objects bound by the real app statements and real order state callbacks.
    source = textwrap.dedent(inspect.getsource(worker_fixture.PostalWorkerRuntimeTests._invoke_start_job))
    source = source.replace("exec(function, namespace)", "namespace.update(helper_bindings)\n    exec(function, namespace)")
    mark_error = kwargs.pop("mark_verified_error", None)
    source = source.replace("def mark_verified(self, results): pass", "def mark_verified(self, results):\n            if mark_error is not None: raise mark_error")
    helper_names = {
        "apply_writeback_outcome", "mark_results_completed", "mark_results_failed",
        "mark_unfinished_orders", "summarize_job_results", "update_order_status_from_log",
        "update_order_status_from_event", "safe_log_event", "log_runtime_failure",
        "redact_operational_log", "reconcile_job_exception",
    }
    scope = dict(worker_fixture.__dict__, mark_error=mark_error,
                 helper_bindings={k: v for k, v in bindings.items() if k in helper_names})
    exec(compile(source, "offline_bound_worker.py", "exec"), scope)
    return scope["_invoke_start_job"](worker_fixture.PostalWorkerRuntimeTests(), **kwargs)


class CurrentSourceModuleTests(unittest.TestCase):
    def _run_isolated(self, assertion_name):
        script = f"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'tests'))
import streamlit
from unittest.mock import patch
from test_current_source_modules import CurrentSourceModuleTests
with patch('socket.create_connection', side_effect=AssertionError('external network disabled')) as network:
    CurrentSourceModuleTests().{assertion_name}()
    assert not network.called
"""
        result = subprocess.run([sys.executable, "-c", textwrap.dedent(script)], cwd=ROOT,
                                text=True, capture_output=True, encoding="utf-8",
                                env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=30)
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-4000:])

    def require_loader(self):
        self.assertIsNotNone(snapshots, "current-source snapshot loader is required")
        return snapshots

    def test_allowlist_rejects_non_project_helpers(self):
        loader = self.require_loader()
        for name in ("os", "../job_control", "bot.sheets", "job_control.py"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                loader.load_current_source_module(name)

    def test_content_change_same_size_and_mtime_keeps_old_globals(self):
        loader = self.require_loader()
        with tempfile.TemporaryDirectory() as folder, patch.object(loader, "_SOURCE_ROOT", Path(folder)):
            path = Path(folder) / "job_control.py"
            path.write_bytes(b"VALUE=1\ndef value(): return VALUE\n")
            before = path.stat()
            first = loader.load_current_source_module("job_control")
            path.write_bytes(b"VALUE=2\ndef value(): return VALUE\n")
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            second = loader.load_current_source_module("job_control")
            self.assertIsNot(first, second)
            self.assertEqual(first.value(), 1)
            self.assertEqual(second.value(), 2)
            self.assertIs(loader.load_current_source_module("job_control"), second)

    def test_source_path_is_part_of_snapshot_identity(self):
        loader = self.require_loader()
        modules = []
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            for folder in (a, b):
                path = Path(folder) / "safe_logging.py"
                path.write_text("LOCATION=__file__\n", encoding="utf-8")
                with patch.object(loader, "_SOURCE_ROOT", Path(folder)):
                    modules.append(loader.load_current_source_module("safe_logging"))
            self.assertIsNot(*modules)
            self.assertEqual(Path(modules[0].LOCATION).parent, Path(a))
            self.assertEqual(Path(modules[1].LOCATION).parent, Path(b))

    def test_concurrent_callers_share_snapshot_and_canonical_module_is_untouched(self):
        loader = self.require_loader()
        canonical = importlib.import_module("job_control")
        with ThreadPoolExecutor(max_workers=8) as pool:
            modules = list(pool.map(lambda _: loader.load_current_source_module("job_control"), range(24)))
        self.assertTrue(all(module is modules[0] for module in modules))
        self.assertIs(sys.modules["job_control"], canonical)

    def test_failed_compile_or_execution_does_not_publish_partial_alias(self):
        loader = self.require_loader()
        with tempfile.TemporaryDirectory() as folder, patch.object(loader, "_SOURCE_ROOT", Path(folder)):
            path = Path(folder) / "safe_logging.py"
            for source, error in (("def broken(:\n", SyntaxError), ("raise RuntimeError('synthetic')\n", RuntimeError)):
                path.write_text(source, encoding="utf-8")
                before = set(sys.modules)
                with self.assertRaises(error):
                    loader.load_current_source_module("safe_logging")
                self.assertEqual({k for k in set(sys.modules) - before if k.startswith("_jppost_source_")}, set())

    def test_dataclass_module_lookup_uses_registered_snapshot(self):
        loader = self.require_loader()
        with tempfile.TemporaryDirectory() as folder, patch.object(loader, "_SOURCE_ROOT", Path(folder)):
            (Path(folder) / "job_control.py").write_text(
                "from __future__ import annotations\nfrom dataclasses import dataclass\n@dataclass\nclass Row:\n value: int\n", encoding="utf-8"
            )
            module = loader.load_current_source_module("job_control")
            self.assertIs(sys.modules[module.__name__], module)
            self.assertEqual(module.Row(7).value, 7)

    def test_snapshot_compile_does_not_inherit_loader_future_annotations(self):
        loader = self.require_loader()
        with tempfile.TemporaryDirectory() as folder, patch.object(loader, "_SOURCE_ROOT", Path(folder)):
            path = Path(folder) / "job_control.py"
            path.write_text("def f(value: int): return value\n", encoding="utf-8")
            module = loader.load_current_source_module("job_control")
            self.assertIs(module.f.__annotations__["value"], int)

    def test_real_app_bindings_bypass_legacy_helpers_and_worker_completes_once(self):
        self._run_isolated("_assert_real_app_bindings_bypass_legacy_helpers_and_worker_completes_once")

    def _assert_real_app_bindings_bypass_legacy_helpers_and_worker_completes_once(self):
        synthetic = {"order_id": "ORDER-MOCK-01", "trans_type": "ePacket", "shipment_role": "primary",
                     "status": "success", "tracking": "LX123456789JP"}
        old_job, old_log = legacy_module("job_control"), legacy_module("safe_logging")
        with patch.dict(sys.modules, {"job_control": old_job, "safe_logging": old_log}):
            bindings = app_bindings()
            self.assertIs(sys.modules["job_control"], old_job)
            self.assertIs(sys.modules["safe_logging"], old_log)
            _, job, calls, _ = invoke_bound_worker(bindings, automation_results=[synthetic])
            self.assertEqual(job["status"], "completed")
            self.assertEqual([r["status"] for r in job["results"]], ["completed"])
            self.assertEqual(sum(c[0] == "backfill" for c in calls), 1)
            _, failed, _, _ = invoke_bound_worker(bindings, automation_error=RuntimeError("synthetic"))
            self.assertTrue(any("stage=automation_run reason=job_exception" in line for line in failed["logs"]))

    def test_real_streamlit_registry_and_running_job_survive_new_bindings(self):
        self._run_isolated("_assert_real_streamlit_registry_and_running_job_survive_new_bindings")

    def _assert_real_streamlit_registry_and_running_job_survive_new_bindings(self):
        import pandas as pd
        st = importlib.import_module("streamlit")
        st.cache_resource.clear()
        try:
            old_job, old_log = legacy_module("job_control"), legacy_module("safe_logging")
            with patch.dict(sys.modules, {"job_control": old_job, "safe_logging": old_log}):
                first = app_bindings(include_registry=True)
                registry = first["_JOB_REGISTRY"]
                ok, running, _ = registry.start("synthetic@example.com", pd.DataFrame([
                    {"order_id": "SYNTHETIC-REGISTRY", "trans_type": "ePacket"}
                ]), None)
                self.assertTrue(ok)
                second = app_bindings(include_registry=True)
                self.assertIs(second["_JOB_REGISTRY"], registry)
                self.assertIs(registry.get("synthetic@example.com"), running)
                self.assertEqual(running["status"], "running")
                self.assertEqual(first["_JOB_REGISTRY_CACHE_VERSION"], "2026-08-05-postal-ui")
        finally:
            st.cache_resource.clear()

    def test_warm_worker_after_verified_exception_has_one_complete_and_visible_notice(self):
        self._run_isolated("_assert_warm_worker_after_verified_exception_has_one_complete_and_visible_notice")

    def _assert_warm_worker_after_verified_exception_has_one_complete_and_visible_notice(self):
        from postal_ui_feedback import summarize_batch_results
        with patch.dict(sys.modules, {"job_control": legacy_module("job_control"), "safe_logging": legacy_module("safe_logging")}):
            bindings = app_bindings()
            _, job, _, _ = invoke_bound_worker(bindings, automation_results=[{
                "order_id": "ORDER-MOCK-01", "trans_type": "ePacket", "shipment_role": "primary",
                "status": "success", "tracking": "LX123456789JP"
            }], mark_verified_error=RuntimeError("token=PRIVATE-SYNTHETIC-SECRET"))
        summary = summarize_batch_results(job["results"])
        self.assertEqual((summary["total_count"], summary["completed_count"], summary["failed_count"]), (1, 1, 0))
        self.assertEqual(job["orders"][0]["status"], "success")
        self.assertTrue(job.get("postprocess_error_message"))
        self.assertTrue(any("stage=writeback_verify reason=job_exception" in line for line in job["logs"]))
        self.assertNotIn("PRIVATE-SYNTHETIC-SECRET", str(job))

    def test_warm_worker_unverified_label_failure_is_never_counted_completed(self):
        self._run_isolated("_assert_warm_worker_unverified_label_failure_is_never_counted_completed")

    def _assert_warm_worker_unverified_label_failure_is_never_counted_completed(self):
        from postal_ui_feedback import summarize_batch_results
        with patch.dict(sys.modules, {"job_control": legacy_module("job_control"), "safe_logging": legacy_module("safe_logging")}):
            bindings = app_bindings()
            _, job, _, _ = invoke_bound_worker(bindings, automation_results=[{
                "order_id": "ORDER-MOCK-01", "trans_type": "ePacket", "shipment_role": "primary",
                "status": "success", "tracking": "LX123456789JP"
            }], backfill_error=RuntimeError("synthetic"))
        summary = summarize_batch_results(job["results"])
        self.assertEqual((summary["total_count"], summary["completed_count"], summary["failed_count"]), (1, 0, 1))
        self.assertEqual(job["results"][0]["tracking"], "LX123456789JP")
        self.assertNotIn("postprocess_error_message", job)
