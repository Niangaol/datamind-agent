"""Built-in parser plugin for measurement-JSON records.

This plugin claims the display name ``measurement-json`` (G10), which the upload path has
always reported as the adapter of a ``.json`` upload. It is not one of the contract
adapters: no ``application/json`` handler is registered, because a measurement record is
parsed by ``datamind.cad_dimensions`` and then described as a validated contract result
with an explicit "no locator" gap instead of by an adapter dispatch.

The JSON source is decoded here only to read the identity the record declares; the record
itself is validated by ``cad_dimensions.measurement_from_bytes``, exactly as the
``file_jobs`` measurement target does. Importing this module costs nothing heavy (G7):
``fastapi``, ``datamind.cad_dimensions`` and ``datamind.parse_contract`` are imported
inside :func:`parse` and :func:`_measurement_id` only.
"""
from __future__ import annotations

import json

from datamind.plugin.manifest import ParserPluginManifest

MANIFEST = ParserPluginManifest(
    'measurement-json', '1.0.0', ('application/json',), ('.json',),
    capabilities=('measurement_json',),
    loader='ingestion.plugins.measurement_json_plugin:parse')

INVALID_SOURCE_CODE = 'UPLOAD_MEASUREMENT_SOURCE_INVALID'


def parse(path, document_id, source_version, options=None):
    """Validate one measurement record and describe it as a contract result.

    A measurement has no page/region geometry, so the contract cannot carry a block-level
    locator for it; rather than inventing one, the result declares
    ``unsupported_features=['locator_unavailable']`` and ``parse_status='unsupported'``
    exactly as the OHR-Bench JSON branch of ``ingestion/ingest.py`` does.
    """
    from datamind import cad_dimensions, parse_contract

    options = options or {}
    data = path.read_bytes()
    measurement_id, version = _measurement_id(data)
    record = cad_dimensions.measurement_from_bytes(
        data, measurement_id, version, options.get('provenance'))
    payload = {
        'classification': {'detected_media_type': MANIFEST.media_types[0],
                           'source_name': options.get('filename') or path.name},
        'adapter_capabilities': ['measurement_json'],
        'blocks': [], 'relations': [], 'parsed_scope': [],
        'failed_scope': [{'kind': 'object', 'ref': document_id,
                          'status': 'unsupported', 'detail': 'locator_unavailable'}],
        'quality_findings': [], 'unsupported_features': ['locator_unavailable'],
        'artifact_refs': [],
        'manifest': {'parser_version': 'cad-measurement-adapter-v1',
                     'adapter': MANIFEST.name, 'measurement_id': measurement_id,
                     'record_version': record['source']['record_version']},
        'parse_status': 'unsupported'}
    return parse_contract.validate_parse_result(payload), {
        'measurement_id': measurement_id, 'record_version': record['source']['record_version'],
        'measurements': len(record.get('measurements') or [])}


def _measurement_id(data):
    """Read the measurement identity the JSON declares; the parser re-validates it."""
    from fastapi import HTTPException

    try:
        raw = json.loads(data.decode('utf-8'))
        first = raw['measurements'][0]
        measurement_id, version = first['measurement_id'], raw['record_version']
        if not isinstance(measurement_id, str) or isinstance(version, bool) \
                or not isinstance(version, int):
            raise ValueError('identity')
        return measurement_id, version
    except (ValueError, TypeError, KeyError, IndexError, UnicodeDecodeError):
        raise HTTPException(422, detail={'error_code': INVALID_SOURCE_CODE})
