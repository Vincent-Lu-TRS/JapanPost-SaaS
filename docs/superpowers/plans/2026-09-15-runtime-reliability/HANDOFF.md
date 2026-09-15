# JPPOST 2026-09-15 — GPT-5.6 Luna Max Fast 接手點

**最新狀態（2026-09-15）：候選版本已推送至 `codex/jppost-runtime-reliability-20260915`，PR #7 的 Linux CI 與真實 Chromium 冷啟動已通過；PR 等待 JapanPost-SaaS 域主覆核／合併。正式 Streamlit App 尚未更新，正式站不製單 smoke 尚未執行。沒有真實製單或 Google Sheets／Drive 業務寫入；G2/G3仍關閉。**

## 起始需求與目前授權

起始需求是排查9月15日製單失敗並提出長期方案；其後使用者切換模型並授權實作，再明確表示完全授權直接推進正式發布流程，不要再為資料夾存取權或發布授權浪費時間。因此已授權對隔離分支提交／推送、執行GitHub Linux CI、更新現有Streamlit站並作不製單健康檢查。正式站更新前仍須確認沒有活躍製單／回填job。此授權不包括真實Japan Post標籤、Google Sheets／Drive業務寫入、新服務費用或G3主機遷移。

## 接手資料

- 主計畫：[IMPLEMENTATION-PLAN.md](IMPLEMENTATION-PLAN.md)，含T0–T6、A當期方案、B下階段容器路線與G0–G3。
- 根因與版本界線：[EVIDENCE.md](EVIDENCE.md)。
- 可重跑的原缺陷mock：[DIAGNOSTIC-MOCK.md](DIAGNOSTIC-MOCK.md)。
- Repo：`C:\Users\shaku\個人\Claude Cowork\jppost\tmp\streamlit-deploy-JapanPost-SaaS`；main及遠端main=`5be34cd6f7372178be8f579447b3cc83a4f3a5e8`。下次仍須重新核對，不能假設未變。
- 唯讀原日誌：`C:\Users\shaku\Desktop\logs-vincent-lu-trs-japanpost-saas-main-app.py-2026-09-15T01_26_16.623Z.txt`；hash見EVIDENCE。
- 已核驗建置输入：`C:\Users\shaku\AppData\Local\Temp\jppost-runtime-full-validation`中的61份deb，53.1MiB。下次先重驗hash；不信任其中`.ready`或空的展開libdir。

## 關鍵結論

六筆都通過preflight，失敗在取得runtime元件。具體HTTPcode被舊程式抹掉，无法確定上游原因；61个URL目前本機全200。真正可復現的長期鎖死是app將準備失敗的False快取，使下一次不再準備。使用真Streamlit1.56.0隔離mock已證明第一次失敗、第二次仍False且prepare僅1次；清該隔離cache後才成功。

因此A方案保留Streamlit，將既有61個SHA驗證deb隨版本部署，成功-only初始化manager、固定整組依賴profile、版本失效驗證、browser下載有限重試與真正空白頁啟動閘門；耗時初始化放在最後業務preflight之前。Chromium官方下載、Cloud OS及外部服務仍有風險，不能保證永不故障。B完整容器化另核准，不自動購買／建主機或改OAuth。

## 接手後第一步

1. 只有使用者明確說已切換並依本方案開始，才開G0；確認實際模型設定，不自稱已切换。
2. 讀治理AGENTS、registry、board，取得實作鎖，建立codex namespace隔離worktree；既存`.planning/`／`backups/`／`tmp/`不刪。
3. 依T0取得baseline，再T1 RED→T2–T4最小修復→T5回歸→T6真Linux；不先反覆reboot。
4. fresh reviewer通過後才請求G1；main域主未登記，不能自審自合。G2真實訂單另外核准，先讀最新完成證據。

## 不可重蹈的錯誤

- 不把「來源狀態疑似快取過期」的全表警告當本批下載失敗的原因；不移除防重製。
- 不把本機HEAD200當Cloud過去没有HTTP錯誤；不將mock503寫成真實503。
- 不把Playwright解析到1.62.0說成requirements已釘住；現況為`>=1.44.0`。
- 不只修app，`automation.py`也有runtime入口；不忘`*.json`會忽略新的manifest/profile。
- 不把61deb hash吻合當作解包／ABI／browser啟動已驗證；不把舊`.ready`當成功。
- 不把9/9四筆已完成訂單拿來重跑，不把PDF檔名存在說成內容已核對。
- 不把code push、Updated app、installer rc0或mock綠燈當正式業務驗收。
- 不保留對失敗布林值的長期cache；不能只清資料cache就期待browser準備重試。
- 不因環境故障自動重送已進入郵局提交的整批；tracking／回填證據必须保留。

起始診斷階段沒有產品修改。之後G0開啟並開始本機實作；沒有Git提交／push／merge、部署／reboot、真實製單或Google回填。方案文件與本機mock都不等於正式修復驗收。

## 2026-09-15 本機實作更新

- 分支：`codex/jppost-runtime-reliability-20260915`；隔離worktree：`C:\Users\shaku\個人\Claude Cowork\jppost\tmp\JapanPost-SaaS-worktrees\codex-jppost-runtime-reliability-20260915`；原repo未修改。
- 已實作runtime manager／成功結果才快取／失敗可重試、Playwright 1.62.0精確pin、瀏覽器啟動probe、App流程安全閘、mock worker案例。整套unittest 427項通過（Python 3.14.6）；`pip check`無相依衝突、AST 53檔通過、`git diff --check`通過。沒有使用Linux/Python 3.12。發布前P1與環境阻塞見下方最新接續紀錄。
- 獨立review指出中段HTTP狀態會被有限輸出裁切（已加入結構化擷取修正及回歸測試）、Retry-After跨chunk及混合503/429（已修正並測試）、warm runtime未驗執行檔SHA（已加入）、原生資產hash／解包deadline檢查（已加入）。
- **歷史狀態（已被下方9/15最新接續更新）：** 當時尚未實作request-scoped硬時限fence，故將P1記為同步I/O無法硬中止及detached Chromium所有權未證明；當時草案誤以為需要持續存活的Linux supervisor。不要照此過期建議新增常駐helper。
- 阻塞：worktree內`vendor/playwright-runtime`不可讀（Windows Access Denied），無法確認Git是否包含61份deb；本機WSL/Docker不可用，Linux冷啟動與真Chromium未驗證。不可變更ACL、繞過或宣稱production ready。
- 此項較早記錄的待辦僅反映當時狀態；後續狀態以下方最後一節為準。G1/G2仍關閉。

## 最新接續紀錄

- 歷史驗證（後續已增加request fence與新測試）：當時427項通過；只是在Windows／Python 3.14 mock環境，不能視作Linux/Cloud冷啟動驗收。
- 歷史review當時建議persistent Linux supervisor；使用者其後澄清要雲端App自足、不需本機helper，方案已改為同Cloud container內request-scoped子程序，詳見本文件最後一節。
- 本機`vendor/playwright-runtime`仍Access Denied；WSL與Docker不可用。中央治理`G:\共用雲端硬碟\ClaudeCode\AI\AGENTS.md`亦不可讀，請下一位接手先取得可讀副本。不得改ACL或將資產改存到未核准位置。
- `git ls-files -- vendor/playwright-runtime`無檔案輸出，因此不可宣稱61份原生套件已納入分支；`requirements.in`、瀏覽器實機診斷工具、Linux cold-start測試及CI workflow尚未建立，requirements仍有版本範圍。
- 當前狀態：隔離worktree未提交、未推送、未部署；無真實製單或Sheets寫入。保持`G0`本機範圍，不開`G1/G2/G3`。

## 2026-09-15 最新接續狀態：雲端自足 request fence

使用者明確要求完成後不需本機輔助，執行完全留在雲端。因此前文「持續運作的Linux supervisor／主機遷移」不是本輪方向；短期實作改為Streamlit Cloud App在需要時自行透過`sys.executable -m bot.runtime_fence`啟動短命子程序，同一容器內做準備／驗證並自行收尾，不增加使用者電腦上的daemon或cron。依Streamlit官方說明，Cloud App可以在雲端呼叫Python子程序，但Community Cloud仍會在閒置後休眠，不可承諾免費服務永久醒著：[子程序說明](https://docs.streamlit.io/knowledge-base/deploy/invoking-python-subprocess-deployed-streamlit-app)、[Cloud休眠管理](https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app)。

- `bot/runtime_fence.py`及`bot/browser_runtime.py`已加request-scoped `prepare`／`validate` process fence：最小子程序環境、allowlisted有限JSON IPC、Linux subreaper fail-closed、同步操作硬時限、後代清理。第一輪review發現SIGKILL快照後新後代競態；先加mock RED、再修成迴圈重掃並身份核對SIGKILL，另加Linux `setsid` race fixture；第二輪獨立品質review確認這項P1在程式層修正。
- 歷史驗證曾為438 tests／4 skipped；**最新**Windows Python 3.14.6驗證為`python -m unittest discover -s tests` → 458 tests，OK、6 skipped；跳過項均需真Linux／Chromium／`/proc`／subreaper。`pip check`、`compileall`與`git diff --check`皆通過。`requirements.in`／84項精確hash鎖與Linux CI已建立，Windows wheel-only/hash dry-run通過；仍沒有GitHub真Linux或Streamlit Cloud cold-start結果。
- 嚴格界線：fence涵蓋browser runtime準備與驗證，不涵蓋`run_automation()`之真正Japan Post browser。使用者最近日誌在準備成功訊息後才出現`TargetClosedError`，所以目前不能說該業務階段錯誤已修復；日誌記載的automation build ID `2026-08-05-m060505-address1-width-fix`也不同於目前worktree source ID `2026-09-09-browser-launch-fallback`，production是否載入新launch fallback仍待雲端安全驗證。
- 舊目錄`vendor/playwright-runtime`讀取被Windows拒絕；它未列舉、未修改、未stage。為避免存取受保護資料，61個核對過的deb已明確放入新路徑`vendor/playwright-runtime-v1-linux-x86_64/`，此目錄可讀且完成hash驗證。`requirements.in`、runtime檢查器、cold-start測試與immutable-image Linux workflow現已建立。
- 全域治理AGENTS在本輪開始時已讀取；沒有改ACL或繞過。domain owner登錄仍未指定，故implementer不得自審自合main或繞過repository branch protection。
- 修改仍只在隔離worktree及分支；目前尚未commit/push/merge、部署或reboot，沒有真實製單或Google Sheets／Drive業務寫入。使用者已授權繼續G1 branch/CI/no-order Cloud smoke；不得再詢問資料夾存取權。G2/G3仍關閉。

## 先前待辦（已由下方PR與CI紀錄取代）

1. 完成獨立審查；P0/P1先修正，再重跑完整458項本機測試。
2. 審查乾淨後依核准檔案清單stage、commit並推送`codex/jppost-runtime-reliability-20260915`。不要stage `backups/`、session `tmp/`、secrets或舊受保護`vendor/playwright-runtime/`。
3. 等GitHub Actions實際跑完；須確認hash-only wheel install、完整test suite、真Chromium cold/warm、Linux子程序回收都綠燈。失敗時依workflow logs修正，不將Windows skip當通過。
4. CI通過後，唯讀確認正式站沒有活躍製單／回填；更新時記錄build／asset／Python／browser識別，透過「讀取診斷 → 製單環境檢查」執行真正Cloud blank-page probe（不登入郵局、不建立標籤、不寫Google Sheets／Drive）。冷啟動及warm probe都通過前不得宣稱線上恢復。
5. 最近`TargetClosedError`位於runtime準備成功後的真正郵局browser啟動；目前只新增明確TargetClosed低資源重試mock，真Cloud結果仍未知。若Cloud probe可用但實際製單若再失敗，本輪沒有真訂單重試授權；先以安全診斷定位郵局browser，不得盲目提交。
6. repository治理登錄的domain owner仍為「待使用者指定」，implementer不得自審自合main或繞過branch protection。可先推feature branch/PR完成CI及Cloud safe smoke；若合併流程需要owner approval，將其作為唯一治理閘門回報，不再重問存取權。

## 最新發布候選紀錄（2026-09-15）

- Worktree：`C:\Users\shaku\個人\Claude Cowork\jppost\tmp\JapanPost-SaaS-worktrees\codex-jppost-runtime-reliability-20260915`；branch `codex/jppost-runtime-reliability-20260915`；runtime/product code commit `a2f9fa1746b2d012453527fadc33156e329968df`，其後文件同步提交 `0779fcdf78d65f85a1c13ba8494cdb7bdc7f50a1`。遠端 `main` 仍為基線 `5be34cd6f7372178be8f579447b3cc83a4f3a5e8`。
- PR [#7 修復 Streamlit Cloud 製單瀏覽器啟動](https://github.com/Vincent-Lu-TRS/JapanPost-SaaS/pull/7) 已建立；狀態 OPEN、mergeable，尚無 review decision。不要將GitHub帳號管理權等同於登記域主覆核；不得自行合併或改正式App分支設定繞過此閘門。
- Linux CI run `34941076703`（push）及 `34941299377`（PR）均成功；Bookworm runtime bundle builder `34941076696` 成功。文件同步後最新HEAD的push run `34941882604` 與PR run `34941888002` 也都成功；PR CI輸出 `469 tests`、獨立 cold-start suite `3 tests`，真Chromium readiness輸出 `status=ready / stage=complete / error_code=none`，冷啟與warm probe均完成。
- 本機 Windows Python 3.14：`python -X utf8 -m unittest discover -s tests` → 469 tests，OK，6 skipped（需Linux）。
- 發布包為 147 個 SHA-256 驗證的 Debian 12 `.deb`，manifest SHA-256 `1d7dd656cfbe09d6f33424b3c1413a6a33b52b52fdb3224ffd60d0f8b2f3fdce`；執行期不再網路下載這批Linux系統套件。真實失敗點已定位並修正：套件包建置時需以apt列出的落盤檔名配對URI；執行期亦須同時收集 `/lib` 與 `/usr/lib` 下的Chromium元件。
- `backups/`、`tmp/`、`docs/.../backups/`均為本次工作既有／新增之本機未追蹤資料，勿stage或刪除；舊受保護目錄 `vendor/playwright-runtime/` 未列舉、未修改、未stage。
- 尚未完成：由登記域主覆核並合併PR、更新正式Streamlit Cloud App、讀取部署後安全診斷及不製單冷啟／warm smoke。發布前仍只做唯讀活躍job檢查，不得中斷正在執行的製單；部署後僅用不製單探針，無G2真實訂單授權。
