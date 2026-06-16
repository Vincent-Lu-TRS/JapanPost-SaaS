import sys
import types
import unittest

sys.modules.setdefault(
    "pandas",
    types.SimpleNamespace(Series=object, DataFrame=object, isna=lambda value: False),
)
sys.modules.setdefault("bot.drive", types.SimpleNamespace(upload_pdf=lambda *a, **k: None))
sys.modules.setdefault("bot.gemini_helper", types.SimpleNamespace(predict_hs_code=lambda *a, **k: ""))

from bot.automation import _with_base_href


class AutomationHtmlTests(unittest.TestCase):
    def test_with_base_href_inserts_base_inside_head(self):
        html = "<html><head><title>Main</title></head><body>Create New Labels</body></html>"

        result = _with_base_href(html, "https://www.int-mypage.post.japanpost.jp/mypage/")

        self.assertIn(
            '<base href="https://www.int-mypage.post.japanpost.jp/mypage/">',
            result,
        )
        self.assertLess(result.index("<base "), result.index("<title>"))

    def test_with_base_href_does_not_duplicate_existing_base(self):
        html = '<html><head><base href="https://example.com/"><title>Main</title></head></html>'

        result = _with_base_href(html, "https://www.int-mypage.post.japanpost.jp/mypage/")

        self.assertEqual(result.count("<base "), 1)


if __name__ == "__main__":
    unittest.main()
