"""Parser plugin manifests and the media-type/extension registry service.

The manifest is the declarative half of a parser plugin (what it claims, whether it is
enabled, how its loader is reached); the registry is the runtime half (who parses this
media type or extension, with conflicts refused and registrations reversible). These tests
exercise both through their public surface, plus the ``parse_contract`` unregister seam a
plugin reload depends on.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datamind import parse_contract as contract
from datamind.plugin import ConfigError, Context, PluginError
from datamind.plugin.manifest import ManifestError, ParserPluginManifest, ParserRegistry

ROOT = Path(__file__).resolve().parents[1]
_UNSET = object()


def manifest(name='pdf', *, version='1.0', media_types=('application/pdf',),
             extensions=('.pdf',), capabilities=('native_text',), loader=_UNSET,
             priority=100, config_schema=None, enabled=True):
    """A valid manifest with overridable fields; the default loader is a harmless callable."""
    return ParserPluginManifest(
        name, version, media_types, extensions, capabilities=capabilities,
        loader=(lambda **kwargs: None) if loader is _UNSET else loader,
        priority=priority, config_schema=config_schema, enabled=enabled)


class ManifestValidationTests(unittest.TestCase):
    def test_declarations_are_normalized_and_left_unresolved(self):
        plugin = ParserPluginManifest(
            ' pdf ', '1.0', ['Application/PDF', 'Text/Plain'], ['PDF', '.DocX'],
            capabilities=[' native_text ', 'tables'], loader='pkg.module:parse')

        self.assertEqual(plugin.name, 'pdf')
        self.assertEqual(plugin.media_types, ('application/pdf', 'text/plain'))
        self.assertEqual(plugin.extensions, ('.pdf', '.docx'))
        self.assertEqual(plugin.capabilities, ('native_text', 'tables'))
        self.assertEqual(plugin.loader, 'pkg.module:parse')
        self.assertEqual(plugin.priority, 100)
        self.assertIsNone(plugin.config_schema)
        self.assertTrue(plugin.enabled)

    def test_name_and_version_are_required_text(self):
        for kwargs, subject in (({'name': ''}, 'manifest.name'),
                                ({'name': '  '}, 'manifest.name'),
                                ({'version': ''}, 'manifest.version'),
                                ({'version': 1.0}, 'manifest.version')):
            with self.assertRaises(ManifestError) as caught:
                manifest(**kwargs)
            error = caught.exception
            self.assertEqual(error.error_code, 'PARSE_RESULT_INVALID')
            self.assertEqual(error.details['subject'], subject)

    def test_media_types_must_be_a_non_empty_sequence_of_media_types(self):
        with self.assertRaises(ManifestError) as caught:
            manifest(media_types=())
        self.assertEqual(caught.exception.details['subject'], 'manifest.media_types')
        for media_types in (['nonsense'], ['application'], ['application/'], 'application/pdf',
                            [None], ['application/pdf', 'application/pdf'],
                            ['application/pdf; charset=utf-8'], ['application/pdf;q=1']):
            with self.assertRaises(ManifestError) as caught:
                manifest(media_types=media_types)
            self.assertEqual(caught.exception.error_code, 'PARSE_RESULT_INVALID',
                             f'{media_types!r} was accepted')

    def test_extensions_are_optional(self):
        plugin = manifest(extensions=())

        self.assertEqual(plugin.extensions, ())

    def test_extension_must_be_a_non_empty_string_with_a_leading_dot_after_normalizing(self):
        for extension in ('', '  ', '.', '..pdf', 'p df', 'a/b'):
            with self.assertRaises(ManifestError) as caught:
                manifest(extensions=[extension])
            self.assertEqual(caught.exception.details['subject'], 'manifest.extensions',
                             f'{extension!r} was accepted')
        self.assertEqual(manifest(extensions=['.TAR.GZ']).extensions, ('.tar.gz',))

    def test_capabilities_are_free_form_but_must_be_non_empty_strings(self):
        for capabilities in ((), [''], ['  '], [1], None):
            with self.assertRaises(ManifestError) as caught:
                manifest(capabilities=capabilities)
            self.assertEqual(caught.exception.details['subject'], 'manifest.capabilities',
                             f'{capabilities!r} was accepted')
        self.assertEqual(manifest(capabilities=['ocr', 'formula']).capabilities,
                         ('ocr', 'formula'))

    def test_loader_must_be_callable_or_a_module_function_reference(self):
        for loader in (5, 'module_only', 'module:', ':function', 'module:function:extra',
                       '1module:function', None):
            with self.assertRaises(ManifestError) as caught:
                manifest(loader=loader)
            self.assertEqual(caught.exception.details['subject'], 'manifest.loader',
                             f'{loader!r} was accepted')
        self.assertEqual(manifest(loader='pkg.sub:Klass.parse').loader, 'pkg.sub:Klass.parse')

    def test_priority_must_be_an_integer(self):
        for priority in ('100', 1.5, True, None):
            with self.assertRaises(ManifestError) as caught:
                manifest(priority=priority)
            self.assertEqual(caught.exception.details['subject'], 'manifest.priority')

    def test_enabled_must_be_a_boolean(self):
        with self.assertRaises(ManifestError) as caught:
            manifest(enabled='yes')

        self.assertEqual(caught.exception.details['subject'], 'manifest.enabled')

    def test_config_schema_must_declare_fields_with_known_spec_keys(self):
        with self.assertRaises(ManifestError) as caught:
            manifest(config_schema=[])
        self.assertEqual(caught.exception.details['subject'], 'manifest.config_schema')
        with self.assertRaises(ManifestError) as caught:
            manifest(config_schema={'pages': ['int']})
        self.assertEqual(caught.exception.details['subject'], 'manifest.config_schema.pages')
        with self.assertRaises(ManifestError) as caught:
            manifest(config_schema={'pages': {'type': int, 'defualt': 1}})
        self.assertEqual(caught.exception.details['subject'], 'manifest.config_schema.pages')
        self.assertEqual(caught.exception.details['reason'], 'unknown_field_spec_key')

    def test_config_schema_failures_name_the_field_path(self):
        plugin = manifest(config_schema={'pages': {'type': int, 'required': True},
                                         'mode': {'type': str, 'default': 'strict',
                                                  'choices': ('strict', 'loose')}})

        with self.assertRaises(ConfigError) as caught:
            plugin.validate_config({})
        self.assertEqual(caught.exception.path, 'plugin.pdf.pages')
        with self.assertRaises(ConfigError) as caught:
            plugin.validate_config({'pages': 'one'})
        self.assertEqual(caught.exception.path, 'plugin.pdf.pages')
        with self.assertRaises(ConfigError) as caught:
            plugin.validate_config({'pages': 1, 'mode': 'loose-ish'})
        self.assertEqual(caught.exception.path, 'plugin.pdf.mode')
        with self.assertRaises(ConfigError) as caught:
            plugin.validate_config({'pages': 1, 'ghost': True})
        self.assertEqual(caught.exception.path, 'plugin.pdf.ghost')
        self.assertEqual(plugin.validate_config({'pages': 2}),
                         {'pages': 2, 'mode': 'strict'})

    def test_resolve_imports_a_module_function_loader_only_when_asked(self):
        plugin = manifest(loader='json:loads')

        self.assertIs(plugin.resolve(), json.loads)

        def loader(**kwargs):
            return 'parsed'

        self.assertIs(manifest(loader=loader).resolve(), loader)

    def test_manifest_errors_are_plugin_errors_carrying_the_stable_code_and_details(self):
        with self.assertRaises(PluginError) as caught:
            manifest(media_types=())

        detail = caught.exception.as_detail()
        self.assertEqual(detail['error_code'], 'PARSE_RESULT_INVALID')
        self.assertEqual(detail['details']['subject'], 'manifest.media_types')


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context()
        self.registry = ParserRegistry()
        self.ctx.set('parser', self.registry)

    def test_the_registry_is_the_parser_service_of_its_context(self):
        self.assertIs(self.ctx.parser, self.registry)
        self.assertTrue(self.ctx.has('parser'))

    def test_registration_exposes_both_lookups(self):
        pdf = manifest()
        ohr = manifest(name='ohr', media_types=('application/vnd.ohr-bench+json',),
                       extensions=(), capabilities=('ohr_bench',))
        self.registry.register(pdf)
        self.registry.register(ohr)

        self.assertIs(self.registry.lookup('Application/PDF'), pdf)
        self.assertIs(self.registry.lookup_by_extension('PDF'), pdf)
        self.assertIs(self.registry.lookup_by_extension('.pdf'), pdf)
        self.assertIs(self.registry.lookup('application/vnd.ohr-bench+json'), ohr)
        self.assertEqual(self.registry.known_extensions(), ('.pdf',))
        self.assertEqual(self.registry.list(), (pdf, ohr))
        self.assertEqual(self.registry.capabilities('pdf'), ('native_text',))
        self.assertEqual(self.registry.capabilities(),
                         {'pdf': ('native_text',), 'ohr': ('ohr_bench',)})
        self.assertEqual(self.registry.adapter_name('.pdf'), 'pdf')

    def test_duplicate_media_type_is_refused_and_changes_nothing(self):
        first = manifest()
        self.registry.register(first)
        second = manifest(name='pdf-ng', media_types=('application/pdf', 'image/tiff'),
                          extensions=('.tiff',))

        with self.assertRaises(ManifestError) as caught:
            self.registry.register(second)

        error = caught.exception
        self.assertEqual(error.error_code, 'PARSE_RESULT_INVALID')
        self.assertEqual(error.details['reason'], 'media_type_already_registered')
        self.assertEqual(error.details['plugin'], 'pdf')
        # Nothing of the refused manifest leaked in: its other media type and extension
        # are still free, and the registered manifest is untouched.
        self.assertEqual(self.registry.list(), (first,))
        self.assertEqual(self.registry.known_extensions(), ('.pdf',))
        with self.assertRaises(ManifestError):
            self.registry.lookup('image/tiff')
        self.assertIs(self.registry.lookup('application/pdf'), first)

    def test_duplicate_extension_is_refused_and_changes_nothing(self):
        self.registry.register(manifest())
        second = manifest(name='pdf-copy', media_types=('image/tiff',), extensions=('.pdf',))

        with self.assertRaises(ManifestError) as caught:
            self.registry.register(second)

        self.assertEqual(caught.exception.error_code, 'PARSE_RESULT_INVALID')
        self.assertEqual(caught.exception.details['reason'], 'extension_already_registered')
        self.assertEqual(self.registry.known_extensions(), ('.pdf',))
        with self.assertRaises(ManifestError):
            self.registry.lookup('image/tiff')

    def test_duplicate_plugin_name_is_refused(self):
        self.registry.register(manifest())

        with self.assertRaises(ManifestError) as caught:
            self.registry.register(manifest(media_types=('image/tiff',), extensions=('.tiff',)))

        self.assertEqual(caught.exception.details['reason'], 'plugin_already_registered')
        self.assertEqual(len(self.registry.list()), 1)

    def test_one_manifest_may_cover_several_media_types(self):
        bundle = manifest(name='office', media_types=('application/msword', 'text/csv'),
                          extensions=('.doc', '.csv'), capabilities=('text',))
        dispose = self.registry.register(bundle)

        self.assertIs(self.registry.lookup('application/msword'), bundle)
        self.assertIs(self.registry.lookup('text/csv'), bundle)
        self.assertEqual(self.registry.known_extensions(), ('.csv', '.doc'))

        dispose()

        for media_type in ('application/msword', 'text/csv'):
            with self.assertRaises(ManifestError):
                self.registry.lookup(media_type)
        self.assertEqual(self.registry.known_extensions(), ())

    def test_media_only_manifest_registers_without_an_extension(self):
        ohr = manifest(name='ohr', media_types=('application/vnd.ohr-bench+json',),
                       extensions=(), capabilities=('ohr_bench',))
        self.registry.register(ohr)

        self.assertIs(self.registry.lookup('application/vnd.ohr-bench+json'), ohr)
        self.assertEqual(ohr.extensions, ())
        self.assertEqual(self.registry.known_extensions(), ())
        with self.assertRaises(ManifestError) as caught:
            self.registry.lookup_by_extension('.json')
        self.assertEqual(caught.exception.error_code, 'PARSE_ADAPTER_UNKNOWN')

    def test_list_and_lookup_never_resolve_the_loader(self):
        calls = []

        def loader(**kwargs):
            calls.append(kwargs)
            raise AssertionError('the loader must not run during listing or lookup')

        absent = manifest(name='pdf', media_types=('application/pdf',), loader=loader)
        lazy = manifest(name='lazy', media_types=('image/tiff',), extensions=('.tiff',),
                        loader='datamind_missing_plugin_module:parse')
        self.registry.register(absent)
        self.registry.register(lazy)

        self.assertEqual(self.registry.list(), (absent, lazy))
        self.assertIs(self.registry.lookup('image/tiff'), lazy)
        self.assertIs(self.registry.lookup_by_extension('.pdf'), absent)
        self.assertEqual(self.registry.known_extensions(), ('.pdf', '.tiff'))
        self.assertEqual(self.registry.capabilities('lazy'), ('native_text',))
        self.assertEqual(self.registry.adapter_name('.tiff'), 'lazy')
        self.assertEqual(calls, [])

    def test_disposer_frees_the_names_is_idempotent_and_allows_reregistration(self):
        first = manifest()
        dispose = self.registry.register(first)

        dispose()
        dispose()

        self.assertEqual(self.registry.list(), ())
        self.assertEqual(self.registry.known_extensions(), ())
        with self.assertRaises(ManifestError):
            self.registry.lookup('application/pdf')

        replacement = manifest()
        self.registry.register(replacement)

        self.assertIs(self.registry.lookup('application/pdf'), replacement)
        self.assertIs(self.registry.lookup_by_extension('.pdf'), replacement)
        self.assertEqual(self.registry.capabilities('pdf'), ('native_text',))

    def test_unregister_frees_the_names_and_allows_reregistration(self):
        first = manifest()
        self.registry.register(first)

        self.assertIs(self.registry.unregister('pdf'), True)

        self.assertEqual(self.registry.list(), ())
        self.assertEqual(self.registry.known_extensions(), ())
        with self.assertRaises(ManifestError) as caught:
            self.registry.lookup_by_extension('.pdf')
        self.assertEqual(caught.exception.error_code, 'PARSE_ADAPTER_UNKNOWN')

        replacement = manifest()
        self.registry.register(replacement)

        self.assertIs(self.registry.lookup_by_extension('.pdf'), replacement)

    def test_unregister_of_an_unknown_name_removes_nothing(self):
        registered = manifest()
        self.registry.register(registered)

        self.assertIs(self.registry.unregister('ghost'), False)

        self.assertEqual(self.registry.list(), (registered,))
        self.assertIs(self.registry.lookup('application/pdf'), registered)
        self.assertEqual(self.registry.known_extensions(), ('.pdf',))

    def test_a_stale_release_leaves_a_replacement_that_owns_the_name(self):
        first = manifest()
        dispose = self.registry.register(first)
        dispose()
        replacement = manifest()
        self.registry.register(replacement)

        # The identity check the disposer and `unregister` share: a release captured for the
        # old registration must not free the replacement's names.
        dispose()

        self.assertIs(self.registry.lookup('application/pdf'), replacement)
        self.assertEqual(self.registry.known_extensions(), ('.pdf',))

        # Releasing by name targets the manifest that owns the name now, and is idempotent.
        self.assertIs(self.registry.unregister('pdf'), True)
        self.assertIs(self.registry.unregister('pdf'), False)

        self.assertEqual(self.registry.list(), ())
        self.assertEqual(self.registry.known_extensions(), ())
        with self.assertRaises(ManifestError):
            self.registry.lookup('application/pdf')

    def test_disposing_one_plugin_leaves_the_other_and_its_manifest(self):
        def pdf_plugin(inner, config):
            inner.effect(inner.parser.register(manifest()))

        def ohr_plugin(inner, config):
            inner.effect(inner.parser.register(
                manifest(name='ohr', media_types=('application/vnd.ohr-bench+json',),
                         extensions=(), capabilities=('ohr_bench',))))

        pdf_handle = self.ctx.plugin(pdf_plugin)
        self.ctx.plugin(ohr_plugin)

        pdf_handle.dispose()

        with self.assertRaises(ManifestError):
            self.registry.lookup('application/pdf')
        self.assertEqual(self.registry.lookup('application/vnd.ohr-bench+json').name, 'ohr')
        self.assertEqual([item.name for item in self.registry.list()], ['ohr'])

    def test_a_plugin_unload_frees_the_manifests_it_registered(self):
        def ohr_plugin(inner, config):
            inner.effect(inner.parser.register(
                manifest(name='ohr', media_types=('application/vnd.ohr-bench+json',),
                         extensions=(), capabilities=('ohr_bench',))))

        handle = self.ctx.plugin(ohr_plugin)

        self.assertIsNotNone(self.registry.lookup('application/vnd.ohr-bench+json'))

        handle.dispose()

        with self.assertRaises(ManifestError):
            self.registry.lookup('application/vnd.ohr-bench+json')
        self.assertEqual(self.registry.list(), ())
        # The names are free again, so reloading the same plugin succeeds.
        self.ctx.plugin(ohr_plugin)
        self.assertEqual(self.registry.list()[0].name, 'ohr')

    def test_a_failed_plugin_load_rolls_its_manifest_back(self):
        def broken_plugin(inner, config):
            inner.effect(inner.parser.register(manifest()))
            raise RuntimeError('load failed after registering')

        with self.assertRaises(RuntimeError):
            self.ctx.plugin(broken_plugin)

        self.assertEqual(self.registry.list(), ())
        with self.assertRaises(ManifestError):
            self.registry.lookup('application/pdf')

    def test_disabled_manifest_is_known_but_not_selected(self):
        disabled = manifest(enabled=False)
        self.registry.register(disabled)

        self.assertEqual(self.registry.known_extensions(), ('.pdf',))
        self.assertEqual(self.registry.known_extensions(include_disabled=False), ())
        for lookup in (lambda: self.registry.lookup('application/pdf'),
                       lambda: self.registry.lookup_by_extension('.pdf'),
                       lambda: self.registry.adapter_name('.pdf')):
            with self.assertRaises(ManifestError) as caught:
                lookup()
            self.assertEqual(caught.exception.error_code, 'PARSE_ADAPTER_UNSUPPORTED')
            self.assertEqual(caught.exception.details['reason'], 'plugin_disabled')
        self.assertEqual(self.registry.capabilities('pdf'), ('native_text',))

        disabled.enabled = True

        self.assertIs(self.registry.lookup('application/pdf'), disabled)
        self.assertEqual(self.registry.known_extensions(include_disabled=False), ('.pdf',))

    def test_lookups_and_capability_queries_refuse_unknown_names(self):
        self.registry.register(manifest())

        for lookup, reason in ((lambda: self.registry.lookup('image/tiff'),
                                'media_type_not_registered'),
                               (lambda: self.registry.lookup('nonsense'),
                                'invalid_media_type'),
                               (lambda: self.registry.lookup_by_extension('.tiff'),
                                'extension_not_registered'),
                               (lambda: self.registry.capabilities('ghost'),
                                'plugin_not_registered')):
            with self.assertRaises(ManifestError) as caught:
                lookup()
            self.assertEqual(caught.exception.error_code, 'PARSE_ADAPTER_UNKNOWN')
            self.assertEqual(caught.exception.details['reason'], reason)

    def test_register_refuses_anything_that_is_not_a_manifest(self):
        with self.assertRaises(ManifestError) as caught:
            self.registry.register({'name': 'pdf'})

        self.assertEqual(caught.exception.error_code, 'PARSE_RESULT_INVALID')
        self.assertEqual(caught.exception.details['reason'], 'manifest_required')

    def test_concurrent_registration_of_one_media_type_has_a_single_winner(self):
        workers = 8
        registry = ParserRegistry()
        barrier = threading.Barrier(workers, timeout=10)
        winners = []
        refusals = []

        def claim(index):
            candidate = manifest(name=f'p{index}', extensions=())
            barrier.wait()
            try:
                registry.register(candidate)
            except ManifestError as error:
                refusals.append(error.error_code)
            else:
                winners.append(index)

        threads = [threading.Thread(target=claim, args=(index,)) for index in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(len(winners), 1, f'winners={winners}')
        self.assertEqual(refusals, ['PARSE_RESULT_INVALID'] * (workers - 1))
        self.assertEqual(len(registry.list()), 1)


class AdapterUnregisterTests(unittest.TestCase):
    """The seam a plugin reload needs: free the media types one adapter record holds."""

    def setUp(self):
        self._saved = dict(contract._ADAPTERS)
        contract._ADAPTERS.clear()

    def tearDown(self):
        contract._ADAPTERS.clear()
        contract._ADAPTERS.update(self._saved)

    def test_unregister_removes_every_media_type_of_the_named_record(self):
        contract.register_adapter('multi',
                                  media_types=['application/pdf', 'application/vnd.ohr-bench+json'],
                                  capabilities=['native_text'])
        contract.register_adapter('other', media_types=['image/png'], capabilities=['image'])

        removed = contract._unregister_adapter('multi')

        self.assertEqual(removed['name'], 'multi')
        self.assertEqual(contract.registered_adapters(), ('other',))
        self.assertEqual(contract.supported_media_types(), ('image/png',))
        for media_type in ('application/pdf', 'application/vnd.ohr-bench+json'):
            with self.assertRaises(Exception) as caught:
                contract.adapter_for(media_type)
            self.assertEqual(caught.exception.detail['error_code'], 'PARSE_ADAPTER_UNKNOWN')
        self.assertEqual(contract.adapter_for('image/png')['name'], 'other')
        self.assertEqual(contract.capabilities('other'), ('image',))

    def test_unregister_is_idempotent_and_ignores_an_unknown_name(self):
        contract.register_adapter('pdf', media_types=['application/pdf'], capabilities=['native_text'])

        self.assertIsNone(contract._unregister_adapter('ghost'))
        self.assertIsNotNone(contract._unregister_adapter('pdf'))
        self.assertIsNone(contract._unregister_adapter('pdf'))
        self.assertEqual(contract.supported_media_types(), ())

    def test_reload_after_unregister_does_not_hit_media_type_already_registered(self):
        def handler(**kwargs):
            return {'not': 'validated here'}

        contract.register_adapter('pdf', media_types=['application/pdf'],
                                  capabilities=['native_text'], handler=handler)
        contract._unregister_adapter('  pdf  ')

        record = contract.register_adapter('pdf', media_types=['application/pdf'],
                                           capabilities=['native_text'], handler=handler)

        self.assertIs(record['handler'], handler)
        self.assertIs(contract.adapter_for('application/pdf'), record)
        self.assertEqual(contract.registered_adapters(), ('pdf',))

    def test_removing_a_record_does_not_touch_a_sibling_on_the_same_name_prefix(self):
        contract.register_adapter('pdf', media_types=['application/pdf'], capabilities=['a'])
        contract.register_adapter('pdf-ocr', media_types=['image/tiff'], capabilities=['b'])

        contract._unregister_adapter('pdf')

        self.assertEqual(contract.registered_adapters(), ('pdf-ocr',))
        self.assertEqual(contract.adapter_for('image/tiff')['name'], 'pdf-ocr')


class StdlibOnlyTests(unittest.TestCase):
    def test_the_plugin_package_imports_without_the_application_dependencies(self):
        code = ('import sys, datamind.plugin.manifest; '
                'banned = {"fastapi", "datamind.parse_contract", "ingestion.ingest"}; '
                'print(",".join(sorted(banned & set(sys.modules))))')
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                                cwd=str(ROOT), env=dict(os.environ, PYTHONPATH=str(ROOT)))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '')


if __name__ == '__main__':
    unittest.main()
