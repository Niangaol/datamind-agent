import type {
  AuthMe,
  AuthorityCandidate,
  AuthorityEvent,
  AuthorityEventInput,
  AuthorityRule,
  AuthorityRuleInput,
  ChatPayload,
  DimensionApprovalInput,
  DimensionApprovalResult,
  DimensionBindingInput,
  DimensionBindingList,
  DimensionBindingRegistrationResult,
  DimensionBindingResult,
  DimensionComparisonHistory,
  DimensionComparisonInput,
  DimensionComparisonRecord,
  DimensionMeasurementInput,
  DimensionMeasurementList,
  DimensionMeasurementResult,
  DimensionSourceListResult,
  DocumentFactsResult,
  DocumentListResult,
  DocumentSelection,
  DrawingDetail,
  DrawingEntitiesResult,
  DrawingFeatureReview,
  DrawingFeatureReviewInput,
  DrawingFeaturesResult,
  DrawingListResult,
  DrawingRegistrationInput,
  DrawingRegistrationResult,
  DrawingSourceListResult,
  DrawingVersionList,
  EvidenceGraph,
  FactApprovalHistoryResult,
  FactApprovalInput,
  FactApprovalSubmitResult,
  FactReviewHistoryResult,
  FactReviewInput,
  FactReviewSubmitResult,
  FeatureReviewHistory,
  FinalResult,
  ModelConfigCleared,
  ModelConfigView,
  ModelImportResult,
  ModelListResult,
  ModelProfile,
  ModelProfileExport,
  ModelProfileInput,
  ModelProfileListResult,
  ModelTestResult,
  RunDetail,
  RunListResult,
  SessionDetail,
  SessionListResult,
  SessionResultsPage,
  SessionSummary,
  TraceEvent,
  UploadApproveResult,
  UploadCreateResult,
  UploadDetail,
  UploadListParams,
  UploadListResult,
  UploadProvenance,
  WikiEntriesData,
  WikiEntryGraphParams,
  WikiEntryListParams,
  WikiEnvelope,
  WikiGraphData,
  WikiProvenanceData,
  WikiProvenanceParams,
  WikiSpaceGraphParams,
} from './types'

// 统一走相对路径：开发时由 Vite 代理到 127.0.0.1:8000，生产时与后端同源。
export async function health(): Promise<Record<string, any>> {
  const res = await fetch('/health')
  if (!res.ok) throw new Error(`health ${res.status}`)
  return res.json()
}

/** 运行记录列表：GET /v1/runs（分页走 limit+1/has_more 口径）。
    注意 `/v1/runs` 属于公开读前缀，无需身份；但读到的 run 不含会话归属之外的隐私字段。 */
export async function listRuns(
  params: { limit?: number; offset?: number } = {},
): Promise<RunListResult> {
  const search = new URLSearchParams()
  if (params.limit != null) search.set('limit', String(params.limit))
  if (params.offset != null) search.set('offset', String(params.offset))
  const suffix = search.toString()
  const res = await fetch(`/v1/runs${suffix ? `?${suffix}` : ''}`)
  if (!res.ok) throw new Error(`listRuns ${res.status}`)
  return res.json()
}

/** 单条运行：GET /v1/runs/{run_id}（404 = RUN_NOT_FOUND）。 */
export async function getRun(runId: string): Promise<RunDetail> {
  const res = await fetch(`/v1/runs/${encodeURIComponent(runId)}`)
  if (!res.ok) {
    const err = new Error(`run ${res.status}`)
    ;(err as any).errorCode = res.status === 404 ? 'RUN_NOT_FOUND' : `HTTP_${res.status}`
    throw err
  }
  return res.json()
}

export async function getEvidence(runId: string): Promise<EvidenceGraph> {
  const res = await fetch(`/v1/runs/${runId}/evidence`)
  if (!res.ok) throw new Error(`evidence ${res.status}`)
  return res.json()
}

export async function getEvents(runId: string): Promise<{ events: TraceEvent[] }> {
  const res = await fetch(`/v1/runs/${runId}/events`)
  if (!res.ok) throw new Error(`events ${res.status}`)
  return res.json()
}

/**
 * CSRF：0.39.0 起写操作（含 POST /v1/chat/stream）必须带 X-CSRF-Token，
 * 令牌由 GET /v1/auth/me 下发，写请求通过同源凭据与服务端 CSRF 校验，
 * 否则问答请求会以 403 CSRF_REQUIRED 失败。
 */
let csrfToken: string | null = null

export async function csrf(): Promise<string> {
  if (csrfToken) return csrfToken
  const res = await fetch('/v1/auth/me')
  if (!res.ok) throw new Error(`auth/me ${res.status}`)
  const data = await res.json()
  csrfToken = data?.csrf_token ?? ''
  return csrfToken ?? ''
}

/* ── 共享请求内核（模型接入 / 字段复核 / 权威 / 图纸 / 尺寸 / 上传统一走这两个）──
   两条后端事实决定了这里的写法：

   1. **错误信封恒为** `{"detail":{"error_code":"CODE"}}`（有时再加 details 子对象）。
      `{ok:false,…}` 只是 `/v1/model/config/test`、`/v1/model/config/models` 这类
      **探针**接口的响应体，绝不能拿它当通用错误形状。
   2. FastAPI 自身的 422（pydantic 请求体校验、Query 边界）是
      `{"detail":[{loc,msg,…}]}` 数组，**不带 error_code**。必须显式识别成
      VALIDATION_ERROR，否则 message 会退化成一段原始 JSON。

   另外：CSRF 只对**写**操作要（GET 不要），令牌来自 GET /v1/auth/me 并缓存在 csrf()。
   所有路径都是相对路径，与服务端同源——`/v1/model/*` 属于敏感读前缀，服务端会做
   同源校验，绝对地址（http://127.0.0.1:8000/...）会被 ORIGIN_NOT_ALLOWED 拒绝。 */

/** 解析错误信封 → 带 `.errorCode` 的 Error。 */
async function apiError(res: Response, name: string): Promise<Error> {
  let message = ''
  let code = ''
  try {
    const body = await res.json()
    const detail = body?.detail
    if (typeof detail === 'string') {
      message = detail
    } else if (Array.isArray(detail)) {
      // FastAPI 自己的 422：没有 error_code，只有 {loc,msg} 数组，这里拼成人能看懂的一行。
      code = 'VALIDATION_ERROR'
      message = detail
        .map((item: any) => {
          const loc = Array.isArray(item?.loc) ? item.loc.filter((k: any) => k !== 'body').join('.') : ''
          const msg = typeof item?.msg === 'string' ? item.msg : JSON.stringify(item)
          return loc ? `${loc}: ${msg}` : msg
        })
        .join('; ')
    } else if (detail && typeof detail === 'object') {
      code = typeof detail.error_code === 'string' ? detail.error_code : ''
      const extra = detail.details != null ? ` ${JSON.stringify(detail.details)}` : ''
      message = `${code || (typeof detail.message === 'string' ? detail.message : JSON.stringify(detail))}${extra}`
    }
  } catch {
    /* 空体 / 非 JSON（例如被反代截断）：message 留空，下面回落到状态码 */
  }
  const err = new Error(message || `${name} HTTP ${res.status}`)
  ;(err as any).errorCode = code || `HTTP_${res.status}`
  ;(err as any).status = res.status
  return err
}

/** 读内核：GET（或调用方显式给的 init），**不加** CSRF——读操作后端不校验。 */
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, init)
  if (!res.ok) throw await apiError(res, path)
  return res.json() as Promise<T>
}

/** 写内核：POST（可用第三参换成 PUT/PATCH/DELETE）+ csrf() + X-CSRF-Token + JSON。
    body 为 undefined 时**不**声明 Content-Type、也不发请求体：FastAPI 上无 body 的
    路由收到空体会以 422 失败，而无 body 的 POST（如 activate）本来就是合法调用。 */
async function write<T>(
  path: string,
  body?: unknown,
  method: 'POST' | 'PUT' | 'PATCH' | 'DELETE' = 'POST',
): Promise<T> {
  const token = await csrf()
  const headers: Record<string, string> = { 'X-CSRF-Token': token }
  const init: RequestInit = { method, headers }
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json'
    init.body = JSON.stringify(body)
  }
  const res = await fetch(path, init)
  if (!res.ok) throw await apiError(res, `${method} ${path}`)
  return res.json() as Promise<T>
}

/* ── 身份（GET /v1/auth/me）───────────────────────────────────────────────
   401 LOGIN_REQUIRED / SESSION_INVALID 时 user 为 null；代理会同时下发 csrf_token。
   视图层通常只需要 csrf()；要解释 403（PERMISSION_DENIED）才需要知道角色，
   所以这里只读不写、且**不**把结果并进 csrf 缓存之外的任何模块状态。 */
export async function me(): Promise<AuthMe> {
  return request<AuthMe>('/v1/auth/me')
}

/** 流式提问：POST /v1/chat/stream，手动解析 SSE（EventSource 不支持 POST）。 */
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

  if (!res.ok || !res.body) {
    let detail = ''
    try {
      const j = await res.json()
      detail = typeof j?.detail === 'string' ? j.detail : JSON.stringify(j)
    } catch {
      /* ignore */
    }
    throw new Error(detail || `HTTP ${res.status}`)
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let final: FinalResult | null = null

  const processChunk = (chunk: string) => {
    let data: string | null = null
    for (const line of chunk.split('\n')) {
      if (line.startsWith('data:')) data = line.slice(5).trim()
    }
    if (!data) return
    const ev: TraceEvent = JSON.parse(data)
    onEvent(ev)
    if (
      ev.event_type === 'answer.completed' ||
      ev.event_type === 'answer.abstained' ||
      ev.event_type === 'answer.clarification' ||
      ev.event_type === 'run.failed'
    ) {
      final = ev.output_summary as unknown as FinalResult
    }
  }

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let idx: number
    while ((idx = buffer.indexOf('\n\n')) >= 0) {
      const raw = buffer.slice(0, idx)
      buffer = buffer.slice(idx + 2)
      if (raw.trim()) processChunk(raw)
    }
  }
  if (buffer.trim()) processChunk(buffer)

  if (!final) throw new Error('未收到最终结果（流可能中断）')
  return final
}

/**
 * 当前启用配置：GET /v1/model/config。
 * 它属于 SENSITIVE_READ_PREFIXES，中间件会做同源校验——必须与后端同源调用
 * （所以开发时也走 vite 代理，不要直连另一个端口的绝对地址）。
 * 返回体只说明「配了没有」，不含连通性：has_key 为 true 只等于密钥已填写。
 */
export async function modelConfig(): Promise<ModelConfigView> {
  const res = await fetch('/v1/model/config')
  if (!res.ok) {
    let detail = ''
    try {
      const j = await res.json()
      detail = typeof j?.detail === 'string' ? j.detail : JSON.stringify(j?.detail ?? j)
    } catch {
      /* ignore */
    }
    throw new Error(detail || `model/config HTTP ${res.status}`)
  }
  return res.json()
}

/**
 * 连通性检测：POST /v1/model/config/test（后端 api.py::probe_chat）。
 * 请求体就是 ProfileRequest，全部字段可选；**空对象 `{}` 即合法输入**——
 * resolve_profile 会取「当前启用配置 + 环境变量 + 默认值」解析出的 profile，
 * 与 GET /v1/model/config 同一份来源。想测别的目标可传
 * {profile_id} 或 {base_url, model, api_key, protocol}。
 * 这是写操作：需要 CSRF 令牌，且服务端做同源校验。
 * 注意它**不会写库、也不落盘**任何检测结果——持久化由前端自己负责。
 */
export async function testModelConfig(
  body: Record<string, any> = {},
): Promise<ModelTestResult> {
  const token = await csrf()
  const res = await fetch('/v1/model/config/test', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': token },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    let detail = ''
    try {
      const j = await res.json()
      const d = j?.detail
      detail = typeof d === 'string' ? d : (d?.error_code || d?.message || JSON.stringify(d ?? j))
    } catch {
      /* ignore */
    }
    const err = new Error(detail || `model/config/test HTTP ${res.status}`)
    ;(err as any).errorCode = `HTTP_${res.status}`
    throw err
  }
  return res.json()
}

/* ── 会话管理（后端 datamind/sessions.py）─────────────────────────────────
   全部要求身份（不在任何 PUBLIC_* 清单里 → 默认拒绝），写操作还要
   X-CSRF-Token + 同源；响应头是 Cache-Control: no-store。

   错误解析刻意用 testModelConfig 那一套（解 detail.error_code / detail.message），
   不能用 streamChat 的：后者只认**字符串** detail，而会话接口返回的是
   {"detail":{"error_code":"…"}}，直接抛出去会让气泡里显示一段原始 JSON。
   实现已并入共享的 apiError（同一套信封解析），这里只保留名字与调用点。 */

async function sessionError(res: Response, name: string): Promise<Error> {
  return apiError(res, name)
}

/** 会话列表 + 检索：GET /v1/sessions。q 为空/空白即不过滤，由这里剔除。 */
export async function listSessions(
  params: { q?: string; limit?: number; offset?: number } = {},
): Promise<SessionListResult> {
  const search = new URLSearchParams()
  // 只带真正有值的参数：后端把空串当「无搜索词」，但显式省略更干净。
  if (params.q != null && String(params.q).trim() !== '') search.set('q', String(params.q).trim())
  if (params.limit != null) search.set('limit', String(params.limit))
  if (params.offset != null) search.set('offset', String(params.offset))
  const suffix = search.toString()
  const res = await fetch(`/v1/sessions${suffix ? `?${suffix}` : ''}`)
  if (!res.ok) throw await sessionError(res, 'listSessions')
  return res.json()
}

/** 会话详情：GET /v1/sessions/{id}。messages 是一个 run 一条，不是聊天消息。 */
export async function getSession(sessionId: string): Promise<SessionDetail> {
  const res = await fetch(`/v1/sessions/${encodeURIComponent(sessionId)}`)
  if (!res.ok) throw await sessionError(res, 'getSession')
  return res.json()
}

/**
 * 历史会话的富内容：GET /v1/sessions/{id}/results（最近优先，按 run 分页）。
 *
 * 为什么与 getSession 分开：终态负载平均 14 kB/轮、最大 140 kB，把全量富内容塞进详情
 * 会让一个 221 轮的会话返回约 4 MB（见 datamind/sessions.py::results）。这里只取窗口，
 * 前端按 run_id 合并到已经渲染的正文气泡上。
 */
export async function listSessionResults(
  sessionId: string,
  params: { limit?: number; offset?: number } = {},
): Promise<SessionResultsPage> {
  const search = new URLSearchParams()
  if (params.limit != null) search.set('limit', String(params.limit))
  if (params.offset != null) search.set('offset', String(params.offset))
  const suffix = search.toString()
  const res = await fetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/results${suffix ? `?${suffix}` : ''}`,
  )
  if (!res.ok) throw await sessionError(res, 'listSessionResults')
  return res.json()
}

/** 新建会话：POST /v1/sessions。不传 title 时后端用默认标题，并在首轮提问后自动命名。 */
export async function createSession(title?: string): Promise<{ session: SessionSummary }> {
  const token = await csrf()
  const body = title != null && title.trim() !== '' ? { title: title.trim() } : {}
  const res = await fetch('/v1/sessions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': token },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw await sessionError(res, 'createSession')
  return res.json()
}

/** 重命名：PATCH /v1/sessions/{id}。空白标题由后端以 422 SESSION_TITLE_REQUIRED 拒绝。 */
export async function renameSession(
  sessionId: string,
  title: string,
): Promise<{ session: SessionSummary }> {
  const token = await csrf()
  const res = await fetch(`/v1/sessions/${encodeURIComponent(sessionId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': token },
    body: JSON.stringify({ title }),
  })
  if (!res.ok) throw await sessionError(res, 'renameSession')
  return res.json()
}

/** 删除：DELETE /v1/sessions/{id}。后端是**软删**（保留 runs 的审计链），
    删后再查同一个 id 会得到 404 SESSION_NOT_FOUND。 */
export async function deleteSession(sessionId: string): Promise<{ session: SessionSummary }> {
  const token = await csrf()
  const res = await fetch(`/v1/sessions/${encodeURIComponent(sessionId)}`, {
    method: 'DELETE',
    headers: { 'X-CSRF-Token': token },
  })
  if (!res.ok) throw await sessionError(res, 'deleteSession')
  return res.json()
}

/* ══════════════════════════════════════════════════════════════════════════
   1) 模型接入（/v1/model/*）
   **全部路由要求 admin，包括读取**：配置视图带端点与密钥掩码，中间件把整个
   /v1/model/ 前缀归到 admin。所以 GET 也可能 403 PERMISSION_DENIED——视图要按
   「权限不足」如实解释，不能显示成「加载失败」或「没有配置」。
   ══════════════════════════════════════════════════════════════════════════ */

/** 已保存的接入配置 + 可选协议。读取侧不下发任何凭据值（密钥只给掩码、敏感头只给键名）。 */
export async function getModelProfiles(): Promise<ModelProfileListResult> {
  return request<ModelProfileListResult>('/v1/model/profiles')
}

/** 新建配置：name 为空后端 400 PROFILE_NAME_REQUIRED；返回单条 public_profile。 */
export async function createModelProfile(body: ModelProfileInput): Promise<ModelProfile> {
  return write<ModelProfile>('/v1/model/profiles', body)
}

/** 更新配置：`api_key` / `headers` / `proxy` 留空表示**保持原值**（键级合并），
    不是清空——表单看不到真实凭据是设计，不能把「表单空的」当成「用户要删」。 */
export async function updateModelProfile(
  id: string,
  body: ModelProfileInput,
): Promise<ModelProfile> {
  return write<ModelProfile>(`/v1/model/profiles/${encodeURIComponent(id)}`, body, 'PUT')
}

/** 删除配置：返回删除后的完整列表（active 可能因此换人）。 */
export async function deleteModelProfile(id: string): Promise<ModelProfileListResult> {
  return write<ModelProfileListResult>(
    `/v1/model/profiles/${encodeURIComponent(id)}`,
    undefined,
    'DELETE',
  )
}

/** 激活配置：无请求体（不要发 `{}`，这里刻意让 write 不带 body）。 */
export async function activateModelProfile(id: string): Promise<ModelProfileListResult> {
  return write<ModelProfileListResult>(
    `/v1/model/profiles/${encodeURIComponent(id)}/activate`,
  )
}

/** 导出配置：默认脱敏（不含 api_key、剔除敏感头、代理去 userinfo）。
    include_keys=true 是**显式**要求把凭据一起搬走，返回体里的 include_keys 就是证明。 */
export async function exportModelProfiles(includeKeys = false): Promise<ModelProfileExport> {
  const search = new URLSearchParams({ include_keys: String(!!includeKeys) })
  return request<ModelProfileExport>(`/v1/model/profiles/export?${search.toString()}`)
}

/** 导入配置（ccswitch 风格搬迁）：同名不覆盖，一律作为新配置写入；上限 50 条。 */
export async function importModelProfiles(body: {
  profiles: ModelProfileExport['profiles']
  activate_first: boolean
}): Promise<ModelImportResult> {
  return write<ModelImportResult>('/v1/model/profiles/import', body)
}

/** 写当前启用配置（PUT /v1/model/config，兼容单配置调用方）。
    `api_key` 传 "clear"/"none"/"-" 才是清空密钥，空/缺省 = 保持原密钥。 */
export async function putModelConfig(body: ModelProfileInput): Promise<ModelConfigView> {
  return write<ModelConfigView>('/v1/model/config', body, 'PUT')
}

/** 清空启用配置：返回体**只有** {has_key,key_masked,source}，刻意不复用 ModelConfigView
    ——那个有 21 个键，混用会让调用方以为还能读到端点/模型。 */
export async function deleteModelConfig(): Promise<ModelConfigCleared> {
  return write<ModelConfigCleared>('/v1/model/config', undefined, 'DELETE')
}

/** 上游模型列表（POST /v1/model/config/models）。空对象 `{}` 合法：解析当前启用配置。
    跨源指定 base_url 且未自带密钥时后端 400 MODEL_KEY_NOT_INHERITED（不给已存密钥外泄）。 */
export async function listModels(body: ModelProfileInput = {}): Promise<ModelListResult> {
  return write<ModelListResult>('/v1/model/config/models', body)
}

/* ══════════════════════════════════════════════════════════════════════════
   2) 字段复核（/v1/documents*）
   ⚠ 命名陷阱：列表用 doc_version/sha256，事实接口用 document_version/source_sha256，
   是同一行的**同一个值**（见 types.ts 的 DocumentSummary / DocumentFactsResult 注释）。
   列表还多三个派生字段 searchable/index_state/index_note：收录 ≠ 可检索。
   ══════════════════════════════════════════════════════════════════════════ */

/** 文档列表（可选 collection 过滤）。非白名单 collection → 400 COLLECTION_NOT_ALLOWED。
    选文件给问答用时必须要求 `searchable === true`，仅「已发布」不足以说明检索得到。 */
export async function listDocuments(collection?: string): Promise<DocumentListResult> {
  const search = new URLSearchParams()
  if (collection != null && String(collection).trim() !== '') {
    search.set('collection', String(collection).trim())
  }
  const suffix = search.toString()
  return request<DocumentListResult>(`/v1/documents${suffix ? `?${suffix}` : ''}`)
}

/** 文档事实（已通过定位器哈希校验的才算数）。`quality` 是抽取质量摘要，可为 null。 */
export async function getDocumentFacts(documentId: string): Promise<DocumentFactsResult> {
  return request<DocumentFactsResult>(`/v1/documents/${encodeURIComponent(documentId)}/facts`)
}

/** 抽取复核历史（只查抽取保真度，与来源权威/冲突消解无关）。 */
export async function getFactReviews(
  documentId: string,
  factId: string,
): Promise<FactReviewHistoryResult> {
  return request<FactReviewHistoryResult>(
    `/v1/documents/${encodeURIComponent(documentId)}/facts/${encodeURIComponent(factId)}/reviews`,
  )
}

/** 审批历史（独立的版本绑定审批；不覆盖源文件自己的批准声明）。 */
export async function getFactApprovals(
  documentId: string,
  factId: string,
): Promise<FactApprovalHistoryResult> {
  return request<FactApprovalHistoryResult>(
    `/v1/documents/${encodeURIComponent(documentId)}/facts/${encodeURIComponent(factId)}/approvals`,
  )
}

/** 提交复核（reviewer）。version/payload 哈希必须与当前事实一致，否则 409
    FACT_VERSION_MISMATCH；previous_review_id 必须等于当前链尾，否则 409 REVIEW_STATE_CHANGED。 */
export async function submitFactReview(
  documentId: string,
  factId: string,
  body: FactReviewInput,
): Promise<FactReviewSubmitResult> {
  return write<FactReviewSubmitResult>(
    `/v1/documents/${encodeURIComponent(documentId)}/facts/${encodeURIComponent(factId)}/reviews`,
    body,
  )
}

/** 提交审批（approver）。必须绑一条 decision='confirmed' 的人工复核，且复核人 ≠ 审批人。 */
export async function submitFactApproval(
  documentId: string,
  factId: string,
  body: FactApprovalInput,
): Promise<FactApprovalSubmitResult> {
  return write<FactApprovalSubmitResult>(
    `/v1/documents/${encodeURIComponent(documentId)}/facts/${encodeURIComponent(factId)}/approvals`,
    body,
  )
}

/**
 * 版本/哈希钉住参数：给了哪个就发哪个，**不**替调用方补另一个。
 * 只给一个是后端的 422 DOCUMENT_SELECTION_INVALID——这是刻意的：宁可图片/链接直接坏掉
 * （可见的失败），也绝不在声称「第 N 版」的位置静默渲染当前版本的字节。
 * 两个都不给 = 明确读当前版本。
 */
function selectionQuery(selection: DocumentSelection): string {
  const search = new URLSearchParams()
  if (selection.version != null) search.set('expected_version', String(selection.version))
  if (selection.sha256 != null && selection.sha256 !== '') {
    search.set('expected_sha256', selection.sha256)
  }
  const suffix = search.toString()
  return suffix ? `?${suffix}` : ''
}

/** 页面图片 URL（`<img src>`，公开 GET，**不是** fetch 调用）。 */
export function pageImageUrl(
  documentId: string,
  pageNo: number,
  selection: DocumentSelection = {},
): string {
  return `/v1/documents/${encodeURIComponent(documentId)}/pages/${pageNo}${selectionQuery(selection)}`
}

/** 原文件 URL（`<a href>` / 内联预览，公开 GET）。不含钉住参数时读的是**当前版本**。 */
export function documentFileUrl(
  documentId: string,
  selection: DocumentSelection = {},
): string {
  return `/v1/documents/${encodeURIComponent(documentId)}/file${selectionQuery(selection)}`
}

/* ══════════════════════════════════════════════════════════════════════════
   3) 来源权威规则（/v1/authority/*）
   权威只来自**显式登记**：来源名字（「质检报告」）不构成任何权威。冲突两侧原值都要
   显示；payload_valid=false 表示登记载荷与哈希对不上，整条规则不可作依据。
   ══════════════════════════════════════════════════════════════════════════ */

/** 未消解冲突的候选项（admin）。问题文本解析不出候选时后端 422（error_code 为原因串）。 */
export async function authorityCandidates(
  question: string,
): Promise<{ candidates: AuthorityCandidate[] }> {
  const search = new URLSearchParams({ question })
  return request<{ candidates: AuthorityCandidate[] }>(
    `/v1/authority/candidates?${search.toString()}`,
  )
}

/** 规则注册表（附带最新激活事件与 payload_valid）。 */
export async function listAuthorityRules(): Promise<{ rules: AuthorityRule[] }> {
  return request<{ rules: AuthorityRule[] }>('/v1/authority/rules')
}

/** 登记规则（admin）。候选上下文变了 → 409 AUTHORITY_CONTEXT_CHANGED；
    报告侧事实未正式批准 → 409 APPROVED_FIELD_REQUIRED；reason/rationale 过短是 422。 */
export async function registerAuthorityRule(
  body: AuthorityRuleInput,
): Promise<{ rule: AuthorityRule }> {
  return write<{ rule: AuthorityRule }>('/v1/authority/rules', body)
}

/** 某条规则的激活/撤销事件链（按时间正序）。 */
export async function authorityRuleEvents(ruleId: string): Promise<{ events: AuthorityEvent[] }> {
  return request<{ events: AuthorityEvent[] }>(
    `/v1/authority/rules/${encodeURIComponent(ruleId)}/events`,
  )
}

/** 激活/驳回/撤销规则（approver）。创建人不能自己批准；previous_event_id 必须等于链尾。 */
export async function submitAuthorityEvent(
  ruleId: string,
  body: AuthorityEventInput,
): Promise<{ event: AuthorityEvent }> {
  return write<{ event: AuthorityEvent }>(
    `/v1/authority/rules/${encodeURIComponent(ruleId)}/events`,
    body,
  )
}

/* ══════════════════════════════════════════════════════════════════════════
   4) 图纸与实体（/v1/drawings/*）
   全部只证明「本地解析 + 人工关联复核」；**不**等于图纸正式批准、生产绑定或质量结论。
   两条使用门槛（inspection_gate / review_gate）与 semantics 必须跟随数据一起渲染。
   ══════════════════════════════════════════════════════════════════════════ */

/** 图纸资产列表（每份只列当前版本）。 */
export async function listDrawings(): Promise<DrawingListResult> {
  return request<DrawingListResult>('/v1/drawings')
}

/** 可登记的 .dxf 源文件（admin）。 */
export async function listDrawingSources(): Promise<DrawingSourceListResult> {
  return request<DrawingSourceListResult>('/v1/drawings/sources')
}

/** 版本历史（含 current_version）。 */
export async function drawingVersions(documentId: string): Promise<DrawingVersionList> {
  return request<DrawingVersionList>(
    `/v1/drawings/${encodeURIComponent(documentId)}/versions`,
  )
}

/** 图纸详情。省略 version = 读**当前**版本（UI 必须把读的是哪一版显示出来）。 */
export async function getDrawing(documentId: string, version?: number): Promise<DrawingDetail> {
  return request<DrawingDetail>(
    `/v1/drawings/${encodeURIComponent(documentId)}${versionQuery(version)}`,
  )
}

/** 图元 + 视图。 */
export async function drawingEntities(
  documentId: string,
  version?: number,
): Promise<DrawingEntitiesResult> {
  return request<DrawingEntitiesResult>(
    `/v1/drawings/${encodeURIComponent(documentId)}/entities${versionQuery(version)}`,
  )
}

/** 尺寸要素 + 两道门槛 + 关联复核快照（association_reviews 里的 latest_review 为 null
    就代表该要素**从未**被人工确认过）。 */
export async function drawingFeatures(
  documentId: string,
  version?: number,
): Promise<DrawingFeaturesResult> {
  return request<DrawingFeaturesResult>(
    `/v1/drawings/${encodeURIComponent(documentId)}/features${versionQuery(version)}`,
  )
}

/** 单个要素的关联复核历史。 */
export async function featureReviews(
  documentId: string,
  featureId: string,
  version?: number,
): Promise<FeatureReviewHistory> {
  return request<FeatureReviewHistory>(
    `/v1/drawings/${encodeURIComponent(documentId)}/features/${encodeURIComponent(featureId)}/reviews${versionQuery(version)}`,
  )
}

/** 记录关联复核（reviewer）。confirmed 必带属于 target_candidates 的 selected_target。 */
export async function submitFeatureReview(
  documentId: string,
  featureId: string,
  body: DrawingFeatureReviewInput,
): Promise<{ review: DrawingFeatureReview }> {
  return write<{ review: DrawingFeatureReview }>(
    `/v1/drawings/${encodeURIComponent(documentId)}/features/${encodeURIComponent(featureId)}/reviews`,
    body,
  )
}

/** 登记图纸版本（admin）。previous_version/previous_source_sha256 必须等于当前链尾。 */
export async function registerDrawing(
  body: DrawingRegistrationInput,
): Promise<DrawingRegistrationResult> {
  return write<DrawingRegistrationResult>('/v1/drawings', body)
}

/** `?version=` 查询参数（省略 = 当前版本，由服务端 COALESCE 决定）。 */
function versionQuery(version?: number | null): string {
  return version != null ? `?version=${encodeURIComponent(String(version))}` : ''
}

/** 原始 DXF（`<a href>` 下载/查看，公开 GET）。 */
export function drawingFileUrl(documentId: string, version?: number | null): string {
  return `/v1/drawings/${encodeURIComponent(documentId)}/file${versionQuery(version)}`
}

/** 服务端渲染的 SVG 预览（`<img src>`）。 */
export function drawingPreviewUrl(documentId: string, version?: number | null): string {
  return `/v1/drawings/${encodeURIComponent(documentId)}/preview${versionQuery(version)}`
}

/** 服务端渲染的 HTML 查看器（`<iframe src>`；CSP 为 sandbox allow-scripts）。 */
export function drawingViewerUrl(documentId: string, version?: number | null): string {
  return `/v1/drawings/${encodeURIComponent(documentId)}/viewer${versionQuery(version)}`
}

/* ══════════════════════════════════════════════════════════════════════════
   5) 尺寸核验（/v1/dimensions/*）
   记录链是「注册 → 人工关联复核 → 独立使用审批 → 对照」。任何结论的
   authentication_verified / formal_drawing_approval / product_quality_conclusion
   都是 false，且对照结果**不是**制造授权或产品质量判定。
   ══════════════════════════════════════════════════════════════════════════ */

/** 可登记的尺寸记录源文件（admin）。 */
export async function listDimensionSources(): Promise<DimensionSourceListResult> {
  return request<DimensionSourceListResult>('/v1/dimensions/sources')
}

/** 尺寸记录列表（id + 当前版本）。
    ⚠ 后端的列表路由**没有** version 查询参数（只有单条详情有）。这里按契约透传，
    服务端会忽略它——所以返回体的 truncated/current_version 才是唯一可信信息，
    **不要**把 version 当成已生效的过滤条件；要按版本读内容请用 getMeasurement。 */
export async function listMeasurements(version?: number): Promise<DimensionMeasurementList> {
  const search = new URLSearchParams()
  if (version != null) search.set('version', String(version))
  const suffix = search.toString()
  return request<DimensionMeasurementList>(
    `/v1/dimensions/measurements${suffix ? `?${suffix}` : ''}`,
  )
}

/** 绑定列表（id + 当前版本）。 */
export async function listBindings(): Promise<DimensionBindingList> {
  return request<DimensionBindingList>('/v1/dimensions/bindings')
}

/** 单条尺寸记录（省略 version = 当前版本）。 */
export async function getMeasurement(
  measurementId: string,
  version?: number,
): Promise<DimensionMeasurementResult> {
  return request<DimensionMeasurementResult>(
    `/v1/dimensions/measurements/${encodeURIComponent(measurementId)}${versionQuery(version)}`,
  )
}

/** 单条绑定：绑定内容 + 审批链 + 当前对照结论 + 认证范围说明。 */
export async function getBinding(
  bindingId: string,
  version?: number,
): Promise<DimensionBindingResult> {
  return request<DimensionBindingResult>(
    `/v1/dimensions/bindings/${encodeURIComponent(bindingId)}${versionQuery(version)}`,
  )
}

/** 登记尺寸记录（admin，追加新版本）。 */
export async function registerMeasurement(
  body: DimensionMeasurementInput,
): Promise<DimensionMeasurementResult> {
  return write<DimensionMeasurementResult>('/v1/dimensions/measurements', body)
}

/**
 * 登记绑定（admin）。必须先有 confirmed 关联复核，否则 409
 * DIMENSION_CONFIRMED_ASSOCIATION_REQUIRED；上游版本变了是 DIMENSION_INPUT_VERSION_CHANGED。
 * 注意返回体**只有**新登记的绑定（没有审批链、没有当前结论）——那两样要再 GET 详情，
 * 所以这里刻意不用 DimensionBindingResult。
 */
export async function registerBinding(
  body: DimensionBindingInput,
): Promise<DimensionBindingRegistrationResult> {
  return write<DimensionBindingRegistrationResult>('/v1/dimensions/bindings', body)
}

/** 使用审批（approver）。审批人必须独立于登记人与复核人。 */
export async function submitBindingApproval(
  bindingId: string,
  body: DimensionApprovalInput,
): Promise<DimensionApprovalResult> {
  return write<DimensionApprovalResult>(
    `/v1/dimensions/bindings/${encodeURIComponent(bindingId)}/approvals`,
    body,
  )
}

/** 保存一次对照（admin）：服务端按冻结的评估器重算并落库，返回的 replay=false 表示新算。 */
export async function saveComparison(
  bindingId: string,
  body: DimensionComparisonInput,
): Promise<DimensionComparisonRecord> {
  return write<DimensionComparisonRecord>(
    `/v1/dimensions/bindings/${encodeURIComponent(bindingId)}/comparisons`,
    body,
  )
}

/** 对照历史（分页；next_offset 为 null 表示没有更多）。 */
export async function comparisonHistory(
  bindingId: string,
  params: { offset?: number; limit?: number } = {},
): Promise<DimensionComparisonHistory> {
  const search = new URLSearchParams()
  if (params.offset != null) search.set('offset', String(params.offset))
  if (params.limit != null) search.set('limit', String(params.limit))
  const suffix = search.toString()
  return request<DimensionComparisonHistory>(
    `/v1/dimensions/bindings/${encodeURIComponent(bindingId)}/comparisons${suffix ? `?${suffix}` : ''}`,
  )
}

/** 按 id 重放一条历史对照（replay=true，用它就能证明历史结论仍可复现）。 */
export async function getComparison(comparisonId: string): Promise<DimensionComparisonRecord> {
  return request<DimensionComparisonRecord>(
    `/v1/dimensions/comparisons/${encodeURIComponent(comparisonId)}`,
  )
}

/* ══════════════════════════════════════════════════════════════════════════
   6) 我的文件 / 上传管道（/v1/uploads，全部 admin）
   三条不许含糊的事实：
     1. 唯一上传入口是 `POST /v1/uploads?filename=&provenance=`，**raw bytes**，
        不是 multipart；本文件**不提供** /v1/files/uploads 或 /v1/files/jobs
        （那是退役的 file_jobs 契约）；
     2. 解析由**独立 worker 进程**推进，关页面不会推进它 → worker.alive=false 时
        UI 必须说「后台处理器未运行」，不能把 queued 说成「解析中」；
     3. 审批只解开复核闸门；解析波次会建立 chunks 与向量（vector_index='embedded'），
        但未复核内容对检索器不可见。approve 之后 searchable/indexed 才可能为 true；
        建索引失败（vector_index='failed'）时 approve 只会得到 no_chunks。
   ══════════════════════════════════════════════════════════════════════════ */

/**
 * 上传一个文件（唯一的 201 路径）：查询参数带 filename/provenance，body 是 File 本身，
 * Content-Type 固定 application/octet-stream，另加 X-CSRF-Token。
 * 返回 = **已排队**（status 多为 queued/running），不是「已解析」。文件名后缀不在
 * 白名单是 415（.dwg 也是 415，仓库没有 DWG 转换器），文件名不合法是 422。
 */
export async function uploadFile(
  file: File,
  filename: string,
  provenance: UploadProvenance,
): Promise<UploadCreateResult> {
  const search = new URLSearchParams({ filename, provenance })
  const token = await csrf()
  const res = await fetch(`/v1/uploads?${search.toString()}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/octet-stream', 'X-CSRF-Token': token },
    body: file,
  })
  if (!res.ok) throw await apiError(res, 'uploadFile')
  return res.json() as Promise<UploadCreateResult>
}

/** 上传列表 + **worker 心跳**（worker.alive 是判断「队列有没有人在处理」的唯一依据）。 */
export async function listUploads(params: UploadListParams = {}): Promise<UploadListResult> {
  const search = new URLSearchParams()
  if (params.status != null && String(params.status).trim() !== '') {
    search.set('status', String(params.status).trim())
  }
  if (params.media_type != null && String(params.media_type).trim() !== '') {
    search.set('media_type', String(params.media_type).trim())
  }
  if (params.since != null && String(params.since).trim() !== '') {
    search.set('since', String(params.since).trim())
  }
  if (params.limit != null) search.set('limit', String(params.limit))
  const suffix = search.toString()
  return request<UploadListResult>(`/v1/uploads${suffix ? `?${suffix}` : ''}`)
}

/** 单条上传详情（带 analysis/vector_index/contract_url/worker）。纯读：不会触发解析。 */
export async function getUpload(uploadId: string): Promise<UploadDetail> {
  return request<UploadDetail>(`/v1/uploads/${encodeURIComponent(uploadId)}`)
}

/** 审批通过（admin）：解除复核闸门。返回体多一个 `indexed`（= searchable）；
    它和 searchable 都为 false 时不能说「已可检索」（通常是没有 chunks / 建索引失败）。 */
export async function approveUpload(uploadId: string): Promise<UploadApproveResult> {
  return write<UploadApproveResult>(`/v1/uploads/${encodeURIComponent(uploadId)}/approve`)
}

/* ══════════════════════════════════════════════════════════════════════════
   7) Wiki 知识图谱（/v1/wiki/*，契约 docs/WIKI_API.md）

   四件必须照实处理的事：

     (a) **信封是 `{data, meta, paging, warnings}`**，不是裸对象。`request<T>` 直接透传，
         所以类型参数一律是 `WikiEnvelope<...>`。
     (b) `/v1/wiki` **不在** `PUBLIC_READ_PREFIXES` 里（契约 §2.2 明令不得加入），
         所以这些 GET 也需要身份；未登录是 401 LOGIN_REQUIRED，不是"没有数据"。
         错误信封仍是 `{"detail":{"error_code":…}}`，`request()` 已经按它解析。
     (c) **`limit` 不许越界**：目录 ≤ 50、空间图固定 ≤ 50、邻域 ≤ 100、证据 ≤ 200。
         这里只做"不主动超发"，真正的判定在服务端（越界是框架 422）。
     (d) `layer` 或筛选变化时**服务端会忽略 cursor 并返回第一批**（`warnings` 里带
         `CURSOR_RESET_ON_FILTER_CHANGE`）。调用方**必须同时清空已加载集合**，
         否则"继续加载绕过上限"会在客户端侧发生（契约 §4 语义 4）。下面每个函数都
         把 `cursor` 原样透传，重置的责任在调用方，函数不替它猜。
   ══════════════════════════════════════════════════════════════════════════ */

/** 契约 §4 的四个上限（服务端同一组数字；前端只用它来"不主动超发"与显示说明）。 */
export const WIKI_PAGE_LIMITS = {
  entries: { default: 20, max: 50 },
  graph: { default: 50, max: 50 },
  neighborhood: { default: 50, max: 100 },
  provenance: { default: 50, max: 200 },
} as const

function cappedLimit(value: number | undefined, fallback: number, max: number): string {
  const n = Number.isFinite(value) ? Math.floor(value as number) : fallback
  return String(Math.min(max, Math.max(1, n)))
}

/** 「可重复」查询参数：同一个键出现多次（FastAPI 的 `list[str]`）。空数组 = 不传。 */
function appendRepeated(search: URLSearchParams, key: string, values?: string[] | string | null): void {
  if (values == null) return
  const list = Array.isArray(values) ? values : [values]
  for (const value of list) {
    const text = String(value).trim()
    if (text !== '') search.append(key, text)
  }
}

function trimOrNull(value?: string | null): string | null {
  if (value == null) return null
  const text = String(value).trim()
  return text === '' ? null : text
}

/**
 * #1 词条目录与关键词搜索：`GET /v1/wiki/entries`。
 *
 * `search.body_searched=false` 时**正文没有被搜过**（中文正文检索策略未定，
 * 响应里的 `search` 段会如实声明本次覆盖的字段），界面必须原样呈现这句话，
 * 否则用户会以为搜了正文。空白 `q` 等于不传（沿用 `/v1/sessions` 的语义）。
 */
export async function listWikiEntries(
  params: WikiEntryListParams = {},
): Promise<WikiEnvelope<WikiEntriesData>> {
  const search = new URLSearchParams()
  const q = trimOrNull(params.q)
  if (q != null) search.set('q', q)
  appendRepeated(search, 'type', params.type)
  const status = trimOrNull(params.status)
  if (status != null) search.set('status', status)
  const category = trimOrNull(params.category)
  if (category != null) search.set('category', category)
  const updatedSince = trimOrNull(params.updated_since)
  if (updatedSince != null) search.set('updated_since', updatedSince)
  const spaceId = trimOrNull(params.space_id)
  if (spaceId != null) search.set('space_id', spaceId)
  search.set('limit', cappedLimit(params.limit, WIKI_PAGE_LIMITS.entries.default, WIKI_PAGE_LIMITS.entries.max))
  if (params.cursor) search.set('cursor', params.cursor)
  return request<WikiEnvelope<WikiEntriesData>>(`/v1/wiki/entries?${search.toString()}`)
}

/**
 * #2 当前授权范围图：`GET /v1/wiki/spaces/{space_id}/graph`。
 *
 * ⚠ 这个接口**不是**"全库全图"，界面标题必须用响应里的 `scope_description`。
 * `limit` 是**每批节点数**（服务端固定上限 50）；累计到 100 节点 / 300 关系后
 * `next_cursor` 变 null、`limit_reached` 非空 —— 那时不要再请求下一页。
 *
 * `q`/`type`/`category`/`status` **只对 `layer=entries` 有意义**：对另外两层传它们
 * 后端会 422（它拒绝静默忽略）。所以这里只在 `layer` 为 entries（或缺省）时才带上，
 * 避免调用方无意间触发一个看不懂的 422。
 */
export async function wikiSpaceGraph(
  spaceId: string,
  params: WikiSpaceGraphParams = {},
): Promise<WikiEnvelope<WikiGraphData>> {
  const search = new URLSearchParams()
  const layer = params.layer ?? 'entries'
  search.set('layer', layer)
  if (layer === 'entries') {
    const q = trimOrNull(params.q)
    if (q != null) search.set('q', q)
    const type = trimOrNull(params.type)
    if (type != null) search.set('type', type)
    const category = trimOrNull(params.category)
    if (category != null) search.set('category', category)
    const status = trimOrNull(params.status)
    if (status != null) search.set('status', status)
  }
  search.set('limit', cappedLimit(params.limit, WIKI_PAGE_LIMITS.graph.default, WIKI_PAGE_LIMITS.graph.max))
  if (params.cursor) search.set('cursor', params.cursor)
  return request<WikiEnvelope<WikiGraphData>>(
    `/v1/wiki/spaces/${encodeURIComponent(spaceId)}/graph?${search.toString()}`,
  )
}

/**
 * #6 授权关系邻域：`GET /v1/wiki/entries/{entry_id}/graph`。
 *
 * `depth` 只接受 1..2（超出是框架 422），`limit` ≤ 100 且仍受 100 节点 / 300 关系约束。
 * `truncated=true` 时**必带** `truncated_reason`；`unreached_neighbors` **只**统计
 * 当前授权范围内因截断而未返回的邻居数（非授权对象不计入）。
 */
export async function wikiEntryGraph(
  entryId: string,
  params: WikiEntryGraphParams = {},
): Promise<WikiEnvelope<WikiGraphData>> {
  const search = new URLSearchParams()
  const depth = Number.isFinite(params.depth) ? Math.floor(params.depth as number) : 1
  search.set('depth', String(Math.min(2, Math.max(1, depth))))
  search.set(
    'limit',
    cappedLimit(params.limit, WIKI_PAGE_LIMITS.neighborhood.default, WIKI_PAGE_LIMITS.neighborhood.max),
  )
  search.set('layer', params.layer ?? 'entries')
  search.set('direction', params.direction ?? 'both')
  return request<WikiEnvelope<WikiGraphData>>(
    `/v1/wiki/entries/${encodeURIComponent(entryId)}/graph?${search.toString()}`,
  )
}

/**
 * #7 证据与执行事件摘要：`GET /v1/wiki/entries/{entry_id}/provenance`。
 *
 * 它**不依赖**图数据（契约 §5.7 约束 4：窄屏必须能在不加载完整图谱的前提下单独调用）。
 * `error_summary` 的三种形状（有错误 / `{error_code:null,none:true}` / `{unavailable:true}`）
 * 语义各不相同，界面不得把它们合并成"无错误"。
 */
export async function wikiEntryProvenance(
  entryId: string,
  params: WikiProvenanceParams = {},
): Promise<WikiEnvelope<WikiProvenanceData>> {
  const search = new URLSearchParams()
  const revisionId = trimOrNull(params.revision_id)
  if (revisionId != null) search.set('revision_id', revisionId)
  search.set(
    'limit',
    cappedLimit(params.limit, WIKI_PAGE_LIMITS.provenance.default, WIKI_PAGE_LIMITS.provenance.max),
  )
  if (params.cursor) search.set('cursor', params.cursor)
  if (params.layer) search.set('layer', params.layer)
  const suffix = search.toString()
  return request<WikiEnvelope<WikiProvenanceData>>(
    `/v1/wiki/entries/${encodeURIComponent(entryId)}/provenance?${suffix}`,
  )
}

/**
 * #8 Markdown 导出**的 URL**（`<a href>` 下载，不是 fetch）。
 *
 * 刻意不返回 `Promise<Response>`：导出体是带 `Content-Disposition: attachment` 的
 * `text/markdown`，用 fetch 取回来只会拿到一段文本、丢掉浏览器的下载语义。
 * 省略 `revision_id` = 导出**当前发布头**。
 */
export function wikiEntryExportUrl(entryId: string, revisionId?: string | null): string {
  const search = new URLSearchParams()
  const rev = trimOrNull(revisionId)
  if (rev != null) search.set('revision_id', rev)
  const suffix = search.toString()
  return `/v1/wiki/entries/${encodeURIComponent(entryId)}/export${suffix ? `?${suffix}` : ''}`
}
