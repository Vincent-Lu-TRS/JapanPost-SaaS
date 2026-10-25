# 拆單與補製單 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. 本輪唯一產品寫者執行；主代理安排獨立規格與品質審查，驗收通過並指示後才本機 commit。

**Goal:** 前台完整分配每項原始數量至多包，並提供精確歷史查詢、補製與持久化安全恢復。

**Architecture:** 以不可變 shipment/plan payload 串接既有郵政流程。Google Sheets 原子 addSheet/updateCells 固定title／隨機generation sheetId鎖跨正常製單、拆單、補製與回填，鎖不自動過期；加密 ledger 存快照與階段。既有 normal preflight 保留；新工作獨立驗證來源及 ledger 身份。

**Tech Stack:** Streamlit、pandas、既有 gspread、cryptography Fernet、unittest/AppTest；不新增服務或依賴。

---

## 契約與檔案

- `shipment_plans.py`：source item ID = source row identity + 原始欄位位置；計畫 hash 包含原始 fingerprint、完整確認 payload、allocation、mode、補製 nonce。每包 shipment ID 不含商業單號後綴，允許同方式多包。分配為非負整數、逐項守恆、每包至少一項正數；零項清除，USD逐包重算。
- `bot/shipment_ledger.py`：固定title的 Sheets global lock（每generation新隨機sheetId，同一 batchUpdate 建 sheet 及 owner marker）、持鎖 read-back、owner check後只刪captured generation ID；無 TTL 接管。JSON快照以 auth secret 的 domain-separated key 加密，無key新功能 fail closed。ledger encrypted records 含 planned/issuing/label_created/uploaded/writeback_pending/completed/unknown、操作者、來源時間、完整不可變確認payload、結果。
- `bot/shipment_service.py`：全程鎖內驗證/保存/逐包執行/恢復，issuing不得重製；uploaded/writeback_pending只回填；無耐久PDF的label_created須人工恢復。normal也共用鎖；未知郵政結果保留鎖與人工核對。
- `bot/sheets.py`：精確歷史單查詢，伺服器先篩單號，才投影白名單欄位；不走pending篩選，衝突資料阻擋。既有B/C/D/J RAW、公式保護、leading zero和回讀保持。split/reissue多tracking僅由可信service轉成受控additional回填，外部訂單號維持原值。
- `bot/automation.py`：HS以shipment ID隔離、結果/事件含shipment ID，新增嚴格同步 lifecycle_cb；Register前issuing，tracking取得立刻label_created，Drive成功必須file ID；callback失敗停止，不走吞例外status_cb。
- `features/shipment_workbench.py`、`app.py`：待單卡的包裹分配、補製tab、來源時間/歷史tracking/PDF、補製原因與新tracking確認；身份守門、最少資料、限速。既有整單追加保留，與拆單互斥。
- `job_control.py`：shipment ID作新工作事件身份、max_rows不切斷計畫；normal身份與防重測試保留。

## 任務與驗收

- [x] 1. 新增 `tests/test_shipment_plans.py`，RED→GREEN：A2/B3兩個例子、同方式兩包、同名不同item、超漏配/負小數/空包、不可變身份、max_rows邊界。`python -m unittest discover -s tests -p test_shipment_plans.py -v`。
- [x] 2. 新增 `tests/test_shipment_ledger.py`，RED→GREEN：原子鎖、兩客戶端互斥/timeout結果不明不重試、owner驗證、無TTL接管、ciphertext無PII、重啟還原與payload不可變。
- [x] 3. 新增 `tests/test_shipment_service.py`，RED→GREEN：跨兩使用者部分重疊、normal原防重、split source變更、補製必須歷史tracking及原因/確認、部分完成、回填重試、issuing unknown、無PDF恢復阻擋、同單多tracking原ID。
- [x] 4. 擴充 `tests/test_automation_helpers.py` / `tests/test_postal_mock_e2e.py`：shipment HS隔離、strict callback Register之前、tracking在Drive前保存、Drive None不成功。保留原郵政所有表單和彈窗。
- [x] 5. 新增 `tests/test_shipment_workbench_app.py` 真實AppTest：多包分配與同方式、補製搜尋與必填確認、歷史來源提示、匿名/員工/admin界線；既有tab斷言更新。
- [x] 6. 完整驗證：`python -m unittest discover -s tests`、`python -m compileall -q app.py job_control.py shipment_plans.py bot features`、`git diff --check`。更新操作手冊的配置、加密key保留、鎖/unknown人工核對、舊PDF重印與補製區別。
- [x] 7. freeze，報主代理新鮮測試輸出與未驗證限制；等獨立審查後修正，不push/merge/deploy/真製單/正式資料寫入。

## 驗證邊界

全部離線合成資料與mock API；不宣稱正式Sheets權限/配額、Cloud runtime、真郵政回應已驗證。人工解除崩潰鎖需確認舊worker停止及郵政tracking證據，產品不提供TTL自動解除或一般員工強制重製。

## 最終本機驗證

2026-10-25第二freeze：完整suite533tests OK/skipped6，compileall與diffcheck通過。獨立spec QA：core23例與真主app UI11組通過；fresh品質review及主代理各自重跑533例通過。API計數（不含來源/目標回填）：兩包split34read/14write、normal回填前32read/14write；429-only最多3次有限退避，timeout/500不重試。主代理准許本機commit，未授權push/PR/merge/deploy或正式製單/雲端資料寫入。
