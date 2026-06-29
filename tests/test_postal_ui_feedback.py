import unittest

from postal_ui_feedback import summarize_pending_read_logs


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


if __name__ == "__main__":
    unittest.main()
