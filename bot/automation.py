"""
日本郵政自動化打單核心模組（Server-side Headless Playwright）
完整繼承 pa_playwright.py 的防禦機制：
- 雙重 jQuery UI 彈窗防禦
- 歷史資料對話框強制重置
- ePacket / PostalParcel_Air 精確分流
- EU 訂單 Gemini HS Code 預測注入
- PDF 封包攔截（不依賴下載對話框）
- 標準化命名 + Google Drive 即時上傳
"""
import os
import re
import time
import logging
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin
from datetime import date
import pandas as pd

from .drive import upload_pdf
from .gemini_helper import predict_hs_code

# ── 日本郵政登入憑證 ────────────────────────────────────
def _get_jp_post_creds() -> tuple[str, str]:
    import streamlit as st
    try:
        return st.secrets["JP_POST_USER"], st.secrets["JP_POST_PASS"]
    except Exception:
        return (
            os.environ.get("JP_POST_USER", ""),
            os.environ.get("JP_POST_PASS", ""),
        )


# ── 工具函數 ──────────────────────────────────────────
def _clean(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return str(v).strip()


def _sanitize_filename(s: str) -> str:
    return re.sub(r'[\\/*?:"<>|\r\n]', "_", s).strip()


def _get_excel_val(row: pd.Series, keys: list[str]) -> str:
    for k in keys:
        if k in row.index:
            v = _clean(row[k])
            if v:
                return v
    return ""


def _with_base_href(html: str, base_url: str) -> str:
    """Ensure response HTML resolves relative Japan Post URLs inside Playwright."""
    if re.search(r"<base\s", html, flags=re.IGNORECASE):
        return html
    base_tag = f'<base href="{base_url}">'
    if re.search(r"<head[^>]*>", html, flags=re.IGNORECASE):
        return re.sub(r"(<head[^>]*>)", r"\1" + base_tag, html, count=1, flags=re.IGNORECASE)
    return base_tag + html


# ── 主要自動化流程 ────────────────────────────────────
def _html_for_playwright_form(html: str) -> str:
    """Strip legacy site scripts but keep enough Struts helpers for button onclicks."""
    sanitized = re.sub(
        r"<script\b[^>]*>.*?</script\s*>",
        "",
        html or "",
        flags=re.IGNORECASE | re.DOTALL,
    )
    submit_stub = """
<script>
function setValue(name, value) {
  var el = document.getElementsByName(name)[0] || document.getElementById(name);
  if (el) el.value = value;
}
function submitCommand(command) {
  var form = document.forms[0];
  if (!form) return;
  var input = document.createElement("input");
  input.type = "hidden";
  input.name = "method:" + command;
  input.value = "";
  form.appendChild(input);
  form.submit();
}
function regist() { submitCommand("regist"); }
</script>
"""
    if re.search(r"</head\s*>", sanitized, flags=re.IGNORECASE):
        return re.sub(r"</head\s*>", submit_stub + r"</head>", sanitized, count=1, flags=re.IGNORECASE)
    return submit_stub + sanitized


def _set_value_assignments(script: str) -> dict[str, str]:
    assignments: dict[str, str] = {}
    for name, value in re.findall(
        r"setValue\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]*)['\"]\s*\)",
        script or "",
    ):
        assignments[name] = value
    return assignments


class _StrutsFormParser(HTMLParser):
    def __init__(self, label: str):
        super().__init__(convert_charrefs=True)
        self.label = label.lower()
        self.in_first_form = False
        self.seen_form = False
        self.form_action = ""
        self.fields: dict[str, str] = {}
        self.href_stack: list[str] = []
        self.command = ""

    def handle_starttag(self, tag, attrs):
        attrs_d = {k.lower(): (v or "") for k, v in attrs}
        tag = tag.lower()
        if tag == "form" and not self.seen_form:
            self.seen_form = True
            self.in_first_form = True
            self.form_action = attrs_d.get("action", "")
        elif tag == "a":
            self.href_stack.append(attrs_d.get("href", ""))
        elif tag == "img" and self.href_stack:
            alt = attrs_d.get("alt", "").lower()
            if self.label and self.label in alt:
                self.command = self.command or _command_from_href(self.href_stack[-1])
        elif self.in_first_form and tag == "input":
            name = attrs_d.get("name", "")
            if name:
                input_type = attrs_d.get("type", "").lower()
                if input_type in ("radio", "checkbox"):
                    if "checked" in attrs_d:
                        self.fields[name] = attrs_d.get("value", "")
                        self.fields.update(_set_value_assignments(attrs_d.get("onclick", "")))
                    elif name not in self.fields:
                        self.fields[name] = ""
                else:
                    self.fields[name] = attrs_d.get("value", "")
            label_value = attrs_d.get("value", "").lower()
            if self.label and self.label in label_value:
                self.command = self.command or _command_from_href(attrs_d.get("onclick", ""))
        elif self.in_first_form and tag == "select":
            name = attrs_d.get("name", "")
            if name and name not in self.fields:
                self.fields[name] = attrs_d.get("value", "")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "form" and self.in_first_form:
            self.in_first_form = False
        elif tag == "a" and self.href_stack:
            self.href_stack.pop()

    def handle_data(self, data):
        if self.label and self.href_stack and self.label in data.lower():
            self.command = self.command or _command_from_href(self.href_stack[-1])


def _command_from_href(href: str) -> str:
    match = re.search(r"submitCommand\(['\"]([^'\"]+)['\"]\)", href or "")
    if match:
        return match.group(1)
    if re.search(r"\bregist\(\s*\)", href or ""):
        return "regist"
    return ""


def _extract_submit_command_for_label(html: str, label: str) -> str:
    parser = _StrutsFormParser(label)
    parser.feed(html or "")
    return parser.command


def _summarize_submit_commands(html: str) -> str:
    commands: list[str] = []
    for command in re.findall(r"submitCommand\(['\"]([^'\"]+)['\"]\)", html or ""):
        if command not in commands:
            commands.append(command)
    if re.search(r"\bregist\(\s*\)", html or "") and "regist" not in commands:
        commands.append("regist")
    return ", ".join(commands[:12])


def _extract_preferred_submit_command(html: str, preferred: list[str]) -> str:
    available = re.findall(r"submitCommand\(['\"]([^'\"]+)['\"]\)", html or "")
    if re.search(r"\bregist\(\s*\)", html or ""):
        available.append("regist")
    for command in preferred:
        if command in available:
            return command
    return ""


def _choose_label_flow_command(html: str, current_url: str) -> str:
    if "M060400" in (current_url or ""):
        preferred = ["directInput", "add", "regist"]
    elif "M060105" in (current_url or ""):
        preferred = ["addrSet", "regist", "directInput"]
    else:
        preferred = ["addrSet", "regist", "directInput"]
    return _extract_preferred_submit_command(html, preferred)


def _build_struts_submit(html: str, command: str, base_url: str) -> tuple[str, dict[str, str]]:
    parser = _StrutsFormParser("")
    parser.feed(html or "")
    action = urljoin(base_url, parser.form_action or "")
    payload = {
        name: value
        for name, value in parser.fields.items()
        if name != "command"
    }
    payload[f"method:{command}"] = ""
    return action, payload


def run_automation(
    df: pd.DataFrame,
    max_rows: int | None = None,
    log_cb=None,
    headless: bool = True,
) -> list[dict]:
    """
    執行日本郵政自動化打單。

    Parameters:
        df        : 待打單 DataFrame（來自 sheets.get_pending_orders）
        max_rows  : 最多處理幾筆（None = 全部）
        log_cb    : 進度回呼函數 (str -> None)
        headless  : 是否以 headless 模式執行（生產環境固定 True）

    Returns:
        成功結果清單，每筆為 dict {name, order_id, tracking, country_raw, date}
    """
    def _log(msg: str):
        if log_cb:
            log_cb(msg)
        logging.info(msg)

    from playwright.sync_api import sync_playwright

    rows = df if max_rows is None else df.head(max_rows)
    results: list[dict] = []
    user, pwd = _get_jp_post_creds()
    pw_cookies = []

    if not user or not pwd:
        _log("❌ 未設定 JP_POST_USER / JP_POST_PASS，無法登入日本郵政")
        return results

    with sync_playwright() as p:
        chromium_args = [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--no-zygote",          # 容器環境必加：停用 zygote fork，避免 seccomp 限制殺掉進程
                "--disable-gpu",
                "--disable-software-rasterizer",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-default-apps",
                "--mute-audio",
                "--disable-features=site-per-process",
                "--blink-settings=imagesEnabled=false",
                "--disable-background-timer-throttling",
                "--disable-hang-monitor",
                "--disable-ipc-flooding-protection",
            ]

        def launch_browser():
            return p.chromium.launch(headless=headless, args=chromium_args)

        def new_context_with_cookies():
            new_context = browser.new_context(accept_downloads=True, ignore_https_errors=True)
            if pw_cookies:
                new_context.add_cookies(pw_cookies)
            return new_context

        browser = launch_browser()
        context = new_context_with_cookies()
        page = context.new_page()
        # 以 resource_type 攔截非必要資源（比副檔名更全面），大幅降低 Chromium 記憶體
        # stylesheet/image/font/media 全擋，保留 document/script/xhr/fetch（登入表單需要）
        def _abort_heavy(route):
            try:
                if route.request.resource_type in (
                    "image", "stylesheet", "font", "media", "ping", "eventsource", "other"
                ):
                    route.abort()
                else:
                    route.continue_()
            except Exception:
                pass  # route 可能已被解析
        page.route("**/*", _abort_heavy)

        def reset_playwright_page(reason: str):
            nonlocal browser, context, page
            _log(f"🔄 Playwright page 已關閉，重建頁面：{reason}")
            try:
                if not page.is_closed():
                    page.close()
            except Exception:
                pass
            try:
                page = context.new_page()
            except Exception:
                try:
                    context = new_context_with_cookies()
                    page = context.new_page()
                except Exception:
                    _log("🔄 Chromium browser process 已關閉，重新啟動")
                    browser = launch_browser()
                    context = new_context_with_cookies()
                    page = context.new_page()
            page.route("**/*", _abort_heavy)
            return page

        def ensure_playwright_page(reason: str):
            try:
                if page.is_closed():
                    reset_playwright_page(reason)
            except Exception:
                reset_playwright_page(reason)
            return page

        def set_content_from_requests(html: str):
            content = _with_base_href(
                _html_for_playwright_form(html),
                "https://www.int-mypage.post.japanpost.jp/mypage/",
            )
            last_exc = None
            for attempt in range(2):
                ensure_playwright_page(f"set_content attempt {attempt + 1}")
                try:
                    page.set_content(
                        content,
                        wait_until="domcontentloaded",
                        timeout=15000,
                    )
                    return
                except Exception as e:
                    last_exc = e
                    if "Target page, context or browser has been closed" not in str(e):
                        raise
                    reset_playwright_page("set_content target closed")
            raise last_exc

        # ── 診斷：驗證瀏覽器基礎導航能力 ────────────────
        try:
            _log(f"🔍 Chromium 版本: {browser.version}")
            page.goto("about:blank", wait_until="domcontentloaded", timeout=5000)
            _log("✅ 瀏覽器基礎導航測試通過")
        except Exception as _diag_e:
            _log(f"❌ 瀏覽器基礎導航失敗（可能缺少系統函式庫）: {_diag_e}")
            raise
        try:
            page.goto("https://example.com", wait_until="commit", timeout=15000)
            _log("✅ 外部網路連線測試通過")
        except Exception as _diag_e:
            _log(f"⚠️ 外部網路連線測試失敗（略過）: {_diag_e}")

        # ── 工具：重試包裝 ──────────────────────────
        def retry(fn, attempts=3, delay=1, name="action"):
            last_exc = None
            for i in range(attempts):
                try:
                    return fn()
                except Exception as e:
                    last_exc = e
                    time.sleep(delay)
            raise last_exc

        # ── 工具：安全點擊 ──────────────────────────
        def safe_click(sel: str, timeout=5000, label="click", critical=True):
            def _():
                loc = page.locator(sel)
                count = loc.count()
                target = None
                if count > 1:
                    for i in range(count):
                        c = loc.nth(i)
                        if c.is_visible() and c.is_enabled():
                            target = c
                            break
                target = target or loc.first
                target.wait_for(state="visible", timeout=timeout)
                target.click(timeout=timeout)
            try:
                retry(_, attempts=2, delay=1, name=label)
            except Exception as e:
                if critical:
                    _log(f"❌ 關鍵點擊失敗 [{label}]: {e}")
                    raise
                else:
                    _log(f"⚠️ 非關鍵點擊失敗 [{label}]，繼續執行")

        # ── 工具：安全填寫 ──────────────────────────
        def safe_fill(sel: str, value: str, timeout=5000, label="fill", critical=True):
            def _():
                loc = page.locator(sel).first
                loc.wait_for(state="visible", timeout=timeout)
                loc.fill(str(value), timeout=timeout)
            try:
                retry(_, attempts=2, delay=1, name=label)
            except Exception as e:
                if critical:
                    _log(f"❌ 填寫失敗 [{label}]: {e}")
                    raise
                else:
                    _log(f"⚠️ 填寫失敗 [{label}]，跳過")

        # ── 工具：安全下拉選單 ───────────────────────
        def safe_select(sel: str, label=None, value=None, timeout=5000):
            def _():
                page.wait_for_selector(sel, timeout=timeout)
                if label is not None:
                    page.select_option(sel, label=label)
                elif value is not None:
                    page.select_option(sel, value=value)
            retry(_, attempts=3, delay=1, name=f"select {sel}")

        # ── 工具：jQuery UI 雙重彈窗防禦 ─────────────
        def dismiss_dialogs(max_attempts=5):
            """
            偵測並關閉所有可見的 jQuery UI 警告對話框。
            包含 #dngrWarnDialog 危險物品警告及其他 .ui-dialog。
            """
            dismissed = 0
            for _ in range(max_attempts):
                result = page.evaluate("""() => {
                    // 1. 先嘗試 warningMsgOff 勾選 + OK 按鈕
                    const warnOff = document.getElementById('warningMsgOff');
                    if (warnOff && !warnOff.checked) {
                        warnOff.checked = true;
                    }
                    // 2. 點擊任何可見 ui-dialog 的按鈕
                    const buttons = document.querySelectorAll('.ui-dialog-buttonpane button');
                    for (const btn of buttons) {
                        const dialog = btn.closest('.ui-dialog');
                        if (dialog && dialog.style.display !== 'none' && dialog.offsetParent !== null) {
                            btn.click();
                            return true;
                        }
                    }
                    // 3. 針對 Yes/OK 類型確認窗
                    const okBtns = document.querySelectorAll('button[class*="yes"], button[class*="ok"]');
                    for (const b of okBtns) {
                        if (b.offsetParent !== null) {
                            b.click();
                            return true;
                        }
                    }
                    return false;
                }""")
                if result:
                    dismissed += 1
                    page.wait_for_timeout(400)
                else:
                    break
            if dismissed:
                _log(f"🛡️ 關閉 {dismissed} 個警告對話框")
            return dismissed > 0

        # ── 工具：處理「前次未完成」對話框 ──────────
        def handle_previous_label_dialog():
            try:
                btn = page.locator(
                    'button:has-text("Create a new label"), '
                    'input[value="Create a new label"]'
                )
                if btn.count() > 0 and btn.first.is_visible():
                    _log("🔄 偵測到前次未完成的資料，點擊『新製單』重置")
                    page.evaluate("""() => {
                        const btns = document.querySelectorAll('button, input[type=button]');
                        for (const b of btns) {
                            if (b.textContent.includes('Create a new label') ||
                                b.value === 'Create a new label') {
                                b.click(); break;
                            }
                        }
                    }""")
                    page.wait_for_timeout(800)
            except Exception:
                pass

        # ── 登入流程 ─────────────────────────────────
        def check_logged_in() -> bool:
            try:
                if page.locator('img[alt="Log out"], a:has-text("Log out")').count() > 0:
                    return True
                if "/mypage/M010001.do" in page.url or "/mypage/M06" in page.url:
                    return True
                if page.locator('img[alt="Create New Labels"]').count() > 0:
                    return True
            except Exception:
                pass
            return False

        def _login_via_requests():
            """
            用 requests HTTP POST 直接登入，繞過 Playwright 導航至登入頁（會 crash）
            回傳：(playwright_cookies, post_login_url, success_bool, response_html)
            """
            import requests as _req
            import re as _re

            base = "https://www.int-mypage.post.japanpost.jp"
            s = _req.Session()
            s.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            })

            # Step 1: GET 登入頁，取 CSRF token 與 session cookie
            _log("🌐 requests 登入：取得登入頁面...")
            r1 = s.get(f"{base}/mypage/M010000.do?request_locale=en", timeout=30)
            _log(f"  → {r1.status_code}, {len(r1.content)} bytes")

            csrf = ""
            m = _re.search(r'name="csrfToken"[^>]+value="([^"]+)"', r1.text)
            if not m:
                m = _re.search(r'value="([^"]+)"[^>]*name="csrfToken"', r1.text)
            if m:
                csrf = m.group(1)
                _log(f"  → CSRF: {csrf[:10]}...")
            else:
                _log("  ⚠️ 找不到 CSRF token")

            # Step 2: POST 登入表單
            # submitCommand('login') 的實際行為：把 command 欄位名改為 method:login
            # 所以 POST body 要用 method:login=（空值），而非 command=login
            _log("🌐 requests 登入：提交表單...")
            r2 = s.post(
                f"{base}/mypage/M010000.do",
                data={
                    "method:login": "",
                    "csrfToken": csrf,
                    "loginBean.id": user,
                    "loginBean.pw": pwd,
                    "request_locale": "en",
                    "localeSel": "en",
                },
                headers={
                    "Referer": f"{base}/mypage/M010000.do?request_locale=en",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=30,
                allow_redirects=True,
            )
            _log(f"  → {r2.status_code}, final URL: {r2.url}")
            # 回應 body 前 300 字（debug 用）
            _body_snip = r2.text[:300].replace('\n', ' ').replace('\r', '')
            _log(f"  → body[:300]: {_body_snip}")

            success = (
                "M010001.do" in r2.url
                or "/mypage/M06" in r2.url
                or "Log out" in r2.text
                or "Create New Labels" in r2.text
            )
            _log(f"  → {'✅ 登入成功' if success else '⚠️ 登入狀態不明'}")

            # 轉換為 Playwright cookie 格式
            pw_cookies = []
            for c in s.cookies:
                domain = c.domain
                if domain and not domain.startswith("."):
                    domain = "." + domain
                pw_cookies.append({
                    "name": c.name,
                    "value": c.value,
                    "domain": domain or ".int-mypage.post.japanpost.jp",
                    "path": c.path or "/",
                })
            _log(f"  → {len(pw_cookies)} 個 cookies 提取完成")
            return s, pw_cookies, r2.url, success, r2.text

        def attempt_login():
            _log(f"🔐 執行登入，帳號: {user[:3]}***")
            login_url = (
                "https://www.int-mypage.post.japanpost.jp/mypage/M010000.do"
                "?request_locale=en"
            )
            # ?request_locale=en 強制英文介面
            page.goto(login_url, wait_until="load", timeout=60000)

            # 填帳號密碼
            # 精確 ID（DOM 檢查確認）
            user_loc = page.locator('#M010000_loginBean_id, input[name="loginBean.id"]')
            pass_loc = page.locator('#M010000_loginBean_pw, input[name="loginBean.pw"]')
            if user_loc.count() > 0:
                user_loc.first.fill(user)
            else:
                _log("⚠️ 找不到帳號欄位")
            if pass_loc.count() > 0:
                pass_loc.first.fill(pwd)
            else:
                _log("⚠️ 找不到密碼欄位")

            page.wait_for_timeout(500)

            # 登入鈕結構：<a href="javascript:submitCommand('login')"><img alt="Log in"></a>
            # 必須點錨點（而非 img）才能觸發 javascript: href
            clicked = False
            for sel in [
                'a:has(img[alt="Log in"])',    # DOM 確認的結構
                'a[href*="submitCommand"]',     # 備用
                'img[alt="Log in"]',            # 備用（事件冒泡）
            ]:
                loc = page.locator(sel)
                if loc.count() > 0:
                    loc.first.click()
                    clicked = True
                    _log(f"✅ 點擊登入按鈕 ({sel})")
                    break
            if not clicked:
                _log("⚠️ 未找到登入按鈕，嘗試 JS submitCommand")
                page.evaluate("submitCommand('login')")

            page.wait_for_timeout(3000)

            if not check_logged_in():
                # 備案：JS submit
                try:
                    page.evaluate("() => { if(typeof submitCommand === 'function') submitCommand('login'); }")
                    page.wait_for_timeout(2000)
                except Exception:
                    pass

        # ── 執行登入：優先用 requests 繞過 Playwright 登入頁 crash ──────
        _login_ok = False
        req_session = None
        main_menu_html = ""
        main_menu_url = "https://www.int-mypage.post.japanpost.jp/mypage/"
        try:
            req_session, pw_cookies, post_url, req_ok, post_html = _login_via_requests()
            if pw_cookies:
                context.add_cookies(pw_cookies)
                _log("✅ Cookies 已注入 Playwright context")
            if req_ok:
                main_menu_html = post_html
                main_menu_url = post_url or main_menu_url
                # Struts login success is a server-side forward: the URL can remain M010000.do
                # while the response body already contains the logged-in main menu.
                set_content_from_requests(post_html)
                page.wait_for_timeout(500)
                create_count = page.locator(
                    "img[alt='Create New Labels'], a:has-text('Create New Labels')"
                ).count()
                _log(
                    "🧭 Playwright 已載入登入後主選單 HTML："
                    f"url={page.url}, title={page.title()!r}, create_buttons={create_count}"
                )
                if create_count == 0:
                    body_snip = page.locator("body").inner_text(timeout=3000)[:300]
                    _log(f"⚠️ 主選單 HTML 未找到 Create New Labels，body[:300]={body_snip!r}")
                _login_ok = True
                _log("✅ requests 登入成功，Cookies 與主選單 HTML 已就位")
        except Exception as _re_err:
            _log(f"⚠️ requests 登入例外：{_re_err}")

        if not _login_ok:
            _log("❌ requests 登入失敗，請確認帳號密碼")
            raise RuntimeError("登入失敗：requests HTTP 登入未成功，請確認帳號密碼是否正確")

        if _login_ok:
            _log("✅ 登入成功")
        else:
            _log("⚠️ 登入狀態未確認，嘗試繼續...")

        # ── 逐筆處理訂單 ─────────────────────────────
        def open_create_label_form_via_requests() -> bool:
            if not req_session or not main_menu_html:
                return False
            current_html = main_menu_html
            referer_url = main_menu_url
            command_labels = [["Create New Labels"]] + [
                ["Enter the sender", "sender", "Next", "Register", "Select"]
                for _ in range(7)
            ]
            try:
                for step_idx, labels in enumerate(command_labels, start=1):
                    command = ""
                    if step_idx > 1:
                        command = _choose_label_flow_command(current_html, referer_url)
                    if not command:
                        for label in labels:
                            command = _extract_submit_command_for_label(current_html, label)
                            if command:
                                break
                    if not command:
                        _log(
                            "⚠️ requests 開啟打單頁：找不到下一步 command "
                            f"(step={step_idx}, labels={labels}, "
                            f"commands={_summarize_submit_commands(current_html)})"
                        )
                        raise RuntimeError("requests 無法找到下一步 command，停止以避免 Playwright crash")
                    action, payload = _build_struts_submit(
                        current_html,
                        command,
                        "https://www.int-mypage.post.japanpost.jp/mypage/",
                    )
                    _log(f"🌐 requests 開啟打單頁：step={step_idx}, command={command}, action={action}")
                    r = req_session.post(
                        action,
                        data=payload,
                        headers={
                            "Referer": referer_url,
                            "Content-Type": "application/x-www-form-urlencoded",
                        },
                        timeout=30,
                        allow_redirects=True,
                    )
                    body_snip = r.text[:240].replace("\n", " ").replace("\r", "")
                    _log(f"  → {r.status_code}, final URL: {r.url}, body[:240]: {body_snip}")
                    if r.status_code != 200:
                        return False
                    looks_like_sender_form = (
                        "M060505" in r.url
                        or "addrToBean" in r.text
                        or "#M060505_" in r.text
                    )
                    if looks_like_sender_form:
                        set_content_from_requests(r.text)
                        _log(
                            "✅ requests 已載入寄件人表單 HTML："
                            f"url={page.url}, title={page.title()!r}"
                        )
                        return True
                    current_html = r.text
                    referer_url = r.url
                _log(
                    "⚠️ requests 開啟打單頁：多步提交後仍未到寄件人表單，"
                    f"commands={_summarize_submit_commands(current_html)}"
                )
                raise RuntimeError("requests 多步提交後仍未到寄件人表單，停止以避免 Playwright crash")
            except Exception as e:
                _log(f"❌ requests 開啟打單頁例外：{e}")
                raise

        for row_idx, row in rows.iterrows():
            order_id = _get_excel_val(row, ["注文番号(貼上原始資料)", "注文番号(貼上原始資料)_1"])
            _log(f"\n{'='*50}\n▶ 開始處理訂單：{order_id}（索引 {row_idx}）")

            tracking = "ERROR"
            try:
                # ── 防前次未完成對話框 ────────────────
                handle_previous_label_dialog()

                # ── 點擊「Create New Labels」────────────
                if not open_create_label_form_via_requests():
                    safe_click(
                        "img[alt='Create New Labels'], "
                        "a:has-text('Create New Labels')",
                        label="main_menu_create",
                        critical=True,
                    )
                    page.wait_for_timeout(800)
                    handle_previous_label_dialog()  # 進入製單後再次檢查

                # ── Step 1: 寄件人頁 → Next ───────────
                if page.locator("#M060505_addrToBean_nam").count() == 0:
                    safe_click(
                        "input[value='Next']:not([disabled])",
                        label="sender_next",
                        critical=True,
                    )
                    page.wait_for_timeout(1000)
                else:
                    _log("✅ requests 已載入收件人輸入表單，略過 sender_next")

                # ── Step 2: 收件人資訊注入 ────────────
                # 先選國家（必須在填姓名前）
                country_raw = _get_excel_val(row, ["收件人國家", "Country"])
                from .sheets import COUNTRY_CODE_MAP
                country_code = COUNTRY_CODE_MAP.get(country_raw, "")

                country_sel = "#M060505_addrToBean_couCode"
                if country_raw:
                    try:
                        safe_select(country_sel, label=country_raw)
                    except Exception:
                        if country_code and country_code != "EU":
                            try:
                                safe_select(country_sel, value=country_code)
                            except Exception:
                                _log(f"⚠️ 國家選擇失敗: {country_raw}")

                # 姓名格式：「Shipping Name + 注文番号」
                name_val = _get_excel_val(row, ["Shipping Name", "Shipping Name_1"])
                final_name = f"{name_val} {order_id}".strip()

                # 使用精確 HTML ID 注入（防合併儲存格欄位錯位）
                safe_fill("#M060505_addrToBean_nam", final_name, label="name")
                safe_fill("#M060505_addrToBean_add1", "", label="add1_placeholder")
                safe_fill(
                    "#M060505_addrToBean_add2",
                    _get_excel_val(row, ["Shipping Street", "收件地址"]),
                    label="address",
                )
                safe_fill(
                    "#M060505_addrToBean_add3",
                    _get_excel_val(row, ["Shipping City", "城市"]),
                    label="city",
                )
                safe_fill(
                    "#M060505_addrToBean_pref",
                    _get_excel_val(row, ["收件人洲/省", "State"]),
                    label="state",
                )
                safe_fill(
                    "#M060505_addrToBean_postal",
                    _get_excel_val(row, ["Shipping Zip", "郵遞區號"]),
                    label="postal",
                )
                safe_fill(
                    "#M060505_addrToBean_tel",
                    _get_excel_val(row, ["Shipping Phone", "電話"]),
                    label="phone",
                )

                # ── Step 2 → Next ─────────────────────
                safe_click("input[type='button'][value='Next']", label="addr_next", critical=True)
                page.wait_for_timeout(1500)
                dismiss_dialogs()

                # ── Step 3: 運送方式分流 ──────────────
                shipping = _get_excel_val(row, ["郵局運送方式(複數商品請自行確認是否走小包)"])
                _log(f"📦 運送方式：{shipping}")
                is_eu = (country_code == "EU")

                if "ePacket" in shipping or "小包" in shipping:
                    # ── ePacket 子流程 ────────────────
                    _log("➡️ ePacket 子流程")
                    safe_click("img[alt='International ePacket light']", label="select_epacket")
                    page.wait_for_timeout(1000)
                    dismiss_dialogs()

                    for i in range(1, 5):
                        pkg = _clean(row.get(f"內容物{i}", ""))
                        if not pkg:
                            break
                        cost = _clean(row.get(f"申告金額{i}", "0"))
                        raw_num = row.get(f"數量{i}", 1)
                        try:
                            num = str(int(float(raw_num))) if raw_num != "" else "1"
                        except Exception:
                            num = "1"

                        safe_fill("input[name='itemBean.pkg']", pkg, label=f"pkg_{i}")
                        safe_fill("input[name='itemBean.cost.value']", cost, label=f"cost_{i}")
                        try:
                            page.select_option("select[name='itemBean.curUnit']", "USD")
                        except Exception:
                            pass
                        safe_fill("input[name='itemBean.num.value']", num, label=f"num_{i}")

                        # EU → Gemini HS Code
                        if is_eu:
                            hs = predict_hs_code(pkg, log_cb=log_cb)
                            if hs:
                                # 嘗試帶 .value 後綴變體
                                for hs_sel in [
                                    "input[name='itemBean.hsCode']",
                                    "input[name='itemBean.hsCode.value']",
                                ]:
                                    try:
                                        if page.locator(hs_sel).count() > 0:
                                            safe_fill(hs_sel, hs, label=f"hscode_{i}", critical=False)
                                            break
                                    except Exception:
                                        pass

                        safe_click("input[value='Confirm']", label=f"confirm_{i}")
                        page.wait_for_timeout(800)
                        dismiss_dialogs()

                        # 檢查數量上限警告
                        try:
                            limit_text = page.evaluate("""() => {
                                const ds = document.querySelectorAll('.ui-dialog[style*="display: block"]');
                                for (const d of ds) {
                                    if (d.innerText && (d.innerText.includes('allowable limit') ||
                                        d.innerText.includes('制限数を超えています'))) {
                                        return d.innerText;
                                    }
                                }
                                return null;
                            }""")
                            if limit_text:
                                _log(f"⚠️ 數量上限警告（第 {i} 項），停止添加")
                                dismiss_dialogs()
                                break
                        except Exception:
                            pass

                    # 填寫 JPY 合計金額
                    total_jpy = _get_excel_val(row, ["訂單合計申告金額(JPY)"])
                    if total_jpy:
                        safe_fill(
                            "input[name='shippingBean.pkgTotalPrice.value']",
                            total_jpy, label="total_jpy", critical=False,
                        )

                else:
                    # ── PostalParcel_Air 子流程 ────────
                    _log("➡️ PostalParcel Air 子流程")
                    safe_click("img[alt='POSTAL PARCEL']", label="select_postal")
                    page.wait_for_timeout(1000)
                    dismiss_dialogs()
                    safe_click("img[alt='AIR']", label="select_air")
                    page.wait_for_timeout(800)
                    dismiss_dialogs()

                    try:
                        page.check("input[name*='senderInstruction']")
                        page.check("input[name*='fwTransType']")
                    except Exception:
                        pass

                    pkg = _clean(row.get("內容物1", ""))
                    if pkg:
                        cost = _clean(row.get("申告金額1", "0"))
                        raw_num = row.get("數量1", 1)
                        try:
                            num = str(int(float(raw_num))) if raw_num != "" else "1"
                        except Exception:
                            num = "1"
                        safe_fill("input[name='itemBean.pkg']", pkg, label="parcel_pkg")
                        safe_fill("input[name='itemBean.cost.value']", cost, label="parcel_cost")
                        safe_fill("input[name='itemBean.num.value']", num, label="parcel_num")

                        if is_eu:
                            hs = predict_hs_code(pkg, log_cb=log_cb)
                            if hs:
                                for hs_sel in [
                                    "input[name='itemBean.hsCode']",
                                    "input[name='itemBean.hsCode.value']",
                                ]:
                                    try:
                                        if page.locator(hs_sel).count() > 0:
                                            safe_fill(hs_sel, hs, label="parcel_hscode", critical=False)
                                            break
                                    except Exception:
                                        pass

                        safe_click("input[value='Confirm']", label="parcel_confirm")
                        page.wait_for_timeout(800)
                        dismiss_dialogs()

                # ── Step 3: 危險物品聲明 + Next ────────
                for danger_sel in [
                    "#M060800_ShippingBean_danger",
                    "input[name='ShippingBean.danger']",
                ]:
                    try:
                        if page.locator(danger_sel).is_visible(timeout=1500):
                            page.locator(danger_sel).check()
                            _log("☑️ 勾選危險物品聲明")
                            break
                    except Exception:
                        pass

                safe_click("input[value='Next']", label="contents_next", critical=True)
                page.wait_for_timeout(1500)

                # ── Step 4: 重量頁（若存在）────────────
                weight_sel = "#M060900_shippingBean_totalWeight_value"
                if page.locator(weight_sel).is_visible(timeout=2000):
                    _log("⚖️ Step 4 重量頁")
                    try:
                        if page.locator("#M060900_ShippingBean_danger").is_visible(timeout=1000):
                            page.locator("#M060900_ShippingBean_danger").check()
                    except Exception:
                        pass
                    safe_fill(weight_sel, "100", label="weight")
                    safe_click("input[type='button'][value='Next']", label="weight_next", critical=True)
                    page.wait_for_timeout(1500)

                # ── Step 5: 確認 + Register Shipment ───
                safe_click("input[value='Register Shipment']", label="register", critical=True)
                page.wait_for_timeout(2000)

                # ── Step 6: PDF 封包攔截（M061000.do）──
                _log("📥 抵達 M061000.do，準備攔截 PDF...")
                page.wait_for_url("**/M061000.do*", timeout=10000)
                pdf_content = None

                try:
                    with page.expect_request(
                        lambda req: "DOWNLOAD?pdf=" in req.url, timeout=15000
                    ) as req_info:
                        page.locator(
                            "input[value*='Print after agreeing'][onclick*='print']"
                        ).evaluate("n => n.click()")

                    pdf_url = req_info.value.url
                    cookie_str = "; ".join(
                        [f"{c['name']}={c['value']}" for c in page.context.cookies()]
                    )
                    resp = page.request.get(pdf_url, headers={"Cookie": cookie_str})
                    if resp.ok:
                        pdf_content = resp.body()
                        _log(f"✅ PDF 攔截成功（{len(pdf_content)} bytes）")
                    else:
                        _log(f"⚠️ PDF 請求失敗: HTTP {resp.status}")
                except Exception as e:
                    _log(f"⚠️ PDF 攔截失敗: {e}")

                # ── Step 7: 擷取貨運單號（M061100.do）──
                _log("🔍 等待跳轉至 M061100.do 擷取貨運單號...")
                try:
                    page.wait_for_url("**/M061100.do*", timeout=6000)
                    page.wait_for_timeout(1500)
                except Exception:
                    _log("⚠️ 未偵測到自動跳轉，強制導航至完成頁")
                    page.goto(
                        "https://www.int-mypage.post.japanpost.jp/mypage/M061100.do"
                    )
                    page.wait_for_timeout(2000)

                page_text = page.locator("body").inner_text()
                match = re.search(r"([A-Z]{2}\d{9}JP)", page_text)
                if match:
                    tracking = match.group(1)
                    _log(f"🎉 貨運單號：{tracking}")
                else:
                    # 備案：精確 CSS 路徑
                    tracking_css = (
                        "#loaded > table:nth-child(4) > tbody > tr:nth-child(1) > "
                        "td > div > div:nth-child(3) > div > table > tbody > "
                        "tr:nth-child(2) > td:nth-child(1) > div > b"
                    )
                    try:
                        if page.locator(tracking_css).is_visible(timeout=2000):
                            tracking = page.locator(tracking_css).inner_text().strip()
                            _log(f"💡 備案 CSS 取得單號：{tracking}")
                    except Exception:
                        _log("⚠️ 無法擷取貨運單號")

                # ── Step 8: PDF 命名、存檔、上傳 Drive ─
                if pdf_content and tracking not in ("ERROR", "N/A", ""):
                    content_name = _get_excel_val(row, ["郵局內容物"]) or "Item"
                    ship_name = _get_excel_val(row, ["Shipping Name", "Shipping Name_1"])
                    fname = _sanitize_filename(
                        f"{content_name}_{order_id}_{tracking}_{ship_name}.pdf"
                    )
                    upload_pdf(pdf_content, fname, log_cb=log_cb)

                # ── Step 9: 點擊 Completed 返回首頁 ────
                try:
                    final_btn = page.locator(
                        "input[value='Print Completed'], input[value='Completed']"
                    )
                    if final_btn.count() > 0:
                        final_btn.first.click(timeout=5000)
                        page.wait_for_timeout(800)
                        _log("✅ 已點擊 Completed，返回首頁")
                    else:
                        _log("⚠️ 未找到 Completed 按鈕，略過")
                except Exception as e:
                    _log(f"⚠️ Step 9 點擊 Completed 失敗（略過）：{e}")

                # ── 收集結果 ────────────────────────────
                results.append({
                    "name": _get_excel_val(row, ["Shipping Name", "Shipping Name_1"]),
                    "order_id": order_id,
                    "tracking": tracking,
                    "country": _get_excel_val(row, ["收件人國家", "Country"]),
                    "date": time.strftime("%Y-%m-%d"),
                })
                _log(f"📌 訂單 {order_id} 完成，貨運單號：{tracking}")

            except Exception as e:
                import traceback as _tb
                _log(f"❌ 訂單 {order_id} 例外：{type(e).__name__}: {e}")
                _log(f"詳細：{_tb.format_exc()}")

    return results
