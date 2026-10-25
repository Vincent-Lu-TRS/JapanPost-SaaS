import textwrap
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest


class WorkbenchAppTests(unittest.TestCase):
    def app(self, body):
        return AppTest.from_string(textwrap.dedent('''
            import streamlit as st
            from features.shipment_workbench import render_split_planner, render_reissue_tab
            import pandas as pd
            from pending_editor import SHIPPING_COL
            row = pd.Series({'注文番号(貼上原始資料)':'imy2099999', 'Shipping Name':'Recipient',
               '收件人國家':'GERMANY', 'Shipping Street':'Example Street', SHIPPING_COL:'ePacket',
               '內容物1':'A','申告金額1':'2.50','數量1':'2',
               '內容物2':'B','申告金額2':'3','數量2':'3'})
        ''') + '\n' + body)

    def authenticate(self, app):
        app.run()
        app.session_state['authenticated'] = True
        app.session_state['user_email'] = 'staff@tkrjm.co.jp'
        app.session_state['_verified_auth_email'] = 'staff@tkrjm.co.jp'
        return app.run()

    def test_actual_split_form_quantity_interaction_and_preview(self):
        app = self.authenticate(self.app("render_split_planner(row, source_row=row, key='test')"))
        next(x for x in app.checkbox if x.label == '拆分包裹').check().run()
        next(x for x in app.number_input if x.label == '包裹數').set_value(2).run()
        next(x for x in app.number_input if x.key == 'test_qty_0_1').set_value(1).run()
        next(x for x in app.number_input if x.key == 'test_qty_1_1').set_value(1).run()
        next(x for x in app.number_input if x.key == 'test_qty_0_2').set_value(0).run()
        next(x for x in app.number_input if x.key == 'test_qty_1_2').set_value(3).run()
        next(x for x in app.button if x.label == '確認拆單計畫').click().run()
        self.assertFalse(app.exception)
        plan = app.session_state['test_reviewed_plan']
        self.assertEqual([s['total_usd'] for s in plan['shipments']], ['2.50','11.50'])
        self.assertEqual(len({s['shipment_id'] for s in plan['shipments']}), 2)
        self.assertEqual(plan['shipments'][0]['method'], plan['shipments'][1]['method'])

    def test_anonymous_cannot_query_history(self):
        app = self.app('render_reissue_tab()').run()
        self.assertFalse(app.text_input)
        self.assertFalse(app.button)

    def test_reissue_requires_reason_and_new_tracking_confirmation(self):
        history = {'order_id':'imy2099999','row':{'注文番号(貼上原始資料)':'imy2099999',
            'Shipping Name':'Recipient', 'Shipping Street':'Example Street', '收件人國家':'GERMANY',
            '郵局運送方式(複數商品請自行確認是否走小包)':'ePacket',
            '內容物1':'A','申告金額1':'2.50','數量1':'2'},
            'source_kind':'current_source_not_historical_snapshot','source_time':'2026-10-25T00:00:00Z',
            'tracking_baseline':['EL000000001JP'],'shipments':[],'source_ref':''}
        with patch('bot.shipment_service.lookup_history', return_value=history), patch('bot.shipment_ledger.encryption_secret', return_value='x'*40):
            app = self.authenticate(self.app('render_reissue_tab()'))
            next(x for x in app.text_input if x.label=='完整訂單編號').set_value('imy2099999').run()
            next(x for x in app.button if x.label=='查詢歷史訂單').click().run()
            self.assertTrue(any('非當時製單快照' in x.value for x in app.warning))
            next(x for x in app.button if x.label=='確認補製計畫').click().run()
            self.assertTrue(app.error)
            next(x for x in app.text_area if x.label=='補製原因').set_value('原運單破損').run()
            next(x for x in app.checkbox if '將產生新 tracking' in x.label).check().run()
            next(x for x in app.button if x.label=='確認補製計畫').click().run()
            self.assertFalse(app.exception)
            self.assertEqual(app.session_state['reissue_reviewed_plan']['mode'], 'reissue')
