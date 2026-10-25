"""Immutable shipping plans; commercial order IDs are never rewritten."""
import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import pandas as pd
from pending_editor import SHIPPING_COL, SHIPPING_OPTIONS, build_pending_item_frame

ORDER = '注文番号(貼上原始資料)'
BUSINESS_COLUMNS = (
    ORDER, 'Shipping Name', 'Shipping Name_1', '收件人國家', 'Country',
    'Shipping Street', '收件地址', 'Shipping City', '城市', '收件人洲/省',
    'State', 'Shipping Province Name', 'Province', 'Shipping Zip', '郵遞區號',
    'Shipping Phone', '電話', 'Shipping Postal Code', '郵便番号', SHIPPING_COL, '郵局內容物', '郵局申告金額(USD)',
    '數量集合', '数量', '訂單合計申告金額(JPY)',
) + tuple(f'{prefix}{i}' for i in range(1, 11) for prefix in ('內容物', '申告金額', '數量', 'HSCode'))


def clean(value):
    return '' if value is None or pd.isna(value) else str(value).strip()


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def business_snapshot(row):
    return {key: clean(row.get(key, '')) for key in BUSINESS_COLUMNS}


def source_fingerprint(row):
    return digest(business_snapshot(row))


def integer(value):
    if isinstance(value, bool):
        raise ValueError('數量必須為非負整數')
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or number > 1000000 or number != number.to_integral_value():
            raise ValueError('數量必須為非負整數')
        return int(number)
    except (InvalidOperation, TypeError):
        raise ValueError('數量必須為非負整數') from None


def source_items(row):
    for key, value in row.items():
        match = re.fullmatch(r'(?:內容物|申告金額|數量|HSCode)(\d+)', str(key))
        if match and int(match.group(1)) > 10 and clean(value):
            raise ValueError('內容物超過10項，不可截斷製單')
    if clean(row.get('內容物1')) and clean(row.get('郵局內容物')) and clean(row.get('內容物1')) != clean(row.get('郵局內容物')):
        raise ValueError('新舊內容物欄位矛盾，請先核對')
    items = []
    identity = clean(row.get('_source_row_number')) or clean(row.get(ORDER))
    for _, item in build_pending_item_frame(pd.Series(row)).iterrows():
        index = clean(item['Content'])
        if not clean(item['Description']):
            continue
        quantity = integer(item['Quantity'])
        try:
            value = Decimal(clean(item['Value']).replace(',', ''))
        except InvalidOperation:
            raise ValueError('申告單價格式錯誤') from None
        if quantity <= 0 or not value.is_finite() or value <= 0:
            raise ValueError('原品項數量與申告單價必須大於零')
        items.append({'id': digest([identity, index])[:24], 'source_index': index,
                      'description': clean(item['Description']), 'value': format(value, 'f'),
                      'quantity': quantity, 'hs_code': clean(item['HSCode'])})
    if not items:
        raise ValueError('沒有可分配的原始品項')
    return items


def _seal(plan):
    return digest({key: value for key, value in plan.items() if key != 'integrity'})


def build_plan(row, packages, *, mode='split', reason='', original_tracking='', nonce='',
               source_kind='pending', source_time='', source_row=None, source_ref='', copy_type=''):
    if mode not in {'split', 'reissue'}:
        raise ValueError('工作模式無效')
    if copy_type and (copy_type != 'whole_order' or mode != 'reissue' or len(packages) != 2):
        raise ValueError('整單追加只限補製兩包完整副本')
    snapshot = business_snapshot(row)
    if not snapshot[ORDER]:
        raise ValueError('缺少原始訂單編號')
    if mode == 'reissue' and (not original_tracking.strip() or not nonce):
        raise ValueError('補製需原運單及新工作識別')
    items = source_items(row)
    totals = {item['id']: 0 for item in items}
    shipments = []
    if not packages or len(packages) > 20:
        raise ValueError('包裹數必須介於1至20')
    for package_index, package in enumerate(packages, 1):
        method = package.get('method')
        if method not in SHIPPING_OPTIONS:
            raise ValueError('運送方式無效')
        quantities = package.get('quantities', {})
        if set(quantities) - set(totals):
            raise ValueError('分配含未知原始品項')
        assigned = []
        for item in items:
            quantity = integer(quantities.get(item['id'], 0))
            totals[item['id']] += quantity
            if quantity:
                assigned.append({**item, 'quantity': quantity})
        if not assigned:
            raise ValueError(f'包裹{package_index}中至少要有一項數量為1以上')
        amount = sum(Decimal(i['value']) * i['quantity'] for i in assigned)
        shipments.append({'method': method, 'items': assigned, 'total_usd': f'{amount:.2f}'})
    if copy_type:
        if len({s['method'] for s in shipments}) != len(shipments) or any(s['items'] != items for s in shipments):
            raise ValueError('整單追加需不同寄送方式，且每包保留全部品項與完整數量')
    elif any(totals[i['id']] != i['quantity'] for i in items):
        raise ValueError('每項分配合計必須等於原始數量，不可超配或漏配')
    source = business_snapshot(source_row if source_row is not None else row)
    identity = {'source': source, 'payload': snapshot, 'shipments': shipments,
                'mode': mode, 'reason': reason.strip(), 'original_tracking': original_tracking.strip(),
                'nonce': nonce}
    if copy_type:
        identity['copy_type'] = copy_type
    plan_id = digest(identity)
    for index, shipment in enumerate(shipments):
        shipment['shipment_id'] = digest([plan_id, index])
    plan = {**identity, 'source_snapshot': source, 'source_fingerprint': digest(source),
            'plan_id': plan_id, 'plan_version': 2 if copy_type else 1, 'order_id': snapshot[ORDER],
            'source_kind': source_kind, 'source_time': source_time, 'source_ref': source_ref,
            'shipments': shipments}
    plan['integrity'] = _seal(plan)
    return copy.deepcopy(plan)


def validate_plan(plan):
    if plan.get('integrity') != _seal(plan):
        raise ValueError('計畫內容已變更，請重新確認')
    if plan.get('plan_version') != (2 if plan.get('copy_type') else 1):
        raise ValueError('計畫版本無效')
    # Rebuild from authoritative original item positions to validate all quantities.
    row = dict(plan['payload'])
    original_items = source_items(row)
    id_by_index = {i['source_index']: i['id'] for i in original_items}
    packages = [{'method': s['method'], 'quantities': {
        id_by_index[i['source_index']]: i['quantity'] for i in s['items']
    }} for s in plan['shipments']]
    build_plan(row, packages, mode=plan['mode'], reason=plan['reason'],
               original_tracking=plan['original_tracking'], nonce=plan['nonce'], copy_type=plan.get('copy_type', ''))


def plan_frame(plan):
    validate_plan(plan)
    rows = []
    for shipment in plan['shipments']:
        row = dict(plan['payload'])
        for index in range(1, 11):
            for prefix in ('內容物', '申告金額', '數量', 'HSCode'):
                row[f'{prefix}{index}'] = ''
        # Clear legacy fallbacks so omitted source items cannot reappear.
        for key in ('郵局內容物', '數量集合', '数量'):
            row[key] = ''
        for index, item in enumerate(shipment['items'], 1):
            row.update({f'內容物{index}': item['description'], f'申告金額{index}': item['value'],
                        f'數量{index}': str(item['quantity']), f'HSCode{index}': item['hs_code'],
                        f'_source_item_id{index}': item['id']})
        row.update({SHIPPING_COL: shipment['method'], '郵局申告金額(USD)': shipment['total_usd'],
                    '_shipment_id': shipment['shipment_id'], '_plan_id': plan['plan_id'],
                    '_shipment_role': 'additional', '_shipment_mode': plan['mode']})
        # Each package gets its proportional declaration, never the whole order JPY.
        original_usd = sum(Decimal(i['value']) * i['quantity'] for i in source_items(plan['payload']))
        jpy = clean(plan['payload'].get('訂單合計申告金額(JPY)'))
        if jpy:
            row['訂單合計申告金額(JPY)'] = str(round(Decimal(jpy.replace(',', '')) * Decimal(shipment['total_usd']) / original_usd))
        rows.append(row)
    return pd.DataFrame(rows).fillna('')


def selected_complete_orders(frame, max_rows):
    if max_rows is None or max_rows <= 0 or '_plan_id' not in frame.columns:
        return frame.copy() if max_rows is None or max_rows <= 0 else frame.head(max_rows).copy()
    ids = list(dict.fromkeys(frame[ORDER].astype(str)))[:max_rows]
    return frame[frame[ORDER].astype(str).isin(ids)].copy()


def utc_now():
    return datetime.now(timezone.utc).isoformat()
