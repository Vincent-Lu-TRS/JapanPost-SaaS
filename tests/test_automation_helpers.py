import sys
import types
import unittest
from unittest.mock import patch

try:
    import pandas  # noqa: F401
except Exception:
    sys.modules.setdefault(
        "pandas",
        types.SimpleNamespace(Series=object, DataFrame=object, isna=lambda value: False),
    )
sys.modules.setdefault(
    "bot.drive",
    types.SimpleNamespace(
        DRIVE_FOLDER_ID="debug-folder",
        upload_file_to_drive=lambda *a, **k: {},
        upload_pdf=lambda *a, **k: None,
    ),
)
try:
    import bot.gemini_helper  # noqa: F401
except Exception:
    sys.modules.setdefault("bot.gemini_helper", types.SimpleNamespace(predict_hs_code=lambda *a, **k: ""))

from bot.automation import (
    _build_m060800_item_payload,
    _build_m060800_next_payload,
    _build_m060900_weight_payload,
    _build_m061000_register_payload,
    _build_m061100_print_payload,
    _build_m061101_completed_payload,
    _build_result_record,
    _build_failure_record,
    _build_struts_submit,
    _choose_label_flow_command,
    _classify_address_error,
    _diagnose_address_payload,
    _extract_preferred_submit_command,
    _extract_pdf_download_url,
    _extract_submit_command_for_label,
    _html_for_playwright_form,
    _has_m060800_item_book_warning,
    _iter_content_items,
    _format_addr_to_bean_name,
    _prepare_batch_hs_codes,
    _prepare_addr_to_bean_recipient_fields,
    _select_bilingual_english_address_segment,
    _select_preferred_recipient_name,
    _split_addr_to_bean_address_lines,
    _resolve_addr_country_value,
    _validate_required_hs_codes,
    _parse_forms,
    _pick_form,
    _select_option_value,
    _shipping_profile,
    _summarize_error_text,
    _summarize_field_context,
    _summarize_forms,
    _summarize_m060800_item_state,
    _summarize_submit_commands,
    _with_base_href,
    _shipment_log_qualifier,
    run_automation,
)


class AutomationHtmlTests(unittest.TestCase):
    def test_prepare_recipient_fields_moves_pccc_from_name_to_address(self):
        row = {
            "Shipping Name": "kim sang woo (PCCC:P210006411542)",
            "Shipping Street": "123 Gangnam-daero",
        }

        fields = _prepare_addr_to_bean_recipient_fields(row)

        self.assertEqual(fields["name"], "kim sang woo")
        self.assertEqual(fields["address_line"], "123 Gangnam-daero PCCC:P210006411542")
        self.assertEqual(fields["recipient_id"], "PCCC:P210006411542")

    def test_prepare_recipient_fields_moves_prc_id_from_name_to_address(self):
        row = {
            "Shipping Name": "Maria Silva (PRC ID:12345678901)",
            "Shipping Street": "Rua Um 20",
        }

        fields = _prepare_addr_to_bean_recipient_fields(row)

        self.assertEqual(fields["name"], "Maria Silva")
        self.assertEqual(fields["address_line"], "Rua Um 20 PRC ID:12345678901")
        self.assertEqual(fields["recipient_id"], "PRC ID:12345678901")

    def test_format_recipient_name_keeps_order_id_but_removes_pccc(self):
        row = {"Shipping Name": "kim sang woo (PCCC:P210006411542)"}

        self.assertEqual(_format_addr_to_bean_name(row, "imy2036430"), "kim sang woo imy2036430")

    def test_select_preferred_recipient_name_removes_parenthesized_thai_alias(self):
        name = (
            "Teerapan (\u0e18\u0e35\u0e23\u0e1e\u0e31\u0e19\u0e18\u0e38\u0e4c) "
            "Kaewkong (\u0e41\u0e01\u0e49\u0e27\u0e04\u0e07)"
        )

        selected = _select_preferred_recipient_name(name)

        self.assertEqual(selected, "Teerapan Kaewkong")

    def test_select_preferred_recipient_name_keeps_ascii_parenthetical_alias(self):
        self.assertEqual(
            _select_preferred_recipient_name("John (Johnny) Doe"),
            "John (Johnny) Doe",
        )

    def test_format_recipient_name_uses_latin_name_before_order_id(self):
        row = {
            "Shipping Name": (
                "Teerapan (\u0e18\u0e35\u0e23\u0e1e\u0e31\u0e19\u0e18\u0e38\u0e4c) "
                "Kaewkong (\u0e41\u0e01\u0e49\u0e27\u0e04\u0e07)"
            )
        }

        self.assertEqual(
            _format_addr_to_bean_name(row, "imy2038490"),
            "Teerapan Kaewkong imy2038490",
        )

    def test_split_address_lines_keeps_address_2_and_3_within_japan_post_limits(self):
        address = (
            "3518, Changmil-ro, Miryang-si, Gyeongsangnam-do, Republic of Korea, "
            "e-Pyeonhansesang Nanovalley 103-2501 PCCC:P210006411542"
        )

        lines = _split_addr_to_bean_address_lines(address, "Seoul")

        combined = " ".join(
            lines[key]
            for key in ["addrToBean.add1", "addrToBean.add2", "addrToBean.add3"]
            if lines[key]
        )
        self.assertLessEqual(len(lines["addrToBean.add1"]), 80)
        self.assertLessEqual(len(lines["addrToBean.add2"]), 80)
        self.assertLessEqual(len(lines["addrToBean.add3"]), 36)
        self.assertIn("Nanovalley 103-2501", combined)
        self.assertIn("PCCC:P210006411542", lines["addrToBean.add3"])
        self.assertNotIn("PCCC:P210006411542", lines["addrToBean.add2"])

    def test_split_short_address_with_recipient_id_keeps_street_in_address_2(self):
        lines = _split_addr_to_bean_address_lines(
            "35 Eonju-ro 30-gil Gangnam-gu Seoul PCCC:P240000629871",
            "",
        )

        self.assertEqual(lines["addrToBean.add1"], "")
        self.assertEqual(lines["addrToBean.add2"], "35 Eonju-ro 30-gil Gangnam-gu Seoul")
        self.assertEqual(lines["addrToBean.add3"], "PCCC:P240000629871")

    def test_split_address_lines_without_recipient_id_keeps_street_in_address_2(self):
        lines = _split_addr_to_bean_address_lines("22331 Circle J Ranch Road", "Santa Clarita")

        self.assertEqual(lines["addrToBean.add1"], "")
        self.assertEqual(lines["addrToBean.add2"], "22331 Circle J Ranch Road")
        self.assertEqual(lines["addrToBean.add3"], "Santa Clarita")

    def test_split_address_lines_normalizes_imy2038230_for_japan_post_width(self):
        address = "Aleea Locotenent Gheorghe Stâlpeanu 11‚ bl 8‚ sc B‚ et 4‚ ap 38‚ interfon 38"

        lines = _split_addr_to_bean_address_lines(address, "București")

        self.assertEqual(
            lines["addrToBean.add2"],
            "Aleea Locotenent Gheorghe Stalpeanu 11, bl 8, sc B, et 4, ap 38, interfon 38",
        )
        self.assertEqual(lines["addrToBean.add3"], "Bucuresti")
        self.assertLessEqual(
            sum(1 if ord(char) < 128 else 2 for char in lines["addrToBean.add2"]),
            80,
        )

    def test_split_address_lines_preserves_non_latin_text(self):
        lines = _split_addr_to_bean_address_lines("台北市 Stâlpeanu‚ 1", "台北市")

        self.assertEqual(lines["addrToBean.add2"], "台北市 Stalpeanu, 1")
        self.assertEqual(lines["addrToBean.add3"], "台北市")

    def test_split_address_lines_uses_weighted_limits_for_non_ascii_text(self):
        address = "台" * 41

        lines = _split_addr_to_bean_address_lines(address)

        self.assertEqual(lines["addrToBean.add2"], "台" * 40)
        self.assertEqual(lines["addrToBean.add3"], "台")
        for key, limit in (
            ("addrToBean.add1", 80),
            ("addrToBean.add2", 80),
            ("addrToBean.add3", 36),
        ):
            width = sum(1 if ord(char) < 128 else 2 for char in lines[key])
            self.assertLessEqual(width, limit)

    def test_split_address_lines_rejects_unrepresentable_overflow(self):
        with self.assertRaisesRegex(ValueError, "日本郵局收件地址過長"):
            _split_addr_to_bean_address_lines("A" * 197)

    def test_diagnose_address_payload_separates_thai_and_length_evidence(self):
        diagnostics = _diagnose_address_payload(
            "กรุงเทพมหานคร " + "A" * 180,
            "Bangkok",
        )

        self.assertGreater(diagnostics["raw_chars"], 180)
        self.assertGreater(diagnostics["normalized_width"], 80)
        self.assertGreater(diagnostics["thai_codepoints"], 0)
        self.assertTrue(diagnostics["capacity_exceeded"])

    def test_select_bilingual_english_address_segment_removes_duplicate_city(self):
        raw = (
            "\u0e04\u0e2d\u0e19\u0e42\u0e14\u0e28\u0e38\u0e20\u0e32\u0e25\u0e31\u0e22 1577 \u0e0a\u0e31\u0e49\u0e19 25 \u0e01\u0e23\u0e38\u0e07\u0e40\u0e17\u0e1e\u0e2f 10160\u201a "
            "Supalai Verada Condo\u201a Phasi Charoen Station\u201a Room 1577\u201a "
            "25th Floor\u201a Building B\u201a Petchkasem Road\u201a Bang Wa Subdistrict\u201a "
            "Phasi Charoen District\u201a Bangkok 10160"
        )
        city = "Bang Wa Subdistrict\u201a Phasi Charoen District\u201a Bangkok"

        selected = _select_bilingual_english_address_segment(raw, city, "10160")

        self.assertIn("Supalai Verada Condo", selected)
        self.assertIn("Petchkasem Road", selected)
        self.assertNotIn("Bang Wa Subdistrict", selected)
        self.assertNotIn("\u0e04\u0e2d\u0e19\u0e42\u0e14", selected)

    def test_select_bilingual_address_segment_fails_closed_without_strong_overlap(self):
        raw = "\u0e01\u0e23\u0e38\u0e07\u0e40\u0e17\u0e1e 10160\u201a Main Street, London"

        self.assertEqual(
            _select_bilingual_english_address_segment(raw, "Bangkok", "10160"),
            raw,
        )

    def test_prepare_bilingual_address_can_be_packed_with_long_city(self):
        raw = (
            "\u0e04\u0e2d\u0e19\u0e42\u0e14\u0e28\u0e38\u0e20\u0e32\u0e25\u0e31\u0e22 1577 \u0e0a\u0e31\u0e49\u0e19 25 \u0e01\u0e23\u0e38\u0e07\u0e40\u0e17\u0e1e\u0e2f 10160\u201a "
            "Supalai Verada Condo\u201a Phasi Charoen Station\u201a Room 1577\u201a "
            "25th Floor\u201a Building B\u201a Petchkasem Road\u201a Bang Wa Subdistrict\u201a "
            "Phasi Charoen District\u201a Bangkok 10160"
        )
        city = "Bang Wa Subdistrict\u201a Phasi Charoen District\u201a Bangkok"
        fields = _prepare_addr_to_bean_recipient_fields(
            {"Shipping Street": raw, "Shipping City": city, "Shipping Zip": "10160"}
        )

        lines = _split_addr_to_bean_address_lines(fields["address_line"], city)
        combined = " ".join(lines.values())
        self.assertIn("Supalai Verada Condo", combined)
        self.assertIn("Bang Wa Subdistrict", combined)
        for key, limit in (("addrToBean.add1", 40), ("addrToBean.add2", 80), ("addrToBean.add3", 36)):
            self.assertLessEqual(sum(1 if ord(char) < 128 else 2 for char in lines[key]), limit)

    def test_classify_address_error_distinguishes_length_and_remote_character_rejection(self):
        self.assertEqual(
            _classify_address_error(ValueError("日本郵局收件地址過長：超過容量")),
            "address_too_long",
        )
        self.assertEqual(
            _classify_address_error(
                RuntimeError("M060505 validation"),
                response_text="地址過長，請重新輸入",
                status_code=200,
            ),
            "address_too_long",
        )
        self.assertEqual(
            _classify_address_error(
                RuntimeError("M060505 rejected"),
                response_text="Invalid character in address field",
                status_code=200,
            ),
            "address_invalid_character",
        )
        self.assertEqual(
            _classify_address_error(
                RuntimeError("M060505 rejected"),
                response_text="Thai characters are not supported",
                status_code=200,
            ),
            "address_invalid_character",
        )

    def test_build_failure_record_keeps_order_and_reason_for_frontend_alert(self):
        result = _build_failure_record(
            {"注文番号(貼上原始資料)": "imy2038490", "Shipping Name": "Receiver"},
            "imy2038490",
            ValueError("日本郵局收件地址過長：超過容量"),
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["order_id"], "imy2038490")
        self.assertEqual(result["reason_code"], "address_too_long")
        self.assertIn("地址過長", result["reason_text"])

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

    def test_resolve_addr_country_value_uses_eu_for_europe_destination(self):
        html = """
        <form action="M060505.do" method="post">
          <select name="addrToBean.couCode">
            <option value="">Select</option>
            <option value="EU">Europe</option>
            <option value="US">United States</option>
          </select>
        </form>
        """
        form = _parse_forms(html)[0]

        self.assertEqual(
            _resolve_addr_country_value(form, {"addrToBean.couCode": "US"}, "GERMANY", "EU"),
            "EU",
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

    def test_parse_forms_keeps_blank_first_select_option_as_browser_default(self):
        html = """
        <form action="M060800.do" method="post">
          <select name="itemBean.couCd">
            <option value=""></option>
            <option value="AF">AFGHANISTAN</option>
            <option value="JP">JAPAN</option>
          </select>
        </form>
        """

        form = _parse_forms(html)[0]

        self.assertEqual(form["fields"]["itemBean.couCd"], "")

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

    def test_build_m060800_item_payload_sets_epacket_air_trans_type(self):
        html = """
        <form action="/mypage/M060800.do" method="post">
          <input type="hidden" name="command" value="">
          <input type="hidden" name="csrfToken" value="token">
          <input type="hidden" name="shippingBean.sendType" value="">
          <input type="hidden" name="shippingBean.transType" value="">
          <input name="itemBean.pkg" value="">
          <input name="itemBean.cost.value" value="">
          <input name="itemBean.num.value" value="">
          <select name="itemBean.curUnit"><option value="USD">USD</option></select>
          <button type="button" id="ID_SENDTYPE_BTN_EPACK_LITE" onclick="chgSendTypeBtn(8);">
            <img alt="International Air Packet">
          </button>
          <button type="button" id="ID_TRANSTYPE_BTN_AIR" onclick="chgTransTypeBtn(1);">
            <img alt="AIR">
          </button>
        </form>
        """
        row = {
            "郵局運送方式(複數商品請自行確認是否走小包)": "ePacket",
            "內容物1": "Pouch TRSN6161",
            "申告金額1": "10.11",
            "數量1": "1",
        }

        _, payload = _build_m060800_item_payload(
            html,
            "https://www.int-mypage.post.japanpost.jp/mypage/M060800.do",
            row,
        )

        self.assertEqual(payload["shippingBean.sendType"], "8")
        self.assertEqual(payload["shippingBean.transType"], "1")

    def test_build_m060800_item_payload_preserves_existing_item_count_fields(self):
        html = """
        <form action="/mypage/M060800.do" method="post">
          <input type="hidden" name="command" value="">
          <input type="hidden" name="csrfToken" value="token">
          <input type="hidden" name="shippingBean.sendType" value="8">
          <input type="hidden" name="shippingBean.transType" value="1">
          <input type="hidden" name="shippingBean.itemList[0].no.value" value="-1">
          <input type="hidden" name="cost.value" value="10.11">
          <input type="hidden" name="curUnit" value="USD">
          <input type="hidden" name="printCurUnit" value="USD">
          <input name="itemCount" value="1">
          <input type="hidden" name="shippingBean.itemList[1].no.value" value="-2">
          <input type="hidden" name="cost.value" value="6.44">
          <input type="hidden" name="curUnit" value="USD">
          <input type="hidden" name="printCurUnit" value="USD">
          <input name="itemCount" value="1">
          <input name="itemBean.pkg" value="">
          <input name="itemBean.cost.value" value="">
          <input name="itemBean.num.value" value="">
          <select name="itemBean.curUnit"><option value="USD">USD</option></select>
        </form>
        """
        row = {
            "郵局運送方式(複數商品請自行確認是否走小包)": "ePacket",
            "內容物3": "Car Tissue Holder TRSN0285",
            "申告金額3": "6.62",
            "數量3": "1",
        }

        _, payload = _build_m060800_item_payload(
            html,
            "https://www.int-mypage.post.japanpost.jp/mypage/M060800.do",
            row,
            item_index=3,
        )

        payload_pairs = list(payload.items()) if hasattr(payload, "items") else list(payload)

        self.assertEqual([name for name, _ in payload_pairs].count("itemCount"), 2)
        self.assertEqual([value for name, value in payload_pairs if name == "itemCount"], ["1", "1"])
        self.assertEqual([name for name, _ in payload_pairs].count("cost.value"), 2)
        self.assertEqual(payload["itemBean.num.value"], "1")

    def test_build_m060800_next_payload_uses_regist_after_item_confirm(self):
        html = """
        <form action="/mypage/M060800.do" method="post">
          <input type="hidden" name="command" value="">
          <input type="hidden" name="csrfToken" value="token2">
          <input type="hidden" name="shippingBean.sendType" value="8">
          <input type="hidden" name="shippingBean.transType" value="">
          <input type="hidden" name="shippingBean.pkgType" value="0">
          <input name="shippingBean.pkgTotalPrice.value" value="">
          <input name="itemBean.pkg" value="Pillow TRSN9842">
          <input name="itemBean.cost.value" value="1.55">
          <input name="itemBean.num.value" value="1">
          <select name="itemBean.curUnit"><option value="JPY">JPY</option><option value="USD">USD</option></select>
          <select name="itemBean.couCd"><option value=""></option><option value="AF">AFGHANISTAN</option></select>
          <input name="itemBean.hsCode.value" value="940490">
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
        self.assertEqual(payload["itemBean.pkg"], "")
        self.assertEqual(payload["itemBean.cost.value"], "")
        self.assertEqual(payload["itemBean.num.value"], "")
        self.assertEqual(payload["itemBean.curUnit"], "JPY")
        self.assertEqual(payload["itemBean.couCd"], "")
        self.assertEqual(payload["itemBean.hsCode.value"], "")
        self.assertEqual(payload["command"], "regist")
        self.assertEqual(payload["method:regist"], "")

    def test_build_m060800_next_payload_preserves_browser_default_selects(self):
        html = """
        <form action="/mypage/M060800.do" method="post">
          <input type="hidden" name="command" value="">
          <input type="hidden" name="csrfToken" value="token">
          <input type="hidden" name="shippingBean.sendType" value="5">
          <input type="hidden" name="shippingBean.transType" value="1">
          <input type="hidden" name="shippingBean.pkgType" value="0">
          <input type="hidden" name="shippingBean.itemList[0].no.value" value="-1">
          <input type="hidden" name="cost.value" value="1.55">
          <input type="hidden" name="curUnit" value="USD">
          <input type="hidden" name="printCurUnit" value="USD">
          <input name="itemCount" value="1">
          <input name="itemBean.pkg" value="">
          <input name="itemBean.cost.value" value="">
          <input name="itemBean.num.value" value="">
          <input name="itemBean.weight.value" value="">
          <select name="itemBean.curUnit">
            <option value="JPY">JPY</option>
            <option value="USD">USD</option>
          </select>
          <select name="itemBean.couCd">
            <option value="" selected></option>
            <option value="AF">AFGHANISTAN</option>
          </select>
          <input name="itemBean.hsCode" value="">
        </form>
        """

        _, payload = _build_m060800_next_payload(
            html,
            "https://www.int-mypage.post.japanpost.jp/mypage/M060800.do",
            {},
        )

        self.assertEqual(payload["itemBean.pkg"], "")
        self.assertEqual(payload["itemBean.cost.value"], "")
        self.assertEqual(payload["itemBean.num.value"], "")
        self.assertEqual(payload["itemBean.weight.value"], "")
        self.assertEqual(payload["itemBean.curUnit"], "JPY")
        self.assertEqual(payload["itemBean.couCd"], "")
        self.assertEqual(payload["itemBean.hsCode"], "")

    def test_build_m060800_next_payload_preserves_duplicate_item_list_fields(self):
        html = """
        <form action="/mypage/M060800.do" method="post">
          <input type="hidden" name="command" value="">
          <input type="hidden" name="csrfToken" value="token">
          <input type="hidden" name="shippingBean.sendType" value="5">
          <input type="hidden" name="shippingBean.transType" value="1">
          <input type="hidden" name="shippingBean.pkgType" value="0">
          <input type="hidden" name="shippingBean.itemList[0].no.value" value="-1">
          <input type="hidden" name="cost.value" value="1.55">
          <input type="hidden" name="curUnit" value="USD">
          <input type="hidden" name="printCurUnit" value="USD">
          <input name="itemCount" value="1">
          <input type="hidden" name="shippingBean.itemList[1].no.value" value="-2">
          <input type="hidden" name="cost.value" value="2.55">
          <input type="hidden" name="curUnit" value="USD">
          <input type="hidden" name="printCurUnit" value="USD">
          <input name="itemCount" value="1">
          <input name="shippingBean.pkgTotalPrice.value" value="">
          <input name="itemBean.pkg" value="">
          <input name="itemBean.cost.value" value="">
          <input name="itemBean.num.value" value="">
          <select name="itemBean.curUnit"><option value="JPY">JPY</option><option value="USD">USD</option></select>
          <select name="itemBean.couCd"><option value="" selected></option><option value="AF">AFGHANISTAN</option></select>
          <input type="checkbox" name="ShippingBean.danger" value="1">
        </form>
        """

        _, payload = _build_m060800_next_payload(
            html,
            "https://www.int-mypage.post.japanpost.jp/mypage/M060800.do",
            {},
        )

        self.assertEqual(payload["ShippingBean.danger"], "1")
        self.assertEqual([name for name, _ in payload].count("cost.value"), 2)
        self.assertEqual([name for name, _ in payload].count("curUnit"), 2)
        self.assertEqual([name for name, _ in payload].count("printCurUnit"), 2)
        self.assertEqual([name for name, _ in payload].count("itemCount"), 2)
        self.assertIn("items=[0, 1]", _summarize_m060800_item_state(html))

    def test_build_m060800_next_payload_removes_blank_pending_item_fields_for_multi_item(self):
        html = """
        <form action="/mypage/M060800.do" method="post">
          <input type="hidden" name="command" value="">
          <input type="hidden" name="csrfToken" value="token">
          <input type="hidden" name="shippingBean.sendType" value="8">
          <input type="hidden" name="shippingBean.transType" value="">
          <input type="hidden" name="shippingBean.pkgType" value="0">
          <input type="hidden" name="shippingBean.itemList[0].no.value" value="-1">
          <input type="hidden" name="cost.value" value="10.11">
          <input type="hidden" name="curUnit" value="USD">
          <input type="hidden" name="printCurUnit" value="USD">
          <input name="itemCount" value="1">
          <input type="hidden" name="shippingBean.itemList[1].no.value" value="-2">
          <input type="hidden" name="cost.value" value="6.44">
          <input type="hidden" name="curUnit" value="USD">
          <input type="hidden" name="printCurUnit" value="USD">
          <input name="itemCount" value="1">
          <input type="hidden" name="shippingBean.itemList[2].no.value" value="-3">
          <input type="hidden" name="cost.value" value="6.62">
          <input type="hidden" name="curUnit" value="USD">
          <input type="hidden" name="printCurUnit" value="USD">
          <input name="itemCount" value="1">
          <input name="shippingBean.pkgTotalPrice.value" value="">
          <input name="itemBean.pkg" value="">
          <input name="itemBean.cost.value" value="">
          <input name="itemBean.num.value" value="">
          <select name="itemBean.curUnit"><option value="JPY">JPY</option><option value="USD">USD</option></select>
          <select name="itemBean.couCd"><option value="" selected></option><option value="TW">TAIWAN</option></select>
          <input name="itemBean.hsCode" value="">
        </form>
        """

        _, payload = _build_m060800_next_payload(
            html,
            "https://www.int-mypage.post.japanpost.jp/mypage/M060800.do",
            {"訂單合計申告金額(JPY)": "3750"},
        )

        payload_names = [name for name, _ in payload]
        self.assertNotIn("itemBean.pkg", payload_names)
        self.assertNotIn("itemBean.cost.value", payload_names)
        self.assertNotIn("itemBean.num.value", payload_names)
        self.assertNotIn("itemBean.curUnit", payload_names)
        self.assertNotIn("itemBean.couCd", payload_names)
        self.assertNotIn("itemBean.hsCode", payload_names)
        self.assertEqual(payload["shippingBean.pkgTotalPrice.value"], "3750")
        self.assertEqual([name for name, _ in payload].count("cost.value"), 3)
        self.assertEqual(payload["method:regist"], "")

    def test_build_m060800_next_payload_keeps_epacket_air_trans_type(self):
        html = """
        <form action="/mypage/M060800.do" method="post">
          <input type="hidden" name="command" value="">
          <input type="hidden" name="csrfToken" value="token2">
          <input type="hidden" name="shippingBean.sendType" value="8">
          <input type="hidden" name="shippingBean.transType" value="">
          <input type="hidden" name="shippingBean.pkgType" value="0">
          <input type="hidden" name="shippingBean.itemList[0].no.value" value="-1">
          <input type="hidden" name="cost.value" value="10.11">
          <input name="shippingBean.pkgTotalPrice.value" value="3750">
          <input name="itemBean.pkg" value="">
          <input name="itemBean.cost.value" value="">
          <input name="itemBean.num.value" value="">
          <button type="button" id="ID_SENDTYPE_BTN_EPACK_LITE" onclick="chgSendTypeBtn(8);">
            <img alt="International Air Packet">
          </button>
          <button type="button" id="ID_TRANSTYPE_BTN_AIR" onclick="chgTransTypeBtn(1);">
            <img alt="AIR">
          </button>
        </form>
        """

        _, payload = _build_m060800_next_payload(
            html,
            "https://www.int-mypage.post.japanpost.jp/mypage/M060800.do",
            {"郵局運送方式(複數商品請自行確認是否走小包)": "ePacket"},
        )

        self.assertEqual(payload["shippingBean.transType"], "1")

    def test_build_m060800_next_payload_sets_over_confirm_for_multi_item_warning(self):
        html = """
        <form action="/mypage/M060800.do" method="post">
          <input type="hidden" name="command" value="">
          <input type="hidden" name="csrfToken" value="token">
          <input type="hidden" name="shippingBean.overConfirm" value="">
          <input type="hidden" name="shippingBean.sendType" value="8">
          <input type="hidden" name="shippingBean.transType" value="">
          <input type="hidden" name="shippingBean.pkgType" value="0">
          <input type="hidden" name="shippingBean.itemList[0].no.value" value="-1">
          <input type="hidden" name="cost.value" value="10.11">
          <input type="hidden" name="shippingBean.itemList[1].no.value" value="-2">
          <input type="hidden" name="cost.value" value="6.44">
          <input type="hidden" name="shippingBean.itemList[2].no.value" value="-3">
          <input type="hidden" name="cost.value" value="6.62">
          <input name="shippingBean.pkgTotalPrice.value" value="">
          <input name="itemBean.pkg" value="">
          <input name="itemBean.cost.value" value="">
          <input name="itemBean.num.value" value="">
        </form>
        """

        _, payload = _build_m060800_next_payload(
            html,
            "https://www.int-mypage.post.japanpost.jp/mypage/M060800.do",
            {"訂單合計申告金額(JPY)": "3750"},
        )

        self.assertEqual(payload["shippingBean.overConfirm"], "true")

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

    def test_shipping_profile_detects_ems_goods(self):
        row = {"郵局運送方式(複數商品請自行確認是否走小包)": "EMS"}

        self.assertEqual(_shipping_profile(row), "ems_goods")

    def test_build_m060800_item_payload_selects_ems_goods_not_business_papers(self):
        html = """
        <form action="/mypage/M060800.do" method="post">
          <input type="hidden" name="command" value="">
          <input type="hidden" name="csrfToken" value="token">
          <input type="hidden" name="shippingBean.sendType" value="0">
          <input type="hidden" name="shippingBean.transType" value="">
          <input type="hidden" name="shippingBean.pkgType" value="0">
          <button type="button" id="ID_SENDTYPE_BTN_DOC" onclick="chgSendTypeBtn(0);">
            <img src="images/mypage_en/sendType/DOC_W.PNG" alt="EMS(Business Papers)">
          </button>
          <button type="button" id="ID_SENDTYPE_BTN_PKG" onclick="chgSendTypeBtn(1);">
            <img src="images/mypage_en/sendType/PKG.PNG" alt="EMS(Goods)">
          </button>
          <input name="itemBean.pkg" value="">
          <input name="itemBean.cost.value" value="">
          <input name="itemBean.num.value" value="">
          <select name="itemBean.curUnit"><option value="USD">USD</option></select>
        </form>
        """
        row = {
            "郵局運送方式(複數商品請自行確認是否走小包)": "EMS",
            "內容物1": "Skin Care Device(without lithium battery) TRSN7068",
            "申告金額1": "23.21",
            "數量1": "1",
        }

        _, payload = _build_m060800_item_payload(
            html,
            "https://www.int-mypage.post.japanpost.jp/mypage/M060505.do",
            row,
            is_eu=False,
        )

        self.assertEqual(payload["shippingBean.sendType"], "1")
        self.assertEqual(payload["itemBean.pkg"], "Skin Care Device(without lithium battery) TRSN7068")

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

    def test_iter_content_items_uses_legacy_single_item_fallback_fields(self):
        row = {
            "郵局內容物": "Pillow TRSN3392",
            "郵局申告金額(USD)": "10.98",
            "数量": "1",
            "內容物1": "",
            "申告金額1": "",
            "數量1": "",
        }

        self.assertEqual(
            _iter_content_items(row),
            [{"index": "1", "pkg": "Pillow TRSN3392", "cost": "10.98", "num": "1"}],
        )

    def test_iter_content_items_skips_canceled_items_and_preserves_indexes(self):
        row = {
            "內容物1": "Face Primer TRSN8666",
            "申告金額1": "4.85",
            "數量1": "0",
            "內容物2": "Canceled Blank Item",
            "申告金額2": "2.00",
            "數量2": "",
            "內容物3": "Canceled Negative Item",
            "申告金額3": "3.00",
            "數量3": "-1",
            "內容物4": "Beads TRSN9960",
            "申告金額4": "1.31",
            "數量4": "4",
        }

        self.assertEqual(
            _iter_content_items(row),
            [{"index": "4", "pkg": "Beads TRSN9960", "cost": "1.31", "num": "4"}],
        )

    def test_iter_content_items_rejects_invalid_quantities(self):
        for quantity in ("abc", "1.5"):
            with self.subTest(quantity=quantity):
                with self.assertRaisesRegex(ValueError, "內容物1.*數量格式錯誤"):
                    _iter_content_items({"內容物1": "Item", "申告金額1": "1", "數量1": quantity})

    def test_iter_content_items_returns_empty_when_every_item_is_canceled(self):
        row = {
            "內容物1": "Canceled Blank Item",
            "數量1": "",
            "內容物2": "Canceled Zero Item",
            "數量2": "0",
        }

        self.assertEqual(_iter_content_items(row), [])

    def test_prepare_batch_hs_codes_resolves_each_required_item_before_flow(self):
        calls = []

        def predictor(item_name, *, required_length=6, country="", country_code="", log_cb=None):
            calls.append((item_name, required_length, country, country_code))
            return {
                ("Mask", 6): "330499",
                ("Pillow", 6): "940490",
                ("Gift", 10): "9503009999",
            }[(item_name, required_length)]

        rows = [
            {
                "注文番号(貼上原始資料)": "DE-1",
                "收件人國家": "GERMANY",
                "內容物1": "Mask",
                "數量1": "1",
                "內容物2": "Pillow",
                "數量2": "1",
            },
            {
                "注文番号(貼上原始資料)": "IE-1",
                "收件人國家": "IRELAND",
                "內容物1": "Gift",
                "數量1": "1",
            },
        ]

        codes = _prepare_batch_hs_codes(
            rows,
            {"GERMANY": "EU", "IRELAND": "EU"},
            predictor=predictor,
        )

        self.assertEqual(codes["DE-1"], {"1": "330499", "2": "940490"})
        self.assertEqual(codes["IE-1"], {"1": "9503009999"})
        self.assertEqual(
            calls,
            [
                ("Mask", 6, "GERMANY", "EU"),
                ("Gift", 10, "IRELAND", "EU"),
            ],
        )

    def test_prepare_batch_hs_codes_resolves_country_abbreviation_from_dictionary(self):
        calls = []

        def predictor(item_name, *, required_length=6, country="", country_code="", log_cb=None):
            calls.append((item_name, required_length, country, country_code))
            return "940490"

        rows = [
            {
                "注文番号(貼上原始資料)": "WhoWht-DE",
                "收件人國家": "DE",
                "內容物1": "Wooden Ornament",
                "申告金額1": "23.25",
                "數量1": "1",
            }
        ]

        codes = _prepare_batch_hs_codes(rows, {}, predictor=predictor)

        self.assertEqual(codes["WhoWht-DE"], {"1": "940490"})
        self.assertEqual(calls, [("Wooden Ornament", 6, "DE", "EU")])

    def test_prepare_batch_hs_codes_dedupes_same_item_for_same_destination_rule(self):
        calls = []

        def predictor(item_name, *, required_length=6, country="", country_code="", log_cb=None):
            calls.append((item_name, required_length, country, country_code))
            return "330499"

        rows = [
            {
                "注文番号(貼上原始資料)": "DE-1",
                "收件人國家": "GERMANY",
                "內容物1": "Mask",
                "數量1": "1",
            },
            {
                "注文番号(貼上原始資料)": "DE-2",
                "收件人國家": "GERMANY",
                "內容物1": "Mask",
                "數量1": "1",
            },
        ]

        codes = _prepare_batch_hs_codes(
            rows,
            {"GERMANY": "EU"},
            predictor=predictor,
        )

        self.assertEqual(codes["DE-1"], {"1": "330499"})
        self.assertEqual(codes["DE-2"], {"1": "330499"})
        self.assertEqual(calls, [("Mask", 6, "GERMANY", "EU")])

    def test_prepare_batch_hs_codes_prefers_manual_hscode_columns(self):
        calls = []

        def predictor(item_name, *, required_length=6, country="", country_code="", log_cb=None):
            calls.append(item_name)
            return "000000"

        rows = [
            {
                "注文番号(貼上原始資料)": "DE-1",
                "收件人國家": "GERMANY",
                "內容物1": "Mask",
                "數量1": "1",
                "HSCode1": "HS:3304.99",
            }
        ]

        codes = _prepare_batch_hs_codes(
            rows,
            {"GERMANY": "EU"},
            predictor=predictor,
        )

        self.assertEqual(codes["DE-1"], {"1": "330499"})
        self.assertEqual(calls, [])

    def test_validate_required_hs_codes_reports_missing_without_raising(self):
        items = [
            {"index": "1", "pkg": "Facial Mask"},
            {"index": "2", "pkg": "Unknown Item"},
        ]

        missing = _validate_required_hs_codes(
            items,
            is_eu=True,
            hs_codes_by_item={"1": "330499"},
        )

        self.assertEqual(missing, ["item=2/2, pkg=Unknown Item"])

    def test_validate_required_hs_codes_allows_non_eu_without_codes(self):
        items = [{"index": "1", "pkg": "Unknown Item"}]

        _validate_required_hs_codes(items, is_eu=False, hs_codes_by_item={})

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

    def test_summarize_field_context_extracts_attrs_and_nearby_label(self):
        html = """
        <html><body>
          <tr>
            <th>Customs reference number</th>
            <td><input id="M060505_addrToBean_sortNum" name="addrToBean.sortNum"
                title="Enter PCCC" maxlength="13" value=""></td>
          </tr>
        </body></html>
        """

        summary = _summarize_field_context(html, ["addrToBean.sortNum"])

        self.assertIn("addrToBean.sortNum", summary)
        self.assertIn("Customs reference number", summary)
        self.assertIn("title=Enter PCCC", summary)
        self.assertIn("maxlength=13", summary)

    def test_detects_m060800_item_book_warning_markup(self):
        html = """
        <html><body>
          <div id="itemWarnDialog">
            Number of items in content list exceeds allowable limit.
            Edited data is not saved in content list.
            <input id="warningMsgOff" type="checkbox">
          </div>
          <script>if (!isStopAlert('ItemBookAlert')) $('#itemWarnDialog').dialog('open');</script>
        </body></html>
        """

        self.assertTrue(_has_m060800_item_book_warning(html))
        self.assertFalse(_has_m060800_item_book_warning("<html><body>M060800</body></html>"))

    def test_build_m060900_weight_payload_sets_total_weight_and_uses_regist(self):
        html = """
        <form action="/mypage/M060900.do" method="post">
          <input type="hidden" name="command" value="">
          <input type="hidden" name="csrfToken" value="token">
          <input name="emsNo.value" value="">
          <input name="shippingBean.sendDate.YMD" value="2026/06/18">
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
        self.assertEqual(payload["shippingBean.totalWeight.value"], "100")
        self.assertEqual(payload["shippingBean.cost.value"], "23.41")
        self.assertNotIn("command", payload)
        self.assertEqual(payload["method:regist"], "")

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
        self.assertNotIn("command", payload)
        self.assertEqual(payload["method:regist"], "")

    def test_build_m060900_weight_payload_preserves_blank_postal_parcel_counts_and_weight(self):
        html = """
        <form action="/mypage/M060900.do" method="post">
          <input type="hidden" name="command" value="">
          <input type="hidden" name="csrfToken" value="token">
          <input name="shippingBean.num.value" value="">
          <input name="shippingBean.totalNum.value" value="">
          <input name="shippingBean.totalWeight.value" value="">
          <input name="shippingBean.cost.value" value="">
        </form>
        """

        _, payload = _build_m060900_weight_payload(
            html,
            "https://www.int-mypage.post.japanpost.jp/mypage/M060900.do",
            weight_grams="100",
        )

        self.assertEqual(payload["shippingBean.num.value"], "")
        self.assertEqual(payload["shippingBean.totalNum.value"], "")
        self.assertEqual(payload["shippingBean.totalWeight.value"], "")
        self.assertNotIn("command", payload)
        self.assertEqual(payload["method:regist"], "")

    def test_build_m060900_weight_payload_selects_economical_failed_delivery_route(self):
        html = """
        <form action="/mypage/M060900.do" method="post">
          <input type="hidden" name="command" value="">
          <input type="hidden" name="csrfToken" value="token">
          <input name="shippingBean.num.value" value="">
          <input name="shippingBean.totalNum.value" value="">
          <input name="shippingBean.totalWeight.value" value="">
          <input type="radio" name="shippingBean.senderInstruction" value="1" checked>
          <input type="radio" name="shippingBean.fwTransType" value="1" checked>
          <input type="radio" name="shippingBean.fwTransType" value="4">
          <input type="radio" name="shippingBean.invPrintType" value="0" checked>
          <input type="hidden" name="shippingBean.noCm" value="true">
        </form>
        """

        _, payload = _build_m060900_weight_payload(
            html,
            "https://www.int-mypage.post.japanpost.jp/mypage/M060900.do",
            weight_grams="100",
        )

        self.assertEqual(payload["shippingBean.senderInstruction"], "1")
        self.assertEqual(payload["shippingBean.fwTransType"], "4")
        self.assertEqual(payload["shippingBean.invPrintType"], "0")
        self.assertEqual(payload["shippingBean.noCm"], "true")
        self.assertNotIn("command", payload)
        self.assertEqual(payload["method:regist"], "")

    def test_build_m060900_weight_payload_omits_disabled_insurance_checkbox_value(self):
        html = """
        <form action="/mypage/M060900.do" method="post">
          <input type="hidden" name="command" value="">
          <input type="hidden" name="csrfToken" value="token">
          <input name="shippingBean.num.value" value="">
          <input name="shippingBean.totalNum.value" value="">
          <input name="shippingBean.totalWeight.value" value="">
          <input type="checkbox" name="shippingBean.withInsurance" value="true" disabled>
          <input type="hidden" name="__checkbox_shippingBean.withInsurance" value="true">
          <input name="shippingBean.damges" value="">
          <input name="shippingBean.insure.value" value="">
        </form>
        """

        _, payload = _build_m060900_weight_payload(
            html,
            "https://www.int-mypage.post.japanpost.jp/mypage/M060900.do",
            weight_grams="100",
        )

        self.assertNotIn("shippingBean.withInsurance", payload)
        self.assertEqual(payload["__checkbox_shippingBean.withInsurance"], "true")
        self.assertEqual(payload["shippingBean.damges"], "")
        self.assertEqual(payload["shippingBean.insure.value"], "")
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
            "內容物1": "Water Bottle TRSN9767",
            "申告金額1": "3.14",
            "數量1": "1",
        }

        result = _build_result_record(row, "WhoWhy1566", "EN521206692JP")

        self.assertEqual(result["name"], "Klas Eklof")
        self.assertEqual(result["order_id"], "WhoWhy1566")
        self.assertEqual(result["tracking"], "EN521206692JP")
        self.assertEqual(result["country"], "UNITED STATES OF AMERICA")
        self.assertEqual(result["country_raw"], "UNITED STATES OF AMERICA")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["items_expected"], 1)
        self.assertEqual(result["items_submitted"], 1)
        self.assertEqual(result["shipment_role"], "primary")
        self.assertRegex(result["date"], r"^\d{4}-\d{2}-\d{2}$")

    def test_result_records_preserve_explicit_additional_shipment_role(self):
        row = {
            "Shipping Name": "Synthetic Recipient",
            "_shipment_role": "additional",
        }

        success = _build_result_record(row, "Synthetic-Order-1", "LX123456789JP")
        failure = _build_failure_record(row, "Synthetic-Order-1", RuntimeError("synthetic failure"))

        self.assertEqual(success["shipment_role"], "additional")
        self.assertEqual(failure["shipment_role"], "additional")

    def test_failure_record_defaults_legacy_shipment_role_to_primary(self):
        failure = _build_failure_record({}, "Synthetic-Order-2", RuntimeError("synthetic failure"))

        self.assertEqual(failure["shipment_role"], "primary")

    def test_run_automation_rejects_invalid_role_before_credentials_or_browser(self):
        rows = pandas.DataFrame(
            [{"order_id": "Synthetic-Order-3", "_shipment_role": "unexpected"}]
        )

        with patch(
            "bot.automation._get_jp_post_creds",
            side_effect=AssertionError("credentials must not be read"),
        ):
            with self.assertRaisesRegex(ValueError, "invalid shipment role"):
                run_automation(rows)

    def test_shipment_log_qualifier_includes_transport_and_role(self):
        row = {"TransType": "ePacket", "_shipment_role": "additional"}

        self.assertEqual(
            _shipment_log_qualifier(row),
            "[trans_type=ePacket shipment_role=additional]",
        )

    def test_playwright_success_path_uses_structured_result_record(self):
        from pathlib import Path

        source = Path(__file__).parents[1].joinpath("bot", "automation.py").read_text(encoding="utf-8")
        collection_start = source.index("# ── 收集結果")
        collection_end = source.index("except Exception as e:", collection_start)
        collection_block = source[collection_start:collection_end]

        self.assertIn("results.append(_build_result_record(row, order_id, tracking))", collection_block)
        self.assertNotIn("results.append({", collection_block)

    def test_playwright_item_paths_do_not_drop_items_after_four(self):
        from pathlib import Path

        source = Path(__file__).parents[1].joinpath("bot", "automation.py").read_text(encoding="utf-8")

        self.assertNotIn("_iter_content_items(row, max_items=4)", source)

    def test_run_automation_only_uses_playwright_html_injection_for_failure_snapshot(self):
        from pathlib import Path

        source = Path(__file__).parents[1].joinpath("bot", "automation.py").read_text(encoding="utf-8")
        body = source.split("def set_content_from_requests", 1)[1]

        self.assertNotIn("set_content_from_requests(", body)
        self.assertIn("def capture_requests_debug_snapshot", source)
        self.assertIn("page.screenshot(path=str(png_path), full_page=True)", source)
        self.assertIn("upload_file_to_drive(str(local_path), DRIVE_FOLDER_ID", source)
        self.assertIn("M060800_next_failed", source)


if __name__ == "__main__":
    unittest.main()
