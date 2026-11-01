"""Initialize clearly labelled CAD/dimension/Wiki examples in an isolated demo.

Requires DATAMIND_DEMO_SEED=1, an explicit --dsn (or DATAMIND_META_WRITE_DSN),
and the temporary loopback API at port 18000. Loopback PostgreSQL port 5432 is
always rejected. --allow-seed additionally allows the demo container network
(postgres:5432 and loopback API :8000) from inside a container. No model calls,
user model profiles, or provider credentials are read or copied.

The synthetic approval account is only for this local demo; its deliberately
public password is DemoApprove2026!. Existing unrelated accounts are refused.
Service operations and API reads make reruns reuse the same examples.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

from smoke import ApiError, Client

ROOT = Path(__file__).resolve().parents[2]
APPROVER_NAME = 'demo-seed-approver'
APPROVER_DISPLAY = '演示独立审批员（合成数据）'
APPROVER_PASSWORD = 'DemoApprove2026!'


def guard(url, dsn, allow_seed):
    from psycopg2.extensions import parse_dsn
    if os.environ.get('DATAMIND_DEMO_SEED') != '1':
        raise SystemExit('Refused: DATAMIND_DEMO_SEED=1 is required')
    if not dsn:
        raise SystemExit('Refused: an explicit seed write DSN is required')
    parts = urlsplit(url)
    local_api = parts.hostname in {'127.0.0.1', 'localhost', '::1'}
    container = allow_seed and Path('/.dockerenv').exists()
    if not local_api or parts.scheme != 'http' or (parts.port != 18000 and not (container and parts.port == 8000)):
        raise SystemExit('Refused: expected isolated loopback API :18000')
    config = parse_dsn(dsn)
    host, port = config.get('host', ''), config.get('port', '5432')
    host_seed = host in {'127.0.0.1', 'localhost', '::1'} and port == '55433'
    container_seed = container and host == 'postgres' and port == '5432'
    if not (host_seed or container_seed):
        raise SystemExit('Refused: expected isolated PostgreSQL :55433 or explicit demo container')


def optional_get(api, path):
    try:
        return api.request(path)
    except ApiError as exc:
        if exc.status == 404:
            return None
        raise


def approval_client(api, dsn):
    import psycopg2
    from datamind import auth
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute('SELECT display_name, roles, active FROM auth_users WHERE username=%s', (APPROVER_NAME,))
        row = cur.fetchone()
    if row is None:
        auth.provision(APPROVER_NAME, APPROVER_DISPLAY, APPROVER_PASSWORD, ['approver'], dsn)
    elif row[0] != APPROVER_DISPLAY or set(row[1]) != {'approver'} or not row[2]:
        raise RuntimeError('Refused: existing seed-approver identity was changed')
    approver = Client(api.url)
    approver.request('/v1/auth/login', {'username': APPROVER_NAME, 'password': APPROVER_PASSWORD})
    me = approver.request('/v1/auth/me')
    approver.csrf = me['csrf_token']
    return approver


def seed_cad(api, approver):
    from ingestion.cad_dimension_contract import digest
    from datamind.cad_dimensions import projection
    document_id, measurement_id, binding_id = 'demo-cad-a200', 'dimension-demo-1', 'demo-dimension-a200'
    existing = optional_get(api, '/v1/drawings/' + document_id)
    if existing is None:
        source = next(x for x in api.request('/v1/drawings/sources')['sources']
                      if x['source_path'].replace('\\', '/') == 'data/cad_demo/development/dev-mm.dxf')
        version = api.request('/v1/drawings', {
            'request_id': 'demo-seed-cad-a200-v1', 'document_id': document_id,
            'title': '演示合成图纸 A200（二维外径）', 'source_path': source['source_path'],
            'source_sha256': source['source_sha256'], 'provenance': 'synthetic_demo',
            'previous_version': 0, 'previous_source_sha256': None})['version']
    else:
        version = existing['version']
    data = api.request('/v1/drawings/' + document_id + '/features')
    feature = next(x for x in data['features'] if x['feature_type'] == 'diameter')
    state = next(x for x in data['association_reviews'] if x['feature_id'] == feature['feature_id'])
    review = state.get('latest_review')
    if review is None:
        review = api.request('/v1/drawings/' + document_id + '/features/' + feature['feature_id'] + '/reviews', {
            **{k: version[k] for k in ('document_version', 'source_sha256', 'parser_version', 'bundle_sha256')},
            'request_id': 'demo-seed-cad-association-v1', 'feature_sha256': state['feature_sha256'],
            'previous_review_id': None, 'decision': 'confirmed',
            'selected_target': feature['selected_target'] or feature['target_candidates'][0],
            'reason': '合成演示：核对 DXF 显式对象句柄与外径标注，仅确认本地提取关联。'})['review']
    elif review['decision'] != 'confirmed':
        raise RuntimeError('Seed CAD review was changed; will not overwrite a user decision')
    existing = optional_get(api, '/v1/dimensions/measurements/' + measurement_id)
    if existing is None:
        source = next(x for x in api.request('/v1/dimensions/sources')['sources']
                      if x['source_path'].replace('\\', '/') == 'data/dimension_demo/development/demo-diameter.json')
        measurement = api.request('/v1/dimensions/measurements', {
            'request_id': 'demo-seed-measurement-a200-v1', 'measurement_id': measurement_id,
            'source_path': source['source_path'], 'source_sha256': source['source_sha256'],
            'provenance': 'synthetic_demo', 'previous_version': 0, 'previous_sha256': None})['measurement']
    else:
        measurement = existing['measurement']
    existing = optional_get(api, '/v1/dimensions/bindings/' + binding_id)
    if existing is None:
        projected_review = projection(review)
        binding = api.request('/v1/dimensions/bindings', {
            'request_id': 'demo-seed-dimension-binding-v1', 'binding_id': binding_id,
            'previous_version': 0, 'previous_sha256': None, 'document_id': document_id,
            **{k: version[k] for k in ('document_version', 'source_sha256', 'bundle_sha256')},
            'feature_id': feature['feature_id'], 'feature_sha256': state['feature_sha256'],
            'target_handle': review['selected_target'], 'measurement_id': measurement_id,
            'measurement_record_version': measurement['payload']['source']['record_version'],
            'measurement_sha256': measurement['sha256'], 'review_id': projected_review['review_id'],
            'review_sha256': digest(projected_review),
            'valid_from': '2026-10-05T00:00:00+08:00', 'valid_to': '2026-10-06T00:00:00+08:00',
            'rationale': '合成演示：A200 的 20.04 mm 测量与 20±0.1 mm 标注明确关联，不作制造授权。'})['binding']
        existing = api.request('/v1/dimensions/bindings/' + binding_id)
    else:
        binding = existing['binding']
    approvals = existing.get('approvals') or []
    if not approvals:
        approver.request('/v1/dimensions/bindings/' + binding_id + '/approvals', {
            'request_id': 'demo-seed-dimension-approval-v1',
            'binding_version': binding['payload']['binding_version'], 'binding_sha256': binding['sha256'],
            'previous_approval_id': None, 'decision': 'approved',
            'reason': '合成演示：独立账户批准本地标注比较；不是生产放行或整体质量结论。'})
    history = api.request('/v1/dimensions/bindings/' + binding_id + '/comparisons')['comparisons']
    if not history:
        api.request('/v1/dimensions/bindings/' + binding_id + '/comparisons', {
            'request_id': 'demo-seed-dimension-comparison-v1',
            'binding_version': binding['payload']['binding_version'], 'binding_sha256': binding['sha256']})
    result = api.request('/v1/dimensions/bindings/' + binding_id)['current_result']
    return {'drawing_id': document_id, 'measurement_id': measurement_id, 'binding_id': binding_id,
            'status': result['status'], 'reason': result.get('reason'),
            'comparison': result.get('comparison')}


def seed_wiki(dsn, identity):
    from datamind import wiki
    declarations = [
        {'title': '演示零件 A200', 'entry_type': 'business_entity', 'entity_type': 'part',
         'canonical_entity_key': 'A200', 'confirmation': 'confirmed',
         'body': '# 演示零件 A200\n\n这是仓库自带的合成零件，图号 CAD-DEV-1，图纸版次 R2。'
                 '\n\n二维 DXF 样本：`data/cad_demo/development/dev-mm.dxf`。外径标注为 20±0.1 mm。'
                 '\n\n可在“图纸与实体”查看原图、实体句柄与认证关联复核。'},
        {'title': '演示指标：外径', 'entry_type': 'metric', 'metric_id': 'demo.outer_diameter',
         'body': '# 演示指标：外径\n\n本例把 20.04 mm 合成测量与 A200 的外径标注显式绑定。'
                 '\n\n样本：`data/dimension_demo/development/demo-diameter.json`，批次 DIM-BATCH-1，'
                 '工序 30。实际业务批次、图纸版次和工艺版号应分别核对，不能按相似名称自动配对。'},
        {'title': '演示流程：独立尺寸审批', 'entry_type': 'norm',
         'source_document_id': 'demo-cad-a200', 'source_revision': 'R2', 'clause_key': 'demo-approval-flow',
         'body': '# 演示流程：独立尺寸审批\n\n演示顺序为登记图纸→确认标注与对象关联→登记测量→显式绑定'
                 '→由独立审批账户批准→保存比较→回放当时快照。'
                 '\n\n关联复核与尺寸使用批准是两条独立记录。撤销批准会阻断新的比较，历史快照仍可回放。'
                 '\n\n本例 20.04 mm 落在标注区间 [19.9, 20.1] mm 内，只说明该条标注比较结果。'},
    ]
    entries = []
    for decl in declarations:
        payload = {k: v for k, v in decl.items() if k != 'body'}
        payload.update(namespace='demo-seed-v1', category='合成演示',
                       summary='人工编写的演示说明，展示页面与审批流程，不是模型生成或生产事实。')
        entries.append(wiki.create_entry(payload, dsn, identity)['entry'])
    for index, (decl, entry) in enumerate(zip(declarations, entries)):
        current = wiki.entry_detail(entry['entry_id'], dsn, identity)
        if current['entry']['head_revision_id']:
            continue
        revision = current.get('revision')
        if not revision:
            relations = []
            target = entries[0]['entry_id']
            if index:
                relations.append({'src': entry['entry_id'], 'dst': target,
                                  'relation_type': 'measures' if index == 1 else 'applies_to',
                                  'origin': 'wiki_derived', 'rule_version': 'demo-manual-map-v1',
                                  'derivation': {'inputs': ['manual_demo_template']},
                                  'basis_note': '人工演示模板中的导航关系，不代表自动发现或事实核验。'})
            revision = wiki.create_revision(entry['entry_id'], {
                'request_id': 'demo-wiki-revision-' + str(index), 'base_revision_id': None,
                'title': decl['title'], 'body_markdown': decl['body'] +
                '\n\n> 演示材料：以上内容为人工模板说明，无模型调用。发布状态与事实核验状态独立；'
                '此词条不宣称已独立核验，不构成制造授权或产品合格结论。',
                'generation_method': 'human', 'change_note': '初始化合成演示说明',
                'assertions': [], 'citations': [], 'relations': relations}, dsn, identity)['revision']
        detail = wiki.entry_detail(entry['entry_id'], dsn, identity)
        if not detail['review_events']:
            wiki.submit_review(entry['entry_id'], {
                'request_id': 'demo-wiki-review-' + str(index), 'revision_id': revision['revision_id'],
                'expected_head_revision_id': None, 'action': 'submit_review',
                'scope': {'full_revision': True}, 'basis': {'method': 'human_review'},
                'note': '仅检查演示文字与流程说明；不授予 verified 事实状态。'}, dsn, identity)
        wiki.publish(entry['entry_id'], {
            'request_id': 'demo-wiki-publish-' + str(index), 'revision_id': revision['revision_id'],
            'expected_head_revision_id': None, 'note': '发布人工演示说明，保留未核验语义。'}, dsn, identity)
    return {'entry_ids': [x['entry_id'] for x in entries], 'published_count': len(entries),
            'relations': 2, 'content_source': 'manual_synthetic_demo', 'model_calls': 0}


def seed_field_review(api, approver):
    docs = api.request('/v1/documents?collection=industrial_demo')['documents']
    for doc in docs:
        facts = api.request('/v1/documents/' + doc['document_id'] + '/facts')['facts']
        if not facts:
            continue
        fact = facts[0]
        base = '/v1/documents/' + doc['document_id'] + '/facts/' + fact['fact_id']
        shared = {'document_version': doc['doc_version'], 'source_sha256': doc['sha256'],
                  'parser_version': doc['parser_version'], 'payload_sha256': fact['payload_sha256']}
        review = fact.get('latest_review')
        if review is None:
            review = api.request(base + '/reviews', {
                **shared, 'request_id': 'demo-field-review-v1', 'previous_review_id': None,
                'decision': 'confirmed', 'reason': '合成演示：核对预置工业样本的原文与提取字段，仅展示复核流程。'})['review']
        if not fact.get('latest_approval'):
            approver.request(base + '/approvals', {
                **shared, 'request_id': 'demo-field-approval-v1', 'review_id': review['review_id'],
                'previous_approval_id': None, 'decision': 'approved',
                'reason': '合成演示：独立账户审批这一条提取记录，不改来源声明或冲突结论。'})
        return {'document_id': doc['document_id'], 'fact_id': fact['fact_id'], 'local_review_and_approval': True}
    return {'local_review_and_approval': False, 'reason': 'NO_INDUSTRIAL_FACT_SAMPLE'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=os.environ.get('DEMO_URL', 'http://127.0.0.1:18000'))
    parser.add_argument('--dsn', default=os.environ.get('DATAMIND_META_WRITE_DSN'))
    parser.add_argument('--allow-seed', action='store_true')
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    guard(args.url, args.dsn, args.allow_seed)
    sys.path.insert(0, str(ROOT))
    from datamind import auth
    api = Client(args.url)
    health = api.request('/health')
    if health.get('database') != 'ready':
        raise SystemExit('Refused: seed API database is not ready')
    me = api.request('/v1/auth/me')
    if (me.get('user') or {}).get('user_id') != auth.DEMO_USER_ID:
        raise SystemExit('Refused: expected isolated loopback demo administrator')
    api.csrf = me['csrf_token']
    identity = auth.ensure_demo_user(args.dsn)
    approver = approval_client(api, args.dsn)
    report = {'seed_type': 'synthetic_demo', 'model_calls': 0,
              'cad_dimensions': seed_cad(api, approver),
              'wiki': seed_wiki(args.dsn, identity),
              'field_review': seed_field_review(api, approver)}
    # Revoke only this script's temporary approval session. Seed account/history remain.
    approver.request('/v1/auth/logout', {})
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + '\n', encoding='utf-8')
    print(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
