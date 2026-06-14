# 📮 JP Post SaaS 部署指南

## 前置準備：Google Cloud Console 設定

### 1. 建立 OAuth 2.0 憑證
1. 前往 [Google Cloud Console](https://console.cloud.google.com/)
2. 建立專案（或選擇既有專案）
3. APIs & Services → Credentials → **Create Credentials → OAuth 2.0 Client ID**
4. Application type 選 **Web application**
5. **Authorized redirect URIs** 填入你的 Streamlit App URL：
   - 本地測試：`http://localhost:8501/`
   - Streamlit Cloud：`https://your-app.streamlit.app/`
   - HF Spaces：`https://your-space.hf.space/`
6. 複製 Client ID 與 Client Secret

### 2. 啟用必要 API
在 APIs & Services → Library 啟用：
- **Google Sheets API**
- **Google Drive API**
- **Google+ API**（或 People API）

### 3. 建立 Service Account（Sheets/Drive 存取）
1. APIs & Services → Credentials → **Create Credentials → Service Account**
2. 建立後，下載 JSON 金鑰（即 credentials.json）
3. **將此 Service Account 的 email 加入到：**
   - 來源 Google Sheet（編輯者權限）
   - 目標 Google Sheet（編輯者權限）
   - Google Drive 資料夾（編輯者權限）

---

## 部署選項 A：Streamlit Community Cloud（推薦・免費）

### 步驟
1. 將 `saas/` 資料夾推送至 GitHub（**不要包含 secrets.toml**）
2. 前往 [share.streamlit.io](https://share.streamlit.io/) 連結 GitHub
3. 設定：
   - Main file path: `app.py`
   - Branch: `main`
4. **App Settings → Secrets** 貼入以下內容（參考 `.streamlit/secrets.toml.template`）：

```toml
GOOGLE_CLIENT_ID = "xxx.apps.googleusercontent.com"
GOOGLE_CLIENT_SECRET = "GOCSPX-xxx"
OAUTH_REDIRECT_URI = "https://your-app.streamlit.app/"
JP_POST_USER = "your@email.com"
JP_POST_PASS = "your-password"
GEMINI_API_KEY = "AIza..."

[gcp_service_account]
type = "service_account"
project_id = "..."
# ... 其他欄位從 credentials.json 複製
```

### ⚠️ 注意
- Streamlit Cloud 免費方案記憶體 1GB，Playwright 執行時可能接近上限
- 若遇到記憶體問題，考慮改用 HF Spaces Docker

---

## 部署選項 B：Hugging Face Spaces（Docker・免費）

### 步驟
1. 在 [huggingface.co](https://huggingface.co/) 建立新 Space
2. SDK 選 **Docker**
3. 將 `saas/` 資料夾上傳（包含 `Dockerfile`）
4. **Settings → Repository secrets** 添加環境變數：
   - `GOOGLE_CLIENT_ID`
   - `GOOGLE_CLIENT_SECRET`
   - `OAUTH_REDIRECT_URI`（設為 `https://your-username-your-space.hf.space/`）
   - `JP_POST_USER`
   - `JP_POST_PASS`
   - `GEMINI_API_KEY`
   - `GOOGLE_APPLICATION_CREDENTIALS_JSON`（credentials.json 的全文內容）

### 注意：HF Spaces 使用環境變數替代 secrets.toml
在 `bot/sheets.py` 和 `bot/drive.py` 中，當 `st.secrets` 讀取失敗時，
已自動 fallback 到環境變數（`GOOGLE_APPLICATION_CREDENTIALS`）。
HF Spaces 可將 credentials.json 內容存為 `GCP_SA_JSON` 環境變數，
並在啟動時寫入檔案。

---

## 本地開發測試

```bash
# 1. 建立虛擬環境
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# 2. 安裝套件
pip install -r requirements.txt
playwright install chromium

# 3. 建立本地 secrets
cp .streamlit/secrets.toml.template .streamlit/secrets.toml
# 編輯 .streamlit/secrets.toml 填入實際值

# 4. 啟動
streamlit run app.py
```

---

## 白名單設定

若需允許非 @tkrjm.co.jp 帳號登入，編輯 `auth.py`：

```python
ALLOWED_WHITELIST: list[str] = [
    "partner@external.com",
    "consultant@another.co.jp",
]
```

---

## 架構說明

```
使用者瀏覽器
    ↓ HTTPS
Streamlit App（雲端）
    ↓ Google OAuth 驗證（限 @tkrjm.co.jp）
    ↓ 讀取 Google Sheets（待打單清單）
    ↓ 雙重過濾防重製（記憶體集合比對）
    ↓ 啟動背景執行緒
        ↓ Playwright Headless Chromium
            ↓ 日本郵政官網自動化
            ↓ 雙重 jQuery UI 彈窗防禦
            ↓ EU 訂單 → Gemini HS Code 預測
            ↓ PDF 封包攔截（不彈下載對話框）
            ↓ Google Drive 上傳
        ↓ 回填貨運單號至 Google Sheets
    ↓ 即時日誌串流回 Web UI
```
