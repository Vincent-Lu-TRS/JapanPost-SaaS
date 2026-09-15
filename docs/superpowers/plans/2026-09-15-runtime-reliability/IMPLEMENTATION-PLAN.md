# JPPOST 執行環境可靠性 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: 使用 `executing-plans` 逐項實作；需要獨立子任務時依治理規則使用 subagent，最後由 fresh-context reviewer 驗收。清單 checkbox 仍須依證據逐項更新。使用者已明確授權繼續完成發布流程；本文件以下「目前授權」以最新對話為準。

**Goal:** 修復 2026-09-15 元件初始化失敗後無法製單的問題，並使重新建立環境、暫時下載失敗、版本更新都能被可重現地驗證，而不改製單業務或既有版面。

**Architecture:** 當期保留 Streamlit Community Cloud 與現有 OAuth／Google Sheets／郵局流程。runtime manager 在每次需要時以同一雲端容器內的 `sys.executable -m bot.runtime_fence` 啟動短命、受限時的子程序；Linux子程序自行成為 subreaper，工作完成／逾時前回收其browser／installer後代，不另設常駐daemon或任何本機輔助。準備成功後才讀取最後製單快照、進入業務流程。完整 OS／browser 容器化是下一階段的獨立遷移閘門，不能把當期修復宣稱為完整主機環境固定。Streamlit可在雲端App內啟動Python子程序（[官方說明](https://docs.streamlit.io/knowledge-base/deploy/invoking-python-subprocess-deployed-streamlit-app)）；這不會改變Community Cloud閒置後休眠的服務限制（[官方服務管理說明](https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app)），因此不承諾免費站24/7常駐。

**Tech Stack:** Python 3.12、Streamlit 1.56.0、Playwright（候選驗證基線1.62.0）、Chromium、現有 unittest／Streamlit AppTest、Git 版本化二進位資產、現有 Google OAuth／Sheets／Drive。

---

## 0. 停止閘門與目前狀態

**`G0 = LOCAL IMPLEMENTATION COMPLETE; G1 = AUTHORIZED TO PROCEED THROUGH REVIEW, CI, BRANCH PUBLICATION AND NO-ORDER CLOUD SMOKE; G2/G3 = CLOSED`**

日期：2026-09-15，日本時間。原始repo保持不動；本次實作worktree：

`C:\Users\shaku\個人\Claude Cowork\jppost\tmp\JapanPost-SaaS-worktrees\codex-jppost-runtime-reliability-20260915`

分支：`codex/jppost-runtime-reliability-20260915`；基線：`5be34cd6f7372178be8f579447b3cc83a4f3a5e8`。下文路徑均相對此worktree。

使用者已明確授權不再停留本機：允許在候選隔離分支完成review後提交／推送、執行GitHub Linux CI，並按順序完成正式站更新與不製單Cloud smoke；不再因資料夾存取權或發布授權重複詢問。此授權不包含真實Japan Post標籤、Google Sheets／Drive業務寫入、G3主機遷移或費用支出。正式站更新／重啟前仍須唯讀確認沒有進行中的製單／回填工作；不得中斷活躍工作。任何Linux CI或Cloud probe失敗都先停在該閘門修正，不以本機mock代替。

### 本次執行紀錄（持續更新）

- 已完成：隔離worktree與分支建立；runtime manager、固定Playwright依賴版本、隨版原生套件驗證／解包、headless啟動probe、App安全停止與mock worker測試已實作。
- 已補強：大型輸出保留中段HTTP失敗狀態；跨讀取區塊解析Retry-After，混合503／429時尊重429限流等待；runtime warm reuse 驗證瀏覽器執行檔SHA256；原生資產hash／解包各階段加入合作式deadline檢查。
- 最新本機驗證：Python 3.14.6執行全套unittest 458項通過、6項真Linux Chromium／程序測試因Windows host跳過；`pip check`無相依衝突；`compileall`與`git diff --check`通過。測試輸出兩行mock錯誤logger是預期的失敗路徑案例，不影響suite（exit 0）。84個Python套件均有精確版與SHA-256 lock；Windows cross-target wheel/hash dry-run通過，但不能取代Linux實裝。
- 已修正的P1：同步準備／驗證已移入按需啟動的雲端子程序；審查發現的「SIGKILL快照後新後代未再殺」競態已改為限時重掃並終止新發現的PID/start-time，並新增mock與Linux故障注入案例。獨立審查確認程式邏輯已修正，但所有4項Linux `/proc`／subreaper整合測試在Windows均跳過，所以真Linux程序回收仍未證明。
- 重要範圍界線：上述程序閘只包住runtime `prepare`／`validate`，不包郵局自動化的`run_automation()`正式瀏覽器。提供的Cloud日誌在runtime prep後出現`TargetClosedError`；目前沒有Cloud/Linux證據能證明它已消失。該日誌build id也不能證明當前checkout的launch fallback正在production生效。
- 已完成的T5/T6工件：61份SHA-256核對的原生套件放入全新、可讀的`vendor/playwright-runtime-v1-linux-x86_64/`版本目錄；`requirements.in`／帶hash的`requirements.txt`、無secrets的immutable-image GitHub Actions Linux workflow、冷啟動檢查器及測試均已建立。原先受Windows ACL保護的舊`vendor/playwright-runtime/`未列舉、未修改、未stage。尚待獨立review、branch push後實跑Linux workflow、正式站OS指紋及真Cloud冷／暖空白頁probe；目前不得稱部署完成或已排除所有Cloud `TargetClosedError`。

| 閘門 | 如何開啟 | 開啟後範圍 |
|---|---|---|
| G0 本機實作 | 已完成 | runtime候選實作及mock已完成；不代表Linux／Cloud驗收通過 |
| G1 發布與正式維護 | 使用者已明確授權繼續發布；依序需獨立review、CI通過及正式站無活躍job | 限本版branch/PR、核准後更新現有Streamlit站、cold/warm readiness與不製單smoke；不含新服務／費用／改權限 |
| G2 真實業務驗收 | G1無副作用smoke通過；核對當時待製清單、完成證據，使用者確認本次訂單／包裹範圍 | 僅批准的真實批次；PDF、Drive、Sheet逐一回讀；不得盲目整批重試 |
| G3 容器與主機遷移 | 使用者另外核准候選host、費用上限、資源隔離與遷移計畫 | 另案B；本計畫不建立VM、不改域名／OAuth、不動其他系統 |

域主尚未正式登記，實作者不得自審自合main；因此先以獨立review agent完成程式覆核、以branch/PR提供審查證據，且不繞過repository branch protection。此治理規則不妨礙已授權的隔離branch push及CI。沿用分支namespace `codex/*`，保留既存untracked `.planning/`、`backups/`、`tmp/`。更新／重啟前必須唯讀確認沒有正在製單或回填的job；不得把部署當成可以中斷活躍業務的授權。

## 1. 已知原因、未知原因與修復範圍

完整證據：[EVIDENCE.md](EVIDENCE.md)。

- 今天兩次均為6筆通過preflight，故障發生於原生runtime下載，尚未啟動郵局自動化。
- 已重現：一次HTTP失敗被helper轉成False，再被`st.cache_resource`記住；即使取得元件的能力恢復，後續仍直接拿到False。
- 未知：Cloud當時HTTP status與失敗套件，因舊程式抹除了資訊；本機61個URL今天都回200，不能斷言404或來源永久失效。
- 當日Playwright解析成1.62.0，但requirements仍是`>=1.44.0`。只固定部分Python套件，不能稱作環境已完整固定。
- 存在61份可重新核驗的原始deb，約53.1MiB。展開資料夾是空的；不要拿舊`.ready`當成功證據。

本次不重做姓名／品項／寄送方式／追加製作／HSCode／重量／雙彈窗／PDF命名／回填設計。保留：Weight全程不輸入、只有明確追加製作才有追加包裹、取消訂單排除、來源與目標防重製、回填驗證後才算完成、背景進度、20分鐘資料快取、日本時間、橘色開始按鈕、簡潔UI。不能為了通過測試放寬既有安全檢查。

## 2. 方案選擇

| 選項 | 判定 |
|---|---|
| 只reboot／清cache | 能暫時解除負面cache，但不能修復下載故障或防止重演；不作交付方案。 |
| 只換Debian mirror／加retry | 仍需冷啟動逐一取得61檔，亦不能解決失敗cache；不作主方案。 |
| **A：版本隨附原生資產＋可復原初始化＋實際啟動閘門** | **當期採用。** 保留目前免費站及操作體驗；不新增外部服務憑證。 |
| B：整個現有Streamlit App置於固定容器映像 | 更能控制OS／browser整組環境；符合長期方向，但免費host可用性／容量尚未核實，另立G3，不阻塞A。 |

**A的誠實界線：** 移除Debian逐包下載，不等於整個App完全離線。Git部署、官方Chromium取得、Google／郵局服務及Cloud主機仍可能中斷；瀏覽器下載要可復原並有總時限，失敗須保留可診斷原因。平台OS仍非我方控制，遇到不相容profile必須停機報明原因，不能自動升／降任意套件去湊。

## 3. 檔案責任與變更清單

| 檔案 | 計畫責任 |
|---|---|
| `vendor/playwright-runtime-v1-linux-x86_64/manifest.json`（新）及61份原檔名`.deb` | 保留原來源URL、檔名、bytes、SHA256、版本；只存公開套件，不存cookie／訂單／secret。舊受ACL保護的`vendor/playwright-runtime/`不讀取、不修改。 |
| `scripts/vendor_playwright_runtime.py`（新） | 從已知本機來源逐檔核驗並複製；未核驗不得輸出manifest。只供建置，不給製單路徑使用。 |
| `bot/playwright_runtime.py`（改） | 預設只讀隨版deb；版本化目錄、完整檢查、暫存解包、原子發布；保留安全解包防護。 |
| `bot/browser_bootstrap.py`（新） | 成功資源快取、單次初始化鎖、總時限、固定版browser安裝、空白頁probe、結構化錯誤。不得呼叫Streamlit。 |
| `bot/runtime_profile.json`（新） | 程式release、manifest digest、Python minor、Playwright version／browser revision、經驗證OS／arch／glibc相容範圍及launch參數識別。數值取自驗證工具，不猜填。 |
| `scripts/check_browser_runtime.py`（新） | 不帶郵局／Google憑證，真正啟停headless browser；輸出安全JSON和非零exit code。 |
| `app.py`（改） | 移除快取False的installer；共用manager、先準備後最終preflight、簡短環境錯誤及job安全收尾。 |
| `bot/automation.py`（小改） | 與app使用相同profile／runtime環境；消除第二條即時下載入口，不改郵局流程。 |
| `safe_logging.py`（改，`app.py:77`已核對） | 只擴允許的runtime診斷欄位，不能取消遮罩。 |
| `.gitignore`（小改） | 現在`*.json`會忽略manifest/profile；只加兩個精確白名單，仍保護secret JSON。 |
| `requirements.in`（新）、`requirements.txt`（改） | 保存直接依賴意圖，發布版完整釘住直接與間接Python依賴；檢查完整resolver結果。 |
| `tests/test_playwright_runtime.py`、`test_deployment_dependencies.py`、`test_postal_start_flow.py`、`test_automation_helpers.py`、`test_safe_logging.py`（改） | 回歸、安全與既有流程不變。 |
| `tests/test_browser_bootstrap.py`、`test_runtime_assets.py`、`test_runtime_cold_start.py`（新） | 失敗後復原、單次併發、資產／版本／冷啟動檢查。 |
| `.github/workflows/runtime-validation.yml`（新） | 隔離Linux測試；PR不接production secrets、不自動發版。 |
| `DEPLOY_GUIDE.md`、`HANDOFF.md`、`memory.md`（完成後改） | 分開記錄程式版／資產版／部署版／測試與實單狀態，刪除「稍後再試即可」的錯誤保證。 |

所有既有檔修改前依治理規則備份。新檔同名若已存在，先比對內容、不得覆蓋其他session工作。

## T0：接手、凍結基線與實機profile取證

- [ ] 讀G0授權、AGENTS／registry／board；建立實作鎖並read-back。先核對此方案與EVIDENCE，不能從旧HANDOFF跳回其他任務。
- [ ] 檢查HEAD／dirty後，建立`codex/jppost-runtime-reliability-20260915`隔離worktree。只從核實的main基線分支；不reset／清tmp。
- [ ] 保存`git rev-parse HEAD`、`git status --short`、日誌hash；先跑原測試取得本輪baseline，不引用歷史「388 passed」代替。
- [ ] 核對安全log實際檔案位置、app兩個runtime入口、現有Docker與workflow；將本表路徑落到實際檔案。不要批量改寫大型automation。
- [ ] 在可取得的正式環境唯讀取得OS／arch／glibc／Python／Playwright／browser revision；若現站沒有安全入口，不在本機假裝取得，先用G1核准的診斷部署收集，再繼續Cloud相容性驗收。

取證命令（不輸出env／secret）：

```bash
python -c 'import sys,platform,importlib.metadata as m; print(sys.version); print(platform.system(), platform.machine(), platform.libc_ver()); print(m.version("streamlit"), m.version("playwright"))'
cat /etc/os-release
python -m pip check
python -m unittest discover -s tests -v
```

`cat /etc/os-release`為唯讀Linux命令；不得對secret檔照做。browser revision由安裝的Playwright `driver/package/browsers.json`讀取並記錄實際使用的chromium／headless-shell entry，不自訂編號。Cloud Python只能選擇平台提供版本，不承諾可固定平台patch／OS。

T0驗收：基線清楚、original tests結果已保存、尚未有業務變更；OS若未知明列`NOT_OBSERVED`，不得通過G1最終驗收。

## T1：先重現缺陷並建立拒絕假成功的測試

**Files:** 新`tests/test_browser_bootstrap.py`；改`tests/test_playwright_runtime.py`、`tests/test_postal_start_flow.py`。

- [ ] 保留現有False-cache重現作baseline證據：第一次失敗→第二次仍False、prepare只執行一次；它應在舊碼重現，不當作修復後通過標準。
- [ ] 先寫新的目標測試：一次失敗後下一次初始化能成功、但只重試環境準備而不重送訂單。使用T3的接口，舊碼下應因接口尚不存在或錯誤行為而FAIL。

```python
import unittest
from unittest.mock import Mock
from bot.browser_bootstrap import RuntimeManager, RuntimeSetupError

class RecoveryTest(unittest.TestCase):
    def test_failure_is_not_reused_as_ready(self):
        handle = object()
        prepare = Mock(side_effect=[
            RuntimeSetupError("browser_install", "download_failed", retryable=True),
            handle,
        ])
        manager = RuntimeManager(prepare=prepare, validate=lambda _, deadline: True)
        with self.assertRaises(RuntimeSetupError):
            manager.ensure_ready("profile-a")
        self.assertIs(manager.ensure_ready("profile-a"), handle)
        self.assertEqual(prepare.call_count, 2)

    def test_good_handle_is_reused_but_invalid_handle_is_not(self):
        first, second = object(), object()
        prepare = Mock(side_effect=[first, second])
        valid = Mock(side_effect=[True, False])
        manager = RuntimeManager(prepare=prepare, validate=valid)
        self.assertIs(manager.ensure_ready("a"), first)
        self.assertIs(manager.ensure_ready("a"), first)
        self.assertIs(manager.ensure_ready("a"), second)
        self.assertEqual(prepare.call_count, 2)
```

- [ ] 跑`python -m unittest discover -s tests -p test_browser_bootstrap.py -v`，保存RED結果。
- [ ] 加入表T5全部故障案例的具體assertions；不能只檢查原碼包含某一字串。

## T2：把已驗證原生元件變成部署資產

**Files:** `scripts/vendor_playwright_runtime.py`、`vendor/playwright-runtime-v1-linux-x86_64/*`、`bot/playwright_runtime.py`、`tests/test_runtime_assets.py`。

- [ ] 為builder寫失敗測試：少一檔、hash不符、過大檔、重複／越界檔名均不能生成成功manifest。
- [ ] builder只從`C:\Users\shaku\AppData\Local\Temp\jppost-runtime-full-validation`讀原始61個deb；再次核對現有`REQUIRED_RUNTIME_PACKAGES`的hash，不信任舊`.ready`或空libdir。
- [ ] 實作builder最小核心如下；外層以argparse提供`--source-dir`／`--output-dir`，輸出必須是repo內新的版本資產目錄。若output已存在，先驗一致性或停止，不自動覆寫。

```python
import hashlib, json, shutil, tempfile
from pathlib import Path
from bot.playwright_runtime import REQUIRED_RUNTIME_PACKAGES, MAX_PACKAGE_BYTES

def stage_assets(source: Path, output: Path) -> str:
    if output.exists():
        raise FileExistsError(output)
    checked = []
    seen = set()
    for package in REQUIRED_RUNTIME_PACKAGES:
        if Path(package.filename).name != package.filename or package.filename in seen:
            raise ValueError("invalid asset filename")
        seen.add(package.filename)
        path = source / package.filename
        if path.is_symlink() or not path.is_file():
            raise ValueError("missing or linked asset")
        size = path.stat().st_size
        if not 0 < size <= MAX_PACKAGE_BYTES:
            raise ValueError("asset size rejected")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != package.sha256:
            raise ValueError("asset checksum rejected")
        checked.append((path, {"name": package.name, "filename": package.filename,
                               "source_url": package.url, "sha256": digest, "bytes": size}))
    output.parent.mkdir(parents=True, exist_ok=True)
    # TemporaryDirectory只清理本次新建目錄；不接收任意既有目錄作清理目標。
    with tempfile.TemporaryDirectory(prefix="assets-", dir=output.parent) as temporary:
        staged = Path(temporary) / "payload"
        staged.mkdir()
        for path, metadata in checked:
            copied = staged / metadata["filename"]
            shutil.copyfile(path, copied)
            with copied.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != metadata["sha256"] or copied.stat().st_size != metadata["bytes"]:
                raise ValueError("copied asset verification rejected")
        payload = {"schema": 1, "packages": [entry for _, entry in checked]}
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        (staged / "manifest.json").write_bytes(body)
        staged.rename(output)
    return hashlib.sha256(body).hexdigest()
```

以測試核對明確關閉、複製後再hash、失敗只清自己的暫存目錄及manifest最後發布。builder只能單一寫者執行，不與其他session同時建置同一output。

- [ ] 輸出61檔、總大小55,695,076 bytes，保留來源與套件授權資訊；普通Git追蹤這批原檔即可作優先方案。每檔均低於25MiB，不必先引入LFS／公開Release／新下載服務。G1前核對repo總體積和免費方案限制；若不符，停止請使用者選擇資產承載方式，不自動購買或公開repo。
- [ ] 在`.gitignore`的`*.json`後只追加`!vendor/playwright-runtime-v1-linux-x86_64/manifest.json`及`!bot/runtime_profile.json`。驗收stage/commit後`git ls-files`確實含這兩檔和61個deb；`git check-ignore .streamlit/secrets.toml`仍命中，不解除任何secret保護。
- [ ] runtime production預設讀`vendor/playwright-runtime-v1-linux-x86_64`，先bytes+SHA核對，禁止呼叫`urlopen`去Debian作隱性fallback。`scripts/vendor_playwright_runtime.py`是離線bundle建置工具，不由app呼叫。
- [ ] 解包至`/tmp/jppost-playwright-runtime/<manifest_digest>.staging-<random>`；保留原路徑／symlink越界防護，拒絕device／FIFO等非必要類型。檢查所有資產／解包結果後才發布成digest目錄。
- [ ] 用同一process鎖及Linux檔案鎖避免兩個initializer互相覆蓋；只在自己新建的子目錄做失敗清理。不得遞迴刪`/tmp`或他人活躍版本。
- [ ] marker改成包含manifest digest、platform fingerprint、解包檔案hash清單的ready metadata；reuse前驗metadata／所需檔案，啟动probe另驗真正linkage。`.ready`單獨存在不能通過。
- [ ] `apply_to_process_env=False`作新caller預設，LD_LIBRARY_PATH只傳子process，不每次重複往全域env添加同一段路徑。

驗收命令：

```bash
python -m unittest discover -s tests -p test_runtime_assets.py -v
python -m unittest discover -s tests -p test_playwright_runtime.py -v
```

Expected：缺檔／hash／解包失敗沒有ready marker、沒有browser啟動；Debian網路被完全封鎖仍能從部署資產準備lib。實際ABI是否可用由T3／T6驗證，不能只靠mock斷言。

## T3：獨立初始化管理器，失敗可復原但不無限重試

**Files:** 新`bot/browser_bootstrap.py`、`scripts/check_browser_runtime.py`、`bot/runtime_profile.json`；改runtime helper。

### 2026-09-15 G0增補進度

- [x] `prepare`和`validate`透過`sys.executable -m bot.runtime_fence`在目前Cloud App容器內按需執行；維持原manager API，子程序只收最小必要環境，IPC只傳明確核准的RuntimeHandle欄位，不傳Google／Shopify／Japan Post secrets。
- [x] request子程序在Linux工作前啟用subreaper；同步準備／檔案解包／驗證有父程序硬時限；非零／逾時／留下後代均不得標為ready。回收期間每輪重新列舉並終止新出現的後代，並以PID及start-time雙重確認避免誤殺重用PID。
- [x] 先寫mock回歸測試確認舊cleanup在SIGKILL快照後漏殺新後代（RED），修正後focused suite通過（GREEN）。另有Linux `setsid`新後代注入測試，但本Windows工作站跳過，不能將之記為Linux實測。
- [x] 全套本機測試458項通過、6項真Linux／Chromium測試在Windows跳過；`pip check`、`compileall`、`git diff --check`通過。
- [ ] Linux 3.12／Community Cloud冷啟動、完整真Chromium程序回收與production不製單smoke尚未完成；使用者已授權推進這些發布驗收，不含真實製單。
- [ ] 本子程序只監督runtime prepare/validate，不監督之後真正郵局製單的`run_automation()` browser；仍需在Linux/Cloud單獨驗證當前`TargetClosedError`是否排除，不能用本mock suite替代。

- [ ] 實作下面success-only管理核心；它只共享不可變環境描述，不共享Playwright page／browser／context。`prepare`成功前不可放進快取；所有失敗讓下一次顯式請求仍可嘗試。

```python
import threading, time
from concurrent.futures import Future, TimeoutError as FutureTimeout
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

@dataclass(frozen=True)
class RuntimeHandle:
    profile_key: str
    executable_path: Path
    browser_revision: str
    env: Mapping[str, str]  # 建立時用MappingProxyType包住獨立dict，不共享可變env。

class RuntimeSetupError(RuntimeError):
    def __init__(self, stage, code, *, retryable=False, http_status=None):
        super().__init__(code)
        self.stage, self.code = stage, code
        self.retryable, self.http_status = retryable, http_status

class RuntimeManager:
    def __init__(self, *, prepare, validate, budget_seconds=120):
        self._prepare, self._validate = prepare, validate
        self._budget = budget_seconds
        self._lock = threading.Lock()
        self._ready = None
        self._key = None
        self._flight, self._flight_key = None, None

    def _remaining(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeSetupError("bootstrap", "deadline_exceeded", retryable=True)
        return remaining

    def _acquire(self, deadline):
        if not self._lock.acquire(timeout=self._remaining(deadline)):
            raise RuntimeSetupError("bootstrap", "lock_timeout", retryable=True)

    def ensure_ready(self, key):
        deadline = time.monotonic() + self._budget
        self._acquire(deadline)
        try:
            if self._flight is not None and not self._flight.done():
                if self._flight_key != key:
                    raise RuntimeSetupError("bootstrap", "profile_busy", retryable=True)
                flight, leader, candidate = self._flight, False, None
            else:
                candidate = self._ready if self._key == key else None
                flight, leader = Future(), True
                self._flight, self._flight_key = flight, key
                self._ready, self._key = None, None
        finally:
            self._lock.release()
        if not leader:
            try:
                return flight.result(timeout=self._remaining(deadline))
            except FutureTimeout as exc:
                raise RuntimeSetupError("bootstrap", "wait_timeout", retryable=True) from exc
        try:
            valid = candidate is not None and self._validate(candidate, deadline)
            handle = candidate if valid else self._prepare(key, deadline)
            if handle is None or handle is False:
                raise RuntimeSetupError("bootstrap", "invalid_ready_result")
            self._acquire(deadline)
            try:
                self._ready, self._key = handle, key
            finally:
                self._lock.release()
            flight.set_result(handle)
            return handle
        except BaseException as exc:
            # 同輪等待者收到相同failure，不各自開新120秒prepare。
            # 下一個顯式呼叫看到flight.done()，才開下一輪，不永久cache錯誤。
            flight.set_exception(exc)
            raise
```

- [ ] `prepare(key, deadline)`串接：profile相容性檢查→T2準備libs→固定Playwright對應Chromium安裝→實際headless probe→回傳RuntimeHandle。`validate(handle, deadline)`核對profile／manifest／執行檔／資產有效性，無效回False；驗證錯誤需分類，不吞後永遠返回True。key包含release digest、asset digest、Python minor、Playwright version、browser revision、platform與launch profile，不能只用手寫日期常數。
- [ ] **整個ensure_ready從入口共用120秒總deadline，含lock等待、檔案核验／解包、安裝、probe與子process收尾。** 重工作放到可終止的獨立bootstrap子process；leader以剩餘budget等待並保留收尾時間，不能只在工作完成後才檢查是否超時。單次browser安裝最多45秒、最多3次，間隔1秒／3秒均受剩餘deadline限制。殺掉並wait自己建立的process group及其child；不能留browser／installer孤兒。同輪等候者共享Future的單次結果，下一個顯式請求才開始新一輪。
- [ ] 只對可辨识的暫時網路錯誤（timeout／DNS暫時故障／HTTP408、429、5xx）有限重試；429尊重可解析且不超剩餘budget的Retry-After。403／404／unsupported平台／hash錯／ABI缺少不反覆重試同一輸入。installer stderr無法可靠辨識時標記unknown，不能猜狀態碼；最多一次重新取得官方同revision，總attempt仍≤3。
- [ ] Chromium仍採Playwright官方installer、固定套件對應revision；統一`BROWSER_ROOT=/tmp/ms-playwright`（production Linux），installer env、Playwright driver啟動前設定及profile紀錄一致。只在process啟動前設定，不在不同job之間切換全域path。profile必須記錄實際驗證過的browser flavor與相對執行檔位置；組成的絕對路徑必須位於BROWSER_ROOT，版本／hash核對後才放進RuntimeHandle。禁止`latest`、任意mirror、下載別的browser revision或略過校驗。
- [ ] **probe、正式launch及fallback一律明確傳`executable_path=str(handle.executable_path)`和同一份`env=dict(handle.env)`。** `chromium.launch(env=...)`不足以控制driver查找執行檔的位置，不能只改它。`_launch_browser_with_fallback`新增這個keyword並傳到每次launch，所有callsite／測試一併更新；郵局流程本身不變。增加父process未設／故意設錯PLAYWRIGHT_BROWSERS_PATH的真Linuxprobe，仍須使用已驗證的明確執行檔。官方browser下载依赖仍存在，不能宣稱完全消除下載風險。
- [ ] 空白頁probe必須真的啟動browser→new_context→new_page→`set_content("<title>JPPOST runtime probe</title>")`→assert title→在finally關閉page/context/browser。不導航郵局、不讀secret、不產PDF或寫Google。probe使用與實際job相同headless參數及child env，確認default與既有fallback識別，不能靠不同browser掩蓋問題。
- [ ] `scripts/check_browser_runtime.py`輸出`release_digest/asset_digest/playwright_version/browser_revision/platform/stage/status/elapsed_ms`的安全JSON；錯誤exit!=0。不能把單純installer rc=0當此腳本通過。
- [ ] 錯誤分類最少為`asset_missing`、`asset_checksum`、`asset_extract`、`profile_unsupported`、`browser_download`、`browser_install_timeout`、`browser_launch`、`bootstrap_unknown`。為每種分類建立fixture，包含原始訊息含token／email時仍不外洩的測試。

驗收：T1新測試GREEN；新增profile改變、cache檔案消失、同時5個caller遇持續失敗仍只prepare一次且全部在各自budget內退出、下一個顯式呼叫可復原、所有鎖／檔案／子process階段deadline中止及資源釋放測試全過。測試用Event/Barrier控制等待者已加入同輪，不用任意sleep假定併發成立。

## T4：接回現有App，保留安全業務邊界

**Files:** `app.py`、`bot/automation.py`、現有安全log模組、`tests/test_postal_start_flow.py`／`test_safe_logging.py`。

- [ ] 移除`_install_playwright`快取布林結果的做法；主Streamlit thread可以用`st.cache_resource(show_spinner=False)`保存RuntimeManager物件，背景worker只呼叫普通Python manager，不建立spinner或接觸st.session_state。
- [ ] 在合法登入後作首次預備；只用既有位置的一句「正在準備製單環境」／「製單環境暫時無法啟動，訂單尚未送出，請稍後再試」呈現。不改版、不新增技術表格、不重複列六個同樣的紅色錯誤。
- [ ] 每次job入口再次validate／ensure_ready；**耗時的初始化必須在最終來源與完成證據preflight之前**。若中途重新初始化或準備超時後恢復，重新讀最終來源／目標，不沿用幾分鐘前的可製快照。HS預查依既有流程保留，不借機改業務fingerprint。
- [ ] app及`automation.py:1744`的兩個runtime入口都改為同profile／local資產。`run_automation`也可由CLI直接呼叫，所以不可只修app而留第二條download路徑；在CLI入口沿用同manager或明確傳入已驗證handle。
- [ ] runtime錯誤在进入`run_automation`之前專門捕捉：整批標為「尚未送出／環境準備失敗」，成功數0，保留選取／編輯資料，可重新開始，安全釋放本次job lock。不要假設六筆訂單本身有問題。
- [ ] 一旦进入真實automation，禁止manager自動重跑整批；已取得tracking／PDF的訂單只能沿現有回填／補收尾路徑。狀態不明先查完成證據，不自動當「未製單」再送。
- [ ] log只擴allowlist：`stage/code/package_name/source_host/http_status/attempt/retryable/elapsed_ms/release_digest/asset_digest/browser_revision/exit_code`。URL只可記預知公開套件host/path，不記query、Authorization、cookie、頁面內容、訂單／姓名／地址；原始例外不能直接顯示給同仁。未知HTTP狀態必須null，不編造503。
- [ ] runtime事件以一個diagnostic id串起後台資料；UI只簡短摘要與既有收合診斷。不擴大角色／白名單權限。

**現有`test_postal_start_flow.py:462–513`主要是原碼斷言，不存在可直接套用的worker執行fixture。先新增`tests/test_postal_worker_runtime.py`作實際執行harness，再做以下assertion。**

harness以AST只編譯`app.py`的`_start_job`函式（沿用DIAGNOSTIC-MOCK的隔離方式，不執行App頂層），提供獨立fake registry／job lock、受控thread（同步執行target）、mock runtime manager、source／completion讀取、`_load_current_automation_module`、HS預查、回填。函式若仍有直接`from bot.automation import`，移除冗餘import，統一走可替換loader。所有外部網路及副作用預設raise AssertionError，僅fake允許路徑；新增registry和job結果讀回斷言。

`calls=[]`由每個Mock side_effect追加事件名；fake loader返回SimpleNamespace，含真會被執行的mock run_automation／HS函式。回填也明確注入，不能只是讀原碼說有呼叫。依序真執行三個harness scenario：準備失敗、成功、準備期間來源變已完成。必須加入的assertion：

```python
# 在新增worker harness注入RuntimeSetupError後：
run_automation.assert_not_called()
backfill_results.assert_not_called()
# 注入環境準備會改變來源狀態：最後的completion read必須在prepare之後。
assert calls.index("prepare_ready") < calls.index("final_completion_read")
assert calls.index("final_completion_read") < calls.index("run_automation")
```

fixture中的三個call名稱由Mock side_effect明確append，不依賴真Sheets。失敗的測試不得靠清全域正式cache修好。

成功情境應assert run_automation只呼叫1次且mock回填被執行；來源已完成情境assert run_automation與回填都未呼叫；三情境均assert本次job lock已釋放、沒有遺留running job。另以現有Streamlit AppTest驗證畫面正常與錯誤摘要只有一次，不能只靠AST harness判斷UI。

## T5：固定依賴與測試矩陣

- [ ] 將直接依賴保留於`requirements.in`；候選Playwright明確定為1.62.0（不是保證此版已在所有host驗收）。在乾淨Python3.12/Linux環境解析完整相依樹，產出包含所有直接／間接依賴精確版本與hash的requirements.txt。編譯工具版本也記錄於release evidence，禁止在正式App動態pip upgrade。
- [ ] Linux解析與安装後`pip check`、完整unit／AppTest過關。Cloud額外注入rich等平台套件單獨記錄，不宣稱是repo能完全控制的依賴。
- [ ] Python／browser／native資產任一改版均產生新profile digest；CI拒絕發布requirements內的`>=`／未固定應用依賴與browserprofile不相符。不要為修下載去任意降GoogleAPI或PDF套件。
- [ ] 以下每列新增具體測試，並保留既有全部業務測試：

| 情境 | 必須驗證 |
|---|---|
| 首次HTTP503→恢復 | 不cache失敗；有限重試／新請求可成功；訂單送出次數0直到環境與preflight過關 |
| 429／timeout／DNS暫時故障 | attempt與總deadline有界；不無限等待或堆積執行緒 |
| 403／404／unknown | 正確分類／不捏造狀態碼；不換未核驗來源 |
| 缺deb／hash錯／截斷／過大／解包越界 | fail closed、沒有ready、沒有browser、沒有Google寫入 |
| 舊marker但libdir空／lib檔被更動 | 不reuse，重建或明確失敗 |
| 同時多個使用者／重复按開始 | 初始化單次、業務lock有效，不重複產單 |
| profile改變／`/tmp`檔案消失 | 失效後重新準備；不沿用舊browser與env |
| installer rc0但browser不能啟動 | readiness不得成功，保留safe stage及exit類型 |
| 初始化前來源可製、完成後被取消／已製 | 最終preflight擋住，不自動製單 |
| browser在已產tracking後失敗 | 既有tracking／回填待確認狀態不丟失，不重送整批 |
| EMS／国際小包／ePacket／追加包裹 | 舊欄位、Weight略過、雙彈窗、HS及回填規則完全不變 |
| UI／OAuth | 未授權帳號仍拒絕；無技術欄位外露、沒有六筆重複錯誤、進度仍按真實結果 |

執行命令：

```bash
python -m pip check
python -m unittest discover -s tests -p test_browser_bootstrap.py -v
python -m unittest discover -s tests -p test_runtime_assets.py -v
python -m unittest discover -s tests -p test_postal_start_flow.py -v
python -m unittest discover -s tests -p test_postal_worker_runtime.py -v
python -m unittest discover -s tests -p test_safe_logging.py -v
python -m unittest discover -s tests -v
python -m compileall -q app.py bot scripts tests
git diff --check
```

Expected：全數PASS、exit0；記錄實際測試數、Python／OS與時間。不能將只有舊5+4測試通過當本計畫完成。

## T6：Linux冷啟動與部署驗收，不拿mock冒充實機

**Files:** `.github/workflows/runtime-validation.yml`、`scripts/check_browser_runtime.py`、`tests/test_runtime_cold_start.py`、release evidence。

- [ ] 在乾淨Linux/Python3.12、未預装GUI libs的隔離測試容器及與Cloud相容profile各執行一次；CI不是正式production，不掛正式secrets。容器基底與測試工具依digest固定，OS資料写入evidence。
- [ ] 冷啟動測試用新暫存根目錄，不刪真`/tmp`。從clone／完整requirements安装／61deb核驗／解包／browser安裝到空白頁probe完整走一次，不mocklibrary／browser。
- [ ] 再用另一個空暫存目錄重跑；刻意禁止Debianhost網路，原生資產仍能準備。Chromium官方下載另測「失敗→恢復」；這兩個測試分開，不能拿第二次warmcache掩蓋第一次cold的下載依赖。
- [ ] 第三組使用既有已驗cache作warm測試，確認無重複下載且profile／檔案驗證有效。注入安裝中斷，不能留下可被誤判ready的半成品。
- [ ] 成功執行`python scripts/check_browser_runtime.py`，輸出真browser version／revision／assetdigest，確認每次probe後沒有殘留browserprocess。沒有登入或製單。
- [ ] CI取消`continue-on-error`或吞例外綠燈做法；任何preparation／probe失敗都使job非零，輸出安全診斷。workflow只測試，不設定cron、keepalive、發布或production寫入。
- [ ] fresh-context reviewer閱讀diff與RED/GREEN、真Linux結果及安全邊界；P0/P1未清除不進G1。每個可獨立驗收的小變更commit到隔離branch，不自合main。

### G1開啟後的正式維護

- [ ] 確認無活躍job、保存前版code／asset／requirements profile及回復步驟；確認repo含全部資產，不能只push app.py。
- [ ] 域主審核合併及部署後，在批准維護時段冷重啟；記錄**實際進程**release digest／asset digest／Python／browserrevision，不只看「Updated app」。
- [ ] 在真正Community Cloud執行不製單probe；OS／glibc未觀察或不符合驗證profile直接擋住。不用Ubuntu CI代替Cloud結果。
- [ ] 重複一次乾淨重建／cold驗證及一次正常warm驗證；觀察初始化失敗復原；只清測試或本版自有runtime，不能清job／業務cache來造成功。
- [ ] 確認登入／跨境揀貨單／待製列表正常，開始前環境檢查可用；以read-only檢查來源與目標，不點真正製單。

### G2開啟後的實際製單驗收

- [ ] 重新取得當時待製清單與完成證據；不能照抄9/9的四筆或本日匿名六筆直接再製。記錄本次使用者核准的order+package範圍，避免其他同仁同時操作。
- [ ] 首次只做核准的最小批次；若順利，再做本次核准範圍內的剩餘包裹。不要為了涵蓋EMS／ePacket／国際小包而對沒有實際需求的訂單額外製單。
- [ ] 每個包裹核對：真進度、郵局tracking、PDF可開且內容／收件人／品項／寄送方式／Weight符合原規則、Drive檔案存在、目標Sheet收件人／注文番号／追跡番号與列數正確，刷新待製清單已排除完成單。
- [ ] 有error則依階段處理：尚未提交可在原因修正後重試環境；已提交或不確定先核對郵局／Drive／Sheet，不自動重送。沒有以上readback不宣稱「實單驗收完成」。

### 回復與發布後檢查

新版本不通過readiness就停止接受新製單，登入及資料讀取可保留；不要自動製單或切到未驗證runtime。回復必須是code+資產+依赖profile整組，且仍須啟動probe。舊`b27c114`有已知下載／cache缺陷，**不是保證可工作的回復選項**；若沒有已通過的整組版，維持不製單並清楚報告，不用盲目reboot循環。

不自動建立定時喚醒或監控。手冊要求每次平台重建／依赖改版重跑coldprobe；每30天由維護者檢查安全更新，在隔離環境測完才發布新profile。若要自動排程，另取得使用者授權。

## 4. B階段：完整環境固定的長期路線（G3關閉）

將**同一個**Streamlit App和server-side Playwright搬入一個固定映像，不必先重寫前端或拆微服務。官方Playwright映像／自建相容Linux映像需按實際tag存在性與digest核對，Python Playwright與內含browser配對；CI build時把browser與OS libs裝好，job執行時不再安裝。browser安裝與執行路徑必須一致，不能沿用目前Dockerfile的Python3.11加app強制`/tmp/ms-playwright`錯配。

G3前另交付並核准：

1. 實際可用免費host容量／休眠／流量／儲存限制；沒有已取得資源就不能承諾免費常駐。既有OCI其他系統不得擅自共用或改設定。
2. OS／arch選擇：amd64或arm64各自build與browser驗證，不能把現有amd64資產直接搬到ARM。
3. OAuth callback、company allowlist、secret注入、TLS／權限隔離及資料保存規範；GoogleSheet仍是業務核心，不新增未核准資料庫。
4. 單一活躍製單worker、任務排空後切換與中斷後完成證據核對；切換期間不得兩個站同時產單。
5. 空白頁probe、mock、真實最小批次、PDF與回填readback、完整回復演練，全部通過才切正式入口。

容器能減少環境漂移，不能消除郵局／Google／host中斷；需要可復原狀態和運維流程，而非「永遠不會再壞」的承諾。本階段只留下路線，不執行主機遷移。

## 5. 交付定義與下一位模型接手

只有下列狀態可標成「修復正式完成」：T0–T6、G1實機cold／warm、G2核准真實包裹的PDF+Drive+Sheet回讀全部有證據。否則分別報`LOCAL_TESTED`、`DEPLOYED_NOT_BUSINESS_VERIFIED`或`BLOCKED_WITH_EVIDENCE`，不可混稱完成。

本次執行模型為GPT-5.6 Luna Max Fast。後續接手讀取順序：本檔→EVIDENCE→HANDOFF；重讀最新授權及治理文件。G1發布流程已有使用者明確授權並正在推進；G2真實製單與G3主機遷移仍關閉。Linux冷啟動、review或Cloud probe的P0/P1尚未解決前，不可宣稱此案已正式修復或正式部署完成。
