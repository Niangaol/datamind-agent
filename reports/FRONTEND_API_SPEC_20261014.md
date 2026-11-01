# DataMind Agent — Frontend Workbench & HTTP API Surface Spec

Base: `/Users/caicai/DataMind Agent/`. Every claim carries file + line. Verified by importing the real app
(`.venv/bin/python -c "import datamind.api"`) and enumerating `app.routes` — **92 HTTP routes + 4 FastAPI docs routes + 1 static mount**.

---

# PART 1 — Backend API surface

## 1.0 Shape of the backend

| Fact | Evidence |
| --- | --- |
| Single FastAPI app, **no `APIRouter` sub-files** (grep for `APIRouter(` across `datamind/`, `ingestion/`, `scripts/` → 0 hits) | `datamind/api.py:221` `app = FastAPI(title="DataMind Agent", version="0.39.0")` |
| Service modules are **not** routers; request models are imported from them | `datamind/api.py:25-27`, `datamind/api.py:623`, `datamind/api.py:693` |
| Vue build served at `/` | `datamind/api.py:840-844` (`VUE_DIR = web/vue`, `app.mount("/assets/vue", StaticFiles(...))`), `datamind/api.py:847-854` |
| Auth is a **default-deny** HTTP middleware (not per-route deps) | `datamind/api.py:307-334` `identity_permissions` |
| Identity/role/CSRF/origin implemented in | `datamind/auth.py` — `COOKIE='datamind_session'` (:15), `SESSION_SECONDS=8*60*60` (:16), `ROLES={'viewer','reviewer','approver','admin'}` (:17), `origin_check` (:128-142), `login` (:145-174), `current` (:177-199) |

### Anonymous surface (whitelist)

| Set | Contents | Evidence |
| --- | --- | --- |
| `PUBLIC_PATHS` | `/`, `/health`, `/v1/auth/login`, `/v1/auth/me` | `datamind/api.py:232` |
| `PUBLIC_READ_PREFIXES` (GET/HEAD/OPTIONS only) | `/v1/documents`, `/v1/runs`, `/assets/` | `datamind/api.py:235` |
| `SENSITIVE_READ_PREFIXES` (identity + **origin check** even on GET) | `/v1/model/profiles`, `/v1/model/config` | `datamind/api.py:238` |
| `PUBLIC_WRITE_PATHS` (only when `DATAMIND_PUBLIC_CHAT=1`) | `/v1/chat`, `/v1/chat/stream` → origin check only, no CSRF | `datamind/api.py:246`, `:263-269`, `:324-331` |
| `_NO_STORE_PREFIXES` (forces `Cache-Control: no-store`) | `/v1/pdf-snapshots`, `/v1/pdf-knowledge`, `/v1/drawings`, `/v1/dimensions`, `/v1/files`, `/v1/uploads`, `/v1/plans`, `/v1/model/profiles`, `/v1/model/config`, `/v1/sessions`, `/` | `datamind/api.py:247-260`, `:281-282`, `:333` |

**Default behaviour** (no env var set): every route not listed above requires an identity; every non-GET also requires
`X-CSRF-Token` + a same-origin `Origin` (`datamind/api.py:313-323`, `datamind/auth.py:196-198`).
Error codes: `401 LOGIN_REQUIRED` / `401 SESSION_INVALID` / `403 PERMISSION_DENIED` / `403 CSRF_REQUIRED` /
`403 ORIGIN_NOT_ALLOWED` (`datamind/auth.py:91-92`, `:137-142`, `:182`, `:191`, `:195`, `:198`).

**Role resolution** is a single chain, evaluated top-to-bottom, first match wins: `_required_permission(request)`
(`datamind/api.py:285-304`). `admin` passes every gate (`datamind/auth.py:195`). Routes returning `None` = "any
authenticated identity, no role gate".

**Local demo identity**: a loopback peer (`127.0.0.1`/`::1`/`localhost`) acts as a provisioned `admin`
(`本机管理员`) with no password (`datamind/auth.py:19-26`, `:59-69`, `:81-88`); overridable with
`DATAMIND_LOCAL_NO_LOGIN` (`:60-63`).

### Request-model catalogue (Pydantic)

| Model | Fields (+ constraints) | Defined |
| --- | --- | --- |
| `DocumentReference` | `document_id` 1-64 `^[A-Za-z0-9][A-Za-z0-9_.-]*$`, `document_version` ≥1, `source_sha256` 64-hex, `parser_version` 1-64; `extra=forbid, strict` | `api.py:40-45` |
| `Scope` | `database_sources: list[str]\|None`, `document_collections: list[str]\|None`, `document_refs: list[DocumentReference]\|None` (1-8); `extra=forbid` | `api.py:48-52` |
| `ChatRequest` | `question` 1-2000, `scope: Scope\|None`, `mode: 'auto'\|'sql'\|'rag'\|'industrial'='auto'`, `history: Any=None`, `conversation_id: str\|None` ≤64 `^sess-[0-9a-f]{24}$` | `api.py:55-70` |
| `FactReviewRequest` | `request_id` `^[A-Za-z0-9_-]+$`, `document_version`≥1, `source_sha256`, `parser_version`, `payload_sha256`, `previous_review_id?`, `decision: confirmed\|rejected\|needs_review`, `reviewer_label?` 1-120, `reason` 1-1000 | `api.py:100-110` |
| `FactApprovalRequest` | same prefix + `review_id`, `previous_approval_id?`, `decision: approved\|rejected\|revoked`, `reason` | `api.py:113-123` |
| `LoginRequest` | `username` 1-64, `password` 1-256 | `api.py:126-128` |
| `DrawingRegistrationRequest` | `request_id`, `document_id`, `title` 1-200, `source_path` 1-500, `source_sha256`, `provenance: synthetic_demo\|authorized_external`, `previous_version`≥0, `previous_source_sha256?` | `api.py:131-140` |
| `AuthorityBasis` / `AuthorityRequest` | `{document_id, document_version≥1, source_sha256, page_no≥1}`; +`request_id, question, measurement_id, candidate_sha256, selected_source: production\|report, valid_from, valid_to, basis, rationale` 10-1000 | `api.py:143-175` |
| `AuthorityEventRequest` | `request_id`, `previous_event_id?`, `decision: approved\|rejected\|revoked`, `reason` | `api.py:178-183` |
| `DrawingAssociationReviewRequest` | `request_id, document_version, source_sha256, parser_version, bundle_sha256, feature_sha256, previous_review_id?, decision, selected_target? ^[1-9A-F][0-9A-F]*$, reason` | `api.py:151-162` |
| `ProfileRequest` | `id, profile_id, name≤120, protocol≤24, base_url≤500, model≤200, api_key≤500, max_tokens 0-200000, timeout_s 0-600, auth_style, headers, proxy≤300, note≤500, activate` — all optional | `api.py:186-201` |
| `ProfileImportRequest` | `profiles: list[dict]`, `activate_first: bool=False` (import caps at 50) | `api.py:204-206`, `:1011` |
| `SessionCreateRequest` / `SessionRenameRequest` | `title?` ≤200 / `title` 1-200; `extra=forbid` | `api.py:209-218` |
| `PdfSnapshotRegistrationRequest` | `request_id, document_id, title, job_id, attempt_token, manifest_sha256, previous_version 0..2147483646, previous_source_sha256?`; `extra=forbid, strict, frozen` | `datamind/pdf_publications.py:35-44` |
| `PdfKnowledgePublicationRequest` | `request_id, document_id, document_version≥1, expected_manifest_sha256, previous_publication_id? ^pdf-pub-[a-f0-9]{32}$` | `datamind/pdf_index.py:35-41` |
| `PdfKnowledgeSearchRequest` | `question` 1-2000 (UTF-8 ≤8000 bytes), `publications: list[PublicationReference]` 1-4, `limit` 4-12 = 8; duplicate publication ids rejected | `datamind/pdf_search.py:37-60`, refs `:31-34` |
| `FileJobRequest` | `request_id`, `max_concurrency` 1-4 = 2, `items: list[FileJobItem]` 1-20; `FileJobItem = {item_id, type: drawing\|measurement, payload}` | `datamind/file_jobs.py:66-75`, `:54-63` |
| `MeasurementRegistration` | `request_id, measurement_id, source_path, source_sha256, provenance, previous_version≥0, previous_sha256?` | `datamind/cad_dimensions.py:31-38` |
| `BindingRegistration` | `request_id, binding_id, previous_version, previous_sha256?, document_id, document_version, source_sha256, bundle_sha256, feature_id, feature_sha256, target_handle ^[1-9A-F][0-9A-F]*$, measurement_id, measurement_record_version, measurement_sha256, review_id, review_sha256, valid_from, valid_to, rationale` | `datamind/cad_dimensions.py:41-60` |
| `ApprovalRequest` / `ComparisonRequest` | `request_id, binding_version, binding_sha256, previous_approval_id?, decision: approved\|rejected\|revoked, reason` / `request_id, binding_version, binding_sha256` | `datamind/cad_dimensions.py:63-75` |

## 1.1 Auth & identity

| Method | Path | Purpose | Request | Response (top-level keys) | Auth / role | CSRF | SSE |
| --- | --- | --- | --- | --- | --- | --- | --- |
| GET | `/v1/auth/me` | Current identity + CSRF token; **never provisions** the demo row (`provision_demo=False`) | — | `{user: {user_id,username,display_name,roles}\|null, csrf_token: str\|null}`; strips `csrf_token`/`_session_hash`/`_demo` | **anon** (`PUBLIC_PATHS`) | no | no |
| POST | `/v1/auth/login` | Password login; scrypt + 10-failure/15-min limit per client **and** per account (`429 LOGIN_RATE_LIMITED`, `401 LOGIN_FAILED`) | `LoginRequest` | `{user, csrf_token}` + `Set-Cookie: datamind_session` (httponly, samesite=strict, 8 h) | **anon** | no (origin check inside `login`) | no |
| POST | `/v1/auth/logout` | Revoke the session row + delete cookie | — | `{status:'signed_out'}` | any | yes | no |

Evidence: `api.py:776-802`, `auth.py:145-174`, `:221-228`.

## 1.2 Chat / question answering (ask & query)

| Method | Path | Purpose | Request | Response | Auth / role | CSRF | SSE |
| --- | --- | --- | --- | --- | --- | --- | --- |
| POST | `/v1/chat` | One synchronous run: question → SQL / RAG / industrial → verified answer + evidence | `ChatRequest` | `FinalResult` + clarification projection: `run_id, status, answer, confidence_level, error_code, turns_used, sql_result?, rag_result?, industrial_result?, cross_source_result?`, plus on `NEEDS_CLARIFICATION`: `clarification_missing_slots/candidates/slot_questions/contract_version` | any (anon only if `DATAMIND_PUBLIC_CHAT=1`) | yes (unless public-chat) | no |
| POST | `/v1/chat/stream` | **The streaming twin of `/v1/chat`**; identical validation + identical agent kwargs | `ChatRequest` | `text/event-stream`; frames `event: trace` + `data: {TraceEvent JSON}`, heartbeat `: heartbeat` every 15 s; terminal event types `answer.completed` / `answer.abstained` / `answer.clarification` / `run.failed` carry the full final payload in `output_summary` | any (anon only if `DATAMIND_PUBLIC_CHAT=1`) | yes (unless public-chat) | **YES** |
| POST | `/v1/diagnostics/sql` | Fixed browser→Agent→Guard→readonly-DB self-test with a **stubbed generator** (no model call) | — | `FinalResult` for `2024年一共有多少笔订单？` on `demo_retail` | any | yes | no |

Evidence: `api.py:1132-1140`, `:1157-1194` (worker thread + `queue.Queue` sink, `threading.Thread(..., daemon=True)`), `:1182-1194` (`yield ": heartbeat\n\n"`, `media_type="text/event-stream"`, `Cache-Control: no-cache`, `X-Accel-Buffering: no`), `:1143-1154`, `_chat_kwargs` `:73-97`, terminal payload `agent.py:161-174`, clarification projection `api.py:1113-1129`.

**Multi-turn `history` contract (backend side, validates but is client-supplied):** `api.py:59-65` (docstring), validated
by `normalize_history` (`api.py:86`, `sql_engine.py:100-106`) whose limits live at `sql_engine.py:52-56`
(`HISTORY_MAX_TURNS=20`, entry 2000 chars, total 8000 chars, roles `user|assistant`) with stable codes
`HISTORY_INVALID` / `HISTORY_TOO_LONG` / `HISTORY_ENTRY_TOO_LONG` → **422** (`api.py:87-88`, `sql_engine.py:68-93`).
The server keeps **no** session state; `history` is added to the agent kwargs **only when non-empty**, so a
history-less request builds byte-identical prompts to the pre-multi-turn path (`api.py:73-97`, `agent.py:326-354`).

## 1.3 Sessions / conversation

| Method | Path | Purpose | Request | Response | Auth / role | CSRF |
| --- | --- | --- | --- | --- | --- | --- |
| POST | `/v1/sessions` | Create a session (title optional; auto-named from the first question) | `SessionCreateRequest` | `{session:{session_id,title,created_at,updated_at}}` | any | yes |
| GET | `/v1/sessions` | Owner-scoped list, newest activity first; `q` matches title **or** any linked run's question/answer | Query `limit` 1-50=20, `offset` ≥0 ≤10000, `q` | `{sessions:[…], limit, offset, has_more}` (`limit+1` idiom) | any | no |
| GET | `/v1/sessions/{session_id}` | One session + transcript **derived from `runs`** (no messages table) | — | `{session, messages:[{run_id,question,answer,status,confidence_level,started_at}], turns}` | any (own only; else `404 SESSION_NOT_FOUND`) | no |
| GET | `/v1/sessions/{session_id}/results` | Rich content per run (SQL table / citations / industrial), newest first, paged — split out because terminal payloads average 14 kB, max 140 kB | `limit` 1-50=20, `offset` | `{session_id, results:[{run_id, result}\|…], limit, offset, has_more}` | any (own only) | no |
| PATCH | `/v1/sessions/{session_id}` | Rename (`422 SESSION_TITLE_REQUIRED` on blank) | `SessionRenameRequest` | `{session}` | any (own) | yes |
| DELETE | `/v1/sessions/{session_id}` | **Soft** delete (keeps the run audit chain) | — | `{session}` | any (own) | yes |

Evidence: `api.py:1208-1250`; `sessions.py:168-182`, `:185-236` (listing), `:239-255` (detail), `:291-331` (results),
`:332-364`; all routes are **not** in any `PUBLIC_*` set and `/v1/sessions` is in `_NO_STORE_PREFIXES`
(`api.py:1203-1206`, `:247-260`). Sessions are **metadata only, not model memory** (`docs/SESSIONS.md:15-18`).

## 1.4 Files / upload

| Method | Path | Purpose | Request | Response | Auth / role | CSRF |
| --- | --- | --- | --- | --- | --- | --- |
| POST | `/v1/uploads` | **The only upload entry**; raw bytes body (not multipart), 201 | Query `filename` 1-200, `provenance: synthetic_demo\|authorized_external`; body = file (`application/octet-stream`) | `UploadCreateResult` (queued/source identity; 415 on bad extension, 422 on bad name) | **admin** | yes |
| GET | `/v1/uploads` | Page of uploads + `worker.alive`/`worker.last_seen_at` heartbeat | Query `status?`, `media_type?`, `since?`, `limit` 1-200=50 | listing (no total — page size only) | **admin** | no |
| GET | `/v1/uploads/{upload_id}` | Pure read of one upload incl. `analysis`/`vector_index`/`contract_url`/`worker`; **never drives parsing** | — | upload detail | **admin** | no |
| POST | `/v1/uploads/{upload_id}/approve` | Opens the review gate only; does not imply searchable/indexed | — | approval result incl. `indexed` | **admin** | yes |
| POST | `/v1/files/uploads` | **Retired** two-step staging surface (raw bytes + query `filename`, `provenance`) | Query as above | `{source}` | **admin** | yes |
| POST | `/v1/files/jobs` | **Retired** batch registration job (DXF/measurement items) | `FileJobRequest` | job object | **admin** | yes |
| GET | `/v1/files/jobs` | Retired job list | — | `{jobs:[…]}` | **admin** | no |
| GET | `/v1/files/jobs/{job_id}` | Retired job detail | — | job object | **admin** | no |
| POST | `/v1/files/jobs/{job_id}/cancel` | Retired cancel | — | job object | **admin** | yes |
| POST | `/v1/files/jobs/{job_id}/retry` | Retired retry | — | job object | **admin** | yes |

Evidence: `api.py:743-773` (uploads), `:696-730` (files), permission branch `api.py:295` (both prefixes → `admin`),
parsing is driven only by the standalone worker `python -m ingestion.upload_worker --watch|--once`
(`api.py:733-741`, `scripts/start_local.sh:85-86`). The Vue app uses **only** `/v1/uploads`
(`docs/FRONTEND_VIEWS.md:323`).

## 1.5 Knowledge / RAG / private PDF

| Method | Path | Purpose | Request / Query | Response | Auth / role | CSRF |
| --- | --- | --- | --- | --- | --- | --- |
| POST | `/v1/pdf-knowledge/publications` | Publish a registered snapshot into the private vector index | `PdfKnowledgePublicationRequest` | `{publication}` | **admin** | yes |
| GET | `/v1/pdf-knowledge/publications` | Publication list | `limit` 1-50=20, `offset` ≤10000 | listing | **admin** | no |
| GET | `/v1/pdf-knowledge/publications/{publication_id}` | Poll one publication | — | `{publication}` | **admin** | no |
| POST | `/v1/pdf-knowledge/publications/{publication_id}/retry` | Retry a failed publication | — | `{publication}` | **admin** | yes |
| GET | `/v1/pdf-knowledge/publications/{publication_id}/chunks` | Chunk plan + `409 PDF_INDEX_SELECTION_CHANGED` if `expected_plan_sha256` differs | `expected_plan_sha256` (64-hex) | chunks result | **admin** | no |
| GET | `/v1/pdf-knowledge/documents/{document_id}` | Current publication head for a document | — | `{publication}` | **admin** | no |
| POST | `/v1/pdf-knowledge/search` | Bounded hybrid retrieval over **explicitly selected** published versions | `PdfKnowledgeSearchRequest` | search result (hits/scores) | **admin** | yes |
| POST | `/v1/pdf-knowledge/answer` | Same search + grounded answer | `PdfKnowledgeSearchRequest` | answer result | **admin** | yes |
| POST | `/v1/pdf-knowledge/publications/{publication_id}/repair` | Repair one publication's index | — | repair report | **admin** | yes |
| GET | `/v1/pdf-knowledge/orphans` | Scan Qdrant/DB orphans | — | orphan scan | **admin** | no |
| POST | `/v1/pdf-knowledge/orphans/reclaim` | Reclaim orphans (needs `confirm`) | free-form `dict` → `reclaim_payload` | reclaim result | **admin** | yes |
| POST | `/v1/pdf-snapshots` | Register a **parsing snapshot** (explicitly *not* indexed/visible/approved: `semantics` in the body) | `PdfSnapshotRegistrationRequest` | `{version, semantics}` | **admin** | yes |
| GET | `/v1/pdf-snapshots` | Snapshot list | `limit` 1-50=20, `offset` | listing | **admin** | no |
| GET | `/v1/pdf-snapshots/{document_id}/versions` | Version history | `limit`, `offset` | history | **admin** | no |
| GET | `/v1/pdf-snapshots/{document_id}` | One snapshot | `version?` ≥1 | `{version}` | **admin** | no |
| GET | `/v1/pdf-snapshots/{document_id}/file` | Original PDF (`409 PDF_SNAPSHOT_SELECTION_CHANGED` on hash mismatch) | `version`, `expected_manifest_sha256` | `application/pdf` + attachment + `nosniff` + CSP sandbox | **admin** | no |
| GET | `/v1/pdf-snapshots/{document_id}/pages/{page_no}` | Rendered page PNG | `version`, `expected_manifest_sha256` | `image/png` + `nosniff` | **admin** | no |
| GET | `/v1/pdf-snapshots/{document_id}/bundle` | Version metadata + full JSON bundle | `version`, `expected_manifest_sha256` | `{version, bundle}` | **admin** | no |
| POST | `/v1/plans/execute` | Execute a V-CAR dependency plan | free-form `dict` | plan result | **admin** | yes |

Evidence: `api.py:425-523` (pdf-knowledge), `:349-422` (snapshots), `:493-498` (plans), permission branch `api.py:295`
(`plan_path`/`pdf_snapshot_path`/`pdf_knowledge_path` → `admin`).

## 1.6 Evidence / runs / documents / facts / review & approval

| Method | Path | Purpose | Request | Response | Auth | CSRF |
| --- | --- | --- | --- | --- | --- | --- |
| GET | `/v1/runs` | Recent runs (「运行记录」); projects `conversation_id` so NULL can be shown honestly | `limit` 1-200=20, `offset` ≤100000 | `{runs:[{run_id,conversation_id,question,status,confidence_level,started_at,ended_at}], limit, offset, has_more}` | **anon** (public read) | no |
| GET | `/v1/runs/{run_id}` | Full run row | — | all `runs` columns | **anon** | no |
| GET | `/v1/runs/{run_id}/events` | Persisted trace events, cursor-paged (JSON, **not** SSE) | `after` (must be `evt-<run[4:]>-…`, else `400 INVALID_CURSOR`) | `{events:[≤200], next_cursor}` | **anon** | no |
| GET | `/v1/runs/{run_id}/evidence` | Evidence graph: nodes + edges + claims | — | `{nodes, edges, claims}` | **anon** | no |
| GET | `/v1/runs/{run_id}/conflicts` | Persisted cross-source conflict records | — | `{conflicts:[payload,…]}` | **anon** | no |
| GET | `/v1/documents` | Published documents (**the Q&A file selector source**); rejects non-allow-listed `collection` with `400 COLLECTION_NOT_ALLOWED`; adds derived `searchable`/`index_state`/`index_note` | `collection?` | `{documents:[…]}`, LIMIT 200 | **anon** | no |
| GET | `/v1/documents/{document_id}` | One published document (`object_uri` stripped) | — | document row | **anon** | no |
| GET | `/v1/documents/{document_id}/file` | Original file inline (`409 DOCUMENT_SELECTION_CHANGED`, `422 DOCUMENT_SELECTION_INVALID`, `404 SOURCE_FILE_UNAVAILABLE`, `409 SOURCE_VERSION_MISMATCH`) | `expected_version?`, `expected_sha256?` | FileResponse by `FILE_MEDIA_TYPES` | **anon** | no |
| GET | `/v1/documents/{document_id}/contract` | One bounded page of everything the parse contract stored (`blocks` + `locators`); unpublished upload rows readable **only by an authenticated admin** and then `no-store` | `limit?`, `offset?` (validated → `400 CONTRACT_PAGE_*_INVALID`) | `{document_id, document_version, source_sha256, parser_version, contract_version, block_count, locator_count, block_locator_count, locator_total, relation_count, page, store_pages, totals, counts, parse_contract, unsupported_features, blocks, locators, relations}` | anon if published; else admin | no |
| GET | `/v1/documents/{document_id}/facts` | Verified facts (`read_verified_facts`) | — | `{document_id, document_version, source_sha256, parser_version, facts, fact_count, quality}` | **anon** | no |
| GET | `/v1/documents/{document_id}/facts/{fact_id}/reviews` | Extraction-fidelity review history | — | `{reviews, semantics}` | **anon** | no |
| POST | `/v1/documents/{document_id}/facts/{fact_id}/reviews` | Record an extraction review; reviewer label is **server-owned** (`422 REVIEWER_IDENTITY_SERVER_OWNED`), reason required (`422 REVIEW_REASON_REQUIRED`), stale fact `409 FACT_UNAVAILABLE_OR_STALE` | `FactReviewRequest` | `{review, field_state, semantics}` | **reviewer** | yes |
| GET | `/v1/documents/{document_id}/facts/{fact_id}/approvals` | Version-bound local approval history | — | `{approvals, semantics}` | **anon** | no |
| POST | `/v1/documents/{document_id}/facts/{fact_id}/approvals` | Record a separate local approval (never overwrites source declarations) | `FactApprovalRequest` | `{approval, field_state, semantics}` | **approver** | yes |
| GET | `/v1/documents/{document_id}/pages/{page_no}` | Page PNG | `expected_version?`, `expected_sha256?` | `image/png` | **anon** | no |

Evidence: `api.py:1564-1626`, `:1280-1319`, `:1346-1353`, `:1419-1474` + `_contract_visible` `:1375-1391`,
`:1477-1485`, `:1488-1540`, `:1543-1555`; media map `FILE_MEDIA_TYPES` `:1339-1343`; source-path allow-list
`_source_path` `:1322-1328`. Permission: `/v1/documents` GETs sit inside the anonymous read prefix; the POSTs fall to
the generic `reviewer`/`approver` rules (`api.py:303-304`).

## 1.7 Model config / profiles

| Method | Path | Purpose | Request | Response | Auth / role | CSRF |
| --- | --- | --- | --- | --- | --- | --- |
| GET | `/v1/model/profiles` | Saved profiles (masked) + protocol list | — | `{items:[…public_profile], active, protocols:[{id,label}]}` | **admin** + **origin check** | no |
| GET | `/v1/model/profiles/export` | Export profiles; masked unless `include_keys=true` | `include_keys=false` | `{exported_at, include_keys, active, profiles}` | **admin** + origin | no |
| POST | `/v1/model/profiles/import` | Import (ccswitch-style), ≤50, same name never overwrites | `ProfileImportRequest` | `{imported, …profiles list}` | **admin** | yes |
| POST | `/v1/model/profiles` | Create profile (`400 PROFILE_NAME_REQUIRED`) | `ProfileRequest` | public profile | **admin** | yes |
| PUT | `/v1/model/profiles/{profile_id}` | Update; blank `api_key`/`headers`/`proxy` = **keep** (key-level merge); `404 PROFILE_NOT_FOUND` | `ProfileRequest` | public profile | **admin** | yes |
| DELETE | `/v1/model/profiles/{profile_id}` | Delete profile | — | full profile list | **admin** | yes |
| POST | `/v1/model/profiles/{profile_id}/activate` | Activate profile (**no body** — sending `{}` is a 422 on some paths) | — | full profile list | **admin** | yes |
| GET | `/v1/model/config` | Active config view (never a connectivity claim) | — | `ModelConfigView` (21 keys) + `environment_fallback` | **admin** + origin | no |
| PUT | `/v1/model/config` | Single-config write; `api_key` `clear`/`none`/`-` = erase, empty = keep | `ProfileRequest` | config view | **admin** | yes |
| DELETE | `/v1/model/config` | Clear active config | — | **narrow** `{has_key, key_masked, source}` (3 keys) | **admin** | yes |
| POST | `/v1/model/config/models` | **Probe**: list upstream models (`400 MODEL_KEY_NOT_INHERITED` when a cross-host probe would reuse the stored key) | `ProfileRequest` (empty `{}` legal) | `{ok, …models, base_url, protocol}` | **admin** | yes |
| POST | `/v1/model/config/test` | **Probe**: the only proof the model answers | `ProfileRequest` (empty `{}` legal) | `{ok, model, elapsed_ms, protocol, reply?, error_code?, message?, base_url, configured_model}` | **admin** | yes |

Evidence: `api.py:975-1111`, permission branch `:300-301`, `SENSITIVE_READ_PREFIXES` `:238`, resolution entry
`resolve_profile` `:935-965`, `_reject_cross_origin_inheritance` `:968-972`.

## 1.8 CAD drawings & dimension verification

| Method | Path | Purpose | Request / Query | Response | Auth / role | CSRF |
| --- | --- | --- | --- | --- | --- | --- |
| GET | `/v1/drawings/sources` | Registrable `.dxf` sources | — | `{sources}` | **admin** | no |
| POST | `/v1/drawings` | Register a drawing version (explicitly *not* formal approval / production binding — `semantics` in body) | `DrawingRegistrationRequest` | `{version, semantics}` | **admin** | yes |
| GET | `/v1/drawings` | Drawing assets (current version each) | — | `{drawings}` | any | no |
| GET | `/v1/drawings/{document_id}/versions` | Version history | — | history | any | no |
| GET | `/v1/drawings/{document_id}` | Detail (source, identity, metadata without `render_entities`) | `version?` | `{version, source, identity, metadata}` | any | no |
| GET | `/v1/drawings/{document_id}/entities` | Entities + views | `version?` | `{version, entities, views}` | any | no |
| GET | `/v1/drawings/{document_id}/features` | Frozen machine-extracted features + `inspection_gate` + association-review snapshot + `semantics` | `version?` | `{version, features, inspection_gate, …inspect(), semantics}` | any | no |
| GET | `/v1/drawings/{document_id}/features/{feature_id}/reviews` | Association review history (`latest_review: null` = never human-confirmed) | `version?` | review history | any | no |
| POST | `/v1/drawings/{document_id}/features/{feature_id}/reviews` | Record association review (`confirmed` needs a `selected_target` from `target_candidates`) | `DrawingAssociationReviewRequest` | `{review}` | **reviewer** | yes |
| GET | `/v1/drawings/{document_id}/file` | Original DXF download | `version?` | `application/dxf` + attachment + nosniff | any | no |
| GET | `/v1/drawings/{document_id}/preview` | Server-rendered SVG preview | `version?` | `image/svg+xml`, CSP `sandbox; default-src 'none'` | any | no |
| GET | `/v1/drawings/{document_id}/viewer` | Server-rendered HTML viewer (for `<iframe>`) | `version?` | `text/html`, CSP `sandbox allow-scripts` (**no** `allow-same-origin`) | any | no |
| GET | `/v1/dimensions/sources` | Registrable measurement sources | — | `{sources}` | **admin** | no |
| GET | `/v1/dimensions/measurements` | Measurement list (**no `version` query param**) | — | listing (`truncated`/`current_version`) | any | no |
| POST | `/v1/dimensions/measurements` | Register measurement (append-only version chain) | `MeasurementRegistration` | `{measurement}` | **admin** | yes |
| GET | `/v1/dimensions/measurements/{measurement_id}` | One measurement | `version?` | `{measurement}` | any | no |
| GET | `/v1/dimensions/bindings` | Binding list | — | listing | any | no |
| POST | `/v1/dimensions/bindings` | Register binding (needs a `confirmed` association review → `409 DIMENSION_CONFIRMED_ASSOCIATION_REQUIRED`) | `BindingRegistration` | `{binding}` **(no approval chain, no verdict)** | **admin** | yes |
| GET | `/v1/dimensions/bindings/{binding_id}` | Binding = content + approval chain + current verdict + authentication scope | `version?` | full binding view | any | no |
| POST | `/v1/dimensions/bindings/{binding_id}/approvals` | Independent use approval (approver ≠ registrar/reviewer) | `ApprovalRequest` | `{approval}` | **approver** | yes |
| POST | `/v1/dimensions/bindings/{binding_id}/comparisons` | Compute + persist a comparison snapshot (`replay:false`) | `ComparisonRequest` | comparison record | **admin** | yes |
| GET | `/v1/dimensions/bindings/{binding_id}/comparisons` | Comparison history (paged, `next_offset`) | `offset` 0-10000=0, `limit` 1-50=20 | history | any | no |
| GET | `/v1/dimensions/comparisons/{comparison_id}` | Byte-identical replay of a stored comparison (`replay:true`) | — | comparison record | any | no |

Evidence: `api.py:526-620` (drawings), `:623-690` (dimensions), permission chain `:288-299`; `ds/FRONTEND_VIEWS.md:238-307`.

## 1.9 Authority rules (source precedence) — evidence governance

| Method | Path | Purpose | Request | Response | Auth / role | CSRF |
| --- | --- | --- | --- | --- | --- | --- |
| GET | `/v1/authority/candidates` | Unresolved numeric-conflict candidates for a question (`422` with the reason code if unparsable) | `question` 1-2000 | `{candidates}` | **admin** | no |
| GET | `/v1/authority/rules` | Rule registry (+ latest activation event, `payload_valid`) | — | `{rules}` | any | no |
| POST | `/v1/authority/rules` | Register a rule (explicit registration only; `409 AUTHORITY_CONTEXT_CHANGED`, `409 APPROVED_FIELD_REQUIRED`) | `AuthorityRequest` | `{rule}` | **admin** | yes |
| GET | `/v1/authority/rules/{rule_id}/events` | Activation/revocation event chain, chronological | — | `{events}` | any | no |
| POST | `/v1/authority/rules/{rule_id}/events` | Approve / reject / revoke (creator cannot self-approve) | `AuthorityEventRequest` | `{event}` | **approver** | yes |

Evidence: `api.py:805-835`, permission chain `:300-302`.

## 1.10 Framework / static routes (not in the `PUBLIC_*` whitelist)

`GET /docs`, `/redoc`, `/openapi.json`, `/docs/oauth2-redirect` are auto-registered by FastAPI and are **not** in
`PUBLIC_PATHS` (`api.py:232`) → they still pass through `current(required=True)`; they work locally only because the
loopback demo identity is an admin (`auth.py:59-69`). The static mount `app.mount("/assets/vue", …)` (`api.py:844`)
is covered by the `/assets/` public read prefix (`api.py:235`).

---

# PART 2 — Frontend

## 2.1 Framework, build tool, entry, routing

| Item | Value | Evidence |
| --- | --- | --- |
| Framework | **Vue 3**, `vue ^3.5.13` (+ `vue-router ^4.5.0`) | `frontend/package.json:17-18` |
| Language | TypeScript `~5.7.2` (`<script setup lang="ts">` SFCs) | `frontend/package.json:25` |
| Build tool | **Vite `^6.0.3`** + `@vitejs/plugin-vue ^5.2.1` + Tailwind CSS 4 via `@tailwindcss/vite` | `frontend/package.json:23-26`, `:21` |
| Extra runtime deps (landing-page animation) | `gsap ^3.15.0`, `matter-js ^0.20.0`, `motion-v ^1.10.3`, `ogl ^1.0.11` | `frontend/package.json:13-16` |
| Entry HTML | `frontend/index.html` → `<div id="app">` + `<script type="module" src="/src/main.ts">` | `frontend/index.html:10-11` |
| Entry TS | `createApp(App).use(router).mount('#app')` | `frontend/src/main.ts:1-6` |
| Root component | `App.vue` = `<router-view />` only | `frontend/src/App.vue:1-3` |
| Routing library | `vue-router` with **`createWebHashHistory()`** (hash mode chosen because the backend has no SPA fallback and the bundle is mounted under `/assets/vue/`) | `frontend/src/router/index.ts:1`, `:5` |
| Build output | `vite build --outDir ../web/vue --emptyOutDir` → `web/vue/` (present in the repo) | `frontend/package.json:10`; `web/vue/index.html` exists |
| Base path | `base: '/assets/vue/'` | `frontend/vite.config.ts:12` |
| Dev server | port 5173, proxies `/v1` and `/health` → `http://127.0.0.1:8000` | `frontend/vite.config.ts:15-24` |

## 2.2 Pages / views

Routes are declared in `frontend/src/router/index.ts:8-24`; the six governance-view contract is
`docs/FRONTEND_VIEWS.md:21-38` (route × view × endpoints × role × CSRF).

| Route | View file | Purpose | What it displays | API calls (from `src/api/client.ts` unless noted) |
| --- | --- | --- | --- | --- |
| `/#/` (`name: landing`) | `frontend/src/views/LandingPage.vue` (1131 L) | Marketing / explainer "why DataMind" (`router/index.ts:8`) | Hero with WebGL/text animations (`WarpText`, `BlurText`, `FoldText`, `FallingText`, `TrueFocus`, `PixelSwap`, `Topography`, `GlassSurface`, `Folder`, `FlipCard` — `LandingPage.vue:3-15`), capability cards, "Evidence Constellation" section that reuses `ConstellationGraph` as a **static demo** (`LandingPage.vue:15`, `:330`) | **none** (no `api/client` import) |
| `/#/app` (`name: app`) | `frontend/src/views/MainPage.vue` (803 L) | **The workbench**: chat + evidence graph, session sidebar | Two-tab main area: `对话` and `证据轨迹` (`MainPage.vue:370-395`); `ChatPanel` (`:413`), `ConstellationGraph` + `NodeDetail` (`:431-456`), `SideNav` (`:360`); topbar service-status pill (`:397-402`) | `streamChat` (`MainPage.vue:8`, `:269`), `getEvidence` (`:8`, `:150`, `:286`); sessions via `useSessions` → `listSessions/getSession/listSessionResults/createSession/renameSession/deleteSession` (`composables/useSessions.ts:3-10`); `/health` + `GET /v1/model/config` + `POST /v1/model/config/test` via `useModelAccess` (`composables/useModelAccess.ts:2`, `:9-11`) |
| `/#/runs` (`name: runs`) | `frontend/src/views/RunsPage.vue` (449 L) | Run records / history (paged list + detail) | Run list with status tone, `会话 xxx` or `未归属会话` honest NULL rendering (`RunsPage.vue:162-163`), detail panel with a jump to the owning session (`:178-185`) | `listRuns`, `getRun` (`RunsPage.vue:5`) |
| `/#/model` (`name: model`) | `frontend/src/views/ModelPage.vue` (977 L) | Model access configuration (3 non-implying facts kept apart: configured / connectivity / upstream model names — `docs/FRONTEND_VIEWS.md:114-124`) | Active config, profile list + editor, probe panel, import/export; renders `403 PERMISSION_DENIED` as a **permission** fact, not "load failed" (`ModelPage.vue:513`, `:532`) | `modelConfig`, `getModelProfiles`, `createModelProfile`, `updateModelProfile`, `deleteModelProfile`, `activateModelProfile`, `exportModelProfiles`, `importModelProfiles`, `putModelConfig`*, `deleteModelConfig`, `listModels`, `testModelConfig`, `me` (`ModelPage.vue` import block; sub-components `ProfileList.vue`, `ProfileEditor.vue`, `ProbePanel.vue`) |
| `/#/fields` (`name: fields`) | `frontend/src/views/FieldsPage.vue` (1049 L) | Field / fact review & approval | Document list (with `searchable`/`index_state`/`index_note` shown as information, **never** as a filter — `docs/FRONTEND_VIEWS.md:184-187`), fact list, review & approval chains, locator overlay | `listDocuments`, `getDocumentFacts` (`FieldsPage.vue`); `getFactReviews`, `getFactApprovals`, `submitFactReview`, `submitFactApproval`, `me` (`components/fields/FactReviewPanel.vue`); `pageImageUrl` for the overlay (`components/fields/LocatorOverlay.vue`) |
| `/#/authority` (`name: authority`) | `frontend/src/views/AuthorityPage.vue` (463 L) | Source-authority rule registration + independent activation | 3-step layout (candidates → register → registered rules) (`AuthorityPage.vue:153`, `:206`, `:236`) | via `components/authority/useAuthority.ts`: `authorityCandidates`, `listAuthorityRules`, `authorityRuleEvents`, `registerAuthorityRule`, `submitAuthorityEvent`, `me` |
| `/#/cad` (`name: cad`) | `frontend/src/views/CadPage.vue` (1010 L) | Drawings & entities: frozen machine extraction vs. human association review | Drawing list/versions/entities table, machine extraction, gate summary, viewer, review panel | `listDrawings`, `listDrawingSources`, `drawingVersions`, `getDrawing`, `drawingEntities`, `drawingFeatures`, `featureReviews`, `registerDrawing`, `submitFeatureReview`, `me` (`CadPage.vue`); `drawingFileUrl`/`drawingPreviewUrl`/`drawingViewerUrl` (`components/cad/CadViewer.vue`) |
| `/#/dimensions` (`name: dimensions`) | `frontend/src/views/DimensionsPage.vue` (903 L) | Dimension verification chain: register → bind → approve → compare | Measurement/binding lists, result card, comparison section, admin forms, approval form | `listDimensionSources`, `listMeasurements`, `listBindings`, `getMeasurement`, `getBinding`, `me` (`DimensionsPage.vue`); `registerMeasurement`, `registerBinding`, `drawingFeatures`, `featureReviews`, `getDrawing` (`components/dimensions/DimensionAdminForms.vue`); `submitBindingApproval` (`DimensionApprovalForm.vue`); `saveComparison`, `comparisonHistory`, `getComparison` (`DimensionComparisonSection.vue`) |
| `/#/files` (`name: files`) | `frontend/src/views/FilesPage.vue` (829 L) | My files / upload pipeline (admin-only, raw-bytes upload) | Upload panel + upload records list with `index_state`/`index_note` and honest `worker.alive` handling (`FilesPage.vue:556`; `docs/FRONTEND_VIEWS.md:325-340`) | `uploadFile`, `listUploads`, `getUpload`, `approveUpload` (`FilesPage.vue:8`) |
| `/#/:pathMatch(.*)*` (`name: not-migrated`) | `frontend/src/views/NotMigrated.vue` (143 L) | Explicit fallback for unknown/legacy deep links (never a blank white page) | "这个视图还没有搬到新前端" + what *is* available (`NotMigrated.vue:33`, `:43`, `:51`) | none (`useRoute` only) |

`*` `putModelConfig` is exported by the client but the page's visible import list uses `modelConfig`/profiles;
the exported-but-unused client functions overall are `getEvents` (0 callers — the trace replay endpoint
`GET /v1/runs/{id}/events` has **no UI**) and `putModelConfig` (kept for the legacy single-config caller).
`SideNav.vue` (1305 L) owns navigation, the identity footer (`me()` — `SideNav.vue:47`), the persistent session
history list with search/rename/delete and the 3-group nav (`SideNav.vue:147-166`).

## 2.3 API client module

| Item | Detail | Evidence |
| --- | --- | --- |
| Path | `frontend/src/api/client.ts` (921 lines); types in `frontend/src/api/types.ts` (1578 lines) | — |
| Transport | `fetch` on **relative paths only**, same-origin required (`/v1/model/*` even on GET is origin-checked) | `client.ts:65-67` (comment), `:279-283` |
| Read kernel | `request<T>(path, init?)` — plain `fetch`, no CSRF, `res.json()` | `client.ts:176-180` |
| Write kernel | `write<T>(path, body?, method='POST')` — awaits `csrf()`, sets `X-CSRF-Token`, sets `Content-Type: application/json` **only when a body exists** (a body-less POST would otherwise 422) | `client.ts:185-201` |
| CSRF source | `csrf()` → `GET /v1/auth/me`, cached in a module-level `csrfToken` | `client.ts:108-124` |
| SSE | `streamChat(payload, onEvent)` — `fetch('/v1/chat/stream', {POST, X-CSRF-Token})`, manual SSE parsing (EventSource cannot POST), splits on `\n\n`, reads `data:` lines, resolves on `answer.completed` / `answer.abstained` / `answer.clarification` / `run.failed`, throws `未收到最终结果（流可能中断）` if the stream ends without a terminal event | `client.ts:211-277` |
| Error handling | `apiError(res, name)` normalises **both** envelopes: `{"detail":{"error_code":…}}` → `Error.errorCode` (plus `detail.details`), a bare string `detail`, and FastAPI's own 422 `detail: [{loc,msg}]` array → `VALIDATION_ERROR`; falls back to `HTTP_<status>`; also sets `err.status` | `client.ts:143-174` |
| Deliberate asymmetry | Probe endpoints (`/v1/model/config/test`, `/v1/model/config/models`) return `{ok:false,…}` and are **never** treated as the generic error shape | `client.ts:128-136` (comment), `:303-336` |
| Session errors | `sessionError()` is a thin alias over `apiError` (sessions return `detail.error_code`, not a string) | `client.ts:337-340` |
| URL builders (not `fetch`) | `pageImageUrl`, `documentFileUrl`, `drawingFileUrl`, `drawingPreviewUrl`, `drawingViewerUrl` — used directly as `<img src>` / `<a href>` / `<iframe src>` | `client.ts:588-606`, `:738-754` |
| Upload specifics | `uploadFile(file, filename, provenance)` → `POST /v1/uploads?filename=&provenance=` with `Content-Type: application/octet-stream` (raw bytes, **not** multipart) and `X-CSRF-Token` | `client.ts:891-905` |

## 2.4 CRITICAL — does the frontend send full multi-turn history?

**No. The frontend sends only the current question (plus mode / scope / an optional session id). It never sends
`history`.** Confirmed three ways:

**(a) The payload type has no `history` field at all** — `frontend/src/api/types.ts:18-25`:

```ts
export interface ChatPayload {
  question: string
  mode?: RouteMode
  scope?: Scope
  /** 会话归属：后端把它写进 runs.conversation_id（见 datamind/sessions.py）。
      仅在确有活动会话时下发——不传与传 null 在契约上都等于「不属于任何会话」。 */
  conversation_id?: string
}
```

**(b) The payload builder only ever emits question/mode/scope** — `frontend/src/views/MainPage.vue:198-218`:

```ts
function buildPayload(question: string, mode: string): ChatPayload {
  switch (mode) {
    case 'retail':
      return { question, mode: 'sql', scope: { database_sources: ['demo_retail'] } }
    case 'chinook':
      return { question, mode: 'sql', scope: { database_sources: ['chinook'] } }
    case 'industrial_sql':
      return { question, mode: 'sql', scope: { database_sources: ['demo_industrial'] } }
    case 'rag':
      return { question, mode: 'rag', scope: { document_collections: ['demo_kb'] } }
    case 'industrial':
      return {
        question,
        mode: 'industrial',
        scope: { database_sources: ['demo_industrial'], document_collections: ['industrial_demo'] },
      }
    case 'auto':
    default:
      return { question, mode: 'auto' }
  }
}
```

and the single call site that adds the only other key — `frontend/src/views/MainPage.vue:263-271`:

```ts
  // conversation_id 只在真有会话时才写进 body：没有会话时请求体与旧版逐字节一致
  //（后端也只在收到该键时才给 run() 传 conversation_id，见 api.py::_chat_kwargs）。
  const body: ChatPayloadWithSession = buildPayload(question, payload.mode)
  if (conversationId) body.conversation_id = conversationId

  try {
    const final = await streamChat(body, (ev) => {
      assistant.thinking!.push(ev)
    })
```

**(c) `streamChat` serialises exactly that object** — `frontend/src/api/client.ts:211-220`:

```ts
export async function streamChat(
  payload: ChatPayload,
  onEvent: (ev: TraceEvent) => void,
): Promise<FinalResult> {
  const token = await csrf()
  const res = await fetch('/v1/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': token },
    body: JSON.stringify(payload),
  })
```

Corroborating statements: `README.md:109` — “界面虽然显示多条消息，目前提问请求尚未携带历史上下文；
后端多轮能力见 多轮上下文”；`docs/MULTI_TURN.md:37-47` — history is 100 % client-supplied and the server persists
nothing; `docs/SESSIONS.md:15-18` — sessions are **not** multi-turn memory and do not feed the model.

**Consequence for the spec:** the backend *accepts* up to 20 history turns / 2000 chars per entry / 8000 chars total
(`datamind/sql_engine.py:52-55`) and reports `turns_used` in every final payload
(`datamind/agent.py:161-174`, surfaced in `answer.completed.output_summary`), but with this frontend
`turns_used` is always `0` and the visible multi-message transcript is a **rendering + persistence** feature,
not a model-context feature. The assistant bubbles come from `runs` rows via
`GET /v1/sessions/{id}` (`MainPage.vue:87-127`, `useSessions.ts:22-30`), not from a conversation buffer.

## 2.5 Evidence / citations / evidence-graph rendering

| Artefact | Component / file | What it renders | Evidence |
| --- | --- | --- | --- |
| Citations ("引用来源") | `frontend/src/components/chat/ChatPanel.vue` | Numbered cards: `title \|\| document_id`, `v{document_version}`, page range, `section_path` joined by `›`, quote (`quote → excerpt → text`) with clamp/expand, and `page_url` / `source_url` links opening in a new tab | `ChatPanel.vue:269-315`, helpers `:125-151`, overflow measuring `:178-202` |
| SQL result table | `ChatPanel.vue` | `查询结果 · N 行` caption + `<table>` from `sql_result.columns/rows` | `ChatPanel.vue:254-267` |
| Industrial cross-source verification table | `frontend/src/components/industrial/VerificationTable.vue` (573 L) | One row per measurement: DB value+unit, normalized value, report-side original value, `compliance`, `agreement`, **both sides of each conflict** (`issues[].left_value`/`right_value`), applied rule ids, plus summary counts | `ChatPanel.vue:248-252`; `VerificationTable.vue:1-60` (data contract + label maps) |
| **Evidence graph ("证据轨迹" / 推理星链)** | `frontend/src/components/stargraph/ConstellationGraph.vue` (1163 L) rendered in `MainPage.vue:433-439` | Directed, layered card graph: BFS depth from `Claim` nodes, barycentre ordering, cubic-Bézier edges, 9 node types with colour/icon/label (`Claim`, `SQLQuery`, `SQLRow`, `Document`, `Chunk`, `DocumentFact`, `VerificationCheck`, `ConflictRecord`, `AuthorityRule`) and 4 edge styles | `ConstellationGraph.vue:6-17` (props), `:40-52` (TYPE_STYLE), `:54-59` (EDGE_STYLE) |
| Evidence node detail panel | `frontend/src/components/stargraph/NodeDetail.vue` (555 L) rendered in `MainPage.vue:454` | Per-node payload view with per-node-type field ordering and Chinese field labels (SQL/bound params, DB row, document version+url, fact raw/normalized values + approval status, verification row, conflict record, authority rule) | `NodeDetail.vue:1-60` (explicit field-name contract comment) |
| Locator overlay (page bbox) | `frontend/src/components/fields/LocatorOverlay.vue` (383 L) | Draws citation bboxes over the page image; non-`pdf_points` / rotated / invalid bbox → "无法绘制：<原因>" label (never a guessed box) | `LocatorOverlay.vue` + `pageImageUrl`; contract `docs/FRONTEND_VIEWS.md:188-191` |
| CAD viewer | `frontend/src/components/cad/CadViewer.vue` (132 L) | `<iframe src=drawingViewerUrl>` (server HTML, CSP `sandbox allow-scripts`) + SVG preview link + DXF download | `CadViewer.vue` import line `drawingFileUrl, drawingPreviewUrl, drawingViewerUrl`; `api.py:603-620` |
| Graph empty/running states | `MainPage.vue` | "正在生成证据…" overlay while running; otherwise an explanatory empty state | `MainPage.vue:440-445` |

Graph data source: `GET /v1/runs/{run_id}/evidence` → `{nodes, edges, claims}` (`datamind/api.py:1608-1618`),
fetched after each completed run (`MainPage.vue:284-294`) and, for history, for the most recent terminal run only
(`MainPage.vue:140-161`). Note `Graph`/`NodeDetail` are also reused on the landing page as a **static demo**
(`LandingPage.vue:15`).

## 2.6 Is there a visible execution-trace / reasoning panel?

**Yes — two distinct surfaces.**

1. **In-bubble collapsible "思考过程" (thinking process) step list.** `ChatPanel.vue:227-239`:

```html
                <!-- 思考过程（可折叠） -->
                <details v-if="m.thinking && m.thinking.length" class="thinking" :open="m.status === 'running'">
                  <summary>
                    <span>{{ m.status === 'running' ? '正在思考…' : `思考过程 · ${m.thinking.length} 步` }}</span>
                  </summary>
                  <ol>
                    <li v-for="ev in m.thinking" :key="ev.event_id">
                      <span class="mark" :class="ev.status">{{ ev.status === 'failed' ? '✕' : ev.status === 'warning' ? '!' : '✓' }}</span>
                      <span class="label">{{ eventLabel(ev) }}</span>
                      <span class="agent">{{ ev.agent_name }}</span>
                    </li>
                  </ol>
                </details>
```

Populated live from the SSE stream — every `TraceEvent` is pushed into `assistant.thinking`
(`MainPage.vue:269-271`). Event-type and tool labels are mapped to Chinese in `ChatPanel.vue:45-73`
(e.g. `query.parsed` → 理解问题, `sql.validated` → SQL 校验通过, `verification.passed/failed`,
`answer.abstained` → 证据不足，拒绝作答, `answer.clarification` → 需要澄清, `run.failed`).
While running with no events yet, a pulsing "正在思考…" line replaces it (`ChatPanel.vue:241-243`).

2. **A full-width `证据轨迹` (evidence trail) tab** in the main area (`MainPage.vue:370-395`, section
`:417-458`) holding the reasoning graph + node detail (see 2.5).

**Gap:** the *persisted* trace (`GET /v1/runs/{run_id}/events`, cursor-paged, `datamind/api.py:1596-1605`) is
**not** replayed by any view — `client.ts:106` exports `getEvents()` and it has **0 call sites**. So a historical
run shows only answer text/SQL/citations (from `/v1/sessions/{id}/results`) and its evidence graph, **not** its
step-by-step trace. Reopening a session therefore loses the "思考过程 · N 步" panel for older runs.

## 2.7 Real screenshots checked into the repo

Command used to source this table: `find reports docs 技术文档 交付 output -iname '*.png' -o -iname '*.jpg'`, plus
`sips -g pixelWidth -g pixelHeight` for dimensions and a repo-wide grep for the filename in `*.md/*.json/*.html`.

⚠️ **Honesty caveat:** my model has **no image input**, so `read_image` was rejected. Column "what it shows" is
inferred from the filename **and** from the document that references the image; only the seven filenames with **no**
referencing document lack any independent textual evidence, and they are marked `(unreferenced)`.
Also note `README.md:111` — the old HTML/JS/CSS console was **retired** — so most referenced screenshots depict the
**legacy** UI, not the current Vue app. Only the seven 2026-10-12/10-10 dated files post-date the Vue migration.

| # | Path | Size | Dimensions | What it shows (basis) | Referenced by |
| --- | --- | --- | --- | --- | --- |
| 1 | `reports/landing_vue_preview_20261010.png` | 120K | 1440×1400 | Vue **landing page** (`/#/`) preview, dated 2026-10-10 (filename only) **(unreferenced)** | — |
| 2 | `reports/sessions_ui_20261012.png` | 120K | 1440×1000 | Vue **chat-session UI** (sidebar history + workbench), dated 2026-10-12 **(unreferenced)** | — |
| 3 | `reports/runs_page_20261012.png` | 296K | 1440×1200 | Vue **运行记录 page** (`/#/runs`), dated 2026-10-12 **(unreferenced)** | — |
| 4 | `reports/view_fields_20261012.png` | 256K | 1600×1400 | Vue **字段复核 page** (`/#/fields`), dated 2026-10-12 **(unreferenced)** | — |
| 5 | `reports/view_files_20261012.png` | 272K | 1600×1400 | Vue **我的文件 page** (`/#/files`), dated 2026-10-12 **(unreferenced)** | — |
| 6 | `reports/dimension_ui_approved.jpg` | 164K | 1218×1786 | Dimension UI, **approved** state (filename) **(unreferenced)** | — |
| 7 | `reports/dimension_ui_revoked_replay.jpg` | 216K | 1218×2321 | Dimension UI, **revoked + replay** state (filename) **(unreferenced)** | — |
| 8 | `reports/industrial_sql_browser.png` | 124K | 1280×1283 | Industrial **NL2SQL** browser run: SSE result, SQL rows, scope/SQL/binding, 9 batches / 10 measurements for A102 op 30 | `技术文档/…V1.2.md:1392`, `reports/industrial_query_verification.json` |
| 9 | `reports/industrial_browser.png` | 208K | 1198×2300 | Industrial cross-source verification run: SSE result + verification table + trace replay | `技术文档/…V1.2.md:1357`, `reports/industrial_verification.json` |
| 10 | `reports/industrial_table.png` | 76K | 1198×771 | The industrial verification table (per-measurement rows, conflicts) | `reports/industrial_verification.json` |
| 11 | `reports/industrial_locator.png` | 52K | 1198×771 | Original-text / range locator overlay with overlapping-version dual citation | `技术文档/…V1.2.md:1357`, `reports/industrial_verification.json` |
| 12 | `reports/industrial_forms_v2_browser.png` | 40K | 339×793 | Narrow-viewport industrial form (v2) render | `技术文档/…V1.2.md`, `reports/industrial_forms_v2_verification.json` |
| 13 | `reports/rag_browser.jpg` | 108K | 1280×1370 | RAG document Q&A browser run (answer + citations) | `docs/OFFICIAL_REQUIREMENTS_MAPPING.md`, `docs/SCORE_GAP_LEDGER.md` |
| 14 | `reports/multifile_rag_browser.jpg` | 192K | 1218×1883 | Multi-file RAG (explicit multi-document selection, up to 8) | `docs/OFFICIAL_REQUIREMENTS_MAPPING.md`, `docs/SCORE_GAP_LEDGER.md` |
| 15 | `reports/multifile_queue_browser.jpg` | 124K | 1218×1348 | Multi-file registration **queue** states | (filename; no doc reference found) |
| 16 | `reports/field_review_browser.png` | 80K | 1218×791 | Field review: isolated copy → locator → submit → history verified | `技术文档/…V1.2.md`, `PROGRESS.md:1427`, `reports/field_review_ui_test.json` |
| 17 | `reports/field_review_current.png` | 76K | 1218×791 | Final page: operation/measurement/revision + original approval declaration, original page shown under the field | `PROGRESS.md:1428`, `reports/field_review_verification.json` |
| 18 | `reports/document_fields_browser.png` | 60K | 1280×720 | Document field extraction page | `技术文档/…V1.2.md`, `reports/fields_verification.json` |
| 19 | `reports/document_fields_locator.png` | 44K | 1280×720 | Field locator on the source page | `技术文档/…V1.2.md`, `reports/fields_verification.json` |
| 20 | `reports/identity_approval_browser.png` | 112K | 1218×793 | Separate-identity approval (reviewer ≠ approver) page | `技术文档/…V1.2.md`, `PROGRESS.md`, `reports/identity_approval_verification.json` |
| 21 | `reports/source_authority_browser.png` | 88K | 1215×790 | Source-authority rule registration page | `技术文档/…V1.2.md`, `reports/source_authority_verification.json` |
| 22 | `reports/source_authority_result_browser.png` | 96K | 1215×790 | Post-activation query resolved by the authority rule (report value 850 wins; production 870 preserved + out-of-range) | `PROGRESS.md:158`, `技术文档/…V1.2.md`, `reports/source_authority_verification.json` |
| 23 | `reports/source_authority_revoked_browser.png` | 96K | 1215×790 | After revocation: new query unresolved again; page history contains approve + revoke | `PROGRESS.md:158`, `技术文档/…V1.2.md`, `reports/source_authority_verification.json` |
| 24 | `reports/cad_dxf_browser.jpg` | 36K | 339×790 | DXF drawing viewer on a narrow viewport | `技术文档/…V1.2.md`, `PROGRESS.md`, `data/cad_demo/DXF_PROFILE.md` |
| 25 | `reports/cad_dxf_acceptance_browser.jpg` | 68K | 1280×720 | DXF acceptance run | `reports/cad_dxf_verification.json` |
| 26 | `reports/cad_assets_v1_context_browser.jpg` | 92K | 1215×790 | CAD asset **v1 context** (superseded-version context shown) | `data/cad_demo/ASSET_WORKFLOW.md` |
| 27 | `reports/cad_assets_v1_replay_browser.jpg` | 92K | 1215×790 | CAD asset v1 **replay** (historical version still renderable) | `技术文档/…V1.2.md`, `PROGRESS.md`, `data/cad_demo/ASSET_WORKFLOW.md` |
| 28 | `reports/cad_assets_v2_browser.jpg` | 92K | 1215×790 | CAD asset v2 (current version) | `技术文档/…V1.2.md`, `PROGRESS.md`, `data/cad_demo/ASSET_WORKFLOW.md` |
| 29 | `reports/cad_reviews_v2_unreviewed_browser.jpg` | 88K | 1215×790 | Feature with **no** human association review (`latest_review: null`) | `技术文档/…V1.2.md` |
| 30 | `reports/cad_reviews_confirmed_browser.jpg` | 104K | 1215×790 | Confirmed association review with `selected_target` | `技术文档/…V1.2.md` |
| 31 | `reports/cad_reviews_v1_replay_browser.jpg` | 108K | 1215×790 | v1 association-review replay | `技术文档/…V1.2.md` |
| 32 | `reports/ocr_formula_browser.jpg` | 108K | 1280×1393 | Scanned-OCR + formula recognition page | `技术文档/…V1.2.md`, `reports/ocr_verification.json` |

**Not UI screenshots** (do not use as figures): `tmp/**/*.png` — 77 files, all **PDF page renders** of the
交付 documents and delivery previews (e.g. `tmp/competition_docs_20261014/render_implementation/page-15.png`);
`data/knowledge_base/f_net.png`, `f_sales.png`, `f_yoy.png` — demo knowledge-base figures (41 PNGs total under
`data/`); `交付/校内初赛_20261014/*.pdf|docx` are deliverables, not screenshots.

**Best candidates for current-Vue demo figures:** #1–#5 (dated, 1440/1600 px wide, unreferenced in docs so they are
free to reuse) plus #3 (`runs_page_20261012.png`) and #2 for the workbench/session story. #8, #9, #13, #14 are the
strongest *content* figures but show the **legacy** console.

---

# PART 3 — How the app is actually run

## 3.1 Commands (macOS / Linux, repo root)

```bash
npm --prefix frontend ci                    # README.md:67
npm --prefix frontend run deploy            # → web/vue/   README.md:68
bash scripts/start_local.sh --background    # README.md:69
# SQL-only (skips Qdrant + ingestion + upload worker):
bash scripts/start_local.sh --sql-only --background   # README.md:72, start_local.sh:10,110-112
```

Windows PowerShell equivalent: `npm --prefix frontend ci` / `npm --prefix frontend run deploy` /
`powershell -ExecutionPolicy Bypass -File scripts/start_local.ps1 -Background` (`README.md:76-80`).

Frontend dev loop (hot reload, two terminals) — `README.md:119-128`:

```bash
DATAMIND_ALLOWED_ORIGINS="http://127.0.0.1:5173,http://localhost:5173" \
  .venv/bin/uvicorn datamind.api:app --host 127.0.0.1 --port 8000 --no-proxy-headers
npm --prefix frontend run dev               # Vite 5173, proxies /v1 + /health → 8000
```

Optional parse dependencies (`README.md:134-142`): `requirements-office.txt`, `requirements-cad.txt`,
`scripts/setup_ocr_models.py`, `scripts/setup_formula.py --python /path/to/python3.12`.

Verification (`README.md:152-168`): `scripts/verify.py`, `scripts/verify_frontend_layout.py` (headless Chrome + real
hit-tests; skips with exit 0 when Chrome is absent — `scripts/verify_frontend_layout.py:24-30`),
`scripts/test_isolated.py`, `scripts/status_sync.py --check`.

## 3.2 Required services, ports, containers

| Service | Image / process | Port (bind) | Evidence |
| --- | --- | --- | --- |
| PostgreSQL 16 | `postgres:16-alpine`, container `datamind-postgres`, volume `pg_meta_data` | `127.0.0.1:${POSTGRES_PORT:-5432}:5432` | `deploy/compose.yaml:5-6`, `:17`, `:19`; `:23` healthcheck |
| Qdrant 1.12.4 | `qdrant/qdrant:v1.12.4`, container `datamind-qdrant`, volume `qdrant_data` | `127.0.0.1:${QDRANT_PORT:-6333}:6333` (REST) + `127.0.0.1:6334` (gRPC) | `deploy/compose.yaml:29-35`; `:41` healthcheck |
| API (uvicorn) | `.venv/bin/uvicorn datamind.api:app --host 127.0.0.1 --port 8000 --no-proxy-headers` | 8000 (loopback) | `scripts/start_local.sh:126`; background variant `scripts/start_api_background.py:70` |
| Upload worker | `.venv/bin/python -m ingestion.upload_worker --watch` (interval `DATAMIND_UPLOAD_WORKER_INTERVAL`, default 2 s) | — (no HTTP) | `scripts/start_local.sh:83-89`; heartbeat `ingestion/upload_pipeline.py:857-867` |
| File-job worker (DXF/measurement batches) | `scripts/file_job_worker.py` — separate from the upload worker | — | `README.md:146` |
| Vite dev server (dev only) | `npm --prefix frontend run dev` | 5173 | `frontend/vite.config.ts:15` |
| Object storage | **MinIO removed**; local volume `data/object_store/` | — | `deploy/compose.yaml:46-53` |

**Databases** on the single Postgres container: `datamind_meta` (metadata, `META_DB`), `demo_retail`,
`chinook`, `demo_industrial` (created by schema grant `scripts/start_local.sh:33`); roles `datamind_app` and
`readonly_user` (`deploy/init/01_roles.sql`; DSN defaults `datamind/agent.py:47-54`).
`docker exec datamind-postgres pg_isready -U postgres -d datamind_meta` is the readiness probe
(`scripts/start_local.sh:27,32`).

## 3.3 What `start_local.sh` actually does (in order)

1. venv bootstrap + dependency probe/install (`scripts/start_local.sh:15-23`; RAG gate asserts
   `qdrant-client 1.12.*` and `rapidocr == 3.9.2`).
2. `docker compose -f deploy/compose.yaml up -d postgres` + readiness loop (`:25-32`).
3. `CREATE SCHEMA IF NOT EXISTS demo_industrial` + grant to `readonly_user` (`:33`).
4. Idempotent `runs_recent_idx` DDL as `postgres` (ownership workaround) (`:34-38`).
5. `.venv/bin/python db/migrate.py` (`:39`).
6. Seed only when empty: Chinook SQL (`:41-45`), `db/seed_demo_retail.py` (`:47-49`), `db/build_catalog.py`
   (`:50-52`), `db/seed_eval_cases.py` (`:53-55`).
7. If RAG enabled: warnings for missing OCR/formula models (`:58-64`), `up -d qdrant` + `/readyz` poll
   `http://127.0.0.1:6333/readyz` (`:65-72`), `ingestion/ingest.py` (`:74`), `db/seed_industrial.py` (`:75`),
   `db/build_industrial_catalog.py` (`:76`).
8. Short-circuits with a message if an API already answers `/health` (`:98-105` — code changes require a restart).
9. Background mode → `scripts/start_api_background.py` (detached API + worker; logs `.cache/api/server.log`,
   `.cache/api/upload_worker.log`; pids `.cache/api/server.pid`, `.cache/api/upload_worker.pid`)
   (`:107-117`, `scripts/start_api_background.py:39-78`). Foreground → uvicorn + worker with SIGTERM traps
   (`:119-129`).

Migrations/DDL live in `db/migrations/` (e.g. `db/migrations/020_chat_sessions.sql`, `013` for `task_leases` —
`datamind/api.py:893`) and `deploy/init/*.sql` (only on **first** container init — `scripts/start_local.sh:34`).

## 3.4 URLs

| Entry | URL | Evidence |
| --- | --- | --- |
| Landing page | `http://127.0.0.1:8000/#/` | `README.md:88` |
| Workbench | `http://127.0.0.1:8000/#/app` | `README.md:89` |
| API docs (Swagger) | `http://127.0.0.1:8000/docs` | `README.md:90`; `api.py` FastAPI default (identity-gated, loopback demo admin) |
| Health | `http://127.0.0.1:8000/health` | `README.md:91`; `api.py:875-901` |
| Six governance pages | `/#/model`, `/#/fields`, `/#/authority`, `/#/cad`, `/#/dimensions`, `/#/files` | `README.md:93` |
| Deep links | `/#/app?session=<sess-id>`, `/#/app?q=<question>&mode=<mode>`, `/#/app?panel=history` | `MainPage.vue:324-354` |
| Assets | `/assets/vue/*` | `api.py:844`, `frontend/vite.config.ts:12` |

## 3.5 Environment variables

| Variable | Meaning / default | Evidence |
| --- | --- | --- |
| `NEW_API_BASE_URL`, `NEW_API_MODEL`, `NEW_API_KEY` | Upstream generation endpoint; export **before** starting the API; stored DB config with a key **wins over** env | `README.md:44-48`, `:58`; `datamind/settings_store.py:418-422` |
| `NEW_API_PROTOCOL` | `openai` (default) or `anthropic` | `README.md:58`; `settings_store.py:418` |
| `NEW_API_MAX_TOKENS`, `NEW_API_TIMEOUT`, `NEW_API_AUTH_STYLE`, `NEW_API_HEADERS`, `NEW_API_PROXY` | Probe/generation transport knobs (`proxy` now honoured on question paths too) | `settings_store.py:418-422`; `sql_engine.py:133-155` |
| Defaults when nothing is configured | `DEFAULT_BASE_URL = https://api.deepseek.com/v1`, `DEFAULT_MODEL = deepseek-chat` | `settings_store.py:23-24` |
| `DATAMIND_META_DSN` / `DATAMIND_META_WRITE_DSN` | Read-only DSN (default `readonly_user`/`readonly_dev`) and write DSN (`datamind_app`/`datamind_app_dev`) | `datamind/agent.py:52-54`, `:47-49` |
| `DATAMIND_QDRANT_URL` | Default `http://127.0.0.1:6333` | `api.py:864`; `knowledge.py:203` |
| `DATAMIND_PDF_QDRANT_URL`, `DATAMIND_PDF_QDRANT_API_KEY` | Separate private-PDF vector store | `pdf_vectors.py:411`; `scripts/verify_pdf_search_local.py:75` |
| `DATAMIND_ALLOWED_ORIGINS` | Comma-separated extra origins added to the fixed allow-list (defaults `127.0.0.1:8000`, `localhost:8000`, `127.0.0.1:3000`, `localhost:3000`) | `auth.py:31-41` |
| `DATAMIND_LOCAL_NO_LOGIN` | Tri-state loopback bypass: unset ⇒ loopback is `admin`; `0/false/…` ⇒ require login; `1` ⇒ bypass for **every** client (documented global auth bypass) | `auth.py:59-63`; `docs/DEPLOYMENT_BOUNDARIES.md:9-19` |
| `DATAMIND_PUBLIC_CHAT` | `1` ⇒ `POST /v1/chat` + `/v1/chat/stream` anonymously callable (origin-checked, no CSRF) | `api.py:239-246`, `:263-269` |
| `DATAMIND_UPLOAD_WORKER_INTERVAL` | Worker poll interval (default 2 s) | `scripts/start_local.sh:86` |
| `DATAMIND_UPLOAD_WORKER_ALIVE_SECONDS` / `..._HEARTBEAT_SECONDS` | Heartbeat alive threshold (default 30 s) / renewal interval | `ingestion/upload_pipeline.py:140-142`, `:861` |
| `DATAMIND_FORMULA_PYTHON` | Separate Python 3.10–3.12 for formula recognition (default `.venv-formula/bin/python`) | `scripts/start_local.sh:61` |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `META_DB`, `POSTGRES_PORT`, `QDRANT_PORT` | Compose inputs (defaults `postgres`, `datamind_dev`, `datamind_meta`, `5432`, `6333`) | `deploy/compose.yaml:9-17`, `:34`; `deploy/.env.example:1-8` |
| `DATAMIND_BASE_URL`, `DATAMIND_CHROME` | Verification-script only (frontend layout check) | `scripts/verify_frontend_layout.py:31-42` |

**No model is downloaded for generation** — the LLM is called over HTTP; only the local embedding model
`BAAI/bge-small-zh-v1.5` plus optional OCR/formula ONNX weights are fetched into `.cache/`
(`datamind/knowledge.py:22-23`; `README.md:31`, `scripts/start_local.sh:58-64`).
Qdrant collections/datasets in use: `demo_kb`, `ohrbench_kb`, `industrial_demo` (`datamind/knowledge.py:23`);
SQL sources `demo_retail`, `chinook`, `demo_industrial` (`datamind/agent.py:61`).

---

# UNCERTAINTIES

1. **Screenshots were not visually inspected.** My model has no image input, so `read_image` was rejected
   (`Error: model "deepseek-v4.1-flash" does not declare image input`). Every "what it shows" cell in §2.7 is
   inferred from the filename plus the referencing document's surrounding text, not from the pixels. Seven files
   (rows #1–#7) have no referencing document at all and are the least certain; their dated filenames and dimensions
   are verified, their content is not. Before publishing figures, someone with vision should confirm #1–#7 really
   depict the current Vue app and pick crops.
2. **Two different UIs may be present in the screenshot set.** `README.md:111` says the legacy HTML/JS/CSS console
   was retired, and the referenced screenshots (#8–#32) come from earlier phases (2026-09/10-08…10-10) whose pages
   had different layouts. Whether e.g. `industrial_browser.png` shows the *old* console or a Vue page is not
   determinable from filenames/JSON alone — the surrounding prose (`技术文档/…V1.2.md:1357`) is consistent with the
   old console, but I could not confirm.
3. **`dimension_ui_approved.jpg` / `dimension_ui_revoked_replay.jpg` (rows #6–#7)** are unreferenced and their
   filenames map onto the dimension-verification state machine (`approved`, `revoked`, `replay`) documented in
   `docs/FRONTEND_VIEWS.md:271-307`, but I cannot prove they show the **Vue** `/#/dimensions` page rather than a
   test harness render. `reports/dimension_ui_*.jpg` have no sibling JSON naming them.
4. **Purpose statements for several endpoints are summarised from code, not from a written spec.** For the private
   PDF family (`/v1/pdf-snapshots*`, `/v1/pdf-knowledge*`), `/v1/plans/execute`, and the retired
   `/v1/files/*`, I derived purpose from route bodies (`api.py:349-523`, `:696-730`) plus their module docstrings;
   I did not read `docs/PDF_*.md`, `docs/VCAR.md`, or `docs/UPLOAD_PIPELINE.md` end-to-end. A dedicated pass over
   those docs would likely sharpen the "purpose" column and may reveal intended semantics not visible in code.
5. **Response shapes are dict-level, not schema-level.** FastAPI declares no `response_model=` on any route
   (routes `return` plain dicts / `jsonable_encoder(...)`), so I enumerated top-level keys by reading the return
   expressions and the storing service modules. Nested key sets (e.g. the full `BindingRegistration` read view,
   `DocumentFactsResult.quality`, `UploadDetail.analysis`) are **not** exhaustively verified field-by-field; the
   TypeScript interfaces in `frontend/src/api/types.ts` are the most complete written contract for those and were
   used as a cross-check, not as the primary source.
6. **Role classification of two edge cases relies on chain order.** `_required_permission` is a single nested
   conditional (`api.py:295-304`); I resolved it by evaluating the chain in source order. Two results are worth
   double-checking against tests if precision matters: `POST /v1/uploads/{id}/approve` is **admin** (not approver,
   because the `upload_path` branch at `:295` fires before the generic `/approvals` branch at `:304`), and
   `GET /v1/dimensions/sources` is **admin** while the other dimension GETs are role-free.
7. **`/docs`, `/redoc`, `/openapi.json` are not whitelisted** (`api.py:232`). I state that they work locally only
   via the loopback demo admin; I did not empirically probe them against a non-loopback/unauthenticated client.
8. **`/v1/model/config/test` body with no fields** — `client.ts:496` sends `{}` for `listModels`, and the API notes
   an empty object is legal for `ProfileRequest`; I did not confirm whether `PUT /v1/model/config` and
   `POST /v1/model/profiles/{id}/activate` are similarly tolerant, and `client.ts:185-201` deliberately omits the
   body for the activate call to avoid a 422 (the comment asserts this; I did not reproduce the failure).
9. **Route/method list is exact** (enumerated from the imported app), but the **ordering** of the table rows follows
   source order within each functional group rather than any external numbering; if the technical document needs
   stable endpoint IDs, they should be assigned by the document author.
10. **`web/vue/` is a built artifact** (present, rebuilt 2026-10-14 09:24). I did not verify that the current
    `frontend/src` compiles to it, nor did I run `npm run deploy`; `docs/FRONTEND_VIEWS.md:346-360` gives the
    commands (`npm run type-check`/build) but I executed none of them, so "the source matches the served bundle" is
    **not** verified by me.
11. **The frontend never sends `history` today** (verified in this source tree at the cited lines). This is a
    property of the current working tree, which contains uncommitted work (`frontend/src` mtimes 2026-10-12,
    `datamind/api.py` 2026-10-13); if the parent is describing a *different* commit, re-verify
    `frontend/src/api/types.ts:18-25` and `frontend/src/views/MainPage.vue:198-271`.
