import sys
import types
import unittest
from unittest.mock import patch

sys.modules.setdefault("requests", types.SimpleNamespace())
sys.modules.setdefault("streamlit", types.SimpleNamespace(secrets={}, session_state={}))

import auth


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
        self.assertNotIn("target=", markup)
        self.assertNotIn("window.open", markup)


if __name__ == "__main__":
    unittest.main()
