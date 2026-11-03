"""Upload -> queue -> contract store -> review gate, against the disposable metadata clone.

Every fixture here is a real one: the staged bytes are the checked-in DXF/measurement
samples (copied through the endpoint, never parsed by a stub), the contract is read back
through the real `/v1/documents/{id}/contract` route, and the visibility gate is asserted
through the real `/v1/documents` query. The metadata rows and the staged files are removed
in teardown; staging is redirected to a temporary root so nothing lands in `data/`.
"""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from fastapi.testclient import TestClient
import psycopg2

from datamind import file_uploads
from datamind.agent import _write_dsn
from datamind.api import app
from ingestion import upload_pipeline
import auth_fixtures

ROOT = Path(__file__).resolve().parents[1]
DXF = ROOT / 'data/cad_demo/development/dev-mm.dxf'
MEASUREMENT = ROOT / 'data/dimension_demo/development/demo-diameter.json'


class UploadPipelineTests(unittest.TestCase):
    def setUp(self):
        self.dsn = _write_dsn()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [patch.object(file_uploads, 'ROOT', self.root),
                        patch.object(upload_pipeline, 'ROOT', self.root)]
        for item in self.patches:
            item.start()
        self.addCleanup(self._stop)
        self.admin = TestClient(app, client=('upload-admin-' + self.root.name, 50000))
        self.viewer = TestClient(app)
        auth_fixtures.login(self.admin, roles=('admin',))
        auth_fixtures.login(self.viewer, roles=('viewer',))
        self.uploads = []
        self.docs = []
        self.addCleanup(self._cleanup)

    # ---------------------------------------------------------------- lifecycle/plumbing
    def _stop(self):
        for client in (self.admin, self.viewer):
            auth_fixtures.cleanup(client)
            client.close()
        for item in self.patches:
            item.stop()
        self.temp.cleanup()

    def _cleanup(self):
        with psycopg2.connect(self.dsn) as conn, conn.cursor() as cur:
            for table in ('ingest_jobs', 'document_contract_blocks', 'document_block_relations',
                          'document_block_locators', 'chunks', 'document_blocks'):
                cur.execute(f'DELETE FROM {table} WHERE document_id=ANY(%s)', (self.docs,))
            cur.execute('DELETE FROM document_assets WHERE document_id=ANY(%s)', (self.docs,))

    def upload(self, data, filename, provenance='synthetic_demo', client=None):
        response = (client or self.admin).post(
            '/v1/uploads', params={'filename': filename, 'provenance': provenance}, content=data,
            headers={'Content-Type': 'application/octet-stream'})
        if response.status_code == 201:
            body = response.json()
            self.uploads.append(body['upload_id'])
            self.docs.append(upload_pipeline.identities(
                hashlib.sha256(data).hexdigest())['document_id'])
        return response

    def fixture(self, relative):
        return (ROOT / relative).read_bytes()

    def dxf(self):
        return self.upload(self.fixture('data/cad_demo/development/dev-mm.dxf'), 'dev-mm.dxf')

    def measurement(self):
        return self.upload(self.fixture('data/dimension_demo/development/demo-diameter.json'),
                           'demo-diameter.json')

    def drain(self, upload_id):
        # Explicit in-process opt-in: this test stages under a temporary root that a child
        # process would not see, and the bounded child path has its own wiring test below.
        return upload_pipeline.drain(self.dsn, upload_id, in_process=True)

    def detail(self, upload_id):
        response = self.admin.get('/v1/uploads/' + upload_id)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        return response.json()

    def asset_row(self, document_id):
        with psycopg2.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute('''SELECT status,needs_review,quality_detail,object_uri,parser_version
                FROM document_assets WHERE document_id=%s''', (document_id,))
            return cur.fetchone()

    def job_row(self, upload_id):
        with psycopg2.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute('SELECT status,error FROM ingest_jobs WHERE job_id=%s',
                        (upload_pipeline.job_id_for(upload_id),))
            return cur.fetchone()

    def listed_document_ids(self):
        response = self.admin.get('/v1/documents')
        self.assertEqual(response.status_code, 200, response.text)
        return {row['document_id'] for row in response.json()['documents']}

    # -------------------------------------------------- 1. accepted types and hard bounds
    def test_auth_csrf_type_and_size_bounds(self):
        with TestClient(app) as anonymous:
            self.assertEqual(anonymous.post('/v1/uploads',
                                            params={'filename': 'x.dxf',
                                                    'provenance': 'synthetic_demo'},
                                            content=b'x').status_code, 401)
        self.assertEqual(self.viewer.post('/v1/uploads',
                                          params={'filename': 'x.dxf',
                                                  'provenance': 'synthetic_demo'},
                                          content=b'x').status_code, 403)
        token = self.admin.headers.pop('X-CSRF-Token')
        self.assertEqual(self.dxf().status_code, 403)
        self.admin.headers['X-CSRF-Token'] = token
        self.assertEqual(self.dxf().status_code, 201, 'the fixture DXF must be accepted')

        # The interface's per-suffix ceilings, exactly.
        self.assertEqual(file_uploads.UPLOAD_LIMITS,
                         {'.dxf': 8 * 1024 * 1024, '.json': 1024 * 1024,
                          '.docx': 16 * 1024 * 1024, '.pptx': 16 * 1024 * 1024,
                          '.xlsx': 16 * 1024 * 1024, '.pdf': 32 * 1024 * 1024})
        # A streamed body over the ceiling is refused without ever being staged.
        before = set((self.root / 'data').rglob('upload-*'))
        with patch.dict(file_uploads.UPLOAD_LIMITS, {'.dxf': 16}):
            response = self.upload(b'x' * 17, 'too-big.dxf')
        self.assertEqual(response.status_code, 413, response.text)
        self.assertEqual(response.json()['detail']['error_code'], 'UPLOAD_TOO_LARGE')
        self.assertEqual(set((self.root / 'data').rglob('upload-*')), before,
                         'an oversized body must not be staged')

        for filename, code, status in [('x.txt', 'UNSUPPORTED_MEDIA_TYPE', 415),
                                       ('noextension', 'UNSUPPORTED_MEDIA_TYPE', 415),
                                       ('../x.dxf', 'FILE_TYPE_OR_NAME_INVALID', 422),
                                       ('/tmp/x.json', 'FILE_TYPE_OR_NAME_INVALID', 422),
                                       ('x\\x.dxf', 'FILE_TYPE_OR_NAME_INVALID', 422)]:
            response = self.upload(b'bytes', filename)
            self.assertEqual(response.status_code, status, filename)
            self.assertEqual(response.json()['detail']['error_code'], code, filename)

        empty = self.admin.post('/v1/uploads', params={'filename': 'empty.dxf',
                                                       'provenance': 'synthetic_demo'},
                                content=b'',
                                headers={'Content-Type': 'application/octet-stream'})
        self.assertEqual(empty.status_code, 422)
        self.assertEqual(empty.json()['detail']['error_code'], 'UPLOAD_EMPTY')

    def test_dwg_is_refused_with_the_missing_converter_code(self):
        response = self.upload(b'AC1032 fake dwg bytes', 'block.dwg')
        self.assertEqual(response.status_code, 415, response.text)
        self.assertEqual(response.json()['detail'],
                         {'error_code': 'CAD_DWG_CONVERTER_UNAVAILABLE'})
        self.assertEqual(list((self.root / 'data').rglob('upload-*')), [],
                         'a refused .dwg must not be staged')
    # --------------------------------------------- 2. one call stages + enqueues a job
    def test_one_call_returns_a_job_id_and_queues_exactly_one_job(self):
        response = self.dxf()
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(set(body), {'upload_id', 'job_id', 'filename', 'media_type', 'status',
                                     'review_required'})
        self.assertEqual(body['filename'], 'dev-mm.dxf')
        self.assertEqual(body['media_type'], 'image/vnd.dxf')
        self.assertEqual(body['status'], 'queued')
        self.assertTrue(body['review_required'])
        self.assertTrue(body['upload_id'].startswith('upl-'))
        self.assertNotEqual(body['upload_id'], body['job_id'])

        # The POST itself did the staging and the enqueue, not a later call.
        self.assertEqual(self.job_row(body['upload_id']), ('queued', None))
        with psycopg2.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute('''SELECT j.status,i.status,i.needs_review FROM ingest_jobs j
                JOIN document_assets i USING(document_id) WHERE j.job_id=%s''',
                        (body['job_id'],))
            job_status, asset_status, needs_review = cur.fetchone()
        self.assertEqual([job_status, asset_status, needs_review], ['queued', 'parsing', True])

        staged = self.asset_row(upload_pipeline.identities(
            hashlib.sha256(self.fixture('data/cad_demo/development/dev-mm.dxf')).hexdigest()
        )['document_id'])
        self.assertTrue((self.root / staged[3]).is_file(),
                        'the staged bytes must exist under the upload root')

        # One file, one identity: the same bytes are the same upload, re-armed.
        repeat = self.dxf()
        self.assertEqual(repeat.json()['upload_id'], body['upload_id'])
        self.assertEqual(repeat.json()['job_id'], body['job_id'])
        with psycopg2.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM ingest_jobs WHERE job_id=%s', (body['job_id'],))
            self.assertEqual(cur.fetchone()[0], 1)

    # --------------------------------- 3. contract store + /contract locator round trip
    def test_parsed_dxf_persists_the_contract_and_the_contract_route_reads_a_locator(self):
        created = self.dxf()
        self.assertEqual(created.status_code, 201, created.text)
        upload_id = created.json()['upload_id']
        self.assertEqual(self.drain(upload_id)['claimed'], 1)
        document_id = self.docs[-1]

        body = self.detail(upload_id)
        self.assertIn(body['status'], ('succeeded', 'partial_failure'))
        self.assertEqual(body['error_code'], None)
        # The parse wave indexes the document, but indexing is not publishing: the asset
        # still waits behind the review gate.
        self.assertEqual(body['vector_index'], 'embedded')
        self.assertGreater(body['chunk_count'], 0)
        self.assertIsNone(body['index_error_code'])
        self.assertIn('ingestion/upload_index.py', body['vector_index_note'])
        self.assertTrue(body['review_required'])
        self.assertEqual(body['review_state'], 'pending_review')
        self.assertEqual(body['contract_url'], f'/v1/documents/{document_id}/contract')
        analysis = body['analysis']
        self.assertEqual(analysis['adapter'], 'dxf-native')
        self.assertGreaterEqual(analysis['blocks'], 1)
        self.assertEqual(analysis['locators_written'], analysis['blocks'])
        self.assertGreaterEqual(analysis['layers'], 1)
        self.assertGreaterEqual(analysis['entities'], 1)
        self.assertIn(analysis['parse_status'], ('parsed', 'partial'))
        self.assertGreaterEqual(body['block_count'], 1)
        self.assertGreaterEqual(body['locator_count'], 1)

        with psycopg2.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM document_contract_blocks WHERE document_id=%s',
                        (document_id,))
            self.assertEqual(cur.fetchone()[0], analysis['blocks'])

        contract = self.admin.get(f'/v1/documents/{document_id}/contract')
        self.assertEqual(contract.status_code, 200, contract.text)
        stored = contract.json()
        self.assertEqual(stored['document_id'], document_id)
        self.assertGreaterEqual(stored['block_count'], 1)
        self.assertEqual(stored['blocks'][0]['locator']['locator_kind'], 'cad_entity')
        self.assertTrue(stored['blocks'][0]['locator']['handle'])
        self.assertTrue(stored['blocks'][0]['content_hash'])
        self.assertIn(stored['parse_contract']['parse_status'], ('parsed', 'partial'))
        self.assertEqual(stored['parse_contract']['adapter'], 'dxf-native')

    def test_object_uri_is_stored_as_an_absolute_path(self):
        """Regression: a repository-relative uri broke the isolated runner's clone guard.

        Every other `document_assets` row stores an absolute path, and the regression
        runner relocates rows by the `/data/` marker. The upload pipeline wrote
        `data/cad_demo/...` instead, so the runner's fail-closed guard reported
        "Clone source unavailable" and the entire regression could not start.
        """
        import os
        created = self.dxf()
        self.assertEqual(created.status_code, 201, created.text)
        document_id = self.docs[-1]
        with psycopg2.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute('SELECT object_uri FROM document_assets WHERE document_id=%s', (document_id,))
            uri = cur.fetchone()[0]
        self.assertTrue(os.path.isabs(uri), uri)
        self.assertIn('/data/', uri, uri)
        self.assertTrue(os.path.isfile(uri), uri)

    # ------------------------------------------------------------- 4. the review gate
    def test_unapproved_upload_is_hidden_and_approve_records_the_operator(self):
        created = self.dxf()
        upload_id = created.json()['upload_id']
        self.drain(upload_id)
        document_id = self.docs[-1]

        self.assertNotIn(document_id, self.listed_document_ids())
        self.assertEqual(self.admin.get(f'/v1/documents/{document_id}').status_code, 404)
        self.assertTrue(self.asset_row(document_id)[1], 'needs_review must stay set')

        # approve is admin + CSRF + same-origin, like every other upload mutation.
        with TestClient(app) as anonymous:
            self.assertEqual(anonymous.post(f'/v1/uploads/{upload_id}/approve').status_code, 401)
        self.assertEqual(self.viewer.post(f'/v1/uploads/{upload_id}/approve').status_code, 403)
        token = self.admin.headers.pop('X-CSRF-Token')
        self.assertEqual(self.admin.post(f'/v1/uploads/{upload_id}/approve').status_code, 403)
        self.admin.headers['X-CSRF-Token'] = token

        approved = self.admin.post(f'/v1/uploads/{upload_id}/approve')
        self.assertEqual(approved.status_code, 200, approved.text)
        body = approved.json()
        self.assertFalse(body['review_required'])
        self.assertEqual(body['review_state'], 'approved')
        # The parse wave wrote this document's chunks, so approve promotes it to `indexed`
        # and the retriever can select it. Approval did not *create* the index; it opened
        # the gate in front of an index that already existed (see §2 of UPLOAD_PIPELINE.md).
        self.assertTrue(body['indexed'])
        self.assertTrue(body['searchable'])
        self.assertEqual(body['index_state'], 'indexed')
        self.assertIsNone(body['index_note'])
        self.assertEqual(body['vector_index'], 'embedded')
        operator = body['approval']
        self.assertEqual(operator['user_id'], self.admin._fixture_users[0])
        self.assertTrue(operator['approved_at'])
        self.assertEqual(operator['roles'], ['admin'])
        self.assertTrue(operator['indexed'], 'the recorded decision must match the derived state')

        # And it now appears in the Q&A selector, with the same derived judgment.
        self.assertIn(document_id, self.listed_document_ids())
        listed = self.admin.get(f'/v1/documents/{document_id}')
        self.assertEqual(listed.status_code, 200)
        status, needs_review, detail, _uri, _parser = self.asset_row(document_id)
        self.assertEqual((status, needs_review), ('indexed', False))
        self.assertEqual(detail['review_approval']['user_id'], self.admin._fixture_users[0])

    def test_a_queued_upload_cannot_be_approved(self):
        created = self.dxf()
        upload_id = created.json()['upload_id']
        response = self.admin.post(f'/v1/uploads/{upload_id}/approve')
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()['detail']['error_code'], 'UPLOAD_NOT_REVIEWABLE')

    # -------------------------------------------------- 5. stable failure error codes
    def test_an_unparseable_upload_reports_a_stable_error_code(self):
        created = self.upload(b'this is not a drawing at all', 'broken.dxf')
        self.assertEqual(created.status_code, 201, created.text)
        upload_id = created.json()['upload_id']
        self.assertEqual(self.drain(upload_id)['claimed'], 1)
        document_id = self.docs[-1]

        body = self.detail(upload_id)
        self.assertEqual(body['status'], 'failed')
        self.assertEqual(body['error_code'], 'PARSE_ADAPTER_UNKNOWN')
        self.assertNotIn('this is not a drawing', json.dumps(body), 'no raw input in the report')
        self.assertNotIn(document_id, self.listed_document_ids())
        status, _needs_review, _detail, _uri, _parser = self.asset_row(document_id)
        self.assertEqual(status, 'failed')
        self.assertEqual(self.job_row(upload_id), ('failed', 'PARSE_ADAPTER_UNKNOWN'))
        response = self.admin.post(f'/v1/uploads/{upload_id}/approve')
        self.assertEqual(response.status_code, 409)

    def test_changed_staged_bytes_fail_instead_of_parsing_different_content(self):
        created = self.dxf()
        upload_id = created.json()['upload_id']
        document_id = self.docs[-1]
        staged = self.asset_row(document_id)[3]
        (self.root / staged).write_bytes(b'tampered')
        self.assertEqual(self.drain(upload_id)['claimed'], 1)
        body = self.detail(upload_id)
        self.assertEqual(body['status'], 'failed')
        self.assertEqual(body['error_code'], 'UPLOAD_STAGED_SOURCE_CHANGED')

    # --------------------------------------------- 6. honest vector_index + listing
    def test_a_document_with_nothing_to_index_is_reported_as_deferred(self):
        created = self.measurement()
        self.assertEqual(created.status_code, 201, created.text)
        upload_id = created.json()['upload_id']
        self.drain(upload_id)
        document_id = self.docs[-1]
        body = self.detail(upload_id)
        # A measurement record carries no page/region geometry: the gap is declared, not faked.
        self.assertEqual(body['status'], 'partial_failure')
        # No contract blocks means nothing to index. That is `deferred`, not `failed`:
        # the pipeline did not try and fail, it had nothing to do.
        self.assertEqual(body['vector_index'], 'deferred')
        self.assertEqual(body['chunk_count'], 0)
        self.assertIsNone(body['index_error_code'])
        self.assertIn('ingestion/upload_index.py', body['vector_index_note'])
        self.assertIn('locator_unavailable', body['unsupported_features'])
        self.assertEqual(body['analysis']['adapter'], 'measurement-json')
        self.assertEqual(body['block_count'], 0)
        with psycopg2.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM chunks WHERE document_id=%s', (document_id,))
            self.assertEqual(cur.fetchone()[0], 0)
        approved = self.admin.post(f'/v1/uploads/{upload_id}/approve')
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertFalse(approved.json()['indexed'])
        self.assertFalse(approved.json()['searchable'])
        self.assertEqual(approved.json()['index_state'], 'no_chunks')
        self.assertEqual(approved.json()['index_note'], 'UPLOAD_HAS_NO_CONTRACT_BLOCKS')
        self.assertNotIn(document_id, self.listed_document_ids())

    def test_listing_filters_and_item_shape(self):
        self.assertEqual(self.admin.get('/v1/uploads').status_code, 200)
        with TestClient(app) as anonymous:
            self.assertEqual(anonymous.get('/v1/uploads').status_code, 401)
        self.assertEqual(self.viewer.get('/v1/uploads').status_code, 403)

        first = self.dxf().json()
        second = self.measurement().json()
        self.drain(first['upload_id'])
        self.drain(second['upload_id'])

        everything = self.admin.get('/v1/uploads', params={'limit': 200}).json()
        self.assertEqual(everything['count'], len(everything['uploads']))
        ids = {row['upload_id'] for row in everything['uploads']}
        self.assertLessEqual({first['upload_id'], second['upload_id']}, ids)
        item = next(row for row in everything['uploads'] if row['upload_id'] == first['upload_id'])
        self.assertEqual(set(item), {'upload_id', 'job_id', 'document_id', 'filename',
                                      'media_type', 'provenance', 'status', 'created_at',
                                      'review_state', 'review_required', 'searchable',
                                      'index_state', 'index_note', 'index_error_code',
                                      'block_count', 'locator_count', 'unsupported_features'})
        # R05: the list states retrievability per item instead of leaving it to be inferred
        # from `status`/`review_state`.
        self.assertFalse(item['searchable'])
        self.assertEqual(item['index_state'], 'not_approved')
        self.assertEqual(item['index_note'], 'UPLOAD_NOT_APPROVED')

        succeeded = self.admin.get('/v1/uploads', params={'status': 'succeeded'}).json()
        succeeded_ids = {row['upload_id'] for row in succeeded['uploads']}
        self.assertIn(first['upload_id'], succeeded_ids)
        self.assertNotIn(second['upload_id'], succeeded_ids)
        partial = self.admin.get('/v1/uploads', params={'status': 'partial_failure'}).json()
        self.assertIn(second['upload_id'], {row['upload_id'] for row in partial['uploads']})
        self.assertNotIn(first['upload_id'], {row['upload_id'] for row in partial['uploads']})
        medias = self.admin.get('/v1/uploads', params={'media_type': 'application/json'}).json()
        self.assertIn(second['upload_id'], {row['upload_id'] for row in medias['uploads']})
        recent = self.admin.get('/v1/uploads', params={'since': '2000-01-01T00:00:00Z'}).json()
        self.assertIn(first['upload_id'], {row['upload_id'] for row in recent['uploads']})
        epoch = self.admin.get('/v1/uploads', params={'since': '4102444800'}).json()
        self.assertEqual(epoch['uploads'], [])
        for params, code in [({'status': 'nope'}, 'UPLOAD_FILTER_INVALID'),
                             ({'since': 'not-a-time'}, 'UPLOAD_FILTER_INVALID')]:
            response = self.admin.get('/v1/uploads', params=params)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(response.json()['detail']['error_code'], code)

    def test_the_detail_get_is_a_pure_read_and_the_listing_reports_the_worker(self):
        """Review 5.1: no route drives parsing any more; liveness is reported instead.

        `GET /v1/uploads/{upload_id}` used to call `work_once`, so a browser poll was the
        queue's clock. It is now read-only, and the queue's silence is made explicit
        through `worker.alive`/`worker.last_seen_at` on the listing (and `/health`), so a
        client can honestly say "后台处理器未运行" instead of showing `queued` as "解析中".
        """
        created = self.dxf()
        upload_id = created.json()['upload_id']
        document_id = self.docs[-1]
        for _ in range(2):
            body = self.detail(upload_id)
            self.assertEqual(body['status'], 'queued', 'a GET must not advance the job')
            self.assertEqual(body['attempt'], 0)
        self.assertEqual(self.job_row(upload_id), ('queued', None))
        self.assertEqual(self.asset_row(document_id)[0], 'parsing')
        with psycopg2.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM ingest_job_claims WHERE job_id=%s',
                        (upload_pipeline.job_id_for(upload_id),))
            self.assertEqual(cur.fetchone()[0], 0, 'a GET must not open a claim generation')
            cur.execute('SELECT count(*) FROM document_contract_blocks WHERE document_id=%s',
                        (document_id,))
            self.assertEqual(cur.fetchone()[0], 0, 'a GET must not parse or write results')

        listing = self.admin.get('/v1/uploads', params={'limit': 200}).json()
        self.assertEqual(set(listing), {'uploads', 'count', 'worker'})
        worker = listing['worker']
        self.assertEqual(set(worker), {'alive', 'last_seen_at', 'alive_seconds'})
        self.assertIsInstance(worker['alive'], bool)
        self.assertGreaterEqual(worker['alive_seconds'], 1)
        self.assertEqual(self.admin.get('/health').json()['worker'], worker,
                         '/health and the upload listing must agree on liveness')
        self.assertEqual(self.detail(upload_id)['worker'], worker,
                         'the detail a polling client reads must report liveness too')

        # Only an explicit worker call moves the queue.
        self.assertEqual(self.drain(upload_id)['claimed'], 1)
        self.assertEqual(self.detail(upload_id)['status'], 'succeeded')

    def test_unknown_upload_detail_is_404(self):
        response = self.admin.get('/v1/uploads/upl-' + '0' * 40)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()['detail']['error_code'], 'UPLOAD_NOT_FOUND')
        self.assertEqual(self.admin.get('/v1/uploads/not-an-id').status_code, 404)

    # ------------------------------------------------------- bounded-worker wiring
    def test_the_default_wave_is_always_a_bounded_child_process(self):
        """No caller — not even one that passes no deadline — parses inline."""
        created = self.dxf()
        upload_id = created.json()['upload_id']
        captured = {}

        def fake(module, function, args, **options):
            captured.update(module=module, function=function, args=args, options=options)
            return {'status': 'ok',
                    'value': {'status': 'succeeded', 'analysis': {'adapter': 'dxf-native'}},
                    'elapsed_ms': 1.0}

        with patch.object(upload_pipeline, 'run_parse',
                          side_effect=AssertionError('must not parse in the API process')), \
                patch.object(upload_pipeline.task_budget, 'run_bounded', side_effect=fake):
            wave = upload_pipeline.work_once(self.dsn, upload_id)
        self.assertEqual(wave['outcomes'][0]['outcome'], 'succeeded', wave['outcomes'][0])
        self.assertEqual(captured['module'], 'ingestion.upload_pipeline')
        self.assertEqual(captured['function'], 'run_parse')
        self.assertEqual(captured['options']['deadline_s'],
                         upload_pipeline.DEFAULT_WAVE_DEADLINE_SECONDS)
        self.assertEqual(captured['options']['cwd'], str(self.root))
        self.assertEqual(captured['args'][0]['job_id'], upload_pipeline.job_id_for(upload_id))
        self.assertEqual(captured['args'][0]['suffix'], '.dxf')
        self.assertNotIn('_session_hash', json.dumps(captured['args']))
        self.assertEqual(self.detail(upload_id)['status'], 'succeeded')

    def test_an_explicit_deadline_overrides_the_wave_budget(self):
        created = self.dxf()
        upload_id = created.json()['upload_id']
        captured = {}
        with patch.object(upload_pipeline.task_budget, 'run_bounded',
                          side_effect=lambda m, f, a, **o: captured.update(o) or
                          {'status': 'ok', 'value': {'status': 'succeeded'}, 'elapsed_ms': 1.0}):
            upload_pipeline.work_once(self.dsn, upload_id, deadline_s=12.5)
        self.assertEqual(captured['deadline_s'], 12.5)

    def test_the_wave_budget_falls_back_and_can_be_overridden(self):
        self.assertEqual(upload_pipeline.DEFAULT_WAVE_DEADLINE_SECONDS, 45.0)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(upload_pipeline.WAVE_DEADLINE_ENV, None)
            self.assertEqual(upload_pipeline.wave_deadline(), 45.0)
        with patch.dict(os.environ, {upload_pipeline.WAVE_DEADLINE_ENV: '12.5'}):
            self.assertEqual(upload_pipeline.wave_deadline(), 12.5)
            self.assertGreater(upload_pipeline.stale_running_seconds(),
                               upload_pipeline.wave_deadline())
        for value in ('', '   ', 'not-a-number', '0', '99999'):
            with patch.dict(os.environ, {upload_pipeline.WAVE_DEADLINE_ENV: value}):
                self.assertEqual(upload_pipeline.wave_deadline(),
                                 upload_pipeline.DEFAULT_WAVE_DEADLINE_SECONDS, value)

    def test_a_wave_timeout_reports_running_and_stays_claimable(self):
        created = self.dxf()
        upload_id = created.json()['upload_id']
        document_id = self.docs[-1]
        timeout = {'status': 'timeout', 'error_code': 'TASK_DEADLINE_EXCEEDED',
                   'deadline_seconds': 45.0, 'elapsed_ms': 45000.0}
        with patch.object(upload_pipeline.task_budget, 'run_bounded', return_value=timeout):
            wave = upload_pipeline.work_once(self.dsn, upload_id)
        self.assertEqual(wave['outcomes'][0]['outcome'], 'running')
        # Truthful: not a failure, not a fake success, and the row is still claimable.
        body = self.detail(upload_id)
        self.assertEqual(body['status'], 'running')
        self.assertEqual(body['error_code'], None)
        self.assertEqual(self.job_row(upload_id), ('running', None))
        self.assertEqual(self.asset_row(document_id)[0], 'parsing')
        self.assertEqual(self.asset_row(document_id)[1], True)
        # A second live poll must not start a rival parse while the wave budget is assumed live.
        with patch.object(upload_pipeline.task_budget, 'run_bounded',
                          side_effect=AssertionError('reclaimed while still fresh')):
            self.assertEqual(upload_pipeline.work_once(self.dsn, upload_id)['claimed'], 0)
        # Once the row is stale, a later poll reclaims and finishes it.
        with psycopg2.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("UPDATE ingest_jobs SET started_at=now()-interval '10 minutes' "
                        'WHERE job_id=%s', (upload_pipeline.job_id_for(upload_id),))
        with patch.object(upload_pipeline.task_budget, 'run_bounded',
                          return_value={'status': 'ok', 'value': {'status': 'succeeded'},
                                        'elapsed_ms': 5.0}):
            self.assertEqual(upload_pipeline.work_once(self.dsn, upload_id)['claimed'], 1)
        self.assertEqual(self.detail(upload_id)['status'], 'succeeded')

    def test_parser_stdout_noise_does_not_corrupt_the_result_channel(self):
        """A library banner on stdout must not turn a persisted parse into a failure.

        `pymupdf` announces `pymupdf_layout` on stdout the first time layout analysis runs;
        the bounded child's stdout is its result channel, so the parse runs with stdout
        redirected to stderr.
        """
        created = self.dxf()
        upload_id = created.json()['upload_id']
        quiet = upload_pipeline.PARSERS['.dxf']

        def noisy(path, claim):
            print('library banner on stdout')
            return quiet(path, claim)

        captured = io.StringIO()
        with patch.dict(upload_pipeline.PARSERS, {'.dxf': noisy}), \
                contextlib.redirect_stderr(captured):
            outcome = upload_pipeline.work_once(self.dsn, upload_id, in_process=True)
        self.assertEqual(outcome['outcomes'][0]['outcome'], 'succeeded', outcome['outcomes'][0])
        self.assertIn('library banner on stdout', captured.getvalue())
        self.assertEqual(self.detail(upload_id)['status'], 'succeeded')

    def test_a_permanently_too_slow_upload_fails_after_the_wave_budget_is_exhausted(self):
        created = self.dxf()
        upload_id = created.json()['upload_id']
        timeout = {'status': 'timeout', 'error_code': 'TASK_DEADLINE_EXCEEDED',
                   'deadline_seconds': 45.0, 'elapsed_ms': 45000.0}
        with patch.object(upload_pipeline.task_budget, 'run_bounded', return_value=timeout):
            for attempt in range(1, upload_pipeline.MAX_WAVE_ATTEMPTS + 1):
                wave = upload_pipeline.work_once(self.dsn, upload_id)
                self.assertEqual(wave['outcomes'][0]['wave_timeouts'], attempt)
                if attempt < upload_pipeline.MAX_WAVE_ATTEMPTS:
                    self.assertEqual(wave['outcomes'][0]['outcome'], 'running')
                    self.assertEqual(self.detail(upload_id)['status'], 'running')
                    with psycopg2.connect(self.dsn) as conn, conn.cursor() as cur:
                        cur.execute("UPDATE ingest_jobs SET started_at=now()-interval '10 minutes' "
                                    'WHERE job_id=%s', (upload_pipeline.job_id_for(upload_id),))
                else:
                    self.assertEqual(wave['outcomes'][0]['outcome'], 'failed')
        body = self.detail(upload_id)
        self.assertEqual(body['status'], 'failed')
        self.assertEqual(body['error_code'], 'UPLOAD_WAVE_ATTEMPTS_EXHAUSTED')
        self.assertEqual(body['wave_timeouts'], upload_pipeline.MAX_WAVE_ATTEMPTS)
        self.assertNotIn(self.docs[-1], self.listed_document_ids())

    def test_worker_bounds_are_enforced(self):
        for value in (0, 5, True, 'x'):
            with self.assertRaises(ValueError):
                upload_pipeline.work_once(self.dsn, workers=value)
        for value in (0, 99999, True, 'x'):
            with self.assertRaises(ValueError):
                upload_pipeline.work_once(self.dsn, deadline_s=value)


if __name__ == '__main__':
    unittest.main()
