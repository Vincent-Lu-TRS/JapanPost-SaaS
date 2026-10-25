import copy
import unittest
from bot.shipment_ledger import GlobalShipmentLock, ShipmentLedger, LedgerUnavailable, NormalShipmentSafety


class MemorySheet:
    def __init__(self, book, sid, title):
        self.book, self.id, self.title = book, sid, title
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

    def batch_update(self, body):
        self.calls.append(copy.deepcopy(body))
        result = copy.deepcopy(self.sheets)
        for req in body['requests']:
            if 'addSheet' in req:
                p = req['addSheet']['properties']
                if p['sheetId'] in result or any(s.title == p['title'] for s in result.values()):
                    raise RuntimeError('duplicate sheet')
                result[p['sheetId']] = MemorySheet(self, p['sheetId'], p['title'])
            if 'updateCells' in req:
                p = req['updateCells']
                result[p['start']['sheetId']].rows = [
                    [v['userEnteredValue']['stringValue'] for v in row['values']] for row in p['rows']
                ]
            if 'deleteSheet' in req:
                del result[req['deleteSheet']['sheetId']]
        # Real Worksheet handles continue to address the same remote ID after
        # unrelated metadata writes; preserve that identity in this fake.
        for sid,sheet in result.items():
            if sid in self.sheets:
                self.sheets[sid].rows=sheet.rows
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
        with self.assertRaises(LedgerUnavailable):
            first.release()
        second.assert_owned()

    def test_unknown_acquisition_never_retries_or_deletes(self):
        book = MemoryBook()
        real = book.batch_update
        def timeout_after_commit(body):
            real(body)
            raise TimeoutError()
        book.batch_update = timeout_after_commit
        with self.assertRaises(LedgerUnavailable):
            GlobalShipmentLock(book).acquire()
        self.assertEqual(len(book.calls), 1)
        self.assertEqual(len(book.sheets), 1)

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
