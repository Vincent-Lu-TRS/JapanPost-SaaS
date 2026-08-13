import unittest

import pandas as pd

from pending_editor import (
    EDITABLE_PENDING_COLUMNS,
    PENDING_SUMMARY_COLUMNS,
    SHIPMENT_ROLE_COLUMN,
    SHIPPING_COL,
    SHIPPING_OPTIONS,
    apply_pending_editor_values,
    apply_pending_order_editor_values,
    build_pending_editor_frame,
    build_pending_item_frame,
    build_pending_summary_frame,
    calculate_total_value_usd,
    coerce_pending_editor_values,
    compose_shipping_name,
    country_kind,
    display_country,
    expand_pending_orders_for_trans_types,
    has_zero_value_items,
    parse_shipping_name,
    pending_order_warning_lines,
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

    def test_legacy_single_item_uses_top_level_postal_fields(self):
        row = pd.Series(
            {
                "郵局內容物": "Pillow TRSN3392",
                "郵局申告金額(USD)": "10.98",
                "数量": "1",
                "內容物1": "",
                "申告金額1": "",
                "數量1": "",
                "訂單合計申告金額(JPY)": "0",
            }
        )

        items = build_pending_item_frame(row)
        summary = build_pending_summary_frame(pd.DataFrame([row]))

        self.assertEqual(
            items.iloc[0].to_dict(),
            {
                "Content": "1",
                "Description": "Pillow TRSN3392",
                "HSCode": "",
                "Value": "10.98",
                "Quantity": "1",
            },
        )
        self.assertEqual(summary.iloc[0]["TotalValue(USD)"], "10.98")

    def test_applying_untouched_legacy_single_item_preserves_postal_amount(self):
        original = pd.DataFrame(
            [
                {
                    "注文番号(貼上原始資料)": "imy2038370",
                    "郵局內容物": "Pillow TRSN3392",
                    "郵局申告金額(USD)": "10.98",
                    "数量": "1",
                    "內容物1": "",
                    "申告金額1": "",
                    "數量1": "",
                }
            ]
        )
        summary = build_pending_summary_frame(original)
        items = build_pending_item_frame(original.iloc[0])

        applied = apply_pending_order_editor_values(original, summary, {0: items})

        self.assertEqual(applied.loc[0, "郵局申告金額(USD)"], "10.98")
        self.assertEqual(applied.loc[0, "申告金額1"], "10.98")
        self.assertEqual(applied.loc[0, "數量1"], "1")

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

    def test_total_excludes_blank_zero_and_negative_quantity_items(self):
        row = pd.Series(
            {
                "內容物1": "Face Primer",
                "申告金額1": "4.85",
                "數量1": "0",
                "內容物2": "Canceled Blank Item",
                "申告金額2": "2.00",
                "數量2": "",
                "內容物3": "Canceled Negative Item",
                "申告金額3": "3.00",
                "數量3": "-1",
                "內容物4": "Beads",
                "申告金額4": "1.31",
                "數量4": "4",
            }
        )

        self.assertEqual(calculate_total_value_usd(row), 5.24)

    def test_total_rejects_invalid_quantity(self):
        for quantity in ("abc", "1.5"):
            with self.subTest(quantity=quantity):
                row = pd.Series({"內容物1": "Item", "申告金額1": "1", "數量1": quantity})
                with self.assertRaisesRegex(ValueError, "內容物1.*數量格式錯誤"):
                    calculate_total_value_usd(row)

    def test_pending_order_warning_lines_explain_blank_quantity_and_mixed_shipping(self):
        row = pd.Series(
            {
                "注文番号(貼上原始資料)": "imy2038510",
                "內容物1": "Water Bottle TRSN9767",
                "申告金額1": "3.14",
                "數量1": "",
                "_pending_warnings": "mixed_shipping_method",
            }
        )

        warnings = pending_order_warning_lines(row)

        self.assertTrue(any("數量1" in warning and "空白" in warning for warning in warnings))
        self.assertTrue(any("運送方式不一致" in warning for warning in warnings))

    def test_sanitize_hscode_keeps_digits_only(self):
        self.assertEqual(sanitize_hscode("9404.90"), "940490")
        self.assertEqual(sanitize_hscode("HS:940490"), "940490")
        self.assertEqual(sanitize_hscode("9404-90"), "940490")

    def test_parse_shipping_name_splits_prc_id_and_pccc(self):
        self.assertEqual(
            parse_shipping_name("zhuxiaomu (PRC ID:110108198309121213)"),
            {"clean_name": "zhuxiaomu", "prc_id": "110108198309121213", "pccc": ""},
        )
        self.assertEqual(
            parse_shipping_name("Eunseo Ha (PCCC：P180026936191)"),
            {"clean_name": "Eunseo Ha", "prc_id": "", "pccc": "P180026936191"},
        )
        self.assertEqual(
            parse_shipping_name("Eunseo Ha (PCCC:P18026936191)"),
            {"clean_name": "Eunseo Ha", "prc_id": "", "pccc": "P18026936191"},
        )

    def test_compose_shipping_name_restores_country_specific_identifier(self):
        self.assertEqual(country_kind("CHINA（中國）"), "china")
        self.assertEqual(country_kind("KOREA（韓國）"), "korea")
        self.assertEqual(
            compose_shipping_name("zhuxiaomu", "CHINA", prc_id="110108198309121213"),
            "zhuxiaomu (PRC ID:110108198309121213)",
        )
        self.assertEqual(
            compose_shipping_name("Eunseo Ha", "KOREA", pccc="P180026936191"),
            "Eunseo Ha (PCCC:P180026936191)",
        )
        self.assertEqual(compose_shipping_name("Fabian Kohlhaas", "GERMANY", prc_id="x"), "Fabian Kohlhaas")

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


    def test_expand_pending_orders_for_trans_types_duplicates_order_with_extra_shipping_types(self):
        original = pd.DataFrame(
            [
                {
                    "瘜冽??芸(鞎潔???鞈?)": "WhoWht-Test1",
                    "Shipping Name": "Fabian Kohlhaas",
                    SHIPPING_COL: "EMS",
                }
            ],
            index=[10],
        )

        expanded = expand_pending_orders_for_trans_types(original, {10: ["ePacket", "EMS", "國際小包"]})

        self.assertEqual(len(expanded), 3)
        self.assertEqual(
            list(expanded[SHIPPING_COL]),
            ["EMS", "ePacket", "國際小包"],
        )
        self.assertEqual(
            list(expanded[SHIPMENT_ROLE_COLUMN]),
            ["primary", "additional", "additional"],
        )
        self.assertEqual(list(expanded["瘜冽??芸(鞎潔???鞈?)"]), ["WhoWht-Test1"] * 3)

    def test_expand_pending_orders_for_trans_types_retains_role_for_empty_frame(self):
        expanded = expand_pending_orders_for_trans_types(
            pd.DataFrame(columns=[SHIPPING_COL]),
            {},
        )

        self.assertIn(SHIPMENT_ROLE_COLUMN, expanded.columns)
        self.assertTrue(expanded.empty)

    def test_expand_pending_orders_resets_existing_additional_source_to_primary(self):
        original = pd.DataFrame(
            [
                {
                    SHIPPING_COL: "EMS",
                    SHIPMENT_ROLE_COLUMN: "additional",
                }
            ],
            index=[10],
        )

        expanded = expand_pending_orders_for_trans_types(original, {10: ["ePacket"]})

        self.assertEqual(
            list(expanded[SHIPMENT_ROLE_COLUMN]),
            ["primary", "additional"],
        )

    def test_expand_pending_orders_rejects_invalid_shipment_role(self):
        original = pd.DataFrame(
            [{SHIPPING_COL: "EMS", SHIPMENT_ROLE_COLUMN: "unexpected"}],
            index=[10],
        )

        with self.assertRaisesRegex(ValueError, "invalid shipment role"):
            expand_pending_orders_for_trans_types(original, {10: ["ePacket"]})


if __name__ == "__main__":
    unittest.main()
