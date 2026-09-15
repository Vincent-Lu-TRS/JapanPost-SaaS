# JapanPost-SaaS 操作、故障排除與發布手冊

更新：2026-09-15 JST。適用正式站：<https://jppost.streamlit.app/>。

本手冊以當前程式、可讀 CI 證據與最新 runtime reliability 交接為準。它是操作指南，不構成真實製單、Google Drive／Sheets 寫入、秘密／權限變更或未涵蓋範圍之正式發布授權。

## 1. 先看證據等級

- **已確認**：有目前程式／測試、GitHub run 或本次交接可指認的證據。
- **使用者回報**：使用者於 2026-09-15 看到最新正式批次 8 筆全部完成；這是當時的實際業務結果，但不揭示 Streamlit 主機 OS，也不是所有環境相容性的測試。
- **未驗證**：沒有 Cloud 主機唯讀 fingerprint 或相應實機測試；不得以 runtime profile 宣告、CI 容器結果或一次成功批次代替。
- **歷史文件**：root `CLAUDE.md` 的 2026-06-20 source-of-truth 區塊及 `DEPLOY_GUIDE.md` 的舊 runtime／發布步驟已不是目前 runtime 狀態的依據；詳見第 8 節。

本次最後確認的程式發布基線為 PR #7 merge commit `2df259570b99aa3425f46b72a58648f67453b34d`；GitHub Actions `Runtime validation` run `34943051463` 在該 commit 成功。該正式基線不可與本文件所在的本機候選 worktree HEAD 混為一談。

- [PR #7 merge commit](https://github.com/Vincent-Lu-TRS/JapanPost-SaaS/commit/2df259570b99aa3425f46b72a58648f67453b34d)
- [Runtime validation run 34943051463](https://github.com/Vincent-Lu-TRS/JapanPost-SaaS/actions/runs/34943051463)

## 2. Runtime 相容性：已確認與未確認

| 項目 | 已確認 | 邊界／未驗證 |
|---|---|---|
| 原生 Linux 相依套件 | 程式讀取版本化目錄 `vendor/playwright-runtime-v1-linux-x86_64/`，以 manifest、大小及 SHA-256 驗證後才使用；9/15 核對為 147 個 `.deb`、缺件／多件／大小或 hash 不符皆為 0。manifest SHA-256：`1d7dd656cfbe09d6f33424b3c1413a6a33b52b52fdb3224ffd60d0f8b2f3fdce`。 | 這些是 OS 共享程式庫，不是 Chromium 執行檔；hash 相符不代表所有主機都能載入。 |
| 應用執行方式 | 相依套件由程式解到 user-space 暫存路徑；Cloud runtime 不會為這些 Debian 套件執行 apt 或即時下載。現行程式未使用舊 `packages.txt` 路徑。 | 暫存目錄可用空間、權限、清理及主機資源仍是環境因素。 |
| Chromium 本體 | Playwright 1.62.0 固定於 `requirements.txt`；瀏覽器放在 `/tmp/ms-playwright`。空 browser cache 時程式仍會執行 `python -m playwright install chromium`。 | Playwright 預設由 Microsoft CDN 下載瀏覽器；冷啟動仍依賴外連、暫存空間及下載成功。 |
| CI 執行環境 | Run 34943051463 的 job 在 pinned `python:3.12.14-slim-bookworm` linux/amd64 容器實跑；完整套件 469 項通過，workflow 另將 `test_runtime_cold_start.py` 3 項測試再跑一次。 | runner 外層雖為 Ubuntu 24.04，這些結果仍是容器內 Debian 12 Bookworm，不是 Streamlit Cloud 主機驗證。 |
| 真 Chromium 冷啟動 | 冷啟動測試使用隔離的空 Chromium cache 及空 native-runtime 暫存目錄，安裝 pinned Chromium，實際啟動 headless Chromium 並完成空白頁 probe；會檢查安全狀態欄位。 | 不測日本郵政登入、表單／雙彈窗、實際運單、PDF、Drive、Sheets，也不測 Streamlit Cloud 的限制。 |
| Runtime profile | `bot/runtime_profile.json` 接受 Linux x86_64、Python 3.12、glibc ≥2.35，宣告 Debian 12／13 及 Ubuntu 22.04／24.04／26.04。 | CI 本次只實測 Debian 12 Bookworm；profile 列出的其他 OS 版本不是已驗證矩陣。 |
| Streamlit Cloud 主機 | 最新 8 筆完成提供該次部署能處理實際業務的證據。 | Cloud 的 `/etc/os-release`、glibc、架構、Python／Playwright 版本及 Chromium cache 狀態本輪未讀回。Streamlit 官方文件描述平台一般環境為 Debian 11；部署文件的預設 Python 為 3.12，但 app 可選其他支援版本。這些文件不是此 app 單一執行個體的即時指紋；與 repo Debian 12 起跳 profile／CI 有明確相容性疑問。若實機是 Debian 11，製單 runtime gate 可能回 `profile_unsupported`；不要因此推論登入首頁一定整頁不可用。 |
| 暖快取 | 單元測試涵蓋 native-library bundle 的驗證與快取重用邏輯。 | 相關暖快取測試使用 fake extractor／測試 handle；目前沒有可指認的真 Chromium 暖快取重用驗收，不得把它寫成已驗證。 |

因此，事故中如果日誌只記到 `HTTPError` 而沒有可信 HTTP 狀態碼或上游回應，請只記「下載／取得 Chromium 失敗、狀態碼未確認」；不可推定 403、429 或特定供應商故障。

## 3. 安全操作原則

1. **先確認作業階段與是否已送出。** 若日本郵政已收到 submit、或日誌無法判定是否收到，不可直接重點「開始製單」。先核對追蹤號、目標表及 Drive 證據，避免重複出單。
2. **保留雙重防重與 final preflight。** 待製單讀取會比對目標表完成紀錄；開始執行前又會重新讀取來源與目標並檢查所選列。遇到來源 tracking／目標缺完成證據或公式延遲警示，查明受影響列，不可移除過濾或手動繞過。
3. **runtime probe 不等於業務 smoke。** 「檢查製單環境」只短暫啟動／關閉空白頁，不送訂單、不修改試算表；它不證明日本郵政登入、實際出單、PDF、Drive 或 Sheets 回填成功。
4. **部署授權與業務授權分開。** 若既有明確授權仍涵蓋同一範圍的發布，不要重問已授權事項；但不得把發布授權延伸為真實製單、Drive／Sheets 寫入、秘密／白名單變更、Reboot 或其他新範圍的授權。
5. 不在截圖或一般畫面貼出 credentials、OAuth／Gemini secrets、完整收件人地址或未遮罩的訂單資料；回報只留時間、錯誤階段、去識別訂單識別碼及必要錯誤碼。
6. 有執行中的 job 時不得 reboot、切換 runtime 或啟動另一批；先在 UI 確認 job 已終止並核實其業務結果。

## 4. 日常操作與安全 smoke

### 一般操作

1. 使用核准的 Google 登入進入正式站；遇到登入問題先查 Streamlit Cloud logs 與 OAuth 設定狀態，不在事故中臨時改 secrets 或白名單。
2. 訂單缺漏時先開「讀取診斷」，查看來源讀取、篩選原因、目標完成紀錄及最終可打單筆數。需要更新來源清單時才按「重新讀取」；不要短時間重複刷新。
3. 檢查本批每列的收件人、國家、運送方式、金額、數量與必要收件人識別欄位。EU HS Code 的 Gemini 結果是輔助資料，應核對純數字與適用碼長；程式在無預測結果時會記錄 fallback，不能把空白欄位當成已成功預測。
4. 確認所選資料已通過製單前檢查、沒有同一 job 正在執行，並且操作者已有該批真實製單授權後，才開始。啟動後只按一次，依狀態與進度等待。
5. 完成後分別核對日本郵政結果／追蹤號、PDF 上傳及目標 Sheets 回填；某一項成功不能代替其餘項目的 read-back。

### 部署後不製單 smoke

在沒有執行中的 job 時，依序做下列唯讀／安全檢查：

1. 開啟正式站並確認登入及主要頁面可載入。
2. 以「讀取診斷」確認來源與目標表可讀；這會讀取業務資料但不應寫入。
3. 展開「製單環境檢查」並按「檢查製單環境」。UI 明確說明此檢查只啟動／關閉空白頁，不送出訂單、不修改試算表。若 Chromium cache 為空，probe 仍可能透過 Playwright CDN 下載 Chromium。
4. 確認結果與 Cloud logs 時間相符，再記錄實際部署 commit、probe 結果及錯誤階段。

上述 smoke 只證明頁面／資料讀取與當下 browser runtime probe 的範圍；不可宣稱它驗證了日本郵政 submit、真實 PDF、Drive 或 Sheets writeback。真實端到端測試須另有明確業務授權及指定、已核對未出單的測試訂單。

## 5. 故障排除：先定位第一個失敗階段

事故開始後前 15 分鐘只做唯讀診斷。記下 JST 時間、目前部署 commit、UI 顯示階段、第一個錯誤碼、job 是否仍在執行及去識別訂單識別碼；不要先重點製單或 reboot。15 分鐘內若無法取得必要證據，停止重複檢查，將未知項目明列並另立技術追蹤。

| 第一個失敗階段／訊息 | 判斷與安全動作 | 禁止的捷徑 |
|---|---|---|
| 登入／首頁載入 | 查 Cloud logs、部署時間與 OAuth 狀態；不要把登入故障當成 browser runtime 故障。 | 不臨時貼換 secrets、擴大 whitelist 或刪除／重建 App。 |
| 待製單讀取／清單缺漏 | 用「讀取診斷」核對來源讀取、必要欄位、重複單號與目標完成集合；再決定是否重新讀取。 | 不關閉雙重過濾，不用公式延遲警示推定全部訂單都安全可送。 |
| final preflight blocked／要求重新讀取與重選 | 程式在出單前重讀目標完成紀錄及來源快照；所選列可能已完成或資料已改。依 UI 指示重新讀取、重新選取，再確認一次。這一階段代表批次尚未進入日本郵政 submit。 | 不略過 preflight、不把本批整體失敗改判為成功。 |
| 製單環境暫時無法啟動／runtime stage | 現行 `app.py` 先準備 browser runtime，再做最後一次 Sheets／來源 preflight，之後才進自動化；runtime setup failure 會標示「訂單尚未送出」。記錄 `stage`、`error_code`、是否有 HTTP status，核對 exact commit、manifest 與權威 CI。確認沒有 job、沒有 submit 證據後，最多做一次受控人工重試；再次失敗就停止並升級。 | 不把未帶狀態碼的 HTTPError 推定為 403／429；不靠連續「重新讀取」當 runtime 修復；不手動刪除 cache／改主機套件。 |
| 日本郵政登入、欄位或雙彈窗階段 | 核對日誌最後成功步驟、登入狀態及目前可見對話框；檢查 `dismiss_dialogs`／表單欄位的具體回歸。未確認 submit 前可停在頁面，不要盲點對話框。 | 不用腳本暴力點擊未知按鈕、不連續重送同一單。 |
| 日本郵政已 submit，但 tracking／結果不明 | 視為可能已出單；先查日本郵政結果、tracking、PDF／Drive 與目標表，再決定後續處理。 | 不因 UI 顯示 timeout 就認定未出單並重送。 |
| PDF／Drive 上傳／Sheets 回填失敗 | 區分「郵局已出單」與「雲端收尾完成」。依 tracking 與目標表 read-back 判斷 partial write，再依既有授權處理收尾。 | 不重新建立運單來修補上傳或回填問題。 |

「來源 tracking 但目標表缺完成證據」是資料完整性警示：受影響列會被排除，並不單獨證明其他已選列或 browser runtime 故障。應依該批 final preflight 與第一個錯誤階段判斷因果。

## 6. 修復與驗證節奏

1. 選一個有日誌／程式證據支持的根因；每次事故只做一項針對性修正，除非新證據顯示前一假設錯誤。
2. 跑變更直接相關的測試，再對 exact candidate commit 跑一次權威 `Runtime validation`。相同 commit、相同環境沒有新證據時不要重複派發同一 workflow。
3. 若改動 native package 清單、vendor manifest 或 extractor，另跑 `Build Debian 12 Runtime Assets` workflow；核對 package 清單、manifest 數量與 SHA-256，再由 `Runtime validation` 實際解包並啟動 Chromium。
4. 保留完整 CI run URL、commit SHA、結果及測試環境。CI 紅燈不得靠修改測試期待或宣稱 Streamlit Cloud 一定不同而略過。
5. 發布完成後執行第 4 節的不製單 smoke。若 smoke 需要登入以外的外部寫入或真實出單，停止並另取明確授權。

## 7. 發布與失敗回復檢查表

- [ ] 開始前確認沒有 JapanPost 其他 writer／active job，記錄目前 main、PR 與部署 commit；不得把本機候選 worktree HEAD 當成正式站版本。
- [ ] 只在自己的 `codex/*` namespace 推送候選分支；依 registry 的域主／審核流程處理 main，實作者不自審自合。JapanPost 域主目前仍待治理表明確指定，未明確時不得自行合併。
- [ ] 僅在此次修改範圍需要時執行 bundle build；對 exact candidate commit 有一筆成功的 `Runtime validation`。
- [ ] 部署只沿用仍明確涵蓋本次程式範圍的既有授權；改動 secrets、OAuth／白名單、依賴主機、真實訂單或 Sheets／Drive 寫入，均視為不同 gate。
- [ ] 以 Streamlit Cloud logs 與部署狀態確認新 commit 已載入。歷史 log 依時間區分，不以舊錯誤判斷新部署。
- [ ] Reboot 不是一般 smoke 步驟；只有證據指向舊程序且已確認沒有 active job 時才考慮一次 Reboot，並依既有授權執行。
- [ ] 完成不製單 smoke，記錄其有限範圍；任何真實業務端到端驗收另外核准，不因 CI／runtime probe 通過而自動放行。
- [ ] 更新唯一最新 handoff 與治理 board 狀態；舊交接保留為歷史，不覆寫或刪除。
- [ ] 若不製單 smoke 失敗，暫停新批次並保留 logs；以已知健康 commit 建立受審核的 revert PR，照樣跑 exact-commit CI 與安全 smoke。不得直接改 main、force-push 或把回退當成已授權的真實業務操作。

## 8. 已知文件落差與未完成追蹤

### 舊文件已確認落差

- `DEPLOY_GUIDE.md` 的 2026-09-09 段落仍描述執行時下載整套 OS 共享元件；現行程式改為驗證 repo 中的 Debian `.deb` bundle，再解到 user-space 暫存路徑。**只有 Chromium 本體仍可能在冷啟動時走 Playwright CDN。**
- `DEPLOY_GUIDE.md` 舊發布驗收把 Reboot 與真實運單、Drive PDF 及 Sheets 回填綁成固定流程；本手冊改採不製單 smoke，真實寫入另設授權 gate。
- root `CLAUDE.md` 的 2026-06-20 current-worktree／commit 資訊是歷史快照，不是本次確認的正式發布 commit。

已在兩份舊文件開頭加上本手冊指向與 runtime／發布範圍提示；其餘歷史內容未重寫。`docs/superpowers/plans/2026-09-15-runtime-reliability/` 中的舊 HANDOFF／EVIDENCE 保留原文，不作為最新部署狀態。

### 另立技術追蹤，不在本手冊任務執行

1. **本輪未取得實際 Cloud host fingerprint。** 官方文件只描述平台一般環境（Debian 11；Python 預設 3.12、app 可另選），不證明此 app 執行中的 OS／Python。公開首頁、HTTP headers 與瀏覽器端 JavaScript 不能驗證伺服器 process；開啟 Cloud app 會建立 session 並執行 app script，而既有「檢查製單環境」會準備 browser runtime，因此尚未證明是零副作用的純唯讀 host probe。本輪不開 app、不按診斷、不看 secrets 或商務資料。下一步由具 repo write access 的管理者先檢查既有 Cloud logs／部署設定中是否已有不含秘密的必要欄位；若沒有，再另立最小診斷／部署方案並取得對應授權，才讀回 OS release、glibc、架構、Python／Playwright 版本及 Chromium cache。
2. 取得實機 fingerprint 後，把 CI 相容矩陣收斂到實際 Cloud 或逐項新增真實矩陣測試；若主機仍是 Debian 11，先設計相容策略再改產品。
3. 獨立評估是否固定部署 Chromium 本體，以降低 Playwright CDN／冷啟動風險；這是單獨技術決策，不代表本次已離線化。
4. 補足真 Chromium 暖快取重用的直接驗證；mock／fake handle 不算。

## 9. 參考來源

- 程式與 workflow：[app.py](../app.py)、[bot/browser_runtime.py](../bot/browser_runtime.py)、[bot/playwright_runtime.py](../bot/playwright_runtime.py)、[bot/runtime_profile.json](../bot/runtime_profile.json)、[bot/sheets.py](../bot/sheets.py)、[Runtime validation](../.github/workflows/runtime-validation.yml)、[Build Debian 12 Runtime Assets](../.github/workflows/build-runtime-assets.yml)。
- 冷啟動證據：[tests/test_runtime_cold_start.py](../tests/test_runtime_cold_start.py)；native bundle 測試：[tests/test_playwright_runtime.py](../tests/test_playwright_runtime.py)。
- Streamlit 官方：[App dependencies](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/app-dependencies)、[Status and limitations](https://docs.streamlit.io/deploy/streamlit-community-cloud/status)、[Deploy an app / Python version / logs](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy)、[App execution model](https://docs.streamlit.io/get-started/fundamentals/summary)、[Manage your app](https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app)。官方文件列 Community Cloud 為 Debian 11，Python 預設 3.12 但可由 app 選擇其他支援版本；平台一般文件不等同本 app 的 host read-back。
- Playwright 官方：[Browsers](https://playwright.dev/python/docs/browsers)、[Page evaluation](https://playwright.dev/docs/evaluating)。文件說明 Playwright 依版本下載相符的瀏覽器，預設瀏覽器下載來源為 Microsoft CDN；page evaluation 在瀏覽器頁面環境執行，不能作為伺服器 OS 證據。
- 最新 runtime reliability 事實基準：中央交接 `coordination/handoff/20260915-jppost-runtime-reliability-2.md`；本手冊完成後的新交接由治理 board 指向。
