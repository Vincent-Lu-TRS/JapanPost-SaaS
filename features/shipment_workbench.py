"""Authenticated Streamlit surfaces for explicit split and reissue work."""
import copy
import html
import re
import time
import uuid

import pandas as pd
import streamlit as st

from pending_editor import (SHIPPING_COL, SHIPPING_OPTIONS, apply_pending_order_editor_values,
                            build_pending_item_frame, build_pending_summary_frame, compose_shipping_name,
                            country_kind, parse_shipping_name)
from postal_ui_v2 import build_v2_item_display_frame, restore_v2_item_frame
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


def _preview(plan, *, compact=False):
    if compact:
        for shipment in plan['shipments']:
            quantity = sum(item['quantity'] for item in shipment['items'])
            st.caption(f"{shipment['method']}｜{quantity} 件｜USD {shipment['total_usd']}")
        return
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


def _execute(plan, *, key, baseline=None, confirmed=False, disabled=False, compact=False):
    attempted = False
    _preview(plan, compact=compact)
    if st.button('送出拆單製單' if plan['mode'] == 'split' else '開始製作補單',
                 key=key + '_execute', disabled=disabled,
                 **({'type': 'primary', 'width': 'stretch'} if compact else {})):
        email = verified_email()
        if not email:
            st.error('請先登入授權帳號')
            return
        attempted = True
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
        finally:
            if plan['mode'] == 'reissue':
                _refresh_reissue_history(plan['order_id'])
    if not compact:
        _show_results(st.session_state.get(key + '_results', []))
    return attempted


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


def _invalidate_reissue_review():
    st.session_state.pop('reissue_reviewed_plan', None)


REISSUE_CARD_CSS = """
<style>
.st-key-reissue_workbench {
    container-name: reissue-workbench;
    container-type: inline-size;
    min-width: 0;
}
.st-key-reissue_workbench > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
    min-width: 0;
}
.st-key-reissue_actions {
    min-width: 0;
    overflow-wrap: anywhere;
}
@container reissue-workbench (max-width: 900px) {
    .st-key-reissue_workbench > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"] {
        flex-direction: column;
    }
    .st-key-reissue_workbench > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
        flex: 1 1 100%;
        width: 100%;
        min-width: 0;
    }
}
.st-key-reissue_order_card {
    container-name: reissue-order;
    container-type: inline-size;
}
@media (min-width: 641px) {
    @container reissue-order (max-width: 760px) {
        .st-key-reissue_order_card .st-key-reissue_recipient_fields [data-testid="stHorizontalBlock"],
        .st-key-reissue_order_card .st-key-reissue_address_fields [data-testid="stHorizontalBlock"] {
            flex-wrap: wrap;
        }
        .st-key-reissue_order_card .st-key-reissue_recipient_fields [data-testid="stHorizontalBlock"] > [data-testid="stColumn"],
        .st-key-reissue_order_card .st-key-reissue_address_fields [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
            flex: 1 1 calc(50% - .5rem);
            width: calc(50% - .5rem);
            min-width: min(100%, 220px);
        }
    }
}
</style>
"""


def _edit_history_row(history, *, disabled=False, usd_jpy_rate=None):
    row = pd.Series(copy.deepcopy(history['row']))
    country = str(row.get('收件人國家') or row.get('Country', ''))
    parsed = parse_shipping_name(row.get('Shipping Name') or row.get('Shipping Name_1', ''))
    kind = country_kind(country)
    with st.container(border=True, key='reissue_order_card'):
        st.markdown('<span class="postal-v2-card-marker"></span>', unsafe_allow_html=True)
        summary_slot = st.empty()
        with st.container(key='reissue_recipient_fields'):
            actions = st.columns([1.4, 1.8, 1.2] if kind else [1.4, 1.8], gap='small')
            with actions[0]:
                name = st.text_input('姓名', value=parsed['clean_name'], key='reissue_name', disabled=disabled, on_change=_invalidate_reissue_review)
            with actions[1]:
                method = st.selectbox('寄送方式', SHIPPING_OPTIONS,
                                      index=SHIPPING_OPTIONS.index(row.get(SHIPPING_COL)) if row.get(SHIPPING_COL) in SHIPPING_OPTIONS else 0,
                                      key='reissue_method', disabled=disabled, on_change=_invalidate_reissue_review)
            prc, pccc = parsed['prc_id'], parsed['pccc']
            if kind:
                with actions[2]:
                    if kind == 'china':
                        prc = st.text_input('PRC ID', value=prc, key='reissue_prc', disabled=disabled, on_change=_invalidate_reissue_review)
                    else:
                        pccc = st.text_input('PCCC', value=pccc, key='reissue_pccc', disabled=disabled, on_change=_invalidate_reissue_review)
        row['Shipping Street'] = st.text_input('完整地址', value=str(row.get('Shipping Street') or row.get('收件地址', '')),
                                               key='reissue_Shipping Street', disabled=disabled, on_change=_invalidate_reissue_review)
        with st.container(key='reissue_address_fields'):
            addresses = st.columns([1.3, 1.3, 1, 1.4], gap='small')
            for area, column, label, fallback in zip(addresses,
                    ('Shipping City','收件人洲/省','Shipping Zip','Shipping Phone'),
                    ('城市','州／省','郵遞區號','電話'), ('城市','State','郵遞區號','電話')):
                with area:
                    row[column] = st.text_input(label, value=str(row.get(column) or row.get(fallback, '')),
                                                 key='reissue_' + column, disabled=disabled, on_change=_invalidate_reissue_review)
        edited = st.data_editor(build_v2_item_display_frame(build_pending_item_frame(row)),
                                hide_index=True, width='stretch', num_rows='fixed',
                                disabled=True if disabled else ['No.'], key='reissue_items', on_change=_invalidate_reissue_review,
                                column_config={
                                    'No.': st.column_config.TextColumn('No.', width=54),
                                    'Description': st.column_config.TextColumn('品名 / 描述', width='large'),
                                    'HSCode': st.column_config.TextColumn('HS Code', width=120),
                                    'Value': st.column_config.TextColumn('申報價值（USD）', width=120),
                                    'Quantity': st.column_config.TextColumn('數量', width=90)})
        row['Shipping Name'] = compose_shipping_name(name, country, prc, pccc)
        summary = pd.DataFrame([{'Name': row['Shipping Name'], 'TransType': method}])
        applied = apply_pending_order_editor_values(pd.DataFrame([row]), summary,
                                                    {0: restore_v2_item_frame(edited)},
                                                    usd_jpy_rate=usd_jpy_rate).iloc[0]
        summary_row = build_pending_summary_frame(pd.DataFrame([applied])).iloc[0]
        with summary_slot.container():
            for area, label, value in zip(st.columns([1.75, 2.45, .86, .86], gap='small'),
                    ('Order No.', 'Country', 'USD', 'JPY'),
                    (history['order_id'], summary_row['Country'], summary_row['TotalValue(USD)'], summary_row['TotalValue(JPY)'])):
                style = 'order' if label == 'Order No.' else 'country' if label == 'Country' else 'standard'
                with area:
                    st.markdown(f'<div class="native-info native-info-{style}">'
                                f'<span class="native-info-label">{html.escape(label)}</span>'
                                f'<span class="native-info-value">{html.escape(str(value))}</span></div>',
                                unsafe_allow_html=True)
    return applied


def _refresh_reissue_history(order_id):
    # Only explicit execution/recovery triggers this refresh; reruns use the
    # lookup snapshot. The execution service still checks authoritative state.
    st.session_state.pop('reissue_reviewed_plan', None)
    st.session_state['reissue_nonce'] = uuid.uuid4().hex
    try:
        from bot.shipment_service import lookup_history
        st.session_state['reissue_history'] = lookup_history(order_id)
        st.session_state.pop('reissue_refresh_warning', None)
    except Exception:
        st.session_state.pop('reissue_history', None)
        message = '執行後狀態無法安全更新；請重新查詢原訂單，核對未完成計畫後再操作。'
        st.session_state['reissue_refresh_warning'] = message
        st.warning(message)


def _render_reprint(history, *, disabled=False):
    with st.expander('既有 PDF／重印'):
        for index, shipment in enumerate(history['shipments'], 1):
            file_id = shipment.get('drive_file_id', '')
            if re.fullmatch(r'[A-Za-z0-9_-]+', file_id):
                st.link_button(f'開啟既有 PDF {index}（重印）', f'https://drive.google.com/file/d/{file_id}/view')
        if st.button('尋找舊 PDF（只重印）', key='reissue_find_pdf', disabled=disabled):
            from bot.drive import DRIVE_FOLDER_ID, list_drive_files
            try:
                found = [file for file in list_drive_files(DRIVE_FOLDER_ID, name_prefix=history['order_id'])
                         if '_' + history['order_id'] + '_' in file.get('name','')]
                st.session_state['reissue_old_pdfs'] = [{'id': file['id'], 'name': file.get('name','PDF')} for file in found]
            except Exception:
                st.session_state.pop('reissue_old_pdfs', None)
                st.warning('舊 PDF 無法讀取，請到受限 Drive 資料夾確認。')
        if 'reissue_old_pdfs' in st.session_state and not st.session_state['reissue_old_pdfs']:
            st.info('找不到此訂單的舊 PDF；不會因此建立新運單。')
        for file in st.session_state.get('reissue_old_pdfs', []):
            if re.fullmatch(r'[A-Za-z0-9_-]+', file['id']):
                st.link_button(file['name'], f'https://drive.google.com/file/d/{file["id"]}/view')


def render_reissue_tab(*, disabled=False, usd_jpy_rate=None):
    email = verified_email()
    if not email:
        st.caption('請先登入授權帳號')
        return
    query = st.columns([3, 1], gap='small', vertical_alignment='bottom')
    with query[0]:
        order = st.text_input('完整訂單編號', key='reissue_order_query', max_chars=80, disabled=disabled)
    with query[1]:
        search = st.button('查詢歷史訂單', key='reissue_query', disabled=disabled)
    if search:
        last = float(st.session_state.get('reissue_last_query', 0))
        if time.monotonic() - last < 2:
            st.warning('請稍候再查詢')
        else:
            st.session_state['reissue_last_query'] = time.monotonic()
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
    for key in ('reissue_resume_warning', 'reissue_refresh_warning'):
        if st.session_state.get(key):
            st.warning(st.session_state[key])
    if st.session_state.get('reissue_resume_error'):
        st.error(st.session_state['reissue_resume_error'])
    history = st.session_state.get('reissue_history')
    if not history or order.strip() != history['order_id']:
        _show_results([r for r in st.session_state.get('reissue_results', [])
                       if r['order_id'] == order.strip()])
        return
    st.markdown(REISSUE_CARD_CSS, unsafe_allow_html=True)
    with st.container(key='reissue_workbench'):
        left_panel, right_panel = st.columns([3.05, 1.2], gap='medium', vertical_alignment='top')
        with left_panel:
            if history['tracking_baseline']:
                if not usd_jpy_rate:
                    st.warning('暫時無法取得 USD/JPY 匯率；若編輯 Value 或 Quantity，'
                               'TotalValue(JPY) 會保留來源預設值。')
                row = _edit_history_row(history, disabled=disabled, usd_jpy_rate=usd_jpy_rate)
            else:
                st.warning('此訂單沒有已確認的舊運單，只能恢復原計畫或由管理員核對。')
        with right_panel:
            with st.container(border=True, key='reissue_actions'):
                st.markdown('<span class="postal-v2-operation-panel"></span>', unsafe_allow_html=True)
                if history['tracking_baseline']:
                    st.markdown('<div class="postal-v2-panel-heading">補單操作</div>', unsafe_allow_html=True)
                    if len(history['tracking_baseline']) == 1:
                        original_tracking = history['tracking_baseline'][0]
                        st.caption('關聯原運單：' + original_tracking)
                    else:
                        original_tracking = st.selectbox('關聯原運單', history['tracking_baseline'],
                                                          key='reissue_original_tracking', disabled=disabled, on_change=_invalidate_reissue_review)
                    confirmed = st.checkbox('我已核對', help='這次將產生新 tracking，並保留舊運單。請核對完整收件與品項資料。',
                                             key='reissue_new_tracking', disabled=disabled, on_change=_invalidate_reissue_review)
                    candidate = None
                    validation_error = ''
                    try:
                        from bot.shipment_service import validate_recipient
                        validate_recipient(row)
                        items = source_items(row)
                        candidate = build_plan(row, [{'method': row[SHIPPING_COL],
                                                      'quantities': {i['id']: i['quantity'] for i in items}}],
                                               mode='reissue', original_tracking=original_tracking,
                                               nonce=st.session_state['reissue_nonce'], source_row=history['row'],
                                               source_kind=history['source_kind'], source_time=history['source_time'],
                                               source_ref=history.get('source_ref',''))
                    except ValueError as error:
                        validation_error = str(error)
                    if st.button('確認補單資料', key='reissue_review', disabled=disabled, width='stretch'):
                        if not candidate or not confirmed:
                            st.error(validation_error or '請確認將產生新 tracking，並核對完整收件與品項資料。')
                        else:
                            st.session_state['reissue_reviewed_plan'] = candidate
                    reviewed = st.session_state.get('reissue_reviewed_plan')
                    if candidate and reviewed and candidate['integrity'] == reviewed['integrity'] and confirmed:
                        _execute(reviewed, key='reissue', baseline=history['tracking_baseline'],
                                 confirmed=confirmed, disabled=disabled, compact=True)
                    elif reviewed:
                        st.caption('補製資料或確認狀態已變更，請重新確認補單資料。')
                # Execution can create an unfinished record even when the initial
                # query had none. Use the explicit post-operation refresh now.
                history = st.session_state.get('reissue_history')
                if history:
                    _render_resume(history, email, disabled=disabled)
                    _render_reprint(history, disabled=disabled)
    _show_results(st.session_state.get('reissue_results', []))


def _render_resume(history, email, *, disabled=False):
    if not verified_email() or not history.get('unfinished_records'):
        return
    with st.expander('未完成計畫／恢復'):
        for record in history['unfinished_records']:
            plan = record['plan']
            st.write(f"計畫 {plan['plan_id']}")
            for shipment in record['shipments']:
                st.write(f"{shipment['shipment_id']}｜{shipment['status']}｜{shipment.get('tracking','')}")
            unknown = any(s['status'] in {'issuing','unknown','label_created'} for s in record['shipments'])
            if unknown:
                st.warning('此計畫需管理員核對郵政結果或恢復 PDF，禁止再次製單。')
            st.button('恢復原計畫（已製單者只回填）', key='resume_' + plan['plan_id'],
                      disabled=disabled or unknown, on_click=_resume_plan,
                      args=(copy.deepcopy(plan), email))


def _resume_plan(plan, email):
    # Widget callbacks run before the page is drawn, so status, actions and
    # results all use the refreshed ledger snapshot in the same interaction.
    current_email = verified_email()
    if not current_email or current_email != email:
        for key in ('reissue_history', 'reissue_reviewed_plan', 'reissue_results'):
            st.session_state.pop(key, None)
        st.session_state['reissue_resume_warning'] = '登入狀態已變更，請重新登入並查詢原訂單後再恢復。'
        return
    st.session_state.pop('reissue_resume_error', None)
    st.session_state.pop('reissue_resume_warning', None)
    try:
        from bot.shipment_service import execute_plan
        st.session_state['reissue_results'] = execute_plan(copy.deepcopy(plan), requested_by=current_email)
    except ValueError as error:
        st.session_state['reissue_resume_error'] = str(error)
    except Exception:
        st.session_state['reissue_resume_warning'] = '恢復已停止；請核對原計畫狀態，不可改建新計畫。'
    finally:
        _refresh_reissue_history(plan['order_id'])
