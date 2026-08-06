import unittest

import pandas as pd

from postal_ui_feedback import (
    filter_pending_orders_after_batch,
    summarize_batch_results,
    summarize_pending_read_logs,
)


class PostalUiFeedbackTests(unittest.TestCase):
    def test_summarizes_pending_read_counts_and_elapsed_time(self):
        summary = summarize_pending_read_logs(
            [
                "📋 篩選後（未打單+必填）：6 筆",
                "🔥 雙重過濾（已完成 6080 筆）：6 → 6 筆",
                "✅ 來源內同注文番号去重：6 → 5 筆",
                "✅ 最終可打單：5 筆，總讀取耗時 15.9s",
            ]
        )

        self.assertEqual(summary["base_count"], "6")
        self.assertEqual(summary["completed_filter"], "6 → 6")
        self.assertEqual(summary["dedup_filter"], "6 → 5")
        self.assertEqual(summary["final_count"], "5")
        self.assertEqual(summary["elapsed"], "15.9s")

    def test_handles_missing_log_lines(self):
        summary = summarize_pending_read_logs([])

        self.assertEqual(summary["base_count"], "-")
        self.assertEqual(summary["final_count"], "-")
        self.assertEqual(summary["elapsed"], "-")

    def test_summarizes_batch_results_and_lists_unmade_order_alerts(self):
        summary = summarize_batch_results(
            [
                {"order_id": "ok-1", "status": "success", "tracking": "LX123456789JP"},
                {"order_id": "ok-2", "status": "completed", "tracking": "LX123456780JP"},
                {
                    "order_id": "imy2038490",
                    "status": "failed",
                    "reason_code": "address_too_long",
                    "reason_text": "地址欄位過長",
                },
            ]
        )

        self.assertEqual(summary["completed_count"], 2)
        self.assertEqual(summary["failed_count"], 1)
        self.assertEqual(summary["failure_alerts"], ["訂單編號 imy2038490：未製單（地址欄位過長）"])

    def test_batch_result_summary_has_no_failure_alert_when_all_complete(self):
        summary = summarize_batch_results(
            [{"order_id": "ok-1", "status": "success", "tracking": "LX123456789JP"}]
        )

        self.assertEqual(summary["completed_count"], 1)
        self.assertEqual(summary["failure_alerts"], [])

    def test_completed_orders_are_removed_from_cached_pending_view(self):
        pending = pd.DataFrame(
            {
                "order_id": ["ok-1", "failed-1", "untouched-1"],
                "Shipping Name": ["A", "B", "C"],
            }
        )
        visible = filter_pending_orders_after_batch(
            pending,
            [
                {"order_id": "ok-1", "status": "completed"},
                {"order_id": "failed-1", "status": "failed"},
            ],
        )

        self.assertEqual(visible["order_id"].tolist(), ["failed-1", "untouched-1"])
        self.assertEqual(pending["order_id"].tolist(), ["ok-1", "failed-1", "untouched-1"])

    def test_non_completed_results_do_not_change_pending_view(self):
        pending = pd.DataFrame({"注文番号(貼上原始資料)": ["blocked-1", "retry-1"]})

        visible = filter_pending_orders_after_batch(
            pending,
            [
                {"order_id": "blocked-1", "status": "blocked"},
                {"order_id": "retry-1", "status": "backfill_failed"},
            ],
        )

        self.assertEqual(visible["注文番号(貼上原始資料)"].tolist(), ["blocked-1", "retry-1"])


if __name__ == "__main__":
    unittest.main()
