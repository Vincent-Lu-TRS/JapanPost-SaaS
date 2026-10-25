import subprocess
import sys
import unittest
from pathlib import Path

class WorkbenchSubprocessTests(unittest.TestCase):
    def test_real_streamlit_forms(self):
        result = subprocess.run([sys.executable, '-m', 'unittest', 'tests.shipment_workbench_app_scenario', '-v'],
                                cwd=Path(__file__).parents[1], capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout[-2000:] + result.stderr[-5000:])
