import copy
import unittest
from bot.shipment_ledger import GlobalShipmentLock, ShipmentLedger, LedgerUnavailable
from bot.shipment_service import execute_plan, lookup_history
from bot.sheets import CompletionAuthority
from shipment_plans import build_plan, source_items
from test_shipment_plans import source
from test_shipment_ledger import MemoryBook


class ServiceTests(unittest.TestCase):
    def test_missing_recipient_fields_and_import_id_refuse_before_persist(self):
        for field in ('Shipping Name','Shipping Street','收件人國家'):
            self.setUp()
            bad=self.row.copy();bad[field]=''
            a,b=source_items(bad)
            plan=build_plan(bad,[{'method':'EMS','quantities':{a['id']:2,b['id']:3}}])
            with self.subTest(field=field),self.assertRaises(ValueError):self.execute(plan)
            self.assertFalse(self.calls)
            self.assertEqual(ShipmentLedger(self.book,'x'*40).records(),[])
        for country,suffix in [('CHINA',' (PRC ID:110108198309121213)'),('KOREA',' (PCCC:P180026936191)')]:
            self.setUp();self.row['收件人國家']=country
            a,b=source_items(self.row)
            packages=[{'method':'EMS','quantities':{a['id']:2,b['id']:3}}]
            with self.assertRaises(ValueError):self.execute(build_plan(self.row,packages))
            self.assertEqual(ShipmentLedger(self.book,'x'*40).records(),[])
            self.row['Shipping Name']+=suffix
            self.execute(build_plan(self.row,packages))
            self.assertEqual(len(self.calls),1)

    def setUp(self):
        self.book = MemoryBook()
        self.row = source()
        a, b = source_items(self.row)
        self.plan = build_plan(self.row, [
            {'method': 'ePacket', 'quantities': {a['id']: 1}},
            {'method': 'ePacket', 'quantities': {a['id']: 1, b['id']: 3}},
        ])
        self.pairs = set()
        self.calls = []

    def completion(self):
        return CompletionAuthority(frozenset(o for o,t in self.pairs), frozenset(self.pairs))

    def runner(self, frame, *, lifecycle_cb, **kwargs):
        sid = frame.iloc[0]['_shipment_id']
        self.calls.append(sid)
        lifecycle_cb('issuing', frame.iloc[0])
        tracking = f'EL{len(self.calls):09}JP'
        lifecycle_cb('label_created', frame.iloc[0], tracking=tracking)
        lifecycle_cb('uploaded', frame.iloc[0], tracking=tracking, drive_file_id='pdf' + tracking)
        return [{'status': 'success', 'tracking': tracking}]

    def backfill(self, results, **kwargs):
        for result in results:
            self.pairs.add((result['order_id'], result['tracking']))
        return {'ok': True, 'items': [{'input_index': i, 'status': 'written'} for i in range(len(results))]}

    def execute(self, plan=None, **kwargs):
        lock = GlobalShipmentLock(self.book)
        ledger = ShipmentLedger(self.book, 'x' * 40, lock)
        return execute_plan(plan or self.plan, requested_by='staff@tkrjm.co.jp', lock=lock, ledger=ledger,
                            source_loader=lambda order: self.row, completion_reader=self.completion,
                            runner=kwargs.pop('runner', self.runner), backfill=kwargs.pop('backfill', self.backfill), **kwargs)

    def test_two_packages_keep_original_order_and_retry_after_restart_never_reissues(self):
        results = self.execute()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual({r['order_id'] for r in results}, {'imy2099999'})
        self.assertEqual(len(self.pairs), 2)
        self.assertEqual([r['status'] for r in self.execute()], ['completed', 'completed'])
        self.assertEqual(len(self.calls), 2)

    def test_completed_replay_ignores_deleted_source_and_later_legal_tracking(self):
        self.execute()
        self.row = {}
        self.pairs.add(('imy2099999','EL999999999JP'))
        replay = self.execute()
        self.assertTrue(all(r['status']=='completed' for r in replay))
        self.assertEqual(len(self.calls),2)

    def test_old_baseline_and_duplicate_shipment_tracking_are_never_accepted_as_new(self):
        def duplicate(frame, *, lifecycle_cb, **kwargs):
            self.calls.append(frame.iloc[0]['_shipment_id'])
            lifecycle_cb('issuing',frame.iloc[0])
            lifecycle_cb('label_created',frame.iloc[0],tracking='EL999999999JP')
            lifecycle_cb('uploaded',frame.iloc[0],tracking='EL999999999JP',drive_file_id='file')
            return []
        with self.assertRaises(LedgerUnavailable):
            self.execute(runner=duplicate)
        self.assertEqual(len(self.calls),2)
        self.assertEqual(len(self.pairs),1)
        self.book=MemoryBook(); self.pairs={('imy2099999','EL999999999JP')}; self.calls=[]
        a,b=source_items(self.row)
        plan=build_plan(self.row,[{'method':'EMS','quantities':{a['id']:2,b['id']:3}}],
                        mode='reissue',reason='damage',original_tracking='EL999999999JP',nonce='test')
        with self.assertRaises(LedgerUnavailable):
            self.execute(plan,runner=duplicate,tracking_baseline={'EL999999999JP'},confirmed_new_tracking=True)
        self.assertEqual(len(self.pairs),1)

    def test_label_created_without_durable_pdf_never_automatically_reissues(self):
        def no_pdf(frame, *, lifecycle_cb, **kwargs):
            self.calls.append('once')
            lifecycle_cb('issuing',frame.iloc[0])
            lifecycle_cb('label_created',frame.iloc[0],tracking='EL000000001JP')
            return []
        with self.assertRaises(LedgerUnavailable):
            self.execute(runner=no_pdf)
        with self.assertRaises(LedgerUnavailable):
            self.execute()
        self.assertEqual(self.calls,['once'])

    def test_writeback_failure_retries_only_same_tracking(self):
        def failure(results, **kwargs):
            return {'ok': False, 'items': [{'input_index': 0, 'status': 'write_failed'}]}
        result = self.execute(backfill=failure)
        self.assertEqual(result[0]['status'], 'backfill_failed')
        self.assertEqual(len(self.calls), 2)
        self.execute()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(self.pairs), 2)

    def test_source_change_and_existing_legacy_completion_block_split(self):
        self.row['Shipping Street'] = 'Changed'
        with self.assertRaises(ValueError):
            self.execute()
        self.assertFalse(self.calls)

        self.row = source()
        self.pairs.add(('imy2099999', 'EL999999999JP'))
        with self.assertRaises(ValueError):
            self.execute()
        self.assertFalse(self.calls)

    def test_initial_split_ineligible_source_blocks_without_postal_submission(self):
        self.row['_eligible_for_initial_split'] = False
        with self.assertRaises(ValueError):
            self.execute()
        self.assertFalse(self.calls)

    def test_known_tracking_recovery_does_not_require_live_source(self):
        self.execute(backfill=lambda results, **kwargs: {'ok':False, 'items':[]})
        self.row['Shipping Street'] = 'Source now changed'
        results = self.execute()
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all(r['status']=='completed' for r in results))

    def test_new_plan_refuses_other_unfinished_plan_and_normal_guard(self):
        self.pairs.add(('imy2099999','EL999999999JP'))
        a,b = source_items(self.row)
        package = [{'method':'EMS','quantities':{a['id']:2,b['id']:3}}]
        p1 = build_plan(self.row,package,mode='reissue',reason='damage',original_tracking='EL999999999JP',nonce='one')
        p2 = build_plan(self.row,package,mode='reissue',reason='damage',original_tracking='EL999999999JP',nonce='two')
        self.execute(p1,tracking_baseline={'EL999999999JP'},confirmed_new_tracking=True,
                     backfill=lambda results, **kwargs: {'ok':False,'items':[]})
        with self.assertRaises(ValueError):
            self.execute(p2,tracking_baseline={'EL999999999JP'},confirmed_new_tracking=True)
        self.assertEqual(len(self.calls),1)
        # Independent legacy writer leaves a durable tombstone even with no key.
        self.book = MemoryBook(); self.pairs.clear(); self.calls.clear()
        from bot.shipment_ledger import NormalShipmentSafety
        lock=GlobalShipmentLock(self.book).acquire(); safety=NormalShipmentSafety(lock)
        safety.lifecycle('issuing',self.row)
        safety.lifecycle('label_created',self.row,tracking='EL000000001JP')
        safety.lifecycle('uploaded',self.row,tracking='EL000000001JP',drive_file_id='f1')
        lock.release()
        with self.assertRaises(ValueError):
            self.execute()
        self.assertFalse(self.calls)

    def test_register_unknown_retains_lock_and_never_automatically_reissues(self):
        def crash(frame, *, lifecycle_cb, **kwargs):
            lifecycle_cb('issuing', frame.iloc[0])
            raise TimeoutError()
        with self.assertRaises(LedgerUnavailable):
            self.execute(runner=crash)
        self.assertTrue(any(s.title == '_jppost_exclusive_writer_v1' for s in self.book.worksheets()))
        with self.assertRaises(LedgerUnavailable):
            self.execute()
        self.assertFalse(self.calls)

    def test_reissue_target_baseline_blocks_second_users_stale_confirmation(self):
        self.pairs.add(('imy2099999', 'EL999999999JP'))
        a, b = source_items(self.row)
        packages = [{'method': 'EMS', 'quantities': {a['id']: 2, b['id']: 3}}]
        p1 = build_plan(self.row, packages, mode='reissue', reason='damaged label',
                        original_tracking='EL999999999JP', nonce='user1')
        p2 = build_plan(self.row, packages, mode='reissue', reason='damaged label',
                        original_tracking='EL999999999JP', nonce='user2')
        baseline = {'EL999999999JP'}
        self.execute(p1, tracking_baseline=baseline, confirmed_new_tracking=True)
        with self.assertRaises(ValueError):
            self.execute(p2, tracking_baseline=baseline, confirmed_new_tracking=True)
        self.assertEqual(len(self.calls), 1)

    def test_ledger_history_prefers_complete_plan_payload_and_returns_package_list(self):
        self.execute()
        history = lookup_history('imy2099999', ledger=ShipmentLedger(self.book, 'x' * 40),
                                 completion_reader=self.completion,
                                 source_loader=lambda order: (_ for _ in ()).throw(AssertionError()))
        self.assertEqual(history['source_kind'], 'ledger_snapshot')
        self.assertEqual(history['row']['數量1'], '2')
        self.assertEqual(history['row']['數量2'], '3')
        self.assertEqual(len(history['shipments']), 2)


if __name__ == '__main__':
    unittest.main()
