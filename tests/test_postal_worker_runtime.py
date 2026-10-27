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
from bot.shipment_ledger import GlobalShipmentLock, LedgerUnavailable, NormalShipmentSafety, LEDGER_TITLE
from test_shipment_ledger import MemoryBook, MemorySheet
from job_control import apply_writeback_outcome as real_apply_writeback_outcome
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
    def _invoke_start_job(self, *, runtime_error=None, scenario="normal", automation_results=None,
                          real_writer=None, safety_error=None, automation_error=None, backfill_error=None,
                          lifecycle=False, lifecycle_stop_at=None, release_observer=None,
                          safety_cleanup_error=None):
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
                job["status"] = "running"
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
            if automation_error is not None:
                raise automation_error
            if lifecycle:
                for _, selected_row in frame.iterrows():
                    for stage in ("issuing", "label_created", "uploaded"):
                        kwargs["lifecycle_cb"](stage, selected_row, tracking="LX123456789JP")
                        if stage == lifecycle_stop_at:
                            stopped = RuntimeError("private lifecycle failure")
                            stopped.partial_results = [{**row, "status": "failed", "reason_code": "shipment_requires_review"}]
                            raise stopped
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
            if scenario == 'partial_unknown':
                stopped=RuntimeError('shipment_execution_requires_review')
                stopped.partial_results=list(automation_results or [])
                raise stopped
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

        def apply_writeback(_job, candidates, outcome, **kwargs):
            calls.append(("apply_writeback", outcome.get("ok")))
            return real_apply_writeback_outcome(_job, candidates, outcome, **kwargs)

        def backfill(results, **_kwargs):
            calls.append(("backfill", len(results)))
            if backfill_error is not None:
                raise backfill_error
            return {"ok": True, "written": len(results), "failed": [], "error": "",
                    "items": [{"input_index": index, "status": "written", "row": index + 1}
                              for index in range(len(results))]}

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
            "LedgerUnavailable": LedgerUnavailable,
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
            "backfill_results": backfill,
            "time": time,
            "sys": sys,
            "print": lambda *_args, **_kwargs: None,
        }
        exec(function, namespace)

        class Safety:
            def __init__(self, lock): pass
            def assert_clear(self, rows):
                if safety_error is not None:
                    raise safety_error
            def lifecycle(self, *args, **kwargs): pass
            def mark_verified(self, results): pass
            def safe_to_release(self):
                if safety_cleanup_error is not None:
                    raise safety_cleanup_error
                return True
        class Writer:
            held = False
            retain = False
            sheet_id = 123
            def acquire(self): self.held = True
            def release(self): self.held = False
        selected_writer = real_writer or Writer()
        if release_observer is not None:
            original_release = selected_writer.release

            def observed_release():
                release_observer(job, calls)
                return original_release()

            selected_writer.release = observed_release
        with patch('bot.shipment_ledger.global_writer_lock', return_value=selected_writer), patch(
            'bot.shipment_ledger.NormalShipmentSafety', NormalShipmentSafety if real_writer else Safety
        ), patch.dict(sys.modules, {"bot.sheets": sheets, "bot.automation": automation}), patch(
            "socket.create_connection", side_effect=AssertionError("network access disabled in mock")
        ) as network_connect:
            result = namespace["_start_job"]("user@example.com", frame, None)
            self.assertFalse(network_connect.called)
        return result, job, calls, namespace

    def test_real_safety_gates_preserve_safe_failure_code_and_never_run_automation(self):
        scenarios = (
            ("busy", "shipment_writer_busy_or_unknown", "writer_lock"),
            ("prior_guard", "shipment_previous_submission_requires_review", "safety_preflight"),
            ("unreadable_ledger", "shipment_ledger_unreadable", "safety_preflight"),
        )
        for scenario, expected_code, expected_stage in scenarios:
            with self.subTest(scenario=scenario):
                book = MemoryBook()
                writer = GlobalShipmentLock(book)
                if scenario == "busy":
                    GlobalShipmentLock(book).acquire()
                elif scenario == "prior_guard":
                    previous = GlobalShipmentLock(book).acquire()
                    NormalShipmentSafety(previous).lifecycle("issuing", {
                        "order_id": "ORDER-MOCK-01", "trans_type": "ePacket", "shipment_role": "primary",
                    })
                    previous.release()
                else:
                    sheet = MemorySheet(book, 100, LEDGER_TITLE)
                    sheet.rows = [["synthetic-plan", "invalid-synthetic-token", "1"]]
                    book.sheets[100] = sheet
                with patch("bot.shipment_ledger.encryption_secret", return_value="synthetic-key-" * 4):
                    _, job, calls, _ = self._invoke_start_job(real_writer=writer)
                self.assertEqual(job["status"], "error")
                self.assertEqual(job["results"][0]["reason_code"], expected_code)
                self.assertIn("詳細除錯日誌", job["results"][0]["reason_text"])
                self.assertTrue(any(f"stage={expected_stage} reason={expected_code}" in line for line in job["logs"]))
                self.assertFalse(any(call[0] in {"automation", "backfill"} for call in calls))
                if scenario == "busy":
                    self.assertTrue(writer.retain)
                    self.assertFalse(writer.held)

    def test_unknown_failure_details_never_reach_job_results_or_logs(self):
        private = "person@example.invalid token=SECRET-TEST https://secret.invalid PRIVATE-ORDER"
        hostile = LedgerUnavailable(private)
        for error in (RuntimeError(private), hostile):
            with self.subTest(error_type=type(error).__name__):
                _, job, calls, _ = self._invoke_start_job(safety_error=error)
                self.assertEqual(job["results"][0]["reason_code"], "job_exception")
                self.assertIn("詳細除錯日誌", job["results"][0]["reason_text"])
                self.assertTrue(any("stage=safety_preflight reason=job_exception" in line for line in job["logs"]))
                self.assertNotIn(private, str(job))
                self.assertFalse(any(call[0] in {"automation", "backfill"} for call in calls))

    def test_errors_after_automation_start_never_claim_orders_were_not_submitted(self):
        for stage in ("automation_run", "writeback"):
            with self.subTest(stage=stage):
                arguments = {"automation_error": RuntimeError("private failure")} if stage == "automation_run" else {
                    "automation_results": [{"order_id": "ORDER-MOCK-01", "status": "success", "tracking": "LX123456789JP"}],
                    "backfill_error": RuntimeError("private failure"),
                }
                _, job, calls, _ = self._invoke_start_job(**arguments)
                self.assertIn("automation", [call[0] for call in calls])
                self.assertTrue(any(f"stage={stage} reason=job_exception" in line for line in job["logs"]))
                self.assertNotIn("尚未送出", str(job["results"]))
                self.assertNotIn("未開始製單", str(job["results"]))
                self.assertNotIn("private failure", str(job))

    def test_writer_release_failure_keeps_completed_results_and_exposes_safe_review_warning(self):
        book = MemoryBook()
        writer = GlobalShipmentLock(book)
        original_batch = book.batch_update

        def reject_release(body):
            if "deleteSheet" in body["requests"][0]:
                raise TimeoutError("token=PRIVATE-RELEASE-SECRET https://secret.invalid")
            return original_batch(body)

        book.batch_update = reject_release
        _, job, calls, _ = self._invoke_start_job(real_writer=writer, lifecycle=True,
            automation_results=[{"order_id": "ORDER-MOCK-01", "trans_type": "ePacket",
                                 "shipment_role": "primary", "status": "success", "tracking": "LX123456789JP"}])
        self.assertEqual(job["results"][0]["status"], "completed")
        self.assertEqual(job["status"], "completed")
        self.assertTrue(job.get("safety_review_required"))
        self.assertEqual(job.get("safety_lock_generation"), writer.sheet_id)
        self.assertIn("執行鎖尚未確認釋放", job.get("safety_lock_message", ""))
        self.assertTrue(any("stage=writer_release reason=shipment_lock_release_unknown" in line for line in job["logs"]))
        self.assertNotIn("PRIVATE-RELEASE-SECRET", str(job))
        self.assertEqual(sum(call[0] == "automation" for call in calls), 1)
        self.assertEqual(sum(call[0] == "backfill" for call in calls), 1)
        self.assertTrue(writer.held)
        self.assertTrue(writer.retain)

    def test_retained_writer_after_completed_guard_readback_failure_is_visible(self):
        book = MemoryBook()
        writer = GlobalShipmentLock(book)
        original_by_id = book.get_worksheet_by_id

        def instrument(sheet_id):
            sheet = original_by_id(sheet_id)
            if sheet.title == NormalShipmentSafety.title and not getattr(sheet, "instrumented", False):
                sheet.instrumented = True
                original_get = sheet.get

                def bad_completed_readback(a1):
                    rows = original_get(a1)
                    return [] if "completed" in str(rows) else rows

                sheet.get = bad_completed_readback
            return sheet

        book.get_worksheet_by_id = instrument
        _, job, calls, _ = self._invoke_start_job(real_writer=writer, lifecycle=True,
            automation_results=[{"order_id": "ORDER-MOCK-01", "trans_type": "ePacket",
                                 "shipment_role": "primary", "status": "success", "tracking": "LX123456789JP"}])
        self.assertEqual(job["results"][0]["status"], "completed")
        self.assertEqual(job["status"], "error")
        self.assertTrue(writer.retain)
        self.assertTrue(job.get("safety_review_required"))
        self.assertEqual(job.get("safety_lock_generation"), writer.sheet_id)
        self.assertNotIn("safety_lock_message", job)
        self.assertTrue(any("stage=writer_release reason=shipment_lock_requires_review" in line for line in job["logs"]))
        self.assertEqual(sum(call[0] == "automation" for call in calls), 1)
        self.assertEqual(sum(call[0] == "backfill" for call in calls), 1)

    def test_unknown_issuing_result_keeps_critical_warning_instead_of_release_only_copy(self):
        writer = GlobalShipmentLock(MemoryBook())
        _, job, calls, _ = self._invoke_start_job(real_writer=writer, lifecycle=True, lifecycle_stop_at="issuing")
        self.assertTrue(job.get("safety_review_required"))
        self.assertTrue(writer.retain)
        self.assertNotIn("safety_lock_message", job)
        self.assertNotIn("運單結果已保留", str(job))
        self.assertEqual(job.get("safety_lock_generation"), writer.sheet_id)
        self.assertNotIn("backfill", [call[0] for call in calls])
        self.assertNotIn("private lifecycle failure", str(job))

    def test_terminal_status_and_local_unlock_wait_for_writer_cleanup(self):
        for scenario, fail_release in (("normal", False), ("normal", True),
                                        ("already_completed", False), ("source_cancelled", False)):
            with self.subTest(scenario=scenario, fail_release=fail_release):
                observed = []
                release_entered = threading.Event()
                allow_release = threading.Event()

                def observer(job, calls):
                    release_entered.set()

                    def inspect_while_worker_waits():
                        if release_entered.wait(2):
                            observed.append((job["status"], any(call[0] == "finish" for call in calls),
                                             any(call[0] == "unlock" for call in calls)))
                        allow_release.set()

                    reader = threading.Thread(target=inspect_while_worker_waits)
                    reader.start()
                    self.assertTrue(allow_release.wait(2))
                    reader.join(2)
                    if fail_release:
                        raise TimeoutError("private cleanup failure")

                success = [{"order_id": "ORDER-MOCK-01", "trans_type": "ePacket",
                            "shipment_role": "primary", "status": "success", "tracking": "LX123456789JP"}]
                _, job, calls, _ = self._invoke_start_job(scenario=scenario, release_observer=observer,
                                                        automation_results=success)
                self.assertEqual(observed, [("running", False, False)])
                self.assertNotEqual(job["status"], "running")
                self.assertEqual(sum(call[0] == "finish" for call in calls), 1)
                self.assertEqual(sum(call[0] == "unlock" for call in calls), 1)
                self.assertNotIn("private cleanup failure", str(job))

    def test_cleanup_safety_exception_still_finishes_once_and_preserves_review(self):
        _, job, calls, _ = self._invoke_start_job(safety_cleanup_error=RuntimeError("private cleanup failure"))
        self.assertNotEqual(job["status"], "running")
        self.assertTrue(job.get("safety_review_required"))
        self.assertEqual(sum(call[0] == "finish" for call in calls), 1)
        self.assertEqual(sum(call[0] == "unlock" for call in calls), 1)
        self.assertNotIn("private cleanup failure", str(job))

    @staticmethod
    def _mark_unfinished_orders(job, status, stage, message):
        for order in job.get("orders") or []:
            if order.get("status") in {"queued", "running"}:
                order.update({"status": status, "stage": stage, "message": message})

    def test_unknown_later_shipment_keeps_prior_tracking_for_writeback(self):
        result,job,calls,_namespace=self._invoke_start_job(
            scenario='partial_unknown', automation_results=[
                {'order_id':'ORDER-1','trans_type':'ePacket','shipment_role':'primary',
                 'tracking':'EL000000001JP','status':'success'},
                {'order_id':'ORDER-2','status':'failed','reason_code':'shipment_requires_review'},
            ])
        self.assertTrue(job.get('safety_review_required'))
        self.assertIn(('backfill',1),calls)
        self.assertEqual(job['results'][0]['tracking'],'EL000000001JP')
        self.assertEqual(job['results'][0]['status'],'completed')

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
        self.assertIn(job["orders"][0]["status"], {"success", "completed"})
        self.assertEqual(job["results"][0]["backfill_status"], "written")
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
        self.assertTrue(namespace["_should_show_safe_diagnostic"](
            {"logs": ["safe lock review"], "safety_review_required": True},
            {"failure_alerts": []},
        ))
        self.assertFalse(namespace["_should_show_safe_diagnostic"](
            {"logs": ["success"]},
            {"failure_alerts": []},
        ))


if __name__ == "__main__":
    unittest.main()
