import unittest

from bot.picking_journal import finalize_picking_lock
from bot.shipment_ledger import GlobalShipmentLock, LedgerUnavailable, LOCK_TITLE
from test_shipment_ledger import MemoryBook


def _lock_titles(book):
    return [sheet.title for sheet in book.worksheets()]


class PickingLockFinalizeTests(unittest.TestCase):
    def test_normal_finalize_marks_completed_and_releases(self):
        book = MemoryBook()
        lock = GlobalShipmentLock(book).acquire()
        finalize_picking_lock(lock, 'tx1', sleep=lambda _s: None)
        self.assertFalse(lock.held)
        self.assertNotIn(LOCK_TITLE, _lock_titles(book))

    def test_transient_marker_read_failure_is_rechecked_and_lock_released(self):
        book = MemoryBook()
        lock = GlobalShipmentLock(book).acquire()
        original = book.values_get
        state = {'fail': 1}

        def flaky(range_name, params=None):
            if state['fail']:
                state['fail'] -= 1
                raise TimeoutError('synthetic transient read failure')
            return original(range_name, params=params)

        book.values_get = flaky
        waits = []
        finalize_picking_lock(lock, 'tx1', sleep=waits.append)
        self.assertEqual(waits, [3])
        self.assertFalse(lock.held)
        self.assertFalse(lock.retain)
        self.assertNotIn(LOCK_TITLE, _lock_titles(book))

    def test_ownership_never_confirmed_stays_retained_and_fail_closed(self):
        book = MemoryBook()
        lock = GlobalShipmentLock(book).acquire()

        def always_fail(range_name, params=None):
            raise TimeoutError('synthetic persistent read failure')

        book.values_get = always_fail
        finalize_picking_lock(lock, 'tx1', sleep=lambda _s: None)
        self.assertTrue(lock.held)
        self.assertTrue(lock.retain)
        self.assertIn(LOCK_TITLE, _lock_titles(book))

    def test_different_owner_generation_is_never_released(self):
        book = MemoryBook()
        lock = GlobalShipmentLock(book).acquire()
        other = GlobalShipmentLock(MemoryBook())
        lock.sheet_id, lock.owner = lock.sheet_id, 'someone-else'
        finalize_picking_lock(lock, 'tx1', sleep=lambda _s: None)
        self.assertIn(LOCK_TITLE, _lock_titles(book))
        self.assertTrue(lock.retain)


if __name__ == '__main__':
    unittest.main()
