"""Portable demo checks. Default mode sends GET requests only; --live calls models.

DEMO_URL defaults to http://127.0.0.1:18000. No credentials or response bodies are
written to the report. --strict-data requires the prepared CAD/dimension/Wiki
fixtures as well as searchable knowledge documents. --live persists three demo
runs and can incur provider charges; it never configures or copies model keys.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPCookieProcessor, ProxyHandler, Request


class ApiError(RuntimeError):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(f"HTTP {status}: {code}")


class Client:
    def __init__(self, url):
        self.url = url.rstrip('/')
        parts = urlsplit(self.url)
        if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username:
            raise ValueError('Expected an HTTP base URL without credentials')
        self.opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.csrf = None

    def request(self, path, body=None, *, method=None, timeout=15):
        method = method or ('POST' if body is not None else 'GET')
        headers = {'Accept': 'application/json'}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        if method not in {'GET', 'HEAD'} and self.csrf:
            headers['X-CSRF-Token'] = self.csrf
        if method not in {'GET', 'HEAD'}:
            headers['Origin'] = self.url
        req = Request(self.url + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=timeout) as response:
                raw = response.read()
                if 'json' in response.headers.get('Content-Type', ''):
                    return json.loads(raw)
                return {'http_status': response.status, 'bytes': len(raw)}
        except HTTPError as exc:
            code = 'HTTP_ERROR'
            try:
                payload = json.loads(exc.read())
                detail = payload.get('detail') or payload
                if isinstance(detail, dict):
                    code = detail.get('error_code') or 'HTTP_ERROR'
            except (ValueError, AttributeError):
                pass
            raise ApiError(exc.code, str(code)) from None
        except (URLError, TimeoutError, OSError):
            raise ApiError(0, 'CONNECTION_UNAVAILABLE') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=os.environ.get('DEMO_URL', 'http://127.0.0.1:18000'))
    parser.add_argument('--out', type=Path)
    parser.add_argument('--strict-data', action='store_true')
    parser.add_argument('--live', action='store_true', help='Explicitly allow paid model calls and persist demo runs')
    args = parser.parse_args()
    api = Client(args.url)
    report = {'mode': 'live' if args.live else 'read_only', 'checks': [], 'counts': {}}

    def record(name, ok, *, data=None, optional=False):
        status = 'pass' if ok else ('warn' if optional else 'fail')
        report['checks'].append({'check': name, 'status': status, **({'data': data} if data is not None else {})})

    def get(path, name):
        try:
            value = api.request(path)
            record(name, True)
            return value
        except ApiError as exc:
            record(name, False, data={'http_status': exc.status, 'error_code': exc.code})
            return {}

    health = get('/health', 'health_endpoint')
    record('database_ready', health.get('database') == 'ready')
    record('vector_ready', health.get('vector_store') == 'ready')
    report['counts']['published_documents_health'] = health.get('document_count')
    get('/', 'frontend_built')
    me = get('/v1/auth/me', 'local_identity')
    api.csrf = me.get('csrf_token')
    record('administrative_demo_identity', bool((me.get('user') or {}).get('user_id')))
    config = get('/v1/model/config', 'model_configuration_page')
    record('model_configured', bool(config.get('has_key') or config.get('environment_fallback')), optional=not args.live)
    docs = get('/v1/documents', 'document_catalogue').get('documents', [])
    report['counts']['documents_listed'] = len(docs)
    report['counts']['searchable_documents'] = sum(bool(x.get('searchable')) for x in docs)
    record('searchable_knowledge_samples', report['counts']['searchable_documents'] >= 10)
    if docs:
        get('/v1/documents/' + docs[0]['document_id'] + '/contract', 'document_contract')
    runs = get('/v1/runs?limit=5', 'run_history').get('runs', [])
    report['counts']['runs_on_first_page'] = len(runs)
    if runs:
        run_id = runs[0]['run_id']
        ev = get('/v1/runs/' + run_id + '/evidence', 'persisted_run_evidence')
        report['counts']['sample_evidence_nodes'] = len(ev.get('nodes', []))
        get('/v1/runs/' + run_id + '/events', 'persisted_run_events')
    sources = get('/v1/drawings/sources', 'cad_source_materials').get('sources', [])
    drawings = get('/v1/drawings', 'cad_registry').get('drawings', [])
    report['counts']['cad_staged_sources'] = len(sources)
    report['counts']['cad_registered_drawings'] = len(drawings)
    record('cad_example_present', bool(drawings), optional=not args.strict_data)
    if drawings:
        did = drawings[0]['document_id']
        get('/v1/drawings/' + did + '/features', 'cad_features_and_review_gate')
        get('/v1/drawings/' + did + '/preview', 'cad_svg_preview')
    measures = get('/v1/dimensions/measurements', 'measurement_registry').get('measurements', [])
    bindings = get('/v1/dimensions/bindings', 'dimension_binding_registry').get('bindings', [])
    report['counts'].update(measurements=len(measures), dimension_bindings=len(bindings))
    record('dimension_example_present', bool(bindings), optional=not args.strict_data)
    if bindings:
        bid = bindings[0]['binding_id']
        detail = get('/v1/dimensions/bindings/' + bid, 'dimension_current_approval_gate')
        current = detail.get('current_result') or {}
        record('dimension_comparison_ready', current.get('status') == 'compared',
               data={'status': current.get('status'), 'reason': current.get('reason')}, optional=not args.strict_data)
        history = get('/v1/dimensions/bindings/' + bid + '/comparisons', 'dimension_comparison_history')
        comparisons = history.get('comparisons', [])
        report['counts']['dimension_saved_comparisons'] = len(comparisons)
        if comparisons:
            cid = comparisons[0].get('comparison_id')
            if cid:
                get('/v1/dimensions/comparisons/' + cid, 'dimension_immutable_replay')
    uploads = get('/v1/uploads?limit=200', 'upload_and_parser_registry')
    report['counts']['uploads'] = len(uploads.get('uploads', []))
    record('upload_worker_alive', bool((uploads.get('worker') or {}).get('alive')))
    get('/v1/authority/rules', 'authority_rules_page')
    wiki = get('/v1/wiki/entries?limit=50', 'wiki_catalogue')
    entries = (wiki.get('data') or {}).get('entries', [])
    published = [x for x in entries if (x.get('status') or {}).get('publication') == 'published']
    report['counts'].update(wiki_entries=len(entries), wiki_published_entries=len(published))
    record('wiki_published_examples', bool(published), optional=not args.strict_data)
    if entries:
        entry = published[0] if published else entries[0]
        eid = entry['entry_id']
        get('/v1/wiki/entries/' + eid, 'wiki_article')
        get('/v1/wiki/entries/' + eid + '/graph', 'wiki_neighborhood')
        space_id = (entry.get('business_key') or {}).get('space_id')
        if space_id:
            graph = get('/v1/wiki/spaces/' + space_id + '/graph?limit=50', 'wiki_space_graph')
            report['counts']['wiki_graph_relations'] = len((graph.get('data') or {}).get('relations', []))
    if args.live:
        cases = [
            ('sql', {'question': '2025年销售额最高的前三个产品是什么？', 'mode': 'sql',
                     'scope': {'database_sources': ['demo_retail']}}),
            ('rag', {'question': '七天无理由退款的条件是什么？', 'mode': 'rag',
                     'scope': {'document_collections': ['demo_kb']}}),
            ('industrial', {'question': '2026年9月A102工序30哪些批次温度超限？生产记录和质检报告是否一致？',
                            'mode': 'industrial', 'scope': {'database_sources': ['demo_industrial'],
                                                          'document_collections': ['industrial_demo']}}),
        ]
        if not api.csrf:
            record('live_authentication', False, data={'error_code': 'CSRF_UNAVAILABLE'})
        else:
            for name, payload in cases:
                started = time.monotonic()
                try:
                    result = api.request('/v1/chat', payload, timeout=240)
                    summary = {'status': result.get('status'), 'error_code': result.get('error_code'),
                               'run_id': result.get('run_id'), 'duration_seconds': round(time.monotonic() - started, 2),
                               'citation_count': len((result.get('rag_result') or {}).get('citations') or []),
                               'sql_row_count': len((result.get('sql_result') or {}).get('rows') or []),
                               'industrial_row_count': len((result.get('industrial_result') or {}).get('rows') or [])}
                    has_result = {'sql': summary['sql_row_count'] == 3,
                                  'rag': summary['citation_count'] > 0,
                                  'industrial': summary['industrial_row_count'] > 0}[name]
                    record('live_' + name, result.get('status') == 'COMPLETED' and has_result, data=summary)
                    if result.get('run_id'):
                        graph = get('/v1/runs/' + result['run_id'] + '/evidence', 'live_' + name + '_evidence')
                        record('live_' + name + '_evidence_populated', bool(graph.get('nodes')),
                               data={'nodes': len(graph.get('nodes') or [])})
                except ApiError as exc:
                    record('live_' + name, False, data={'http_status': exc.status, 'error_code': exc.code})
    report['passed'] = not any(x['status'] == 'fail' for x in report['checks'])
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(output + '\n', encoding='utf-8')
    print(output)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
