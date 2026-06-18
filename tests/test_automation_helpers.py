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
    _build_m060800_item_payload,
    _build_m060800_next_payload,
    _build_m060900_weight_payload,
    _build_m061000_register_payload,
    _build_m061100_print_payload,
    _build_m061101_completed_payload,
    _build_result_record,
    _build_struts_submit,
    _choose_label_flow_command,
    _extract_preferred_submit_command,
    _extract_pdf_download_url,
    _extract_submit_command_for_label,
    _html_for_playwright_form,
    _iter_content_items,
    _parse_forms,
    _pick_form,
    _select_option_value,
    _summarize_error_text,
    _summarize_forms,
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

    def test_parse_forms_extracts_m060505_recipient_fields_and_country_options(self):
        html = """
        <form action="M060505.do" method="post">
          <input type="hidden" name="command" value="">
          <input name="addrToBean.nam" value="">
          <select name="addrToBean.couCode">
            <option value="">Select</option>
            <option value="US">United States</option>
            <option value="FR" selected>France</option>
          </select>
          <textarea name="memo">hello</textarea>
        </form>
        """

        form = _pick_form(html, preferred_action="M060505", required_fields=["addrToBean.nam"])

        self.assertEqual(form["action"], "M060505.do")
        self.assertEqual(form["fields"]["addrToBean.nam"], "")
        self.assertEqual(form["fields"]["addrToBean.couCode"], "FR")
        self.assertEqual(form["fields"]["memo"], "hello")
        self.assertEqual(
            _select_option_value(form, "addrToBean.couCode", "United States"),
            "US",
        )

    def test_parse_forms_uses_first_radio_when_none_checked_and_keeps_checked_value(self):
        html = """
        <form action="M060800.do" method="post">
          <input type="radio" name="shippingBean.senderInstruction" value="1">
          <input type="radio" name="shippingBean.senderInstruction" value="2">
          <input type="radio" name="shippingBean.fwTransType" value="surface">
          <input type="radio" name="shippingBean.fwTransType" value="air" checked>
        </form>
        """

        form = _parse_forms(html)[0]

        self.assertEqual(form["fields"]["shippingBean.senderInstruction"], "1")
        self.assertEqual(form["fields"]["shippingBean.fwTransType"], "air")

    def test_summarize_forms_lists_actions_and_key_fields(self):
        html = """
        <form action="M060800.do" method="post">
          <input type="hidden" name="command" value="">
          <input name="itemBean.pkg" value="">
          <input name="shippingBean.pkgTotalPrice.value" value="">
          <select name="itemBean.curUnit"><option value="USD">USD</option></select>
        </form>
        """

        summary = _summarize_forms(html)

        self.assertIn("M060800.do", summary)
        self.assertIn("itemBean.pkg", summary)
        self.assertIn("shippingBean.pkgTotalPrice.value", summary)
        self.assertIn("selects=itemBean.curUnit", summary)

    def test_build_m060800_item_payload_fills_first_item_and_uses_item_add(self):
        html = """
        <form action="/mypage/M060800.do" method="post">
          <input type="hidden" name="command" value="">
          <input type="hidden" name="csrfToken" value="token">
          <input type="hidden" name="shippingBean.sendType" value="parcel">
          <input type="hidden" name="shippingBean.transType" value="air">
          <input name="itemBean.pkg" value="">
          <input name="itemBean.cost.value" value="">
          <input name="itemBean.num.value" value="">
          <input name="shippingBean.pkgTotalPrice.value" value="">
          <input type="checkbox" name="ShippingBean.danger" value="1">
          <select name="itemBean.curUnit"><option value="USD">USD</option></select>
        </form>
        """
        row = {
            "內容物1": "T-shirt",
            "申告金額1": "12.5",
            "數量1": "2",
            "訂單合計申告金額(JPY)": "1800",
        }

        action, payload = _build_m060800_item_payload(
            html,
            "https://www.int-mypage.post.japanpost.jp/mypage/M060505.do",
            row,
            is_eu=False,
        )

        self.assertEqual(action, "https://www.int-mypage.post.japanpost.jp/mypage/M060800.do")
        self.assertEqual(payload["csrfToken"], "token")
        self.assertEqual(payload["shippingBean.sendType"], "parcel")
        self.assertEqual(payload["itemBean.pkg"], "T-shirt")
        self.assertEqual(payload["itemBean.cost.value"], "12.5")
        self.assertEqual(payload["itemBean.num.value"], "2")
        self.assertEqual(payload["itemBean.curUnit"], "USD")
        self.assertEqual(payload["shippingBean.pkgTotalPrice.value"], "1800")
        self.assertEqual(payload["ShippingBean.danger"], "1")
        self.assertEqual(payload["method:itemAdd2"], "")
        self.assertNotIn("command", payload)

    def test_build_m060800_next_payload_uses_regist_after_item_confirm(self):
        html = """
        <form action="/mypage/M060800.do" method="post">
          <input type="hidden" name="command" value="">
          <input type="hidden" name="csrfToken" value="token2">
          <input type="hidden" name="shippingBean.sendType" value="8">
          <input type="hidden" name="shippingBean.transType" value="">
          <input type="hidden" name="shippingBean.pkgType" value="0">
          <input name="shippingBean.pkgTotalPrice.value" value="">
          <input type="checkbox" name="ShippingBean.danger" value="1">
        </form>
        """
        row = {"訂單合計申告金額(JPY)": "1846"}

        action, payload = _build_m060800_next_payload(
            html,
            "https://www.int-mypage.post.japanpost.jp/mypage/M060800.do",
            row,
        )

        self.assertEqual(action, "https://www.int-mypage.post.japanpost.jp/mypage/M060800.do")
        self.assertEqual(payload["csrfToken"], "token2")
        self.assertEqual(payload["shippingBean.sendType"], "8")
        self.assertEqual(payload["shippingBean.pkgTotalPrice.value"], "1846")
        self.assertEqual(payload["ShippingBean.danger"], "1")
        self.assertEqual(payload["method:regist"], "")
        self.assertNotIn("command", payload)

    def test_build_m060800_item_payload_selects_postal_parcel_air_for_international_parcel(self):
        html = """
        <form action="/mypage/M060800.do" method="post">
          <input type="hidden" name="command" value="">
          <input type="hidden" name="csrfToken" value="token">
          <input type="hidden" name="shippingBean.sendType" value="ems">
          <input type="hidden" name="shippingBean.transType" value="ems-default">
          <input type="hidden" name="shippingBean.pkgType" value="">
          <input type="button" value="Postal Parcel"
            onclick="setValue('shippingBean.sendType', 'parcel');setValue('shippingBean.pkgType', 'gift');">
          <input type="button" value="Air Packet"
            onclick="setValue('shippingBean.transType', 'air-packet');">
          <input type="button" value="Air"
            onclick="setValue('shippingBean.transType', 'air');">
          <input name="itemBean.pkg" value="">
          <input name="itemBean.cost.value" value="">
          <input name="itemBean.num.value" value="">
          <select name="itemBean.curUnit"><option value="USD">USD</option></select>
        </form>
        """
        row = {
            "郵局運送方式(複數商品請自行確認是否走小包)": "國際小包",
            "內容物1": "Groundsheet",
            "申告金額1": "23.41",
            "數量1": "1",
        }

        _, payload = _build_m060800_item_payload(
            html,
            "https://www.int-mypage.post.japanpost.jp/mypage/M060505.do",
            row,
            is_eu=False,
        )

        self.assertEqual(payload["shippingBean.sendType"], "parcel")
        self.assertEqual(payload["shippingBean.transType"], "air")
        self.assertEqual(payload["shippingBean.pkgType"], "gift")

    def test_build_m060800_item_payload_reads_assignments_from_image_anchor_controls(self):
        html = """
        <form action="/mypage/M060800.do" method="post">
          <input type="hidden" name="command" value="">
          <input type="hidden" name="csrfToken" value="token">
          <input type="hidden" name="shippingBean.sendType" value="0">
          <input type="hidden" name="shippingBean.transType" value="">
          <input type="hidden" name="shippingBean.pkgType" value="0">
          <a href="javascript:changeValue('shippingBean.sendType','2');changeValue('shippingBean.pkgType','1');">
            <img alt="POSTAL PARCEL" src="parcel.gif">
          </a>
          <a href="javascript:changeValue('shippingBean.transType','1');">
            <img alt="AIR" src="air.gif">
          </a>
          <input name="itemBean.pkg" value="">
          <input name="itemBean.cost.value" value="">
          <input name="itemBean.num.value" value="">
          <select name="itemBean.curUnit"><option value="USD">USD</option></select>
        </form>
        """
        row = {
            "郵局運送方式(複數商品請自行確認是否走小包)": "國際小包",
            "內容物1": "Groundsheet",
            "申告金額1": "23.41",
            "數量1": "1",
        }

        _, payload = _build_m060800_item_payload(
            html,
            "https://www.int-mypage.post.japanpost.jp/mypage/M060505.do",
            row,
            is_eu=False,
        )

        self.assertEqual(payload["shippingBean.sendType"], "2")
        self.assertEqual(payload["shippingBean.transType"], "1")
        self.assertEqual(payload["shippingBean.pkgType"], "1")

    def test_build_m060800_item_payload_reads_japan_post_button_functions(self):
        html = """
        <form action="/mypage/M060800.do" method="post">
          <input type="hidden" name="command" value="">
          <input type="hidden" name="csrfToken" value="token">
          <input type="hidden" name="shippingBean.sendType" value="0">
          <input type="hidden" name="shippingBean.transType" value="">
          <input type="hidden" name="shippingBean.pkgType" value="0">
          <td>
            <button type="button" id="ID_SENDTYPE_BTN_PAR" onclick="chgSendTypeBtn(5);">
              <img src="images/mypage_en/sendType/PAR_W.PNG" id="ID_SENDTYPE_IMG_PAR" alt="POSTAL PARCEL">
            </button>
          </td>
          <td>
            <button type="button" id="ID_TRANSTYPE_BTN_AIR" onclick="chgTransTypeBtn(1);">
              <img src="images/mypage_en/transType/AIR_W.PNG" id="ID_TRANSTYPE_IMG_AIR" alt="AIR">
            </button>
          </td>
          <input name="itemBean.pkg" value="">
          <input name="itemBean.cost.value" value="">
          <input name="itemBean.num.value" value="">
          <select name="itemBean.curUnit"><option value="USD">USD</option></select>
        </form>
        """
        row = {
            "郵局運送方式(複數商品請自行確認是否走小包)": "國際小包",
            "內容物1": "Groundsheet",
            "申告金額1": "23.41",
            "數量1": "1",
        }

        _, payload = _build_m060800_item_payload(
            html,
            "https://www.int-mypage.post.japanpost.jp/mypage/M060505.do",
            row,
            is_eu=False,
        )

        self.assertEqual(payload["shippingBean.sendType"], "5")
        self.assertEqual(payload["shippingBean.transType"], "1")
        self.assertEqual(payload["shippingBean.pkgType"], "0")

    def test_build_m060800_item_payload_uses_postal_button_not_previous_ems_button(self):
        html = """
        <form action="/mypage/M060800.do" method="post">
          <input type="hidden" name="command" value="">
          <input type="hidden" name="csrfToken" value="token">
          <input type="hidden" name="shippingBean.sendType" value="0">
          <input type="hidden" name="shippingBean.transType" value="">
          <input type="hidden" name="shippingBean.pkgType" value="0">
          <td>
            <button type="button" id="ID_SENDTYPE_BTN_PKG" onclick="chgSendTypeBtn(1);">
              <img src="images/mypage_en/sendType/PKG.PNG" id="ID_SENDTYPE_IMG_PKG" alt="EMS(Goods)">
            </button>
          </td>
          <td>
            <button type="button" id="ID_SENDTYPE_BTN_PAR" onclick="chgSendTypeBtn(5);">
              <img src="images/mypage_en/sendType/PAR_W.PNG" id="ID_SENDTYPE_IMG_PAR" alt="POSTAL PARCEL">
            </button>
          </td>
          <td>
            <button type="button" id="ID_TRANSTYPE_BTN_AIR" onclick="chgTransTypeBtn(1);">
              <img src="images/mypage_en/transType/AIR_W.PNG" id="ID_TRANSTYPE_IMG_AIR" alt="AIR">
            </button>
          </td>
          <input name="itemBean.pkg" value="">
          <input name="itemBean.cost.value" value="">
          <input name="itemBean.num.value" value="">
          <select name="itemBean.curUnit"><option value="USD">USD</option></select>
        </form>
        """
        row = {
            "郵局運送方式(複數商品請自行確認是否走小包)": "國際小包",
            "內容物1": "Groundsheet",
            "申告金額1": "23.41",
            "數量1": "1",
        }

        _, payload = _build_m060800_item_payload(
            html,
            "https://www.int-mypage.post.japanpost.jp/mypage/M060505.do",
            row,
            is_eu=False,
        )

        self.assertEqual(payload["shippingBean.sendType"], "5")
        self.assertEqual(payload["shippingBean.transType"], "1")

    def test_build_m060800_item_payload_air_scan_does_not_overwrite_postal_send_type(self):
        html = """
        <form action="/mypage/M060800.do" method="post">
          <input type="hidden" name="command" value="">
          <input type="hidden" name="csrfToken" value="token">
          <input type="hidden" name="shippingBean.sendType" value="0">
          <input type="hidden" name="shippingBean.transType" value="">
          <input type="hidden" name="shippingBean.pkgType" value="0">
          <button type="button" id="ID_SENDTYPE_BTN_PAR" onclick="chgSendTypeBtn(5);">
            <img src="images/mypage_en/sendType/PAR_W.PNG" id="ID_SENDTYPE_IMG_PAR" alt="POSTAL PARCEL">
          </button>
          <button type="button" id="ID_SENDTYPE_BTN_EPACK_LITE" onclick="chgSendTypeBtn(8);">
            <img src="images/mypage_en/sendType/EPACK_LITE_W.PNG" id="ID_SENDTYPE_IMG_EPACK_LITE" alt="International ePacket light">
          </button>
          <script>
            function chgTransTypeBtn(transTypeValue) {
              setValue('shippingBean.transType', transTypeValue);
              if (transTypeValue == 1) {
                document.getElementById('ID_TRANSTYPE_IMG_AIR').src = "images/mypage_en/transType/AIR.PNG";
              }
            }
          </script>
          <button type="button" id="ID_TRANSTYPE_BTN_AIR" onclick="chgTransTypeBtn(1);">
            <img src="images/mypage_en/transType/AIR_W.PNG" id="ID_TRANSTYPE_IMG_AIR" alt="AIR">
          </button>
          <input name="itemBean.pkg" value="">
          <input name="itemBean.cost.value" value="">
          <input name="itemBean.num.value" value="">
          <select name="itemBean.curUnit"><option value="USD">USD</option></select>
        </form>
        """
        row = {
            "郵局運送方式(複數商品請自行確認是否走小包)": "國際小包",
            "內容物1": "Groundsheet",
            "申告金額1": "23.41",
            "數量1": "1",
        }

        _, payload = _build_m060800_item_payload(
            html,
            "https://www.int-mypage.post.japanpost.jp/mypage/M060505.do",
            row,
            is_eu=False,
        )

        self.assertEqual(payload["shippingBean.sendType"], "5")
        self.assertEqual(payload["shippingBean.transType"], "1")

    def test_build_m060800_item_payload_defaults_postal_air_when_only_trans_function_exists(self):
        html = """
        <form action="/mypage/M060800.do" method="post">
          <input type="hidden" name="command" value="">
          <input type="hidden" name="csrfToken" value="token">
          <input type="hidden" name="shippingBean.sendType" value="0">
          <input type="hidden" name="shippingBean.transType" value="">
          <input type="hidden" name="shippingBean.pkgType" value="0">
          <button type="button" id="ID_SENDTYPE_BTN_PAR" onclick="chgSendTypeBtn(5);">
            <img src="images/mypage_en/sendType/PAR_W.PNG" id="ID_SENDTYPE_IMG_PAR" alt="POSTAL PARCEL">
          </button>
          <script>
            function chgTransTypeBtn(transTypeValue) {
              setValue('shippingBean.transType', transTypeValue);
              if (transTypeValue == 1) {
                document.getElementById('ID_TRANSTYPE_IMG_AIR').src = "images/mypage_en/transType/AIR.PNG";
              }
            }
          </script>
          <input name="itemBean.pkg" value="">
          <input name="itemBean.cost.value" value="">
          <input name="itemBean.num.value" value="">
          <select name="itemBean.curUnit"><option value="USD">USD</option></select>
        </form>
        """
        row = {
            "郵局運送方式(複數商品請自行確認是否走小包)": "國際小包",
            "內容物1": "Frying Pan",
            "申告金額1": "1.56",
            "數量1": "1",
        }

        _, payload = _build_m060800_item_payload(
            html,
            "https://www.int-mypage.post.japanpost.jp/mypage/M060505.do",
            row,
            is_eu=False,
        )

        self.assertEqual(payload["shippingBean.sendType"], "5")
        self.assertEqual(payload["shippingBean.transType"], "1")

    def test_build_m060800_item_payload_selects_epacket_light_for_epacket(self):
        html = """
        <form action="/mypage/M060800.do" method="post">
          <input type="hidden" name="command" value="">
          <input type="hidden" name="csrfToken" value="token">
          <input type="hidden" name="shippingBean.sendType" value="0">
          <input type="hidden" name="shippingBean.transType" value="">
          <input type="hidden" name="shippingBean.pkgType" value="0">
          <button type="button" id="ID_SENDTYPE_BTN_PKG" onclick="chgSendTypeBtn(1);">
            <img src="images/mypage_en/sendType/PKG.PNG" id="ID_SENDTYPE_IMG_PKG" alt="EMS(Goods)">
          </button>
          <button type="button" id="ID_SENDTYPE_BTN_EPACK_LITE" onclick="chgSendTypeBtn(8);">
            <img src="images/mypage_en/sendType/EPACK_LITE_W.PNG" id="ID_SENDTYPE_IMG_EPACK_LITE" alt="International ePacket light">
          </button>
          <input name="itemBean.pkg" value="">
          <input name="itemBean.cost.value" value="">
          <input name="itemBean.num.value" value="">
          <select name="itemBean.curUnit"><option value="USD">USD</option></select>
        </form>
        """
        row = {
            "郵局運送方式(複數商品請自行確認是否走小包)": "ePacket",
            "內容物1": "Portable Cooking Stove",
            "申告金額1": "11.49",
            "數量1": "1",
        }

        _, payload = _build_m060800_item_payload(
            html,
            "https://www.int-mypage.post.japanpost.jp/mypage/M060505.do",
            row,
            is_eu=False,
        )

        self.assertEqual(payload["shippingBean.sendType"], "8")
        self.assertEqual(payload["itemBean.pkg"], "Portable Cooking Stove")

    def test_iter_content_items_reads_up_to_multiple_numbered_items(self):
        row = {
            "內容物1": "Facial Mask TRSN6764",
            "申告金額1": "1.55",
            "數量1": "1",
            "內容物2": "Pillow TRSN9842",
            "申告金額2": "1.55",
            "數量2": "2",
        }

        items = _iter_content_items(row)

        self.assertEqual(
            items,
            [
                {"index": "1", "pkg": "Facial Mask TRSN6764", "cost": "1.55", "num": "1"},
                {"index": "2", "pkg": "Pillow TRSN9842", "cost": "1.55", "num": "2"},
            ],
        )

    def test_build_m060800_item_payload_can_submit_second_item(self):
        html = """
        <form action="/mypage/M060800.do" method="post">
          <input type="hidden" name="command" value="">
          <input type="hidden" name="csrfToken" value="token">
          <input type="hidden" name="shippingBean.sendType" value="8">
          <input type="hidden" name="shippingBean.transType" value="">
          <input type="hidden" name="shippingBean.pkgType" value="0">
          <input name="itemBean.pkg" value="">
          <input name="itemBean.cost.value" value="">
          <input name="itemBean.num.value" value="">
          <select name="itemBean.curUnit"><option value="USD">USD</option></select>
        </form>
        """
        row = {
            "郵局運送方式(複數商品請自行確認是否走小包)": "ePacket",
            "內容物1": "Facial Mask TRSN6764",
            "申告金額1": "1.55",
            "數量1": "1",
            "內容物2": "Pillow TRSN9842",
            "申告金額2": "1.55",
            "數量2": "2",
        }

        _, payload = _build_m060800_item_payload(
            html,
            "https://www.int-mypage.post.japanpost.jp/mypage/M060505.do",
            row,
            is_eu=False,
            item_index=2,
        )

        self.assertEqual(payload["itemBean.pkg"], "Pillow TRSN9842")
        self.assertEqual(payload["itemBean.cost.value"], "1.55")
        self.assertEqual(payload["itemBean.num.value"], "2")

    def test_build_m060800_item_payload_stops_if_epacket_keeps_ems_default(self):
        html = """
        <form action="/mypage/M060800.do" method="post">
          <input type="hidden" name="command" value="">
          <input type="hidden" name="csrfToken" value="token">
          <input type="hidden" name="shippingBean.sendType" value="0">
          <input type="hidden" name="shippingBean.transType" value="">
          <input type="hidden" name="shippingBean.pkgType" value="0">
          <button type="button" id="ID_SENDTYPE_BTN_PKG" onclick="chgSendTypeBtn(1);">
            <img src="images/mypage_en/sendType/PKG.PNG" id="ID_SENDTYPE_IMG_PKG" alt="EMS(Goods)">
          </button>
          <input name="itemBean.pkg" value="">
          <input name="itemBean.cost.value" value="">
          <input name="itemBean.num.value" value="">
          <select name="itemBean.curUnit"><option value="USD">USD</option></select>
        </form>
        """
        row = {
            "郵局運送方式(複數商品請自行確認是否走小包)": "ePacket",
            "內容物1": "Portable Cooking Stove",
            "申告金額1": "11.49",
            "數量1": "1",
        }

        with self.assertRaisesRegex(RuntimeError, "Unable to resolve ePacket payload"):
            _build_m060800_item_payload(
                html,
                "https://www.int-mypage.post.japanpost.jp/mypage/M060505.do",
                row,
                is_eu=False,
            )

    def test_build_m060800_item_payload_stops_if_postal_parcel_keeps_ems_defaults(self):
        html = """
        <form action="/mypage/M060800.do" method="post">
          <input type="hidden" name="command" value="">
          <input type="hidden" name="csrfToken" value="token">
          <input type="hidden" name="shippingBean.sendType" value="0">
          <input type="hidden" name="shippingBean.transType" value="">
          <input type="hidden" name="shippingBean.pkgType" value="0">
          <img alt="POSTAL PARCEL" src="parcel.gif">
          <img alt="AIR" src="air.gif">
          <input name="itemBean.pkg" value="">
          <input name="itemBean.cost.value" value="">
          <input name="itemBean.num.value" value="">
          <select name="itemBean.curUnit"><option value="USD">USD</option></select>
        </form>
        """
        row = {
            "郵局運送方式(複數商品請自行確認是否走小包)": "國際小包",
            "內容物1": "Groundsheet",
            "申告金額1": "23.41",
            "數量1": "1",
        }

        with self.assertRaisesRegex(RuntimeError, "POSTAL PARCEL=>"):
            _build_m060800_item_payload(
                html,
                "https://www.int-mypage.post.japanpost.jp/mypage/M060505.do",
                row,
                is_eu=False,
            )

    def test_summarize_error_text_extracts_visible_validation_messages(self):
        html = """
        <html><body>
          <div class="error">Please enter the total weight.</div>
          <script>var x = "Please ignore script";</script>
        </body></html>
        """

        self.assertIn("Please enter the total weight", _summarize_error_text(html))

    def test_build_m060900_weight_payload_sets_total_weight_and_uses_regist(self):
        html = """
        <form action="/mypage/M060900.do" method="post">
          <input type="hidden" name="command" value="">
          <input type="hidden" name="csrfToken" value="token">
          <input name="emsNo.value" value="">
          <input name="shippingBean.sendDate.YMD" value="2026/06/18">
          <input name="shippingBean.num.value" value="1">
          <input name="shippingBean.totalNum.value" value="1">
          <input name="shippingBean.totalWeight.value" value="">
          <input name="shippingBean.cost.value" value="23.41">
          <select name="shippingBean.sendDate.YMD">
            <option value="2026/06/18" selected>2026/06/18</option>
          </select>
        </form>
        """

        action, payload = _build_m060900_weight_payload(
            html,
            "https://www.int-mypage.post.japanpost.jp/mypage/M060800.do",
            weight_grams="100",
        )

        self.assertEqual(action, "https://www.int-mypage.post.japanpost.jp/mypage/M060900.do")
        self.assertEqual(payload["csrfToken"], "token")
        self.assertEqual(payload["shippingBean.sendDate.YMD"], "2026/06/18")
        self.assertEqual(payload["shippingBean.num.value"], "1")
        self.assertEqual(payload["shippingBean.totalWeight.value"], "100")
        self.assertEqual(payload["shippingBean.cost.value"], "23.41")
        self.assertEqual(payload["method:regist"], "")
        self.assertNotIn("command", payload)

    def test_build_m060900_weight_payload_sets_invoice_print_num_when_select_exists(self):
        html = """
        <form action="/mypage/M060900.do" method="post">
          <input type="hidden" name="command" value="">
          <input type="hidden" name="csrfToken" value="token">
          <input name="shippingBean.totalWeight.value" value="">
          <select name="shippingBean.invPrintNum.value">
            <option value=""></option>
            <option value="1">1</option>
            <option value="2">2</option>
          </select>
        </form>
        """

        _, payload = _build_m060900_weight_payload(
            html,
            "https://www.int-mypage.post.japanpost.jp/mypage/M060900.do",
            weight_grams="100",
        )

        self.assertEqual(payload["shippingBean.totalWeight.value"], "100")
        self.assertEqual(payload["shippingBean.invPrintNum.value"], "1")
        self.assertEqual(payload["method:regist"], "")

    def test_build_m061000_register_payload_uses_regist(self):
        html = """
        <form action="/mypage/M061000.do" method="post">
          <input type="hidden" name="command" value="">
          <input type="hidden" name="csrfToken" value="token">
        </form>
        """

        action, payload = _build_m061000_register_payload(
            html,
            "https://www.int-mypage.post.japanpost.jp/mypage/M060900.do",
        )

        self.assertEqual(action, "https://www.int-mypage.post.japanpost.jp/mypage/M061000.do")
        self.assertEqual(payload["csrfToken"], "token")
        self.assertEqual(payload["method:regist"], "")
        self.assertNotIn("command", payload)

    def test_build_m061100_print_payload_uses_print(self):
        html = """
        <form action="/mypage/M061100.do" method="post">
          <input type="hidden" name="command" value="">
          <input type="hidden" name="csrfToken" value="token">
        </form>
        """

        action, payload = _build_m061100_print_payload(
            html,
            "https://www.int-mypage.post.japanpost.jp/mypage/M061000.do",
        )

        self.assertEqual(action, "https://www.int-mypage.post.japanpost.jp/mypage/M061100.do")
        self.assertEqual(payload["csrfToken"], "token")
        self.assertEqual(payload["method:print"], "")
        self.assertNotIn("command", payload)

    def test_extract_pdf_download_url_from_m061100_html(self):
        html = """
        <html><body>
          <a href="/mypage/DOWNLOAD?pdf=abc123&amp;locale=en">PDF</a>
        </body></html>
        """

        url = _extract_pdf_download_url(
            html,
            "https://www.int-mypage.post.japanpost.jp/mypage/M061100.do",
        )

        self.assertEqual(
            url,
            "https://www.int-mypage.post.japanpost.jp/mypage/DOWNLOAD?pdf=abc123&locale=en",
        )

    def test_build_m061101_completed_payload_uses_regist(self):
        html = """
        <form action="/mypage/M061101.do" method="post">
          <input type="hidden" name="command" value="">
          <input type="hidden" name="csrfToken" value="token">
        </form>
        """

        action, payload = _build_m061101_completed_payload(
            html,
            "https://www.int-mypage.post.japanpost.jp/mypage/M061100.do",
        )

        self.assertEqual(action, "https://www.int-mypage.post.japanpost.jp/mypage/M061101.do")
        self.assertEqual(payload["csrfToken"], "token")
        self.assertEqual(payload["method:regist"], "")
        self.assertNotIn("command", payload)

    def test_build_result_record_uses_tracking_and_order_fields(self):
        row = {
            "Shipping Name": "Klas Eklof",
            "收件人國家": "UNITED STATES OF AMERICA",
        }

        result = _build_result_record(row, "WhoWhy1566", "EN521206692JP")

        self.assertEqual(result["name"], "Klas Eklof")
        self.assertEqual(result["order_id"], "WhoWhy1566")
        self.assertEqual(result["tracking"], "EN521206692JP")
        self.assertEqual(result["country"], "UNITED STATES OF AMERICA")
        self.assertEqual(result["country_raw"], "UNITED STATES OF AMERICA")
        self.assertRegex(result["date"], r"^\d{4}-\d{2}-\d{2}$")

    def test_run_automation_does_not_call_playwright_html_injection(self):
        from pathlib import Path

        source = Path(__file__).parents[1].joinpath("bot", "automation.py").read_text(encoding="utf-8")
        body = source.split("def set_content_from_requests", 1)[1]

        self.assertNotIn("set_content_from_requests(", body)


if __name__ == "__main__":
    unittest.main()
