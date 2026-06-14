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
from pathlib import Path
from datetime import date
import pandas as pd

from .drive import upload_pdf
from .gemini_helper import predict_hs_code


def _get_jp_post_creds() -> tuple[str, str]:
    import streamlit as st
    try:
        return st.secrets["JP_POST_USER"], st.secrets["JP_POST_PASS"]
    except Exception:
        return os.environ.get("JP_POST_USER",""), os.environ.get("JP_POST_PASS","")


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
            if v: return v
    return ""


def run_automation(df: pd.DataFrame, max_rows: int | None = None, log_cb=None, headless: bool = True) -> list[dict]:
    def _log(msg: str):
        if log_cb: log_cb(msg)
        logging.info(msg)

    from playwright.sync_api import sync_playwright

    rows = df if max_rows is None else df.head(max_rows)
    results: list[dict] = []
    user, pwd = _get_jp_post_creds()

    if not user or not pwd:
        _log("❌ POST credentials not configured")
        return results

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        def retry(fn, attempts=3, delay=1, name="action"):
            last_exc = None
            for i in range(attempts):
                try: return fn()
                except Exception as e: last_exc = e; time.sleep(delay)
            raise last_exc

        def safe_click(sel, timeout=5000, label="click", critical=True):
            def _():
                loc = page.locator(sel)
                count = loc.count()
                target = None
                if count > 1:
                    for i in range(count):
                        c = loc.nth(i)
                        if c.is_visible() and c.is_enabled(): target = c; break
                target = target or loc.first
                target.wait_for(state="visible", timeout=timeout)
                target.click(timeout=timeout)
            try: retry(_, attempts=2, delay=1, name=label)
            except Exception as e:
                if critical: _log(f"❌ step {label}: {e}"); raise
                else: _log(f"⚠️ step {label} failed, skipping")

        def safe_fill(sel, value, timeout=5000, label="fill", critical=True):
            def _():
                loc = page.locator(sel).first
                loc.wait_for(state="visible", timeout=timeout)
                loc.fill(str(value), timeout=timeout)
            try: retry(_, attempts=2, delay=1, name=label)
            except Exception as e:
                if critical: _log(f"❌ fill {label}: {e}"); raise
                else: _log(f"⚠️ fill {label} failed")

        def safe_select(sel, label=None, value=None, timeout=5000):
            def _():
                page.wait_for_selector(sel, timeout=timeout)
                if label is not None: page.select_option(sel, label=label)
                elif value is not None: page.select_option(sel, value=value)
            retry(_, attempts=3, delay=1, name=f"select {sel}")

        def dismiss_dialogs(max_attempts=5):
            dismissed = 0
            for _ in range(max_attempts):
                r = page.evaluate("""() => {
                    const wOff = document.getElementById('warningMsgOff');
                    if (wOff && !wOff.checked) wOff.checked = true;
                    const btns = document.querySelectorAll('.ui-dialog-buttonpane button');
                    for (const b of btns) {
                        const d = b.closest('.ui-dialog');
                        if (d && d.style.display !== 'none' && d.offsetParent) { b.click(); return true; }
                    }
                    const okBtns = document.querySelectorAll('button[class*="yes"],button[class*="ok"]');
                    for (const b of okBtns) { if (b.offsetParent) { b.click(); return true; } }
                    return false;
                }""")
                if r: dismissed += 1; page.wait_for_timeout(400)
                else: break
            if dismissed: _log(f"🛡️ Dismissed {dismissed} dialog(s)")
            return dismissed > 0

        def handle_previous_label_dialog():
            try:
                btn = page.locator('button:has-text("Create a new label"), input[value="Create a new label"]')
                if btn.count() > 0 and btn.first.is_visible():
                    _log("🔄 Found previous label dialog, clicking Create new")
                    page.evaluate("() => { const btns = document.querySelectorAll('button,input[type=button]'); for (const b of btns) { if (b.textContent.includes('Create a new label') || b.value==='Create a new label') { b.click(); break; } } }")
                    page.wait_for_timeout(800)
            except Exception: pass

        def check_logged_in() -> bool:
            try:
                if page.locator('img[alt="Log out"], a:has-text("Log out")').count() > 0: return True
                if "/mypage/M010001.do" in page.url or "/mypage/M06" in page.url: return True
                if page.locator('img[alt="Create New Labels"]').count() > 0: return True
            except Exception: pass
            return False

        def attempt_login():
            _log(f"🔐 Logging in as {user[:3]}***")
            login_url = "https://www.int-mypage.post.japanpost.jp/mypage/M010000.do?request_locale=en"
            page.goto(login_url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(1500)
            user_loc = page.locator('input[id*="mailAddress"], input[id*="loginBean.id"], input[name="loginBean.id"], input[type="text"]')
            pass_loc = page.locator('input[type="password"]')
            if user_loc.count() > 0: user_loc.first.fill(user)
            if pass_loc.count() > 0: pass_loc.first.fill(pwd)
            page.wait_for_timeout(600)
            clicked = False
            for sel in ['img[alt="Log in"]','input[type="image"][alt*="Log in"]','a:has-text("Log in")','button:has-text("Log in")','#M010000_login']:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible(): loc.first.click(); clicked = True; break
            if not clicked: page.keyboard.press("Enter")
            page.wait_for_timeout(3000)
            if not check_logged_in():
                try: page.evaluate("() => { if(typeof submitCommand==='function') submitCommand('login'); }"); page.wait_for_timeout(2000)
                except Exception: pass

        login_url = "https://www.int-mypage.post.japanpost.jp/mypage/M010000.do?request_locale=en"
        page.goto(login_url, wait_until="networkidle", timeout=30000)
        if not check_logged_in(): attempt_login()
        if check_logged_in(): _log("✅ Logged in")
        else: _log("⚠️ Login may have failed, continuing...")

        for row_idx, row in rows.iterrows():
            order_id = _get_excel_val(row, ["注文番号(貼上原始資料)", "注文番号(貼上原始資料)_1"])
            _log(f"\n▶ Processing order: {order_id} (row {row_idx})")
            tracking = "ERROR"
            try:
                handle_previous_label_dialog()
                safe_click("img[alt='Create New Labels'], a:has-text('Create New Labels')", label="main_menu_create", critical=True)
                page.wait_for_timeout(800)
                handle_previous_label_dialog()
                safe_click("input[value='Next']:not([disabled])", label="sender_next", critical=True)
                page.wait_for_timeout(1000)
                country_raw = _get_excel_val(row, ["收件人國家'�, "Country"])
                from .sheets import COUNTRY_CODE_MAP
                country_code = COUNTRY_CODE_MAP.get(country_raw, "")
                country_sel = "#M060505_addrToBean_couCode"
                if country_raw:
                    try: safe_select(country_sel, label=country_raw)
                    except Exception:
                        if country_code and country_code != "EU":
                            try: safe_select(country_sel, value=country_code)
                            except Exception: _log(f"⚠️ Country select failed: {country_raw}")
                name_val = _get_excel_val(row, ["Shipping Name", "Shipping Name_1"])
                final_name = f"{name_val} {order_id}".strip()
                safe_fill("#M060505_addrToBean_nam", final_name, label="name")
                safe_fill("#M060505_addrToBean_add1", "", label="add1_ph")
                safe_fill("#M060505_addrToBean_add2", _get_excel_val(row, ["Shipping Street", "收件圠址"]), label="address")
                safe_fill("#M060505_addrToBean_add3", _get_excel_val(row, ["Shipping City", "城市"]), label="city")
                safe_fill("#M060505_addrToBean_pref", _get_excel_val(row, ["收件人洲/省", "State"]), label="state")
                safe_fill("#M060505_addrToBean_postal", _get_excel_val(row, ["Shipping Zip", "郵醞區號"]), label="postal")
                safe_fill("#M060505_addrToBean_tel", _get_excel_val(row, ["Shipping Phone", "電話"]), label="phone")
                safe_click("input[type='button'][value='Next']", label="addr_next", critical=True)
                page.wait_for_timeout(1500)
                dismiss_dialogs()
                shipping = _get_excel_val(row, ["郵局運送方式觇數商品請自行確認是否走小包"])
                is_eu = (country_code == "EU")
                if "ePacket" in shipping or "小包" in shipping:
                    _log("➡️ ePacket mode")
                    safe_click( "img[alt='International ePacket light']", label="select_epacket")
                    page.wait_for_timeout(1000)
                    dismiss_dialogs()
                    for i in range(1, 5):
                        pkg = _clean(row.get(f"內容皗{i}", ""))
                        if not pkg: break
                        cost = _clean(row.get(f"申告金額{i}", "0"))
                        raw_num = row.get(f"數量{i}", 1)
                        try: num = str(int(float(raw_num))) if raw_num != "" else "1"
                        except Exception: num = "1"
                        safe_fill("input[name='itemBean.pkg']", pkg, label=f"pkg_{i}")
                        safe_fill("input[name='itemBean.cost.value']", cost, label=f"cost_{i}")
                        try: page.select_option("select[name='itemBean.curUnit']", "USD")
                        except Exception: pass
                        safe_fill("input[name='itemBean.num.value']", num, label=f"num_{i}")
                        if is_eu:
                            hs = predict_hs_code(pkg, log_cb=log_cb)
                            if hs:
                                for hs_sel in ["input[name='itemBean.hsCode']","input[name='itemBean.hsCode.value']"]:
                                    try:
                                        if page.locator(hs_sel).count() > 0: safe_fill(hs_sel, hs, label=f"hscode_{i}", critical=False); break
                                    except Exception: pass
                        safe_click( "input[value='Confirm']", label=f"confirm_{i}")
                        page.wait_for_timeout(800)
                        dismiss_dialogs()
                    total_jpy = _get_excel_val(row, ["訂單Total(JPY)"])
                    if total_jpy: safe_fill("input[name='shippingBean.pkgTotalPrice.value']", total_jpy, label="total_jpy", critical=False)
                else:
                    _log("➡ ️ PostalParcel Air mode")
                    safe_click("img[alt='POSTAL PARCEL']", label="select_postal")
                    page.wait_for_timeout(1000)
                    dismiss_dialogs()
                    safe_click("img[alt='AIR']", label="select_air")
                    page.wait_for_timeout(800)
                    dismiss_dialogs()
                    try: page.check("input[name*='senderInstruction']"); page.check("input[name*='fwTransType']")
                    except Exception: pass
                    pkg = _clean(row.get("內容癩u", ""))
                    if pkg:
                        cost = _clean(row.get("申告金額1", "0"))
                        raw_num = row.get("數量1", 1)
                        try: num = str(int(float(raw_num))) if raw_num != "" else "1"
                        except Exception: num = "1"
                        safe_fill("input[name='itemBean.pkg']", pkg, label="parcel_pkg")
                        safe_fill("input[name='itemBean.cost.value']", cost, label="parcel_cost")
                        safe_fill("input[name='itemBean.num.value']", num, label="parcel_num")
                        if is_eu:
                            hs = predict_hs_code(pkg, log_cb=log_cb)
                            if hs:
                                for hs_sel in ["input[name='itemBean.hsCode']","input[name='itemBean.hsCode.value']"]:
                                    try:
                                        if page.locator(hs_sel).count() > 0: safe_fill(hs_sel, hs, label="parcel_hs", critical=False); break
                                    except Exception: pass
                        safe_click("input[value='Confirm']", label="parcel_confirm")
                        page.wait_for_timeout(800)
                        dismiss_dialogs()
                for dsel in ["#M060800_ShippingBean_danger","input[name='ShippingBean.danger']"]:
                    try:
                        if page.locator(dsel).is_visible(timeout=1500): page.locator(dsel).check(); break
                    except Exception: pass
                safe_click( "input[value='Next']", label="contents_next", critical=True)
                page.wait_for_timeout(1500)
                weight_sel = "#M060900_shippingBean_totalWeight_value"
                if page.locator(weight_sel).is_visible(timeout=2000):
                    try:
                        if page.locator("#M060900_ShippingBean_danger").is_visible(timeout=1000): page.locator("#M060900_ShippingBean_danger").check()
                    except Exception: pass
                    safe_fill(weight_sel, "100", label="weight")
                    safe_click("input[type='button'][value='Next']", label="weight_next", critical=True)
                    page.wait_for_timeout(1500)
                safe_click( "input[value='Register Shipment']", label="register", critical=True)
                page.wait_for_timeout(2000)
                _log("📥 Waiting for M061000.do and PDF...")
                page.wait_for_url("**/M061000.do*", timeout=10000)
                pdf_content = None
                try:
                    with page.expect_request(lambda req: "DOWNLOAD?pdf=" in req.url, timeout=15000) as req_info:
                        page.locator("input[value*='Print after agreeing'][onclick*='print']").evaluate("n => n.click()")
                    pdf_url = req_info.value.url
                    cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in page.context.cookies()])
                    resp = page.request.get(pdf_url, headers={"Cookie": cookie_str})
                    if resp.ok: pdf_content = resp.body(); _log(f"✅ PDF {len(pdf_content)} bytes")
                    else: _log(f"⚠️ PDF HTTP {resp.status}")
                except Exception as e: _log(f"⚠️ PDF error: {e}")
                _log("🔍 Waiting for M061100.do...")
                try:
                    page.wait_for_url("**/M061100.do*", timeout=6000)
                    page.wait_for_timeout(1500)
                except Exception:
                    _log("⚠️ Timeout waiting for M061100.do, navigating directly")
                    page.goto("https://www.int-mypage.post.japanpost.jp/mypage/M061100.do")
                    page.wait_for_timeout(2000)
                page_text = page.locator("body").inner_text()
                match = re.search(r"([A-Z]{2}\d{9}JP)", page_text)
                if match: tracking = match.group(1); _log(f"🎉 Tracking: {tracking}")
                else:
                    tracking_css = ("#loaded > table:nth-child(4) > tbody > tr:nth-child(1) > td > div > div:nth-child(3) > div > table > tbody > tr:nth-child(2) > td:nth-child(1) > div > b")
                    try:
                        if page.locator(tracking_css).is_visible(timeout=2000): tracking = page.locator(tracking_css).inner_text().strip(); _log(f"💡 CSS tracking: {tracking}")
                    except Exception: _log("⚠️ Could not extract tracking")
                if pdf_content and tracking not in ("ERROR","N/A",""):
                    content_name = _get_excel_val(row, ["郵局內容物"]) or "Item"
                    ship_name = _get_excel_val(row, ["Shipping Name", "Shipping Name_1"])
                    fname = _sanitize_filename(f"{content_name}_{order_id}_{tracking}_{ship_name}.pdf")
                    upload_pdf(pdf_content, fname, log_cb=log_cb)
                try:
                    final_btn = page.locator("input[value='Print Completed'], input[value='Completed']")
                    if final_btn.is_visible(timeout=3000): final_btn.click(); page.wait_for_timeout(1500)
                except Exception: pass
                if tracking not in ("ERROR","N/A",""):
                    results.append({"name": _get_excel_val(row,["Shipping Name","Shipping Name_1"]), "order_id": order_id, "tracking": tracking, "country_raw": country_raw, "date": str(date.today())})
                    _log(f"✅ Order {order_id}: {tracking}")
                else: _log(f"❌ Order {order_id}: no tracking")
            except Exception as e:
                _log(f"❌ Order {order_id} failed: {e}")
                try: page.goto("https://www.int-mypage.post.japanpost.jp/mypage/M010001.do", timeout=10000); page.wait_for_timeout(1500)
                except Exception: pass
        browser.close()
    _log(f"\n���� Done: {len(results)}/{len(rows)}")
    return results
