import sys
import types
import unittest

import pandas as pd

sys.modules.setdefault(
    "gspread",
    types.SimpleNamespace(Client=object, authorize=lambda *_args, **_kwargs: None),
)
sys.modules.setdefault("streamlit", types.SimpleNamespace(secrets={}))

from bot.sheets import (
    COUNTRY_CODE_MAP,
    _filter_pending_orders_dataframe,
    _get_worksheet_by_gid,
    _prefer_shipping_method_rows,
    _shipping_priority,
)


class SheetsHelperTests(unittest.TestCase):
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
        self.assertTrue(any("已在目標表完成而排除" in line and "WhoWhy-Test6" in line for line in logs))

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
        self.assertTrue(any("製單檢核 TRUE 排除" in line and "WhoWhy-Test6" in line for line in logs))
        self.assertTrue(any("Shipping Name 空白排除" in line and "WhoWhy-Test8" in line for line in logs))

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

        whowhy_lines = [line for line in logs if "- 關注訂單" in line]
        self.assertEqual(len(whowhy_lines), 2)
        self.assertTrue(any("WhoWhy-Test6" in line and "基礎=FAIL" in line for line in whowhy_lines))
        self.assertTrue(any("WhoWhy-Test7" in line and "基礎=PASS" in line for line in whowhy_lines))


if __name__ == "__main__":
    unittest.main()
