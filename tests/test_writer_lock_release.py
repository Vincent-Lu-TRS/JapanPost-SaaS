"""手動解除共用鎖：只改名封存、需達最低建立時間、且鎖必須與使用者剛看到的一致。"""
import unittest
from datetime import datetime, timedelta, timezone

from bot.shipment_ledger import (
    GlobalShipmentLock, LOCK_TITLE, LockReleaseRefused,
    inspect_writer_lock, release_stuck_writer_lock,
)
from tests.test_shipment_ledger import MemoryBook


def _locked_book():
    book = MemoryBook()
    lock = GlobalShipmentLock(book)
    lock.acquire()
    return book, lock


class WriterLockReleaseTests(unittest.TestCase):
    def test_inspect_returns_none_without_lock(self):
        self.assertIsNone(inspect_writer_lock(MemoryBook()))

    def test_inspect_reports_stage_and_age(self):
        book, lock = _locked_book()
        later = datetime.now(timezone.utc) + timedelta(minutes=10)
        info = inspect_writer_lock(book, now=later)
        self.assertEqual(info['generation'], lock.sheet_id)
        self.assertEqual(info['stage'], 'locked')
        self.assertGreaterEqual(info['age_seconds'], 590)

    def test_release_renames_without_deleting_and_allows_new_lock(self):
        book, lock = _locked_book()
        later = datetime.now(timezone.utc) + timedelta(minutes=10)
        title = release_stuck_writer_lock(lock.sheet_id, book=book, now=later)
        self.assertTrue(title.startswith('_jppost_writer_retired_'))
        retired = book.sheets[lock.sheet_id]
        self.assertEqual(retired.title, title)
        self.assertTrue(retired.hidden)
        self.assertIn('"stage": "locked"', retired.rows[0][0])  # 標記內容保留
        self.assertIsNone(inspect_writer_lock(book))
        fresh = GlobalShipmentLock(book)
        fresh.acquire()  # 解除後可重新取得新鎖
        self.assertTrue(fresh.held)

    def test_recent_lock_is_refused(self):
        book, lock = _locked_book()
        with self.assertRaises(LockReleaseRefused):
            release_stuck_writer_lock(lock.sheet_id, book=book)
        self.assertEqual(book.sheets[lock.sheet_id].title, LOCK_TITLE)

    def test_changed_generation_is_refused(self):
        book, lock = _locked_book()
        later = datetime.now(timezone.utc) + timedelta(minutes=10)
        with self.assertRaises(LockReleaseRefused):
            release_stuck_writer_lock(lock.sheet_id + 1, book=book, now=later)
        self.assertEqual(book.sheets[lock.sheet_id].title, LOCK_TITLE)

    def test_missing_lock_is_refused(self):
        with self.assertRaises(LockReleaseRefused):
            release_stuck_writer_lock(1, book=MemoryBook())


if __name__ == '__main__':
    unittest.main()
