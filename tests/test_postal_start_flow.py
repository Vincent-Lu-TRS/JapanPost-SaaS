import ast
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


class PostalStartFlowTests(unittest.TestCase):
    def test_target_read_failure_preserves_package_identity_for_every_order_state(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        failure_block = app_source[
            app_source.index("except Exception as preflight_error:"):
            app_source.index("latest_pending_df = get_pending_orders(")
        ]

        self.assertIn(
            '"trans_type": str(order.get("trans_type") or "").strip()',
            failure_block,
        )
        self.assertIn(
            '"shipment_role": str(order.get("shipment_role") or "primary").strip()',
            failure_block,
        )

    def test_protected_pending_snapshot_does_not_repeat_rerun_after_picking_applies(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        tree = ast.parse(app_source)
        active_tick = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_active_refresh_tick"
        )
        active_tick.decorator_list = []
        module = ast.Module(body=[active_tick], type_ignores=[])
        ast.fix_missing_locations(module)

        class SessionState(dict):
            __getattr__ = dict.__getitem__
            __setattr__ = dict.__setitem__

        class StreamlitStub:
            def __init__(self, *, editor_dirty):
                self.session_state = SessionState(
                    last_pending_loaded_at="pending-old",
                    picking_snapshot_loaded_at="picking-old",
                    pending_editor_dirty=editor_dirty,
                )
                self.rerun_count = 0

            def rerun(self, **_kwargs):
                self.rerun_count += 1

        pending_result = SimpleNamespace(
            data=object(),
            status=SimpleNamespace(loaded_at="pending-new"),
        )
        picking_result = SimpleNamespace(
            data=object(),
            status=SimpleNamespace(loaded_at="picking-new"),
        )

        for is_busy, editor_dirty in ((True, False), (False, True)):
            with self.subTest(is_busy=is_busy, editor_dirty=editor_dirty):
                streamlit = StreamlitStub(editor_dirty=editor_dirty)

                def refresh_source(source, *, force):
                    self.assertFalse(force)
                    return pending_result if source == "pending" else picking_result

                def apply_pending_result(_result, *, is_busy, allow_dirty_reset, job):
                    self.assertFalse(allow_dirty_reset)
                    self.assertEqual(job, {})
                    return not is_busy and not streamlit.session_state.pending_editor_dirty

                namespace = {
                    "st": streamlit,
                    "_refresh_source": refresh_source,
                    "_apply_pending_result": apply_pending_result,
                    "apply_picking_payload": lambda *_args, **_kwargs: None,
                }
                exec(compile(module, "app.py", "exec"), namespace)

                namespace["_active_refresh_tick"](is_busy=is_busy, job={})
                namespace["_active_refresh_tick"](is_busy=is_busy, job={})

                self.assertEqual(streamlit.rerun_count, 1)

    def test_postal_start_button_directly_starts_job_without_confirm_gate(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")

        self.assertIn("ok, reason = _start_job(email, df_pending_for_run, max_rows_val)", app_source)
        self.assertNotIn("pending_start_requested", app_source)
        self.assertNotIn("confirm_start_job", app_source)

    def test_pending_and_picking_snapshots_load_automatically_and_independently(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")

        render_body = app_source[app_source.index("def _render_main_app():"):]
        self.assertIn('_refresh_source("pending", force=False)', render_body)
        self.assertIn('_refresh_source("picking", force=False)', render_body)
        self.assertNotIn("pending_manual_reload_requested", render_body)

    def test_refresh_coordinator_and_active_fragment_are_wired(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")

        self.assertIn('@st.fragment(run_every="20m")', app_source)
        self.assertIn("def _active_refresh_tick(*, is_busy: bool, job)", app_source)
        self.assertIn("allow_dirty_reset=False", app_source)
        self.assertIn("preserve_selection=True", app_source)
        self.assertIn("ttl=timedelta(minutes=20)", app_source)
        self.assertIn("now=lambda: datetime.now(JST)", app_source)

    def test_manual_reload_forces_shared_snapshot_without_clearing_last_good_data(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        picking_source = (ROOT / "features" / "picking_labels.py").read_text(encoding="utf-8")

        self.assertIn('_refresh_source("pending", force=True)', app_source)
        self.assertIn('refresh_source("picking", force=True)', picking_source)
        self.assertNotIn('st.session_state.pop("last_pending_df", None)', app_source)

    def test_v2_editable_widgets_mark_pending_editor_dirty(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")

        self.assertIn("def _mark_pending_editor_dirty()", app_source)
        v2_body = app_source[
            app_source.index("def _render_postal_pending_v2("):
            app_source.index("def _render_running_progress(")
        ]
        self.assertGreaterEqual(v2_body.count("on_change=_mark_pending_editor_dirty"), 6)

    def test_v1_renderer_and_session_keys_are_removed(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")

        for marker in (
            "pending_reset_",
            "pending_name_",
            "pending_selected_by_order",
            "pending_extra_trans_single_",
        ):
            self.assertNotIn(marker, app_source)
        for marker in (
            "_start_job",
            "_native_info",
            "_apply_data_editor_state",
            "pending_v2_selected_by_order",
        ):
            self.assertIn(marker, app_source)

    def test_job_and_pending_log_boundaries_redact_before_storage_or_ui(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        sheets_source = (ROOT / "bot" / "sheets.py").read_text(encoding="utf-8")
        start_body = app_source[
            app_source.index("def _start_job("):
            app_source.index("def _render_login_page(")
        ]

        self.assertIn("from safe_logging import", app_source)
        self.assertIn("redact_operational_log", app_source)
        self.assertIn("safe_message = redact_operational_log", start_body)
        self.assertIn('job["logs"].append(entry)', start_body)
        self.assertNotIn('entry = f"[{ts}] {msg}"', start_body)
        self.assertIn("_safe_operational_log_lines", app_source)
        self.assertGreaterEqual(app_source.count("_safe_operational_log_lines("), 4)
        self.assertIn("redact_operational_log", sheets_source)
        self.assertIn("safe_message = redact_operational_log", sheets_source)
        self.assertNotIn("_last_non_empty_row_sample", sheets_source)

    def test_pending_refresh_failure_sets_safe_status_and_exact_warning_copy(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        apply_body = app_source[
            app_source.index("def _apply_pending_result("):
            app_source.index("if not hasattr(st, \"fragment\")")
        ]

        self.assertIn('st.session_state["pending_refresh_error_code"]', apply_body)
        self.assertIn("if result.data is None", apply_body)
        self.assertIn(
            "暫時無法取得最新資料，目前顯示上次成功讀取的內容。",
            app_source,
        )
        self.assertIn(
            "目前無法取得待製郵便運單資料，請稍後重新讀取。",
            app_source,
        )
        self.assertNotIn(
            'st.warning(st.session_state["pending_refresh_error_code"])',
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
        render_body = app_source[app_source.index("def _render_main_app():"):]
        v2_body = app_source[
            app_source.index("def _render_postal_pending_v2("):
            app_source.index("def _render_running_progress(")
        ]

        self.assertLess(
            render_body.index("rate, rate_date, _ = _load_usd_jpy_rate()"),
            render_body.index("_render_postal_pending_v2("),
        )
        self.assertIn("editable_count = min(len(df_pending), 20)", v2_body)
        self.assertNotIn(
            "if not df_pending.empty:\n        rate, rate_date, _ = _load_usd_jpy_rate()",
            render_body,
        )

    def test_completed_pending_rows_are_filtered_before_editor_state_is_built(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")

        self.assertIn("filter_pending_orders_after_batch", app_source)
        self.assertIn('st.session_state["last_pending_df"] = df_pending', app_source)

    def test_fresh_pending_snapshot_bypasses_stale_batch_hide_filter(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")

        self.assertIn("authoritative_snapshot: bool = False", app_source)
        self.assertIn('st.session_state["pending_snapshot_authoritative"]', app_source)
        self.assertIn("snapshot_authoritative=", app_source)
        self.assertIn(
            'not st.session_state.get("pending_snapshot_authoritative")',
            app_source,
        )
        self.assertIn(
            'st.session_state.pop("pending_snapshot_authoritative", None)',
            app_source,
        )

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

    def test_v2_summary_uses_three_metrics_without_spacer(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        v2_body = app_source[
            app_source.index("def _render_postal_pending_v2("):
            app_source.index("def _render_running_progress(")
        ]

        self.assertIn(
            'left_panel, right_panel = st.columns([3.05, 1.2], gap="medium", vertical_alignment="top")',
            v2_body,
        )
        self.assertIn('metric_cols = st.columns(3, gap="small", vertical_alignment="center")', v2_body)
        for label in ('("待製單", pending_count', '("已選取", selected_count', '("本次完成", done'):
            self.assertIn(label, v2_body)

    def test_postal_navigation_has_one_formal_entry_and_preserves_order_contract(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")

        self.assertIn(
            '["跨境揀貨單", "待製郵便運單", "使用說明", "讀取診斷"]',
            app_source,
        )
        self.assertNotIn("郵局待打單（新版測試）", app_source)
        self.assertNotIn("with preview_tab:", app_source)
        self.assertIn("with postal_tab:", app_source)
        self.assertIn("_render_postal_pending_v2(", app_source)
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

    def test_postal_operational_copy_is_concise_and_hides_cache_internals(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        renderer_body = app_source[
            app_source.index("def _render_postal_pending_v2("):
            app_source.index("def _render_running_progress(")
        ]

        for message in (
            "資料更新於",
            "暫時無法取得最新資料，目前顯示上次成功讀取的內容。",
            "目前無法取得待製郵便運單資料，請稍後重新讀取。",
        ):
            self.assertIn(message, app_source)
        for marker in (
            "row_count",
            "ttl",
            "pending_refresh_error_code",
            "_job_lock",
            "{rate_source}",
            "traceback",
            "st.exception",
        ):
            self.assertNotIn(marker, renderer_body)

    def test_v2_rate_is_secondary_and_v2_operation_panel_keeps_original_controls(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")

        self.assertIn("format_secondary_rate_badge", app_source)
        self.assertIn("postal-v2-rate-badge", app_source)
        self.assertIn("postal-v2-operation-panel", app_source)
        for marker in ["最大處理", "開始製單", "重新讀取", "全部恢復預設資料"]:
            self.assertIn(marker, app_source)
        self.assertIn("藍框：可編輯", app_source)

    def test_v2_labels_and_order_card_layout_match_latest_ui_contract(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")

        self.assertNotIn(
            '<div class="postal-v2-list-heading">待製單訂單</div>',
            app_source,
        )
        self.assertIn('"姓名",\n                                value=pending_name,', app_source)
        self.assertIn('                                "寄送方式",', app_source)
        self.assertIn('"追加製作",\n                                options=extra_options,', app_source)
        self.assertIn(
            '[.58, 1.75, 2.45, .86, .86, 1.0],',
            app_source,
        )
        self.assertIn(
            '[1.2, 1.8, 1.8, 1.1, 1.2, .7],',
            app_source,
        )
        self.assertIn(
            '[1.42, 1.8, 1.8, 1.25, .9],',
            app_source,
        )
        self.assertIn(
            'padding: .65rem .7rem 1.12rem !important;',
            app_source,
        )
        self.assertIn(
            'div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] .postal-v2-operation-panel)',
            app_source,
        )
        self.assertIn(
            '.postal-v2-card-marker) .native-info-value {',
            app_source,
        )
        self.assertIn(
            'native-info-country .native-info-value',
            app_source,
        )

    def test_v2_uses_scoped_flat_dark_palette_and_preserves_job_feedback_hooks(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        config_source = (ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8")

        self.assertIn(".postal-v2-", app_source)
        self.assertIn("#0A0D13", app_source)
        self.assertIn("#5275A8", app_source)
        self.assertIn('[data-baseweb="tab-highlight"]', app_source)
        self.assertIn('primaryColor = "#5275A8"', config_source)
        self.assertNotIn("#EA580C", config_source)
        self.assertNotIn("linear-gradient", app_source)
        self.assertNotIn("radial-gradient", app_source)
        for marker in [
            "_render_blocking_running_guard",
            "failure_alerts",
            "詳細除錯日誌",
            "summarize_batch_results",
        ]:
            self.assertIn(marker, app_source)

    def test_job_launching_state_locks_v2_controls_until_running_job_is_visible(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        v2_body = app_source[
            app_source.index("def _render_postal_pending_v2("):
            app_source.index("def _render_running_progress(")
        ]

        self.assertIn('is_launching = bool(st.session_state.get("job_launching"))', app_source)
        self.assertIn('is_busy = is_running or is_launching', app_source)
        self.assertIn('if is_running and st.session_state.get("job_launching"):', app_source)
        self.assertIn('disabled=is_busy,', v2_body)
        self.assertIn('elif is_busy:', v2_body)
        self.assertIn('st.info("製單任務啟動中，正在建立執行狀態。")', v2_body)
        self.assertIn('_render_running_progress(job)', v2_body)

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

        self.assertIn("read_completion_authority", app_source)
        self.assertIn("preflight_batch_orders", app_source)
        self.assertIn("partition_preflight_rows", app_source)
        self.assertIn("exclude_completed=False", app_source)
        self.assertIn("target_read_error", app_source)
        self.assertIn("source_changed", app_source)
        self.assertIn("latest_pending_df", app_source)

    def test_start_job_wires_package_status_events_and_executes_only_ready_rows(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        start_body = app_source[
            app_source.index("def _start_job("):
            app_source.index("# ══════════════════════════════════════════════════════\n# 頁面渲染函數")
        ]

        self.assertIn("def _status(event)", start_body)
        self.assertIn("update_order_status_from_event(job, event)", start_body)
        self.assertIn("status_cb=_status", start_body)
        self.assertIn("ready_rows", start_body)
        self.assertIn("writeback_pending", start_body)
        self.assertIn("writeback_verified", start_body)

    def test_start_job_aborts_entire_batch_when_any_preflight_item_is_blocked(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        start_body = app_source[
            app_source.index("def _start_job("):
            app_source.index("# ══════════════════════════════════════════════════════\n# 頁面渲染函數")
        ]

        blocker_gate = start_body.index("if preflight_blocked_results:")
        automation_start = start_body.index("_install_playwright()")
        ready_assignment = start_body.index("rows_for_run = ready_rows")
        self.assertLess(blocker_gate, ready_assignment)
        self.assertLess(blocker_gate, automation_start)
        blocker_body = start_body[blocker_gate:ready_assignment]
        self.assertIn('_JOB_REGISTRY.finish(job, "error")', blocker_body)
        self.assertIn("return", blocker_body)

    def test_preflight_abort_does_not_duplicate_ready_rows_as_failure_results(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        start_body = app_source[
            app_source.index("def _start_job(") : app_source.index(
                "# ══════════════════════════════════════════════════════\n# 頁面渲染函數"
            )
        ]

        self.assertIn('job["batch_preflight_blocked_count"]', start_body)
        self.assertNotIn('job["results"].extend(aborted_results)', start_body)
        self.assertIn("mark_results_failed(job, aborted_results)", start_body)

    def test_terminal_batch_view_hides_editor_cards_until_refresh(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        v2_body = app_source[
            app_source.index("def _render_postal_pending_v2(") : app_source.index(
                "def _render_running_progress("
            )
        ]

        self.assertIn("show_editor_cards", v2_body)
        self.assertIn("elif show_editor_cards:", v2_body)
        self.assertIn('st.session_state["postal_batch_view_active"]', app_source)

    def test_pending_refresh_applies_mixed_package_preservation_with_job_authority(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        apply_body = app_source[
            app_source.index("def _apply_pending_result("):
            app_source.index("if not hasattr(st, \"fragment\")")
        ]

        self.assertIn("preserve_incomplete_submitted_orders", apply_body)
        self.assertIn('existing=st.session_state.get("last_pending_df")', apply_body)
        self.assertIn('submitted_orders=job.get("orders") or []', apply_body)
        self.assertIn('results=job.get("results") or []', apply_body)

    def test_start_job_only_backfills_successful_results_with_tracking(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        start_body = app_source[
            app_source.index("def _start_job("):
            app_source.index("# ══════════════════════════════════════════════════════\n# 頁面渲染函數")
        ]
        candidates = start_body[
            start_body.index("successful_results = ["):
            start_body.index("failed_results = [")
        ]

        self.assertIn('str(result.get("tracking") or "").strip()', candidates)

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
