import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.modules.setdefault("streamlit", types.SimpleNamespace(secrets={}, session_state={}))
sys.modules.setdefault("google", types.SimpleNamespace())
sys.modules.setdefault("google.oauth2", types.SimpleNamespace())
sys.modules.setdefault("google.oauth2.service_account", types.SimpleNamespace(Credentials=types.SimpleNamespace()))
sys.modules.setdefault("googleapiclient", types.SimpleNamespace())
sys.modules.setdefault("googleapiclient.discovery", types.SimpleNamespace(build=lambda *a, **k: None))
sys.modules.setdefault("googleapiclient.http", types.SimpleNamespace(MediaIoBaseUpload=object))
sys.modules.setdefault("gspread", types.SimpleNamespace(authorize=lambda *a, **k: None, Client=object))

from bot.picking_labels import (
    PickingItem,
    PickingOrder,
    build_picking_source_diagnostics,
    build_shipping_deadline_lookup,
    build_picking_pdf_pages,
    build_picking_label_summary,
    format_shipping_deadline,
    generate_picking_labels_transaction,
    estimate_total_pages,
    normalize_done_state,
    parse_picking_label_candidates,
    resolve_picking_done_row_numbers,
)
from bot.picking_pdf import (
    can_fit_items_on_page,
    get_registered_cjk_font_info,
    plan_logistics_header_text,
    plan_item_text_layout,
    plan_page_grid,
    plan_source_header_text,
    render_picking_labels_pdf,
)
sys.modules.pop("bot.drive", None)
from bot.drive import choose_safe_picking_filename, next_sequence_filename
from bot.sheets import build_picking_done_updates


def _header(max_items=2):
    row = [
        "注文番号",
        "訂單狀態",
        "製單後勾選",
        "注文日",
        "訂單來源",
        "國際物流方式",
    ]
    for idx in range(1, max_items + 1):
        row.extend([f"商品SKU{idx}", f"商品名{idx}", f"JAN-{idx}", f"數量{idx}", f"入荷進捗{idx}"])
    return row


def _row(order_no="imy1", status="可出貨", done="", max_items=2, items=None):
    values = [
        order_no,
        status,
        done,
        "6/27/2026",
        "Official website - imy Shop",
        "郵便局",
    ]
    items = items if items is not None else [("TRSN1", "商品一", "JAN1", "2", "本日着予定")]
    for idx in range(max_items):
        if idx < len(items):
            values.extend(items[idx])
        else:
            values.extend(["", "", "", "", ""])
    return values


class PickingLabelParsingTests(unittest.TestCase):
    def test_done_state_normalizes_custom_checkbox_values(self):
        cases = [
            ("未製單", "NOT_DONE"),
            (False, "NOT_DONE"),
            ("FALSE", "NOT_DONE"),
            ("false", "NOT_DONE"),
            ("", "NOT_DONE"),
            (None, "NOT_DONE"),
            ("已製單", "DONE"),
            (True, "DONE"),
            ("TRUE", "DONE"),
            ("true", "DONE"),
        ]

        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_done_state(raw), expected)

    def test_parse_includes_custom_unchecked_and_excludes_custom_checked_rows(self):
        values = [
            _header(),
            _row(order_no="custom-unchecked", done="未製單"),
            _row(order_no="custom-checked", done="已製單"),
        ]

        orders, _warnings = parse_picking_label_candidates(values)

        self.assertEqual([order.order_no for order in orders], ["custom-unchecked"])

    def test_parse_includes_false_and_blank_done_rows(self):
        values = [
            _header(),
            _row(order_no="blank", done=""),
            _row(order_no="false-string", done="FALSE"),
            _row(order_no="false-bool", done=False),
        ]

        orders, warnings = parse_picking_label_candidates(values)

        self.assertEqual([order.order_no for order in orders], ["blank", "false-string", "false-bool"])
        self.assertEqual(warnings, ["目前來源表只提供至商品SKU2，因此 App 目前只能讀到 2 個商品。若要完整支援 10 個以上商品，請先擴充來源表欄位或改接 normalized 訂單商品資料來源。"])

    def test_parse_excludes_non_shippable_or_already_done_rows(self):
        values = [
            _header(),
            _row(order_no="ok", status="可出貨", done="FALSE"),
            _row(order_no="waiting", status="缺貨", done="FALSE"),
            _row(order_no="done", status="可出貨", done="TRUE"),
        ]

        orders, _warnings = parse_picking_label_candidates(values)

        self.assertEqual([order.order_no for order in orders], ["ok"])

    def test_parse_dynamic_item_columns_up_to_ten(self):
        items = [
            (f"SKU{idx}", f"商品 {idx}", f"JAN{idx}", str(idx), "")
            for idx in range(1, 11)
        ]
        values = [_header(max_items=10), _row(max_items=10, items=items)]

        orders, warnings = parse_picking_label_candidates(values)

        self.assertEqual(len(orders), 1)
        self.assertEqual(len(orders[0].items), 10)
        self.assertEqual(orders[0].items[-1].sku, "SKU10")
        self.assertEqual(warnings, [])

    def test_normalized_headers_detect_wrapped_ten_item_groups(self):
        header = [
            "注文番号",
            "訂單狀態",
            "製單後勾選",
            "注文日",
            "訂單來源",
            "國際物流方式",
        ]
        for idx in range(1, 11):
            header.extend([
                f"商品\nSKU {idx}",
                f"商品 名　{idx}",
                f"JAN － {idx}",
                f"數量 {idx}",
                f"入荷 進捗 {idx}",
            ])
        row = _row(
            order_no="imy10",
            done="FALSE",
            max_items=10,
            items=[("SKU10", "商品十", "JAN10", "1", "本日着予定")],
        )

        orders, warnings = parse_picking_label_candidates([header, row])
        diagnostics = build_picking_source_diagnostics([header, row], orders, warnings)

        self.assertEqual(len(orders[0].items), 1)
        self.assertEqual(diagnostics["max_item_group"], 10)
        self.assertEqual(diagnostics["missing_item_headers"], [])
        self.assertEqual(warnings, [])

    def test_diagnostics_reports_exclusion_reasons_and_l_value_sample(self):
        values = [
            _header(max_items=1),
            _row(order_no="ok", status="可出貨", done="未製單", max_items=1),
            _row(order_no="done", status="可出貨", done="已製單", max_items=1),
            _row(order_no="status", status="缺貨", done="未製單", max_items=1),
            _row(order_no="", status="可出貨", done="未製單", max_items=1),
            _row(order_no="no-items", status="可出貨", done="未製單", max_items=1, items=[]),
        ]

        orders, warnings = parse_picking_label_candidates(values)
        diagnostics = build_picking_source_diagnostics(values, orders, warnings)

        self.assertEqual(diagnostics["total_source_rows"], 5)
        self.assertEqual(diagnostics["candidate_order_count"], 1)
        self.assertEqual(diagnostics["excluded_because_done"], 1)
        self.assertEqual(diagnostics["excluded_because_status"], 1)
        self.assertEqual(diagnostics["excluded_because_order_no_missing"], 1)
        self.assertEqual(diagnostics["excluded_because_item_data_missing"], 1)
        self.assertIn("未製單", diagnostics["done_raw_values_sample"])
        self.assertIn("已製單", diagnostics["done_raw_values_sample"])
        self.assertEqual(diagnostics["near_candidate_exclusions"][0]["normalized_l_state"], "DONE")

    def test_parse_falls_back_to_k_l_columns_when_headers_are_missing(self):
        header = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "", "", "注文日", "訂單來源", "注文番号", "國際物流方式", "商品SKU1", "商品名1", "JAN-1", "數量1", "入荷進捗1"]
        row = [""] * len(header)
        row[10] = "可出貨"
        row[11] = ""
        row[12:21] = ["2026/06/27", "imy Shop", "fallback-1", "郵便局", "SKU1", "商品一", "", "1", ""]

        orders, _warnings = parse_picking_label_candidates([header, row])

        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0].source_row_number, 2)
        self.assertEqual(orders[0].order_no, "fallback-1")

    def test_parse_prefers_qr_specific_field_when_present(self):
        header = _header(max_items=1) + ["QR內容"]
        row = _row(order_no="imy2035810", max_items=1) + ["QR-SPECIFIC-2035810"]

        orders, _warnings = parse_picking_label_candidates([header, row])

        self.assertEqual(orders[0].qr_content, "QR-SPECIFIC-2035810")

    def test_parse_defaults_qr_content_to_order_number(self):
        orders, _warnings = parse_picking_label_candidates([_header(max_items=1), _row(order_no="imy2035810", max_items=1)])

        self.assertEqual(orders[0].qr_content, "imy2035810")

    def test_parse_preserves_sku_exactly_without_suffix(self):
        orders, _warnings = parse_picking_label_candidates([
            _header(max_items=1),
            _row(
                order_no="imy2035810",
                max_items=1,
                items=[(" TRSN8688 ", "商品", "4901234567890", "1", "")],
            ),
        ])

        self.assertEqual(orders[0].items[0].sku, "TRSN8688")

    def test_parse_applies_shipping_deadline_lookup(self):
        orders, _warnings = parse_picking_label_candidates(
            [_header(max_items=1), _row(order_no="imy2035810", max_items=1)],
            shipping_deadlines={"imy2035810": "2026/07/03"},
        )

        self.assertEqual(orders[0].shipping_deadline, "2026/07/03")


class PickingLabelPaginationTests(unittest.TestCase):
    def test_build_pages_splits_after_ten_items(self):
        items = [PickingItem(f"SKU{idx}", f"商品 {idx}", "", "1", "") for idx in range(1, 12)]
        order = PickingOrder(2, "2026/06/27", "imy Shop", "imy1", "郵便局", items)

        pages = build_picking_pdf_pages([order])

        self.assertEqual(len(pages), 2)
        self.assertEqual([len(page.items) for page in pages], [10, 1])
        self.assertEqual((pages[0].page_index, pages[0].total_pages), (1, 2))
        self.assertEqual((pages[1].page_index, pages[1].total_pages), (2, 2))

    def test_fixed_ten_row_grid_for_1_2_5_10_and_11_items(self):
        for count, expected_pages, expected_filled in [
            (1, 1, [1]),
            (2, 1, [2]),
            (5, 1, [5]),
            (10, 1, [10]),
            (11, 2, [10, 1]),
        ]:
            with self.subTest(count=count):
                items = [PickingItem(f"SKU{idx}", f"商品 {idx}", "", "1", "") for idx in range(1, count + 1)]
                pages = build_picking_pdf_pages([PickingOrder(2, "2026/06/27", "imy Shop", "imy1", "郵便局", items)])

                self.assertEqual(len(pages), expected_pages)
                self.assertEqual([len(page.items) for page in pages], expected_filled)
                for page in pages:
                    grid = plan_page_grid(page.items)
                    self.assertEqual(grid["row_count"], 10)
                    self.assertEqual(grid["blank_rows"], 10 - len(page.items))

    def test_build_pages_keeps_ten_realistic_long_items_when_dry_layout_fits(self):
        names = [
            "THERMOS 膳魔師 - 真空保温調理器シャトルシェフ 4.5L ヴィンテージセピア KBG-4500 CBW",
            "Nishikawa 西川 - 睡眠博士 2023 年モデル 寝返りアシスト 枕 低め EH93009549L",
            "APA HOTELS&RESORTS - ADJUSTFIT (アジャストフィット) APA オリジナル 4way まくら",
            "ZOJIRUSHI 象印 - ステンレスマグ シームレスせん SM-ZB48 スレートブラック 480ml",
            "HITACHI 日立 - 衣類スチーマー CSI-RX70 アイボリー 連続スチームモデル",
            "Panasonic パナソニック - ナノケア ヘアドライヤー EH-NA0J ディープネイビー",
            "TIGER タイガー - 土鍋圧力IHジャー炊飯器 JRX-T100 コスモブラック",
            "MUJI 無印良品 - 体にフィットするソファ カバーセット チャコールグレー",
            "IRIS OHYAMA アイリスオーヤマ - サーキュレーターアイ DC JET PCF-SDCC15T",
            "YAMAZEN 山善 - 電気圧力鍋 4.0L マイコン式 ブラック YPCA-M400",
        ]
        items = [
            PickingItem(f"TRSN{8688 + idx}", name, f"49012345678{idx:02d}"[-13:], "1", "本日着予定")
            for idx, name in enumerate(names)
        ]
        order = PickingOrder(2, "2026/06/27", "imy Shop", "imy1", "郵便局", items)

        pages = build_picking_pdf_pages([order])

        self.assertEqual([len(page.items) for page in pages], [10])

    def test_estimate_total_pages_handles_twenty_one_items(self):
        items = [PickingItem(f"SKU{idx}", f"商品 {idx}", "", "1", "") for idx in range(1, 22)]
        order = PickingOrder(2, "2026/06/27", "imy Shop", "imy1", "郵便局", items)

        self.assertEqual(estimate_total_pages([order]), 3)


class PickingLabelDriveTests(unittest.TestCase):
    def test_next_sequence_filename_uses_max_existing_daily_sequence(self):
        filename = next_sequence_filename(
            [
                {"name": "260627-1揀貨標籤.pdf"},
                {"name": "260627-7揀貨標籤.pdf"},
                {"name": "260626-9揀貨標籤.pdf"},
                {"name": "260627-note.pdf"},
            ],
            today="2026-06-27",
        )

        self.assertEqual(filename, "260627-8揀貨標籤.pdf")

    def test_choose_safe_filename_uses_timestamp_when_candidate_exists_after_recheck(self):
        filename = choose_safe_picking_filename(
            initial_files=[{"name": "260627-1揀貨標籤.pdf"}],
            rechecked_files=[{"name": "260627-2揀貨標籤.pdf"}],
            today="2026-06-27",
            timestamp="145901",
        )

        self.assertEqual(filename, "260627-145901揀貨標籤.pdf")


class PickingLabelTransactionTests(unittest.TestCase):
    def test_transaction_does_not_mark_rows_when_drive_upload_fails(self):
        calls = {"mark": []}
        order = PickingOrder(22, "6/27/2026", "imy Shop", "imy1", "郵便局", [PickingItem("SKU", "商品", "", "1", "")])

        result = generate_picking_labels_transaction(
            orders=[order],
            output_dir=str(ROOT / "tmp"),
            list_files=lambda prefix: [],
            upload_file=lambda path: (_ for _ in ()).throw(RuntimeError("upload failed")),
            mark_done=lambda rows: calls["mark"].append(rows),
            now="2026-06-27 14:59:01",
        )

        self.assertFalse(result.success)
        self.assertEqual(calls["mark"], [])

    def test_transaction_marks_only_uploaded_order_source_rows(self):
        calls = {"mark": []}
        orders = [
            PickingOrder(22, "6/27/2026", "imy Shop", "imy1", "郵便局", [PickingItem("SKU1", "商品", "", "1", "")]),
            PickingOrder(35, "6/27/2026", "imy Shop", "imy2", "郵便局", [PickingItem("SKU2", "商品", "", "1", "")]),
        ]

        result = generate_picking_labels_transaction(
            orders=orders,
            output_dir=str(ROOT / "tmp"),
            list_files=lambda prefix: [],
            upload_file=lambda path: {"id": "file-id", "name": Path(path).name},
            mark_done=lambda rows: calls["mark"].append(rows),
            now="2026-06-27 14:59:01",
        )

        self.assertTrue(result.success)
        self.assertEqual(calls["mark"], [[22, 35]])

    def test_transaction_reports_uploaded_file_when_l_column_writeback_fails(self):
        calls = {"mark": []}
        order = PickingOrder(22, "6/27/2026", "imy Shop", "imy1", "郵便局", [PickingItem("SKU", "商品", "", "1", "")])

        result = generate_picking_labels_transaction(
            orders=[order],
            output_dir=str(ROOT / "tmp"),
            list_files=lambda prefix: [],
            upload_file=lambda path: {"id": "file-id", "name": Path(path).name, "webViewLink": "https://drive.test/file-id"},
            mark_done=lambda rows: calls["mark"].append(rows) or (_ for _ in ()).throw(RuntimeError("writeback failed")),
            now="2026-06-27 14:59:01",
        )

        self.assertFalse(result.success)
        self.assertEqual(calls["mark"], [[22]])
        self.assertEqual(result.marked_rows, [])
        self.assertEqual(result.drive_file["id"], "file-id")
        self.assertIn("writeback failed", result.error)

    def test_resolve_done_rows_revalidates_source_row_and_falls_back_to_order_number(self):
        values = [
            _header(max_items=1),
            _row(order_no="wrong-order", max_items=1),
            _row(order_no="imy-selected", done="FALSE", max_items=1),
        ]
        order = PickingOrder(2, "6/27/2026", "imy Shop", "imy-selected", "郵便局", [PickingItem("SKU", "商品", "", "1", "")])

        self.assertEqual(resolve_picking_done_row_numbers(values, [order]), [3])

    def test_done_writeback_updates_use_custom_checked_value(self):
        self.assertEqual(
            build_picking_done_updates([22, 35]),
            [
                {"range": "L22:L22", "values": [["已製單"]]},
                {"range": "L35:L35", "values": [["已製單"]]},
            ],
        )

    def test_transaction_uses_mark_done_returned_rows_when_writeback_resolves_fallback(self):
        order = PickingOrder(22, "6/27/2026", "imy Shop", "imy1", "郵便局", [PickingItem("SKU", "商品", "", "1", "")])

        result = generate_picking_labels_transaction(
            orders=[order],
            output_dir=str(ROOT / "tmp"),
            list_files=lambda prefix: [],
            upload_file=lambda path: {"id": "file-id", "name": Path(path).name},
            mark_done=lambda rows: [35],
            now="2026-06-27 14:59:01",
        )

        self.assertTrue(result.success)
        self.assertEqual(result.marked_rows, [35])

    def test_summary_reports_filter_counts_pages_and_qr_content(self):
        orders = [
            PickingOrder(22, "6/27/2026", "imy Shop", "imy1", "郵便局", [PickingItem("SKU1", "商品", "", "1", "")], qr_content="QR1"),
            PickingOrder(35, "6/27/2026", "imy Shop", "imy2", "郵便局", [PickingItem("SKU2", "商品", "", "1", "")], qr_content="QR2"),
        ]

        summary = build_picking_label_summary(orders)

        self.assertEqual(summary["source_sheet"], "南巽出貨Label")
        self.assertEqual(summary["filter_condition"], "K 訂單狀態 = 可出貨，且 L 製單後勾選 = 未製單")
        self.assertEqual(summary["order_count"], 2)
        self.assertEqual(summary["item_count"], 2)
        self.assertEqual(summary["estimated_pdf_pages"], 2)
        self.assertEqual(summary["qr_contents"], ["QR1", "QR2"])

    def test_shipping_deadline_lookup_uses_earliest_non_empty_date(self):
        values = [
            ["A", "B", "訂單編號", "D", "E", "F", "G", "H", "I", "J", "発送期限"],
            ["", "", "imy1", "", "", "", "", "", "", "", ""],
            ["", "", "imy1", "", "", "", "", "", "", "", "2026/07/04 00:00:00"],
            ["", "", "imy1", "", "", "", "", "", "", "", "2026/07/03 00:00:00"],
            ["", "", "imy2", "", "", "", "", "", "", "", "2026/07/05"],
        ]

        lookup = build_shipping_deadline_lookup(values)

        self.assertEqual(lookup["imy1"], "2026/07/03")
        self.assertEqual(lookup["imy2"], "2026/07/05")

    def test_format_shipping_deadline_strips_time_and_handles_blank(self):
        self.assertEqual(format_shipping_deadline("2026/07/03 00:00:00"), "2026/07/03")
        self.assertEqual(format_shipping_deadline(""), "")


class PickingLabelPdfTests(unittest.TestCase):
    def test_render_pdf_uses_label_page_size_and_splits_pages(self):
        output_path = ROOT / "tmp" / "test-picking-label.pdf"
        output_path.parent.mkdir(exist_ok=True)
        items = [PickingItem(f"SKU{idx}", f"KIWABI 綺和美 商品 {idx}", f"JAN{idx}", "1", "本日着予定") for idx in range(1, 12)]
        order = PickingOrder(2, "6/27/2026", "Official website - imy Shop", "imy2035810", "郵便局", items)

        result = render_picking_labels_pdf([order], str(output_path))

        self.assertEqual(result.total_pages, 2)
        self.assertTrue(output_path.exists())

        from pypdf import PdfReader

        reader = PdfReader(str(output_path))
        first_page = reader.pages[0]
        self.assertAlmostEqual(float(first_page.mediabox.width), 283.46, delta=0.5)
        self.assertAlmostEqual(float(first_page.mediabox.height), 425.20, delta=0.5)
        self.assertEqual(len(reader.pages), 2)

    def test_long_product_text_layout_keeps_name_to_two_lines_and_jan_separate(self):
        layout = plan_item_text_layout(
            PickingItem(
                "TRSN-LONG-SKU-1234567890",
                "KIWABI 綺和美 - Root Vanish 白髮染め ダークブラウン 超ロング商品名 テストパッケージ",
                "4901234567890",
                "12",
                "本日着予定",
            ),
            row_height_points=16 * 2.83465,
            name_width_points=64 * 2.83465,
        )

        self.assertLessEqual(len(layout["name_lines"]), 2)
        self.assertEqual(layout["jan"], "4901234567890")
        self.assertGreaterEqual(layout["name_font_size"], 5.5)
        self.assertTrue(layout["sku_text"])

    def test_registered_cjk_font_is_not_thin(self):
        info = get_registered_cjk_font_info()

        self.assertNotIn("thin", info["normal_source"].lower())
        self.assertNotIn("thin", info["bold_source"].lower())
        self.assertTrue(info["normal_font"])
        self.assertTrue(info["bold_font"])

    def test_dense_long_name_layout_keeps_readable_minimum_sizes(self):
        layout = plan_item_text_layout(
            PickingItem(
                "TRSN-LONG-8601",
                "THERMOS 膳魔師 - 真空保温調理器シャトルシェフ 4.5L ヴィンテージセピア KBG-4500 CBW",
                "4901234567890",
                "12",
                "本日着予定",
            ),
            row_height_points=11.2 * 2.83465,
            name_width_points=64 * 2.83465,
        )

        self.assertLessEqual(len(layout["name_lines"]), 2)
        self.assertGreaterEqual(layout["name_font_size"], 6.5)
        self.assertGreaterEqual(layout["jan_font_size"], 6.0)
        self.assertGreaterEqual(layout["sku_font_size"], 8.0)
        self.assertGreaterEqual(layout["quantity_font_size"], 14.0)
        self.assertGreaterEqual(layout["progress_font_size"], 6.5)

    def test_product_wrapping_preserves_common_model_and_unit_tokens(self):
        layout = plan_item_text_layout(
            PickingItem(
                "TRSN-TEST",
                "THERMOS 膳魔師 4.5L 480ml EH-NA0J JRX-T100 KBG-4500 EH93009549L",
                "4901234567890",
                "1",
                "本日着予定",
            ),
            row_height_points=13 * 2.83465,
            name_width_points=64 * 2.83465,
        )

        joined = "\n".join(layout["name_lines"])
        for token in ["4.5L", "480ml", "EH-NA0J", "JRX-T100", "KBG-4500", "EH93009549L"]:
            if token in joined.replace("\n", " "):
                self.assertTrue(any(token in line for line in layout["name_lines"]), token)

    def test_sparse_layout_keeps_long_name_tail_or_adds_ellipsis(self):
        layout = plan_item_text_layout(
            PickingItem(
                "TRSN8688",
                "Panasonic パナソニック - ナノケア ヘアドライヤー EH-NA0J ディープネイビー",
                "4901234567890",
                "1",
                "本日着予定",
            ),
            row_height_points=120,
            name_width_points=64 * 2.83465,
        )
        text = "".join(layout["name_lines"])

        self.assertTrue("ネイビー" in text or text.endswith("…"))

    def test_dry_layout_reports_realistic_ten_items_fit_dense_page(self):
        items = [
            PickingItem("TRSN8688", "THERMOS 膳魔師 - 真空保温調理器シャトルシェフ 4.5L ヴィンテージセピア KBG-4500 CBW", "4901234567800", "1", "本日着予定"),
            PickingItem("TRSN8689", "Nishikawa 西川 - 睡眠博士 2023 年モデル 寝返りアシスト 枕 低め EH93009549L", "4901234567801", "2", "本日着予定"),
            PickingItem("TRSN8690", "APA HOTELS&RESORTS - ADJUSTFIT (アジャストフィット) APA オリジナル 4way まくら", "4901234567802", "3", "本日着予定"),
            PickingItem("TRSN8691", "ZOJIRUSHI 象印 - ステンレスマグ シームレスせん SM-ZB48 スレートブラック 480ml", "4901234567803", "4", "本日着予定"),
            PickingItem("TRSN8692", "HITACHI 日立 - 衣類スチーマー CSI-RX70 アイボリー 連続スチームモデル", "4901234567804", "1", "本日着予定"),
            PickingItem("TRSN8693", "Panasonic パナソニック - ナノケア ヘアドライヤー EH-NA0J ディープネイビー", "4901234567805", "2", "本日着予定"),
            PickingItem("TRSN8694", "TIGER タイガー - 土鍋圧力IHジャー炊飯器 JRX-T100 コスモブラック", "4901234567806", "3", "本日着予定"),
            PickingItem("TRSN8695", "MUJI 無印良品 - 体にフィットするソファ カバーセット チャコールグレー", "4901234567807", "4", "本日着予定"),
            PickingItem("TRSN8696", "IRIS OHYAMA アイリスオーヤマ - サーキュレーターアイ DC JET PCF-SDCC15T", "4901234567808", "1", "本日着予定"),
            PickingItem("TRSN8697", "YAMAZEN 山善 - 電気圧力鍋 4.0L マイコン式 ブラック YPCA-M400", "4901234567809", "2", "本日着予定"),
        ]

        self.assertTrue(can_fit_items_on_page(items))

    def test_source_header_combines_source_and_japan_on_one_line(self):
        layout = plan_source_header_text(
            "Official website - imy Shop",
            width_points=54 * 2.83465,
        )

        self.assertEqual(layout["text"], "Official website - imy Shop Japan")
        self.assertGreaterEqual(layout["font_size"], 6.5)
        self.assertFalse(layout["truncated"])

    def test_source_header_supports_marketplace_and_long_official_variants(self):
        shopee = plan_source_header_text("Shopee-nikkahonpo.sg", width_points=54 * 2.83465)
        whowhy = plan_source_header_text("Official website - WhoWhy International", width_points=54 * 2.83465)

        self.assertEqual(shopee["text"], "Shopee-nikkahonpo.sg")
        self.assertIn("WhoWhy", whowhy["text"])
        self.assertGreaterEqual(whowhy["font_size"], 6.0)

    def test_logistics_header_supports_sagawa_two_line_variants(self):
        layout = plan_logistics_header_text("佐川-SLS")

        self.assertEqual(layout["lines"], ["佐川 -", "SLS"])
        self.assertGreaterEqual(layout["font_size"], 8.0)


if __name__ == "__main__":
    unittest.main()
