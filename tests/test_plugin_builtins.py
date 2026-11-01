"""Built-in parser plugins: the PDF and OHR-Bench JSON declarations and their bootstrap.

These tests pin the two contracts the plugin layer adds without changing parse behaviour:
the declarations must match the adapters ``ingestion.ingest`` already registers, and the
process-wide default context must be the one place both the API process and the parsing
child process bootstrap from. Listing them must never import a parser dependency (G7).
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datamind import parse_contract as contract
from ingestion import ingest, upload_pipeline
from ingestion.plugins import builtin_manifests, default_context

ROOT = Path(__file__).resolve().parents[1]


class BuiltinDeclarationTests(unittest.TestCase):
    def test_the_builtins_are_the_adapters_ingest_already_registers(self):
        self.assertEqual([manifest.name for manifest in builtin_manifests()],
                         [ingest._PDF_ADAPTER, ingest.OHR_ADAPTER])

    def test_the_pdf_manifest_declares_the_adapter_capability_set(self):
        pdf = builtin_manifests()[0]

        self.assertEqual(pdf.name, 'pdf-native')
        self.assertEqual(pdf.media_types, ('application/pdf',))
        self.assertEqual(pdf.extensions, ('.pdf',))
        self.assertEqual(pdf.capabilities, ('native_text', 'pixel_ocr', 'tables', 'formula'))
        self.assertEqual(pdf.loader, 'ingestion.plugins.pdf_plugin:parse')

    def test_the_ohr_manifest_claims_a_media_type_and_no_extension(self):
        ohr = builtin_manifests()[1]

        self.assertEqual(ohr.name, 'ohr-bench-json')
        self.assertEqual(ohr.media_types, (ingest.OHR_MEDIA_TYPE,))
        self.assertEqual(ohr.extensions, ())
        self.assertEqual(ohr.capabilities, ('native_text',))
        self.assertEqual(ohr.loader, 'ingestion.ingest:_ohr_contract_summary')

    def test_the_ohr_manifest_never_claims_json(self):
        ohr = builtin_manifests()[1]

        for extension in ohr.extensions:
            self.assertNotIn('json', extension)
        self.assertNotIn('.json', repr(ohr))


class DefaultContextTests(unittest.TestCase):
    def test_the_default_context_is_created_once(self):
        self.assertIs(default_context(), default_context())

    def test_the_default_context_installs_the_registry_with_both_builtins(self):
        ctx = default_context()

        self.assertIsInstance(ctx.parser.list(), tuple)
        self.assertEqual([manifest.name for manifest in ctx.parser.list()],
                         ['pdf-native', 'ohr-bench-json'])
        self.assertEqual(ctx.parser.capabilities(),
                         {'pdf-native': ('native_text', 'pixel_ocr', 'tables', 'formula'),
                          'ohr-bench-json': ('native_text',)})

    def test_every_media_type_and_extension_routes_to_its_plugin(self):
        parser = default_context().parser

        self.assertEqual(parser.lookup('application/pdf').name, 'pdf-native')
        self.assertEqual(parser.lookup('APPLICATION/PDF').name, 'pdf-native')
        self.assertEqual(parser.lookup_by_extension('.pdf').name, 'pdf-native')
        self.assertEqual(parser.lookup_by_extension('pdf').name, 'pdf-native')
        self.assertEqual(parser.lookup(ingest.OHR_MEDIA_TYPE).name, 'ohr-bench-json')
        self.assertEqual(parser.known_extensions(), ('.pdf',))

    def test_loader_references_stay_strings(self):
        for manifest in default_context().parser.list():
            self.assertIsInstance(manifest.loader, str)
            self.assertFalse(callable(manifest.loader))


class StdlibOnlyTests(unittest.TestCase):
    def test_listing_the_builtins_imports_no_parser_dependency(self):
        code = ('import sys, ingestion.plugins as plugins; '
                'plugins.builtin_manifests(); '
                '[(m.name, m.capabilities) for m in plugins.default_context().parser.list()]; '
                'banned = {"pymupdf", "qdrant_client", "ingestion.ingest"}; '
                'print(",".join(sorted(banned & set(sys.modules))))')
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                                cwd=str(ROOT), env=dict(os.environ, PYTHONPATH=str(ROOT)))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '')


class AdapterBootstrapTests(unittest.TestCase):
    """The legacy ensure_* helpers must also bootstrap the plugin side, exactly once."""

    def setUp(self):
        self._saved = dict(contract._ADAPTERS)
        contract._ADAPTERS.clear()

    def tearDown(self):
        contract._ADAPTERS.clear()
        contract._ADAPTERS.update(self._saved)

    def test_ensure_pdf_adapter_registers_once_and_bootstraps_the_context(self):
        ingest._ensure_pdf_adapter()
        first = contract.adapter_for('application/pdf')
        ingest._ensure_pdf_adapter()

        self.assertEqual(contract.registered_adapters().count('pdf-native'), 1)
        self.assertIs(contract.adapter_for('application/pdf'), first)
        self.assertEqual(first['capabilities'], ('native_text', 'pixel_ocr', 'tables', 'formula'))
        self.assertIs(first['handler'], ingest._pdf_contract_payload)
        self.assertEqual(default_context().parser.lookup('application/pdf').name, 'pdf-native')

    def test_ensure_ohr_adapter_registers_once_and_bootstraps_the_context(self):
        ingest._ensure_ohr_adapter()
        first = contract.adapter_for(ingest.OHR_MEDIA_TYPE)
        ingest._ensure_ohr_adapter()

        self.assertEqual(contract.registered_adapters().count('ohr-bench-json'), 1)
        self.assertIs(contract.adapter_for(ingest.OHR_MEDIA_TYPE), first)
        self.assertEqual(first['capabilities'], ('native_text',))
        self.assertEqual(default_context().parser.lookup(ingest.OHR_MEDIA_TYPE).name,
                         'ohr-bench-json')


class ParserTableTests(unittest.TestCase):
    def test_the_pdf_entry_still_points_at_the_patchable_module_function(self):
        self.assertIs(upload_pipeline.PARSERS['.pdf'], upload_pipeline._parse_pdf)

    def test_the_pdf_function_delegates_to_the_plugin_unchanged(self):
        sentinel = ('result', {'pages': 3})
        path, claim = Path('staged.pdf'), {'document_id': 'doc-1',
                                           'source_sha256': 'a' * 64}

        with mock.patch('ingestion.plugins.pdf_plugin.parse', return_value=sentinel) as parse:
            self.assertIs(upload_pipeline._parse_pdf(path, claim), sentinel)

        parse.assert_called_once_with(path, 'doc-1', 'a' * 64, claim)


if __name__ == '__main__':
    unittest.main()
