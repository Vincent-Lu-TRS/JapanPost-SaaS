"""Authenticated Streamlit surfaces for explicit split and reissue work."""
import copy
import re
import time
import uuid

import pandas as pd
import streamlit as st

from pending_editor import (SHIPPING_COL, SHIPPING_OPTIONS, apply_pending_order_editor_values,
                            build_pending_item_frame, compose_shipping_name, parse_shipping_name)
from shipment_plans import ORDER, build_plan, digest, plan_frame, source_items, utc_now


def verified_email():
    from auth import has_native_auth_config, is_authorized
    if has_native_auth_config():
        from auth import _st_user_get
        email = str(_st_user_get('email', '')).lower().strip()
        return email if (_st_user_get('is_logged_in', False) and _st_user_get('email_verified', False) is True
                         and is_authorized(email)) else ''
    email = str(st.session_state.get('_verified_auth_email', '')).lower().strip()
    return email if (st.session_state.get('authenticated') is True
                     and email == str(st.session_state.get('user_email', '')).lower().strip()
                     and is_authorized(email)) else ''


def _preview(plan):
    st.caption(f"原訂單 {plan['order_id']}｜{len(plan['shipments'])} 包｜計畫 {plan['plan_id'][:16]}")
    rows = []
    for index, shipment in enumerate(plan['shipments'], 1):
        rows.append({'包裹': index, '方式': shipment['method'], '申告 USD': shipment['total_usd'],
                     '內容物': '、'.join(f"{i['description']} × {i['quantity']}" for i in shipment['items'])})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width='stretch')


def _show_results(results):
    for result in results:
        st.write(f"{result['order_id']}｜{result.get('tracking','')}｜{result['status']}")
        file_id = result.get('drive_file_id', '')
        if re.fullmatch(r'[A-Za-z0-9_-]+', file_id):
            st.link_button('開啟既有 PDF（重印，不製單）', f'https://drive.google.com/file/d/{file_id}/view')


def _execute(plan, *, key, baseline=None, confirmed=False, disabled=False):
    _preview(plan)
    if st.button('送出拆單製單' if plan['mode'] == 'split' else '送出新補製單',
                 key=key + '_execute', disabled=disabled):
        email = verified_email()
        if not email:
            st.error('請先登入授權帳號')
            return
        try:
            from bot.shipment_service import execute_plan
            with st.spinner('正在執行已確認計畫；中斷後請按原計畫恢復，勿新建補製。'):
                results = execute_plan(copy.deepcopy(plan), requested_by=email,
                                       tracking_baseline=baseline, confirmed_new_tracking=confirmed)
            st.session_state[key + '_results'] = results
        except ValueError as error:
            st.error(str(error))
        except Exception as error:
            code = getattr(error, 'reason_code', 'shipment_safety_unavailable')
            st.error(f'工作已停止，請以原計畫恢復或交管理員核對。計畫 {plan["plan_id"]}｜{code}')
    _show_results(st.session_state.get(key + '_results', []))


def render_split_planner(row, *, source_row=None, key='split', extra_selected=False, disabled=False):
    if not verified_email():
        return
    if not st.checkbox('拆分包裹', key=key + '_enabled', disabled=disabled):
        return
    if extra_selected:
        st.error('整單追加會複製完整內容；拆單請先把「追加製作」設為「無」。')
        return
    source_row = source_row if source_row is not None else row
    try:
        items = source_items(row)
        original = source_items(source_row)
        if [(i['source_index'], i['quantity']) for i in items] != [(i['source_index'], i['quantity']) for i in original]:
            raise ValueError('拆單需保留來源各項總數量；請恢復數量後再分配包裹')
    except ValueError as error:
        st.error(str(error))
        return
    count = int(st.number_input('包裹數', min_value=1, max_value=20, value=1,
                                step=1, key=key + '_count', disabled=disabled))
    packages = []
    for index in range(count):
        st.markdown(f'包裹 {index + 1}')
        method = st.selectbox('包裹寄送方式', SHIPPING_OPTIONS,
                              index=SHIPPING_OPTIONS.index(row.get(SHIPPING_COL)) if row.get(SHIPPING_COL) in SHIPPING_OPTIONS else 0,
                              key=f'{key}_method_{index}', disabled=disabled)
        quantities = {}
        for item in items:
            quantities[item['id']] = st.number_input(
                f"{item['description']}（原數量 {item['quantity']}）", min_value=0,
                max_value=1000000, value=item['quantity'] if index == 0 else 0, step=1,
                key=f"{key}_qty_{index}_{item['source_index']}", disabled=disabled)
        packages.append({'method': method, 'quantities': quantities})
    st.caption('每項分配合計必須等於原數量；每包至少一項。相同寄送方式可分成多包。拆單使用獨立送出按鈕。')
    candidate = None
    try:
        from bot.shipment_service import validate_recipient
        validate_recipient(row)
        candidate = build_plan(row, packages, source_row=source_row)
    except ValueError as error:
        st.warning(str(error))
    if st.button('確認拆單計畫', key=key + '_review', disabled=disabled or candidate is None):
        st.session_state[key + '_reviewed_plan'] = candidate
    reviewed = st.session_state.get(key + '_reviewed_plan')
    if candidate and reviewed and candidate['integrity'] == reviewed['integrity']:
        _execute(reviewed, key=key, disabled=disabled)
    elif reviewed:
        st.caption('分配已變更，請重新確認拆單計畫。')


def _edit_history_row(history):
    row = pd.Series(copy.deepcopy(history['row']))
    parsed = parse_shipping_name(row.get('Shipping Name',''))
    name = st.text_input('姓名', value=parsed['clean_name'], key='reissue_name')
    prc = st.text_input('PRC ID', value=parsed['prc_id'], key='reissue_prc') if 'CHINA' in str(row.get('收件人國家','')).upper() else parsed['prc_id']
    pccc = st.text_input('PCCC', value=parsed['pccc'], key='reissue_pccc') if 'KOREA' in str(row.get('收件人國家','')).upper() else parsed['pccc']
    method = st.selectbox('補製寄送方式', SHIPPING_OPTIONS,
                          index=SHIPPING_OPTIONS.index(row.get(SHIPPING_COL)) if row.get(SHIPPING_COL) in SHIPPING_OPTIONS else 0,
                          key='reissue_method')
    with st.expander('收件地址與聯絡資料'):
        for column in ('Shipping Street','Shipping City','收件人洲/省','Shipping Zip','Shipping Phone'):
            row[column] = st.text_input(column, value=str(row.get(column,'')), key='reissue_' + column)
    edited = st.data_editor(build_pending_item_frame(row), hide_index=True, num_rows='fixed',
                            disabled=['Content'], key='reissue_items')
    summary = pd.DataFrame([{'Name': compose_shipping_name(name, str(row.get('收件人國家','')), prc, pccc),
                             'TransType': method}])
    return apply_pending_order_editor_values(pd.DataFrame([row]), summary, {0: edited}).iloc[0]


def render_reissue_tab(*, disabled=False):
    email = verified_email()
    if not email:
        st.caption('請先登入授權帳號')
        return
    st.caption('重新列印既有 PDF 不會產生 tracking；補製會建立新運單並保留舊紀錄。')
    order = st.text_input('完整訂單編號', key='reissue_order_query', max_chars=80)
    if st.button('查詢歷史訂單', key='reissue_query', disabled=disabled):
        last = float(st.session_state.get('reissue_last_query', 0))
        if time.monotonic() - last < 2:
            st.warning('請稍候再查詢')
        else:
            st.session_state['reissue_last_query'] = time.monotonic()
            # Clear old form values on a new explicit query; same query remains
            # stable across reruns and double clicks after confirmation.
            for name in list(st.session_state):
                if name.startswith('reissue_') and name not in {'reissue_order_query','reissue_query','reissue_last_query'}:
                    del st.session_state[name]
            try:
                from bot.shipment_service import lookup_history
                st.session_state['reissue_history'] = lookup_history(order.strip())
                st.session_state['reissue_nonce'] = uuid.uuid4().hex
            except ValueError as error:
                st.error(str(error))
            except Exception:
                st.error('歷史資料或加密明細帳無法安全讀取；請聯絡管理員。')
    history = st.session_state.get('reissue_history')
    if not history or order.strip() != history['order_id']:
        return
    st.caption(f"資料來源時間 {history['source_time']}")
    if history['source_kind'] != 'ledger_snapshot':
        st.warning('來源現存資料，非當時製單快照；請逐項核對地址、內容物與數量。')
    else:
        st.info('已保存的完整訂單快照；下方既有包裹清單保留各包 tracking 與 PDF。')
    st.write('既有 tracking：' + '、'.join(history['tracking_baseline']))
    for shipment in history['shipments']:
        _show_results([{**shipment, 'order_id': history['order_id']}])
    if st.button('尋找舊 PDF（只重印）', key='reissue_find_pdf', disabled=disabled):
        from bot.drive import DRIVE_FOLDER_ID, list_drive_files
        try:
            found = [file for file in list_drive_files(DRIVE_FOLDER_ID, name_prefix=history['order_id'])
                     if '_' + history['order_id'] + '_' in file.get('name','')]
            st.session_state['reissue_old_pdfs'] = [{'id': file['id'], 'name': file.get('name','PDF')} for file in found]
        except Exception:
            st.warning('舊 PDF 無法讀取，請到受限 Drive 資料夾確認。')
    for file in st.session_state.get('reissue_old_pdfs', []):
        if re.fullmatch(r'[A-Za-z0-9_-]+', file['id']):
            st.link_button(file['name'], f'https://drive.google.com/file/d/{file["id"]}/view')
    if not history['tracking_baseline']:
        st.warning('此訂單沒有已確認的舊運單，只能恢復原計畫或由管理員核對。')
        _render_resume(history['order_id'], email, disabled=disabled)
        return
    row = _edit_history_row(history)
    original_tracking = st.selectbox('關聯原運單', history['tracking_baseline'], key='reissue_original_tracking')
    reason = st.text_area('補製原因', key='reissue_reason', max_chars=500)
    confirmed = st.checkbox('我已核對，這次將產生新 tracking，並保留舊運單', key='reissue_new_tracking')
    candidate = None
    try:
        from bot.shipment_service import validate_recipient
        validate_recipient(row)
        items = source_items(row)
        candidate = build_plan(row, [{'method': row[SHIPPING_COL],
                                      'quantities': {i['id']: i['quantity'] for i in items}}],
                               mode='reissue', reason=reason, original_tracking=original_tracking,
                               nonce=st.session_state['reissue_nonce'], source_row=history['row'],
                               source_kind=history['source_kind'], source_time=history['source_time'],
                               source_ref=history.get('source_ref',''))
    except ValueError:
        pass
    if st.button('確認補製計畫', key='reissue_review', disabled=disabled):
        if not candidate or not confirmed:
            st.error('請填補製原因並確認將產生新 tracking；品項資料也必須完整。')
        else:
            st.session_state['reissue_reviewed_plan'] = candidate
    reviewed = st.session_state.get('reissue_reviewed_plan')
    if candidate and reviewed and candidate['integrity'] == reviewed['integrity'] and confirmed:
        _execute(reviewed, key='reissue', baseline=history['tracking_baseline'], confirmed=True, disabled=disabled)
    _render_resume(history['order_id'], email, disabled=disabled)


def _render_resume(order_id, email, *, disabled=False):
    if not verified_email():
        return
    if not st.checkbox('查看此訂單未完成計畫／恢復', key='reissue_resume_visible'):
        return
    try:
        from bot.shipment_ledger import configured_ledger
        from bot.shipment_service import execute_plan
        records = [r for _,r in configured_ledger().records() if r['plan']['order_id'] == order_id
                   and any(s['status'] != 'completed' for s in r['shipments'])]
        if not records:
            st.caption('沒有未完成計畫')
        for record in records:
            plan = record['plan']
            st.write(f"計畫 {plan['plan_id']}")
            for shipment in record['shipments']:
                st.write(f"{shipment['shipment_id']}｜{shipment['status']}｜{shipment.get('tracking','')}")
            unknown = any(s['status'] in {'issuing','unknown','label_created'} for s in record['shipments'])
            if unknown:
                st.warning('此計畫需管理員核對郵政結果或恢復 PDF，禁止再次製單。')
            if st.button('恢復原計畫（已製單者只回填）', key='resume_' + plan['plan_id'], disabled=disabled or unknown):
                _show_results(execute_plan(plan, requested_by=email))
    except Exception:
        st.warning('未完成明細帳無法安全讀取，請管理員核對；不可改建新計畫。')
