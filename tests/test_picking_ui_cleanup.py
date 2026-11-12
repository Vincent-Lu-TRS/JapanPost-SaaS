"""Presentation-only regressions; all orders are synthetic, no live providers."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_picking_labels import _picking_payload_for_rows
from bot.picking_labels import picking_order_key
from refresh_payloads import PickingPayload
import features.picking_labels as ui


class FakeStreamlit:
    def __init__(self):
        self.session_state = {}; self.warnings = []; self.tables = []; self.editors = []
        self.links = []; self.buttons = []; self.expanders = []
        self.column_config = SimpleNamespace(CheckboxColumn=lambda *a, **k: None)
    def info(self, *a, **k): pass
    def error(self, *a, **k): pass
    def markdown(self, *a, **k): pass
    def caption(self, *a, **k): pass
    def json(self, *a, **k): pass
    def write(self, *a, **k): pass
    def warning(self, text): self.warnings.append(text)
    def columns(self, *a, **k): return [self] * 4
    def button(self, label, **kwargs): self.buttons.append((label, kwargs)); return False
    def form_submit_button(self, label, **kwargs): self.buttons.append((label, kwargs)); return False
    def form(self, *a, **k): return self
    def container(self, *a, **k): return self
    def link_button(self, label, url, **kwargs): self.links.append((label, url))
    def data_editor(self, frame, **kwargs): self.editors.append(frame.copy()); return frame
    def dataframe(self, frame, **kwargs): self.tables.append(frame.copy())
    def expander(self, label, **kwargs):
        self.expanders.append((label, kwargs))
        return self
    def __enter__(self): return self
    def __exit__(self, *args): return False


def mixed_payload():
    pending, review, repair, unknown = copy.deepcopy(_picking_payload_for_rows(2, 3, 4, 5).orders)
    reviews = []
    for row in range(10, 16):
        invalid = copy.deepcopy(pending)
        invalid.source_row_number = row; invalid.order_source = ''; invalid.order_no = 'synthetic-' + str(row)
        reviews.append({'key': 'invalid-row-' + str(row), 'row': row, 'order': invalid,
                        'review': '缺少訂單來源或注文番号', 'done': 'NOT_DONE'})
    reviews.append({'key': picking_order_key(review), 'row': 3, 'order': review,
                    'review': '人工已製單品項與來源不同', 'file_id': 'review-pdf'})
    recovery = [
        {'key': picking_order_key(repair), 'order': repair, 'file_id': 'repair-pdf'},
        {'key': picking_order_key(unknown), 'order': unknown, 'file_id': 'unknown-pdf', 'recovery_kind': 'check_upload'},
    ]
    return PickingPayload((pending,), (), {'picking_review': reviews, 'picking_recovery': recovery})


class PickingUiCleanupTests(unittest.TestCase):
    def test_identity_only_source_noise_is_diagnostic_but_real_operations_stay_visible(self):
        fake = FakeStreamlit(); payload = mixed_payload()
        with patch.object(ui, 'st', fake):
            ui.apply_picking_payload(payload, preserve_selection=False)
            ui.render_picking_label_tab()
            self.assertFalse(any('缺少訂單來源或注文番号' in text for text in fake.warnings))
            self.assertTrue(any('人工已製單品項與來源不同' in text for text in fake.warnings))
            self.assertTrue(any('PDF 已存在' in text for text in fake.warnings))
            self.assertTrue(any('上傳結果未確認' in text for text in fake.warnings))
            self.assertEqual(len(fake.editors[-1]), 1)
            self.assertIn('只補標此訂單', [label for label, options in fake.buttons])
            self.assertIn('核對原上傳結果／補標', [label for label, options in fake.buttons])
            self.assertEqual(len(fake.session_state['picking_diagnostics']['picking_review']), 7)
            self.assertFalse(next(options['disabled'] for label, options in fake.buttons if label == '產生揀貨單'))
            with patch.object(ui.auth, 'can_view_diagnostics', return_value=True):
                ui.render_picking_label_diagnostics_panel()
        diagnostic_text = '\n'.join(frame.to_string(index=False) for frame in fake.tables)
        for row in range(10, 16): self.assertIn('synthetic-' + str(row), diagnostic_text)
        self.assertIn('缺少訂單來源或注文番号', diagnostic_text)
        self.assertTrue(any(options.get('expanded') is False for label, options in fake.expanders if '身份' in label))
    def test_receipt_review_is_never_hidden_as_source_noise(self):
        fake = FakeStreamlit(); payload = mixed_payload()
        payload.diagnostics['picking_review'][0]['file_id'] = 'existing-receipt'
        with patch.object(ui, 'st', fake):
            ui.apply_picking_payload(payload, preserve_selection=False)
            ui.render_picking_label_tab()
        self.assertTrue(any('synthetic-10' in text for text in fake.warnings))
        self.assertTrue(any('existing-receipt' in url for label, url in fake.links))
