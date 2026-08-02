# Japan Post Address Width Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make M060505 recipient addresses safe for Japan Post's non-ASCII width limits without changing source data.

**Architecture:** Keep the change inside `bot/automation.py` at the address preparation boundary. Normalize Latin diacritics and Unicode punctuation, preserve other scripts, measure Japan Post width units, and split without silent truncation. Extend the existing helper tests with the exact `imy2038230` regression.

**Tech Stack:** Python 3.12, `unicodedata`, `unittest`, requests

---

### Task 1: Normalize and measure postal address text

**Files:**
- Modify: `tests/test_automation_helpers.py`
- Modify: `bot/automation.py`

- [ ] Add a failing test using `Aleea Locotenent Gheorghe Stâlpeanu 11‚ bl 8‚ sc B‚ et 4‚ ap 38‚ interfon 38` and require `Stalpeanu`, ASCII commas, and at most 80 Japan Post width units in Address 2.
- [ ] Add a failing test proving Latin diacritics are normalized while CJK text remains present.
- [ ] Run the two focused tests and confirm failure because normalization and weighted measurement are absent.
- [ ] Add `_normalize_japan_post_address_text()` using Unicode decomposition only for Latin letters plus an explicit punctuation map.
- [ ] Add `_japan_post_text_width()` with ASCII counting as one and other code points counting as two.
- [ ] Run the focused tests and confirm they pass.

### Task 2: Split without silent address loss

**Files:**
- Modify: `tests/test_automation_helpers.py`
- Modify: `bot/automation.py`

- [ ] Add failing tests proving each returned field obeys its 80/80/36 weighted limit and impossible overflow raises a descriptive `ValueError`.
- [ ] Replace character slicing in `_split_text_at_limit()` with width-aware word-boundary splitting.
- [ ] Normalize street and city before `_split_addr_to_bean_address_lines()` allocates fields.
- [ ] Preserve the existing short-address and PCCC placement behavior; use Address 1 only when normal Address 2/3 allocation cannot preserve all input.
- [ ] Raise `ValueError("日本郵局收件地址過長...")` when prepared text cannot fit instead of truncating it.
- [ ] Run `python -m unittest tests.test_automation_helpers -v` and require zero failures.

### Task 3: Improve diagnostics and verify

**Files:**
- Modify: `bot/automation.py`

- [ ] Extend M060505 request diagnostics with `add1_units`, `add2_units`, and `add3_units` alongside ordinary lengths.
- [ ] Run `python -m unittest discover -s tests -v` and require zero failures.
- [ ] Run `python -m py_compile app.py bot/automation.py` and `git diff --check`.
- [ ] Review the diff against every scope and error-handling requirement in the design spec.
- [ ] Commit only the spec, plan, implementation, and tests; push `codex/fix-postal-address-width` for domain-owner review.
