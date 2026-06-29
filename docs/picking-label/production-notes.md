# Cross-Border Picking Label Production Notes

Last updated: 2026-06-29

## Do Not Redesign

The Cross-Border PDF layout is accepted as the production candidate. Do not redesign it.

Hard constraints:

- Keep `100mm × 150mm` label format.
- Keep fixed 10-row grid layout.
- Do not revert to sparse / medium / dense adaptive layout.
- Keep blank rows when fewer than 10 items are present.
- Keep pagination by 10 items.
- Do not broadly change QR, header, product columns, quantity column, or blank grid rows.
- Do not change candidate filtering unless a real production bug proves it is necessary.
- Do not change Drive upload / L-column writeback transaction behavior unless the user explicitly requests it.

Recent accepted polish:

- CJK font preference is Meiryo / Meiryo Bold where available, with Noto CJK fallback.
- Latin font is Arial / Arial-Bold, with Liberation Sans registered as Arial-compatible fallback on Linux/Streamlit Cloud.
- Product names are normalized to remove invisible text control characters before wrapping.

## Production Data Rules

Source worksheet: `南巽出貨Label`

Use anchored column positions:

- K `訂單狀態`
- L `製單後勾選`
- M `注文日`
- N `訂單來源`
- O `注文番号`
- P `國際物流方式`
- Q onward item groups 1 through 10

Do not use A:C or E for Cross-Border order identity or source parsing. These columns may contain similar names but are not the Cross-Border anchored schema.

Writeback revalidation must compare O `注文番号`, not C.

## Column L Checkbox Values

Column L uses custom checkbox/data validation values:

- Checked: `已製單`
- Unchecked: `未製單`

Candidate rows use NOT_DONE semantics:

- `未製單`, blank, boolean False, and compatible legacy false-like values can be considered not done.
- `已製單`, TRUE, and compatible true-like values are done/excluded.

Formal writeback must write `已製單`, not boolean TRUE.

Never write L before Drive upload succeeds.

## Transaction Safety

Required sequence:

1. Preview/reload/selection must not write Google Sheets.
2. Generate PDF.
3. Upload PDF to Drive.
4. Only after Drive upload succeeds, write source L = `已製單`.
5. If Drive upload fails, do not write L.
6. If partial success happens, mark only successful source rows.

Writeback must use original source row numbers, not preview table indexes.

## Drive Filename Sequencing

Filename pattern:

`YYMMDD-N揀貨標籤.pdf`

Before upload:

- List existing files in the configured Drive folder.
- Find filenames matching today's prefix and sequence pattern.
- Use highest sequence + 1.
- Re-check immediately before upload.
- Never overwrite an existing Drive file.
- If a collision still happens, retry with the next sequence.
- Use timestamp fallback only as a last-resort safety fallback.

## UI Boundaries

Cross-Border main page should stay calm and operational. Keep daily users focused on:

- 待製單訂單
- 最後讀取
- 訂單列表
- 產生揀貨單
- 重新讀取
- 全選 / 取消全選

Put diagnostics under `讀取診斷` → `跨境揀貨單`, including:

- QR/debug summary
- source row numbers
- item count
- PDF page count
- warnings
- possible causes
- parser details
- system limits
- font diagnostics

The `使用說明` tab includes Cross-Border operational guidance. Keep this user-facing documentation aligned when changing the picking-label workflow.

## Postal Label Tab Boundary

The `郵局待打單` tab is separate from the Cross-Border picking-label feature.

Recent production fix:

- Commit `ba549e24094bc8f8380da9a4eb4375cd3a46928a` restored `開始製單` to directly call the existing `_start_job(...)` postal workflow.

Do not change that postal start behavior while working on Cross-Border, and do not add a two-step confirmation to postal start unless the user explicitly requests it.

The main postal operation page should not show a permanent technical read-summary row. If reload details are needed, keep them as one-time feedback or in diagnostics.

## Non-Blocking Technical Debt

Streamlit may still log:

`Please replace use_container_width with width`

This is a deprecation warning, not a business-logic error.

If handled in a separate cleanup task only:

- `use_container_width=True` → `width="stretch"`
- `use_container_width=False` → `width="content"`

Do not mix this cleanup with business logic, PDF rendering, parsing, Drive upload, writeback, or postal workflow changes.

## Secrets And Credentials

Root may contain service account JSON files such as `japanpost-488013-*.json`.

These are sensitive credentials. Do not copy, quote, summarize, or write private key contents into markdown, chat responses, logs, tests, or commits.

Only mention that the file exists and must be protected. Never expose `private_key` or credential content.
