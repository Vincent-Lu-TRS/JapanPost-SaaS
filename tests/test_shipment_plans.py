import unittest
import pandas as pd

import shipment_plans as plans
from pending_editor import SHIPPING_COL


def source():
    return pd.Series({
        '注文番号(貼上原始資料)': 'imy2099999', 'Shipping Name': 'Test Recipient',
        'Shipping Street': 'Example Street', '收件人國家': 'GERMANY',
        SHIPPING_COL: 'ePacket', '_source_row_number': '22', '_eligible_for_initial_split': True,
        '內容物1': 'A', '申告金額1': '2.50', '數量1': '2', 'HSCode1': '111111',
        '內容物2': 'B', '申告金額2': '3.00', '數量2': '3', 'HSCode2': '222222',
    })


class ShipmentPlansTests(unittest.TestCase):
    def test_reissue_accepts_empty_reason_but_requires_original_tracking_and_nonce(self):
        row = source()
        packages = [{'method': 'EMS', 'quantities': {i['id']: i['quantity'] for i in plans.source_items(row)}}]
        plan = plans.build_plan(row, packages, mode='reissue', original_tracking='AA000000000JP', nonce='test')
        self.assertEqual(plan['reason'], '')
        plans.validate_plan(plan)
        for missing in ('original_tracking', 'nonce'):
            options = {'original_tracking': 'AA000000000JP', 'nonce': 'test'}
            options[missing] = ''
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                plans.build_plan(row, packages, mode='reissue', **options)

    def test_legacy_reason_plan_identity_and_integrity_remain_unchanged(self):
        row = source()
        packages = [{'method': 'EMS', 'quantities': {i['id']: i['quantity'] for i in plans.source_items(row)}}]
        plan = plans.build_plan(row, packages, mode='reissue', reason='damaged label',
                                original_tracking='AA000000000JP', nonce='legacy')
        before = (plan['plan_id'], plan['integrity'], plan['reason'])
        plans.validate_plan(plan)
        self.assertEqual(before, (plan['plan_id'], plan['integrity'], plan['reason']))

    def test_item_partition_and_totals(self):
        row = source()
        items = plans.source_items(row)
        plan = plans.build_plan(row, [
            {'method': 'ePacket', 'quantities': {items[0]['id']: 2}},
            {'method': '國際小包', 'quantities': {items[1]['id']: 3}},
        ])
        frame = plans.plan_frame(plan)
        self.assertEqual(frame['注文番号(貼上原始資料)'].tolist(), ['imy2099999'] * 2)
        self.assertEqual(frame['內容物1'].tolist(), ['A', 'B'])
        self.assertEqual(frame['郵局申告金額(USD)'].tolist(), ['5.00', '9.00'])
        self.assertEqual(frame['HSCode1'].tolist(), ['111111', '222222'])
        self.assertFalse(any(frame.get('內容物2', pd.Series(['', ''])).fillna('')))

    def test_item_spans_same_method_packages_and_stable_ids(self):
        row = source()
        a, b = plans.source_items(row)
        allocations = [
            {'method': 'ePacket', 'quantities': {a['id']: 1}},
            {'method': 'ePacket', 'quantities': {a['id']: 1, b['id']: 3}},
        ]
        plan = plans.build_plan(row, allocations)
        self.assertEqual(plan, plans.build_plan(row, allocations))
        self.assertEqual([s['total_usd'] for s in plan['shipments']], ['2.50', '11.50'])
        self.assertEqual(len({s['shipment_id'] for s in plan['shipments']}), 2)
        self.assertEqual(plans.plan_frame(plan)['數量1'].tolist(), ['1', '1'])

    def test_invalid_allocations_are_rejected(self):
        row = source()
        a, b = plans.source_items(row)
        for bad in (-1, 0.5, 3, True, '1.5'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                plans.build_plan(row, [{'method': 'ePacket', 'quantities': {a['id']: bad, b['id']: 3}}])
        with self.assertRaises(ValueError):
            plans.build_plan(row, [{'method': 'ePacket', 'quantities': {a['id']: 2, b['id']: 3}},
                                   {'method': 'EMS', 'quantities': {}}])
        with self.assertRaises(ValueError):
            plans.build_plan(row, [{'method': 'ePacket', 'quantities': {a['id']: 1, b['id']: 3}}])

    def test_same_name_items_have_distinct_identity_and_no_mutation(self):
        row = source()
        row['內容物2'] = 'A'
        a, b = plans.source_items(row)
        self.assertNotEqual(a['id'], b['id'])
        plan = plans.build_plan(row, [{'method': 'EMS', 'quantities': {a['id']: 2, b['id']: 3}}])
        row['Shipping Name'] = 'Changed'
        self.assertEqual(plan['source_snapshot']['Shipping Name'], 'Test Recipient')

    def test_business_fingerprint_ignores_completion_state_only(self):
        row = source()
        fp = plans.source_fingerprint(row)
        row['製單上傳狀態(請用[未打單]檢視模式)'] = 'EL123456789JP'
        row['製單檢核'] = 'TRUE'
        self.assertEqual(fp, plans.source_fingerprint(row))
        row['Shipping Street'] = 'Changed address'
        self.assertNotEqual(fp, plans.source_fingerprint(row))

    def test_plan_validation_detects_tampering(self):
        row = source()
        a, b = plans.source_items(row)
        plan = plans.build_plan(row, [{'method': 'EMS', 'quantities': {a['id']: 2, b['id']: 3}}])
        plan['shipments'][0]['items'][0]['quantity'] = 999
        with self.assertRaises(ValueError):
            plans.validate_plan(plan)

    def test_order_limit_keeps_all_packages_of_selected_order(self):
        frame=pd.DataFrame([{plans.ORDER:'one','_plan_id':'p1'},
                            {plans.ORDER:'one','_plan_id':'p1'},
                            {plans.ORDER:'two','_plan_id':'p2'}])
        self.assertEqual(len(plans.selected_complete_orders(frame,1)),2)

    def test_oversized_or_conflicting_legacy_source_is_never_silently_truncated(self):
        row=source();row['內容物11']='extra'
        with self.assertRaises(ValueError):plans.source_items(row)
        row=source();row['郵局內容物']='different'
        with self.assertRaises(ValueError):plans.source_items(row)


if __name__ == '__main__':
    unittest.main()
