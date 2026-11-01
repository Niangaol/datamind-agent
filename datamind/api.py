"""Local API for SQL/document agents, source files and persisted evidence."""
from __future__ import annotations

import json
import hashlib
import os
import queue
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import psycopg2
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from psycopg2.extras import RealDictCursor

from .agent import DataMindAgent, _read_dsn, _write_dsn
from .clarification import clarification_contract_for_answer
from .knowledge import COLLECTIONS, ROOT
from .pdf_publications import RegistrationRequest as PdfSnapshotRegistrationRequest
from .pdf_index import PublicationRequest as PdfKnowledgePublicationRequest
from .pdf_search import SearchRequest as PdfKnowledgeSearchRequest
from .providers import (OPENAI, PROTOCOLS, list_models as provider_list_models,
                        test_chat as provider_test_chat)
from .settings_store import (DEFAULT_BASE_URL, DEFAULT_MODEL, PROFILE_FIELDS, PROFILE_VALUE_FIELDS,
                             activate_profile, active_profile, clear_model_config, delete_profile,
                             list_profiles, load_model_config, normalize_base_url,
                             public_model_config, public_profile, public_profiles, redact_profile,
                             resolve_profile_values, save_model_config, save_profile)
from .sql_engine import normalize_history
from .visibility import (collection_is_publishable, has_public_acl, is_published,
                         public_reviewed_sql, published_document_sql)


class DocumentReference(BaseModel):
    model_config = {'extra':'forbid','strict':True}
    document_id: str = Field(min_length=1,max_length=64,pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]*$')
    document_version: int = Field(ge=1)
    source_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    parser_version: str = Field(min_length=1,max_length=64)


class Scope(BaseModel):
    model_config = {'extra':'forbid'}
    database_sources: list[str] | None = None
    document_collections: list[str] | None = None
    document_refs: list[DocumentReference] | None = Field(default=None,min_length=1,max_length=8)


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    scope: Scope | None = None
    mode: Literal["auto", "sql", "rag", "industrial"] = "auto"
    #: Optional client-supplied multi-turn history: ``[{"role": "user"|"assistant",
    #: "content": "<non-empty>"}, ...]``. The server keeps **no** session and never inherits a
    #: previous request's scope; the transcript belongs to this one request only. The type is
    #: deliberately loose (``Any``) because the contract is validated by ``normalize_history``
    #: below, which answers 422 + a stable ``HISTORY_*`` error code for a structural violation
    #: instead of a framework-shaped validation error.
    history: Any = None
    #: 可选会话归属：非空时该 run 通过 `runs.conversation_id` 关联到已有会话。格式与
    #: sessions.py 生成的 `sess-` + uuid4 hex[:24] 一致；不传时**完全不出现**在 agent
    #: kwargs 里，保证无会话调用的提示词与调用签名与多轮之前逐字节相同。
    conversation_id: str | None = Field(default=None, max_length=64,
                                        pattern=r'^sess-[0-9a-f]{24}$')


def _chat_kwargs(payload: ChatRequest) -> dict:
    """Agent keyword arguments for one chat request, with history validated first.

    ``history`` is added **only when non-empty**, so a request without history (or with ``[]``)
    calls ``DataMindAgent.run`` with exactly the pre-multi-turn keyword set — and therefore builds
    exactly the same prompts and returns the same result payload (apart from the new
    ``turns_used: 0``). A history over the contract is rejected, never truncated.

    ``conversation_id`` follows the same rule: the key is added **only when the caller supplied
    one**. An absent session must not turn into ``conversation_id=None`` in the ``run()`` kwarg
    set, or every existing call site changes shape for no reason.
    """
    try:
        history = normalize_history(payload.history)
    except ValueError as exc:
        raise HTTPException(422, detail={"error_code": str(exc)})
    kwargs = {"mode": payload.mode,
              "database_sources": payload.scope.database_sources if payload.scope else None,
              "document_collections": payload.scope.document_collections if payload.scope else None,
              "document_refs":[r.model_dump() for r in payload.scope.document_refs] if payload.scope and payload.scope.document_refs is not None else None}
    if history:
        kwargs["history"] = history
    if payload.conversation_id is not None:
        kwargs["conversation_id"] = payload.conversation_id
    return kwargs


class FactReviewRequest(BaseModel):
    model_config = {'extra':'forbid'}
    request_id: str = Field(min_length=1,max_length=64,pattern=r'^[A-Za-z0-9_-]+$')
    document_version: int = Field(ge=1)
    source_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    parser_version: str = Field(min_length=1,max_length=64)
    payload_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    previous_review_id: str | None = Field(default=None,max_length=64)
    decision: Literal['confirmed','rejected','needs_review']
    reviewer_label: str | None = Field(default=None,min_length=1,max_length=120)
    reason: str = Field(min_length=1,max_length=1000)


class FactApprovalRequest(BaseModel):
    model_config = {'extra':'forbid'}
    request_id: str = Field(min_length=1,max_length=64,pattern=r'^[A-Za-z0-9_-]+$')
    document_version: int = Field(ge=1)
    source_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    parser_version: str = Field(min_length=1,max_length=64)
    payload_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    review_id: str = Field(min_length=1,max_length=64)
    previous_approval_id: str | None = Field(default=None,max_length=64)
    decision: Literal['approved','rejected','revoked']
    reason: str = Field(min_length=1,max_length=1000)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1,max_length=64)
    password: str = Field(min_length=1,max_length=256)


class DrawingRegistrationRequest(BaseModel):
    model_config = {'extra':'forbid','strict':True}
    request_id: str = Field(min_length=1,max_length=64,pattern=r'^[A-Za-z0-9_-]+$')
    document_id: str = Field(min_length=1,max_length=64,pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]*$')
    title: str = Field(min_length=1,max_length=200)
    source_path: str = Field(min_length=1,max_length=500)
    source_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    provenance: Literal['synthetic_demo','authorized_external']
    previous_version: int = Field(ge=0)
    previous_source_sha256: str | None = Field(pattern=r'^[a-f0-9]{64}$')


class AuthorityBasis(BaseModel):
    model_config = {'extra':'forbid'}
    document_id: str = Field(min_length=1,max_length=64)
    document_version: int = Field(ge=1)
    source_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    page_no: int = Field(ge=1)


class DrawingAssociationReviewRequest(BaseModel):
    model_config = {'extra':'forbid','strict':True}
    request_id: str = Field(min_length=1,max_length=64,pattern=r'^[A-Za-z0-9_-]+$')
    document_version: int = Field(ge=1)
    source_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    parser_version: str = Field(min_length=1,max_length=64)
    bundle_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    feature_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    previous_review_id: str | None = Field(default=None,max_length=64)
    decision: Literal['confirmed','rejected','needs_review']
    selected_target: str | None = Field(default=None,pattern=r'^[1-9A-F][0-9A-F]*$',max_length=32)
    reason: str = Field(min_length=1,max_length=1000)


class AuthorityRequest(BaseModel):
    model_config = {'extra':'forbid'}
    request_id: str = Field(min_length=1,max_length=64,pattern=r'^[A-Za-z0-9_-]+$')
    question: str = Field(min_length=1,max_length=2000)
    measurement_id: str = Field(min_length=1,max_length=64)
    candidate_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    selected_source: Literal['production','report']
    valid_from: str = Field(min_length=1,max_length=50)
    valid_to: str = Field(min_length=1,max_length=50)
    basis: AuthorityBasis
    rationale: str = Field(min_length=10,max_length=1000)


class AuthorityEventRequest(BaseModel):
    model_config = {'extra':'forbid'}
    request_id: str = Field(min_length=1,max_length=64,pattern=r'^[A-Za-z0-9_-]+$')
    previous_event_id: str | None = Field(default=None,max_length=64)
    decision: Literal['approved','rejected','revoked']
    reason: str = Field(min_length=1,max_length=1000)


class ProfileRequest(BaseModel):
    """接入配置（provider profile）；protocol 决定走 OpenAI 兼容还是 Anthropic 协议。"""
    id: str | None = Field(default=None, max_length=64)
    profile_id: str | None = Field(default=None, max_length=64)
    name: str | None = Field(default=None, max_length=120)
    protocol: str | None = Field(default=None, max_length=24)
    base_url: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=200)
    api_key: str | None = Field(default=None, max_length=500)
    max_tokens: int | None = Field(default=None, ge=0, le=200000)
    timeout_s: float | None = Field(default=None, ge=0, le=600)
    auth_style: str | None = Field(default=None, max_length=24)
    headers: dict | None = None
    proxy: str | None = Field(default=None, max_length=300)
    note: str | None = Field(default=None, max_length=500)
    activate: bool | None = None


class ProfileImportRequest(BaseModel):
    profiles: list[dict]
    activate_first: bool = False


class SessionCreateRequest(BaseModel):
    """新建会话：标题可省略，省略即用服务端默认标题（首轮提问会自动命名）。"""
    model_config = {'extra':'forbid'}
    title: str | None = Field(default=None, max_length=200)


class SessionRenameRequest(BaseModel):
    """重命名会话：标题必填；全空白由 sessions.py 归一化后按 SESSION_TITLE_REQUIRED 拒绝。"""
    model_config = {'extra':'forbid'}
    title: str = Field(min_length=1, max_length=200)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Validate plugin config and discover external plugins before serving a request.

    Plugin discovery is not left to the first upload or the first ``GET /v1/parsers``: a
    bad ``DATAMIND_PLUGIN_CONFIG`` (or a plugin path that is not a directory) raises
    :class:`~datamind.plugin.ConfigError` here, so the process refuses to start instead of
    answering an unlabelled 500 on the first request that needs a parser. The import stays
    inside the function and the call is made here (not at module import), which keeps
    ``import datamind.api`` as light as it was (G7): discovery imports the external modules
    an operator configured, and nothing else.
    """
    from ingestion.plugins import ensure_loaded
    ensure_loaded()
    yield


app = FastAPI(title="DataMind Agent", version="0.39.0", lifespan=_lifespan)


# ---------- request authorisation surface (default deny) ----------
#
# The middleware below no longer enumerates "protected prefixes" and lets everything else
# through: that is how POST /v1/chat, POST /v1/chat/stream and POST /v1/diagnostics/sql
# ended up with no identity, CSRF or origin check at all. Now only the routes listed here
# are reachable anonymously; every other route needs an identity, and every mutation also
# needs CSRF + a same-origin request.

PUBLIC_PATHS = frozenset({'/', '/health', '/v1/auth/login', '/v1/auth/me'})
#: Public reads: the document/run viewers stay usable without a session (their payloads are
#: ACL-filtered to public documents); static assets are the page itself.
PUBLIC_READ_PREFIXES = ('/v1/documents', '/v1/runs', '/assets/')
#: Reads that expose credentials or connection targets; a browser on another origin must
#: not be able to read them, so they carry the same origin check as mutations do.
SENSITIVE_READ_PREFIXES = ('/v1/model/profiles', '/v1/model/config')
#: Anonymous writes, opt-in via DATAMIND_PUBLIC_CHAT=1, for sharing a read-only demo over a
#: LAN address or tunnel. Without it a remote visitor reaches the page but every question
#: fails with 401 LOGIN_REQUIRED: the loopback demo identity does not apply to them, and the
#: repository provisions no accounts. Only the Q&A entry points are listed — everything that
#: changes state (model config, authority rules, uploads, snapshots, approvals) keeps
#: requiring a session, and these two stay same-origin checked below because an anonymous
#: caller has no session to bind a CSRF token to.
PUBLIC_WRITE_PATHS = frozenset({'/v1/chat', '/v1/chat/stream'})
_NO_STORE_PREFIXES = ('/v1/pdf-snapshots', '/v1/pdf-knowledge', '/v1/drawings',
                      '/v1/dimensions', '/v1/files', '/v1/uploads', '/v1/plans',
                      '/v1/model/profiles', '/v1/model/config',
                      # 解析插件清单是**进程本地**的（发现路径与 DATAMIND_PLUGIN_CONFIG 都按进程读），
                      # 且 `loaded_from` 是绝对路径：缓存一份就会把某个 worker 的答案当成全局事实，
                      # 也会把部署目录结构留在共享缓存里，所以永不缓存。
                      '/v1/parsers',
                      # 会话列表/详情就是聊天记录本身（问题与答案），一旦被浏览器或中间代理
                      # 缓存，下一位使用同一台机器的用户就能读到别人的对话，因此永不缓存。
                      '/v1/sessions',
                      # Wiki 词条正文、证据引用定位与运行摘要都是私有内容
                      # （`docs/WIKI_API.md` §2.3）：成功与失败的响应都必须 no-store，
                      # 导出另带 `Content-Disposition: attachment`（由服务层给）。
                      '/v1/wiki',
                      # 根路径的 index.html 是**指向内容哈希产物的指针**：`npm run deploy`
                      # 带 --emptyOutDir，每次重建都会删掉旧哈希文件。若浏览器按启发式缓存
                      # 住旧 index.html，它就会去请求已被删除的旧 bundle → 404 → 白屏，
                      # 或者继续跑旧代码（表现为「界面上没有新功能的按钮」）。所以入口
                      # HTML 必须每次回源校验；带哈希的产物本身可以放心长期缓存。
                      # 注意 '/' 在这里只会命中根路径：_no_store_path 用 prefix+'/' 匹配，
                      # 即 '//'，正常路径不会以它开头。
                      '/')


def _public_chat_enabled() -> bool:
    value = (os.environ.get('DATAMIND_PUBLIC_CHAT') or '').strip().lower()
    return value not in {'', '0', 'false', 'no', 'off'}


def _is_public_write(path: str) -> bool:
    return _public_chat_enabled() and path in PUBLIC_WRITE_PATHS


def _is_public_request(request) -> bool:
    path = request.url.path
    if path in PUBLIC_PATHS:
        return True
    if request.method not in {'GET','HEAD','OPTIONS'}:
        return _is_public_write(path)
    return path.startswith(PUBLIC_READ_PREFIXES)


def _no_store_path(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + '/') for prefix in _NO_STORE_PREFIXES)


#: `/v1/wiki/*` 的五条**写**路由：完整路径形状（`*` = 一个路径段，不是后缀匹配）→ 契约
#: `docs/WIKI_API.md` §3.1 矩阵里的权限字符串。逐条列名的理由是实测缺陷：写路由里只有
#: #13 恰好命中下面 `POST + /reviews` 的通用兜底，`#9`/`#11`/`#12` 和以 `/publications`
#: 结尾的 **#14 全部落到 `None`（= "任意已登录身份"）**，也就是默认放开。`reviewer` 与
#: `approver` 互不继承（`auth.py:195` 只给 `admin` 旁路），所以这里不能互相兜底。
WIKI_WRITE_PERMISSIONS = (
    (('v1', 'wiki', 'builds'), 'reviewer'),                       # #9  POST /v1/wiki/builds
    (('v1', 'wiki', 'builds', '*', 'retry'), 'reviewer'),         # #11 POST /v1/wiki/builds/{id}/retry
    (('v1', 'wiki', 'entries', '*', 'revisions'), 'reviewer'),    # #12 POST /v1/wiki/entries/{id}/revisions
    (('v1', 'wiki', 'entries', '*', 'reviews'), 'reviewer'),      # #13 POST /v1/wiki/entries/{id}/reviews
    (('v1', 'wiki', 'entries', '*', 'publications'), 'approver'),  # #14 POST /v1/wiki/entries/{id}/publications
)


def _wiki_write_permission(path: str) -> str | None:
    """`/v1/wiki/*` 写路由的权限；没有列名的路径返回 `None`（由调用方兜底成 `admin`）。

    `*` 只吃**非空**路径段（空段不是路径段）：形状不匹配就当作"没列名"，落到默认拒绝。
    """
    segments=path.strip('/').split('/')
    for route_segments,permission in WIKI_WRITE_PERMISSIONS:
        if len(segments)==len(route_segments) and all(
                actual and (expected=='*' or expected==actual)
                for expected,actual in zip(route_segments,segments)):
            return permission
    return None


def _required_permission(request) -> str | None:
    path=request.url.path
    authority_path=path.startswith('/v1/authority/')
    cad_path=path=='/v1/drawings' or path.startswith('/v1/drawings/')
    dimension_path=path.startswith('/v1/dimensions/')
    files_path=path.startswith('/v1/files/')
    upload_path=path=='/v1/uploads' or path.startswith('/v1/uploads/')
    pdf_snapshot_path=path=='/v1/pdf-snapshots' or path.startswith('/v1/pdf-snapshots/')
    pdf_knowledge_path=path=='/v1/pdf-knowledge' or path.startswith('/v1/pdf-knowledge/')
    plan_path=path.startswith('/v1/plans')
    wiki_path=path=='/v1/wiki' or path.startswith('/v1/wiki/')
    # 解析插件清单是只读的，但它暴露 `loaded_from` 绝对路径（部署目录结构），所以和模型配置
    # 一样只给 `admin`；放在通用兜底之前，避免被任何后缀规则意外接住。
    parser_path=path=='/v1/parsers' or path.startswith('/v1/parsers/')
    # 契约 §2/§3.1：`/v1/wiki/*` 的五条写路由走上表；**读接口不做角色门**（落到最后的
    # `None` = 任意已登录身份，与 `/v1/sessions` 同款）。没被上表列名的 wiki 写方法落到
    # 下面的 `admin` 兜底而不是"默认放开"，且放在两条通用后缀兜底之前，避免被
    # `/reviews` 之类的规则意外接住——漏一条规则必须是误拒，绝不能是越权。
    wiki_write=_wiki_write_permission(path) if request.method=='POST' else None
    return (wiki_write if wiki_write else
            'admin' if wiki_path and request.method=='POST' else
            'admin' if plan_path or files_path or upload_path or pdf_snapshot_path or
            pdf_knowledge_path or parser_path else
            'approver' if dimension_path and request.method=='POST' and path.endswith('/approvals') else
            'admin' if dimension_path and (request.method=='POST' or path.endswith('/sources')) else
            'reviewer' if cad_path and request.method=='POST' and path.endswith('/reviews') else
            'admin' if path=='/v1/drawings/sources' or (cad_path and request.method=='POST') else
            'admin' if path.startswith('/v1/model/') or path=='/v1/authority/candidates' or
            (authority_path and request.method=='POST' and path.endswith('/rules')) else
            'approver' if authority_path and request.method=='POST' and path.endswith('/events') else
            'reviewer' if request.method=='POST' and path.endswith('/reviews') else
            'approver' if request.method=='POST' and path.endswith('/approvals') else None)


@app.middleware('http')
async def identity_permissions(request: Request, call_next):
    from .auth import current,origin_check
    path=request.url.path
    read_only=request.method in {'GET','HEAD','OPTIONS'}
    no_store=_no_store_path(path)
    if not _is_public_request(request):
        try:
            if read_only and path.startswith(SENSITIVE_READ_PREFIXES):
                origin_check(request)
            request.state.identity=await run_in_threadpool(current,request,_write_dsn(),required=True,
                permission=_required_permission(request),mutation=not read_only,
                # A GET must never provision the demo user: reads do not write identity rows.
                provision_demo=not read_only)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code,content={'detail':exc.detail},
                                headers={'Cache-Control':'no-store'} if no_store else None)
    elif not read_only and _is_public_write(path):
        # Anonymous Q&A still has to be same-origin: there is no session here to bind a CSRF
        # token to, so the origin allow-list is the only thing stopping another site from
        # driving this endpoint through a visitor's browser.
        try:
            origin_check(request)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code,content={'detail':exc.detail})
    response=await call_next(request)
    if no_store:response.headers['Cache-Control']='no-store'
    return response


def _pdf_snapshot_recheck(request):
    # File verification can outlive the middleware's initial identity check.
    from .auth import current
    current(request,_write_dsn(),required=True,permission='admin',provision_demo=False)


def _admin_recheck(request):
    # Long-running work can outlive the middleware's initial identity check.
    from .auth import current
    current(request,_write_dsn(),required=True,permission='admin',provision_demo=False)


@app.post('/v1/pdf-snapshots')
def pdf_snapshot_register(payload: PdfSnapshotRegistrationRequest,request: Request):
    from .pdf_publications import register
    version=jsonable_encoder(register(payload.model_dump(),_write_dsn(),request.state.identity))
    _pdf_snapshot_recheck(request)
    return {'version':version,
            'semantics':'Registered parsing snapshot; not indexed, knowledge-visible, or source-authority approval'}


@app.get('/v1/pdf-snapshots')
def pdf_snapshots(request: Request,limit: int=Query(default=20,ge=1,le=50),offset: int=Query(default=0,ge=0,le=10000)):
    from .pdf_publications import list_assets
    result=jsonable_encoder(list_assets(_write_dsn(),limit=limit,offset=offset))
    _pdf_snapshot_recheck(request)
    return result


@app.get('/v1/pdf-snapshots/{document_id}/versions')
def pdf_snapshot_versions(document_id: str,request: Request,limit: int=Query(default=20,ge=1,le=50),
                          offset: int=Query(default=0,ge=0,le=10000)):
    from .pdf_publications import history
    result=jsonable_encoder(history(document_id,_write_dsn(),limit=limit,offset=offset))
    _pdf_snapshot_recheck(request)
    return result


@app.get('/v1/pdf-snapshots/{document_id}')
def pdf_snapshot_detail(document_id: str,request: Request,version: int | None=Query(default=None,ge=1)):
    from .pdf_publications import read_version,public_version
    version_record=jsonable_encoder(public_version(read_version(document_id,_write_dsn(),version)))
    _pdf_snapshot_recheck(request)
    return {'version':version_record}


def _pdf_snapshot_selected(document_id,version,expected_manifest_sha256):
    from .pdf_publications import read_version
    row=read_version(document_id,_write_dsn(),version)
    if row['manifest_sha256']!=expected_manifest_sha256:
        raise HTTPException(409,detail={'error_code':'PDF_SNAPSHOT_SELECTION_CHANGED'})
    return row


@app.get('/v1/pdf-snapshots/{document_id}/file')
def pdf_snapshot_file(document_id: str,request: Request,version: int=Query(ge=1),
                      expected_manifest_sha256: str=Query(pattern=r'^[a-f0-9]{64}$')):
    from .pdf_publications import read_artifact
    row=_pdf_snapshot_selected(document_id,version,expected_manifest_sha256)
    data=read_artifact(row,'source')
    _pdf_snapshot_recheck(request)
    return Response(data,media_type='application/pdf',
                    headers={'Content-Disposition':f'attachment; filename="document-v{row["document_version"]}.pdf"',
                             'X-Content-Type-Options':'nosniff','Content-Security-Policy':"sandbox; default-src 'none'"})


@app.get('/v1/pdf-snapshots/{document_id}/pages/{page_no}')
def pdf_snapshot_page(document_id: str,page_no: int,request: Request,version: int=Query(ge=1),
                      expected_manifest_sha256: str=Query(pattern=r'^[a-f0-9]{64}$')):
    from .pdf_publications import read_artifact
    row=_pdf_snapshot_selected(document_id,version,expected_manifest_sha256)
    data=read_artifact(row,'page_image',page_no)
    _pdf_snapshot_recheck(request)
    return Response(data,media_type='image/png',
                    headers={'X-Content-Type-Options':'nosniff'})


@app.get('/v1/pdf-snapshots/{document_id}/bundle')
def pdf_snapshot_bundle(document_id: str,request: Request,version: int=Query(ge=1),
                        expected_manifest_sha256: str=Query(pattern=r'^[a-f0-9]{64}$')):
    from .pdf_publications import read_artifact,public_version
    row=_pdf_snapshot_selected(document_id,version,expected_manifest_sha256)
    result={'version':jsonable_encoder(public_version(row)),
            'bundle':json.loads(read_artifact(row,'bundle'))}
    _pdf_snapshot_recheck(request)
    return result


@app.post('/v1/pdf-knowledge/publications')
def pdf_knowledge_publish(payload: PdfKnowledgePublicationRequest,request: Request):
    from .pdf_index import submit
    result=jsonable_encoder(submit(payload.model_dump(),_write_dsn(),request.state.identity))
    _pdf_snapshot_recheck(request)
    return {'publication':result}


@app.get('/v1/pdf-knowledge/publications')
def pdf_knowledge_publications(request: Request,limit: int=Query(default=20,ge=1,le=50),
                               offset: int=Query(default=0,ge=0,le=10000)):
    from .pdf_index import listing
    result=jsonable_encoder(listing(_write_dsn(),limit=limit,offset=offset))
    _pdf_snapshot_recheck(request)
    return result


@app.get('/v1/pdf-knowledge/publications/{publication_id}')
def pdf_knowledge_publication(publication_id: str,request: Request):
    from .pdf_index import poll
    result=jsonable_encoder(poll(publication_id,_write_dsn()))
    _pdf_snapshot_recheck(request)
    return {'publication':result}


@app.post('/v1/pdf-knowledge/publications/{publication_id}/retry')
def pdf_knowledge_retry(publication_id: str,request: Request):
    from .pdf_index import retry
    result=jsonable_encoder(retry(publication_id,_write_dsn(),request.state.identity))
    _pdf_snapshot_recheck(request)
    return {'publication':result}


@app.get('/v1/pdf-knowledge/publications/{publication_id}/chunks')
def pdf_knowledge_chunks(publication_id: str,request: Request,
                          expected_plan_sha256: str=Query(pattern=r'^[a-f0-9]{64}$')):
    from .pdf_index import read_chunks
    result=jsonable_encoder(read_chunks(publication_id,_write_dsn()))
    if result['publication']['plan_sha256']!=expected_plan_sha256:
        raise HTTPException(409,detail={'error_code':'PDF_INDEX_SELECTION_CHANGED'})
    _pdf_snapshot_recheck(request)
    return result


@app.get('/v1/pdf-knowledge/documents/{document_id}')
def pdf_knowledge_current(document_id: str,request: Request):
    from .pdf_index import current
    result=jsonable_encoder(current(document_id,_write_dsn()))
    _pdf_snapshot_recheck(request)
    return {'publication':result}


@app.post('/v1/pdf-knowledge/search')
def pdf_knowledge_search(payload: PdfKnowledgeSearchRequest, request: Request):
    from .pdf_search import search
    result = jsonable_encoder(search(payload.model_dump(), _write_dsn()))
    _pdf_snapshot_recheck(request)
    return result


@app.post('/v1/pdf-knowledge/answer')
def pdf_knowledge_answer(payload: PdfKnowledgeSearchRequest, request: Request):
    from .pdf_answer import answer
    result = jsonable_encoder(answer(payload.model_dump(), _write_dsn()))
    _pdf_snapshot_recheck(request)
    return result


@app.post('/v1/plans/execute')
def vcar_plan_execute(payload: dict, request: Request):
    from .vcar import run_request
    result = jsonable_encoder(run_request(payload, _write_dsn()))
    _admin_recheck(request)
    return result


@app.post('/v1/pdf-knowledge/publications/{publication_id}/repair')
def pdf_knowledge_repair(publication_id: str, request: Request):
    from .pdf_maintenance import repair_publication
    result = jsonable_encoder(repair_publication(publication_id, _write_dsn()))
    _pdf_snapshot_recheck(request)
    return result


@app.get('/v1/pdf-knowledge/orphans')
def pdf_knowledge_orphans(request: Request):
    from .pdf_maintenance import orphan_scan
    result = jsonable_encoder(orphan_scan(_write_dsn()))
    _pdf_snapshot_recheck(request)
    return result


@app.post('/v1/pdf-knowledge/orphans/reclaim')
def pdf_knowledge_reclaim(payload: dict, request: Request):
    from .pdf_maintenance import reclaim_orphans, reclaim_payload
    body = reclaim_payload(payload)
    result = jsonable_encoder(reclaim_orphans(body['collection'], _write_dsn(), confirm=body['confirm']))
    _pdf_snapshot_recheck(request)
    return result


@app.get('/v1/drawings/sources')
def drawing_sources():
    from .cad_store import sources
    return {'sources':sources()}


@app.post('/v1/drawings')
def register_drawing(payload: DrawingRegistrationRequest,request: Request):
    from .cad_store import register
    return {'version':jsonable_encoder(register(payload.model_dump(),_write_dsn(),request.state.identity)),
            'semantics':'Versioned CAD inspection registration; not formal approval or production binding'}


@app.get('/v1/drawings')
def drawings():
    from .cad_store import list_assets
    return {'drawings':jsonable_encoder(list_assets(_read_dsn()))}


@app.get('/v1/drawings/{document_id}/versions')
def drawing_versions(document_id: str):
    from .cad_store import history
    return jsonable_encoder(history(document_id,_read_dsn()))


@app.get('/v1/drawings/{document_id}')
def drawing_detail(document_id: str,version: int | None=Query(default=None,ge=1)):
    from .cad_store import read_version,public_version
    row=read_version(document_id,_read_dsn(),version)
    return {'version':jsonable_encoder(public_version(row)),'source':row['bundle']['snapshot']['source'],
            'identity':row['bundle']['snapshot']['identity'],
            'metadata':{k:v for k,v in row['bundle']['metadata'].items() if k!='render_entities'}}


@app.get('/v1/drawings/{document_id}/entities')
def drawing_entities(document_id: str,version: int | None=Query(default=None,ge=1)):
    from .cad_store import read_version,public_version
    row=read_version(document_id,_read_dsn(),version)
    snapshot=row['bundle']['snapshot']
    return {'version':jsonable_encoder(public_version(row)),'entities':snapshot['entities'],'views':snapshot['views']}


@app.get('/v1/drawings/{document_id}/features')
def drawing_features(document_id: str,version: int | None=Query(default=None,ge=1)):
    from .cad_store import read_version,public_version
    from .cad_reviews import inspect
    from ingestion.cad_contract import eligible_features
    row=read_version(document_id,_read_dsn(),version);snapshot=row['bundle']['snapshot']
    context={k:f['value'] for k,f in snapshot['identity'].items()};context['source_sha256']=row['source_sha256']
    return {'version':jsonable_encoder(public_version(row)),'features':snapshot['features'],
            'inspection_gate':eligible_features(snapshot,context),
            **jsonable_encoder(inspect(row,_read_dsn())),
            'semantics':'Original extraction/object consistency; association reviews are separate, without formal approval or production binding'}


@app.get('/v1/drawings/{document_id}/features/{feature_id}/reviews')
def drawing_review_history(document_id: str,feature_id: str,version: int | None=Query(default=None,ge=1)):
    from .cad_store import read_version
    from .cad_reviews import history
    return jsonable_encoder(history(read_version(document_id,_read_dsn(),version),feature_id,_read_dsn()))


@app.post('/v1/drawings/{document_id}/features/{feature_id}/reviews')
def drawing_review_record(document_id: str,feature_id: str,payload: DrawingAssociationReviewRequest,request: Request):
    from .cad_reviews import record
    return {'review':jsonable_encoder(record(document_id,feature_id,payload.model_dump(),_write_dsn(),request.state.identity))}


@app.get('/v1/drawings/{document_id}/file')
def drawing_file(document_id: str,version: int | None=Query(default=None,ge=1)):
    from .cad_store import read_version
    row=read_version(document_id,_read_dsn(),version)
    return Response(row['_source_bytes'],media_type='application/dxf',
                    headers={'Content-Disposition':f'attachment; filename="drawing-v{row["document_version"]}.dxf"',
                             'X-Content-Type-Options':'nosniff'})


@app.get('/v1/drawings/{document_id}/preview')
def drawing_preview(document_id: str,version: int | None=Query(default=None,ge=1)):
    from .cad_store import read_version
    from ingestion.cad_render import render_svg
    row=read_version(document_id,_read_dsn(),version)
    return Response(render_svg(row['bundle']),media_type='image/svg+xml',
                    headers={'Content-Security-Policy':"sandbox; default-src 'none'; style-src 'unsafe-inline'",
                             'X-Content-Type-Options':'nosniff'})


@app.get('/v1/drawings/{document_id}/viewer')
def drawing_viewer(document_id: str,version: int | None=Query(default=None,ge=1)):
    from .cad_store import read_version
    from ingestion.cad_render import render_html
    row=read_version(document_id,_read_dsn(),version)
    return Response(render_html(row['bundle']),media_type='text/html',
                    headers={'Content-Security-Policy':"sandbox allow-scripts; default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'",
                             'X-Content-Type-Options':'nosniff'})


from .cad_dimensions import MeasurementRegistration,BindingRegistration,ApprovalRequest,ComparisonRequest


@app.get('/v1/dimensions/sources')
def dimension_sources():
    from .cad_dimensions import sources
    return {'sources':sources()}


@app.get('/v1/dimensions/measurements')
def dimension_measurements():
    from .cad_dimensions import listing
    return listing('measurements',_read_dsn())


@app.post('/v1/dimensions/measurements')
def dimension_measurement_register(payload: MeasurementRegistration,request: Request):
    from .cad_dimensions import register_measurement
    return {'measurement':register_measurement(payload.model_dump(),_write_dsn(),request.state.identity)}


@app.get('/v1/dimensions/measurements/{measurement_id}')
def dimension_measurement_detail(measurement_id: str,version: int | None=Query(default=None,ge=1)):
    from .cad_dimensions import read_measurement
    return {'measurement':read_measurement(measurement_id,_read_dsn(),version)}


@app.get('/v1/dimensions/bindings')
def dimension_bindings():
    from .cad_dimensions import listing
    return listing('bindings',_read_dsn())


@app.post('/v1/dimensions/bindings')
def dimension_binding_register(payload: BindingRegistration,request: Request):
    from .cad_dimensions import register_binding
    return {'binding':register_binding(payload.model_dump(),_write_dsn(),request.state.identity)}


@app.get('/v1/dimensions/bindings/{binding_id}')
def dimension_binding_detail(binding_id: str,version: int | None=Query(default=None,ge=1)):
    from .cad_dimensions import read_binding
    return read_binding(binding_id,_read_dsn(),version)


@app.post('/v1/dimensions/bindings/{binding_id}/approvals')
def dimension_approval_register(binding_id: str,payload: ApprovalRequest,request: Request):
    from .cad_dimensions import record_approval
    return {'approval':record_approval(binding_id,payload.model_dump(),_write_dsn(),request.state.identity)}


@app.post('/v1/dimensions/bindings/{binding_id}/comparisons')
def dimension_comparison_register(binding_id: str,payload: ComparisonRequest,request: Request):
    from .cad_dimensions import comparison
    return comparison(binding_id,payload.model_dump(),_write_dsn(),request.state.identity)


@app.get('/v1/dimensions/bindings/{binding_id}/comparisons')
def dimension_comparison_history(binding_id: str,offset: int=Query(default=0,ge=0,le=10000),
                                 limit: int=Query(default=20,ge=1,le=50)):
    from .cad_dimensions import comparison_history
    return comparison_history(binding_id,_read_dsn(),offset,limit)


@app.get('/v1/dimensions/comparisons/{comparison_id}')
def dimension_comparison_replay(comparison_id: str):
    from .cad_dimensions import replay
    return replay(comparison_id,_read_dsn())


from .file_jobs import FileJobRequest


@app.post('/v1/files/uploads')
async def file_upload(request: Request,filename: str=Query(min_length=1,max_length=200),
                      provenance: Literal['synthetic_demo','authorized_external']=Query()):
    from .file_uploads import stage
    return {'source':await stage(request,filename,provenance,_write_dsn(),request.state.identity)}


@app.post('/v1/files/jobs')
def file_job_submit(payload: FileJobRequest,request: Request):
    from .file_jobs import submit
    return submit(payload.model_dump(),_write_dsn(),request.state.identity)


@app.get('/v1/files/jobs')
def file_job_listing(request: Request):
    from .file_jobs import listing
    return {'jobs':listing(_write_dsn(),request.state.identity)}


@app.get('/v1/files/jobs/{job_id}')
def file_job_detail(job_id: str,request: Request):
    from .file_jobs import poll
    return poll(job_id,_write_dsn(),request.state.identity)


@app.post('/v1/files/jobs/{job_id}/cancel')
def file_job_cancel(job_id: str,request: Request):
    from .file_jobs import cancel
    return cancel(job_id,_write_dsn(),request.state.identity)


@app.post('/v1/files/jobs/{job_id}/retry')
def file_job_retry(job_id: str,request: Request):
    from .file_jobs import retry
    return retry(job_id,_write_dsn(),request.state.identity)


# ---------- upload pipeline: stage -> queue -> contract store -> review gate ----------
# All three surfaces are admin-only (see `_required_permission`); the POSTs additionally
# carry CSRF and a same-origin check from the shared middleware. Parsing is asynchronous
# **and no route drives it** (review 5.1): the standalone worker
# (`python -m ingestion.upload_worker --watch|--once`) advances queued jobs, so closing a
# page cannot stall one. `GET /v1/uploads` reports `worker.alive`/`worker.last_seen_at` so
# a client can honestly say the background processor is not running instead of rendering a
# `queued` job as "解析中". A parsed upload stays unretrievable (`needs_review`,
# non-`indexed`) until `POST /v1/uploads/{upload_id}/approve`.

@app.post('/v1/uploads', status_code=201)
async def upload_create(request: Request,filename: str=Query(min_length=1,max_length=200),
                        provenance: Literal['synthetic_demo','authorized_external']=Query()):
    from ingestion import upload_pipeline
    return await upload_pipeline.create(request,filename,provenance,_write_dsn(),request.state.identity)


@app.get('/v1/uploads')
def upload_list(status: str | None=Query(default=None,max_length=32),
                media_type: str | None=Query(default=None,max_length=120),
                since: str | None=Query(default=None,max_length=40),
                limit: int=Query(default=50,ge=1,le=200)):
    from ingestion import upload_pipeline
    return jsonable_encoder(upload_pipeline.listing(_write_dsn(),status=status,
                                                    media_type=media_type,since=since,limit=limit))


@app.get('/v1/uploads/{upload_id}')
def upload_detail(upload_id: str,request: Request):
    from ingestion import upload_pipeline
    # Pure read (review 5.1): a GET must not parse, claim or write anything at all. The
    # job's progress is owned by the standalone worker; this endpoint only reports it —
    # including `worker.alive`, so the polling client can honestly say the background
    # processor is not running instead of waiting on a queue nothing is draining.
    return jsonable_encoder(upload_pipeline.detail(upload_id,_write_dsn(),with_worker=True))


@app.post('/v1/uploads/{upload_id}/approve')
def upload_approve(upload_id: str,request: Request):
    from ingestion import upload_pipeline
    return jsonable_encoder(upload_pipeline.approve(upload_id,_write_dsn(),request.state.identity))


# 只读的解析插件清单（admin-only，见 `_required_permission`）。它报告的是**本进程**装了
# 什么、从哪加载、是否启用、是否同时是契约适配器；不解析 loader（G7），因此内置插件的
# `pymupdf`/`ezdxf`/`python-docx` 都不会被拉进 API 进程。这条保证只覆盖内置插件：由
# `DATAMIND_PLUGIN_PATH` 发现的外部插件模块是由发现流程本身（`ensure_loaded()`）导入的，
# 而发现已在进程启动时（`_lifespan`）跑过一次，所以运维放进来的代码在服务开始接受请求
# 之前就已经执行过了，不是在这个端点的第一次请求时。
@app.get('/v1/parsers')
def parsers_listing():
    """Process-local parser inventory; each process reports its own plugins.

    Annotated `process_local: true`: discovery and DATAMIND_PLUGIN_CONFIG are read per
    process, so two workers can answer differently.
    """
    from ingestion.plugins import parser_inventory
    return {'process_local': True, 'parsers': parser_inventory()}


@app.get('/v1/auth/me')
def auth_me(request: Request,response: Response):
    from .auth import current
    # F07: this is a GET, so it must not INSERT the demo user row; the demo identity is
    # a static view and session lookups stay on the write DSN because readonly_user is
    # deliberately granted no access to auth_users/auth_sessions.
    identity=current(request,_write_dsn(),provision_demo=False)
    response.headers['Cache-Control']='no-store'
    return {'user':{k:v for k,v in identity.items() if k not in {'csrf_token','_session_hash','_demo'}} if identity else None,
            'csrf_token':identity['csrf_token'] if identity else None}


@app.post('/v1/auth/login')
def auth_login(payload: LoginRequest,request: Request,response: Response):
    from .auth import login,COOKIE,SESSION_SECONDS
    token,user,csrf=login(request,payload.username,payload.password,_write_dsn())
    response.set_cookie(COOKIE,token,max_age=SESSION_SECONDS,httponly=True,samesite='strict',
                        secure=request.url.scheme=='https',path='/')
    response.headers['Cache-Control']='no-store'
    return {'user':user,'csrf_token':csrf}


@app.post('/v1/auth/logout')
def auth_logout(request: Request,response: Response):
    from .auth import logout,COOKIE
    logout(request,_write_dsn());response.delete_cookie(COOKIE,path='/')
    return {'status':'signed_out'}


@app.get('/v1/authority/candidates')
def authority_candidates(question: str = Query(min_length=1,max_length=2000)):
    from .authority import candidates
    try:return jsonable_encoder({'candidates':candidates(question,_read_dsn())})
    except ValueError as exc:raise HTTPException(422,detail={'error_code':str(exc)})


@app.get('/v1/authority/rules')
def authority_rules():
    from .authority import list_rules
    return jsonable_encoder({'rules':list_rules(_read_dsn())})


@app.post('/v1/authority/rules')
def authority_register(payload: AuthorityRequest,request: Request):
    from .authority import register
    try:rule=register(payload.model_dump(),request.state.identity,_write_dsn())
    except ValueError as exc:raise HTTPException(422,detail={'error_code':str(exc)})
    return jsonable_encoder({'rule':rule})


@app.get('/v1/authority/rules/{rule_id}/events')
def authority_history(rule_id: str):
    from .authority import history
    return jsonable_encoder({'events':history(rule_id,_read_dsn())})


@app.post('/v1/authority/rules/{rule_id}/events')
def authority_activate(rule_id: str,payload: AuthorityEventRequest,request: Request):
    from .authority import activation
    return jsonable_encoder({'event':activation(rule_id,payload.model_dump(),request.state.identity,_write_dsn())})
WEB_DIR = Path(__file__).resolve().parents[1] / "web"
# 对外入口是新前端（Vue）的构建产物：在 frontend/ 执行 `npm run deploy` 生成到 web/vue/。
# 首次构建前目录可能缺失，所以这里先确保目录存在——StaticFiles 默认 check_dir=True，
# 目录缺失会在 **import 期** 抛 RuntimeError，连带后端起不来、全部 API 测试收集失败。
VUE_DIR = WEB_DIR / "vue"
VUE_DIR.mkdir(parents=True, exist_ok=True)
VUE_INDEX = VUE_DIR / "index.html"
# 静态资源仅提供 Vue 构建目录；旧单页前端已退役并从工作区移除。
app.mount("/assets/vue", StaticFiles(directory=VUE_DIR), name="vue")


@app.get("/")
def index():
    if not VUE_INDEX.is_file():
        return JSONResponse(
            {'error_code': 'FRONTEND_NOT_BUILT',
             'detail': '前端产物缺失：请在 frontend/ 执行 npm install && npm run deploy'},
            status_code=503)
    return FileResponse(VUE_INDEX)


def _vector_store_status() -> str:
    """Probe Qdrant's readiness endpoint; any failure degrades, never raises.

    /health is the liveness/readiness signal the start scripts poll, so a vector-store
    outage must surface as a field value, not as a 500.
    """
    import urllib.request
    base = (os.environ.get('DATAMIND_QDRANT_URL') or 'http://127.0.0.1:6333').strip().rstrip('/')
    if '://' not in base:
        base = 'http://' + base
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(base + '/readyz', timeout=1.0) as response:
            return 'ready' if 200 <= response.status < 300 else 'unavailable'
    except Exception:
        return 'unavailable'


@app.get("/health")
def health():
    try:
        with psycopg2.connect(_read_dsn()) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
            # The readiness probe goes through the policy layer too: `public_reviewed_sql`
            # is the publish gate without the collection allow-list, which is what this
            # count has always been.
            readiness = public_reviewed_sql("document_assets")
            cur.execute(f"SELECT count(*) FROM document_assets WHERE {readiness.text}",
                        readiness.params)
            document_count = cur.fetchone()[0]
        knowledge = "ready" if document_count else "empty"
        vector_store = _vector_store_status()
        if knowledge == "ready" and vector_store != "ready":
            knowledge = "degraded"
        # 上传队列没有一个常驻 HTTP 调用者：任务只由独立 worker 推进（审查 5.1）。心跳读取
        # 复用既有 `task_leases`（迁移 013，readonly_user 无权读），所以这里单独用写 DSN。
        from ingestion import upload_pipeline
        upload_worker = upload_pipeline.worker_heartbeat(_write_dsn())
        return {"status": "ok", "database": "ready", "agent": "sql+rag",
                "capabilities": ["sql", "rag", "multifile_rag", "industrial_rules", "industrial_nl2sql", "fact_reviews", "local_identity", "fact_approvals", "source_authority", "cad_assets", "cad_dimensions", "file_jobs", "private_pdf_answer", "upload_worker"],
                "knowledge": knowledge, "document_count": document_count,
                "vector_store": vector_store, "worker": upload_worker}
    except psycopg2.Error:
        raise HTTPException(503, detail={"error_code": "DATABASE_UNAVAILABLE"})


def _stored_config() -> dict:
    try:
        return _select_stored_profile()
    except psycopg2.Error:
        raise HTTPException(503, detail={"error_code": "DATABASE_UNAVAILABLE"})


def _config_source(stored: dict) -> str:
    # 与 sql_engine.settings() 的 MODEL_CONFIG_SOURCE 同一份判定（唯一解析入口）。
    return resolve_profile_values(stored=stored)["source"]


def _profiles_safe() -> dict:
    try:
        data = public_profiles()
    except psycopg2.Error:
        raise HTTPException(503, detail={"error_code": "DATABASE_UNAVAILABLE"})
    data["protocols"] = [{"id": key, "label": label} for key, label in PROTOCOLS.items()]
    return jsonable_encoder(data)


def _select_stored_profile(profile_id: str | None = None) -> dict:
    """从元数据库选出目标配置：指定 id 优先，未命中则回退当前启用配置。"""
    if profile_id:
        state = list_profiles()
        found = next((item for item in state["items"] if item.get("id") == profile_id), None)
        if found:
            return dict(found)
    return dict(load_model_config())


def resolve_profile(request: ProfileRequest | None = None, *, stored: dict | None = None,
                    inherit_sensitive_headers: bool = True) -> dict:
    """api.py 内唯一的 profile 解析入口（探活、模型列表等共用）。

    字段拼装只有一份实现：``settings_store.resolve_profile_values()``（问数链路的
    ``sql_engine.settings()`` 是同一份结果的 NEW_API_* 视图），本函数只把它铺回既有
    的 profile 结构，因此同一份输入在任何入口都解析出同一结果（含 proxy / headers）。
    优先级：请求显式字段 > 指定/启用配置 > 环境变量 > 默认值。``stored`` 可显式注入
    已保存配置，便于测试与无数据库场景。

    敏感头补回（**无条件**，不因调用方式而异）：请求头来自浏览器脱敏表单，只提交非敏感
    头的请求会丢掉未提交的 Authorization / api-key / Cookie，导致「编辑非敏感头后测试
    连接」401。因此统一用 ``merge_sensitive_headers()`` 从已存配置补回敏感头，请求显式
    提交的值优先，已存没有的不凭空造；删除敏感头只有提交 ``__clear__`` 哨兵一条通道。
    这个行为**不随 ``stored`` 是否注入而改变**——让参数悄悄翻转合并语义会把同一个逻辑
    操作变成两种含义，正是本缺陷的成因之一。
    """
    if stored is None:
        try:
            stored = _select_stored_profile(getattr(request, "profile_id", None))
        except psycopg2.Error:
            stored = {}
    resolved = resolve_profile_values(request, stored=stored,
                                      inherit_sensitive_headers=inherit_sensitive_headers)
    profile = dict(stored or {})
    for field in PROFILE_VALUE_FIELDS:
        profile[field] = resolved[field]
    # Cross-source probes must be rejectable: the key was neither inherited nor invented.
    profile["key_inheritance_blocked"] = bool(resolved.get("key_inheritance_blocked"))
    profile["stored_credentials_restricted"] = bool(resolved.get("stored_credentials_restricted"))
    return profile


def _reject_cross_origin_inheritance(profile: dict) -> dict:
    """F01: a probe aimed at a different host must not reuse the stored key."""
    if profile.get("key_inheritance_blocked"):
        raise HTTPException(400, detail={"error_code": "MODEL_KEY_NOT_INHERITED"})
    return profile


@app.get("/v1/model/profiles")
def get_profiles():
    """已保存的接入配置列表（密钥只回显掩码）+ 可选协议。

    读取侧不下发任何凭据值：敏感请求头只回显键名（``header_keys``），``proxy``
    去掉 userinfo。表单因此看不到真实值，留空即可；保存时由 settings_store 做
    键级合并，不会用空值覆盖已存的真实凭据。
    """
    return _profiles_safe()


@app.get("/v1/model/profiles/export")
def export_profiles(include_keys: bool = Query(default=False)):
    """导出接入配置；默认（include_keys=False）经过 redact_profile() 脱敏。

    脱敏会移除 api_key，并剔除 headers 中的 authorization/api-key/x-api-key/cookie
    等敏感头、去掉 proxy URL 里的 userinfo，保证「无密钥导出」不含任何认证材料。
    include_keys=True 是显式要求带密钥的搬迁导出，原样返回。
    """
    state = list_profiles()
    items = []
    for item in state["items"]:
        payload = {field: item.get(field) for field in PROFILE_FIELDS}
        payload["id"] = item.get("id")
        if not include_keys:
            payload = redact_profile(payload)
        items.append(payload)
    return jsonable_encoder({"exported_at": datetime.now(timezone.utc).isoformat(),
                             "include_keys": include_keys, "active": state.get("active"),
                             "profiles": items})


@app.post("/v1/model/profiles/import")
def import_profiles(request: ProfileImportRequest):
    """导入配置（ccswitch 风格的搬迁）：同名不覆盖，一律作为新配置写入。"""
    imported = []
    for raw in request.profiles[:50]:
        if not isinstance(raw, dict):
            continue
        payload = {field: raw.get(field) for field in PROFILE_FIELDS if raw.get(field) is not None}
        payload["id"] = ""
        if not payload.get("name"):
            payload["name"] = "导入配置"
        imported.append(public_profile(save_profile(payload)))
    if request.activate_first and imported:
        activate_profile(imported[0]["id"])
    return jsonable_encoder({"imported": len(imported), **_profiles_safe()})


@app.post("/v1/model/profiles")
def create_profile(request: ProfileRequest):
    raw = request.model_dump(exclude_none=True)
    raw.pop("profile_id", None)
    raw["id"] = ""
    if not (raw.get("name") or "").strip():
        raise HTTPException(400, detail={"error_code": "PROFILE_NAME_REQUIRED"})
    if not (raw.get("api_key") or "").strip():
        raw.pop("api_key", None)
    return jsonable_encoder(public_profile(save_profile(raw)))


@app.put("/v1/model/profiles/{profile_id}")
def update_profile(profile_id: str, request: ProfileRequest):
    state = list_profiles()
    if not any(item.get("id") == profile_id for item in state["items"]):
        raise HTTPException(404, detail={"error_code": "PROFILE_NOT_FOUND"})
    raw = request.model_dump(exclude_none=True)
    raw.pop("profile_id", None)
    raw["id"] = profile_id
    if not (raw.get("api_key") or "").strip():
        raw.pop("api_key", None)      # 留空 = 保持原密钥
    return jsonable_encoder(public_profile(save_profile(raw)))


@app.delete("/v1/model/profiles/{profile_id}")
def remove_profile(profile_id: str):
    if not delete_profile(profile_id):
        raise HTTPException(404, detail={"error_code": "PROFILE_NOT_FOUND"})
    return _profiles_safe()


@app.post("/v1/model/profiles/{profile_id}/activate")
def activate(profile_id: str):
    try:
        activate_profile(profile_id)
    except KeyError:
        raise HTTPException(404, detail={"error_code": "PROFILE_NOT_FOUND"})
    return _profiles_safe()


@app.get("/v1/model/config")
def get_model_config():
    """当前启用配置（兼容单配置调用方）。"""
    stored = _stored_config()
    view = public_model_config(stored, source=_config_source(stored))
    view["environment_fallback"] = bool(os.environ.get("NEW_API_KEY"))
    return jsonable_encoder(view)


@app.put("/v1/model/config")
def put_model_config(request: ProfileRequest):
    stored = _stored_config()
    if request.api_key is None or not request.api_key.strip():
        api_key = stored.get("api_key", "")
    elif request.api_key.strip().lower() in {"clear", "none", "-"}:
        api_key = ""
    else:
        api_key = request.api_key.strip()
    base_url = normalize_base_url(request.base_url) or stored.get("base_url") or DEFAULT_BASE_URL
    model = (request.model or stored.get("model") or DEFAULT_MODEL).strip()
    save_model_config(base_url, model, api_key, protocol=request.protocol or stored.get("protocol") or OPENAI)
    return jsonable_encoder(public_model_config(active_profile(),
                                                source="app_settings" if api_key else "unset"))


@app.delete("/v1/model/config")
def delete_model_config():
    clear_model_config()
    return {"has_key": False, "key_masked": "", "source": "unset"}


@app.post("/v1/model/config/models")
def probe_models(request: ProfileRequest):
    profile = _reject_cross_origin_inheritance(resolve_profile(request))
    result = provider_list_models(profile)
    result["base_url"] = profile.get("base_url")
    result["protocol"] = profile.get("protocol") or OPENAI
    return jsonable_encoder(result)


@app.post("/v1/model/config/test")
def probe_chat(request: ProfileRequest):
    profile = _reject_cross_origin_inheritance(resolve_profile(request))
    result = provider_test_chat(profile)
    result["base_url"] = profile.get("base_url")
    result["configured_model"] = profile.get("model")
    return jsonable_encoder(result)

def _with_clarification_contract(payload: dict, question: str) -> dict:
    """把 ``NEEDS_CLARIFICATION`` 的规则式话术投影成结构化缺项（最小接线，只新增键）。

    ``status`` / ``answer`` / ``error_code`` 的含义与取值完全不变；新增
    ``clarification_missing_slots`` / ``clarification_candidates`` /
    ``clarification_slot_questions`` / ``clarification_contract_version``，让前端能做
    「一次补充后直接完成」。槽位契约只有一份实现，在生产包 ``datamind/clarification.py``
    （评测脚本只做再导出），因此服务端不再在请求期导入评测脚本
    （第 1 轮复查 L4）；投影本身抛错时原样返回，澄清响应绝不因为这个可选字段而失败。
    """
    if not isinstance(payload, dict) or payload.get("status") != "NEEDS_CLARIFICATION":
        return payload
    try:
        fields = clarification_contract_for_answer(question or "", payload.get("answer") or "")
    except Exception:
        return payload
    return {**payload, **fields}


@app.post("/v1/chat")
def chat(request: ChatRequest):
    kwargs = _chat_kwargs(request)
    agent = DataMindAgent()
    try:
        result = agent.run(request.question, **kwargs)
        return jsonable_encoder(_with_clarification_contract(result, request.question))
    finally:
        agent.close()


@app.post("/v1/diagnostics/sql")
def diagnostic_sql():
    """Fixed query proving browser → Agent → Guard → readonly DB without a model call."""
    question = "2024年一共有多少笔订单？"
    sql = ("SELECT count(*) FROM orders WHERE created_at >= '2024-01-01' "
           "AND created_at < '2025-01-01'")
    agent = DataMindAgent()
    try:
        return jsonable_encoder(agent.run(question, database_sources=["demo_retail"],
                                         generator=lambda *_: {"sql": sql, "interpretation": "固定链路自检"}))
    finally:
        agent.close()


@app.post("/v1/chat/stream")
def chat_stream(request: ChatRequest):
    # Same validation and same agent keyword set as `/v1/chat`; the terminal `answer.completed`
    # trace event carries the same `turns_used` the non-streaming response would report.
    kwargs = _chat_kwargs(request)
    updates: queue.Queue = queue.Queue()

    def sink(event: dict):
        # 非流式响应与流式终态事件必须带同一套澄清键，否则前端走 SSE 时拿不到补充候选。
        if isinstance(event, dict) and event.get("event_type") == "answer.clarification":
            event["output_summary"] = _with_clarification_contract(
                event.get("output_summary") or {}, request.question)
        updates.put(event)

    agent = DataMindAgent(sink=sink)

    def worker():
        try:
            agent.run(request.question, **kwargs)
        finally:
            agent.close()
            updates.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def events():
        while True:
            try:
                event = updates.get(timeout=15)
            except queue.Empty:
                yield ": heartbeat\n\n"
                continue
            if event is None:
                break
            yield "event: trace\ndata: " + json.dumps(event, ensure_ascii=False, default=str) + "\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------- chat sessions ----------
#
# 会话只是对 `runs` 行的命名分组，没有消息表：transcript 由 `runs` 推导（question=用户轮次、
# answer=助手轮次，按 started_at 排序），run 通过 `runs.conversation_id` 入组（见
# `ChatRequest.conversation_id` / `_chat_kwargs`）。
#
# 授权：这里不列进 PUBLIC_PATHS / PUBLIC_READ_PREFIXES / PUBLIC_WRITE_PATHS，所以默认拒绝
# 保证必须有身份，变更还自动带 origin + CSRF；`_required_permission` 也不为 /v1/sessions
# 返回任何角色，即“任意已登录用户只管理自己的会话”。越权与不存在统一由服务层回 404
# SESSION_NOT_FOUND，避免泄露会话是否存在。

@app.post('/v1/sessions')
def session_create(payload: SessionCreateRequest,request: Request):
    from .sessions import create
    return jsonable_encoder({'session':create(payload.model_dump(),_write_dsn(),request.state.identity)})


@app.get('/v1/sessions')
def session_list(request: Request,limit: int=Query(default=20,ge=1,le=50),
                 offset: int=Query(default=0,ge=0,le=10000),q: str | None=Query(default=None)):
    from .sessions import listing
    return jsonable_encoder(listing(_write_dsn(),request.state.identity,q=q,limit=limit,offset=offset))


@app.get('/v1/sessions/{session_id}')
def session_detail(session_id: str,request: Request):
    from .sessions import detail
    return jsonable_encoder(detail(session_id,_write_dsn(),request.state.identity))


@app.get('/v1/sessions/{session_id}/results')
def session_results(session_id: str,request: Request,limit: int=Query(default=20,ge=1,le=50),
                    offset: int=Query(default=0,ge=0,le=10000)):
    """历史会话的富内容（SQL 结果表 / 引用 / 工业核验），按 run 分页、最近优先。

    独立于 `detail` 的理由（体积）见 `datamind/sessions.py::results`。授权面与
    `/v1/sessions` 完全一致：默认拒绝、必须有身份、不限角色、只读自己的会话；
    `/v1/sessions` 已在 `_NO_STORE_PREFIXES` 里，这条子路径同样恒为 no-store。
    """
    from .sessions import results
    return jsonable_encoder(results(session_id,_write_dsn(),request.state.identity,
                                   limit=limit,offset=offset))


@app.patch('/v1/sessions/{session_id}')
def session_rename(session_id: str,payload: SessionRenameRequest,request: Request):
    from .sessions import rename
    return jsonable_encoder(rename(session_id,payload.model_dump(),_write_dsn(),request.state.identity))


@app.delete('/v1/sessions/{session_id}')
def session_remove(session_id: str,request: Request):
    from .sessions import remove
    return jsonable_encoder(remove(session_id,_write_dsn(),request.state.identity))


def _document_asset(document_id: str) -> dict | None:
    """The raw `document_assets` row, published or not; None when the id is unknown.

    One loader exists so the public detail routes and the contract route judge the *same*
    row with the *same* predicates, instead of each embedding its own SQL filter that can
    drift apart.
    """
    with psycopg2.connect(_read_dsn()) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""SELECT * FROM document_assets WHERE document_id=%s""", (document_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def _public_document(document_id: str) -> dict:
    """The raw `document_assets` row, published or not; rejects the unknown id.

    One loader exists so the public detail routes and the contract route judge the *same*
    row, and every judgement is `datamind.visibility.is_published` — the single publish
    policy, shared with `knowledge`'s SQL gate and `authority`'s basis check — instead of
    each route embedding its own filter that can drift apart.
    """
    document = _document_asset(document_id)
    if not is_published(document):
        raise HTTPException(404, detail={"error_code": "DOCUMENT_NOT_FOUND"})
    return document


@app.get("/v1/documents")
def list_documents(collection: str | None = None):
    if collection and collection not in COLLECTIONS:
        raise HTTPException(400, detail={"error_code": "COLLECTION_NOT_ALLOWED"})
    # R05: this route is the Q&A file selector's data source, and the retriever
    # (`datamind.knowledge`) reads `chunks` only. The published status alone cannot say
    # whether anything is retrievable, so every item states the derived fact explicitly,
    # through the *same* `upload_pipeline.searchability` judgment the upload surfaces use
    # (one rule, not two). Consumers that select files must require `searchable=true`; the
    # publish filter itself (`datamind.visibility`) is unchanged so no existing consumer
    # silently loses rows.
    from ingestion import upload_pipeline
    with psycopg2.connect(_read_dsn()) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        published = published_document_sql(
            "document_assets", [collection] if collection else sorted(COLLECTIONS))
        cur.execute(f"""SELECT document_id,title,collection,file_type,origin,page_count,doc_version,sha256,
                              quality_score,quality_detail,parser_version,status,needs_review,
                              (SELECT count(*) FROM chunks c
                               WHERE c.document_id=document_assets.document_id) AS chunk_count,
                              (SELECT count(*) FROM document_contract_blocks b
                               WHERE b.document_id=document_assets.document_id) AS block_count
                       FROM document_assets
                       WHERE {published.text}
                       ORDER BY title LIMIT 200""", published.params)
        documents = []
        for row in cur.fetchall():
            item = dict(row)
            searchable, index_state, index_note = upload_pipeline.searchability(
                item.pop("status"), item.pop("needs_review"), item.pop("chunk_count"),
                block_count=item.pop("block_count"))
            item.update(searchable=searchable, index_state=index_state, index_note=index_note)
            documents.append(item)
    return {"documents": jsonable_encoder(documents)}


@app.get("/v1/documents/{document_id}")
def get_document(document_id: str):
    document = _public_document(document_id)
    document.pop("object_uri", None)
    return jsonable_encoder(document)


def _source_path(document: dict) -> Path:
    path = Path(document.get("object_uri") or "").resolve()
    if not any(path.is_relative_to(ROOT / name) for name in ("data/knowledge_base", "data/ohr_bench_samples", "data/industrial_demo", "data/uploads", "data/cad_demo", "data/dimension_demo")) or not path.is_file():
        raise HTTPException(404, detail={"error_code": "SOURCE_FILE_UNAVAILABLE"})
    if hashlib.sha256(path.read_bytes()).hexdigest() != document["sha256"]:
        raise HTTPException(409, detail={"error_code": "SOURCE_VERSION_MISMATCH"})
    return path


def _expected_document(document,version,sha):
    if version is not None or sha is not None:
        if version is None or sha is None:
            raise HTTPException(422,detail={'error_code':'DOCUMENT_SELECTION_INVALID'})
        if document['doc_version']!=version or document['sha256']!=sha:
            raise HTTPException(409,detail={'error_code':'DOCUMENT_SELECTION_CHANGED'})


FILE_MEDIA_TYPES = {"pdf": "application/pdf", "json": "application/json",
                    "dxf": "application/dxf",
                    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}


@app.get("/v1/documents/{document_id}/file")
def document_file(document_id: str,expected_version: int | None=Query(default=None,ge=1),
                  expected_sha256: str | None=Query(default=None,pattern=r'^[a-f0-9]{64}$')):
    document = _public_document(document_id)
    _expected_document(document,expected_version,expected_sha256)
    path = _source_path(document)
    return FileResponse(path, media_type=FILE_MEDIA_TYPES.get(document["file_type"], "application/octet-stream"),
                        filename=path.name, content_disposition_type="inline")


def _pending_upload_evidence(document: dict | None) -> bool:
    """An upload row whose only readable reason is pre-approval evidence browsing.

    The upload pipeline marks its own rows `origin='upload'` and keeps `needs_review=TRUE`
    with a non-`indexed` status until an explicit approve, but it also writes the default
    `public` ACL. ACL alone is therefore not a publish decision for these rows, and the
    contract route sits inside the anonymous public-read prefix: without an identity check
    an unreviewed upload's full text and locators leaked to anonymous callers.
    """
    return bool(document) and document.get("origin") == "upload" \
        and has_public_acl(document) \
        and collection_is_publishable(document.get("collection")) \
        and not is_published(document)


def _identity_is_admin(identity) -> bool:
    return bool(identity) and "admin" in (identity.get("roles") or [])


def _contract_visible(document: dict | None, identity) -> bool:
    """The single authority for reading one document's contract.

    Two disjoint reasons grant access, and the caller of this function owns no other one:

    * **published** — the same predicate the detail/file/facts routes use, so a normally
      indexed document (including anonymous public read) behaves exactly as before;
    * **pre-approval upload evidence** — an authenticated administrator only. The loopback
      no-login demo admin resolves to an admin identity, so the local review flow keeps
      working.

    Anything else is unreadable here, and the route answers 404 exactly as the detail route
    does: an unauthorised caller must not learn that the id exists.
    """
    if is_published(document):
        return True
    return _pending_upload_evidence(document) and _identity_is_admin(identity)


def _contract_window(limit, offset):
    """Parse the raw query values into a validated `(limit, offset)` page window.

    Conversion and bounds share the store's `page_window`, so the route cannot drift from
    `read_result`. A blank value takes the default; a non-numeric or out-of-range value is a
    stable 400 with the store's own error code, never a silent clamp.
    """
    from ingestion.contract_store import DEFAULT_PAGE_LIMIT, page_window

    def number(raw, default, code):
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return default
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            raise HTTPException(400, detail={'error_code': code})

    parsed = (number(limit, DEFAULT_PAGE_LIMIT, 'CONTRACT_PAGE_LIMIT_INVALID'),
              number(offset, 0, 'CONTRACT_PAGE_OFFSET_INVALID'))
    try:
        return page_window(*parsed)
    except ValueError as error:
        raise HTTPException(400, detail={'error_code': str(error)})


@app.get("/v1/documents/{document_id}/contract")
def document_contract(document_id: str, request: Request, response: Response,
                      limit: str | None = Query(default=None),
                      offset: str | None = Query(default=None)):
    """One bounded page of everything the contract stored for one document, from both stores.

    `blocks` holds the contract blocks of formats without a page concept (Office, DXF);
    `locators` holds the validated locators of page-shaped PDF parses, which live beside
    `document_blocks`. A document may have either or both; returning only one would report
    "nothing stored" for a document that does have validated locators.

    **Who may read it** is decided by `_contract_visible`: a published document is public
    (anonymous included, unchanged), while an unreviewed upload is readable only by an
    authenticated administrator and then carries `Cache-Control: no-store`.

    **Pagination.** `limit`/`offset` window each store; the legacy `*_count` keys keep
    counting this page, while `page`/`store_pages`/`totals`/`counts` separate the page from
    the database so a truncated read is never presented as the whole contract.

    `parse_contract` repeats the summary the ingest path recorded, including
    `unsupported_features`. Without it a document whose format cannot carry a locator (an
    OHR-Bench JSON record has a page index but no page geometry, so no honest `pdf_region`
    exists) reads as "indexed but nothing stored" — the counts alone cannot distinguish
    "nothing was parsed" from "a gap was declared".
    """
    from .auth import current
    from ingestion.contract_store import read_result
    limit, offset = _contract_window(limit, offset)
    # This route is inside the anonymous public-read prefix, so the middleware did not
    # resolve an identity. Identity lives in the auth tables, which the read-only role
    # cannot see, so this uses the same DSN the middleware uses — and `provision_demo=False`
    # keeps a read from ever writing the demo user row.
    identity = current(request, _write_dsn(), required=False, provision_demo=False)
    document = _document_asset(document_id)
    if not _contract_visible(document, identity):
        raise HTTPException(404, detail={"error_code": "DOCUMENT_NOT_FOUND"})
    stored = read_result(document_id, _read_dsn(), limit=limit, offset=offset)
    summary = (document.get("quality_detail") or {}).get("parse_contract") or {}
    if not is_published(document):
        # Pre-approval evidence (or any non-published row an admin may still read) must not
        # be cached by a browser or an intermediary.
        response.headers["Cache-Control"] = "no-store"
    return {"document_id": document_id, "document_version": document["doc_version"],
            "source_sha256": document["sha256"], "parser_version": document["parser_version"],
            "contract_version": document.get("contract_version"),
            "block_count": stored["block_count"], "locator_count": stored["locator_count"],
            "block_locator_count": stored.get("block_locator_count"),
            "locator_total": stored.get("locator_total"),
            "relation_count": stored["relation_count"],
            "page": stored["page"], "store_pages": stored["store_pages"],
            "totals": stored["totals"], "counts": stored["counts"],
            "parse_contract": jsonable_encoder(summary),
            "unsupported_features": list(summary.get("unsupported_features") or []),
            "blocks": jsonable_encoder(stored["blocks"]),
            "locators": jsonable_encoder(stored["locators"]),
            "relations": jsonable_encoder(stored["relations"])}


@app.get("/v1/documents/{document_id}/facts")
def document_facts(document_id: str):
    from .field_store import read_verified_facts
    document = _public_document(document_id)
    source = _source_path(document)
    facts = read_verified_facts(document, source, _read_dsn())
    return {"document_id": document_id, "document_version": document["doc_version"], "source_sha256": document["sha256"],
            "parser_version": document["parser_version"], "facts": jsonable_encoder(facts), "fact_count": len(facts),
            "quality": (document.get("quality_detail") or {}).get("fact_extraction")}


@app.get('/v1/documents/{document_id}/facts/{fact_id}/reviews')
def get_fact_reviews(document_id: str,fact_id: str):
    document=_public_document(document_id);_source_path(document)
    from .fact_reviews import history
    return {'reviews':jsonable_encoder(history(document_id,fact_id,_read_dsn())),
            'semantics':'Authenticated local extraction review; legacy rows have no authenticated identity; separate from approval/authority'}


@app.post('/v1/documents/{document_id}/facts/{fact_id}/reviews')
def review_document_fact(document_id: str,fact_id: str,request: FactReviewRequest,http_request: Request):
    from .field_store import read_verified_facts
    from .fact_reviews import record
    document=_public_document(document_id);source=_source_path(document)
    facts=read_verified_facts(document,source,_read_dsn())
    fact=next((f for f in facts if f['fact_id']==fact_id),None)
    if not fact:raise HTTPException(409,detail={'error_code':'FACT_UNAVAILABLE_OR_STALE'})
    identity=http_request.state.identity
    if request.reviewer_label is not None and request.reviewer_label!=identity['display_name']:
        raise HTTPException(422,detail={'error_code':'REVIEWER_IDENTITY_SERVER_OWNED'})
    payload=request.model_dump();payload['reviewer_label']=identity['display_name'];payload['reason']=payload['reason'].strip()
    if not payload['reason']:raise HTTPException(422,detail={'error_code':'REVIEW_REASON_REQUIRED'})
    payload.update(reviewer_user_id=identity['user_id'],reviewer_roles=identity['roles'])
    result=record(document,fact,source,payload,_write_dsn(),identity=identity)
    current_fact=next((f for f in read_verified_facts(document,source,_read_dsn()) if f['fact_id']==fact_id),None)
    return {'review':jsonable_encoder({k:v for k,v in result.items() if k not in ('fact_snapshot','request_payload')}),
            'field_state':jsonable_encoder({k:current_fact[k] for k in ('latest_review','latest_approval','formal_approval_status')}) if current_fact else None,
            'semantics':'Only extraction fidelity is reviewed; source approval and unresolved conflicts remain unchanged'}


@app.get('/v1/documents/{document_id}/facts/{fact_id}/approvals')
def get_fact_approvals(document_id: str,fact_id: str):
    document=_public_document(document_id);_source_path(document)
    from .approvals import history
    return {'approvals':jsonable_encoder(history(document_id,fact_id,_read_dsn())),
            'semantics':'Separate version-bound local approval; does not overwrite source declarations or resolve conflicts'}


@app.post('/v1/documents/{document_id}/facts/{fact_id}/approvals')
def approve_document_fact(document_id: str,fact_id: str,request: FactApprovalRequest,http_request: Request):
    from .field_store import read_verified_facts
    from .approvals import record
    document=_public_document(document_id);source=_source_path(document)
    fact=next((f for f in read_verified_facts(document,source,_read_dsn()) if f['fact_id']==fact_id),None)
    if not fact:raise HTTPException(409,detail={'error_code':'FACT_UNAVAILABLE_OR_STALE'})
    payload=request.model_dump();payload['reason']=payload['reason'].strip()
    if not payload['reason']:raise HTTPException(422,detail={'error_code':'APPROVAL_REASON_REQUIRED'})
    identity=http_request.state.identity
    payload.update(approver_user_id=identity['user_id'],approver_label=identity['display_name'],approver_roles=identity['roles'])
    result=record(document,fact,source,payload,_write_dsn(),identity=identity)
    current_fact=next((f for f in read_verified_facts(document,source,_read_dsn()) if f['fact_id']==fact_id),None)
    return {'approval':jsonable_encoder({k:v for k,v in result.items() if k not in {'fact_snapshot','request_payload'}}),
            'field_state':jsonable_encoder({k:current_fact[k] for k in ('latest_review','latest_approval','formal_approval_status')}) if current_fact else None,
            'semantics':'A separate local approval; original approval declaration and conflicting source values are preserved'}


@app.get("/v1/documents/{document_id}/pages/{page_no}")
def document_page(document_id: str,page_no: int,expected_version: int | None=Query(default=None,ge=1),
                  expected_sha256: str | None=Query(default=None,pattern=r'^[a-f0-9]{64}$')):
    document = _public_document(document_id)
    _expected_document(document,expected_version,expected_sha256)
    if document["file_type"] != "pdf" or not 1 <= page_no <= (document["page_count"] or 0):
        raise HTTPException(404, detail={"error_code": "SOURCE_PAGE_UNAVAILABLE"})
    _source_path(document)
    image_root = ROOT / "data/object_store/page_images"
    path = (image_root / document_id / f"page_{page_no:03d}.png").resolve()
    if not path.is_relative_to(image_root) or not path.is_file():
        raise HTTPException(404, detail={"error_code": "SOURCE_PAGE_UNAVAILABLE"})
    return FileResponse(path, media_type="image/png")


def _run_exists(cur, run_id: str):
    cur.execute("SELECT run_id FROM runs WHERE run_id=%s", (run_id,))
    if not cur.fetchone():
        raise HTTPException(404, detail={"error_code": "RUN_NOT_FOUND"})


@app.get("/v1/runs")
def list_runs(limit: int = Query(default=20, ge=1, le=200),
              offset: int = Query(default=0, ge=0, le=100000)):
    """Recent runs for the 「运行记录」 view.

    ``conversation_id`` is projected so the client can tell which session (if any) a run
    belongs to: runs created before the session feature existed carry NULL there, and the
    UI has to be able to say "尚未归属会话" honestly instead of guessing.
    Pagination follows the house `limit+1` / `has_more` idiom so 「运行记录」 can page
    through the whole history rather than only the newest page.
    """
    with psycopg2.connect(_read_dsn()) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""SELECT run_id, conversation_id, question, status, confidence_level,
                              started_at, ended_at
                       FROM runs ORDER BY started_at DESC, run_id DESC LIMIT %s OFFSET %s""",
                    (limit + 1, offset))
        rows = [dict(r) for r in cur.fetchall()]
    has_more = len(rows) > limit
    return {"runs": jsonable_encoder(rows[:limit]), "limit": limit, "offset": offset,
            "has_more": has_more}


@app.get("/v1/runs/{run_id}")
def get_run(run_id: str):
    with psycopg2.connect(_read_dsn()) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM runs WHERE run_id=%s", (run_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(404, detail={"error_code": "RUN_NOT_FOUND"})
    return jsonable_encoder(dict(row))


@app.get("/v1/runs/{run_id}/events")
def get_events(run_id: str, after: str | None = Query(default=None)):
    if after and not after.startswith("evt-" + run_id[4:] + "-"):
        raise HTTPException(400, detail={"error_code": "INVALID_CURSOR"})
    with psycopg2.connect(_read_dsn()) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        _run_exists(cur, run_id)
        cur.execute("""SELECT * FROM trace_events WHERE run_id=%s AND event_id > %s
                       ORDER BY event_id LIMIT 200""", (run_id, after or ""))
        rows = [dict(r) for r in cur.fetchall()]
    return {"events": jsonable_encoder(rows), "next_cursor": rows[-1]["event_id"] if rows else after}


@app.get("/v1/runs/{run_id}/evidence")
def get_evidence(run_id: str):
    with psycopg2.connect(_read_dsn()) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        _run_exists(cur, run_id)
        cur.execute("SELECT * FROM evidence_nodes WHERE run_id=%s ORDER BY created_at, evidence_id", (run_id,))
        nodes = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT * FROM evidence_edges WHERE run_id=%s ORDER BY edge_id", (run_id,))
        edges = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT * FROM claims WHERE run_id=%s ORDER BY created_at", (run_id,))
        claims = [dict(r) for r in cur.fetchall()]
    return jsonable_encoder({"nodes": nodes, "edges": edges, "claims": claims})


@app.get('/v1/runs/{run_id}/conflicts')
def get_conflicts(run_id: str):
    with psycopg2.connect(_read_dsn()) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        _run_exists(cur,run_id)
        cur.execute('SELECT payload FROM conflict_records WHERE run_id=%s ORDER BY conflict_id',(run_id,))
        return {'conflicts':[r['payload'] for r in cur.fetchall()]}


# ---------- wiki knowledge graph (docs/WIKI_API.md, 14 routes) ----------
#
# 接线纪律（三条都是实测出来的，不是风格偏好）：
#
# 1. **读也必须用应用角色连接**：迁移 `021:605-607` 已把 8 张 `wiki_*` 表从 `readonly_user`
#    REVOKE（实测 42501），传 `_read_dsn()` 会让这 14 条全部 500。先例：`/v1/sessions`
#    用 `_write_dsn()` 读 `chat_sessions`。
# 2. **权限走 `WIKI_WRITE_PERMISSIONS`**：五条写路由逐条列名（#14 以 `/publications` 结尾，
#    不命中任何通用兜底），读接口不做角色门。`/v1/wiki` 不进 `PUBLIC_PATHS` /
#    `PUBLIC_READ_PREFIXES` / `PUBLIC_WRITE_PATHS`：中间件保证"必须有身份"，POST 另外
#    自动带 CSRF + 同源检查（契约 §1）。
# 3. **不动服务层**：授权投影（`wiki.resolve_space` / `authorized_entry` / `_load_job`）与
#    错误信封（`wiki.fail` → `{"detail":{"error_code",…}}`）都在冻结层里，本层只做参数透传
#    与 `jsonable_encoder`；越权与不存在统一 404，本层**不**翻译成 403。
#
# 参数边界只在契约 §4 明说"框架 422"的数字（`limit` / `depth`）上用 `Query` 收口；文本与
# 枚举一律透传给服务层校验，两层各写一套会漂移（服务层已有稳定错误码）。

@app.get('/v1/wiki/entries')
def wiki_entry_catalogue(request: Request,q: str | None=Query(default=None),
                         type: list[str] | None=Query(default=None),
                         status: list[str] | None=Query(default=None),
                         category: str | None=Query(default=None),
                         updated_since: str | None=Query(default=None),
                         space_id: str | None=Query(default=None),
                         limit: int=Query(default=20,ge=1,le=50),
                         cursor: str | None=Query(default=None)):
    """#1 授权词条目录与关键词搜索（契约 §5.1）。`type` / `status` 可重复，`q` 覆盖标题/别名/分类。"""
    from .wiki_query import entry_catalogue
    return jsonable_encoder(entry_catalogue(_write_dsn(),request.state.identity,q=q,entry_type=type,
                                            status=status,category=category,
                                            updated_since=updated_since,space_id=space_id,
                                            limit=limit,cursor=cursor))


@app.get('/v1/wiki/spaces/{space_id}/graph')
def wiki_space_graph(space_id: str,request: Request,layer: str | None=Query(default=None),
                     q: str | None=Query(default=None),type: list[str] | None=Query(default=None),
                     status: list[str] | None=Query(default=None),
                     category: str | None=Query(default=None),limit: int=Query(default=50,ge=1,le=50),
                     cursor: str | None=Query(default=None)):
    """#2 当前授权范围图（契约 §5.2；每批固定 50 节点，累计上限由服务层给 `limit_reached`）。

    请求别人的 `space_id` → 404 `WIKI_SPACE_NOT_FOUND`（`entry_catalogue` 同一个授权投影）。
    """
    from .wiki_query import space_graph
    return jsonable_encoder(space_graph(space_id,_write_dsn(),request.state.identity,layer=layer,q=q,
                                        entry_type=type,status=status,category=category,
                                        limit=limit,cursor=cursor))


@app.get('/v1/wiki/entries/{entry_id}')
def wiki_entry_detail(entry_id: str,request: Request,revision_id: str | None=Query(default=None)):
    """#3 当前发布版本 / 指定版本（契约 §5.3；草稿需编辑权限，否则与不存在一致的 404）。"""
    from .wiki import entry_detail
    return jsonable_encoder(entry_detail(entry_id,_write_dsn(),request.state.identity,
                                         revision_id_value=revision_id))


@app.get('/v1/wiki/entries/{entry_id}/revisions')
def wiki_entry_revisions(entry_id: str,request: Request,limit: int=Query(default=20,ge=1,le=50),
                         cursor: str | None=Query(default=None)):
    """#4 可见版本历史（契约 §5.4；每条一个不含正文的摘要，带 `body_sha256`）。"""
    from .wiki import revision_history
    return jsonable_encoder(revision_history(entry_id,_write_dsn(),request.state.identity,
                                             limit=limit,cursor=cursor))


@app.get('/v1/wiki/entries/{entry_id}/revisions/{revision_id}')
def wiki_revision_detail(entry_id: str,revision_id: str,request: Request,
                         expected_body_sha256: str | None=Query(default=None),
                         expected_base_revision_id: str | None=Query(default=None)):
    """#5 固定版本详情（契约 §5.5；`expected_*` 不一致 → 409 `WIKI_VERSION_CONFLICT`）。

    `expected_body_sha256` 的格式校验留给服务层：它同时接受裸 hex64 与 `sha256:<hex64>`
    （`wiki.strip_sha256_prefix`），在路由上写死 `pattern` 会把后者误拒。
    """
    from .wiki import revision_detail
    return jsonable_encoder(revision_detail(entry_id,revision_id,_write_dsn(),request.state.identity,
                                            expected_body_sha256=expected_body_sha256,
                                            expected_base_revision_id=expected_base_revision_id))


@app.get('/v1/wiki/entries/{entry_id}/graph')
def wiki_entry_neighborhood(entry_id: str,request: Request,depth: int=Query(default=1,ge=1,le=2),
                            limit: int=Query(default=50,ge=1,le=100),
                            layer: str | None=Query(default=None),
                            direction: str | None=Query(default=None)):
    """#6 授权关系邻域（契约 §5.6；`truncated` / `unreached_neighbors` 由服务层给）。"""
    from .wiki_query import entry_neighborhood
    return jsonable_encoder(entry_neighborhood(entry_id,_write_dsn(),request.state.identity,
                                               depth=depth,limit=limit,layer=layer,
                                               direction=direction))


@app.get('/v1/wiki/entries/{entry_id}/provenance')
def wiki_entry_provenance(entry_id: str,request: Request,revision_id: str | None=Query(default=None),
                          limit: int=Query(default=50,ge=1,le=200),
                          cursor: str | None=Query(default=None),
                          layer: str | None=Query(default=None)):
    """#7 固定版本的证据与执行事件摘要（契约 §5.7；轨迹读取失败时保留可用部分并显式标注）。

    刻意**不**把 `events_unavailable` 升级成 502：契约 §8.2 的硬要求是"不得显示成无数据"，
    服务层用 `summary.events_unavailable` + `error_summary.unavailable` + `WIKI_LOAD_FAILED`
    警告三条满足它，比整单失败更有用（`wiki_query.py` 的 docstring 已把这个判断摆在明面上）。
    """
    from .wiki_query import entry_provenance
    return jsonable_encoder(entry_provenance(entry_id,_write_dsn(),request.state.identity,
                                             revision_id=revision_id,limit=limit,cursor=cursor,
                                             layer=layer))


@app.get('/v1/wiki/entries/{entry_id}/export')
def wiki_entry_export(entry_id: str,request: Request,revision_id: str | None=Query(default=None),
                      expected_body_sha256: str | None=Query(default=None)):
    """#8 带状态与引用的 Markdown（契约 §5.8；`no-store` + `attachment` 由服务层的头给）。"""
    from .wiki_query import entry_export
    export=entry_export(entry_id,_write_dsn(),request.state.identity,revision_id=revision_id,
                        expected_body_sha256=expected_body_sha256)
    return Response(export['markdown'],media_type=export['media_type'],headers=export['headers'])


@app.post('/v1/wiki/builds',status_code=202)
def wiki_build_submit(payload: dict,request: Request):
    """#9 从指定运行创建候选草稿（契约 §5.9；权限 `reviewer`，按 `(owner, request_id)` 幂等）。

    返回任务状态、不阻塞：整理由 `wiki_jobs` 的任务行承载（`queued`），失败只记在任务行上，
    绝不改写原问答的 `runs` / `claims` / `trace_events`。
    """
    from .wiki_jobs import submit_build
    return jsonable_encoder(submit_build(payload,_write_dsn(),request.state.identity))


@app.get('/v1/wiki/builds/{build_id}')
def wiki_build_read(build_id: str,request: Request):
    """#10 查看自己的整理任务（契约 §5.10；无角色门，owner 校验在服务层的 SQL 里）。"""
    from .wiki_jobs import read_build
    return jsonable_encoder(read_build(build_id,_write_dsn(),request.state.identity))


@app.post('/v1/wiki/builds/{build_id}/retry')
def wiki_build_retry(build_id: str,request: Request,payload: dict | None=None):
    """#11 原请求失败重试（契约 §5.11；权限 `reviewer` + 原 owner）。

    `{"request_id": …}` 是**可选**体（省略即在原行上重试、`attempts+1`），所以这里声明为
    `dict | None`：一个必填体参数会让"不带 body 的重试"变成框架 422，与契约 §5.11 不符。
    """
    from .wiki_jobs import retry_build
    return jsonable_encoder(retry_build(build_id,payload,_write_dsn(),request.state.identity))


@app.post('/v1/wiki/entries/{entry_id}/revisions',status_code=201)
def wiki_revision_create(entry_id: str,payload: dict,request: Request):
    """#12 以基线创建新版本（契约 §5.12；权限 `reviewer`，只追加、不覆盖已存版本）。"""
    from .wiki import create_revision
    return jsonable_encoder(create_revision(entry_id,payload,_write_dsn(),request.state.identity))


@app.post('/v1/wiki/entries/{entry_id}/reviews',status_code=201)
def wiki_review_submit(entry_id: str,payload: dict,request: Request):
    """#13 对指定版本追加复核结果（契约 §5.13；权限 `reviewer`，复核记录只追加不改删）。"""
    from .wiki import submit_review
    return jsonable_encoder(submit_review(entry_id,payload,_write_dsn(),request.state.identity))


@app.post('/v1/wiki/entries/{entry_id}/publications',status_code=201)
def wiki_publication_create(entry_id: str,payload: dict,request: Request):
    """#14 发布指定版本并检查旧发布头（契约 §5.14；权限 `approver`，CAS 冲突 → 409）。

    权限**不是** `reviewer`：`reviewer` 与 `approver` 互不继承（`auth.py:195` 只给 `admin`
    旁路），发布是 `approver` 独有的写操作。
    """
    from .wiki import publish
    return jsonable_encoder(publish(entry_id,payload,_write_dsn(),request.state.identity))
