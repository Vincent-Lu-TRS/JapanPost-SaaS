import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


APP_TEST_SCRIPT = textwrap.dedent(
    r'''
    from pathlib import Path
    from unittest.mock import patch

    import pandas as pd
    from streamlit.testing.v1 import AppTest

    mock_pending = pd.DataFrame(
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
            },
            {
                "注文番号(貼上原始資料)": "mock-v2-8220",
                "Shipping Name": "Ying Chan",
                "收件人國家": "UNITED KINGDOM",
                "郵局運送方式(複數商品請自行確認是否走小包)": "ePacket",
                "郵局申告金額(USD)": "6.00",
                "內容物1": "Water Bottle TRSN3392",
                "申告金額1": "3.00",
                "數量1": "1",
                "HSCode1": "392490",
                "內容物2": "Water Bottle TRSN6195",
                "申告金額2": "3.00",
                "數量2": "1",
                "HSCode2": "392490",
                "訂單合計申告金額(JPY)": "900",
            },
        ]
    )

    app = AppTest.from_file(str(Path.cwd() / "app.py"))
    with patch("fx_rates.fetch_usd_jpy_rate", return_value=(157.79, "2026-08-07", "mock")):
        app.run(timeout=30)
        app.session_state["authenticated"] = True
        app.session_state["user_email"] = "tester@tkrjm.co.jp"
        app.session_state["user_name"] = "Mock Tester"
        app.session_state["last_pending_df"] = mock_pending
        app.session_state["last_pending_logs"] = []
        app.run(timeout=30)

    assert not app.exception, app.exception
    assert not app.error, app.error
    buttons = [item.label for item in app.button]
    markdown = [item.value for item in app.markdown]
    assert any("postal-v2" in value for value in markdown)
    assert any("USD/JPY 157.79" in value for value in markdown)
    for label in [
        "\u9078\u53d6\u5168\u90e8",
        "\u6e05\u9664\u5168\u90e8",
        "\u958b\u59cb\u88fd\u55ae",
        "\u91cd\u65b0\u8b80\u53d6",
        "\u5168\u90e8\u6062\u5fa9\u9810\u8a2d\u8cc7\u6599",
    ]:
        assert label in buttons, label
    assert len(
        [
            item.key
            for item in app.text_input
            if item.key and item.key.startswith("pending_v2_name_")
        ]
    ) == 2

    next(button for button in app.button if button.label == "\u6e05\u9664\u5168\u90e8").click().run(timeout=30)
    assert [
        item.value
        for item in app.checkbox
        if item.key and item.key.startswith("pending_v2_selected_")
    ] == [False, False]
    next(button for button in app.button if button.label == "\u9078\u53d6\u5168\u90e8").click().run(timeout=30)
    assert [
        item.value
        for item in app.checkbox
        if item.key and item.key.startswith("pending_v2_selected_")
    ] == [True, True]
    assert not app.exception, app.exception
    assert not app.error, app.error
    '''
)


class PostalUiV2AppTest(unittest.TestCase):
    def test_v2_app_test_with_mock_orders(self):
        probe = subprocess.run(
            [sys.executable, "-c", "from streamlit.testing.v1 import AppTest"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        if probe.returncode != 0:
            self.skipTest("本機執行環境未安裝 Streamlit AppTest")

        result = subprocess.run(
            [sys.executable, "-c", APP_TEST_SCRIPT],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        if result.returncode != 0:
            self.fail(
                "Streamlit AppTest mock 失敗\n"
                f"stdout:\n{result.stdout}\n"
                f"stderr:\n{result.stderr}"
            )


if __name__ == "__main__":
    unittest.main()
