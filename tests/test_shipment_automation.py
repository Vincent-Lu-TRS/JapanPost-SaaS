import ast
import unittest
from pathlib import Path
import pandas as pd
from bot.automation import _prepare_batch_hs_codes, _build_result_record
from shipment_plans import build_plan, source_items, plan_frame
from test_shipment_plans import source


class AutomationShipmentTests(unittest.TestCase):
    def test_hs_cache_and_results_use_shipment_identity(self):
        row = source(); a,b = source_items(row)
        frame = plan_frame(build_plan(row, [
            {'method': 'ePacket', 'quantities': {a['id']: 2}},
            {'method': 'ePacket', 'quantities': {b['id']: 3}},
        ]))
        codes = _prepare_batch_hs_codes(frame, {'GERMANY':'EU'})
        self.assertEqual(codes[frame.iloc[0]['_shipment_id']], {'1': '111111'})
        self.assertEqual(codes[frame.iloc[1]['_shipment_id']], {'1': '222222'})
        result = _build_result_record(frame.iloc[0], 'imy2099999', 'EL000000001JP')
        self.assertEqual(result['shipment_id'], frame.iloc[0]['_shipment_id'])

    def test_strict_lifecycle_register_and_upload_order(self):
        source_text = (Path(__file__).parents[1] / 'bot/automation.py').read_text(encoding='utf-8')
        tree = ast.parse(source_text)
        run = next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='run_automation')
        self.assertIn('lifecycle_cb', [a.arg for a in run.args.args])
        register = next(n for n in ast.walk(run) if isinstance(n,ast.FunctionDef) and n.name=='submit_m061000_register_via_requests')
        text = ast.get_source_segment(source_text, register)
        self.assertLess(text.index("_lifecycle('issuing'"), text.index('req_session.post('))
        self.assertIn("_lifecycle('label_created'", text)
        printer = next(n for n in ast.walk(run) if isinstance(n,ast.FunctionDef) and n.name=='submit_m061100_print_via_requests')
        text = ast.get_source_segment(source_text, printer)
        self.assertLess(text.index("_lifecycle('label_created'"), text.index('upload_pdf('))
        self.assertIn('pdf_uploaded = bool(file_id)', text)


if __name__ == '__main__':
    unittest.main()
