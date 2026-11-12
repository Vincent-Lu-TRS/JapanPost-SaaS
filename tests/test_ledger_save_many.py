import unittest

from bot.picking_journal import PickingJournal
from bot.shipment_ledger import GlobalShipmentLock, LedgerUnavailable, ShipmentLedger
from test_picking_reliability import compact_snapshot_fixture
from test_shipment_ledger import MemoryBook


class LedgerSaveManyQuotaTests(unittest.TestCase):
    def _journal(self):
        book = MemoryBook()
        lock = GlobalShipmentLock(book).acquire()
        return book, lock, PickingJournal(ShipmentLedger(book, 'x' * 40, lock))

    def test_large_snapshot_uses_few_sheet_requests_and_round_trips(self):
        book, lock, journal = self._journal()
        snapshot = compact_snapshot_fixture(8000)
        journal.save_source_snapshot(snapshot)
        sheet = next(s for s in book.worksheets() if s.title == '_jppost_encrypted_shipments_v1')
        # ~60 shards used to cost >120 requests (over the 60/min quota).
        self.assertGreater(len(sheet.rows), 40)
        self.assertLessEqual(sheet.update_calls, 6)
        self.assertEqual(journal.load_source_snapshot(), snapshot)
        lock.release()

    def test_rewriting_the_snapshot_keeps_the_previous_head_until_commit(self):
        book, lock, journal = self._journal()
        first = compact_snapshot_fixture(500)
        journal.save_source_snapshot(first)
        second = compact_snapshot_fixture(600)
        journal.save_source_snapshot(second)
        self.assertEqual(journal.load_source_snapshot(), second)
        lock.release()

    def test_failed_batch_write_leaves_committed_snapshot_readable(self):
        book, lock, journal = self._journal()
        first = compact_snapshot_fixture(500)
        journal.save_source_snapshot(first)
        sheet = next(s for s in book.worksheets() if s.title == '_jppost_encrypted_shipments_v1')
        real_update = sheet.update

        def broken(values, range_name=None, value_input_option=None):
            raise TimeoutError('synthetic write failure')

        sheet.update = broken
        with self.assertRaises(LedgerUnavailable):
            journal.save_source_snapshot(compact_snapshot_fixture(700))
        sheet.update = real_update
        self.assertEqual(journal.load_source_snapshot(), first)
        lock.release()


if __name__ == '__main__':
    unittest.main()
