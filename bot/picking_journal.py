"""Durable picking reservations and verified PDF / source-mirror recovery.

All persistence uses the existing encrypted ledger and the shared writer mutex.
Reads never create a ledger, baseline, reservation, or source completion marker.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

from .picking_labels import (
    PICKING_SOURCE_BINDING, PickingOrder, PickingTransactionResult,
    _build_picking_source_schema, _cell, _header_map, _items_from_row,
    is_allowed_picking_logistics, normalize_done_state, normalize_status_value,
    picking_order_key, picking_render_fingerprint,
)
from .shipment_ledger import LedgerUnavailable

PREFIX = 'picking:v1:'
KINDS = ('snapshot', 'batch', 'chunk')


class PickingValidationError(ValueError):
    """Only application-authored messages may be shown to the operator."""



def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def _hex(value, size):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{' + str(size) + r'}', value) is not None


def _document_kind(key, binding):
    if key == f'{PREFIX}snapshot:{binding}':
        return 'snapshot', ''
    prefix = f'{PREFIX}batch:{binding}:'
    if isinstance(key, str) and key.startswith(prefix) and _hex(key[len(prefix):], 32):
        return 'batch', key[len(prefix):]
    raise LedgerUnavailable('picking_record_namespace_invalid')


def is_picking_record(key, record):
    """Validate standalone envelopes, including mutable/immutable namespaces."""
    if not str(key).startswith(PREFIX):
        if isinstance(record, dict) and record.get('type') == 'picking:v1':
            raise LedgerUnavailable('picking_record_namespace_invalid')
        return False
    if (not isinstance(key, str) or not isinstance(record, dict) or record.get('type') != 'picking:v1'
            or record.get('kind') not in KINDS or record.get('key') != key
            or not _hex(record.get('binding'), 64) or not _hex(record.get('generation'), 32)
            or not _hex(record.get('digest'), 64)):
        raise LedgerUnavailable('picking_record_invalid')
    binding = record['binding']; generation = record['generation']
    if record['kind'] == 'chunk':
        if (not isinstance(record.get('text'), str) or record['digest'] != _digest(record['text'])
                or type(record.get('index')) is not int or record['index'] < 0):
            raise LedgerUnavailable('picking_record_invalid')
        kind, txid = _document_kind(record.get('document'), binding)
        if kind == 'snapshot':
            if record.get('slot') not in ('A', 'B'):
                raise LedgerUnavailable('picking_record_invalid')
            expected = f"{PREFIX}chunk:{binding}:snapshot:{record['slot']}:{record['index']}"
        else:
            if 'slot' in record:
                raise LedgerUnavailable('picking_record_invalid')
            expected = f"{PREFIX}chunk:{binding}:batch:{txid}:{generation}:{record['index']}"
        if key != expected:
            raise LedgerUnavailable('picking_record_namespace_invalid')
    else:
        kind, txid = _document_kind(key, binding)
        if kind != record['kind'] or not isinstance(record.get('chunks'), list) or not record['chunks']:
            raise LedgerUnavailable('picking_record_invalid')
        if kind == 'snapshot':
            if record.get('slot') not in ('A', 'B'):
                raise LedgerUnavailable('picking_record_invalid')
            stem = f"{PREFIX}chunk:{binding}:snapshot:{record['slot']}"
        else:
            if 'slot' in record:
                raise LedgerUnavailable('picking_record_invalid')
            stem = f'{PREFIX}chunk:{binding}:batch:{txid}:{generation}'
        if record['chunks'] != [f'{stem}:{index}' for index in range(len(record['chunks']))]:
            raise LedgerUnavailable('picking_document_references_invalid')
    return True


def picking_source_snapshot(values, shipping_deadlines=None, source_binding=PICKING_SOURCE_BINDING):
    """Read every identity, including L done and non-pending rows, without writing.

    Anchored sheets also contain raw B/C item rows. A seller collision in those
    rows is evidence that upstream UNIQUE(order_no) grouping is ambiguous.
    """
    if not values:
        return {}
    schema = _build_picking_source_schema(values[0]); headers = _header_map(values[0])
    qr_idx = next((headers[name] for name in ('QR內容', 'QR Code', 'QR', 'QRCode') if name in headers), None)
    raw_sellers = {}
    if schema.anchored:
        for row in values[1:]:
            raw_order, raw_seller = _cell(row, 2), _cell(row, 1)
            if raw_order and raw_seller:
                raw_sellers.setdefault(raw_order, set()).add(raw_seller)
    snapshot = {}
    for row_number, row in enumerate(values[1:], 2):
        order_no, seller = _cell(row, schema.order_no_idx), _cell(row, schema.order_source_idx)
        if not order_no and not seller:
            continue
        order = PickingOrder(row_number, _cell(row, schema.order_date_idx), seller, order_no,
                             _cell(row, schema.logistics_idx), _items_from_row(row, schema.item_groups),
                             _cell(row, qr_idx) or order_no, (shipping_deadlines or {}).get(order_no, ''))
        review = ''
        try:
            key = picking_order_key(order, source_binding)
        except ValueError:
            key = 'invalid-row-' + str(row_number); review = '缺少訂單來源或注文番号'
        if len(raw_sellers.get(order_no, set())) > 1:
            review = '同注文番号存在多個訂單來源，上游商品合併需核對'
        entry = {'key': key, 'row': row_number, 'done': normalize_done_state(_cell(row, schema.done_idx)),
                 'fingerprint': picking_render_fingerprint(order), 'order': order,
                 'eligible': normalize_status_value(_cell(row, schema.status_idx)) == '可出貨'
                             and is_allowed_picking_logistics(order.logistics_method) and bool(order.items),
                 'review': review}
        if key in snapshot:
            snapshot[key]['review'] = '同一訂單身份出現多列，請先核對'
        else:
            snapshot[key] = entry
    return snapshot


def validate_selected_orders(orders, snapshot, source_binding=PICKING_SOURCE_BINDING):
    selected = []
    seen = set()
    for order in orders:
        key = picking_order_key(order, source_binding)
        entry = snapshot.get(key)
        if key in seen or not entry or entry.get('review') or not entry.get('eligible'):
            raise PickingValidationError('來源訂單身份遺失、重複或不再符合條件，請重新讀取並核對。')
        if entry['fingerprint'] != picking_render_fingerprint(order):
            raise PickingValidationError('已選訂單品項或列印資料已變更，請重新讀取並核對。')
        seen.add(key); selected.append(entry['order'])
    if not selected:
        raise PickingValidationError('請選取至少一筆訂單。')
    return selected


class PickingJournal:
    def __init__(self, ledger, source_binding=PICKING_SOURCE_BINDING):
        self.ledger = ledger
        self.source_binding = tuple(source_binding)
        self.binding = _digest(list(source_binding))

    def _records(self):
        records = dict(self.ledger.records())
        validate_picking_documents(records)
        return records

    def _read_document(self, key, records=None):
        records = records if records is not None else self._records()
        head = records.get(key)
        if head is None:
            return None
        if not is_picking_record(key, head) or head['binding'] != self.binding:
            raise LedgerUnavailable('picking_document_invalid')
        try:
            chunks = []
            for index, chunk_key in enumerate(head['chunks']):
                chunk = records[chunk_key]
                if (not is_picking_record(chunk_key, chunk) or chunk['kind'] != 'chunk'
                        or chunk['binding'] != self.binding or chunk['document'] != key
                        or chunk['generation'] != head['generation'] or chunk['index'] != index):
                    raise ValueError()
                chunks.append(chunk['text'])
            data = json.loads(''.join(chunks))
            if _digest(data) != head['digest']:
                raise ValueError()
            return data
        except Exception:
            raise LedgerUnavailable('picking_document_unreadable') from None

    def _save_document(self, kind, key, data):
        """Immutable shards first, commit the head last. Each token stays <45k."""
        encoded = json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
        slot = None
        generation = uuid.uuid4().hex
        if kind == 'snapshot':
            current = self.ledger.get(key)
            slot = 'B' if current and current.get('slot') == 'A' else 'A'
            # Only the inactive source slot may be overwritten. The committed
            # head continues pointing at complete active chunks until head save.
            chunk_stem = f'{PREFIX}chunk:{self.binding}:snapshot:{slot}'
        else:
            _kind, txid = _document_kind(key, self.binding)
            chunk_stem = f'{PREFIX}chunk:{self.binding}:batch:{txid}:{generation}'
        chunks = []
        pending_chunks = []
        cursor = 0
        while cursor < len(encoded):
            chunk_key = f'{chunk_stem}:{len(chunks)}'
            length = min(30000, len(encoded) - cursor)
            while True:
                text = encoded[cursor:cursor + length]
                chunk = {'type': 'picking:v1', 'kind': 'chunk', 'key': chunk_key, 'binding': self.binding,
                         'text': text, 'digest': _digest(text), 'document': key,
                         'generation': generation, 'index': len(chunks)}
                if slot:
                    chunk['slot'] = slot
                # Fernet adds padding, IV, MAC and base64. Keep plaintext below
                # 32k bytes so encrypted cells remain strictly below 45k.
                if len(json.dumps(chunk, ensure_ascii=False, sort_keys=True).encode()) < 32000:
                    break
                length //= 2
            cursor += length
            pending_chunks.append((chunk_key, chunk))
            chunks.append(chunk_key)
        if hasattr(self.ledger, 'save_many'):
            # Few requests, each verified by an exact read-back (quota-safe).
            self.ledger.save_many(pending_chunks)
        else:
            for chunk_key, chunk in pending_chunks:
                self.ledger.save(chunk_key, chunk)
                if self.ledger.get(chunk_key) != chunk:
                    raise LedgerUnavailable('picking_persistence_unconfirmed')
        head = {'type': 'picking:v1', 'kind': kind, 'key': key, 'binding': self.binding,
                'chunks': chunks, 'digest': _digest(data), 'generation': generation}
        if slot:
            head['slot'] = slot
        self.ledger.save(key, head)
        if self.ledger.get(key) != head:
            raise LedgerUnavailable('picking_persistence_unconfirmed')

    def load_source_snapshot(self):
        return self._read_document(f'{PREFIX}snapshot:{self.binding}') or {}

    def save_source_snapshot(self, snapshot):
        _validate_snapshot_document(snapshot)
        self._save_document('snapshot', f'{PREFIX}snapshot:{self.binding}', snapshot)

    def batches(self):
        records = self._records()
        result = []
        for key, head in records.items():
            if is_picking_record(key, head) and head['kind'] == 'batch' and head['binding'] == self.binding:
                record = self._read_document(key, records)
                _validate_batch_document(key, self.binding, record)
                result.append(record)
        return result

    def save_batch(self, record):
        _validate_batch_document(f"{PREFIX}batch:{self.binding}:{record.get('txid', '')}", self.binding, record)
        self._save_document('batch', f"{PREFIX}batch:{self.binding}:{record['txid']}", record)

    def key_evidence(self):
        evidence = {}
        for record in self.batches():
            for item in record['manifest']:
                if item['key'] in evidence:
                    raise LedgerUnavailable('picking_multiple_reservations')
                evidence[item['key']] = (record, item)
        return evidence


def _validate_snapshot_document(snapshot):
    fields = {'key', 'row', 'done', 'fingerprint', 'manual_observed', 'untrusted_done'}
    if not isinstance(snapshot, dict):
        raise LedgerUnavailable('picking_snapshot_invalid')
    for key, entry in snapshot.items():
        if (not _hex(key, 64) or not isinstance(entry, dict) or set(entry) != fields
                or entry['key'] != key or type(entry['row']) is not int or entry['row'] < 2
                or entry['done'] not in ('DONE', 'NOT_DONE', 'UNKNOWN')
                or not _hex(entry['fingerprint'], 64)
                or type(entry['manual_observed']) is not bool or type(entry['untrusted_done']) is not bool):
            raise LedgerUnavailable('picking_snapshot_invalid')


def _validate_batch_document(key, binding, record):
    if (not isinstance(record, dict) or not _hex(record.get('txid'), 32)
            or key != f"{PREFIX}batch:{binding}:{record['txid']}"
            or record.get('stage') not in ('reserved', 'upload_started', 'uploaded_verified')
            or not isinstance(record.get('filename'), str) or not isinstance(record.get('file_id'), str)
            or not isinstance(record.get('manifest'), list) or not record['manifest']
            or record.get('manifest_hash') != _digest(record['manifest'])
            or not isinstance(record.get('mirrors'), dict)
            or any(not isinstance(item, dict) or set(item) != {'key', 'fingerprint'}
                   or not _hex(item['key'], 64) or not _hex(item['fingerprint'], 64)
                   for item in record['manifest'])
            or len({item['key'] for item in record['manifest']}) != len(record['manifest'])):
        raise LedgerUnavailable('picking_batch_invalid')
    if record['stage'] in {'upload_started', 'uploaded_verified'} and (
            re.fullmatch(r'[A-Za-z0-9_-]{1,200}', record['file_id']) is None
            or not _hex(record.get('md5'), 32) or not _hex(record.get('sha256'), 64)
            or type(record.get('size')) is not int or record['size'] <= 0):
        raise LedgerUnavailable('picking_batch_receipt_invalid')
    keys = {item['key'] for item in record['manifest']}
    for mirror_key, outcome in record['mirrors'].items():
        if (mirror_key not in keys or not isinstance(outcome, dict)
                or outcome.get('status') not in ('verified', 'failed', 'review')
                or (outcome.get('status') == 'verified' and (type(outcome.get('row')) is not int or outcome['row'] < 2))):
            raise LedgerUnavailable('picking_batch_mirror_invalid')


def validate_picking_documents(records):
    """Validate complete typed documents before any postal consumer skips them."""
    for key, head in records.items():
        if not is_picking_record(key, head) or head['kind'] == 'chunk':
            continue
        checker = object.__new__(PickingJournal)
        checker.binding = head['binding']
        data = checker._read_document(key, records)
        if head['kind'] == 'batch':
            _validate_batch_document(key, head['binding'], data)
        else:
            _validate_snapshot_document(data)


def _compact_snapshot(snapshot, previous):
    """Keep prior completion associations even after rows move/disappear/reset."""
    result = copy.deepcopy(previous)
    for key, entry in snapshot.items():
        if not _hex(key, 64):
            # Missing identities remain visible review entries, never baseline keys.
            continue
        old = previous.get(key, {})
        result[key] = {'key': key, 'row': entry['row'], 'done': entry['done'],
                       # Keep the content fingerprint associated with the human
                       # completion proof; unrelated operations cannot approve drift.
                       'fingerprint': old['fingerprint'] if old.get('manual_observed') else entry['fingerprint'],
                       'manual_observed': old.get('manual_observed', False),
                       'untrusted_done': old.get('untrusted_done', False)}
        # Only initial baseline trusts all current human L marks. Later new marks
        # require a stable existing row association; inherited row marks don't.
        same_row_key = next((k for k, value in previous.items() if value.get('row') == entry['row']), None)
        if entry['done'] == 'DONE' and previous and same_row_key != key:
            result[key]['untrusted_done'] = True
        if entry['done'] == 'DONE' and not result[key]['untrusted_done'] and (not previous or same_row_key == key):
            result[key]['manual_observed'] = True
    return result


def classify_picking_snapshot(snapshot, journal):
    previous = journal.load_source_snapshot(); evidence = journal.key_evidence()
    by_row = {entry['row']: key for key, entry in previous.items()}
    result = {name: [] for name in ('pending', 'done', 'mirror_pending', 'review')}
    for key, entry in snapshot.items():
        item = dict(entry)
        if entry.get('review'):
            result['review'].append(item); continue
        receipt, manifest = evidence.get(key, (None, None))
        if receipt:
            item.update(file_id=receipt.get('file_id', ''), txid=receipt['txid'], provenance='pdf_receipt')
            if manifest['fingerprint'] != entry['fingerprint']:
                item['review'] = '已保存 PDF 的品項資料與目前來源不同'; result['review'].append(item)
            elif receipt.get('stage') == 'upload_started' and receipt.get('file_id'):
                item.update(recovery_kind='check_upload', review='上傳結果未確認；只能核對原預留檔案 ID，禁止重新產生')
                result['mirror_pending'].append(item)
            elif receipt.get('stage') != 'uploaded_verified':
                item['review'] = '製作或上傳結果待核對，禁止重新產生'; result['review'].append(item)
            elif entry['done'] != 'DONE' or receipt['mirrors'].get(key, {}).get('status') != 'verified':
                item['review'] = 'PDF 已存在；可開啟原檔重印或只補標 L'; result['mirror_pending'].append(item)
            else:
                result['done'].append(item)
        elif previous.get(key, {}).get('manual_observed'):
            item['provenance'] = 'manual_observed'
            if previous[key]['fingerprint'] != entry['fingerprint']:
                item['review'] = '人工已製單紀錄的品項或列印資料與目前來源不同，請核對歷史 PDF，禁止自動重製'
                result['review'].append(item)
            elif entry['done'] == 'NOT_DONE':
                # 曾人工勾「已製單」、之後 L 被取消，且系統沒有任何 PDF 製作紀錄：
                # 沒有證據顯示實際製作過，不應永久禁止製單（否則訂單無法處理）。
                # 品項／列印資料未變，視為待製單；仍標記來源供畫面提示。
                item['provenance'] = 'manual_reset'
                if entry['eligible']:
                    result['pending'].append(item)
            elif entry['done'] != 'DONE':
                item['review'] = 'L 欄狀態無法辨識'
                result['review'].append(item)
            else:
                result['done'].append(item)
        elif entry['done'] == 'DONE':
            if previous.get(key, {}).get('untrusted_done') or (previous and by_row.get(entry['row']) != key):
                item['review'] = '此列已製單勾選可能承接其他訂單，請核對'; result['review'].append(item)
            else:
                item['provenance'] = 'manual_observed'; result['done'].append(item)
        elif entry['done'] != 'NOT_DONE':
            item['review'] = 'L 欄狀態無法辨識'; result['review'].append(item)
        elif entry['eligible']:
            result['pending'].append(item)
    return result


def validate_pdf_receipt(metadata, record, folder_id):
    if (not isinstance(metadata, dict) or metadata.get('id') != record.get('file_id')
            or metadata.get('mimeType') != 'application/pdf' or metadata.get('trashed') is not False
            or folder_id not in metadata.get('parents', []) or 'shortcutDetails' in metadata
            or metadata.get('appProperties', {}).get('picking_tx') != record['txid']
            or metadata.get('appProperties', {}).get('picking_manifest') != record['manifest_hash']
            or str(metadata.get('size')) != str(record.get('size'))
            or metadata.get('md5Checksum') != record.get('md5')
            or not record.get('md5') or not record.get('sha256')):
        return None
    if metadata.get('sha256Checksum') and metadata['sha256Checksum'] != record['sha256']:
        return None
    return {'id': record['file_id'], 'name': record['filename'],
            'webViewLink': f"https://drive.google.com/file/d/{record['file_id']}/view"}


def finalize_picking_lock(lock, txid, *, sleep=time.sleep, waits=(0, 3, 10)):
    """Terminalize and release this worker's own lock after the batch stopped.

    Only the already-verified owner/generation is ever released. A transient
    read/stage failure is rechecked a bounded number of times (the worker has
    stopped, so waiting is harmless). If ownership is never re-confirmed the lock
    stays retained and fail-closed; nothing is reclaimed by age or by guessing.
    """
    last_error = None
    for wait in waits:
        if wait:
            sleep(wait)
        try:
            if not lock.revalidate_after_worker_stopped():
                continue
            try:
                lock.stage('completed', picking_tx=txid)
            except LedgerUnavailable:
                if lock.retain:
                    continue
                # Confirmed not committed while still owned: release anyway,
                # exactly like the postal worker's cleanup.
            lock.release()
            return
        except LedgerUnavailable as error:
            if not lock.held:
                return
            last_error = error
    if last_error is not None and lock.held:
        # Same visible outcome as before: the lock stays retained for review.
        raise last_error


def execute_picking_batch(orders, *, output_dir, source_loader, journal, lock, list_files,
                          reserve_file_id, upload_file, get_file, mark_done, render=None,
                          folder_id, now=None):
    """Reserve once, verify exact Drive ID, then repair only unverified mirrors."""
    from .drive import choose_safe_picking_filename
    from .picking_pdf import render_picking_labels_pdf
    render = render or render_picking_labels_pdf
    lock.acquire()
    record = None; path = ''; drive_file = None
    try:
        snapshot = source_loader()
        fresh = validate_selected_orders(orders, snapshot, journal.source_binding)
        classified = classify_picking_snapshot(snapshot, journal)
        evidence = journal.key_evidence()
        keys = [picking_order_key(o, journal.source_binding) for o in fresh]
        existing = {evidence[key][0]['txid'] for key in keys if key in evidence}
        if existing:
            # Recovery accepts a subset of one existing batch; it never folds in
            # new orders or combines distinct original PDFs under one operation.
            if len(existing) != 1 or any(key not in evidence for key in keys):
                raise PickingValidationError('請分別補標原有 PDF 與產生新訂單。')
            record = copy.deepcopy(evidence[keys[0]][0])
            for key in keys:
                if evidence[key][1]['fingerprint'] != snapshot[key]['fingerprint']:
                    raise PickingValidationError('已保存 PDF 與來源內容不同，請核對原 PDF。')
        else:
            pending = {e['key'] for e in classified['pending']}
            if any(key not in pending for key in keys):
                raise PickingValidationError('此訂單已有人工製作紀錄或來源需核對，禁止重製。')
            previous = journal.load_source_snapshot()
            journal.save_source_snapshot(_compact_snapshot(snapshot, previous))
            manifest = [{'key': picking_order_key(o, journal.source_binding), 'fingerprint': picking_render_fingerprint(o)} for o in fresh]
            record = {'txid': uuid.uuid4().hex, 'manifest': manifest, 'manifest_hash': _digest(manifest),
                      'stage': 'reserved', 'mirrors': {}, 'filename': '', 'file_id': ''}
            journal.save_batch(record)
            dt = datetime.now() if now is None else datetime.strptime(now, '%Y-%m-%d %H:%M:%S')
            prefix = dt.strftime('%y%m%d-')
            record['filename'] = choose_safe_picking_filename(list_files(prefix), list_files(prefix), today=dt.strftime('%Y-%m-%d'), timestamp=dt.strftime('%H%M%S'))
            path = str(Path(output_dir) / record['filename'])
            render(fresh, path)
            data = Path(path).read_bytes()
            record.update(file_id=reserve_file_id(), md5=hashlib.md5(data).hexdigest(),
                          sha256=hashlib.sha256(data).hexdigest(), size=len(data), stage='upload_started')
            if not record['file_id']:
                raise PickingValidationError('Drive 未傳回預留檔案 ID。')
            journal.save_batch(record)
            lock.assert_owned()
            try:
                upload_file(path, record)
            except Exception:
                # Includes 409 and commit-timeout: the exact reserved ID alone
                # determines whether bytes exist. No new ID or blind retry.
                pass
        if record.get('file_id'):
            try:
                drive_file = validate_pdf_receipt(get_file(record), record, folder_id)
            except Exception:
                drive_file = None
        if not drive_file:
            return PickingTransactionResult(False, path, record.get('filename', ''), [], error='上傳結果待核對；禁止重新產生，請聯絡管理員。')
        if record['stage'] != 'uploaded_verified':
            record['stage'] = 'uploaded_verified'
            journal.save_batch(record)
        # The durable receipt must precede every source mutation.
        to_mark = [o for o in fresh if (record['mirrors'].get(picking_order_key(o, journal.source_binding), {}).get('status') != 'verified'
                                    or snapshot[picking_order_key(o, journal.source_binding)]['done'] != 'DONE')]
        if to_mark:
            lock.assert_owned()
            try:
                outcomes = mark_done(to_mark)
            except Exception:
                outcomes = None
            for order in to_mark:
                key = picking_order_key(order, journal.source_binding)
                outcome = outcomes.get(key) if isinstance(outcomes, dict) else None
                if not isinstance(outcome, dict) or outcome.get('status') not in ('verified', 'failed', 'review'):
                    outcome = {'status': 'failed', 'reason': '標記結果未確認'}
                record['mirrors'][key] = copy.deepcopy(outcome)
            journal.save_batch(record)
        verified = [record['mirrors'][key].get('row') for key in keys if record['mirrors'].get(key, {}).get('status') == 'verified']
        success = len(verified) == len(keys)
        return PickingTransactionResult(success, path, record['filename'], verified, drive_file,
                                        '' if success else 'PDF 已保存；部分 L 欄未確認，請只補標原 PDF。')
    finally:
        # All synchronous work stopped. Durable per-key reservations survive
        # release, including unknown upload outcomes. Ownership loss retains it.
        finalize_picking_lock(lock, (record or {}).get('txid', ''))
