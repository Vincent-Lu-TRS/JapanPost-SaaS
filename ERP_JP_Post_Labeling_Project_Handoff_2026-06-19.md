# ERP 日本郵便製單系統交接綱要

更新日期：2026-06-19  
Repository：`https://github.com/Vincent-Lu-TRS/JapanPost-SaaS`  
Production：`https://jppost.streamlit.app/`

## 專案定位

本系統定義為 ERP 相關子專案，負責將 ERP / Google Sheets 中的出貨資料轉換為日本郵便 International Mail My Page 的製單流程，並將產出的 PDF 標籤與貨運單號回寫到公司既有作業資料流。

目前系統仍是輕量 SaaS 形態：

- 使用者只透過 Streamlit Web UI 操作。
- 日本郵便自動化在伺服器端執行。
- Google OAuth 限制公司網域或白名單登入。
- 資料來源與回填仍以 Google Sheets 為 ERP 過渡資料層。
- 目標是逐步穩定成 ERP 出貨模組的一部分。

## 核心業務流程

1. 使用者登入 Streamlit App。
2. 系統讀取來源 Google Sheet 的待製單資料。
3. 套用防重製邏輯：
   - 來源狀態必須是 `未打單`。
   - `郵局申告金額(USD)` 不可空。
   - `Shipping Name` 不可空。
   - `製單檢核` 為 `TRUE` 時排除。
   - 目標表已完成的注文番号排除。
   - 來源內同注文番号去重，優先順序為 EMS > 國際小包 > ePacket。
4. 前台顯示待打單預覽，使用者可在啟動前調整部分欄位。
5. 啟動製單後，後端鎖定同一批任務，避免重複點擊造成重複製單。
6. 系統預查本批 EU 訂單 HS Code。
7. requests 流程登入日本郵便並逐步提交表單。
8. 產出 PDF、上傳 Google Drive。
9. 回填貨運單號至目標 Google Sheet。

## 目前主要檔案

- `app.py`
  - Streamlit UI。
  - Google OAuth 後的主頁。
  - 待打單預覽與可編輯工作台。
  - 任務啟動、任務狀態、重點日誌。

- `auth.py`
  - Google OAuth / Streamlit native auth。
  - 公司網域與白名單限制。

- `bot/sheets.py`
  - 來源表讀取。
  - 待製單篩選。
  - 雙重防重製。
  - 製單結果回填。

- `bot/automation.py`
  - 日本郵便 requests / Playwright 自動化核心。
  - 目前重要方向是優先 requests 提交 Japan Post Struts 表單，避免 Streamlit Cloud Chromium 崩潰。

- `bot/hs_codes.py`
  - EU HS / CN / TARIC 碼數規則。
  - 本地 HS Code fallback。
  - Gemini 預測前的本地命中。

- `bot/gemini_helper.py`
  - Gemini API HS Code fallback。
  - 注意免費額度限制，不能將 Gemini 當唯一來源。

- `job_control.py`
  - 批次任務鎖。
  - 同一使用者 / 同一批資料不可重複啟動。
  - 訂單狀態更新輔助。

- `pending_editor.py`
  - 前台待打單可編輯欄位定義。
  - 將使用者編輯值套回原始 DataFrame。

- `tests/`
  - 目前主要保障為 unit tests。
  - 每次修改後至少跑 `python -m unittest discover -s tests`。

## 最近完成的重點

### 1. Postal Parcel / M060900 卡關修復

已確認日本郵便 M060900 重量頁 payload 需要貼近瀏覽器實際提交：

- 不送 `command=regist`。
- 保留 `method:regist`。
- 不送 disabled 的 `shippingBean.withInsurance=true`。
- Referer 改為 M060900 action URL。
- Postal Parcel 重量欄位可以維持空白，由郵局處理秤重。

相關 commit：

- `4f3fcc7 fix: align postal parcel M060900 browser payload`
- `208ab6c fix: match postal parcel M060900 return route`
- `bd7c4e1 fix: preserve blank postal parcel M060900 fields`

### 2. 防重複製單

已新增 job lock，避免使用者快速連點或重新整理造成同批任務啟動兩次。

相關 commit：

- `80e6c57 fix: lock label jobs and preflight HS codes`

### 3. 前台狀態與日誌

已調整：

- 待打單預覽上移。
- 製單狀態逐筆顯示。
- 重點日誌移到底部。
- 詳細 debug log 放在展開區塊。
- 移除 Streamlit 寬度參數 deprecation 警告，改用 `width="stretch"`。

相關 commit：

- `67d5775 fix: show pending-order filter diagnostics`
- `c3a6cf2 fix: bypass pending order cache and log exclusions`

### 4. 待製單篩選診斷

目前診斷會顯示：

- 來源原始筆數。
- 關注訂單 `WhoWhy*` 逐筆狀態。
- 基礎篩選排除原因。
- 目標表完成排除。
- 來源內同注文番号去重前後。

重要規則：

- `製單檢核 TRUE` 必須排除。
- `製單上傳狀態` 若已是貨運單號，不應再進入待製單。

相關 commit：

- `1f2b9b5 fix: restore checked-order exclusion`

### 5. HS Code 預查與 fallback

目前流程：

1. 啟動任務後先預查本批 HS Code。
2. 先走本地參照表。
3. 本地查不到才呼叫 Gemini。
4. Gemini 429 時，不會影響已在本地表中的常見品項。
5. 若 EU 訂單仍缺必要 HS Code，會在任何 M060800 item submit 前停止，避免半成品。

目前本地表已涵蓋：

- `Facial Mask(No Alcohol)` -> `330499`
- `Pillow` -> `940490`
- `Hair Conditioner` -> `330590`
- `Toothbrush` -> `960321`
- `Spice Grinder` -> `821000`
- `Frying Pan` -> `732399`
- `Portable Cooking Stove` -> `732111`

相關 commit：

- `3fbc800 fix: add HS fallback and editable pending orders`

## 前台可編輯工作台現況

目前待打單預覽已改為可編輯表格。

可調整欄位：

- 郵局運送方式：
  - `EMS`
  - `國際小包`
  - `ePacket`
- `郵局申告金額(USD)`
- `內容物1` 至 `內容物5`
- `申告金額1` 至 `申告金額5`
- `數量1` 至 `數量5`
- `訂單合計申告金額(JPY)`

不可調整欄位：

- 注文番号
- Shipping Name
- 收件人國家
- HSCode

注意：

- 使用者編輯後的資料只套用於本次啟動製單，不會回寫來源表。
- 目前 UI 只顯示前 20 筆供編輯。
- 若未來待製單可能超過 20 筆，需要再設計分頁或批次選取。
- `EMS` 目前在 automation 裡代表不強制覆寫 Japan Post 預設 sendType；國際小包與 ePacket 會走既有分流。

## HS Code 國家規則

依日本郵便官方資料：

- Ireland / `IRELAND（アイルランド）`：TARIC 10 碼。
- France / `FRANCE（法國）` 與相關法屬地區：CN 8 碼。
- 其他 EU：HS 6 碼。

參考：

- `https://www.post.japanpost.jp/service/send/oversea/attention/region/europe.html`
- `https://www.post.japanpost.jp/service/send/oversea/use/label/hscode/index.php?lang=_ja`

注意：

- Gemini free tier 可能 429，每日或短時間 quota 很低。
- 常見品項應逐步加入 `LOCAL_HS_CODE_RULES`，不要依賴 Gemini。
- 對於高風險品項，建議未來做人工審核欄或固定 master table。

## Google Sheets 資料流

### 來源表

在 `bot/sheets.py`：

- `SOURCE_SHEET_ID = "1HDndg8GU35v6ft02pcOcfvABVt_J3rtCLfMuXWi14KM"`
- `SOURCE_GID = "605188303"`

主要欄位：

- `注文番号(貼上原始資料)`
- `製單上傳狀態(請用[未打單]檢視模式)`
- `製單檢核`
- `郵局申告金額(USD)`
- `Shipping Name`
- `收件人國家`
- `郵局運送方式(複數商品請自行確認是否走小包)`
- `內容物1..n`
- `申告金額1..n`
- `數量1..n`
- `訂單合計申告金額(JPY)`

### 目標表

在 `bot/sheets.py`：

- `TARGET_SHEET_ID = "1QJFFW7aWGpYX3W5nPW_HgUnVWk9AtggFvYow14BRW8U"`
- `TARGET_GID = "465870894"`

注意：

- C 欄目前被視為已完成注文番号集合。
- 回填時也會寫入收件人、注文番号、貨運單號與國家碼。
- 修改目標表欄位前要同步調整 `backfill_results()` 與防重製邏輯。

## 日本郵便自動化注意事項

### 優先 requests，少用 Playwright

Streamlit Cloud 上的 Chromium 對 Japan Post legacy HTML 很脆弱。歷史上曾因 `page.set_content()` 注入郵局 HTML 直接導致 Chromium process 被殺。

目前方向：

- requests 登入。
- requests submit Struts forms。
- Playwright 只保留必要環節。
- 不把 Japan Post HTML 回灌 Playwright。

### Struts 表單規則

Japan Post 多數按鈕不是單純 `command=xxx`，而是 `method:xxx`。

已踩過的重點：

- M060800 item add 使用 `method:itemAdd2`。
- M060800 Next 使用 `method:regist`。
- M060900 使用 `method:regist`，且不要送 `command=regist`。
- 要保留 hidden fields、select values、重複 itemList fields。

### 前次未完成資料跳窗

登入後若出現 previous data dialog：

- 正常正式流程應按 `Create a new label` 丟棄草稿。
- Debug 時可以按 `Create from previous data` 直達段落。

這是日本郵便端狀態，不是 app 錯誤。

## 已知限制與風險

1. Gemini quota
   - 目前已有本地 fallback，但未知品項仍可能因 429 缺碼。
   - 建議逐步建立公司內部 HS Code master。

2. UI 可編輯但不回寫來源表
   - 目前只影響本次製單。
   - 若需要 ERP 正式流程，應增加「保存修正」或「本次覆寫記錄」。

3. 待打單預覽只編輯前 20 筆
   - 現階段足夠測試。
   - 大批次前需加分頁或批次範圍。

4. EMS 支援需再實測
   - UI 已可選 EMS。
   - automation 目前主要穩定驗證為 ePacket / 國際小包。
   - EMS 需確認 M060800 sendType 是否需要明確 setValue。

5. Japan Post HTML / payload 變動
   - 日本郵便頁面是 legacy Struts，欄位名與 hidden fields 很敏感。
   - 每次修改 payload 建議加 diagnostics，不要盲改。

6. Streamlit Cloud 記憶體限制
   - Playwright headless flags 不要隨便移除。
   - 不要重新導入完整 GUI 操作模式。

## 下一個 session 建議優先事項

### P0：實測最新版 WhoWhy-Test7

確認：

- Pillow 是否走本地 `940490`。
- 不再觸發 Gemini 429 導致停止。
- HSCode 欄是否在製單狀態表中顯示。
- PDF 與回填是否正常。

### P1：補強 UI 編輯體驗

可考慮：

- 將每張單展開成「訂單 + 品項明細」視圖，而不是橫向多欄表。
- 提供品項 HS Code 手動覆寫欄。
- 提供「本次修改值」摘要，避免使用者忘記改過什麼。
- 增加「只製作勾選訂單」功能。

### P1：建立 HS Code master

建議新增來源：

- Google Sheet master table。
- 欄位：關鍵字、標準英文品名、HS6、CN8、TARIC10、最後確認人、最後確認日。
- 程式查詢順序改為：
  1. 手動輸入 / 本次覆寫。
  2. 公司 HS master。
  3. 程式內建 fallback。
  4. Gemini。

### P2：EMS 實測與明確 payload

目前 UI 可以切換 EMS，但 automation 邏輯仍主要針對 ePacket / 國際小包。

建議用 GUI/Network 擷取 EMS 的 M060800 payload，確認：

- sendType
- transType
- pkgType
- 後續 M060900 是否和國際小包相同。

### P2：ERP 正式化資料模型

未來不應長期依賴 Google Sheets 欄位位置。

可逐步抽象：

- `Order`
- `Recipient`
- `ShipmentMethod`
- `ContentItem`
- `CustomsDeclaration`
- `LabelResult`

目前 DataFrame 欄位仍是臨時 ERP adapter。

## 開發與驗證指令

執行全部測試：

```powershell
python -m unittest discover -s tests
```

語法檢查：

```powershell
python -m py_compile app.py auth.py job_control.py pending_editor.py bot\automation.py bot\sheets.py bot\hs_codes.py bot\gemini_helper.py
```

查看狀態：

```powershell
git status --short
git log --oneline -12
```

提交範例：

```powershell
git add .
git commit -m "fix: concise description"
git push
```

## 接手時請先確認

1. `git status --short` 是否乾淨。
2. 最新 commit 是否至少包含：
   - `3fbc800 fix: add HS fallback and editable pending orders`
   - `1f2b9b5 fix: restore checked-order exclusion`
3. Streamlit 是否已部署最新 `main`。
4. Live app 的「待製單讀取診斷」是否符合來源 Google Sheet。
5. 若製單錯誤，優先看最後一段 `response diagnostics`，不要只看 HTTP 200。

## 非談不可的安全原則

- 不提交 secrets。
- 不提交 `.streamlit/secrets.toml`。
- 不提交 service account JSON。
- Google OAuth 必須維持 `@tkrjm.co.jp` 或白名單限制。
- 使用者端不得要求安裝 Python、Playwright 或瀏覽器外掛。
- SaaS 使用者只透過網頁操作；自動化保持 server-side headless / requests。

