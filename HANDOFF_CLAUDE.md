# JP Post SaaS Claude Handoff

## 2026-06-20 Latest Continuation Note

Use `CLAUDE.md` as the current first-read handoff file, and use `memory.md` for durable UI/data preferences. This file contains older but still useful auth and automation context.

Latest confirmed working commit before the 2026-06-20 documentation handoff:

```text
a849f1c feat: split command rows and recipient IDs
```

The current UI direction is Streamlit-native, single-column, and split into type-homogeneous rows:

- toolbar info row: text only
- toolbar operation row: widgets only
- order info row: text only
- order operation row: Name / TransType / optional PRC ID or PCCC / reset widgets

Do not return to forcing mixed custom text and Streamlit widgets into one perfectly aligned framed row; that caused repeated visual alignment regressions.

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

---

## 2026-06-17 Latest Claude Handoff Addendum

This section supersedes the older "Current Known Follow-Up Work" notes above for Japan Post automation.

### Current Production Status

Google login is working and the protected Streamlit UI can be entered. The active blocker is Japan Post automation after successful requests login.

The latest live run reached the create-label flow successfully:

```text
step=1 command=onlineS -> M010100.do
step=2 command=regist -> M060000.do
step=3 command=regist -> M060400.do
step=4 command=directInput -> M060400.do
```

After `directInput`, the returned response appears to be the recipient input stage (`M060505` / `addrToBean` indicators). The failure happens when the code tries to hand that HTML back to Playwright.

Latest failure signature:

```text
🔄 Playwright page 已關閉，重建頁面：set_content target closed
🔄 Chromium browser process 已關閉，重新啟動
🔄 Playwright page 已關閉，重建頁面：set_content target closed
🔄 Chromium browser process 已關閉，重新啟動
❌ requests 開啟打單頁例外：Locator.count: Target page, context or browser has been closed

File "/mount/src/japanpost-saas/bot/automation.py", line 752, in open_create_label_form_via_requests
    set_content_from_requests(r.text)
File "/mount/src/japanpost-saas/bot/automation.py", line 359, in set_content_from_requests
    page.locator("body").count()
```

Interpretation: this is not just one closed page. The entire Chromium browser process is being killed on Streamlit Cloud after Japan Post form HTML is injected. This still happens after:

- stripping source `<script>` tags,
- injecting only a small `submitCommand()` / `regist()` stub,
- reducing the HTML to only the recipient form,
- rebuilding the Playwright page/context,
- relaunching Chromium and rehydrating cookies.

### Important Recent Commits

Recent relevant commits on `main`:

```text
42c9bdd chore: log automation build id
8fcdd4d fix: inject minimal recipient form html
a98d881 fix: sanitize Japan Post html before Playwright injection
92e16f0 fix: rebuild closed Playwright page before html injection
8b9d97f fix: choose recipient direct input flow
4d3fda9 fix: submit selected sender in requests flow
9aef9c9 fix: preserve checked Struts radio values
```

Do not revert these casually. They document the path already tried and keep the requests flow moving to `M060400 -> directInput`.

### Current Automation Helpers

Read these first in `bot/automation.py`:

- `AUTOMATION_BUILD_ID`
- `_command_from_href()`
- `_set_value_assignments()`
- `_StrutsFormParser`
- `_summarize_submit_commands()`
- `_extract_preferred_submit_command()`
- `_choose_label_flow_command()`
- `_build_struts_submit()`
- `open_create_label_form_via_requests()`
- `set_content_from_requests()`

Current behavior:

- `regist()` is treated as the Struts command `regist`.
- checked radio/checkbox values are preserved.
- checked input `onclick="setValue('hiddenName', 'value')"` side effects are applied to request payloads.
- sender selection now correctly uses `regist` instead of `directInput`.
- recipient address-book page `M060400` now correctly chooses `directInput`.
- `AUTOMATION_BUILD_ID` is logged from both `app.py` and `run_automation()`.

### Deployment Freshness Check

Every new production run should show:

```text
🧭 automation build: 2026-06-17-8fcdd4d-no-page-title
```

If a log does not show this, or if it references old calls such as `page.title()`, verify Streamlit Cloud is deployed from GitHub `main` and force a redeploy before debugging automation logic.

### Recommended Next Development Direction

Stop trying to stabilize `page.set_content()` for Japan Post HTML on Streamlit Cloud. The evidence points to a cloud Chromium/runtime instability triggered by this legacy form HTML, not a normal selector bug.

Recommended next implementation:

1. Continue from the successful requests response after:

```text
step=4 command=directInput
```

2. Parse the returned recipient form with a requests/form parser instead of Playwright.

3. Build and POST the recipient payload directly with requests. Start with these fields already used by the Playwright fill code:

```text
addrToBean.couCode
addrToBean.nam
addrToBean.add1
addrToBean.add2
addrToBean.add3
addrToBean.pref
addrToBean.postal
addrToBean.tel
```

The existing Playwright selectors indicate the likely Struts field ids:

```text
#M060505_addrToBean_couCode
#M060505_addrToBean_nam
#M060505_addrToBean_add1
#M060505_addrToBean_add2
#M060505_addrToBean_add3
#M060505_addrToBean_pref
#M060505_addrToBean_postal
#M060505_addrToBean_tel
```

These ids usually map to names like:

```text
addrToBean.couCode
addrToBean.nam
addrToBean.add1
addrToBean.add2
addrToBean.add3
addrToBean.pref
addrToBean.postal
addrToBean.tel
```

Confirm against the returned HTML before posting.

4. Submit one Japan Post step at a time with requests:

- recipient form submit,
- shipping method selection,
- contents/items entry,
- weight/confirmation,
- register shipment,
- PDF download.

5. Use Playwright again only if a later step truly requires browser rendering or a download event. Prefer direct requests download for PDF if a URL/form action can be extracted.

### Why PAexample Worked But SaaS Is Harder

PAexample runs local GUI automation in a full desktop browser. Japan Post's old Struts pages execute their legacy JavaScript naturally in that environment.

The SaaS version runs inside Streamlit Cloud's limited headless Chromium environment. The live logs now show that this environment can terminate the entire Chromium process when Japan Post form HTML is injected, even after aggressive minimization. Therefore, "make Playwright click like PAexample" is likely the wrong path for the current free-cloud constraint.

### MCP Notes

Chrome MCP was tested. In this session it could open/list tabs and expose `playwright.evaluate` / `locator`, but navigation attempts were blocked by the MCP evaluate sandbox:

- assigning `location.href` failed because `href` was getter-only,
- `window.location.assign()` was unavailable,
- assigning `document.location` failed,
- setting `document.body.innerHTML` failed,
- GUI `Ctrl+L`/typing did not navigate the blank tab.

So MCP can be retried later, but do not assume it can currently drive the production app end-to-end from this Codex session.

### Suggested Tests For Next Fix

Add focused tests to `tests/test_automation_helpers.py` before changing production flow:

- parse a recipient `M060505` form and preserve hidden fields,
- map row data into `addrToBean.*` payload keys,
- apply selected country code fallback from `COUNTRY_CODE_MAP`,
- add `method:regist` or the actual next command expected by the recipient form,
- ensure no Playwright call is required for the recipient form submit path.
