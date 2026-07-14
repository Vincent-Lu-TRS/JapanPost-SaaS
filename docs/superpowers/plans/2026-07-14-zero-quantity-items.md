# Zero-Quantity Postal Items Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Exclude canceled content rows, represented by blank or non-positive quantities, from every Japan Post shipment stage.

**Architecture:** Add one strict quantity parser at the automation boundary and use it for both the requests and Playwright paths. Correct editor totals independently so UI totals match the filtered shipment, while preserving source item indexes.

**Tech Stack:** Python 3.12, pandas, unittest, requests, Playwright

---

### Task 1: Define shipment quantity behavior

**Files:**
- Modify: `tests/test_automation_helpers.py`
- Modify: `bot/automation.py`

- [x] Add failing tests proving blank, zero, and negative quantities are skipped; later positive items retain their original indexes; text and fractional quantities raise a descriptive `ValueError`.
- [x] Run `python -m unittest tests.test_automation_helpers.AutomationHtmlTests.test_iter_content_items_skips_canceled_items -v` and confirm it fails against the current fallback-to-one behavior.
- [x] Add `parse_shipment_quantity(raw_value, item_index)` and make `_iter_content_items` return only positive integer quantities.
- [x] Run the focused automation helper tests and confirm they pass.

### Task 2: Correct declared totals

**Files:**
- Modify: `tests/test_pending_editor.py`
- Modify: `pending_editor.py`

- [x] Add failing tests proving blank, zero, and negative quantities contribute zero, while positive integers multiply declared value normally and invalid formats raise a descriptive error.
- [x] Run the focused pending-editor tests and confirm the current `(quantity or 1)` implementation fails.
- [x] Replace fallback-to-one total calculation with the confirmed source-sheet quantity rules.
- [x] Run the focused pending-editor tests and confirm they pass.

### Task 3: Align both automation paths

**Files:**
- Modify: `bot/automation.py`
- Modify: `tests/test_automation_helpers.py`

- [x] Add regression coverage for the shared item iterator used by direct Playwright content loops.
- [x] Update requests all-canceled error text to explain that no positive-quantity content remains.
- [x] Update Playwright ePacket and other direct content loops to apply the same parser before filling Japan Post fields.
- [x] Run `python -m unittest tests.test_automation_helpers -v` and confirm all automation tests pass.

### Task 4: Verify and publish

**Files:**
- Modify: `docs/superpowers/plans/2026-07-14-zero-quantity-items.md`

- [x] Run `python -m unittest discover -s tests -v` and require zero failures.
- [x] Run `python -m py_compile app.py pending_editor.py shipment_quantity.py bot/automation.py` and `git diff --check`.
- [x] Review the final diff against every confirmed behavior in the design spec.
- [x] Commit only the spec, plan, implementation, and tests; push `codex/fix-zero-quantity-items` for integration.
