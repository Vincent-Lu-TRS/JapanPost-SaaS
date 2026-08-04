# memory.md - Durable Project Memory

Last updated: 2026-08-04 JST

## Canonical Status

- Production: `https://jppost.streamlit.app/`
- Repository: `https://github.com/Vincent-Lu-TRS/JapanPost-SaaS`
- Production branch and entrypoint: `main` / `app.py`
- Current verified `main`: `8d9c2ae7953e912a2a221f67e7bbc166ebe08d84`
- PR #1 and PR #2 were merged on 2026-08-02.
- The production app was verified after PR #2 to render the authenticated Cross-Border UI rather than Streamlit's error page.
- The four top-level views are cross-border picking labels, Japan Post pending labels, usage instructions, and read diagnostics.
- `HANDOFF.md` is the canonical continuation file. Older handoff files are historical evidence only.

## Durable Architecture

- Streamlit is the operator UI; browser automation runs server-side/headless.
- Google authentication must remain restricted to `@tkrjm.co.jp` or explicit whitelist entries.
- Google Sheets is the operational source and writeback surface for pending orders, results, and duplicate-prevention state.
- Japan Post navigation is requests-first. Avoid injecting legacy Japan Post HTML into Playwright because Streamlit Cloud previously killed Chromium during that path.
- Keep Playwright only for steps that truly require browser rendering or browser-only interaction; prefer structured HTTP form parsing/submission and direct PDF download.
- Successful PDF upload and source-sheet writeback are part of job completion. Do not mark an order complete before both are confirmed.
- Never commit Streamlit secrets, OAuth client secrets, Japan Post credentials, API keys, or service-account JSON.

## Shipment Item Semantics

- Explicit quantity `0`, a negative quantity, or a blank quantity means that shipment item is canceled and must be skipped.
- Do not silently convert a blank or invalid explicit quantity to `1`.
- Legacy single-item rows may store only the top-level fields `郵局內容物`, `郵局申告金額(USD)`, and `数量`/`數量集合`.
- Only when numbered item content is absent may item 1 fall back to those top-level legacy fields.
- If numbered item content exists, its explicit quantity remains authoritative; a blank numbered quantity still means canceled.
- Recalculating editor values must preserve untouched legacy item description, amount, and quantity instead of overwriting the postal amount with `0.00`.

## Japan Post Form Rules

- Recipient address splitting must respect Japan Post's field limits using Japan Post's effective character width, not Python character count alone.
- Preserve the full address across `addrToBean.add1`, `addrToBean.add2`, and `addrToBean.add3`; do not truncate silently.
- `addrToBean.add2` has been observed with `maxlength=80`; `addrToBean.add3` with `maxlength=36`.
- PRC ID/PCCC belongs at the end of the address, not in `addrToBean.sortNum`.
- An unavailable EU HS Code is a warning, not a hard stop. Leave it blank and continue; never invent a code.
- Failure diagnostics should retain the relevant Japan Post response markers and capture a failure snapshot when available, without exposing secrets.

## 2026-08-02 Regression Fixes

- `3bb0c64` / PR #1: handle non-ASCII recipient addresses using Japan Post-compatible width limits. This fixed the Romania address path used by `imy2038230`.
- `f26332d` / PR #2: preserve legacy single-item postal values and make missing HS Code lookup non-fatal. This fixed the `0.00` Hong Kong payload path seen for `imy2038370` and the HS precheck stop seen for `imy2038230`.
- Merge commits: `c2c6d81` (PR #1) and `8d9c2ae` (PR #2).
- Post-merge verification on 2026-08-02: `python -m unittest discover -s tests` reported 209 tests, all passing.
- The two production orders were not automatically retried after deployment to avoid duplicate labels. Their later business outcome is not established by the code merge alone.

## User Preferences

- The user wants a high-efficiency shipping ERP work screen, not a showcase or landing-page UI.
- Prefer dense, scan-friendly layouts with clear hierarchy and minimal vertical waste.
- Dark theme is preferred, inspired by the cross-border shipping ERP reference.
- Amber / yellow-orange is the preferred visual anchor color.
- Avoid decorative hero sections, large cards, ornamental gradients, and marketing copy.
- The user accepts the short button labels:
  - `恢復全部預設`
  - `恢復預設`

## Streamlit UI Lessons

- Do not force custom text cells and Streamlit-native widgets into the same perfectly aligned framed row.
- Native `st.text_input`, `st.selectbox`, `st.number_input`, and `st.button` have different generated wrappers and vertical metrics.
- The stable pattern is:
  - Text/info row separately.
  - Widget/action row separately.
- Use `st.columns`, `st.container`, `st.expander`, `st.caption`, `st.markdown`, `st.text_input`, `st.selectbox`, `st.number_input`, and `st.button` first.
- CSS should only tighten spacing, typography, widget height, and table appearance.
- Avoid broad brittle CSS hacks such as global `:has()` marker chains.
- Do not place `st.success` / `st.info` / `st.warning` in the toolbar after button clicks because it causes layout jumps. Use `st.toast` or a fixed-height chip.

## Current UI Direction

- Header:
  - `JP Post 製單系統`
  - user and logout on the right
  - compact divider spacing
- Toolbar:
  - Row 1 info only:
    - `待打單預覽`
    - `USD/JPY 161.20｜26/06/20`
    - `待製單 7`
    - `本次完成 0`
  - Row 2 controls only:
    - `最大處理 [20]（0=全部）`
    - `重新讀取待製單`
    - `開始自動製單`
    - `恢復全部預設`
- Order card:
  - Row 1 info only:
    - `Order No.`
    - `Country`
    - `USD`
    - `JPY`
  - Row 2 controls:
    - `Name`
    - `TransType`
    - optional `PRC ID` or `PCCC`
    - `恢復預設`
  - Row 3 item table.

## Data Behavior To Preserve

- `Name`, `PRC ID`, and `PCCC` edits persist in `st.session_state` across reruns.
- Reset single order restores parsed original recipient fields.
- Reset all restores all editable fields to source defaults.
- Start job uses currently displayed frontend values, not raw original values.
- `HSCode` must be pure digits in UI and payload.
- Result display must show actual sent data.

## China / Korea Recipient Rules

- China orders show `PRC ID`; Korea orders show `PCCC`; other countries do not.
- Parse:
  - `PRC ID:` and `PRC ID：`
  - `PCCC:` and `PCCC：`
- Japan Post payload:
  - Name is sent as clean recipient name plus order id, e.g. `kim sang woo imy2036430`.
  - PRC ID / PCCC is removed from the name field and appended at the end of the address.
  - Full `Shipping Street` must be preserved.
  - Split recipient address across `addrToBean.add1`, `addrToBean.add2`, and `addrToBean.add3` to avoid Japan Post limits.
  - Keep PRC ID / PCCC in the final address line; do not use `addrToBean.sortNum` for this value.
- Block start if:
  - China order lacks PRC ID.
  - Korea order lacks PCCC.

## Verification Habit

Before saying the work is done:

```powershell
python -m py_compile app.py pending_editor.py job_control.py
python -m unittest discover -s tests
```

If UI was changed, inspect the deployed Streamlit page or local `localhost:8502` with browser screenshots when feasible.

## Common Local Pitfalls

- Local Streamlit secrets file must be `.streamlit\secrets.toml`, not `secrets.toml.txt`.
- `extra_streamlit_components` missing affects legacy CookieManager auth fallback.
- Playwright browser dependencies may need `python -m playwright install chromium`.
- A `UnicodeDecodeError` in Playwright install stderr on Windows can be noisy; it is not necessarily the app failure.
