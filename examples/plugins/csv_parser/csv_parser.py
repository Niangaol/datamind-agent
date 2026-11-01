"""Minimal third-party parser plugin: one CSV record becomes one contract block.

This file is the runnable example of the plugin interface, not a built-in plugin. It is
never in ``ingestion.plugins.builtin_manifests()`` and is only loaded when the operator
points ``DATAMIND_PLUGIN_PATH`` at this directory::

    DATAMIND_PLUGIN_PATH=examples/plugins/csv_parser \\
        .venv/Scripts/python.exe -m ingestion.upload_worker --once

Everything it uses is public: :class:`~datamind.plugin.manifest.ParserPluginManifest` for
the declaration and ``datamind.parse_contract.validate_parse_result`` for the result. It
imports nothing from ``ingestion.plugins`` (no built-in internals) and needs no change to
core parsing code, which is the point of the example.

The declaration's loader deliberately names this file by its own stem
(``'csv_parser:parse'``). Discovery imports the file under a synthetic module name and
rewrites that stem reference, so the plugin directory never has to be on ``sys.path``.
"""
from __future__ import annotations

import csv
import hashlib
import io
from pathlib import Path

from datamind.plugin.manifest import ParserPluginManifest

PARSER_VERSION = 'csv-example-1.0.0'
SOURCE_MEDIA_TYPE = 'text/csv'

MANIFEST = ParserPluginManifest(
    'csv-example', '1.0.0', (SOURCE_MEDIA_TYPE,), ('.csv',),
    capabilities=('native_text',), loader='csv_parser:parse')


def parse(path, document_id, source_version, options=None):
    """Parse one CSV file into one ``table_row`` block per record.

    ``path`` is a :class:`pathlib.Path` to the staged source, ``document_id`` and
    ``source_version`` identify the document revision, and ``options`` is the uniform
    plugin signature's optional mapping (unused here: the CSV reader needs no options).

    Returns the ``(ParseResult, extra)`` pair the upload handler expects, where ``extra``
    contributes ``rows`` to ``quality_detail.analysis``. An empty file is reported as
    ``parse_status='unsupported'`` instead of a fake success: there is nothing to parse and
    the contract requires at least one block for ``'parsed'``.
    """
    # Imported here, not at module top: importing this plugin must stay cheap (G7), and the
    # contract module pulls in fastapi. It is still the public validation entry point.
    from datamind.parse_contract import validate_parse_result

    source = Path(path)
    # utf-8-sig strips a leading BOM if one is present; a file without one is unaffected.
    rows = list(csv.reader(io.StringIO(source.read_text(encoding='utf-8-sig'))))
    if not rows:
        return validate_parse_result({
            'classification': {'detected_media_type': SOURCE_MEDIA_TYPE,
                               'source_name': source.name},
            'adapter_capabilities': ['native_text'],
            'blocks': [],
            'relations': [],
            'parsed_scope': [],
            'failed_scope': [{'kind': 'object', 'ref': document_id,
                              'status': 'unsupported', 'detail': 'empty_source'}],
            'quality_findings': [],
            'unsupported_features': ['empty_source'],
            'artifact_refs': [],
            'manifest': {'parser_version': PARSER_VERSION, 'adapter': MANIFEST.name},
            'parse_status': 'unsupported',
        }), {'rows': 0}

    sheet = source.stem or 'csv'
    blocks = []
    for row, cells in enumerate(rows, start=1):
        text = ','.join(cells)
        blocks.append({
            'block_id': f'{document_id}:csv:{row}',
            'document_id': document_id,
            'source_version': source_version,
            'parser_version': PARSER_VERSION,
            'block_type': 'table_row',
            'text': text,
            'native_attributes': {'cells': list(cells)},
            'reading_order': row - 1,
            'locator': {'locator_kind': 'sheet_range', 'document_id': document_id,
                        'source_version': source_version, 'sheet': sheet,
                        'range': f'A{row}:Z{row}'},
            'quality': {'method': 'native_text'},
            'content_hash': hashlib.sha256(text.encode('utf-8')).hexdigest(),
        })

    payload = {
        'classification': {'detected_media_type': SOURCE_MEDIA_TYPE,
                           'source_name': source.name},
        'adapter_capabilities': ['native_text'],
        'blocks': blocks,
        'relations': [],
        'parsed_scope': [{'kind': 'object', 'ref': document_id, 'status': 'parsed'}],
        'failed_scope': [],
        'quality_findings': [],
        'unsupported_features': [],
        'artifact_refs': [],
        'manifest': {'parser_version': PARSER_VERSION, 'adapter': MANIFEST.name},
        'parse_status': 'parsed',
    }
    return validate_parse_result(payload), {'rows': len(rows)}
