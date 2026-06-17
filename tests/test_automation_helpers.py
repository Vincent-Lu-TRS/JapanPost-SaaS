import sys
import types
import unittest

sys.modules.setdefault(
    "pandas",
    types.SimpleNamespace(Series=object, DataFrame=object, isna=lambda value: False),
)
sys.modules.setdefault("bot.drive", types.SimpleNamespace(upload_pdf=lambda *a, **k: None))
sys.modules.setdefault("bot.gemini_helper", types.SimpleNamespace(predict_hs_code=lambda *a, **k: ""))

from bot.automation import (
    _build_struts_submit,
    _extract_preferred_submit_command,
    _extract_submit_command_for_label,
    _summarize_submit_commands,
    _with_base_href,
)


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

    def test_extract_submit_command_from_image_alt_inside_link(self):
        html = """
        <form action="M010001.do" method="post">
          <a href="javascript:submitCommand('createNewLabel')">
            <img alt="Create New Labels" src="btn.gif">
          </a>
        </form>
        """

        command = _extract_submit_command_for_label(html, "Create New Labels")

        self.assertEqual(command, "createNewLabel")

    def test_extract_submit_command_from_input_value_and_onclick(self):
        html = """
        <form action="M010100.do" method="post">
          <input type="button" value="Next" onclick="submitCommand('goSender')">
        </form>
        """

        command = _extract_submit_command_for_label(html, "Next")

        self.assertEqual(command, "goSender")

    def test_summarize_submit_commands_lists_unique_commands(self):
        html = """
        <a href="javascript:submitCommand('onlineS')">Create New Labels</a>
        <input type="button" value="Next" onclick="submitCommand('regist')">
        <input type="button" value="Back" onclick="submitCommand('onlineS')">
        """

        summary = _summarize_submit_commands(html)

        self.assertEqual(summary, "onlineS, regist")

    def test_extract_preferred_submit_command_uses_priority_order(self):
        html = """
        <a href="javascript:submitCommand('returnTop')">Top</a>
        <input type="button" value="Register" onclick="submitCommand('regist')">
        <input type="button" value="Set address" onclick="submitCommand('addrSet')">
        """

        command = _extract_preferred_submit_command(html, ["addrSet", "directInput", "regist"])

        self.assertEqual(command, "addrSet")

    def test_build_struts_submit_renames_command_field_to_method_command(self):
        html = """
        <form action="M010001.do" method="post">
          <input type="hidden" name="command" value="">
          <input type="hidden" name="csrfToken" value="">
          <input type="hidden" name="request_locale" value="en">
        </form>
        """

        action, payload = _build_struts_submit(
            html,
            "createNewLabel",
            "https://www.int-mypage.post.japanpost.jp/mypage/",
        )

        self.assertEqual(action, "https://www.int-mypage.post.japanpost.jp/mypage/M010001.do")
        self.assertEqual(payload["method:createNewLabel"], "")
        self.assertEqual(payload["request_locale"], "en")
        self.assertNotIn("command", payload)


if __name__ == "__main__":
    unittest.main()
