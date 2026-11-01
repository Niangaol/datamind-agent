"""The runnable third-party example: discovery, contract output and lazy baselines.

``examples/plugins/csv_parser`` is the only plugin in this repository that is *not*
built in. It must work through the public interface alone, and it must stay inert until an
operator points ``DATAMIND_PLUGIN_PATH`` at its directory. Every scenario therefore runs in
a fresh interpreter: the process-wide ``ensure_loaded`` flag cannot be unset, so reusing
this process would let one scenario's discovery answer the next one.

The last two suites mirror the baselines the plugin layer promises elsewhere: listing
plugins imports no parser dependency (G7), and the second ``ensure_loaded`` call is a
cheap no-op after the first pass paid the discovery cost.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingestion.plugins import builtin_manifests

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / 'examples' / 'plugins' / 'csv_parser' / 'csv_parser.py'
EXAMPLE_DIR = EXAMPLE.parent
PLUGIN_ENV_KEYS = ('DATAMIND_PLUGIN_PATH', 'DATAMIND_PLUGIN_CONFIG')
# The developer's shell must not decide what these tests discover.
BASE_ENV = {key: value for key, value in os.environ.items() if key not in PLUGIN_ENV_KEYS}
INVENTORY_KEYS = {'name', 'version', 'media_types', 'extensions', 'capabilities',
                  'enabled', 'loaded_from', 'registered_in_contract'}

SCRIPT_DISCOVER = '''\
import json
from ingestion import upload_pipeline
from ingestion.plugins import (default_context, ensure_loaded, loaded_plugins,
                               parser_inventory)

context = ensure_loaded()
manifest = context.parser.lookup_by_extension('.csv')
print(json.dumps({
    'target': upload_pipeline.target('sample.csv'),
    'name': manifest.name,
    'in_parsers': '.csv' in upload_pipeline.PARSERS,
    'loaded_from': [record.loaded_from for record in loaded_plugins()
                    if record.manifest.name == 'csv-example'],
    'inventory': {item['name']: item for item in parser_inventory()
                  if item['name'] == 'csv-example'},
}))
'''

SCRIPT_PARSE = '''\
import hashlib
import json
import sys
from pathlib import Path

from ingestion.plugins import default_context, ensure_loaded

manifest = ensure_loaded().parser.lookup_by_extension('.csv')
result, extra = manifest.resolve()(Path(sys.argv[1]), 'doc-csv-1', 'b' * 64)
payload = result.payload
print(json.dumps({
    'parse_status': payload['parse_status'],
    'extra': extra,
    'texts': [block['text'] for block in payload['blocks']],
    'reading_order': [block['reading_order'] for block in payload['blocks']],
    'block_ids': [block['block_id'] for block in payload['blocks']],
    'ranges': [block['locator']['range'] for block in payload['blocks']],
    'sheets': sorted({block['locator']['sheet'] for block in payload['blocks']}),
    'block_types': sorted({block['block_type'] for block in payload['blocks']}),
    'parser_versions': sorted({block['parser_version'] for block in payload['blocks']}),
    'hashes_ok': all(block['content_hash']
                     == hashlib.sha256(block['text'].encode('utf-8')).hexdigest()
                     for block in payload['blocks']),
}))
'''

SCRIPT_EMPTY = '''\
import json
import sys
from pathlib import Path

from ingestion.plugins import default_context, ensure_loaded

manifest = ensure_loaded().parser.lookup_by_extension('.csv')
result, extra = manifest.resolve()(Path(sys.argv[1]), 'doc-csv-1', 'b' * 64)
payload = result.payload
print(json.dumps({
    'parse_status': payload['parse_status'],
    'blocks': len(payload['blocks']),
    'unsupported_features': payload['unsupported_features'],
    'failed_scope': payload['failed_scope'],
    'parsed_scope': payload['parsed_scope'],
    'extra': extra,
}))
'''

SCRIPT_LAZY = '''\
import sys
import ingestion.upload_pipeline
from ingestion.plugins import parser_inventory

parser_inventory()
banned = {'pymupdf', 'qdrant_client', 'ingestion.ingest', 'ingestion.office',
          'ingestion.cad_adapter', 'docx', 'ezdxf'}
print(','.join(sorted(banned & set(sys.modules))))
'''

SCRIPT_TIMING = '''\
import json
import time
from ingestion.plugins import ensure_loaded

started = time.perf_counter()
first = ensure_loaded()
first_ms = (time.perf_counter() - started) * 1000
started = time.perf_counter()
second = ensure_loaded()
second_ms = (time.perf_counter() - started) * 1000
print(json.dumps({'first_ms': round(first_ms, 3), 'second_ms': round(second_ms, 3),
                  'same_context': first is second}))
'''


def run_script(code, *arguments, plugin_path=None):
    """Run ``code`` in a child interpreter with the example path configured if asked."""
    environment = dict(BASE_ENV, PYTHONPATH=str(ROOT))
    if plugin_path is not None:
        environment['DATAMIND_PLUGIN_PATH'] = str(plugin_path)
    result = subprocess.run([sys.executable, '-c', code, *arguments],
                            capture_output=True, text=True, cwd=str(ROOT),
                            env=environment)
    if result.returncode != 0:
        raise AssertionError(f'child failed ({result.returncode}): {result.stderr}')
    return result.stdout


class DiscoveryTests(unittest.TestCase):
    def test_the_configured_directory_registers_the_example(self):
        payload = json.loads(run_script(SCRIPT_DISCOVER, plugin_path=EXAMPLE_DIR))

        self.assertEqual(payload['target'], '.csv')
        self.assertEqual(payload['name'], 'csv-example')
        self.assertTrue(payload['in_parsers'])
        self.assertEqual(len(payload['loaded_from']), 1)
        # ``loaded_from`` is the resolved absolute path of the discovered module.
        self.assertEqual(Path(payload['loaded_from'][0]).resolve(), EXAMPLE.resolve())

    def test_the_inventory_reports_the_example_as_enabled_but_not_a_contract_adapter(self):
        item = json.loads(run_script(SCRIPT_DISCOVER, plugin_path=EXAMPLE_DIR))['inventory'][
            'csv-example']

        self.assertEqual(set(item), INVENTORY_KEYS)
        self.assertTrue(item['enabled'])
        self.assertFalse(item['registered_in_contract'])
        self.assertEqual(item['media_types'], ['text/csv'])
        self.assertEqual(item['extensions'], ['.csv'])
        self.assertEqual(item['capabilities'], ['native_text'])

    def test_the_example_is_not_a_builtin_and_stays_disabled_without_the_path(self):
        self.assertNotIn('csv-example', [manifest.name for manifest in builtin_manifests()])

        # Without DATAMIND_PLUGIN_PATH the suffix is not served at all.
        code = ('import json\n'
                'from fastapi import HTTPException\n'
                'from ingestion import upload_pipeline\n'
                'try:\n'
                '    print(json.dumps({"suffix": upload_pipeline.target("sample.csv")}))\n'
                'except HTTPException as error:\n'
                '    print(json.dumps({"status": error.status_code, "detail": error.detail}))\n')
        payload = json.loads(run_script(code))
        self.assertEqual(payload['status'], 415)
        self.assertEqual(payload['detail'], {'error_code': 'UNSUPPORTED_MEDIA_TYPE',
                                             'details': {'suffix': '.csv'}})


class ContractOutputTests(unittest.TestCase):
    def test_a_csv_file_becomes_one_table_row_block_per_record(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sample.csv'
            path.write_text('\ufeffname,value\nalpha,1\nbeta,2\n', encoding='utf-8')

            payload = json.loads(run_script(SCRIPT_PARSE, str(path), plugin_path=EXAMPLE_DIR))

        self.assertEqual(payload['parse_status'], 'parsed')
        self.assertEqual(payload['extra'], {'rows': 3})
        self.assertEqual(payload['texts'], ['name,value', 'alpha,1', 'beta,2'])
        self.assertEqual(payload['reading_order'], [0, 1, 2])
        self.assertEqual(payload['block_ids'], ['doc-csv-1:csv:1', 'doc-csv-1:csv:2',
                                                'doc-csv-1:csv:3'])
        self.assertEqual(payload['ranges'], ['A1:Z1', 'A2:Z2', 'A3:Z3'])
        self.assertEqual(payload['sheets'], ['sample'])
        self.assertEqual(payload['block_types'], ['table_row'])
        self.assertEqual(payload['parser_versions'], ['csv-example-1.0.0'])
        self.assertTrue(payload['hashes_ok'])

    def test_an_empty_file_is_unsupported_instead_of_a_fake_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'empty.csv'
            path.write_text('', encoding='utf-8')

            payload = json.loads(run_script(SCRIPT_EMPTY, str(path), plugin_path=EXAMPLE_DIR))

        self.assertEqual(payload['parse_status'], 'unsupported')
        self.assertEqual(payload['blocks'], 0)
        self.assertEqual(payload['unsupported_features'], ['empty_source'])
        self.assertEqual(payload['parsed_scope'], [])
        self.assertEqual(payload['failed_scope'], [{'kind': 'object', 'ref': 'doc-csv-1',
                                                    'status': 'unsupported',
                                                    'detail': 'empty_source'}])
        self.assertEqual(payload['extra'], {'rows': 0})


class LazyImportBaselineTests(unittest.TestCase):
    def test_listing_plugins_imports_no_parser_dependency(self):
        output = run_script(SCRIPT_LAZY).strip()

        print(f'parser dependency intersection: {output!r}')
        self.assertEqual(output, '')


class LoadTimingBaselineTests(unittest.TestCase):
    def test_the_second_ensure_loaded_call_is_a_cheap_no_op(self):
        payload = json.loads(run_script(SCRIPT_TIMING, plugin_path=EXAMPLE_DIR))

        print(f"ensure_loaded timing: first={payload['first_ms']} ms, "
              f"second={payload['second_ms']} ms")
        self.assertTrue(payload['same_context'])
        self.assertLess(payload['second_ms'], 50)


if __name__ == '__main__':
    unittest.main()
