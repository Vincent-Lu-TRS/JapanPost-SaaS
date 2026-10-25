import copy
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import features.shipment_workbench as workbench

class ReissueCommitFenceTests(unittest.TestCase):
    def setUp(self):
        self.actor='staff@tkrjm.co.jp'; self.nonce='query-a'; self.token='click-a'
        self.intent={'actor':self.actor,'nonce':self.nonce,'token':self.token,'created_at':100.0,'stage':'waiting'}
        self.state={'reissue_query_actor':self.actor,'reissue_nonce':self.nonce,'reissue_intent':copy.deepcopy(self.intent)}
        self.ctx=SimpleNamespace(current_fragment_id='fence-a',fragment_ids_this_run=['fence-a'])
    def accept(self,*,ctx=None,actor=None,nonce=None,token=None,now=100.25):
        with patch.object(workbench,'st',SimpleNamespace(session_state=self.state)),patch.object(workbench,'verified_email',return_value=self.actor),patch.object(workbench.time,'monotonic',return_value=now):
            return workbench._accept_reissue_commit_ack(actor or self.actor,nonce or self.nonce,token or self.token,context=self.ctx if ctx is None else ctx)
    def test_full_app_runs_never_acknowledge_even_when_repeated(self):
        self.assertTrue(hasattr(workbench,'_accept_reissue_commit_ack'),'browser fence is required')
        for _ in range(3):
            self.assertFalse(self.accept(ctx=SimpleNamespace(current_fragment_id='fence-a',fragment_ids_this_run=None)))
        self.assertEqual(self.state['reissue_intent']['stage'],'waiting')
    def test_true_browser_fragment_ack_is_bound_and_one_way(self):
        self.assertTrue(self.accept())
        self.assertEqual(self.state['reissue_intent']['stage'],'ready')
        self.assertFalse(self.accept())
    def test_stale_nonce_token_or_login_cannot_acknowledge_new_draft(self):
        for changes in ({'nonce':'old-query'},{'token':'old-click'},{'actor':'other@tkrjm.co.jp'}):
            with self.subTest(changes=changes):self.assertFalse(self.accept(**changes))
        self.actor='other@tkrjm.co.jp'
        self.assertFalse(self.accept())
        self.assertEqual(self.state['reissue_intent']['stage'],'waiting')
    def test_wrong_fragment_and_timeout_fail_closed(self):
        self.assertFalse(self.accept(ctx=SimpleNamespace(current_fragment_id='other',fragment_ids_this_run=['fence-a'])))
        self.assertFalse(self.accept(now=111))
        self.assertEqual(self.state['reissue_intent']['stage'],'failed')
        self.assertIn('重新查詢',self.state['reissue_submission_error'])
    def test_missing_context_or_intent_is_never_ready(self):
        self.assertFalse(self.accept(ctx=SimpleNamespace()))
        self.state.pop('reissue_intent')
        self.assertFalse(self.accept())
