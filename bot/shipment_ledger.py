"""Sheets-backed exclusive writer and authenticated encrypted shipment journal.

The fixed title is the server uniqueness constraint. Each acquisition uses a fresh random generation sheet ID; there is no
intentional reuse. A crashed/unknown writer leaves the lock for manual reconciliation.
"""
import base64
import copy
import json
import secrets
import uuid
import time

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from shipment_plans import utc_now

LOCK_TITLE = '_jppost_exclusive_writer_v1'
LEDGER_TITLE = '_jppost_encrypted_shipments_v1'


class LedgerUnavailable(RuntimeError):
    def __init__(self, code='shipment_safety_unavailable'):
        super().__init__(code)
        self.reason_code = code


def _quota_call(operation, *args, sleep_fn=time.sleep, **kwargs):
    """Retry only a known 429 rejection; timeout/5xx mutations remain unknown."""
    for attempt in range(3):
        try:
            return operation(*args, **kwargs)
        except Exception as error:
            response=getattr(error, 'response', None)
            if getattr(response, 'status_code', None) != 429 or attempt == 2:
                raise
            sleep_fn(2 ** attempt)


def _cells(sheet_id, values):
    return {'updateCells': {'start': {'sheetId': sheet_id, 'rowIndex': 0, 'columnIndex': 0},
                            'rows': [{'values': [{'userEnteredValue': {'stringValue': v}} for v in values]}],
                            'fields': 'userEnteredValue'}}


class GlobalShipmentLock:
    def __init__(self, spreadsheet, *, owner=None):
        self.book = spreadsheet
        self.owner = owner or uuid.uuid4().hex
        self.sheet_id = secrets.randbelow(2_000_000_000) + 1
        self.held = False
        self.retain = False
        self._worksheet_handle = None

    def acquire(self):
        marker = json.dumps({'owner': self.owner, 'generation': self.sheet_id,
                             'created_at': utc_now(), 'stage': 'locked'})
        try:
            _quota_call(self.book.batch_update, {'requests': [
                {'addSheet': {'properties': {'sheetId': self.sheet_id, 'title': LOCK_TITLE,
                                              'hidden': True, 'gridProperties': {'rowCount': 2, 'columnCount': 2}}}},
                _cells(self.sheet_id, [marker]),
            ]})
            self.held = True
            self.assert_owned()
            return self
        except Exception:
            # A timeout can mean the server committed. Never retry or clear it.
            self.retain = True
            raise LedgerUnavailable('shipment_writer_busy_or_unknown') from None

    def marker(self):
        try:
            if self._worksheet_handle is None:
                self._worksheet_handle = _quota_call(self.book.get_worksheet_by_id, self.sheet_id)
            # Cache the handle, never the ownership value. Every safety gate
            # still reads the actual owner from the captured generation.
            rows = _quota_call(self._worksheet_handle.get_all_values)
            marker = json.loads(rows[0][0])
            if marker['owner'] != self.owner or marker['generation'] != self.sheet_id:
                raise ValueError()
            return marker
        except Exception:
            self.retain = True
            raise LedgerUnavailable('shipment_lock_ownership_lost') from None

    def assert_owned(self):
        if not self.held:
            raise LedgerUnavailable('shipment_lock_not_held')
        self.marker()

    def stage(self, stage, **fields):
        marker = self.marker()
        marker.update(stage=stage, updated_at=utc_now(), **fields)
        _quota_call(self.book.batch_update, {'requests': [_cells(self.sheet_id, [json.dumps(marker)])]})
        if self.marker() != marker:
            raise LedgerUnavailable('shipment_lock_readback_failed')

    def release(self):
        self.assert_owned()
        if self.retain:
            raise LedgerUnavailable('shipment_lock_requires_review')
        # Delete only this generation. Even stale release cannot delete a later lock.
        try:
            _quota_call(self.book.batch_update, {'requests': [{'deleteSheet': {'sheetId': self.sheet_id}}]})
        except Exception:
            self.retain = True
            raise LedgerUnavailable('shipment_lock_release_unknown') from None
        self.held = False


def encryption_secret():
    import streamlit as st
    try:
        value = (st.secrets.get('SHIPMENT_LEDGER_SECRET', '')
                 or st.secrets.get('auth', {}).get('cookie_secret', '')
                 or st.secrets.get('GOOGLE_CLIENT_SECRET', ''))
        return str(value).strip()
    except Exception:
        return ''


def open_target_book():
    from .sheets import TARGET_SHEET_ID, _get_gspread_client
    return _get_gspread_client().open_by_key(TARGET_SHEET_ID)


def global_writer_lock():
    return GlobalShipmentLock(open_target_book())


class ShipmentLedger:
    def __init__(self, spreadsheet, secret, lock=None):
        if not secret or len(secret) < 32:
            raise LedgerUnavailable('shipment_encryption_not_configured')
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                   info=b'jppost/shipment-ledger/v1').derive(secret.encode())
        self.cipher = Fernet(base64.urlsafe_b64encode(key))
        self.book, self.lock = spreadsheet, lock
        self._worksheet_handle = None
        self._record_cache = None
        self._positions = {}

    def _worksheet(self, create=False):
        if self._worksheet_handle is not None:
            return self._worksheet_handle
        matches = [s for s in _quota_call(self.book.worksheets) if s.title == LEDGER_TITLE]
        if matches:
            self._worksheet_handle = matches[0]
            return self._worksheet_handle
        if not create:
            return None
        self.lock.assert_owned()
        sheet_id = secrets.randbelow(2_000_000_000) + 1
        _quota_call(self.book.batch_update, {'requests': [{'addSheet': {'properties': {
            'sheetId': sheet_id, 'title': LEDGER_TITLE, 'hidden': True,
            'gridProperties': {'rowCount': 1000, 'columnCount': 3}}}}]})
        self._worksheet_handle = _quota_call(self.book.get_worksheet_by_id, sheet_id)
        return self._worksheet_handle

    def records(self):
        if self.lock is not None and self._record_cache is not None:
            return copy.deepcopy(self._record_cache)
        ws = self._worksheet()
        if ws is None:
            return []
        records = []
        try:
            for position,row in enumerate(_quota_call(ws.get_all_values),1):
                if not row or not row[0]:
                    continue
                if len(row) != 3 or row[2] != '1':
                    raise ValueError()
                record = json.loads(self.cipher.decrypt(row[1].encode()))
                if 'plan' in record and record['plan'].get('plan_id') != row[0]:
                    raise ValueError()
                records.append((row[0], record))
                self._positions[row[0]] = position
            if len({key for key, _ in records}) != len(records):
                raise ValueError()
            if self.lock is not None:
                self._record_cache = copy.deepcopy(records)
            return records
        except Exception:
            raise LedgerUnavailable('shipment_ledger_unreadable') from None

    def get(self, key):
        if self.lock is not None and self._record_cache is not None and key in self._positions:
            try:
                position=self._positions[key]
                row=_quota_call(self._worksheet().get, f'A{position}:C{position}')[0]
                if len(row)!=3 or row[0]!=key or row[2]!='1':
                    raise ValueError()
                record=json.loads(self.cipher.decrypt(row[1].encode()))
                if 'plan' in record and record['plan'].get('plan_id')!=key:
                    raise ValueError()
                return record
            except Exception:
                raise LedgerUnavailable('shipment_ledger_unreadable') from None
        return next((record for identity, record in self.records() if identity == key), None)

    def save(self, key, record):
        self.lock.assert_owned()
        encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False)
        token = self.cipher.encrypt(encoded.encode()).decode()
        if len(token) > 45000:
            raise LedgerUnavailable('shipment_snapshot_too_large')
        ws = self._worksheet(create=True)
        self.records()
        position = self._positions.get(key, max(self._positions.values(), default=0) + 1)
        if hasattr(ws, 'row_count') and position > ws.row_count:
            _quota_call(ws.add_rows, position - ws.row_count)
        _quota_call(ws.update, [[key, token, '1']], range_name=f'A{position}:C{position}', value_input_option='RAW')
        # Read only the row just written. Comparing authenticated ciphertext
        # verifies both identity and exact saved payload without a full-table read.
        if _quota_call(ws.get, f'A{position}:C{position}') != [[key, token, '1']]:
            self.lock.retain = True
            raise LedgerUnavailable('shipment_ledger_readback_failed')
        self._positions[key] = position
        cached = dict(self._record_cache or [])
        cached[key] = copy.deepcopy(record)
        self._record_cache = list(cached.items())


def configured_ledger(lock=None):
    return ShipmentLedger(lock.book if lock else open_target_book(), encryption_secret(), lock)


class NormalShipmentSafety:
    """Minimal non-PII tombstones also protect legacy jobs without a snapshot key.

    Full payloads belong only in the encrypted ledger. These guard rows contain
    hashes, tracking/file IDs and stages, sufficient to refuse accidental resend.
    """
    title = '_jppost_submission_guards_v1'

    def __init__(self, lock):
        self.lock = lock
        self.active = {}
        self._worksheet_handle = None
        self._record_cache = None
        self._positions = {}

    def _ws(self, create=False):
        if self._worksheet_handle is not None:
            return self._worksheet_handle
        found = next((s for s in _quota_call(self.lock.book.worksheets) if s.title == self.title), None)
        if found or not create:
            self._worksheet_handle = found
            return found
        self.lock.assert_owned()
        sid = secrets.randbelow(2_000_000_000) + 1
        _quota_call(self.lock.book.batch_update, {'requests': [{'addSheet': {'properties': {
            'sheetId': sid, 'title': self.title, 'hidden': True,
            'gridProperties': {'rowCount': 1000, 'columnCount': 2}}}}]})
        self._worksheet_handle = _quota_call(self.lock.book.get_worksheet_by_id, sid)
        return self._worksheet_handle

    def records(self):
        if self._record_cache is not None:
            return copy.deepcopy(self._record_cache)
        ws = self._ws()
        if ws is None:
            return []
        try:
            result=[]
            for position,row in enumerate(_quota_call(ws.get_all_values),1):
                if row and row[0]:
                    self._positions[row[0]]=position
                    result.append((row[0],json.loads(row[1])))
            self._record_cache=copy.deepcopy(result)
            return result
        except Exception:
            raise LedgerUnavailable('shipment_guards_unreadable') from None

    @staticmethod
    def identity(row):
        from shipment_plans import ORDER, SHIPPING_COL, clean, digest
        order = clean(row.get(ORDER) or row.get('order_id'))
        method = clean(row.get(SHIPPING_COL) or row.get('trans_type'))
        role = clean(row.get('_shipment_role') or row.get('shipment_role')) or 'primary'
        return digest([order, method, role]), digest(order)

    def assert_clear(self, rows):
        from shipment_plans import digest, ORDER
        rows = list(row for _,row in rows.iterrows()) if hasattr(rows, 'iterrows') else rows
        orders = {self.identity(row)[1] for row in rows}
        identities = {self.identity(row)[0] for row in rows}
        if any(key in identities or (r.get('order_key') in orders and r['stage'] != 'completed')
               for key,r in self.records()):
            raise LedgerUnavailable('shipment_previous_submission_requires_review')
        if any(s.title == LEDGER_TITLE for s in _quota_call(self.lock.book.worksheets)):
            for _, record in configured_ledger(self.lock).records():
                if digest(record['plan']['order_id']) in orders and any(s['status'] != 'completed' for s in record['shipments']):
                    raise LedgerUnavailable('shipment_existing_plan_requires_resume')

    def _save(self, key, data):
        self.lock.assert_owned()
        self.records()
        position = self._positions.get(key,max(self._positions.values(),default=0)+1)
        ws = self._ws(create=True)
        if hasattr(ws, 'row_count') and position > ws.row_count:
            _quota_call(ws.add_rows, position - ws.row_count)
        encoded=json.dumps(data)
        _quota_call(ws.update, [[key, encoded]], range_name=f'A{position}:B{position}', value_input_option='RAW')
        if _quota_call(ws.get, f'A{position}:B{position}') != [[key,encoded]]:
            self.lock.retain = True
            raise LedgerUnavailable('shipment_guard_readback_failed')
        self._positions[key]=position
        cached=dict(self._record_cache or [])
        cached[key]=copy.deepcopy(data)
        self._record_cache=list(cached.items())

    def lifecycle(self, stage, row, **fields):
        key, order_key = self.identity(row)
        data = self.active.setdefault(key, {'order_key': order_key, 'stage': 'planned'})
        if data.get('stage') == stage and all(data.get(k) == v for k,v in fields.items()):
            return
        data.update(stage=stage, updated_at=utc_now(), **fields)
        try:
            self._save(key, data)
            self.lock.stage(stage, package_key=key, tracking=data.get('tracking',''))
        except Exception:
            self.lock.retain = True
            raise

    def mark_verified(self, results):
        records = dict(self.records())
        for result in results:
            key, _ = self.identity(result)
            data = records.get(key)
            if data and result.get('status') == 'completed' and data.get('tracking') == result.get('tracking'):
                data['stage'] = 'completed'
                self._save(key, data)
                self.active[key] = data

    def safe_to_release(self):
        return all(r['stage'] not in {'issuing', 'label_created'} for r in self.active.values())
