"""Built-in Office and DXF parser plugins: declarations, routing and upload delegation.

The declarations must match what ``ingestion.office`` and ``ingestion.cad_adapter``
already register, because Task 5 derives upload routing from the registry and a drift
between the two would silently route a format to the wrong parser. The upload delegators
are pinned through the derived ``PARSERS`` table, and the DXF ``provenance`` fallback is
exercised through a mocked adapter, not a real drawing.
"""
from __future__ import annotations

import ast
import inspect
import sys
import textwrap
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException

from datamind import parse_contract as contract
from datamind.plugin.manifest import PARSE_ADAPTER_UNKNOWN, ManifestError
from ingestion import cad_adapter, office, upload_pipeline
from ingestion.plugins import builtin_manifests, default_context, dxf_plugin, office_plugin

#: The exact declaration the plan fixes for each format; the media types are pinned to
#: ``ingestion.office.ADAPTERS`` below as well, so a literal-only drift is caught too.
EXPECTED = {
    'docx-native': ('application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                    ('.docx',), ('text', 'headings', 'tables')),
    'pptx-native': ('application/vnd.openxmlformats-officedocument.presentationml.presentation',
                    ('.pptx',), ('text', 'slides')),
    'xlsx-native': ('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    ('.xlsx',), ('text', 'sheet_rows', 'cached_values')),
}
DXF_CAPABILITIES = ('entities', 'text', 'layers', 'dimensions', 'blocks', 'arcs',
                    'polylines', 'mtext', 'native_dimensions')


def _declared_office_capabilities():
    """The ``capabilities`` mapping ``register_office_adapters`` writes in its own source."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(office.register_office_adapters)))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(getattr(target, 'id', None) == 'capabilities'
                                                for target in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError('register_office_adapters declares no capabilities mapping')


class OfficeDeclarationTests(unittest.TestCase):
    def test_each_manifest_declares_its_media_type_extensions_and_capabilities(self):
        for manifest in office_plugin.MANIFESTS:
            media_type, extensions, capabilities = EXPECTED[manifest.name]
            self.assertEqual(manifest.media_types, (media_type,))
            self.assertEqual(manifest.extensions, extensions)
            self.assertEqual(manifest.capabilities, capabilities)
            self.assertEqual(manifest.loader, 'ingestion.plugins.office_plugin:parse')

    def test_the_declared_media_types_are_the_adapters_office_registers(self):
        self.assertEqual({name: (media_type,) for name, (media_type, _, _) in EXPECTED.items()},
                         {name: (media_type,) for name, media_type in office.ADAPTERS.items()})

    def test_the_capabilities_are_the_ones_register_office_adapters_declares(self):
        declared = _declared_office_capabilities()

        self.assertEqual({manifest.name: list(manifest.capabilities)
                          for manifest in office_plugin.MANIFESTS}, declared)

    def test_all_three_formats_share_one_loader(self):
        loaders = {manifest.loader for manifest in office_plugin.MANIFESTS}

        self.assertEqual(loaders, {'ingestion.plugins.office_plugin:parse'})
        for manifest in office_plugin.MANIFESTS:
            self.assertIs(manifest.resolve(), office_plugin.parse)


class DxfDeclarationTests(unittest.TestCase):
    def test_the_manifest_declares_the_dxf_media_type_and_extension(self):
        self.assertEqual(dxf_plugin.MANIFEST.name, 'dxf-native')
        self.assertEqual(dxf_plugin.MANIFEST.media_types, (contract.DXF_MEDIA_TYPE,))
        self.assertEqual(dxf_plugin.MANIFEST.extensions, ('.dxf',))
        self.assertEqual(dxf_plugin.MANIFEST.loader, 'ingestion.plugins.dxf_plugin:parse')

    def test_the_capabilities_are_the_adapter_v2_set(self):
        self.assertEqual(tuple(cad_adapter._CAPABILITIES['v2']), DXF_CAPABILITIES)
        self.assertEqual(dxf_plugin.MANIFEST.capabilities, DXF_CAPABILITIES)

    def test_no_manifest_claims_dwg(self):
        for manifest in builtin_manifests():
            for media_type in manifest.media_types:
                self.assertNotIn('dwg', media_type)
            for extension in manifest.extensions:
                self.assertNotEqual(extension, '.dwg')


class ExtensionRoutingTests(unittest.TestCase):
    def test_every_office_and_dxf_extension_routes_to_its_plugin(self):
        parser = default_context().parser

        for extension, name in (('.docx', 'docx-native'), ('.pptx', 'pptx-native'),
                                ('.xlsx', 'xlsx-native'), ('.dxf', 'dxf-native')):
            self.assertEqual(parser.lookup_by_extension(extension).name, name)
            self.assertEqual(parser.lookup_by_extension(extension.lstrip('.')).name, name)

    def test_the_office_and_dxf_media_types_route_to_their_plugins(self):
        parser = default_context().parser

        for name, media_type in office.ADAPTERS.items():
            self.assertEqual(parser.lookup(media_type).name, name)
        self.assertEqual(parser.lookup(contract.DXF_MEDIA_TYPE).name, 'dxf-native')

    def test_dwg_has_no_route(self):
        parser = default_context().parser

        for lookup, value in ((parser.lookup, contract.DWG_MEDIA_TYPE),
                              (parser.lookup_by_extension, '.dwg')):
            with self.assertRaises(ManifestError) as caught:
                lookup(value)
            self.assertEqual(caught.exception.error_code, PARSE_ADAPTER_UNKNOWN)


class UploadDelegatorTests(unittest.TestCase):
    def test_every_office_and_dxf_extension_has_a_callable_entry(self):
        for extension in ('.docx', '.pptx', '.xlsx', '.dxf'):
            self.assertTrue(callable(upload_pipeline.PARSERS[extension]))

    def test_the_office_entries_delegate_the_whole_claim(self):
        sentinel = ('result', {})
        path, claim = Path('staged.docx'), {'document_id': 'doc-1',
                                            'source_sha256': 'a' * 64}

        for extension in ('.docx', '.pptx', '.xlsx'):
            with mock.patch.object(office_plugin, 'parse', return_value=sentinel) as parse:
                self.assertIs(upload_pipeline.PARSERS[extension](path, claim), sentinel)

            parse.assert_called_once_with(path, 'doc-1', 'a' * 64, claim)

    def test_the_dxf_entry_delegates_the_whole_claim(self):
        sentinel = ('result', {'layers': 0})
        path, claim = Path('staged.dxf'), {'document_id': 'doc-1',
                                           'source_sha256': 'a' * 64,
                                           'provenance': 'synthetic_demo'}

        with mock.patch.object(dxf_plugin, 'parse', return_value=sentinel) as parse:
            self.assertIs(upload_pipeline.PARSERS['.dxf'](path, claim), sentinel)

        parse.assert_called_once_with(path, 'doc-1', 'a' * 64, claim)


def _payload():
    """A payload shaped like a DXF contract result: four entities and one dimension."""
    def entity(layer, kind='cad_entity'):
        return {'block_type': 'paragraph', 'locator': {'locator_kind': kind, 'layer': layer}}

    return {'blocks': [entity('WALLS'), entity('WALLS'), entity('DOORS'),
                       entity('IGNORED', kind='file_region'),
                       {'block_type': 'dimension',
                        'locator': {'locator_kind': 'cad_entity', 'layer': 'DIMS'}}]}


class DxfProvenanceFallbackTests(unittest.TestCase):
    def setUp(self):
        self.path = Path('staged.dxf')
        self.claim = {'document_id': 'doc-1', 'source_sha256': 'b' * 64,
                      'provenance': 'synthetic_demo'}
        self.result = mock.Mock(payload=_payload())

    def test_a_handler_signature_refusal_retries_without_provenance(self):
        refusal = HTTPException(500, detail={'details': {'reason': 'handler_signature'}})

        with mock.patch.object(cad_adapter, 'parse',
                               side_effect=[refusal, self.result]) as parse:
            result, detail = dxf_plugin.parse(self.path, 'doc-1', 'b' * 64, self.claim)

        self.assertIs(result, self.result)
        self.assertEqual(detail, {'layers': 3, 'layer_names': ['DIMS', 'DOORS', 'WALLS'],
                                  'entities': 4, 'dimensions': 1})
        self.assertEqual(parse.call_args_list,
                         [mock.call(self.path, document_id='doc-1', source_version='b' * 64,
                                    provenance='synthetic_demo'),
                          mock.call(self.path, document_id='doc-1',
                                    source_version='b' * 64)])

    def test_any_other_refusal_is_reraised_and_not_retried(self):
        refusal = HTTPException(422, detail={'details': {'reason': 'profile_unsupported'}})

        with mock.patch.object(cad_adapter, 'parse', side_effect=[refusal]) as parse:
            with self.assertRaises(HTTPException) as caught:
                dxf_plugin.parse(self.path, 'doc-1', 'b' * 64, self.claim)

        self.assertIs(caught.exception, refusal)
        parse.assert_called_once_with(self.path, document_id='doc-1',
                                      source_version='b' * 64, provenance='synthetic_demo')

    def test_a_refusal_without_a_detail_mapping_is_reraised(self):
        refusal = HTTPException(500, detail='boom')

        with mock.patch.object(cad_adapter, 'parse', side_effect=[refusal]) as parse:
            with self.assertRaises(HTTPException):
                dxf_plugin.parse(self.path, 'doc-1', 'b' * 64, self.claim)

        parse.assert_called_once()

    def test_without_options_the_provenance_hint_is_none(self):
        with mock.patch.object(cad_adapter, 'parse', return_value=self.result) as parse:
            dxf_plugin.parse(self.path, 'doc-1', 'b' * 64)

        parse.assert_called_once_with(self.path, document_id='doc-1',
                                      source_version='b' * 64, provenance=None)


class OfficeParseTests(unittest.TestCase):
    def test_parse_delegates_to_office_and_reports_no_detail(self):
        path, result = Path('staged.xlsx'), mock.Mock()

        with mock.patch.object(office, 'parse', return_value=result) as parse:
            self.assertEqual(office_plugin.parse(path, 'doc-1', 'c' * 64, {'ignored': True}),
                             (result, {}))

        parse.assert_called_once_with(path, document_id='doc-1', source_version='c' * 64)


if __name__ == '__main__':
    unittest.main()
