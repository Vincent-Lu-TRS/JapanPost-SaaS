# memory.md - Durable Project Memory

Last updated: 2026-06-20 JST

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
- Recompose payload:
  - `Name (PRC ID:value)` for China.
  - `Name (PCCC:value)` for Korea.
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
