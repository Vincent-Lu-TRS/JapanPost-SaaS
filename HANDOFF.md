# JapanPost-SaaS Session Handoff

Last updated: 2026-08-04 JST

## Purpose

This is the canonical continuation entry for JapanPost-SaaS. Read `memory.md` for durable behavior and this file for the current boundary, evidence, and next task.

## Current Verified State

- Repository: `https://github.com/Vincent-Lu-TRS/JapanPost-SaaS`
- Local repo: `C:\Users\shaku\個人\Claude Cowork\jppost\tmp\streamlit-deploy-JapanPost-SaaS`
- Production: `https://jppost.streamlit.app/`
- Branch/entrypoint: `main` / `app.py`
- `main` and `origin/main`: `8d9c2ae7953e912a2a221f67e7bbc166ebe08d84`
- PR #1: merged recipient address-width fix (`3bb0c64`, merge `c2c6d81`).
- PR #2: merged legacy postal item and HS precheck fix (`f26332d`, merge `8d9c2ae`).
- Post-merge unit verification on 2026-08-02: 209 tests passed.
- Authenticated production UI rendered successfully after deployment, including the four main tabs.
- No affected order was automatically retried after deployment, preventing accidental duplicate labels.

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
