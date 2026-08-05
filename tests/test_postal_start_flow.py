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
        self.assertIn('disabled=is_busy,', app_source)
        self.assertIn('if is_busy:', app_source)
        self.assertIn('_render_blocking_running_guard(job, launching=is_launching)', app_source)

    def test_job_launching_state_clears_after_terminal_or_stale_launch(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")

        self.assertIn('if job is not None and not is_running and st.session_state.get("job_launching"):', app_source)
        self.assertIn('st.session_state.pop("job_launching_started_at", None)', app_source)
        self.assertIn('launch_lock_active = _job_lock_is_active(email)', app_source)
        self.assertIn('if job is None and st.session_state.get("job_launching") and not launch_lock_active:', app_source)
        self.assertIn('_write_job_lock(email)', app_source)
        self.assertIn('_clear_job_lock(email)', app_source)
        self.assertIn('st.session_state["job_launching_started_at"] = time.time()', app_source)
        self.assertNotIn('elif reason == "batch_running":\n                        st.session_state["job_launching"] = True', app_source)

    def test_start_job_rechecks_target_and_source_before_automation(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")

        self.assertIn("read_completed_order_ids", app_source)
        self.assertIn("preflight_batch_orders", app_source)
        self.assertIn("target_read_error", app_source)
        self.assertIn("source_changed", app_source)
        self.assertIn("latest_pending_df", app_source)

    def test_completion_count_comes_from_structured_results_and_failure_alerts(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")

        self.assertIn("summarize_batch_results", app_source)
        self.assertIn('completed_count', app_source)
        self.assertIn('failure_alerts', app_source)
        self.assertIn('backfill_outcome.get("ok")', app_source)
        self.assertNotIn('done = len(job["results"]) if job else 0', app_source)

    def test_job_registry_is_cached_across_streamlit_reruns(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")

        self.assertIn(
            '@st.cache_resource(show_spinner=False)\n'
            'def _get_job_registry()',
            app_source,
        )
        self.assertIn('_JOB_REGISTRY = _get_job_registry()', app_source)


if __name__ == "__main__":
    unittest.main()
