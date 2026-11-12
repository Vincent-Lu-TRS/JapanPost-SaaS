"""揀貨單完成後：顯示成功批次說明，並自動重整讓已製單訂單移出清單。"""
import types
import unittest

import pandas as pd

import features.picking_labels as picking_ui


class _Rerun(Exception):
    pass


class _Col:
    def __init__(self, st, index):
        self.st, self.index = st, index

    def form_submit_button(self, *args, **kwargs):
        return self.st.generate_clicked and self.index == 0

    def button(self, *args, **kwargs):
        return False


class _FakeSt:
    def __init__(self):
        self.session_state = {}
        self.column_config = types.SimpleNamespace(CheckboxColumn=lambda *a, **k: None)
        self.generate_clicked = False
        self.messages = []
        self.reruns = 0

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.messages.append((name, args[0] if args else ""))
            return False
        return record

    def columns(self, spec, **kwargs):
        count = spec if isinstance(spec, int) else len(spec)
        return [_Col(self, i) for i in range(count)]

    def form(self, *a, **k):
        return self

    def container(self, *a, **k):
        return self

    def expander(self, *a, **k):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def data_editor(self, df, **kwargs):
        return df

    def rerun(self):
        self.reruns += 1
        raise _Rerun()


class PickingAutoRefreshTests(unittest.TestCase):
    def setUp(self):
        self.fake = _FakeSt()
        self.original = {name: getattr(picking_ui, name) for name in ("st", "_generate_and_upload")}
        picking_ui.st = self.fake
        picking_ui.pd = pd

    def tearDown(self):
        for name, value in self.original.items():
            setattr(picking_ui, name, value)

    def test_last_batch_summary_is_rendered_from_session_state(self):
        self.fake.session_state["picking_last_batch"] = {
            "filename": "261112-2揀貨標籤.pdf", "link": "https://drive.example/x",
            "orders": [("shop", "A1"), ("shop", "A2")], "finished_at": "13:17:00"}
        picking_ui._render_last_batch()
        success = [m for kind, m in self.fake.messages if kind == "success"]
        self.assertTrue(success and "261112-2揀貨標籤.pdf" in success[0] and "2 筆" in success[0])

    def test_no_summary_when_no_batch(self):
        picking_ui._render_last_batch()
        self.assertFalse(any(kind == "success" for kind, _ in self.fake.messages))

    def test_successful_generate_refreshes_source_then_reruns(self):
        refreshed = []
        self.fake.generate_clicked = True
        picking_ui._generate_and_upload = lambda orders: True
        status = types.SimpleNamespace(loaded_at=None)

        def refresh_source(kind, force):
            refreshed.append((kind, force))
            return types.SimpleNamespace(data=None, status=status)

        with self.assertRaises(_Rerun):
            picking_ui.render_picking_label_tab(refresh_source=refresh_source)
        self.assertEqual(refreshed, [("picking", True)])
        self.assertEqual(self.fake.reruns, 1)

    def test_failed_generate_does_not_refresh_or_rerun(self):
        self.fake.generate_clicked = True
        picking_ui._generate_and_upload = lambda orders: False
        picking_ui.render_picking_label_tab(refresh_source=lambda *a, **k: self.fail("不應重新讀取"))
        self.assertEqual(self.fake.reruns, 0)


if __name__ == "__main__":
    unittest.main()
