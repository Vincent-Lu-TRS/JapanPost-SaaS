import threading
import time
from types import SimpleNamespace
import unittest

import pandas as pd

from job_control import (
    BatchJobRegistry,
    build_batch_fingerprint,
    create_order_states,
    filter_key_log_lines,
    mark_results_completed,
    mark_results_failed,
    partition_preflight_rows,
    preflight_batch_orders,
    shipment_package_key,
    summarize_job_results,
    summarize_job_progress,
    update_order_status_from_event,
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
                    "TransType": "EMS",
                },
                {
                    "注文番号(貼上原始資料)": "WhoWhy-Test7",
                    "Shipping Name": "Ines",
                    "收件人國家": "GERMANY",
                    "TransType": "ePacket",
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

    def test_batch_fingerprint_changes_when_shipment_role_changes(self):
        primary = pd.DataFrame(
            [{"order_id": "Synthetic-Order-1", "TransType": "EMS", "_shipment_role": "primary"}]
        )
        additional = primary.copy()
        additional.loc[0, "_shipment_role"] = "additional"

        self.assertNotEqual(
            build_batch_fingerprint(primary, max_rows=None),
            build_batch_fingerprint(additional, max_rows=None),
        )

    def test_shipment_package_key_reads_production_series_shape(self):
        row = pd.Series(
            {
                "注文番号(貼上原始資料)": "Synthetic-Order-1",
                "郵局運送方式(複數商品請自行確認是否走小包)": "EMS",
                "_shipment_role": "additional",
            }
        )

        self.assertEqual(
            shipment_package_key(row),
            ("Synthetic-Order-1", "EMS", "additional"),
        )

    def test_shipment_package_key_reads_dict_and_defaults_legacy_role(self):
        row = {"order_id": "Synthetic-Order-2", "trans_type": "ePacket"}

        self.assertEqual(
            shipment_package_key(row),
            ("Synthetic-Order-2", "ePacket", "primary"),
        )

    def test_shipment_package_key_rejects_invalid_role(self):
        row = {
            "order_id": "Synthetic-Order-3",
            "trans_type": "EMS",
            "shipment_role": "unexpected",
        }

        with self.assertRaisesRegex(ValueError, "invalid shipment role"):
            shipment_package_key(row)

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
        self.assertEqual(states[0]["shipment_role"], "primary")

    def test_create_order_states_assigns_distinct_state_ids_by_shipment_role(self):
        rows = pd.DataFrame(
            [
                {"order_id": "Synthetic-Order-1", "TransType": "EMS", "_shipment_role": "primary"},
                {"order_id": "Synthetic-Order-1", "TransType": "EMS", "_shipment_role": "additional"},
            ]
        )

        states = create_order_states(rows, max_rows=None)

        self.assertEqual([state["shipment_role"] for state in states], ["primary", "additional"])
        self.assertNotEqual(states[0]["state_id"], states[1]["state_id"])

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

    def test_mark_results_completed_promotes_result_after_backfill_verification(self):
        job = {"orders": [{"order_id": "WhoWhy-Test5", "status": "queued", "tracking_no": ""}]}
        results = [{"order_id": "WhoWhy-Test5", "status": "success", "tracking": "LX323090458JP"}]

        mark_results_completed(job, results)

        self.assertEqual(results[0]["status"], "completed")

    def test_summarize_job_progress_reports_current_running_order(self):
        job = {
            "orders": [
                {"order_id": "WhoWht-Test1", "status": "success", "stage": "完成"},
                {"order_id": "WhoWht-Test2", "status": "running", "stage": "填寫收件人"},
                {"order_id": "WhoWht-Test3", "status": "queued", "stage": "待機中"},
            ]
        }

        progress = summarize_job_progress(job)

        self.assertEqual(progress["total"], 3)
        self.assertEqual(progress["done"], 1)
        self.assertEqual(progress["active_order_id"], "WhoWht-Test2")
        self.assertEqual(progress["active_stage"], "填寫收件人")
        self.assertAlmostEqual(progress["ratio"], 1 / 3)

    def test_preflight_batch_orders_blocks_completed_and_changed_orders(self):
        selected = pd.DataFrame(
            [
                {"注文番号(貼上原始資料)": "imy2038510", "TransType": "EMS", "_source_fingerprint": "fp-1"},
                {"注文番号(貼上原始資料)": "imy2038490", "TransType": "EMS", "_source_fingerprint": "fp-2"},
                {"注文番号(貼上原始資料)": "imy2038410", "TransType": "EMS", "_source_fingerprint": "fp-3"},
            ]
        )
        latest = pd.DataFrame(
            [
                {"注文番号(貼上原始資料)": "imy2038510", "TransType": "EMS", "_source_fingerprint": "fp-1"},
                {"注文號": "not-used"},
                {"注文番号(貼上原始資料)": "imy2038410", "TransType": "EMS", "_source_fingerprint": "fp-new"},
            ]
        )

        checks = preflight_batch_orders(selected, latest, {"imy2038490"})

        self.assertEqual(
            {check["order_id"]: check["status"] for check in checks},
            {
                "imy2038510": "ready",
                "imy2038490": "already_completed",
                "imy2038410": "source_changed",
            },
        )

    def test_preflight_batch_orders_blocks_invalid_shipment_role(self):
        selected = pd.DataFrame(
            [
                {
                    "order_id": "Synthetic-Order-3",
                    "TransType": "EMS",
                    "_shipment_role": "unexpected",
                }
            ]
        )

        checks = preflight_batch_orders(selected, selected.copy(), set())

        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["status"], "blocked")
        self.assertEqual(checks[0]["reason_code"], "invalid_shipment_role")

    def test_preflight_is_package_aware_and_partitions_completed_primary_from_ready_additional(self):
        selected = pd.DataFrame(
            [
                {
                    "注文番号(貼上原始資料)": "Synthetic-Order-1",
                    "郵局運送方式(複數商品請自行確認是否走小包)": "EMS",
                    "_shipment_role": "primary",
                    "_source_fingerprint": "fp-1",
                },
                {
                    "注文番号(貼上原始資料)": "Synthetic-Order-1",
                    "郵局運送方式(複數商品請自行確認是否走小包)": "ePacket",
                    "_shipment_role": "additional",
                    "_source_fingerprint": "fp-1",
                },
            ],
            index=[17, 42],
        )
        latest = selected.iloc[[0]].copy()
        completion = SimpleNamespace(
            legacy_order_ids=frozenset({"Synthetic-Order-1"}),
            exact_pairs=frozenset({("Synthetic-Order-1", "LX123456789JP")}),
        )

        checks = preflight_batch_orders(selected, latest, completion)
        ready, already_completed, hard_blocked = partition_preflight_rows(selected, checks)

        self.assertEqual([check["row_index"] for check in checks], [0, 1])
        self.assertEqual([check["status"] for check in checks], ["already_completed", "ready"])
        self.assertEqual(ready.index.tolist(), [42])
        self.assertEqual(len(already_completed), 1)
        self.assertEqual(hard_blocked, [])
        for check in checks:
            self.assertEqual(
                set(("order_id", "trans_type", "shipment_role", "row_index", "reason_code", "reason_text", "status"))
                - set(check),
                set(),
            )

    def test_preflight_blocks_duplicate_package_and_additional_matching_primary_transport(self):
        selected = pd.DataFrame(
            [
                {"order_id": "Synthetic-Order-2", "TransType": "EMS", "shipment_role": "primary"},
                {"order_id": "Synthetic-Order-2", "TransType": "EMS", "shipment_role": "additional"},
                {"order_id": "Synthetic-Order-2", "TransType": "ePacket", "shipment_role": "additional"},
                {"order_id": "Synthetic-Order-2", "TransType": "ePacket", "shipment_role": "additional"},
            ]
        )
        latest = selected.iloc[[0]].copy()

        checks = preflight_batch_orders(
            selected,
            latest,
            SimpleNamespace(legacy_order_ids=frozenset(), exact_pairs=frozenset()),
        )

        self.assertEqual(
            [check["reason_code"] for check in checks],
            ["", "additional_transport_matches_primary", "", "duplicate_package_request"],
        )

    def test_preflight_compares_additional_transport_to_selected_primary_package(self):
        selected = pd.DataFrame(
            [
                {
                    "order_id": "Synthetic-Order-Selected-Primary",
                    "TransType": "AIR",
                    "shipment_role": "primary",
                },
                {
                    "order_id": "Synthetic-Order-Selected-Primary",
                    "TransType": "AIR",
                    "shipment_role": "additional",
                },
            ]
        )
        latest = pd.DataFrame(
            [
                {
                    "order_id": "Synthetic-Order-Selected-Primary",
                    "TransType": "EMS",
                }
            ]
        )

        checks = preflight_batch_orders(selected, latest, set())

        self.assertEqual(checks[0]["status"], "ready")
        self.assertEqual(checks[1]["status"], "blocked")
        self.assertEqual(
            checks[1]["reason_code"],
            "additional_transport_matches_primary",
        )

    def test_preflight_missing_identity_and_partition_missing_result_fail_closed(self):
        selected = pd.DataFrame(
            [
                {"order_id": "", "TransType": "EMS", "shipment_role": "primary"},
                {"order_id": "Synthetic-Order-3", "TransType": "", "shipment_role": "additional"},
                {"order_id": "Synthetic-Order-4", "TransType": "ePacket", "shipment_role": "primary"},
            ]
        )

        checks = preflight_batch_orders(selected, selected.copy(), set())
        ready, already_completed, hard_blocked = partition_preflight_rows(selected, checks[:2])

        self.assertEqual([check["reason_code"] for check in checks[:2]], ["missing_package_identity"] * 2)
        self.assertTrue(ready.empty)
        self.assertEqual(already_completed, [])
        self.assertEqual(hard_blocked[-1]["reason_code"], "missing_preflight_result")
        self.assertEqual(hard_blocked[-1]["row_index"], 2)

    def test_summarize_job_results_counts_success_and_failure_reasons(self):
        summary = summarize_job_results(
            [
                {"order_id": "ok-1", "status": "success", "tracking": "LX123456789JP"},
                {"order_id": "ok-2", "status": "completed", "tracking": "LX123456780JP"},
                {"order_id": "bad-1", "status": "failed", "reason_code": "address_too_long", "reason_text": "地址過長"},
            ]
        )

        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["completed"], 2)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["failures"][0]["order_id"], "bad-1")

    def test_mark_results_failed_records_reason_on_order(self):
        job = {"orders": create_order_states(self._pending_df(), None)}

        mark_results_failed(
            job,
            [{"order_id": "WhoWhy-Test5", "status": "failed", "reason_code": "address_too_long", "reason_text": "地址過長"}],
        )

        self.assertEqual(job["orders"][0]["status"], "failed")
        self.assertEqual(job["orders"][0]["reason_code"], "address_too_long")
        self.assertIn("地址過長", job["orders"][0]["message"])

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

        self.assertEqual(job["orders"][0]["status"], "running")
        self.assertEqual(job["orders"][0]["tracking_no"], "CN123456789JP")
        self.assertEqual(summarize_job_progress(job)["done"], 0)

    def test_update_order_status_from_event_targets_package_without_false_success(self):
        job = {
            "orders": create_order_states(
                pd.DataFrame(
                    [
                        {
                            "order_id": "Synthetic-Order-1",
                            "TransType": "EMS",
                            "_shipment_role": "primary",
                        },
                        {
                            "order_id": "Synthetic-Order-1",
                            "TransType": "ePacket",
                            "_shipment_role": "additional",
                        },
                    ]
                ),
                None,
            )
        }

        updated = update_order_status_from_event(
            job,
            {
                "event": "automation_completed",
                "order_id": "Synthetic-Order-1",
                "trans_type": "ePacket",
                "shipment_role": "additional",
                "tracking": "LX123456789JP",
            },
        )

        self.assertFalse(updated)
        self.assertEqual(job["orders"][0]["status"], "queued")
        self.assertEqual(job["orders"][1]["status"], "queued")
        self.assertEqual(job["orders"][1]["tracking_no"], "")
        self.assertEqual(summarize_job_progress(job)["done"], 0)

    def test_status_events_are_strict_and_label_creation_never_completes(self):
        rows = pd.DataFrame(
            [
                {"order_id": "Synthetic-Order-5", "TransType": "EMS", "shipment_role": "primary"},
                {"order_id": "Synthetic-Order-5", "TransType": "ePacket", "shipment_role": "additional"},
            ],
            index=[8, 99],
        )
        job = {"orders": create_order_states(rows, None)}
        package = {
            "order_id": "Synthetic-Order-5",
            "trans_type": "ePacket",
            "shipment_role": "additional",
            "row_index": 1,
        }

        update_order_status_from_event(job, {"event": "unknown", **package})
        self.assertEqual([row["status"] for row in job["orders"]], ["queued", "queued"])

        update_order_status_from_event(
            job,
            {"event": "label_created", "tracking": "LX123456789JP", **package},
        )
        self.assertEqual(job["orders"][1]["status"], "running")
        self.assertIn("回填", job["orders"][1]["stage"])
        self.assertEqual(summarize_job_progress(job)["done"], 0)

        update_order_status_from_event(
            job,
            {"event": "order_failed", "reason_code": "writeback_readback_failed", **package},
        )
        self.assertEqual(job["orders"][1]["status"], "failed")
        self.assertEqual(job["orders"][0]["status"], "queued")

    def test_writeback_verified_is_only_success_event_and_requires_exact_package(self):
        rows = pd.DataFrame(
            [
                {"order_id": "Synthetic-Order-6", "TransType": "EMS", "shipment_role": "primary"},
                {"order_id": "Synthetic-Order-6", "TransType": "ePacket", "shipment_role": "additional"},
            ]
        )
        job = {"orders": create_order_states(rows, None)}

        update_order_status_from_event(
            job,
            {"event": "writeback_verified", "order_id": "Synthetic-Order-6"},
        )
        self.assertEqual([row["status"] for row in job["orders"]], ["queued", "queued"])

        update_order_status_from_event(
            job,
            {
                "event": "writeback_verified",
                "order_id": "Synthetic-Order-6",
                "trans_type": "EMS",
                "shipment_role": "primary",
                "tracking": "LX123456789JP",
            },
        )
        self.assertEqual([row["status"] for row in job["orders"]], ["success", "queued"])

    def test_qualified_log_targets_package_and_legacy_ambiguous_log_marks_neither(self):
        rows = pd.DataFrame(
            [
                {
                    "order_id": "Synthetic-Order-1",
                    "TransType": "EMS",
                    "_shipment_role": "primary",
                },
                {
                    "order_id": "Synthetic-Order-1",
                    "TransType": "ePacket",
                    "_shipment_role": "additional",
                },
            ]
        )
        ambiguous_job = {"orders": create_order_states(rows, None)}

        update_order_status_from_log(
            ambiguous_job,
            "✅ 訂單 Synthetic-Order-1 完成，單號 LX123456789JP",
        )

        self.assertEqual(
            [order["status"] for order in ambiguous_job["orders"]],
            ["queued", "queued"],
        )

        qualified_job = {"orders": create_order_states(rows, None)}
        update_order_status_from_log(
            qualified_job,
            "✅ 訂單 Synthetic-Order-1 完成，單號 LX123456789JP "
            "[trans_type=ePacket shipment_role=additional]",
        )

        self.assertEqual(
            [order["status"] for order in qualified_job["orders"]],
            ["queued", "running"],
        )
        self.assertEqual(qualified_job["orders"][1]["tracking_no"], "LX123456789JP")
        self.assertEqual(summarize_job_progress(qualified_job)["done"], 0)

    def test_qualified_failure_log_targets_only_matching_package(self):
        rows = pd.DataFrame(
            [
                {
                    "order_id": "Synthetic-Order-1",
                    "TransType": "EMS",
                    "_shipment_role": "primary",
                },
                {
                    "order_id": "Synthetic-Order-1",
                    "TransType": "ePacket",
                    "_shipment_role": "additional",
                },
            ]
        )
        job = {"orders": create_order_states(rows, None)}

        update_order_status_from_log(
            job,
            "⏸️ 訂單 Synthetic-Order-1 requests 流程已停止但未取得完整結果 "
            "[trans_type=ePacket shipment_role=additional]",
        )

        self.assertEqual(
            [order["status"] for order in job["orders"]],
            ["queued", "failed"],
        )

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
