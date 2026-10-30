import copy
import unittest
from bot.shipment_ledger import GlobalShipmentLock, ShipmentLedger, LedgerUnavailable, NormalShipmentSafety


class MemorySheet:
    def __init__(self, book, sid, title, hidden=False):
        self.book, self.id, self.title, self.hidden = book, sid, title, hidden
        self.rows = []

    def get_all_values(self):
        return copy.deepcopy(self.rows)

    def get(self, a1_range):
        index=int(a1_range.split(':')[0][1:])-1
        return copy.deepcopy(self.rows[index:index+1])

    def update(self, values, range_name=None, value_input_option=None):
        index = int(range_name.split(':')[0][1:]) - 1
        while len(self.rows) <= index:
            self.rows.append([])
        self.rows[index] = list(values[0])


class MemoryBook:
    def __init__(self):
        self.sheets = {}
        self.calls = []

    def worksheets(self):
        return list(self.sheets.values())

    def get_worksheet_by_id(self, sid):
        return self.sheets[sid]

    def values_get(self, range_name, params=None):
        title = range_name.split('!', 1)[0].strip("'")
        matches = [sheet for sheet in self.sheets.values() if sheet.title == title]
        if len(matches) != 1 or not matches[0].rows:
            raise RuntimeError('range not found')
        return {'values': [[matches[0].rows[0][0]]]}

    def batch_update(self, body):
        self.calls.append(copy.deepcopy(body))
        result = copy.deepcopy(self.sheets)
        for req in body['requests']:
            if 'addSheet' in req:
                p = req['addSheet']['properties']
                if p['sheetId'] in result or any(s.title == p['title'] for s in result.values()):
                    raise RuntimeError('duplicate sheet')
                result[p['sheetId']] = MemorySheet(self, p['sheetId'], p['title'], p.get('hidden', False))
            if 'updateCells' in req:
                p = req['updateCells']
                result[p['start']['sheetId']].rows = [
                    [v['userEnteredValue']['stringValue'] for v in row['values']] for row in p['rows']
                ]
            if 'deleteSheet' in req:
                del result[req['deleteSheet']['sheetId']]
            if 'updateSheetProperties' in req:
                properties = req['updateSheetProperties']['properties']
                target = result[properties['sheetId']]
                if 'title' in properties:
                    if any(sheet.id != target.id and sheet.title == properties['title'] for sheet in result.values()):
                        raise RuntimeError('duplicate sheet title')
                    target.title = properties['title']
                if 'hidden' in properties:
                    target.hidden = properties['hidden']
        # Real Worksheet handles continue to address the same remote ID after
        # unrelated metadata writes; preserve that identity in this fake.
        for sid,sheet in result.items():
            if sid in self.sheets:
                self.sheets[sid].rows=sheet.rows
                self.sheets[sid].title=sheet.title
                self.sheets[sid].hidden=sheet.hidden
                result[sid]=self.sheets[sid]
        self.sheets = result
        return {}


class ShipmentLedgerTests(unittest.TestCase):
    def test_only_explicit_429_rejection_gets_bounded_backoff(self):
        from bot.shipment_ledger import _quota_call
        from types import SimpleNamespace
        waits=[];calls=[]
        class Rejected(Exception):
            def __init__(self,status):self.response=SimpleNamespace(status_code=status)
        def quota():
            calls.append(1)
            if len(calls)<3:raise Rejected(429)
            return 'ok'
        self.assertEqual(_quota_call(quota,sleep_fn=waits.append),'ok')
        self.assertEqual(waits,[1,2])
        self.assertEqual(len(calls),3)
        for error in (TimeoutError(),Rejected(500),Rejected(429)):
            calls.clear();waits.clear()
            def fail():calls.append(1);raise error
            with self.assertRaises(type(error)):_quota_call(fail,sleep_fn=waits.append)
            self.assertEqual(len(calls),3 if getattr(error,'response',None) and error.response.status_code==429 else 1)

    def test_normal_known_tracking_guard_survives_restart_without_encryption_key(self):
        book = MemoryBook()
        lock = GlobalShipmentLock(book).acquire()
        from test_shipment_plans import source
        row = source()
        guard = NormalShipmentSafety(lock)
        guard.assert_clear([row])
        guard.lifecycle('issuing', row)
        guard.lifecycle('label_created', row, tracking='EL000000001JP')
        guard.lifecycle('uploaded', row, tracking='EL000000001JP', drive_file_id='file1')
        self.assertTrue(guard.safe_to_release())
        lock.release()
        next_lock = GlobalShipmentLock(book).acquire()
        with self.assertRaises(LedgerUnavailable):
            NormalShipmentSafety(next_lock).assert_clear([row])
        self.assertNotIn('Test Recipient', str([s.rows for s in book.worksheets()]))

    def test_guard_update_commit_then_timeout_is_verified_without_retaining_global_lock(self):
        book = MemoryBook()
        lock = GlobalShipmentLock(book).acquire()
        from test_shipment_plans import source
        guard = NormalShipmentSafety(lock)
        sheet = guard._ws(create=True)
        real_update = sheet.update
        calls = []

        def commit_then_timeout(*args, **kwargs):
            result = real_update(*args, **kwargs)
            calls.append(1)
            raise TimeoutError('response lost after commit')

        sheet.update = commit_then_timeout
        guard.lifecycle('issuing', source())

        self.assertEqual(calls, [1])
        self.assertFalse(lock.retain)
        self.assertEqual(dict(guard.records())[guard.identity(source())[0]]['stage'], 'issuing')
        lock.release()

    def test_internal_guard_sheet_creation_commit_then_timeout_is_verified_without_retry(self):
        book = MemoryBook()
        lock = GlobalShipmentLock(book).acquire()
        guard = NormalShipmentSafety(lock)
        real_batch_update = book.batch_update
        adds = []

        def commit_guard_sheet_then_timeout(body):
            result = real_batch_update(body)
            if any(request.get('addSheet', {}).get('properties', {}).get('title') == guard.title
                   for request in body['requests']):
                adds.append(1)
                raise TimeoutError('response lost after addSheet')
            return result

        book.batch_update = commit_guard_sheet_then_timeout
        self.assertIsNotNone(guard._ws(create=True))
        self.assertEqual(adds, [1])
        self.assertFalse(lock.retain)
        lock.release()

    def test_internal_ledger_sheet_creation_commit_then_timeout_is_verified_without_retry(self):
        book = MemoryBook()
        lock = GlobalShipmentLock(book).acquire()
        ledger = ShipmentLedger(book, 'x' * 40, lock)
        real_batch_update = book.batch_update
        adds = []

        def commit_ledger_sheet_then_timeout(body):
            result = real_batch_update(body)
            if any(request.get('addSheet', {}).get('properties', {}).get('title') == '_jppost_encrypted_shipments_v1'
                   for request in body['requests']):
                adds.append(1)
                raise TimeoutError('response lost after addSheet')
            return result

        book.batch_update = commit_ledger_sheet_then_timeout
        self.assertIsNotNone(ledger._worksheet(create=True))
        self.assertEqual(adds, [1])
        self.assertFalse(lock.retain)
        lock.release()

    def test_unconfirmed_guard_write_stops_order_but_does_not_freeze_global_writer(self):
        book = MemoryBook()
        lock = GlobalShipmentLock(book).acquire()
        from test_shipment_plans import source
        guard = NormalShipmentSafety(lock)
        sheet = guard._ws(create=True)

        def failed_update(*args, **kwargs):
            raise TimeoutError('write outcome unavailable')

        sheet.update = failed_update
        sheet.get = lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError('readback unavailable'))
        with self.assertRaises(LedgerUnavailable):
            guard.lifecycle('issuing', source())

        self.assertFalse(lock.retain)
        self.assertTrue(lock.held)
        lock.stage('completed', worker_finished=True)
        lock.release()
        self.assertFalse(any(s.title == '_jppost_exclusive_writer_v1' for s in book.worksheets()))

    def test_confirmed_uncommitted_safety_stage_remains_automatically_releasable(self):
        from test_shipment_plans import source

        for stage in ('issuing', 'writeback_pending'):
            with self.subTest(stage=stage):
                book = MemoryBook()
                lock = GlobalShipmentLock(book).acquire()
                safety = NormalShipmentSafety(lock)
                row = source()
                if stage == 'issuing':
                    operation = lambda: safety.lifecycle('issuing', row)
                else:
                    result = {**row, 'order_id': row['注文番号(貼上原始資料)'],
                              'trans_type': 'ePacket', 'shipment_role': 'primary',
                              'tracking': 'EL000000001JP', 'drive_file_id': 'pdf'}
                    operation = lambda: safety.prepare_writeback([result])
                real_batch = book.batch_update

                def fail_global_stage(body):
                    if any('updateCells' in request for request in body['requests']):
                        raise TimeoutError('synthetic known non-commit')
                    return real_batch(body)

                book.batch_update = fail_global_stage
                with self.assertRaises(LedgerUnavailable):
                    operation()
                self.assertFalse(lock.retain)
                book.batch_update = real_batch
                lock.stage('completed', worker_finished=True)
                lock.release()
                self.assertFalse(any(s.title == '_jppost_exclusive_writer_v1'
                                     for s in book.worksheets()))

    def test_unconfirmed_ledger_write_does_not_freeze_global_writer(self):
        book = MemoryBook()
        lock = GlobalShipmentLock(book).acquire()
        ledger = ShipmentLedger(book, 'x' * 40, lock)
        ledger.save('plan1', {'status': 'planned'})
        sheet = ledger._worksheet()

        def failed_update(*args, **kwargs):
            raise TimeoutError('write outcome unavailable')

        sheet.update = failed_update
        sheet.get = lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError('readback unavailable'))
        with self.assertRaises(LedgerUnavailable):
            ledger.save('plan1', {'status': 'issuing'})

        self.assertFalse(lock.retain)
        lock.stage('completed', worker_finished=True)
        lock.release()
        self.assertFalse(any(s.title == '_jppost_exclusive_writer_v1' for s in book.worksheets()))

    def test_encrypted_ledger_update_commit_then_timeout_is_verified_without_retry(self):
        book = MemoryBook()
        lock = GlobalShipmentLock(book).acquire()
        ledger = ShipmentLedger(book, 'x' * 40, lock)
        sheet = ledger._worksheet(create=True)
        real_update = sheet.update
        calls = []

        def commit_then_timeout(*args, **kwargs):
            result = real_update(*args, **kwargs)
            calls.append(1)
            raise TimeoutError('response lost after commit')

        sheet.update = commit_then_timeout
        payload = {'status': 'planned', 'payload': {'name': 'kept private'}}
        ledger.save('plan1', payload)

        self.assertEqual(calls, [1])
        self.assertFalse(lock.retain)
        self.assertEqual(ledger.get('plan1'), payload)
        lock.release()

    def test_atomic_creation_owner_and_two_clients_exclude_each_other(self):
        book = MemoryBook()
        first = GlobalShipmentLock(book, owner='first')
        first.acquire()
        self.assertEqual(set(book.calls[0]['requests'][0]), {'addSheet'})
        self.assertEqual(set(book.calls[0]['requests'][1]), {'updateCells'})
        self.assertEqual(first.marker()['owner'], 'first')
        with self.assertRaises(LedgerUnavailable):
            GlobalShipmentLock(book, owner='second').acquire()
        first.release()
        second = GlobalShipmentLock(book, owner='second').acquire()
        self.assertNotEqual(first.sheet_id, second.sheet_id)
        # Stale release can only target its old generation ID.
        first.release()
        second.assert_owned()

    def test_unknown_acquisition_never_retries_or_deletes(self):
        book = MemoryBook()
        real = book.batch_update
        def timeout_after_commit(body):
            real(body)
            raise TimeoutError()
        book.batch_update = timeout_after_commit
        lock = GlobalShipmentLock(book).acquire()
        self.assertEqual(len(book.calls), 1)
        self.assertEqual(len(book.sheets), 1)
        self.assertTrue(lock.held)
        lock.release()

    def test_encrypted_records_survive_new_client_without_pii(self):
        book = MemoryBook()
        lock = GlobalShipmentLock(book).acquire()
        ledger = ShipmentLedger(book, 'x' * 40, lock)
        data = {'status': 'planned', 'payload': {'Shipping Name': 'Sensitive Recipient'}}
        ledger.save('plan1', data)
        self.assertNotIn('Sensitive Recipient', str([s.rows for s in book.worksheets()]))
        restored = ShipmentLedger(book, 'x' * 40, lock)
        self.assertEqual(restored.get('plan1'), data)
        with self.assertRaises(LedgerUnavailable):
            ShipmentLedger(book, 'y' * 40, lock).get('plan1')
        lock.release()

    def test_missing_key_and_tampered_record_fail_closed(self):
        book = MemoryBook()
        lock = GlobalShipmentLock(book).acquire()
        with self.assertRaises(LedgerUnavailable):
            ShipmentLedger(book, '', lock)
        ledger = ShipmentLedger(book, 'x' * 40, lock)
        ledger.save('plan1', {'status': 'planned'})
        ws = next(s for s in book.worksheets() if s.id != lock.sheet_id)
        ws.rows[0][1] = 'tampered'
        with self.assertRaises(LedgerUnavailable):
            ledger.get('plan1')

    def test_encrypted_cell_limit_checked_before_any_record_write(self):
        book=MemoryBook();lock=GlobalShipmentLock(book).acquire()
        ledger=ShipmentLedger(book,'x'*40,lock)
        with self.assertRaises(LedgerUnavailable):
            ledger.save('huge',{'payload':'x'*40000})
        self.assertIsNone(ledger.get('huge'))


if __name__ == '__main__':
    unittest.main()
