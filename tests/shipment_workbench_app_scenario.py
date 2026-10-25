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
        ''') + '\n' + body, default_timeout=15)


    def authenticate(self, app):
        app.run()
        app.session_state['authenticated'] = True
        app.session_state['user_email'] = 'staff@tkrjm.co.jp'
        app.session_state['_verified_auth_email'] = 'staff@tkrjm.co.jp'
        return app.run()


    def test_actual_split_form_quantity_interaction_and_preview(self):
        app = self.authenticate(self.app("render_split_planner(row, source_row=row, key='test')"))
        next(x for x in app.toggle if x.label == '拆分包裹').set_value(True).run()
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
        self.assertEqual(app.button(key='test_execute').label, '送出拆單製單')
        self.assertEqual(list(app.dataframe[0].value.columns), ['包裹','方式','申告 USD','內容物'])
        self.assertTrue(any('計畫 ' in x.value for x in app.caption))
        result=[{'order_id':'imy2099999','tracking':'split-one','status':'completed','drive_file_id':'split_pdf'}]
        with patch('bot.shipment_service.execute_plan',return_value=result):
            app.button(key='test_execute').click().run()
        self.assertTrue(any('imy2099999｜split-one｜completed' in x.value for x in app.markdown))
        self.assertTrue(any(x.label=='開啟既有 PDF（重印，不製單）' for x in app.get('link_button')))


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


    def test_country_kind_ids_follow_shared_chinese_and_korean_classification(self):
        for country, key in [('中國', 'reissue_prc'), ('韓國', 'reissue_pccc')]:
            with self.subTest(country=country), patch('bot.shipment_service.lookup_history', return_value=self.history(country=country)):
                app = self.queried()
                self.assertTrue(any(x.key == key for x in app.text_input))


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
            self.assertTrue(any('補單製作完成' in x.value for x in app.success))
            self.assertTrue(any(x.label == '開啟本次補單' and x.proto.url.endswith('/pdf1/view') for x in app.get('link_button')))


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
                self.assertTrue(any('補單製作完成' in x.value for x in app.success))
                self.assertTrue(any(x.label == '開啟本次補單' and x.proto.url.endswith('/pdf1/view') for x in app.get('link_button')))
                if isinstance(refreshed, Exception):
                    self.assertNotIn('reissue_history', app.session_state)
                    self.assertNotIn('reissue_reviewed_plan', app.session_state)
                    self.assertTrue(any('重新查詢原訂單' in x.value for x in app.warning))
                    app.run()
                    self.assertTrue(any('重新查詢原訂單' in x.value for x in app.warning))
                    self.assertTrue(any('補單製作完成' in x.value for x in app.success))
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


    def setUp(self):
        self.pdf_patch = patch('bot.drive.resolve_original_label_urls', return_value={}, create=True)
        self.pdf = self.pdf_patch.start()
        self.addCleanup(self.pdf_patch.stop)

    def finish_commit_fence(self, app, edited_rows=None):
        # AppTest does not run browser auto-rerun timers. Model only the server
        # state after the separately tested true fragment acknowledgment.
        # Editor values still enter via the browser's native WidgetStates JSON.
        if 'reissue_intent' in app.session_state:
            intent=dict(app.session_state['reissue_intent'])
            if intent.get('stage') == 'waiting':
                intent['stage']='ready'
                app.session_state['reissue_intent']=intent
                return app.run() if edited_rows is None else self.run_editor_event(app,edited_rows)
        return app

    def submit(self, app):
        app.button(key='reissue_execute').click().run()
        return self.finish_commit_fence(app)

    def test_single_button_direct_submit_uses_current_native_widget_values(self):
        history = self.history()
        history['row'].update({'內容物3':'C','申告金額3':'3','數量3':'4','HSCode3':'333333'})
        with patch('bot.shipment_service.lookup_history', return_value=history), patch('bot.shipment_service.execute_plan', return_value=[]) as execute:
            app = self.queried('render_reissue_tab(usd_jpy_rate=150)')
            self.assertFalse(any(x.key in {'reissue_new_tracking','reissue_review'} for group in (app.checkbox,app.button) for x in group))
            self.assertEqual(app.button(key='reissue_execute').label, '開始製作補單')
            app.text_input(key='reissue_name').set_value('Latest Recipient')
            app.text_input(key='reissue_Shipping Street').set_value('Latest Street')
            app.selectbox(key='reissue_method').select('EMS')
            app.button(key='reissue_execute').click()
            self.run_editor_event(app, {'1':{'Description':'Changed C','Value':'4.00','Quantity':'2','HSCode':'444444'}})
            self.finish_commit_fence(app, {'1':{'Description':'Changed C','Value':'4.00','Quantity':'2','HSCode':'444444'}})
            self.assertFalse(app.exception)
            execute.assert_called_once()
            plan = execute.call_args.args[0]
            self.assertEqual(plan['payload']['Shipping Name'], 'Latest Recipient')
            self.assertEqual(plan['payload']['Shipping Street'], 'Latest Street')
            self.assertEqual(plan['payload']['內容物3'], 'Changed C')
            self.assertEqual(plan['payload']['HSCode3'], '444444')
            self.assertEqual(plan['payload']['數量3'], '2')
            self.assertEqual(plan['shipments'][0]['total_usd'], '13.00')
            self.assertEqual(plan['payload']['訂單合計申告金額(JPY)'], '1950')
            self.assertEqual(plan['shipments'][0]['method'], 'EMS')
            self.assertEqual(plan['source_kind'],history['source_kind'])
            self.assertEqual(plan['source_time'],history['source_time'])
            self.assertEqual(plan['source_snapshot']['Shipping Name'],'Recipient')
            self.assertEqual(execute.call_args.kwargs, {'requested_by':'staff@tkrjm.co.jp','tracking_baseline':['EL000000001JP'],'confirmed_new_tracking':True})

    def test_attempt_freezes_plan_nonce_actor_and_blocks_double_submission(self):
        for outcome in ([], [{'order_id':'imy2099999','tracking':'new','status':'completed'}], [{'order_id':'imy2099999','tracking':'new','status':'backfill_failed'}], RuntimeError('private')):
            with self.subTest(outcome=str(outcome)), patch('bot.shipment_service.lookup_history', return_value=self.history()) as lookup, patch('bot.shipment_service.execute_plan', side_effect=outcome if isinstance(outcome,Exception) else None, return_value=outcome) as execute:
                app = self.queried()
                nonce = app.session_state['reissue_nonce']
                self.submit(app)
                self.assertFalse(app.exception)
                self.assertEqual(execute.call_count,1)
                frozen = app.session_state['reissue_submission']
                self.assertEqual(frozen['verified_actor'], 'staff@tkrjm.co.jp')
                self.assertEqual(frozen['plan']['nonce'], nonce)
                app.run()
                self.assertTrue(app.button(key='reissue_execute').disabled)
                app.text_input(key='reissue_name').set_value('Changed After Attempt').run()
                self.submit(app)
                self.assertEqual(execute.call_count,1)
                self.assertEqual(app.session_state['reissue_nonce'], nonce)
                self.assertEqual(app.session_state['reissue_submission'],frozen)
                self.assertEqual(lookup.call_count,2)

    def test_validation_failure_is_editable_then_directly_submittable(self):
        history=self.history(); history['row']['Shipping Street']=''
        with patch('bot.shipment_service.lookup_history',return_value=history), patch('bot.shipment_service.execute_plan',return_value=[]) as execute:
            app=self.queried()
            self.submit(app)
            execute.assert_not_called()
            self.assertNotIn('reissue_submission',app.session_state)
            self.assertTrue(app.error)
            app.text_input(key='reissue_Shipping Street').set_value('Fixed Street').run()
            self.submit(app)
            execute.assert_called_once()

    def test_query_actor_change_requires_fresh_lookup_and_hides_pdf(self):
        self.pdf.return_value={'EL000000001JP':'https://drive.google.com/file/d/found/view'}
        with patch('bot.shipment_service.lookup_history',return_value=self.history()), patch('bot.shipment_service.execute_plan') as execute:
            app=self.queried()
            app.session_state['user_email']='other@tkrjm.co.jp'; app.session_state['_verified_auth_email']='other@tkrjm.co.jp'
            self.submit(app)
            execute.assert_not_called()
            self.assertFalse(app.get('link_button'))
            self.assertFalse(any(x.key=='reissue_name' for x in app.text_input))

    def test_split_default_two_empty_package_live_message_and_single_unit_hide(self):
        app=self.authenticate(self.app("render_split_planner(row, source_row=row, key='test')"))
        next(x for x in app.toggle if x.key=='test_enabled').set_value(True).run()
        self.assertEqual(app.number_input(key='test_count').value,2)
        self.assertTrue(any('包裹2中至少要有一項數量為1以上' in x.value for x in app.warning))
        self.assertTrue(app.button(key='test_review').disabled)
        body="row['數量1']='1'; row['內容物2']=''; render_split_planner(row, source_row=row, key='test')"
        app=self.authenticate(self.app(body)); app.session_state['test_enabled']=True; app.session_state['test_reviewed_plan']={'old':True}; app.run()
        self.assertFalse(app.toggle)
        self.assertFalse(app.session_state['test_enabled'])
        self.assertNotIn('test_reviewed_plan',app.session_state)

    def test_reissue_split_header_toggle_and_quantities_match_pending(self):
        with patch('bot.shipment_service.lookup_history',return_value=self.history()),patch('bot.shipment_service.execute_plan',return_value=[]) as execute:
            app=self.queried()
            app.toggle(key='reissue_split_enabled').set_value(True).run()
            self.assertEqual(app.number_input(key='reissue_split_count').value,2)
            self.assertTrue(app.button(key='reissue_execute').disabled)
            app.number_input(key='reissue_split_qty_0_1').set_value(1).run()
            app.number_input(key='reissue_split_qty_1_1').set_value(1).run()
            self.submit(app)
            plan=execute.call_args.args[0]
            self.assertEqual(plan['mode'],'reissue')
            self.assertEqual(plan['plan_version'],1)
            self.assertEqual([s['items'][0]['quantity'] for s in plan['shipments']],[1,1])

    def test_reissue_item_quantity_change_hides_toggle_and_clears_stale_split(self):
        with patch('bot.shipment_service.lookup_history',return_value=self.history()),patch('bot.shipment_service.execute_plan',return_value=[]) as execute:
            app=self.queried()
            app.toggle(key='reissue_split_enabled').set_value(True).run()
            self.run_editor_event(app,{'0':{'Quantity':'1'}})
            self.assertFalse(app.exception)
            self.assertFalse(app.toggle)
            self.assertFalse(app.session_state['reissue_split_enabled'])
            app.button(key='reissue_execute').click()
            self.run_editor_event(app,{'0':{'Quantity':'1'}})
            self.finish_commit_fence(app,{'0':{'Quantity':'1'}})
            self.assertEqual(execute.call_args.args[0]['shipments'][0]['items'][0]['quantity'],1)

    def test_reissue_additional_is_whole_order_deduplicates_method_and_excludes_split(self):
        with patch('bot.shipment_service.lookup_history',return_value=self.history()),patch('bot.shipment_service.execute_plan',return_value=[]) as execute:
            app=self.queried()
            app.selectbox(key='reissue_extra_method').select('EMS').run()
            app.toggle(key='reissue_split_enabled').set_value(True).run()
            self.assertTrue(app.button(key='reissue_execute').disabled)
            self.assertTrue(any('追加' in x.value for x in app.error))
            app.toggle(key='reissue_split_enabled').set_value(False).run()
            self.submit(app)
            plan=execute.call_args.args[0]
            self.assertEqual(plan['plan_version'],2)
            self.assertEqual([s['method'] for s in plan['shipments']],['ePacket','EMS'])
            self.assertEqual([s['items'][0]['quantity'] for s in plan['shipments']],[2,2])
        with patch('bot.shipment_service.lookup_history',return_value=self.history()),patch('bot.shipment_service.execute_plan',return_value=[]) as execute:
            app=self.queried(); app.selectbox(key='reissue_extra_method').select('ePacket').run()
            self.submit(app)
            self.assertEqual(len(execute.call_args.args[0]['shipments']),1)
            self.assertEqual(execute.call_args.args[0]['plan_version'],1)

    def test_original_pdf_controls_follow_safe_cache_and_keep_full_baseline(self):
        first='https://drive.google.com/file/d/first/view'
        second='https://drive.google.com/file/d/second/view'
        for found,expected in (({},'old-one'),({'old-one':first},'old-one'),
                               ({'old-two':second},'old-two'),({'old-one':first,'old-two':second},'old-two')):
            self.pdf.reset_mock(); self.pdf.return_value=found
            with self.subTest(found=list(found)),patch('bot.shipment_service.lookup_history',return_value=self.history(tracking=['old-one','old-two'])),patch('bot.shipment_service.execute_plan',return_value=[]) as execute:
                app=self.queried()
                self.assertEqual(app.button(key='reissue_query').label,'查詢訂單')
                self.assertFalse(any(x.key=='reissue_find_pdf' for x in app.button))
                self.assertFalse(any('重印' in x.label for x in app.expander))
                if len(found)>1:
                    self.assertEqual(app.selectbox(key='reissue_original_tracking').options,['old-one','old-two'])
                    app.selectbox(key='reissue_original_tracking').select('old-two').run()
                else:
                    self.assertFalse(any(x.key=='reissue_original_tracking' for x in app.selectbox))
                    self.assertEqual(any('關聯原運單：' in x.value for x in app.caption),bool(found))
                links=app.get('link_button')
                self.assertEqual(len(links),int(bool(found)))
                if found:
                    self.assertEqual(links[0].label,'開啟原運單')
                    self.assertEqual(links[0].proto.url,found[expected])
                app.text_input(key='reissue_name').set_value('Changed').run()
                app.run(); self.pdf.assert_called_once()
                self.submit(app)
                execute.assert_called_once()
                self.assertEqual(execute.call_args.args[0]['original_tracking'],expected)
                self.assertEqual(execute.call_args.kwargs['tracking_baseline'],['old-one','old-two'])

    def test_post_execution_refresh_failure_preserves_submission_and_results(self):
        with patch('bot.shipment_service.lookup_history',side_effect=[self.history(),RuntimeError('private')]),patch('bot.shipment_service.execute_plan',return_value=[]):
            app=self.queried(); self.submit(app)
            self.assertFalse(app.exception)
            self.assertNotIn('reissue_history',app.session_state)
            self.assertIn('reissue_submission',app.session_state)
            app._run()  # No historical widgets remain after failed lookup.
            self.assertFalse(any(x.key=='reissue_execute' for x in app.button))
            self.assertTrue(any('重新查詢原訂單' in x.value for x in app.warning))

    def test_new_explicit_query_starts_fresh_draft_after_completed_attempt(self):
        with patch('bot.shipment_service.lookup_history',return_value=self.history()),patch('bot.shipment_service.execute_plan',return_value=[]) as execute:
            app=self.queried(); first=app.session_state['reissue_nonce']
            self.submit(app)
            app.session_state['reissue_last_query']=0
            app.button(key='reissue_query').click().run()
            self.assertNotEqual(app.session_state['reissue_nonce'],first)
            self.assertNotIn('reissue_submission',app.session_state)
            self.submit(app)
            self.assertEqual(execute.call_count,2)
            self.assertNotEqual(execute.call_args_list[0].args[0]['plan_id'],execute.call_args_list[1].args[0]['plan_id'])

    def test_direct_unknown_and_partial_refresh_keep_recovery_and_never_new_submit(self):
        for status in ('unknown','writeback_pending'):
            record={'plan':{'plan_id':'saved','order_id':'imy2099999'},'shipments':[{'shipment_id':'s','status':status,'tracking':'new'}]}
            with self.subTest(status=status),patch('bot.shipment_service.lookup_history',side_effect=[self.history(),self.history(unfinished=[record])]),patch('bot.shipment_service.execute_plan',side_effect=RuntimeError('private')) as execute:
                app=self.queried(); nonce=app.session_state['reissue_nonce']
                self.submit(app); app.run()
                self.assertTrue(app.button(key='reissue_execute').disabled)
                self.assertEqual(app.button(key='resume_saved').disabled,status=='unknown')
                self.assertEqual(app.session_state['reissue_nonce'],nonce)
                execute.assert_called_once()

    def test_button_waits_for_browser_fence_and_uses_later_native_editor_delta(self):
        with patch('bot.shipment_service.lookup_history',return_value=self.history()),patch('bot.shipment_service.execute_plan',return_value=[]) as execute:
            app=self.queried('render_reissue_tab(usd_jpy_rate=150)')
            app.button(key='reissue_execute').click().run()
            execute.assert_not_called()
            self.assertEqual(app.session_state['reissue_intent']['stage'],'waiting')
            self.assertTrue(any('reissue-busy-guard' in x.value for x in app.markdown))
            app.run()  # Full rerenders, including editor callbacks, are not browser acknowledgments.
            execute.assert_not_called()
            intent=dict(app.session_state['reissue_intent']); intent['stage']='ready'; app.session_state['reissue_intent']=intent
            self.run_editor_event(app,{'0':{'Value':'21.00'}})
            execute.assert_called_once()
            self.assertEqual(execute.call_args.args[0]['shipments'][0]['total_usd'],'42.00')
            self.assertFalse(any('class="running-guard-overlay reissue-busy-guard"' in x.value for x in app.markdown))
            self.assertTrue(app.button(key='reissue_execute').disabled)

    def test_submission_error_remains_visible_on_every_rerender(self):
        with patch('bot.shipment_service.lookup_history',return_value=self.history()),patch('bot.shipment_service.execute_plan',side_effect=RuntimeError('PRIVATE')):
            app=self.queried(); app.button(key='reissue_execute').click().run()
            if 'reissue_intent' in app.session_state:
                intent=dict(app.session_state['reissue_intent']);intent['stage']='ready';app.session_state['reissue_intent']=intent;app.run()
            self.assertTrue(app.button(key='reissue_execute').disabled)
            self.assertTrue(any('工作已停止' in x.value for x in app.error))
            app.run()
            self.assertTrue(any('工作已停止' in x.value for x in app.error))
            self.assertFalse(any('PRIVATE' in x.value for x in app.error))

    def test_late_invalid_editor_input_cancels_intent_and_correction_requires_new_click(self):
        for field,bad in (('Quantity','illegal'),('Quantity','NaN'),('Quantity','Inf'),('Quantity','1.5'),('Quantity',''),('Value','illegal'),('Value','NaN'),('Value','Inf')):
            with self.subTest(field=field,bad=bad),patch('bot.shipment_service.lookup_history',return_value=self.history()),patch('bot.shipment_service.execute_plan',return_value=[]) as execute:
                app=self.queried('render_reissue_tab(usd_jpy_rate=150)')
                app.button(key='reissue_execute').click().run()
                old=dict(app.session_state['reissue_intent'])
                self.run_editor_event(app,{'0':{field:bad}})
                self.assertFalse(app.exception)
                self.assertTrue(app.error)
                self.assertNotIn('reissue_intent',app.session_state)
                self.assertNotIn('reissue_submission',app.session_state)
                self.assertTrue(app.button(key='reissue_execute').disabled)
                execute.assert_not_called()
                fixed='2' if field=='Quantity' else '2.50'
                self.run_editor_event(app,{'0':{field:fixed}})
                self.assertFalse(app.exception)
                self.assertNotIn('reissue_intent',app.session_state)
                self.assertFalse(app.button(key='reissue_execute').disabled)
                app.run()
                execute.assert_not_called()
                # Even a captured true fragment tick from the discarded click cannot revive it.
                from types import SimpleNamespace
                from features import shipment_workbench as workbench
                with patch.object(workbench,'st',SimpleNamespace(session_state=dict(app.session_state._state.filtered_state))),patch.object(workbench,'verified_email',return_value='staff@tkrjm.co.jp'):
                    self.assertFalse(workbench._accept_reissue_commit_ack(old['actor'],old['nonce'],old['token'],context=SimpleNamespace(current_fragment_id='old',fragment_ids_this_run=['old'])))
                self.submit(app)
                execute.assert_called_once()

    def test_late_invalid_input_while_split_enabled_clears_safely_without_revival(self):
        for field,bad in (('Quantity','illegal'),('Value','NaN')):
            with self.subTest(field=field),patch('bot.shipment_service.lookup_history',return_value=self.history()),patch('bot.shipment_service.execute_plan',return_value=[]) as execute:
                app=self.queried('render_reissue_tab(usd_jpy_rate=150)')
                app.toggle(key='reissue_split_enabled').set_value(True).run()
                app.number_input(key='reissue_split_qty_0_1').set_value(1).run()
                app.number_input(key='reissue_split_qty_1_1').set_value(1).run()
                app.button(key='reissue_execute').click().run()
                self.run_editor_event(app,{'0':{field:bad}})
                self.assertFalse(app.exception)
                self.assertNotIn('reissue_intent',app.session_state)
                self.assertFalse(app.session_state['reissue_split_enabled'])
                self.run_editor_event(app,{'0':{field:'2' if field=='Quantity' else '2.50'}})
                app.run()
                execute.assert_not_called()
                self.assertNotIn('reissue_intent',app.session_state)

    def test_invalid_edits_after_execution_keep_frozen_submission(self):
        with patch('bot.shipment_service.lookup_history',return_value=self.history()),patch('bot.shipment_service.execute_plan',return_value=[]) as execute:
            app=self.queried();self.submit(app)
            frozen=dict(app.session_state['reissue_submission'])
            self.run_editor_event(app,{'0':{'Quantity':'illegal'}})
            self.assertFalse(app.exception)
            self.assertEqual(app.session_state['reissue_submission'],frozen)
            self.assertTrue(app.button(key='reissue_execute').disabled)
            self.run_editor_event(app,{'0':{'Quantity':'2'}})
            app.run()
            self.assertEqual(app.session_state['reissue_submission'],frozen)
            execute.assert_called_once()

    def test_reissue_results_are_in_action_panel_and_keep_original_and_new_pdf_distinct(self):
        self.pdf.return_value={'EL000000001JP':'https://drive.google.com/file/d/original/view'}
        results=[{'order_id':'imy2099999','tracking':'new-one','status':'completed','drive_file_id':'pdf1'}]
        with patch('bot.shipment_service.lookup_history',return_value=self.history()),patch('bot.shipment_service.execute_plan',return_value=results):
            app=self.queried();self.submit(app)
            self.assertEqual(app.session_state['reissue_results'],results)
            self.assertFalse(any('imy2099999｜new-one｜completed' in x.value for x in app.markdown))
            self.assertFalse(any(x.label=='開啟既有 PDF（重印，不製單）' for x in app.get('link_button')))
            right=next(c for c in app.columns if any(x.key=='reissue_execute' for x in c.button))
            self.assertTrue(any('補單製作完成' in x.value for x in right.success))
            self.assertEqual({x.label for x in right.get('link_button')},{'開啟原運單','開啟本次補單'})

    def test_reissue_partial_results_keep_tracking_and_package_pdf_without_machine_status(self):
        results=[{'order_id':'imy2099999','tracking':'new-one','status':'completed','drive_file_id':'pdf1'},
                 {'order_id':'imy2099999','tracking':'new-two','status':'backfill_failed','drive_file_id':'pdf2'}]
        with patch('bot.shipment_service.lookup_history',return_value=self.history()),patch('bot.shipment_service.execute_plan',return_value=results):
            app=self.queried();self.submit(app)
            self.assertTrue(any('回填' in x.value for x in app.warning))
            self.assertTrue(any('new-two' in x.value for x in app.caption))
            self.assertFalse(any('backfill_failed' in x.value for x in app.markdown))
            self.assertEqual({x.label for x in app.get('link_button')},{'開啟本次補單（包裹 1）','開啟本次補單（包裹 2）'})
            self.assertFalse(any('class="running-guard-overlay reissue-busy-guard"' in x.value for x in app.markdown))

    def test_resume_preserves_native_tab_container_identity_without_root_slot(self):
        def tab_paths(block, path=()):
            paths=[]
            for index,child in block.children.items():
                location=path+(index,)
                if getattr(child,'type','')=='tab_container':paths.append(location)
                if hasattr(child,'children'):paths.extend(tab_paths(child,location))
            return paths
        record={'plan':{'plan_id':'FAKEPLAN','order_id':'imy2099999'},
                'shipments':[{'shipment_id':'s1','status':'uploaded','tracking':'new-one'}]}
        result=[{'order_id':'imy2099999','tracking':'new-one','status':'completed','drive_file_id':'pdf1'}]
        body="tabs=st.tabs(['first','補製單']);\nwith tabs[1]: render_reissue_tab()"
        for outcome in (result,RuntimeError('fake unknown')):
            refreshed=self.history()
            if isinstance(outcome,Exception):
                unknown={'plan':record['plan'],'shipments':[{'shipment_id':'s1','status':'unknown','tracking':'new-one'}]}
                refreshed=self.history(unfinished=[unknown])
            with self.subTest(outcome=type(outcome).__name__),patch('bot.shipment_service.lookup_history',side_effect=[self.history(unfinished=[record]),refreshed]),patch('bot.shipment_service.execute_plan',side_effect=outcome if isinstance(outcome,Exception) else None,return_value=outcome) as execute:
                app=self.queried(body)
                before=tab_paths(app._tree.main)
                self.assertEqual(before,[(0,)])
                app.button(key='resume_FAKEPLAN').click().run()
                self.assertFalse(app.exception)
                self.assertEqual(tab_paths(app._tree.main),before)
                # Callback updates arrive before the page deltas; UI order is
                # determined by their native child addresses, not dict insertion.
                tabs=app._tree.main.children[0]
                self.assertEqual([child.label for _,child in sorted(tabs.children.items())],['first','補製單'])
                self.assertFalse(any('class="running-guard-overlay reissue-busy-guard"' in x.value for x in app.markdown))
                execute.assert_called_once()
