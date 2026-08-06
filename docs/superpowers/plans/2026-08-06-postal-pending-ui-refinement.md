# Postal Pending UI Refinement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the postal pending-order screen show the cached USD/JPY rate on entry, remain readable at narrow viewport widths, remove successful orders from the visible pending list after a batch, and keep execution/debug output scoped to failed batches.

**Architecture:** Keep Google Sheets loading, order validation, address/name parsing, Japan Post automation, backfill, and duplicate-prevention logic unchanged. Add a presentation-only filter over the already-loaded pending DataFrame using structured batch results; keep the authoritative next read on the existing manual reload path. Keep the existing structured `製單狀態` table and failure alerts as the only batch result summary. Load the existing cached FX helper independently of the pending DataFrame so entering the page can display the rate without forcing a Sheets read.

**Tech Stack:** Python 3.12, Streamlit, pandas, unittest, existing Streamlit Community Cloud deployment.

---

### Task 1: Add regression tests for result filtering and UI visibility rules

**Files:**
- Modify: `tests/test_postal_ui_feedback.py`
- Modify: `tests/test_postal_start_flow.py`

- [ ] Add a failing helper test with two pending rows and batch results containing one `success`/`completed` order plus one failed order; require only the successful order to be filtered from the visible pending DataFrame.
- [ ] Add a failing helper test proving empty results and non-completed statuses leave the pending DataFrame unchanged.
- [ ] Update the source-flow test to require `_load_usd_jpy_rate()` before the pending-data condition, and require no `本次製單結果` or `執行日誌` rendering block.
- [ ] Add source assertions that detailed debug-log rendering is guarded by `batch_summary["failure_alerts"]`, and that the toolbar uses four responsive summary columns rather than the previous spacer column.
- [ ] Run the focused tests and confirm they fail for the current implementation.

### Task 2: Implement presentation-only completed-order filtering

**Files:**
- Modify: `postal_ui_feedback.py`
- Modify: `app.py`

- [ ] Add a small helper that extracts successful order IDs from structured results and filters matching rows from a pending DataFrame without mutating the input or changing source-data parsing.
- [ ] After a non-running job has results, apply that helper to the cached/reloaded pending DataFrame before computing counts, selection state, and editor rows.
- [ ] Remove successful IDs from the visible selection-state dictionary so completed checkbox state cannot persist into the next rendered pending list.
- [ ] Remove the per-card `已製單` summary from the pending-order editor; successful cards will no longer be rendered after the filter, and the structured `製單狀態` table remains unchanged.
- [ ] Run the focused helper and start-flow tests and confirm they pass.

### Task 3: Load the exchange rate on entry and make the summary row responsive

**Files:**
- Modify: `app.py`

- [ ] Call the existing cached `_load_usd_jpy_rate()` independently of `df_pending`; keep the existing warning behavior for a failed rate when pending data is present.
- [ ] Replace the five-column summary layout (including the empty spacer) with four weighted columns for rate, pending, selected, and completed counts.
- [ ] Add `min-width: 0`, wrapping, no-break numeric values, and consistent vertical alignment to the toolbar text/count styles so the rate cannot overflow into the pending count at narrow widths.
- [ ] Do not change the action buttons, input values, data-editor fields, order eligibility checks, or automation inputs.
- [ ] Run the focused tests and `python -m py_compile app.py postal_ui_feedback.py`.

### Task 4: Restrict batch logs to failed batches and remove redundant result blocks

**Files:**
- Modify: `app.py`

- [ ] Delete the `本次製單結果` block and the `執行日誌` text area from the postal pending screen.
- [ ] Render only `詳細除錯日誌` when the completed job has at least one failure/skipped/blocked result; show the existing last-log window inside that expander.
- [ ] Remove the now-unused `filter_key_log_lines` import from `app.py` without deleting the shared helper used by its tests.
- [ ] Run the full unittest suite and inspect the source diff for unrelated changes.

### Task 5: Verify the deployed behavior

**Files:**
- No additional source files unless verification finds a regression.

- [ ] Run `python -m unittest discover -s tests -v`, `python -m py_compile app.py postal_ui_feedback.py`, and `git diff --check`.
- [ ] Commit only the plan, implementation, and regression tests; leave existing untracked user directories untouched.
- [ ] Push the commit to the deployment branch and wait for Streamlit Cloud to reload.
- [ ] Open the deployed app and capture fresh screenshots for: initial rate display, narrow-width summary row, successful batch with pending cards removed and no result/log blocks, and failed batch with failure alerts plus detailed debug log.
- [ ] Report the commit/deployment status and the observed verification results; do not claim completion without fresh checks.
