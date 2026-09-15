import ast
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.browser_bootstrap import RuntimeHandle, RuntimeSetupError
from bot.browser_runtime import ensure_browser_runtime
from safe_logging import log_runtime_failure, redact_operational_log, safe_log_event


ROOT = Path(__file__).resolve().parents[1]


class FakeFrame:
    def __init__(self, rows):
        self.rows = rows
        self.empty = not rows

    def head(self, count):
        return FakeFrame(self.rows[:count])

    def iterrows(self):
        return iter(enumerate(self.rows))

    def __len__(self):
        return len(self.rows)


class PostalWorkerRuntimeTests(unittest.TestCase):
    def _invoke_start_job(self, *, runtime_error=None, scenario="normal", automation_results=None):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        node = next(
            item for item in tree.body
            if isinstance(item, ast.FunctionDef) and item.name == "_start_job"
        )
        function = compile(ast.Module(body=[node], type_ignores=[]), str(ROOT / "app.py"), "exec")
        calls = []
        runtime_ready_calls = 0
        validated_handle = RuntimeHandle("mock-profile-key", Path("/mock/chrome"), "r1", {})
        replacement_handle = RuntimeHandle("mock-profile-key", Path("/mock/chrome"), "r1", {})
        row = {"order_id": "ORDER-MOCK-01", "trans_type": "ePacket", "shipment_role": "primary"}
        frame = FakeFrame([row])
        job = {"orders": [{**row, "status": "queued"}], "logs": [], "results": []}

        class Registry:
            def start(self, _email, _df, _max_rows):
                return True, job, ""

            def finish(self, _job, status):
                calls.append(("finish", status))
                _job["status"] = status

        class Manager:
            def ensure_ready(self, key):
                nonlocal runtime_ready_calls
                runtime_ready_calls += 1
                calls.append(("runtime_ready", key))
                if runtime_error is not None:
                    raise runtime_error
                if scenario == "runtime_changed" and runtime_ready_calls > 1:
                    return replacement_handle
                return validated_handle

        class InlineThread:
            def __init__(self, *, target, daemon):
                self.target = target
                self.daemon = daemon

            def start(self):
                self.target()

        def run_automation(*_args, **kwargs):
            calls.append(("automation", kwargs.get("runtime_handle")))
            if scenario == "runtime_changed":
                with patch(
                    "bot.browser_runtime.get_browser_runtime_manager",
                    return_value=Manager(),
                ), patch(
                    "bot.browser_runtime.build_runtime_profile_key",
                    return_value="mock-profile-key",
                ), patch(
                    "bot.browser_runtime._runtime_environment_supported",
                    return_value=True,
                ):
                    ensure_browser_runtime(expected_handle=kwargs.get("runtime_handle"))
            return list(automation_results or [])

        automation = types.ModuleType("bot.automation")
        automation.AUTOMATION_BUILD_ID = "mock-build"
        automation._prepare_batch_hs_codes = lambda *_args, **_kwargs: calls.append(("hs",)) or {}
        automation.run_automation = run_automation

        sheets = types.ModuleType("bot.sheets")
        sheets.COUNTRY_CODE_MAP = {}
        sheets.read_completion_authority = lambda: calls.append(("read_completion",)) or {}
        sheets.get_pending_orders = lambda **_kwargs: calls.append(("read_pending",)) or frame

        def preflight_batch(_rows, _latest_pending, _authority):
            calls.append(("preflight", scenario))
            if scenario == "already_completed":
                return [{"order_id": row["order_id"], "reason_text": "已完成"}]
            if scenario == "source_cancelled":
                return [{
                    "order_id": row["order_id"],
                    "reason_code": "source_changed",
                    "reason_text": "來源資料已變更",
                }]
            return []

        def partition(rows, checks):
            if scenario == "already_completed":
                return FakeFrame([]), checks, []
            if scenario == "source_cancelled":
                return FakeFrame([]), [], checks
            return rows, [], []

        def mark_completed(_job, results):
            completed_ids = {result.get("order_id") for result in results}
            for order in _job.get("orders", []):
                if order.get("order_id") in completed_ids:
                    order.update({"status": "completed", "stage": "已完成"})

        def mark_failed(_job, results):
            failed_ids = {result.get("order_id") for result in results}
            for order in _job.get("orders", []):
                if order.get("order_id") in failed_ids:
                    order.update({"status": "failed", "stage": "需排查"})

        def apply_writeback(_job, candidates, outcome):
            calls.append(("apply_writeback", outcome.get("ok")))
            for result in candidates:
                result.update({"status": "completed", "writeback_verified": True})
            mark_completed(_job, candidates)
            return "completed"

        namespace = {
            "pd": types.SimpleNamespace(DataFrame=FakeFrame),
            "threading": types.SimpleNamespace(Thread=InlineThread),
            "_JOB_REGISTRY": Registry(),
            "_write_job_lock": lambda _email: calls.append(("lock",)),
            "_clear_job_lock": lambda _email: calls.append(("unlock",)),
            "get_browser_runtime_manager": lambda: Manager(),
            "build_runtime_profile_key": lambda: "mock-profile-key",
            "ensure_browser_runtime": lambda: Manager().ensure_ready("mock-profile-key"),
            "RuntimeSetupError": RuntimeSetupError,
            "log_runtime_failure": log_runtime_failure,
            "safe_log_event": safe_log_event,
            "redact_operational_log": redact_operational_log,
            "update_order_status_from_log": lambda *_args: None,
            "update_order_status_from_event": lambda *_args: None,
            "_job_sensitive_values": lambda *_args, **_kwargs: (),
            "mark_unfinished_orders": self._mark_unfinished_orders,
            "mark_results_completed": mark_completed,
            "mark_results_failed": mark_failed,
            "apply_writeback_outcome": apply_writeback,
            "summarize_job_results": lambda results: {
                "completed": sum(result.get("status") == "completed" for result in results),
                "total": len(results),
            },
            "partition_preflight_rows": partition,
            "preflight_batch_orders": preflight_batch,
            "_load_current_automation_module": lambda: automation,
            "backfill_results": lambda results, **_kwargs: calls.append(("backfill", len(results))) or {
                "ok": True, "written": len(results), "failed": [], "error": ""
            },
            "time": time,
            "sys": sys,
            "print": lambda *_args, **_kwargs: None,
        }
        exec(function, namespace)

        with patch.dict(sys.modules, {"bot.sheets": sheets, "bot.automation": automation}), patch(
            "socket.create_connection", side_effect=AssertionError("network access disabled in mock")
        ) as network_connect:
            result = namespace["_start_job"]("user@example.com", frame, None)
            self.assertFalse(network_connect.called)
        return result, job, calls, namespace

    @staticmethod
    def _mark_unfinished_orders(job, status, stage, message):
        for order in job.get("orders") or []:
            if order.get("status") in {"queued", "running"}:
                order.update({"status": status, "stage": stage, "message": message})

    def test_runtime_failure_stops_before_any_google_sheets_or_automation_call(self):
        error = RuntimeSetupError("browser_install", "browser_download", retryable=True, http_status=503)
        error.attempts = 2
        error.__cause__ = RuntimeError(
            "private@example.com ORDER-SECRET-8490 LX123456789JP https://secret.invalid"
        )

        result, job, calls, _namespace = self._invoke_start_job(runtime_error=error)

        self.assertEqual(result, (True, ""))
        self.assertEqual(job["status"], "error")
        self.assertEqual(job["runtime_setup_message"], "製單環境暫時無法啟動，訂單尚未送出，請稍後再試。")
        self.assertEqual(job["orders"][0]["status"], "failed")
        self.assertNotIn("read_completion", [item[0] for item in calls])
        self.assertNotIn("read_pending", [item[0] for item in calls])
        self.assertNotIn("automation", [item[0] for item in calls])
        self.assertNotIn("hs", [item[0] for item in calls])
        self.assertIn(("unlock",), calls)
        self.assertTrue(any("browser_runtime_failed stage=browser_install code=browser_download" in line for line in job["logs"]))
        for secret in (
            "user@example.com",
            "private@example.com",
            "ORDER-SECRET-8490",
            "LX123456789JP",
            "secret.invalid",
        ):
            self.assertFalse(any(secret in line for line in job["logs"]), secret)

    def test_runtime_is_ready_before_final_sheet_snapshot_and_handle_reaches_automation(self):
        result, job, calls, _namespace = self._invoke_start_job()

        self.assertEqual(result, (True, ""))
        event_names = [item[0] for item in calls]
        self.assertLess(event_names.index("runtime_ready"), event_names.index("read_completion"))
        self.assertLess(event_names.index("runtime_ready"), event_names.index("read_pending"))
        self.assertLess(event_names.index("read_pending"), event_names.index("automation"))
        automation_handle = next(call[1] for call in calls if call[0] == "automation")
        self.assertIsInstance(automation_handle, RuntimeHandle)
        self.assertEqual(automation_handle.profile_key, "mock-profile-key")
        self.assertNotIn("runtime_setup_message", job)
        self.assertNotIn("unlock", event_names[:event_names.index("finish")])

    def test_latest_completed_source_is_closed_without_starting_automation(self):
        result, job, calls, _namespace = self._invoke_start_job(scenario="already_completed")

        self.assertEqual(result, (True, ""))
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["orders"][0]["status"], "completed")
        self.assertTrue(job["pending_refresh_needed"])
        self.assertNotIn("automation", [item[0] for item in calls])
        self.assertNotIn("backfill", [item[0] for item in calls])
        self.assertIn(("unlock",), calls)
        self.assertEqual(calls.count(("unlock",)), 1)

    def test_changed_or_cancelled_source_is_blocked_before_automation(self):
        result, job, calls, _namespace = self._invoke_start_job(scenario="source_cancelled")

        self.assertEqual(result, (True, ""))
        self.assertEqual(job["status"], "error")
        self.assertTrue(job["preflight_reload_required"])
        self.assertEqual(job["results"][0]["reason_code"], "source_changed")
        self.assertNotIn("automation", [item[0] for item in calls])
        self.assertNotIn("backfill", [item[0] for item in calls])
        self.assertEqual(calls.count(("unlock",)), 1)

    def test_tracking_result_is_backfilled_and_verified_in_mock(self):
        result, job, calls, _namespace = self._invoke_start_job(
            automation_results=[{
                "order_id": "ORDER-MOCK-01",
                "trans_type": "ePacket",
                "shipment_role": "primary",
                "status": "success",
                "tracking": "LX123456789JP",
            }]
        )

        self.assertEqual(result, (True, ""))
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["orders"][0]["status"], "completed")
        self.assertTrue(job["results"][0]["writeback_verified"])
        self.assertIn(("backfill", 1), calls)
        self.assertIn(("apply_writeback", True), calls)
        self.assertEqual(len([item for item in calls if item[0] == "automation"]), 1)
        self.assertEqual(len([item for item in calls if item[0] == "backfill"]), 1)
        self.assertEqual(calls.count(("unlock",)), 1)

    def test_runtime_profile_change_at_automation_boundary_stops_before_backfill(self):
        result, job, calls, _namespace = self._invoke_start_job(scenario="runtime_changed")

        self.assertEqual(result, (True, ""))
        self.assertEqual(job["status"], "error")
        self.assertEqual(job["runtime_setup_message"], "製單環境暫時無法啟動，訂單尚未送出，請稍後再試。")
        self.assertIn("automation", [item[0] for item in calls])
        self.assertNotIn("backfill", [item[0] for item in calls])
        self.assertIn(("unlock",), calls)
        self.assertTrue(any("code=runtime_changed" in line for line in job["logs"]))
        self.assertEqual(calls.count(("unlock",)), 1)

    def test_safe_diagnostic_gate_keeps_runtime_failure_logs_visible(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        node = next(
            item for item in tree.body
            if isinstance(item, ast.FunctionDef) and item.name == "_should_show_safe_diagnostic"
        )
        code = compile(ast.Module(body=[node], type_ignores=[]), str(ROOT / "app.py"), "exec")
        namespace = {}
        exec(code, namespace)

        self.assertTrue(namespace["_should_show_safe_diagnostic"](
            {"logs": ["safe error"] , "runtime_setup_message": "尚未送出"},
            {"failure_alerts": []},
        ))
        self.assertTrue(namespace["_should_show_safe_diagnostic"](
            {"logs": ["safe error"]},
            {"failure_alerts": ["failure"]},
        ))
        self.assertFalse(namespace["_should_show_safe_diagnostic"](
            {"logs": ["success"]},
            {"failure_alerts": []},
        ))


if __name__ == "__main__":
    unittest.main()
