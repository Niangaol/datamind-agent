"""WP3 innovation metric: a frozen binding-trap set plus the ``plain`` / ``bind_only`` / ``full``
three-arm ablation.

What this module is
-------------------
The first round of this project never delivered WP3: there was no frozen trap set, no three-arm
comparison and therefore no number for ``绑定准确率`` / **``错误绑定率``** / conflict P/R /
mis-repair rate.  This module builds exactly that, and nothing else:

* :func:`generate_dataset` deterministically *materialises* the frozen trap set from the label
  table in :func:`_specs`.  Labels (``expected_binding`` / ``expected_answerable`` /
  ``expected_conflict`` / ``expected_resolution``) are written **before** any arm exists and are
  pinned by a digest (:data:`TRAPS_SHA256`); :func:`check_frozen` regenerates the set and refuses
  when the checked-in file differs from the generator (that is the ``--check`` discipline: a
  loosened label is an error, not a quiet retune).
* Three arms answer the **same frozen cases**:

  - ``plain``      - stitch the SQL result rows and the document snippets together and answer with
    the first numeric source, taking its unit/version/scope *verbatim*.  No binding layer, no
    verification: every claim is emitted at ``confidence=high``;
  - ``bind_only``  - canonicalise the binding (approved entity alias, canonical unit, parsed time
    scope, declared revision) but **do not verify** (no revision-applicability check, no unit
    comparability check, no cross-source comparison, no repair);
  - ``full``       - binding + verification + at most **one** accounted repair.

* The deterministic answerer is a **stub** (no model call; ``model_calls == 0``).  Every number
  produced here describes *mechanism behaviour under the frozen definitions*, never a real model's
  accuracy.  That boundary is written into the report and into ``docs/BINDING_ABLATION.md``.

Metric arithmetic follows :mod:`datamind.ablation`: a zero denominator reports ``None`` (printed as
``N/A``), never ``0``.  Binding/conflict semantics are reused from :mod:`datamind.binding`,
:mod:`datamind.evidence_graph` and :mod:`datamind.cross_source`.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from . import binding as binding_mod
from . import cross_source as cs

ROOT = Path(__file__).resolve().parents[1]
TRAPS_PATH = ROOT / 'data/evaluation/binding_traps_v1.json'

BINDING_EVAL_VERSION = 'binding-eval-v1'
SUITE_VERSION = '1.0.0'
ARMS = ('plain', 'bind_only', 'full')
TRAP_KINDS = ('cross_version', 'unit_mismatch', 'time_scope_mismatch', 'doc_db_opposite')
RESOLUTIONS = ('resolved_by_authority', 'unresolved', 'needs_review')
TRAP_KIND_DEFINITIONS = {
    'cross_version': '同对象跨版本：V1/V2 数值或适用范围不同，必须按登记的有效期窗口绑定版本',
    'unit_mismatch': '单位不同或不可归一：英寸/毫米、degC/degF、无法归一的自定义单位',
    'time_scope_mismatch': '时间口径不同：自然月 / 季度 / 生产批次期间',
    'doc_db_opposite': '文档与库数值相反：同一对象同一版本两侧数值矛盾',
}
RESOLUTION_VOCABULARY = {
    'resolved_by_authority': '由登记依据确定性收敛：source_authority_rules 登记规则、登记版本有效期窗口、'
                             '或登记单位等价换算——三者都是已登记、确定、不猜测的依据',
    'unresolved': '同一 entity+metric+version 的可比数值互相矛盾且没有登记权威：必须保留双方原值、拒答',
    'needs_review': '两侧不可比（单位不可归一、时间口径不同、版本适用性未知、时区缺失）：不得合并，标记待复核',
}
LABEL_POLICY = ('标签在任何臂实现之前冻结；expected_binding 的 entity/unit/time_scope 由主声称的 fixture '
                '机械导出，version 与 answerable/conflict/resolution 由人工声明；--check 重新生成并逐字节比对，'
                '任何放宽都会以 BINDING_TRAPS_LABELS_LOOSENED 报错。')

#: Digest of the frozen label table (see :func:`dataset_digest`).  A hand-edited trap file no
#: longer matches it, so the evaluator refuses to run instead of reporting numbers against
#: different labels.
TRAPS_SHA256 = '1fdfa89be8eee18fc166a906250639a8f01b89b5ef663c831c335fa9faf0b281'

_HEX_SHA256 = re.compile(r'^[0-9a-f]{64}$')


# ------------------------------------------------------------------ small helpers

def _sha(text: str) -> str:
    return hashlib.sha256(str(text).encode('utf-8')).hexdigest()


def _canonical(payload) -> str:
    return json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      default=str)


def dataset_digest(payload) -> str:
    """Stable digest of a trap-set payload (sorted keys, no whitespace)."""
    body = {key: value for key, value in (payload or {}).items() if key != 'digest'}
    return hashlib.sha256(_canonical(body).encode('utf-8')).hexdigest()


def ratio(numerator, denominator):
    """ablation.py's arithmetic discipline: a zero denominator is ``None`` (N/A), never ``0``."""
    if not denominator:
        return None
    return round(numerator / denominator, 4)


def _scope(start, end=None):
    if start is None and end is None:
        return None
    return {'start': start, 'end': end}


def scope_bounds(scope):
    """``(start, end)`` datetimes of a time scope, or ``None``.

    Prefers ``binding.py``'s parser and falls back to the identical half-open semantics if that
    private helper ever moves (``datamind/binding.py`` is shared with other work packages).
    """
    parser = getattr(binding_mod, '_scope_bounds', None)
    if parser is not None:
        return parser(scope)
    if not isinstance(scope, dict):
        return None
    start, end = scope.get('start'), scope.get('end')
    if start is None and end is None:
        return None
    try:
        return (binding_mod.timestamp(start) if start is not None else None,
                binding_mod.timestamp(end) if end is not None else None)
    except (ValueError, TypeError):
        return None


# ------------------------------------------------------------------ fixture builders

def _sql_row(entity, metric, value, unit, *, version=None, scope=None, instant=None, basis=None,
             row_id=None, drop_unit=False):
    row = {'row_id': row_id or f'{entity}-{metric}', 'entity_label': entity, 'metric': metric,
           'value': value}
    if not drop_unit:
        row['unit'] = unit
    if version:
        row['applies_to_version'] = version
    if scope:
        row['time_scope'] = dict(scope)
    if instant:
        row['measured_at'] = instant
    if basis:
        row['time_basis'] = basis
    return row


def _value_block(document_id, version, entity, metric, value, unit, *, scope=None, basis=None,
                 title=None, text=None):
    block = {'document_id': document_id, 'document_version': version, 'chunk_id': f'{document_id}-c1',
             'title': title or document_id, 'entity_label': entity, 'metric': metric,
             'value': value, 'unit': unit}
    if scope:
        block['time_scope'] = dict(scope)
    if basis:
        block['time_basis'] = basis
    block['text'] = text or f'{entity} 的 {metric} 为 {value} {unit}。'
    block['source_sha256'] = _sha(_canonical(block))
    return block


def _clause_block(document_id, version, entity, metric, op, limit, unit, valid_from, valid_to, *,
                  title=None, text=None, basis=None):
    clause = {'clause_id': f'{document_id}-clause', 'metric': metric, 'op': op, 'limit': limit,
              'unit': unit, 'entity_label': entity, 'valid_from': valid_from, 'valid_to': valid_to}
    block = {'document_id': document_id, 'document_version': version, 'chunk_id': f'{document_id}-c1',
             'title': title or document_id, 'entity_label': entity, 'metric': metric,
             'unit': unit, 'clause': clause,
             'text': text or f'{entity} {metric} {op} {limit} {unit}（{valid_from} 起）。'}
    if basis:
        block['time_basis'] = basis
    block['source_sha256'] = _sha(_canonical(block))
    return block


def _authority_rule(rule_id, entity, metric, selected):
    """Registered authority: ``selected_source`` is ``production`` (SQL side) or ``report`` (doc)."""
    return {'rule_id': rule_id,
            'payload': {'version': 'source-authority-v1', 'selected_source': selected,
                        'context': {'scope': {'part_id': entity, 'metric': metric}},
                        'rationale': '登记口径优先级（合成 fixture）'}}


def _claim_binding(rows_or_blocks, side, metric):
    """The frozen answer binding, taken mechanically from the primary fixture's own fields."""
    for item in rows_or_blocks:
        if item.get('metric') != metric or item.get('value') is None:
            continue
        scope = item.get('time_scope')
        return {'entity_id': item.get('entity_label'), 'version': item.get('document_version'),
                'unit': item.get('unit'), 'time_scope': None if scope is None else dict(scope)}
    raise ValueError('PRIMARY_CLAIM_NOT_FOUND:' + metric)


# ------------------------------------------------------------------ the frozen label table

def _specs():
    """The 40 frozen trap cases (10 per trap kind), labels written before any arm exists."""
    month = '2025-03'
    cases = []

    # ---------------------------------------------------------------- (a) cross_version
    def v1v2(entity, metric='holding_temperature', v1=None, v2=None):
        v1 = v1 or f'{entity}-V1'
        v2 = v2 or f'{entity}-V2'
        return [
            _clause_block(f'doc-{entity}-v1', v1, entity, metric, '<=', '850', 'degC',
                          '2024-01-01T00:00:00+08:00', '2025-03-01T00:00:00+08:00',
                          title=f'工艺规范 {entity}（{v1}）',
                          text=f'{entity} 工艺规范 {v1}：{metric} 不得超过 850 degC，适用期至 2025-03-01。'),
            _clause_block(f'doc-{entity}-v2', v2, entity, metric, '<=', '900', 'degC',
                          '2025-03-01T00:00:00+08:00', None,
                          title=f'工艺规范 {entity}（{v2}）',
                          text=f'{entity} 工艺规范 {v2}：{metric} 不得超过 900 degC，自 2025-03-01 起适用。'),
        ]

    MAY = _scope('2025-05-01T00:00:00+08:00', '2025-06-01T00:00:00+08:00')
    APR = _scope('2025-04-01T00:00:00+08:00', '2025-05-01T00:00:00+08:00')
    JUL = _scope('2025-07-01T00:00:00+08:00', '2025-08-01T00:00:00+08:00')
    SEP = _scope('2025-09-01T00:00:00+08:00', '2025-10-01T00:00:00+08:00')
    NOV24 = _scope('2024-11-01T00:00:00+08:00', '2024-12-01T00:00:00+08:00')
    BOUNDARY = _scope('2025-03-01T00:00:00+08:00', '2025-03-02T00:00:00+08:00')
    DEC24 = _scope('2024-12-28T00:00:00+08:00', '2024-12-31T00:00:00+08:00')
    AUG25 = _scope('2025-08-01T00:00:00+08:00', '2025-09-01T00:00:00+08:00')

    alias_entities = {'bt-a-04': ('3号淬火线', 'A103'), 'bt-a-05': ('3号淬火线', 'A103')}
    for case_id, entity, scope, instant, declared, version, trap in (
            ('bt-a-01', 'A102', MAY, '2025-05-10T09:00:00+08:00', None, 'A102-V2',
             '生产行未声明版本，登记窗口把声称绑定到 V2'),
            ('bt-a-02', 'A102', APR, '2025-04-06T08:30:00+08:00', 'A102-V1', 'A102-V2',
             '生产行声明的是过期版本 V1，登记窗口要求 V2'),
            ('bt-a-03', 'A102', JUL, '2025-07-18T10:15:00+08:00', 'A102-V1', 'A102-V2',
             '过期版本声明 + 更晚的声称时点'),
            ('bt-a-04', 'A103', SEP, '2025-09-09T09:00:00+08:00', None, 'A103-V2',
             '同对象跨版本换实体（A103）'),
            ('bt-a-05', 'A103', NOV24, '2024-11-20T14:00:00+08:00', 'A103-V2', 'A103-V1',
             '反向过期：声称声明 V2，但时点落在 V1 窗口内'),
            ('bt-a-06', 'A102', BOUNDARY, '2025-03-01T00:00:00+08:00', 'A102-V1', 'A102-V2',
             '半开区间边界：窗口起点归属新版本'),
            ('bt-a-07', 'A102', DEC24, '2024-12-29T11:00:00+08:00', None, 'A102-V1',
             '窗口结束前一刻仍属 V1'),
            ('bt-a-08', 'B201', MAY, '2025-05-22T13:20:00+08:00', 'B201-V1', 'B201-V2',
             '另一个工序对象的跨版本陷阱')):
        raw_entity, canonical = alias_entities.get(case_id, (entity, entity))
        aliases = ([] if raw_entity == canonical
                   else [{'alias': raw_entity, 'canonical_name': canonical}])
        rows = [_sql_row(raw_entity, 'holding_temperature', '880', 'degC', version=declared,
                         scope=scope, instant=instant, basis='production_batch',
                         row_id=f'{case_id}-row')]
        cases.append(_case(case_id, 'cross_version',
                           f'{canonical} 在 {scope["start"][:10]} 的保温温度上限按哪一版工艺规范？',
                           rows=rows, blocks=v1v2(entity), version=version, answerable=True,
                           conflict=False, resolution='resolved_by_authority',
                           provenance=f'自建冻结陷阱（同对象跨版本）：{trap}',
                           aliases=aliases,
                           expected_entity=canonical if raw_entity != canonical else None))
    # a09: no registered window covers the instant -> applicability unknown -> refuse
    cases.append(_case('bt-a-09', 'cross_version',
                       'A205 在 2025-08 的保温温度上限按哪一版工艺规范？',
                       rows=[_sql_row('A205', 'holding_temperature', '870', 'degC',
                                      scope=AUG25, instant='2025-08-14T09:00:00+08:00',
                                      basis='production_batch', row_id='bt-a-09-row')],
                       blocks=[_clause_block('doc-a205-v1', 'A205-V1', 'A205', 'holding_temperature',
                                             '<=', '860', 'degC', '2024-01-01T00:00:00+08:00',
                                             '2025-03-01T00:00:00+08:00',
                                             title='工艺规范 A205（V1）',
                                             text='A205 工艺规范 V1：保温温度不得超过 860 degC。')],
                       version=None, answerable=False, conflict=False, resolution='needs_review',
                       provenance='自建冻结陷阱（同对象跨版本）：声称时点没有任何登记窗口覆盖，版本适用性未知'))
    # a10: control - declared revision already equals the applicable one
    cases.append(_case('bt-a-10', 'cross_version', 'A102 在 2025-05 的保温温度上限按哪一版工艺规范？',
                       rows=[_sql_row('A102', 'holding_temperature', '880', 'degC',
                                      version='A102-V2', scope=MAY,
                                      instant='2025-05-06T09:00:00+08:00', row_id='bt-a-10-row')],
                       blocks=v1v2('A102'), version='A102-V2', answerable=True, conflict=False,
                       resolution='resolved_by_authority',
                       provenance='自建冻结陷阱（同对象跨版本）负向对照：声明版本与登记窗口一致，无需修复'))

    # ---------------------------------------------------------------- (b) unit_mismatch
    def unit_case(case_id, trap, entity, metric, sql_value, sql_unit, doc_value, doc_unit,
                  *, version, scope, basis, expect_resolution, answerable, conflict,
                  unit_missing=False):
        rows = [_sql_row(entity, metric, sql_value, sql_unit, version=version, scope=scope,
                         instant=scope['start'], basis=basis, row_id=f'{case_id}-row',
                         drop_unit=unit_missing)]
        blocks = [_value_block(f'{case_id}-doc', version, entity, metric, doc_value, doc_unit,
                               scope=scope, basis=basis,
                               title=f'{case_id} 文档', text=f'{entity} {metric}：{doc_value} {doc_unit}。')]
        return _case(case_id, 'unit_mismatch', f'{entity} 的 {metric} 在两侧是否可比/一致？',
                     rows=rows, blocks=blocks, version=version, answerable=answerable,
                     conflict=conflict, resolution=expect_resolution, provenance=trap)

    MAY_SCOPE = MAY
    cases.append(unit_case('bt-b-01', '自建冻结陷阱（单位不可归一）：英寸 vs 毫米，量纲看似相同但英寸未登记',
                           'A102', 'outer_diameter', '20.04', 'in', '508', 'mm',
                           version='A102-V2', scope=MAY_SCOPE, basis='production_batch',
                           expect_resolution='needs_review', answerable=False, conflict=True))
    cases.append(unit_case('bt-b-02', '自建冻结陷阱（单位不可归一）：毫米 vs 英寸（方向相反）',
                           'A102', 'outer_diameter', '0.79', 'mm', '0.79', 'in',
                           version='A102-V2', scope=MAY_SCOPE, basis='production_batch',
                           expect_resolution='needs_review', answerable=False, conflict=True))
    cases.append(unit_case('bt-b-03', '自建冻结陷阱（单位不可精确归一）：degF→degC 的 5/9 不是终止小数，'
                                      '登记换算必须拒绝而不是四舍五入',
                           'B201', 'quench_temperature', '70', 'degF', '21.1', 'degC',
                           version='B201-V2', scope=MAY_SCOPE, basis='production_batch',
                           expect_resolution='needs_review', answerable=False, conflict=True))
    cases.append(unit_case('bt-b-04', '自建冻结陷阱（单位不可精确归一）：文档侧 degF 值同样的非终止换算',
                           'B201', 'quench_temperature', '850', 'degC', '70', 'degF',
                           version='B201-V2', scope=MAY_SCOPE, basis='production_batch',
                           expect_resolution='needs_review', answerable=False, conflict=True))
    cases.append(unit_case('bt-b-05', '自建冻结陷阱（自定义单位不可归一）：AU vs LU，两侧单位都未登记',
                           'C310', 'line_pressure', '12.5', 'AU', '900', 'LU',
                           version='C310-V1', scope=MAY_SCOPE, basis='natural_month',
                           expect_resolution='needs_review', answerable=False, conflict=True))
    cases.append(unit_case('bt-b-06', '自建冻结陷阱（自定义单位不可归一）：mm/s vs mm/min，复合单位未登记',
                           'C310', 'flow_speed', '3.2', 'mm/s', '192', 'mm/min',
                           version='C310-V1', scope=MAY_SCOPE, basis='natural_month',
                           expect_resolution='needs_review', answerable=False, conflict=True))
    cases.append(unit_case('bt-b-07', '自建冻结陷阱（单位缺失）：库侧无单位，登记同类声称可补单位后仍与文档矛盾',
                           'C310', 'line_pressure', '0.62', None, '0.55', 'MPa',
                           version='C310-V1', scope=MAY_SCOPE, basis='natural_month',
                           expect_resolution='unresolved', answerable=False, conflict=True,
                           unit_missing=True))
    cases.append(unit_case('bt-b-08', '自建冻结陷阱（可比但不等）：K vs degC 可精确换算，换算后数值不等',
                           'A102', 'holding_temperature', '1123.15', 'K', '900', 'degC',
                           version='A102-V2', scope=MAY_SCOPE, basis='production_batch',
                           expect_resolution='unresolved', answerable=False, conflict=True))
    cases.append(unit_case('bt-b-09', '自建冻结陷阱（单位等价）负向对照：1123.15 K == 850 degC，不得误报冲突',
                           'A102', 'holding_temperature', '1123.15', 'K', '850', 'degC',
                           version='A102-V2', scope=MAY_SCOPE, basis='production_batch',
                           expect_resolution='resolved_by_authority', answerable=True, conflict=False))
    cases.append(unit_case('bt-b-10', '自建冻结陷阱（单位等价）负向对照：50 cm == 500 mm，不得误报冲突',
                           'A102', 'outer_diameter', '50', 'cm', '500', 'mm',
                           version='A102-V2', scope=MAY_SCOPE, basis='production_batch',
                           expect_resolution='resolved_by_authority', answerable=True, conflict=False))

    # ---------------------------------------------------------------- (c) time_scope_mismatch
    def time_case(case_id, trap, entity, metric, sql_value, sql_scope, sql_basis, doc_value,
                  doc_scope, doc_basis, *, expect_resolution, answerable, conflict, version,
                  sql_unit='degC', doc_unit='degC', sql_scope_raw=None):
        row = _sql_row(entity, metric, sql_value, sql_unit, version=version,
                       scope=sql_scope if sql_scope_raw is None else sql_scope_raw,
                       instant=sql_scope['start'] if sql_scope else None, basis=sql_basis,
                       row_id=f'{case_id}-row')
        block = _value_block(f'{case_id}-doc', version, entity, metric, doc_value, doc_unit,
                             scope=doc_scope, basis=doc_basis, title=f'{case_id} 文档',
                             text=f'{entity} {metric}：{doc_value} {doc_unit}（口径：{doc_basis}）。')
        return _case(case_id, 'time_scope_mismatch', f'{entity} 的 {metric} 在两侧时间口径下是否可比？',
                     rows=[row], blocks=[block], version=version, answerable=answerable,
                     conflict=conflict, resolution=expect_resolution, provenance=trap)

    MARCH = _scope('2025-03-01T00:00:00+08:00', '2025-04-01T00:00:00+08:00')
    FEB = _scope('2025-02-01T00:00:00+08:00', '2025-03-01T00:00:00+08:00')
    Q1 = _scope('2025-01-01T00:00:00+08:00', '2025-04-01T00:00:00+08:00')
    BATCH = _scope('2025-03-15T00:00:00+08:00', '2025-03-20T00:00:00+08:00')
    BATCH2 = _scope('2025-03-06T00:00:00+08:00', '2025-03-12T00:00:00+08:00')
    April = _scope('2025-04-01T00:00:00+08:00', '2025-05-01T00:00:00+08:00')

    cases.append(time_case('bt-c-01', '自建冻结陷阱（时间口径不同）：自然月 vs 上一自然月，区间不重叠',
                           'A102', 'holding_temperature', '880', MARCH, 'natural_month', '905', FEB,
                           'natural_month', expect_resolution='needs_review', answerable=False,
                           conflict=True, version='A102-V2'))
    cases.append(time_case('bt-c-02', '自建冻结陷阱（时间口径不同）：自然月 vs 季度，区间部分重叠但口径不同',
                           'A102', 'holding_temperature', '880', MARCH, 'natural_month', '905', Q1,
                           'quarter', expect_resolution='needs_review', answerable=False,
                           conflict=True, version='A102-V2'))
    cases.append(time_case('bt-c-03', '自建冻结陷阱（时间口径不同）：季度 vs 自然月（方向相反）',
                           'A103', 'holding_temperature', '1820', Q1, 'quarter', '600', MARCH,
                           'natural_month', expect_resolution='needs_review', answerable=False,
                           conflict=True, version='A103-V2'))
    cases.append(time_case('bt-c-04', '自建冻结陷阱（时间口径不同）：生产批次期间 vs 自然月',
                           'B201', 'quench_temperature', '851', BATCH, 'production_batch', '860',
                           MARCH, 'natural_month', expect_resolution='needs_review',
                           answerable=False, conflict=True, version='B201-V2'))
    cases.append(time_case('bt-c-05', '自建冻结陷阱（时间口径不同）：生产批次期间 vs 季度，区间不重叠',
                           'B201', 'quench_temperature', '851', BATCH, 'production_batch', '844',
                           Q1, 'quarter', expect_resolution='needs_review', answerable=False,
                           conflict=True, version='B201-V2'))
    cases.append(time_case('bt-c-06', '自建冻结陷阱（时间口径不同）：自然月 vs 生产批次期间（方向相反）',
                           'C310', 'line_pressure', '0.62', MARCH, 'natural_month', '0.58', BATCH2,
                           'production_batch', expect_resolution='needs_review', answerable=False,
                           conflict=True, version='C310-V1', sql_unit='MPa', doc_unit='MPa'))
    cases.append(time_case('bt-c-07', '自建冻结陷阱（时间口径不同）：同为自然月但月份不同，区间不重叠',
                           'C310', 'line_pressure', '0.62', MARCH, 'natural_month', '0.71', April,
                           'natural_month', expect_resolution='needs_review', answerable=False,
                           conflict=True, version='C310-V1', sql_unit='MPa', doc_unit='MPa'))
    cases.append(time_case('bt-c-08', '自建冻结陷阱（时间口径不同）：库侧时间戳没有时区，无法确定口径',
                           'A205', 'holding_temperature', '870',
                           _scope('2025-03-01T00:00:00+08:00', '2025-04-01T00:00:00+08:00'),
                           'natural_month', '880',
                           _scope('2025-03-01T00:00:00+08:00', '2025-04-01T00:00:00+08:00'),
                           'natural_month', expect_resolution='needs_review', answerable=False,
                           conflict=True, version='A205-V1',
                           sql_scope_raw=_scope(month + '-01', '2025-04-01')))
    cases.append(time_case('bt-c-09', '自建冻结陷阱（时间口径不同）负向对照：季度包含自然月且数值一致，不得不必要拒答',
                           'A205', 'holding_temperature', '870', MARCH, 'natural_month', '870',
                           Q1, 'natural_month', expect_resolution='resolved_by_authority',
                           answerable=True, conflict=False, version='A205-V1'))
    cases.append(time_case('bt-c-10', '自建冻结陷阱（时间口径不同）负向对照：同口径开区间包含且数值一致',
                           'A205', 'holding_temperature', '870', MARCH, 'natural_month', '870',
                           _scope('2025-01-01T00:00:00+08:00', None), 'natural_month',
                           expect_resolution='resolved_by_authority', answerable=True,
                           conflict=False, version='A205-V1'))

    # ---------------------------------------------------------------- (d) doc_db_opposite
    def opposite_case(case_id, trap, entity, metric, sql_value, doc_value, unit, *, version,
                      scope, basis, resolution, answerable, conflict, selected=None,
                      doc_scope=None, rule_id=None, alias=None, expected_entity=None):
        rows = [_sql_row(alias or entity, metric, sql_value, unit, version=version, scope=scope,
                         instant=scope['start'], basis=basis, row_id=f'{case_id}-row')]
        blocks = [_value_block(f'{case_id}-doc', version, entity, metric, doc_value, unit,
                               scope=doc_scope or scope, basis=basis,
                               title=f'{case_id} 质检报告',
                               text=f'{entity} 质检报告：{metric} 实测 {doc_value} {unit}。')]
        authority = [] if selected is None else [
            _authority_rule(rule_id or f'auth-{case_id}', entity, metric, selected)]
        return _case(case_id, 'doc_db_opposite', f'{entity} 的 {metric} 与质检报告是否一致？',
                     rows=rows, blocks=blocks, version=version, answerable=answerable,
                     conflict=conflict, resolution=resolution, authority=authority,
                     provenance=trap,
                     aliases=[] if not alias else [{'alias': alias, 'canonical_name': entity}],
                     expected_entity=expected_entity)

    cases.append(opposite_case('bt-d-01', '自建冻结陷阱（文档与库数值相反）：同对象同版本，880 vs 905 无登记权威',
                               'A102', 'holding_temperature', '880', '905', 'degC',
                               version='A102-V2', scope=MAY, basis='production_batch',
                               resolution='unresolved', answerable=False, conflict=True))
    cases.append(opposite_case('bt-d-02', '自建冻结陷阱（文档与库数值相反）：850 vs 843 无登记权威',
                               'A102', 'holding_temperature', '850', '843', 'degC',
                               version='A102-V2', scope=MAY, basis='production_batch',
                               resolution='unresolved', answerable=False, conflict=True))
    cases.append(opposite_case('bt-d-03', '自建冻结陷阱（文档与库数值相反）：退款金额 7800 vs 7500 CNY',
                               'A103', 'refund_amount', '7800', '7500', 'CNY',
                               version='refund-policy-2025-04', scope=APR, basis='natural_month',
                               resolution='unresolved', answerable=False, conflict=True))
    cases.append(opposite_case('bt-d-04', '自建冻结陷阱（文档与库数值相反）：报废数量 12 vs 15 件'
                                           '（库侧标签是未登记别名，必须按 approved 别名规范化）',
                               'B201', 'scrap_count', '12', '15', 'count',
                               version='B201-V2', scope=MAY, basis='natural_month',
                               resolution='unresolved', answerable=False, conflict=True,
                               alias='B201 淬火线', expected_entity='B201'))
    cases.append(opposite_case('bt-d-05', '自建冻结陷阱（文档与库数值相反）：管线压力 0.62 vs 0.55 MPa',
                               'C310', 'line_pressure', '0.62', '0.55', 'MPa',
                               version='C310-V1', scope=MAY, basis='production_batch',
                               resolution='unresolved', answerable=False, conflict=True))
    cases.append(opposite_case('bt-d-06', '自建冻结陷阱（文档与库数值相反）：保温时长 45 vs 60 min',
                               'C310', 'holding_time', '45', '60', 'min',
                               version='C310-V1', scope=MAY, basis='production_batch',
                               resolution='unresolved', answerable=False, conflict=True))
    cases.append(opposite_case('bt-d-07', '自建冻结陷阱（文档与库数值相反）+ 登记权威指向库侧：必须按登记收敛',
                               'A102', 'holding_temperature', '880', '905', 'degC',
                               version='A102-V2', scope=MAY, basis='production_batch',
                               resolution='resolved_by_authority', answerable=True, conflict=True,
                               selected='production', rule_id='auth-bt-d-07'))
    cases.append(opposite_case('bt-d-08', '自建冻结陷阱（文档与库数值相反）+ 登记权威指向文档侧：收敛到报告口径与报告时间范围',
                               'A102', 'holding_temperature', '880', '905', 'degC',
                               version='A102-V2', scope=MAY, basis='production_batch',
                               resolution='resolved_by_authority', answerable=True, conflict=True,
                               selected='report', rule_id='auth-bt-d-08',
                               doc_scope=_scope('2025-05-10T00:00:00+08:00',
                                                '2025-05-11T00:00:00+08:00')))
    cases.append(opposite_case('bt-d-09', '自建冻结陷阱（文档与库数值相反）+ 登记权威指向库侧（金额口径）',
                               'A103', 'refund_amount', '7800', '7500', 'CNY',
                               version='refund-policy-2025-04', scope=APR, basis='natural_month',
                               resolution='resolved_by_authority', answerable=True, conflict=True,
                               selected='production', rule_id='auth-bt-d-09'))
    cases.append(opposite_case('bt-d-10', '自建冻结陷阱（文档与库数值相反）负向对照：两侧数值一致，不得误报冲突',
                               'A102', 'holding_temperature', '880', '880', 'degC',
                               version='A102-V2', scope=MAY, basis='production_batch',
                               resolution='resolved_by_authority', answerable=True, conflict=False))
    return cases


def _case(case_id, trap_kind, question, *, rows, blocks, version, answerable, conflict, resolution,
          authority=None, provenance='', aliases=None, expected_entity=None):
    """Assemble one frozen case: fixtures plus the pre-declared labels."""
    if trap_kind not in TRAP_KINDS:
        raise ValueError('TRAP_KIND_INVALID:' + trap_kind)
    if resolution not in RESOLUTIONS:
        raise ValueError('RESOLUTION_INVALID:' + resolution)
    metric = rows[0]['metric']
    primary_side = _doc_selected(authority, metric) or 'sql'
    expected = (_claim_binding(blocks, 'doc', metric) if primary_side == 'doc'
                else _claim_binding(rows, 'sql', metric))
    if version is not None:
        expected['version'] = version
    if expected_entity is not None:
        expected['entity_id'] = expected_entity
    return {
        'case_id': case_id, 'trap_kind': trap_kind, 'question': question,
        'provenance': provenance,
        'sql': {'source_ref': 'demo_industrial', 'snapshot_id': 'ind-snapshot-5', 'columns': [],
                'sql_hash': _sha(case_id + ':sql'), 'rows': rows},
        'docs': {'source_ref': 'demo_kb', 'blocks': blocks},
        'authority_rules': list(authority or []),
        'aliases': list(aliases or []),
        'primary': {'side': primary_side, 'metric': metric},
        'expected_binding': expected,
        'expected_answerable': bool(answerable),
        'expected_conflict': bool(conflict),
        'expected_resolution': resolution,
        'expected_value': rows[0]['value'],
    }


def _doc_selected(authority, metric):
    """A case whose registered authority picks the document side answers with the document claim."""
    for rule in authority or []:
        payload = rule.get('payload') or {}
        if payload.get('selected_source') == 'report':
            return 'doc'
    return None


def generate_dataset():
    """Deterministic materialisation of the frozen trap set (no timestamps, no randomness)."""
    cases = _specs()
    payload = {
        'suite': 'binding_traps_v1',
        'suite_version': SUITE_VERSION,
        'frozen_at': '2026-10-10',
        'kind': 'binding-trap-set',
        'binding_eval_version': BINDING_EVAL_VERSION,
        'trap_kinds': TRAP_KIND_DEFINITIONS,
        'resolution_vocabulary': RESOLUTION_VOCABULARY,
        'label_policy': LABEL_POLICY,
        'case_count': len(cases),
        'counts_by_trap_kind': {kind: sum(1 for case in cases if case['trap_kind'] == kind)
                                for kind in TRAP_KINDS},
        'cases': cases,
    }
    payload['digest'] = dataset_digest(payload)
    return payload


# ------------------------------------------------------------------ load / verify labels

def load_dataset(path=None, *, expected_sha256=None):
    """Load the frozen set; refuse a silently changed label table when a digest is pinned."""
    raw = Path(path or TRAPS_PATH).read_bytes()
    try:
        payload = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        raise ValueError('BINDING_TRAPS_DATASET_INVALID') from None
    digest = hashlib.sha256(raw).hexdigest()
    canonical = dataset_digest(payload)
    if expected_sha256 is not None and canonical != expected_sha256:
        raise ValueError('BINDING_TRAPS_DATASET_CHANGED')
    cases = payload.get('cases')
    if not isinstance(cases, list) or not cases:
        raise ValueError('BINDING_TRAPS_DATASET_INVALID')
    return {'version': payload.get('suite_version'), 'suite': payload.get('suite'),
            'sha256': canonical, 'file_sha256': digest, 'payload': payload, 'cases': cases}


def _numeric_claims(case):
    """The comparable numeric claims of one case, read from the fixtures (never from labels)."""
    claims = []
    aliases = case.get('aliases') or []

    def canonical(label):
        return binding_mod.entity_bind(label, aliases).get('entity_id') or label

    for row in case['sql']['rows']:
        if row.get('value') is not None and row.get('metric'):
            claims.append({'side': 'sql', 'entity': canonical(row.get('entity_label')),
                           'metric': row.get('metric'), 'value': str(row.get('value')),
                           'unit': row.get('unit'),
                           'version': (row.get('applies_to_version') or row.get('recorded_revision')
                                       or row.get('revision') or row.get('version')
                                       or case['sql'].get('snapshot_id')),
                           'time_scope': row.get('time_scope'),
                           'time_basis': row.get('time_basis')})
    for block in case['docs']['blocks']:
        if block.get('value') is not None and block.get('metric'):
            claims.append({'side': 'doc', 'entity': canonical(block.get('entity_label')),
                           'metric': block.get('metric'), 'value': str(block.get('value')),
                           'unit': block.get('unit'),
                           'version': (block.get('applies_to_version') or block.get('document_version')),
                           'time_scope': block.get('time_scope'),
                           'time_basis': block.get('time_basis')})
    return claims


def _derived_conflict(case):
    """Re-derive ``expected_conflict`` from the fixtures alone (independent of the label)."""
    claims = _numeric_claims(case)
    if len(claims) < 2:
        return False
    left = claims[0]
    for right in claims[1:]:
        if (left['entity'], left['metric'], left['version']) != \
                (right['entity'], right['metric'], right['version']):
            continue
        if not binding_mod.units_equivalent(left['unit'], right['unit']):
            return True
        if (left.get('time_basis') or right.get('time_basis')) and \
                left.get('time_basis') != right.get('time_basis'):
            return True
        if binding_mod.values_agree(left['value'], left['unit'], right['value'], right['unit']) is False:
            return True
    return False


def validate_labels(payload):
    """Independent label audit: a loosened label is an error, not a quiet retune."""
    problems = []
    cases = payload.get('cases') or []
    if len(cases) < 40:
        problems.append(f'CASE_COUNT_TOO_SMALL:{len(cases)}<40')
    kinds = {}
    seen = set()
    for case in cases:
        case_id = case.get('case_id')
        if not case_id or case_id in seen:
            problems.append(f'CASE_ID_INVALID:{case_id}')
        seen.add(case_id)
        kind = case.get('trap_kind')
        kinds[kind] = kinds.get(kind, 0) + 1
        if kind not in TRAP_KINDS:
            problems.append(f'TRAP_KIND_INVALID:{case_id}:{kind}')
        for field in ('expected_binding', 'expected_answerable', 'expected_conflict',
                      'expected_resolution'):
            if field not in case:
                problems.append(f'LABEL_MISSING:{case_id}:{field}')
        binding = case.get('expected_binding') or {}
        for field in ('entity_id', 'unit', 'time_scope'):
            if field not in binding:
                problems.append(f'BINDING_FIELD_MISSING:{case_id}:{field}')
        if case.get('expected_resolution') not in RESOLUTIONS:
            problems.append(f'RESOLUTION_INVALID:{case_id}:{case.get("expected_resolution")}')
        # (1) answerable <=> settled by a registered deterministic basis
        if bool(case.get('expected_answerable')) != \
                (case.get('expected_resolution') == 'resolved_by_authority'):
            problems.append(f'ANSWERABLE_LABEL_LOOSENED:{case_id}')
        # (2) expected_conflict must equal what the fixtures imply
        derived = _derived_conflict(case)
        if bool(case.get('expected_conflict')) != derived:
            problems.append(f'CONFLICT_LABEL_LOOSENED:{case_id}:'
                            f'label={bool(case.get("expected_conflict"))} derived={derived}')
        if case.get('expected_resolution') == 'unresolved' and not derived:
            problems.append(f'UNRESOLVED_WITHOUT_CONFLICT:{case_id}')
        # (3) every fixture digest must be a real sha256
        for block in (case.get('docs') or {}).get('blocks') or []:
            if not _HEX_SHA256.match(str(block.get('source_sha256') or '')):
                problems.append(f'SOURCE_SHA256_INVALID:{case_id}:{block.get("chunk_id")}')
        # (4) an approved alias row must carry both fields a binding layer needs
        for alias in case.get('aliases') or []:
            if not alias.get('alias') or not alias.get('canonical_name'):
                problems.append(f'ALIAS_ROW_INVALID:{case_id}')
    for kind in TRAP_KINDS:
        if kinds.get(kind, 0) < 8:
            problems.append(f'TRAP_KIND_TOO_SMALL:{kind}:{kinds.get(kind, 0)}<8')
    wrong = sorted(set(kinds) - set(TRAP_KINDS))
    if wrong:
        problems.append('UNKNOWN_TRAP_KINDS:' + ','.join(str(item) for item in wrong))
    return problems


def _first_difference(left, right, path=''):
    if type(left) is not type(right):
        return f'{path}:type'
    if isinstance(left, dict):
        for key in sorted(set(left) | set(right)):
            if key not in left or key not in right:
                return f'{path}.{key}'
            found = _first_difference(left[key], right[key], f'{path}.{key}')
            if found:
                return found
        return None
    if isinstance(left, list):
        if len(left) != len(right):
            return f'{path}:length'
        for index, (a, b) in enumerate(zip(left, right)):
            found = _first_difference(a, b, f'{path}[{index}]')
            if found:
                return found
        return None
    return None if left == right else path


def check_frozen(path=None):
    """``--check``: regenerate the frozen set and re-audit the labels of the checked-in file."""
    target = Path(path or TRAPS_PATH)
    try:
        frozen = json.loads(target.read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeDecodeError):
        return {'ok': False, 'error_code': 'BINDING_TRAPS_DATASET_INVALID', 'path': str(target),
                'problems': ['UNREADABLE_OR_INVALID_JSON']}
    generated = generate_dataset()
    problems = []
    if dataset_digest(frozen) != dataset_digest(generated):
        problems.append('GENERATED_DATASET_DIFFERS:'
                        + str(_first_difference(generated, frozen, 'cases')))
    problems += validate_labels(frozen)
    return {'ok': not problems,
            'error_code': None if not problems else 'BINDING_TRAPS_LABELS_LOOSENED',
            'path': str(target), 'problems': problems,
            'sha256': dataset_digest(frozen), 'generated_sha256': dataset_digest(generated),
            'pinned_sha256': TRAPS_SHA256,
            'pin_matches': (not TRAPS_SHA256) or TRAPS_SHA256 == dataset_digest(frozen),
            'cases': len(frozen.get('cases') or []),
            'counts_by_trap_kind': {kind: sum(1 for case in frozen.get('cases') or []
                                              if case.get('trap_kind') == kind)
                                    for kind in TRAP_KINDS}}


# ------------------------------------------------------------------ arm plumbing

def _row_binding(row, snapshot_id):
    """The *declared* binding of a SQL row, before any verification (plain/bind_only use this)."""
    version = (row.get('applies_to_version') or row.get('recorded_revision')
               or row.get('revision') or row.get('version') or row.get('snapshot_version')
               or snapshot_id)
    scope = row.get('time_scope')
    if not scope:
        start = row.get('period_start') or row.get('window_start') or row.get('start')
        end = row.get('period_end') or row.get('window_end') or row.get('end')
        scope = None if (start is None and end is None) else {'start': start, 'end': end}
    return {'entity_id': row.get('entity_label'), 'version': version,
            'unit': row.get('unit'), 'time_scope': scope}


def _claim(case, binding, *, confidence, value, unit, asserted, source='sql', problems=None,
           conflict_detected=False, resolution='not_evaluated', sides=None,
           selected_source=None, repair=None):
    return {'case_id': case['case_id'], 'trap_kind': case['trap_kind'],
            'primary_side': case['primary']['side'], 'source': source,
            'binding': binding, 'confidence': confidence, 'asserted': bool(asserted),
            'value': value, 'unit': unit, 'binding_problems': list(problems or []),
            'conflict_detected': bool(conflict_detected), 'resolution': resolution,
            'sides': list(sides or []), 'selected_source': selected_source,
            'repair': repair, 'model_calls': 0}


def arm_plain(case):
    """Stitch SQL rows and document snippets, answer with the first numeric source, verbatim."""
    started = time.perf_counter()
    row = case['sql']['rows'][0]
    binding = _row_binding(row, case['sql'].get('snapshot_id'))
    answer = ('SQL 结果：' + json.dumps(case['sql']['rows'], ensure_ascii=False, sort_keys=True)
              + ' 文档片段：' + ' | '.join(block.get('text', '') for block in case['docs']['blocks']))
    claim = _claim(case, binding, confidence='high', value=row.get('value'), unit=row.get('unit'),
                   asserted=True, problems=[], conflict_detected=False, resolution='not_evaluated')
    claim['answer'] = answer
    claim['elapsed_ms'] = round((time.perf_counter() - started) * 1000, 4)
    return claim


def arm_bind_only(case):
    """Canonicalise the binding of the first SQL row, then answer without verifying anything."""
    started = time.perf_counter()
    row = case['sql']['rows'][0]
    declared = _row_binding(row, case['sql'].get('snapshot_id'))
    entity = binding_mod.entity_bind(declared['entity_id'], case.get('aliases') or [])
    unit_status = binding_mod.unit_status(declared['unit'])
    binding = {'entity_id': entity['entity_id'] or declared['entity_id'],
               'entity_label': entity['entity_label'] or declared['entity_label'],
               'version': declared['version'],
               'unit': unit_status['unit'] or declared['unit'],
               'time_scope': declared['time_scope']}
    sha = cs.sql_container(case['sql'])['evidence_sha256']
    record = binding_mod.build_binding(entity_id=binding['entity_id'],
                                       entity_label=binding['entity_label'],
                                       version=binding['version'], unit=binding['unit'],
                                       time_scope=binding['time_scope'],
                                       source_ref=case['sql'].get('source_ref'),
                                       source_sha256=sha)
    _, problems = binding_mod.binding_complete(record)
    confidence = 'high' if not problems else 'low'
    claim = _claim(case, binding, confidence=confidence, value=row.get('value'),
                   unit=row.get('unit'), asserted=confidence == 'high', problems=problems,
                   conflict_detected=False, resolution='not_evaluated')
    claim['answer'] = (f"{binding['entity_label']} 的 {row.get('metric')} 为 {row.get('value')} "
                       f"{row.get('unit') or '（单位未绑定）'}（版本 {binding['version']}）")
    claim['elapsed_ms'] = round((time.perf_counter() - started) * 1000, 4)
    return claim


# ------------------------------------------------------------------ verification & repair

def _claim_instant(row):
    for name in ('measured_at', 'produced_at', 'recorded_at', 'instant'):
        if row.get(name):
            return row[name]
    scope = row.get('time_scope') or {}
    return scope.get('start')


def _registered_windows(case, metric):
    """Registered revision windows: the ``valid_from``/``valid_to`` of each applicable clause."""
    windows = []
    for block in case['docs']['blocks']:
        clause = block.get('clause') or {}
        if (clause.get('metric') or block.get('metric')) != metric:
            continue
        valid_from, valid_to = clause.get('valid_from'), clause.get('valid_to')
        if valid_from is None and valid_to is None:
            continue
        windows.append({'version': block.get('document_version'), 'valid_from': valid_from,
                        'valid_to': valid_to})
    return windows


def _applicable_versions(case, metric, instant):
    applicable = []
    for window in _registered_windows(case, metric):
        try:
            if binding_mod.version_applies(instant, window['valid_from'], window['valid_to']):
                applicable.append(window['version'])
        except (ValueError, TypeError):
            continue
    return sorted(set(item for item in applicable if item))


def verify(case):
    """Binding verification for the primary SQL row: revision applicability + time-basis."""
    row = case['sql']['rows'][0]
    binding = _row_binding(row, case['sql'].get('snapshot_id'))
    windows = _registered_windows(case, row.get('metric'))
    scope = binding['time_scope']
    scope_ok = True
    if scope is not None:
        try:
            scope_ok = scope_bounds(scope) is not None
        except (ValueError, TypeError):
            scope_ok = False
    instant = _claim_instant(row)
    applicable = _applicable_versions(case, row.get('metric'), instant)
    if not windows:
        version_status = 'no_registered_window'
        version_ok = True
    elif binding['version'] in applicable:
        version_status = 'version_applicable'
        version_ok = True
    elif len(applicable) == 1:
        version_status = 'version_repairable'
        version_ok = False
    elif not applicable:
        version_status = 'version_unverifiable'
        version_ok = False
    else:
        version_status = 'version_ambiguous'
        version_ok = False
    return {'row': row, 'binding': binding, 'windows': windows, 'applicable': applicable,
            'version_status': version_status, 'version_ok': version_ok, 'scope_ok': scope_ok,
            'unverifiable': (not version_ok and len(applicable) != 1) or not scope_ok,
            'instant': instant}


def _sibling_unit(case, metric, exclude_row=True):
    for row in ([] if exclude_row else case['sql']['rows']):
        if row.get('metric') == metric and row.get('unit') and \
                not binding_mod.unit_status(row.get('unit'))['unit_unknown']:
            return row['unit']
    for block in case['docs']['blocks']:
        if block.get('metric') == metric and block.get('unit') and \
                not binding_mod.unit_status(block.get('unit'))['unit_unknown']:
            return block['unit']
    return None


def _repair_decision(case, verification, probe):
    """At most one repair.  Only binding errors are repairable; the reason is always recorded."""
    row = verification['row']
    metric = row.get('metric')
    if verification['version_status'] == 'version_repairable':
        return ({'kind': 'version_from_validity_window', 'target': 'sql:0',
                 'before': verification['binding']['version'],
                 'after': verification['applicable'][0], 'applied': True,
                 'rule': 'registered_validity_window'},
                {'field': 'applies_to_version', 'value': verification['applicable'][0]})
    if verification['version_status'] in ('version_unverifiable', 'version_ambiguous'):
        return ({'kind': 'version_from_validity_window', 'target': 'sql:0',
                 'before': verification['binding']['version'], 'after': None, 'applied': False,
                 'rule': ('NO_COVERING_VALIDITY_WINDOW:*' if not verification['applicable']
                          else 'AMBIGUOUS_VALIDITY_WINDOW:' + ','.join(verification['applicable']))},
                None)
    weak = [claim for claim in probe.get('claims', []) if claim.get('binding_status') == 'unbound']
    for claim in weak:
        if any(str(problem).startswith('unit') for problem in claim.get('binding_problems') or []):
            unit = claim.get('unit')
            if unit in (None, ''):
                sibling = _sibling_unit(case, claim.get('metric'))
                if sibling:
                    return ({'kind': 'unit_from_sibling', 'target': claim['claim_id'],
                             'before': unit, 'after': sibling, 'applied': True,
                             'rule': 'same_metric_registered_unit'}, {'field': 'unit', 'value': sibling})
            # A stated-but-unregistered unit must be flagged, never relabelled: relabelling
            # 20.04 in as 20.04 mm silently changes the quantity (see the round-1 security review
            # L2).  The arm refuses this repair and says so in the ledger.
            return ({'kind': 'unit_from_sibling', 'target': claim.get('claim_id'), 'before': unit,
                     'after': None, 'applied': False,
                     'rule': 'UNSUPPORTED_UNIT_NOT_REPAIRABLE'}, None)
    detail = probe.get('failure_detail') or 'UNKNOWN'
    return ({'kind': 'none_applicable', 'target': weak[0]['claim_id'] if weak else None,
             'before': None, 'after': None, 'applied': False,
             'rule': 'NO_BINDING_REPAIR_AVAILABLE:' + str(detail)}, None)


def _apply_repair(case, patch):
    """Return a copy of the case with exactly one binding field patched on the primary row."""
    import copy
    patched = copy.deepcopy(case)
    row = patched['sql']['rows'][0]
    if patch['field'] == 'applies_to_version':
        for key in ('version', 'revision', 'recorded_revision', 'snapshot_version'):
            row.pop(key, None)
        row['applies_to_version'] = patch['value']
    elif patch['field'] == 'unit':
        row['unit'] = patch['value']
    return patched


def _plan(case, sql, docs):
    return cs.plan(case['question'], [sql.get('source_ref')], [docs.get('source_ref')],
                   sql_evidence=sql, rag_evidence=docs, aliases=case.get('aliases') or [],
                   authority_rules=case.get('authority_rules') or [], repair_budget=0)


def _side_claim(result, side, metric, node_id=None):
    claims = [claim for claim in result.get('claims', [])
              if claim.get('source_kind') == side and claim.get('metric') == metric]
    if node_id is not None:
        claims = [claim for claim in claims if claim.get('node_id') == node_id] or claims
    return claims[0] if claims else None


def _resolution(case, result, verification, basis_mismatch):
    """Map the pipeline outcome onto the frozen resolution vocabulary (never from the label)."""
    if verification['unverifiable']:
        return 'needs_review'
    records = list(result.get('conflicts') or [])
    unresolved_records = [record for record in records
                          if record.get('resolution_status') != 'resolved_by_registered_authority']
    if not unresolved_records:
        return 'resolved_by_authority' if result.get('status') == 'completed' else 'needs_review'
    for record in unresolved_records:
        if record.get('conflict_type') != 'value_conflict':
            return 'needs_review'
        overlap = binding_mod.time_scope_overlap(record['left'].get('time_scope'),
                                                 record['right'].get('time_scope'))
        if overlap is not True or basis_mismatch:
            return 'needs_review'
    return 'unresolved'


def _basis_mismatch(case):
    baselines = {}
    for row in case['sql']['rows']:
        if row.get('time_basis'):
            baselines.setdefault('sql', set()).add(row['time_basis'])
    for block in case['docs']['blocks']:
        if block.get('time_basis'):
            baselines.setdefault('doc', set()).add(block['time_basis'])
    if 'sql' in baselines and 'doc' in baselines:
        return baselines['sql'] != baselines['doc']
    return False


def arm_full(case, *, version_check=True, conflict_gate=True, repair_budget=1):
    """Binding + verification + at most one accounted repair (all deterministic, no model)."""
    started = time.perf_counter()
    verification = verify(case)
    if not version_check:
        # The degraded variant has no revision-applicability mechanism at all: no verification
        # and no version repair.  It is only used by the within-full sensitivity probe.
        verification = {**verification, 'version_ok': True, 'version_status': 'not_checked',
                        'unverifiable': not verification['scope_ok']}
    result = _plan(case, case['sql'], case['docs'])
    ledger = []
    pre = _classify_full(case, result, verification, ledger)
    if repair_budget > 0:
        needs = (version_check and verification['version_status'] in (
            'version_repairable', 'version_unverifiable', 'version_ambiguous'))
        needs = needs or result.get('error_code') in (cs.DOC_BINDING_MISSING,
                                                      cs.CROSS_SOURCE_EVIDENCE_INSUFFICIENT)
        if needs:
            entry, patch = _repair_decision(case, verification, result)
            ledger.append(entry)
            if patch is not None:
                patched = _apply_repair(case, patch)
                verification = verify(patched)
                result = _plan(patched, patched['sql'], patched['docs'])
    claim = _classify_full(case, result, verification, ledger, version_check=version_check,
                           conflict_gate=conflict_gate)
    claim['pre_repair'] = pre
    claim['elapsed_ms'] = round((time.perf_counter() - started) * 1000, 4)
    return claim


def _classify_full(case, result, verification, ledger, *, version_check=True, conflict_gate=True):
    """Turn one pipeline result into the arm's single answer claim plus its verdict."""
    metric = case['primary']['metric']
    records = list(result.get('conflicts') or [])
    unresolved_records = [record for record in records
                          if record.get('resolution_status') != 'resolved_by_registered_authority']
    selected = None
    for record in records:
        if record.get('selected_node_id'):
            selected = record['selected_node_id']
    side = 'doc' if case['primary']['side'] == 'doc' else 'sql'
    claim = _side_claim(result, side, metric, node_id=selected if side == 'doc' else None)
    if claim is None:
        claim = _side_claim(result, 'sql', metric) or _side_claim(result, 'doc', metric)
    binding = (claim or {}).get('binding') or {}
    answer_binding = {'entity_id': binding.get('entity_id'), 'version': binding.get('version'),
                      'unit': binding.get('unit'), 'time_scope': binding.get('time_scope')}
    basis = _basis_mismatch(case)
    resolution = _resolution(case, result, verification, basis)
    conflict_detected = bool(records)
    if not conflict_gate:
        conflict_detected = False
    blocked = bool(unresolved_records) if conflict_gate else False
    version_ok = verification['version_ok'] if version_check else True
    if conflict_gate:
        confidence = 'high' if (result.get('status') == 'completed' and version_ok and not blocked
                                and not verification['unverifiable']) else 'low'
    else:
        # Degraded variant: an unresolved conflict does not stop a confident answer.
        confidence = 'high' if (result.get('status') in ('completed', 'abstained')
                                and version_ok and not verification['unverifiable']) else 'low'
    sides = []
    for record in unresolved_records or records:
        for key in ('left', 'right'):
            value_side = record.get(key) or {}
            if value_side:
                sides.append({'source_kind': value_side.get('source_kind'),
                              'value': value_side.get('value'), 'unit': value_side.get('unit'),
                              'version': value_side.get('version'),
                              'node_id': value_side.get('node_id')})
    selected_source = None
    for record in records:
        if record.get('selected_node_id'):
            selected_source = record['selected_node_id']
    selected_source = (claim or {}).get('source_kind') if selected_source else None
    repair = {'attempts': len(ledger), 'applied': [entry for entry in ledger if entry.get('applied')],
              'entries': ledger}
    out = _claim(case, answer_binding, confidence=confidence,
                 value=(claim or {}).get('value'), unit=(claim or {}).get('unit'),
                 asserted=confidence == 'high', source=(claim or {}).get('source_kind') or 'sql',
                 problems=(claim or {}).get('binding_problems') or [],
                 conflict_detected=conflict_detected, resolution=resolution, sides=sides,
                 selected_source=selected_source if len(sides) > 0 else None, repair=repair)
    out['status'] = result.get('status')
    out['error_code'] = result.get('error_code')
    out['failure_detail'] = result.get('failure_detail')
    out['answer'] = result.get('answer')
    out['conflicts'] = [{'conflict_type': record.get('conflict_type'),
                         'resolution_status': record.get('resolution_status'),
                         'left': record['left'].get('value'), 'right': record['right'].get('value'),
                         'left_unit': record['left'].get('unit'),
                         'right_unit': record['right'].get('unit')} for record in records]
    out['applicable_versions'] = verification['applicable']
    out['version_status'] = verification['version_status']
    out['arm'] = 'full'
    return out


ARM_FUNCTIONS = {'plain': arm_plain, 'bind_only': arm_bind_only, 'full': arm_full}


# ------------------------------------------------------------------ scoring

def _unit_equal(left, right):
    status_left = binding_mod.unit_status(left)
    status_right = binding_mod.unit_status(right)
    if status_left['unit_unknown'] or status_right['unit_unknown']:
        return (left or None) == (right or None)
    return status_left['unit'] == status_right['unit']


def _scope_equal(left, right):
    if left is None or right is None:
        return left is None and right is None
    bounds_left = scope_bounds(left)
    bounds_right = scope_bounds(right)
    if bounds_left is None or bounds_right is None:
        return left == right
    return bounds_left == bounds_right


def binding_equal(actual, expected):
    """Field-level binding equality: entity (normalised), version (exact), unit (canonical),
    time scope (parsed bounds; a naive timestamp never equals a timezone-aware one)."""
    actual = actual or {}
    expected = expected or {}
    if binding_mod.normalize_label(actual.get('entity_id')) != \
            binding_mod.normalize_label(expected.get('entity_id')):
        return False
    if (actual.get('version') or None) != (expected.get('version') or None):
        return False
    if not _unit_equal(actual.get('unit'), expected.get('unit')):
        return False
    return _scope_equal(actual.get('time_scope'), expected.get('time_scope'))


def _binding_field_correct(actual, expected):
    actual = actual or {}
    expected = expected or {}
    return {
        'entity_id': binding_mod.normalize_label(actual.get('entity_id')) ==
                     binding_mod.normalize_label(expected.get('entity_id')),
        'version': (actual.get('version') or None) == (expected.get('version') or None),
        'unit': _unit_equal(actual.get('unit'), expected.get('unit')),
        'time_scope': _scope_equal(actual.get('time_scope'), expected.get('time_scope')),
    }


def score_case(case, outcome):
    """Compare one arm's answer claim with the frozen labels, preserving every detail."""
    expected = case['expected_binding']
    answerable = bool(case['expected_answerable'])
    fields = _binding_field_correct(outcome.get('binding'), expected)
    if answerable:
        binding_correct = binding_equal(outcome.get('binding'), expected)
    elif case['expected_conflict']:
        # A discrepancy exists: the arm must detect it *and* refuse to assert a single answer.
        binding_correct = (outcome.get('confidence') != 'high') and bool(outcome.get('conflict_detected'))
    else:
        # The reason is a binding/verification gap (no competing claim): refusing is the answer.
        binding_correct = outcome.get('confidence') != 'high'
    conflict_correct = bool(outcome.get('conflict_detected')) == bool(case['expected_conflict'])
    resolution_correct = outcome.get('resolution') == case['expected_resolution']
    misbinding = (not binding_correct) and outcome.get('confidence') == 'high'
    handled = (binding_correct and conflict_correct and (not misbinding)
               and (resolution_correct or not (outcome.get('resolution') != 'not_evaluated')))
    return {'case_id': case['case_id'], 'trap_kind': case['trap_kind'], 'arm': outcome['arm'],
            'expected_binding': expected, 'actual_binding': outcome.get('binding'),
            'label_fields': fields,
            'expected_answerable': answerable,
            'expected_conflict': bool(case['expected_conflict']),
            'expected_resolution': case['expected_resolution'],
            'actual_resolution': outcome.get('resolution'),
            'confidence': outcome.get('confidence'),
            'binding_correct': bool(binding_correct), 'conflict_correct': bool(conflict_correct),
            'resolution_correct': bool(resolution_correct), 'misbinding': bool(misbinding),
            'handled_correct': bool(handled),
            'evaluates_resolution': outcome.get('resolution') != 'not_evaluated',
            'conflict_detected': bool(outcome.get('conflict_detected')),
            'asserted': bool(outcome.get('asserted')),
            'repair': outcome.get('repair'), 'elapsed_ms': outcome.get('elapsed_ms'),
            'model_calls': int(outcome.get('model_calls') or 0),
            'sides': outcome.get('sides'), 'selected_source': outcome.get('selected_source'),
            'status': outcome.get('status'), 'error_code': outcome.get('error_code')}


def summarize_ledger(entries, pre_correct=None, post_correct=None):
    """Repair-ledger arithmetic.  Denominators are reported with every ratio (zero -> N/A)."""
    entries = list(entries or [])
    changed = [entry for entry in entries if entry.get('applied')]
    attempts = len(entries)
    succeeded = 0
    redundant = 0
    misrepair = 0
    for index, _entry in enumerate(changed):
        if pre_correct is None or post_correct is None:
            continue
        pre = pre_correct[index]
        post = post_correct[index]
        if (not pre) and post:
            succeeded += 1
        elif pre and post:
            redundant += 1
        else:
            misrepair += 1
    return {'attempts': attempts, 'changed': len(changed), 'applied': len(changed),
            'rejected': attempts - len(changed), 'succeeded': succeeded, 'redundant': redundant,
            'misrepair': misrepair,
            'success_rate': ratio(succeeded, len(changed)),
            'over_repair_rate': ratio(misrepair, len(changed)),
            'by_kind': {kind: sum(1 for entry in entries if entry.get('kind') == kind)
                        for kind in sorted({entry.get('kind') for entry in entries if entry.get('kind')})}}


def summarize(records):
    cases = len(records)
    binding_correct = sum(1 for item in records if item['binding_correct'])
    asserted = [item for item in records if item['confidence'] == 'high']
    misbinding = sum(1 for item in records if item['misbinding'])
    # Conflict confusion matrix over the frozen labels (comparable claim pairs only).
    true_conflicts = [item for item in records if item['expected_conflict']]
    false_expected = [item for item in records if not item['expected_conflict']]
    true_positive = sum(1 for item in true_conflicts if item['conflict_detected'])
    false_negative = len(true_conflicts) - true_positive
    false_positive = sum(1 for item in false_expected if item['conflict_detected'])
    true_negative = len(false_expected) - false_positive
    evaluated = [item for item in records if item['evaluates_resolution']]
    answerable_records = [item for item in records if item['expected_answerable']]
    field_names = ('entity_id', 'version', 'unit', 'time_scope')

    def field_table(subset):
        return {field: {'numerator': sum(1 for item in subset if item['label_fields'][field]),
                        'denominator': len(subset),
                        'value': ratio(sum(1 for item in subset if item['label_fields'][field]),
                                       len(subset))}
                for field in field_names}

    return {
        'cases': cases,
        'binding_correct': binding_correct,
        'binding_accuracy': ratio(binding_correct, cases),
        'binding_field_accuracy': field_table(answerable_records),
        'binding_field_accuracy_all_cases': field_table(records),
        'asserted_high': len(asserted),
        'abstained_or_low': cases - len(asserted),
        'misbinding': misbinding,
        'misbinding_rate': ratio(misbinding, len(asserted)),
        'misbinding_rate_all_cases': ratio(misbinding, cases),
        'conflict': {
            'expected_true': len(true_conflicts), 'expected_false': len(false_expected),
            'true_positive': true_positive, 'false_positive': false_positive,
            'false_negative': false_negative, 'true_negative': true_negative,
            'precision': ratio(true_positive, true_positive + false_positive),
            'recall': ratio(true_positive, true_positive + false_negative),
            'false_positive_rate': ratio(false_positive, false_positive + true_negative),
        },
        'resolution_accuracy': ratio(sum(1 for item in evaluated if item['resolution_correct']),
                                     len(evaluated)),
        'resolution_evaluated': len(evaluated),
        'handled_correct': sum(1 for item in records if item['handled_correct']),
        'handled_correct_rate': ratio(sum(1 for item in records if item['handled_correct']), cases),
    }


def _failure_entry(item):
    reasons = []
    if not item['binding_correct']:
        if item['expected_answerable']:
            reasons.append('BINDING_MISMATCH:' + _binding_diff(item['actual_binding'],
                                                               item['expected_binding']))
        else:
            reasons.append('ASSERTED_WITHOUT_DETECTION' if item['asserted']
                           else 'NO_CONFLICT_DETECTED')
    if not item['conflict_correct']:
        reasons.append('CONFLICT_FALSE_NEGATIVE' if item['expected_conflict']
                       else 'CONFLICT_FALSE_POSITIVE')
    if item['evaluates_resolution'] and not item['resolution_correct']:
        reasons.append(f"RESOLUTION_MISMATCH:{item['actual_resolution']}"
                       f"!={item['expected_resolution']}")
    if item['misbinding']:
        reasons.append('MISBINDING_AT_HIGH_CONFIDENCE')
    if not reasons:
        return None
    return {'case_id': item['case_id'], 'trap_kind': item['trap_kind'], 'arm': item['arm'],
            'expected': {'binding': item['expected_binding'],
                         'answerable': item['expected_answerable'],
                         'conflict': item['expected_conflict'],
                         'resolution': item['expected_resolution']},
            'actual': {'binding': item['actual_binding'], 'confidence': item['confidence'],
                       'asserted': item['asserted'], 'conflict_detected': item['conflict_detected'],
                       'resolution': item['actual_resolution'], 'status': item['status']},
            'why': ';'.join(reasons)}


def _binding_diff(actual, expected):
    actual = actual or {}
    expected = expected or {}
    parts = []
    for field in ('entity_id', 'version', 'unit', 'time_scope'):
        left, right = actual.get(field), expected.get(field)
        if _canonical(left) != _canonical(right):
            parts.append(f'{field}={left!r}!={right!r}')
    return ','.join(parts) or 'UNKNOWN'


def run(dataset, *, arms=None):
    """Run every arm over the **same** frozen cases and return metrics plus per-case detail."""
    names = list(arms or ARMS)
    for name in names:
        if name not in ARM_FUNCTIONS:
            raise ValueError('BINDING_EVAL_ARM_INVALID:' + str(name))
    results = []
    for name in names:
        started = time.perf_counter()
        outcomes = []
        for case in dataset['cases']:
            outcome = ARM_FUNCTIONS[name](case)
            outcome['arm'] = name
            outcomes.append(outcome)
        records = [score_case(case, outcome) for case, outcome in zip(dataset['cases'], outcomes)]
        elapsed = round((time.perf_counter() - started) * 1000, 2)
        metrics = summarize(records)
        # Repair-ledger arithmetic: every changed repair is scored *before* and *after* so a
        # mis-repair (a correct handling rewritten into a wrong one) is measurable.
        ledger_entries, pre_correct, post_correct = [], [], []
        for case, outcome in zip(dataset['cases'], outcomes):
            pre = outcome.get('pre_repair')
            for entry in (outcome.get('repair') or {}).get('entries') or []:
                ledger_entries.append(entry)
                if entry.get('applied'):
                    pre_correct.append(score_case(case, pre)['handled_correct'])
                    post_correct.append(score_case(case, outcome)['handled_correct'])
        ledger = summarize_ledger(ledger_entries, pre_correct, post_correct)
        ledger['cost_ms'] = elapsed
        ledger['model_calls'] = 0
        ledger['tokens'] = 0
        ledger['token_note'] = 'stub 作答器不调用模型，token 成本不适用（恒为 0，不得当作真实成本）'
        results.append({'arm': name, 'metrics': metrics, 'repair_ledger': ledger,
                        'elapsed_ms': elapsed, 'cases': records, 'outcomes': outcomes})
    per_trap_kind = {}
    for kind in TRAP_KINDS:
        per_trap_kind[kind] = {}
        for arm in results:
            subset = [item for item in arm['cases'] if item['trap_kind'] == kind]
            per_trap_kind[kind][arm['arm']] = summarize(subset)
    failures = []
    for arm in results:
        for item in arm['cases']:
            entry = _failure_entry(item)
            if entry:
                failures.append(entry)
    return {'dataset': {'suite': dataset.get('suite'), 'version': dataset.get('version'),
                        'sha256': dataset['sha256'], 'cases': len(dataset['cases']),
                        'counts_by_trap_kind': {kind: sum(1 for case in dataset['cases']
                                                          if case['trap_kind'] == kind)
                                                for kind in TRAP_KINDS}},
            'arms': results, 'per_trap_kind': per_trap_kind, 'failures': failures}


#: Within-``full`` sensitivity probe: which verification sub-step actually carries the gain.
SENSITIVITY_VARIANTS = {
    'full_no_version_check': {'version_check': False},
    'full_no_conflict_gate': {'conflict_gate': False},
    'full_no_repair': {'repair_budget': 0},
}


def run_sensitivity(dataset):
    """Degrade one ``full`` sub-step at a time; the same frozen cases, the same stub answerer."""
    out = {}
    for name, flags in SENSITIVITY_VARIANTS.items():
        outcomes = []
        for case in dataset['cases']:
            outcome = arm_full(case, **flags)
            outcome['arm'] = name
            outcomes.append(outcome)
        metrics = summarize([score_case(case, outcome)
                             for case, outcome in zip(dataset['cases'], outcomes)])
        out[name] = {'cases': metrics['cases'],
                     'binding_accuracy': metrics['binding_accuracy'],
                     'misbinding_rate': metrics['misbinding_rate'],
                     'misbinding_rate_all_cases': metrics['misbinding_rate_all_cases'],
                     'asserted_high': metrics['asserted_high'],
                     'conflict_recall': metrics['conflict']['recall'],
                     'false_positive_rate': metrics['conflict']['false_positive_rate'],
                     'handled_correct_rate': metrics['handled_correct_rate'],
                     'flags': flags}
    return out


def judge(per_arm):
    """The WP3 acceptance criterion, decided by the numbers and nothing else."""
    plain = (per_arm.get('plain') or {}).get('misbinding_rate')
    full = (per_arm.get('full') or {}).get('misbinding_rate')
    criterion = 'full 臂错误绑定率相对 plain 臂下降 ≥50% 且绝对值 ≤5%'
    if plain is None or full is None:
        return {'criterion': criterion, 'plain_misbinding_rate': plain,
                'full_misbinding_rate': full, 'relative_drop': None, 'meets': False,
                'reason': 'ZERO_DENOMINATOR:至少一侧没有高置信输出，门槛不可判定'}
    if plain == 0:
        return {'criterion': criterion, 'plain_misbinding_rate': plain,
                'full_misbinding_rate': full, 'relative_drop': None, 'meets': False,
                'reason': 'PLAIN_HAS_NO_MISBINDING:基线本来就不犯错，下降幅度无法定义，'
                          '本项不构成技术领先性证据'}
    drop = round((plain - full) / plain, 4)
    meets = drop >= 0.5 and full <= 0.05
    return {'criterion': criterion, 'plain_misbinding_rate': plain,
            'full_misbinding_rate': full, 'relative_drop': drop, 'absolute_limit': 0.05,
            'meets': bool(meets),
            'reason': None if meets else 'CRITERION_NOT_MET',
            'plain_already_clean': bool(plain <= 0.05)}
