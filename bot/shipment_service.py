"""Trusted server workflow for split/reissue. No API call is made at import."""
import copy
import re

import pandas as pd

from shipment_plans import ORDER, plan_frame, source_fingerprint, utc_now, validate_plan
from .shipment_ledger import LedgerUnavailable, configured_ledger, global_writer_lock


def _defaults():
    from .automation import run_automation
    from .sheets import backfill_results, get_history_order, read_completion_authority
    return run_automation, backfill_results, get_history_order, read_completion_authority


def validate_recipient(row):
    """Use the existing name/ID parser and Japan Post address preparation rules."""
    from pending_editor import country_kind, parse_shipping_name
    from .automation import (_prepare_addr_to_bean_recipient_fields,
                             _split_addr_to_bean_address_lines, _row_val)
    name=_row_val(row,['Shipping Name','Shipping Name_1'])
    address=_row_val(row,['Shipping Street','收件地址'])
    country=_row_val(row,['收件人國家','Country'])
    if not name or not address or not country:
        raise ValueError('收件姓名、地址及國家資料不完整，請先補齊再確認計畫')
    parsed=parse_shipping_name(name)
    if country_kind(country)=='china' and not parsed['prc_id']:
        raise ValueError('中國訂單需填入 PRC ID 才能製單')
    if country_kind(country)=='korea' and not parsed['pccc']:
        raise ValueError('韓國訂單需填入 PCCC 才能製單')
    recipient=_prepare_addr_to_bean_recipient_fields(row)
    if not recipient['name']:
        raise ValueError('缺少可用的收件姓名')
    try:
        _split_addr_to_bean_address_lines(recipient['address_line'],recipient['city'])
    except ValueError:
        raise ValueError('收件地址未通過既有日本郵政欄位長度檢查，請修正地址') from None


def lookup_history(order_id, *, ledger=None, source_loader=None, completion_reader=None):
    if not isinstance(order_id, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', order_id):
        raise ValueError('請輸入完整訂單編號')
    from .sheets import get_history_order, read_completion_authority
    source_loader = source_loader or get_history_order
    completion_reader = completion_reader or read_completion_authority
    # No encryption key: historical snapshots cannot be interpreted safely.
    ledger = ledger or configured_ledger()
    authority = completion_reader()
    tracking = sorted(t for o, t in authority.exact_pairs if o == order_id)
    records = [r for _, r in ledger.records() if r.get('plan', {}).get('order_id') == order_id]
    records.sort(key=lambda r: r.get('created_at', ''), reverse=True)
    shipments = []
    for record in records:
        for shipment in record['shipments']:
            if shipment.get('tracking'):
                shipments.append({key: copy.deepcopy(shipment.get(key, '')) for key in
                                  ('shipment_id', 'tracking', 'drive_file_id', 'status', 'payload')})
    if records:
        latest = records[0]
        row = copy.deepcopy(latest['plan']['payload'])
        kind, stamp = 'ledger_snapshot', latest['created_at']
    else:
        row = dict(source_loader(order_id))
        kind, stamp = 'current_source_not_historical_snapshot', utc_now()
    if not tracking and not records:
        raise ValueError('目標表沒有此訂單的既有運單，不能以補製繞過一般製單檢查')
    return {'order_id': order_id, 'row': row, 'source_kind': kind, 'source_time': stamp,
            'source_ref': records[0]['plan']['plan_id'] if records else '',
            'tracking_baseline': tracking, 'shipments': shipments,
            'unfinished_records': copy.deepcopy([r for r in records if any(
                s['status'] != 'completed' for s in r['shipments'])])}


def _result(plan, shipment):
    return {'shipment_id': shipment['shipment_id'], 'plan_id': plan['plan_id'],
            'order_id': plan['order_id'], 'tracking': shipment['tracking'],
            'name': shipment['payload'].get('Shipping Name', ''),
            'country_raw': shipment['payload'].get('收件人國家') or shipment['payload'].get('Country', ''),
            'trans_type': shipment['payload'].get('郵局運送方式(複數商品請自行確認是否走小包)', ''),
            'shipment_role': 'additional', 'drive_file_id': shipment['drive_file_id'],
            'status': 'completed' if shipment['status'] == 'completed' else 'backfill_failed'}


def execute_plan(plan, *, requested_by, tracking_baseline=None, confirmed_new_tracking=False,
                 lock=None, ledger=None, source_loader=None, completion_reader=None,
                 runner=None, backfill=None, log_cb=None, status_cb=None, runtime_handle=None):
    """Execute/resume the same immutable plan; unknown submissions never retry.

    The caller must supply provider-verified requested_by (UI enforces auth before
    calling). The saved server record, fresh source and target remain authority.
    """
    validate_plan(plan)
    if not requested_by or '@' not in requested_by:
        raise ValueError('缺少已驗證的操作人員')
    defaults = _defaults()
    runner, backfill = runner or defaults[0], backfill or defaults[1]
    source_loader, completion_reader = source_loader or defaults[2], completion_reader or defaults[3]
    lock = lock or global_writer_lock()
    # Validate encryption before acquiring a lock or touching Japan Post.
    ledger = ledger or configured_ledger(lock)
    lock.acquire()
    record = None
    results = []
    pending_writebacks = []
    try:
        from .shipment_ledger import NormalShipmentSafety
        from shipment_plans import digest
        if any(r.get('order_key') == digest(plan['order_id']) and r.get('stage') != 'completed'
               for _,r in NormalShipmentSafety(lock).records()):
            raise ValueError('此訂單有一般製單尚未回填，請恢復原運單')
        existing_records = ledger.records()
        record = ledger.get(plan['plan_id'])
        if record and record['plan'] == plan and all(sh['status'] == 'completed' for sh in record['shipments']):
            results = [_result(plan, sh) for sh in record['shipments']]
            return results
        if record and record['plan'] != plan:
            raise ValueError('已保存的計畫不可變更')
        for _, other in existing_records:
            if other.get('plan', {}).get('order_id') != plan['order_id'] or other.get('plan', {}).get('plan_id') == plan['plan_id']:
                continue
            if any(s['status'] != 'completed' for s in other['shipments']):
                raise ValueError('此訂單已有待核對運單，請先完成核對')
            if plan['mode'] == 'split' and other['plan']['mode'] == 'split':
                raise ValueError('此訂單已有拆單計畫，請恢復該計畫')
        authority = completion_reader()
        current_tracking = {t for o,t in authority.exact_pairs if o == plan['order_id']}
        own_tracking = {s['tracking'] for s in (record or {}).get('shipments', []) if s.get('tracking')}
        baseline = set((record or {}).get('tracking_baseline', tracking_baseline or []))
        if plan['mode'] == 'reissue':
            if not record and (not confirmed_new_tracking or not baseline
                               or plan['original_tracking'] not in baseline):
                raise ValueError('補製必須確認將產生新運單，並選取既有tracking')
            if current_tracking - own_tracking != baseline:
                raise ValueError('歷史運單已更新，請重新查詢並確認補製')
        elif (plan['order_id'] in authority.legacy_order_ids and not own_tracking) or current_tracking - own_tracking:
            raise ValueError('目標表已有運單，請改用明確補製流程')
        if not record or any(sh['status'] == 'planned' for sh in record['shipments']):
            if plan.get('source_kind') == 'ledger_snapshot':
                source_record = ledger.get(plan.get('source_ref', ''))
                if not source_record or source_record['plan']['order_id'] != plan['order_id']:
                    raise ValueError('原始快照不存在，請重新查詢')
                latest = source_record['plan']['payload']
            else:
                latest = source_loader(plan['order_id'])
            if plan['mode'] == 'split' and not record and not latest.get('_eligible_for_initial_split', False):
                raise ValueError('來源已不再符合未打單條件，請重新讀取')
            if source_fingerprint(latest) != plan['source_fingerprint']:
                raise ValueError('來源收件資料或品項已變更，請重新讀取並分配')
        if not record:
            validate_recipient(plan['payload'])
            frame = plan_frame(plan)
            record = {'plan': copy.deepcopy(plan), 'requested_by': requested_by,
                      'created_at': utc_now(), 'tracking_baseline': sorted(baseline),
                      'shipments': [{'shipment_id': row['_shipment_id'], 'status': 'planned',
                                     'payload': row.to_dict(), 'tracking': '', 'drive_file_id': ''}
                                    for _, row in frame.iterrows()]}
            ledger.save(plan['plan_id'], record)
        def flush_writebacks():
            if not pending_writebacks:
                return
            candidates = [result for _shipment, result in pending_writebacks]
            for shipment, _result_row in pending_writebacks:
                shipment['status'] = 'writeback_pending'
            try:
                ledger.save(plan['plan_id'], record)
            except Exception:
                raise LedgerUnavailable('shipment_persistence_unknown') from None
            # Each order/shipment is durably guarded before the shared writer
            # enters the target-sheet phase. One batch marker avoids a pair of
            # ownership reads for every package in a split/reissue plan.
            lock.stage('writeback_pending', shipment_count=len(candidates),
                       pending_count=len(candidates))
            try:
                outcome = backfill(candidates, log_cb=log_cb)
            except Exception:
                outcome = None
            outcome_items = {
                item.get('input_index'): item
                for item in (outcome or {}).get('items', [])
                if isinstance(item, dict) and isinstance(item.get('input_index'), int)
            }
            for index, (shipment, result_row) in enumerate(pending_writebacks):
                item = outcome_items.get(index)
                verified = bool(item and item.get('status') in {'written', 'already_present'})
                if item:
                    # Real backfill results always carry these identities. Keep
                    # compatibility with small injected adapters that omit them,
                    # but reject any explicit mismatch before marking complete.
                    for key, expected in (
                        ('order_id', result_row['order_id']),
                        ('tracking', result_row['tracking']),
                        ('trans_type', result_row['trans_type']),
                        ('shipment_role', result_row['shipment_role']),
                    ):
                        actual = item.get(key)
                        if actual not in (None, '') and str(actual) != str(expected):
                            verified = False
                            break
                if verified:
                    shipment['status'] = result_row['status'] = 'completed'
                else:
                    shipment['status'] = 'writeback_pending'
                    result_row['status'] = 'backfill_failed'
            try:
                ledger.save(plan['plan_id'], record)
            except Exception:
                raise LedgerUnavailable('shipment_persistence_unknown') from None
            pending_writebacks.clear()

        def execute_shipments():
            for shipment in record['shipments']:
                status = shipment['status']
                if status in {'issuing', 'unknown'}:
                    raise LedgerUnavailable('shipment_unknown_requires_review')
                if status == 'label_created':
                    raise LedgerUnavailable('shipment_pdf_requires_manual_recovery')

                def persist(stage, row, **fields):
                    if row.get('_shipment_id') != shipment['shipment_id']:
                        raise LedgerUnavailable('shipment_identity_mismatch')
                    if shipment['status'] == stage and all(shipment.get(k) == v for k,v in fields.items()):
                        return
                    allowed = {'planned': {'issuing'}, 'issuing': {'label_created'},
                               'label_created': {'label_created', 'uploaded'}}
                    if stage not in allowed.get(shipment['status'], set()):
                        raise LedgerUnavailable('shipment_invalid_transition')
                    if stage in {'label_created', 'uploaded'} and not fields.get('tracking'):
                        raise LedgerUnavailable('shipment_tracking_missing')
                    if stage == 'label_created' and (fields.get('tracking') in baseline or any(
                        other is not shipment and other.get('tracking') == fields.get('tracking') for other in record['shipments'])):
                        raise LedgerUnavailable('shipment_tracking_not_new')
                    if shipment.get('tracking') and fields.get('tracking') != shipment['tracking']:
                        raise LedgerUnavailable('shipment_tracking_changed')
                    if stage == 'uploaded' and not fields.get('drive_file_id'):
                        raise LedgerUnavailable('shipment_pdf_missing')
                    if stage == 'issuing' and fields.get('hs_codes'):
                        for index, code in fields['hs_codes'].items():
                            shipment['payload'][f'HSCode{index}'] = str(code)
                    shipment.update(status=stage, updated_at=utc_now(), **fields)
                    try:
                        ledger.save(plan['plan_id'], record)
                    except Exception:
                        raise LedgerUnavailable('shipment_persistence_unknown') from None
                    lock.stage(stage, package_key=shipment['shipment_id'],
                               tracking=shipment.get('tracking', ''))

                if status == 'planned':
                    try:
                        runner(pd.DataFrame([shipment['payload']]), headless=True,
                               lifecycle_cb=persist, log_cb=log_cb, status_cb=status_cb,
                               runtime_handle=runtime_handle)
                    except BaseException:
                        if shipment['status'] == 'issuing':
                            shipment['status'] = 'unknown'
                            try:
                                ledger.save(plan['plan_id'], record)
                            except Exception:
                                raise LedgerUnavailable('shipment_persistence_unknown') from None
                        raise LedgerUnavailable('shipment_execution_stopped') from None
                    if shipment['status'] == 'issuing':
                        shipment['status'] = 'unknown'
                        try:
                            ledger.save(plan['plan_id'], record)
                        except Exception:
                            raise LedgerUnavailable('shipment_persistence_unknown') from None
                        raise LedgerUnavailable('shipment_unknown_requires_review')
                    if shipment['status'] != 'uploaded':
                        # No Register was attempted: safely keep planned. A known
                        # tracking with missing PDF is visible for manual recovery.
                        raise LedgerUnavailable('shipment_not_fully_uploaded')
                result_row = _result(plan, shipment)
                if shipment['status'] != 'completed':
                    pending_writebacks.append((shipment, result_row))
                results.append(result_row)

        try:
            execute_shipments()
        except BaseException:
            # Preserve the former per-package behavior: labels already uploaded
            # before a later package fails are still backfilled as one batch.
            try:
                flush_writebacks()
            except BaseException:
                pass
            raise
        flush_writebacks()
        return results
    finally:
        if lock.held and lock.retain:
            # All postal/backfill calls in execute_plan have stopped. Only a
            # fresh exact owner read may clear transient uncertainty.
            lock.revalidate_after_worker_stopped()
        if lock.held and not lock.retain:
            try:
                # Reaching this point means this worker has stopped all external
                # requests. Per-order nonterminal states remain in the encrypted
                # ledger and continue to block reissue of that shipment.
                if record is not None:
                    lock.stage('completed', shipment_count=len(record['shipments']),
                               pending_count=sum(sh.get('status') != 'completed' for sh in record['shipments']))
                else:
                    lock.stage('completed', shipment_count=0, pending_count=0)
            except Exception:
                if lock.retain:
                    warning = '補單結果已保留，但執行鎖終態確認未完成；請勿重製相同訂單。'
                    for result in results:
                        result['writer_release_warning'] = warning
                elif log_cb:
                    try:
                        log_cb('⚠️ 終態標記未提交，但持有者已確認；嘗試釋放已停止工作的鎖。')
                    except Exception:
                        pass
            if not lock.retain:
                try:
                    lock.release()
                except Exception:
                    warning = '補單結果已保留，但執行鎖釋放狀態未明；請勿重製相同訂單。'
                    for result in results:
                        result['writer_release_warning'] = warning
                    if log_cb:
                        try:
                            log_cb('⚠️ 補製結果已保留；執行鎖釋放狀態未明。')
                        except Exception:
                            pass
            elif results and not any(result.get('writer_release_warning') for result in results):
                warning = '補單結果已保留，但執行鎖仍需核對；請勿重製相同訂單。'
                for result in results:
                    result['writer_release_warning'] = warning
