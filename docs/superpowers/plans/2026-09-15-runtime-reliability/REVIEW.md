# 方案覆核紀錄

日期：2026-09-15；範圍僅為方案文件，不是修復／部署／實單驗收。

獨立fresh-context reviewer第一次：`NEEDS_REVISION`。

1. P1：installer／Playwright driver／真正browser的執行檔位置必須閉合。
2. P1：初始化總時限必須包含lock等待；同輪等待者不能各自再跑120秒。
3. P1：既有start-flow測試並沒有可執行worker fixture，需要新增真正隔離執行harness。
4. P2：asset builder範例要完整關閉檔案、複製後核驗、暫存目錄清理及最後發布。

主計畫已修正：RuntimeHandle顯式executable_path傳到probe／正式／fallback；Future單輪共用结果，deadline含lock及重工作子process；新增`test_postal_worker_runtime.py`執行測試要求；builder範例使用with、staging、copy後hash與最後rename。另自行補`.gitignore`對兩份JSON的精確白名單，未放寬secret保護。

獨立reviewer重新讀取修正版後回覆：**`APPROVED`**。

主對話另檢查：四個Python範例語法可解析、主計畫T0–T6／G0停止閘門與交接文件存在；tracked產品diff仍為0。隔離mock及原有5+4測試的真實輸出見EVIDENCE與DIAGNOSTIC-MOCK。沒有執行修復後Linux／正式Cloud／真實製單測試。

此APPROVED僅代表方案覆核通過，**不開啟G0–G3、不授權部署或製單，也不代表故障已修好。**
