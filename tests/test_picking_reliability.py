"""Picking safety regressions. All providers are in-memory; no live API access."""
import copy
import hashlib
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_picking_labels import _header, _row
from bot import picking_labels as labels
from bot.shipment_ledger import GlobalShipmentLock, ShipmentLedger, LedgerUnavailable, NormalShipmentSafety
from test_shipment_ledger import MemoryBook


def values(*rows):
    return [_header(1), *rows]


def order_rows(*rows):
    return labels.parse_picking_label_candidates(values(*rows))[0]


def compact_snapshot_fixture(count, done='NOT_DONE'):
    result = {}
    for i in range(count):
        key = hashlib.sha256(('identity-' + str(i)).encode()).hexdigest()
        result[key] = {'key': key, 'row': i + 2, 'done': done,
                       'fingerprint': hashlib.sha256(str(i).encode()).hexdigest(),
                       'manual_observed': done == 'DONE', 'untrusted_done': False}
    return result


class FakeDrive:
    def __init__(self):
        self.files = {}; self.creates = []; self.allocations = 0
        self.timeout = False; self.invisible = False
    def reserve_id(self):
        self.allocations += 1
        return 'reserved-' + str(self.allocations)
    def upload(self, path, record):
        self.creates.append(record['file_id'])
        data = Path(path).read_bytes()
        self.files[record['file_id']] = {
            'id': record['file_id'], 'name': record['filename'], 'mimeType': 'application/pdf',
            'parents': ['folder'], 'trashed': False,
            'appProperties': {'picking_tx': record['txid'], 'picking_manifest': record['manifest_hash']},
            'md5Checksum': hashlib.md5(data).hexdigest(), 'size': str(len(data)),
        }
        if self.timeout: raise TimeoutError('upload response lost')
    def get(self, record):
        if self.invisible: raise TimeoutError('metadata unavailable')
        return copy.deepcopy(self.files.get(record['file_id']))


class PickingReliabilityTests(unittest.TestCase):
    def module(self):
        path = Path(labels.__file__).with_name('picking_journal.py')
        self.assertTrue(path.exists(), 'durable picking journal must exist')
        return importlib.import_module('bot.picking_journal')
    def setup_run(self, raw=None):
        mod = self.module()
        book = MemoryBook(); drive = FakeDrive()
        source = raw or values(_row(max_items=1))
        calls = {'render': 0, 'mark': []}
        def render(orders, path):
            calls['render'] += 1; Path(path).write_bytes(b'%PDF-1.4 picking')
        def mark(orders):
            calls['mark'].append([o.order_no for o in orders])
            for order in orders:
                source[order.source_row_number - 1][2] = '已製單'
            return {labels.picking_order_key(o): {'status': 'verified', 'row': o.source_row_number} for o in orders}
        def run(orders=None, mark_fn=None):
            lock = GlobalShipmentLock(book)
            journal = mod.PickingJournal(ShipmentLedger(book, 'x' * 40, lock))
            with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "tmp", ignore_cleanup_errors=True) as directory:
                return mod.execute_picking_batch(
                    orders or labels.parse_picking_label_candidates(source)[0], output_dir=directory,
                    source_loader=lambda: mod.picking_source_snapshot(source), journal=journal, lock=lock,
                    list_files=lambda prefix: [], reserve_file_id=drive.reserve_id,
                    upload_file=drive.upload, get_file=drive.get,
                    mark_done=mark_fn or mark, render=render, folder_id='folder', now='2026-11-11 12:00:00')
        return mod, book, source, drive, calls, run
    def test_resolver_never_first_matches_another_seller(self):
        raw = values(_row(order_no='same', max_items=1), _row(order_no='same', max_items=1))
        raw[2][4] = 'seller B'
        selected = order_rows(_row(order_no='same', max_items=1))[0]
        selected.order_source = 'missing seller'
        with self.assertRaises(ValueError): labels.resolve_picking_done_row_numbers(raw, [selected])
    def test_resolver_rejects_duplicate_identity(self):
        raw = values(_row(max_items=1), _row(max_items=1))
        selected = order_rows(_row(max_items=1))[0]
        with self.assertRaises(ValueError): labels.resolve_picking_done_row_numbers(raw, [selected])
    def test_keys_and_fingerprints_follow_business_identity_and_payload(self):
        self.assertTrue(hasattr(labels, 'picking_order_key'), 'stable picking key required')
        a = order_rows(_row(max_items=1))[0]; b = copy.deepcopy(a); b.source_row_number = 99
        self.assertEqual(labels.picking_order_key(a), labels.picking_order_key(b))
        self.assertEqual(labels.picking_render_fingerprint(a), labels.picking_render_fingerprint(b))
        b.order_source = 'seller B'
        self.assertNotEqual(labels.picking_order_key(a), labels.picking_order_key(b))
        for field in ('sku', 'name', 'jan', 'quantity', 'progress'):
            b = copy.deepcopy(a); setattr(b.items[0], field, 'changed')
            self.assertNotEqual(labels.picking_render_fingerprint(a), labels.picking_render_fingerprint(b))
    def test_restart_after_upload_mark_failure_only_repairs_same_pdf(self):
        mod, book, source, drive, calls, run = self.setup_run()
        first = run(mark_fn=lambda orders: None)
        self.assertFalse(first.success)
        second = run([first_order for first_order in labels.parse_picking_label_candidates(source)[0]])
        self.assertTrue(second.success)
        self.assertEqual((calls['render'], drive.allocations, len(drive.creates)), (1, 1, 1))
        self.assertEqual(first.drive_file['id'], second.drive_file['id'])
    def test_partial_mark_only_retries_unverified_keys(self):
        mod, book, source, drive, calls, run = self.setup_run(values(_row(order_no='a', max_items=1), _row(order_no='b', max_items=1)))
        def partial(orders):
            source[1][2] = '已製單'
            return {labels.picking_order_key(orders[0]): {'status': 'verified', 'row': 2}}
        self.assertFalse(run(mark_fn=partial).success)
        self.assertTrue(run().success)
        self.assertEqual(calls['mark'], [['b']])
        self.assertEqual(len(drive.creates), 1)
    def test_upload_timeout_committed_is_recovered_by_exact_id(self):
        mod, book, source, drive, calls, run = self.setup_run(); drive.timeout = True
        self.assertTrue(run().success)
        self.assertEqual(len(drive.creates), 1)
    def test_unknown_upload_never_allocates_or_renders_again(self):
        mod, book, source, drive, calls, run = self.setup_run(); drive.invisible = True
        self.assertFalse(run().success); self.assertFalse(run().success)
        self.assertEqual((calls['render'], drive.allocations, len(drive.creates)), (1, 1, 1))
        self.assertEqual(calls['mark'], [])
    def test_changed_selected_payload_stops_before_any_side_effect(self):
        mod, book, source, drive, calls, run = self.setup_run()
        selected = labels.parse_picking_label_candidates(source)[0]; source[1][6] = 'different SKU'
        with self.assertRaises(ValueError): run(selected)
        self.assertEqual((calls['render'], drive.allocations, len(drive.creates)), (0, 0, 0))
    def test_selected_key_survives_row_move_without_replacing_order(self):
        mod, book, source, drive, calls, run = self.setup_run()
        selected = labels.parse_picking_label_candidates(source)[0]; source.insert(1, _row(order_no='new', max_items=1))
        self.assertTrue(run(selected).success); self.assertEqual(calls['mark'], [['imy1']])
    def test_load_has_no_mutation_manual_baseline_and_inherited_l_require_review(self):
        mod, book, source, drive, calls, run = self.setup_run(values(_row(done='已製單', max_items=1), _row(order_no='pending', max_items=1)))
        journal = mod.PickingJournal(ShipmentLedger(book, 'x' * 40))
        first = mod.classify_picking_snapshot(mod.picking_source_snapshot(source), journal)
        self.assertEqual(book.calls, []); self.assertEqual(first['done'][0]['provenance'], 'manual_observed')
        self.assertTrue(run().success)
        source[1][0] = 'new-identity'
        fresh = mod.PickingJournal(ShipmentLedger(book, 'x' * 40))
        state = mod.classify_picking_snapshot(mod.picking_source_snapshot(source), fresh)
        self.assertTrue(state['review']); self.assertEqual(source[1][2], '已製單')
    def test_manual_reset_keeps_original_pdf_link(self):
        mod, book, source, drive, calls, run = self.setup_run()
        self.assertTrue(run().success)
        source[1][2] = 'FALSE'
        journal = mod.PickingJournal(ShipmentLedger(book, 'x' * 40))
        state = mod.classify_picking_snapshot(mod.picking_source_snapshot(source), journal)
        self.assertEqual(state['pending'], []); self.assertTrue(state['mirror_pending'])
        self.assertEqual(state['mirror_pending'][0]['file_id'], 'reserved-1')
    def test_snapshot_is_sharded_and_failed_head_save_has_no_upload(self):
        mod, book, source, drive, calls, run = self.setup_run()
        lock = GlobalShipmentLock(book).acquire(); ledger = ShipmentLedger(book, 'x' * 40, lock)
        journal = mod.PickingJournal(ledger)
        snapshot = compact_snapshot_fixture(1200, done='DONE')
        journal.save_source_snapshot(snapshot)
        self.assertEqual(journal.load_source_snapshot(), snapshot)
        self.assertTrue(all(len(row[1]) < 45000 for sheet in book.worksheets() if sheet.title.endswith('shipments_v1') for row in sheet.rows))
        lock.release()
        with patch.object(ShipmentLedger, 'save', side_effect=LedgerUnavailable('failed')):
            with self.assertRaises(LedgerUnavailable): run()
        self.assertEqual((calls['render'], drive.allocations, len(drive.creates)), (0, 0, 0))
    def test_mixed_records_do_not_break_postal_but_unknown_records_block(self):
        mod, book, source, drive, calls, run = self.setup_run(); self.assertTrue(run().success)
        lock = GlobalShipmentLock(book).acquire()
        from test_shipment_plans import source as postal_source
        with patch('bot.shipment_ledger.encryption_secret', return_value='x' * 40):
            NormalShipmentSafety(lock).assert_clear([postal_source()])
        ledger = ShipmentLedger(book, 'x' * 40, lock); ledger.save('unknown', {'kind': 'mystery'})
        with patch('bot.shipment_ledger.encryption_secret', return_value='x' * 40):
            with self.assertRaises(LedgerUnavailable): NormalShipmentSafety(lock).assert_clear([postal_source()])
        lock.release()
    def test_two_writers_cannot_both_run(self):
        mod, book, source, drive, calls, run = self.setup_run(); held = GlobalShipmentLock(book).acquire()
        with self.assertRaises(LedgerUnavailable): run()
        self.assertEqual(drive.creates, []); held.release()


class VerifiedPickingWriteTests(unittest.TestCase):
    def helper(self):
        from bot import sheets
        self.assertTrue(hasattr(sheets, 'mark_picking_done_verified'), 'verified key writeback helper required')
        return sheets.mark_picking_done_verified
    def test_timeout_commit_is_read_back_without_repeating_write(self):
        helper = self.helper(); raw = values(_row(max_items=1)); orders = order_rows(_row(max_items=1))
        writes = []
        def write(rows):
            writes.append(rows); raw[1][2] = '已製單'; raise TimeoutError()
        result = helper(orders, source_loader=lambda: copy.deepcopy(raw), write_rows=write)
        self.assertEqual(result[labels.picking_order_key(orders[0])]['status'], 'verified'); self.assertEqual(writes, [[2]])
    def test_silent_partial_and_identity_drift_are_not_verified(self):
        helper = self.helper(); raw = values(_row(max_items=1)); orders = order_rows(_row(max_items=1))
        self.assertEqual(helper(orders, source_loader=lambda: raw, write_rows=lambda rows: None)[labels.picking_order_key(orders[0])]['status'], 'failed')
        def drift(rows): raw[1][0] = 'other'; raw[1][2] = '已製單'
        self.assertEqual(helper(orders, source_loader=lambda: raw, write_rows=drift)[labels.picking_order_key(orders[0])]['status'], 'review')
        self.assertEqual(raw[1][2], '已製單')

class PickingIntegrationContractTests(unittest.TestCase):
    def test_selection_refresh_tracks_key_and_payload_and_resets_editor_epoch(self):
        import features.picking_labels as ui
        from refresh_payloads import PickingPayload
        original = order_rows(_row(order_no='a', max_items=1), _row(order_no='b', max_items=1))
        with patch.object(ui.st, 'session_state', {}):
            ui.apply_picking_payload(PickingPayload(tuple(original), (), {}), preserve_selection=False)
            selected_key = labels.picking_order_key(original[0])
            ui.st.session_state['picking_selected_keys'] = {selected_key}
            epoch = ui.st.session_state.get('picking_editor_epoch', 0)
            moved = copy.deepcopy(list(reversed(original))); moved[1].source_row_number = 8
            ui.apply_picking_payload(PickingPayload(tuple(moved), (), {}), preserve_selection=True)
            self.assertEqual(ui.st.session_state['picking_selected_rows'], {8})
            self.assertGreater(ui.st.session_state['picking_editor_epoch'], epoch)
            moved[1].items[0].quantity = '99'
            ui.apply_picking_payload(PickingPayload(tuple(moved), (), {}), preserve_selection=True)
            self.assertEqual(ui.st.session_state['picking_selected_keys'], set())
    def test_drive_adapter_uses_reserved_id_and_shared_drive_metadata_contract(self):
        from bot import drive
        self.assertTrue(hasattr(drive, 'reserve_picking_file_id'), 'reserved Drive adapter required')
        calls = []
        class Request:
            def __init__(self, result): self.result = result
            def execute(self): return self.result
        class Files:
            def generateIds(self, **kwargs): calls.append(('ids', kwargs)); return Request({'ids': ['reserved']})
            def create(self, **kwargs): calls.append(('create', kwargs)); return Request({'id': 'reserved'})
            def get(self, **kwargs): calls.append(('get', kwargs)); return Request({'id': 'reserved'})
        service = type('Service', (), {'files': lambda self: Files()})()
        with patch.object(drive, '_get_drive_service', return_value=service), patch.object(drive, 'MediaIoBaseUpload', side_effect=lambda *a, **k: object()):
            self.assertEqual(drive.reserve_picking_file_id(), 'reserved')
            with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / 'tmp', ignore_cleanup_errors=True) as directory:
                path = Path(directory) / 'label.pdf'; path.write_bytes(b'%PDF')
                drive.upload_reserved_picking_pdf(str(path), 'folder', {'file_id': 'reserved', 'filename': 'label.pdf', 'txid': 'tx', 'manifest_hash': 'hash'})
            drive.get_reserved_picking_pdf({'file_id': 'reserved'})
        create = next(kwargs for name, kwargs in calls if name == 'create')
        get = next(kwargs for name, kwargs in calls if name == 'get')
        self.assertEqual(create['body']['id'], 'reserved'); self.assertTrue(create['supportsAllDrives']); self.assertTrue(get['supportsAllDrives'])
        self.assertEqual(create['body']['appProperties'], {'picking_tx': 'tx', 'picking_manifest': 'hash'})
        self.assertIn('md5Checksum', get['fields'])
    def test_known_inherited_done_does_not_become_trusted_after_other_work(self):
        mod, book, source, drive, calls, run = PickingReliabilityTests().setup_run(values(_row(done='已製單', max_items=1), _row(order_no='pending', max_items=1)))
        self.assertTrue(run().success)
        source[1][0] = 'new-identity'; source.append(_row(order_no='later', max_items=1))
        self.assertTrue(run([labels.parse_picking_label_candidates(source)[0][-1]]).success)
        journal = mod.PickingJournal(ShipmentLedger(book, 'x' * 40))
        state = mod.classify_picking_snapshot(mod.picking_source_snapshot(source), journal)
        self.assertTrue(any(item['order'].order_no == 'new-identity' for item in state['review']))
    def test_verified_mark_uses_fresh_deadline_snapshot(self):
        from bot import sheets
        from bot.picking_journal import picking_source_snapshot
        raw = values(_row(max_items=1)); snap = picking_source_snapshot(raw, {'imy1': '2026/11/12 00:00'})
        orders = [next(iter(snap.values()))['order']]
        def load(): return picking_source_snapshot(raw, {'imy1': '2026/11/12 00:00'})
        def write(rows): raw[1][2] = '已製單'
        outcomes = sheets.mark_picking_done_verified(orders, source_loader=load, write_rows=write)
        self.assertEqual(outcomes[labels.picking_order_key(orders[0])]['status'], 'verified')

class PickingAdversarialTests(unittest.TestCase):
    def test_postwrite_key_moved_to_prechecked_row_is_review(self):
        from bot.sheets import mark_picking_done_verified
        raw = values(_row(order_no='a', max_items=1), _row(order_no='b', done='已製單', max_items=1))
        selected = labels.parse_picking_label_candidates(raw)[0]
        writes = []
        def move(rows):
            writes.append(rows)
            raw[1], raw[2] = raw[2][:2] + [raw[1][2]] + raw[2][3:], raw[1][:2] + [raw[2][2]] + raw[1][3:]
            raw[1][2] = '已製單'
        outcome = mark_picking_done_verified(selected, source_loader=lambda: raw, write_rows=move)
        self.assertEqual(outcome[labels.picking_order_key(selected[0])]['status'], 'review')
        self.assertEqual(writes, [[2]]); self.assertEqual(raw[1][2], '已製單')
    def test_malformed_picking_head_cannot_hide_from_postal(self):
        from bot.picking_journal import PickingJournal
        from bot.shipment_ledger import postal_ledger_records
        book = MemoryBook(); lock = GlobalShipmentLock(book).acquire()
        ledger = ShipmentLedger(book, 'x' * 40, lock); journal = PickingJournal(ledger)
        key = 'picking:v1:batch:' + journal.binding + ':tx'
        ledger.save(key, {'type': 'picking:v1', 'kind': 'batch', 'key': key, 'binding': journal.binding,
                          'chunks': ['picking:v1:chunk:missing'], 'digest': 'a' * 64})
        with self.assertRaises(LedgerUnavailable): postal_ledger_records(ledger)
        lock.release()
    def test_upload_started_persistence_failure_prevents_upload_and_second_render(self):
        mod, book, source, drive, calls, run = PickingReliabilityTests().setup_run()
        real_save = mod.PickingJournal.save_batch
        def save(journal, record):
            if record['stage'] == 'upload_started': raise LedgerUnavailable('injected checkpoint failure')
            return real_save(journal, record)
        with patch.object(mod.PickingJournal, 'save_batch', new=save):
            with self.assertRaises(LedgerUnavailable): run()
        self.assertEqual(drive.creates, []); self.assertEqual(calls['mark'], [])
        self.assertFalse(run().success)
        self.assertEqual(calls['render'], 1)
    def test_uploaded_receipt_persistence_failure_never_marks_then_restart_repairs(self):
        mod, book, source, drive, calls, run = PickingReliabilityTests().setup_run()
        real_save = mod.PickingJournal.save_batch
        def save(journal, record):
            if record['stage'] == 'uploaded_verified': raise LedgerUnavailable('injected receipt failure')
            return real_save(journal, record)
        with patch.object(mod.PickingJournal, 'save_batch', new=save):
            with self.assertRaises(LedgerUnavailable): run()
        self.assertEqual(calls['mark'], [])
        self.assertTrue(run().success)
        self.assertEqual((calls['render'], drive.allocations, len(drive.creates)), (1, 1, 1))
    def test_snapshot_head_failure_keeps_previous_committed_generation(self):
        from bot.picking_journal import PickingJournal
        book = MemoryBook(); lock = GlobalShipmentLock(book).acquire(); ledger = ShipmentLedger(book, 'x' * 40, lock)
        journal = PickingJournal(ledger); old = compact_snapshot_fixture(1, done='DONE'); journal.save_source_snapshot(old)
        real_save = ledger.save
        def save(key, record):
            if record['kind'] == 'snapshot': raise LedgerUnavailable('head failed')
            return real_save(key, record)
        with patch.object(ledger, 'save', side_effect=save):
            with self.assertRaises(LedgerUnavailable): journal.save_source_snapshot(compact_snapshot_fixture(1))
        self.assertEqual(PickingJournal(ShipmentLedger(book, 'x' * 40)).load_source_snapshot(), old)
        lock.release()
    def test_wrong_pdf_metadata_never_marks_or_reuploads(self):
        for field, bad in [('id','wrong'), ('mimeType','text/plain'), ('parents',['other']), ('trashed', True),
                           ('md5Checksum','wrong'), ('size','0'), ('sha256Checksum','wrong'), ('shortcutDetails', {})]:
            with self.subTest(field=field):
                mod, book, source, drive, calls, run = PickingReliabilityTests().setup_run()
                original_get = drive.get
                def tampered(record):
                    metadata = original_get(record); metadata[field] = bad; return metadata
                drive.get = tampered
                self.assertFalse(run().success); self.assertFalse(run().success)
                self.assertEqual(calls['mark'], []); self.assertEqual(len(drive.creates), 1)
        for app_property in ('picking_tx', 'picking_manifest'):
            mod, book, source, drive, calls, run = PickingReliabilityTests().setup_run()
            original_get = drive.get
            def tampered(record):
                metadata = original_get(record); metadata['appProperties'][app_property] = 'wrong'; return metadata
            drive.get = tampered
            self.assertFalse(run().success); self.assertEqual(calls['mark'], [])
    def test_large_batch_complete_manifest_and_recovery_are_sharded(self):
        raw = values(*[_row(order_no='order-' + str(i), max_items=1) for i in range(600)])
        mod, book, source, drive, calls, run = PickingReliabilityTests().setup_run(raw)
        first = run(mark_fn=lambda orders: None); self.assertFalse(first.success)
        journal = mod.PickingJournal(ShipmentLedger(book, 'x' * 40))
        self.assertEqual(len(journal.batches()[0]['manifest']), 600)
        self.assertTrue(run().success); self.assertEqual((calls['render'], len(drive.creates)), (1, 1))
        self.assertTrue(all(len(row[1]) < 45000 for sheet in book.worksheets() if sheet.title.endswith('shipments_v1') for row in sheet.rows))

class LegacyPickingTransactionContractTests(unittest.TestCase):
    def test_none_mark_callback_cannot_report_success(self):
        selected = order_rows(_row(max_items=1))
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / 'tmp', ignore_cleanup_errors=True) as directory:
            result = labels.generate_picking_labels_transaction(selected, directory, lambda prefix: [],
                                                               lambda path: {'id': 'file'}, lambda rows: None)
        self.assertFalse(result.success)
    def test_none_upload_cannot_mark_source(self):
        selected = order_rows(_row(max_items=1)); marks = []
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / 'tmp', ignore_cleanup_errors=True) as directory:
            result = labels.generate_picking_labels_transaction(selected, directory, lambda prefix: [],
                                                               lambda path: None, lambda rows: marks.append(rows))
        self.assertFalse(result.success); self.assertEqual(marks, [])

class PickingSnapshotCapacityTests(unittest.TestCase):
    def test_repeated_source_snapshots_use_bounded_two_slots(self):
        from bot.picking_journal import PickingJournal
        book = MemoryBook(); lock = GlobalShipmentLock(book).acquire(); ledger = ShipmentLedger(book, 'x' * 40, lock)
        journal = PickingJournal(ledger)
        sample = compact_snapshot_fixture(1000)
        journal.save_source_snapshot(sample)
        first_count = len(ledger.records()) - 1
        for generation in range(8):
            sample[next(iter(sample))]['fingerprint'] = str(generation) * 64
            journal.save_source_snapshot(sample)
            self.assertEqual(journal.load_source_snapshot(), sample)
        self.assertLessEqual(len(ledger.records()), 2 * first_count + 1)
        lock.release()

class PickingSelectionReloadTests(unittest.TestCase):
    def test_reload_preserves_only_same_key_and_payload_selection(self):
        import features.picking_labels as ui
        from refresh_payloads import PickingPayload
        original = order_rows(_row(order_no='a', max_items=1), _row(order_no='b', max_items=1))
        with patch.object(ui.st, 'session_state', {}):
            ui.apply_picking_payload(PickingPayload(tuple(original), (), {}), preserve_selection=False)
            selected = labels.picking_order_key(original[0])
            ui.st.session_state['picking_selected_keys'] = {selected}
            changed = copy.deepcopy(original); changed[0].items[0].quantity = '9'
            with patch.object(ui, 'load_picking_payload', return_value=PickingPayload(tuple(changed), (), {})):
                ui._load_orders()
            self.assertEqual(ui.st.session_state['picking_selected_keys'], set())

class PickingSourceShapeTests(unittest.TestCase):
    def test_anchored_source_with_eleven_items_fingerprints_and_paginates_all(self):
        from test_picking_labels import _anchored_duplicate_header, _anchored_duplicate_row
        raw = [_anchored_duplicate_header(11), _anchored_duplicate_row(max_items=11, items=[('S'+str(i),'name','jan','1','') for i in range(11)])]
        from bot.picking_journal import picking_source_snapshot
        order = next(iter(picking_source_snapshot(raw).values()))['order']
        self.assertEqual(len(order.items), 11)
        self.assertEqual([len(page.items) for page in labels.build_picking_pdf_pages([order])], [10, 1])
    def test_raw_seller_collisions_are_detected_independent_of_normalized_rows(self):
        from test_picking_labels import _anchored_duplicate_header, _anchored_duplicate_row
        from bot.picking_journal import picking_source_snapshot
        a = _anchored_duplicate_row(order_no='order-a', max_items=1)
        a[1], a[2] = 'sellerA', 'same-order'
        b = [''] * len(a); b[1], b[2] = 'sellerB', 'same-order'
        c = _anchored_duplicate_row(order_no='same-order', max_items=1)
        snap = picking_source_snapshot([_anchored_duplicate_header(1), a, b, c])
        self.assertTrue(any(entry['review'] for entry in snap.values() if entry['order'].order_no == 'same-order'))

class PickingMixedConsumerTests(unittest.TestCase):
    def test_write_window_swap_leaves_innocent_other_key_visible_for_review(self):
        from bot.sheets import mark_picking_done_verified
        mod, book, source, drive, calls, run = PickingReliabilityTests().setup_run(values(_row(order_no='a', max_items=1), _row(order_no='b', max_items=1)))
        selected = labels.parse_picking_label_candidates(source)[0][:1]
        def mark(orders):
            def swap(rows):
                source[1], source[2] = source[2], source[1]
                source[1][2] = '已製單'
            return mark_picking_done_verified(orders, source_loader=lambda: source, write_rows=swap)
        self.assertFalse(run(selected, mark_fn=mark).success)
        state = mod.classify_picking_snapshot(mod.picking_source_snapshot(source), mod.PickingJournal(ShipmentLedger(book, 'x' * 40)))
        b = next(item for item in state['review'] if item['order'].order_no == 'b')
        self.assertEqual(b['done'], 'DONE'); self.assertEqual(source[1][2], '已製單')
    def test_valid_receipts_and_uncommitted_snapshot_chunks_preserve_postal_history_and_replay(self):
        from test_shipment_service import ServiceTests
        from bot.shipment_service import lookup_history
        mod, book, source, drive, calls, run = PickingReliabilityTests().setup_run(); self.assertTrue(run().success)
        lock = GlobalShipmentLock(book).acquire(); ledger = ShipmentLedger(book, 'x' * 40, lock); journal = mod.PickingJournal(ledger)
        real_save = ledger.save
        def save(key, record):
            if record['kind'] == 'snapshot': raise LedgerUnavailable('head failed')
            return real_save(key, record)
        with patch.object(ledger, 'save', side_effect=save):
            with self.assertRaises(LedgerUnavailable): journal.save_source_snapshot(compact_snapshot_fixture(1))
        lock.release()
        case = ServiceTests(); case.setUp(); case.book = book
        first = case.execute(); self.assertTrue(all(item['status'] == 'completed' for item in first))
        history = lookup_history('imy2099999', ledger=ShipmentLedger(book, 'x' * 40), completion_reader=case.completion)
        self.assertEqual(len(history['shipments']), 2)
        self.assertEqual(case.execute(), first); self.assertEqual(len(case.calls), 2)
        current = mod.PickingJournal(ShipmentLedger(book, 'x' * 40))
        self.assertEqual(len(current.batches()), 1)

class PickingUnknownUploadUiTests(unittest.TestCase):
    def test_restart_exposes_exact_id_recovery_for_unconfirmed_upload(self):
        mod, book, source, drive, calls, run = PickingReliabilityTests().setup_run()
        drive.invisible = True; self.assertFalse(run().success)
        journal = mod.PickingJournal(ShipmentLedger(book, 'x' * 40))
        state = mod.classify_picking_snapshot(mod.picking_source_snapshot(source), journal)
        self.assertEqual(len(state['mirror_pending']), 1)
        self.assertEqual(state['mirror_pending'][0]['recovery_kind'], 'check_upload')
        drive.invisible = False; self.assertTrue(run().success)
        self.assertEqual((calls['render'], drive.allocations, len(drive.creates)), (1, 1, 1))

class PickingProductionSchemaGateTests(unittest.TestCase):
    def fixture(self):
        from test_picking_labels import _anchored_duplicate_header, _anchored_duplicate_row
        row = _anchored_duplicate_row(order_no='normalized-O-order', max_items=1)
        row[1], row[2], row[4] = 'Old source', 'raw-C-order', '郵便局'
        return [_anchored_duplicate_header(1), row]
    def test_canonical_k_through_q_anchors_are_required_without_first_header_fallback(self):
        import features.picking_labels as ui
        for index in range(10, 17):
            with self.subTest(column=index):
                raw = self.fixture(); raw[0][index] = 'renamed-or-shifted'
                with patch.object(ui, 'load_picking_sheet_values', return_value=raw), patch.object(ui, 'load_sheet_values', return_value=[]):
                    with self.assertRaises(ValueError): ui._source_snapshot()
    def test_expected_auxiliary_duplicate_headers_keep_normalized_identity(self):
        import features.picking_labels as ui
        raw = self.fixture()
        with patch.object(ui, 'load_picking_sheet_values', return_value=raw) as load, patch.object(ui, 'load_sheet_values', return_value=[]):
            loaded, snapshot = ui._source_snapshot()
        order = next(iter(snapshot.values()))['order']
        self.assertEqual(order.order_no, 'normalized-O-order')
        self.assertNotEqual(order.order_source, 'Old source')
        load.assert_called_once_with(labels.PICKING_SOURCE_SPREADSHEET_ID, labels.PICKING_SOURCE_WORKSHEET_ID)
    def test_invalid_production_schema_blocks_cached_fallback_identity_before_side_effects(self):
        import features.picking_labels as ui
        from bot.picking_journal import picking_source_snapshot
        from bot.picking_pdf import render_picking_labels_pdf
        from unittest.mock import Mock
        raw = self.fixture(); raw[0][10] = 'status anchor missing'
        selected = [next(iter(picking_source_snapshot(raw).values()))['order']]
        book = MemoryBook(); recorder = Mock()
        def configured(lock): return ShipmentLedger(book, 'x' * 40, lock)
        with (patch.object(ui, 'st', recorder), patch.object(ui, '_picking_source_spreadsheet_id', return_value=labels.PICKING_SOURCE_SPREADSHEET_ID),
              patch.object(ui, 'list_drive_files', return_value=[]), patch.object(ui, 'load_picking_sheet_values', return_value=raw),
              patch.object(ui, 'load_sheet_values', return_value=[]), patch.object(ui, 'global_writer_lock', side_effect=lambda: GlobalShipmentLock(book)),
              patch.object(ui, 'configured_ledger', side_effect=configured), patch.object(ShipmentLedger, 'save') as save,
              patch('bot.picking_pdf.render_picking_labels_pdf') as render,
              patch.object(ui, 'reserve_picking_file_id') as reserve, patch.object(ui, 'upload_reserved_picking_pdf') as upload,
              patch.object(ui, 'mark_picking_sheet_rows') as mark):
            self.assertFalse(ui._generate_and_upload(selected))
        save.assert_not_called(); render.assert_not_called(); reserve.assert_not_called(); upload.assert_not_called(); mark.assert_not_called()

class PickingTypedDocumentValidationTests(unittest.TestCase):
    def journal(self):
        from bot.picking_journal import PickingJournal
        book = MemoryBook(); lock = GlobalShipmentLock(book).acquire(); ledger = ShipmentLedger(book, 'x' * 40, lock)
        self.addCleanup(lock.release)
        return book, ledger, PickingJournal(ledger)
    def test_malformed_snapshot_body_is_refused_before_save(self):
        book, ledger, journal = self.journal()
        with self.assertRaises(LedgerUnavailable): journal.save_source_snapshot({'malformed-entry': {}})
    def test_malformed_encrypted_snapshot_body_fails_closed_for_private_and_postal_reads(self):
        from bot.shipment_ledger import postal_ledger_records
        book, ledger, journal = self.journal()
        key = 'picking:v1:snapshot:' + journal.binding
        journal._save_document('snapshot', key, {'malformed-entry': {}})
        for read in (journal.load_source_snapshot, lambda: postal_ledger_records(ledger)):
            with self.subTest(read=read), self.assertRaises(LedgerUnavailable): read()
    def test_batch_head_cannot_reference_mutable_snapshot_slot(self):
        from bot.shipment_ledger import postal_ledger_records
        mod, book, source, drive, calls, run = PickingReliabilityTests().setup_run(); self.assertTrue(run().success)
        lock = GlobalShipmentLock(book).acquire(); self.addCleanup(lock.release)
        ledger = ShipmentLedger(book, 'x' * 40, lock); journal = mod.PickingJournal(ledger)
        records = dict(ledger.records())
        key, head = next((key, copy.deepcopy(head)) for key, head in records.items() if head.get('kind') == 'batch')
        chunk = copy.deepcopy(records[head['chunks'][0]])
        slot = 'picking:v1:chunk:' + journal.binding + ':snapshot:B:0'
        chunk['key'] = slot; ledger.save(slot, chunk)
        head['chunks'] = [slot]; ledger.save(key, head)
        with self.assertRaises(LedgerUnavailable): postal_ledger_records(ledger)

class PickingTypedCorruptionCases(unittest.TestCase):
    def assert_safe_error(self, operation):
        try:
            operation()
        except LedgerUnavailable:
            return
        except Exception as error:
            self.fail('Expected LedgerUnavailable, got ' + type(error).__name__)
        self.fail('Corrupt typed record was accepted')
    def test_snapshot_wrong_field_types_and_hashes_always_use_safe_error(self):
        from bot.picking_journal import PickingJournal
        for field, value in [('row', True), ('row', 1), ('row', '2'), ('done', []), ('done', 'sent'),
                             ('fingerprint', 'wrong'), ('manual_observed', 1), ('untrusted_done', None)]:
            with self.subTest(field=field, value=value):
                book = MemoryBook(); lock = GlobalShipmentLock(book).acquire(); ledger = ShipmentLedger(book, 'x' * 40, lock)
                journal = PickingJournal(ledger); bad = compact_snapshot_fixture(1)
                bad[next(iter(bad))][field] = value
                journal._save_document('snapshot', 'picking:v1:snapshot:' + journal.binding, bad)
                self.assert_safe_error(journal.load_source_snapshot)
                lock.release()
    def test_unhashable_envelope_enum_is_safe_error(self):
        from bot.picking_journal import PickingJournal
        from bot.shipment_ledger import postal_ledger_records
        book = MemoryBook(); lock = GlobalShipmentLock(book).acquire(); ledger = ShipmentLedger(book, 'x' * 40, lock)
        journal = PickingJournal(ledger); journal.save_source_snapshot(compact_snapshot_fixture(1))
        key, head = next((key, copy.deepcopy(head)) for key, head in ledger.records() if head['kind'] == 'snapshot')
        head['kind'] = []; ledger.save(key, head)
        self.assert_safe_error(lambda: postal_ledger_records(ledger)); lock.release()
    def test_source_head_cannot_mix_generation_or_order(self):
        from bot.picking_journal import PickingJournal
        for mutation in ('generation', 'order'):
            with self.subTest(mutation=mutation):
                book = MemoryBook(); lock = GlobalShipmentLock(book).acquire(); ledger = ShipmentLedger(book, 'x' * 40, lock)
                journal = PickingJournal(ledger); journal.save_source_snapshot(compact_snapshot_fixture(1000))
                key, head = next((key, copy.deepcopy(head)) for key, head in ledger.records() if head['kind'] == 'snapshot')
                if mutation == 'generation': head['generation'] = '0' * 32
                else: head['chunks'] = list(reversed(head['chunks']))
                ledger.save(key, head)
                self.assert_safe_error(journal.load_source_snapshot); lock.release()

class PickingProductionRefreshCallbackTests(unittest.TestCase):
    def test_reload_widget_callback_preserves_only_unchanged_identity_selection(self):
        import features.picking_labels as ui
        from refresh_payloads import PickingPayload
        from types import SimpleNamespace
        class Rerun(BaseException): pass
        class Column:
            def button(self, label, **kwargs): return label == '重新讀取'
        class Streamlit:
            def __init__(self): self.session_state = {}
            def info(self, *args, **kwargs): pass
            def markdown(self, *args, **kwargs): pass
            def error(self, *args, **kwargs): pass
            def columns(self, *args, **kwargs): return [Column() for i in range(4)]
            def rerun(self): raise Rerun()
        for changed_payload in (True, False):
            with self.subTest(changed_payload=changed_payload):
                fake = Streamlit(); original = order_rows(_row(order_no='a', max_items=1), _row(order_no='b', max_items=1))
                with patch.object(ui, 'st', fake):
                    ui.apply_picking_payload(PickingPayload(tuple(original), (), {}), preserve_selection=False)
                    selected = labels.picking_order_key(original[0]); fake.session_state['picking_selected_keys'] = {selected}
                    epoch = fake.session_state['picking_editor_epoch']
                    refreshed = copy.deepcopy(original); refreshed[0].source_row_number = 9
                    if changed_payload: refreshed[0].items[0].quantity = '99'
                    response = SimpleNamespace(data=PickingPayload(tuple(refreshed), (), {}), status=SimpleNamespace(loaded_at=None))
                    with self.assertRaises(Rerun): ui.render_picking_label_tab(refresh_source=lambda *a, **k: response)
                    self.assertEqual(fake.session_state['picking_selected_keys'], set() if changed_payload else {selected})
                    self.assertGreater(fake.session_state['picking_editor_epoch'], epoch)

class PickingManualProofDriftTests(unittest.TestCase):
    def test_manual_content_drift_survives_restart_and_unrelated_operation(self):
        for field in ('sku', 'jan', 'quantity', 'deadline'):
            with self.subTest(field=field):
                mod, book, source, drive, calls, run = PickingReliabilityTests().setup_run(values(
                    _row(order_no='manual', done='已製單', max_items=1), _row(order_no='new', max_items=1)))
                deadlines = {'manual': '2026/11/12 00:00'}
                original_snapshot = mod.picking_source_snapshot
                def read(raw): return original_snapshot(raw, deadlines)
                with patch.object(mod, 'picking_source_snapshot', side_effect=read):
                    self.assertTrue(run().success)
                    manual = next(entry for entry in read(source).values() if entry['order'].order_no == 'manual')
                    key, original_fp = manual['key'], manual['fingerprint']
                    if field == 'deadline': deadlines['manual'] = '2026/11/13 00:00'
                    else: source[1][{'sku': 6, 'jan': 8, 'quantity': 9}[field]] = 'changed'
                    fresh = mod.PickingJournal(ShipmentLedger(book, 'x' * 40))
                    state = mod.classify_picking_snapshot(read(source), fresh)
                    self.assertTrue(any(item['key'] == key for item in state['review']))
                    self.assertFalse(any(item['key'] == key for item in state['done']))
                    source.append(_row(order_no='later', max_items=1))
                    self.assertTrue(run().success)
                    restarted = mod.PickingJournal(ShipmentLedger(book, 'x' * 40))
                    state = mod.classify_picking_snapshot(read(source), restarted)
                    self.assertTrue(any(item['key'] == key for item in state['review']))
                    self.assertEqual(restarted.load_source_snapshot()[key]['fingerprint'], original_fp)
                    self.assertEqual(source[1][2], '已製單')
                    self.assertEqual(calls['mark'], [['new'], ['later']]); self.assertEqual(calls['render'], 2)
    def test_unchanged_manual_proof_follows_same_identity_to_new_row(self):
        mod, book, source, drive, calls, run = PickingReliabilityTests().setup_run(values(
            _row(order_no='manual', done='已製單', max_items=1), _row(order_no='new', max_items=1)))
        self.assertTrue(run().success)
        source.insert(1, _row(order_no='later', max_items=1))
        state = mod.classify_picking_snapshot(mod.picking_source_snapshot(source), mod.PickingJournal(ShipmentLedger(book, 'x' * 40)))
        manual = next(item for item in state['done'] if item['order'].order_no == 'manual')
        self.assertEqual(manual['row'], 3)
        self.assertTrue(run().success)
        state = mod.classify_picking_snapshot(mod.picking_source_snapshot(source), mod.PickingJournal(ShipmentLedger(book, 'x' * 40)))
        self.assertTrue(any(item['order'].order_no == 'manual' for item in state['done']))
    def test_manual_reset_then_recheck_does_not_approve_changed_content(self):
        mod, book, source, drive, calls, run = PickingReliabilityTests().setup_run(values(
            _row(order_no='manual', done='已製單', max_items=1), _row(order_no='new', max_items=1)))
        self.assertTrue(run().success)
        source[1][2] = 'FALSE'; source[1][6] = 'changed'
        source.append(_row(order_no='later', max_items=1))
        later = [order for order in labels.parse_picking_label_candidates(source)[0] if order.order_no == 'later']
        self.assertTrue(run(later).success)
        source[1][2] = '已製單'
        state = mod.classify_picking_snapshot(mod.picking_source_snapshot(source), mod.PickingJournal(ShipmentLedger(book, 'x' * 40)))
        self.assertTrue(any(item['order'].order_no == 'manual' for item in state['review']))
        self.assertEqual(source[1][2], '已製單'); self.assertEqual(calls['mark'], [['new'], ['later']])
