"""R05: "parsed", "approved" and "searchable" are three different facts.

The parse wave now writes `chunks` and Qdrant points (`ingestion/upload_index.py`), so the
searchable state is finally reachable. This does **not** collapse the three facts:

* a parsed upload has chunks but is still unapproved — the retriever's own publication
  gate (`datamind.knowledge`) refuses to select it;
* `approve` clears only the review gate; it publishes the asset (`status='indexed'`) iff
  the retriever's own store (`chunks`) is non-empty. A document whose index failed keeps
  `no_chunks` and stays out of `GET /v1/documents`;
* `searchable` is derived on every surface from those three facts, never stored as a flag.

These tests drive the real endpoints against a real parse and assert the *reason* per
state, never a bare boolean.

Non-vacuity:

* `test_approved_upload_without_chunks_stays_unsearchable_and_unlisted` pins the original
  defect guard — `approve` must key on `chunks`, not on `block_count`. If it went back to
  `status='indexed' iff block_count > 0`, this test fails on `searchable`, `index_state`,
  the asset status and the `document_id not in GET /v1/documents` assertion;
* `test_chunks_from_the_parse_wave_make_the_document_searchable_and_retrievable` is the
  reverse control: it proves the flag is not a blanket "false" and that it follows the
  retriever's own store — deleting the chunks flips it back to false.

Every fixture is real: bytes go through `POST /v1/uploads` and the parse runs through
`upload_pipeline` (in-process because the staging root is a temp dir). Rows and staged
files are removed in teardown.
"""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import psycopg2

from datamind import file_uploads
from datamind.agent import _write_dsn
from datamind.api import app
from datamind.knowledge import KnowledgeRetriever
from ingestion import upload_index, upload_pipeline
import auth_fixtures

ROOT = Path(__file__).resolve().parents[1]
DXF = ROOT / 'data/cad_demo/development/dev-mm.dxf'
#: Real text in the DXF, so the lexical retriever really scores this chunk.
QUESTION = 'CAD-DEV-1 图号'


class _FailingClient:
    """A Qdrant client whose write fails, without a Qdrant."""

    def collection_exists(self, name):
        return True

    def create_collection(self, *args, **kwargs):
        return None

    def create_payload_index(self, *args, **kwargs):
        return None

    def delete(self, collection, points_selector=None, wait=False):
        return None

    def upsert(self, collection, points):
        raise RuntimeError('R05_STUB_QDRANT_UNAVAILABLE')

    def close(self):
        return None


class UploadSearchabilityTests(unittest.TestCase):
    def setUp(self):
        self.dsn = _write_dsn()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [patch.object(file_uploads, 'ROOT', self.root),
                        patch.object(upload_pipeline, 'ROOT', self.root)]
        for item in self.patches:
            item.start()
        self.addCleanup(self._stop)
        self.admin = TestClient(app, client=('r05-admin-' + self.root.name, 50000))
        auth_fixtures.login(self.admin, roles=('admin',))
        self.uploads = []
        self.documents = []
        self.addCleanup(self._cleanup)

    # ------------------------------------------------------------------ harness
    def _stop(self):
        auth_fixtures.cleanup(self.admin)
        self.admin.close()
        for item in self.patches:
            item.stop()
        self.temp.cleanup()

    def _cleanup(self):
        with psycopg2.connect(self.dsn) as conn, conn.cursor() as cur:
            for table in ('ingest_jobs', 'document_contract_blocks', 'document_block_relations',
                          'document_block_locators', 'chunks', 'document_blocks'):
                cur.execute(f'DELETE FROM {table} WHERE document_id=ANY(%s)', (self.documents,))
            cur.execute('DELETE FROM document_assets WHERE document_id=ANY(%s)', (self.documents,))

    def upload(self, data=None, filename='dev-mm.dxf'):
        data = DXF.read_bytes() if data is None else data
        response = self.admin.post(
            '/v1/uploads', params={'filename': filename, 'provenance': 'synthetic_demo'},
            content=data, headers={'Content-Type': 'application/octet-stream'})
        if response.status_code == 201:
            self.uploads.append(response.json()['upload_id'])
            self.documents.append(upload_pipeline.identities(
                hashlib.sha256(data).hexdigest())['document_id'])
        return response

    def parsed_dxf(self, *, index_client=None):
        """A fully parsed, still unapproved DXF: the state every test here starts from.

        `index_client` lets a test make the index fail while the parse succeeds.
        """
        created = self.upload()
        self.assertEqual(created.status_code, 201, created.text)
        upload_id = created.json()['upload_id']
        document_id = self.documents[-1]
        if index_client is None:
            result = upload_pipeline.drain(self.dsn, upload_id, in_process=True)
        else:
            with patch.object(upload_index, '_qdrant_client', return_value=index_client):
                result = upload_pipeline.drain(self.dsn, upload_id, in_process=True)
        self.assertEqual(result['claimed'], 1)
        return upload_id, document_id

    def detail(self, upload_id):
        response = self.admin.get('/v1/uploads/' + upload_id)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def approve(self, upload_id):
        return self.admin.post(f'/v1/uploads/{upload_id}/approve')

    def listed_documents(self):
        response = self.admin.get('/v1/documents')
        self.assertEqual(response.status_code, 200, response.text)
        return {row['document_id']: row for row in response.json()['documents']}

    def asset(self, document_id):
        with psycopg2.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute('SELECT status,needs_review,doc_version,parser_version,sha256 '
                        'FROM document_assets WHERE document_id=%s', (document_id,))
            return cur.fetchone()

    def chunk_count(self, document_id):
        with psycopg2.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM chunks WHERE document_id=%s', (document_id,))
            return cur.fetchone()[0]

    def upload_row(self, document_id):
        rows = self.admin.get('/v1/uploads', params={'limit': 200}).json()['uploads']
        return next(row for row in rows if row['document_id'] == document_id)

    def delete_chunks(self, document_id):
        """Emulate an empty retriever store (an index that never completed)."""
        with psycopg2.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute('DELETE FROM chunks WHERE document_id=%s', (document_id,))

    # ------------------------------------------- 1. parsed but not approved: not_approved
    def test_a_parsed_but_unapproved_upload_is_not_searchable(self):
        upload_id, document_id = self.parsed_dxf()
        body = self.detail(upload_id)
        self.assertIn(body['status'], ('succeeded', 'partial_failure'))
        self.assertTrue(body['review_required'])
        self.assertEqual(body['review_state'], 'pending_review')
        # The parse wave really did build the index — and that still does not publish it.
        self.assertEqual(body['vector_index'], 'embedded')
        self.assertGreaterEqual(self.chunk_count(document_id), 1)
        # The reason is the missing approval, not the missing index.
        self.assertFalse(body['searchable'])
        self.assertEqual(body['index_state'], 'not_approved')
        self.assertEqual(body['index_note'], 'UPLOAD_NOT_APPROVED')
        self.assertGreaterEqual(body['block_count'], 1, 'the real parse stored contract blocks')
        # The list agrees with the detail, in both directions.
        item = self.upload_row(document_id)
        self.assertFalse(item['searchable'])
        self.assertEqual(item['index_state'], 'not_approved')
        self.assertEqual(item['index_note'], 'UPLOAD_NOT_APPROVED')
        self.assertNotIn(document_id, self.listed_documents())

    # ------------------------- 2. approved but nothing to retrieve: no_chunks (the defect)
    def test_approved_upload_without_chunks_stays_unsearchable_and_unlisted(self):
        """A failed index leaves contract blocks but no chunk store; approval must not lie."""
        upload_id, document_id = self.parsed_dxf(index_client=_FailingClient())
        self.assertGreaterEqual(self.detail(upload_id)['block_count'], 1)
        self.assertEqual(self.chunk_count(document_id), 0)
        approved = self.approve(upload_id)
        self.assertEqual(approved.status_code, 200, approved.text)
        body = approved.json()
        self.assertFalse(body['review_required'])
        self.assertEqual(body['review_state'], 'approved')
        # Approving cleared the gate; it did not create an index.
        self.assertFalse(body['indexed'])
        self.assertFalse(body['searchable'])
        self.assertEqual(body['index_state'], 'no_chunks')
        self.assertEqual(body['index_note'], 'UPLOAD_HAS_NO_CHUNKS')
        self.assertGreaterEqual(body['block_count'], 1, 'blocks alone must not imply retrieval')
        self.assertFalse(body['approval']['indexed'])
        self.assertEqual(body['index_error_code'], upload_index.INDEX_ERROR_CODE)
        # The asset is *not* published, so the selector cannot offer it.
        status, needs_review, _version, _parser, _sha = self.asset(document_id)
        self.assertEqual((status, needs_review), ('parsed', False))
        self.assertNotEqual(status, 'indexed')
        self.assertNotIn(document_id, self.listed_documents(), 'must not be an answerable source')
        self.assertEqual(self.admin.get(f'/v1/documents/{document_id}').status_code, 404)
        self.assertEqual(self.detail(upload_id)['index_state'], 'no_chunks')
        # And the listing says the same thing for the same reason.
        item = self.upload_row(document_id)
        self.assertEqual(item['review_state'], 'approved')
        self.assertFalse(item['searchable'])
        self.assertEqual(item['index_state'], 'no_chunks')
        self.assertEqual(item['index_note'], 'UPLOAD_HAS_NO_CHUNKS')
        self.assertEqual(item['index_error_code'], upload_index.INDEX_ERROR_CODE)

    # ------------------------------- 3. reverse control: a real chunk store flips it to true
    def test_chunks_from_the_parse_wave_make_the_document_searchable_and_retrievable(self):
        upload_id, document_id = self.parsed_dxf()
        self.assertEqual(self.approve(upload_id).status_code, 200)
        body = self.detail(upload_id)
        self.assertTrue(body['searchable'])
        self.assertEqual(body['index_state'], 'indexed')
        self.assertIsNone(body['index_note'])
        status, needs_review, _version, _parser, _sha = self.asset(document_id)
        self.assertEqual((status, needs_review), ('indexed', False))
        listed = self.listed_documents()
        self.assertIn(document_id, listed, 'a searchable document must be selectable')
        self.assertTrue(listed[document_id]['searchable'])
        item = self.upload_row(document_id)
        self.assertTrue(item['searchable'])
        self.assertEqual(item['index_state'], 'indexed')
        self.assertIsNone(item['index_note'])

        # The flag is not a label: the real retriever returns this document's chunk.
        _status, _review, version, parser_version, sha = self.asset(document_id)
        result = KnowledgeRetriever(self.dsn, mode='lexical').retrieve(
            QUESTION, collections=['demo_kb'],
            document_refs=[{'document_id': document_id, 'document_version': version,
                            'source_sha256': sha, 'parser_version': parser_version}])
        self.assertGreaterEqual(result['eligible_chunks'], 1)
        self.assertEqual(result['coverage']['all_covered'], True, result['coverage'])
        self.assertIn(document_id, {hit['document_id'] for hit in result['hits']})

        # ...and an empty retriever store flips it back: it is derived, not stored.
        self.delete_chunks(document_id)
        self.assertFalse(self.detail(upload_id)['searchable'])
        self.assertEqual(self.detail(upload_id)['index_state'], 'no_chunks')
        # The row may still be *visible* (it is approved and published), but it is reported
        # as not searchable — which is what the Q&A selector gates on.
        self.assertFalse(self.listed_documents()[document_id]['searchable'])

    # ------------------------------------------------- 4. `searchable` on every listed row
    def test_every_documents_item_carries_searchable(self):
        listed = self.listed_documents()
        self.assertTrue(listed, 'the demo knowledge base is expected to have visible documents')
        for document_id, row in listed.items():
            self.assertIn('searchable', row, document_id)
            self.assertIsInstance(row['searchable'], bool, document_id)
            self.assertIn('index_state', row, document_id)
            self.assertIn('index_note', row, document_id)
            self.assertIn(row['index_state'],
                          ('indexed', 'no_chunks', 'not_approved'), document_id)
            self.assertEqual(row['index_state'] == 'indexed', row['searchable'], document_id)
        # At least one row is genuinely retrievable, so the field is not uniformly false.
        self.assertTrue(any(row['searchable'] for row in listed.values()))

    # ------------- 4b. one judgment: `/v1/documents` and the upload surfaces cannot drift
    def test_documents_and_upload_detail_agree_on_the_index_state(self):
        upload_id, document_id = self.parsed_dxf()
        self.assertEqual(self.approve(upload_id).status_code, 200)
        # Emulate the historical/hand-marked row this fix is about: published as `indexed`
        # while the retriever's own store is still empty.
        self.delete_chunks(document_id)
        item = self.listed_documents()[document_id]
        body = self.detail(upload_id)
        self.assertEqual(item['index_state'], body['index_state'])
        self.assertEqual(item['index_note'], body['index_note'])
        self.assertFalse(item['searchable'] or body['searchable'])
        self.assertEqual((item['index_state'], item['index_note']),
                         ('no_chunks', 'UPLOAD_HAS_NO_CHUNKS'))
        self.assertEqual((body['index_state'], body['index_note']),
                         ('no_chunks', 'UPLOAD_HAS_NO_CHUNKS'))
        # With a real chunk store both surfaces flip together, to the same values.
        report = upload_index.index_document(self.dsn, document_id)
        self.assertEqual(report['index_state'], upload_index.INDEX_EMBEDDED)
        item = self.listed_documents()[document_id]
        body = self.detail(upload_id)
        self.assertTrue(item['searchable'] and body['searchable'])
        self.assertEqual((item['index_state'], item['index_note']), ('indexed', None))
        self.assertEqual((body['index_state'], body['index_note']), ('indexed', None))

    # ---------------------------------------------------------------- 5. approve idempotent
    def test_approve_is_idempotent_and_never_creates_a_second_index(self):
        upload_id, document_id = self.parsed_dxf()
        first = self.approve(upload_id)
        second = self.approve(upload_id)
        for response in (first, second):
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()['indexed'])
            self.assertTrue(response.json()['searchable'])
            self.assertEqual(response.json()['index_state'], 'indexed')
            self.assertIsNone(response.json()['index_note'])
            self.assertEqual(response.json()['review_state'], 'approved')
        self.assertEqual(self.asset(document_id)[0], 'indexed', 'no second state change')
        first_count = self.chunk_count(document_id)
        self.assertGreaterEqual(first_count, 1)
        self.assertEqual(self.chunk_count(document_id), first_count,
                         'approving twice must not duplicate the index')
        with psycopg2.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM document_assets WHERE document_id=%s', (document_id,))
            self.assertEqual(cur.fetchone()[0], 1)
            cur.execute('SELECT count(*) FROM ingest_jobs WHERE document_id=%s', (document_id,))
            self.assertEqual(cur.fetchone()[0], 1, 'no second job or attempt was created')
            cur.execute("SELECT quality_detail->'review_approval'->>'user_id' "
                        'FROM document_assets WHERE document_id=%s', (document_id,))
            self.assertEqual(cur.fetchone()[0], self.admin._fixture_users[0])

    # --------------------------------- 6. a failed parse can never be approved into retrieval
    def test_a_failed_parse_cannot_be_approved_into_a_searchable_document(self):
        created = self.upload(b'this is not a drawing at all', filename='broken.dxf')
        self.assertEqual(created.status_code, 201, created.text)
        upload_id = created.json()['upload_id']
        document_id = self.documents[-1]
        self.assertEqual(upload_pipeline.drain(self.dsn, upload_id, in_process=True)['claimed'], 1)
        body = self.detail(upload_id)
        self.assertEqual(body['status'], 'failed')
        self.assertEqual(body['error_code'], 'PARSE_ADAPTER_UNKNOWN')
        self.assertFalse(body['searchable'])
        self.assertEqual(body['index_state'], 'not_approved')
        rejected = self.approve(upload_id)
        self.assertEqual(rejected.status_code, 409, rejected.text)
        self.assertEqual(rejected.json()['detail']['error_code'], 'UPLOAD_NOT_REVIEWABLE')
        self.assertEqual(self.asset(document_id)[:2], ('failed', True))
        self.assertEqual(self.chunk_count(document_id), 0)
        self.assertNotIn(document_id, self.listed_documents())


if __name__ == '__main__':
    unittest.main()
