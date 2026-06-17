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
    _choose_label_flow_command,
    _extract_preferred_submit_command,
    _extract_submit_command_for_label,
    _html_for_playwright_form,
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

    def test_html_for_playwright_form_removes_source_scripts_and_adds_submit_stub(self):
        html = """
        <html>
          <head><script src="legacy.js"></script></head>
          <body>
            <form action="M060505.do">
              <input type="button" value="Next" onclick="regist()">
            </form>
            <script>throw new Error("legacy");</script>
          </body>
        </html>
        """

        result = _html_for_playwright_form(html)

        self.assertNotIn("legacy.js", result)
        self.assertNotIn('throw new Error("legacy")', result)
        self.assertIn("function submitCommand(command)", result)
        self.assertIn("function regist()", result)

    def test_html_for_playwright_form_keeps_only_target_recipient_form(self):
        html = """
        <html><body>
          <form action="unrelated.do">
            <input name="noise" value="1">
          </form>
          <form action="M060505.do">
            <input id="M060505_addrToBean_nam" name="addrToBean.nam">
          </form>
        </body></html>
        """

        result = _html_for_playwright_form(html)

        self.assertIn("M060505_addrToBean_nam", result)
        self.assertIn('action="M060505.do"', result)
        self.assertNotIn('name="noise"', result)

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

    def test_extract_submit_command_from_input_value_and_regist_onclick(self):
        html = """
        <form action="M060000.do" method="post">
          <input type="button" value="Next" onclick="regist()">
        </form>
        """

        command = _extract_submit_command_for_label(html, "Next")

        self.assertEqual(command, "regist")

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

    def test_choose_label_flow_command_prefers_direct_input_on_recipient_select(self):
        html = """
        <form action="M060400.do" method="post">
          <input type="button" value="Next" onclick="regist()">
          <input type="button" value="Direct input" onclick="submitCommand('directInput')">
        </form>
        """

        command = _choose_label_flow_command(
            html,
            "https://www.int-mypage.post.japanpost.jp/mypage/M060400.do",
        )

        self.assertEqual(command, "directInput")

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

    def test_build_struts_submit_uses_checked_radio_value(self):
        html = """
        <form action="M060105.do" method="post">
          <input type="hidden" name="command" value="">
          <input type="radio" name="addressBookNo" value="old">
          <input type="radio" name="addressBookNo" value="selected" checked>
          <input type="radio" name="addressBookNo" value="later">
        </form>
        """

        _, payload = _build_struts_submit(
            html,
            "addrSet",
            "https://www.int-mypage.post.japanpost.jp/mypage/",
        )

        self.assertEqual(payload["addressBookNo"], "selected")

    def test_build_struts_submit_applies_checked_input_set_value_side_effect(self):
        html = """
        <form action="M060000.do" method="post">
          <input type="hidden" name="command" value="">
          <input type="hidden" name="selID" value="">
          <input
            type="radio"
            name="sel"
            value="3693083"
            checked
            onclick="setValue('selID', 'sender@example.com');"
          >
        </form>
        """

        _, payload = _build_struts_submit(
            html,
            "regist",
            "https://www.int-mypage.post.japanpost.jp/mypage/",
        )

        self.assertEqual(payload["sel"], "3693083")
        self.assertEqual(payload["selID"], "sender@example.com")
        self.assertEqual(payload["method:regist"], "")


if __name__ == "__main__":
    unittest.main()
