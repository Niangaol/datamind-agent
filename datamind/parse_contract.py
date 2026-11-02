"""Unified parse contracts: ParseResult, SourceLocator, blocks, relations and adapters.

Implements the target contract of the design document (sections 5.2 and 5.4): a parser
adapter returns one `ParseResult` carrying its classification, capabilities, blocks,
relations, parsed/failed scope, quality findings, unsupported features, artifact
references, manifest and a parse status; every block locates its origin through a typed
`SourceLocator`.

This module is the contract and its validation only. It does not read files, call models
or route real ingestion: callers map their own parser output into it. Missing information
is never filled in from defaults or guesses.

Relationship to other contracts:

* The CAD source contract (``cad-contract-v1``, `ingestion/cad_contract.py`, hash-frozen in
  `data/evaluation/frozen/cad_dxf_v1`) is **not** this contract. Its ``Locator``
  (``source_sha256``, ``handle``, ``layer``, ``view_id``, ``coordinate_system``,
  ``layout``) is the source contract; the unified ``cad_entity`` locator is a *projection*
  of it, and the only place that builds the projection is `ingestion/cad_adapter.py`.
  Because a projection may not silently lose evidence, every source field is either
  required or optional here (`CAD_SOURCE_LOCATOR_FIELDS`), and any field the projection
  deliberately discards is declared in `CAD_SOURCE_LOCATOR_DROPPED_FIELDS` (empty today).
* ``cad-contract-v2`` (`ingestion/cad_contract_v2.py`, reader `ingestion/cad_dxf_v2.py`) is
  the wide engineering profile: the same locator plus ``block_path``, the INSERT chain that
  makes a block-internal handle unique (`CAD_SOURCE_LOCATOR_V2_FIELDS`). It is carried in
  the same optional field set, so one locator validation covers both profiles.
* A validated block is also a *storeable* block. Its keys are closed
  (`BLOCK_REQUIRED_FIELDS` + `BLOCK_OPTIONAL_FIELDS`), every key has a column
  (`db/migrations/015_contract_store.sql` and `017_contract_block_shape.sql`), and every
  identifier is checked against that column's width (`BLOCK_COLUMN_WIDTHS`) so a block
  that passes validation cannot then fail its INSERT.
* A `cad_entity` locator under a handle-less (positional) source names its entity with
  `entity_index` + `handle_source='positional'` instead of `handle`. The mode is explicit
  so nothing downstream has to guess whether a missing handle means "unknown" or "the
  source has none"; a positional locator may not carry a handle at all.
"""
from __future__ import annotations

import copy
import hashlib
import re
import zipfile
from pathlib import Path

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_VERSION = 'parse-contract-v1'

PARSE_STATUSES = ('parsed', 'partial', 'needs_review', 'failed', 'unsupported')
INGEST_STATES = ('queued', 'classifying', 'parsing', 'validating', 'indexing', 'ready', 'failed')
INGEST_TRANSITIONS = {'queued': ('classifying', 'failed'), 'classifying': ('parsing', 'failed'),
                      'parsing': ('validating', 'failed'), 'validating': ('indexing', 'failed'),
                      'indexing': ('ready', 'failed'), 'ready': (), 'failed': ()}
RELATION_TYPES = ('precedes', 'contains', 'caption_of', 'header_of', 'continuation_of',
                  'annotates', 'connected_to', 'flows_to', 'references')
RELATION_STATES = ('candidate', 'verified', 'needs_review')
LOCATOR_KINDS = ('pdf_region', 'image_region', 'office_object', 'sheet_range', 'cad_entity',
                 'temporal_segment', 'model_spatial_object')
SCOPE_KINDS = ('page', 'region', 'object')
SCOPE_STATUSES = ('parsed', 'failed', 'gap', 'unsupported')
# Relations are only confirmed from an explicit marker or reviewed structure; naming
# similarity, proximity or adjacency are never sufficient on their own.
INSUFFICIENT_RECOVERY_METHODS = ('name_similarity', 'proximity', 'adjacency', 'guess')

_LOCATOR_COMMON = ('locator_kind', 'document_id', 'source_version')
LOCATOR_REQUIRED_FIELDS = {
    'pdf_region': _LOCATOR_COMMON + ('page_no', 'page_width', 'page_height', 'bbox', 'unit', 'coordinate_system'),
    'image_region': _LOCATOR_COMMON + ('image_id', 'image_width', 'image_height', 'bbox', 'unit', 'coordinate_system'),
    'office_object': _LOCATOR_COMMON + ('part',),
    'sheet_range': _LOCATOR_COMMON + ('sheet',),
    'cad_entity': _LOCATOR_COMMON + ('layout', 'layer', 'model_xy', 'unit'),
    'temporal_segment': _LOCATOR_COMMON + ('start', 'end', 'time_unit', 'time_base'),
    'model_spatial_object': _LOCATOR_COMMON + ('object_id', 'coordinate_reference', 'unit'),
}
# Optional fields are carried through untouched; anything else is refused.
LOCATOR_OPTIONAL_FIELDS = {
    'pdf_region': ('page_label', 'rotation_degrees', 'transform_chain', 'render_sha256'),
    'image_region': ('rotation_degrees', 'transform_chain', 'page_no'),
    'office_object': ('paragraph', 'table', 'cell', 'slide', 'shape', 'workbook', 'sheet'),
    'sheet_range': ('cell', 'range', 'formula', 'hidden'),
    # `source_sha256` is part of the CAD source contract's Locator; it is optional here so
    # projecting a source locator keeps the evidence that ties the block to one drawing
    # revision instead of dropping it (see CAD_SOURCE_LOCATOR_FIELDS).
    # `block_path` is the v2 source locator's INSERT chain (see CAD_SOURCE_LOCATOR_V2_FIELDS):
    # a block-internal handle is only unique together with the references that reach it.
    # `handle` is optional because a handle-less source has no handle to carry: the identity
    # is then the declared positional `entity_index` with `handle_source='positional'` (see
    # CAD_POSITIONAL_LOCATOR_FIELDS). At least one of the two must be present, and positional
    # mode may not carry a handle at all, which `_validate_cad` enforces.
    'cad_entity': ('view', 'view_id', 'render_ref', 'model_z', 'coordinate_system',
                   'source_sha256', 'block_path', 'handle', 'entity_index', 'handle_source'),
    'temporal_segment': ('speaker', 'frame_ref'),
    'model_spatial_object': ('transform', 'view'),
}
# Source locator of the frozen CAD contract (`ingestion/cad_contract.py`), field by field.
# The unified `cad_entity` locator is its projection: `locator_kind`/`layout`/`layer` are
# required here, `handle`/`view_id`/`coordinate_system`/`source_sha256` are optional
# (accepted so the projection can carry them through), while `model_xy` and `unit` are
# added from the drawing's insertion/bbox and INSUNITS because the source locator has no
# such fields. tests/test_parse_contract.py asserts this set is fully covered above.
CAD_SOURCE_LOCATOR_FIELDS = ('locator_kind', 'source_sha256', 'handle', 'layer', 'view_id',
                             'coordinate_system', 'layout')
# The `cad-contract-v2` source locator (`ingestion/cad_contract_v2.py`) is the v1 locator
# plus the INSERT chain of an expanded block entity: `handle` alone is not unique once a
# block is inserted twice, so `(handle, block_path)` is the v2 identity. `block_path` is
# optional here (a Model-space entity has an empty chain) but when it is present it must
# be a bounded list of well-formed handles, validated in `_validate_cad`.
CAD_SOURCE_LOCATOR_V2_FIELDS = CAD_SOURCE_LOCATOR_FIELDS + ('block_path',)
# The declared identity mode of a handle-less source. `handle_source` is explicit so a
# reader that could not read a source handle never leaves the identity implicit: with
# `handle_source='positional'` the locator must carry an `entity_index` and must **not**
# carry a `handle` (a handle there could only be a value the DXF library synthesised).
# Omitting `handle_source` keeps the historical meaning: a `handle`-bearing locator is
# `source` mode, so the frozen v1 locators keep validating unchanged.
CAD_HANDLE_SOURCES = ('source', 'positional')
CAD_POSITIONAL_LOCATOR_FIELDS = ('entity_index', 'handle_source')
MAX_CAD_BLOCK_PATH = 32
# Source fields the projection deliberately discards, with the reason. Empty on purpose:
# nothing in the frozen source locator is dropped today. A new entry here is a contract
# change, not an implementation detail.
CAD_SOURCE_LOCATOR_DROPPED_FIELDS = {}
BLOCK_REQUIRED_FIELDS = ('block_id', 'document_id', 'source_version', 'parser_version', 'block_type',
                         'text', 'native_attributes', 'reading_order', 'locator', 'quality', 'content_hash')
# Block keys a parser may add on top of the required ones. Closed set: the block shape is
# exactly BLOCK_REQUIRED_FIELDS + BLOCK_OPTIONAL_FIELDS, and by migration 017 each of these
# keys has a column in `document_contract_blocks` (F07: validation must not accept a field
# the store has nowhere to put).
BLOCK_OPTIONAL_FIELDS = ('page_no', 'heading_level', 'table_cells', 'formula_latex',
                         'ocr_confidence', 'bbox')
# Column widths of the stores a validated block is written into (document_blocks,
# document_block_locators, document_contract_blocks, document_block_relations). Every one of
# those stores holds `document_id` and `block_id` in a VARCHAR(64), so a longer value would
# pass validation and then fail its INSERT; the width is checked here instead (F06). The
# same rule is shared with the adapters through `require_document_id`, so an adapter refuses
# an over-long id with the same stable reason instead of handing the caller a "parsed"
# result that cannot be stored.
BLOCK_COLUMN_WIDTHS = {'block_id': 64, 'document_id': 64, 'parser_version': 64,
                       'source_version': 128}
DOCUMENT_ID_MAX_LENGTH = BLOCK_COLUMN_WIDTHS['document_id']

RELATION_REQUIRED_FIELDS = ('source_block_id', 'target_block_id', 'relation_type', 'evidence',
                            'recovery_method', 'state')
RELATION_OPTIONAL_FIELDS = ('relation_id', 'note')
RESULT_REQUIRED_FIELDS = ('classification', 'adapter_capabilities', 'blocks', 'relations', 'parsed_scope',
                          'failed_scope', 'quality_findings', 'unsupported_features', 'artifact_refs',
                          'manifest', 'parse_status')
# Block keys the existing ingest/OCR parsers really emit; only these are carried over.
SOURCE_BLOCK_CONTRACT_FIELDS = ('block_id', 'page_no', 'block_type', 'text', 'heading_level', 'table_cells',
                               'formula_latex', 'reading_order', 'ocr_confidence', 'bbox', 'content_hash')

_SHA256 = re.compile(r'^[a-f0-9]{64}$')
_HANDLE = re.compile(r'^[1-9A-F][0-9A-F]{0,31}$')
_CELL = re.compile(r'^[A-Z]{1,3}[1-9][0-9]{0,6}$')
_SHEET_RANGE = re.compile(r'^[A-Z]{1,3}[1-9][0-9]{0,6}(:[A-Z]{1,3}[1-9][0-9]{0,6})?$')
_TIME_UNITS = ('s', 'ms', 'us', 'ns', 'iso8601', 'frame')
_BBOX_EPSILON = 0.5

_ADAPTERS: dict = {}
# Signature sniffing answers "what is this really"; the extension is only a declared hint.
_MAGIC = ((b'%PDF-', 'application/pdf'), (b'\x89PNG\r\n\x1a\n', 'image/png'),
          (b'\xff\xd8\xff', 'image/jpeg'), (b'PK\x03\x04', 'application/zip'))
OCTET_STREAM = 'application/octet-stream'
# 二维图纸：DXF 可原生读取；DWG 是专有二进制格式，需要外部转换器。
DXF_MEDIA_TYPE = 'image/vnd.dxf'
DWG_MEDIA_TYPE = 'image/vnd.dwg'
# OOXML packages are all ZIP containers, so the subtype needs the archive index, not the
# leading bytes. Anything unrecognised stays a plain ZIP instead of being guessed.
OOXML_MEDIA_TYPES = {
    'word/document.xml': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'ppt/presentation.xml': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    'xl/workbook.xml': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
}


def fail(code, **context):
    detail = {'error_code': code}
    if context:
        detail['details'] = context
    raise HTTPException(422, detail=detail)


def design_doc_path():
    """Path of the design document, resolved relative to the repository root."""
    return ROOT / '技术文档' / 'DataMind_Agent_技术设计说明书_V1.3.md'


def _mapping(value, code, subject):
    if not isinstance(value, dict):
        fail(code, subject=subject, reason='not_a_mapping')
    return value


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _sequence(value):
    return isinstance(value, (list, tuple))


def _number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float))


def validate_ingest_state(state):
    """Accept only a documented ingestion state."""
    if state not in INGEST_STATES:
        fail('PARSE_STATUS_INVALID', subject='ingest_state', value=repr(state))
    return state


def require_document_id(document_id, *, code='PARSE_BLOCK_INVALID', subject='block'):
    """Refuse a document id the contract stores cannot hold (VARCHAR(64) everywhere).

    `document_id` is an identifier on every contract table, so accepting a longer one would
    produce a result that validates and then fails to persist. Adapters call this before
    parsing so an over-long id is refused with a stable `document_id_too_long` reason
    instead of a half-written parse. The *family* code belongs to the caller
    (`PARSE_LOCATOR_INVALID` for a locator, `PARSE_BLOCK_INVALID` for a block,
    `PARSE_RESULT_INVALID` for an adapter's own input) while the reason stays the same
    everywhere, so the one stable signal to assert is `reason='document_id_too_long'`.
    """
    if isinstance(document_id, str) and len(document_id) > DOCUMENT_ID_MAX_LENGTH:
        fail(code, subject=subject, reason='document_id_too_long',
             length=len(document_id), limit=DOCUMENT_ID_MAX_LENGTH)
    return document_id


def ingest_step(state, target):
    """Validate one ingestion transition; illegal jumps are refused."""
    validate_ingest_state(state)
    validate_ingest_state(target)
    if target not in INGEST_TRANSITIONS[state]:
        fail('PARSE_STATUS_INVALID', subject='ingest_transition', state=state, target=target)
    return target


class _Record:
    """Small carrier so callers can read attributes and the raw payload."""

    __slots__ = ('payload',)

    def __init__(self, payload):
        self.payload = payload

    def __getattr__(self, name):
        try:
            return self.payload[name]
        except KeyError:
            raise AttributeError(name) from None


class SourceLocator(_Record):
    __slots__ = ()


class DocumentBlock(_Record):
    __slots__ = ()


class BlockRelation(_Record):
    __slots__ = ()


class ParseResult(_Record):
    __slots__ = ()


def _validate_bbox(bbox, width, height, subject):
    if not _sequence(bbox) or len(bbox) != 4 or any(not _number(v) for v in bbox):
        fail('PARSE_LOCATOR_INVALID', subject=subject, reason='bbox_shape')
    x0, y0, x1, y1 = (float(v) for v in bbox)
    if x0 >= x1 or y0 >= y1:
        fail('PARSE_LOCATOR_INVALID', subject=subject, reason='bbox_inverted')
    if (x0 < -_BBOX_EPSILON or y0 < -_BBOX_EPSILON
            or x1 > float(width) + _BBOX_EPSILON or y1 > float(height) + _BBOX_EPSILON):
        fail('PARSE_LOCATOR_INVALID', subject=subject, reason='bbox_out_of_page')
    return [x0, y0, x1, y1]


def _validate_region(payload, kind):
    subject = f'locator.{kind}'
    if kind == 'pdf_region':
        page_no = payload.get('page_no')
        if isinstance(page_no, bool) or not isinstance(page_no, int) or page_no < 1:
            fail('PARSE_LOCATOR_INVALID', subject=subject, reason='page_no')
        width, height = payload.get('page_width'), payload.get('page_height')
    else:
        width, height = payload.get('image_width'), payload.get('image_height')
        if not _text(payload.get('image_id')):
            fail('PARSE_LOCATOR_INVALID', subject=subject, reason='image_id')
    if not _number(width) or not _number(height) or float(width) <= 0 or float(height) <= 0:
        fail('PARSE_LOCATOR_INVALID', subject=subject, reason='page_size')
    _validate_bbox(payload.get('bbox'), width, height, subject)
    if payload.get('unit') not in ('point', 'px'):
        fail('PARSE_LOCATOR_INVALID', subject=subject, reason='unit')
    if payload.get('coordinate_system') not in ('top-left', 'bottom-left'):
        fail('PARSE_LOCATOR_INVALID', subject=subject, reason='coordinate_system')
    rotation = payload.get('rotation_degrees', 0)
    if not _number(rotation) or not -360.0 <= float(rotation) <= 360.0:
        fail('PARSE_LOCATOR_INVALID', subject=subject, reason='rotation_degrees')


def _validate_cad(payload):
    """Validate a `cad_entity` locator under either declared identity mode.

    The historical rule is unchanged for a handle-bearing locator: a `handle` must be a
    well-formed DXF handle (`[1-9A-F][0-9A-F]*`). The new positional mode exists for a
    source that carries no handle at all: the locator then declares
    `handle_source='positional'`, **must** carry an `entity_index` (int >= 1) and **must
    not** carry a `handle`, because any handle there could only be a value the DXF library
    synthesised for a file that has none. A locator that names neither a handle nor an
    index is refused: it would locate nothing. Leaving `handle_source` out keeps the old
    meaning exactly, so every existing v1/v2 locator still validates unchanged.
    """
    subject = 'locator.cad_entity'
    if not _text(payload.get('layout')) or not _text(payload.get('layer')):
        fail('PARSE_LOCATOR_INVALID', subject=subject, reason='layout_or_layer')
    handle = payload.get('handle')
    entity_index = payload.get('entity_index')
    declared = payload.get('handle_source')
    if declared is not None and declared not in CAD_HANDLE_SOURCES:
        fail('PARSE_LOCATOR_INVALID', subject=subject, reason='handle_source', value=repr(declared))
    if entity_index is not None and (isinstance(entity_index, bool)
                                     or not isinstance(entity_index, int) or entity_index < 1):
        fail('PARSE_LOCATOR_INVALID', subject=subject, reason='entity_index',
             value=repr(entity_index))
    if declared == 'positional':
        # The mode is explicit, so the report names exactly what positional mode needs.
        if handle is not None:
            fail('PARSE_LOCATOR_INVALID', subject=subject, reason='positional_handle_forbidden')
        if entity_index is None:
            fail('PARSE_LOCATOR_INVALID', subject=subject, reason='entity_index')
    elif declared == 'source':
        if not isinstance(handle, str) or not _HANDLE.fullmatch(handle):
            fail('PARSE_LOCATOR_INVALID', subject=subject, reason='handle')
    elif handle is None:
        # No mode declared and no handle: the only possible identity is a positional index.
        if entity_index is None:
            fail('PARSE_LOCATOR_INVALID', subject=subject, reason='handle_or_entity_index')
    elif not isinstance(handle, str) or not _HANDLE.fullmatch(handle):
        fail('PARSE_LOCATOR_INVALID', subject=subject, reason='handle')
    mode = declared or ('source' if handle is not None else 'positional')
    xy = payload.get('model_xy')
    if not _sequence(xy) or len(xy) != 2 or any(not _number(v) for v in xy):
        fail('PARSE_LOCATOR_INVALID', subject=subject, reason='model_xy')
    if not _text(payload.get('unit')):
        fail('PARSE_LOCATOR_INVALID', subject=subject, reason='unit')
    source_sha256 = payload.get('source_sha256')
    if source_sha256 is not None and (not isinstance(source_sha256, str)
                                      or not _SHA256.fullmatch(source_sha256)):
        fail('PARSE_LOCATOR_INVALID', subject=subject, reason='source_sha256')
    block_path = payload.get('block_path')
    if block_path is not None:
        if not _sequence(block_path) or len(block_path) > MAX_CAD_BLOCK_PATH:
            fail('PARSE_LOCATOR_INVALID', subject=subject, reason='block_path')
        for item in block_path:
            if not isinstance(item, str) or not _HANDLE.fullmatch(item):
                fail('PARSE_LOCATOR_INVALID', subject=subject, reason='block_path_handle', value=repr(item))
        if len(set(block_path)) != len(block_path):
            # A repeated reference would mean the entity is inside the same INSERT twice,
            # which cannot happen in a real INSERT chain and would make the key ambiguous.
            fail('PARSE_LOCATOR_INVALID', subject=subject, reason='block_path_cycle')
        if mode == 'positional' and block_path:
            # `block_path` can only hold handles; a positional locator must not smuggle one.
            fail('PARSE_LOCATOR_INVALID', subject=subject, reason='positional_block_path_forbidden')


def _validate_office(payload):
    subject = 'locator.office_object'
    if not _text(payload.get('part')):
        fail('PARSE_LOCATOR_INVALID', subject=subject, reason='part')
    anchors = [key for key in ('paragraph', 'table', 'slide', 'shape') if payload.get(key) is not None]
    if not anchors:
        fail('PARSE_LOCATOR_INVALID', subject=subject, reason='object_anchor')
    for key in anchors:
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            fail('PARSE_LOCATOR_INVALID', subject=subject, reason=key)


def _validate_sheet(payload):
    subject = 'locator.sheet_range'
    if not _text(payload.get('sheet')):
        fail('PARSE_LOCATOR_INVALID', subject=subject, reason='sheet')
    cell, cell_range = payload.get('cell'), payload.get('range')
    if cell is None and cell_range is None:
        fail('PARSE_LOCATOR_INVALID', subject=subject, reason='cell_or_range')
    if cell is not None and (not _text(cell) or not _CELL.fullmatch(cell.strip().upper())):
        fail('PARSE_LOCATOR_INVALID', subject=subject, reason='cell')
    if cell_range is not None and (not _text(cell_range) or not _SHEET_RANGE.fullmatch(cell_range.strip().upper())):
        fail('PARSE_LOCATOR_INVALID', subject=subject, reason='range')


def _validate_temporal(payload):
    subject = 'locator.temporal_segment'
    start, end = payload.get('start'), payload.get('end')
    if not _number(start) or not _number(end) or float(start) >= float(end):
        fail('PARSE_LOCATOR_INVALID', subject=subject, reason='interval')
    if str(payload.get('time_unit', '')).lower() not in _TIME_UNITS:
        fail('PARSE_LOCATOR_INVALID', subject=subject, reason='time_unit')
    if not _text(payload.get('time_base')):
        fail('PARSE_LOCATOR_INVALID', subject=subject, reason='time_base')


def _validate_spatial(payload):
    subject = 'locator.model_spatial_object'
    if not _text(payload.get('object_id')) or not _text(payload.get('coordinate_reference')):
        fail('PARSE_LOCATOR_INVALID', subject=subject, reason='object_id_or_reference')
    if not _text(payload.get('unit')):
        fail('PARSE_LOCATOR_INVALID', subject=subject, reason='unit')


_LOCATOR_VALIDATORS = {'pdf_region': lambda p: _validate_region(p, 'pdf_region'),
                       'image_region': lambda p: _validate_region(p, 'image_region'),
                       'office_object': _validate_office, 'sheet_range': _validate_sheet,
                       'cad_entity': _validate_cad, 'temporal_segment': _validate_temporal,
                       'model_spatial_object': _validate_spatial}


def validate_locator(payload):
    """Validate a typed source locator and return it.

    Required fields per kind come from the design document; unknown keys are refused so a
    caller cannot smuggle an unvalidated coordinate space into a citation.
    """
    data = _mapping(payload, 'PARSE_LOCATOR_INVALID', 'locator')
    kind = data.get('locator_kind')
    if kind not in LOCATOR_KINDS:
        fail('PARSE_LOCATOR_INVALID', subject='locator', reason='locator_kind', value=repr(kind))
    required = LOCATOR_REQUIRED_FIELDS[kind]
    for field in required:
        if field not in data or data[field] is None:
            fail('PARSE_LOCATOR_INVALID', subject=f'locator.{kind}', reason='missing_field', field=field)
    allowed = set(required) | set(LOCATOR_OPTIONAL_FIELDS[kind])
    unknown = sorted(set(data) - allowed)
    if unknown:
        fail('PARSE_LOCATOR_INVALID', subject=f'locator.{kind}', reason='unknown_field', fields=unknown)
    for field in _LOCATOR_COMMON[1:]:
        if not _text(data[field]):
            fail('PARSE_LOCATOR_INVALID', subject=f'locator.{kind}', reason=field)
    # A locator is stored per block (`document_block_locators.document_id`), so the same
    # column bound as for blocks applies here.
    require_document_id(data['document_id'], code='PARSE_LOCATOR_INVALID', subject=f'locator.{kind}')
    _LOCATOR_VALIDATORS[kind](data)
    return SourceLocator(copy.deepcopy(dict(data)))


def pdf_region_locator(*, document_id, source_version, page_no, bbox, page_width, page_height,
                       rotation_degrees, page_label=None, transform_chain=None):
    """Build and validate one `pdf_region` locator (points, page rotated explicitly)."""
    payload = {'locator_kind': 'pdf_region', 'document_id': document_id,
               'source_version': source_version, 'page_no': page_no, 'bbox': list(bbox),
               'page_width': page_width, 'page_height': page_height, 'unit': 'point',
               'coordinate_system': 'top-left', 'rotation_degrees': rotation_degrees}
    if page_label is not None:
        payload['page_label'] = page_label
    if transform_chain is not None:
        payload['transform_chain'] = transform_chain
    return validate_locator(payload)


def validate_block(payload):
    """Validate one document block, including its locator.

    The block shape is closed: a key outside ``BLOCK_REQUIRED_FIELDS`` +
    ``BLOCK_OPTIONAL_FIELDS`` is refused rather than accepted and then dropped by the
    store, and every identifier is checked against its destination column's width so
    "the contract accepted it" implies "the store can hold it".
    """
    data = _mapping(payload, 'PARSE_BLOCK_INVALID', 'block')
    for field in BLOCK_REQUIRED_FIELDS:
        if field not in data:
            fail('PARSE_BLOCK_INVALID', subject='block', reason='missing_field', field=field)
    unknown = sorted(set(data) - set(BLOCK_REQUIRED_FIELDS) - set(BLOCK_OPTIONAL_FIELDS))
    if unknown:
        fail('PARSE_BLOCK_INVALID', subject='block', reason='unknown_field', fields=unknown)
    if not _text(data['block_id']) or not _text(data['document_id']) or not _text(data['source_version']):
        fail('PARSE_BLOCK_INVALID', subject='block', reason='identity')
    for field, limit in BLOCK_COLUMN_WIDTHS.items():
        if not isinstance(data[field], str):
            fail('PARSE_BLOCK_INVALID', subject='block', reason='not_text', field=field)
        if len(data[field]) > limit:
            fail('PARSE_BLOCK_INVALID', subject='block', reason=f'{field}_too_long',
                 field=field, length=len(data[field]), limit=limit)
    if not _text(data['block_type']) or not isinstance(data['text'], str):
        fail('PARSE_BLOCK_INVALID', subject='block', reason='type_or_text')
    if not isinstance(data['reading_order'], int) or isinstance(data['reading_order'], bool) \
            or data['reading_order'] < 0:
        fail('PARSE_BLOCK_INVALID', subject='block', reason='reading_order')
    if not _text(data['content_hash']) or not _SHA256.fullmatch(data['content_hash']):
        fail('PARSE_BLOCK_INVALID', subject='block', reason='content_hash')
    if not isinstance(data['native_attributes'], dict) or not isinstance(data['quality'], dict):
        fail('PARSE_BLOCK_INVALID', subject='block', reason='native_attributes_or_quality')
    locator = validate_locator(data['locator'])
    if locator.payload['document_id'] != data['document_id']:
        fail('PARSE_BLOCK_INVALID', subject='block', reason='locator_document_mismatch')
    return DocumentBlock(copy.deepcopy(dict(data)))


def validate_relation(payload, block_ids):
    """Validate one block-to-block relation against the block ids of its result."""
    data = _mapping(payload, 'PARSE_RELATION_INVALID', 'relation')
    for field in RELATION_REQUIRED_FIELDS:
        if field not in data:
            fail('PARSE_RELATION_INVALID', subject='relation', reason='missing_field', field=field)
    known = set(block_ids or ())
    for role in ('source_block_id', 'target_block_id'):
        value = data[role]
        if not _text(value):
            fail('PARSE_RELATION_INVALID', subject='relation', reason=role)
        if known and value not in known:
            fail('PARSE_RELATION_INVALID', subject='relation', reason='unknown_endpoint', block_id=value)
    if data['relation_type'] not in RELATION_TYPES:
        fail('PARSE_RELATION_INVALID', subject='relation', reason='relation_type', value=repr(data['relation_type']))
    if data['state'] not in RELATION_STATES:
        fail('PARSE_RELATION_INVALID', subject='relation', reason='state', value=repr(data['state']))
    if not _text(data['evidence']):
        fail('PARSE_RELATION_INVALID', subject='relation', reason='evidence')
    method = data['recovery_method']
    if not _text(method):
        fail('PARSE_RELATION_INVALID', subject='relation', reason='recovery_method')
    if method in INSUFFICIENT_RECOVERY_METHODS:
        fail('PARSE_RELATION_INVALID', subject='relation', reason='insufficient_recovery_method', method=method)
    unknown = sorted(set(data) - set(RELATION_REQUIRED_FIELDS) - set(RELATION_OPTIONAL_FIELDS))
    if unknown:
        fail('PARSE_RELATION_INVALID', subject='relation', reason='unknown_field', fields=unknown)
    return BlockRelation(copy.deepcopy(dict(data)))


def _validate_scope(records, field):
    if not _sequence(records):
        fail('PARSE_RESULT_INVALID', subject=field, reason='not_a_sequence')
    checked = []
    for item in records:
        data = _mapping(item, 'PARSE_RESULT_INVALID', field)
        if set(data) - {'kind', 'ref', 'status', 'detail'} or not {'kind', 'ref', 'status'} <= set(data):
            fail('PARSE_RESULT_INVALID', subject=field, reason='shape')
        if data['kind'] not in SCOPE_KINDS or data['status'] not in SCOPE_STATUSES:
            fail('PARSE_RESULT_INVALID', subject=field, reason='value')
        checked.append(copy.deepcopy(dict(data)))
    return checked


def validate_parse_result(payload):
    """Validate a full parse result; nothing is defaulted or invented."""
    data = _mapping(payload, 'PARSE_RESULT_INVALID', 'parse_result')
    for field in RESULT_REQUIRED_FIELDS:
        if field not in data:
            fail('PARSE_RESULT_INVALID', subject='parse_result', reason='missing_field', field=field)
    unknown = sorted(set(data) - set(RESULT_REQUIRED_FIELDS) - {'ingest_state', 'contract_version'})
    if unknown:
        fail('PARSE_RESULT_INVALID', subject='parse_result', reason='unknown_field', fields=unknown)
    if data['parse_status'] not in PARSE_STATUSES:
        fail('PARSE_RESULT_INVALID', subject='parse_result', reason='parse_status', value=repr(data['parse_status']))
    if not isinstance(data['classification'], dict) or not data['classification']:
        fail('PARSE_RESULT_INVALID', subject='parse_result.classification', reason='shape')
    if not _sequence(data['adapter_capabilities']) or not all(_text(item) for item in data['adapter_capabilities']):
        fail('PARSE_RESULT_INVALID', subject='parse_result.adapter_capabilities', reason='shape')
    if not isinstance(data['manifest'], dict) or not data['manifest']:
        fail('PARSE_RESULT_INVALID', subject='parse_result.manifest', reason='shape')
    for field in ('quality_findings', 'unsupported_features', 'artifact_refs'):
        if not _sequence(data[field]):
            fail('PARSE_RESULT_INVALID', subject=f'parse_result.{field}', reason='not_a_sequence')
    if not _sequence(data['blocks']):
        fail('PARSE_RESULT_INVALID', subject='parse_result.blocks', reason='not_a_sequence')
    if not data['blocks'] and data['parse_status'] not in ('unsupported', 'failed'):
        fail('PARSE_RESULT_INVALID', subject='parse_result.blocks', reason='empty_for_status')
    blocks = [validate_block(item) for item in data['blocks']]
    ids = [item.payload['block_id'] for item in blocks]
    if len(set(ids)) != len(ids):
        fail('PARSE_RESULT_INVALID', subject='parse_result.blocks', reason='duplicate_block_id')
    if not _sequence(data['relations']):
        fail('PARSE_RESULT_INVALID', subject='parse_result.relations', reason='not_a_sequence')
    relations = [validate_relation(item, ids) for item in data['relations']]
    result = {'classification': copy.deepcopy(data['classification']),
              'adapter_capabilities': list(data['adapter_capabilities']),
              'blocks': [item.payload for item in blocks],
              'relations': [item.payload for item in relations],
              'parsed_scope': _validate_scope(data['parsed_scope'], 'parsed_scope'),
              'failed_scope': _validate_scope(data['failed_scope'], 'failed_scope'),
              'quality_findings': copy.deepcopy(data['quality_findings']),
              'unsupported_features': copy.deepcopy(data['unsupported_features']),
              'artifact_refs': copy.deepcopy(data['artifact_refs']),
              'manifest': copy.deepcopy(data['manifest']),
              'parse_status': data['parse_status'],
              'contract_version': CONTRACT_VERSION}
    if data.get('ingest_state') is not None:
        result['ingest_state'] = validate_ingest_state(data['ingest_state'])
    return ParseResult(result)


def _normalize_media_type(media_type):
    if not _text(media_type) or '/' not in media_type:
        fail('PARSE_ADAPTER_UNKNOWN', subject='adapter', reason='invalid_media_type', value=repr(media_type))
    top, _, sub = media_type.strip().lower().partition('/')
    if not top or not sub:
        fail('PARSE_ADAPTER_UNKNOWN', subject='adapter', reason='invalid_media_type', value=repr(media_type))
    return f'{top}/{sub}'


def register_adapter(name, *, media_types, capabilities, handler=None):
    """Register an in-process adapter contract and its optional dispatch handler.

    A `handler(**kwargs)` performs the actual mapping and must return a ParseResult
    payload; `dispatch` validates whatever it returns. Without a handler the adapter can
    still declare capabilities, but a dispatch to it is refused.
    """
    if not _text(name) or not _sequence(media_types) or not _sequence(capabilities):
        fail('PARSE_RESULT_INVALID', subject='adapter', reason='shape')
    if handler is not None and not callable(handler):
        fail('PARSE_RESULT_INVALID', subject='adapter', reason='handler_must_be_callable')
    if not media_types or not capabilities:
        fail('PARSE_RESULT_INVALID', subject='adapter', reason='empty_sequence')
    normalized = tuple(_normalize_media_type(item) for item in media_types)
    if not all(_text(item) for item in capabilities):
        fail('PARSE_RESULT_INVALID', subject='adapter', reason='capability_must_be_text')
    record = {'name': name.strip(), 'media_types': normalized,
              'capabilities': tuple(capabilities), 'handler': handler}
    for media_type in normalized:
        if media_type in _ADAPTERS:
            fail('PARSE_RESULT_INVALID', subject='adapter', reason='media_type_already_registered',
                 media_type=media_type)
    for media_type in normalized:
        _ADAPTERS[media_type] = record
    return record


def _unregister_adapter(name):
    """Free every media type held by the adapter record called ``name``.

    Internal counterpart of `register_adapter`, used by tests that need to undo a
    registration in the same process: it frees the media types the record currently
    claims, so a re-registration under the same name succeeds. It is **not** part of the
    parser plugin registry's load/unload path - that registry releases a plugin's
    declarations through the disposer `ParserRegistry.register` returns. Only the named
    record's media types are removed - a sibling whose name merely starts with the same
    prefix stays registered - and the removed record is returned, or None when nothing has
    that name. Running it twice is a no-op the second time.
    """
    target = name.strip() if isinstance(name, str) else name
    media_types = [media_type for media_type, record in _ADAPTERS.items()
                   if record['name'] == target]
    if not media_types:
        return None
    record = _ADAPTERS.pop(media_types[0])
    for media_type in media_types[1:]:
        del _ADAPTERS[media_type]
    return record


def _ooxml_subtype(path):
    """Distinguish an OOXML subtype from a plain ZIP by its package index."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
    except (zipfile.BadZipFile, OSError):
        return None
    for marker, media_type in OOXML_MEDIA_TYPES.items():
        if marker in names:
            return media_type
    return None


def detect_media_type(path):
    """Sniff a file signature; unknown content is reported, never guessed from a name."""
    try:
        with open(path, 'rb') as handle:
            head = handle.read(512)
    except OSError:
        fail('PARSE_MEDIA_UNREADABLE', path=str(path))
    if head.startswith(b'AutoCAD Binary DXF'):
        return DXF_MEDIA_TYPE
    if head[:4] == b'AC10' and b'SECTION' not in head.upper():
        # A DWG also starts with its version tag; without an external converter it is
        # reported as DWG rather than being mistaken for a readable DXF.
        return DWG_MEDIA_TYPE
    stripped = head.lstrip()[:64].upper()
    if stripped.startswith(b'0') and b'SECTION' in head.upper():
        return DXF_MEDIA_TYPE
    for magic, media_type in _MAGIC:
        if head.startswith(magic):
            if media_type == 'application/zip':
                return _ooxml_subtype(path) or media_type
            return media_type
    return OCTET_STREAM


def dispatch(media_type, **kwargs):
    """Route a media type to its registered handler and validate the returned result.

    This is the seam a new format plugs into: register an adapter with a handler and the
    router will find it; an unregistered type or a handler-less adapter is refused rather
    than silently producing an empty parse.
    """
    record = adapter_for(media_type)
    handler = record.get('handler')
    if handler is None:
        fail('PARSE_ADAPTER_UNSUPPORTED', subject='adapter', adapter=record['name'],
             reason='no_handler')
    try:
        payload = handler(**kwargs)
    except HTTPException:
        raise
    except TypeError as error:
        fail('PARSE_RESULT_INVALID', subject='adapter', adapter=record['name'],
             reason='handler_signature', detail=str(error))
    except Exception:
        fail('PARSE_ADAPTER_UNSUPPORTED', subject='adapter', adapter=record['name'],
             reason='handler_failed')
    return validate_parse_result(payload)


def adapter_for(media_type):
    """Look up a registered adapter; an unregistered type is refused, never empty-parsed."""
    record = _ADAPTERS.get(_normalize_media_type(media_type))
    if record is None:
        fail('PARSE_ADAPTER_UNKNOWN', subject='adapter', media_type=media_type)
    return record


def _adapter(name):
    for record in {id(item): item for item in _ADAPTERS.values()}.values():
        if record['name'] == name:
            return record
    return None


def capabilities(adapter_name=None):
    """Capabilities of one adapter, or a snapshot of every registered adapter."""
    if adapter_name is None:
        return {record['name']: record['capabilities'] for record in _ADAPTERS.values()}
    record = _adapter(adapter_name)
    if record is None:
        fail('PARSE_ADAPTER_UNKNOWN', subject='adapter', target=adapter_name)
    return record['capabilities']


def registered_adapters():
    return tuple(sorted({record['name'] for record in _ADAPTERS.values()}))


def supported_media_types():
    return tuple(sorted(_ADAPTERS))


def supports_capability(target, capability):
    record = _adapter(target) if target in registered_adapters() else _ADAPTERS.get(str(target).lower())
    return bool(record and _text(capability) and capability in record['capabilities'])


def require_capability(target, capability):
    """Require a capability, distinguishing an unknown adapter from a missing one."""
    if not _text(capability):
        fail('PARSE_ADAPTER_UNSUPPORTED', subject='adapter', reason='capability_must_be_text')
    record = _adapter(target)
    if record is None:
        fail('PARSE_ADAPTER_UNKNOWN', subject='adapter', target=target)
    if capability not in record['capabilities']:
        fail('PARSE_ADAPTER_UNSUPPORTED', subject='adapter', adapter=record['name'], capability=capability)
    return capability


def _page_size(page_sizes, page_no):
    if isinstance(page_sizes, dict):
        value = page_sizes.get(page_no, page_sizes.get(str(page_no)))
    else:
        value = page_sizes[page_no - 1] if _sequence(page_sizes) and 0 < page_no <= len(page_sizes) else None
    if not _sequence(value) or len(value) != 2 or any(not _number(v) for v in value):
        fail('PARSE_LOCATOR_INVALID', subject='pdf_region', reason='missing_page_size', page_no=page_no)
    return float(value[0]), float(value[1])


def _page_optional(table, page_no, default=None):
    if not isinstance(table, dict):
        return default
    return table.get(page_no, table.get(str(page_no), default))


def _pdf_block_identifier(document_id, page_no, index):
    """Return a stable, fixed-length id for a block the parser did not name itself.

    ``{document_id}-p{page}-b{index}`` reaches 64 characters for a document_id of 59 and is
    then no longer injective, so distinct blocks would share one id and the result would be
    refused as a duplicate. The identity is hashed instead of truncated, exactly as
    `ingestion/office.py:_block_identifier` and `ingestion/cad_adapter.py` do.
    """
    material = f'{document_id}\x1f{page_no}\x1f{index}'.encode('utf-8')
    return 'pdf-' + hashlib.sha256(material).hexdigest()[:32]


def from_pdf_blocks(blocks, *, page_sizes, parser_version, document_id=None, source_version=None,
                    page_rotations=None, page_transforms=None, page_labels=None):
    """Map real ingest/OCR block dictionaries into validated contract blocks.

    Only keys the parsers actually emit are carried over; a missing page size, bbox or
    document identity is refused instead of being guessed.
    """
    if not _sequence(blocks):
        fail('PARSE_BLOCK_INVALID', subject='blocks', reason='not_a_sequence')
    if not _text(parser_version):
        fail('PARSE_BLOCK_INVALID', subject='blocks', reason='parser_version')
    mapped = []
    for index, source in enumerate(blocks):
        data = _mapping(source, 'PARSE_BLOCK_INVALID', 'block')
        page_no = data.get('page_no')
        if isinstance(page_no, bool) or not isinstance(page_no, int) or page_no < 1:
            fail('PARSE_BLOCK_INVALID', subject='block', reason='page_no')
        width, height = _page_size(page_sizes, page_no)
        bbox = data.get('bbox')
        if not _sequence(bbox) or len(bbox) != 4:
            fail('PARSE_BLOCK_INVALID', subject='block', reason='bbox')
        block_document = document_id or data.get('document_id')
        block_source = source_version or data.get('source_version')
        if not _text(block_document) or not _text(block_source):
            fail('PARSE_BLOCK_INVALID', subject='block', reason='document_identity')
        # Checked here rather than only inside the locator so the block family answers for
        # the block's own document identity (the locator reports the same reason under
        # PARSE_LOCATOR_INVALID when it is validated on its own).
        require_document_id(block_document)
        identifier = data.get('block_id') or _pdf_block_identifier(block_document, page_no, index)
        locator_payload = pdf_region_locator(
            document_id=block_document, source_version=block_source, page_no=page_no, bbox=bbox,
            page_width=width, page_height=height,
            rotation_degrees=_page_optional(page_rotations, page_no, 0),
            page_label=_page_optional(page_labels, page_no),
            transform_chain=_page_optional(page_transforms, page_no)).payload
        contract_block = {'block_id': identifier, 'document_id': block_document,
                          'source_version': block_source, 'parser_version': parser_version,
                          'block_type': data.get('block_type') or 'paragraph',
                          'text': data.get('text') if isinstance(data.get('text'), str) else '',
                          'native_attributes': {}, 'reading_order': index,
                          'locator': locator_payload, 'quality': {},
                          'content_hash': data.get('content_hash') or _content_hash(
                              block_document, block_source, parser_version, page_no, identifier,
                              data.get('text') if isinstance(data.get('text'), str) else '', bbox)}
        # Carry the parser's own columns through verbatim so nothing is silently dropped.
        for key in SOURCE_BLOCK_CONTRACT_FIELDS:
            if key in data and key not in contract_block:
                contract_block[key] = copy.deepcopy(data[key])
        mapped.append(validate_block(contract_block).payload)
    return mapped


def _content_hash(document_id, source_version, parser_version, page_no, block_id, text, bbox):
    payload = repr([document_id, source_version, parser_version, page_no, block_id, text,
                    [float(v) for v in bbox]]).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()
