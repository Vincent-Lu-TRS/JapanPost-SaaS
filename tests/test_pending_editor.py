import unittest

import pandas as pd

from pending_editor import (
    EDITABLE_PENDING_COLUMNS,
    SHIPPING_OPTIONS,
    apply_pending_editor_values,
    build_pending_editor_frame,
    coerce_pending_editor_values,
)


class PendingEditorTests(unittest.TestCase):
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
