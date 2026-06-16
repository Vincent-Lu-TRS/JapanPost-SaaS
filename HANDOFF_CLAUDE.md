# JP Post SaaS Claude Handoff

Last updated: 2026-06-17

## Current Project Direction

This project converts the local Japan Post label automation scripts into a lightweight SaaS:

- UI: Streamlit Cloud app at `https://jppost.streamlit.app/`
- Repository: `https://github.com/Vincent-Lu-TRS/JapanPost-SaaS`
- Runtime model: users operate only through the web UI; Playwright runs server-side in headless mode.
- Security model: Google login is required and limited to `@tkrjm.co.jp` or explicit whitelist entries in `auth.py`.
- Business workflow: read eligible orders from Google Sheets, prevent duplicate label creation, automate Japan Post My Page, predict HS codes for EU orders, download PDFs, upload PDFs to Google Drive, and write results back to Sheets.

## Recent Important Fixes

### 1. Google OAuth new-tab problem on Streamlit Cloud

The previous custom OAuth link could not reliably complete login in the original page. The root cause is Streamlit Cloud's outer iframe sandbox. The iframe allows popups but does not allow top-level navigation by user activation, so links inside the app may open or complete in a separate Google auth window.

The app now supports Streamlit native OIDC login via `st.login()`:

- Commit: `14b550f fix: use Streamlit native OIDC login`
- Files changed: `auth.py`, `app.py`, `requirements.txt`, `.streamlit/secrets.toml.template`, `tests/test_auth.py`
- `st.user` is mirrored into the existing session-state contract:
  - `st.session_state.authenticated`
  - `st.session_state.user_email`
  - `st.session_state.user_name`
  - `st.session_state.user_picture`
- Legacy custom OAuth remains as fallback when `[auth]` secrets are missing.

To fully activate this fix, Streamlit Cloud Secrets must include the `[auth]` block described below.

### 2. Streamlit auth cookie persistence

`requirements.txt` pins:

```txt
streamlit[auth]==1.56.0
```

Reason: native `st.login()` provides an identity cookie intended for roughly 30 days. Avoid upgrading Streamlit casually until persistence is re-tested, because newer versions have had auth-cookie persistence regressions.

### 3. Japan Post requests login and Playwright crash avoidance

The automation uses a `requests` login path first, extracts Japan Post cookies, injects them into Playwright, and skips Playwright login navigation. This avoids crashes around Japan Post's heavy legacy pages.

Important implementation notes:

- Japan Post base URL should include `www`: `https://www.int-mypage.post.japanpost.jp/mypage/`
- The login form is Apache Struts style. `submitCommand('login')` renames the hidden `command` field to `method:login` before submit.
- A successful login may return body content for `M010000.do` without changing the final URL in a simple redirect-like way. Do not rely only on URL.
- After requests login succeeds, do not navigate Playwright back through the login page unless necessary.

## Required Streamlit Cloud Secrets

Keep the existing flat legacy keys because parts of the code still read them:

```toml
GOOGLE_CLIENT_ID = "existing-google-oauth-client-id.apps.googleusercontent.com"
GOOGLE_CLIENT_SECRET = "existing-google-oauth-client-secret"
OAUTH_REDIRECT_URI = "https://jppost.streamlit.app/"
JP_POST_USER = "existing-japan-post-login-email"
JP_POST_PASS = "existing-japan-post-password"
GEMINI_API_KEY = "existing-gemini-api-key"
```

Add this native Streamlit OIDC section:

```toml
[auth]
redirect_uri = "https://jppost.streamlit.app/oauth2callback"
cookie_secret = "replace-with-a-long-random-secret-at-least-32-chars"
client_id = "same-value-as-GOOGLE_CLIENT_ID"
client_secret = "same-value-as-GOOGLE_CLIENT_SECRET"
server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"
```

Then keep the existing `[gcp_service_account]` block unchanged.

Do not commit real secrets, service-account JSON files, or `.streamlit/secrets.toml`.

## Required Google Cloud OAuth Redirect URIs

The OAuth client must include:

```text
https://jppost.streamlit.app/oauth2callback
```

The old legacy redirect can remain for fallback:

```text
https://jppost.streamlit.app/
```

For local development, include:

```text
http://localhost:8501/oauth2callback
http://localhost:8501/
```

## How To Verify The Login Fix

1. Update Streamlit Cloud Secrets with `[auth]`.
2. Add the `/oauth2callback` URI in Google Cloud Console.
3. Redeploy the Streamlit app.
4. Open `https://jppost.streamlit.app/`.
5. Expected: the app should no longer show:

```text
目前使用舊版 OAuth 入口；若要避免新分頁，請設定 Streamlit 原生 [auth]。
```

6. Click the Google login button.
7. Expected: login returns to the same Streamlit app flow via `/oauth2callback`, not a separate permanent auth window.
8. After login, refresh/reopen the app. Expected: the user remains logged in without repeating Google auth, subject to Streamlit's identity-cookie behavior.

## Current Known Follow-Up Work

### A. Verify live native login after secrets are updated

The code is already deployed, but as of the last check the live app still showed the fallback warning. That means `[auth]` was not yet present in Streamlit Cloud Secrets.

### B. Continue debugging Japan Post automation after login

Recent live log example:

```text
requests 登入成功，Cookies 已就位，略過 Playwright 驗證
關鍵點擊失敗 [main_menu_create]:
waiting for locator("img[alt='Create New Labels'], a:has-text('Create New Labels')")
```

Likely next step: inspect the real post-login page with Playwright/Chrome and update selectors/navigation around the main menu and create-label entry. Be careful not to reintroduce heavy navigation that causes Chromium crashes.

### C. Maintain duplicate-prevention behavior

The project requirement includes "即時雙重過濾防重製邏輯". Before modifying Sheets or order selection logic, re-read `SaaS_Requirements.md` and confirm:

- Source sheet filtering
- Target sheet/tracking-number checks
- In-memory processed set during the current run
- Failure/retry behavior

### D. Keep Playwright cloud constraints in mind

Streamlit Cloud is memory-limited. Existing Chromium launch flags are intentional:

- `--no-sandbox`
- `--disable-dev-shm-usage`
- `--no-zygote`
- `--disable-gpu`
- image disabling and other background throttling flags

Avoid loading unnecessary Japan Post pages, images, or long waits.

## Useful Commands

Run tests:

```powershell
python -m unittest discover -s tests
```

Syntax check:

```powershell
python -c "import ast, pathlib; [ast.parse(pathlib.Path(p).read_text(encoding='utf-8')) for p in ['app.py','auth.py','bot/automation.py','tests/test_auth.py','tests/test_automation_helpers.py']]; print('syntax ok')"
```

Deploy:

```powershell
git status --short
git add .
git commit -m "fix: concise description"
git push
```

## Files To Read First

- `SaaS_Requirements.md` in the user's project folder for business rules.
- `auth.py` for Google auth, whitelist, custom cookie fallback, and native OIDC state restore.
- `app.py` for Streamlit UI and job orchestration.
- `bot/automation.py` for Japan Post Playwright/requests automation.
- `bot/sheets.py` for Google Sheets filtering and writeback.
- `DEPLOY_GUIDE.md` for deployment and secrets.

## Non-Negotiables

- Do not commit secrets.
- Do not commit `.streamlit/secrets.toml`.
- Do not commit service-account JSON files.
- Keep login restricted to `@tkrjm.co.jp` or explicit whitelist.
- Keep Playwright server-side/headless for SaaS users.
- Prefer focused fixes over broad refactors; this app is constrained by Streamlit Cloud memory and Japan Post's legacy pages.
