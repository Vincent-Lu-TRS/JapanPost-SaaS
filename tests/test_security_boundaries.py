"""Synthetic authorization and secret-disclosure regressions; no live services."""
import ast
import base64
import hashlib
import hmac
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import auth
from safe_logging import redact_operational_log


ROOT = Path(__file__).resolve().parents[1]


def picking_ui_for_test():
    # Older automation tests install a deliberately partial bot.drive stub at
    # collection time. Keep these UI tests isolated from that import ordering.
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location("features._picking_security_test", ROOT / "features/picking_labels.py")
    module = importlib.util.module_from_spec(spec)
    original_drive = sys.modules.get("bot.drive")
    sys.modules["bot.drive"] = SimpleNamespace(list_drive_files=Mock(), upload_file_to_drive=Mock())
    try:
        spec.loader.exec_module(module)
    finally:
        if original_drive is None:
            sys.modules.pop("bot.drive", None)
        else:
            sys.modules["bot.drive"] = original_drive
    return module


class State(dict):
    __getattr__ = dict.__getitem__
    __setattr__ = dict.__setitem__


class Cookies(dict):
    def set(self, cookie, value, **kwargs):
        self[cookie] = value

    def delete(self, cookie, **kwargs):
        self.pop(cookie, None)


class AuthSecurityTests(unittest.TestCase):
    def setUp(self):
        self.fake_st = SimpleNamespace(
            secrets={}, session_state=State(), user={}, query_params={}, logout=Mock()
        )
        self.patch = patch.object(auth, "st", self.fake_st)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_public_fallback_cannot_sign_admin_sessions(self):
        payload = f"admin@tkrjm.co.jp|{int(time.time()) + 600}|Synthetic^"
        signature = hmac.new(b"jp-saas-fallback-secret", payload.encode(), hashlib.sha256).hexdigest()
        token = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=") + "." + signature
        with patch.object(auth, "_get_client_secret", return_value=""):
            self.assertIsNone(auth._parse_session_token(token))
            with self.assertRaises(ValueError):
                auth._make_session_token("admin@tkrjm.co.jp", "Synthetic", "")

    def test_old_cookie_format_requires_fresh_verified_google_login(self):
        payload = f"admin@tkrjm.co.jp|{int(time.time()) + 600}|Synthetic^"
        signature = hmac.new(b"synthetic-private-secret", payload.encode(), hashlib.sha256).hexdigest()
        token = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=") + "." + signature
        with patch.object(auth, "_get_client_secret", return_value="synthetic-private-secret"):
            self.assertIsNone(auth._parse_session_token(token))

    def test_native_mode_does_not_restore_legacy_cookies_or_handle_legacy_callback(self):
        self.fake_st.session_state.update(authenticated=True, user_email="admin@tkrjm.co.jp")
        self.fake_st.query_params.update(code="synthetic-code", state="synthetic-state")
        with patch.object(auth, "has_native_auth_config", return_value=True), patch.object(
            auth, "_restore_from_cookie"
        ) as restore, patch.object(auth, "exchange_code_for_token") as exchange:
            auth.init_auth_state(Cookies())
            self.assertFalse(self.fake_st.session_state["authenticated"])
            self.assertFalse(auth.handle_oauth_callback(Cookies()))
            restore.assert_not_called()
            exchange.assert_not_called()

    def test_native_claim_requires_verified_email(self):
        for verified in (None, False, "true"):
            with self.subTest(verified=verified):
                self.fake_st.user = dict(is_logged_in=True, email="admin@tkrjm.co.jp", email_verified=verified)
                self.assertFalse(auth.restore_native_auth_state())

    def test_admin_permission_requires_verified_identity_and_ignores_session_email(self):
        with patch.object(auth, "has_native_auth_config", return_value=True):
            for user, expected in (
                ({}, False),
                (dict(is_logged_in=True, email="staff@tkrjm.co.jp", email_verified=True), False),
                (dict(is_logged_in=True, email="ADMIN@TKRJM.CO.JP", email_verified=True), True),
                (dict(is_logged_in=True, email="admin@tkrjm.co.jp", email_verified=False), False),
            ):
                with self.subTest(user=user):
                    self.fake_st.user = user
                    self.fake_st.session_state.update(authenticated=True, user_email="admin@tkrjm.co.jp")
                    self.assertEqual(auth.can_view_diagnostics(), expected)
        self.fake_st.user = {}
        with patch.object(auth, "has_native_auth_config", return_value=False):
            self.assertFalse(auth.can_view_diagnostics())
            self.fake_st.session_state["_verified_auth_email"] = "staff@tkrjm.co.jp"
            self.assertFalse(auth.can_view_diagnostics())

    def test_legacy_valid_signed_cookie_establishes_admin_permission(self):
        with patch.object(auth, "_get_client_secret", return_value="synthetic-private-secret"), patch.object(
            auth, "has_native_auth_config", return_value=False
        ):
            cookie = Cookies({auth.SESSION_COOKIE_NAME: auth._make_session_token("Admin@tkrjm.co.jp", "Synthetic", "")})
            auth.init_auth_state(cookie)
            self.assertTrue(auth.can_view_diagnostics())

    def test_legacy_state_is_browser_bound_and_consumed_once(self):
        cookies = Cookies()
        with patch.object(auth, "_get_client_secret", return_value="synthetic-private-secret"), patch.object(
            auth, "_get_client_id", return_value="synthetic-client-id"
        ):
            _, state = auth.get_login_url(cookies)
            self.assertTrue(state)
            self.fake_st.session_state = State()  # OAuth redirects create a new Streamlit session.
            self.assertFalse(auth._verify_state(state, Cookies()))
            self.assertTrue(auth._verify_state(state, cookies))
            self.assertFalse(auth._verify_state(state, cookies))

    def test_oauth_redirect_reads_server_cookie_before_component_hydration(self):
        class UnhydratedCookies(Cookies):
            def delete(self, cookie, **kwargs):
                # Real component schedules browser deletion, then raises when
                # its initial getAll default has not hydrated self.cookies.
                raise KeyError(cookie)

        with patch.object(auth, "_get_client_secret", return_value="synthetic-private-secret"), patch.object(
            auth, "_get_client_id", return_value="synthetic-client-id"
        ):
            cookies = Cookies()
            _, state = auth.get_login_url(cookies)
            self.fake_st.session_state = State()
            self.fake_st.context = SimpleNamespace(cookies={auth.OAUTH_STATE_COOKIE_NAME: state})
            self.assertTrue(auth._verify_state(state, UnhydratedCookies()))
            self.assertFalse(auth._verify_state(state, UnhydratedCookies()))
            self.fake_st.session_state = State()
            self.fake_st.context = SimpleNamespace(cookies={})
            self.assertFalse(auth._verify_state(state, UnhydratedCookies()))

    def test_legacy_callback_rejects_unverified_google_email(self):
        self.fake_st.query_params.update(code="synthetic-code", state="synthetic-state")
        with patch.object(auth, "has_native_auth_config", return_value=False), patch.object(
            auth, "_verify_state", return_value=True
        ), patch.object(auth, "exchange_code_for_token", return_value={"access_token": "synthetic-token"}), patch.object(
            auth, "get_user_info", return_value={"email": "admin@tkrjm.co.jp", "email_verified": False}
        ):
            auth.handle_oauth_callback()
        self.assertFalse(self.fake_st.session_state.get("authenticated", False))

    def test_verified_legacy_callback_restores_admin_and_staff_without_replaying_state(self):
        for email in ("admin@tkrjm.co.jp", "staff@tkrjm.co.jp"):
            with self.subTest(email=email):
                self.fake_st.session_state = State()
                cookies = Cookies()
                with patch.object(auth, "has_native_auth_config", return_value=False), patch.object(
                    auth, "_get_client_secret", return_value="synthetic-private-secret"
                ), patch.object(auth, "_get_client_id", return_value="synthetic-client-id"), patch.object(
                    auth, "exchange_code_for_token", return_value={"access_token": "synthetic-token"}
                ) as exchange, patch.object(
                    auth, "get_user_info", return_value={"email": email, "email_verified": True}
                ):
                    _, state = auth.get_login_url(cookies)
                    self.fake_st.session_state = State()
                    self.fake_st.query_params.update(code="synthetic-code", state=state)
                    self.assertTrue(auth.handle_oauth_callback(cookies))
                    self.assertTrue(self.fake_st.session_state["authenticated"])
                    self.assertEqual(auth.can_view_diagnostics(), email.startswith("admin@"))
                    self.assertFalse(auth._verify_state(state, cookies))
                    exchange.assert_called_once()
                    self.fake_st.session_state = State()
                    auth.init_auth_state(cookies)
                    self.assertTrue(self.fake_st.session_state["authenticated"])
                    self.assertEqual(self.fake_st.session_state["user_email"], email)

    def test_native_verified_staff_login_succeeds_without_admin_permission(self):
        self.fake_st.user = dict(is_logged_in=True, email="staff@tkrjm.co.jp", email_verified=True)
        with patch.object(auth, "has_native_auth_config", return_value=True):
            auth.init_auth_state()
            self.assertTrue(self.fake_st.session_state["authenticated"])
            self.assertFalse(auth.can_view_diagnostics())

    def test_logout_cannot_restore_stale_request_cookie_but_fresh_oauth_can_login(self):
        with patch.object(auth, "has_native_auth_config", return_value=False), patch.object(
            auth, "_get_client_secret", return_value="synthetic-private-secret"
        ), patch.object(auth, "_get_client_id", return_value="synthetic-client-id"):
            token = auth._make_session_token("admin@tkrjm.co.jp", "Synthetic", "")
            self.fake_st.context = SimpleNamespace(cookies={auth.SESSION_COOKIE_NAME: token})
            cookies = Cookies({auth.SESSION_COOKIE_NAME: token})
            auth.init_auth_state(cookies)
            self.assertTrue(self.fake_st.session_state["authenticated"])
            auth.logout(cookies)
            auth.init_auth_state(cookies)
            self.assertFalse(self.fake_st.session_state["authenticated"])
            self.assertFalse(auth.can_view_diagnostics())
            _, state = auth.get_login_url(cookies)
            self.fake_st.query_params.update(code="synthetic-code", state=state)
            with patch.object(auth, "exchange_code_for_token", return_value={"access_token": "synthetic-token"}), patch.object(
                auth, "get_user_info", return_value={"email": "admin@tkrjm.co.jp", "email_verified": True}
            ):
                auth.handle_oauth_callback(cookies)
            self.assertTrue(self.fake_st.session_state["authenticated"])
            self.assertTrue(auth.can_view_diagnostics())

    def test_oauth_error_never_returns_token_response_or_exception_text(self):
        self.fake_st.query_params.update(code="synthetic-code", state="synthetic-state")
        with patch.object(auth, "has_native_auth_config", return_value=False), patch.object(
            auth, "_verify_state", return_value=True
        ), patch.object(auth, "exchange_code_for_token", side_effect=RuntimeError("PRIVATE-RESPONSE")):
            auth.handle_oauth_callback()
        self.assertNotIn("PRIVATE-RESPONSE", self.fake_st.session_state["_auth_error"])


class RenderingSecurityTests(unittest.TestCase):
    def test_concurrent_preview_pdfs_use_private_paths_and_correct_download_bytes(self):
        picking = picking_ui_for_test()
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        barrier = Barrier(2)
        paths, downloads = [], []

        def render(orders, output_path):
            paths.append(output_path)
            Path(output_path).write_bytes(orders[0].encode())
            barrier.wait(timeout=5)
            return SimpleNamespace(total_orders=1, total_pages=1)

        recorder = Mock()
        recorder.download_button.side_effect = lambda label, **kwargs: downloads.append(kwargs["data"])
        with patch.object(picking, "st", recorder), patch.object(picking, "render_picking_labels_pdf", side_effect=render), patch.object(
            auth, "can_view_diagnostics", return_value=False
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                list(executor.map(picking._preview_pdf, (["synthetic-first"], ["synthetic-second"])))
        self.assertEqual(len(set(paths)), 2)
        self.assertCountEqual(downloads, [b"synthetic-first", b"synthetic-second"])
        self.assertTrue(all(not Path(path).exists() for path in paths))
        recorder.json.assert_not_called()

    def test_picking_external_exceptions_never_reach_ui(self):
        picking = picking_ui_for_test()
        recorder = Mock()
        recorder.spinner.return_value.__enter__ = Mock()
        recorder.spinner.return_value.__exit__ = Mock(return_value=False)
        order = SimpleNamespace(source_row_number=2)
        with patch.object(picking, "st", recorder), patch.object(
            picking, "load_sheet_values", side_effect=RuntimeError("PRIVATE-SHEET-RESPONSE")
        ):
            self.assertFalse(picking._generate_and_upload([order]))
        self.assertNotIn("PRIVATE-SHEET-RESPONSE", str(recorder.error.call_args_list))
        recorder.exception.assert_not_called()
        recorder = Mock()
        with patch.object(picking, "st", recorder), patch.object(
            picking, "render_picking_labels_pdf", side_effect=RuntimeError("PRIVATE-PDF-RESPONSE")
        ):
            picking._preview_pdf([order])
        self.assertNotIn("PRIVATE-PDF-RESPONSE", str(recorder.error.call_args_list))
        recorder.exception.assert_not_called()

    def test_direct_picking_diagnostics_call_does_not_render_for_non_admin(self):
        picking = picking_ui_for_test()
        recorder = Mock()
        recorder.session_state = {"picking_diagnostics": {"source_sheet": "PRIVATE-SOURCE"}}
        with patch.object(picking, "st", recorder), patch.object(auth, "can_view_diagnostics", return_value=False):
            picking.render_picking_label_diagnostics_panel()
        self.assertEqual(recorder.mock_calls, [])

    def test_main_diagnostics_renderer_returns_before_any_ui_for_non_admin(self):
        tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_render_read_diagnostics")
        module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
        st = Mock()
        namespace = {"st": st, "can_view_diagnostics": lambda: False}
        exec(compile(module, "app.py", "exec"), namespace)
        namespace["_render_read_diagnostics"]([], None, 0, False)
        self.assertEqual(st.mock_calls, [])

    def test_main_only_constructs_diagnostics_tab_for_admin(self):
        tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_main_tab_labels")
        module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
        for allowed in (False, True):
            namespace = {"can_view_diagnostics": lambda: allowed}
            exec(compile(module, "app.py", "exec"), namespace)
            self.assertEqual("讀取診斷" in namespace["_main_tab_labels"](), allowed)


class SecretLoggingTests(unittest.TestCase):
    def test_postal_logging_does_not_emit_download_urls_or_navigation_exceptions(self):
        source = (ROOT / "bot/automation.py").read_text(encoding="utf-8")
        unsafe = __import__("re").findall(r'_log\(f[^\n]*(?:\{pdf_url\}|\{_diag_e\})', source)
        self.assertEqual(unsafe, [])

    def test_free_text_writeback_uses_raw_and_preserves_tracking_leading_zeroes(self):
        from bot.sheets import backfill_results
        from tests.fake_gspread import FakeClient, FakeWorksheet
        sheet = FakeWorksheet([["", "name", "order", "tracking", "", "", "", "", "", "country"]], row_count=2)
        # Record the real API mode: Google evaluates formulas only for USER_ENTERED.
        outcome = backfill_results(
            [{"name": "=1+1", "order_id": "SYNTHETIC-ORDER", "tracking": "0012345", "country_raw": "US"}],
            client=FakeClient(sheet), readback_delay_seconds=0,
        )
        self.assertTrue(outcome["ok"])
        batches = [call for call in sheet.calls if call[0] == "batch_update"]
        self.assertEqual(batches[0][2], "RAW")
        self.assertEqual(sheet.rows[1][1:4], ["=1+1", "SYNTHETIC-ORDER", "0012345"])

    def test_gemini_exception_callback_never_receives_raw_key_or_response(self):
        import importlib.util
        import sys
        spec = importlib.util.spec_from_file_location("bot._gemini_security_test", ROOT / "bot/gemini_helper.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        messages = []
        genai = SimpleNamespace(Client=Mock(side_effect=RuntimeError("https://api.test?key=PRIVATE-CALLBACK-KEY")))
        with patch.object(module, "_get_gemini_key", return_value="synthetic-key"), patch.dict(
            sys.modules, {"google": SimpleNamespace(genai=genai)}
        ):
            module.predict_hs_code("synthetic item", log_cb=messages.append)
        self.assertTrue(any("RuntimeError" in message for message in messages))
        self.assertNotIn("PRIVATE-CALLBACK-KEY", " ".join(messages))

    def test_redacts_url_keys_bearer_tokens_and_private_keys(self):
        for raw, private in (
            ("https://api.test/path?key=SYNTHETIC-KEY&other=x", "SYNTHETIC-KEY"),
            ('{"api_key": "SYNTHETIC-KEY"}', "SYNTHETIC-KEY"),
            ("Authorization: Bearer SYNTHETIC-TOKEN", "SYNTHETIC-TOKEN"),
            ('{"client_secret":"SYNTHETIC-SECRET"}', "SYNTHETIC-SECRET"),
            ("-----BEGIN PRIVATE KEY-----\nSYNTHETIC-PRIVATE\n-----END PRIVATE KEY-----", "SYNTHETIC-PRIVATE"),
        ):
            with self.subTest(raw=raw):
                self.assertNotIn(private, redact_operational_log(raw))

    def test_production_http_security_is_enabled(self):
        import tomllib
        settings = tomllib.loads((ROOT / ".streamlit/config.toml").read_text(encoding="utf-8"))
        self.assertTrue(settings["server"]["enableCORS"])
        self.assertTrue(settings["server"]["enableXsrfProtection"])
        self.assertEqual(settings["client"]["showErrorDetails"], "none")


if __name__ == "__main__":
    unittest.main()
