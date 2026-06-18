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
    _prefer_shipping_method_rows,
    _shipping_priority,
)


class SheetsHelperTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
