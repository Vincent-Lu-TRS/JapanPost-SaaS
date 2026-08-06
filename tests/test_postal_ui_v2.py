import unittest

import pandas as pd

from pending_editor import (
    apply_pending_order_editor_values,
    build_pending_item_frame,
    build_pending_summary_frame,
    expand_pending_orders_for_trans_types,
)

from postal_ui_v2 import (
    V2_FIELD_CONTRACT,
    apply_batch_selection,
    build_v2_item_display_frame,
    format_secondary_rate_badge,
    restore_v2_item_frame,
    v2_field_contract,
)


class PostalUiV2HelperTests(unittest.TestCase):
    def test_select_all_marks_only_current_pending_order_ids(self):
        original = {"A": False, "B": True, "OLD": True}

        updated = apply_batch_selection(original, ["A", "B"], "select_all")

        self.assertEqual(updated, {"A": True, "B": True, "OLD": True})
        self.assertEqual(original, {"A": False, "B": True, "OLD": True})
        self.assertIsNot(updated, original)

    def test_clear_all_marks_only_current_pending_order_ids(self):
        original = {"A": True, "B": True, "OLD": False}

        updated = apply_batch_selection(original, ["A", "B"], "clear_all")

        self.assertEqual(updated, {"A": False, "B": False, "OLD": False})
        self.assertEqual(original, {"A": True, "B": True, "OLD": False})
        self.assertIsNot(updated, original)

    def test_unknown_batch_action_is_rejected(self):
        with self.assertRaises(KeyError):
            apply_batch_selection({"A": True}, ["A"], "toggle")

    def test_secondary_rate_badge_is_single_line_and_observable(self):
        self.assertEqual(
            format_secondary_rate_badge(157.79, "2026-08-06"),
            "USD/JPY 157.79 · 26/08/06",
        )
        self.assertNotIn("\n", format_secondary_rate_badge(None, ""))

    def test_v2_field_contract_preserves_current_editability(self):
        self.assertEqual(
            v2_field_contract(),
            {
                "editable": [
                    "Name",
                    "TransType",
                    "追加",
                    "PRC ID/PCCC",
                    "Description",
                    "HSCode",
                    "Value",
                    "Quantity",
                ],
                "display_only": ["製單", "Order No.", "Country", "No."],
                "system_calculated": ["TotalValue(USD)", "TotalValue(JPY)"],
            },
        )
        self.assertEqual(V2_FIELD_CONTRACT, v2_field_contract())
        self.assertNotIn("內容品名（僅顯示）", str(v2_field_contract()))

    def test_v2_item_adapter_hides_content_column_but_restores_internal_index(self):
        source = pd.DataFrame(
            [
                {
                    "Content": "1",
                    "Description": "Bottle",
                    "HSCode": "392490",
                    "Value": "3.14",
                    "Quantity": "1",
                }
            ]
        )

        display = build_v2_item_display_frame(source)
        self.assertEqual(
            list(display.columns),
            ["No.", "Description", "HSCode", "Value", "Quantity"],
        )
        self.assertNotIn("Content", display.columns)

        edited = display.copy()
        edited.loc[0, "Description"] = "Edited bottle"
        restored = restore_v2_item_frame(edited)
        self.assertEqual(list(restored.columns), list(source.columns))
        self.assertEqual(restored.loc[0, "Content"], "1")
        self.assertEqual(restored.loc[0, "Description"], "Edited bottle")

    def test_v2_item_adapter_round_trip_feeds_existing_submission_pipeline(self):
        original = pd.DataFrame(
            [
                {
                    "注文番号(貼上原始資料)": "mock-v2-8490",
                    "Shipping Name": "Teerapan Kaewkong",
                    "收件人國家": "THAILAND",
                    "郵局運送方式(複數商品請自行確認是否走小包)": "ePacket",
                    "郵局申告金額(USD)": "8.00",
                    "內容物1": "Water Bottle TRSN3392",
                    "申告金額1": "3.00",
                    "數量1": "1",
                    "HSCode1": "392490",
                    "內容物2": "Water Bottle TRSN6195",
                    "申告金額2": "5.00",
                    "數量2": "1",
                    "HSCode2": "392490",
                    "訂單合計申告金額(JPY)": "1200",
                }
            ]
        )

        source_items = build_pending_item_frame(original.iloc[0])
        display_items = build_v2_item_display_frame(source_items)
        display_items.loc[0, "Description"] = "Water Bottle TRSN3392 edited"
        display_items.loc[0, "Value"] = "4.00"
        display_items.loc[1, "Description"] = "Water Bottle TRSN6195 edited"
        display_items.loc[1, "Quantity"] = "2"
        restored_items = restore_v2_item_frame(display_items)

        summary = build_pending_summary_frame(original)
        summary.loc[0, "Name"] = "Teerapan Kaewkong edited"
        summary.loc[0, "TransType"] = "EMS"
        applied = apply_pending_order_editor_values(
            original,
            summary,
            {0: restored_items},
            usd_jpy_rate=150,
        )

        self.assertEqual(applied.loc[0, "Shipping Name"], "Teerapan Kaewkong edited")
        self.assertEqual(
            applied.loc[0, "郵局運送方式(複數商品請自行確認是否走小包)"],
            "EMS",
        )
        self.assertEqual(applied.loc[0, "內容物1"], "Water Bottle TRSN3392 edited")
        self.assertEqual(applied.loc[0, "申告金額1"], "4.00")
        self.assertEqual(applied.loc[0, "內容物2"], "Water Bottle TRSN6195 edited")
        self.assertEqual(applied.loc[0, "數量2"], "2")
        self.assertEqual(applied.loc[0, "郵局申告金額(USD)"], "14.00")
        self.assertEqual(applied.loc[0, "訂單合計申告金額(JPY)"], "2100")
        self.assertEqual(original.loc[0, "內容物1"], "Water Bottle TRSN3392")
        self.assertEqual(original.loc[0, "數量2"], "1")

    def test_v2_round_trip_preserves_conditional_id_and_extra_transport(self):
        original = pd.DataFrame(
            [
                {
                    "注文番号(貼上原始資料)": "mock-v2-china",
                    "Shipping Name": "zhuxiaomu (PRC ID:110108198309121213)",
                    "收件人國家": "CHINA",
                    "郵局運送方式(複數商品請自行確認是否走小包)": "ePacket",
                    "內容物1": "Water Bottle TRSN3392",
                    "申告金額1": "4.00",
                    "數量1": "1",
                    "HSCode1": "392490",
                    "訂單合計申告金額(JPY)": "600",
                }
            ]
        )
        display_items = build_v2_item_display_frame(build_pending_item_frame(original.iloc[0]))
        restored_items = restore_v2_item_frame(display_items)
        summary = build_pending_summary_frame(original)
        summary.loc[0, "Name"] = "zhuxiaomu (PRC ID:110108198309121213)"
        applied = apply_pending_order_editor_values(
            original,
            summary,
            {0: restored_items},
            usd_jpy_rate=150,
        )

        expanded = expand_pending_orders_for_trans_types(applied, {0: ["EMS"]})

        self.assertEqual(
            list(expanded["郵局運送方式(複數商品請自行確認是否走小包)"].astype(str)),
            ["ePacket", "EMS"],
        )
        self.assertEqual(
            list(expanded["Shipping Name"].astype(str)),
            [
                "zhuxiaomu (PRC ID:110108198309121213)",
                "zhuxiaomu (PRC ID:110108198309121213)",
            ],
        )


if __name__ == "__main__":
    unittest.main()
