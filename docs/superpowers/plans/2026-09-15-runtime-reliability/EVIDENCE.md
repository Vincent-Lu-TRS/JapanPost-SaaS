# JPPOST 2026-09-15 故障證據與判讀

原始診斷快照：當時修復未實作、未部署、未執行真實製單。隔離實作後的最新狀態見第9節。時間均以日本時間顯示；原日誌為 UTC。

## 1. 基線與來源

- 使用者日誌：`C:\Users\shaku\Desktop\logs-vincent-lu-trs-japanpost-saas-main-app.py-2026-09-15T01_26_16.623Z.txt`，7,240 bytes／164 行。
- 日誌 SHA256：`1E32213108C6A511BBD589DFC4E263316B1A19B3C3755B7776FDE11269FFD9D4`。
- repo：`C:\Users\shaku\個人\Claude Cowork\jppost\tmp\streamlit-deploy-JapanPost-SaaS`。
- 本次 `git rev-parse HEAD` 與 `git ls-remote origin refs/heads/main` 相同：`5be34cd6f7372178be8f579447b3cc83a4f3a5e8`，branch=`main`。
- 開始時 tracked working tree 無修改；既存 untracked `.planning/`、`backups/`、`tmp/` 均保留。
- 上次已記錄的產品程式基線是 `b27c114`，其後三筆是文件提交。本次没有用 Streamlit 進程的 SHA 探針核實實際載入版本，因此不能把遠端 main 一致等同正在運行的每個模組一致。
- 本輪只新增本日期方案／交接文件及治理紀錄；不改舊 HANDOFF 的歷史內容。

## 2. 時間線與錯誤分類

| 日本時間 | 證據（原日誌行） | 可下的結論 |
|---|---|---|
| 09:19:03–09:19:13 | 4–18、114–117 | 平台重新 clone repo 並完成 Python dependencies 安裝。觸發重建的原因、當時 `/tmp` 狀態沒有記錄。 |
| 09:53:55–09:54:04 | 121–137 | 來源讀取成功；6 筆最終可打單，且本批全部通過製單前檢查。 |
| 09:54:04 | 139–143 | 準備 Playwright runtime libraries 時 `HTTPError`；上層轉成 `RuntimeError`。 |
| 09:57:53–09:58:02 | 144–160 | 第二次再讀來源，仍是 6 筆全部通過檢查。 |
| 09:58:02 | 161–164 | 直接 `RuntimeError`，没有第二次 library preparation 訊息。 |

兩次 `job_exception` 是同一初始化故障的表現，不是已知六個不同的訂單資料錯誤。日誌中沒有這兩次的 Chromium 啟動成功、HS Code 預查、郵局登入、登錄／PDF／回填階段。配合 `app.py:1626` 位於 `run_automation` 之前，可確認所示兩次在郵局自動化之前退出；這不代表已查核這六筆在其他人／其他批次的製單紀錄。

### 不应歸咎的訊息

- 行 126／149 的 2,291 筆，是來源已帶 tracking 而目標集合缺證據的其他資料；本批 6 筆随后明确通過 preflight。不能將此訊息當成本次直接原因，更不能因此移除防重製或來源快照保護。
- 行 140 的 `missing ScriptRunContext` 是背景執行緒使用 Streamlit 功能的警告；不是 `HTTPError` 的原因。改由不依賴 Streamlit context 的 runtime manager，可順帶移除這個初始化路徑的依賴，但不以「消除警告」替代修復。
- 本次没有 `TargetClosedError`、`inotify instance limit`、安裝 requirements 失敗或記憶體不足的證據。不能套用 9/9 的推測。

## 3. 程式可證實的故障鏈

1. `bot/playwright_runtime.py:90` `_download_package` 直接對單一 `package.url` 執行 `urlopen(... timeout=120)`；没有 HTTP 分類重試／備援。
2. `bot/playwright_runtime.py:218` 起逐包下載／解包；任一例外中止全組。
3. `bot/playwright_runtime.py:243` 捕捉例外後，只保留 `type(exc).__name__`。套件名稱、HTTP status、來源、重試次數均消失，無法從今天日誌還原是 403／404／429／5xx 中哪一種。
4. `app.py:134–169` `_install_playwright` 裝飾為沒有 TTL／validate 的 `st.cache_resource`。準備失敗回傳 `False`，是可被快取的正常回傳值。
5. `app.py:1626–1627` 收到 False 就拋 `RuntimeError`；`app.py:1738` 起只記例外類型、使用通用「製單流程發生錯誤」訊息。
6. 重新讀取訂單不會清除上述另一份初始化快取。只要快取未因程式更新／清除／進程結束失效，再按開始就繼續拿到 False。

已證實的根因層級：**外部元件取得失敗未復原，加上負面結果被長期快取，造成整批無法開始。** 具體遠端 HTTP 回應原因：**未記錄，無法確定**。

## 4. 本次唯讀 URL 與既存檔案核對

2026-09-15 10:36:36 JST，以有限並行 HEAD 檢查 manifest 的 61 個固定 `deb.debian.org` URL：

- HTTP 200：61；非 200：0。
- Content-Length 總和：55,695,076 bytes，約 53.1 MiB。
- 最大檔 `libllvm19`：25,978,316 bytes；上限 25 MiB = 26,214,400，尚有 236,084 bytes 餘裕；没有超限。
- `mesa-libgallium`：9,630,224 bytes。
- 本機 HEAD 成功，不證明 Cloud 在 09:54 的 GET 成功，也不證明當時來源没有限流／封鎖／暫時故障。没有據此宣稱「網址永久失效」或「故障已消失」。

既存資料：`C:\Users\shaku\AppData\Local\Temp\jppost-runtime-full-validation`。

- 61 份 `.deb`，總和 55,695,076 bytes，逐份 SHA256 與目前 manifest **61/61 相符**。
- 這是 9/9 保留的建置輸入，不是此次下載的新檔。
- 其中 `.ready` 雖寫 `2026-09-09-t2`，展開的 `usr/lib/x86_64-linux-gnu` 實際檔案數為 0。**不可當成已可啟動的 runtime 使用。**
- 這批已核對來源檔適合在下一輪建立受版本控制的部署資產；仍須解包、ABI／browser 配對、冷啟動及授權清單驗證。未發布／未拷入產品。

## 5. 本次隔離 mock 結果與界線

用 `C:\Python314\python.exe -B -` 執行記憶體內 mock；只以 AST 擷取 `app._install_playwright`，真實 Streamlit 1.56.0 cache；下載、解包、mkdir、write_text、browser subprocess 全部隔離。無真實製單或對 Google／郵局寫入。

| 測項 | 本次觀察 |
|---|---|
| helper 首次注入 HTTP 503 | `ok=False`；訊息僅剩 `HTTPError`，503／套件來源被丟棄 |
| helper 第二次恢復下載 | `ok=True`；共 62 次 mock download（第一次失敗 1 次，第二次成功 61 次） |
| 同一個真 Streamlit cache 連續呼叫 installer | `(False, False)`；prepare_calls=1、browser_install_calls=0 |
| 只清掉該隔離 cache 再呼叫 | `True`；prepare_calls=2、browser_install_calls=1 |

這證實 helper 本身可以恢復，但 app 的失敗快取讓它失去再次執行機會。不是修復後測試；没有宣稱 Windows／Python 3.14 mock 等於 Cloud Linux／Python 3.12 Chromium 驗收。

主對話另重跑既有`test_playwright_runtime.py` 5/5與`test_deployment_dependencies.py` 4/4，兩命令exit0。它們在含此缺陷的舊版仍全綠，正好顯示過去測試未覆蓋失敗cache／冷啟動完整鏈；不能用這9個PASS作修復成功聲明。完整重現命令見[DIAGNOSTIC-MOCK.md](DIAGNOSTIC-MOCK.md)。

## 6. 版本與既有驗證不足

- 9/15 安裝結果：Python 3.12.14、Streamlit 1.56.0、Playwright 1.62.0（日誌15／63／88）。
- 實際 `requirements.txt` 仍是 `playwright>=1.44.0`，多數 Google／PDF 等套件也是下限约束。**1.62.0 是當日解出的版本，不是完整固定版本。**
- 目前 runtime manifest `RUNTIME_VERSION=2026-09-09-t2`，固定61份原生套件及hash；Python套件／browserrevision／hostABI没有被同一份可驗證profile鎖定。
- 9/9 的4/4真實製單成功只證明當時那次執行可用，不能證明清空快取後、下載端異常時、平台重建或自動換版本後仍可用。
- 原生套件 hash 正確證明檔案相同，不等於所有依赖可載入；安裝命令 rc=0 不等於 Chromium可啟動；App首頁可開不等於製單可用。

## 7. 官方資料與方案依據（2026-09-15 查核）

- [Streamlit 1.56.0 cache 原始碼](https://raw.githubusercontent.com/streamlit/streamlit/1.56.0/lib/streamlit/runtime/caching/cache_utils.py)：正常回傳才寫 cache；False 也是正常值。
- [cache_resource／validate](https://docs.streamlit.io/1.56.0/develop/api-reference/caching-and-state/st.cache_resource)：可驗證快取資源，無效時重建；切勿將初始化 False 與 validate=False 混為一談。
- [Streamlit 檔案保存界線](https://docs.streamlit.io/develop/concepts/configuration/serving-static-files)、[Reboot](https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app/reboot-your-app)：執行期檔案不能當永久部署資產；普通 rerun 不等於每次刪 `/tmp`。
- [Debian DAK 清理](https://ftp-team.pages.debian.net/dak/docs/generated/dak.clean_suites.html)、[Debian 舊版套件 FAQ](https://www.debian.org/doc/manuals/debian-faq/ftparchives.en.html)、[Snapshot](https://snapshot.debian.org/)：一般 pool 非永久留存，snapshot 可保留歷史但仍有網路與服務可用性限制。這是架構風險，不是本案404證據。
- [Streamlit dependencies](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/app-dependencies)、[平台限制](https://docs.streamlit.io/deploy/streamlit-community-cloud/status)、[Playwright Linux requirements](https://playwright.dev/python/docs/intro#system-requirements)：兩邊文件的支援範圍存在差異；實作前須查實機 os-release／glibc／arch。不能用本機Windows成功代替。
- [Playwright browsers](https://playwright.dev/python/docs/browsers)、[Playwright Docker](https://playwright.dev/python/docs/docker)、[Docker digest pinning](https://docs.docker.com/build/building/best-practices/#pin-base-image-versions)：完整固定環境要將套件／browser／系統image配對；容器仍须受控更新。
- [Community Cloud Dockerfile 歷史官方答覆](https://discuss.streamlit.io/t/can-you-give-streamlit-a-dockerfile-to-build-your-environment/28618/2)：不能只增加Dockerfile就宣稱在原Community Cloud使用了自訂容器；要另選支援容器的host。
- [GitHub大型檔案限制](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github)、[Streamlit repository檔案行為](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/file-organization)：現61deb最大約24.8MiB，低於GitHub單檔50MiB警告／100MiB阻擋界線，可優先採普通Git；Streamlit會複製repository檔案。repo完整歷史大小、展開後資源與實際cold耗時仍須驗證，不能只憑檔案門檻判定正式部署成功。

## 8. 尚未驗證／本輪明確不做

沒有 Cloud 當時 HTTP status／回應headers、沒有實機 OS／glibc readback、沒有當前六筆訂單身分與最新完成證據、没有此次真實 Linux 冷啟動／PDF內容／Sheets回讀。診斷階段沒有測試修復碼、清除正式快取、reboot、部署或真正製單。完整方案與閘門見同資料夾 `IMPLEMENTATION-PLAN.md`。

## 9. 2026-09-15 隔離實作進度（GPT-5.6 Luna Max Fast）

這段記錄更新本文件前述「未實作」診斷狀態。變更僅在隔離分支/worktree，未提交、推送、合併、部署或碰真實訂單／Google Sheets。

- 已新增成功才快取的runtime manager、統一Playwright環境入口、runtime profile、有限下載重試／錯誤分類、瀏覽器啟動probe與job流程安全閘；Playwright pin為1.62.0。詳見worktree目前`bot/browser_bootstrap.py`、`bot/browser_runtime.py`、`bot/playwright_runtime.py`、`app.py`及新增mock測試。
- 最新完整本機測試：`C:\Python314\python.exe -B -m unittest discover -s tests -q`，438 tests，OK、4 skipped；4項均為Linux `/proc`／`setsid`／subreaper整合測試在Windows跳過。測試中的兩行ERROR logger是預期的mock失敗案例，不代表suite失敗。
- 靜態核對：`C:\Python314\python.exe -m pip check`回報No broken requirements；`compileall -q app.py bot tests safe_logging.py`及`git diff --check`通過（只見CRLF轉換提示）。本輪沒有重新統計AST檔數。
- 以上僅在Windows／Python 3.14 mock與靜態檢查，不是Python 3.12/Linux ABI、真實Chromium冷啟動、Streamlit Cloud、Google Sheets或Japan Post驗收。完整直接與間接依賴尚未產出可驗證的精確鎖定檔。
- 初次fresh-context review指出P1：合作式deadline無法中斷同步解包，SIGKILL快照後可能漏掉新生detached descendant。其後已增加request-scoped雲端子程序，Linux subreaper、deadline/process-tree fencing、allowlisted IPC，並以持續重掃/kill修正後代競態；mock重現先RED、修正後GREEN，第二輪獨立review確認窄範圍缺陷修正。Linux process integration test仍因Windows host全部跳過，故Linux行為尚未驗證。
- 範圍限制：fence處理runtime `prepare`／`validate`，不處理後續`run_automation()`正式郵局browser。Cloud日誌所示`TargetClosedError`發生在runtime準備回報成功之後；僅此fence不能證明該錯誤消失。日誌automation build ID為`2026-08-05-m060505-address1-width-fix`，而worktree automation source build ID不同；日誌不能當作目前工作樹launch-fallback已部署的證據。
- `vendor/playwright-runtime`在本機受Windows Access Denied保護；無法核驗資產目錄內容，且`git ls-files -- vendor/playwright-runtime`未列出檔案，故不能認定61份deb已納入版本。WSL列舉受拒且Docker daemon不存在，故無Linux實測。沒有調整ACL、繞過權限或觸碰部署資產。
- T5/T6所列的`requirements.in`、`scripts/check_browser_runtime.py`、`tests/test_runtime_cold_start.py`與runtime-validation workflow尚不存在；`requirements.txt`仍有多個未鎖定版本範圍，不是完整Python相依鎖定。
- 全域治理檔`G:\共用雲端硬碟\ClaudeCode\AI\AGENTS.md`讀取遭拒／路徑不可用；後續若要繼續，需提供可讀副本或可用位置。

交付狀態：`LOCAL_TESTED / BLOCKED_WITH_EVIDENCE`；不是`READY_TO_DEPLOY`。G1/G2/G3仍關閉。

## 10. 2026-09-15 發布候選更新（最新）

使用者後續明確授權不再停留本機，要求直接推進正式發布流程；包含隔離分支推送、Linux CI、正式站更新後的不製單健康檢查。不再因資料夾／資源存取權或發布授權重複詢問。本段取代第9節後半的舊狀態，但不擴大到真實Japan Post標籤、Google Sheets／Drive業務寫入或主機遷移。

- 隔離候選仍以branch `codex/jppost-runtime-reliability-20260915`、base `5be34cd6f7372178be8f579447b3cc83a4f3a5e8`為準；目前尚未commit/push，正式Streamlit站尚未更新，無真實訂單或Sheets寫入。
- 61份核對過的Debian `.deb`及manifest目前位於新路徑`vendor/playwright-runtime-v1-linux-x86_64/`，單檔均小於25 MiB。`bot/playwright_runtime.py`預設只讀此版本化資產，執行期不再下載Debian套件；舊受Windows ACL保護的`vendor/playwright-runtime/`未列舉、未修改、未stage。需Linux冷啟動確認這組套件在目標ABI上可載入。
- 新增`requirements.in`與精確版`requirements.txt`：84個套件、1,249個SHA-256候選雜湊，Playwright釘在1.62.0；`uv 0.12.2`以Python 3.12／`manylinux_2_28`生成。Windows上對多種Linux wheel tag執行pip wheel-only/hash dry-run成功；這不是Linux實際安裝。
- 新增`.github/workflows/runtime-validation.yml`：使用固定SHA的checkout action及固定digest之Python 3.12.14 Bookworm容器，進行`pip --require-hashes --only-binary=:all:`、`pip check`、完整unittest/AppTest、真Chromium空白頁probe與冷啟動程序殘留檢查。workflow不注入production secrets、不部署、不回寫業務資料。此workflow尚未在GitHub實跑。
- 最新本機結果：`python -m unittest discover -s tests` → **458 tests、OK、6 skipped**；略過項需真Linux `/proc`／subreaper／Chromium。`python -m pip check` → No broken requirements；`compileall`與`git diff --check` exit 0。兩行mock錯誤logger是預期測試輸出。
- `tests.test_postal_worker_runtime`與Streamlit AppTest只使用合成訂單／stub，不呼叫Google Sheets／Japan Post。完整驗收只能證明mock邊界正確，不能當線上成功製單。
- 尚未完成：獨立差異審查、branch push、GitHub真Linux workflow、production OS／glibc指紋核對、Community Cloud cold/warm空白頁smoke。最近Cloud日誌的`TargetClosedError`發生於runtime準備成功後的郵局自動化啟動；目前新增的精簡模式 fallback尚未經真Cloud證明有效，因此不得先宣稱該錯誤已排除。
- 部署前仍須唯讀確認沒有活躍製單／回填工作；不得藉reboot打斷現有job。G1的branch/CI/no-order smoke已獲使用者授權；G2真實業務驗收與G3主機遷移仍未授權。main域主尚未登記，實作者不得自審自合main或繞過repository branch protection。

目前正確狀態：`LOCAL_TESTED / RELEASE_CANDIDATE_PENDING_LINUX_CI_AND_CLOUD_SMOKE`；不是「已部署」或「正式修復完成」。
