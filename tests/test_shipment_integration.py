import ast
import unittest
from pathlib import Path


class IntegrationTests(unittest.TestCase):
    def test_normal_worker_and_retry_use_same_global_safety_gate(self):
        source = (Path(__file__).parents[1] / 'app.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        worker = next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_start_job')
        text = ast.get_source_segment(source,worker)
        self.assertIn('global_writer_lock', text)
        self.assertIn('NormalShipmentSafety', text)
        self.assertLess(text.index('writer.acquire()'), text.index('completion_authority = read_completion_authority()'))
        self.assertIn('lifecycle_cb=safety.lifecycle', text)
        retry = next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_retry_writeback_results')
        text = ast.get_source_segment(source,retry)
        self.assertIn('writer.acquire()', text)
        self.assertIn('writer.release()', text)
