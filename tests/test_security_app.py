"""Render the actual app with provider claims and fully synthetic data sources."""
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SecurityAppTests(unittest.TestCase):
    def test_anonymous_staff_and_admin_receive_only_authorized_ui_payloads(self):
        script = textwrap.dedent('''
            from pathlib import Path
            from unittest.mock import patch
            import pandas as pd
            from streamlit.testing.v1 import AppTest
            from refresh_payloads import PickingPayload

            provider = {}
            patch("auth.has_native_auth_config", return_value=True).start()
            patch("auth._st_user_get", side_effect=lambda key, default="": provider.get(key, default)).start()
            patch("fx_rates.fetch_usd_jpy_rate", return_value=(150.0, "2026-10-25", "synthetic")).start()
            pending = patch("bot.sheets.get_pending_orders", return_value=pd.DataFrame()).start()
            patch("features.picking_labels.load_picking_payload", return_value=PickingPayload((), (), {
                "source_sheet": "PRIVATE-DIAGNOSTICS-SOURCE",
                "source_spreadsheet_id": "PRIVATE-DIAGNOSTICS-ID",
                "actual_detected_headers": ["PRIVATE-DIAGNOSTICS-HEADER"],
            })).start()
            patch("requests.sessions.Session.request", side_effect=AssertionError("No live requests in UI security tests")).start()

            app = AppTest.from_file(str(Path.cwd() / "app.py"))
            app.run(timeout=30)
            assert not app.exception, app.exception
            assert len(app.tabs) == 0
            assert pending.call_count == 0
            for email, expected_tab_count in (("staff@tkrjm.co.jp", 4), ("ADMIN@TKRJM.CO.JP", 5)):
                provider.update(is_logged_in=True, email=email, email_verified=True, name="Synthetic")
                app.run(timeout=30)
                assert not app.exception, app.exception
                assert len(app.tabs) == expected_tab_count, [tab.label for tab in app.tabs]
                payload = " ".join(str(element.value) for element in app.get("json"))
                for marker in ("PRIVATE-DIAGNOSTICS-SOURCE", "PRIVATE-DIAGNOSTICS-ID", "PRIVATE-DIAGNOSTICS-HEADER"):
                    assert (marker in payload) == (expected_tab_count == 5), (email, marker)
                probe_buttons = [button for button in app.button if button.key == "cloud_browser_runtime_probe"]
                assert bool(probe_buttons) == (expected_tab_count == 5)
            # A stale display email from the last session does not retain access.
            provider.clear()
            app.run(timeout=30)
            assert not app.exception, app.exception
            assert len(app.tabs) == 0
        ''')
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=ROOT,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        if result.returncode:
            self.fail(result.stdout[-3000:] + result.stderr[-5000:])


if __name__ == "__main__":
    unittest.main()
