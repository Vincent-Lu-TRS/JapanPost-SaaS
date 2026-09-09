# JapanPost-SaaS Session Handoff

Last updated: 2026-09-09 JST

## Purpose

This is the canonical continuation entry for JapanPost-SaaS. Read `memory.md` for durable behavior and this file for the current boundary, evidence, and next task.

## Current Verified State

- Repository: `https://github.com/Vincent-Lu-TRS/JapanPost-SaaS`
- Local repo: `C:\Users\shaku\個人\Claude Cowork\jppost\tmp\streamlit-deploy-JapanPost-SaaS`
- Production: `https://jppost.streamlit.app/`
- Branch/entrypoint: `main` / `app.py`
- `main` and `origin/main`: current branch includes the deployed code baseline `b27c114` plus documentation-only follow-ups.
- PR #1: merged recipient address-width fix (`3bb0c64`, merge `c2c6d81`).
- PR #2: merged legacy postal item and HS precheck fix (`f26332d`, merge `8d9c2ae`).
- Latest local verification before deployment: 388 tests passed; `compileall` and `git diff --check` passed.
- The production app was fully rebooted from the Streamlit Cloud management panel after the GitHub update; this is required because an “Updated app” message alone can leave an existing Python process holding old modules.
- A single explicitly authorised production smoke run completed 4/4 orders. The UI showed real progress `0/4 → 1/4 → 3/4 → 4/4`, the result table showed all rows as completed, four PDFs were present in the configured Drive folder, and the target sheet contained the corresponding recipient/order/tracking writebacks.
- The historical source/target consistency notice about rows with tracking but missing completion evidence is non-blocking. Those rows are excluded from the pending list and must not be interpreted as the cause of a current batch failure.

## 2026-09-09 Production Reliability Record

This section supersedes the older deployment observations below for the current `main` version.

### Root cause of the repeated production failure

1. The browser process could close immediately in the 1 GB Streamlit container (`TargetClosedError`), even though Playwright installation returned success.
2. The repository had been updated, but the already-running Streamlit process still held the old `bot.automation` module. This is why production logs continued to show the old automation build after “Updated app”.
3. The whole-table source/target consistency message was worded too strongly. It was a data-integrity reminder, not a blocker for the four eligible rows.

### Durable fixes now in `main`

- `bot/playwright_runtime.py` provides the verified Chromium shared-library chain without apt/root.
- `bot/automation.py` retries one startup-only browser failure with low-resource flags and records a redacted failure category.
- `app.py` fingerprints and reloads a changed automation module before starting a job, with a lock to avoid concurrent reloads.
- `bot/sheets.py` labels the source/target mismatch as non-blocking and excludes those historical rows from pending orders.

### Required deployment/release check

After every production code update:

1. Wait for dependency processing to finish.
2. Use Streamlit Cloud **Manage app → Reboot app**; do not rely on “Updated app” alone.
3. Confirm a fresh session can load the four tabs and the pending page.
4. For a safe batch, verify the new automation build, changing progress counters, final completed statuses, Drive PDF creation, and target-sheet writeback. Never retry a real order solely to inspect an old log.

The cumulative Cloud log may retain previous `TargetClosedError` entries. The current result table plus Drive and target-sheet evidence are the authoritative smoke-test result.

## Completed In The Closed Session

1. Diagnosed `WhoWhy1580` item progression failures and added failure snapshots/diagnostics during the earlier debugging cycle.
2. Added a persistent job mask and locked start controls while a postal job is active; the mask now clears at actual completion.
3. Fixed Streamlit import/startup regressions encountered during deployment updates.
4. Corrected item-confirm sequencing so quantities are present before Japan Post validates Next.
5. Defined canceled-item behavior: blank, zero, and negative explicit quantities are skipped.
6. Added legacy single-item fallback so top-level postal fields are not converted into a zero-value item.
7. Corrected non-ASCII recipient address width handling for Japan Post fields.
8. Changed missing EU HS lookup from a hard exception to a warning-and-continue path.

## Important Boundaries

- Do not infer production order success solely from source-sheet status or the presence of a later manually created tracking number.
- Do not retry real orders during analysis; duplicate prevention is a core business invariant.
- Do not modify Shopify orders, inventory, status, Japan Post records, Google Sheets, Streamlit settings, or production code without explicit approval.
- Do not combine Japan and Taiwan legal/entity data. The next task is technical architecture analysis, not entity consolidation.
- Treat `HANDOFF_CLAUDE.md`, `CLAUDE_SESSION_NOTES.md`, and dated handoffs as history. `HANDOFF.md` plus `memory.md` are current.

## Next Session: Read-Only Integration Assessment

Objective: determine whether the complete JapanPost-SaaS functions, operator instructions, and cross-border Shopify order information can be integrated into the CB-ERP ERPConsole as one coherent operator interface.

Read-only means:

- no file edits, commits, pushes, PRs, deployments, or settings changes;
- no mutation of Shopify, Google Sheets/Drive, Japan Post, Streamlit, OAuth, or ERP data;
- no real label generation, order retries, status updates, inventory updates, or webhook registration;
- redact credentials and customer personal data from notes and reports.

Sources to inspect:

- This repository: `app.py`, `auth.py`, `job_control.py`, `pending_editor.py`, `postal_ui_feedback.py`, `bot/automation.py`, `bot/sheets.py`, tests, `memory.md`, and `DEPLOY_GUIDE.md`.
- Original business requirements: `C:\Users\shaku\個人\Claude Cowork\jppost\SaaS_Requirements.md`.
- CB-ERP repository and its current ERPConsole UI/data contracts, after checking AI coordination ownership and locks.
- Shopify store mappings and business map from the AI governance folder; keep Japan and Taiwan entities isolated.
- Connected Shopify/Sheets sources only through read-only APIs/connectors when available and only to the minimum extent needed to identify fields and ownership.

Required deliverable:

1. A complete JapanPost capability and operator-instruction inventory.
2. A Shopify-to-JapanPost-to-CB-ERP order-field mapping with source of truth, direction, owner, and sensitivity.
3. A gap matrix for auth, tenant/store identity, duplicate prevention, job state, long-running automation, PDF/Drive artifacts, writeback, observability, and error recovery.
4. A recommendation among: shared UI with JapanPost as a separate service/API, controlled deep-link/embedded surface, or codebase consolidation.
5. A staged integration plan with explicit non-goals, migration risks, acceptance criteria, and a rollback boundary.
6. A clear verdict: fully integrable now, integrable after prerequisites, or not advisable, with evidence and unverified items separated.

## Initial Architecture Hypothesis, Not Yet A Decision

The likely robust direction is one ERPConsole operator interface backed by a separately deployable JapanPost service/API and shared order/job contracts. Directly merging Streamlit automation code into the CB-ERP frontend is unlikely to be the cleanest boundary because authentication, long-running headless automation, Streamlit reruns, and duplicate-prevention state have different lifecycle requirements. The next session must verify this against both codebases and actual data contracts before recommending implementation.

## Verification Commands For Future Write Sessions

```powershell
$env:PYTHONDONTWRITEBYTECODE = "1"
python -m py_compile app.py pending_editor.py job_control.py bot\automation.py
python -m unittest discover -s tests
git diff --check
```

For the immediate next session, remain read-only and do not run commands that create caches or mutate the worktree.
