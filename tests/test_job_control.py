import threading
import time
import unittest

import pandas as pd

from job_control import (
    BatchJobRegistry,
    build_batch_fingerprint,
    create_order_states,
    filter_key_log_lines,
    mark_results_completed,
    update_order_status_from_log,
)


class JobControlTests(unittest.TestCase):
    def _pending_df(self):
        return pd.DataFrame(
            [
                {
                    "注文番号(貼上原始資料)": "WhoWhy-Test5",
                    "Shipping Name": "Ada",
                    "收件人國家": "GERMANY",
                },
                {
                    "注文番号(貼上原始資料)": "WhoWhy-Test7",
                    "Shipping Name": "Ines",
                    "收件人國家": "GERMANY",
                },
            ]
        )

    def test_batch_fingerprint_is_stable_for_same_selected_orders(self):
        df = self._pending_df()

        first = build_batch_fingerprint(df, max_rows=1)
        second = build_batch_fingerprint(df.copy(), max_rows=1)

        self.assertEqual(first, second)

    def test_batch_fingerprint_changes_when_selected_orders_change(self):
        df = self._pending_df()

        one_order = build_batch_fingerprint(df, max_rows=1)
        two_orders = build_batch_fingerprint(df, max_rows=2)

        self.assertNotEqual(one_order, two_orders)

    def test_batch_fingerprint_changes_when_trans_type_changes(self):
        first = pd.DataFrame(
            [{"order_id": "WhoWht-Test1", "name": "Fabian", "country": "GERMANY", "TransType": "EMS"}]
        )
        second = first.copy()
        second.loc[0, "TransType"] = "ePacket"

        self.assertNotEqual(
            build_batch_fingerprint(first, max_rows=None),
            build_batch_fingerprint(second, max_rows=None),
        )

    def test_registry_rejects_second_start_for_same_running_user(self):
        registry = BatchJobRegistry()
        df = self._pending_df()

        ok1, _, reason1 = registry.start("user@tkrjm.co.jp", df, None)
        ok2, _, reason2 = registry.start("user@tkrjm.co.jp", df, None)

        self.assertTrue(ok1, reason1)
        self.assertFalse(ok2)
        self.assertEqual(reason2, "user_running")

    def test_registry_rejects_concurrent_same_batch_start_atomically(self):
        registry = BatchJobRegistry()
        df = self._pending_df()
        barrier = threading.Barrier(6)
        results = []

        def worker(i):
            barrier.wait()
            ok, _, _ = registry.start(f"user{i}@tkrjm.co.jp", df, None)
            results.append(ok)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=2)

        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 4)

    def test_completed_job_releases_same_batch_lock(self):
        registry = BatchJobRegistry()
        df = self._pending_df()
        ok1, job1, _ = registry.start("user1@tkrjm.co.jp", df, None)
        registry.finish(job1, "completed")

        ok2, _, reason2 = registry.start("user2@tkrjm.co.jp", df, None)

        self.assertTrue(ok1)
        self.assertTrue(ok2, reason2)

    def test_create_order_states_uses_selected_rows(self):
        states = create_order_states(self._pending_df(), max_rows=1)

        self.assertEqual(len(states), 1)
        self.assertEqual(states[0]["order_id"], "WhoWhy-Test5")
        self.assertEqual(states[0]["status"], "queued")
        self.assertEqual(states[0]["stage"], "待機中")

    def test_mark_results_completed_matches_duplicate_order_by_trans_type(self):
        job = {
            "orders": [
                {"order_id": "WhoWht-Test1", "trans_type": "EMS", "status": "queued", "tracking_no": ""},
                {"order_id": "WhoWht-Test1", "trans_type": "ePacket", "status": "queued", "tracking_no": ""},
            ]
        }

        mark_results_completed(
            job,
            [{"order_id": "WhoWht-Test1", "trans_type": "ePacket", "tracking": "LX323090458JP"}],
        )

        self.assertEqual(job["orders"][0]["status"], "queued")
        self.assertEqual(job["orders"][1]["status"], "success")
        self.assertEqual(job["orders"][1]["tracking_no"], "LX323090458JP")

    def test_update_order_status_from_log_marks_running_and_stopped(self):
        job = {"orders": create_order_states(self._pending_df(), None)}

        update_order_status_from_log(job, "▶ 開始處理訂單：WhoWhy-Test7（索引 1）")
        self.assertEqual(job["orders"][1]["status"], "running")
        self.assertEqual(job["orders"][1]["stage"], "製單中")

        update_order_status_from_log(job, "⏸️ 訂單 WhoWhy-Test7 requests 流程已停止但未取得完整結果")
        self.assertEqual(job["orders"][1]["status"], "failed")
        self.assertIn("未取得完整結果", job["orders"][1]["message"])

    def test_update_order_status_from_log_marks_completed_from_result(self):
        job = {"orders": create_order_states(self._pending_df(), None)}

        update_order_status_from_log(job, "✅ 訂單 WhoWhy-Test5 完成，單號 CN123456789JP")

        self.assertEqual(job["orders"][0]["status"], "success")
        self.assertEqual(job["orders"][0]["tracking_no"], "CN123456789JP")

    def test_filter_key_log_lines_keeps_human_progress(self):
        logs = [
            "[12:00:00] 🔎 M060900 response diagnostics：very noisy",
            "[12:00:01] ▶ 開始處理訂單：WhoWhy-Test5（索引 0）",
            "[12:00:02] 🌐 requests 提交 M060800 Confirm 內容物 payload：debug",
            "[12:00:03] ✅ 完成！共處理 1 筆訂單。",
        ]

        filtered = filter_key_log_lines(logs)

        self.assertEqual(
            filtered,
            [
                "[12:00:01] ▶ 開始處理訂單：WhoWhy-Test5（索引 0）",
                "[12:00:03] ✅ 完成！共處理 1 筆訂單。",
            ],
        )


if __name__ == "__main__":
    unittest.main()
