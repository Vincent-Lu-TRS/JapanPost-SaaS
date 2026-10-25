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
from .shipment_busy_ui import render_running_guard


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


def _show_reissue_results(results):
    if not results:
        return
    statuses = {result.get('status', '') for result in results}
    if statuses == {'completed'}:
        st.success('補單製作完成。再次補製請重新查詢訂單。')
    elif statuses & {'issuing', 'unknown', 'label_created'}:
        st.warning('運單結果需要人工核對，請勿重新製單。')
    else:
        st.warning('補單尚有未完成的回填，請恢復原計畫；不要重新製單。')
    for index, result in enumerate(results, 1):
        if result.get('status') != 'completed' and result.get('tracking'):
            st.caption('追蹤號碼：' + str(result['tracking']))
        file_id = result.get('drive_file_id', '')
        if re.fullmatch(r'[A-Za-z0-9_-]+', file_id):
            label = '開啟本次補單' if len(results) == 1 else f'開啟本次補單（包裹 {index}）'
            st.link_button(label, f'https://drive.google.com/file/d/{file_id}/view')


def _render_reissue_busy(guard, order_id, stage):
    render_running_guard(guard, title='補單製作中', active_order=order_id,
                         active_stage=stage, block_background=True)


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


SUMMARY_COLUMN_WIDTHS = [.58, 1.75, 2.45, .86, .86, 1.0]


def action_column_widths(kind):
    return [1.2, 1.8, 1.8, 1.1, 1.2, .7] if kind in {'china','korea'} else [1.42, 1.8, 1.8, 1.25, .9]


def split_eligible(row):
    try:
        return sum(item['quantity'] for item in source_items(row)) >= 2
    except ValueError:
        return False


def effective_split_enabled(row, *, key):
    # Conflicting split + additional choices stay out of normal submission too.
    return bool(split_eligible(row) and st.session_state.get(key + '_enabled', False))


def render_split_toggle(row, *, key, disabled=False):
    if not split_eligible(row):
        st.session_state[key + '_enabled'] = False
        st.session_state.pop(key + '_reviewed_plan', None)
        return False
    return st.toggle('拆分包裹', key=key + '_enabled', disabled=disabled)


def _render_package_editor(row, *, key, disabled=False):
    items = source_items(row)
    count = int(st.number_input('包裹數', min_value=2, max_value=20, value=2,
                                step=1, key=key + '_count', disabled=disabled))
    packages = []
    valid = True
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
        if not any(quantities.values()):
            valid = False
            st.warning(f'包裹{index + 1}中至少要有一項數量為1以上')
    st.caption('每項分配合計必須等於原數量；每包至少一項。相同寄送方式可分成多包。')
    return packages, valid


def render_split_planner(row, *, source_row=None, key='split', extra_selected=False, disabled=False, header_rendered=False):
    if not verified_email():
        return
    enabled = (st.session_state.get(key + '_enabled', False) if header_rendered
               else render_split_toggle(row, key=key, disabled=disabled))
    if not enabled or not split_eligible(row):
        return
    if extra_selected:
        st.error('整單追加會複製完整內容；拆單請先把「追加製作」設為「無」。')
        return
    source_row = source_row if source_row is not None else row
    candidate = None
    try:
        items, original = source_items(row), source_items(source_row)
        if [(i['source_index'], i['quantity']) for i in items] != [(i['source_index'], i['quantity']) for i in original]:
            raise ValueError('拆單需保留來源各項總數量；請恢復數量後再分配包裹')
        packages, valid = _render_package_editor(row, key=key, disabled=disabled)
        if valid:
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
.st-key-reissue_query_bar [data-testid="stTextInput"] {
    grid-template-columns: max-content minmax(0, 1fr);
}
.st-key-reissue_query_bar [data-testid="stTextInput"] label {
    white-space: nowrap;
}
.st-key-reissue_query_bar [data-testid="stHorizontalBlock"] {
    flex-wrap: nowrap;
}
.st-key-reissue_query_bar [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:first-child {
    flex: 1 1 0%;
    width: 0;
    min-width: 0;
}
.st-key-reissue_query_bar [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:last-child {
    flex: 0 0 88px;
    width: 88px;
    min-width: 0;
}
:is(.st-key-pending_workbench, .st-key-reissue_workbench) {
    container-name: shipment-workbench;
    container-type: inline-size;
    min-width: 0;
}
:is(.st-key-pending_workbench, .st-key-reissue_workbench) > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
    min-width: 0;
}
.st-key-reissue_actions {
    min-width: 0;
    overflow-wrap: anywhere;
}
.st-key-reissue_actions .postal-v2-panel-heading {
    margin-bottom: 8px;
    font-weight: 750;
}
.st-key-reissue_actions .postal-v2-panel-heading strong {
    font-weight: inherit;
}
@container shipment-workbench (max-width: 900px) {
    :is(.st-key-pending_workbench, .st-key-reissue_workbench) > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"] {
        flex-direction: column;
    }
    :is(.st-key-pending_workbench, .st-key-reissue_workbench) > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
        flex: 1 1 100%;
        width: 100%;
        min-width: 0;
    }
}
:is(.st-key-reissue_order_card, [class*="st-key-pending_order_card_"]) {
    container-name: shipment-order;
    container-type: inline-size;
    min-width: 0;
}
@media (min-width: 641px) {
    @container shipment-order (max-width: 760px) {
        :is(.st-key-reissue_recipient_fields, .st-key-reissue_address_fields, [class*="st-key-pending_recipient_fields_"]) [data-testid="stHorizontalBlock"] {
            flex-wrap: wrap;
        }
        :is(.st-key-reissue_recipient_fields, .st-key-reissue_address_fields, [class*="st-key-pending_recipient_fields_"]) [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
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
            actions = st.columns(action_column_widths(kind), gap='small', vertical_alignment='center')
            with actions[0]:
                name = st.text_input('姓名', value=parsed['clean_name'], key='reissue_name', disabled=disabled, on_change=_invalidate_reissue_review)
            with actions[1]:
                method = st.selectbox('寄送方式', SHIPPING_OPTIONS,
                                      index=SHIPPING_OPTIONS.index(row.get(SHIPPING_COL)) if row.get(SHIPPING_COL) in SHIPPING_OPTIONS else 0,
                                      key='reissue_method', disabled=disabled, on_change=_invalidate_reissue_review)
            with actions[2]:
                extra = st.selectbox('追加製作', ['無'] + SHIPPING_OPTIONS, key='reissue_extra_method', disabled=disabled)
            prc, pccc = parsed['prc_id'], parsed['pccc']
            if kind:
                with actions[3]:
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
        try:
            applied = apply_pending_order_editor_values(pd.DataFrame([row]), summary,
                                                        {0: restore_v2_item_frame(edited)},
                                                        usd_jpy_rate=usd_jpy_rate).iloc[0]
            source_items(applied)
            summary_row = build_pending_summary_frame(pd.DataFrame([applied])).iloc[0]
        except (ValueError, OverflowError) as error:
            # Keep the real editor and its invalid text visible. No corrected
            # fallback row may flow into a plan, and a discarded click cannot
            # restart when the operator later fixes the cell.
            applied = None
            if not st.session_state.get('reissue_submission'):
                st.session_state.pop('reissue_intent', None)
            message = (str(error) if isinstance(error, ValueError) else
                       '品項金額或數量超出可計算範圍，請修正後重新按開始。')
            st.error(message)
            summary_row = {'Country':country, 'TotalValue(USD)':'—', 'TotalValue(JPY)':'—'}
        with summary_slot.container():
            columns = st.columns(SUMMARY_COLUMN_WIDTHS[1:], gap='small', vertical_alignment='center')
            with columns[-1]:
                if applied is None:
                    # The header widget has not been instantiated in this run.
                    st.session_state['reissue_split_enabled'] = False
                    st.session_state.pop('reissue_split_reviewed_plan', None)
                    split_active = False
                else:
                    split_active = render_split_toggle(applied, key='reissue_split', disabled=disabled)
            for area, label, value in zip(columns[:4],
                    ('Order No.', 'Country', 'USD', 'JPY'),
                    (history['order_id'], summary_row['Country'], summary_row['TotalValue(USD)'], summary_row['TotalValue(JPY)'])):
                style = 'order' if label == 'Order No.' else 'country' if label == 'Country' else 'standard'
                with area:
                    st.markdown(f'<div class="native-info native-info-{style}">'
                                f'<span class="native-info-label">{html.escape(label)}</span>'
                                f'<span class="native-info-value">{html.escape(str(value))}</span></div>',
                                unsafe_allow_html=True)
        packages = None
        if split_active:
            if extra != '無':
                st.error('整單追加會複製完整內容；拆單請先把「追加製作」設為「無」。')
            else:
                packages, _ = _render_package_editor(applied, key='reissue_split', disabled=disabled)
    return applied, packages, split_active, extra


def _refresh_reissue_history(order_id):
    # Only explicit execution/recovery triggers this refresh; reruns use the
    # lookup snapshot. The execution service still checks authoritative state.
    st.session_state.pop('reissue_reviewed_plan', None)
    try:
        from bot.shipment_service import lookup_history
        st.session_state['reissue_history'] = lookup_history(order_id)
        st.session_state.pop('reissue_refresh_warning', None)
    except Exception:
        st.session_state.pop('reissue_history', None)
        message = '執行後狀態無法安全更新；請重新查詢原訂單，核對未完成計畫後再操作。'
        st.session_state['reissue_refresh_warning'] = message
        st.warning(message)


def _cache_original_pdf_urls(history, actor):
    try:
        from bot.drive import resolve_original_label_urls
        urls = resolve_original_label_urls(history['order_id'], history['tracking_baseline'],
                                           shipments=history.get('shipments', ()))
    except Exception:
        urls = {}
    st.session_state['reissue_pdf_cache'] = {'order_id':history['order_id'], 'actor':actor, 'urls':urls}


def _reissue_candidate(row, packages, history, original_tracking, extra, split_active):
    from bot.shipment_service import validate_recipient
    validate_recipient(row)
    if split_active and extra != '無':
        raise ValueError('整單追加與拆單不能同時使用')
    items = source_items(row)
    if not split_active:
        packages = [{'method':row[SHIPPING_COL], 'quantities':{i['id']:i['quantity'] for i in items}}]
        if extra != '無' and extra != row[SHIPPING_COL]:
            packages.append({'method':extra, 'quantities':dict(packages[0]['quantities'])})
    return build_plan(row, packages, mode='reissue', original_tracking=original_tracking,
                      nonce=st.session_state['reissue_nonce'], source_row=history['row'],
                      source_kind=history['source_kind'], source_time=history['source_time'],
                      source_ref=history.get('source_ref',''),
                      copy_type='whole_order' if not split_active and len(packages) == 2 else '')


# Streamlit 1.56.0's native DataEditor syncEditState is debounced by 150 ms.
# A browser fragment tick is enqueued after that commit on the same WebSocket.
# A full script rerun is never treated as that acknowledgment.
REISSUE_COMMIT_FENCE_SECONDS = .25
REISSUE_COMMIT_FENCE_TIMEOUT = 10


def _accept_reissue_commit_ack(actor, nonce, token, *, context=None):
    intent = st.session_state.get('reissue_intent')
    if not intent or intent.get('stage') != 'waiting':
        return False
    if (intent.get('actor'), intent.get('nonce'), intent.get('token')) != (actor, nonce, token):
        return False
    if (verified_email() != actor or st.session_state.get('reissue_query_actor') != actor
            or st.session_state.get('reissue_nonce') != nonce):
        return False
    if context is None:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        context = get_script_run_ctx()
    current = getattr(context, 'current_fragment_id', None)
    if not current or current not in (getattr(context, 'fragment_ids_this_run', None) or []):
        return False
    if time.monotonic() - intent['created_at'] > REISSUE_COMMIT_FENCE_TIMEOUT:
        intent['stage'] = 'failed'
        st.session_state['reissue_submission_error'] = '編輯同步確認逾時，未送出製單；請重新查詢原訂單。'
        return False
    intent['stage'] = 'ready'
    return True


def _render_reissue_commit_fence(intent):
    actor, nonce, token = intent['actor'], intent['nonce'], intent['token']
    def commit_tick():
        current = st.session_state.get('reissue_intent', {})
        if (current.get('token') != token or current.get('nonce') != nonce
                or verified_email() != actor or st.session_state.get('reissue_nonce') != nonce):
            st.rerun(scope='app')
        if _accept_reissue_commit_ack(actor, nonce, token):
            st.rerun(scope='app')
        if st.session_state.get('reissue_intent', {}).get('stage') == 'failed':
            st.rerun(scope='app')
        if st.session_state.get('reissue_submission_error'):
            st.error(st.session_state['reissue_submission_error'])
    # Streamlit 1.56 hashes __qualname__ into fragment_id. Old draft timers
    # cannot acknowledge a later click that happens to occupy the same UI slot.
    commit_tick.__qualname__ = '_reissue_commit_fence_' + token
    st.fragment(run_every=REISSUE_COMMIT_FENCE_SECONDS)(commit_tick)()


def _submit_reissue(plan, history, actor, *, guard=None):
    current_actor = verified_email()
    if not current_actor or current_actor != actor or st.session_state.get('reissue_query_actor') != current_actor:
        st.error('登入狀態已變更，請重新登入並查詢原訂單。')
        return
    if st.session_state.get('reissue_submission'):
        return
    submission = {'plan':copy.deepcopy(plan), 'baseline':copy.deepcopy(history['tracking_baseline']),
                  'verified_actor':current_actor}
    st.session_state['reissue_submission'] = submission
    guard = guard if guard is not None else st.empty()
    _render_reissue_busy(guard, plan['order_id'], '製作運單與回填資料')
    try:
        from bot.shipment_service import execute_plan
        with st.spinner('正在製作補單；中斷後請以原計畫恢復。'):
            st.session_state['reissue_results'] = execute_plan(copy.deepcopy(submission['plan']),
                requested_by=submission['verified_actor'], tracking_baseline=copy.deepcopy(submission['baseline']),
                confirmed_new_tracking=True)
    except ValueError as error:
        st.session_state['reissue_submission_error'] = str(error)
    except Exception as error:
        code = getattr(error, 'reason_code', 'shipment_safety_unavailable')
        st.session_state['reissue_submission_error'] = f'工作已停止，請以原計畫恢復或交管理員核對。計畫 {plan["plan_id"]}｜{code}'
    finally:
        try:
            _refresh_reissue_history(plan['order_id'])
        finally:
            guard.empty()


def render_reissue_tab(*, disabled=False, usd_jpy_rate=None):
    email = verified_email()
    if not email:
        st.caption('請先登入授權帳號')
        return
    guard = st.empty()
    st.markdown(REISSUE_CARD_CSS, unsafe_allow_html=True)
    with st.container(key='reissue_query_bar', width=440):
        query = st.columns([4, 1], gap='small', vertical_alignment='center')
        with query[0]:
            order = st.text_input('完整訂單編號', key='reissue_order_query', max_chars=80, disabled=disabled)
        with query[1]:
            search = st.button('查詢訂單', key='reissue_query', disabled=disabled)
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
                st.session_state['reissue_query_actor'] = email
                _cache_original_pdf_urls(st.session_state['reissue_history'], email)
            except ValueError as error:
                st.error(str(error))
            except Exception:
                st.error('歷史資料或加密明細帳無法安全讀取；請聯絡管理員。')
    if st.session_state.get('reissue_submission_error'):
        st.error(st.session_state['reissue_submission_error'])
    for key in ('reissue_resume_warning', 'reissue_refresh_warning'):
        if st.session_state.get(key):
            st.warning(st.session_state[key])
    if st.session_state.get('reissue_resume_error'):
        st.error(st.session_state['reissue_resume_error'])
    history = st.session_state.get('reissue_history')
    if st.session_state.get('reissue_query_actor') not in (None, email):
        st.warning('登入狀態已變更，請重新查詢原訂單。')
        return
    if not history or order.strip() != history['order_id']:
        results = [r for r in st.session_state.get('reissue_results', []) if r['order_id'] == order.strip()]
        if results:
            with st.columns([3.05, 1.2], gap='medium', vertical_alignment='top')[1]:
                with st.container(border=True, key='reissue_actions'):
                    st.markdown('<div class="postal-v2-panel-heading"><strong>補單操作</strong></div>', unsafe_allow_html=True)
                    _show_reissue_results(results)
        return
    with st.container(key='reissue_workbench'):
        left_panel, right_panel = st.columns([3.05, 1.2], gap='medium', vertical_alignment='top')
        with left_panel:
            if history['tracking_baseline']:
                if not usd_jpy_rate:
                    st.warning('暫時無法取得 USD/JPY 匯率；若編輯 Value 或 Quantity，'
                               'TotalValue(JPY) 會保留來源預設值。')
                row, packages, split_active, extra = _edit_history_row(history, disabled=disabled, usd_jpy_rate=usd_jpy_rate)
            else:
                st.warning('此訂單沒有已確認的舊運單，只能恢復原計畫或由管理員核對。')
        with right_panel:
            with st.container(border=True, key='reissue_actions'):
                st.markdown('<span class="postal-v2-operation-panel"></span>', unsafe_allow_html=True)
                if history['tracking_baseline']:
                    baseline = history['tracking_baseline']
                    original_tracking = st.session_state.get('reissue_original_tracking', baseline[0])
                    if original_tracking not in baseline:
                        original_tracking = baseline[0]
                    cached = st.session_state.get('reissue_pdf_cache', {})
                    urls = (cached.get('urls', {}) if cached.get('actor') == email
                            and cached.get('order_id') == history['order_id'] else {})
                    available = [tracking for tracking in baseline if urls.get(tracking)]
                    if available and original_tracking not in available:
                        original_tracking = available[0]
                    # Preserve the current association when no PDF control is
                    # rendered; the complete safety baseline is never filtered.
                    st.session_state['reissue_original_tracking'] = original_tracking
                    if available:
                        if len(available) == 1:
                            st.caption('關聯原運單：' + original_tracking)
                        else:
                            original_tracking = st.selectbox('關聯原運單', available,
                                                              key='reissue_original_tracking', disabled=disabled)
                        st.link_button('開啟原運單', urls[original_tracking])
                    st.markdown('<div class="postal-v2-panel-heading"><strong>補單操作</strong></div>', unsafe_allow_html=True)
                    split_invalid = row is None
                    if split_active:
                        try:
                            _reissue_candidate(row, packages, history, original_tracking, extra, split_active)
                        except ValueError as error:
                            split_invalid = True
                            if not packages or all(any(p['quantities'].values()) for p in packages):
                                st.warning(str(error))
                    submission = st.session_state.get('reissue_submission')
                    intent = st.session_state.get('reissue_intent')
                    if st.button('開始製作補單', key='reissue_execute', type='primary', width='stretch',
                                 disabled=disabled or bool(submission) or bool(intent) or split_invalid):
                        if not st.session_state.get('reissue_submission') and not st.session_state.get('reissue_intent'):
                            st.session_state.pop('reissue_submission_error', None)
                            st.session_state['reissue_intent'] = {'actor':email,
                                'nonce':st.session_state['reissue_nonce'], 'token':uuid.uuid4().hex,
                                'created_at':time.monotonic(), 'stage':'waiting'}
                            st.rerun()
                    if intent and intent.get('stage') == 'waiting':
                        _render_reissue_busy(guard, history['order_id'], '同步最後編輯內容')
                        st.caption('正在同步最後編輯內容…')
                        _render_reissue_commit_fence(intent)
                    elif intent and intent.get('stage') == 'ready' and not submission:
                        if (intent['actor'] == verified_email() == email
                                and intent['nonce'] == st.session_state.get('reissue_nonce')
                                and time.monotonic() - intent['created_at'] <= REISSUE_COMMIT_FENCE_TIMEOUT):
                            try:
                                plan = _reissue_candidate(row, packages, history, original_tracking, extra, split_active)
                            except ValueError as error:
                                st.session_state['reissue_submission_error'] = str(error)
                                st.session_state.pop('reissue_intent', None)
                            else:
                                _submit_reissue(plan, history, email, guard=guard)
                            st.rerun()
                        else:
                            intent['stage'] = 'failed'
                            st.session_state['reissue_submission_error'] = '編輯同步確認已失效，未送出製單；請重新查詢原訂單。'
                            st.rerun()
                # Execution can create an unfinished record even when the initial
                # query had none. Use the explicit post-operation refresh now.
                history = st.session_state.get('reissue_history')
                if history:
                    _render_resume(history, email, disabled=disabled, guard=guard)
                _show_reissue_results([r for r in st.session_state.get('reissue_results', [])
                                       if r['order_id'] == order.strip()])


def _render_resume(history, email, *, disabled=False, guard=None):
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
                      args=(copy.deepcopy(plan), email), kwargs={'guard':guard})


def _resume_plan(plan, email, *, guard=None):
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
    guard = guard if guard is not None else st.empty()
    _render_reissue_busy(guard, plan['order_id'], '恢復原計畫與回填資料')
    try:
        from bot.shipment_service import execute_plan
        st.session_state['reissue_results'] = execute_plan(copy.deepcopy(plan), requested_by=current_email)
    except ValueError as error:
        st.session_state['reissue_resume_error'] = str(error)
    except Exception:
        st.session_state['reissue_resume_warning'] = '恢復已停止；請核對原計畫狀態，不可改建新計畫。'
    finally:
        try:
            _refresh_reissue_history(plan['order_id'])
        finally:
            guard.empty()
