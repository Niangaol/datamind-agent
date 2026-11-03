"""Upload -> staged bytes -> durable parse queue -> contract store -> review gate.

One call (`create`) stages the raw body, registers the `document_assets` row and queues a
durable parse job; parsing runs asynchronously and writes the unified parse contract with
`ingestion.contract_store.persist_result`. A successfully parsed upload is **never
retrievable**: the asset keeps `needs_review=TRUE` and a non-`indexed` status until an
explicit `approve` action clears the gate.

Why this is not the `file_jobs` queue
-------------------------------------
`file_jobs` is the durable queue for staged DXF/measurement *registrations*, and its two
targets are reused here in spirit: the DXF branch parses through `ingestion.cad_adapter`
(the adapter both CAD paths share) and the JSON branch validates through
`datamind.cad_dimensions.measurement_from_bytes` (the parser behind the existing
measurement target). The queue itself lives in the pre-existing `ingest_jobs` table
because:

* `file_job_items.kind` is `CHECK(kind IN ('drawing','measurement'))` (migration 010), so
  an Office/PDF parse cannot be queued there without a new migration, and this change is
  not allowed to add one;
* neither `cad_store.register` nor `cad_dimensions.register_measurement` writes the
  unified contract store or a `document_assets` row, which the upload contract requires.

The runner is the same bounded-process/deadline mechanism (`datamind.task_budget`), and a
claim is fenced by the row's own `queued -> running -> terminal` transition with
`FOR UPDATE SKIP LOCKED` **and** by the claim generation (`claim_token`/`attempt`, R04);
a crashed worker's stale `running` row is reclaimable after `stale_running_seconds()`.

Claim generations (R04)
-----------------------
A row transition is not an identity: the same bytes re-uploaded are the same `job_id`, so
"still `running`" can be true for *two* different claims. Every claim therefore stamps
`ingest_job_claims.claim_token` with a fresh random value and increments
`ingest_job_claims.attempt` in the same transaction (migration 018; the generation lives in
its own table because `ingest_jobs` is owned by `postgres` and cannot be altered by the
`datamind_app` migration role). All claim-dependent writes — the contract result,
`quality_detail`, the terminal status and the wave-timeout counter — re-check that token
under `SELECT ... FOR UPDATE` and commit **nothing** when it no longer matches:

* `run_parse` parses outside any transaction, then does
  `lock + token check -> persist_result -> quality_detail -> terminal status` in **one**
  transaction, so an old claim can neither leave half a result nor a wrong terminal state;
* a re-upload of a `running` job does **not** reset it (no new generation is opened and
  the current one is returned as-is); a terminal job is re-armed to `queued` and the next
  claim opens the next generation.

A worker is the clock
---------------------
A job advances **only when something calls the worker**:

* `ingestion.upload_worker` (`python -m ingestion.upload_worker --watch|--once`) is that
  caller; it reuses `work_once`/`drain` and publishes a heartbeat in `task_leases`;
* `GET /v1/uploads/{upload_id}` is a **pure read** — it reports the truth but never
  advances anything;
* `GET /v1/uploads` (the list) is read-only too, and additionally reports
  `worker.alive`/`worker.last_seen_at` so a client can honestly say "后台处理器未运行"
  instead of showing a `queued` job as "解析中";
* with no worker running, a job stays `queued` forever (that is a deployment fact, not a
  parse failure).

Every parse runs in a bounded child process (`task_budget.run_bounded`); nothing parses
inline in the API process unless a caller explicitly passes `in_process=True` (tests and a
worker that already owns a disposable process).

Honesty rules enforced here
---------------------------
* `vector_index` reports what really happened to the index, never a claim: `embedded`
  once the parse wave has written this document's `chunks` rows and Qdrant points
  (`ingestion/upload_index.py`), `failed` when indexing was attempted and did not
  complete, and `deferred` when there was nothing to index. The index is **not** the
  review gate: `embedded` still means "not retrievable" until an administrator approves.
* Retrievability is **derived, never stored** (R05): every item carries
  `searchable = (status == 'indexed') and not needs_review and chunk_count > 0`, plus the
  reason as `index_state` (`indexed`/`no_chunks`/`not_approved`) and `index_note`.
  `approve` clears the review gate, but it only marks the asset `indexed` when the chunk
  store the retriever actually reads (`datamind.knowledge` reads `chunks` only) is
  non-empty. A contract with blocks is evidence, not an index.
* A parse result the contract refuses is stored as a stable `error_code`, never as a
  successful empty document; an explicit contract gap (`unsupported_features`) is carried
  through into the summary the `/contract` endpoint repeats.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import socket
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException
import psycopg2
from psycopg2.extras import Json, RealDictCursor

from datamind import parse_contract, task_budget
from datamind.auth import lock_identity
from datamind.knowledge import ROOT

from . import contract_store

QUEUE_JOB_TYPE = 'upload_parse'
ORIGIN = 'upload'
COLLECTION = 'demo_kb'
STATUSES = ('queued', 'running', 'succeeded', 'failed', 'partial_failure', 'cancelled')
TERMINAL = ('succeeded', 'failed', 'partial_failure', 'cancelled')
ASSET_TERMINAL = {'succeeded': 'parsed', 'partial_failure': 'parsed', 'failed': 'failed',
                  'cancelled': 'pending'}
#: One poll advances at most one bounded wave. The default is deliberately short: a GET
#: must return, and an unfinished parse stays `running` for a later poll to reclaim.
DEFAULT_WAVE_DEADLINE_SECONDS = 45.0
WAVE_DEADLINE_ENV = 'DATAMIND_UPLOAD_WAVE_DEADLINE_SECONDS'
#: A `running` row older than this is reclaimable. The effective value is raised above the
#: configured wave deadline so a live child is never treated as stale.
STALE_RUNNING_SECONDS = 120
#: Total wave timeouts tolerated before the upload fails honestly instead of being retried
#: forever (a 32 MiB PDF that never fits any single wave must eventually say so).
MAX_WAVE_ATTEMPTS = 3
DEADLINE_ERROR_CODE = 'UPLOAD_DEADLINE_EXCEEDED'
WAVE_EXHAUSTED_ERROR_CODE = 'UPLOAD_WAVE_ATTEMPTS_EXHAUSTED'
FALLBACK_ERROR_CODE = 'UPLOAD_PARSE_FAILED'
DWG_ERROR_CODE = 'CAD_DWG_CONVERTER_UNAVAILABLE'
#: A completion (or a result write) whose claim generation is no longer the current one.
#: Nothing is written when this is returned; the newer claim owns the row.
CLAIM_LOST_ERROR_CODE = 'UPLOAD_CLAIM_LOST'

#: Cross-process wave slots (mirrors `ingestion.ocr.OCR_SLOT_NAME`): the advisory lock is
#: held per wave, so N worker processes can never run more than ``DATAMIND_TASK_SLOTS``
#: waves — and each wave's claims parse sequentially — bounding total concurrent parses.
UPLOAD_SLOT_NAME = 'upload-parse'
UPLOAD_SLOTS = 2

#: Worker heartbeat (review 5.1). The worker registers a row in the pre-existing
#: `task_leases` table (migration 013) and renews it; `worker_heartbeat` reports
#: ``alive = last seen within `worker_alive_seconds()` ``. The lease TTL equals that
#: threshold, so an un-renewed (crashed) worker stops being "alive" by itself.
HEARTBEAT_PREFIX = 'upload_worker'
WORKER_ALIVE_SECONDS = 30.0
WORKER_ALIVE_ENV = 'DATAMIND_UPLOAD_WORKER_ALIVE_SECONDS'
WORKER_HEARTBEAT_SECONDS = 5.0
WORKER_HEARTBEAT_ENV = 'DATAMIND_UPLOAD_WORKER_HEARTBEAT_SECONDS'
MIN_WORKER_ALIVE_SECONDS = 1.0
MAX_WORKER_ALIVE_SECONDS = 3600.0

#: Extension -> media type. `.dwg` is deliberately absent: it is reported with its own
#: "no converter" code instead of the generic unsupported one.
MEDIA_TYPES = {'.dxf': parse_contract.DXF_MEDIA_TYPE, '.json': 'application/json',
               '.pdf': 'application/pdf',
               '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
               '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
               '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}
ADAPTERS = {'.dxf': 'dxf-native', '.json': 'measurement-json', '.pdf': 'pdf-native',
            '.docx': 'docx-native', '.pptx': 'pptx-native', '.xlsx': 'xlsx-native'}
#: The vector index state of an upload. These are **index** facts, a different thing from
#: the derived `index_state` (retrievability) below:
#:   * `embedded` — the parse wave wrote this document's chunks and Qdrant points;
#:   * `failed`   — indexing was attempted and did not complete, so no index exists;
#:   * `deferred` — there was nothing to index (no text, no chunks).
VECTOR_INDEX = 'deferred'
VECTOR_INDEX_NOTE = ('上传管道在解析波次内建立切片与向量（ingestion/upload_index.py），'
                     '与知识库摄取共用同一发布顺序协议；未建立时如实报告 deferred/failed。')
#: The vector index state once the upload's chunks and vectors exist.
VECTOR_INDEX_EMBEDDED = 'embedded'
#: The vector index state when indexing was attempted and failed.
VECTOR_INDEX_FAILED = 'failed'
#: Derived searchability (R05). `index_state` is the frozen trichotomy; `index_note` is the
#: machine-readable reason a caller maps to human text (never shown raw in a UI).
INDEX_STATE_INDEXED = 'indexed'
INDEX_STATE_NO_CHUNKS = 'no_chunks'
INDEX_STATE_NOT_APPROVED = 'not_approved'
INDEX_NOTE_NOT_APPROVED = 'UPLOAD_NOT_APPROVED'
INDEX_NOTE_NO_CHUNKS = 'UPLOAD_HAS_NO_CHUNKS'
INDEX_NOTE_NO_CONTRACT_BLOCKS = 'UPLOAD_HAS_NO_CONTRACT_BLOCKS'
INDEX_NOTE_STATUS_NOT_INDEXED = 'UPLOAD_STATUS_NOT_INDEXED'
_UPLOAD_ID = re.compile(r'^upl-[a-f0-9]{40}$')


def fail(code, status=409, **context):
    detail = {'error_code': code}
    if context:
        detail['details'] = context
    raise HTTPException(status, detail=detail)


def wave_deadline():
    """The hard wall-clock budget of one poll-driven parse wave.

    `DATAMIND_UPLOAD_WAVE_DEADLINE_SECONDS` overrides it; a malformed or out-of-range
    override falls back to the default rather than silently becoming unbounded (which is
    exactly the failure mode this budget exists to prevent).
    """
    raw = os.environ.get(WAVE_DEADLINE_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_WAVE_DEADLINE_SECONDS
    try:
        return task_budget.validate_deadline(float(raw.strip()))
    except (TypeError, ValueError):
        return DEFAULT_WAVE_DEADLINE_SECONDS


def stale_running_seconds():
    """How long a `running` row must be idle before another poll may reclaim it.

    Strictly greater than the wave deadline: while a child is still inside its budget its
    row is not stale, so a second poll reports `running` instead of starting a rival parse.
    """
    return max(float(STALE_RUNNING_SECONDS), wave_deadline() + 30.0)


def _bounded_seconds(raw, default, minimum, maximum):
    """Parse one env override; a malformed or out-of-range value falls back to `default`."""
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if not minimum <= value <= maximum:
        return default
    return value


def worker_alive_seconds():
    """How recently a worker heartbeat must have been renewed to count as `alive`."""
    return _bounded_seconds(os.environ.get(WORKER_ALIVE_ENV), WORKER_ALIVE_SECONDS,
                            MIN_WORKER_ALIVE_SECONDS, MAX_WORKER_ALIVE_SECONDS)


def worker_heartbeat_seconds():
    """How often a running worker renews its heartbeat (never longer than the threshold)."""
    interval = _bounded_seconds(os.environ.get(WORKER_HEARTBEAT_ENV), WORKER_HEARTBEAT_SECONDS,
                                MIN_WORKER_ALIVE_SECONDS, MAX_WORKER_ALIVE_SECONDS)
    return min(interval, worker_alive_seconds())


def identities(digest):
    """Content-addressed identities: the same bytes are one upload, not two.

    `upload_id`, `job_id` and `document_id` are all derived from the staged SHA-256 only,
    so a repeated upload is idempotent (the queue is re-armed, the review gate reopens)
    and a `document_assets(collection, sha256)` unique violation is impossible.
    """
    token = hashlib.sha256(digest.encode()).hexdigest()[:40]
    return {'upload_id': 'upl-' + token, 'job_id': 'job-' + token,
            'document_id': 'doc-upload-' + token[:32]}


def job_id_for(upload_id):
    if not isinstance(upload_id, str) or not _UPLOAD_ID.fullmatch(upload_id):
        fail('UPLOAD_NOT_FOUND', 404)
    return 'job-' + upload_id[4:]


def target(filename):
    """Validate the requested name and return its suffix, before any byte is read."""
    if (not isinstance(filename, str) or not filename or len(filename) > 200
            or Path(filename).name != filename or '\\' in filename
            or any(ord(c) < 32 for c in filename)):
        fail('FILE_TYPE_OR_NAME_INVALID', 422)
    suffix = Path(filename).suffix.lower()
    if suffix == '.dwg':
        # This repository ships no DWG converter; the frozen interface pins the exact body.
        fail(DWG_ERROR_CODE, 415)
    if suffix not in MEDIA_TYPES:
        fail('UNSUPPORTED_MEDIA_TYPE', 415, suffix=suffix or None)
    return suffix


async def create(request, filename, provenance, dsn, identity):
    """One call: validate, stage, register the asset and enqueue the parse job."""
    target(filename)
    from datamind import file_uploads
    staged = await file_uploads.stage_upload(request, filename, provenance, dsn, identity)
    return enqueue(staged, provenance, dsn, identity)


ASSET_UPSERT = '''
    INSERT INTO document_assets(document_id,title,collection,file_type,origin,sha256,
        object_uri,page_count,language,acl,quality_score,quality_detail,parser_version,
        doc_version,status,needs_review,uploaded_at)
    VALUES (%s,%s,%s,%s,%s,%s,%s,NULL,NULL,%s,NULL,%s,NULL,1,'parsing',TRUE,clock_timestamp())
    ON CONFLICT (document_id) DO UPDATE SET title=EXCLUDED.title,collection=EXCLUDED.collection,
        file_type=EXCLUDED.file_type,origin=EXCLUDED.origin,sha256=EXCLUDED.sha256,
        object_uri=EXCLUDED.object_uri,quality_detail=EXCLUDED.quality_detail,
        parser_version=NULL,doc_version=1,status='parsing',needs_review=TRUE,
        uploaded_at=clock_timestamp()'''

JOB_UPSERT = '''
    INSERT INTO ingest_jobs(job_id,document_id,job_type,status,error,started_at,finished_at,created_at)
    VALUES (%s,%s,%s,'queued',NULL,NULL,NULL,clock_timestamp())
    ON CONFLICT (job_id) DO UPDATE SET document_id=EXCLUDED.document_id,
        status=CASE WHEN ingest_jobs.status='running' THEN 'running' ELSE 'queued' END,
        error=CASE WHEN ingest_jobs.status='running' THEN ingest_jobs.error ELSE NULL END,
        started_at=CASE WHEN ingest_jobs.status='running' THEN ingest_jobs.started_at ELSE NULL END,
        finished_at=CASE WHEN ingest_jobs.status='running' THEN ingest_jobs.finished_at ELSE NULL END,
        created_at=CASE WHEN ingest_jobs.status='running' THEN ingest_jobs.created_at
                        ELSE clock_timestamp() END
    RETURNING status'''


def enqueue(staged, provenance, dsn, identity):
    """Register one staged file and arm its parse job; returns the 201 payload.

    Re-uploading the same bytes is the same `job_id` (content-addressed), and the rule is
    explicit rather than an unconditional reset (R04):

    * the job is `running` — the current claim generation is **kept** (status, token,
      attempt, timestamps unchanged) and the response reports that current status, so a
      concurrent worker is never disowned by a duplicate upload;
    * the job is terminal (`succeeded`/`failed`/`partial_failure`/`cancelled`) or `queued` —
      it is re-armed to `queued` with `claim_token=NULL`; the *next* claim opens the next
      generation (`attempt+1` + a fresh token).
    """
    suffix = staged['suffix']
    media_type = MEDIA_TYPES[suffix]
    ids = identities(staged['source_sha256'])
    # `document_assets.object_uri` follows the repository-wide convention of an **absolute**
    # path (every other row, e.g. the knowledge base, stores an absolute path). The old
    # `/v1/files/uploads` contract keeps returning a repository-relative `source_path` for
    # `file_jobs`, which matches that endpoint's allow-list, so the absolute form is derived
    # here instead of changing that contract. A relative uri also broke the isolated
    # regression runner, whose fail-closed guard relocates rows by the `/data/` marker.
    # 绝对路径必须由**拥有暂存根的一方**计算（`file_uploads` 的 ROOT 可能被测试打桩）；
    # 用导入到本模块的 ROOT 去拼会在暂存根被重定向时算出错误路径。
    source_uri = staged.get('absolute_path') or str((ROOT / staged['source_path']).resolve())
    with psycopg2.connect(dsn) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        actor = lock_identity(cur, identity, 'admin')
        upload = {'upload_id': ids['upload_id'], 'job_id': ids['job_id'],
                  'filename': staged['name'], 'media_type': media_type,
                  'provenance': provenance, 'source_path': source_uri,
                  'source_sha256': staged['source_sha256'], 'bytes': staged['bytes'],
                  'submitted_by': {'user_id': actor['user_id'],
                                   'display_name': actor['display_name'],
                                   'roles': list(actor['roles'])},
                  'submitted_at': datetime.now(timezone.utc).isoformat()}
        cur.execute(ASSET_UPSERT, (ids['document_id'], staged['name'], COLLECTION,
                                   suffix.lstrip('.'), ORIGIN, staged['source_sha256'],
                                   source_uri, ['public'], Json({'upload': upload})))
        cur.execute(JOB_UPSERT, (ids['job_id'], ids['document_id'], QUEUE_JOB_TYPE))
        job_status = cur.fetchone()['status']
        if job_status != 'running':
            # The job is re-armed (or was already queued): retire the previous generation's
            # token. The attempt counter is history and is deliberately not reset.
            _clear_claim_token(cur, ids['job_id'])
    return {'upload_id': ids['upload_id'], 'job_id': ids['job_id'], 'filename': staged['name'],
            'media_type': media_type, 'status': job_status, 'review_required': True}


# --------------------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------------------

def _upload_meta(asset):
    stored = (asset.get('quality_detail') or {}).get('upload')
    if isinstance(stored, dict) and stored.get('upload_id'):
        return stored
    # A row whose metadata was lost is still answerable: the identity is deterministic.
    ids = identities(asset['sha256'])
    suffix = '.' + str(asset.get('file_type') or '').lower()
    return {'upload_id': ids['upload_id'], 'job_id': ids['job_id'],
            'filename': asset.get('title'), 'media_type': MEDIA_TYPES.get(suffix),
            'provenance': None, 'source_path': asset.get('object_uri'),
            'source_sha256': asset['sha256'], 'bytes': None}


def _counts(cur, document_id):
    """Validated locator rows per store; the two stores are disjoint by construction."""
    cur.execute('SELECT count(*) AS n FROM document_contract_blocks WHERE document_id=%s',
                (document_id,))
    blocks = cur.fetchone()['n']
    cur.execute('SELECT count(*) AS n FROM document_block_locators WHERE document_id=%s',
                (document_id,))
    page_shaped = cur.fetchone()['n']
    # The retriever (`datamind.knowledge`) reads `chunks` and nothing else, so this count is
    # what decides whether "approved" can honestly be called "searchable".
    cur.execute('SELECT count(*) AS n FROM chunks WHERE document_id=%s', (document_id,))
    chunks = cur.fetchone()['n']
    return {'contract_blocks': blocks, 'page_shaped': page_shaped, 'chunks': chunks,
            'block_count': blocks, 'locator_count': blocks + page_shaped}


def _grouped_counts(cur, table, document_ids):
    counts = {}
    if not document_ids:
        return counts
    cur.execute(f'SELECT document_id, count(*) AS n FROM {table} '
                'WHERE document_id=ANY(%s) GROUP BY document_id', (document_ids,))
    for row in cur.fetchall():
        counts[row['document_id']] = row['n']
    return counts


def _asset_status(asset):
    """The *asset* status behind one item, never the queue status.

    `listing` joins `ingest_jobs j` with `document_assets a` and returns `j.status` under the
    frozen `status` key, so the asset status travels under its own `asset_status` name; a
    plain `document_assets` row keeps reading its own `status`.
    """
    return asset.get('asset_status') or asset['status']


def searchability(status, needs_review, chunks, *, block_count=0):
    """The **one** derived searchability judgment: `(searchable, index_state, index_note)`.

    Every surface that reports retrievability — `GET /v1/uploads`, `GET /v1/uploads/{id}`,
    `POST /v1/uploads/{id}/approve` (via `_item`) and `GET /v1/documents` — calls this
    function, so the three fields cannot drift into two slightly different rules.

    Retrievability is three independent facts and they must not be conflated: the review
    gate is open (`NOT needs_review`), the asset is published (`status == 'indexed'`), and
    the chunk store the retriever actually reads (`datamind.knowledge` reads `chunks` only)
    is non-empty. `approve` clears only the first; because the parse wave now writes
    chunks and `approve` promotes the asset to `indexed` **only when chunks exist**, a
    document whose index failed is honestly reported as `no_chunks`, not as `indexed`.
    """
    approved = not needs_review
    chunks = int(chunks or 0)
    if approved and status == 'indexed' and chunks > 0:
        return True, INDEX_STATE_INDEXED, None
    if not approved:
        return False, INDEX_STATE_NOT_APPROVED, INDEX_NOTE_NOT_APPROVED
    if chunks > 0:
        # Chunks exist but the row was never published as retrievable. Unreachable through
        # these routes for an upload (approve sets `indexed` exactly when chunks exist), but
        # a hand-edited or imported row must still be reported, not silently called
        # searchable.
        return False, INDEX_STATE_NO_CHUNKS, INDEX_NOTE_STATUS_NOT_INDEXED
    note = (INDEX_NOTE_NO_CHUNKS if int(block_count or 0) > 0
            else INDEX_NOTE_NO_CONTRACT_BLOCKS)
    return False, INDEX_STATE_NO_CHUNKS, note


def _searchability(asset, counts):
    """`searchability` over one upload item's asset row and counted stores."""
    return searchability(_asset_status(asset), asset['needs_review'],
                         counts.get('chunks'), block_count=counts.get('block_count'))


def _item(job, asset, counts):
    upload = _upload_meta(asset)
    summary = (asset.get('quality_detail') or {}).get('parse_contract') or {}
    searchable, index_state, index_note = _searchability(asset, counts)
    index_report = _index_report(asset)
    return {'upload_id': upload['upload_id'], 'job_id': job['job_id'],
            'document_id': asset['document_id'], 'filename': upload.get('filename'),
            'media_type': upload.get('media_type'), 'provenance': upload.get('provenance'),
            'status': job['status'], 'created_at': job['created_at'],
            'review_state': 'approved' if not asset['needs_review'] else 'pending_review',
            'review_required': bool(asset['needs_review']),
            'searchable': searchable, 'index_state': index_state, 'index_note': index_note,
            'index_error_code': index_report.get('error_code'),
            'block_count': counts['block_count'], 'locator_count': counts['locator_count'],
            'unsupported_features': list(summary.get('unsupported_features') or [])}


def _analysis(job, asset):
    """The stored parse analysis, or an honest "not parsed yet" placeholder."""
    stored = (asset.get('quality_detail') or {}).get('analysis')
    if isinstance(stored, dict):
        return stored
    suffix = '.' + str(asset.get('file_type') or '').lower()
    return {'parse_status': job['status'], 'adapter': ADAPTERS.get(suffix), 'blocks': 0,
            'locators_written': 0, 'relations': 0, 'block_types': {},
            'unsupported_features': [], 'quality_findings': []}


def _detail(job, asset, counts):
    quality = asset.get('quality_detail') or {}
    item = _item(job, asset, counts)
    index_report = _index_report(asset)
    item.update({'source_sha256': asset['sha256'], 'parser_version': asset['parser_version'],
                 'page_count': asset['page_count'], 'collection': asset['collection'],
                 'analysis': _analysis(job, asset), 'error_code': job['error'],
                 'attempt': int(job.get('attempt') or 0),
                 'vector_index': _vector_index_state(asset, counts),
                 'vector_index_note': VECTOR_INDEX_NOTE,
                 'index_error_code': index_report.get('error_code'),
                 'chunk_count': counts['chunks'],
                 'wave_deadline_seconds': wave_deadline(),
                 'wave_timeouts': int(_upload_meta(asset).get('wave_timeouts') or 0),
                 'locator_stores': {'contract_blocks': counts['contract_blocks'],
                                    'page_shaped': counts['page_shaped']},
                 'contract_url': f"/v1/documents/{asset['document_id']}/contract",
                 'approval': quality.get('review_approval'),
                 'started_at': job['started_at'], 'finished_at': job['finished_at']})
    return item


def _index_report(asset):
    """The stored indexing outcome for this upload, or an empty report."""
    stored = (asset.get('quality_detail') or {}).get('index')
    return stored if isinstance(stored, dict) else {}


def _vector_index_state(asset, counts):
    """Whether this upload's chunks and vectors actually exist.

    The chunks row count is the fact, not a stored flag: a `chunks` row is what the
    retriever reads, and the vector is written in the same publishing step. When there is
    no chunk row the stored index report says *why*: an attempted-and-failed index is
    reported as `failed`, and only a document that had nothing to index is `deferred`.
    A failure must never be laundered into "nothing to do".
    """
    if counts['chunks'] > 0:
        return VECTOR_INDEX_EMBEDDED
    if _index_report(asset).get('error_code'):
        return VECTOR_INDEX_FAILED
    return VECTOR_INDEX


def _load(cur, upload_id, *, locked=False):
    job_id = job_id_for(upload_id)
    # The claim generation lives in its own table (migration 018); `COALESCE` keeps the
    # historical key present for a job that was never claimed.
    cur.execute('''SELECT j.*,COALESCE(c.attempt,0) AS attempt
        FROM ingest_jobs j LEFT JOIN ingest_job_claims c ON c.job_id=j.job_id
        WHERE j.job_id=%s''' + (' FOR UPDATE OF j' if locked else ''), (job_id,))
    job = cur.fetchone()
    if not job:
        fail('UPLOAD_NOT_FOUND', 404)
    cur.execute('SELECT * FROM document_assets WHERE document_id=%s'
                + (' FOR UPDATE' if locked else ''), (job['document_id'],))
    asset = cur.fetchone()
    if not asset:
        fail('UPLOAD_NOT_FOUND', 404)
    return job, asset


def detail(upload_id, dsn, *, with_worker=False):
    """One upload's truth. `with_worker` adds the same `worker` block the listing reports.

    A polling client reads only this endpoint, so without it the client cannot tell
    "nothing is processing this" from "the background processor is not running" — the exact
    dishonesty review 5.1 asks to avoid. It costs one aggregate over `task_leases` on the
    connection this read already holds.
    """
    with psycopg2.connect(dsn) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        conn.set_session(isolation_level='REPEATABLE READ')
        job, asset = _load(cur, upload_id)
        counts = _counts(cur, asset['document_id'])
        result = _detail(job, asset, counts)
        if with_worker:
            result['worker'] = _worker_heartbeat(cur)
        return result


def _timestamp(value):
    """Accept epoch seconds or ISO-8601 for `since`; anything else is a filter error."""
    text = str(value).strip()
    try:
        return datetime.fromtimestamp(float(text), timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        fail('UPLOAD_FILTER_INVALID', 422, filter='since')
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def listing(dsn, *, status=None, media_type=None, since=None, limit=50):
    if status is not None and status not in STATUSES:
        fail('UPLOAD_FILTER_INVALID', 422, filter='status')
    where, params = ['j.job_type=%s'], [QUEUE_JOB_TYPE]
    if status is not None:
        where.append('j.status=%s'); params.append(status)
    if media_type is not None:
        where.append("a.quality_detail->'upload'->>'media_type'=%s"); params.append(media_type)
    if since is not None:
        where.append('j.created_at>=%s'); params.append(_timestamp(since))
    params.append(int(limit))
    with psycopg2.connect(dsn) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        conn.set_session(isolation_level='REPEATABLE READ')
        cur.execute('''SELECT j.*, a.title,a.file_type,a.status AS asset_status,a.needs_review,
            a.quality_detail,a.sha256,a.parser_version,a.page_count,a.collection,a.object_uri
            FROM ingest_jobs j JOIN document_assets a USING(document_id)
            WHERE ''' + ' AND '.join(where) + ' ORDER BY j.created_at DESC LIMIT %s', tuple(params))
        rows = cur.fetchall()
        document_ids = [row['document_id'] for row in rows]
        blocks = _grouped_counts(cur, 'document_contract_blocks', document_ids)
        page_shaped = _grouped_counts(cur, 'document_block_locators', document_ids)
        chunk_rows = _grouped_counts(cur, 'chunks', document_ids)
        worker = _worker_heartbeat(cur)
    uploads = []
    for row in rows:
        counts = {'contract_blocks': blocks.get(row['document_id'], 0),
                  'page_shaped': page_shaped.get(row['document_id'], 0),
                  'chunks': chunk_rows.get(row['document_id'], 0),
                  'block_count': blocks.get(row['document_id'], 0)}
        counts['locator_count'] = counts['contract_blocks'] + counts['page_shaped']
        uploads.append(_item(row, row, counts))
    return {'uploads': uploads, 'count': len(uploads), 'worker': worker}


# --------------------------------------------------------------------------------------
# Durable queue and bounded worker
# --------------------------------------------------------------------------------------

def _new_claim_token():
    """A fresh, unforgeable generation token (never reused, never derived from the row)."""
    return secrets.token_urlsafe(32)


def _clear_claim_token(cur, job_id):
    """Retire the current generation's token, keeping `attempt` as immutable history."""
    cur.execute('UPDATE ingest_job_claims SET claim_token=NULL WHERE job_id=%s', (job_id,))


def _claim(cur, upload_id=None):
    """Take the next claimable job and stamp a new claim generation on it.

    The claim is atomic: `attempt` is incremented and `claim_token` replaced inside the
    same `FOR UPDATE SKIP LOCKED` transaction that selected the row, and the token is
    returned to the caller so every later write can be fenced on that exact generation.
    """
    job_id = job_id_for(upload_id) if upload_id else None
    cur.execute('''SELECT job_id,document_id FROM ingest_jobs
        WHERE job_type=%s AND (%s::text IS NULL OR job_id=%s)
          AND (status='queued' OR (status='running'
               AND started_at<=clock_timestamp()-%s*interval '1 second'))
        ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1''',
                (QUEUE_JOB_TYPE, job_id, job_id, stale_running_seconds()))
    row = cur.fetchone()
    if not row:
        return None
    token = _new_claim_token()
    cur.execute('''UPDATE ingest_jobs SET status='running',started_at=clock_timestamp(),error=NULL
        WHERE job_id=%s AND (status IN ('queued','running')) RETURNING job_id''', (row['job_id'],))
    if not cur.fetchone():
        return None
    cur.execute('''INSERT INTO ingest_job_claims(job_id,claim_token,attempt,claimed_at)
        VALUES (%s,%s,1,clock_timestamp())
        ON CONFLICT (job_id) DO UPDATE SET claim_token=EXCLUDED.claim_token,
            attempt=ingest_job_claims.attempt+1,claimed_at=clock_timestamp()
        RETURNING attempt''', (row['job_id'], token))
    attempt = cur.fetchone()['attempt']
    cur.execute('SELECT * FROM document_assets WHERE document_id=%s', (row['document_id'],))
    asset = cur.fetchone()
    if not asset:
        cur.execute('''UPDATE ingest_jobs SET status='failed',error=%s,finished_at=clock_timestamp()
            WHERE job_id=%s''', ('UPLOAD_ASSET_MISSING', row['job_id']))
        _clear_claim_token(cur, row['job_id'])
        return None
    upload = _upload_meta(asset)
    suffix = '.' + str(asset.get('file_type') or '').lower()
    return {'job_id': row['job_id'], 'document_id': asset['document_id'],
            'claim_token': token, 'attempt': attempt,
            'source_path': asset['object_uri'], 'source_sha256': asset['sha256'],
            'filename': upload.get('filename') or asset['title'], 'suffix': suffix,
            'media_type': upload.get('media_type') or MEDIA_TYPES.get(suffix),
            'provenance': upload.get('provenance'),
            'upload': {key: upload[key] for key in
                       ('upload_id', 'job_id', 'filename', 'media_type', 'provenance',
                        'source_path', 'source_sha256', 'bytes', 'submitted_by', 'submitted_at')
                       if key in upload}}


def _safe_code(value, fallback=FALLBACK_ERROR_CODE):
    if isinstance(value, str) and 0 < len(value) <= 96 and value.replace('_', '').isalnum():
        return value
    return fallback


def _lock_live_claim(cur, claim):
    """Lock the job row and return whether `claim` is still the current generation.

    `FOR UPDATE` holds both the job row and its claim row until this transaction commits,
    so a successful check cannot be invalidated by a concurrent re-claim between the check
    and the write that follows it. Any claim-dependent write must be preceded by this call
    and abandoned (committing nothing) when it returns False.
    """
    token = claim.get('claim_token')
    if not isinstance(token, str) or not token:
        return False
    cur.execute('''SELECT j.job_id FROM ingest_jobs j
        JOIN ingest_job_claims c ON c.job_id=j.job_id
        WHERE j.job_id=%s AND j.status='running' AND c.claim_token=%s
        FOR UPDATE OF j, c''', (claim['job_id'], token))
    return cur.fetchone() is not None


def _record_wave_timeout(dsn, claim):
    """Count how many waves this upload has burned; reads and writes the stored metadata.

    The counter lives in `document_assets.quality_detail.upload` (no new column, no new
    table), so it survives a reclaim and is reset naturally when the upload is re-queued.
    It is a claim-dependent write: a generation that already lost the row increments
    nothing and reports `None` instead of overwriting the newer generation's metadata.
    """
    with psycopg2.connect(dsn) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        if not _lock_live_claim(cur, claim):
            return None
        cur.execute('SELECT quality_detail FROM document_assets WHERE document_id=%s',
                    (claim['document_id'],))
        row = cur.fetchone()
        quality = dict((row or {}).get('quality_detail') or {})
        upload = dict(quality.get('upload') or {})
        attempts = int(upload.get('wave_timeouts') or 0) + 1
        upload['wave_timeouts'] = attempts
        quality['upload'] = upload
        cur.execute('UPDATE document_assets SET quality_detail=%s WHERE document_id=%s',
                    (Json(quality), claim['document_id']))
    return attempts


def _finish(dsn, claim, status, error_code=None):
    """Fence the terminal write on the claim generation that is still running.

    `status='running'` alone is not an identity (R04): a re-upload or a stale re-claim can
    leave an older worker holding a claim while the row is running for a newer one, so the
    same token check that guards the result write guards the terminal write. A mismatch
    writes nothing at all and returns `False`.
    """
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        if not _lock_live_claim(cur, claim):
            return False
        cur.execute('''UPDATE ingest_jobs SET status=%s,error=%s,finished_at=clock_timestamp()
            WHERE job_id=%s RETURNING job_id''', (status, error_code, claim['job_id']))
        if not cur.fetchone():  # unreachable while the row lock is held; never commit half.
            return False
        _clear_claim_token(cur, claim['job_id'])
        # `indexed` is the approved terminal state and is never downgraded by a late worker.
        cur.execute("UPDATE document_assets SET status=%s WHERE document_id=%s AND status<>'indexed'",
                    (ASSET_TERMINAL[status], claim['document_id']))
        return True


def _execute(dsn, claim, deadline_s=None, *, in_process=False):
    """Run one claimed parse under a hard wall-clock budget.

    The default is always the bounded child process (`task_budget.run_bounded`): the API
    process never parses an upload inline. `in_process=True` is an explicit opt-in for
    callers that already own a deadline (tests, or a worker that runs the parse in its own
    disposable process) and is never reachable without asking for it.
    """
    try:
        if in_process:
            outcome = {'status': 'ok', 'value': run_parse(claim, dsn)}
        else:
            if deadline_s is None:
                deadline_s = wave_deadline()
            outcome = task_budget.run_bounded(
                'ingestion.upload_pipeline', 'run_parse',
                [claim, task_budget.effective_dsn(dsn)], deadline_s=deadline_s,
                fallback_error_code=FALLBACK_ERROR_CODE, cwd=str(ROOT))
    except HTTPException as error:
        detail = error.detail if isinstance(error.detail, dict) else {}
        code = _safe_code(detail.get('error_code'))
        return {'outcome': 'failed' if _finish(dsn, claim, 'failed', code) else 'claim_lost',
                'error_code': code}
    except Exception:
        # Never return exception text, stack traces, paths or connection secrets.
        return {'outcome': 'failed' if _finish(dsn, claim, 'failed', FALLBACK_ERROR_CODE)
                else 'claim_lost', 'error_code': FALLBACK_ERROR_CODE}
    if outcome.get('status') == 'ok':
        value = outcome.get('value') or {}
        if value.get('claim_lost'):
            # `run_parse` completed the parse but found its generation already replaced and
            # committed nothing. There is nothing left to fence; report the loss.
            return {'outcome': 'claim_lost', 'error_code': CLAIM_LOST_ERROR_CODE,
                    'elapsed_ms': outcome.get('elapsed_ms')}
        status = value.get('status') if value.get('status') in TERMINAL else 'succeeded'
        if value.get('finalized'):
            # The result *and* the terminal state were committed by `run_parse` in one
            # token-checked transaction; a second terminal write here would be redundant.
            return {'outcome': status, 'analysis': value.get('analysis'),
                    'elapsed_ms': outcome.get('elapsed_ms')}
        saved = _finish(dsn, claim, status)
        return {'outcome': status if saved else 'claim_lost', 'analysis': value.get('analysis'),
                'elapsed_ms': outcome.get('elapsed_ms')}
    if outcome.get('status') == 'cancelled':
        return {'outcome': 'cancelled' if _finish(dsn, claim, 'cancelled') else 'claim_lost',
                'error_code': 'TASK_CANCELLED'}
    if outcome.get('status') == 'timeout':
        # The *wave* ran out of time, not the upload. The job stays `running` and claimable
        # (after `stale_running_seconds`) instead of being reported as a failed parse — but
        # only for a bounded number of waves, so a permanently oversized upload fails
        # honestly instead of being retried forever.
        attempts = _record_wave_timeout(dsn, claim)
        if attempts is None:
            return {'outcome': 'claim_lost', 'error_code': CLAIM_LOST_ERROR_CODE,
                    'deadline_seconds': outcome.get('deadline_seconds'),
                    'elapsed_ms': outcome.get('elapsed_ms')}
        if attempts >= MAX_WAVE_ATTEMPTS:
            saved = _finish(dsn, claim, 'failed', WAVE_EXHAUSTED_ERROR_CODE)
            return {'outcome': 'failed' if saved else 'claim_lost',
                    'error_code': WAVE_EXHAUSTED_ERROR_CODE, 'wave_timeouts': attempts,
                    'deadline_seconds': outcome.get('deadline_seconds')}
        return {'outcome': 'running', 'error_code': DEADLINE_ERROR_CODE,
                'wave_timeouts': attempts, 'deadline_seconds': outcome.get('deadline_seconds'),
                'elapsed_ms': outcome.get('elapsed_ms')}
    code = _safe_code(outcome.get('error_code'))
    return {'outcome': 'failed' if _finish(dsn, claim, 'failed', code) else 'claim_lost',
            'error_code': code, 'elapsed_ms': outcome.get('elapsed_ms')}


def work_once(dsn, upload_id=None, *, deadline_s=None, workers=1, in_process=False):
    """Claim and finish at most one bounded wave; never waits for unrelated workers.

    Without `in_process=True` the parse always runs in a bounded child process under
    `deadline_s` (defaulting to `wave_deadline()`), so this call cannot block indefinitely.
    """
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 4:
        raise ValueError('UPLOAD_WORKER_BOUND_INVALID')
    if deadline_s is not None:
        deadline_s = task_budget.validate_deadline(deadline_s)
    claims = []
    with psycopg2.connect(dsn) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        for _ in range(workers):
            claim = _claim(cur, upload_id)
            if not claim:
                break
            claims.append(claim)
    outcomes = []
    for claim in claims:
        outcome = _execute(dsn, claim, deadline_s, in_process=in_process)
        # The claim identity travels with its outcome so a worker can report *which*
        # generation finished (or was rejected as stale) without re-reading the queue.
        outcome.setdefault('job_id', claim['job_id'])
        outcome.setdefault('attempt', claim.get('attempt'))
        outcomes.append(outcome)
    return {'claimed': len(claims), 'outcomes': outcomes}


def drain(dsn, upload_id=None, *, workers=1, deadline_s=None, in_process=False):
    """Advance claimable uploads until none is left; a later call reclaims stale rows.

    This is the unattended path: an external worker (or a scheduled loop) calls `drain`
    instead of relying on a client to poll `GET /v1/uploads/{upload_id}`.
    """
    waves = 0; claimed = 0; outcomes = []
    while True:
        wave = work_once(dsn, upload_id, workers=workers, deadline_s=deadline_s,
                         in_process=in_process)
        if not wave['claimed']:
            break
        waves += 1; claimed += wave['claimed']; outcomes.extend(wave['outcomes'])
    return {'waves': waves, 'claimed': claimed, 'outcomes': outcomes}


# --------------------------------------------------------------------------------------
# Cross-process wave slots and worker heartbeat (review 5.1)
# --------------------------------------------------------------------------------------

def wave_slot(dsn, slots=None):
    """One cross-process advisory slot for a bounded wave (mirrors the OCR/formula pattern).

    `work_once(workers=N)` bounds a *single* call; it cannot bound N HTTP requests or N
    worker processes. Holding this slot for the duration of a wave — and processing that
    wave's claims sequentially — is what makes "at most `DATAMIND_TASK_SLOTS` parses run at
    once across every process" true. No slot is an immediate, explicit
    `TASK_BUDGET_BUSY` (503), never an unbounded queue.
    """
    return task_budget.resource_slot(dsn, UPLOAD_SLOT_NAME,
                                     slots or task_budget.configured_slots(UPLOAD_SLOTS))


def _worker_heartbeat(cur):
    """`worker_heartbeat` over an existing cursor (the list route shares its connection).

    `alive` counts only renewals inside the threshold; `last_seen_at` is the most recent
    renewal ever recorded, so a client can say "处理器未运行（最后心跳 …）" instead of
    silently dropping the fact that a worker existed.

    The probe is **advisory**: a database error is a real failure and propagates, but any
    other unreadable-heartbeat condition is reported as `alive=false` + `error` rather than
    taking down the upload listing or `/health` (the readiness signal the start scripts
    poll). It never guesses that a worker exists.
    """
    alive_seconds = worker_alive_seconds()
    try:
        cur.execute('''SELECT count(*) FILTER (WHERE renewed_at>=clock_timestamp()-%s*interval '1 second')
                AS alive_count, max(renewed_at) AS last_seen_at
            FROM task_leases WHERE name LIKE %s''',
                    (alive_seconds, HEARTBEAT_PREFIX + ':%'))
        row = cur.fetchone()
        last_seen = row['last_seen_at'] if row else None
        return {'alive': bool(row and row['alive_count']),
                'last_seen_at': (last_seen.astimezone(timezone.utc).isoformat()
                                 if last_seen else None),
                'alive_seconds': alive_seconds}
    except psycopg2.Error:
        raise
    except Exception:
        return {'alive': False, 'last_seen_at': None, 'alive_seconds': alive_seconds,
                'error': 'UPLOAD_WORKER_HEARTBEAT_UNREADABLE'}


def worker_heartbeat(dsn):
    """Whether any upload worker has renewed its heartbeat recently, and its last timestamp.

    `alive` is a *deployment* fact with an explicit threshold
    (`DATAMIND_UPLOAD_WORKER_ALIVE_SECONDS`, default 30s — the same value as the heartbeat
    lease TTL), not a guess: a crashed worker's lease stops being renewed and expires on its
    own, so a client can honestly say "后台处理器未运行" instead of rendering a `queued` job
    as "解析中".
    """
    with psycopg2.connect(dsn) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        return _worker_heartbeat(cur)


class WorkerHeartbeat:
    """Publish "a worker process is processing this queue" through `task_leases`.

    One row per worker process (`upload_worker:<host>:<pid>:<nonce>`) is acquired and
    renewed on a background thread, so a wave that runs for the whole `wave_deadline()`
    cannot let the heartbeat expire. `stop()` releases the lease, which makes `alive` flip
    to false immediately on a graceful shutdown; a *crashed* worker stops being alive when
    the lease TTL (== the alive threshold) elapses instead.
    """

    def __init__(self, dsn, *, holder=None, alive_seconds=None, interval=None):
        self.dsn = dsn
        self.alive_seconds = float(alive_seconds if alive_seconds is not None
                                   else worker_alive_seconds())
        self.interval = float(interval if interval is not None
                              else min(worker_heartbeat_seconds(), self.alive_seconds))
        # `:`/`.`/`-`/alnum only, matching `task_budget._LEASE_NAME`.
        nonce = secrets.token_hex(4)
        host = re.sub(r'[^A-Za-z0-9_.-]', '-', (holder or socket.gethostname()))[:64] or 'host'
        self.name = f'{HEARTBEAT_PREFIX}:{host}:{os.getpid()}:{nonce}'
        self.holder = f'upload_worker pid={os.getpid()} host={host}'
        self.token = None
        self._stopping = threading.Event()
        self._thread = None

    # -- lifecycle ---------------------------------------------------------------------
    def start(self):
        """Register the heartbeat; raises when the lease cannot be taken."""
        self._sweep_dead_stale()
        lease = task_budget.acquire_lease(self.dsn, self.name, ttl_seconds=self._ttl(),
                                         holder=self.holder)
        if not lease:
            raise RuntimeError('UPLOAD_WORKER_HEARTBEAT_UNAVAILABLE')
        self.token = lease['token']
        self._thread = threading.Thread(target=self._renew_loop, name='upload-heartbeat',
                                        daemon=True)
        self._thread.start()
        return lease

    def beat(self):
        """Renew now; a lost lease is re-acquired once. Returns True while alive."""
        if self.token is None:
            return False
        lease = task_budget.renew_lease(self.dsn, self.name, self.token, ttl_seconds=self._ttl())
        if lease:
            return True
        lease = task_budget.acquire_lease(self.dsn, self.name, ttl_seconds=self._ttl(),
                                         holder=self.holder)
        if not lease:
            return False
        self.token = lease['token']
        return True

    def stop(self):
        """Stop renewing and release the lease so `alive` becomes false at once."""
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval + 1.0))
            self._thread = None
        if self.token is not None:
            with contextlib.suppress(Exception):
                task_budget.release_lease(self.dsn, self.name, self.token)
            self.token = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc_info):
        self.stop()
        return False

    # -- internals ---------------------------------------------------------------------
    def _ttl(self):
        """Lease TTL in whole seconds; never shorter than 1s (`task_leases` takes ints)."""
        return max(1, min(int(round(self.alive_seconds)), int(MAX_WORKER_ALIVE_SECONDS)))

    def _renew_loop(self):
        while not self._stopping.wait(self.interval):
            with contextlib.suppress(Exception):
                self.beat()

    def _sweep_dead_stale(self):
        """Drop this subsystem's expired heartbeat rows; never touches other namespaces."""
        try:
            with psycopg2.connect(self.dsn) as conn, conn.cursor() as cur:
                cur.execute('DELETE FROM task_leases WHERE name LIKE %s '
                            'AND expires_at<=clock_timestamp()',
                            (HEARTBEAT_PREFIX + ':%',))
        except psycopg2.Error:
            pass


def retry(upload_id, dsn, identity):
    """Re-arm a failed/cancelled upload; succeeded uploads are never re-queued.

    Re-arming opens the *next* claim generation: the row goes back to `queued` and the
    previous `claim_token` is cleared, so a straggler still holding the failed generation
    can never match the new one.
    """
    job_id = job_id_for(upload_id)
    with psycopg2.connect(dsn) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        lock_identity(cur, identity, 'admin')
        job, asset = _load(cur, upload_id, locked=True)
        if job['status'] in ('queued', 'running', 'succeeded', 'partial_failure'):
            fail('UPLOAD_NOT_RETRYABLE', 409, job_status=job['status'])
        cur.execute('''UPDATE ingest_jobs SET status='queued',error=NULL,started_at=NULL,
            finished_at=NULL,created_at=clock_timestamp() WHERE job_id=%s''', (job_id,))
        _clear_claim_token(cur, job_id)
        cur.execute("UPDATE document_assets SET status='parsing' WHERE document_id=%s AND status<>'indexed'",
                    (asset['document_id'],))
        return _detail({**job, 'status': 'queued', 'error': None, 'started_at': None,
                        'finished_at': None}, asset, _counts(cur, asset['document_id']))


def cancel(upload_id, dsn, identity):
    """Cooperative cancellation of a not-yet-terminal upload.

    Cancelling also clears `claim_token`: the running claim keeps parsing, but its result
    and terminal write are fenced out by the token check and commit nothing.
    """
    with psycopg2.connect(dsn) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        lock_identity(cur, identity, 'admin')
        job, asset = _load(cur, upload_id, locked=True)
        if job['status'] in TERMINAL:
            fail('UPLOAD_NOT_CANCELLABLE', 409, job_status=job['status'])
        cur.execute("UPDATE ingest_jobs SET status='cancelled',finished_at=clock_timestamp() "
                    'WHERE job_id=%s', (job['job_id'],))
        _clear_claim_token(cur, job['job_id'])
        cur.execute("UPDATE document_assets SET status='pending' WHERE document_id=%s AND status<>'indexed'",
                    (asset['document_id'],))
        return _detail({**job, 'status': 'cancelled'}, asset, _counts(cur, asset['document_id']))


def approve(upload_id, dsn, identity, *, reason=None):
    """Clear the review gate, and publish the asset only if it is really retrievable.

    The operator is recorded in `quality_detail.review_approval`. `status` becomes `indexed`
    **only when the retriever's own store is non-empty** (`chunks`; see
    `datamind.knowledge.KnowledgeRetriever`, which reads `chunks` and nothing else). An
    approved parse with contract blocks but no chunks keeps its non-`indexed` status and the
    response says so through `index_state='no_chunks'`/`index_note`; contract blocks are
    evidence, not an index. The call stays idempotent — repeating it only re-records the
    operator and never produces a second state change.
    """
    with psycopg2.connect(dsn) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        actor = lock_identity(cur, identity, 'admin')
        job, asset = _load(cur, upload_id, locked=True)
        if job['status'] not in ('succeeded', 'partial_failure'):
            fail('UPLOAD_NOT_REVIEWABLE', 409, job_status=job['status'])
        counts = _counts(cur, asset['document_id'])
        searchable = counts['chunks'] > 0
        quality = dict(asset.get('quality_detail') or {})
        quality['review_approval'] = {
            'user_id': actor['user_id'], 'display_name': actor['display_name'],
            'roles': list(actor['roles']), 'approved_at': datetime.now(timezone.utc).isoformat(),
            'reason': reason, 'indexed': searchable, 'block_count': counts['block_count'],
            'chunk_count': counts['chunks']}
        status = 'indexed' if searchable else asset['status']
        cur.execute('''UPDATE document_assets SET needs_review=FALSE,status=%s,quality_detail=%s
            WHERE document_id=%s''',
                    (status, Json(quality), asset['document_id']))
        result = _detail(job, {**asset, 'needs_review': False, 'status': status,
                               'quality_detail': quality}, counts)
    result['indexed'] = result['searchable']
    return result


# --------------------------------------------------------------------------------------
# Parse routing and persistence
# --------------------------------------------------------------------------------------

def _staged_path(claim):
    """Resolve the staged source, accepting both uri forms the repository uses.

    `document_assets.object_uri` follows the repository-wide convention of an absolute
    path, while the legacy `/v1/files/uploads` contract hands `file_jobs` a
    repository-relative one. Both must resolve to a file inside the checkout; the
    containment check below is what actually keeps this safe, so the form is normalised
    rather than trusted.
    """
    raw = claim.get('source_path') or ''
    path = (Path(raw) if Path(raw).is_absolute() else (ROOT / raw)).resolve()
    if (not path.is_relative_to(ROOT.resolve()) or not path.is_file()
            or path.suffix.lower() != claim['suffix']):
        fail('UPLOAD_STAGED_SOURCE_INVALID', 409)
    if hashlib.sha256(path.read_bytes()).hexdigest() != claim['source_sha256']:
        fail('UPLOAD_STAGED_SOURCE_CHANGED', 409)
    return path


def _parse_dxf(path, claim):
    from . import cad_adapter
    try:
        result = cad_adapter.parse(path, document_id=claim['document_id'],
                                   source_version=claim['source_sha256'],
                                   provenance=claim['provenance'])
    except HTTPException as error:
        detail = error.detail if isinstance(error.detail, dict) else {}
        # The adapter may not accept `provenance` after a future profile revision; the
        # documented entry point stays `parse(path, document_id=, source_version=)`.
        if (detail.get('details') or {}).get('reason') != 'handler_signature':
            raise
        result = cad_adapter.parse(path, document_id=claim['document_id'],
                                   source_version=claim['source_sha256'])
    payload = result.payload
    layers = sorted({block['locator'].get('layer') for block in payload['blocks']
                     if isinstance(block.get('locator'), dict)
                     and block['locator'].get('locator_kind') == 'cad_entity'
                     and block['locator'].get('layer')})
    entities = sum(1 for block in payload['blocks'] if block['block_type'] != 'dimension')
    dimensions = sum(1 for block in payload['blocks'] if block['block_type'] == 'dimension')
    return result, {'layers': len(layers), 'layer_names': layers, 'entities': entities,
                    'dimensions': dimensions}


def _parse_office(path, claim):
    from . import office
    result = office.parse(path, document_id=claim['document_id'],
                          source_version=claim['source_sha256'])
    return result, {}


def _parse_pdf(path, claim):
    from . import ingest
    blocks, page_count, detail = ingest.parse_pdf(path, claim['document_id'], include_quality=True)
    import pymupdf
    document = pymupdf.open(path)
    try:
        page_sizes = {index + 1: (float(page.rect.width), float(page.rect.height))
                      for index, page in enumerate(document)}
    finally:
        document.close()
    ingest._ensure_pdf_adapter()
    result = parse_contract.dispatch('application/pdf', pdf_path=path,
                                     doc_id=claim['document_id'], blocks=blocks,
                                     page_sizes=page_sizes, detail=detail,
                                     parser_version=ingest.PARSER_VERSION)
    return result, {'pages': page_count}


def _measurement_id(data):
    """Read the measurement identity the JSON declares; the parser re-validates it."""
    try:
        raw = json.loads(data.decode('utf-8'))
        first = raw['measurements'][0]
        measurement_id, version = first['measurement_id'], raw['record_version']
        if not isinstance(measurement_id, str) or isinstance(version, bool) \
                or not isinstance(version, int):
            raise ValueError('identity')
        return measurement_id, version
    except (ValueError, TypeError, KeyError, IndexError, UnicodeDecodeError,
            json.JSONDecodeError):
        fail('UPLOAD_MEASUREMENT_SOURCE_INVALID', 422)


def _parse_json(path, claim):
    """Measurement JSON: the existing parser, with the honest no-locator gap declared.

    `cad_dimensions.measurement_from_bytes` is the parser behind the existing
    `file_jobs` measurement target. A measurement record has no page/region geometry, so
    the contract cannot carry a block-level locator for it; rather than inventing one,
    the result declares `unsupported_features=['locator_unavailable']` exactly as the
    OHR-Bench JSON branch of `ingestion/ingest.py` does. The record itself is validated,
    not merely decoded.
    """
    from datamind import cad_dimensions
    data = path.read_bytes()
    measurement_id, version = _measurement_id(data)
    record = cad_dimensions.measurement_from_bytes(data, measurement_id, version,
                                                   claim['provenance'])
    payload = {
        'classification': {'detected_media_type': MEDIA_TYPES['.json'],
                           'source_name': claim['filename']},
        'adapter_capabilities': ['measurement_json'],
        'blocks': [], 'relations': [], 'parsed_scope': [],
        'failed_scope': [{'kind': 'object', 'ref': claim['document_id'],
                          'status': 'unsupported', 'detail': 'locator_unavailable'}],
        'quality_findings': [], 'unsupported_features': ['locator_unavailable'],
        'artifact_refs': [],
        'manifest': {'parser_version': 'cad-measurement-adapter-v1',
                     'adapter': ADAPTERS['.json'], 'measurement_id': measurement_id,
                     'record_version': record['source']['record_version']},
        'parse_status': 'unsupported'}
    return parse_contract.validate_parse_result(payload), {
        'measurement_id': measurement_id, 'record_version': record['source']['record_version'],
        'measurements': len(record.get('measurements') or [])}


PARSERS = {'.dxf': _parse_dxf, '.json': _parse_json, '.pdf': _parse_pdf,
           '.docx': _parse_office, '.pptx': _parse_office, '.xlsx': _parse_office}


def run_parse(claim, dsn):
    """Parse one claimed upload and persist its validated contract result.

    Parser libraries are third-party and some of them print banners (`pymupdf` announces
    `pymupdf_layout` on stdout the first time it runs layout analysis). In the bounded
    child the runner writes its one JSON result to stdout, so any library chatter would
    corrupt that channel and be reported as `TASK_RESULT_INVALID` even though the parse and
    the contract write succeeded. The parse therefore runs with stdout redirected to
    stderr: diagnostics survive, the result channel stays exactly one JSON object.

    **Claim generation (R04).** The parse itself runs outside any transaction (it can take
    the whole wave budget). Only afterwards — in **one** transaction — is the claim locked
    and re-checked (`SELECT ... FOR UPDATE` on `claim_token`), and then the contract result,
    `quality_detail` and the terminal job/asset status are written together. A claim that
    was superseded while the parse ran commits nothing at all: no blocks, no locators, no
    quality detail, no terminal state — it returns `claim_lost` instead.
    """
    path = _staged_path(claim)
    with contextlib.redirect_stdout(sys.stderr):
        result, extra = PARSERS[claim['suffix']](path, claim)
    payload = result.payload
    parse_status = payload['parse_status']
    terminal = ('succeeded' if parse_status == 'parsed'
                else 'failed' if parse_status == 'failed' else 'partial_failure')
    with psycopg2.connect(dsn) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        if not _lock_live_claim(cur, claim):
            # Nothing has been written yet in this transaction; returning without writing
            # leaves the newer generation's rows exactly as they were.
            return {'status': terminal, 'finalized': False, 'claim_lost': True,
                    'parse_status': parse_status, 'analysis': None, 'blocks': 0,
                    'locators': 0, 'relations': 0}
        stored = contract_store.persist_result(cur, document_id=claim['document_id'],
                                               payload=payload, dsn=dsn)
        analysis = _analysis_of(claim, payload, stored, extra)
        summary = _summary_of(payload, stored, analysis)
        quality = {'upload': claim['upload'], 'parse_contract': summary, 'analysis': analysis}
        cur.execute('''UPDATE document_assets SET quality_detail=%s,parser_version=%s,page_count=%s
            WHERE document_id=%s''',
                    (Json(quality), payload['manifest'].get('parser_version'),
                     extra.get('pages'), claim['document_id']))
        cur.execute('''UPDATE ingest_jobs SET status=%s,error=NULL,finished_at=clock_timestamp()
            WHERE job_id=%s RETURNING job_id''', (terminal, claim['job_id']))
        if not cur.fetchone():
            # Unreachable while the row lock is held; abort the transaction rather than
            # commit contract rows without their terminal state.
            conn.rollback()
            return {'status': terminal, 'finalized': False, 'claim_lost': True,
                    'parse_status': parse_status, 'analysis': None, 'blocks': 0,
                    'locators': 0, 'relations': 0}
        _clear_claim_token(cur, claim['job_id'])
        # `indexed` is the approved terminal state and is never downgraded by a late worker.
        cur.execute("UPDATE document_assets SET status=%s WHERE document_id=%s AND status<>'indexed'",
                    (ASSET_TERMINAL[terminal], claim['document_id']))
    # --- Indexing runs **after** the parse transaction has committed. ---
    #
    # Indexing writes to Qdrant, which cannot share a PostgreSQL transaction, and it does
    # network I/O; holding the claim's row lock across it would block every other writer on
    # the same job for the duration of an embedding call.
    #
    # A failed index does **not** change the parse result. The parse really did succeed,
    # and the contract blocks really were written. Reporting it as a failed parse would
    # misstate what happened; the failure is carried separately as `index_error_code`, and
    # `searchability()` keeps saying `no_chunks` — the truth about retrievability.
    index_report = _index_parsed_document(dsn, claim['document_id'], claim=claim)
    return {'status': terminal, 'finalized': True, 'claim_lost': False,
            'parse_status': parse_status, 'analysis': analysis, 'blocks': stored['blocks'],
            'locators': stored['locators'], 'relations': stored['relations'],
            'index_state': index_report.get('index_state'),
            'index_error_code': index_report.get('error_code'),
            'chunk_count': index_report.get('chunk_count', 0)}


def _index_parsed_document(dsn, document_id, *, claim=None):
    """Index a freshly parsed document and record the outcome on the asset.

    The report is written into `quality_detail.index` so the upload surfaces can state
    what happened to the index without re-deriving it, and so a failure is visible instead
    of silently leaving the document unretrievable.

    `claim` fences the index write on the claim generation that owns the document (see
    `upload_index.index_document`). A superseded generation reports `superseded` and
    writes neither vectors nor chunks — the newer generation owns the document's index.
    """
    from . import upload_index
    report = upload_index.index_document(dsn, document_id, claim=claim)
    if report.get('index_state') == upload_index.INDEX_SUPERSEDED:
        # The newer generation owns this document's index; recording a report here would
        # overwrite the newer generation's own report with a stale one.
        return report
    try:
        with psycopg2.connect(dsn) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT quality_detail FROM document_assets WHERE document_id=%s',
                        (document_id,))
            row = cur.fetchone()
            quality = dict((row or {}).get('quality_detail') or {})
            quality['index'] = {'index_state': report.get('index_state'),
                                'chunk_count': report.get('chunk_count', 0),
                                'error_code': report.get('error_code'),
                                'vector_index': upload_index.COLLECTION}
            cur.execute('UPDATE document_assets SET quality_detail=%s WHERE document_id=%s',
                        (Json(quality), document_id))
    except Exception:
        # The report is a convenience; failing to store it must not fail the parse.
        pass
    return report


def _analysis_of(claim, payload, stored, extra):
    block_types = {}
    for block in payload['blocks']:
        block_types[block['block_type']] = block_types.get(block['block_type'], 0) + 1
    analysis = {'parse_status': payload['parse_status'],
                'adapter': payload['manifest'].get('adapter') or ADAPTERS.get(claim['suffix']),
                'blocks': stored['blocks'], 'locators_written': stored['locators'],
                'relations': stored['relations'], 'block_types': block_types,
                'unsupported_features': list(payload['unsupported_features']),
                'quality_findings': payload['quality_findings'],
                'contract_version': payload['contract_version']}
    analysis.update(extra)
    return analysis


def _summary_of(payload, stored, analysis):
    summary = {'contract_version': payload['contract_version'], 'validation': 'parsed',
               'adapter': analysis['adapter'], 'parse_status': payload['parse_status'],
               'blocks': stored['blocks'], 'locators_written': stored['locators'],
               'relations': stored['relations'],
               'omitted_columns': stored.get('omitted_columns') or []}
    if payload['unsupported_features']:
        # An explicit gap declaration travels with the summary instead of being hidden.
        summary['unsupported_features'] = list(payload['unsupported_features'])
    return summary
