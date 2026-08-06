# Postal Pending UI v2 Batch Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a separate, fully functional postal pending-order UI preview with the selected grouped batch-selection controls, while preserving the existing postal page and all existing data, validation, automation, and write-back behavior until the preview is accepted.

**Architecture:** Keep the current `郵局待打單` tab as the v1 reference implementation. Add a v2 preview tab only on the feature branch, using the same loaded `df_pending`, exchange-rate source, pending-order editor transformations, preflight validation, job registry, automation, result filtering, and Google Sheets write-back functions. V2 gets namespaced Streamlit state keys so v1 and v2 can coexist for comparison without widget collisions. The USD/JPY rate moves only in v2 from the primary KPI row into a compact secondary utility badge beside the postal pending-order controls. V2 uses solid neutral surfaces and borders inspired by the referenced Web Interface Guidelines example; it removes the existing gradient treatment while keeping the right-side order/operation panel on a dark solid background.

**Tech Stack:** Python 3.12, Streamlit, pandas, unittest, existing `pending_editor.py`, existing `postal_ui_feedback.py`, existing Japan Post automation and Streamlit Community Cloud deployment.

---

### Task 1: Lock the v2 behavior contract with failing tests

**Files:**
- Create: `tests/test_postal_ui_v2.py`
- Modify: `tests/test_postal_start_flow.py`

- [x] **Step 1: Add pure behavior tests before implementation**

Add tests that import the not-yet-created `postal_ui_v2` helpers and assert:

```python
def test_select_all_marks_only_current_pending_order_ids():
    assert apply_batch_selection(
        {"A": False, "B": True, "OLD": True},
        ["A", "B"],
        "select_all",
    ) == {"A": True, "B": True, "OLD": True}


def test_clear_all_marks_only_current_pending_order_ids():
    assert apply_batch_selection(
        {"A": True, "B": True, "OLD": False},
        ["A", "B"],
        "clear_all",
    ) == {"A": False, "B": False, "OLD": False}


def test_secondary_rate_badge_is_single_line_and_observable():
    assert format_secondary_rate_badge(157.79, "2026-08-06") == "USD/JPY 157.79 · 26/08/06"


def test_v2_field_contract_preserves_current_editability():
    assert v2_field_contract() == {
        "editable": ["Name", "TransType", "追加", "PRC ID/PCCC", "Description", "HSCode", "Value", "Quantity"],
        "display_only": ["製單", "Order No.", "Country", "No."],
        "system_calculated": ["TotalValue(USD)", "TotalValue(JPY)"],
    }
```

The tests must also assert that applying a batch action returns a new selection mapping and does not mutate the source order dataframe or source rows.

- [x] **Step 2: Add source-flow regression assertions**

Extend `tests/test_postal_start_flow.py` so the source contains a distinct v2 preview tab and the v1 `郵局待打單` rendering block remains present. Assert that the v2 source includes all current editable controls (`Name`, `TransType`, `追加`, conditional `PRC ID`/`PCCC`, `Description`, `HSCode`, `Value`, `Quantity`), renders the item sequence as `No.`, and does not render a user-facing `內容品名（僅顯示）` column. The v2 data-editor adapter must preserve the internal `Content` index when converting edited rows back to the existing `apply_pending_order_editor_values` contract. Assert that the v2 rate is rendered through the secondary rate badge rather than as one of the three primary v2 KPI values.

- [x] **Step 3: Run the focused tests and verify the expected RED state**

Run:

```powershell
python -m unittest tests.test_postal_ui_v2 tests.test_postal_start_flow
```

Expected result before implementation: import failure for `postal_ui_v2` and/or missing v2 source markers. Fix only test setup errors; do not add production implementation before observing the intended feature failure.

### Task 2: Implement pure v2 selection, rate, and field-contract helpers

**Files:**
- Create: `postal_ui_v2.py`
- Test: `tests/test_postal_ui_v2.py`

- [x] **Step 1: Implement the minimal pure helpers**

Create:

```python
from __future__ import annotations

from datetime import datetime


V2_FIELD_CONTRACT = {
    "editable": ["Name", "TransType", "追加", "PRC ID/PCCC", "Description", "HSCode", "Value", "Quantity"],
    "display_only": ["製單", "Order No.", "Country", "No."],
    "system_calculated": ["TotalValue(USD)", "TotalValue(JPY)"],
}


def apply_batch_selection(
    selected_by_order: dict[str, bool],
    current_order_ids: list[str],
    action: str,
) -> dict[str, bool]:
    updated = dict(selected_by_order)
    target_value = {"select_all": True, "clear_all": False}[action]
    for order_id in current_order_ids:
        updated[str(order_id)] = target_value
    return updated


def format_secondary_rate_badge(rate: float | None, rate_date: str) -> str:
    rate_text = f"{rate:.2f}" if rate else "N/A"
    date_text = ""
    if rate_date:
        try:
            date_text = datetime.strptime(rate_date, "%Y-%m-%d").strftime("%y/%m/%d")
        except ValueError:
            date_text = str(rate_date)
    return f"USD/JPY {rate_text}" + (f" · {date_text}" if date_text else "")


def v2_field_contract() -> dict[str, list[str]]:
    return {key: list(value) for key, value in V2_FIELD_CONTRACT.items()}
```

The action mapping must raise `KeyError` for unknown actions rather than silently changing selection state. The helper must only change selection state; it must never modify order rows, item values, source statuses, or write-back fields.

- [x] **Step 2: Run the focused tests and verify GREEN**

Run:

```powershell
python -m unittest tests.test_postal_ui_v2
```

Expected result: all pure-helper tests pass.

### Task 3: Add an isolated v2 renderer without changing v1

**Files:**
- Modify: `app.py`
- Test: `tests/test_postal_start_flow.py`

- [x] **Step 1: Add namespaced v2 state helpers**

Add v2-only keys such as `pending_v2_selected_by_order`, `pending_v2_name_*`, `pending_v2_trans_*`, `pending_v2_extra_trans_*`, `pending_v2_prc_id_*`, `pending_v2_pccc_*`, `pending_v2_items_*`, and `pending_v2_reset_*`. Do not reuse v1 widget keys. The v2 reset operation must restore the same source-derived defaults as v1 and must not write to Google Sheets.

- [x] **Step 2: Add a separate v2 tab while leaving the existing tab unchanged**

Change only the tab declaration to include a feature-branch-only label such as `郵局待打單（新版測試）`, keep the original `郵局待打單` tab and its existing body intact, then add a separate `with postal_v2_tab:` renderer. Do not move or delete v1 widgets during this task.

- [x] **Step 3: Render the v2 primary and secondary areas**

The v2 page must show:

```text
主要指標：待製單 | 已選取 | 本次完成
次要工具資訊：匯率 USD/JPY 157.79 · 26/08/06
批次選取：[選取全部] [清除全部]   3 / 8 筆已選取
```

Place the rate badge in the secondary utility/action row beside the existing refresh/reset controls, not in the primary KPI row. Give it `white-space: nowrap`, `min-width: 0`, and a responsive flex/wrap rule so it remains readable without overlaying the KPI row or creating a large extra vertical block. The v2 primary KPI row must contain only the three operational counts.

- [x] **Step 4: Render complete v2 order cards with the existing field contract**

Each v2 order card must retain the following behavior:

| Card field | V2 display | Existing downstream behavior to preserve |
|---|---|---|
| 製單 | Checkbox | Controls whether the order enters the current batch only; no source-sheet write |
| Order No. | Read-only | Remains the order identity/key for selection, status, and write-back |
| Country | Read-only | Continues to drive country-specific validation and PRC ID/PCCC handling |
| TotalValue(USD) | Read-only calculated | Recomputed from item Value × Quantity |
| TotalValue(JPY) | Read-only calculated | Recomputed from USD total and the loaded rate under the existing fallback rules |
| Name | Text input | Current edited recipient name is used for the actual submission |
| TransType | Selectbox | Current selected primary mail type is used for the actual submission |
| 追加 | Selectbox | Current additional type expands the order using the existing duplicate/expansion logic |
| PRC ID / PCCC | Conditional text input | Existing country-specific required-ID validation and submission mapping remain unchanged |
| 恢復預設 | Button | Restores the source-derived editor state only |
| No. | Read-only table column | Displays the internal `Content` sequence index; the v2 adapter maps it back to `Content` before calling existing editor logic |
| Description | Editable table cell | Current value is submitted as the item description |
| HSCode | Editable table cell | Current value is sanitized and passed through existing HS-code logic |
| Value | Editable table cell | Current value is submitted and participates in total recomputation |
| Quantity | Editable table cell | Current value is submitted and participates in total recomputation |

Use the same `build_pending_item_frame`, `apply_pending_order_editor_values`, `expand_pending_orders_for_trans_types`, `_zero_value_warning_lines`, `_required_id_warning_lines`, and `_pending_data_warning_lines` logic as v1. V2 may change layout and widget keys only. The v2 table adapter must rename the internal `Content` column to `No.` for display and restore `Content` before applying edits; it must never delete or rename the source dataframe's underlying item fields.

- [x] **Step 5: Wire v2 batch actions to the existing start flow**

`選取全部` and `清除全部` must update only the namespaced v2 selection map for the current pending order IDs and rerun the page. The v2 `開始製單` action must call the existing `_start_job` with the v2-prepared frame. It must retain the same disabled conditions and preflight validation as v1. Successful completion, failure alerts, status table, debug-log visibility, pending-card filtering, and Google Sheets updates must continue to use the existing shared result flow.

- [x] **Step 6: Run focused tests and syntax checks**

Run:

```powershell
python -m unittest tests.test_postal_ui_v2 tests.test_postal_start_flow
python -m py_compile app.py postal_ui_v2.py
```

Expected result: focused tests pass and both files compile successfully.

### Task 4: Add v2-only responsive styling and interaction safeguards

**Files:**
- Modify: `app.py`
- Test: `tests/test_postal_start_flow.py`

- [x] **Step 1: Add scoped CSS classes for v2 only**

Add selectors prefixed with `.postal-v2-` for the selection block, rate badge, editability legend, and card metadata. Keep the existing v1 selectors unchanged. Use solid colors only; do not add `linear-gradient`, `radial-gradient`, or gradient background declarations to v2. Base the v2 palette on the referenced example's neutral dark tokens: app background near `#0a0d13`/`#0f1115`, surface near `#171a21`, border near `#262b36`/`#3a4152`, primary text near `#e8eaf0`/`#f5f7f9`, muted text near `#8b93a7`, and a restrained indigo-blue accent for active controls. Keep the right-side order/operation panel and order cards on a darker solid surface than the surrounding content area. The selection controls must remain visually grouped, show disabled states when all/none are already selected, and wrap at narrow widths without overlapping the card content.

- [x] **Step 2: Add editability cues without changing field semantics**

Use a small v2-only legend and consistent border/accent treatment so users can distinguish editable controls from display-only/system-calculated values. Do not turn any currently disabled field into an input, and do not add a second source of truth for order data.

- [x] **Step 3: Run source and style assertions**

Assert that v2 CSS is scoped, contains no gradient declarations, uses the dark right-panel token, the rate badge is single-line, the v1 `toolbar-info` code remains present, and v2 contains no user-facing `內容品名（僅顯示）` column while its internal `Content` mapping remains covered. Run the focused tests again.

### Task 5: Verify the feature branch with mock data before any merge

**Files:**
- Create: `tests/fixtures/postal_ui_v2_orders.json` (only if a fixture file makes the UI smoke test clearer)
- Modify: `tests/test_postal_ui_v2.py`
- Modify: `tests/test_postal_start_flow.py`

- [x] **Step 1: Add mock regression coverage for representative orders**

Use mock rows covering: one ordinary order, one multi-item order, one China/Korea conditional-ID order, one order with edited Value/Quantity, and one order with an additional TransType. Verify that v2 produces the same submission frame as the existing preparation logic when the same values and selection state are supplied.

- [x] **Step 2: Run the full local verification suite**

Run:

```powershell
python -m unittest discover -s tests -p "test_*.py"
python -m py_compile app.py postal_ui_v2.py pending_editor.py postal_ui_feedback.py
git diff --check
```

The existing 245-test baseline must remain green, plus all new v2 tests. Any failure must be fixed on the feature branch before deployment.

- [x] **Step 3: Run a local Streamlit smoke test with the v2 page**

Start the app with the repository's existing Streamlit command and verify with mock data or the existing test harness:

1. The original `郵局待打單` page still renders its existing card fields and controls.
2. The `郵局待打單（新版測試）` page shows the grouped selection block, compact secondary rate badge, full order cards, and editability legend.
3. Selecting and clearing all updates only v2 checkboxes and the selected count.
4. Editing Name, TransType, PRC ID/PCCC, Description, HSCode, Value, Quantity changes the v2 submission frame and recalculates totals according to existing rules.
5. Start-flow mock data reaches the existing job entry point without changing source rows before success.
6. Success and failure result handling remains identical to v1.

- [ ] **Step 4: Capture comparison evidence**

The approved visual preview is the design acceptance reference. A live HTTP screenshot was not captured in this environment because local socket binding is blocked with WinError 10013; the official Streamlit `AppTest` mock smoke test was used instead and passes. A desktop/narrow live screenshot remains an acceptance follow-up before merge.

Capture screenshots at the current desktop viewport and a narrow viewport. Compare v1 and v2 for field presence, editability, selection behavior, rate placement, no overlap, and unchanged action semantics. Keep the v2 feature branch and do not merge or deploy as the default until this evidence is reviewed.

### Task 6: Merge gate and formal deployment

**Files:**
- No production-branch files until the acceptance gate passes.

- [ ] **Step 1: Commit the isolated v2 work**

Commit the plan, pure helpers, renderer, scoped styles, and tests on `codex/postal-ui-v2` (or the current isolated feature branch). Do not push directly to `main`.

- [ ] **Step 2: Review the acceptance checklist**

Merge is allowed only when all of the following are true:

- v1 page remains behaviorally unchanged.
- v2 card fields exactly match the existing editability contract.
- v2 batch selection changes selection state only.
- v2 rate is visible but secondary and non-overlapping.
- mock start-flow outputs match the existing pipeline.
- full tests, compile checks, and `git diff --check` pass.
- user has reviewed the v2 screenshots and explicitly accepts the page.

- [ ] **Step 3: Merge and deploy only after explicit acceptance**

After acceptance, merge the feature branch into the deployment branch, run the full verification suite again, and deploy. Until then, leave the formal `郵局待打單` page and production default unchanged.
