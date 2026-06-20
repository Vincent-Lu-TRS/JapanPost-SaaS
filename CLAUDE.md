# CLAUDE.md - JP Post Label Maker Handoff

Last updated: 2026-06-20 JST

This is the first file to read when continuing work on the JP Post Streamlit app.

## Project Identity

- Production app: https://jppost.streamlit.app/
- Repository: https://github.com/Vincent-Lu-TRS/JapanPost-SaaS
- Runtime: Streamlit Cloud, free-tier friendly.
- App model: staff use only the web UI; Japan Post automation runs server-side in headless Playwright.
- Auth model: Google OAuth / Streamlit native auth, restricted to `@tkrjm.co.jp` and explicit whitelist users.
- Main business flow: read pending orders from Google Sheets, avoid duplicate labels, edit/validate pending label data, automate Japan Post, download PDFs, upload to Google Drive, and write results back to Sheets.

Read next:

1. `ERP_JP_Post_Labeling_Project_Handoff_2026-06-19.md`
2. `HANDOFF_CLAUDE.md` for older auth / automation context
3. `SaaS_Requirements.md` if working in the original project folder

## Current Source Of Truth

The current active working tree used for the latest Streamlit Cloud updates is:

```text
C:\Users\shaku\AppData\Local\Temp\jppost-remote-fix
```

Latest confirmed commit at this handoff:

```text
a849f1c feat: split command rows and recipient IDs
```

Important files:

- `app.py` - Streamlit UI, layout, session state integration, pending order editing surface.
- `pending_editor.py` - pending order grouping, editable row state, value/quantity/HS code normalization.
- `job_control.py` - job start/progress state, actual sent result display state.
- `fx_rates.py` - USD/JPY lookup and display data.
- `auth.py` - Google OAuth / Streamlit native auth.
- `bot/` - Japan Post, Sheets, Gemini, Drive automation logic.
- `tests/` - unit tests.

## Latest UI Direction

Do not continue trying to force custom text blocks and Streamlit native widgets into one perfectly aligned single row. Streamlit inputs/selectboxes/buttons have different box models and rerun-generated wrappers, so repeated CSS alignment patches caused visual drift, clipping, and fragile selectors.

Use type-homogeneous rows instead:

- Info rows: text only.
- Operation rows: Streamlit widgets only.
- Tables: compact dark table style, with minimal CSS assistance.

Current intended page structure:

- Compact header:
  - Left: `JP Post 製單系統`
  - Right: current user and `登出`
  - Keep the divider, but keep top/bottom spacing tight. This is an operations tool, not a hero page.
- Pending toolbar, split into two rows:
  - Info row: `待打單預覽`, `USD/JPY 161.20｜26/06/20`, `待製單 7`, `本次完成 0`
  - Operation row: `最大處理 [20]（0=全部）`, `重新讀取待製單`, `開始自動製單`, `恢復全部預設`
- Order card, split into three rows:
  - Info row: `Order No. WhoWht-Test1`, `Country GERMANY`, `USD 23.25`, `JPY 3751`
  - Operation row: `Name [Fabian Kohlhaas]`, `TransType [國際小包]`, optional `PRC ID` / `PCCC`, `恢復預設`
  - Item table: `Content`, `Description`, `HSCode`, `Value`, `Quantity`

Current copy:

- App title: `JP Post 製單系統`
- Section title: `待打單預覽`
- Reset-all button: `恢復全部預設`
- Single-order reset button: `恢復預設`

The user explicitly accepted the short reset wording.

## Visual Rules

- Keep dark ERP-style operational UI.
- Amber / yellow-orange is the visual anchor color.
- Use amber sparingly for:
  - `JP Post 製單系統`
  - `待打單預覽`
  - labels such as `Order No.`
- Do not create a marketing hero page.
- Reduce vertical spacing wherever it does not hurt readability.
- Do not use broken image/icon markup for the pending section. If an icon fails, prefer text-only amber heading.
- Avoid broad brittle CSS such as global `:has()` selector chains. If a marker selector is unavoidable, scope it narrowly.
- Keep diagnostic/debug output low priority: use an expander collapsed by default, with max height around 220-260px when expanded.
- Never put `st.success`, `st.info`, or `st.warning` inside the toolbar after starting a job. Use `st.toast()` or a fixed-height status chip so the toolbar does not jump.

## Data And Editing Rules

Do not change backend APIs, GAS logic, request payload field names, or core automation flow unless the user explicitly asks.

Current editable pending-order requirements:

- `Name` is editable and stored in `st.session_state`.
- Streamlit rerun must not overwrite edited `Name`, `PRC ID`, or `PCCC`.
- Single-order reset restores that order's original parsed values.
- Reset-all restores all order edits to defaults.
- `TransType` remains selectable and visually shown only once.
- `HSCode` is editable and must display/send only pure digits.
  - Examples:
    - `9404.90` -> `940490`
    - `9404 90` -> `940490`
    - `HS:940490` -> `940490`
    - `9404-90` -> `940490`
- If an AI HS code response contains symbols or labels, normalize to pure digits before UI display and payload send.
- Total USD is calculated from item values and quantities.
- JPY uses current editable totals when an exchange rate is available; otherwise preserve source value.

## China / Korea Recipient ID Rules

Raw `Shipping Name` may include recipient ID data:

```text
zhuxiaomu (PRC ID:110108198309121213)
zhuxiaomu (PRC ID：110108198309121213)
Eunseo Ha (PCCC:P180026936191)
Eunseo Ha (PCCC：P180026936191)
```

Frontend display must split these into fields:

- China / CHINA / 中國 / 中国:
  - `Name`
  - `PRC ID`
- Korea / KOREA / 韓國 / 韩国:
  - `Name`
  - `PCCC`
- Other countries:
  - only `Name`

Payload/output must recombine to the Japan Post expected format:

- China: `zhuxiaomu (PRC ID:110108198309121213)`
- Korea: `Eunseo Ha (PCCC:P180026936191)`

Validation on `開始自動製單`:

- China orders without PRC ID must not be sent.
- Korea orders without PCCC must not be sent.
- UI message:
  - `中國訂單需填入 PRC ID 才能製單`
  - `韓國訂單需填入 PCCC 才能製單`

## Output Result Display

After label creation, show compact actual-sent results. Do not infer from original source rows after edits.

Examples:

```text
已製單｜Name Fabian Kohlhaas｜TransType 國際小包｜HS 940490｜USD 23.25｜JPY 3750
已製單｜Name zhuxiaomu｜PRC ID 110108198309121213｜TransType 國際小包｜HS 940490｜USD 23.25｜JPY 3751
已製單｜Name Eunseo Ha｜PCCC P180026936191｜TransType 國際小包｜HS 940490｜USD 4.87｜JPY 786
```

## Local Setup Notes

Use:

```powershell
python -m streamlit run app.py --server.port 8502
```

Local secrets must be named exactly:

```text
.streamlit\secrets.toml
```

If Windows creates `secrets.toml.txt`, Streamlit will not read it.

Minimum local install:

```powershell
python -m ensurepip --upgrade
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If auth component import fails:

```powershell
python -m pip install extra-streamlit-components
```

If Playwright browser dependencies are missing:

```powershell
python -m playwright install chromium
```

## Verification Commands

Before reporting completion or pushing:

```powershell
python -m py_compile app.py pending_editor.py job_control.py
python -m unittest discover -s tests
```

Latest known verification for commit `a849f1c`:

- `python -m py_compile app.py pending_editor.py job_control.py` passed.
- `python -m unittest discover -s tests` passed: 101 tests OK.
- Remote visual/DOM check confirmed toolbar split rows, order-card rows, and PRC ID/PCCC fields after deploy.

## Deployment Notes

- Push to GitHub `main`; Streamlit Cloud auto-deploys from the repo.
- Streamlit Cloud deployment can lag after push. Wait and refresh before assuming a UI change failed.
- The in-app browser often points to production `https://jppost.streamlit.app/`; local test URL is usually `http://localhost:8502`.

## Current Open UI Follow-ups

The latest user direction before this handoff:

- Keep header divider spacing even tighter.
- Toolbar must remain split:
  - info row with status text only
  - operation row with widgets only
- `待製單` and `本次完成` counts should be visually stronger.
- `最大處理 [20]（0=全部）` should remain tight as one group.
- Order cards must remain split:
  - info row text only
  - operation row widgets only
- Order No must not be truncated.
- TransType must not be truncated.
- Name input width should stay compact.
- China/Korea ShippingName split and validation must remain intact.
- Item table column widths still matter:
  - Content: 70px
  - Description: primary/flex width
  - HSCode: 120px
  - Value: 100px
  - Quantity: 90px
- Card vertical padding can still be compressed if readability remains good.

## Do Not Break

- Google OAuth company-domain lock.
- Server-side headless Japan Post automation.
- Requests-first Japan Post login path and Playwright cookie injection.
- Duplicate-label prevention.
- HS code fallback and normalization.
- Google Sheets read/write contracts.
- Existing session state semantics for pending edits and job control.
- Existing tests.
