"""預留上傳卡死的自動恢復：雲端確認沒有檔案才解除，其餘情況維持保守。"""
import unittest
from pathlib import Path
from unittest.mock import patch

from test_picking_reliability import FakeDrive
from test_picking_labels import _row
from test_picking_reliability import values
from test_shipment_ledger import MemoryBook
import importlib
from bot import picking_labels as labels


class _NotFound(Exception):
    def __init__(self, status='404'):
        self.resp = type('Resp', (), {'status': status})()


class MissingDrive(FakeDrive):
    """上傳從未成功；files.get 對不存在的預留 ID 回 404。"""
    def __init__(self):
        super().__init__(); self.drop_upload = True
    def upload(self, path, record):
        self.creates.append(record['file_id'])
        if not self.drop_upload:
            return FakeDrive.upload(self, path, record)
        raise TimeoutError('upload never committed')
    def get(self, record):
        if record['file_id'] not in self.files:
            raise _NotFound()
        return FakeDrive.get(self, record)


class AbandonTests(unittest.TestCase):
    def _setup(self, drive_cls=MissingDrive):
        mod = importlib.import_module('bot.picking_journal')
        book = MemoryBook(); source = values(_row(max_items=1)); calls = {'render': 0, 'mark': []}
        new = drive_cls()
        def run2(orders=None, mark_fn=None, render_fn=None):
            return self._run(mod, book, source, new, calls, orders, mark_fn, render_fn)
        return mod, book, source, new, calls, run2

    def _run(self, mod, book, source, drive, calls, orders, mark_fn, render_fn):
        import tempfile
        from bot.shipment_ledger import GlobalShipmentLock, ShipmentLedger
        def render(o, path):
            if render_fn: render_fn()
            calls['render'] += 1; Path(path).write_bytes(b'%PDF-1.4 picking')
        def mark(os_):
            calls['mark'].append([o.order_no for o in os_])
            for o in os_: source[o.source_row_number - 1][2] = '已製單'
            return {labels.picking_order_key(o): {'status': 'verified', 'row': o.source_row_number} for o in os_}
        lock = GlobalShipmentLock(book)
        journal = mod.PickingJournal(ShipmentLedger(book, 'x' * 40, lock))
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "tmp", ignore_cleanup_errors=True) as d:
            return mod.execute_picking_batch(
                orders or labels.parse_picking_label_candidates(source)[0], output_dir=d,
                source_loader=lambda: mod.picking_source_snapshot(source), journal=journal, lock=lock,
                list_files=lambda prefix: [], reserve_file_id=drive.reserve_id,
                upload_file=drive.upload, get_file=drive.get, mark_done=mark_fn or mark,
                render=render, folder_id='folder', now='2026-11-11 12:00:00')

    def test_missing_upload_is_released_after_wait_then_can_be_remade(self):
        mod, book, source, drive, calls, run = self._setup()
        self.assertFalse(run().success)
        young = run()
        self.assertFalse(young.released)
        self.assertIn('2 分鐘', young.error)
        import time as real_time
        later = real_time.time() + 600
        with patch.object(mod.time, 'time', return_value=later):
            released = run()
        self.assertTrue(released.released)
        drive.drop_upload = False
        remade = run()
        self.assertTrue(remade.success)
        self.assertEqual(calls['mark'], [['imy1']])
        self.assertEqual(calls['render'], 2)

    def test_reserved_without_upload_is_released(self):
        mod, book, source, drive, calls, run = self._setup(FakeDrive)
        def boom(): raise RuntimeError('process died')
        with self.assertRaises(RuntimeError): run(render_fn=boom)
        import time as real_time
        with patch.object(mod.time, 'time', return_value=real_time.time() + 600):
            self.assertTrue(run().released)
        self.assertTrue(run().success)

    def test_unknown_errors_never_release(self):
        mod, book, source, drive, calls, run = self._setup()
        self.assertFalse(run().success)
        drive.get = lambda record: (_ for _ in ()).throw(TimeoutError('metadata unavailable'))
        import time as real_time
        with patch.object(mod.time, 'time', return_value=real_time.time() + 600):
            result = run()
        self.assertFalse(result.released)
        self.assertIn('聯絡管理員', result.error)


class OverlayTests(unittest.TestCase):
    def test_overlay_is_always_cleared(self):
        import features.picking_labels as ui
        from unittest.mock import MagicMock
        for outcome in (True, False, RuntimeError('x')):
            fake = MagicMock(); guard = fake.empty.return_value
            order = type('O', (), {'order_no': 'imy1'})()
            with patch.object(ui, 'st', fake), patch.object(ui, '_generate_and_upload_locked') as inner:
                if isinstance(outcome, Exception):
                    inner.side_effect = outcome
                    with self.assertRaises(RuntimeError): ui._generate_and_upload([order])
                else:
                    inner.return_value = outcome
                    self.assertEqual(ui._generate_and_upload([order]), outcome)
            guard.markdown.assert_called()
            self.assertIn('running-guard-overlay', guard.markdown.call_args[0][0])
            guard.empty.assert_called_once()
