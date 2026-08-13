# JPPOST cache, writeback, and postal-entry local evidence

Date: 2026-08-13
Branch: `codex/jppost-cache-entry-writeback-20260813`

## Implemented scope

- Logged-in sessions request pending postal orders and cross-border picking data automatically, with a shared 20-minute process-local refresh coordinator and an active-session `st.fragment` tick.
- Refresh failures retain the last successful snapshot and show concise user-facing copy. Busy jobs and dirty editors are not overwritten; explicit reset/reload paths remain available.
- Postal writeback now uses direct target worksheet GID lookup, formula-aware row anchoring, grid expansion, exact `(order_id, tracking)` idempotency, explicit `additional` package handling, conflict/partial-write classification, and B/C/D/J readback verification.
- A same-process retry path replays only the existing tracking-bearing writeback; it never reruns carrier automation. A mixed primary/additional batch remains visible until every submitted package is terminal-success. Protected pending snapshots no longer trigger a repeated rerun loop, and preflight read failures preserve each package's transport/role identity.
- The newer postal UI is now the only postal entry and is labeled `待製郵便運單`. Existing v2 layout, controls, CSS, and internal widget keys were retained.

## Local verification

Commands run on synthetic data only:

| Check | Result |
|---|---|
| Cache/UI acceptance group | 120 passed, 1 skipped (Streamlit AppTest unavailable locally) |
| Package/writeback acceptance group | 210 passed |
| Task 7 sheets/E2E group | 50 passed |
| Task 8 outcome/start-flow group | 76 passed; follow-up focused 51 passed |
| Full unittest discovery | 365 passed, 1 AppTest skip |
| `tests.test_app_imports` alone | 3 passed |
| Tracked Python `py_compile` | passed |
| `git diff --check` | passed |

The former Python 3.14 import-reload mock interaction in `test_app_imports` was reproduced with a stale module entry and fixed by `aa7fd24`; the complete discovery now passes. No test connected to Google Sheets, Japan Post, Google Drive, or external carrier services.

## Commit sequence

- `966a440`, `1240e87`, `105303c`, `83213ea`
- `c053496`, `6f759ca`, `a64e36a`, `93d5a9f`
- `3e1d1cd`, `c5ddf3e`, `2d7b0f7`, `5568ef5`
- `0274e96`, `0e8cb62`, `e619aef`, `aa7fd24`

Pre-existing untracked `.planning/`, `backups/`, and `tmp/` directories were left untouched and were not staged.
