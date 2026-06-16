import sys
import types
import unittest
from unittest.mock import patch

sys.modules.setdefault("requests", types.SimpleNamespace())
sys.modules.setdefault("streamlit", types.SimpleNamespace(secrets={}, session_state={}))

import auth


class AttrDict(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name, value):
        self[name] = value


class AuthSessionTests(unittest.TestCase):
    def test_session_token_restores_user_before_30_day_expiry(self):
        with patch.object(auth, "_get_client_secret", return_value="test-secret"):
            token = auth._make_session_token(
                "user@tkrjm.co.jp",
                "Test User",
                "https://example.com/pic.png",
            )

            user = auth._parse_session_token(token)

        self.assertEqual(user["email"], "user@tkrjm.co.jp")
        self.assertEqual(user["name"], "Test User")
        self.assertEqual(user["picture"], "https://example.com/pic.png")

    def test_session_token_rejects_tampering(self):
        with patch.object(auth, "_get_client_secret", return_value="test-secret"):
            token = auth._make_session_token("user@tkrjm.co.jp", "Test User", "")

            parsed = auth._parse_session_token(token + "tampered")

        self.assertIsNone(parsed)

    def test_session_token_rejects_expired_token(self):
        with patch.object(auth, "_get_client_secret", return_value="test-secret"):
            with patch.object(auth.time, "time", return_value=1_000):
                token = auth._make_session_token("user@tkrjm.co.jp", "Test User", "")

            expired_at = 1_000 + auth.SESSION_DURATION_DAYS * 86400 + 1
            with patch.object(auth.time, "time", return_value=expired_at):
                parsed = auth._parse_session_token(token)

        self.assertIsNone(parsed)

    def test_login_link_markup_uses_same_tab_navigation(self):
        url = "https://accounts.google.com/o/oauth2/v2/auth?client_id=abc"

        markup = auth.render_login_link(url)

        self.assertIn('href="https://accounts.google.com/o/oauth2/v2/auth?client_id=abc"', markup)
        self.assertIn('target="_top"', markup)
        self.assertNotIn("onclick=", markup)
        self.assertNotIn("window.open", markup)

    def test_native_auth_config_detects_complete_auth_section(self):
        fake_secrets = {
            "auth": {
                "redirect_uri": "https://jppost.streamlit.app/oauth2callback",
                "cookie_secret": "test-cookie-secret",
                "client_id": "client-id",
                "client_secret": "client-secret",
                "server_metadata_url": "https://accounts.google.com/.well-known/openid-configuration",
            }
        }

        with patch.object(auth.st, "secrets", fake_secrets):
            self.assertTrue(auth.has_native_auth_config())

    def test_native_auth_config_rejects_flat_legacy_secrets(self):
        fake_secrets = {
            "GOOGLE_CLIENT_ID": "client-id",
            "GOOGLE_CLIENT_SECRET": "client-secret",
        }

        with patch.object(auth.st, "secrets", fake_secrets):
            self.assertFalse(auth.has_native_auth_config())

    def test_restore_native_auth_state_sets_existing_session_shape(self):
        fake_user = {
            "is_logged_in": True,
            "email": "admin@tkrjm.co.jp",
            "name": "Admin User",
            "picture": "https://example.com/avatar.png",
        }
        fake_session_state = AttrDict()

        with patch.object(auth.st, "user", fake_user, create=True):
            with patch.object(auth.st, "session_state", fake_session_state):
                restored = auth.restore_native_auth_state()

        self.assertTrue(restored)
        self.assertTrue(fake_session_state["authenticated"])
        self.assertEqual(fake_session_state["user_email"], "admin@tkrjm.co.jp")
        self.assertEqual(fake_session_state["user_name"], "Admin User")
        self.assertEqual(fake_session_state["user_picture"], "https://example.com/avatar.png")


if __name__ == "__main__":
    unittest.main()
