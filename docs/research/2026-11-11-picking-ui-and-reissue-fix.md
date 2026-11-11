# 揀貨呈現與歷史補單窄例外追修（2026-11-11）

本文件為提交前審查記錄：分支 `codex/picking-ui-cleanup-20261111`，基底 `226484f6b0aa391de6c90fe7eae4522f0667ed48`。五檔變更的 fresh 獨立規格／品質審查均已通過；後續 commit、PR、merge 與部署結果另見 deployment planning 記錄。先前 PR18 的部署不代表包含本追修。

## 揀貨來源身份提示移至診斷

`features/picking_labels.py` 只調整呈現：缺 seller／單號、又沒有自身 PDF receipt 的 source review，不在主揀貨頁逐條 warning。原 `picking_diagnostics.picking_review` 全部保留，既有 admin「讀取診斷」新增預設折疊明細，包含來源列、訂單來源、單號與原因。

有效候選、需操作者核對的有效身份 review、已保存 PDF 的只補標、未知上傳核對與具 receipt 的 review 仍顯示必要提示及操作。沒有補 seller、改 key、讓不完整身份進候選，也沒有改 L、出貨篩選或 journal guards。全選已由 root 精準實機確認正常，本追修未改 selection／editor epoch。

## 歷史來源的 legacy 商品片段

唯讀診斷者及 root 確認：本次特定歷史訂單的 source rows 2737／2738，所有非 legacy 業務欄位一致；完整 indexed 兩品項及數量集合一致，運送方式同為 ePacket。只有 `郵局內容物`、`郵局申告金額(USD)`、`数量` 三欄，分別鏡像完整訂單中的第 1／第 2 品項。第 1 品項代表可通過原 strict `source_items`；第 2 品項片段因舊新內容物不同而被擋，原 raw business digest 因此報 source conflict。本文件不保存私人欄位內容或原始樣本。

只在 `get_history_order` 原始 business digest 已判定多版本時，新增私有 `_legacy_fragment_history_representative` 窄例外：

1. 排除上述三個 legacy 欄後，所有 `BUSINESS_COLUMNS` 必須完全相等；包含收件資料、物流方式、JPY、數量集合與所有 indexed／HS 欄。
2. 每列必須有完整、連續的 2–10 個 indexed 品項，各有 description、正有限單價與正整數數量。複本清空三個 legacy 欄後仍須通過原 `source_items`，不能借 legacy fallback 補缺。
3. 非空數量集合必須逐 token、逐品項等於 indexed 數量；每列 legacy description／Decimal 單價／整數數量三元組，必須恰屬完整 indexed 品項集合。
4. 只從原 rows 選 legacy 三元組等於第 1 項且能通過原 strict `source_items` 的代表。若有多個合法代表，以原 business digest 穩定排序，不用 row 優先或物流優先。

任一條不成立、沒有 strict 首項代表、收件／品項／金額／method 真差異、缺項／非法值、數量集合缺或不符、僅 legacy 資料，都維持原 source conflict；超過 10 項維持原 overflow guard。同 raw snapshot 或僅 status／checkbox 差異沿用原契約。

沒有修改全域 `BUSINESS_COLUMNS`、`business_snapshot`、`source_items` 或運送方式版本選擇。回傳的是原完整 selected row，不重造業務快照。沒有修改 live 記錄、Register 真單或恢復其他已取消操作。

## TDD 與合成執行證據

- UI：新 `test_picking_ui_cleanup.py` 先 1 assertion FAIL → 2 PASS；混合六筆缺身份、有效 pending、actionable review、已保存 PDF／未知上傳 recovery，確認前台與診斷分流且 receipt 提示保留。
- Legacy fragments：`HistoryLegacyFragmentTests` 先 2 assertion FAIL（歷史查詢與完整 guarded fake 執行都被原 source conflict 擋）→ GREEN。5 個 methods 包含正反 row 順序、多合法代表穩定選擇、同 snapshot／status 例外保全、真業務差異及不完整／非法片段拒絕。
- 合成端到端流程：`lookup_history` → strict `source_items`／完整 2 項 `build_pending_item_frame` → `build_plan(mode='reissue')` → `execute_plan`。使用 MemoryBook、fake completion authority／runner／backfill；fake runner 核對完整兩品項並發 synthetic lifecycle，不呼叫 live Register。
- 相關命令已通過：`test_sheets_helpers.py` 54、`test_shipment_history.py` 2、`test_shipment_service.py` 20、`test_shipment_plans.py` 13、`test_picking_reliability.py` 49、`test_picking_labels.py` 66、`test_picking_ui_cleanup.py` 2；皆以 `python -m unittest discover -s tests -p <檔名> -q` 執行。`py_compile` 與 `git diff --check` 通過。

## Fresh 獨立審查結果

- 五檔變更的 fresh 規格與品質審查均 PASS；規格驗證 76 項、UI 驗證 51＋14 項及 legacy 26 項邊界驗證通過。
- 品質驗收者獨立完整執行：696 discovered＝690 PASS＋6 Linux-only SKIP，0 failure／error，47.736 秒；10 個 Python 子程序套用網路 fence。六項跳過為 Windows 不支援的 Linux Chromium cold start 兩項，以及 Linux /proc／setsid／subreaper 四項，未列入已通過數。
- 上述是提交前離線審查證據；後續 Linux CI、實際發布 revision／應用重啟及正式環境結果仍須依 deployment planning 完成並驗證，不將本輪 Windows 離線結果當成已發布。

實作者未重跑長完整 suite，完整結果歸屬獨立品質驗收者。既有檔均先備份，本地 raw backup／tmp 不應隨產品變更一併加入。
