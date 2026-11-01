"""Plugin handler attribution: one invocation seam, and the read-only parser inventory.

Two concerns are pinned here, both DB-free:

*handler failures are attributed, not leaked*
    ``datamind.plugin.invocation.invoke_plugin`` is the single call site a plugin handler is
    invoked from. A deliberate ``HTTPException`` passes through unchanged; a missing parser
    library and a broken handler both become ``PARSE_ADAPTER_UNSUPPORTED`` naming the plugin
    (G12), while the log record - never the HTTP body - carries the reason. The upload path's
    entry point (``upload_pipeline._plugin_parser``) and the two places an upload record names
    its plugin (``_analysis_of``/``_analysis`` and ``_record_wave_timeout``) are covered too.

*the inventory is a declaration, not a configuration dump*
    ``parser_inventory()`` lists every registered manifest - disabled ones included - with
    exactly eight fields and no config values, and never resolves a loader (G7).
"""
from __future__ import annotations

import sys
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException

from datamind import parse_contract
from datamind.plugin.invocation import invoke_plugin
from datamind.plugin.manifest import ParserPluginManifest
from ingestion import upload_pipeline
from ingestion.plugins import default_context, parser_inventory

UNSUPPORTED = 'PARSE_ADAPTER_UNSUPPORTED'
INVENTORY_KEYS = {'name', 'version', 'media_types', 'extensions', 'capabilities',
                  'enabled', 'loaded_from', 'registered_in_contract'}
BUILTIN_NAMES = {'pdf-native', 'ohr-bench-json', 'docx-native', 'pptx-native', 'xlsx-native',
                 'dxf-native', 'measurement-json'}

try:  # The app needs the optional SQL stack; its absence must not fail this suite.
    from datamind import api as api_module
except ImportError as api_import_error:  # pragma: no cover - depends on the environment
    api_module = None
    API_IMPORT_ERROR = api_import_error
else:  # pragma: no cover
    API_IMPORT_ERROR = None


def _manifest(name='probe', loader=None, media_types=('application/x-probe',)):
    """A minimal manifest whose loader is a callable, not an import reference."""
    return ParserPluginManifest(name, '1.0.0', media_types, (),
                                capabilities=('text',),
                                loader=loader or (lambda *args, **kwargs: None))


def _raising(error):
    def loader(*args, **kwargs):
        raise error
    return loader


def _inventory_item(name):
    for item in parser_inventory():
        if item['name'] == name:
            return item
    raise AssertionError(f'{name!r} is missing from the inventory')


class InvocationResultTests(unittest.TestCase):
    def test_the_loader_value_is_returned_unchanged(self):
        sentinel = object()

        self.assertIs(invoke_plugin(_manifest(), lambda *a, **k: sentinel, Path('x.pdf')), sentinel)

    def test_the_arguments_reach_the_loader(self):
        loader = mock.Mock(return_value='ok')

        invoke_plugin(_manifest(), loader, Path('x.pdf'), 'doc-1', 'sha', {'k': 'v'})

        loader.assert_called_once_with(Path('x.pdf'), 'doc-1', 'sha', {'k': 'v'})


class InvocationFailureTests(unittest.TestCase):
    def test_an_http_exception_passes_through_unchanged(self):
        error = HTTPException(415, detail={'error_code': 'UNSUPPORTED_MEDIA_TYPE'})

        with self.assertRaises(HTTPException) as caught:
            invoke_plugin(_manifest(), _raising(error))

        self.assertIs(caught.exception, error)

    def test_a_missing_library_is_reported_as_library_missing(self):
        for error in (ImportError('no pymupdf'), ModuleNotFoundError('No module named pymupdf')):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(HTTPException) as caught:
                    invoke_plugin(_manifest('pdf-native'), _raising(error))

                self.assertEqual(caught.exception.status_code, 422)
                self.assertEqual(caught.exception.detail, {
                    'error_code': UNSUPPORTED,
                    'details': {'subject': 'plugin', 'reason': 'library_missing',
                                'plugin': 'pdf-native', 'adapter': 'pdf-native'}})
                self.assertIs(caught.exception.__cause__, error)

    def test_any_other_failure_is_reported_as_handler_failed(self):
        error = ValueError('bad record')

        with self.assertRaises(HTTPException) as caught:
            invoke_plugin(_manifest('dxf-native'), _raising(error))

        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(caught.exception.detail, {
            'error_code': UNSUPPORTED,
            'details': {'subject': 'plugin', 'reason': 'handler_failed',
                        'plugin': 'dxf-native', 'adapter': 'dxf-native'}})
        self.assertIs(caught.exception.__cause__, error)

    def test_the_body_never_carries_the_exception_text(self):
        with self.assertRaises(HTTPException) as caught:
            invoke_plugin(_manifest(), _raising(RuntimeError('dsn=postgres://secret')))

        self.assertNotIn('secret', repr(caught.exception.detail))


class InvocationLoggingTests(unittest.TestCase):
    def test_a_success_logs_one_structured_record(self):
        with self.assertLogs('datamind.plugin', level='INFO') as captured:
            invoke_plugin(_manifest('pdf-native', media_types=('application/pdf',)),
                          lambda *a, **k: None)

        record = captured.records[-1]
        self.assertEqual(record.plugin, 'pdf-native')
        self.assertEqual(record.media_type, 'application/pdf')
        self.assertEqual(record.reason, 'ok')
        self.assertIsInstance(record.duration_ms, float)
        self.assertIsNone(record.exc_info)

    def test_a_failure_logs_a_warning_with_the_reason_and_the_traceback(self):
        with self.assertLogs('datamind.plugin', level='WARNING') as captured:
            with self.assertRaises(HTTPException):
                invoke_plugin(_manifest('probe'), _raising(ImportError('no ezdxf')))

        record = captured.records[-1]
        self.assertEqual(record.plugin, 'probe')
        self.assertEqual(record.media_type, 'application/x-probe')
        self.assertEqual(record.reason, 'library_missing')
        self.assertIsInstance(record.duration_ms, float)
        self.assertIsNotNone(record.exc_info)


class PluginParserEntryTests(unittest.TestCase):
    def test_a_failing_loader_becomes_the_mapped_422(self):
        parse = upload_pipeline._plugin_parser(_manifest('probe', loader=_raising(
            ImportError('no library'))))

        with self.assertRaises(HTTPException) as caught:
            parse(Path('x.csv'), {'document_id': 'd', 'source_sha256': 's'})

        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(caught.exception.detail['error_code'], UNSUPPORTED)
        self.assertEqual(caught.exception.detail['details']['reason'], 'library_missing')

    def test_a_successful_loader_receives_the_claim_and_its_value_is_returned(self):
        sentinel = ('result', {'pages': 2})
        loader = mock.Mock(return_value=sentinel)
        parse = upload_pipeline._plugin_parser(_manifest('probe', loader=loader))
        path, claim = Path('x.csv'), {'document_id': 'd', 'source_sha256': 's'}

        self.assertIs(parse(path, claim), sentinel)

        loader.assert_called_once_with(path, 'd', 's', claim)


PAYLOAD = {'parse_status': 'parsed', 'contract_version': 1, 'blocks': [],
           'unsupported_features': [], 'quality_findings': [],
           'manifest': {'adapter': 'pdf-native'}}
STORED = {'blocks': 0, 'locators': 0, 'relations': 0}


class AnalysisAttributionTests(unittest.TestCase):
    def test_the_stored_analysis_names_the_plugin_and_keeps_the_adapter(self):
        analysis = upload_pipeline._analysis_of({'suffix': '.pdf'}, PAYLOAD, STORED, {})

        self.assertEqual(analysis['plugin'], 'pdf-native')
        self.assertEqual(analysis['adapter'], 'pdf-native')

    def test_the_plugin_is_the_suffix_owner_even_when_it_is_not_the_contract_adapter(self):
        payload = dict(PAYLOAD, manifest={'adapter': 'measurement-json'})

        analysis = upload_pipeline._analysis_of({'suffix': '.json'}, payload, STORED, {})

        self.assertEqual(analysis['plugin'], 'measurement-json')
        self.assertEqual(analysis['adapter'], 'measurement-json')

    def test_the_placeholder_analysis_has_the_same_shape(self):
        placeholder = upload_pipeline._analysis({'status': 'queued'},
                                                {'file_type': 'pdf', 'quality_detail': {}})

        self.assertEqual(placeholder['plugin'], 'pdf-native')
        self.assertEqual(placeholder['adapter'], 'pdf-native')


class WaveTimeoutAttributionTests(unittest.TestCase):
    def test_the_timeout_counter_names_the_owning_plugin(self):
        conn, cursor = mock.MagicMock(), mock.MagicMock()
        conn.__enter__.return_value = conn
        conn.cursor.return_value.__enter__.return_value = cursor
        cursor.fetchone.return_value = {'quality_detail': {}}
        claim = {'job_id': 'job-1', 'document_id': 'doc-1', 'claim_token': 'token',
                 'suffix': '.dxf'}

        with mock.patch.object(upload_pipeline.psycopg2, 'connect', return_value=conn), \
                mock.patch.object(upload_pipeline, '_lock_live_claim', return_value=True):
            self.assertEqual(upload_pipeline._record_wave_timeout('dsn', claim), 1)

        statement, params = cursor.execute.call_args_list[-1][0]
        self.assertIn('UPDATE document_assets', statement)
        self.assertEqual(params[1], 'doc-1')
        upload = params[0].adapted['upload']
        self.assertEqual(upload['wave_timeouts'], 1)
        self.assertEqual(upload['plugin'], 'dxf-native')

    def test_a_lost_claim_writes_nothing(self):
        cursor = mock.MagicMock()

        with mock.patch.object(upload_pipeline.psycopg2, 'connect'), \
                mock.patch.object(upload_pipeline, '_lock_live_claim', return_value=False):
            self.assertIsNone(upload_pipeline._record_wave_timeout(
                'dsn', {'job_id': 'job-1', 'document_id': 'doc-1', 'claim_token': 't',
                        'suffix': '.dxf'}))


class InventoryShapeTests(unittest.TestCase):
    def test_every_item_has_exactly_the_eight_declaration_fields(self):
        inventory = parser_inventory()

        self.assertTrue(inventory)
        for item in inventory:
            with self.subTest(plugin=item.get('name')):
                self.assertEqual(set(item), INVENTORY_KEYS)
                self.assertNotIn('config', item)
                self.assertIsInstance(item['media_types'], list)
                self.assertIsInstance(item['extensions'], list)
                self.assertIsInstance(item['capabilities'], list)
                self.assertIsInstance(item['enabled'], bool)
                self.assertIsInstance(item['registered_in_contract'], bool)

    def test_the_seven_builtins_are_listed_with_their_provenance(self):
        inventory = parser_inventory()

        self.assertLessEqual(BUILTIN_NAMES, {item['name'] for item in inventory})
        for name in BUILTIN_NAMES:
            with self.subTest(plugin=name):
                self.assertEqual(_inventory_item(name)['loaded_from'], 'builtin')

    def test_a_disabled_plugin_stays_listed(self):
        manifest = next(item for item in default_context().parser.list()
                        if item.name == 'dxf-native')
        try:
            manifest.enabled = False
            item = _inventory_item('dxf-native')
        finally:
            manifest.enabled = True

        self.assertIs(item['enabled'], False)
        self.assertEqual(item['extensions'], ['.dxf'])
        self.assertIs(_inventory_item('dxf-native')['enabled'], True)

    def test_contract_registration_is_reported(self):
        saved = dict(parse_contract._ADAPTERS)
        parse_contract._ADAPTERS.clear()
        try:
            self.assertFalse(_inventory_item('pdf-native')['registered_in_contract'])
            parse_contract.register_adapter(
                'pdf-native', media_types=('application/x-inventory-probe',),
                capabilities=('text',))
            try:
                self.assertTrue(_inventory_item('pdf-native')['registered_in_contract'])
            finally:
                parse_contract._unregister_adapter('pdf-native')
            self.assertFalse(_inventory_item('pdf-native')['registered_in_contract'])
        finally:
            parse_contract._ADAPTERS.clear()
            parse_contract._ADAPTERS.update(saved)


@unittest.skipIf(api_module is None, f'datamind.api is not importable here: {API_IMPORT_ERROR}')
class ParsersEndpointTests(unittest.TestCase):
    """The endpoint is called directly: its route needs no database, only the app module."""

    def _route(self):
        routes = [route for route in api_module.app.routes
                  if getattr(route, 'path', None) == '/v1/parsers']
        self.assertEqual(len(routes), 1)
        return routes[0]

    def test_the_route_exists_as_a_get(self):
        self.assertIn('GET', self._route().methods)

    def test_the_listing_is_process_local_and_exposes_no_config(self):
        payload = self._route().endpoint()

        self.assertIs(payload['process_local'], True)
        self.assertIsInstance(payload['parsers'], list)
        first = payload['parsers'][0]
        self.assertEqual(set(first), INVENTORY_KEYS)
        # No config value, and no field beyond the eight declarations, anywhere in the body.
        self.assertNotIn('config', json.dumps(payload))

    def test_the_listing_is_no_store_and_admin_only(self):
        self.assertTrue(api_module._no_store_path('/v1/parsers'))
        request = SimpleNamespace(url=SimpleNamespace(path='/v1/parsers'), method='GET')
        self.assertEqual(api_module._required_permission(request), 'admin')


if __name__ == '__main__':
    unittest.main()
