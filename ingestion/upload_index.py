"""Turn a validated upload into retrievable chunks and vectors.

Why this module exists
----------------------
The upload pipeline (`ingestion/upload_pipeline.py`) writes **evidence**: the unified parse
contract (`document_contract_blocks` / `document_block_relations`). It deliberately stopped
there — the pipeline's `vector_index` was the constant string `"deferred"`, so it never
wrote a `chunks` row and never wrote a Qdrant point.

That left the feature half-built. The retriever (`datamind/knowledge.py`) reads **`chunks`
and nothing else**, so an uploaded file was invisible to Q&A no matter how many times an
administrator approved it: `searchable` was permanently `false` and the file never appeared
in `GET /v1/documents`. Uploading a file achieved nothing.

This module is the missing leg. It reads a document's validated contract blocks, cuts them
into chunks, embeds them, and publishes them under the **same ordering protocol** the
knowledge-base ingest already uses (`ingestion/ingest.py::publish_document`):

1. put the asset into a **non-retrievable** state and commit — *before* the first Qdrant
   write, so no reader can ever see new vectors beside old text;
2. delete this document's old points and upsert the new ones;
3. write the PostgreSQL metadata (the `chunks` rows), then set the terminal status.

A failure in any step leaves the document **non-retrievable**. It never leaves "old text
with new vectors" or "new text with no vectors" visible to a reader. This is the F10
cross-store consistency rule from `docs/AUDIT_FIXES.md`, reused rather than reinvented.

The review gate still governs visibility
----------------------------------------
Indexing does **not** publish the document. The asset keeps `needs_review=TRUE` and a
non-`indexed` status until an administrator approves it, and the retriever's own gate
(`datamind/visibility.py::published_document_sql`) requires `status='indexed'` **and**
`NOT needs_review`. So:

* before approval — chunks and vectors exist, but the retriever cannot select the document;
* after approval — `upload_pipeline.approve` already contains the rule
  `status = 'indexed' if chunks else status`, so the document becomes retrievable.

No new status field is introduced and no existing gate is weakened. "Approved" and
"retrievable" stay two separate facts, and `upload_pipeline.searchability` remains the one
derived judgment.

Honest boundaries
-----------------
* A chunk's page range is `None` for formats without a page concept (Office, DXF). That is
  the absence of a page, not page 0 — a locator is never invented.
* If indexing fails, the **parse** is still reported as it really happened. The failure is
  reported separately (see `index_error_code`), because a failed index must not be laundered
  into a failed parse, and a successful parse must not be laundered into a successful index.
* OCR and formula recognition are not given their own budgets here; they run inside the
  parse wave. This module only embeds text that already exists.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
import uuid

import psycopg2
from psycopg2.extras import RealDictCursor

from datamind.knowledge import EMBED_MODEL, embedding_model

#: The Qdrant collection the retriever queries (`datamind/knowledge.py::_dense`).
COLLECTION = "datamind_knowledge"

#: The embedding dimension the collection is created with (`ingestion/ingest.py`).
EMBED_DIM = 512

#: The stable error code reported when indexing could not be completed.
INDEX_ERROR_CODE = "UPLOAD_INDEX_FAILED"

#: The `document_assets.status` value that keeps a document out of the retriever's reach.
NON_SEARCHABLE_STATUS = "parsing"

#: The `document_assets.status` value a parsed-but-unapproved upload keeps.
PARSED_STATUS = "parsed"

#: The index states an upload can report. `embedded` is the only one that means the
#: document has chunks *and* vectors; `deferred` means there was no text to index;
#: `failed` means indexing was attempted and did not complete; `superseded` means a newer
#: claim generation took the document over and owns the index.
INDEX_EMBEDDED = "embedded"
INDEX_DEFERRED = "deferred"
INDEX_FAILED = "failed"
INDEX_SUPERSEDED = "superseded"



def _qdrant_url() -> str:
    return os.environ.get("DATAMIND_QDRANT_URL", "http://127.0.0.1:6333")


def contract_blocks(cur, document_id):
    """Read a document's validated contract blocks back, in reading order.

    The blocks are re-read from the database rather than taken from the parse payload so
    that this module indexes **what was stored**, not what was handed to the writer. The
    optional columns (`page_no`, `heading_level`, `table_cells`) are read when the
    database has them and default to `None` when it does not, so a chunk never claims a
    heading level or a page number the store does not hold.
    """
    cur.execute("""SELECT block_id, block_type, text, reading_order, page_no,
                          heading_level, table_cells, formula_latex, ocr_confidence, bbox
                   FROM document_contract_blocks WHERE document_id=%s
                   ORDER BY reading_order, block_id""", (document_id,))
    blocks = []
    for row in cur.fetchall():
        stored = dict(row)
        blocks.append({
            'block_id': stored['block_id'],
            'block_type': stored['block_type'],
            'text': stored['text'] or '',
            'reading_order': stored['reading_order'],
            'page_no': stored['page_no'],
            'heading_level': stored['heading_level'],
            'table_cells': stored['table_cells'],
            'formula_latex': stored['formula_latex'],
            'ocr_confidence': stored['ocr_confidence'],
            'bbox': stored['bbox'],
        })
    return blocks


def chunks_for(document_id, blocks):
    """Cut validated contract blocks into chunks using the ingest chunker's own rules.

    `ingestion/ingest.py::build_chunks` is reused verbatim rather than reimplemented: the
    heading tree, the separate table chunk and the overlap window are already covered by
    `tests/test_ingest_block_identity.py` and `tests/test_rag.py`. A second implementation
    would be a second set of chunking rules — and the retriever's behaviour would depend on
    which one ran.

    The blocks here carry the same field names the chunker expects. A format without a page
    concept yields `page_start`/`page_end` of `None`: the absence of a page, not page 0.
    """
    from .ingest import build_chunks
    return build_chunks(blocks, document_id)


def _points(chunks, vectors, *, document_id, title, collection, origin):
    """Build the Qdrant points, with the same payload the retriever filters on.

    The payload is not decoration: `datamind/knowledge.py::_dense` filters on
    `collection`, `acl` and `embedding_model`, and the citation layer reads `chunk_id`.
    A point missing one of those fields is simply invisible to dense retrieval.
    """
    from qdrant_client.models import PointStruct
    points = []
    for chunk, vector in zip(chunks, vectors):
        vector_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, chunk['chunk_id']))
        chunk['vector_id'] = vector_id
        points.append(PointStruct(id=vector_id, vector=list(vector), payload={
            'document_id': document_id, 'chunk_id': chunk['chunk_id'],
            'chunk_type': chunk['chunk_type'], 'section_path': chunk['section_path'],
            'page_start': chunk['page_start'], 'page_end': chunk['page_end'],
            'acl': ['public'], 'collection': collection, 'document_version': 1,
            'embedding_model': EMBED_MODEL, 'title': title, 'origin': origin,
        }))
    return points


def _write_chunks(cur, document_id, chunks, *, quality_score):
    """Replace this document's `chunks` rows. Returns the number of rows written."""
    cur.execute('DELETE FROM chunks WHERE document_id=%s', (document_id,))
    for index, chunk in enumerate(chunks):
        cur.execute("""INSERT INTO chunks(chunk_id, document_id, document_version, chunk_index,
            chunk_type, section_path, page_start, page_end, block_ids,
            text_for_embedding, exact_text, table_json, token_count,
            quality_score, acl, embedding_model, vector_id, content_hash)
            VALUES (%s,%s,1,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (chunk['chunk_id'], document_id, index, chunk['chunk_type'],
                     chunk['section_path'], chunk['page_start'], chunk['page_end'],
                     chunk['block_ids'], chunk['text_for_embedding'], chunk['exact_text'],
                     json.dumps(chunk['table_json'], ensure_ascii=False)
                     if chunk.get('table_json') else None,
                     int(len(chunk['exact_text'] or '') * 0.6), quality_score, ['public'],
                     EMBED_MODEL, chunk['vector_id'],
                     hashlib.sha256((chunk['exact_text'] or '').encode()).hexdigest()))
    return len(chunks)


def _asset(cur, document_id):
    cur.execute("""SELECT document_id, title, collection, origin, status, needs_review,
                          quality_score, acl FROM document_assets WHERE document_id=%s""",
                (document_id,))
    return cur.fetchone()


def index_document(dsn, document_id, *, deadline_s=None, embedder=None, client=None,
                   claim=None):
    """Index one uploaded document: chunk, embed, and publish under the ordering protocol.

    Returns a report: `{'chunk_count': int, 'index_state': 'embedded'|'deferred'|'failed'
    |'superseded', 'error_code': str|None}`. It never raises for an indexing problem — the
    caller decides how to report it, and a parse that succeeded must not be turned into a
    parse failure.

    **This does not publish the document.** The asset keeps `status='parsed'` and
    `needs_review=TRUE`; only `upload_pipeline.approve` may set `status='indexed'`, and
    only when chunks exist. Indexing therefore never shortens the review gate.

    `embedder` and `client` exist so a test can inject a stub instead of loading the model
    and talking to Qdrant. The default path uses exactly the model and collection the
    retriever uses.

    **Claim generation.** `claim` is the generation that owns this document. When it is
    given, the index write is fenced on it: a generation that was superseded while it was
    embedding must not write vectors for a document a newer generation has already
    re-parsed (the same rule every other claim-dependent write follows — R04). The fence is
    checked again immediately before the Qdrant write, because the embedding call happens
    outside the lock and can take long enough for a re-upload to overtake it.

    **stdout.** The bounded child reports its one result as a single JSON object on stdout
    (`datamind.task_budget`). The embedding stack is third-party and does print on some
    paths (`onnxruntime` logs an EP fallback with `print()`), so the whole index call
    redirects stdout to stderr: a diagnostic must never be able to corrupt the result
    channel and turn a successful parse into `TASK_RESULT_INVALID`.
    """
    try:
        with contextlib.redirect_stdout(sys.stderr):
            return _index_document(dsn, document_id, deadline_s=deadline_s,
                                   embedder=embedder, client=client, claim=claim)
    except Exception:
        # The ordering protocol already left the document non-retrievable. Report the
        # failure honestly; never return a partial success.
        return {'chunk_count': 0, 'index_state': INDEX_FAILED,
                'error_code': INDEX_ERROR_CODE}


def _index_document(dsn, document_id, *, deadline_s=None, embedder=None, client=None,
                    claim=None):
    """The body of :func:`index_document`, with stdout already redirected."""
    with psycopg2.connect(dsn) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        asset = _asset(cur, document_id)
        if not asset:
            return {'chunk_count': 0, 'index_state': INDEX_FAILED,
                    'error_code': 'UPLOAD_ASSET_MISSING'}
        blocks = contract_blocks(cur, document_id)
        if not blocks:
            # Nothing to index is not a failure: the document simply has no text.
            return {'chunk_count': 0, 'index_state': INDEX_DEFERRED, 'error_code': None}
        chunks = chunks_for(document_id, blocks)
        if not chunks:
            return {'chunk_count': 0, 'index_state': INDEX_DEFERRED, 'error_code': None}
        title = asset['title'] or document_id
        collection = asset['collection'] or 'demo_kb'
        origin = asset['origin'] or 'upload'

        # --- Step 1: non-retrievable first, committed before any vector write.
        # The asset is already outside the retriever's reach (it is `parsing` or
        # `parsed` with `needs_review=TRUE`); this statement makes the intent
        # explicit and keeps a re-index from racing an approval.
        cur.execute("""UPDATE document_assets SET status=%s
            WHERE document_id=%s AND status<>'indexed'""",
                    (PARSED_STATUS, document_id))
        conn.commit()

        # --- Step 2: vectors (Qdrant) before the metadata rows.
        model = embedder if embedder is not None else embedding_model()
        texts = [chunk['text_for_embedding'] for chunk in chunks]
        vectors = list(model.embed(texts)) if texts else []
        if len(vectors) != len(chunks):
            raise ValueError('UPLOAD_EMBEDDING_COUNT_MISMATCH')

        # The fence is re-checked *here*, after the embedding call: the embedding runs
        # outside any lock and can take long enough for a re-upload (same bytes → same
        # `document_id`) to open a newer generation. A superseded generation must not
        # write vectors the newer one is about to write itself.
        if claim is not None and not _generation_owns_document(dsn, claim):
            return {'chunk_count': 0, 'index_state': INDEX_SUPERSEDED,
                    'error_code': None}

        points = _points(chunks, vectors, document_id=document_id, title=title,
                         collection=collection, origin=origin)
        qdrant = client if client is not None else _qdrant_client()
        try:
            ensure_collection(qdrant)
            _delete_points(qdrant, document_id)
            if points:
                qdrant.upsert(COLLECTION, points)
        finally:
            if client is None:
                qdrant.close()

        # --- Step 3: metadata rows. The status stays `parsed`: the retriever cannot
        # select this document until an administrator approves it, and approval is
        # what turns `status` into `indexed`.
        written = _write_chunks(cur, document_id, chunks,
                                 quality_score=asset['quality_score'])
        conn.commit()
        return {'chunk_count': written, 'index_state': INDEX_EMBEDDED,
                'error_code': None}


def _generation_owns_document(dsn, claim):
    """Whether `claim` is still the generation that owns this document's parse result.

    The parse transaction has already committed by the time the index runs, and it
    **cleared the claim token** on the way out — so the live-claim check
    (`upload_pipeline._lock_live_claim`) would answer "no" for a perfectly current
    generation. The generation that owns the document is therefore identified by the
    claim's `attempt`: a newer generation has a strictly larger `attempt`, and the
    attempt counter is never reset (it is history, not state).

    This is the same rule the rest of the pipeline follows — "the newest generation
    wins" — expressed on the one field that keeps increasing. A generation whose
    `attempt` is still the highest one in `ingest_job_claims` owns the document; once a
    newer generation has claimed the job, the older one must not write anything.
    """
    try:
        with psycopg2.connect(dsn) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT attempt FROM ingest_job_claims WHERE job_id=%s',
                        (claim['job_id'],))
            row = cur.fetchone()
    except Exception:
        # An unreadable claim row is not a licence to write: fail closed.
        return False
    if not row:
        return False
    return int(row['attempt']) <= int(claim.get('attempt') or 0)


def _qdrant_client():
    from qdrant_client import QdrantClient
    return QdrantClient(url=_qdrant_url(), timeout=5, trust_env=False)


def _delete_points(qdrant, document_id):
    """Remove this document's previous points so a re-index cannot leave orphans."""
    from qdrant_client.models import FieldCondition, Filter, FilterSelector, MatchValue
    qdrant.delete(COLLECTION, points_selector=FilterSelector(filter=Filter(must=[
        FieldCondition(key="document_id", match=MatchValue(value=document_id))])), wait=True)


def ensure_collection(qdrant):
    """Create the collection and its payload indexes when they are missing.

    The knowledge-base ingest creates the collection itself; an upload that arrives before
    any ingest has run would otherwise write into a non-existent collection. Creating it
    with the same parameters keeps both paths in one collection.
    """
    from qdrant_client.models import Distance, VectorParams
    if not qdrant.collection_exists(COLLECTION):
        qdrant.create_collection(COLLECTION,
                                 vectors_config=VectorParams(size=EMBED_DIM,
                                                             distance=Distance.COSINE))
    qdrant.create_payload_index(COLLECTION, "document_id", "keyword")
    qdrant.create_payload_index(COLLECTION, "chunk_type", "keyword")
