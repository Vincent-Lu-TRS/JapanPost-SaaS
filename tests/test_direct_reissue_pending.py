import ast
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import pandas as pd
from pending_editor import SHIPPING_COL, apply_pending_order_editor_values, build_pending_item_frame, compose_shipping_name, parse_shipping_name, expand_pending_orders_for_trans_types
from postal_ui_v2 import build_v2_item_display_frame, restore_v2_item_frame

class PendingSplitRoutingTests(unittest.TestCase):
    def test_current_single_unit_never_disappears_and_real_split_or_conflict_never_normal_send(self):
        source = Path('app.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        fn = next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_build_pending_run_frame_from_v2_state')
        from features.shipment_workbench import effective_split_enabled
        ns = dict(effective_split_enabled=effective_split_enabled, pd=pd, st=SimpleNamespace(session_state={'split_test_0_enabled':True}),
                  _order_id_for_position=lambda row,pos:'test', _v2_reset_version=lambda order:0,
                  _v2_item_key_for=lambda *args:'editor', _v2_trans_key_for=lambda *args:'method',
                  _v2_name_key_for=lambda *args:'name', _v2_prc_id_key_for=lambda *args:'prc',
                  _v2_pccc_key_for=lambda *args:'pccc', _sync_recipient_id_session_fields=lambda *args:None,
                  _v2_selected_source_indices_from_state=lambda frame:[0],
                  _v2_extra_trans_types_by_index_from_state=lambda *args:{0:['EMS']},
                  **{name:globals()[name] for name in ('apply_pending_order_editor_values','build_pending_item_frame','compose_shipping_name','parse_shipping_name','expand_pending_orders_for_trans_types','build_v2_item_display_frame','restore_v2_item_frame','SHIPPING_COL')})
        frame=pd.DataFrame([{'注文番号(貼上原始資料)':'test','Shipping Name':'Recipient','收件人國家':'GERMANY',SHIPPING_COL:'ePacket','內容物1':'A','申告金額1':'2','數量1':'2'}])
        module=ast.Module(body=[fn],type_ignores=[]); ast.fix_missing_locations(module); exec(compile(module,'app.py','exec'),ns)
        for quantity, expected in (('1',2),('2',0)):
            ns['_apply_data_editor_state']=lambda item,key:item.assign(Quantity=quantity)
            with patch('features.shipment_workbench.st', ns['st']):
                result=ns['_build_pending_run_frame_from_v2_state'](frame,1,None)
            self.assertEqual(len(result),expected)

    def test_real_main_app_shared_geometry_and_native_editor_header_hide(self):
        import subprocess, sys, textwrap
        from tests.test_postal_ui_v2_app import APP_TEST_SCRIPT
        prefix = APP_TEST_SCRIPT[:APP_TEST_SCRIPT.index('app = AppTest.from_file')]
        scenario = r"""
        import json
        from unittest.mock import patch
        for country in ('GERMANY','CHINA','KOREA'):
            import streamlit as st
            st.cache_data.clear(); st.cache_resource.clear()
            mock_pending.loc[0,'收件人國家'] = country
            mock_pending.loc[0,'數量1'] = '2'
            mock_pending.loc[0,['內容物2','申告金額2','數量2','HSCode2']] = ''
            pending_loader.return_value=mock_pending.copy(deep=True)
            history={'order_id':'imy2099999','row':dict(mock_pending.iloc[0]),'source_kind':'current_source_not_historical_snapshot',
                     'source_time':'stamp','source_ref':'','tracking_baseline':['old'],'shipments':[],'unfinished_records':[]}
            history['row'].update({'注文番号(貼上原始資料)':'imy2099999','Shipping Street':'Example Street'})
            with patch('bot.shipment_service.lookup_history',return_value=history), patch('bot.drive.resolve_original_label_urls',return_value={}):
                app=AppTest.from_file(str(Path.cwd()/'app.py')).run(timeout=30)
                app.session_state['user_name']='Synthetic Tester'; app.session_state['authenticated']=True; app.session_state['user_email']='staff@tkrjm.co.jp'; app.session_state['_verified_auth_email']='staff@tkrjm.co.jp'
                app.run(timeout=30)
                app.text_input(key='reissue_order_query').set_value('imy2099999').run()
                app.button(key='reissue_query').click().run()
                def widths_for(kind,key):
                    candidates=[c for c in app.columns if any(x.key==key for x in getattr(c,kind))]
                    return min(candidates,key=lambda c:len(list(c)) if hasattr(c,'__iter__') else len(getattr(c,kind))).proto.weight
                pending_name=next(x.key for x in app.text_input if x.key.startswith('pending_v2_name_0_'))
                pending_method=next(x.key for x in app.selectbox if x.key.startswith('pending_v2_trans_0_'))
                pending_extra=next(x.key for x in app.selectbox if x.key.startswith('pending_v2_extra_trans_0_'))
                for kind,left,right in [('text_input',pending_name,'reissue_name'),('selectbox',pending_method,'reissue_method'),('selectbox',pending_extra,'reissue_extra_method')]:
                    assert abs(widths_for(kind,left)-widths_for(kind,right))<1e-8,(country,left,right,widths_for(kind,left),widths_for(kind,right))
                assert not app.exception,app.exception
                toggle=app.toggle(key='split_mock-v2-8490_0_enabled')
                cols=[c for c in app.columns if any(x.key==toggle.key for x in c.toggle)]
                header=min(cols,key=lambda c:len(list(c)))
                assert abs(header.proto.weight-(1/7.5))<1e-6,header.proto.weight
                toggle.set_value(True).run()
                assert app.number_input(key='split_mock-v2-8490_0_count').value==2
                # Browser sends the final editor delta on this same rerun.
                editor=next(d for d in app.dataframe if d.proto.id.endswith('-pending_v2_items_0_mock-v2-8490_0'))
                states=app._tree.get_widget_states(); native=states.widgets.add(id=editor.proto.id)
                native.string_value=json.dumps({'edited_rows':{'0':{'Quantity':'1'}},'added_rows':[],'deleted_rows':[]})
                # One remaining unit hides and clears the former split state on the same event.
                app._run(widget_state=states)
                assert not app.exception,app.exception
                assert not any(t.key=='split_mock-v2-8490_0_enabled' for t in app.toggle)
                assert app.session_state['split_mock-v2-8490_0_enabled'] is False
        """
        result=subprocess.run([sys.executable,'-c',prefix+textwrap.dedent(scenario)],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=100)
        self.assertEqual(result.returncode,0,result.stdout[-1500:]+result.stderr[-3500:])

    def test_both_workbenches_share_scoped_container_responsiveness(self):
        source=Path('app.py').read_text(encoding='utf-8')
        from features.shipment_workbench import REISSUE_CARD_CSS
        self.assertIn('with st.container(key="pending_workbench"):',source)
        self.assertIn('key=f"pending_order_card_{position}"',source)
        self.assertIn('key=f"pending_recipient_fields_{position}"',source)
        self.assertIn('st.markdown(REISSUE_CARD_CSS, unsafe_allow_html=True)',source)
        for selector in ('.st-key-pending_workbench','.st-key-reissue_workbench','st-key-pending_order_card_','st-key-pending_recipient_fields_'):
            self.assertIn(selector,REISSUE_CARD_CSS)
        self.assertIn('@container shipment-workbench (max-width: 900px)',REISSUE_CARD_CSS)
        self.assertIn('@container shipment-order (max-width: 760px)',REISSUE_CARD_CSS)
