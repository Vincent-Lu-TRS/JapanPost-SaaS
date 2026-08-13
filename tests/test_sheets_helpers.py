import sys
import types
import unittest
from unittest.mock import patch

import pandas as pd

sys.modules.setdefault(
    "gspread",
    types.SimpleNamespace(Client=object, authorize=lambda *_args, **_kwargs: None),
)
sys.modules.setdefault("streamlit", types.SimpleNamespace(secrets={}, session_state={}))

import bot.sheets as sheets_module
from tests.fake_gspread import FakeClient, FakeWorksheet

from bot.sheets import (
    CompletionAuthority,
    COUNTRY_CODE_MAP,
    _filter_pending_orders_dataframe,
    _get_worksheet_by_gid,
    _prefer_shipping_method_rows,
    _shipping_priority,
    backfill_results,
    get_pending_orders,
    read_completion_authority,
    resolve_country_code,
)


def result(order_id, tracking, *, role="primary", trans_type="EMS", name="Receiver"):
    return {
        "name": name,
        "order_id": order_id,
        "tracking": tracking,
        "country_raw": "US",
        "trans_type": trans_type,
        "shipment_role": role,
    }


class SheetsHelperTests(unittest.TestCase):
    @staticmethod
    def make_client(data_rows, *, row_count=None):
        header = ["", "收件人", "注文番号", "追跡番号", "", "", "", "", "", "國家"]
        worksheet = FakeWorksheet(
            [header, *data_rows],
            row_count=row_count if row_count is not None else len(data_rows) + 1,
        )
        return FakeClient(worksheet)

    def test_last_used_row_considers_b_c_d_and_j(self):
        columns = {
            "B": ["B", ""],
            "C": ["C"] + [""] * 6 + ["ORDER"],
            "D": ["D"] + [""] * 6 + ["TRACK"],
            "J": ["J"] + [""] * 7 + ["US"],
        }

        self.assertEqual(
            sheets_module._last_used_writeback_row(
                columns,
                occupied_formula_rows=set(),
            ),
            9,
        )

    def test_last_used_row_reserves_formula_empty_string_row(self):
        columns = {
            "B": ["B", "Receiver", ""],
            "C": ["C"],
            "D": ["D"],
            "J": ["J"],
        }

        self.assertEqual(
            sheets_module._last_used_writeback_row(
                columns,
                occupied_formula_rows={3},
            ),
            3,
        )

    def test_read_writeback_grid_detects_formula_with_empty_display(self):
        worksheet = FakeWorksheet(
            [
                ["", "收件人", "注文番号", "追跡番号", "", "", "", "", "", "國家"],
                ["", "", "", "", "", "", "", "", "", ""],
            ],
            formula_cells={(2, 2): '=IF(A2="","",A2)'},
        )

        grid = sheets_module._read_writeback_grid(worksheet)

        self.assertEqual(grid.columns["B"][1], "")
        self.assertEqual(grid.occupied_formula_rows, frozenset({2}))

    def test_backfill_expands_grid_before_batch_update(self):
        client = self.make_client([], row_count=1)

        outcome = backfill_results(
            [result("ORDER-1", "TRACK-1")],
            client=client,
            readback_delay_seconds=0,
        )

        self.assertTrue(outcome["ok"])
        self.assertEqual(client.worksheet.calls[0], ("add_rows", 1))
        self.assertEqual(client.worksheet.calls[1][0], "batch_update")

    def test_add_rows_failure_is_not_reported_success(self):
        client = self.make_client([], row_count=1)
        client.worksheet.fail_add_rows = PermissionError("denied")

        outcome = backfill_results([result("ORDER-1", "TRACK-1")], client=client)

        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["items"][0]["status"], "write_failed")
        self.assertFalse(
            any(call[0] == "batch_update" for call in client.worksheet.calls)
        )

    def test_exact_order_tracking_pair_is_idempotent(self):
        client = self.make_client(
            [["", "Receiver", "ORDER-1", "TRACK-1", "", "", "", "", "", "US"]]
        )

        outcome = backfill_results([result("ORDER-1", "TRACK-1")], client=client)

        self.assertEqual(outcome["existing"], 1)
        self.assertEqual(outcome["written"], 0)
        self.assertEqual(outcome["items"][0]["status"], "already_present")
        self.assertFalse(
            any(call[0] == "batch_update" for call in client.worksheet.calls)
        )

    def test_explicit_additional_tracking_appends_new_row(self):
        client = self.make_client(
            [["", "Receiver", "ORDER-1", "TRACK-1", "", "", "", "", "", "US"]]
        )

        outcome = backfill_results(
            [result("ORDER-1", "TRACK-2", role="additional", trans_type="AIR")],
            client=client,
            readback_delay_seconds=0,
        )

        self.assertEqual(outcome["written"], 1)
        self.assertEqual(outcome["items"][0]["status"], "written")
        self.assertEqual(outcome["items"][0]["row"], 3)

    def test_same_batch_primary_and_additional_each_append(self):
        client = self.make_client([], row_count=2)

        outcome = backfill_results(
            [
                result("ORDER-1", "TRACK-1"),
                result("ORDER-1", "TRACK-2", role="additional", trans_type="AIR"),
            ],
            client=client,
            readback_delay_seconds=0,
        )

        self.assertEqual(
            [item["status"] for item in outcome["items"]],
            ["written", "written"],
        )
        self.assertEqual([item["row"] for item in outcome["items"]], [2, 3])
        self.assertEqual(outcome["written"], 2)

    def test_unexpected_second_tracking_requires_manual_review(self):
        client = self.make_client(
            [["", "Receiver", "ORDER-1", "TRACK-1", "", "", "", "", "", "US"]]
        )

        outcome = backfill_results([result("ORDER-1", "TRACK-2")], client=client)

        self.assertEqual(outcome["items"][0]["status"], "manual_review")
        self.assertEqual(
            outcome["items"][0]["reason_code"],
            "unexpected_second_tracking",
        )
        self.assertFalse(
            any(call[0] == "batch_update" for call in client.worksheet.calls)
        )

    def test_same_tracking_for_different_order_is_conflict(self):
        client = self.make_client(
            [["", "Receiver", "ORDER-1", "TRACK-1", "", "", "", "", "", "US"]]
        )

        outcome = backfill_results([result("ORDER-2", "TRACK-1")], client=client)

        self.assertEqual(outcome["items"][0]["status"], "conflict")
        self.assertEqual(
            outcome["items"][0]["reason_code"],
            "tracking_owned_by_other_order",
        )

    def test_incomplete_existing_order_requires_manual_review(self):
        client = self.make_client(
            [["", "Receiver", "ORDER-1", "", "", "", "", "", "", "US"]]
        )

        outcome = backfill_results([result("ORDER-1", "TRACK-1")], client=client)

        self.assertEqual(outcome["items"][0]["status"], "manual_review")
        self.assertEqual(
            outcome["items"][0]["reason_code"],
            "incomplete_existing_row",
        )

    def test_duplicate_batch_pair_requires_manual_review(self):
        client = self.make_client([], row_count=2)

        outcome = backfill_results(
            [result("ORDER-1", "TRACK-1"), result("ORDER-1", "TRACK-1")],
            client=client,
            readback_delay_seconds=0,
        )

        self.assertEqual(
            [item["status"] for item in outcome["items"]],
            ["written", "manual_review"],
        )
        self.assertEqual(outcome["items"][1]["reason_code"], "duplicate_batch_pair")

    def test_invalid_role_is_invalid_writeback_identity(self):
        client = self.make_client([], row_count=2)

        outcome = backfill_results(
            [result("ORDER-1", "TRACK-1", role="replacement")],
            client=client,
        )

        self.assertEqual(outcome["items"][0]["status"], "manual_review")
        self.assertEqual(
            outcome["items"][0]["reason_code"],
            "invalid_writeback_identity",
        )
        self.assertFalse(
            any(call[0] == "batch_update" for call in client.worksheet.calls)
        )

    def test_readback_verifies_all_four_columns(self):
        client = self.make_client([], row_count=2)
        client.worksheet.drop_columns.add("J")

        outcome = backfill_results(
            [result("ORDER-1", "TRACK-1")],
            client=client,
            readback_attempts=1,
        )

        self.assertEqual(outcome["items"][0]["status"], "partial_write")
        self.assertFalse(outcome["ok"])

    def test_readback_requires_exact_nonblank_order_and_tracking(self):
        client = self.make_client([], row_count=2)
        client.worksheet.drop_columns.add("D")

        outcome = backfill_results(
            [result("ORDER-1", "TRACK-1")],
            client=client,
            readback_attempts=1,
        )

        self.assertEqual(outcome["items"][0]["status"], "partial_write")
        self.assertEqual(outcome["items"][0]["reason_code"], "partial_write")
        self.assertFalse(outcome["ok"])

    def test_readback_requires_nonblank_recipient_and_country(self):
        client = self.make_client([], row_count=2)

        outcome = backfill_results(
            [result("ORDER-1", "TRACK-1", name="")],
            client=client,
            readback_attempts=1,
        )

        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["items"][0]["status"], "partial_write")
        self.assertEqual(outcome["items"][0]["reason_code"], "partial_write")

    def test_readback_retries_until_values_are_visible(self):
        sleeps = []
        client = self.make_client([], row_count=2)
        client.worksheet.stale_reads_remaining = 2

        outcome = backfill_results(
            [result("ORDER-1", "TRACK-1")],
            client=client,
            readback_attempts=3,
            readback_delay_seconds=0.01,
            sleep_fn=sleeps.append,
        )

        self.assertEqual(outcome["items"][0]["status"], "written")
        self.assertEqual(sleeps, [0.01, 0.01])

    def test_readback_retry_exhaustion_is_failure(self):
        client = self.make_client([], row_count=2)
        client.worksheet.stale_reads_remaining = 99

        outcome = backfill_results(
            [result("ORDER-1", "TRACK-1")],
            client=client,
            readback_attempts=3,
            readback_delay_seconds=0,
        )

        self.assertFalse(outcome["ok"])
        self.assertEqual(
            outcome["items"][0]["reason_code"],
            "writeback_readback_failed",
        )

    def test_one_conflict_does_not_erase_verified_success(self):
        client = self.make_client(
            [["", "Receiver", "ORDER-X", "TRACK-X", "", "", "", "", "", "US"]],
            row_count=3,
        )

        outcome = backfill_results(
            [result("ORDER-1", "TRACK-1"), result("ORDER-2", "TRACK-X")],
            client=client,
            readback_delay_seconds=0,
        )

        self.assertEqual(
            [item["status"] for item in outcome["items"]],
            ["written", "conflict"],
        )
        self.assertEqual(outcome["written"], 1)
        self.assertFalse(outcome["ok"])

    def test_batch_update_partial_write_reports_per_item_outcomes(self):
        client = self.make_client([], row_count=3)
        client.worksheet.drop_rows.add(3)

        outcome = backfill_results(
            [result("ORDER-1", "TRACK-1"), result("ORDER-2", "TRACK-2")],
            client=client,
            readback_attempts=1,
        )

        self.assertEqual(outcome["items"][0]["status"], "written")
        self.assertIn(
            outcome["items"][1]["status"],
            {"partial_write", "write_failed"},
        )

    def test_initial_sheet_read_failure_returns_item_outcomes(self):
        client = self.make_client([], row_count=2)
        client.worksheet.col_values = lambda column: (_ for _ in ()).throw(
            PermissionError("ORDER-SECRET")
        )
        logs = []

        outcome = backfill_results(
            [result("ORDER-SECRET", "TRACK-SECRET")],
            client=client,
            log_cb=logs.append,
        )

        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["items"][0]["status"], "write_failed")
        self.assertEqual(
            outcome["items"][0]["reason_code"],
            "writeback_permission_denied",
        )
        self.assertNotIn("ORDER-SECRET", "\n".join(logs))
        self.assertNotIn("TRACK-SECRET", "\n".join(logs))

    def test_readback_failure_returns_item_outcomes_without_exception_text_in_logs(self):
        client = self.make_client([], row_count=2)
        original_get = client.worksheet.get
        call_count = 0

        def failing_get(a1_range, value_render_option=None):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise RuntimeError("ORDER-SECRET TRACK-SECRET")
            return original_get(a1_range, value_render_option=value_render_option)

        client.worksheet.get = failing_get
        logs = []

        outcome = backfill_results(
            [result("ORDER-SECRET", "TRACK-SECRET")],
            client=client,
            log_cb=logs.append,
        )

        self.assertEqual(outcome["items"][0]["status"], "write_failed")
        self.assertEqual(outcome["items"][0]["reason_code"], "writeback_api_error")
        self.assertNotIn("ORDER-SECRET", "\n".join(logs))
        self.assertNotIn("TRACK-SECRET", "\n".join(logs))

    def test_read_completion_authority_uses_target_gid_and_reads_exact_cd_pairs(self):
        class FakeWorksheet:
            def get(self, range_name):
                self.range_name = range_name
                return [
                    ["order_id", "tracking"],
                    ["ORDER-1", "LX123456789JP"],
                    ["ORDER-2", ""],
                    ["", "EE123456789JP"],
                ]

        worksheet = FakeWorksheet()

        class FakeSpreadsheet:
            def get_worksheet_by_id(self, gid):
                self.gid = gid
                return worksheet

        spreadsheet = FakeSpreadsheet()

        class FakeClient:
            def open_by_key(self, key):
                self.key = key
                return spreadsheet

        authority = read_completion_authority(FakeClient())

        self.assertIsInstance(authority, CompletionAuthority)
        self.assertEqual(authority.legacy_order_ids, frozenset({"ORDER-1", "ORDER-2"}))
        self.assertEqual(authority.exact_pairs, frozenset({("ORDER-1", "LX123456789JP")}))
        self.assertEqual(spreadsheet.gid, int(sheets_module.TARGET_GID))
        self.assertEqual(worksheet.range_name, "C:D")

    def test_get_pending_orders_strict_permission_failure_uses_safe_code(self):
        logs = []

        with patch.object(
            sheets_module,
            "_get_gspread_client",
            side_effect=PermissionError("private-sheet@example.com ORDER-8490"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "^pending_read_permission_denied$",
            ) as raised:
                get_pending_orders(log_cb=logs.append, strict=True)

        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(logs, ["pending_read_failed error_type=PermissionError"])
        self.assertNotIn("private-sheet", " ".join(logs))

    def test_get_pending_orders_non_strict_failure_returns_empty_dataframe(self):
        logs = []

        with patch.object(
            sheets_module,
            "_get_gspread_client",
            side_effect=ConnectionError("token=secret"),
        ):
            result = get_pending_orders(log_cb=logs.append, strict=False)

        self.assertTrue(result.empty)
        self.assertEqual(logs, ["pending_read_failed error_type=ConnectionError"])

    def test_get_pending_orders_default_remains_non_strict(self):
        with patch.object(
            sheets_module,
            "_get_gspread_client",
            side_effect=RuntimeError("private details"),
        ):
            result = get_pending_orders()

        self.assertTrue(result.empty)

    def test_get_pending_orders_can_skip_completed_target_read(self):
        header = [
            "注文番号(貼上原始資料)",
            "製單上傳狀態(請用[未打單]檢視模式)",
            "郵局申告金額(USD)",
            "製單檢核",
            "Shipping Name",
            "郵局運送方式(複數商品請自行確認是否走小包)",
        ]
        values = [
            header,
            ["ORDER-1", "未打單", "1.00", "", "Receiver", "ePacket"],
        ]

        class FakeWorksheet:
            def get_all_values(self):
                return values

        class FakeSpreadsheet:
            title = "source"

            def get_worksheet_by_id(self, _gid):
                return FakeWorksheet()

        class FakeClient:
            def open_by_key(self, key):
                self.opened = key
                return FakeSpreadsheet()

        with (
            patch.object(sheets_module, "_get_gspread_client", return_value=FakeClient()),
            patch.object(
                sheets_module,
                "read_completed_order_ids",
                side_effect=AssertionError("completed target should not be read"),
            ),
        ):
            result = get_pending_orders(exclude_completed=False)

        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["_source_row_number"], "2")
        self.assertTrue(result.iloc[0]["_source_fingerprint"])

    def test_backfill_results_writes_eu_for_formula_dictionary_europe_country(self):
        client = self.make_client([], row_count=2)

        outcome = backfill_results(
            [
                {
                    "name": "Julie Rouleau",
                    "order_id": "WhoWht-Test5",
                    "tracking": "LX323090458JP",
                    "country_raw": "PORTUGAL（ポルトガル）",
                }
            ],
            client=client,
            readback_delay_seconds=0,
        )

        batch = next(call[1] for call in client.worksheet.calls if call[0] == "batch_update")
        self.assertIn({"range": "J2:J2", "values": [["EU"]]}, batch)
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["written"], 1)

    def test_backfill_results_rejects_blank_tracking_before_sheet_access(self):
        with patch.object(
            sheets_module,
            "_get_gspread_client",
            side_effect=AssertionError("blank tracking must not reach Google Sheets"),
        ):
            outcome = backfill_results(
                [{"name": "Synthetic", "order_id": "ORDER-BLANK", "tracking": ""}]
            )

        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["written"], 0)
        self.assertEqual(outcome["error"], "invalid_writeback_identity")

    def test_backfill_readback_requires_nonblank_order_and_tracking_pair(self):
        client = self.make_client([], row_count=2)
        client.worksheet.drop_columns.add("D")

        outcome = backfill_results(
            [
                {
                    "name": "Synthetic",
                    "order_id": "ORDER-READBACK",
                    "tracking": "LX123456789JP",
                }
            ],
            client=client,
            readback_attempts=1,
        )

        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["items"][0]["status"], "partial_write")

    def test_get_worksheet_by_gid_uses_direct_lookup(self):
        class FakeSpreadsheet:
            title = "Fake Sheet"

            def __init__(self):
                self.requested_ids = []

            def get_worksheet_by_id(self, gid):
                self.requested_ids.append(gid)
                return f"worksheet-{gid}"

            def worksheets(self):
                raise AssertionError("worksheets() should not be called for direct GID lookup")

        spreadsheet = FakeSpreadsheet()

        result = _get_worksheet_by_gid(spreadsheet, "605188303")

        self.assertEqual(result, "worksheet-605188303")
        self.assertEqual(spreadsheet.requested_ids, [605188303])

    def test_get_worksheet_by_gid_returns_none_when_missing(self):
        class FakeSpreadsheet:
            def get_worksheet_by_id(self, gid):
                raise LookupError(f"missing {gid}")

        self.assertIsNone(_get_worksheet_by_gid(FakeSpreadsheet(), "605188303"))

    def test_country_code_map_includes_new_japanese_variants(self):
        self.assertEqual(COUNTRY_CODE_MAP["KOREA（韓国）"], "KR")
        self.assertEqual(COUNTRY_CODE_MAP["BELGIUM（ベルギー）"], "EU")
        self.assertEqual(COUNTRY_CODE_MAP["GREECE（ギリシャ）"], "EU")
        self.assertEqual(COUNTRY_CODE_MAP["CZECH（チェコ）"], "EU")
        self.assertEqual(COUNTRY_CODE_MAP["ROMANIA（ルーマニア）"], "EU")
        self.assertEqual(COUNTRY_CODE_MAP["INDONESIA（インドネシア）"], "ID")
        self.assertEqual(COUNTRY_CODE_MAP["CYPRUS（キプロス）"], "EU")

    def test_resolve_country_code_uses_country_formula_dictionary_and_europe_region(self):
        self.assertEqual(resolve_country_code("DE"), "EU")
        self.assertEqual(resolve_country_code("GERMANY"), "EU")
        self.assertEqual(resolve_country_code("GERMANY（ドイツ）"), "EU")
        self.assertEqual(resolve_country_code("FRANCE（法國）"), "EU")
        self.assertEqual(resolve_country_code("UNITED STATES OF AMERICA"), "US")
        self.assertEqual(resolve_country_code("US"), "US")
        self.assertEqual(resolve_country_code("KOREA"), "KR")

    def test_shipping_priority_orders_ems_parcel_epacket(self):
        self.assertGreater(_shipping_priority("EMS（US）"), _shipping_priority("國際小包（Air）"))
        self.assertGreater(_shipping_priority("國際小包（Air）"), _shipping_priority("ePacket Light"))

    @unittest.skipIf(pd.DataFrame is object, "real pandas is not available in this unit-test shim")
    def test_prefer_shipping_method_rows_keeps_highest_priority_per_order(self):
        df = pd.DataFrame(
            [
                {
                    "注文番号(貼上原始資料)": "WhoWhy-Test7",
                    "郵局運送方式(複數商品請自行確認是否走小包)": "ePacket",
                    "內容物1": "Facial Mask",
                },
                {
                    "注文番号(貼上原始資料)": "WhoWhy-Test7",
                    "郵局運送方式(複數商品請自行確認是否走小包)": "國際小包",
                    "內容物1": "Facial Mask",
                },
                {
                    "注文番号(貼上原始資料)": "WhoWhy-Test8",
                    "郵局運送方式(複數商品請自行確認是否走小包)": "ePacket",
                    "內容物1": "Hair Conditioner",
                },
            ]
        )

        result = _prefer_shipping_method_rows(
            df,
            order_id_col="注文番号(貼上原始資料)",
            shipping_col="郵局運送方式(複數商品請自行確認是否走小包)",
        )

        self.assertEqual(list(result["注文番号(貼上原始資料)"]), ["WhoWhy-Test7", "WhoWhy-Test8"])
        self.assertEqual(
            result.iloc[0]["郵局運送方式(複數商品請自行確認是否走小包)"],
            "國際小包",
        )

    @unittest.skipIf(pd.DataFrame is object, "real pandas is not available in this unit-test shim")
    def test_filter_pending_orders_keeps_four_unique_orders_from_six_recreated_rows(self):
        df = pd.DataFrame(
            [
                {
                    "注文番号(貼上原始資料)": "WhoWhy-Test5",
                    "製單上傳狀態(請用[未打單]檢視模式)": "未打單",
                    "郵局申告金額(USD)": "1.55",
                    "製單檢核": "",
                    "Shipping Name": "Jimmy Wang",
                    "郵局運送方式(複數商品請自行確認是否走小包)": "國際小包",
                },
                {
                    "注文番号(貼上原始資料)": "WhoWhy-Test6",
                    "製單上傳狀態(請用[未打單]檢視模式)": "未打單",
                    "郵局申告金額(USD)": "1.55",
                    "製單檢核": "",
                    "Shipping Name": "Ioannis Zervos",
                    "郵局運送方式(複數商品請自行確認是否走小包)": "ePacket",
                },
                {
                    "注文番号(貼上原始資料)": "WhoWhy-Test7",
                    "製單上傳狀態(請用[未打單]檢視模式)": "未打單",
                    "郵局申告金額(USD)": "1.55",
                    "製單檢核": "",
                    "Shipping Name": "Ines Budde",
                    "郵局運送方式(複數商品請自行確認是否走小包)": "ePacket",
                },
                {
                    "注文番号(貼上原始資料)": "WhoWhy-Test7",
                    "製單上傳狀態(請用[未打單]檢視模式)": "未打單",
                    "郵局申告金額(USD)": "1.55",
                    "製單檢核": "",
                    "Shipping Name": "Ines Budde",
                    "郵局運送方式(複數商品請自行確認是否走小包)": "國際小包",
                },
                {
                    "注文番号(貼上原始資料)": "WhoWhy-Test8",
                    "製單上傳狀態(請用[未打單]檢視模式)": "未打單",
                    "郵局申告金額(USD)": "1.55",
                    "製單檢核": "",
                    "Shipping Name": "Ceci Chan",
                    "郵局運送方式(複數商品請自行確認是否走小包)": "ePacket",
                },
                {
                    "注文番号(貼上原始資料)": "WhoWhy-Test8",
                    "製單上傳狀態(請用[未打單]檢視模式)": "未打單",
                    "郵局申告金額(USD)": "1.55",
                    "製單檢核": "",
                    "Shipping Name": "Ceci Chan",
                    "郵局運送方式(複數商品請自行確認是否走小包)": "國際小包",
                },
            ]
        )
        logs = []

        result = _filter_pending_orders_dataframe(df, completed_ids=set(), log_cb=logs.append)

        self.assertEqual(
            list(result["注文番号(貼上原始資料)"]),
            ["WhoWhy-Test5", "WhoWhy-Test6", "WhoWhy-Test7", "WhoWhy-Test8"],
        )
        self.assertTrue(any("來源內同注文番号去重" in line for line in logs))

    @unittest.skipIf(pd.DataFrame is object, "real pandas is not available in this unit-test shim")
    def test_filter_pending_orders_legacy_rows_keep_first_row_without_aggregation(self):
        df = pd.DataFrame(
            [
                {
                    "注文番号(貼上原始資料)": "imy2038510",
                    "製單上傳狀態(請用[未打單]檢視模式)": "未打單",
                    "郵局內容物": "Water Bottle TRSN9767",
                    "郵局申告金額(USD)": "3.14",
                    "數量集合": "1",
                    "製單檢核": "",
                    "Shipping Name": "Ying Chan",
                    "郵局運送方式(複數商品請自行確認是否走小包)": "ePacket",
                },
                {
                    "注文番号(貼上原始資料)": "imy2038510",
                    "製單上傳狀態(請用[未打單]檢視模式)": "未打單",
                    "郵局內容物": "Water Bottle TRSN9765",
                    "郵局申告金額(USD)": "2.67",
                    "數量集合": "1",
                    "製單檢核": "",
                    "Shipping Name": "Ying Chan",
                    "郵局運送方式(複數商品請自行確認是否走小包)": "ePacket",
                },
                {
                    "注文番号(貼上原始資料)": "imy2038510",
                    "製單上傳狀態(請用[未打單]檢視模式)": "未打單",
                    "郵局內容物": "Water Bottle TRSN9763",
                    "郵局申告金額(USD)": "2.64",
                    "數量集合": "1",
                    "製單檢核": "",
                    "Shipping Name": "Ying Chan",
                    "郵局運送方式(複數商品請自行確認是否走小包)": "ePacket",
                },
            ]
        )

        result = _filter_pending_orders_dataframe(df, completed_ids=set())

        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["郵局內容物"], "Water Bottle TRSN9767")
        self.assertEqual(result.iloc[0]["郵局申告金額(USD)"], "3.14")
        self.assertFalse(str(result.iloc[0].get("內容物1", "")).strip())

    @unittest.skipIf(pd.DataFrame is object, "real pandas is not available in this unit-test shim")
    def test_filter_pending_orders_treats_repeated_array_formula_payload_as_one_source_row(self):
        order_id = "\u6ce8\u6587\u756a\u53f7(\u8cbc\u4e0a\u539f\u59cb\u8cc7\u6599)"
        status = "\u88fd\u55ae\u4e0a\u50b3\u72c0\u614b(\u8acb\u7528[\u672a\u6253\u55ae]\u6aa2\u8996\u6a21\u5f0f)"
        amount = "\u90f5\u5c40\u7533\u544a\u91d1\u984d(USD)"
        check = "\u88fd\u55ae\u6aa2\u6838"
        shipping = "\u90f5\u5c40\u904b\u9001\u65b9\u5f0f(\u8907\u6578\u5546\u54c1\u8acb\u81ea\u884c\u78ba\u8a8d\u662f\u5426\u8d70\u5c0f\u5305)"
        rows = []
        for source_row in range(3):
            rows.append(
                {
                    order_id: "imy2038510",
                    status: "\u672a\u6253\u55ae",
                    amount: "3.14",
                    check: "",
                    "Shipping Name": "Ying Chan",
                    shipping: "ePacket",
                    "\u5167\u5bb9\u72691": "Water Bottle TRSN9767",
                    "\u5167\u5bb9\u72692": "Water Bottle TRSN9765",
                    "\u5167\u5bb9\u72693": "Water Bottle TRSN9763",
                    "\u7533\u544a\u91d1\u984d1": "3.14",
                    "\u7533\u544a\u91d1\u984d2": "2.67",
                    "\u7533\u544a\u91d1\u984d3": "2.64",
                    "\u6578\u91cf1": "1",
                    "\u6578\u91cf2": "1",
                    "\u6578\u91cf3": "1",
                    "_source_row_number": str(source_row + 1969),
                }
            )

        result = _filter_pending_orders_dataframe(pd.DataFrame(rows), completed_ids=set())

        self.assertEqual(len(result), 1)
        self.assertEqual(
            list(result.iloc[0][["\u5167\u5bb9\u72691", "\u5167\u5bb9\u72692", "\u5167\u5bb9\u72693"]]),
            ["Water Bottle TRSN9767", "Water Bottle TRSN9765", "Water Bottle TRSN9763"],
        )
        self.assertEqual(list(result.iloc[0][["\u7533\u544a\u91d1\u984d1", "\u7533\u544a\u91d1\u984d2", "\u7533\u544a\u91d1\u984d3"]]), ["3.14", "2.67", "2.64"])
        self.assertEqual(list(result.iloc[0][["\u6578\u91cf1", "\u6578\u91cf2", "\u6578\u91cf3"]]), ["1", "1", "1"])
        self.assertTrue(
            all(
                not str(result.iloc[0].get(f"\u5167\u5bb9\u7269{index}", "")).strip()
                for index in range(4, 11)
            )
        )

    def test_filter_pending_orders_excludes_stale_source_tracking_when_target_is_missing(self):
        df = pd.DataFrame(
            [
                {
                    "注文番号(貼上原始資料)": "imy2036360",
                    "製單上傳狀態(請用[未打單]檢視模式)": "LX324329616JP",
                    "郵局申告金額(USD)": "11.57",
                    "製單檢核": "FALSE",
                    "Shipping Name": "David G Derrick Jr",
                    "郵局運送方式(複數商品請自行確認是否走小包)": "ePacket",
                }
            ]
        )

        result = _filter_pending_orders_dataframe(df, completed_ids=set())

        self.assertTrue(result.empty)

    @unittest.skipIf(pd.DataFrame is object, "real pandas is not available in this unit-test shim")
    def test_filter_pending_orders_logs_completed_id_exclusions(self):
        df = pd.DataFrame(
            [
                {
                    "注文番号(貼上原始資料)": "WhoWhy-Test6",
                    "製單上傳狀態(請用[未打單]檢視模式)": "未打單",
                    "郵局申告金額(USD)": "1.55",
                    "製單檢核": "",
                    "Shipping Name": "Ioannis Zervos",
                    "郵局運送方式(複數商品請自行確認是否走小包)": "ePacket",
                }
            ]
        )
        logs = []

        result = _filter_pending_orders_dataframe(df, completed_ids={"WhoWhy-Test6"}, log_cb=logs.append)

        self.assertTrue(result.empty)
        self.assertTrue(any("已在目標表完成而排除" in line for line in logs))
        self.assertNotIn("WhoWhy-Test6", "\n".join(logs))
        self.assertNotIn("Ioannis Zervos", "\n".join(logs))

    @unittest.skipIf(pd.DataFrame is object, "real pandas is not available in this unit-test shim")
    def test_filter_pending_orders_blocks_stale_source_tracking_when_target_missing(self):
        df = pd.DataFrame(
            [
                {
                    "注文番号(貼上原始資料)": "imy2036360",
                    "製單上傳狀態(請用[未打單]檢視模式)": "LX324329616JP",
                    "郵局申告金額(USD)": "11.57",
                    "製單檢核": "FALSE",
                    "Shipping Name": "David G Derrick Jr",
                    "郵局運送方式(複數商品請自行確認是否走小包)": "ePacket",
                }
            ]
        )
        logs = []

        result = _filter_pending_orders_dataframe(df, completed_ids=set(), log_cb=logs.append)

        self.assertTrue(result.empty)
        self.assertTrue(any("來源狀態疑似快取過期" in line for line in logs))
        rendered = "\n".join(logs)
        self.assertNotIn("imy2036360", rendered)
        self.assertNotIn("LX324329616JP", rendered)
        self.assertNotIn("David G Derrick Jr", rendered)

    def test_get_pending_orders_sanitizes_source_samples_before_callback(self):
        header = [
            "注文番号(貼上原始資料)",
            "製單上傳狀態(請用[未打單]檢視模式)",
            "郵局申告金額(USD)",
            "製單檢核",
            "Shipping Name",
            "Email",
            "郵局運送方式(複數商品請自行確認是否走小包)",
        ]
        values = [
            header,
            [
                "ORDER-SECRET",
                "未打單",
                "1.00",
                "",
                "Secret Recipient",
                "receiver@example.com",
                "ePacket",
            ],
        ]

        class FakeWorksheet:
            def get_all_values(self):
                return values

        class FakeSpreadsheet:
            title = "source"

            def get_worksheet_by_id(self, _gid):
                return FakeWorksheet()

        class FakeClient:
            def open_by_key(self, _key):
                return FakeSpreadsheet()

        logs = []
        with patch.object(sheets_module, "_get_gspread_client", return_value=FakeClient()):
            result = get_pending_orders(
                log_cb=logs.append,
                strict=True,
                exclude_completed=False,
            )

        self.assertEqual(len(result), 1)
        rendered = "\n".join(logs)
        for secret in ("ORDER-SECRET", "Secret Recipient", "receiver@example.com"):
            self.assertNotIn(secret, rendered)

    @unittest.skipIf(pd.DataFrame is object, "real pandas is not available in this unit-test shim")
    def test_filter_pending_orders_does_not_override_tracking_status_without_target_authority(self):
        df = pd.DataFrame(
            [
                {
                    "注文番号(貼上原始資料)": "imy2036360",
                    "製單上傳狀態(請用[未打單]檢視模式)": "LX324329616JP",
                    "郵局申告金額(USD)": "11.57",
                    "製單檢核": "FALSE",
                    "Shipping Name": "David G Derrick Jr",
                    "郵局運送方式(複數商品請自行確認是否走小包)": "ePacket",
                }
            ]
        )

        result = _filter_pending_orders_dataframe(df, completed_ids=None)

        self.assertTrue(result.empty)

    @unittest.skipIf(pd.DataFrame is object, "real pandas is not available in this unit-test shim")
    def test_filter_pending_orders_logs_base_exclusion_reasons(self):
        df = pd.DataFrame(
            [
                {
                    "注文番号(貼上原始資料)": "WhoWhy-Test6",
                    "製單上傳狀態(請用[未打單]檢視模式)": "未打單",
                    "郵局申告金額(USD)": "1.55",
                    "製單檢核": "TRUE",
                    "Shipping Name": "Ioannis Zervos",
                    "郵局運送方式(複數商品請自行確認是否走小包)": "ePacket",
                },
                {
                    "注文番号(貼上原始資料)": "WhoWhy-Test8",
                    "製單上傳狀態(請用[未打單]檢視模式)": "未打單",
                    "郵局申告金額(USD)": "1.55",
                    "製單檢核": "",
                    "Shipping Name": "",
                    "郵局運送方式(複數商品請自行確認是否走小包)": "ePacket",
                },
            ]
        )
        logs = []

        result = _filter_pending_orders_dataframe(df, completed_ids=set(), log_cb=logs.append)

        self.assertTrue(result.empty)
        self.assertTrue(any("製單檢核 TRUE 排除" in line and "1 筆" in line for line in logs))
        self.assertTrue(any("Shipping Name 空白排除" in line and "1 筆" in line for line in logs))

    @unittest.skipIf(pd.DataFrame is object, "real pandas is not available in this unit-test shim")
    def test_filter_pending_orders_logs_each_whowhy_row_status(self):
        df = pd.DataFrame(
            [
                {
                    "注文番号(貼上原始資料)": "WhoWhy-Test6",
                    "製單上傳狀態(請用[未打單]檢視模式)": "未打單",
                    "郵局申告金額(USD)": "1.55",
                    "製單檢核": "TRUE",
                    "Shipping Name": "Ioannis Zervos",
                    "郵局運送方式(複數商品請自行確認是否走小包)": "ePacket",
                },
                {
                    "注文番号(貼上原始資料)": "WhoWhy-Test7",
                    "製單上傳狀態(請用[未打單]檢視模式)": "未打單",
                    "郵局申告金額(USD)": "1.55",
                    "製單檢核": "",
                    "Shipping Name": "Ines Budde",
                    "郵局運送方式(複數商品請自行確認是否走小包)": "國際小包",
                },
            ]
        )
        logs = []

        _filter_pending_orders_dataframe(df, completed_ids=set(), log_cb=logs.append)

        whowhy_lines = [line for line in logs if "關注訂單診斷" in line]
        self.assertEqual(len(whowhy_lines), 1)
        self.assertTrue("PASS=1" in whowhy_lines[0])
        self.assertTrue("FAIL=1" in whowhy_lines[0])

    @unittest.skipIf(pd.DataFrame is object, "real pandas is not available in this unit-test shim")
    def test_filter_pending_orders_keeps_watched_diagnostics_concise(self):
        df = pd.DataFrame(
            [
                {
                    "注文番号(貼上原始資料)": f"WhoWhy-Test{i}",
                    "製單上傳狀態(請用[未打單]檢視模式)": "未打單",
                    "郵局申告金額(USD)": "1.55",
                    "製單檢核": "",
                    "Shipping Name": f"Name {i}",
                    "郵局運送方式(複數商品請自行確認是否走小包)": "ePacket",
                }
                for i in range(12)
            ]
        )
        logs = []

        _filter_pending_orders_dataframe(df, completed_ids=set(), log_cb=logs.append)

        whowhy_lines = [line for line in logs if "- 關注訂單" in line]
        self.assertEqual(len(whowhy_lines), 0)
        self.assertTrue(any("關注訂單診斷" in line and "PASS=12" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
