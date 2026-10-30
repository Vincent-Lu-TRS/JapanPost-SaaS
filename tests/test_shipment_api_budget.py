import copy
import json
import unittest
from unittest.mock import patch
from test_shipment_ledger import MemoryBook, MemorySheet
import test_shipment_service as service_cases
from bot.shipment_ledger import GlobalShipmentLock, NormalShipmentSafety
from test_shipment_plans import source
from shipment_plans import ORDER


class ApiBudgetTests(unittest.TestCase):
    def test_twenty_order_batch_keeps_lifecycle_reads_below_safe_budget(self):
        reads = []
        originals = [(MemoryBook, 'worksheets'), (MemoryBook, 'get_worksheet_by_id'),
                     (MemoryBook, 'values_get'), (MemorySheet, 'get_all_values'),
                     (MemorySheet, 'get')]
        patches = []
        for cls, name in originals:
            original = getattr(cls, name)

            def counted(obj, *args, _original=original, **kwargs):
                reads.append(1)
                return _original(obj, *args, **kwargs)

            patches.append(patch.object(cls, name, counted))
        for item in patches:
            item.start()
        try:
            book = MemoryBook()
            lock = GlobalShipmentLock(book).acquire()
            safety = NormalShipmentSafety(lock)
            orders = []
            for index in range(20):
                row = copy.deepcopy(source())
                order_id = f'budget-order-{index}'
                tracking = f'EL{index:09}JP'
                row[ORDER] = order_id
                row['trans_type'] = 'ePacket'
                row['shipment_role'] = 'primary'
                safety.lifecycle('issuing', row)
                safety.lifecycle('label_created', row, tracking=tracking)
                safety.lifecycle('uploaded', row, tracking=tracking, drive_file_id=f'pdf-{index}')
                orders.append({**row, 'order_id': order_id, 'tracking': tracking,
                               'drive_file_id': f'pdf-{index}', 'status': 'completed'})
            safety.prepare_writeback(orders)
            safety.mark_verified(orders)
            lock.stage('completed', worker_finished=True)
            lock.release()

            stage_markers = []
            for body in book.calls:
                for request in body.get('requests', []):
                    update = request.get('updateCells') or {}
                    try:
                        marker = json.loads(update['rows'][0]['values'][0]['userEnteredValue']['stringValue'])
                    except (KeyError, IndexError, TypeError, ValueError):
                        continue
                    if marker.get('stage') == 'writeback_pending':
                        stage_markers.append(marker)
            self.assertEqual(len(stage_markers), 1)
            self.assertEqual(stage_markers[0]['shipment_count'], 20)
            self.assertTrue(all(record['stage'] == 'completed'
                                for _, record in safety.records()))
            # Reserve five reads for target-grid initialization and verification.
            self.assertLessEqual(len(reads) + 5, 45,
                                 f'twenty-order lifecycle plus target reads={len(reads) + 5}')
        finally:
            for item in reversed(patches):
                item.stop()

    def test_two_packages_do_not_exhaust_service_account_per_minute_read_budget(self):
        reads = []
        originals = [(MemoryBook,'worksheets'),(MemoryBook,'get_worksheet_by_id'),
                     (MemoryBook,'values_get'),
                     (MemorySheet,'get_all_values')]
        if hasattr(MemorySheet,'get'):
            originals.append((MemorySheet,'get'))
        patches = []
        for cls,name in originals:
            original=getattr(cls,name)
            def counted(obj,*args,_original=original,**kwargs):
                reads.append(1)
                return _original(obj,*args,**kwargs)
            patches.append(patch.object(cls,name,counted))
        for item in patches:item.start()
        try:
            service=service_cases.ServiceTests();service.setUp()
            base_runner=service.runner
            def repeated(frame,*,lifecycle_cb,**kwargs):
                def cb(stage,row,**fields):
                    lifecycle_cb(stage,row,**fields)
                    if stage=='label_created':
                        lifecycle_cb(stage,row,**fields)
                        lifecycle_cb(stage,row,**fields)
                return base_runner(frame,lifecycle_cb=cb,**kwargs)
            service.execute(runner=repeated)
            self.assertLessEqual(len(reads),40, f'two-package split reads={len(reads)}')
            reads.clear()
            lock=GlobalShipmentLock(MemoryBook()).acquire();safety=NormalShipmentSafety(lock)
            row=source();safety.assert_clear([row])
            for index in range(2):
                item=copy.deepcopy(row);item['注文番号(貼上原始資料)']=f'order{index}'
                for stage,fields in [('issuing',{}),('label_created',{'tracking':f'EL{index:09}JP'}),
                                     ('label_created',{'tracking':f'EL{index:09}JP'}),
                                     ('label_created',{'tracking':f'EL{index:09}JP'}),
                                     ('uploaded',{'tracking':f'EL{index:09}JP','drive_file_id':'pdf'})]:
                    safety.lifecycle(stage,item,**fields)
            self.assertLessEqual(len(reads),35,f'two-package normal reads={len(reads)}')
        finally:
            for item in reversed(patches):item.stop()
