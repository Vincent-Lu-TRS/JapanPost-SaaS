# 跨境揀貨可靠性修正（2026-11-11）

狀態：原規格與人工 proof drift 差異已獨立驗收通過，fresh-context 品質驗收者的 focused／完整離線測試通過；品質最終結論尚待本文件讀回。這不是部署完成報告。本輪未向正式 Sheets／Drive 寫入，未部署、push 或 merge。

## 訂單身份與來源核對

正式來源綁定 `bot/picking_labels.py` 的固定 spreadsheet ID 與不可變 worksheet gid `866944434`；讀寫都透過 worksheet ID，不依分頁名稱定位。Production 另外要求 K:Q 必需欄名錨點完整：任一改名或移位立即停止，不 fallback 前方同名欄；A:C 正常輔助重複欄不因此被拒絕。Generic compact parser 仍保留既有相容行為。身份由來源綁定、訂單來源／seller、注文番号 canonical JSON 的 SHA-256 組成；row 只用來定位。

列印 fingerprint 包含訂單日期、seller、單號、物流方式、完整商品 SKU／名稱／JAN／數量／進度、QR、発送期限，排除 row 與 L。保留選取、重新讀取、產生之前均按身份及 fingerprint 核對。資料排序、row 或內容變動時，Streamlit editor 換 epoch，避免舊 row-index 勾選套用到另一筆內容。

同一身份重複、缺 seller／單號、選取內容變動皆停止該訂單操作。來源 A:C 的 B/C 會另外掃描 seller／單號，不能把這些商品列與 O 列號配對；已知同號跨 seller 的上游合併歧義會顯示待核對。若上游已遺失原始 seller 證據，本系統無法還原或證明商品合併正確。

## 製作紀錄與狀態恢復

`bot/picking_journal.py` 重用既有 encrypted `ShipmentLedger` 與 global writer mutex，不新增服務、依賴、分頁或欄位。紀錄採 `picking:v1:` typed namespace，與郵便 plan／shipments 分離。

每批保存有序身份＋fingerprint manifest、txid、Drive 預留 ID、檔名、PDF MD5／SHA-256／size、upload stage 與逐身份 mirror outcome。批次 manifest 自身獨立保存，不參照之後會重用的 source snapshot 槽位。Head／chunk 驗證包含 document ID、generation、index 與有序分片；batch 的 immutable chunk 路徑包含該 txid 與 generation，不能引用 source 槽或另一份文件。Source head 只可引用同槽、同代且完整有序的分片。

1. 取得共用鎖並重新讀取／核對身份、內容。
2. 先持久化 source baseline 與 `reserved` manifest。
3. render PDF，呼叫 Drive `generateIds` 取得唯一 binary file ID，將 ID 與 hashes 保存為 `upload_started`，確認 ledger 存入後才上傳。
4. 使用固定 `body.id` 呼叫 create，保留每日序號檔名，不覆寫既有檔案。create timeout／409 也只 GET 原 ID。
5. GET 必須核對確切 ID、PDF MIME、parent、非 trash、txid、manifest hash、MD5 及 size；若 SHA-256 metadata 有值也比對。成功才持久化 `uploaded_verified` receipt。
6. receipt 保存後才寫 L；無論寫入成功或 timeout，都讀回身份、seller、完整 fingerprint、row 與 L。只把確實讀回完成的身份記為 verified。

上傳結果不明、GET 無法確認、原檔 404 或原 bytes 已消失時，保留 reservation，不換新 ID、不重新 render／upload。可在 UI 按「核對原上傳結果／補標」重新核對同 ID。若已有 verified PDF，重新啟動後只補未確認的 L；部分成功也只補尚未 verified 的身份。

尚未取得 file ID 的 `reserved` 留待管理員核對，不能以新批次繞過。服務停止後可在確認自己仍持有鎖的情況終止此次 mutex；持久 reservation 繼續阻擋相同身份。無法確認鎖擁有權則維持既有 fail-closed 行為。

## Source snapshot 容量與失敗處理

Baseline body 固定驗證 key、row、done、fingerprint、manual_observed、untrusted_done 六欄：身份／fingerprint 必須為 canonical SHA-256，row 為至少 2 的整數，done 為已知 enum，provenance flags 為 bool。缺欄、錯型、不可讀文件均以 LedgerUnavailable 停止，不讓 raw KeyError／TypeError 漏到消費者。完整 row→身份／L／fingerprint baseline 使用 A/B 兩槽。只覆寫非 current head 指向的槽位，所有 shards 存入並確認後，最後切換單一 current head。中途或 head 保存失敗，舊 head 仍可讀到原完整 snapshot。

每個加密 cell 嚴格小於 45,000 字元；snapshot／大批 manifest 均分片。重複保存 source snapshot 不會每次 append 新一代完整副本；兩槽依各槽歷來最高分片數有界。既有 batch receipt、歷史身份證據與合法 orphan shards 不刪除。資料量仍隨不同訂單及批次累積，本輪未做一般 ledger 歷史清理。

## 人工 L 與原 PDF 重印

第一次正式 mutating operation 才保存當下完整來源的人工 `L=已製單` baseline，provenance 為 `manual_observed`。這尊重既有人工勾選，不宣稱每筆已有可驗證的 PDF receipt；沒有 hardcode 固定筆數或未驗證訂單清單。單純 load、preview、reload 不保存 baseline、不改 L。

初始信任界線：在沒有歷史 row→身份 association 之前，無法證明人工勾選前 O/L 從未錯位；先保留並排除目前人工完成的訂單，不把全部歷史訂單改成必須逐單確認。

有 baseline 之後，完成證據跟身份及當時列印 fingerprint 走。人工完成紀錄若 SKU、JAN、數量、発送期限等列印內容改變，即使 L 仍是已製單，也顯示待核對；後續製作其他訂單時保留原人工 proof fingerprint，不把新內容覆寫成已核准完成。L 重設後重新勾選也不會批准已變動內容。同一身份只移動 row、內容 fingerprint 相同時，既有人工完成證據仍有效。原身份移到 L=false 仍阻重製；新身份承接別筆 row 的 L=已製單且沒有自身完成證據，顯示待核對。這種疑慮不會因後續別筆正式操作保存 snapshot 而被洗成可信人工完成。寫入窗發生 A/B 身份交換，即使新 row 的 L 恰好已製單，也回報 review，且可能被誤勾的另一身份保持可見待核對。

有 receipt 的身份若 L 被重設，UI 顯示「開啟原 PDF／重印」及「只補標此訂單」。原 PDF 重印不產生新 upload。只有人工完成 baseline、沒有 receipt 的歷史訂單，L 重設時顯示核對歷史 PDF 提示，不能猜測原檔連結或靜默重製。本輪未新增任意新 PDF 再製功能。

## 密鑰、損壞紀錄與操作限制

沿用既有 `encryption_secret()`：優先 `SHIPMENT_LEDGER_SECRET`，其次 OAuth `auth.cookie_secret`，再其次 `GOOGLE_CLIENT_SECRET`；選到的值必須至少 32 字元。未配置或解密／紀錄驗證失敗時，正式產生及補標停止，UI 明示需要管理員處理，不回退本機／session 記錄。本輪未讀取或輸出正式 secret，未驗證 Cloud 目前實際配置。

已有 ledger 必須保留原密鑰；直接輪替 fallback secret 會使歷史紀錄無法解密。若要輪替，需要另行設計並驗證既有紀錄遷移，不能把讀不到紀錄視為尚未製作。

Postal consumers 只跳過 namespace、kind、envelope、所引用 shards、digest 與已知資料結構都能驗證的 picking 文件。這些核對使用已讀 ledger 快照，不增加外部 API。合法 inactive／orphan chunks 不阻擋郵便；unknown 或損壞 head 會 fail closed，可能連帶停止郵便／補製。管理員應修復紀錄，不能刪略 head 或偽裝正常來繞過。

Google Sheets 沒有此流程可用的 conditional CAS。外部人員或 GAS 不遵守同一 mutex 時，仍可能在讀取及寫入之間移動 O/N／商品／L。寫前及寫後核對可偵測多種錯位，但不能承諾跨外部寫者完全原子。偵測漂移後停止自動補救、保留 PDF 與待核對狀態，不 rollback 他人的 L；也不以出貨、封存或日期替代揀貨製作證據。

## 本輪離線證據與未驗範圍

- 首次 RED：`test_picking_reliability.py` 16 個 assertion failure，涵蓋現有跨 seller／duplicate resolver 錯誤與缺少 journal／verified helper。
- 分階段新增測試並實跑 RED→GREEN：editor 身份／內容保存、provider ValueError 不洩漏、postwrite swap、損壞 typed head、legacy None callback、source 雙槽容量、reload、anchored 11 項，以及重啟後未確認上傳的 UI recovery。獨立審查提出的 production schema fallback 缺口，另以七個 K:Q 錨點改名與 cached fallback 身份副作用案例重現八個 assertion failure，再以 strict gate 修正；正常輔助重複欄控制案例仍通過。另補 typed body／跨文件分片引用及錯型 enum 的六個 assertion failure→GREEN，以及實際 reload widget＋refresh callback 的兩個選取保存 subcase failure→GREEN。
- 最新 49 個新可靠性測試及既有 66 個揀貨測試通過；另有 shipment ledger 14 項及 service 20 項相關回歸通過；7 個產品模組 py_compile 與 Python 3.12 grammar 核對通過；`git diff --check` 通過。
- 最終完整版本由 fresh-context 品質驗收者獨立執行並回報：689 discovered，683 實際通過、6 跳過，0 failure／error、exit 0，48.900 秒；focused 49 項通過，1.554 秒。父 runner 及 10 個 Python 子程序均封鎖 connect／connect_ex／create_connection／getaddrinfo／sendto，使用 python -B、workspace 專屬暫存路徑與 repo cwd。原規格與人工 proof drift delta 已獨立 PASS；品質驗收者已完成本文件讀回，最終品質審查 PASS，沒有未解決的 must-fix。
- 獨立驗收結果來源是品質驗收者的工具執行回報，沒有另寫 raw log 檔，不能把實作者舊版 686 項的 `tmp/picking-final-three-fixes-offline-20261111.log` 當成最後 689 項的證據。驗收者第一次完整 runner 漏支援 script-path，修正 runner 後重跑通過，產品程式未因此改動。
- 6 項跳過為本機 Windows 不支援的 Linux Chromium cold start 兩項，以及 Linux /proc／setsid／subreaper 四項；未把跳過列為已通過。
- requirements 與 CI 既有基準為 Python 3.12.14，這輪本機 Python 為 3.14.6。未改 requirements，未使用僅新 Python 才提供的 API；未重跑 pinned Linux container／Cloud 瀏覽器環境。
- Drive 預分配 binary ID、body.id 與 shared drive support 依官方 API 契約，已用 synthetic adapters 驗證呼叫；尚未 live 驗證此組合、權限、雲端密鑰或正式 quota。

## 發布時必須完整重啟應用

目前 `current_source_modules` 的受控重載範圍僅有 job_control／safe_logging；app 對 picking、journal、ledger、service 的一般 Python imports 可能保留在既有行程。這輪沒有新增跨模組 reload fence，不能保證 warm 更新時所有 consumer 同版；新舊模組混用可能 KeyError 或 fail closed，尚未證實會繞過防重製。

未來取得發布及正式操作授權後，部署 owner 必須依序完成：

1. 停止接受新製單任務，確認既有 job／worker 都已停止，保留所有 ledger、reservation 與結果不明的鎖狀態。
2. 依既有發布治理完成審核及 main 整合；完整重啟 Streamlit Cloud 應用的 Python 行程，不能以頁面 refresh、Streamlit rerun 或「重新讀取」代替。UI「重新讀取」只重新抓取資料，不代表啟用程式更新。
3. 核對核准 main SHA 與實際 deployed SHA 一致，並確認新 journal、picking、ledger、service 都載入同一核准 revision，沒有沿用舊行程的 cached module。
4. 執行既有 runtime checks，包括 `scripts/check_browser_runtime.py` 與 [JapanPost 操作手冊](../JAPANPOST_OPERATIONS_RUNBOOK.md) 的 Cloud runtime 核對，再做小範圍正式驗收，核對原 PDF／只補標、L 身份讀回與避免重複 upload 的結果。

上述為尚未執行的發布限制與驗收步驟。本輪仍未驗證 live Drive API 組合、Cloud secret 配置或 Linux／Cloud browser runtime；未部署、未 merge，也沒有因離線測試通過自動取得發布授權。
