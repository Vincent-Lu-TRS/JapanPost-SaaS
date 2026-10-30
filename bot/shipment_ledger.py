"""Sheets-backed exclusive writer and authenticated encrypted shipment journal.

The fixed title is the server uniqueness constraint. Each acquisition uses a fresh random generation sheet ID. Only a
durably terminal generation may be archived automatically; a crashed/unknown writer remains fail-closed.
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
LOCK_ARCHIVE_PREFIX = '_jppost_writer_completed_'
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


def _create_hidden_worksheet(book, *, sheet_id, title, rows, columns, failure_code):
    """Create one internal sheet; resolve commit-then-timeout by exact metadata read-back."""
    request = {'requests': [{'addSheet': {'properties': {
        'sheetId': sheet_id, 'title': title, 'hidden': True,
        'gridProperties': {'rowCount': rows, 'columnCount': columns},
    }}}]}
    try:
        _quota_call(book.batch_update, request)
    except Exception:
        try:
            matches = [sheet for sheet in _quota_call(book.worksheets) if sheet.title == title]
            if len(matches) == 1 and matches[0].id == sheet_id:
                return matches[0]
        except Exception:
            pass
        raise LedgerUnavailable(failure_code) from None
    try:
        sheet = _quota_call(book.get_worksheet_by_id, sheet_id)
        if sheet.id != sheet_id or sheet.title != title:
            raise ValueError()
        return sheet
    except Exception:
        try:
            matches = [sheet for sheet in _quota_call(book.worksheets) if sheet.title == title]
            if len(matches) == 1 and matches[0].id == sheet_id:
                return matches[0]
        except Exception:
            pass
        raise LedgerUnavailable(failure_code) from None


class GlobalShipmentLock:
    def __init__(self, spreadsheet, *, owner=None):
        self.book = spreadsheet
        self.owner = owner or uuid.uuid4().hex
        self.sheet_id = secrets.randbelow(2_000_000_000) + 1
        self.held = False
        self.retain = False
        self._current_marker = None

    def _worksheets(self):
        return _quota_call(self.book.worksheets)

    @staticmethod
    def _decode_marker(sheet):
        rows = _quota_call(sheet.get_all_values)
        marker = json.loads(rows[0][0])
        if (not isinstance(marker, dict) or marker.get('generation') != sheet.id
                or not isinstance(marker.get('owner'), str) or not marker.get('owner')):
            raise ValueError()
        return marker

    def _sheet_with_id(self, sheet_id):
        return next((sheet for sheet in self._worksheets() if sheet.id == sheet_id), None)

    def _canonical_sheets(self):
        return [sheet for sheet in self._worksheets() if sheet.title == LOCK_TITLE]

    @staticmethod
    def _is_hidden(sheet):
        hidden = getattr(sheet, 'isSheetHidden', None)
        if hidden is None:
            hidden = getattr(sheet, 'hidden', None)
        return hidden is True

    def _is_archived_terminal_generation(self):
        try:
            sheet = self._sheet_with_id(self.sheet_id)
            if sheet is None or sheet.title != f'{LOCK_ARCHIVE_PREFIX}{self.sheet_id}':
                return False
            marker = self._decode_marker(sheet)
            return (marker.get('owner') == self.owner and marker.get('stage') == 'completed')
        except Exception:
            return False

    def _archive_completed(self, sheet):
        """Archive a proven terminal writer marker while retaining its evidence."""
        archive_title = f'{LOCK_ARCHIVE_PREFIX}{sheet.id}'
        try:
            marker = self._decode_marker(sheet)
            if marker.get('stage') != 'completed':
                raise ValueError()
            if any(s.title == archive_title and s.id != sheet.id for s in self._worksheets()):
                raise ValueError()
            _quota_call(self.book.batch_update, {'requests': [{'updateSheetProperties': {
                'properties': {'sheetId': sheet.id, 'title': archive_title, 'hidden': True},
                'fields': 'title,hidden',
            }}]})
        except Exception:
            # A timeout may follow a successful rename. Confirm this exact sheet
            # was renamed and the canonical title is free; never retry the write.
            try:
                current = self._sheet_with_id(sheet.id)
                canonical = self._canonical_sheets()
                current_marker = self._decode_marker(current) if current is not None else {}
                if (current is None or current.title != archive_title or canonical or not self._is_hidden(current)
                        or current_marker.get('generation') != sheet.id
                        or current_marker.get('stage') != 'completed'):
                    raise ValueError()
                return
            except Exception:
                raise LedgerUnavailable('shipment_writer_busy_or_unknown') from None
        try:
            current = self._sheet_with_id(sheet.id)
            if (current is None or current.title != archive_title or self._canonical_sheets()
                    or not self._is_hidden(current)):
                raise ValueError()
            current_marker = self._decode_marker(current)
            if current_marker.get('stage') != 'completed':
                raise ValueError()
        except Exception:
            raise LedgerUnavailable('shipment_writer_busy_or_unknown') from None

    def _reconcile_terminal_lock(self):
        canonical = self._canonical_sheets()
        if not canonical:
            return
        if len(canonical) != 1:
            raise LedgerUnavailable('shipment_writer_busy_or_unknown')
        try:
            marker = self._decode_marker(canonical[0])
            if marker.get('stage') != 'completed':
                raise ValueError()
        except Exception:
            # In-flight, ambiguous, malformed and legacy locks are deliberately
            # left untouched. Their age alone cannot prove that their writer died.
            raise LedgerUnavailable('shipment_writer_busy_or_unknown') from None
        self._archive_completed(canonical[0])

    def acquire(self):
        marker = json.dumps({'owner': self.owner, 'generation': self.sheet_id,
                             'created_at': utc_now(), 'stage': 'locked'})
        try:
            self._reconcile_terminal_lock()
            _quota_call(self.book.batch_update, {'requests': [
                {'addSheet': {'properties': {'sheetId': self.sheet_id, 'title': LOCK_TITLE,
                                              'hidden': True, 'gridProperties': {'rowCount': 2, 'columnCount': 2}}}},
                _cells(self.sheet_id, [marker]),
            ]})
            self.held = True
            self.assert_owned()
            return self
        except Exception:
            # addSheet may commit before the transport fails. Read back the exact
            # attempted generation once; do not retry or clear an uncertain add.
            try:
                sheet = self._sheet_with_id(self.sheet_id)
                actual = self._decode_marker(sheet) if sheet is not None else None
                if (sheet is not None and sheet.title == LOCK_TITLE and actual is not None
                        and actual.get('owner') == self.owner and actual.get('generation') == self.sheet_id
                        and json.loads(marker) == actual):
                    self.held = True
                    self.retain = False
                    self._current_marker = actual
                    return self
            except Exception:
                pass
            self.retain = True
            raise LedgerUnavailable('shipment_writer_busy_or_unknown') from None

    def marker(self):
        try:
            # A values read addressed by the fixed canonical title verifies the
            # live title binding and the marker in one Sheets request. The
            # generation inside the marker must still equal this sheet ID.
            response = _quota_call(self.book.values_get, f"'{LOCK_TITLE}'!A1")
            rows = response.get('values', []) if isinstance(response, dict) else []
            marker = json.loads(rows[0][0])
            if (not isinstance(marker, dict) or marker.get('generation') != self.sheet_id
                    or not isinstance(marker.get('owner'), str) or not marker.get('owner')):
                raise ValueError()
            if marker['owner'] != self.owner or marker['generation'] != self.sheet_id:
                raise ValueError()
            self._current_marker = copy.deepcopy(marker)
            return marker
        except Exception:
            self.retain = True
            raise LedgerUnavailable('shipment_lock_ownership_lost') from None

    def assert_owned(self, *, fresh=True):
        if not self.held:
            raise LedgerUnavailable('shipment_lock_not_held')
        if fresh:
            self.marker()
            return
        marker = self._current_marker or {}
        if marker.get('owner') != self.owner or marker.get('generation') != self.sheet_id:
            self.retain = True
            raise LedgerUnavailable('shipment_lock_ownership_lost')

    def revalidate_after_worker_stopped(self):
        """Clear a transient retain only after a fresh canonical owner check.

        Call only from a finalizer after the synchronous worker has stopped.
        This is not a lease or age-based takeover: a missing, malformed, or
        different owner/generation remains retained and fail-closed.
        """
        if not self.held:
            return False
        if not self.retain:
            return True
        try:
            self.marker()
        except LedgerUnavailable:
            return False
        self.retain = False
        return True

    def stage(self, stage, **fields):
        # Re-read both the canonical sheet binding and owner marker before an
        # irreversible external side effect. For informational transitions, the
        # already verified generation is sufficient and avoids excess Sheets
        # reads in multi-package jobs.
        if stage in {'issuing', 'writeback_pending', 'completed'}:
            marker = self.marker()
        else:
            self.assert_owned(fresh=False)
            marker = copy.deepcopy(self._current_marker or {})
        previous = copy.deepcopy(marker)
        marker.update(stage=stage, updated_at=utc_now(), **fields)
        expected = json.dumps(marker)
        try:
            _quota_call(self.book.batch_update, {'requests': [_cells(self.sheet_id, [expected])]})
        except Exception:
            # A committed update can still lose its response. One exact read-back
            # resolves that case without repeating the mutation.
            try:
                actual = self.marker()
            except LedgerUnavailable:
                self.retain = True
                raise
            except Exception:
                self.retain = True
                raise LedgerUnavailable('shipment_lock_readback_failed') from None
            if actual == marker:
                self._current_marker = copy.deepcopy(marker)
                return
            if actual == previous:
                # The write is confirmed not to have committed, and this
                # worker still owns the canonical lock. Abort this stage,
                # but leave cleanup able to terminalize/release safely.
                raise LedgerUnavailable('shipment_lock_readback_failed') from None
            self.retain = True
            raise LedgerUnavailable('shipment_lock_readback_failed') from None
        # A successful Sheets mutation response confirms this marker update.
        # Read back only when the mutation raises (the ambiguous-commit case
        # handled above). Critical stages still fresh-check ownership before
        # the update; completed is fresh-checked again by release() before
        # deleting the generation.
        self._current_marker = copy.deepcopy(marker)

    def release(self):
        if not self.held:
            return
        if (self._current_marker or {}).get('stage') == 'completed':
            try:
                expected = copy.deepcopy(self._current_marker or {})
                marker = self.marker()
                if marker != expected or marker.get('stage') != 'completed':
                    raise LedgerUnavailable('shipment_lock_requires_review')
            except LedgerUnavailable:
                if self._is_archived_terminal_generation():
                    self.held = False
                    return
                raise
        else:
            self.assert_owned()
        if self.retain:
            raise LedgerUnavailable('shipment_lock_requires_review')
        # Delete only this generation. Even stale release cannot delete a later lock.
        try:
            _quota_call(self.book.batch_update, {'requests': [{'deleteSheet': {'sheetId': self.sheet_id}}]})
        except Exception:
            # Confirm a committed deletion, or a concurrent terminal archive, once.
            try:
                if self._sheet_with_id(self.sheet_id) is None or self._is_archived_terminal_generation():
                    self.held = False
                    return
            except Exception:
                pass
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
        self._worksheet_handle = _create_hidden_worksheet(
            self.book, sheet_id=sheet_id, title=LEDGER_TITLE, rows=1000, columns=3,
            failure_code='shipment_ledger_write_unknown')
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
        self.lock.assert_owned(fresh=False)
        encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False)
        token = self.cipher.encrypt(encoded.encode()).decode()
        if len(token) > 45000:
            raise LedgerUnavailable('shipment_snapshot_too_large')
        ws = self._worksheet(create=True)
        self.records()
        position = self._positions.get(key, max(self._positions.values(), default=0) + 1)
        if hasattr(ws, 'row_count') and position > ws.row_count:
            _quota_call(ws.add_rows, position - ws.row_count)
        expected = [[key, token, '1']]
        try:
            _quota_call(ws.update, expected, range_name=f'A{position}:C{position}', value_input_option='RAW')
        except Exception:
            try:
                if _quota_call(ws.get, f'A{position}:C{position}') != expected:
                    raise ValueError()
            except Exception:
                raise LedgerUnavailable('shipment_ledger_write_unknown') from None
        # A successful Sheets update response is authoritative. For an
        # ambiguous exception, the branch above performs one exact-row
        # readback and never repeats the mutation.
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
        self._worksheet_handle = _create_hidden_worksheet(
            self.lock.book, sheet_id=sid, title=self.title, rows=1000, columns=2,
            failure_code='shipment_guard_write_unknown')
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
        self.lock.assert_owned(fresh=False)
        self.records()
        position = self._positions.get(key,max(self._positions.values(),default=0)+1)
        ws = self._ws(create=True)
        if hasattr(ws, 'row_count') and position > ws.row_count:
            _quota_call(ws.add_rows, position - ws.row_count)
        encoded=json.dumps(data)
        expected = [[key, encoded]]
        try:
            _quota_call(ws.update, expected, range_name=f'A{position}:B{position}', value_input_option='RAW')
        except Exception:
            try:
                if _quota_call(ws.get, f'A{position}:B{position}') != expected:
                    raise ValueError()
            except Exception:
                raise LedgerUnavailable('shipment_guard_write_unknown') from None
        # Successful update responses confirm the write. An ambiguous error
        # is handled by one exact-row readback above.
        self._positions[key]=position
        cached=dict(self._record_cache or [])
        cached[key]=copy.deepcopy(data)
        self._record_cache=list(cached.items())

    def _save_lifecycle_guard(self, stage, row, fields):
        key, order_key = self.identity(row)
        data = self.active.setdefault(key, {'order_key': order_key, 'stage': 'planned'})
        if data.get('stage') == stage and all(data.get(k) == v for k,v in fields.items()):
            return key, data, False
        data.update(stage=stage, updated_at=utc_now(), **fields)
        self._save(key, data)
        return key, data, True

    def lifecycle(self, stage, row, **fields):
        _key, data, changed = self._save_lifecycle_guard(stage, row, fields)
        if not changed:
            return
        try:
            self.lock.stage(stage, package_key=_key, tracking=data.get('tracking',''))
        except Exception:
            # stage() distinguishes a confirmed non-commit (still owned and
            # safe to terminalize) from an unknown/lost owner (retain=True).
            raise

    def prepare_writeback(self, results):
        """Persist each order tombstone, then advance the shared lock once.

        The per-order records are the duplicate-prevention authority. Mirroring
        every row into the single global lock would spend multiple Sheets reads
        per shipment and exhaust the service-account minute quota at the normal
        batch size. Save every tombstone before allowing any target-sheet write.
        """
        results = list(results)
        if not results:
            return
        for result in results:
            self._save_lifecycle_guard(
                'writeback_pending', result,
                {'tracking': str(result.get('tracking') or ''),
                 'drive_file_id': str(result.get('drive_file_id') or '')},
            )
        try:
            self.lock.stage('writeback_pending', shipment_count=len(results))
        except Exception:
            # No target-sheet write starts after this gate fails. Preserve the
            # lock layer's decision: a confirmed non-commit is cleanable, while
            # ambiguous ownership already sets retain=True inside stage().
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
        # Uncertain USPS outcomes are durable per-order tombstones, not evidence
        # that this worker still owns the global mutex. The same order remains
        # blocked by assert_clear(); unrelated orders may continue after this
        # worker reaches finally. Persistence/ownership failures still set retain.
        safe_stages = {'planned', 'issuing', 'unknown', 'label_created', 'uploaded',
                       'writeback_pending', 'completed'}
        return all(record.get('stage') in safe_stages for record in self.active.values())
