import copy
import unittest
from unittest.mock import patch
from test_shipment_ledger import MemoryBook, MemorySheet
import test_shipment_service as service_cases
from bot.shipment_ledger import GlobalShipmentLock, NormalShipmentSafety
from test_shipment_plans import source


class ApiBudgetTests(unittest.TestCase):
    def test_two_packages_do_not_exhaust_service_account_per_minute_read_budget(self):
        reads = []
        originals = [(MemoryBook,'worksheets'),(MemoryBook,'get_worksheet_by_id'),
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
