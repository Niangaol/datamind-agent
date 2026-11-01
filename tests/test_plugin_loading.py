"""Explicit-path plugin discovery, JSON config and enable/disable.

``ensure_loaded`` is process-wide and the default context is a singleton, so every scenario
that depends on environment configuration runs in a fresh interpreter: reusing this process
would let one scenario's ``_loaded`` flag answer the next one. The helper below starts a
child with the repository on ``PYTHONPATH`` and the two plugin variables removed, so the
plugin path and config of a developer's shell cannot leak into a test.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datamind import task_budget
from datamind.plugin import ConfigError
from ingestion import plugins

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ENV_KEYS = (plugins.PLUGIN_PATH_ENV, plugins.PLUGIN_CONFIG_ENV)

try:  # The worker's import chain needs the optional SQL stack; only its absence may skip.
    import sqlglot  # noqa: F401
except ImportError as worker_import_error:
    WORKER_IMPORT_ERROR = worker_import_error
else:  # pragma: no cover - the import probe itself defines the flag
    WORKER_IMPORT_ERROR = None

CSV_PLUGIN = '''\
from datamind.plugin.manifest import ParserPluginManifest

MANIFEST = ParserPluginManifest(
    'csv-demo', '1.0.0', ('text/csv',), ('.csv',),
    capabilities=('text',), loader='csv_plugin:parse')


def parse(path, document_id, source_version, options=None):
    raise NotImplementedError
'''

CSV_SCHEMA_PLUGIN = '''\
from datamind.plugin.manifest import ParserPluginManifest

MANIFEST = ParserPluginManifest(
    'csv-demo', '1.0.0', ('text/csv',), ('.csv',),
    capabilities=('text',), loader='csv_plugin:parse',
    config_schema={'delimiter': {'type': str, 'required': True}})
'''

REGISTER_PLUGIN = '''\
from datamind.plugin.manifest import ParserPluginManifest


def register(ctx):
    ctx.parser.register(ParserPluginManifest(
        'jsonl-demo', '1.0.0', ('application/x-jsonl',), ('.jsonl',),
        capabilities=('text',), loader='jsonl_plugin:parse'))
'''

REGISTER_THEN_RAISE_PLUGIN = '''\
from datamind.plugin.manifest import ParserPluginManifest


def register(ctx):
    ctx.parser.register(ParserPluginManifest(
        'jsonl-demo', '1.0.0', ('application/x-jsonl',), ('.jsonl',),
        capabilities=('text',), loader='jsonl_plugin:parse'))
    raise RuntimeError('boom after registering')
'''

DUPLICATE_PLUGIN = '''\
from datamind.plugin.manifest import ParserPluginManifest

MANIFEST = ParserPluginManifest(
    'pdf-native', '9.9.9', ('application/pdf',), ('.pdf',),
    capabilities=('text',), loader='duplicate_plugin:parse')
'''

SCRIPT_DISCOVER = '''\
import json
from ingestion import upload_pipeline
from ingestion.plugins import ensure_loaded, loaded_plugins

context = ensure_loaded()
suffix = upload_pipeline.target('x.csv')
print(json.dumps({
    'suffix': suffix,
    'manifest': context.parser.lookup_by_extension('.csv').name,
    'parser_entry': '.csv' in upload_pipeline.PARSERS,
    'loaded': [{'name': record.manifest.name, 'loaded_from': record.loaded_from,
                'config': record.config} for record in loaded_plugins()],
}))
'''

SCRIPT_REGISTER = '''\
import json
from ingestion import upload_pipeline
from ingestion.plugins import ensure_loaded, loaded_plugins

context = ensure_loaded()
suffix = upload_pipeline.target('rows.jsonl')
print(json.dumps({
    'suffix': suffix,
    'manifest': context.parser.lookup_by_extension('.jsonl').name,
    'loaded': [{'name': record.manifest.name, 'loaded_from': record.loaded_from}
               for record in loaded_plugins()],
}))
'''

SCRIPT_DUPLICATE = '''\
import json
from ingestion.plugins import ensure_loaded, loaded_plugins

context = ensure_loaded()
print(json.dumps({
    'pdf': context.parser.lookup_by_extension('.pdf').name,
    'loaded': [{'name': record.manifest.name, 'loaded_from': record.loaded_from}
               for record in loaded_plugins()],
}))
'''

SCRIPT_TARGET = '''\
import json
from fastapi import HTTPException
from ingestion import upload_pipeline

try:
    print(json.dumps({'suffix': upload_pipeline.target('drawing.dxf')}))
except HTTPException as error:
    print(json.dumps({'status': error.status_code, 'detail': error.detail}))
'''

SCRIPT_TARGET_CSV = '''\
import json
from fastapi import HTTPException
from ingestion import upload_pipeline

try:
    print(json.dumps({'suffix': upload_pipeline.target('x.csv')}))
except HTTPException as error:
    print(json.dumps({'status': error.status_code, 'detail': error.detail}))
'''

SCRIPT_STRING_LOADER = '''\
import json
from ingestion.plugins import default_context, ensure_loaded

ensure_loaded()
loader = default_context().parser.lookup_by_extension('.csv').resolve()
try:
    loader('x.csv', 'doc-1', 'a' * 64, {})
except NotImplementedError:
    outcome = 'not_implemented'
else:
    outcome = 'returned'
print(json.dumps({'callable': callable(loader),
                  'module': getattr(loader, '__module__', None),
                  'outcome': outcome}))
'''

SCRIPT_LOAD = '''\
from ingestion.plugins import ensure_loaded

ensure_loaded()
print('loaded')
'''

SCRIPT_REGISTER_THEN_RAISE = '''\
import json
from datamind.plugin.manifest import ManifestError
from ingestion.plugins import default_context, ensure_loaded, loaded_plugins

ensure_loaded()
parser = default_context().parser
outcomes = {}
for label, lookup in (('extension', lambda: parser.lookup_by_extension('.jsonl')),
                      ('media_type', lambda: parser.lookup('application/x-jsonl'))):
    try:
        lookup()
    except ManifestError as error:
        outcomes[label] = error.error_code
    else:
        outcomes[label] = 'resolved'
print(json.dumps({
    'outcomes': outcomes,
    'extensions': list(parser.known_extensions()),
    'loaded': [record.manifest.name for record in loaded_plugins()],
}))
'''


def run_script(script, *, env=None):
    """Run ``script`` in a fresh interpreter with the two plugin variables controlled."""
    child = {name: value for name, value in os.environ.items() if name not in PLUGIN_ENV_KEYS}
    child.update({'PYTHONPATH': str(ROOT), 'PYTHONDONTWRITEBYTECODE': '1',
                  'PYTHONIOENCODING': 'utf-8'})
    child.update(env or {})
    return subprocess.run([sys.executable, '-c', script], cwd=str(ROOT), env=child,
                          capture_output=True, text=True, encoding='utf-8', timeout=180)


def run_module(module, args, *, env=None):
    """Run ``module`` as a fresh interpreter's main program, plugin variables controlled."""
    child = {name: value for name, value in os.environ.items() if name not in PLUGIN_ENV_KEYS}
    child.update({'PYTHONPATH': str(ROOT), 'PYTHONDONTWRITEBYTECODE': '1',
                  'PYTHONIOENCODING': 'utf-8'})
    child.update(env or {})
    return subprocess.run([sys.executable, '-m', module, *args], cwd=str(ROOT), env=child,
                          capture_output=True, text=True, encoding='utf-8', timeout=180)


def write_module(directory, name, source):
    path = Path(directory) / name
    path.write_text(textwrap.dedent(source), encoding='utf-8')
    return path.resolve()


class DirectoryDiscoveryTests(unittest.TestCase):
    def test_a_manifest_module_in_a_configured_directory_is_registered(self):
        with tempfile.TemporaryDirectory() as root:
            plugin_file = write_module(root, 'csv_plugin.py', CSV_PLUGIN)
            result = run_script(SCRIPT_DISCOVER,
                                env={plugins.PLUGIN_PATH_ENV: str(Path(root))})

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['suffix'], '.csv')
        self.assertEqual(payload['manifest'], 'csv-demo')
        # The registry entry reached the suffix table `target()` bootstraps.
        self.assertTrue(payload['parser_entry'])
        self.assertEqual(payload['loaded'][0]['loaded_from'], 'builtin')
        external = [record for record in payload['loaded'] if record['name'] == 'csv-demo']
        self.assertEqual(external, [{'name': 'csv-demo', 'loaded_from': str(plugin_file),
                                     'config': None}])

    def test_a_register_module_declares_through_the_registry(self):
        with tempfile.TemporaryDirectory() as root:
            plugin_file = write_module(root, 'jsonl_plugin.py', REGISTER_PLUGIN)
            result = run_script(SCRIPT_REGISTER,
                                env={plugins.PLUGIN_PATH_ENV: str(Path(root))})

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['suffix'], '.jsonl')
        self.assertEqual(payload['manifest'], 'jsonl-demo')
        self.assertIn({'name': 'jsonl-demo', 'loaded_from': str(plugin_file)},
                      payload['loaded'])

    def test_a_register_plugin_that_raises_after_registering_is_skipped_cleanly(self):
        with tempfile.TemporaryDirectory() as root:
            plugin_file = write_module(root, 'jsonl_plugin.py', REGISTER_THEN_RAISE_PLUGIN)
            result = run_script(SCRIPT_REGISTER_THEN_RAISE,
                                env={plugins.PLUGIN_PATH_ENV: str(Path(root))})

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        # The manifest the failing `register(ctx)` managed to claim is rolled back, so the
        # module is skipped with nothing left registered - not a half-applied plugin.
        self.assertEqual(payload['outcomes'], {'extension': 'PARSE_ADAPTER_UNKNOWN',
                                               'media_type': 'PARSE_ADAPTER_UNKNOWN'})
        self.assertNotIn('.jsonl', payload['extensions'])
        self.assertNotIn('jsonl-demo', payload['loaded'])
        self.assertIn(str(plugin_file), result.stderr)
        self.assertIn('boom after registering', result.stderr)

    def test_one_broken_module_does_not_stop_the_others(self):
        with tempfile.TemporaryDirectory() as root:
            broken = write_module(root, 'broken_plugin.py', 'raise RuntimeError("boom")\n')
            write_module(root, 'csv_plugin.py', CSV_PLUGIN)
            result = run_script(SCRIPT_DISCOVER,
                                env={plugins.PLUGIN_PATH_ENV: str(Path(root))})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['suffix'], '.csv')
        self.assertIn(str(broken), result.stderr)
        self.assertIn('1 external plugin module(s) failed', result.stderr)

    def test_a_valid_plugin_config_is_validated_and_recorded(self):
        with tempfile.TemporaryDirectory() as root:
            plugin_dir = Path(root) / 'plugins'
            plugin_dir.mkdir()
            write_module(plugin_dir, 'csv_plugin.py', CSV_SCHEMA_PLUGIN)
            config = Path(root) / 'plugins.json'
            config.write_text(json.dumps(
                {'csv-demo': {'enabled': True, 'config': {'delimiter': ';'}}}),
                encoding='utf-8')
            result = run_script(SCRIPT_DISCOVER,
                                env={plugins.PLUGIN_PATH_ENV: str(plugin_dir),
                                     plugins.PLUGIN_CONFIG_ENV: str(config)})

        self.assertEqual(result.returncode, 0, result.stderr)
        external = [record for record in json.loads(result.stdout)['loaded']
                    if record['name'] == 'csv-demo']
        self.assertEqual(len(external), 1)
        self.assertEqual(external[0]['config'], {'delimiter': ';'})

    def test_an_explicit_null_config_is_recorded_as_no_config(self):
        with tempfile.TemporaryDirectory() as root:
            plugin_dir = Path(root) / 'plugins'
            plugin_dir.mkdir()
            write_module(plugin_dir, 'csv_plugin.py', CSV_PLUGIN)
            config = Path(root) / 'plugins.json'
            config.write_text(json.dumps(
                {'csv-demo': {'enabled': True, 'config': None}}), encoding='utf-8')
            result = run_script(SCRIPT_DISCOVER,
                                env={plugins.PLUGIN_PATH_ENV: str(plugin_dir),
                                     plugins.PLUGIN_CONFIG_ENV: str(config)})

        self.assertEqual(result.returncode, 0, result.stderr)
        external = [record for record in json.loads(result.stdout)['loaded']
                    if record['name'] == 'csv-demo']
        self.assertEqual(len(external), 1)
        self.assertIsNone(external[0]['config'])

    def test_a_duplicate_name_keeps_the_builtin_and_is_only_reported(self):
        with tempfile.TemporaryDirectory() as root:
            plugin_file = write_module(root, 'duplicate_plugin.py', DUPLICATE_PLUGIN)
            result = run_script(SCRIPT_DUPLICATE,
                                env={plugins.PLUGIN_PATH_ENV: str(Path(root))})

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['pdf'], 'pdf-native')
        pdf_records = [record for record in payload['loaded'] if record['name'] == 'pdf-native']
        self.assertEqual(pdf_records, [{'name': 'pdf-native', 'loaded_from': 'builtin'}])
        self.assertIn(str(plugin_file), result.stderr)
        self.assertIn('pdf-native', result.stderr)


class DisabledPluginTests(unittest.TestCase):
    def _config(self, root, body):
        path = Path(root) / 'plugins.json'
        path.write_text(json.dumps(body), encoding='utf-8')
        return path

    def test_a_configured_disabled_plugin_names_itself_in_the_refusal(self):
        with tempfile.TemporaryDirectory() as root:
            config = self._config(root, {'dxf-native': {'enabled': False}})
            result = run_script(SCRIPT_TARGET,
                                env={plugins.PLUGIN_CONFIG_ENV: str(config)})

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['status'], 415)
        self.assertEqual(payload['detail'],
                         {'error_code': 'UNSUPPORTED_MEDIA_TYPE',
                          'details': {'suffix': '.dxf', 'reason': 'plugin_disabled',
                                      'plugin': 'dxf-native'}})

    def test_without_the_disable_the_same_name_is_served_again(self):
        result = run_script(SCRIPT_TARGET)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'suffix': '.dxf'})

    def test_a_disabled_external_plugin_names_itself_in_the_refusal(self):
        with tempfile.TemporaryDirectory() as root:
            plugin_dir = Path(root) / 'plugins'
            plugin_dir.mkdir()
            write_module(plugin_dir, 'csv_plugin.py', CSV_PLUGIN)
            config = self._config(root, {'csv-demo': {'enabled': False}})
            result = run_script(SCRIPT_TARGET_CSV,
                                env={plugins.PLUGIN_PATH_ENV: str(plugin_dir),
                                     plugins.PLUGIN_CONFIG_ENV: str(config)})

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['status'], 415)
        self.assertEqual(payload['detail'],
                         {'error_code': 'UNSUPPORTED_MEDIA_TYPE',
                          'details': {'suffix': '.csv', 'reason': 'plugin_disabled',
                                      'plugin': 'csv-demo'}})


class ExternalStringLoaderTests(unittest.TestCase):
    def test_a_string_loader_naming_its_own_file_resolves(self):
        with tempfile.TemporaryDirectory() as root:
            write_module(root, 'csv_plugin.py', CSV_PLUGIN)
            result = run_script(SCRIPT_STRING_LOADER,
                                env={plugins.PLUGIN_PATH_ENV: str(root)})

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload['callable'])
        # The loader lives in the synthetically named plugin module, not a bare stem.
        self.assertTrue(payload['module'].startswith('datamind_external_plugin_'),
                        payload['module'])
        self.assertEqual(payload['outcome'], 'not_implemented')


class InvalidConfigTests(unittest.TestCase):
    def _run(self, body, *, raw=None, plugin_dir=None):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / 'plugins.json'
            self.config_path = config
            config.write_text(raw if raw is not None else json.dumps(body), encoding='utf-8')
            env = {plugins.PLUGIN_CONFIG_ENV: str(config)}
            if plugin_dir is not None:
                env[plugins.PLUGIN_PATH_ENV] = str(plugin_dir)
            return run_script(SCRIPT_LOAD, env=env)

    def test_every_malformed_configuration_fails_fast_with_the_field_path(self):
        cases = {
            'enabled_is_not_a_bool': ({'dxf-native': {'enabled': 'yes'}},
                                      ('plugin.dxf-native.enabled', 'expected bool')),
            'unknown_entry_key': ({'dxf-native': {'enable': False}},
                                  ('plugin.dxf-native.enable', 'is not a known field')),
            'config_is_not_an_object': ({'dxf-native': {'config': []}},
                                        ('plugin.dxf-native.config', 'expects an object')),
            'entry_is_not_an_object': ({'dxf-native': True},
                                       ('plugin.dxf-native', 'expects an object')),
            # The subject is the config file itself: a non-object root has no field path.
            'root_is_not_an_object': ([], ('expects a JSON object',)),
            'unknown_plugin_name': ({'ghost-plugin': {'enabled': True}},
                                    ('plugin.ghost-plugin', 'is not a loaded plugin')),
        }
        for name, (body, fragments) in cases.items():
            with self.subTest(case=name):
                result = self._run(body)
                self.assertNotEqual(result.returncode, 0)
                for fragment in fragments:
                    self.assertIn(fragment, result.stderr)
                if name == 'root_is_not_an_object':
                    self.assertIn(str(self.config_path), result.stderr)

    def test_invalid_json_names_the_file(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / 'plugins.json'
            config.write_text('{', encoding='utf-8')
            result = run_script(SCRIPT_LOAD, env={plugins.PLUGIN_CONFIG_ENV: str(config)})

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(config), result.stderr)
        self.assertIn('is not valid JSON', result.stderr)

    def test_a_config_the_manifest_schema_rejects_names_the_field(self):
        with tempfile.TemporaryDirectory() as root:
            plugin_dir = Path(root) / 'plugins'
            plugin_dir.mkdir()
            write_module(plugin_dir, 'csv_plugin.py', CSV_SCHEMA_PLUGIN)
            result = self._run({'csv-demo': {'config': {'delimiter': 3}}},
                               plugin_dir=plugin_dir)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('plugin.csv-demo.delimiter', result.stderr)


class PluginPathTests(unittest.TestCase):
    def test_only_top_level_public_modules_are_discovered(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / 'one.py').write_text('', encoding='utf-8')
            (root / '_private.py').write_text('', encoding='utf-8')
            (root / 'notes.txt').write_text('', encoding='utf-8')
            nested = root / 'nested'
            nested.mkdir()
            (nested / 'deep.py').write_text('', encoding='utf-8')

            files = plugins._plugin_files(str(root))

        self.assertEqual(files, ((root / 'one.py').resolve(),))

    def test_a_directory_named_twice_is_scanned_once(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / 'one.py').write_text('', encoding='utf-8')
            value = os.pathsep.join([root, root, ''])

            files = plugins._plugin_files(value)

        self.assertEqual(len(files), 1)

    def test_a_missing_directory_is_a_configuration_error(self):
        with tempfile.TemporaryDirectory() as root:
            missing = Path(root) / 'missing'
            with self.assertRaises(ConfigError) as caught:
                plugins._plugin_files(os.pathsep.join([root, str(missing)]))

        self.assertEqual(caught.exception.path, str(missing))
        self.assertEqual(caught.exception.reason, 'plugin path is not a directory')


class ChildBootstrapTests(unittest.TestCase):
    def test_a_bounded_child_bootstraps_the_plugins_from_its_environment(self):
        with tempfile.TemporaryDirectory() as root:
            plugin_dir = Path(root) / 'plugins'
            plugin_dir.mkdir()
            write_module(plugin_dir, 'csv_plugin.py', CSV_PLUGIN)
            env = {name: value for name, value in os.environ.items()
                   if name not in PLUGIN_ENV_KEYS}
            env[plugins.PLUGIN_PATH_ENV] = str(plugin_dir)

            outcome = task_budget.run_bounded(
                'ingestion.upload_pipeline', 'target', ['sample.csv'],
                deadline_s=60, cwd=str(ROOT), env=env)

        self.assertEqual(outcome['status'], 'ok', outcome)
        # `target` returns the suffix it admitted, not the name it was handed.
        self.assertEqual(outcome['value'], '.csv')


class InProcessLoadingTests(unittest.TestCase):
    """The process-wide records of this interpreter, with the environment neutralized."""

    def setUp(self):
        saved = {name: os.environ.pop(name) for name in PLUGIN_ENV_KEYS if name in os.environ}
        self.addCleanup(os.environ.update, saved)

    def test_ensure_loaded_is_idempotent_and_matches_the_default_context(self):
        self.assertIs(plugins.ensure_loaded(), plugins.default_context())
        self.assertIs(plugins.loaded_plugins(), plugins.loaded_plugins())

    def test_the_builtins_are_recorded_in_registration_order(self):
        loaded = plugins.loaded_plugins()

        self.assertEqual([record.manifest.name for record in loaded],
                         ['pdf-native', 'ohr-bench-json', 'docx-native', 'pptx-native',
                          'xlsx-native', 'dxf-native', 'measurement-json'])
        self.assertEqual({record.loaded_from for record in loaded}, {'builtin'})
        self.assertTrue(all(record.config is None for record in loaded))


@unittest.skipIf(WORKER_IMPORT_ERROR is not None,
                 f'the optional SQL stack (sqlglot) is not installed here: {WORKER_IMPORT_ERROR}')
class WorkerStartupTests(unittest.TestCase):
    """`python -m ingestion.upload_worker` validates plugin config before anything else.

    Needs no PostgreSQL: the bootstrap step runs before `_write_dsn()` and before any
    connection, so a bad `DATAMIND_PLUGIN_CONFIG` must stop the process at startup instead
    of surfacing later as an unattributed parse failure. That the worker never reaches its
    heartbeat is what proves no database access happened.
    """

    def test_an_invalid_plugin_config_stops_the_worker_before_any_database_work(self):
        with tempfile.TemporaryDirectory() as root:
            plugin_dir = Path(root) / 'plugins'
            plugin_dir.mkdir()
            config = Path(root) / 'plugins.json'
            config.write_text('{', encoding='utf-8')
            result = run_module('ingestion.upload_worker', ['--once'],
                                env={plugins.PLUGIN_PATH_ENV: str(plugin_dir),
                                     plugins.PLUGIN_CONFIG_ENV: str(config)})

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('plugin config error', result.stderr)
        self.assertIn(str(config), result.stderr)
        self.assertIn('is not valid JSON', result.stderr)
        self.assertIn('refusing to start', result.stderr)
        self.assertNotIn('heartbeat', result.stdout)


class DisabledBuiltinDispatchTests(unittest.TestCase):
    """`run_parse`/`enqueue` refuse a builtin disabled after import, inside one process.

    This must run in this interpreter (the manifest it flips is the live one), so the
    environment is neutralized like :class:`InProcessLoadingTests` and ``enabled`` is
    restored in a ``finally`` before any assertion can escape.
    """

    def setUp(self):
        saved = {name: os.environ.pop(name) for name in PLUGIN_ENV_KEYS if name in os.environ}
        self.addCleanup(os.environ.update, saved)

    def test_a_disabled_builtin_is_refused_by_run_parse_and_enqueue(self):
        from fastapi import HTTPException
        from ingestion import upload_pipeline

        # Load the once-per-process pass first; otherwise `run_parse` itself would apply
        # the builtin defaults and re-enable the manifest under the test.
        plugins.ensure_loaded()
        manifest = next(manifest for manifest in plugins.default_context().parser.list()
                        if manifest.name == 'dxf-native')
        expected = {'error_code': 'UNSUPPORTED_MEDIA_TYPE',
                    'details': {'suffix': '.dxf', 'reason': 'plugin_disabled',
                                'plugin': 'dxf-native'}}
        try:
            manifest.enabled = False
            refusals = {}
            for label, call in (
                    ('run_parse',
                     lambda: upload_pipeline.run_parse({'suffix': '.dxf'}, None)),
                    ('enqueue',
                     lambda: upload_pipeline.enqueue(
                         {'suffix': '.dxf'}, 'synthetic_demo', None, None))):
                with self.assertRaises(HTTPException) as caught:
                    call()
                refusals[label] = (caught.exception.status_code, caught.exception.detail)
        finally:
            manifest.enabled = True

        for label, (status, detail) in refusals.items():
            with self.subTest(call=label):
                self.assertEqual(status, 415)
                self.assertEqual(detail, expected)


if __name__ == '__main__':
    unittest.main()
