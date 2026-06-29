# Cross-Border Picking Label Current Status

Last updated: 2026-06-29

## Production

- App name: `Cross-Border製單系統`
- Production URL: `https://jppost.streamlit.app/`
- GitHub repo: `Vincent-Lu-TRS/JapanPost-SaaS`
- Latest production fix commit: `ba549e24094bc8f8380da9a4eb4375cd3a46928a`
- Cross-Border final polish commit: `2322603d81a631b08b08d438c9c1283706f8141a`

## Main Code Paths

Use the production checkout root implementation as the source of truth:

- `app.py`
- `features/picking_labels.py`
- `bot/picking_labels.py`
- `bot/picking_pdf.py`
- `bot/drive.py`
- `bot/sheets.py`
- `tests/`

In this local workspace, the usable production checkout is `tmp/streamlit-deploy-JapanPost-SaaS/`. Do not treat outer broken `.git` folders, `PAexample/`, `vchunk_*.txt`, old logs, or old txt test data as the current production implementation.

## Tabs

Current tab order:

1. `跨境揀貨單`
2. `郵局待打單`
3. `使用說明`
4. `讀取診斷`

`跨境揀貨單` is the first/default tab.

## Accepted PDF Layout

- PDF size is fixed: `100mm × 150mm`.
- Layout is fixed 10-row grid on every page.
- Pages with fewer than 10 items keep blank grid rows.
- Orders with more than 10 items paginate every 10 items.
- Sparse / medium / dense adaptive layout is no longer used.
- PDF layout is accepted; do not redesign it.
- Do not make broad changes to QR, header, product columns, quantity column, or blank grid rows.
- `発送期限` displays to minutes, for example `2026/07/04 00:00`.
- Short progress text such as `06/27\n着予定` is normalized where possible to `06/27着予定`.
- `注文番号` block has already been moved slightly upward.
- CJK font handling now prefers Meiryo / Meiryo Bold and keeps Noto CJK fallback for cloud environments.
- Latin text uses Arial / Arial-Bold. Streamlit Cloud can use Liberation Sans as an Arial-compatible fallback.
- Invisible / bidi / zero-width control characters are removed from product names before PDF wrapping to avoid abnormal gaps.

## Source Sheet Anchored Schema

Source worksheet: `南巽出貨Label`

This sheet has duplicated header names. The Cross-Border picking label must not parse by first matching header name. It must use the anchored schema:

- K = `訂單狀態`
- L = `製單後勾選`
- M = `注文日`
- N = `訂單來源`
- O = `注文番号`
- P = `國際物流方式`
- Q onward = item groups 1 through 10
- The source sheet has been expanded to BN and supports 10 item groups.

Important:

- Columns A:C and E contain similar names, but Cross-Border must not use them.
- `注文番号` must come from column O.
- Writeback revalidation must compare column O, not column C.

## Candidate Filter

A row is a candidate only when all conditions are true:

1. K `訂單狀態` = `可出貨`
2. L `製單後勾選` = NOT_DONE
3. P `國際物流方式` contains one of: `郵便局`, `佐川`, `MLS`, `SLS`

Column L is a Google Sheets checkbox / data validation field with custom values:

- Checked value: `已製單`
- Unchecked value: `未製單`

Therefore:

- `未製單` = NOT_DONE / candidate
- `已製單` = DONE / excluded
- Keep compatibility with boolean False / TRUE / FALSE / blank legacy values.

## Writeback Safety

Formal generation sequence:

1. Generate PDF.
2. Upload PDF to the configured Drive folder.
3. Only after Drive upload succeeds, write back source sheet L.

Writeback value: `已製單`

Never write boolean TRUE to production column L. Drive upload failure must not mark L. Reload, preview, and selection must not write L.

## Drive Filename

Picking label PDF filenames use:

`YYMMDD-N揀貨標籤.pdf`

Examples:

- `260628-1揀貨標籤.pdf`
- `260628-2揀貨標籤.pdf`

Rules:

- Check the target Drive folder before upload.
- Find the highest existing sequence for today's prefix.
- Use highest sequence + 1.
- Never overwrite an existing file.
- If a collision is found during re-check, move to the next sequence.
- Timestamp fallback is only a last-resort safety fallback.

## Daily Operation UI

The Cross-Border main page should only show daily operation information:

- Print instruction: `列印設定：PDF檔尺寸為 100mm × 150mm，請使用對應Label大小輸出。`
- `待製單訂單`
- `最後讀取`
- Buttons:
  1. `產生揀貨單`
  2. `重新讀取`
  3. `全選`
  4. `取消全選`
- Table columns:
  - 選取
  - 注文番号
  - 注文日
  - 訂單來源
  - 國際物流方式
  - 発送期限

Do not show QR/debug summary, source row number, item count, PDF pages, warnings, possible causes, parser details, or system limits on the main operation page. Put those under `讀取診斷` → `跨境揀貨單`.

## Usage Guide Tab

The `使用說明` tab now includes Cross-Border picking-label documentation in the same prose/list style as the original postal instructions.

It covers:

- Cross-Border basic flow.
- Candidate filter rules.
- L-column checkbox values and writeback timing.
- PDF layout and print size.
- Drive filename sequence rules.
- Diagnostic location under `讀取診斷`.

## Postal Tab Status

Recent regression fixed:

- UI cleanup changed `開始製單` into a two-step confirmation flow.
- That broke the original direct `_start_job(...)` postal workflow.
- Fixed in commit `ba549e24094bc8f8380da9a4eb4375cd3a46928a`.

Current expected behavior:

- `開始製單` directly starts the existing postal label workflow.
- Visual order remains:
  1. `開始製單`
  2. `重新讀取`

Do not reintroduce a two-step postal confirmation unless the user explicitly asks for it.

Postal UI note:

- The permanent read-summary row was removed from the main postal page.
- Keep reload details in transient messages or diagnostics, not as a constant toolbar row.
