"""The upload pipeline's missing leg: contract blocks -> chunks -> vectors.

Before this module existed the pipeline wrote **evidence** (the unified parse contract:
`document_contract_blocks` / `document_block_relations`) and stopped there. Its
`vector_index` was the constant string `"deferred"`, it never wrote a `chunks` row and it
never wrote a Qdrant point. The retriever (`datamind/knowledge.py`) reads **`chunks`** and
nothing else, so an uploaded file was invisible to Q&A no matter how many times an
administrator approved it: `searchable` was permanently `false` and the file never appeared
in `GET /v1/documents`. Uploading a file achieved nothing.

These tests drive the whole loop with real bytes (a real DXF and a real DOCX through their
real adapters), a real embedding model and a real Qdrant, and assert the properties that
matter:

* **the loop closes** — upload -> parse -> index -> approve -> `searchable` -> listed in
  `GET /v1/documents` -> retrievable by the real retriever;
* **the review gate is not weakened** — before approval the document has chunks and
  vectors but is *not* retrievable, and the retriever cannot select it;
* **the ordering protocol holds** — a failure at any step leaves the document
  non-retrievable, never "old text with new vectors";
* **honesty is preserved** — a failed index is reported as a failed index, never laundered
  into a failed parse, and a page-less format never gets a fabricated page number.

Non-vacuity: the stub embedder and stub Qdrant client are used only where a *failure* is
being exercised; the happy path uses the real model and the real collection, because the
thing being tested is that the two stores agree.
"""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import psycopg2
from psycopg2.extras import RealDictCursor

from datamind import file_uploads
from datamind.agent import _write_dsn
from datamind.api import app
from datamind.knowledge import KnowledgeRetriever
from ingestion import upload_index, upload_pipeline
import auth_fixtures

ROOT = Path(__file__).resolve().parents[1]
DXF = ROOT / 'data/cad_demo/development/dev-mm.dxf'
DOCX = ROOT / 'data/knowledge_sources/SM-GY-2026-001_工艺温度判定说明材料.docx'

#: Ask about text the DXF really contains; a question about nothing in the document would
#: prove only that the retriever can return zero hits.
QUESTION = 'CAD-DEV-1 图号'
#: The Office sample's own words, so the lexical ranking has real CJK overlap.
DOCX_QUESTION = 'A102 保温温度判定流程'


class _StubEmbedder:
    """A deterministic embedder: one 512-dim vector per text, no model load."""

    def __init__(self):
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        for text in texts:
            yield [0.05] * 512


class _StubClient:
    """A Qdrant client that records what was written, without a Qdrant."""

    def __init__(self, *, fail_on_upsert=False):
        self.points = []
        self.deleted = []
        self.fail_on_upsert = fail_on_upsert
        self.closed = False

    def collection_exists(self, name):
        return True

    def create_collection(self, *args, **kwargs):
        return None

    def create_payload_index(self, *args, **kwargs):
        return None

    def delete(self, collection, points_selector=None, wait=False):
        self.deleted.append(collection)
        return None

    def upsert(self, collection, points):
        if self.fail_on_upsert:
            raise RuntimeError('UPLOAD_STUB_QDRANT_UNAVAILABLE')
        self.points.extend(points)
        return None

    def close(self):
        self.closed = True


class UploadIndexTests(unittest.TestCase):
    def setUp(self):
        self.dsn = _write_dsn()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [patch.object(file_uploads, 'ROOT', self.root),
                        patch.object(upload_pipeline, 'ROOT', self.root)]
        for item in self.patches:
            item.start()
        self.addCleanup(self._stop)
        self.admin = TestClient(app, client=('upload-index-' + self.root.name, 50000))
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
            for table in ('document_block_relations', 'document_contract_blocks',
                          'document_block_locators', 'chunks', 'document_blocks'):
                cur.execute(f'DELETE FROM {table} WHERE document_id=ANY(%s)', (self.documents,))
            cur.execute('DELETE FROM ingest_jobs WHERE document_id=ANY(%s)', (self.documents,))
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

    def parsed_dxf(self):
        """A fully parsed, still unapproved DXF: the state every test here starts from."""
        return self.parsed_upload(DXF, 'dev-mm.dxf')

    def parsed_docx(self):
        """A fully parsed, still unapproved Office document (no page concept)."""
        return self.parsed_upload(DOCX, DOCX.name)

    def parsed_upload(self, path, filename):
        created = self.upload(path.read_bytes(), filename=filename)
        self.assertEqual(created.status_code, 201, created.text)
        upload_id = created.json()['upload_id']
        document_id = self.documents[-1]
        self.assertEqual(upload_pipeline.drain(self.dsn, upload_id, in_process=True)['claimed'], 1)
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

    def chunk_rows(self, document_id):
        with psycopg2.connect(self.dsn) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT * FROM chunks WHERE document_id=%s ORDER BY chunk_index',
                        (document_id,))
            return [dict(row) for row in cur.fetchall()]

    def chunk_count(self, document_id):
        with psycopg2.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM chunks WHERE document_id=%s', (document_id,))
            return cur.fetchone()[0]

    # ------------------------------------------------- 1. the loop actually closes
    def test_upload_parse_index_approve_retrieve(self):
        """The whole point: an uploaded file becomes answerable evidence."""
        upload_id, document_id = self.parsed_dxf()

        # The parse produced contract blocks, and the index produced chunks from them.
        body = self.detail(upload_id)
        self.assertEqual(body['status'], 'succeeded')
        self.assertGreaterEqual(body['block_count'], 1)
        self.assertGreaterEqual(body['chunk_count'], 1)
        self.assertEqual(body['vector_index'], 'embedded')
        self.assertIsNone(body['index_error_code'])
        self.assertGreaterEqual(self.chunk_count(document_id), 1)

        # Approval is what publishes it: before approval the document is not listed.
        self.assertFalse(body['searchable'])
        self.assertEqual(body['index_state'], 'not_approved')
        self.assertNotIn(document_id, self.listed_documents())

        approved = self.approve(upload_id)
        self.assertEqual(approved.status_code, 200, approved.text)
        after = approved.json()
        self.assertTrue(after['indexed'])
        self.assertTrue(after['searchable'])
        self.assertEqual(after['index_state'], 'indexed')
        self.assertIsNone(after['index_note'])

        listed = self.listed_documents()
        self.assertIn(document_id, listed, 'a searchable document must be selectable')
        self.assertTrue(listed[document_id]['searchable'])

        # And the real retriever can select it and return its chunk.
        _status, _review, version, parser_version, sha = self.asset(document_id)
        result = KnowledgeRetriever(self.dsn, mode='lexical').retrieve(
            QUESTION, collections=['demo_kb'],
            document_refs=[{'document_id': document_id, 'document_version': version,
                            'source_sha256': sha, 'parser_version': parser_version}])
        self.assertGreaterEqual(result['eligible_chunks'], 1)
        self.assertTrue(result['coverage']['all_covered'], result['coverage'])
        self.assertIn(document_id, {hit['document_id'] for hit in result['hits']})

    def test_an_office_upload_closes_the_same_loop(self):
        """A DOCX has real text and no pages; the loop must still close end to end."""
        upload_id, document_id = self.parsed_docx()
        body = self.detail(upload_id)
        self.assertEqual(body['status'], 'succeeded', body.get('error_code'))
        self.assertGreaterEqual(body['chunk_count'], 1)
        self.assertEqual(body['vector_index'], 'embedded')
        self.assertFalse(body['searchable'])

        approved = self.approve(upload_id)
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertTrue(approved.json()['searchable'])

        _status, _review, version, parser_version, sha = self.asset(document_id)
        result = KnowledgeRetriever(self.dsn, mode='lexical').retrieve(
            DOCX_QUESTION, collections=['demo_kb'],
            document_refs=[{'document_id': document_id, 'document_version': version,
                            'source_sha256': sha, 'parser_version': parser_version}])
        self.assertTrue(result['coverage']['all_covered'], result['coverage'])
        self.assertIn(document_id, {hit['document_id'] for hit in result['hits']})

    # ------------------------------------- 2. the review gate is NOT weakened
    def test_chunks_exist_before_approval_but_the_retriever_cannot_reach_them(self):
        """Indexing publishes vectors; it does not publish the *document*."""
        upload_id, document_id = self.parsed_dxf()
        self.assertGreaterEqual(self.chunk_count(document_id), 1,
                                'the index runs inside the parse wave')
        # The asset is still unapproved and not published.
        status, needs_review, version, parser_version, sha = self.asset(document_id)
        self.assertEqual((status, needs_review), ('parsed', True))
        self.assertNotIn(document_id, self.listed_documents())
        # The server-side half of the gate: selecting an unapproved document is refused.
        # The ref carries the document's *real* identity, so the refusal can only come from
        # the publication gate — not from a malformed selection.
        with self.assertRaises(ValueError) as caught:
            KnowledgeRetriever(self.dsn, mode='lexical').retrieve(
                QUESTION, collections=['demo_kb'],
                document_refs=[{'document_id': document_id, 'document_version': version,
                                'source_sha256': sha, 'parser_version': parser_version}])
        self.assertEqual(str(caught.exception), 'DOCUMENT_SELECTION_UNAVAILABLE')

    # ------------------------------------------ 3. an index failure is honest
    def test_a_failed_index_is_reported_as_a_failed_index_not_a_failed_parse(self):
        """The parse really succeeded; the index did not. Both facts are reported."""
        created = self.upload()
        upload_id = created.json()['upload_id']
        document_id = self.documents[-1]
        client = _StubClient(fail_on_upsert=True)
        with patch.object(upload_index, '_qdrant_client', return_value=client):
            upload_pipeline.drain(self.dsn, upload_id, in_process=True)

        body = self.detail(upload_id)
        self.assertEqual(body['status'], 'succeeded', 'the parse really did succeed')
        self.assertIsNone(body['error_code'])
        self.assertEqual(body['index_error_code'], upload_index.INDEX_ERROR_CODE)
        self.assertEqual(body['chunk_count'], 0)
        # "attempted and failed" is not "nothing to index"; the two must not be conflated.
        self.assertEqual(body['vector_index'], upload_pipeline.VECTOR_INDEX_FAILED)
        # Retrievability is reported truthfully: no chunks, so not searchable.
        self.assertFalse(body['searchable'])
        self.assertEqual(body['index_state'], 'not_approved')
        # Approving it must not publish a document the retriever cannot read.
        approved = self.approve(upload_id)
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertFalse(approved.json()['indexed'])
        self.assertFalse(approved.json()['searchable'])
        self.assertEqual(approved.json()['index_state'], 'no_chunks')
        self.assertEqual(approved.json()['index_note'], 'UPLOAD_HAS_NO_CHUNKS')
        self.assertNotIn(document_id, self.listed_documents())

    # ------------------------------ 4. the ordering protocol: no "old text, new vectors"
    def test_a_failed_vector_write_leaves_the_document_non_retrievable(self):
        """The F10 rule, reused: any failure stops at the non-retrievable state.

        The parse wave already wrote a good index here, so this is a failed *re*-index.
        What the ordering protocol guarantees is that the failure writes **no** `chunks`
        rows: the old rows are not replaced by a partial set, and the asset is never
        published on the strength of them.
        """
        upload_id, document_id = self.parsed_dxf()
        before = [(row['chunk_id'], row['content_hash']) for row in self.chunk_rows(document_id)]
        self.assertGreaterEqual(len(before), 1)
        client = _StubClient(fail_on_upsert=True)
        report = upload_index.index_document(self.dsn, document_id, client=client,
                                             embedder=_StubEmbedder())
        self.assertEqual(report['index_state'], upload_index.INDEX_FAILED)
        self.assertEqual(report['error_code'], upload_index.INDEX_ERROR_CODE)
        self.assertEqual(report['chunk_count'], 0)
        # The failed attempt wrote no metadata rows at all: the chunk store is byte-for-byte
        # what the successful parse left behind.
        after = [(row['chunk_id'], row['content_hash']) for row in self.chunk_rows(document_id)]
        self.assertEqual(after, before, 'a failed index must not rewrite the chunk store')
        # And the document still cannot be selected: it was never approved.
        self.assertNotIn(document_id, self.listed_documents())

    def test_a_reindex_replaces_rather_than_duplicates(self):
        """Re-indexing the same document rewrites its chunks; it never doubles them."""
        upload_id, document_id = self.parsed_dxf()
        first = self.chunk_count(document_id)
        self.assertGreaterEqual(first, 1)
        report = upload_index.index_document(self.dsn, document_id,
                                             embedder=_StubEmbedder(), client=_StubClient())
        self.assertEqual(report['index_state'], upload_index.INDEX_EMBEDDED)
        self.assertEqual(self.chunk_count(document_id), first,
                         'a re-index replaces the rows, it does not append them')

    # ------------------------------------ 5. honesty about pages and locators
    def test_a_pageless_format_gets_no_fabricated_page_number(self):
        """DXF has no page concept, so its chunks must not claim one."""
        upload_id, document_id = self.parsed_dxf()
        rows = self.chunk_rows(document_id)
        self.assertGreaterEqual(len(rows), 1)
        for row in rows:
            self.assertIsNone(row['page_start'], 'no page concept means no page number')
            self.assertIsNone(row['page_end'])
        # The contract blocks really are page-less, so the chunk is reporting the truth
        # rather than dropping a page that was there.
        with psycopg2.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM document_contract_blocks '
                        'WHERE document_id=%s AND page_no IS NOT NULL', (document_id,))
            self.assertEqual(cur.fetchone()[0], 0)

    def test_chunk_text_and_digest_are_consistent(self):
        """The retriever drops a chunk whose text no longer matches its digest."""
        upload_id, document_id = self.parsed_dxf()
        for row in self.chunk_rows(document_id):
            self.assertTrue(row['exact_text'])
            self.assertEqual(row['content_hash'],
                             hashlib.sha256(row['exact_text'].encode()).hexdigest())
            self.assertTrue(row['text_for_embedding'])

    # ------------------------------------- 6. the claim generation fences the index
    def test_a_superseded_generation_does_not_write_the_index(self):
        """A newer generation owns the document; the older one must not write vectors."""
        upload_id, document_id = self.parsed_dxf()
        # Simulate the newer generation: the attempt counter is history and only grows.
        with psycopg2.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute('UPDATE ingest_job_claims SET attempt=attempt+1 '
                        'WHERE job_id=%s', (upload_pipeline.job_id_for(upload_id),))
        stale_claim = {'job_id': upload_pipeline.job_id_for(upload_id), 'attempt': 1,
                        'claim_token': 'stale-token', 'document_id': document_id}
        client = _StubClient()
        report = upload_index.index_document(self.dsn, document_id, claim=stale_claim,
                                             embedder=_StubEmbedder(), client=client)
        self.assertEqual(report['index_state'], upload_index.INDEX_SUPERSEDED)
        self.assertEqual(report['chunk_count'], 0)
        self.assertEqual(client.points, [], 'a superseded generation writes no vectors')

    # ---------------------------------------------- 7. the surfaces agree
    def test_every_surface_reports_the_same_index_state(self):
        """One judgment, four surfaces: list, detail, approve and /v1/documents."""
        upload_id, document_id = self.parsed_dxf()
        self.assertEqual(self.approve(upload_id).status_code, 200)
        item = self.detail(upload_id)
        listed = self.listed_documents()[document_id]
        rows = self.admin.get('/v1/uploads', params={'limit': 200}).json()['uploads']
        listed_item = next(row for row in rows if row['document_id'] == document_id)
        for surface in (item, listed, listed_item):
            self.assertEqual(surface['index_state'], 'indexed')
            self.assertIsNone(surface['index_note'])
            self.assertTrue(surface['searchable'])


if __name__ == '__main__':
    unittest.main()
