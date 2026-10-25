import unittest
from types import SimpleNamespace
from bot.sheets import get_history_order
from shipment_plans import ORDER


class HistoryTests(unittest.TestCase):
    def client(self, rows):
        ws = SimpleNamespace(id=605188303, get_all_values=lambda: rows)
        book = SimpleNamespace(get_worksheet_by_id=lambda sid: ws)
        return SimpleNamespace(open_by_key=lambda key: book)

    def test_exact_history_bypasses_pending_filter_and_projects_only_order(self):
        rows = [[ORDER, 'Shipping Name', '內容物1', '申告金額1', '數量1', 'unrelated secret'],
                ['000123', 'Recipient', 'A', '2', '3', 'private metadata'],
                ['0001234', 'Other Recipient', 'B', '3', '4', 'other metadata']]
        result = get_history_order('000123', client=self.client(rows))
        self.assertEqual(result[ORDER], '000123')
        self.assertEqual(result['Shipping Name'], 'Recipient')
        self.assertNotIn('unrelated secret', result)
        self.assertNotIn('Other Recipient', str(result))

    def test_missing_ambiguous_and_invalid_queries_fail_closed(self):
        rows = [[ORDER, 'Shipping Name'], ['123', 'A'], ['123', 'B']]
        for order in ('123', '1234', '=123', '123:Z999', ''):
            with self.subTest(order=order), self.assertRaises(ValueError):
                get_history_order(order, client=self.client(rows))


if __name__ == '__main__':
    unittest.main()
