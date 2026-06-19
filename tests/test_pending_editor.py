import unittest

import pandas as pd

from pending_editor import (
    EDITABLE_PENDING_COLUMNS,
    PENDING_SUMMARY_COLUMNS,
    SHIPPING_OPTIONS,
    apply_pending_editor_values,
    apply_pending_order_editor_values,
    build_pending_editor_frame,
    build_pending_item_frame,
    build_pending_summary_frame,
    coerce_pending_editor_values,
    display_country,
    has_zero_value_items,
    sanitize_hscode,
)


class PendingEditorTests(unittest.TestCase):
    def test_build_pending_summary_frame_uses_requested_labels_and_calculates_usd_total(self):
        df = pd.DataFrame(
            [
                {
                    "注文番号(貼上原始資料)": "WhoWht-Test2",
                    "Shipping Name": "Chimwemwe Banda",
                    "收件人國家": "GERMANY（ドイツ）",
                    "郵局運送方式(複數商品請自行確認是否走小包)": "ePacket",
                    "申告金額1": "6.12",
                    "數量1": "1",
                    "申告金額2": "17.13",
                    "數量2": "1",
                    "訂單合計申告金額(JPY)": "3749",
                }
            ]
        )

        summary = build_pending_summary_frame(df)

        self.assertEqual(list(summary.columns), PENDING_SUMMARY_COLUMNS)
        self.assertEqual(summary.iloc[0]["Order No."], "WhoWht-Test2")
        self.assertEqual(summary.iloc[0]["Name"], "Chimwemwe Banda")
        self.assertEqual(summary.iloc[0]["Country"], "GERMANY")
        self.assertEqual(summary.iloc[0]["TransType"], "ePacket")
        self.assertEqual(summary.iloc[0]["TotalValue(USD)"], "23.25")
        self.assertEqual(summary.iloc[0]["TotalValue(JPY)"], "3749")

    def test_build_pending_item_frame_lists_content_hscode_value_and_quantity(self):
        row = pd.Series(
            {
                "內容物1": "Dietary Supplement",
                "申告金額1": "6.12",
                "數量1": "1",
                "內容物2": "Pillow",
                "申告金額2": "17.13",
                "數量2": "2",
            }
        )

        items = build_pending_item_frame(row, hs_codes={"1": "330499", "2": "940490"})

        self.assertEqual(list(items.columns), ["Content", "Description", "HSCode", "Value", "Quantity"])
        self.assertEqual(items.iloc[0].to_dict(), {
            "Content": "1",
            "Description": "Dietary Supplement",
            "HSCode": "330499",
            "Value": "6.12",
            "Quantity": "1",
        })
        self.assertEqual(items.iloc[1]["Content"], "2")
        self.assertEqual(items.iloc[1]["Description"], "Pillow")
        self.assertEqual(items.iloc[1]["HSCode"], "940490")

    def test_zero_value_items_are_detected(self):
        row = pd.Series(
            {
                "內容物1": "Dietary Supplement",
                "申告金額1": "0",
                "數量1": "1",
                "內容物2": "Pillow",
                "申告金額2": "2.50",
                "數量2": "1",
            }
        )

        self.assertEqual(has_zero_value_items(row), [1])

    def test_sanitize_hscode_keeps_digits_only(self):
        self.assertEqual(sanitize_hscode("9404.90"), "940490")
        self.assertEqual(sanitize_hscode("HS:940490"), "940490")
        self.assertEqual(sanitize_hscode("9404-90"), "940490")

    def test_apply_pending_order_editor_values_updates_original_fields_and_recalculates_totals(self):
        original = pd.DataFrame(
            [
                {
                    "注文番号(貼上原始資料)": "WhoWht-Test2",
                    "Shipping Name": "Chimwemwe Banda",
                    "郵局運送方式(複數商品請自行確認是否走小包)": "ePacket",
                    "郵局申告金額(USD)": "23.25",
                    "內容物1": "Dietary Supplement",
                    "申告金額1": "6.12",
                    "數量1": "1",
                    "內容物2": "Pillow",
                    "申告金額2": "17.13",
                    "數量2": "1",
                    "訂單合計申告金額(JPY)": "3749",
                }
            ]
        )
        summary = build_pending_summary_frame(original)
        summary.loc[0, "TransType"] = "EMS"
        summary.loc[0, "Name"] = "Edited Banda"
        items_by_position = {
            0: pd.DataFrame(
                [
                    {"Content": "1", "Description": "Dietary Supplement", "HSCode": "HS:3304.99", "Value": "7", "Quantity": "2"},
                    {"Content": "2", "Description": "Pillow", "HSCode": "9404-90", "Value": "3", "Quantity": "1"},
                ]
            )
        }

        applied = apply_pending_order_editor_values(
            original,
            summary,
            items_by_position,
            usd_jpy_rate=150,
        )

        self.assertEqual(applied.loc[0, "郵局運送方式(複數商品請自行確認是否走小包)"], "EMS")
        self.assertEqual(applied.loc[0, "Shipping Name"], "Edited Banda")
        self.assertEqual(applied.loc[0, "申告金額1"], "7")
        self.assertEqual(applied.loc[0, "數量1"], "2")
        self.assertEqual(applied.loc[0, "申告金額2"], "3")
        self.assertEqual(applied.loc[0, "HSCode1"], "330499")
        self.assertEqual(applied.loc[0, "HSCode2"], "940490")
        self.assertEqual(applied.loc[0, "郵局申告金額(USD)"], "17.00")
        self.assertEqual(applied.loc[0, "訂單合計申告金額(JPY)"], "2550")

    def test_apply_pending_order_editor_values_preserves_jpy_when_only_content_changes(self):
        original = pd.DataFrame(
            [
                {
                    "注文番号(貼上原始資料)": "WhoWht-Test2",
                    "郵局運送方式(複數商品請自行確認是否走小包)": "ePacket",
                    "郵局申告金額(USD)": "6.12",
                    "內容物1": "Old Name",
                    "申告金額1": "6.12",
                    "數量1": "1",
                    "訂單合計申告金額(JPY)": "999",
                }
            ]
        )
        summary = build_pending_summary_frame(original)
        items_by_position = {
            0: pd.DataFrame(
                [{"Content": "1", "Description": "New Name", "HSCode": "", "Value": "6.12", "Quantity": "1"}]
            )
        }

        applied = apply_pending_order_editor_values(original, summary, items_by_position, usd_jpy_rate=150)

        self.assertEqual(applied.loc[0, "內容物1"], "New Name")
        self.assertEqual(applied.loc[0, "訂單合計申告金額(JPY)"], "999")

    def test_display_country_uses_english_prefix_only(self):
        self.assertEqual(display_country("GERMANY（ドイツ）"), "GERMANY")
        self.assertEqual(display_country("AUSTRALIA（オーストラリア）"), "AUSTRALIA")
        self.assertEqual(display_country("Portugal"), "Portugal")

    def test_build_pending_editor_frame_includes_content_and_amount_columns(self):
        df = pd.DataFrame(
            [
                {
                    "注文番号(貼上原始資料)": "WhoWhy-Test7",
                    "Shipping Name": "Ines Budde",
                    "收件人國家": "GERMANY（ドイツ）",
                    "郵局運送方式(複數商品請自行確認是否走小包)": "國際小包",
                    "內容物1": "Facial Mask",
                    "申告金額1": "1.55",
                    "內容物2": "Pillow",
                    "申告金額2": "1.55",
                    "訂單合計申告金額(JPY)": "500",
                }
            ]
        )

        editor = build_pending_editor_frame(df)

        self.assertEqual(editor.iloc[0]["內容物1"], "Facial Mask")
        self.assertEqual(editor.iloc[0]["申告金額2"], "1.55")
        self.assertIn("訂單合計申告金額(JPY)", editor.columns)
        self.assertIn("HSCode", editor.columns)
        self.assertEqual(editor.iloc[0]["HSCode"], "")

    def test_build_pending_editor_frame_preserves_known_column_order(self):
        df = pd.DataFrame([{column: "" for column in EDITABLE_PENDING_COLUMNS if column != "HSCode"}])

        editor = build_pending_editor_frame(df)

        self.assertEqual(list(editor.columns), EDITABLE_PENDING_COLUMNS)

    def test_coerce_pending_editor_values_normalizes_shipping_choices(self):
        edited = pd.DataFrame(
            [
                {
                    "郵局運送方式(複數商品請自行確認是否走小包)": "EMS",
                    "郵局申告金額(USD)": "1.55",
                    "申告金額1": "1.55",
                    "訂單合計申告金額(JPY)": "500",
                },
                {
                    "郵局運送方式(複數商品請自行確認是否走小包)": "ePacket",
                    "郵局申告金額(USD)": "2",
                    "申告金額1": "2",
                    "訂單合計申告金額(JPY)": "320",
                },
            ]
        )

        coerced = coerce_pending_editor_values(edited)

        self.assertEqual(
            list(coerced["郵局運送方式(複數商品請自行確認是否走小包)"]),
            ["EMS", "ePacket"],
        )
        self.assertIn("國際小包", SHIPPING_OPTIONS)

    def test_apply_pending_editor_values_updates_original_rows_without_dropping_columns(self):
        original = pd.DataFrame(
            [
                {
                    "注文番号(貼上原始資料)": "WhoWhy-Test7",
                    "Shipping Name": "Ines Budde",
                    "Address": "Keep me",
                    "郵局運送方式(複數商品請自行確認是否走小包)": "國際小包",
                    "內容物1": "Old",
                    "申告金額1": "1",
                }
            ]
        )
        edited = build_pending_editor_frame(original)
        edited.loc[0, "郵局運送方式(複數商品請自行確認是否走小包)"] = "EMS"
        edited.loc[0, "內容物1"] = "Pillow"
        edited.loc[0, "申告金額1"] = "2.55"

        applied = apply_pending_editor_values(original, edited)

        self.assertEqual(applied.loc[0, "郵局運送方式(複數商品請自行確認是否走小包)"], "EMS")
        self.assertEqual(applied.loc[0, "內容物1"], "Pillow")
        self.assertEqual(applied.loc[0, "申告金額1"], "2.55")
        self.assertEqual(applied.loc[0, "Address"], "Keep me")


if __name__ == "__main__":
    unittest.main()
