import unittest
import copy

import job_control


def package(*, method="ePacket", role="primary", status="success", **extra):
    return {"order_id": "SYNTHETIC-01", "trans_type": method, "shipment_role": role,
            "status": status, **extra}


class JobExceptionReconciliationTests(unittest.TestCase):
    def reconcile(self, job):
        helper = getattr(job_control, "reconcile_job_exception", None)
        self.assertIsNotNone(helper, "exception reconciliation must upsert package outcomes")
        return helper(job, "job_exception", "Synthetic safe failure")

    def test_unverified_label_is_one_reviewable_result_not_success_plus_failure(self):
        result = package(tracking="LX123456789JP", drive_file_id="synthetic-pdf")
        job = {"orders": [package(status="running")], "results": [result]}
        self.reconcile(job)
        self.assertEqual(len(job["results"]), 1)
        self.assertNotIn(result["status"], {"success", "completed"})
        self.assertEqual(result["tracking"], "LX123456789JP")
        self.assertEqual(result["drive_file_id"], "synthetic-pdf")
        self.assertEqual(job_control.summarize_job_results(job["results"])["completed"], 0)

    def test_verified_completion_survives_later_exception_with_separate_notice(self):
        result = package(status="completed", tracking="LX123456789JP", backfill_status="written")
        job = {"orders": [package(status="running")], "results": [result]}
        self.reconcile(job)
        self.assertEqual(job["results"], [result])
        self.assertEqual(result["status"], "completed")
        self.assertIn(job["orders"][0]["status"], {"success", "completed"})
        self.assertTrue(job.get("postprocess_error_message"))
        self.assertEqual(job_control.summarize_job_results(job["results"])["failed"], 0)

    def test_no_label_gets_one_failure_with_full_package_identity(self):
        job = {"orders": [package(status="queued")], "results": []}
        self.reconcile(job)
        self.assertEqual(len(job["results"]), 1)
        self.assertEqual(job_control.shipment_package_key(job["results"][0]), ("SYNTHETIC-01", "ePacket", "primary"))

    def test_different_methods_and_roles_remain_distinct(self):
        results = [package(status="completed", tracking="LX123456789JP", backfill_status="already_present"),
                   package(method="EMS", role="additional", tracking="EE123456789JP")]
        job = {"orders": [package(status="running"), package(method="EMS", role="additional", status="running")], "results": results}
        self.reconcile(job)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["status"], "completed")
        self.assertNotIn(results[1]["status"], {"success", "completed"})
        self.assertEqual(results[1]["tracking"], "EE123456789JP")

    def test_same_method_physical_packages_with_ids_are_not_collapsed_or_cross_updated(self):
        results = [package(package_id="package-a", tracking="LX123456789JP", status="completed", backfill_status="written"),
                   package(package_id="package-b", tracking="LX987654321JP")]
        job = {"orders": [package(package_id="package-a", status="running"), package(package_id="package-b", status="running")], "results": results}
        self.reconcile(job)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["status"], "completed")
        self.assertNotIn(results[1]["status"], {"success", "completed"})
        self.assertIn(job["orders"][0]["status"], {"success", "completed"})
        self.assertEqual(job["orders"][1]["status"], "failed")

    def test_ambiguous_candidates_keep_all_artifacts_without_appending_or_guessing(self):
        results = [package(package_id="package-a", tracking="LX123456789JP"),
                   package(package_id="package-b", tracking="LX987654321JP")]
        job = {"orders": [package(status="running")], "results": results}
        self.reconcile(job)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r["status"] not in {"success", "completed"} for r in results))
        self.assertEqual([r["package_id"] for r in results], ["package-a", "package-b"])

    def test_known_tracking_fills_empty_order_value_but_never_overwrites_another_value(self):
        for original in ("", "EXISTING-TRACKING"):
            with self.subTest(original=original):
                job = {"orders": [package(status="running", tracking_no=original)],
                       "results": [package(tracking="LX123456789JP")]}
                self.reconcile(job)
                self.assertEqual(job["orders"][0]["tracking_no"], original or "LX123456789JP")


class LegacyJobDisplayTests(unittest.TestCase):
    def view(self, job):
        helper = getattr(job_control, "job_result_view", None)
        self.assertIsNotNone(helper, "legacy results need a conservative display projection")
        before = copy.deepcopy(job)
        results, notice = helper(job)
        self.assertEqual(job, before)
        return results, notice

    def legacy_failure(self):
        return {"order_id": "SYNTHETIC-01", "status": "failed", "reason_code": "job_exception", "message": "safe failure"}

    def test_legacy_success_plus_synthetic_failure_is_one_unverified_package(self):
        job = {"status": "error", "orders": [package(status="failed")],
               "results": [package(tracking="LX123456789JP", drive_file_id="synthetic-pdf"), self.legacy_failure()]}
        results, notice = self.view(job)
        self.assertEqual(len(results), 1)
        self.assertEqual(job_control.summarize_job_results(results)["completed"], 0)
        self.assertEqual(results[0]["tracking"], "LX123456789JP")
        self.assertEqual(results[0]["drive_file_id"], "synthetic-pdf")
        self.assertTrue(notice)

    def test_legacy_verified_completion_remains_complete(self):
        job = {"status": "error", "orders": [package(status="failed")],
               "results": [package(status="completed", backfill_status="written", tracking="LX123456789JP"), self.legacy_failure()]}
        results, notice = self.view(job)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "completed")
        self.assertTrue(notice)

    def test_missing_additional_package_is_not_hidden_by_primary_result(self):
        job = {"status": "error", "orders": [package(status="failed"), package(method="EMS", role="additional", status="failed")],
               "results": [package(status="completed", backfill_status="written", tracking="LX123456789JP"), self.legacy_failure()]}
        results, _ = self.view(job)
        self.assertEqual(len(results), 2)
        self.assertEqual(job_control.summarize_job_results(results)["completed"], 1)
        self.assertEqual(job_control.summarize_job_results(results)["failed"], 1)
        self.assertEqual(job_control.shipment_package_key(results[1]), ("SYNTHETIC-01", "EMS", "additional"))

    def test_existing_physical_packages_are_not_collapsed_by_legacy_bookkeeping(self):
        job = {"status": "error", "orders": [package(status="failed")], "results": [
            package(package_id="a", tracking="LX123456789JP"),
            package(package_id="b", tracking="LX987654321JP"), self.legacy_failure()]}
        results, notice = self.view(job)
        self.assertEqual([r["package_id"] for r in results], ["a", "b"])
        self.assertTrue(notice)
        self.assertEqual(job_control.summarize_job_results(results)["completed"], 0)

    def test_non_exception_and_preflight_results_do_not_change(self):
        for status in ("running", "completed", "partial_failure"):
            job = {"status": status, "orders": [package(status="failed")], "results": [package(status="skipped", reason_code="source_changed")]}
            results, notice = self.view(job)
            self.assertEqual(results, job["results"])
            self.assertFalse(notice)

    def test_error_raw_success_without_bookkeeping_still_is_not_verified(self):
        job = {"status": "error", "orders": [package(status="failed")],
               "results": [package(tracking="LX123456789JP")]}
        results, notice = self.view(job)
        self.assertEqual(job_control.summarize_job_results(results)["completed"], 0)
        self.assertEqual(len(results), 1)
        self.assertTrue(notice)
