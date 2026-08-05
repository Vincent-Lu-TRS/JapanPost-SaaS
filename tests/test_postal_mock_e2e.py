import sys
import types
import unittest

import pandas as pd

sys.modules.setdefault(
    "gspread",
    types.SimpleNamespace(Client=object, authorize=lambda *_args, **_kwargs: None),
)
sys.modules.setdefault("streamlit", types.SimpleNamespace(secrets={}))

from bot.automation import (
    AddressValidationError,
    _build_failure_record,
    _build_m060800_item_payload,
    _build_result_record,
    _iter_content_items,
)
from bot.sheets import _filter_pending_orders_dataframe, backfill_results
import bot.sheets as sheets_module
from job_control import create_order_states, mark_results_completed
from pending_editor import build_pending_item_frame
from postal_ui_feedback import summarize_batch_results


class MockJapanPostGateway:
    """Minimal M060800/M061100 stand-in that records submitted item payloads."""

    def __init__(self, tracking: str = "LX000000001JP"):
        self.tracking = tracking
        self.item_payloads: list[dict[str, str]] = []

    def submit_m060800_item(self, payload) -> dict[str, object]:
        self.item_payloads.append(
            {
                "pkg": payload["itemBean.pkg"],
                "cost": payload["itemBean.cost.value"],
                "num": payload["itemBean.num.value"],
                "command": payload.get("command", "") or payload.get("method:itemAdd2", ""),
            }
        )
        return {"status_code": 200, "html": "<form id='mock-next'></form>"}

    def complete_print(self) -> dict[str, object]:
        return {"tracking": self.tracking, "pdf": b"%PDF-mock"}


class FakeTargetWorksheet:
    id = int(sheets_module.TARGET_GID)

    def __init__(self, readback: bool = True):
        self.readback = readback
        self.updated = []
        self.columns = {2: ["receiver"], 3: ["order"], 4: ["tracking"]}

    def col_values(self, column):
        return list(self.columns.get(column, []))

    def batch_update(self, batch, value_input_option=None):
        self.updated = batch
        self.value_input_option = value_input_option
        if not self.readback:
            return
        for update in batch:
            if not str(update.get("range", "")).startswith("B"):
                continue
            values = update["values"][0]
            self.columns[3] = ["order", values[1]]
            self.columns[4] = ["tracking", values[2]]


class FakeTargetSpreadsheet:
    def __init__(self, worksheet):
        self.worksheet = worksheet

    def worksheets(self):
        return [self.worksheet]


class FakeSheetsClient:
    def __init__(self, worksheet):
        self.worksheet = worksheet

    def open_by_key(self, _key):
        return FakeTargetSpreadsheet(self.worksheet)


class PostalMockE2ETests(unittest.TestCase):
    @staticmethod
    def _source_rows() -> pd.DataFrame:
        status_col = "製單上傳狀態(請用[未打單]檢視模式)"
        amount_col = "郵局申告金額(USD)"
        order_col = "注文番号(貼上原始資料)"
        check_col = "製單檢核"
        shipping_col = "郵局運送方式(複數商品請自行確認是否走小包)"
        formula_payload = {
            status_col: "未打單",
            amount_col: "3.14",
            order_col: "imy2038510",
            check_col: "",
            shipping_col: "ePacket",
            "Shipping Name": "Ying Chan",
            "內容物1": "Water Bottle TRSN9767",
            "內容物2": "Water Bottle TRSN9765",
            "內容物3": "Water Bottle TRSN9763",
            "申告金額1": "3.14",
            "申告金額2": "2.67",
            "申告金額3": "2.64",
            "數量1": "1",
            "數量2": "1",
            "數量3": "1",
        }
        return pd.DataFrame(
            [
                {**formula_payload, "_source_row_number": str(source_row)}
                for source_row in ("1969", "1970", "1971")
            ]
        )

    @staticmethod
    def _item_form_html() -> str:
        return """
        <form action="/mypage/M060800.do" method="post">
          <input type="hidden" name="command" value="">
          <input type="hidden" name="csrfToken" value="mock-token">
          <input type="hidden" name="shippingBean.sendType" value="8">
          <input type="hidden" name="shippingBean.transType" value="">
          <input type="hidden" name="shippingBean.pkgType" value="0">
          <input name="itemBean.pkg" value="">
          <input name="itemBean.cost.value" value="">
          <input name="itemBean.num.value" value="">
          <select name="itemBean.curUnit"><option value="USD">USD</option></select>
        </form>
        """

    def test_mock_array_formula_rows_reach_ui_payload_and_verified_target(self):
        pending = _filter_pending_orders_dataframe(self._source_rows(), completed_ids=set())

        self.assertEqual(len(pending), 1)
        row = pending.iloc[0]
        items = _iter_content_items(row)
        editor_items = build_pending_item_frame(row).to_dict("records")
        self.assertEqual(len(items), 3)
        self.assertEqual([item["Description"] for item in editor_items], [
            "Water Bottle TRSN9767",
            "Water Bottle TRSN9765",
            "Water Bottle TRSN9763",
        ])

        gateway = MockJapanPostGateway()
        for item in items:
            _, payload = _build_m060800_item_payload(
                self._item_form_html(),
                "https://mock-japanpost.local/mypage/M060800.do",
                row,
                is_eu=False,
                item_index=int(item["index"]),
                submit_command="itemAdd2",
            )
            gateway.submit_m060800_item(payload)

        self.assertEqual(len(gateway.item_payloads), 3)
        self.assertEqual(
            [payload["pkg"] for payload in gateway.item_payloads],
            ["Water Bottle TRSN9767", "Water Bottle TRSN9765", "Water Bottle TRSN9763"],
        )
        self.assertEqual([payload["num"] for payload in gateway.item_payloads], ["1", "1", "1"])

        print_result = gateway.complete_print()
        result = _build_result_record(row, "imy2038510", print_result["tracking"])
        self.assertEqual(result["items_expected"], 3)
        self.assertEqual(result["items_submitted"], 3)

        worksheet = FakeTargetWorksheet(readback=True)
        original_get_client = sheets_module._get_gspread_client
        sheets_module._get_gspread_client = lambda: FakeSheetsClient(worksheet)
        try:
            outcome = backfill_results([result])
        finally:
            sheets_module._get_gspread_client = original_get_client

        self.assertTrue(outcome["ok"])
        job = {"orders": create_order_states(pending, None)}
        mark_results_completed(job, [result])
        summary = summarize_batch_results([result])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(summary["completed_count"], 1)
        self.assertEqual(summary["failure_alerts"], [])
        self.assertEqual(job["orders"][0]["status"], "success")

    def test_mock_address_failure_does_not_swallow_other_success(self):
        pending = _filter_pending_orders_dataframe(self._source_rows(), completed_ids=set())
        success_row = pending.iloc[0]
        success = _build_result_record(success_row, "imy2038510", "LX000000001JP")
        failed = _build_failure_record(
            {"注文番号(貼上原始資料)": "imy2038490", "Shipping Name": "Receiver"},
            "imy2038490",
            AddressValidationError("地址過長：超過 mock 欄位容量", "address_too_long"),
        )

        worksheet = FakeTargetWorksheet(readback=True)
        original_get_client = sheets_module._get_gspread_client
        sheets_module._get_gspread_client = lambda: FakeSheetsClient(worksheet)
        try:
            outcome = backfill_results([success])
        finally:
            sheets_module._get_gspread_client = original_get_client

        self.assertTrue(outcome["ok"])
        mark_results_completed({"orders": create_order_states(pending, None)}, [success])
        summary = summarize_batch_results([success, failed])
        self.assertEqual(summary["completed_count"], 1)
        self.assertEqual(summary["failed_count"], 1)
        self.assertIn("imy2038490", summary["failure_alerts"][0])
        self.assertIn("地址過長", summary["failure_alerts"][0])

    def test_mock_target_readback_failure_is_not_reported_as_completed(self):
        pending = _filter_pending_orders_dataframe(self._source_rows(), completed_ids=set())
        result = _build_result_record(pending.iloc[0], "imy2038510", "LX000000001JP")
        worksheet = FakeTargetWorksheet(readback=False)
        original_get_client = sheets_module._get_gspread_client
        sheets_module._get_gspread_client = lambda: FakeSheetsClient(worksheet)
        try:
            outcome = backfill_results([result])
        finally:
            sheets_module._get_gspread_client = original_get_client

        self.assertFalse(outcome["ok"])
        self.assertIn("imy2038510", outcome["failed"])
        self.assertEqual(result["status"], "success")


if __name__ == "__main__":
    unittest.main()
