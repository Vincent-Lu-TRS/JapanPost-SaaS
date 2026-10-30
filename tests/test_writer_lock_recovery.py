import json
import unittest

from bot.shipment_ledger import GlobalShipmentLock, LedgerUnavailable, LOCK_TITLE
from test_shipment_ledger import MemoryBook


class WriterLockRecoveryTests(unittest.TestCase):
    def test_add_sheet_commit_then_timeout_is_resolved_by_readback_without_retry(self):
        book = MemoryBook()
        real_batch_update = book.batch_update
        mutations = []

        def commit_then_timeout(body):
            result = real_batch_update(body)
            if any("addSheet" in request for request in body["requests"]):
                mutations.append("addSheet")
                raise TimeoutError("response lost after commit")
            return result

        book.batch_update = commit_then_timeout
        lock = GlobalShipmentLock(book)
        try:
            lock.acquire()
        except LedgerUnavailable as exc:
            self.fail(f"read-back should confirm this generation was acquired: {exc}")

        self.assertTrue(lock.held)
        self.assertEqual(mutations, ["addSheet"])
        self.assertEqual(len([s for s in book.worksheets() if s.title == LOCK_TITLE]), 1)

    def test_stage_commit_then_timeout_is_resolved_without_second_write(self):
        book = MemoryBook()
        lock = GlobalShipmentLock(book).acquire()
        real_batch_update = book.batch_update
        writes = []

        def commit_then_timeout(body):
            result = real_batch_update(body)
            if any("updateCells" in request for request in body["requests"]):
                writes.append("stage")
                raise TimeoutError("response lost after commit")
            return result

        book.batch_update = commit_then_timeout
        try:
            lock.stage("uploaded", tracking="EL000000001JP")
        except Exception as exc:
            self.fail(f"exact marker read-back should resolve the stage write: {type(exc).__name__}")

        self.assertEqual(writes, ["stage"])
        self.assertEqual(lock.marker()["stage"], "uploaded")

    def test_delete_sheet_commit_then_timeout_is_resolved_by_readback_without_retry(self):
        book = MemoryBook()
        lock = GlobalShipmentLock(book).acquire()
        real_batch_update = book.batch_update
        deletes = []

        def commit_then_timeout(body):
            result = real_batch_update(body)
            if any("deleteSheet" in request for request in body["requests"]):
                deletes.append("deleteSheet")
                raise TimeoutError("response lost after commit")
            return result

        book.batch_update = commit_then_timeout
        try:
            lock.release()
        except LedgerUnavailable as exc:
            self.fail(f"read-back should confirm this generation was released: {exc}")

        self.assertEqual(deletes, ["deleteSheet"])
        self.assertFalse(lock.held)
        self.assertFalse(any(s.title == LOCK_TITLE for s in book.worksheets()))

    def test_archived_generation_fails_ownership_check(self):
        book = MemoryBook()
        lock = GlobalShipmentLock(book).acquire()
        book.sheets[lock.sheet_id].title = "_jppost_writer_retired_test"

        with self.assertRaises(LedgerUnavailable):
            lock.assert_owned()

    def test_critical_stage_rechecks_owner_marker_before_external_side_effect(self):
        book = MemoryBook()
        lock = GlobalShipmentLock(book).acquire()
        marker = json.loads(book.sheets[lock.sheet_id].rows[0][0])
        marker["owner"] = "another-worker"
        book.sheets[lock.sheet_id].rows[0][0] = json.dumps(marker)

        with self.assertRaises(LedgerUnavailable) as caught:
            lock.stage("issuing")

        self.assertEqual(caught.exception.reason_code, "shipment_lock_ownership_lost")
        self.assertTrue(lock.retain)
        self.assertEqual(json.loads(book.sheets[lock.sheet_id].rows[0][0])["owner"], "another-worker")

    def test_terminal_stage_rechecks_owner_before_writing_completion(self):
        book = MemoryBook()
        lock = GlobalShipmentLock(book).acquire()
        marker = json.loads(book.sheets[lock.sheet_id].rows[0][0])
        marker["owner"] = "another-worker"
        book.sheets[lock.sheet_id].rows[0][0] = json.dumps(marker)

        with self.assertRaises(LedgerUnavailable) as caught:
            lock.stage("completed", worker_finished=True)

        self.assertEqual(caught.exception.reason_code, "shipment_lock_ownership_lost")
        self.assertTrue(lock.retain)
        self.assertEqual(json.loads(book.sheets[lock.sheet_id].rows[0][0])["owner"], "another-worker")

    def test_confirmed_uncommitted_stage_error_keeps_owned_lock_cleanable(self):
        book = MemoryBook()
        lock = GlobalShipmentLock(book).acquire()
        real_batch_update = book.batch_update

        def fail_stage_before_commit(body):
            if any("updateCells" in request for request in body["requests"]):
                raise TimeoutError("request failed before mutation")
            return real_batch_update(body)

        book.batch_update = fail_stage_before_commit
        with self.assertRaises(LedgerUnavailable) as caught:
            lock.stage("issuing")

        self.assertEqual(caught.exception.reason_code, "shipment_lock_readback_failed")
        self.assertFalse(lock.retain)
        self.assertTrue(lock.held)
        book.batch_update = real_batch_update
        lock.stage("completed", worker_finished=True)
        lock.release()
        self.assertFalse(any(sheet.title == LOCK_TITLE for sheet in book.worksheets()))

    def test_terminal_release_rechecks_owner_marker_before_deleting(self):
        book = MemoryBook()
        lock = GlobalShipmentLock(book).acquire()
        lock.stage("completed")
        marker = json.loads(book.sheets[lock.sheet_id].rows[0][0])
        marker["owner"] = "another-worker"
        book.sheets[lock.sheet_id].rows[0][0] = json.dumps(marker)

        with self.assertRaises(LedgerUnavailable):
            lock.release()

        self.assertIn(lock.sheet_id, book.sheets)
        self.assertEqual(book.sheets[lock.sheet_id].title, LOCK_TITLE)

    def test_next_acquire_archives_only_completed_generation_and_preserves_marker(self):
        book = MemoryBook()
        first = GlobalShipmentLock(book, owner="first").acquire()
        first.stage("completed", outcome="verified")

        second = GlobalShipmentLock(book, owner="second").acquire()

        archived = book.sheets[first.sheet_id]
        self.assertTrue(archived.title.startswith("_jppost_writer_completed_"))
        self.assertTrue(archived.rows)
        self.assertEqual(second.marker()["owner"], "second")
        with self.assertRaises(LedgerUnavailable):
            first.assert_owned()
        # A stale finally block must not delete the archived evidence.
        first.release()
        self.assertIn(first.sheet_id, book.sheets)
        second.release()

    def test_next_acquire_never_reclaims_nonterminal_generation_regardless_of_age(self):
        book = MemoryBook()
        first = GlobalShipmentLock(book).acquire()
        first.stage("writeback_pending", tracking="EL000000001JP")

        with self.assertRaises(LedgerUnavailable):
            GlobalShipmentLock(book).acquire()

        self.assertEqual(book.sheets[first.sheet_id].title, LOCK_TITLE)
        self.assertEqual(first.marker()["stage"], "writeback_pending")

    def test_terminal_archive_commit_then_timeout_is_resolved_without_retry(self):
        book = MemoryBook()
        first = GlobalShipmentLock(book).acquire()
        first.stage("completed")
        real_batch_update = book.batch_update
        archives = []

        def archive_then_timeout(body):
            result = real_batch_update(body)
            if any("updateSheetProperties" in request for request in body["requests"]):
                archives.append("archive")
                raise TimeoutError("response lost after archive")
            return result

        book.batch_update = archive_then_timeout
        second = GlobalShipmentLock(book).acquire()

        self.assertEqual(archives, ["archive"])
        self.assertNotEqual(first.sheet_id, second.sheet_id)
        self.assertEqual(book.sheets[first.sheet_id].title.startswith("_jppost_writer_completed_"), True)


if __name__ == "__main__":
    unittest.main()
