import json
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

    def history(self, *, tracking=None, unfinished=None, country='GERMANY'):
        return {'order_id':'imy2099999','row':{'注文番号(貼上原始資料)':'imy2099999',
            'Shipping Name':'Recipient', 'Shipping Street':'Example Street', 'Shipping City':'Berlin',
            '收件人洲/省':'Berlin', 'Shipping Zip':'10115', 'Shipping Phone':'030123456',
            '收件人國家':country, '郵局運送方式(複數商品請自行確認是否走小包)':'ePacket',
            '內容物1':'A','申告金額1':'2.50','數量1':'2', 'HSCode1':'111111',
            '訂單合計申告金額(JPY)':'750'},
            'source_kind':'current_source_not_historical_snapshot','source_time':'2026-10-25T00:00:00Z',
            'tracking_baseline':tracking if tracking is not None else ['EL000000001JP'],
            'shipments':[], 'source_ref':'', 'unfinished_records':unfinished or []}

    def queried(self, body='render_reissue_tab()'):
        app = self.authenticate(self.app(body))
        app.text_input(key='reissue_order_query').set_value('imy2099999').run()
        app.button(key='reissue_query').click().run()
        self.assertFalse(app.exception)
        return app

    def run_editor_event(self, app, edited_rows):
        # AppTest renders data_editor as a read-only Dataframe and omits its
        # state from public .run(). Send the native widget JSON on every rerun,
        # just as the browser does, without patching the editor or its return.
        states = app._tree.get_widget_states()
        editor_id = app.dataframe[0].proto.id
        state = next((state for state in states.widgets if state.id == editor_id), None)
        if state is None:
            state = states.widgets.add(id=editor_id)
        state.string_value = json.dumps({'edited_rows': edited_rows, 'added_rows': [], 'deleted_rows': []})
        return app._run(widget_state=states)

    def test_reissue_card_has_expanded_address_v2_items_and_no_reason(self):
        with patch('bot.shipment_service.lookup_history', return_value=self.history()):
            app = self.queried()
            self.assertFalse(app.text_area)
            self.assertFalse(any(x.label == '收件地址與聯絡資料' for x in app.expander))
            for key in ('Shipping Street','Shipping City','收件人洲/省','Shipping Zip','Shipping Phone'):
                self.assertTrue(app.text_input(key='reissue_' + key).value)
            self.assertFalse(any('Country' == x.label for x in app.text_input))
            self.assertEqual(list(app.dataframe[0].value.columns), ['No.','Description','HSCode','Value','Quantity'])
            self.assertEqual(app.dataframe[0].value.iloc[0]['No.'], '1')
            self.assertTrue(any('postal-v2-card-marker' in x.value for x in app.markdown))
            self.assertTrue(any('非當時製單快照' in x.value for x in app.warning))
            app.button(key='reissue_review').click().run()
            self.assertTrue(app.error)
            self.assertFalse(any(x.key == 'reissue_execute' for x in app.button))
            app.checkbox(key='reissue_new_tracking').check().run()
            app.button(key='reissue_review').click().run()
            self.assertEqual(app.session_state['reissue_reviewed_plan']['reason'], '')
            self.assertTrue(any(x.key == 'reissue_execute' for x in app.button))

    def test_edit_invalidates_review_and_single_or_multiple_original_tracking(self):
        with patch('bot.shipment_service.lookup_history', return_value=self.history()):
            app = self.queried()
            self.assertFalse(any(x.key == 'reissue_original_tracking' for x in app.selectbox))
            app.checkbox(key='reissue_new_tracking').check().run()
            app.button(key='reissue_review').click().run()
            app.text_input(key='reissue_Shipping Street').set_value('Updated Street').run()
            self.assertFalse(any(x.key == 'reissue_execute' for x in app.button))
            app.text_input(key='reissue_Shipping Street').set_value('Example Street').run()
            self.assertNotIn('reissue_reviewed_plan', app.session_state)
            self.assertFalse(any(x.key == 'reissue_execute' for x in app.button))
            app.text_input(key='reissue_Shipping Street').set_value('Updated Street').run()
            app.button(key='reissue_review').click().run()
            self.assertEqual(app.session_state['reissue_reviewed_plan']['payload']['Shipping Street'], 'Updated Street')
        with patch('bot.shipment_service.lookup_history', return_value=self.history(tracking=['old-one','old-two'])):
            app = self.queried()
            app.selectbox(key='reissue_original_tracking').select('old-two').run()
            app.checkbox(key='reissue_new_tracking').check().run()
            app.button(key='reissue_review').click().run()
            self.assertEqual(app.session_state['reissue_reviewed_plan']['original_tracking'], 'old-two')

    def test_country_kind_ids_follow_shared_chinese_and_korean_classification(self):
        for country, key in [('中國', 'reissue_prc'), ('韓國', 'reissue_pccc')]:
            with self.subTest(country=country), patch('bot.shipment_service.lookup_history', return_value=self.history(country=country)):
                app = self.queried()
                self.assertTrue(any(x.key == key for x in app.text_input))

    def test_reprint_is_secondary_and_drive_search_is_lazy_with_empty_feedback(self):
        with patch('bot.shipment_service.lookup_history', return_value=self.history()), patch('bot.drive.list_drive_files', return_value=[]) as search:
            app = self.queried()
            app.text_input(key='reissue_name').set_value('Edited Recipient').run()
            search.assert_not_called()
            reprint = next(x for x in app.expander if '重印' in x.label)
            self.assertTrue(any(x.key == 'reissue_find_pdf' for x in reprint.button))
            app.button(key='reissue_find_pdf').click().run()
            search.assert_called_once()
            self.assertTrue(any('找不到' in x.value for x in app.info))
            app.run()
            search.assert_called_once()

    def test_resume_is_conditional_and_never_rereads_ledger_on_rerun(self):
        record = {'plan':{'plan_id':'legacy', 'order_id':'imy2099999', 'reason':'old reason'},
                  'shipments':[{'shipment_id':'s1','status':'uploaded','tracking':'new-one'}]}
        with patch('bot.shipment_service.lookup_history', return_value=self.history()) as lookup, patch('bot.shipment_ledger.configured_ledger') as ledger:
            app = self.queried().run()
            self.assertFalse(any('未完成' in x.label for x in app.expander))
            self.assertFalse(any(x.key == 'reissue_resume_visible' for x in app.checkbox))
            ledger.assert_not_called()
            lookup.assert_called_once()
        with patch('bot.shipment_service.lookup_history', side_effect=[self.history(unfinished=[record]), self.history()]) as lookup, patch('bot.shipment_service.execute_plan', return_value=[]) as execute, patch('bot.shipment_ledger.configured_ledger') as ledger:
            app = self.queried()
            self.assertTrue(any('未完成' in x.label for x in app.expander))
            app.button(key='resume_legacy').click().run()
            execute.assert_called_once_with(record['plan'], requested_by='staff@tkrjm.co.jp')
            self.assertEqual(lookup.call_count, 2)
            ledger.assert_not_called()
            app.run()
            self.assertFalse(any('未完成' in x.label for x in app.expander))
            self.assertEqual(lookup.call_count, 2)

    def test_unknown_resume_is_blocked_and_planned_can_resume(self):
        for status in ('issuing','unknown','label_created','planned','writeback_pending'):
            record = {'plan':{'plan_id':'legacy', 'order_id':'imy2099999'},
                      'shipments':[{'shipment_id':'s1','status':status,'tracking':''}]}
            with self.subTest(status=status), patch('bot.shipment_service.lookup_history', return_value=self.history(unfinished=[record])):
                app = self.queried()
                self.assertEqual(app.button(key='resume_legacy').disabled, status in {'issuing','unknown','label_created'})

    def test_execute_success_partial_and_exception_refresh_history_and_invalidate_review(self):
        for result in ([{'order_id':'imy2099999','tracking':'new-one','status':'completed','drive_file_id':'pdf1'}],
                       [{'order_id':'imy2099999','tracking':'new-one','status':'backfill_failed','drive_file_id':'pdf1'}], RuntimeError('stopped')):
            unfinished = [] if isinstance(result, list) and result[0]['status'] == 'completed' else [
                {'plan':{'plan_id':'saved', 'order_id':'imy2099999'}, 'shipments':[{'shipment_id':'s1','status':'unknown' if isinstance(result, Exception) else 'writeback_pending','tracking':'new-one'}]}]
            with self.subTest(result=str(result)), patch('bot.shipment_service.lookup_history', side_effect=[self.history(), self.history(unfinished=unfinished)]) as lookup, patch('bot.shipment_service.execute_plan', side_effect=result if isinstance(result, Exception) else None, return_value=result) as execute:
                app = self.queried()
                app.checkbox(key='reissue_new_tracking').check().run()
                app.button(key='reissue_review').click().run()
                app.button(key='reissue_execute').click().run()
                self.assertFalse(app.exception)
                self.assertEqual(lookup.call_count, 2)
                self.assertTrue(execute.call_args.kwargs['confirmed_new_tracking'])
                self.assertNotIn('reissue_reviewed_plan', app.session_state)
                app.run()
                self.assertEqual(any('未完成' in x.label for x in app.expander), bool(unfinished))
                self.assertFalse(any(x.key == 'reissue_execute' for x in app.button))
                self.assertEqual(lookup.call_count, 2)

    def test_item_edits_preserve_source_positions_values_quantities_and_calculated_usd(self):
        history = self.history()
        history['row'].update({'內容物3':'C','申告金額3':'3.00','數量3':'4','HSCode3':'333333'})
        edits = {'1':{'Description':'Changed C','Value':'4.00','Quantity':'2','HSCode':'444444'}}
        with patch('bot.shipment_service.lookup_history', return_value=history):
            app = self.queried()
            self.run_editor_event(app, edits)
            app.checkbox(key='reissue_new_tracking').check()
            self.run_editor_event(app, edits)
            app.button(key='reissue_review').click()
            self.run_editor_event(app, edits)
            plan = app.session_state['reissue_reviewed_plan']
            self.assertEqual(plan['payload']['內容物3'], 'Changed C')
            self.assertEqual(plan['payload']['HSCode3'], '444444')
            self.assertEqual(plan['payload']['數量3'], '2')
            self.assertEqual(plan['shipments'][0]['total_usd'], '13.00')
            self.assertEqual([i['source_index'] for i in plan['shipments'][0]['items']], ['1','3'])
            self.run_editor_event(app, {'1':{**edits['1'], 'Value':'5.00'}})
            self.assertNotIn('reissue_reviewed_plan', app.session_state)
            self.assertFalse(any(x.key == 'reissue_execute' for x in app.button))

    def test_existing_pdf_is_available_in_secondary_reprint_without_drive_search(self):
        history = self.history()
        history['shipments'] = [{'tracking':'EL000000001JP','drive_file_id':'saved_pdf','status':'completed'}]
        with patch('bot.shipment_service.lookup_history', return_value=history), patch('bot.drive.list_drive_files') as search:
            app = self.queried()
            reprint = next(x for x in app.expander if '重印' in x.label)
            links = reprint.get('link_button')
            self.assertEqual(len(links), 1)
            self.assertEqual(links[0].proto.url, 'https://drive.google.com/file/d/saved_pdf/view')
            search.assert_not_called()
            self.assertEqual(sum('EL000000001JP' in x.value for x in app.caption), 1)

    def test_resume_exception_and_partial_refresh_without_resubmitting(self):
        record = {'plan':{'plan_id':'saved','order_id':'imy2099999','reason':'legacy'},
                  'shipments':[{'shipment_id':'s1','status':'writeback_pending','tracking':'new-one'}]}
        for result in (RuntimeError('stopped'), [{'order_id':'imy2099999','tracking':'new-one','status':'backfill_failed'}]):
            with self.subTest(result=str(result)), patch('bot.shipment_service.lookup_history', return_value=self.history(unfinished=[record])) as lookup, patch('bot.shipment_service.execute_plan', side_effect=result if isinstance(result, Exception) else None, return_value=result) as execute:
                app = self.queried()
                app.button(key='resume_saved').click().run()
                self.assertFalse(app.exception)
                self.assertEqual(lookup.call_count, 2)
                execute.assert_called_once()
                app.run()
                self.assertEqual(lookup.call_count, 2)
                execute.assert_called_once()

    def test_post_execution_refresh_failure_requires_new_query_and_blocks_stale_review(self):
        with patch('bot.shipment_service.lookup_history', side_effect=[self.history(), RuntimeError('unavailable')]) as lookup, patch('bot.shipment_service.execute_plan', side_effect=RuntimeError('stopped')):
            app = self.queried()
            app.checkbox(key='reissue_new_tracking').check().run()
            app.button(key='reissue_review').click().run()
            app.button(key='reissue_execute').click().run()
            self.assertFalse(app.exception)
            self.assertNotIn('reissue_history', app.session_state)
            self.assertNotIn('reissue_reviewed_plan', app.session_state)
            self.assertTrue(any('重新查詢原訂單' in x.value for x in app.warning))
            app.run()
            self.assertFalse(any(x.key == 'reissue_execute' for x in app.button))
            self.assertEqual(lookup.call_count, 2)

    def test_resume_results_remain_visible_after_completed_record_disappears(self):
        record = {'plan':{'plan_id':'saved','order_id':'imy2099999'},
                  'shipments':[{'shipment_id':'s1','status':'uploaded','tracking':'new-one'}]}
        result = [{'order_id':'imy2099999','tracking':'new-one','status':'completed','drive_file_id':'pdf1'}]
        with patch('bot.shipment_service.lookup_history', side_effect=[self.history(tracking=[], unfinished=[record]), self.history()]), patch('bot.shipment_service.execute_plan', return_value=result):
            app = self.queried()
            self.assertFalse(any(x.key == 'reissue_review' for x in app.button))
            app.button(key='resume_saved').click().run()
            app.run()
            self.assertEqual(app.session_state['reissue_results'], result)
            self.assertTrue(any('new-one' in x.value for x in app.markdown))
            self.assertTrue(any(x.proto.url.endswith('/pdf1/view') for x in app.get('link_button')))

    def test_reissue_uses_passed_rate_only_for_edited_values_and_warns_without_rate(self):
        for rate, edit, expected in ((150, True, '1800'), (150, False, '750'), (None, True, '750')):
            edits = {'0':{'Value':'4.00','Quantity':'3'}} if edit else {}
            with self.subTest(rate=rate, edit=edit), patch('bot.shipment_service.lookup_history', return_value=self.history()):
                app = self.queried(f'render_reissue_tab(usd_jpy_rate={rate!r})')
                self.run_editor_event(app, edits)
                self.assertTrue(any('native-info-label">JPY</span><span class="native-info-value">' + expected in x.value for x in app.markdown))
                self.assertEqual(any('保留來源預設值' in x.value for x in app.warning), rate is None)
                app.checkbox(key='reissue_new_tracking').check()
                self.run_editor_event(app, edits)
                app.button(key='reissue_review').click()
                self.run_editor_event(app, edits)
                plan = app.session_state['reissue_reviewed_plan']
                self.assertEqual(plan['payload']['訂單合計申告金額(JPY)'], expected)
                self.assertEqual(plan['shipments'][0]['total_usd'], '12.00' if edit else '5.00')

    def test_native_editor_transport_retains_value_and_identity_through_confirmation(self):
        history = self.history()
        history['row'].update({'申告金額1':'15.00','數量1':'2','訂單合計申告金額(JPY)':'4500'})
        edits = {'0':{'Value':'20.00'}}
        with patch('bot.shipment_service.lookup_history', return_value=history):
            app = self.queried('render_reissue_tab(usd_jpy_rate=150)')
            editor_id = app.dataframe[0].proto.id
            self.run_editor_event(app, edits)
            self.assertTrue(any('40.00' in x.value for x in app.markdown))
            self.assertTrue(any('6000' in x.value for x in app.markdown))
            app.checkbox(key='reissue_new_tracking').check()
            self.run_editor_event(app, edits)
            self.assertEqual(app.dataframe[0].proto.id, editor_id)
            app.button(key='reissue_review').click()
            self.run_editor_event(app, edits)
            self.assertFalse(app.exception)
            self.assertEqual(app.dataframe[0].proto.id, editor_id)
            plan = app.session_state['reissue_reviewed_plan']
            self.assertEqual(plan['payload']['申告金額1'], '20.00')
            self.assertEqual(plan['shipments'][0]['total_usd'], '40.00')
            self.assertEqual(plan['payload']['訂單合計申告金額(JPY)'], '6000')

    def test_resume_immediately_replaces_planned_with_unknown_and_keeps_safe_error(self):
        import copy
        planned = {'plan':{'plan_id':'saved','order_id':'imy2099999'},
                   'shipments':[{'shipment_id':'s1','status':'planned','tracking':''}]}
        unknown = copy.deepcopy(planned)
        unknown['shipments'][0]['status'] = 'unknown'
        with patch('bot.shipment_service.lookup_history', side_effect=[self.history(unfinished=[planned]), self.history(unfinished=[unknown])]) as lookup, patch('bot.shipment_service.execute_plan', side_effect=RuntimeError('PRIVATE_INTERNAL_ERROR')) as execute:
            app = self.queried()
            self.assertFalse(app.button(key='resume_saved').disabled)
            app.button(key='resume_saved').click().run()
            self.assertFalse(app.exception)
            self.assertTrue(app.button(key='resume_saved').disabled)
            self.assertTrue(any('s1｜unknown' in x.value for x in app.markdown))
            self.assertFalse(any('s1｜planned' in x.value for x in app.markdown))
            self.assertTrue(any('恢復已停止' in x.value for x in app.warning))
            self.assertFalse(any('PRIVATE_INTERNAL_ERROR' in x.value for x in app.warning))
            self.assertEqual(lookup.call_count, 2)
            execute.assert_called_once()

    def test_resume_completed_and_refresh_failure_redraw_immediately_with_results_or_warning(self):
        record = {'plan':{'plan_id':'saved','order_id':'imy2099999'},
                  'shipments':[{'shipment_id':'s1','status':'uploaded','tracking':'new-one'}]}
        result = [{'order_id':'imy2099999','tracking':'new-one','status':'completed','drive_file_id':'pdf1'}]
        for refreshed in (self.history(), RuntimeError('PRIVATE_LOOKUP')):
            with self.subTest(refreshed=type(refreshed).__name__), patch('bot.shipment_service.lookup_history', side_effect=[self.history(unfinished=[record]), refreshed]) as lookup, patch('bot.shipment_service.execute_plan', return_value=result) as execute:
                app = self.queried()
                app.button(key='resume_saved').click().run()
                self.assertFalse(app.exception)
                self.assertFalse(any(x.key == 'resume_saved' for x in app.button))
                self.assertTrue(any('new-one' in x.value for x in app.markdown))
                self.assertTrue(any(x.proto.url.endswith('/pdf1/view') for x in app.get('link_button')))
                if isinstance(refreshed, Exception):
                    self.assertNotIn('reissue_history', app.session_state)
                    self.assertNotIn('reissue_reviewed_plan', app.session_state)
                    self.assertTrue(any('重新查詢原訂單' in x.value for x in app.warning))
                    app.run()
                    self.assertTrue(any('重新查詢原訂單' in x.value for x in app.warning))
                    self.assertTrue(any('new-one' in x.value for x in app.markdown))
                self.assertEqual(lookup.call_count, 2)
                execute.assert_called_once()

    def test_resume_callback_revalidates_login_and_rejects_captured_old_identity(self):
        record = {'plan':{'plan_id':'saved','order_id':'imy2099999'},
                  'shipments':[{'shipment_id':'s1','status':'uploaded','tracking':'new-one'}]}
        for signed_in, email in ((False, ''), (True, 'other@tkrjm.co.jp')):
            with self.subTest(signed_in=signed_in, email=email), patch('bot.shipment_service.lookup_history', return_value=self.history(unfinished=[record])) as lookup, patch('bot.shipment_service.execute_plan', return_value=[]) as execute:
                app = self.queried()
                app.session_state['reissue_reviewed_plan'] = {'old':'review'}
                app.session_state['reissue_results'] = [{'order_id':'imy2099999','tracking':'old','status':'completed'}]
                app.session_state['authenticated'] = signed_in
                app.session_state['user_email'] = email
                app.session_state['_verified_auth_email'] = email
                app.button(key='resume_saved').click().run()
                self.assertFalse(app.exception)
                execute.assert_not_called()
                self.assertEqual(lookup.call_count, 1)
                for key in ('reissue_history','reissue_reviewed_plan','reissue_results'):
                    self.assertNotIn(key, app.session_state)
                self.assertIn('重新', app.session_state['reissue_resume_warning'])
