"""Upload admission routing: the suffix gate the parser registry answers.

The upload endpoint is the only place a file extension decides anything: ``target`` asks the
registry whether an enabled plugin claims the suffix, and ``PARSERS`` is filled from the
same registry. What the bytes really are is still signature sniffing (G3), so a file named
``.pdf`` that holds DXF text stays a DXF - the routing hint and the detected identity are
two different answers to two different questions.

The measurement-JSON plugin is pinned here too: it replaced the hand-written tables, and
its payload must stay byte-identical apart from the manifest names that are now read from
the declaration instead of a literal.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException

from datamind import cad_dimensions, parse_contract
from ingestion import upload_pipeline
from ingestion.plugins import default_context, measurement_json_plugin

MEASUREMENT_BODY = {
    'source_id': 'dimension-fixture-v1',
    'record_version': 1,
    'provenance': 'synthetic_demo',
    'measurements': [{
        'measurement_id': 'dimension-demo-1',
        'part_id': 'A200',
        'metric': 'diameter',
        'value': '20.04',
        'unit': 'mm',
    }],
}
#: A DXF text signature: the sniff sees `0`/`SECTION`, the name says `sample.pdf`.
DXF_TEXT = b'0\nSECTION\n2\nHEADER\n0\nENDSEC\n0\nEOF\n'


def _manifest(extension):
    return default_context().parser.lookup_by_extension(extension)


class TargetAdmissionTests(unittest.TestCase):
    def test_every_declared_extension_is_accepted(self):
        for filename in ('plan.pdf', 'brief.docx', 'deck.pptx', 'sheet.xlsx',
                         'drawing.dxf', 'measurement.json'):
            with self.subTest(filename=filename):
                self.assertEqual(upload_pipeline.target(filename), Path(filename).suffix)

    def test_an_unknown_extension_is_refused_with_the_unsupported_media_type_code(self):
        with self.assertRaises(HTTPException) as caught:
            upload_pipeline.target('payload.exe')

        self.assertEqual(caught.exception.status_code, 415)
        self.assertEqual(caught.exception.detail,
                         {'error_code': 'UNSUPPORTED_MEDIA_TYPE',
                          'details': {'suffix': '.exe'}})

    def test_a_name_without_a_suffix_is_refused_without_inventing_one(self):
        with self.assertRaises(HTTPException) as caught:
            upload_pipeline.target('README')

        self.assertEqual(caught.exception.status_code, 415)
        self.assertEqual(caught.exception.detail,
                         {'error_code': 'UNSUPPORTED_MEDIA_TYPE',
                          'details': {'suffix': None}})

    def test_dwg_keeps_its_own_refusal(self):
        with self.assertRaises(HTTPException) as caught:
            upload_pipeline.target('plan.dwg')

        self.assertEqual(caught.exception.status_code, 415)
        self.assertEqual(caught.exception.detail,
                         {'error_code': upload_pipeline.DWG_ERROR_CODE})


class DisabledPluginTests(unittest.TestCase):
    def test_a_disabled_plugin_is_refused_and_names_itself(self):
        manifest = _manifest('.dxf')
        try:
            manifest.enabled = False
            with self.assertRaises(HTTPException) as caught:
                upload_pipeline.target('plan.dxf')
        finally:
            manifest.enabled = True

        self.assertEqual(caught.exception.status_code, 415)
        self.assertEqual(caught.exception.detail,
                         {'error_code': 'UNSUPPORTED_MEDIA_TYPE',
                          'details': {'suffix': '.dxf', 'reason': 'plugin_disabled',
                                      'plugin': manifest.name}})
        # The restore above is what makes the route answerable again.
        self.assertEqual(upload_pipeline.target('plan.dxf'), '.dxf')

    def test_the_refusal_never_leaks_the_registry_code(self):
        manifest = _manifest('.json')
        try:
            manifest.enabled = False
            with self.assertRaises(HTTPException) as caught:
                upload_pipeline.target('measurement.json')
        finally:
            manifest.enabled = True

        self.assertNotIn('PARSE_ADAPTER', json.dumps(caught.exception.detail))


class MagicStillDecidesTests(unittest.TestCase):
    def test_a_pdf_named_file_holding_dxf_bytes_is_still_detected_as_dxf(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'sample.pdf'
            path.write_bytes(DXF_TEXT)

            self.assertEqual(parse_contract.detect_media_type(path),
                             parse_contract.DXF_MEDIA_TYPE)

        # Admission answered from the name alone; detection never consults it.
        self.assertEqual(upload_pipeline.target('sample.pdf'), '.pdf')


class ParserTableTests(unittest.TestCase):
    def test_the_table_stays_a_plain_patchable_dict(self):
        self.assertIs(type(upload_pipeline.PARSERS), dict)

        def marker(path, claim):
            return 'patched', {}

        with mock.patch.dict(upload_pipeline.PARSERS, {'.json': marker}):
            self.assertIs(upload_pipeline.PARSERS['.json'], marker)
            upload_pipeline._sync_parsers()
            self.assertIs(upload_pipeline.PARSERS['.json'], marker)

        self.assertIsNot(upload_pipeline.PARSERS['.json'], marker)

    def test_a_sync_fills_an_extension_the_registry_declares(self):
        real = upload_pipeline.PARSERS.pop('.xlsx')
        try:
            upload_pipeline._sync_parsers()
            self.assertTrue(callable(upload_pipeline.PARSERS['.xlsx']))
        finally:
            upload_pipeline.PARSERS['.xlsx'] = real

    def test_the_table_covers_every_enabled_extension(self):
        self.assertEqual(set(upload_pipeline.PARSERS),
                         set(default_context().parser.known_extensions(include_disabled=False)))


class DisplayNameTests(unittest.TestCase):
    def test_the_display_names_come_from_the_manifests(self):
        for suffix in ('.pdf', '.docx', '.pptx', '.xlsx', '.dxf', '.json'):
            manifest = _manifest(suffix)
            with self.subTest(suffix=suffix):
                self.assertEqual(upload_pipeline._adapter_for(suffix), manifest.name)
                self.assertEqual(upload_pipeline._media_type_for(suffix),
                                 manifest.media_types[0])

    def test_json_keeps_the_measurement_display_names(self):
        self.assertEqual(upload_pipeline._adapter_for('.json'), 'measurement-json')
        self.assertEqual(upload_pipeline._media_type_for('.json'), 'application/json')

    def test_an_unknown_suffix_has_no_display_names(self):
        self.assertIsNone(upload_pipeline._adapter_for('.exe'))
        self.assertIsNone(upload_pipeline._media_type_for('.exe'))


class MeasurementJsonPluginTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.path = Path(self.root.name) / 'demo-diameter.json'
        self.path.write_text(json.dumps(MEASUREMENT_BODY), encoding='utf-8')
        self.claim = {'document_id': 'doc-1', 'source_sha256': 'a' * 64,
                      'filename': 'demo-diameter.json', 'provenance': 'synthetic_demo'}
        self.record = {'source': {'record_version': 1},
                       'measurements': [{'measurement_id': 'dimension-demo-1'}]}

    def test_a_valid_record_keeps_the_payload_shape_and_names(self):
        with mock.patch.object(cad_dimensions, 'measurement_from_bytes',
                               return_value=self.record) as parse:
            result, extra = measurement_json_plugin.parse(self.path, 'doc-1', 'a' * 64,
                                                         self.claim)

        parse.assert_called_once_with(self.path.read_bytes(), 'dimension-demo-1', 1,
                                      'synthetic_demo')
        payload = result.payload
        self.assertEqual(set(payload),
                         set(parse_contract.RESULT_REQUIRED_FIELDS) | {'contract_version'})
        self.assertEqual(payload['classification'],
                         {'detected_media_type': 'application/json',
                          'source_name': 'demo-diameter.json'})
        self.assertEqual(payload['manifest'],
                         {'parser_version': 'cad-measurement-adapter-v1',
                          'adapter': 'measurement-json',
                          'measurement_id': 'dimension-demo-1', 'record_version': 1})
        self.assertEqual(payload['adapter_capabilities'], ['measurement_json'])
        self.assertEqual(payload['blocks'], [])
        self.assertEqual(payload['failed_scope'],
                         [{'kind': 'object', 'ref': 'doc-1', 'status': 'unsupported',
                           'detail': 'locator_unavailable'}])
        self.assertEqual(payload['unsupported_features'], ['locator_unavailable'])
        self.assertEqual(payload['parse_status'], 'unsupported')
        self.assertEqual(extra, {'measurement_id': 'dimension-demo-1',
                                 'record_version': 1, 'measurements': 1})

    def test_without_options_the_source_name_is_the_file_name(self):
        with mock.patch.object(cad_dimensions, 'measurement_from_bytes',
                               return_value=self.record) as parse:
            result, _ = measurement_json_plugin.parse(self.path, 'doc-1', 'a' * 64)

        parse.assert_called_once_with(self.path.read_bytes(), 'dimension-demo-1', 1, None)
        self.assertEqual(result.payload['classification']['source_name'],
                         'demo-diameter.json')

    def test_an_invalid_identity_raises_the_upload_error_code(self):
        invalid = {
            'no_measurements': {'source_id': 'x', 'record_version': 1,
                                'provenance': 'synthetic_demo', 'measurements': []},
            'missing_version': {'source_id': 'x', 'provenance': 'synthetic_demo',
                                'measurements': [{'measurement_id': 'dimension-demo-1'}]},
            'boolean_version': {'source_id': 'x', 'record_version': True,
                                'provenance': 'synthetic_demo',
                                'measurements': [{'measurement_id': 'dimension-demo-1'}]},
            'not_json': None,
        }
        for name, body in invalid.items():
            with self.subTest(case=name):
                self.path.write_text('{' if body is None else json.dumps(body), encoding='utf-8')
                with mock.patch.object(cad_dimensions, 'measurement_from_bytes') as parse:
                    with self.assertRaises(HTTPException) as caught:
                        measurement_json_plugin.parse(self.path, 'doc-1', 'a' * 64,
                                                      self.claim)
                    parse.assert_not_called()

                self.assertEqual(caught.exception.status_code, 422)
                self.assertEqual(caught.exception.detail,
                                 {'error_code': 'UPLOAD_MEASUREMENT_SOURCE_INVALID'})


if __name__ == '__main__':
    unittest.main()
