import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PostalStartFlowTests(unittest.TestCase):
    def test_postal_start_button_directly_starts_job_without_confirm_gate(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")

        self.assertIn("ok, reason = _start_job(email, df_pending_for_run, max_rows_val)", app_source)
        self.assertNotIn("pending_start_requested", app_source)
        self.assertNotIn("confirm_start_job", app_source)

    def test_pending_orders_load_only_after_manual_reload_request(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")

        self.assertIn(
            'pending_manual_reload_requested = bool(st.session_state.pop("pending_manual_reload_requested", False))',
            app_source,
        )
        self.assertIn("if pending_manual_reload_requested:", app_source)
        self.assertNotIn(
            'if not isinstance(cached_pending, pd.DataFrame):\n'
            '            with st.spinner("讀取 Google Sheets 待打單資料..."):',
            app_source,
        )

    def test_playwright_install_is_deferred_until_postal_job_start(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")

        start_body = app_source[
            app_source.index("def _start_job("):app_source.index("# ══════════════════════════════════════════════════════\n# 頁面渲染函數")
        ]

        self.assertLess(start_body.index("_JOB_REGISTRY.start"), start_body.index("_install_playwright()"))
        self.assertLess(start_body.index("_install_playwright()"), start_body.index("from bot.automation import"))
        self.assertNotIn("\n_install_playwright()\ninit_auth_state(_cm)", app_source)

    def test_fx_rate_load_is_skipped_until_pending_orders_exist(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")

        self.assertIn("rate, rate_date, rate_source = None, \"\", \"\"", app_source)
        self.assertIn("if not df_pending.empty:\n        rate, rate_date, rate_source = _load_usd_jpy_rate()", app_source)
        self.assertNotIn("\n    rate, rate_date, rate_source = _load_usd_jpy_rate()\n    editable_count", app_source)

    def test_job_launching_state_locks_ui_until_running_job_is_visible(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")

        self.assertIn('is_launching = bool(st.session_state.get("job_launching"))', app_source)
        self.assertIn('is_busy = is_running or is_launching', app_source)
        self.assertIn('if is_running and st.session_state.get("job_launching"):', app_source)
        self.assertIn('st.session_state.pop("job_launching", None)', app_source)
        self.assertIn('disabled=is_busy,', app_source)
        self.assertIn('if is_busy:', app_source)
        self.assertIn('_render_blocking_running_guard(job, launching=is_launching)', app_source)


if __name__ == "__main__":
    unittest.main()
