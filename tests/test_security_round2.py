"""第二轮（修复轮）安全 / 向后兼容回归：H1 / M1 / M2 / M3 / M4 / L2 / L5。

每条断言对应 ``reports/review_round1_security.md`` §5 的一条发现，并尽量写成
「修复前会怎样 → 修复后必须怎样」两个方向，避免只有单侧证据：

* ``H1`` 空 allow-list：``database_sources=[]`` 必须被拒（不是自动选源）。
* ``M1`` 时间口径：``_frame_scope`` 必须带上兄弟键 ``timezone``（无时区仍拒答）。
* ``M2`` 集合白名单：只认 ``knowledge.COLLECTIONS``，``public_kb`` 这类不存在的名字必须被拒。
* ``M3`` migration 019：``evidence`` 列不得再是 NOT NULL；注释必须写清真实迁移顺序
  （种子脚本会**间接**套用迁移，但只作用于已初始化的克隆，fresh 分支仍未被执行）。
* ``M4`` 别名注册表不可用：实体身份未验证时不得 ``completed``。
* ``L2`` 单位表：``degF`` / ``万元`` 必须可确定性换算；不可换算的记 ``unsupported_unit``。
* ``L5`` 事件一致性：落库的 trace 事件与 SSE 交付的事件在澄清键上必须一致
  （并复刻改前的「先落库后 sink」顺序作为反例）。

DB 存取与既有用例一致；``scripts/test_isolated.py`` 会把 ``datamind_meta`` 重定向到一次性克隆库，
本文件自己清理它创建的 run。
"""
from __future__ import annotations

import copy
import hashlib
from datetime import datetime, timezone
import re
from pathlib import Path
import unittest
from unittest.mock import patch

import psycopg2
from psycopg2.extras import Json

from datamind import binding as binding_mod
from datamind import cross_source as cross_source_mod
from datamind import knowledge as knowledge_mod
from datamind import sql_engine
from datamind.agent import DATABASE_SOURCES, DataMindAgent, _plain, route_for
from datamind.evidence_graph import canonical_json

ROOT = Path(__file__).resolve().parents[1]
WRITE = 'host=127.0.0.1 port=5432 dbname=datamind_meta user=datamind_app password=datamind_app_dev'
SHA_A = 'a' * 64
SCOPE_2025 = {'start': '2025-01-01T00:00:00+08:00', 'end': '2026-01-01T00:00:00+08:00'}
CROSS_QUESTION = '查询销售额前三产品并结合市场分析报告说明原因'
CLARIFY_QUESTION = '销售额最高的品类是哪个？'
CLARIFICATION_KEYS = ('clarification_contract_version', 'clarification_missing_slots',
                      'clarification_candidates', 'clarification_slot_questions')

TRACE_INSERT = """INSERT INTO trace_events(event_id, run_id, agent_name, event_type, status,
                       started_at, input_summary, output_summary, error_code, tool_name)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"""


def cross_evidence(docs=True):
    """The same completing fixture ``tests/test_cross_source.py`` uses, kept local and explicit."""
    rows = [{'category': '家用电器', 'metric': 'sales_amount', 'value': '128000.50',
             'unit': 'CNY', 'time_scope': SCOPE_2025}]
    blocks = []
    if docs:
        blocks.append({'document_id': 'doc-channel', 'document_version': '2025-04',
                       'source_sha256': SHA_A, 'chunk_id': 'doc-channel-c1',
                       'title': 'doc-channel', 'entity_label': '家用电器',
                       'text': '家用电器品类 2025 年渠道经验：线下门店转化率最高。'})
    sql_evidence = {'source_ref': 'demo_retail', 'snapshot_id': 'retail-snapshot-v3',
                    'columns': [],
                    'sql_hash': hashlib.sha256(canonical_json(rows).encode()).hexdigest(),
                    'rows': rows}
    return {'sql': sql_evidence, 'docs': blocks}


def plan_kwargs(evidence=None):
    evidence = evidence if evidence is not None else cross_evidence()
    return {'sql_evidence': evidence['sql'], 'rag_evidence': {'blocks': evidence['docs']}}


class _RunCleanup(unittest.TestCase):
    """Every run this file creates is deleted again, so the clone is left as it was found."""

    def setUp(self):
        self.run_ids = []

    def tearDown(self):
        if not self.run_ids:
            return
        with psycopg2.connect(WRITE) as conn, conn.cursor() as cur:
            for run_id in self.run_ids:
                for table in ('trace_events', 'evidence_edges', 'evidence_nodes', 'claims', 'runs'):
                    cur.execute(f'DELETE FROM {table} WHERE run_id=%s', (run_id,))

    def run_agent(self, agent, **kwargs):
        self.run_ids.append(agent.run_id)
        try:
            return agent.run(**kwargs)
        finally:
            agent.close()


# ---------------------------------------------------------------- H1

class H1EmptyAllowListTest(_RunCleanup):
    def test_the_cross_source_guard_rejects_an_empty_allow_list_like_the_sql_path(self):
        # The predicate is the guard's own predicate: `not sources or any(...)`.
        # R4: this predicate is evaluated on the *normalized* scope in both shipped paths (the SQL
        # path normalizes ``None`` to ``DATABASE_SOURCES`` before its identical guard), so ``None``
        # never arrives here from a real request. ``None`` means "default scope", not "empty
        # allow-list"; the denial is reserved for an explicit ``[]`` — see
        # ``test_none_is_the_default_scope_while_an_empty_list_is_denied`` below.
        reject = lambda sources: (not sources or any(s not in DATABASE_SOURCES for s in sources))
        self.assertTrue(reject([]), '空 allow-list 必须被拒（H1）')
        self.assertFalse(reject(['demo_retail']), '合法单源不能被拒')
        self.assertTrue(reject(['nope']), '未注册源必须被拒')
        self.assertTrue(reject(None), '与 SQL 路径同形：None 也拒')
        self.assertEqual(route_for(CROSS_QUESTION), 'cross_source', '该问法确实走跨源分支')

    def test_plan_with_an_empty_allow_list_refuses_instead_of_picking_a_source(self):
        result = cross_source_mod.plan(CROSS_QUESTION, [], ['demo_kb'], **plan_kwargs())
        self.assertEqual((result['status'], result['error_code']),
                         ('failed', 'SOURCE_NOT_ALLOWED'))
        self.assertEqual(result['failure_detail'], 'EMPTY_ALLOW_LIST')
        self.assertEqual(result['sources'], [])
        self.assertEqual(result['sql_evidence']['present'], False)
        self.assertEqual(result['claims'], [])

    def test_the_sql_evidence_entry_point_refuses_an_empty_allow_list(self):
        with self.assertRaises(cross_source_mod.CrossSourceError) as caught:
            cross_source_mod.live_sql_evidence(CROSS_QUESTION, [])
        self.assertEqual(caught.exception.code, 'SOURCE_NOT_ALLOWED')
        self.assertEqual(caught.exception.detail, 'EMPTY_ALLOW_LIST')

    def test_a_non_empty_allow_list_still_completes(self):
        satisfied = cross_source_mod.plan(CROSS_QUESTION, ['demo_retail'], ['demo_kb'],
                                          **plan_kwargs())
        self.assertEqual((satisfied['status'], satisfied['error_code']), ('completed', None))

    def test_the_agent_refuses_an_empty_scope_and_answers_a_real_one(self):
        refused = self.run_agent(DataMindAgent(), question=CROSS_QUESTION, database_sources=[],
                                 document_collections=['demo_kb'],
                                 cross_source_evidence=cross_evidence())
        self.assertEqual((refused['status'], refused['error_code']),
                         ('FAILED_WITH_EVIDENCE', 'SOURCE_NOT_ALLOWED'))
        self.assertNotIn('cross_source_result', refused)
        accepted = self.run_agent(DataMindAgent(), question=CROSS_QUESTION,
                                  database_sources=['demo_retail'],
                                  document_collections=['demo_kb'],
                                  cross_source_evidence=cross_evidence())
        self.assertEqual(accepted['status'], 'COMPLETED')
        self.assertEqual(accepted['cross_source_result']['status'], 'completed')

    def test_none_is_the_default_scope_while_an_empty_list_is_denied(self):
        """R4: "no scope" is the default scope; only an explicit ``[]`` denies.

        Round 2 applied the H1 guard to the **raw** cross-source scope, so ``None`` was refused
        like ``[]``. This pins both directions at the shipped agent boundary, and keeps the scope
        verdict (``SOURCE_NOT_ALLOWED`` / ``COLLECTION_NOT_ALLOWED``) apart from the evidence
        verdicts the graph engine emits after the scope was accepted.
        """
        default_scope = self.run_agent(DataMindAgent(), question=CROSS_QUESTION,
                                       database_sources=None, document_collections=None,
                                       cross_source_evidence=cross_evidence())
        self.assertEqual(default_scope['status'], 'COMPLETED', default_scope.get('error_code'))
        self.assertNotIn(default_scope.get('error_code'),
                         {'SOURCE_NOT_ALLOWED', 'COLLECTION_NOT_ALLOWED'})
        refused = self.run_agent(DataMindAgent(), question=CROSS_QUESTION, database_sources=[],
                                 document_collections=None, cross_source_evidence=cross_evidence())
        self.assertEqual((refused['status'], refused['error_code']),
                         ('FAILED_WITH_EVIDENCE', 'SOURCE_NOT_ALLOWED'))
        refused_docs = self.run_agent(DataMindAgent(), question=CROSS_QUESTION,
                                      database_sources=['demo_retail'], document_collections=[],
                                      cross_source_evidence=cross_evidence())
        self.assertEqual((refused_docs['status'], refused_docs['error_code']),
                         ('FAILED_WITH_EVIDENCE', 'COLLECTION_NOT_ALLOWED'))
        self.assertNotIn('cross_source_result', refused)
        self.assertNotIn('cross_source_result', refused_docs)


# ---------------------------------------------------------------- M1

class M1FrameScopeTimezoneTest(unittest.TestCase):
    def test_the_real_query_frame_shape_now_yields_a_timezone_aware_scope(self):
        # 线上 `sql_engine.query_frame` 的实测形状：裸日期 + 兄弟键 timezone。
        frame = {'query_frame': {'time_filters': [
            {'field_id': 'orders.created_at', 'from': '2025-01-01', 'to': '2026-01-01',
             'timezone': 'Asia/Shanghai'}]}}
        scope = cross_source_mod._frame_scope(frame)
        self.assertEqual(scope, {'start': '2025-01-01T00:00:00+08:00',
                                 'end': '2026-01-01T00:00:00+08:00'})
        self.assertIsNotNone(binding_mod._scope_bounds(scope))

    def test_a_frame_without_a_timezone_stays_naive_and_is_still_refused(self):
        scope = cross_source_mod._frame_scope({'query_frame': {'time_filters': [
            {'field_id': 'orders.created_at', 'from': '2025-01-01', 'to': '2026-01-01'}]}})
        self.assertEqual(scope, {'start': '2025-01-01', 'end': '2026-01-01'})
        self.assertIsNone(binding_mod._scope_bounds(scope),
                          '没有时区信息时仍必须拒答（TIMEZONE_REQUIRED 纪律不变）')

    def test_an_unusable_timezone_is_not_guessed_at(self):
        scope = cross_source_mod._frame_scope({'query_frame': {'time_filters': [
            {'from': '2025-01-01', 'to': '2026-01-01', 'timezone': 'Mars/Olympus'}]}})
        self.assertIsNone(binding_mod._scope_bounds(scope), '坏时区不得被替换成默认时区')

    def test_an_explicit_offset_is_left_untouched(self):
        scope = cross_source_mod._frame_scope({'query_frame': {'time_filters': [
            {'from': '2025-01-01T00:00:00+08:00', 'to': '2026-01-01T00:00:00+08:00',
             'timezone': 'UTC'}]}})
        self.assertEqual(scope['start'], '2025-01-01T00:00:00+08:00')

    def test_the_timezone_is_what_makes_the_whole_chain_bind(self):
        """改前（丢掉 timezone）必然 `SQL_CLAIM_BINDING_INCOMPLETE:time_scope`，改后 completed。"""
        def plan_with(filters):
            rows = [{'category': '家用电器', 'metric': 'sales_amount', 'value': '128000.50',
                     'unit': 'CNY',
                     'time_scope': cross_source_mod._frame_scope(
                         {'query_frame': {'time_filters': filters}})}]
            evidence = cross_evidence()
            evidence['sql'] = {'source_ref': 'demo_retail', 'snapshot_id': 'retail-snapshot-v3',
                               'columns': [],
                               'sql_hash': hashlib.sha256(
                                   canonical_json(rows).encode()).hexdigest(), 'rows': rows}
            return cross_source_mod.plan(CROSS_QUESTION, ['demo_retail'], ['demo_kb'],
                                         **plan_kwargs(evidence))

        dropped = plan_with([])
        self.assertEqual(dropped['failure_detail'], 'SQL_CLAIM_BINDING_INCOMPLETE:time_scope')
        kept = plan_with([{'field_id': 'orders.created_at', 'from': '2025-01-01',
                           'to': '2026-01-01', 'timezone': 'Asia/Shanghai'}])
        self.assertEqual((kept['status'], kept['error_code']), ('completed', None))

    def test_the_shape_still_matches_sql_engine_query_frame(self):
        """With a real Catalog the frame really is bare dates + a sibling timezone."""
        from datamind.catalog import Catalog
        from datamind.agent import _read_dsn
        frame = sql_engine.query_frame('2025年的销售额是多少？', Catalog(_read_dsn(), 'demo_retail'), [])
        filters = frame['time_filters']
        self.assertTrue(filters, '该问句应解析出时间过滤')
        for item in filters:
            self.assertIn('timezone', item)
            self.assertRegex(str(item['from']), r'^\d{4}-\d{2}-\d{2}$',
                             'from/to 是裸日期：时区只在兄弟键里（M1 的根因）')


# ---------------------------------------------------------------- M2

class M2CollectionWhitelistTest(_RunCleanup):
    def test_the_retriever_vocabulary_is_the_only_list(self):
        self.assertEqual(knowledge_mod.COLLECTIONS,
                         {'demo_kb', 'ohrbench_kb', 'industrial_demo'})
        source = (ROOT / 'datamind' / 'agent.py').read_text(encoding='utf-8')
        self.assertIn('knowledge.COLLECTIONS', source, '跨源守卫必须引用单一来源')
        self.assertNotIn('{"public_kb"', source, '不得再写死第二份集合白名单')
        self.assertNotIn('"ohr_bench"', source, '旧白名单里的错名不得残留（代码中）')

    def test_every_real_collection_is_accepted_by_the_agent(self):
        for collection in sorted(knowledge_mod.COLLECTIONS):
            result = self.run_agent(DataMindAgent(), question=CROSS_QUESTION,
                                    database_sources=['demo_retail'],
                                    document_collections=[collection],
                                    cross_source_evidence=cross_evidence())
            self.assertEqual(result['status'], 'COMPLETED',
                             f'{collection} 必须被接受，得到 {result.get("error_code")}')

    def test_the_legacy_misspelled_collection_is_now_refused(self):
        """契约升级：`public_kb` 从来不是真实集合，改前通过只是因为白名单写错了名字。"""
        result = self.run_agent(DataMindAgent(), question=CROSS_QUESTION,
                                database_sources=['demo_retail'],
                                document_collections=['public_kb'],
                                cross_source_evidence=cross_evidence())
        self.assertEqual((result['status'], result['error_code']),
                         ('FAILED_WITH_EVIDENCE', 'COLLECTION_NOT_ALLOWED'))


# ---------------------------------------------------------------- M3

class M3MigrationFreshOrderTest(unittest.TestCase):
    """静态核验（没有对 `datamind_meta` 执行任何迁移）。

    真实的一次性临时库 fresh-order 执行（改前 NOT NULL 失败 / 改后通过）记录在
    ``reports/cards/r2_security_fixes.md``；这里钉住的是「声明必须与事实一致」。
    """
    @classmethod
    def setUpClass(cls):
        cls.sql = (ROOT / 'db' / 'migrations' / '019_evidence_graph.sql').read_text(encoding='utf-8')

    def test_the_evidence_column_is_not_null_so_the_legacy_writers_keep_working(self):
        match = re.search(r'^\s*evidence\s+TEXT([^,\n]*)', self.sql, re.MULTILINE)
        self.assertIsNotNone(match, '找不到 evidence 列定义')
        self.assertNotIn('NOT NULL', match.group(1),
                         'legacy 写入者不带 evidence 列，NOT NULL 会让 fresh-first 顺序失败')
        self.assertIn('CHECK (evidence IS NULL OR length(btrim(evidence)) > 0)', self.sql,
                      '显式提供的空白 justification 仍必须被拒')

    def test_every_writer_omits_the_evidence_column(self):
        agent_source = (ROOT / 'datamind' / 'agent.py').read_text(encoding='utf-8')
        graph_source = (ROOT / 'datamind' / 'evidence_graph.py').read_text(encoding='utf-8')
        for name, source in (('agent.py', agent_source), ('evidence_graph.py', graph_source)):
            statements = re.findall(r'INSERT INTO evidence_edges\(([^)]*)\)', source)
            self.assertTrue(statements, f'{name} 找不到 evidence_edges 的 INSERT')
            for columns in statements:
                self.assertNotIn('evidence', columns.replace('evidence_', ''),
                                 f'{name} 的列清单带上了 evidence：{columns}')

    def test_the_comment_states_the_real_migration_order(self):
        """复查报告称「harness 不跑迁移」——实际是**间接**跑（种子脚本调用 migrate），必须写对。"""
        self.assertNotIn('applies every file in `db/migrations/`, so the', self.sql,
                         '不实声明必须删掉')
        self.assertIn('db/seed_industrial.py', self.sql)
        self.assertIn('db/migrate.py', self.sql)
        self.assertIn('never exercised', self.sql,
                      '必须写明 section 1 在克隆上是 no-op、fresh 分支未被执行')
        self.assertIn('remains unverified', self.sql,
                      'fresh-first + 完整 deploy/init 的未验证状态必须写明')
        # 注释描述的调用链必须真的存在，否则又是一条「文档声称 > 实证」。
        isolated = (ROOT / 'scripts' / 'test_isolated.py').read_text(encoding='utf-8')
        self.assertIn('db/seed_industrial.py', isolated)
        seed = (ROOT / 'db' / 'seed_industrial.py').read_text(encoding='utf-8')
        self.assertIn('from db.migrate import migrate', seed)
        applier = (ROOT / 'db' / 'migrate.py').read_text(encoding='utf-8')
        self.assertIn('(ROOT / "db/migrations").glob("*.sql")', applier)
        self.assertIn('print("applied", path.name)', applier)


# ---------------------------------------------------------------- M4

class M4AliasRegistryTest(unittest.TestCase):
    def test_an_unavailable_registry_degrades_the_verdict_instead_of_completing(self):
        with patch.object(binding_mod, 'load_aliases', side_effect=RuntimeError('registry down')):
            degraded = cross_source_mod.run(
                CROSS_QUESTION, database_sources=['demo_retail'],
                document_collections=['demo_kb'], authority_rules=[],
                sql_adapter=lambda: cross_evidence()['sql'],
                doc_adapter=lambda: {'blocks': cross_evidence()['docs']})
        self.assertFalse(degraded['alias_registry_available'])
        self.assertNotEqual(degraded['status'], 'completed', '注册表不可用不得判 completed')
        self.assertEqual(degraded['status'], 'abstained')
        self.assertEqual(degraded['failure_detail'],
                         'ENTITY_UNVERIFIED:ALIAS_REGISTRY_UNAVAILABLE')
        self.assertTrue(any(w.startswith('ALIAS_REGISTRY_UNAVAILABLE:')
                            for w in degraded['warnings']))

    def test_the_same_evidence_completes_when_the_registry_is_available(self):
        """正向对照：降级只针对「注册表不可用」，不是把跨源一律拒掉。"""
        with patch.object(binding_mod, 'load_aliases', return_value=[]):
            healthy = cross_source_mod.run(
                CROSS_QUESTION, database_sources=['demo_retail'],
                document_collections=['demo_kb'], authority_rules=[],
                sql_adapter=lambda: cross_evidence()['sql'],
                doc_adapter=lambda: {'blocks': cross_evidence()['docs']})
        self.assertTrue(healthy['alias_registry_available'])
        self.assertEqual((healthy['status'], healthy['error_code']), ('completed', None))

    def test_plan_marks_the_registry_state_and_abstains_when_it_is_unavailable(self):
        degraded = cross_source_mod.plan(CROSS_QUESTION, ['demo_retail'], ['demo_kb'],
                                         aliases=[], alias_registry_available=False,
                                         **plan_kwargs())
        self.assertEqual(degraded['status'], 'abstained')
        self.assertEqual(degraded['failure_detail'], 'ENTITY_UNVERIFIED:ALIAS_REGISTRY_UNAVAILABLE')
        healthy = cross_source_mod.plan(CROSS_QUESTION, ['demo_retail'], ['demo_kb'],
                                        aliases=[], alias_registry_available=True, **plan_kwargs())
        self.assertEqual(healthy['status'], 'completed')


# ---------------------------------------------------------------- L2

def _temperature_case(unit):
    rows = [{'device': '家用电器', 'metric': 'temperature', 'value': '60', 'unit': 'degC',
             'entity_label': '家用电器', 'time_scope': SCOPE_2025}]
    clause = {'clause_id': 'CL-TEMP', 'metric': 'temperature', 'op': '<=', 'limit': '140',
              'unit': unit, 'valid_from': '2025-01-01T00:00:00+08:00',
              'valid_to': '2027-01-01T00:00:00+08:00'}
    blocks = [{'document_id': 'doc-temp', 'document_version': 'snap-1', 'source_sha256': SHA_A,
               'chunk_id': 'c1', 'title': '温度制度', 'entity_label': '家用电器',
               'text': f'设备温度不得超过 140 {unit}。', 'clause': clause}]
    sql_evidence = {'source_ref': 'demo_retail', 'snapshot_id': 'snap-1', 'columns': [],
                    'sql_hash': hashlib.sha256(canonical_json(rows).encode()).hexdigest(),
                    'rows': rows}
    return {'sql': sql_evidence, 'docs': blocks}


class L2UnitTableTest(unittest.TestCase):
    REGEX_UNITS = ('degC', 'degF', 'K', '℃', '°C', 'mm', 'cm', 'km', 'kg', 'g', 't', 'min', 'h',
                   's', 'day', '天', '小时', '分钟', '秒', '%', '元', '万元', '件', '个', '条', '笔',
                   '人')

    def test_every_unit_the_clause_regex_accepts_is_registered(self):
        unknown = [unit for unit in self.REGEX_UNITS
                   if binding_mod.unit_status(unit)['unit_unknown']]
        self.assertEqual(unknown, [], '正则接受但单位表未注册 → 合法输入被硬拒（L2）')

    def test_degf_converts_exactly_and_refuses_non_terminating_results(self):
        from decimal import Decimal
        self.assertEqual(binding_mod.unit_status('degF')['dimension'], 'temperature')
        self.assertEqual(binding_mod.normalize_unit('68', 'degF', 'degC'), Decimal('20'))
        self.assertEqual(binding_mod.normalize_unit('212', 'degF', 'degC'), Decimal('100'))
        self.assertEqual(binding_mod.normalize_unit('98.6', 'degF', 'degC'), Decimal('37'))
        self.assertIsNone(binding_mod.normalize_unit('70', 'degF', 'degC'),
                          '190/9 不是有尽小数：必须拒绝而不是四舍五入（与 s→min 同纪律）')
        self.assertTrue(binding_mod.values_agree('68', 'degF', '20', 'degC'))
        self.assertTrue(binding_mod.units_equivalent('degF', 'degC'))
        self.assertEqual(binding_mod.source_agreement('68', 'degF', '20', 'degC')['status'],
                         'resolved_equivalent')

    def test_wan_yuan_is_registered_as_a_currency_scale(self):
        from decimal import Decimal
        self.assertEqual(binding_mod.unit_status('万元')['dimension'], 'currency')
        self.assertEqual(binding_mod.normalize_unit('1', '万元', 'CNY'), Decimal('10000'))
        self.assertTrue(binding_mod.values_agree('1', '万元', '10000', 'CNY'))
        self.assertFalse(binding_mod.values_agree('1', '万元', '1', 'CNY'))

    def test_an_existing_conversion_keeps_its_exact_scale(self):
        from decimal import Decimal
        self.assertEqual(binding_mod.normalize_unit('1123.15', 'K', 'degC'), Decimal('850.00'))
        self.assertIsNone(binding_mod.normalize_unit('1', 's', 'min'))

    def test_a_degf_clause_binds_and_is_compared(self):
        result = cross_source_mod.plan(CROSS_QUESTION, ['demo_retail'], ['demo_kb'],
                                       **plan_kwargs(_temperature_case('degF')))
        self.assertEqual((result['status'], result['error_code']), ('completed', None))
        checks = result['threshold_checks']
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]['status'], 'checked')
        self.assertEqual(checks[0]['unit'], 'degF')
        # 140 degF == 60 degC, and the 60 degC claim is exactly at the limit.
        self.assertEqual(checks[0]['expression'], '60 <= 60')

    def test_an_unconvertible_unit_is_reported_as_unsupported_not_as_a_missing_binding(self):
        clause = cross_source_mod.extract_clause(
            {'chunk_id': 'c1', 'text': '温度不得超过 140 furlong。', 'metric': 'temperature',
             'clause': {'clause_id': 'CL-X', 'metric': 'temperature', 'op': '<=', 'limit': '140',
                        'unit': 'furlong'}})
        self.assertTrue(clause['unsupported_unit'])
        result = cross_source_mod.plan(CROSS_QUESTION, ['demo_retail'], ['demo_kb'],
                                       **plan_kwargs(_temperature_case('furlong')))
        self.assertEqual(result['error_code'], 'DOC_BINDING_MISSING')
        self.assertIn('unsupported_unit', result['failure_detail'])
        self.assertNotIn('unit:UNIT_UNKNOWN', result['failure_detail'])
        clauses = [node for node in result['evidence_graph']['nodes']
                   if node['node_type'] == 'spec_clause']
        self.assertEqual(len(clauses), 1)
        self.assertTrue(clauses[0]['payload']['unsupported_unit'],
                        '抽取阶段必须把「不可换算的单位」标成 unsupported_unit')


# ---------------------------------------------------------------- L5

def _legacy_event(self, event_type, agent_name, status, *, input_summary=None, output_summary=None,
                  error_code=None, tool_name=None):
    """改前顺序的逐字复刻（先落库、后交 sink），只为 L5 的对照。

    与 ``git diff datamind/agent.py`` 中被删掉的 ``- if self.sink: self.sink(event)``
    （位于 INSERT 之后）一致。
    """
    self.sequence += 1
    event = {"event_id": f"evt-{self.run_id[4:]}-{self.sequence:04d}",
             "run_id": self.run_id, "event_type": event_type, "agent_name": agent_name,
             "status": status, "started_at": datetime.now(timezone.utc).isoformat(),
             "input_summary": _plain(input_summary or {}),
             "output_summary": _plain(output_summary or {}),
             "error_code": error_code, "tool_name": tool_name}
    with self.conn.cursor() as cur:
        cur.execute(TRACE_INSERT,
                    (event["event_id"], self.run_id, agent_name, event_type, status,
                     event["started_at"], Json(event["input_summary"]),
                     Json(event["output_summary"]), error_code, tool_name))
    if self.sink:
        self.sink(event)
    return event


class L5EventPersistenceParityTest(_RunCleanup):
    @staticmethod
    def _rule_only_ask(question, source_id=None, **kwargs):
        """只走真实规则式 clarify()，绝不触发模型调用（与 B3 用例同法）。"""
        message = sql_engine.clarify(question)
        if message:
            return {'status': 'clarification_required', 'clarification': message}
        return {'status': 'failed', 'error_code': 'STUB_NO_MODEL'}

    def _clarification_run(self, legacy_order=False):
        from datamind.api import _with_clarification_contract
        delivered = []

        def sink(event):
            if event.get('event_type') == 'answer.clarification':
                event['output_summary'] = _with_clarification_contract(
                    event.get('output_summary') or {}, CLARIFY_QUESTION)
            # 交付给「流」的那一刻的快照（api.chat_stream 是先补齐再入队）。
            delivered.append(copy.deepcopy(event))

        agent = DataMindAgent(sink=sink)
        self.run_ids.append(agent.run_id)
        try:
            with patch('datamind.agent.ask', side_effect=self._rule_only_ask):
                if legacy_order:
                    with patch.object(DataMindAgent, '_event', _legacy_event):
                        result = agent.run(CLARIFY_QUESTION, mode='sql',
                                           database_sources=['demo_retail'])
                else:
                    result = agent.run(CLARIFY_QUESTION, mode='sql',
                                       database_sources=['demo_retail'])
        finally:
            agent.close()
        return result, delivered

    def _persisted_terminal_event(self, run_id):
        with psycopg2.connect(WRITE) as conn, conn.cursor() as cur:
            cur.execute("SELECT output_summary FROM trace_events WHERE run_id=%s "
                        "AND event_type='answer.clarification' ORDER BY event_id DESC LIMIT 1",
                        (run_id,))
            row = cur.fetchone()
        return row[0] if row else None

    def test_the_persisted_event_carries_the_same_clarification_keys_as_the_stream(self):
        result, delivered = self._clarification_run()
        self.assertEqual(result['status'], 'NEEDS_CLARIFICATION')
        streamed = [event for event in delivered
                    if event.get('event_type') == 'answer.clarification']
        self.assertEqual(len(streamed), 1, 'SSE 必须收到一次澄清终态事件')
        streamed_summary = streamed[0]['output_summary']
        for key in CLARIFICATION_KEYS:
            self.assertIn(key, streamed_summary, key)
        persisted = self._persisted_terminal_event(result['run_id'])
        self.assertIsNotNone(persisted, '澄清事件必须已经落库')
        for key in CLARIFICATION_KEYS:
            self.assertEqual(persisted.get(key), streamed_summary.get(key),
                             f'落库与 SSE 在 {key} 上必须一致（L5）')

    def test_the_pre_fix_order_left_the_clarification_keys_out_of_the_trace(self):
        """反例（改前顺序的复刻）：SSE 拿得到、回放拿不到 —— L5 的失败样本。"""
        result, delivered = self._clarification_run(legacy_order=True)
        self.assertEqual(result['status'], 'NEEDS_CLARIFICATION')
        streamed = [event for event in delivered
                    if event.get('event_type') == 'answer.clarification'][0]['output_summary']
        for key in CLARIFICATION_KEYS:
            self.assertIn(key, streamed)
        persisted = self._persisted_terminal_event(result['run_id'])
        self.assertNotIn('clarification_missing_slots', persisted,
                         '改前顺序下落库事件缺少澄清键（这就是被修掉的行为）')


# ---------------------------------------------------------------- H2（静态）


class H2RetiredCodeDocumentationTest(unittest.TestCase):
    DOCS = ('docs/MULTI_TURN.md',)

    def test_the_runtime_no_longer_produces_the_old_code(self):
        for path in sorted((ROOT / 'datamind').glob('*.py')):
            self.assertNotIn('ROUTE_UNSUPPORTED', path.read_text(encoding='utf-8'), path.name)

    def test_every_remaining_docs_mention_marks_it_retired(self):
        for name in self.DOCS:
            text = (ROOT / name).read_text(encoding='utf-8')
            for line in text.splitlines():
                if 'ROUTE_UNSUPPORTED' in line:
                    self.assertIn('退役', line, f'{name} 仍把旧码描述成现行行为：{line[:80]}')
        doc = (ROOT / self.DOCS[0]).read_text(encoding='utf-8')
        self.assertIn('CROSS_SOURCE_EVIDENCE_INSUFFICIENT', doc)
        self.assertIn('DOC_BINDING_MISSING', doc)
        self.assertIn('CONFLICT_UNRESOLVED', doc)


if __name__ == '__main__':
    unittest.main()
