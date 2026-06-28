# Claude Session Notes — JPPost SaaS
> 每次 Session 結束前更新。新 Session 開始時**必讀**。
> 最後更新：2026-06-28（Production handoff）

---

## 新 Session 必讀順序

新的 Claude Session 開始時，必須先讀完以下文件與程式，再回覆或修改。不要直接猜測目前狀態。

1. `CLAUDE_SESSION_NOTES.md`
2. `docs/picking-label/current-status.md`，如果存在
3. `docs/picking-label/production-notes.md`，如果存在
4. `saas/app.py`
5. `saas/features/picking_labels.py`
6. `saas/bot/picking_labels.py`
7. `saas/bot/picking_pdf.py`
8. `saas/bot/drive.py`
9. `saas/bot/sheets.py`

實際主要程式路徑以 `saas/` 為準。本地根目錄可能有舊檔、測試資料、`tmp/` checkout、`PAexample/`、`vchunk_*.txt`、舊 logs；不要把那些當作 production 主程式來源。

---

## 2026-06-28 Production Handoff Summary

### Production 狀態

- App 名稱：`Cross-Border製單系統`
- Production URL：`https://jppost.streamlit.app/`
- GitHub repo：`Vincent-Lu-TRS/JapanPost-SaaS`
- 目前最新 production 修正 commit：`ba549e24094bc8f8380da9a4eb4375cd3a46928a`
- Cross-Border final polish commit：`2322603d81a631b08b08d438c9c1283706f8141a`

目前頁籤順序：

1. `跨境揀貨單`
2. `郵局待打單`
3. `使用說明`
4. `讀取診斷`

`跨境揀貨單` 是預設第一頁。

### Cross-Border 揀貨單定稿狀態

- PDF 尺寸固定：`100mm × 150mm`
- 使用固定 10-row grid layout。
- 少於 10 個商品時保留空白格線。
- 超過 10 個商品時每 10 個商品分頁。
- 不再使用 sparse / medium / dense adaptive layout。
- PDF layout 已接受，不要重新設計。
- QR、Header、商品欄、數量欄、空白格線不要大改。
- `発送期限` 顯示到分鐘，例如 `2026/07/04 00:00`。
- `06/27\n着予定` 已調整為盡量顯示 `06/27着予定`。
- `注文番号` 區塊已微幅上移。
- 字體已加入 Noto CJK / Meiryo fallback 邏輯。

### Cross-Border 來源表解析規則

來源工作表：`南巽出貨Label`

此表有重複欄名，不能用第一次出現的欄名抓資料。Cross-Border 揀貨單必須使用 anchored schema：

- K 欄 = `訂單狀態`
- L 欄 = `製單後勾選`
- M 欄 = `注文日`
- N 欄 = `訂單來源`
- O 欄 = `注文番号`
- P 欄 = `國際物流方式`
- Q 欄開始 = 商品群組 1～10
- 來源表已擴充到 BN 欄，可支援 10 組商品

重要：

- A:C 和 E 欄有相似欄名，但 Cross-Border 不可使用它們。
- `注文番号` 必須使用 O 欄。
- writeback revalidation 也必須比對 O 欄，不可比對 C 欄。

### Cross-Border 候選篩選條件

候選列必須同時符合：

1. K `訂單狀態` = `可出貨`
2. L `製單後勾選` = NOT_DONE
3. P `國際物流方式` 包含以下任一文字：`郵便局`、`佐川`、`MLS`、`SLS`

L 欄是 Google Sheets checkbox / data validation 自訂值：

- 勾選值：`已製單`
- 未勾選值：`未製單`

因此：

- `未製單` = NOT_DONE / 可候選
- `已製單` = DONE / 排除
- 也需相容 boolean False / TRUE / FALSE / blank 等舊資料形式

正式產生成功後：

- 先上傳 PDF 到雲端資料夾。
- 上傳成功後，才寫回來源表 L 欄。
- 寫回值：`已製單`。
- 不要寫 boolean TRUE。
- Drive upload 失敗時不可寫 L 欄。
- reload / selection 不可寫 L 欄。

### Cross-Border 檔名規則

揀貨標籤 PDF 命名格式：`YYMMDD-N揀貨標籤.pdf`

例如：

- `260628-1揀貨標籤.pdf`
- `260628-2揀貨標籤.pdf`

規則：

- 上傳前檢查目標 Drive folder。
- 找出同日期最高序號。
- 新檔案使用最高序號 + 1。
- 不可覆蓋既有檔案。
- re-check collision 時自動進下一個序號。
- timestamp fallback 只作最後保險。

### Cross-Border UI 現況

主頁只保留日常操作資訊：

- 列印設定：`列印設定：PDF檔尺寸為 100mm × 150mm，請使用對應Label大小輸出。`
- 待製單訂單
- 最後讀取
- 操作按鈕：
  1. `產生揀貨單`
  2. `重新讀取`
  3. `全選`
  4. `取消全選`
- 表格顯示欄位：
  - 選取
  - 注文番号
  - 注文日
  - 訂單來源
  - 國際物流方式
  - 発送期限

不要在 Cross-Border 主頁顯示：

- QR/debug summary
- source row number
- 商品數
- PDF頁數
- 提醒
- 可能原因
- parser details
- 系統限制

這些都放在 `讀取診斷` → `跨境揀貨單`。

### 郵局待打單狀態

最近曾發生 regression：

- UI cleanup 把 `開始製單` 改成兩段式確認。
- 導致原本直接啟動 `_start_job(...)` 的流程被切斷。

已修復：

- commit：`ba549e24094bc8f8380da9a4eb4375cd3a46928a`
- `開始製單` 已恢復直接啟動既有郵局製單 workflow。
- 視覺順序：
  1. `開始製單`
  2. `重新讀取`

後續不要再把郵局待打單改成兩段式確認，除非使用者明確要求。

### 非阻塞技術債

Streamlit log 仍可能出現：

`Please replace use_container_width with width`

這是 deprecation warning，不是功能錯誤。可另開任務全 repo 搜尋替換：

- `use_container_width=True` → `width="stretch"`
- `use_container_width=False` → `width="content"`

這只能作技術債清理，不可改業務邏輯。

### 明確禁止重新推翻

- 不要重設計 Cross-Border PDF layout。
- 不要改掉 fixed 10-row grid。
- 不要改回 sparse / medium / dense adaptive layout。
- 不要用 A:C / E 欄作為 Cross-Border 揀貨單資料來源。
- 不要把 L 欄寫成 TRUE；production sheet 使用 `已製單`。
- 不要在 Drive upload 前寫回 L 欄。
- 不要改壞郵局待打單的 `_start_job(...)` 直接啟動流程。
- 不要把 debug 資訊放回 Cross-Border 主操作頁。
- 不要把憑證 JSON 內容寫入任何 markdown 或回覆。

### 敏感憑證提醒

根目錄可見 service account JSON，例如 `japanpost-488013-*.json`。此類檔案是敏感憑證；可以提醒需要妥善保管，但不可複製、引用、貼出 private key 或任何憑證內容，也不可將憑證內容寫入 markdown。

---

## 🔴 當前未解決問題（下個 Session 優先處理）

### A. 全流程 E2E 測試尚未完成
**狀態**：登入已修通（Session 4），但「登入後」的 Playwright 自動化操作尚未驗證
**具體步驟**：登入後 → 點擊 "Create New Labels" → 填寫收件人表單（M060505.do）→ 填貨物資訊 → Register Shipment → 攔截 PDF → 擷取貨運單號
**風險**：這些步驟每個都有 `page.wait_for_timeout()` 呼叫，任何一個都可能在容器記憶體不足時 crash
**待確認**：跑一次完整製單，觀察是否在任何步驟出現 TargetClosedError

### B. 如果 Playwright 仍在表單頁面 crash（最可能的下一個問題）
**方向**：考慮繼續擴大 requests-based 自動化（目前只有登入用 requests）
**具體**：M060505.do 表單也可用 requests POST 提交，不需 Playwright 互動
**參考**：common.js `submitCommand()` 邏輯已知，任何步驟都可用 `method:{step_name}=""` 提交

---

## ✅ 已解決問題清單

### 5. requests 登入失敗（command=login 欄位錯誤）（Session 4，commit `461fd24`）

**現象**：POST 後回 200，URL 仍是 M010000.do，日誌「⚠️ 登入狀態不明」
**根本原因**：`submitCommand('login')` 的 JS 邏輯（common.js 已確認）：
```javascript
document.forms[0].elements['command'].name = 'method:' + command;
document.forms[0].submit();
```
把欄位名從 `command` 改成 `method:login`，值為空字串。POST body 應是 `method:login=`。

**修法**：`"command": "login"` → `"method:login": ""`

**附錄：登入頁 form fields（Chrome MCP DOM 確認）**
```
command        = ""   (hidden) → 提交時被改名為 method:login
csrfToken      = ""   (hidden, 永遠空白，無需處理)
request_locale = "en" (hidden)
localeSel      = "en" (select)
loginBean.id   = ""   (#M010000_loginBean_id)
loginBean.pw   = ""   (#M010000_loginBean_pw)
mailS          = "on" (checkbox, 可忽略)
```

---

### 6. requests 登入後 Playwright crash（Session 4，commit `8178f09`）

**現象**：requests 登入成功、cookies 注入後，`page.wait_for_timeout(2000)` crash
**根本原因**：Struts forward 行為——伺服器登入成功後不 redirect，直接 forward，`r2.url` 仍是 `M010000.do`。程式用 `post_url` 當 `dest`，等於再次載入會 crash 的登入頁。

**Struts Forward 重要概念**：
- 登入成功 → URL 不變（仍 M010000.do），body 含 "Log out"（已 Chrome MCP 確認：未登入頁面不含此字串）
- success check `"Log out" in r2.text` 是**正確**的

**修法**：requests 成功後直接 `_login_ok = True`，不再讓 Playwright 導航任何 Japan Post 頁

---

### 4. TargetClosedError at automation.py（Session 3，commit `2c167fe`）
**現象**：`page.wait_for_load_state("domcontentloaded")` OOM crash
**修法**：改 `wait_for_timeout`、加 route blocking、加 `--no-zygote`

### 3. packages.txt 相依套件問題（已修）
- `libasound2` 需加入；`libglib2.0-0` 需移除（Trixie 改名 `libglib2.0-0t64`）

### 2. Google OAuth 登入按鈕（Session 2，commit `bbe788c`）
- 最終方案：`st.link_button()`（已棄用 st.components.v1.html）
- 陷阱：`json.dumps(url)` 輸出含雙引號，不能放進 HTML 屬性

### 1. UnicodeDecodeError in app.py
- GitHub Contents API PUT 會腐化 CJK bytes，改用 git push

---

## ⚠️ 極重要：Edit tool 截斷陷阱（已踩坑 N 次，永遠不要再用）

**正確改檔流程**：
```bash
# Step 1: clone（或確認 /tmp/jppost-complete 完整）
cd /tmp && rm -rf jppost-complete
git clone https://{TOKEN}@github.com/Vincent-Lu-TRS/JapanPost-SaaS.git jppost-complete

# Step 2: 驗證
python3 -c "data=open('/tmp/jppost-complete/TARGET.py','rb').read(); data.decode('utf-8'); print(f'OK size={len(data)} last={repr(data[-60:])}')"

# Step 3: Python 字串替換（不用 Edit tool）
python3 << 'EOF'
data = open('/tmp/jppost-complete/TARGET.py', 'r', encoding='utf-8').read()
OLD = """...old..."""
NEW = """...new..."""
assert OLD in data, "OLD not found!"
data = data.replace(OLD, NEW, 1)
open('/tmp/jppost-complete/TARGET.py', 'w', encoding='utf-8').write(data)
print(f"Done, size={len(open('/tmp/jppost-complete/TARGET.py','rb').read())}")
EOF

# Step 4: push
cd /tmp/jppost-complete
git config user.email "admin@tkrjm.co.jp" && git config user.name "Claude Fix"
git add TARGET.py && git commit -m "fix: ..."
git push https://{TOKEN}@github.com/Vincent-Lu-TRS/JapanPost-SaaS.git main

# Step 5: 同步回 mount
cp /tmp/jppost-complete/TARGET.py /sessions/focused-awesome-feynman/mnt/jppost/saas/TARGET.py
```

**黃金規則**：永遠用 bash 確認 last bytes | 永遠用 bash Python 替換 | 不信 Read tool

**完整檔案 size 參考**：app.py ≈ 11279 bytes | automation.py ≈ 35309 bytes（commit `8178f09`）

---

## 📁 專案重要資訊

### 倉庫
```
GitHub: Vincent-Lu-TRS/JapanPost-SaaS（private）
Streamlit: https://jppost.streamlit.app/
```

### 路徑對應
| bash mount | GitHub repo |
|---|---|
| `mnt/jppost/saas/app.py` | `app.py` |
| `mnt/jppost/saas/auth.py` | `auth.py` |
| `mnt/jppost/saas/bot/automation.py` | `bot/automation.py` |
| `mnt/jppost/saas/bot/sheets.py` | `bot/sheets.py` |
| `mnt/jppost/saas/bot/gemini_helper.py` | `bot/gemini_helper.py` |
| `mnt/jppost/saas/bot/drive.py` | `bot/drive.py` |

bash mount 根路徑：`/sessions/focused-awesome-feynman/mnt/jppost/saas/`

### Token
每次 Session 需用戶提供新 `ghp_xxxxxxxxxxxx`（**不可 commit**）

### 安全限制（永遠不可違反）
- `.streamlit/secrets.toml` 絕對不可 commit（gitignored）
- `*.json` 絕對不可 commit（gitignored）
- 只允許 `@tkrjm.co.jp` 或白名單人員登入

---

## 🔧 技術雜項

### Playwright 穩定啟動參數（容器必加）
```python
args=[
    "--no-sandbox", "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--no-zygote",   # 容器 seccomp 必加，移除會 crash
    "--disable-gpu", "--disable-software-rasterizer",
    "--disable-extensions", "--disable-background-networking",
    "--disable-default-apps", "--mute-audio",
    "--disable-features=site-per-process",
    "--blink-settings=imagesEnabled=false",
    "--disable-background-timer-throttling",
    "--disable-hang-monitor", "--disable-ipc-flooding-protection",
]
```

### Japan Post 網站技術細節
- URL：`https://www.int-mypage.post.japanpost.jp/mypage/`（`www.` 必須有）
- 強制英文：加 `?request_locale=en`
- 框架：Apache Struts（forward 不 redirect → POST 後 URL 不變是正常現象）
- JS：jQuery 1.4.2 + jQuery UI 1.8.2（202KB，容器容易 OOM）
- 表單提交邏輯：`submitCommand(name)` → 把 `command` 欄位改名為 `method:{name}` 再 submit
- CSRF token：HTML 中永遠空白，POST 時可不帶或帶空值

### Google Cloud Console OAuth
Authorized redirect URIs:
- `http://localhost:8501/`
- `https://japanpost-sa-8nsrgyfnfdzjkdgdteaagp.streamlit.app/`
- `https://jppost.streamlit.app/`
