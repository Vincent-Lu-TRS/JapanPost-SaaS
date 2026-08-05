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

## 2026-08-05 正式資料核對：多 SKU 歸因修正與安全重測條件

- `imy2038220` 證明舊程式並非一律只保留訂單第一列：它的第一列本身已帶有 `內容物1/2`，因此 `TRSN3392` 與 `TRSN6195` 都能送出。
- `imy2038510` 的三列各自只有一個 legacy 品項；舊流程在同訂單同運送方式下只留下第一列，才形成只送 `TRSN9767` 的結果。正式歸因是條件式資料形狀漏洞，不是單純的「依訂單編號永遠只取第一列」。
- `imy2038410` 目前呈現同樣的兩列 legacy 形狀，但因沒有歷史 log，只能列為高風險案例，不能把當時結果當成已證實。
- 正式來源與 target 的唯讀核對顯示 `imy2038490` 目前已有 target 完成紀錄及 tracking `LP106370435JP`。這只能證明目前狀態已有完成紀錄，不能還原昨天跳過的歷史原因；原單不得直接重跑，以免重複製單。
- 要對正式服務做實證，需用相同收件資料與地址變因建立全新唯一訂單編號的安全 clone，並在已登入正式執行環境擷取 M060505、M060800、PDF/tracking 與 target 回填證據；在這些條件具備前，地址過長與泰文字符都仍是候選原因，不可宣稱已定案。
- 2026-08-05 已在正式 SaaS 唯讀重讀郵局待打單：可製單 0 筆，診斷顯示 2,999 筆來源中 2,997 筆因狀態不是「未打單」排除；因此沒有安全的正式提交對象，也不能把這次 0 筆讀取誤稱為 Japan Post 製單失敗。
- 2026-08-05 依授權建立正式 clone `imy2038901` 測試：前台成功讀取 1 筆單一 legacy 品項，但開始製單後完成數仍為 0，source 未回填 tracking，target 無新增列；正式舊版只留下 Streamlit WebSocket 斷線，沒有可讀 M060505/M060800 錯誤文字。
- 同一來源資料經修正版純函式診斷：加權寬度 raw 423、正規化 412、泰文 code point 88、非 ASCII 99，Address 1/2/3 容量不足，故目前最具體可證實的阻塞分類是 `address_too_long`；泰文影響尚未由隔離實測證明。clone 已標記 `測試失敗｜address_too_long` 防止誤重跑，原單未改。
- 2026-08-05 進一步核對 `官網自動轉表` GID 230039347：K、L、M、N、X、AH 目前均為標題列陣列公式，且 1953 列後的 2038410、2038490、2038510 已重新產生完整明細。行號與歷史現象吻合，2038220 在 1953 前正常、2038510 在 1953 後只剩第一 SKU，因此來源公式未延伸是主要根因。
- 注意：此前本地未部署的 `_aggregate_order_rows` 在陣列公式修正後會把每列重複的完整明細再次合併，最小 fixture 實測 3 SKU 變 9 筆；本階段已將該聚合器移除並回到來源列去重，避免再重複合併。日本郵政 request flow 不必為這個根因另加 SKU 特殊邏輯。

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

## 2026-08-05 製單可靠性與可觀測性實作

- 已依使用者決策回退本地 `_aggregate_order_rows` 多列聚合邏輯；`官網自動轉表` 的 K、L、M、N、X、AH 標題列陣列公式負責整理明細，JPPOST 讀取第一筆代表列即可取得完整 `內容物1..10`、`申告金額1..10`、`數量1..10`、`HSCode1..10`。不可再把每個來源列的完整陣列明細重複合併。
- 保留 numbered 欄位的 1..10 項讀取、數量空白／無效檢核與前台警告；但不再由本地程式根據同一訂單的多個來源列自行拼接 SKU，也不再以本地聚合器產生 `mixed_shipping_method` 等來源列警告。
- 開始製單後會重新讀取目標表完成訂單及來源待製單快照；目標表讀取失敗採 fail-closed。來源 tracking 與目標完成資料不一致、來源 fingerprint 改變或已完成的訂單不會直接進入自動化。
- 自動化結果分為 success、failed、blocked、skipped、backfill_failed，回填後會重新讀取目標表 C/D 欄核對注文番号與 tracking；只有核對成功才把結果提升為 `completed`。
- 地址送出前只記錄長度、Japan Post 加權寬度、欄位寬度、泰文及非 ASCII 統計，不記錄完整地址。失敗分類固定為 `address_too_long`、`address_invalid_character`、`postal_validation_error`、`remote_form_error`、`unknown`，不自動截斷或改寫泰文。
- 前台「本次完成」改由已回填驗證的結構化結果計算；終端狀態後遮罩才消失，失敗批次會逐筆顯示「訂單編號：未製單（原因）」。
- 本階段驗證：`python -m unittest discover -s tests -p "test_*.py"` 共 235 項通過、`py_compile` 與 `git diff --check` 通過；離線以三列「每列皆帶完整陣列公式明細」模擬，確認 1 筆訂單保留 3 項明細且不重複成 9 項。未呼叫真實日本郵政，也尚未完成 staging 多 SKU 實單核對。
- `tests/test_postal_mock_e2e.py` 已改為以陣列公式形狀的來源資料、mock Japan Post gateway、mock target worksheet 驗證三 SKU 形成三次 M060800 payload；另驗證地址失敗不吞掉其他成功，以及 target 回讀失敗不算完成。
- 雙語地址處理：若 Shipping Street 以 U+201A 等分隔符帶有泰文與英文重複段，且英文段為 ASCII 地址、含至少兩個與前段相同的數字錨點及地址關鍵詞，才取英文段；會移除重複的城市／郵遞區號，並以 80/80/36 容量順序分裝。證據不足時維持原值並阻擋，不自動翻譯或截斷。
- 雙語姓名處理：若 Shipping Name 是英文姓名、泰文別名置於括號內，例如 `Teerapan (ธีรพันธุ์) Kaewkong (แก้วคง)`，送往 Japan Post 的姓名欄只移除泰文括號別名，保留 `Teerapan Kaewkong` 與注文番号；原始來源與前台顯示不改，ASCII 括號暱稱維持原樣。製單前記錄姓名字數、加權寬度與泰文字數，不記錄姓名全文。

## 2026-08-05 正式 clone `imy2038902` 實證

- 依授權以 `imy2038490` 的同等收件資料建立新來源列 `A3002:AZ3002`，原單未改；正式服務前台成功讀取並列為可製單候選。
- 正式服務 log 已取得完整根因：在 M060505 表單尚未送出前，舊版 `_split_addr_to_bean_address_lines` 直接處理整段泰文＋英文重複地址，因正規化後仍超過 `Address 1/2/3` 容量而拋出 `ValueError`。因此此次不是日本郵政遠端拒絕、不是 SKU 去重、也不是訂單編號去重；姓名尚未進入可驗證的遠端送出階段。
- 失敗證據：來源 `imy2038902` 仍為 `未打單`、追蹤號空白；目標表 `郵局運費` 查不到該訂單，因此沒有產生真實標籤。
- 本地目前版已以同一份地址資料驗證：先選取可信英文重複段 `Supalai Verada Condo ... Petchkasem Road`，再把城市只加入一次，輸出 `add1=79`、`add2=68`、`add3=0` Japan Post width；姓名輸出為 `Teerapan Kaewkong imy2038902`。直接把原始雙語地址送入舊切分器仍可穩定重現 `address_too_long`。
- 正式服務仍執行 `main` 舊版；本地修正版尚未部署，因此要取得真實成功標籤，下一個必要步驟是先部署已驗證的本地修正版，再以同一測試列重試一次，並以 source/target/PDF 三方回查判定成功。
