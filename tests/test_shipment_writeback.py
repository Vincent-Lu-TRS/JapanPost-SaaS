import unittest
from bot.sheets import backfill_results
from tests.fake_gspread import FakeClient, FakeWorksheet


class ShipmentWritebackTests(unittest.TestCase):
    def test_retry_exact_pair_repairs_only_missing_country_with_same_tracking(self):
        ws = FakeWorksheet([['', 'name','order','tracking','','','','','','country'],
                            ['', 'Recipient','000123','EL000000001JP','','','','','','']])
        result = {'name':'Recipient','order_id':'000123','tracking':'EL000000001JP',
                  'country_raw':'GERMANY','shipment_role':'additional','shipment_id':'s1'}
        outcome = backfill_results([result], client=FakeClient(ws))
        self.assertTrue(outcome['ok'])
        self.assertEqual(outcome['items'][0]['status'], 'written')
        self.assertEqual(ws.rows[1][2:4], ['000123','EL000000001JP'])
        self.assertEqual(ws.rows[1][9], 'EU')
        self.assertEqual(len(ws.rows), 2)
        updates = next(call[1] for call in ws.calls if call[0] == 'batch_update')
        self.assertEqual([u['range'] for u in updates], ['J2:J2'])

    def test_existing_pair_country_formula_or_nonempty_conflict_cannot_be_overwritten(self):
        for country, formula in [('', '=IF(TRUE,"","")'), ('US', '')]:
            ws = FakeWorksheet([['', 'name','order','tracking','','','','','','country'],
                                ['', 'Recipient','000123','EL000000001JP','','','','','',country]],
                               formula_cells={(2,10):formula} if formula else {})
            result = {'name':'Recipient','order_id':'000123','tracking':'EL000000001JP',
                      'country_raw':'GERMANY','shipment_role':'additional'}
            outcome=backfill_results([result],client=FakeClient(ws))
            self.assertFalse(outcome['ok'])
            self.assertFalse(any(c[0]=='batch_update' for c in ws.calls))

