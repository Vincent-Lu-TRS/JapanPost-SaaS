"""Synthetic metadata only: original labels never contact Drive in these tests."""
import sys
import re
import types
import unittest
import importlib.util
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

def _load_drive_module():
    """Read the real source without replacing shared bot.drive or import dependencies."""
    dependencies = {
        'streamlit': types.SimpleNamespace(secrets={}, session_state={}),
        'google': types.SimpleNamespace(),
        'google.oauth2': types.SimpleNamespace(),
        'google.oauth2.service_account': types.SimpleNamespace(Credentials=types.SimpleNamespace()),
        'googleapiclient': types.SimpleNamespace(),
        'googleapiclient.discovery': types.SimpleNamespace(build=lambda *a, **k: None),
        'googleapiclient.http': types.SimpleNamespace(MediaIoBaseUpload=object),
    }
    spec = importlib.util.spec_from_file_location('_original_pdf_drive_under_test', ROOT / 'bot' / 'drive.py')
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, dependencies):
        spec.loader.exec_module(module)
    return module


drive = _load_drive_module()

HISTORY = '1RZ0hxcRxwjNomTTfsznAsBmfumGOzdhe'
ORDER = 'demo123'
TRACK = 'EE123456789JP'
OTHER_TRACK = 'EE987654321JP'


def pdf(file_id='synthetic-file-01', name=None, folder=HISTORY, **changes):
    result = {'id': file_id, 'name': name or f'Item_{ORDER}_{TRACK}_Recipient.pdf',
              'mimeType': 'application/pdf', 'parents': [folder], 'trashed': False,
              'webViewLink': 'https://untrusted.example/ignore-this'}
    result.update(changes)
    return result


class Request:
    def __init__(self, response):
        self.response = response

    def execute(self):
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class SyntheticHttpError(Exception):
    def __init__(self, status):
        super().__init__('synthetic HTTP failure')
        self.resp = types.SimpleNamespace(status=status)


class FakeDrive:
    def __init__(self, pages=None, metadata=None):
        self.pages = [{'files': []}] if pages is None else pages
        self.metadata = metadata or {}
        self.get_calls = []
        self.list_calls = []

    def files(self):
        return self

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        return Request(self.metadata.get(kwargs['fileId'], PermissionError('synthetic denial')))

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        index = len(self.list_calls) - 1
        return Request(self.pages[min(index, len(self.pages) - 1)])


class SearchContractDrive(FakeDrive):
    """Evaluate documented name prefix and fullText phrase/underscore semantics."""
    def list(self, **kwargs):
        request = super().list(**kwargs)
        if isinstance(request.response, Exception):
            return request
        response = dict(request.response)
        files = response.get('files', [])
        for prefix in re.findall(r"name contains '([^']*)'", kwargs['q']):
            files = [item for item in files if str(item.get('name', '')).startswith(prefix)]
        for phrase in re.findall(r'''fullText contains '"([^"']*)"' ''', kwargs['q'] + ' '):
            # Test names use only the explicitly documented underscore word separator.
            words = phrase.replace('_', ' ').split()
            files = [item for item in files if (' ' + ' '.join(words) + ' ') in
                     (' ' + str(item.get('name', '')).replace('_', ' ').replace('.pdf', ' ') + ' ')]
        response['files'] = files
        return Request(response)


class OriginalPdfLookupTests(unittest.TestCase):
    def resolve(self, service, order=ORDER, tracking=(TRACK,), shipments=()):
        resolver = getattr(drive, 'resolve_original_label_urls', None)
        self.assertTrue(callable(resolver), 'original label resolver must be implemented')
        return resolver(order, tracking, shipments=shipments, service=service)

    def test_private_source_loader_preserves_other_tests_drive_fake(self):
        loader = globals().get('_load_drive_module')
        self.assertTrue(callable(loader), 'load real source privately, independently of shared bot.drive mocks')
        sentinel = types.SimpleNamespace(DRIVE_FOLDER_ID='synthetic-other-test-folder')
        with patch.dict(sys.modules, {'bot.drive': sentinel}):
            private_drive = loader()
            self.assertTrue(callable(private_drive.resolve_original_label_urls))
            self.assertIs(sys.modules['bot.drive'], sentinel)

    def test_new_and_legacy_names_resolve_in_history_or_current_output(self):
        for name in (f'Item_{ORDER}_{TRACK}_Recipient.pdf', f'Item {ORDER}_{TRACK}.pdf'):
            for folder in (HISTORY, drive.DRIVE_FOLDER_ID):
                with self.subTest(name=name, folder=folder):
                    service = FakeDrive([{'files': [pdf(name=name, folder=folder)]}])
                    self.assertEqual(self.resolve(service),
                                     {TRACK: 'https://drive.google.com/file/d/synthetic-file-01/view'})
                    query = service.list_calls[0]['q']
                    self.assertIn(HISTORY, query)
                    self.assertIn(drive.DRIVE_FOLDER_ID, query)
                    self.assertIn(ORDER, query)
                    self.assertIn('fullText contains', query)
                    self.assertNotIn('name contains', query)
                    self.assertIn('application/pdf', query)
                    self.assertTrue(service.list_calls[0]['supportsAllDrives'])
                    self.assertTrue(service.list_calls[0]['includeItemsFromAllDrives'])
                    self.assertNotIn('untrusted', str(self.resolve(FakeDrive([{'files': [pdf()]}]))))

    def test_similar_orders_and_tracking_are_rejected(self):
        names = [f'Item_x{ORDER}_{TRACK}.pdf', f'Item_{ORDER}4_{TRACK}.pdf',
                 f'Item_{ORDER}_{TRACK}4.pdf', f'Item_{ORDER}_X{TRACK}.pdf',
                 f'Item_{ORDER}_{OTHER_TRACK}.pdf', f'Item_{ORDER}_extra_{TRACK}.pdf']
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(self.resolve(FakeDrive([{'files': [pdf(name=name)]}])), {})

    def test_unsafe_metadata_is_rejected_even_if_query_was_filtered(self):
        variants = [{'parents': ['unrelated-folder']}, {'trashed': True},
                    {'trashed': None}, {'mimeType': 'application/octet-stream'},
                    {'mimeType': 'application/vnd.google-apps.shortcut'},
                    {'shortcutDetails': {'targetId': 'synthetic-file-02'}},
                    {'id': '../invalid'}, {'id': ''}, {'parents': []}]
        for changes in variants:
            with self.subTest(changes=changes):
                self.assertEqual(self.resolve(FakeDrive([{'files': [pdf(**changes)]}])), {})

    def test_multiple_distinct_matches_are_ambiguous_but_duplicate_id_is_not(self):
        self.assertEqual(self.resolve(FakeDrive([{'files': [pdf(), pdf('synthetic-file-02')]}])), {})
        self.assertIn(TRACK, self.resolve(FakeDrive([{'files': [pdf(), pdf()]}])))

    def test_valid_ledger_association_has_priority_without_listing_or_name_requirement(self):
        service = FakeDrive(metadata={'synthetic-ledger-01': pdf('synthetic-ledger-01', name='old-batch.pdf')})
        result = self.resolve(service, shipments=[{'tracking': TRACK, 'drive_file_id': 'synthetic-ledger-01'}])
        self.assertEqual(result, {TRACK: 'https://drive.google.com/file/d/synthetic-ledger-01/view'})
        self.assertEqual(service.list_calls, [])
        self.assertEqual(service.get_calls[0]['fileId'], 'synthetic-ledger-01')
        self.assertTrue(service.get_calls[0]['supportsAllDrives'])

    def test_other_tracking_ledger_id_is_not_read(self):
        service = FakeDrive([{'files': [pdf()]}])
        self.assertIn(TRACK, self.resolve(service, shipments=[{'tracking': OTHER_TRACK, 'drive_file_id': 'other-file'}]))
        self.assertEqual(service.get_calls, [])

    def test_confirmed_invalid_ledger_metadata_falls_back(self):
        variants = [pdf('synthetic-ledger-01', trashed=True),
                    pdf('synthetic-ledger-01', folder='unrelated-folder'),
                    pdf('different-returned-id')]
        for metadata in variants:
            with self.subTest(metadata=type(metadata).__name__):
                service = FakeDrive([{'files': [pdf()]}], {'synthetic-ledger-01': metadata})
                self.assertIn(TRACK, self.resolve(service, shipments=[{'tracking': TRACK, 'drive_file_id': 'synthetic-ledger-01'}]))
                self.assertTrue(service.list_calls)
        service = FakeDrive([{'files': [pdf()]}])
        self.assertIn(TRACK, self.resolve(service, shipments=[{'tracking': TRACK, 'drive_file_id': '../bad'}]))
        self.assertEqual(service.get_calls, [])

    def test_unavailable_ledger_metadata_hides_tracking_without_fallback(self):
        for exception in (PermissionError('synthetic denial'), RuntimeError('synthetic outage'),
                          SyntheticHttpError(403), SyntheticHttpError(404)):
            service = FakeDrive([{'files': [pdf()]}], {'synthetic-file-02': exception})
            result = self.resolve(service, shipments=[{'tracking': TRACK, 'drive_file_id': 'synthetic-file-02'}])
            self.assertEqual(result, {})
            self.assertEqual(service.list_calls, [])

    def test_partial_ledger_visibility_cannot_select_visible_file(self):
        service = FakeDrive(metadata={'synthetic-file-01': pdf(),
                                      'synthetic-file-02': PermissionError('synthetic denial')})
        result = self.resolve(service, shipments=[{'tracking': TRACK, 'drive_file_id': file_id}
                                                 for file_id in service.metadata])
        self.assertEqual(result, {})
        self.assertEqual(service.list_calls, [])

    def test_complete_identifier_grammar_rejects_punctuation_and_ambiguous_order(self):
        for name in (f'Item_other-{ORDER}_{TRACK}_Recipient.pdf',
                     f'Item_other.{ORDER}_{TRACK}_Recipient.pdf',
                     f'Item_{ORDER}_{TRACK}-extra_Recipient.pdf',
                     f'Item_other_{ORDER}_{TRACK}_Recipient.pdf',
                     f'Item_other_{ORDER}_{TRACK}.pdf'):
            with self.subTest(name=name):
                self.assertEqual(self.resolve(FakeDrive([{'files': [pdf(name=name)]}])), {})

    def test_legacy_complete_order_supports_punctuation_and_underscores(self):
        for order in ('demo_123', 'demo-123', 'demo.123'):
            with self.subTest(order=order):
                self.assertIn(TRACK, self.resolve(FakeDrive([{'files': [pdf(name=f'Item {order}_{TRACK}.pdf')]}]), order=order))

    def test_current_underscore_order_with_multiple_valid_parses_hides(self):
        self.assertEqual(self.resolve(FakeDrive([{'files': [pdf(name=f'Item_demo_123_{TRACK}_Recipient.pdf')]}]), order='demo_123'), {})

    def test_current_recipient_cannot_impersonate_legacy_order_and_tracking(self):
        name = f'Item_realorder_{OTHER_TRACK}_Recipient {ORDER}_{TRACK}.pdf'
        for service_type in (FakeDrive, SearchContractDrive):
            with self.subTest(service=service_type.__name__):
                self.assertEqual(self.resolve(service_type([{'files': [pdf(name=name)]}])), {})

    def test_generic_tracking_grammar_cannot_be_guessed_from_recipient_tokens(self):
        for order, tracking in (('demo123', 'TRACK1'), ('realorder', 'OTHER1')):
            name = 'Item_realorder_OTHER1_Recipient demo123_TRACK1.pdf'
            with self.subTest(order=order):
                self.assertEqual(self.resolve(FakeDrive([{'files': [pdf(name=name)]}]), order=order, tracking=(tracking,)), {})

    def test_other_valid_current_interpretation_in_recipient_is_ambiguous(self):
        name = f'Item_realorder_{OTHER_TRACK}_Recipient_{ORDER}_{TRACK}_Name.pdf'
        self.assertEqual(self.resolve(FakeDrive([{'files': [pdf(name=name)]}])), {})

    def test_search_contract_finds_embedded_fields_without_name_prefix(self):
        for name in (f'Item_{ORDER}_{TRACK}_Recipient.pdf', f'Item {ORDER}_{TRACK}.pdf'):
            with self.subTest(name=name):
                self.assertIn(TRACK, self.resolve(SearchContractDrive([{'files': [pdf(name=name)]}])))

    def test_multiple_valid_ledger_ids_are_ambiguous(self):
        service = FakeDrive(metadata={file_id: pdf(file_id) for file_id in ('synthetic-file-01', 'synthetic-file-02')})
        shipments = [{'tracking': TRACK, 'drive_file_id': file_id} for file_id in service.metadata]
        self.assertEqual(self.resolve(service, shipments=shipments), {})
        self.assertEqual(service.list_calls, [])

    def test_complete_pagination_checks_later_ambiguity(self):
        service = FakeDrive([{'files': [pdf()], 'nextPageToken': 'page-two'},
                             {'files': [pdf('synthetic-file-02')]}])
        self.assertEqual(self.resolve(service), {})
        self.assertEqual(service.list_calls[1]['pageToken'], 'page-two')

    def test_incomplete_search_permission_failure_and_page_cap_fail_closed(self):
        cases = [[{'files': [pdf()], 'incompleteSearch': True}],
                 [{'files': [pdf()], 'nextPageToken': 'two'}, PermissionError('synthetic denial')],
                 [PermissionError('synthetic denial')],
                 [{'files': [pdf()], 'nextPageToken': 'never-finished'}]]
        for pages in cases:
            with self.subTest(case=len(pages)):
                service = FakeDrive(pages)
                self.assertEqual(self.resolve(service), {})
                self.assertLessEqual(len(service.list_calls), 5)

    def test_invalid_inputs_make_no_service_calls(self):
        for order, tracking in [('', (TRACK,)), ('other/order', (TRACK,)), ("x' or trashed=true", (TRACK,)),
                                ('x' * 81, (TRACK,)), (ORDER, ()), (ORDER, ('../bad',))]:
            service = FakeDrive()
            with self.subTest(order=order):
                self.assertEqual(self.resolve(service, order, tracking), {})
                self.assertEqual(service.list_calls + service.get_calls, [])

    def test_requested_tracking_results_are_separate_and_only_requested_ids_return(self):
        second = pdf('synthetic-file-02', name=f'Item {ORDER}_{OTHER_TRACK}.pdf')
        service = FakeDrive([{'files': [pdf(), second]}])
        result = self.resolve(service, tracking=(TRACK, OTHER_TRACK))
        self.assertEqual(set(result), {TRACK, OTHER_TRACK})
        self.assertLessEqual(len(service.list_calls), 2)

    def test_many_tracking_share_one_complete_metadata_query(self):
        trackings = [f'EE{number:09d}JP' for number in range(50)]
        files = [pdf(f'synthetic-file-{number}', name=f'Item {ORDER}_{tracking}.pdf')
                 for number, tracking in enumerate(trackings)]
        service = SearchContractDrive([{'files': files}])
        self.assertEqual(set(self.resolve(service, tracking=trackings)), set(trackings))
        self.assertEqual(len(service.list_calls), 1)

    def test_ledger_gets_are_deduplicated_and_globally_budgeted(self):
        trackings = [f'EE{number:09d}JP' for number in range(50)]
        shipments = [{'tracking': tracking, 'drive_file_id': 'shared-synthetic-file'} for tracking in trackings]
        service = FakeDrive(metadata={'shared-synthetic-file': pdf('shared-synthetic-file')})
        self.assertEqual(set(self.resolve(service, tracking=trackings, shipments=shipments)), set(trackings))
        self.assertEqual(len(service.get_calls), 1)
        self.assertEqual(service.list_calls, [])
        shipments = [{'tracking': tracking, 'drive_file_id': f'synthetic-file-{number}'}
                     for number, tracking in enumerate(trackings)]
        service = FakeDrive(metadata={shipment['drive_file_id']: pdf(shipment['drive_file_id']) for shipment in shipments})
        self.resolve(service, tracking=trackings, shipments=shipments)
        self.assertLessEqual(len(service.get_calls) + len(service.list_calls), 25)

    def test_mixed_ledger_and_fallback_share_global_twenty_five_call_budget(self):
        trackings = [f'EE{number:09d}JP' for number in range(50)]
        shipments = [{'tracking': tracking, 'drive_file_id': f'synthetic-file-{number}'}
                     for number, tracking in enumerate(trackings[:20])]
        files = [pdf(f'synthetic-file-{number}', name=f'Item {ORDER}_{tracking}.pdf')
                 for number, tracking in enumerate(trackings)]
        pages = [{'files': files, 'nextPageToken': f'page-{number}'} for number in range(4)] + [{'files': files}]
        service = SearchContractDrive(pages, {item['id']: item for item in files[:20]})
        result = self.resolve(service, tracking=trackings, shipments=shipments)
        self.assertEqual(set(result), set(trackings))
        self.assertEqual(len(service.get_calls), 20)
        self.assertEqual(len(service.list_calls), 5)

    def test_default_service_factory_failure_is_hidden(self):
        with patch.object(drive, '_get_drive_service', side_effect=PermissionError('synthetic secret')):
            resolver = getattr(drive, 'resolve_original_label_urls', None)
            self.assertTrue(callable(resolver), 'original label resolver must be implemented')
            self.assertEqual(resolver(ORDER, [TRACK]), {})


if __name__ == '__main__':
    unittest.main()
