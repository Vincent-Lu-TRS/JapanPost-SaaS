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

    def test_fx_rate_load_is_ready_before_pending_orders_exist(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")

        self.assertIn(
            'pending_count = len(df_pending)\n    rate, rate_date, rate_source = _load_usd_jpy_rate()\n    editable_count',
            app_source,
        )
        self.assertNotIn(
            "if not df_pending.empty:\n        rate, rate_date, rate_source = _load_usd_jpy_rate()",
            app_source,
        )

    def test_completed_pending_rows_are_filtered_before_editor_state_is_built(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")

        self.assertIn("filter_pending_orders_after_batch", app_source)
        self.assertIn('st.session_state["last_pending_df"] = df_pending', app_source)

    def test_successful_batch_hides_redundant_result_and_execution_log_sections(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")

        self.assertNotIn('st.subheader("✅ 本次製單結果")', app_source)
        self.assertNotIn('st.subheader("📄 執行日誌")', app_source)
        self.assertNotIn('st.text_area(\n                "執行日誌內容"', app_source)

    def test_detailed_debug_log_is_only_rendered_for_failed_batches(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")

        self.assertIn(
            'if job and job.get("logs") and batch_summary["failure_alerts"]:',
            app_source,
        )

    def test_toolbar_summary_uses_four_columns_without_spacer(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")

        self.assertIn(
            'toolbar_info_cols = st.columns([1.75, 1, 1, 1], gap="medium", vertical_alignment="center")',
            app_source,
        )

    def test_v2_preview_tab_is_separate_from_v1_and_preserves_order_contract(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")

        self.assertIn("郵局待打單（新版測試）", app_source)
        self.assertIn("with postal_v2_tab:", app_source)
        self.assertIn('with preview_tab:', app_source)
        for marker in [
            "Name",
            "TransType",
            "追加",
            "PRC ID",
            "PCCC",
            "Description",
            "HSCode",
            "Value",
            "Quantity",
        ]:
            self.assertIn(marker, app_source)
        self.assertIn('"No."', app_source)
        self.assertNotIn("內容品名（僅顯示）", app_source)

    def test_v2_rate_is_secondary_and_v2_operation_panel_keeps_original_controls(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")

        self.assertIn("format_secondary_rate_badge", app_source)
        self.assertIn("postal-v2-rate-badge", app_source)
        self.assertIn("postal-v2-operation-panel", app_source)
        for marker in ["最大處理", "開始製單", "重新讀取", "全部恢復預設資料"]:
            self.assertIn(marker, app_source)
        self.assertIn("藍框：可編輯", app_source)

    def test_v2_uses_scoped_flat_dark_palette_and_preserves_job_feedback_hooks(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")

        self.assertIn(".postal-v2-", app_source)
        self.assertIn("#0A0D13", app_source)
        self.assertIn("#5275A8", app_source)
        self.assertNotIn("linear-gradient", app_source)
        self.assertNotIn("radial-gradient", app_source)
        for marker in [
            "_render_blocking_running_guard",
            "failure_alerts",
            "詳細除錯日誌",
            "summarize_batch_results",
        ]:
            self.assertIn(marker, app_source)

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
            'def _get_job_registry(cache_version: str = _JOB_REGISTRY_CACHE_VERSION)',
            app_source,
        )
        self.assertIn('_JOB_REGISTRY = _get_job_registry(_JOB_REGISTRY_CACHE_VERSION)', app_source)


if __name__ == "__main__":
    unittest.main()
