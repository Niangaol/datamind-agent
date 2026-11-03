// 后端接口与证据图的数据结构（与 datamind/api.py、agent.py 对齐）

export type RouteMode = 'auto' | 'sql' | 'rag' | 'industrial'

export interface DocumentRef {
  document_id: string
  document_version: number
  source_sha256: string
  parser_version: string
}

export interface Scope {
  database_sources?: string[]
  document_collections?: string[]
  document_refs?: DocumentRef[]
}

export interface ChatPayload {
  question: string
  mode?: RouteMode
  scope?: Scope
  /** 会话归属：后端把它写进 runs.conversation_id（见 datamind/sessions.py）。
      仅在确有活动会话时下发——不传与传 null 在契约上都等于「不属于任何会话」。 */
  conversation_id?: string
}

/* ── 模型接入（GET /v1/model/config）────────────────────────────────────────
   实测 2026-10 在 127.0.0.1:8000 上的真实响应字段如下（照抄接口，不增不减）。
   注意：这份视图只回答「配了没有」——它**不含**任何连通性 / 最近检测结果，
   所以前端不能把它当成「模型可用」。 */
export interface ModelConfigView {
  id?: string | null
  name?: string | null
  protocol?: string | null
  protocol_label?: string | null
  base_url?: string | null
  model?: string | null
  /** 唯一能说明「密钥已配置」的字段；不代表密钥有效 */
  has_key: boolean
  key_masked?: string | null
  max_tokens?: number | null
  timeout_s?: number | null
  auth_style?: string | null
  headers?: Record<string, string> | null
  header_keys?: string[] | null
  has_proxy?: boolean
  /** 代理地址里是否夹带了 user:pass@ —— 为 true 时后端**只**回脱敏地址，凭据不下发 */
  proxy_has_credentials?: boolean
  proxy?: string | null
  note?: string | null
  updated_at?: string | null
  source?: string | null
  defaults?: Record<string, any> | null
  environment_fallback?: boolean
}

/** POST /v1/model/config/test 的响应（后端 providers.test_chat + api.py 补三个字段）。
    成功：{ok:true, model, elapsed_ms, protocol, reply, base_url, configured_model}
    失败：{ok:false, error_code, message, elapsed_ms, model, protocol, base_url, configured_model}
    仅 MODEL_NAME_MISSING 这一条会在 elapsed_ms 之前返回，故这些字段都可缺省。 */
export interface ModelTestResult {
  ok: boolean
  /** MODEL_NOT_FOUND / MODEL_AUTH_FAILED / MODEL_EMPTY_RESPONSE / MODEL_NAME_MISSING … */
  error_code?: string | null
  /** 后端给出的中文原因 */
  message?: string | null
  /** 成功时上游回复的前 120 字 */
  reply?: string | null
  elapsed_ms?: number | null
  model?: string | null
  protocol?: string | null
  base_url?: string | null
  /** 后端实际用于检测的模型（= 解析后的 profile.model） */
  configured_model?: string | null
}

/** SSE 里 `event: trace` 的事件（也是 trace_events 表的一行） */
export interface TraceEvent {
  event_id: string
  run_id: string
  event_type: string
  agent_name: string
  status: 'succeeded' | 'running' | 'warning' | 'failed'
  started_at: string
  input_summary: Record<string, any>
  output_summary: Record<string, any>
  error_code: string | null
  tool_name: string | null
}

export type RunStatus =
  | 'COMPLETED'
  | 'ABSTAINED'
  | 'FAILED_WITH_EVIDENCE'
  | 'NEEDS_CLARIFICATION'

export interface SqlResult {
  sql?: string
  columns?: string[]
  rows?: any[][]
  row_count?: number
  elapsed_ms?: number
  source_id?: string
  validator_findings?: string[]
  [key: string]: any
}

/** 引用的来源块（后端 source_blocks 的逐块定位） */
export interface CitationSourceBlock {
  block_id?: string
  page_no?: number
  bbox?: number[]
  [key: string]: any
}

export interface Citation {
  chunk_id: string
  document_id: string
  title?: string
  /** 后端真实返回的原文摘录字段（旧数据的兜底字段见 excerpt / text） */
  quote?: string
  text?: string
  excerpt?: string
  /** 后端真实返回的页码区间；page_no 是旧数据的单页兜底 */
  page_start?: number
  page_end?: number
  page_no?: number
  section_path?: string[]
  source_blocks?: CitationSourceBlock[]
  /** 相对路径：'/v1/documents/{id}/file?...' 与 '/v1/documents/{id}/pages/{n}?...' */
  source_url?: string
  page_url?: string
  document_version?: number
  locator?: any
  [key: string]: any
}

export interface RagResult {
  citations?: Citation[]
  claims?: any[]
  retrieval?: any
  answer_coverage?: any
  [key: string]: any
}

/* ── 工业跨源核验（mode: 'industrial' → output_summary.industrial_result）──
   下面每个字段名都对着 127.0.0.1:8000 的真实响应核过一遍：
   2026-09 A102 工序 30 那次运行的 10 行 rows / 17 条 facts 都在。 */

/** rows[].issues[]：冲突双方原值都保留在这里，前端两方都要显示 */
export interface IndustrialIssue {
  /** value_conflict / version_overlap / unit_or_value / report_gap / measurement_context … */
  kind: string
  /** unresolved / needs_review / insufficient_evidence / resolved_by_authority … */
  status: string
  /** NO_REGISTERED_AUTHORITY_RULE / UNKNOWN_UNIT / REPORT_MISSING_OR_AMBIGUOUS … */
  reason?: string
  /** 冲突双方原值。有值时必须并陈，不允许只显示一侧 */
  left_value?: string | number | null
  right_value?: string | number | null
  fact_ids?: string[]
  authority_rule_ids?: string[]
  [key: string]: any
}

/** rows[].rules[]：本行实际套用的规则（rule_id 可查，版本可复现） */
export interface IndustrialRule {
  rule_id: string
  rule_version?: string
  /** inclusive_temperature_range 等判据会带结论 */
  passed?: boolean
  expression?: string
  conversion?: string
  left_value?: string | number | null
  right_value?: string | number | null
  event_id?: string
  [key: string]: any
}

/** 核验表的一行 = 一条 measurement 的跨源核验结论 */
export interface IndustrialRow {
  batch_id: string
  part_id: string
  produced_at: string
  recorded_revision: string | null
  applicable_revision: string | null
  measurement_id: string
  operation_no: string
  metric: string
  /** 数据库实测值与其原始单位 */
  value: string | number
  unit: string
  /** 归一值可能为 null（UNKNOWN_UNIT 那类未消解的情况） */
  normalized_value: string | number | null
  normalized_unit: string | null
  measured_at: string
  method: string
  quality_id: string | null
  report_id: string | null
  quality_method: string | null
  /** 质检报告侧的原值，用来和 value 对照 */
  report_value?: string | number | null
  report_unit?: string | null
  report_fact_id?: string | null
  requirement_fact_id?: string | null
  compliance: string
  agreement: string
  issues: IndustrialIssue[]
  rules: IndustrialRule[]
  [key: string]: any
}

export interface IndustrialSummary {
  batch_count: number
  measurement_count: number
  /** 超限 measurement_id */
  out_of_range: string[]
  /** 存在来源冲突（agreement === 'unresolved'）的 measurement_id */
  conflicts: string[]
  /** 已被已登记权威规则裁决的 measurement_id */
  authority_resolved: string[]
  /** 需人工复核的 measurement_id */
  needs_review: string[]
}

export interface IndustrialResult {
  kind: string
  status: string
  answer: string
  /** { part_id, operation_no, start, end, month, metric } */
  scope?: Record<string, any>
  rule_version?: string
  authority_version?: string
  /** { dataset_version, seed_sha256, pdf_sha256, provenance } */
  dataset?: Record<string, any>
  summary?: IndustrialSummary
  rows?: IndustrialRow[]
  facts?: any[]
  sql_rows?: any[]
  sql?: string
  bound_params?: any[]
  sql_hash?: string
  source_snapshot_id?: string
  elapsed_ms?: number
  model_used?: boolean
  partial?: boolean
  [key: string]: any
}

export interface FinalResult {
  run_id: string
  status: RunStatus
  answer: string
  confidence_level: string
  error_code?: string | null
  sql_result?: SqlResult
  rag_result?: RagResult
  industrial_result?: IndustrialResult
  /** 跨源核验结果。流式接口与历史接口都会原样带上；当前只做透传，页面尚未渲染。 */
  cross_source_result?: Record<string, any>
}

export type NodeType =
  | 'SQLQuery'
  | 'SQLRow'
  | 'Claim'
  | 'Document'
  | 'Chunk'
  | 'DocumentFact'
  | 'VerificationCheck'
  | 'ConflictRecord'
  | 'AuthorityRule'

export interface EvidenceNode {
  evidence_id: string
  run_id: string
  node_type: NodeType
  ref_key: string
  payload: any
  created_at?: string
}

export type EdgeType = 'derived_from' | 'supports' | 'retrieved_from' | 'resolved_by'

export interface EvidenceEdge {
  edge_id?: string
  run_id: string
  src_evidence_id: string
  dst_evidence_id: string
  edge_type: EdgeType
}

export interface Claim {
  claim_id: string
  run_id: string
  text: string
  status: string
  evidence_ids: string[]
  confidence_level: string
  verification: any
}

export interface EvidenceGraph {
  nodes: EvidenceNode[]
  edges: EvidenceEdge[]
  claims: Claim[]
}

/** 聊天界面里的一条消息（用户或助手） */
export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  status?: 'running' | 'done' | 'error'
  thinking?: TraceEvent[]
  sqlResult?: SqlResult
  citations?: Citation[]
  /** 工业核验结果：核验表的数据源，渲染在答案正文下方 */
  industrialResult?: IndustrialResult
  runStatus?: RunStatus
  confidence?: string
  error?: string
  runId?: string
}

/* ── 会话（服务端实现见 datamind/sessions.py）──────────────────────────────
   后端**没有** messages 表：一次问答就是 runs 里的一行（question / answer /
   status / started_at），runs.conversation_id 把它挂到会话上。所以下面
   SessionMessage 是「一个 run 一条」，前端渲染时摊平成用户气泡 + 助手气泡。 */

/** GET /v1/sessions 列表项 */
export interface SessionSummary {
  session_id: string
  title: string
  created_at: string
  updated_at: string
  /** 该会话下的 run 数（后端子查询得出） */
  run_count?: number
  /** 最近一次提问，用于列表副标题/悬停提示 */
  last_question?: string | null
}

/** 会话详情里的转写单元：一个 run 一条，不是一条聊天消息 */
export interface SessionMessage {
  run_id: string
  question: string
  answer?: string | null
  status?: string | null
  confidence_level?: string | null
  started_at?: string | null
}

export interface SessionListResult {
  sessions: SessionSummary[]
  limit: number
  offset: number
  has_more: boolean
}

export interface SessionDetail {
  session: SessionSummary
  messages: SessionMessage[]
}

/** 历史会话的富内容单元：一个 run 一条，`result` 就是流式接口最后那份 `final`。
    `result` 为 null = 该 run 没走到终态事件（仍在跑，或早于事件落库），不是错误。 */
export interface SessionRunResult {
  run_id: string
  result: FinalResult | null
}

/** GET /v1/sessions/{id}/results：按 run 分页、**最近优先**（与 detail.messages 相反） */
export interface SessionResultsPage {
  session_id: string
  results: SessionRunResult[]
  limit: number
  offset: number
  has_more: boolean
}

/* ── 运行记录（GET /v1/runs，后端 datamind/api.py::list_runs）────────────────
   历史 run 在会话功能之前就存在，那时 conversation_id 从未被写入过，因此
   `conversation_id` 为 null 是**正常且必须如实呈现**的状态（UI 显示「未归属会话」），
   不能把它当成错误、也不能替它猜一个会话。 */

export interface RunSummary {
  run_id: string
  /** null = 该 run 早于会话功能，从未归属任何会话 */
  conversation_id?: string | null
  question: string
  status: RunStatus | string
  confidence_level?: string | null
  started_at: string
  ended_at?: string | null
}

export interface RunListResult {
  runs: RunSummary[]
  limit: number
  offset: number
  has_more: boolean
}

/** GET /v1/runs/{run_id}：后端 `SELECT *`，故除下列已知列外还可能有 plan/versions 等。 */
export interface RunDetail extends RunSummary {
  answer?: string | null
  plan?: any
  versions?: any
}

/* ══════════════════════════════════════════════════════════════════════════
   以下六个视图的接口契约（模型接入 / 字段复核 / 来源权威规则 / 图纸与实体 /
   尺寸核验 / 我的文件）。

   约定：字段名**照抄后端 JSON**，不重命名、不脑补默认值。后端视图里没有的键这里
   就不声明；声明成可空（`| null`）的键必须由 UI 显示为「未知/缺失」，而不是回落到
   一个看起来正常的值——那正是本仓库反复强调的「不要把没证明的事说成证明了」。
   ══════════════════════════════════════════════════════════════════════════ */

/* ── 模型接入：多配置（datamind/settings_store.py + datamind/api.py::_profiles_safe）──
   注意 `/v1/model/*` 全部路由都要求 **admin**，**包括读取**：配置里带端点与密钥掩码，
   中间件把它整体归到 admin 权限，所以 GET profile 列表也可能 403 PERMISSION_DENIED，
   UI 不能假设「读得到」。 */

/** 浏览器安全视图（后端 `public_profile()`）：**永不下发凭据值**。 */
export interface ModelProfile {
  id: string
  name: string | null
  protocol: string
  protocol_label: string
  base_url: string | null
  model: string | null
  /** **只**说明密钥字段填过值；既不说明密钥有效，也不说明模型能应答 */
  has_key: boolean
  key_masked: string
  max_tokens: number | null
  timeout_s: number | null
  auth_style: string
  /** 仅非敏感请求头有键值；敏感头（authorization/api-key/cookie…）不在其中 */
  headers: Record<string, string>
  /** 敏感请求头**只回显键名**：值是凭据，读接口也不下发 */
  header_keys: string[]
  has_proxy: boolean
  /** 已配置代理里是否带 user:pass@（凭据不下发，只此布尔量） */
  proxy_has_credentials: boolean
  /** 已去掉 userinfo 的代理地址，可能为 null */
  proxy: string | null
  note: string
  updated_at: string | null
  /** ⚠ create/update 的**返回体**里这个键恒为 false（后端 public_profile 默认参数），
      不能拿它判断「刚保存的成了当前启用」；只有 getModelProfiles() 列表里的 active 可信。 */
  active: boolean
}

/** `GET /v1/model/profiles` 的 protocols：可选协议清单（id 用于回传，label 用于显示） */
export interface ModelProtocolOption {
  id: string
  label: string
}

export interface ModelProfileListResult {
  active: string | null
  items: ModelProfile[]
  protocols: ModelProtocolOption[]
}

/**
 * 新建/更新/探活共用的请求体（后端 ProfileRequest，全部字段可选）。
 *
 * `api_key` 留空（不传或空串）在**更新**时表示「保持原密钥不变」，不是「清空」；
 * 只有 PUT /v1/model/config 把 "clear"/"none"/"-" 解释为清空（见 putModelConfig）。
 * `headers`/`proxy` 留空同样按键级合并保持原值——表单里看不到真实凭据是**设计**，
 * 所以不能把「表单空的」当成「用户想删掉」。
 */
export interface ModelProfileInput {
  id?: string | null
  profile_id?: string | null
  name?: string | null
  protocol?: string | null
  base_url?: string | null
  model?: string | null
  api_key?: string | null
  max_tokens?: number | null
  timeout_s?: number | null
  auth_style?: string | null
  headers?: Record<string, string> | null
  proxy?: string | null
  note?: string | null
  /** 保存后立即激活（后端 save_profile 读 activate） */
  activate?: boolean | null
}

/** 导出项：就是 PROFILE_FIELDS + id，默认（不含密钥）已由 redact_profile 脱敏 */
export interface ModelProfileExportItem {
  id?: string
  name?: string | null
  protocol?: string | null
  base_url?: string | null
  model?: string | null
  /** **仅** include_keys=true 时出现；这是把凭据原样搬走的显式导出 */
  api_key?: string | null
  max_tokens?: number | null
  timeout_s?: number | null
  auth_style?: string | null
  headers?: Record<string, string> | null
  proxy?: string | null
  note?: string | null
}

/** `GET /v1/model/profiles/export`：include_keys 是「这份导出里是否真的带了密钥」的证明 */
export interface ModelProfileExport {
  exported_at: string
  include_keys: boolean
  active: string | null
  profiles: ModelProfileExportItem[]
}

export interface ModelImportResult extends ModelProfileListResult {
  /** 实际写入条数；同名不覆盖，一律作为新配置写入 */
  imported: number
}

/**
 * `DELETE /v1/model/config` 的**唯一**返回体：只说明「已清空」。
 * 刻意不复用 ModelConfigView——那个有 21 个键，混用会让 UI 以为还能读到端点/模型。
 */
export interface ModelConfigCleared {
  has_key: false
  key_masked: ''
  source: 'unset'
}

/** `POST /v1/model/config/models`（后端 providers.list_models + 路由补 base_url/protocol） */
export interface ModelListResult {
  ok: boolean
  /** ok=true 时的上游模型 id 列表（已排序） */
  models?: string[]
  /** 后端实际使用协议；失败响应里也可能带 */
  protocol?: string
  base_url?: string | null
  /** ok=false：MODEL_AUTH_FAILED / REQUEST_FAILED / MODEL_KEY_NOT_INHERITED … */
  error_code?: string | null
  message?: string | null
}

/* ── 字段复核（GET /v1/documents*，datamind/api.py + field_store.py）──────────
   ⚠ 命名陷阱（必须显式映射，绝不能靠字段名相同就想当然）：
     * 列表 GET /v1/documents 里叫 **doc_version** 和 **sha256**；
     * 事实 GET /v1/documents/{id}/facts 里**同一个值**叫 **document_version**
       和 **source_sha256**。
   两边是同一行 document_assets 的两套别名，前端做类型映射时要写清楚，否则会出现
   「列表点进去版本对不上」的假冲突。 */

export interface DocumentSummary {
  document_id: string
  title: string
  collection: string
  file_type: string
  /** 来源类型（knowledge_base / upload …）；upload 行未过审前不可检索 */
  origin: string
  page_count: number | null
  /** 列表口径的版本号（事实接口叫 document_version） */
  doc_version: number
  /** 列表口径的来源哈希（事实接口叫 source_sha256） */
  sha256: string
  quality_score: number | null
  quality_detail: DocumentQualityDetail | null
  parser_version: string | null
  /** 派生事实：只有它为 true 才代表**检索器真的读得到** */
  searchable: boolean
  index_state: 'indexed' | 'no_chunks' | 'not_approved' | string
  /** 不可检索的原因码（UPLOAD_NOT_APPROVED / UPLOAD_HAS_NO_CHUNKS …），可检索时为 null */
  index_note: string | null
}

export interface DocumentListResult {
  documents: DocumentSummary[]
}

/** 解析质量明细：这里只声明 UI 真要读的两块，其余原样保留不解释 */
export interface DocumentQualityDetail {
  /** parse_contract 摘要（unsupported_features 说明「格式承载不了定位器」这类已声明的缺口） */
  parse_contract?: ParseContractSummary | null
  /** 事实抽取质量（facts 接口的 quality 就是这一块） */
  fact_extraction?: FactExtractionQuality | null
  [key: string]: unknown
}

export interface ParseContractSummary {
  /** 解析器**已声明**不支持的要素；空数组 ≠ 「全支持」，只说明没声明缺口 */
  unsupported_features?: string[]
  [key: string]: unknown
}

export interface FactExtractionQuality {
  extraction_version?: string
  fact_count: number
  /** 未能定位/无法通过校验的候选条数 */
  unresolved?: number
  arithmetic_checks?: FactArithmeticCheck[]
  approval_status?: string
  /** 抽取口径说明（例如「仅显式扫描单元格，实体 ID/批次/有效期未知」） */
  scope?: string
  [key: string]: unknown
}

export interface FactArithmeticCheck {
  entity_name?: string
  block_id?: string
  row?: number
  check: string
  /** passed / mismatch */
  status: string
  computed_percent?: string | null
  source_percent?: string | null
  /** 后端明说「未独立验证」时 UI 就不能声称已核验 */
  semantic_verification?: string
  [key: string]: unknown
}

/** 事实的逐块定位：每条引用都绑定 block_content_hash + 页框，任一项对不上该事实就不返回 */
export interface FactEvidenceRef {
  /** entity / header / value / lower / upper */
  role?: string
  field?: string
  field_value?: string
  block_id?: string
  page_no?: number
  /** [x0,y0,x1,y1]，坐标系见 coordinate_system（pdf_points） */
  bbox?: number[]
  page_size?: number[]
  coordinate_system?: string
  source_sha256?: string
  block_content_hash?: string
  [key: string]: unknown
}

export type FactDecision = 'confirmed' | 'rejected' | 'needs_review'
export type ApprovalDecision = 'approved' | 'rejected' | 'revoked'
/** not_approved=没有审批 · stale=有审批但绑的不是当前审查版本 · 其余为最后一次审批结论 */
export type FactFormalApprovalStatus = 'not_approved' | 'stale' | ApprovalDecision

export interface FactReviewRecord {
  review_id: string
  request_id?: string
  document_id: string
  fact_id: string
  document_version: number
  source_sha256: string
  parser_version: string
  /** 被审查事实的载荷哈希：审查结论只对这一份载荷有效 */
  payload_sha256: string
  previous_review_id: string | null
  decision: FactDecision
  reviewer_label: string
  reviewer_user_id: string
  reviewer_roles: string[]
  reason: string
  created_at: string
}

export interface FactApprovalRecord {
  approval_id: string
  request_id?: string
  document_id: string
  fact_id: string
  document_version: number
  source_sha256: string
  parser_version: string
  payload_sha256: string
  /** 审批必须绑一条「已确认」的人工复核 */
  review_id: string
  previous_approval_id: string | null
  decision: ApprovalDecision
  approver_label: string
  approver_user_id: string
  approver_roles: string[]
  reason: string
  created_at: string
}

/**
 * 一条事实（后端 fact payload + 当前复核/审批投影）。
 * 抽取器不同、fact_kind 不同，字段会少一些，所以业务字段可选；但 payload_sha256 /
 * latest_review / latest_approval / formal_approval_status 一定存在——UI 的「已复核/
 * 已批准」只允许看这三个，不允许看 approval_status（那是**源文件自己写的**声明）。
 */
export interface DocumentFact {
  fact_id: string
  document_id: string
  document_version: number
  source_sha256: string
  parser_version: string
  extraction_version: string
  payload_sha256: string
  fact_kind?: 'measurement' | 'requirement' | string
  entity_name?: string | null
  entity_id?: string | null
  entity_type?: string | null
  metric?: string | null
  method?: string | null
  raw_header?: string | null
  raw_value?: string | null
  raw_unit?: string | null
  normalized_value?: string | null
  normalized_unit?: string | null
  normalized_range?: { min: string; max: string; min_inclusive: boolean; max_inclusive: boolean } | null
  operation_no?: string | null
  batch_id?: string | null
  measurement_id?: string | null
  source_revision?: string | null
  valid_from?: string | null
  valid_to?: string | null
  /** **源文件自己声明的**审批状态（已批准/未批准/unknown），不是本系统的审批结论 */
  approval_status?: string | null
  verification_status?: string | null
  evidence_refs: FactEvidenceRef[]
  ocr_confidence?: number | null
  /** 该载荷哈希下最后一次本地复核；没有则 null */
  latest_review: FactReviewRecord | null
  /** 该载荷哈希下最后一次本地审批；没有则 null */
  latest_approval: FactApprovalRecord | null
  formal_approval_status: FactFormalApprovalStatus
  [key: string]: unknown
}

/**
 * 「钉住版本」查询参数：version + sha256 **必须成对**给出。
 * 后端对「只给一个」返回 422 DOCUMENT_SELECTION_INVALID，对「给了但与当前行不一致」
 * 返回 409 DOCUMENT_SELECTION_CHANGED——这是**故意**的：宁可 4xx/裂图，也不能在
 * 声称「第 N 版」的框里渲染当前版本的字节。两个都不给 = 读当前版本。
 */
export interface DocumentSelection {
  version?: number | null
  sha256?: string | null
}

export interface DocumentFactsResult {
  document_id: string
  /** ⚠ 与列表的 doc_version 是同一个值（见上文命名陷阱） */
  document_version: number
  /** ⚠ 与列表的 sha256 是同一个值 */
  source_sha256: string
  parser_version: string
  facts: DocumentFact[]
  /** 后端已校验过定位器的事实条数（= facts.length） */
  fact_count: number
  quality: FactExtractionQuality | null
}

/** 复核/审批提交后的字段投影：只包含三个键，其余状态由调用方自行重读 */
export interface FactFieldState {
  latest_review: FactReviewRecord | null
  latest_approval: FactApprovalRecord | null
  formal_approval_status: FactFormalApprovalStatus
}

export interface FactReviewHistoryResult {
  reviews: FactReviewRecord[]
  /** 后端自带的语义说明：本地抽取保真度复核，与源权威/冲突消解无关 */
  semantics: string
}

export interface FactApprovalHistoryResult {
  approvals: FactApprovalRecord[]
  semantics: string
}

export interface FactReviewSubmitResult {
  review: FactReviewRecord
  field_state: FactFieldState | null
  semantics: string
}

export interface FactApprovalSubmitResult {
  approval: FactApprovalRecord
  field_state: FactFieldState | null
  semantics: string
}

/**
 * `POST .../reviews` 请求体（后端 FactReviewRequest，extra=forbid：多一个键就是 422）。
 * `reviewer_label` 若传，必须与当前登录身份一致，否则 422 REVIEWER_IDENTITY_SERVER_OWNED。
 */
export interface FactReviewInput {
  request_id: string
  document_version: number
  source_sha256: string
  parser_version: string
  payload_sha256: string
  previous_review_id?: string | null
  decision: FactDecision
  reviewer_label?: string | null
  reason: string
}

/** `POST .../approvals` 请求体（后端 FactApprovalRequest）。审批人不得复核自己的复核记录。 */
export interface FactApprovalInput {
  request_id: string
  document_version: number
  source_sha256: string
  parser_version: string
  payload_sha256: string
  review_id: string
  previous_approval_id?: string | null
  decision: ApprovalDecision
  reason: string
}

/* ── 来源权威规则（datamind/authority.py）──────────────────────────────────
   权威**只来自显式登记**：绝不能因为来源叫「质检报告」就默认它更权威。规则一经激活
   即不可改（只追加事件），所以 UI 展示的每条规则都必须带 payload_valid 一起显示——
   payload_valid=false 表示登记内容与哈希对不上，整份注册表都要按无效处理。 */

export interface AuthorityScope {
  part_id: string
  operation_no: string
  batch_id: string
  measurement_id: string
  metric: string
  method: string
}

export interface AuthorityDatasetRef {
  dataset_version: string
  seed_sha256: string
  pdf_sha256: string
}

/** 报告侧依据：把一条已复核+已批准的事实绑定成权威裁决的依据 */
export interface AuthorityReportBinding {
  document_id: string
  document_version: number
  source_sha256: string
  parser_version: string
  fact_id: string
  payload_sha256: string
  review_id: string | null
  approval_id: string | null
}

export interface AuthorityContext {
  dataset: AuthorityDatasetRef
  scope: AuthorityScope
  production_row_sha256: string
  report: AuthorityReportBinding
}

export interface AuthorityBasis {
  document_id: string
  document_version: number
  source_sha256: string
  page_no: number
}

export interface AuthorityRulePayload {
  version: string
  context: AuthorityContext
  /** 裁决成「以哪一侧为准」；两侧原值都必须并陈，不允许只显示胜方 */
  selected_source: 'production' | 'report'
  valid_from: string
  valid_to: string
  basis: AuthorityBasis
  rationale: string
}

export interface AuthorityRule {
  rule_id: string
  request_id: string
  creator_user_id: string
  creator_label: string
  creator_roles: string[]
  payload: AuthorityRulePayload
  payload_sha256: string
  created_at: string
  request_payload?: Record<string, unknown>
  /** 下列字段只有 GET /v1/authority/rules（LEFT JOIN 最新事件）才带 */
  event_id?: string | null
  decision?: ApprovalDecision | null
  actor_user_id?: string | null
  actor_label?: string | null
  /** 最新激活事件里的理由（列名是 activation_reason，不是 reason） */
  activation_reason?: string | null
  /** false = 登记载荷与哈希不一致：本条不可作为权威依据 */
  payload_valid?: boolean
}

export interface AuthorityEvent {
  event_id: string
  request_id: string
  rule_id: string
  previous_event_id: string | null
  decision: ApprovalDecision
  actor_user_id: string
  actor_label: string
  actor_roles: string[]
  reason: string
  request_payload: Record<string, unknown>
  created_at: string
}

/** GET /v1/authority/candidates 的候选项：只列**未消解**的冲突，原值两侧都给 */
export interface AuthorityCandidate {
  context: AuthorityContext
  /** 提交时必须原样回传；服务端会重算并在不一致时 409 AUTHORITY_CONTEXT_CHANGED */
  candidate_sha256: string
  production_value: string | number
  production_unit: string
  report_value: string | number | null
  report_unit: string | null
  produced_at: string
  basis_suggestion: AuthorityBasis
  valid_from: string
  valid_to: string
  /** false = 报告侧事实尚未通过正式审批：此时**不能**登记规则（409 APPROVED_FIELD_REQUIRED） */
  eligible: boolean
}

export interface AuthorityRuleInput {
  request_id: string
  question: string
  measurement_id: string
  candidate_sha256: string
  selected_source: 'production' | 'report'
  valid_from: string
  valid_to: string
  basis: AuthorityBasis
  rationale: string
}

export interface AuthorityEventInput {
  request_id: string
  previous_event_id?: string | null
  decision: ApprovalDecision
  reason: string
}

/* ── 图纸与实体（datamind/cad_store.py / cad_reviews.py）───────────────────
   这里的一切只证明「本地解析与人工关联复核」，**不**构成图纸正式批准、生产绑定或
   质量结论；assessment 字段（inspection_gate / review_gate）与 semantics 必须一起渲染。 */

export interface DrawingSourceSnapshot {
  document_id: string
  document_version: number
  source_sha256: string
  format: 'dxf'
  parser_version: string
  provenance: 'synthetic_demo' | 'authorized_external' | string
  /** DXF 原始单位设置（0/1/4），单位由它推出而**不是**从几何或文件名猜的 */
  insunits: number
  unit: 'mm' | 'in' | 'unknown' | string
  coordinate_system: string
  layout: string
}

export interface CadPoint {
  x: number
  y: number
}

export interface CadBox {
  xmin: number
  ymin: number
  xmax: number
  ymax: number
}

/** 图元是多态联合：用 entity_type 判别后再读各自的几何字段 */
export interface CadEntity {
  handle: string
  layer: string
  view_id: string
  /** LINE / CIRCLE / TEXT */
  entity_type: string
  // LINE
  start?: CadPoint
  end?: CadPoint
  // CIRCLE
  center?: CadPoint
  radius?: number
  // TEXT
  text?: string
  insertion?: CadPoint
  bbox?: CadBox
  [key: string]: unknown
}

export interface CadView {
  view_id: string
  bbox: CadBox
}

/** 标注在图纸里的定位器：源哈希 + 图层 + 视图三者必须与图元一致，否则是过期定位 */
export interface CadLocator {
  locator_kind: string
  source_sha256: string
  handle: string
  layer: string
  view_id: string
  coordinate_system: string
  layout: string
}

export interface CadTitleField {
  value: string
  locator: CadLocator
}

/** 图纸业务身份（图号/版次/零件号）。图纸版次 **不等于** 工艺版次，不做隐式映射 */
export interface CadBusinessIdentity {
  drawing_id: CadTitleField
  drawing_revision: CadTitleField
  part_id: CadTitleField
  [key: string]: unknown
}

export interface CadAssociationEvidence {
  method: 'native_reference' | 'human_label' | string
  reference_id: string
  source_sha256: string
  annotation_handle: string
  target_handle: string
  rationale: string
}

/**
 * 一条尺寸要素。association_status 是**必须显示**的诚实字段：
 * confirmed 才代表「标注 ↔ 圆」的关联被人确认过；unresolved/ambiguous 都还没有。
 */
export interface DrawingFeature {
  feature_id: string
  feature_type: 'diameter' | string
  annotation_raw: string
  locator: CadLocator
  nominal_value: string | null
  tolerance_lower: string | null
  tolerance_upper: string | null
  unit: string
  association_status: 'confirmed' | 'ambiguous' | 'unresolved' | string
  target_candidates: string[]
  selected_target: string | null
  association_evidence: CadAssociationEvidence | null
  [key: string]: unknown
}

/** GET /v1/drawings 的一项 / 各 version 字段（public_version） */
export interface DrawingVersion {
  document_id: string
  document_version: number
  source_sha256: string
  parser_version: string
  title: string
  bundle_sha256: string
  registrar_label: string
  created_at: string
  provenance: string
}

export interface DrawingListResult {
  drawings: DrawingVersion[]
}

/** 可用于登记的图纸源文件（只列白名单暂存目录里的 .dxf） */
export interface DrawingSourceEntry {
  source_path: string
  name: string
  source_sha256: string
  provenance: string
}

export interface DrawingSourceListResult {
  sources: DrawingSourceEntry[]
}

export interface DrawingVersionList {
  current_version: number
  versions: DrawingVersion[]
}

export interface DrawingDetail {
  version: DrawingVersion
  source: DrawingSourceSnapshot
  identity: CadBusinessIdentity
  metadata: Record<string, unknown>
}

export interface DrawingEntitiesResult {
  version: DrawingVersion
  entities: CadEntity[]
  views: CadView[]
}

/** 可用要素（annotation 与几何一致且已确认关联）；上下界是标注偏差，不是几何公差 */
export interface CadEligibleFeature {
  feature_id: string
  nominal_value?: string
  lower?: string
  upper?: string
  unit?: string
  annotation_handle?: string
  target_handle?: string
}

export interface CadExcludedFeature {
  feature_id: string
  /** CAD_ASSOCIATION_UNCERTAIN / CAD_VALUE_OR_UNIT_UNKNOWN / CAD_GEOMETRY_ANNOTATION_CONFLICT … */
  reason: string
}

/** 使用门槛：status != 'ready' 时 features 里的条目才可以被下游引用 */
export interface CadFeatureGate {
  status: 'ready' | 'needs_review' | string
  reason?: string
  features: CadEligibleFeature[]
  excluded: CadExcludedFeature[]
}

export interface CadAssociationState {
  feature_id: string
  feature_sha256: string
  latest_review: DrawingFeatureReview | null
  history_count: number
}

export interface DrawingFeaturesResult {
  version: DrawingVersion
  features: DrawingFeature[]
  /** 原样抽取/对象一致性门槛 */
  inspection_gate: CadFeatureGate
  association_reviews: CadAssociationState[]
  /** 叠加人工关联复核后的使用门槛 */
  review_gate: CadFeatureGate
  review_semantics: string
  semantics: string
}

/** 关联复核记录（public(row)：去掉 feature_snapshot/request_payload） */
export interface DrawingFeatureReview {
  review_id: string
  request_id?: string
  document_id: string
  document_version: number
  feature_id: string
  source_sha256: string
  parser_version: string
  bundle_sha256: string
  feature_sha256: string
  previous_review_id: string | null
  decision: FactDecision
  selected_target: string | null
  reason: string
  reviewer_user_id: string
  reviewer_label: string
  reviewer_roles: string[]
  created_at: string
}

export interface FeatureReviewHistory {
  document_version: number
  feature_id: string
  reviews: DrawingFeatureReview[]
  latest_review: DrawingFeatureReview | null
}

export interface DrawingFeatureReviewInput {
  request_id: string
  document_version: number
  source_sha256: string
  parser_version: string
  bundle_sha256: string
  feature_sha256: string
  previous_review_id?: string | null
  decision: FactDecision
  /** confirmed 必填且必须属于 target_candidates；其它决策必须省略 */
  selected_target?: string | null
  reason: string
}

export interface DrawingRegistrationInput {
  request_id: string
  document_id: string
  title: string
  source_path: string
  source_sha256: string
  provenance: 'synthetic_demo' | 'authorized_external'
  previous_version: number
  /** required-but-nullable：首次登记必须显式传 null */
  previous_source_sha256: string | null
}

export interface DrawingRegistrationResult {
  version: DrawingVersion
  semantics: string
}

/* ── 尺寸核验（datamind/cad_dimensions.py）────────────────────────────────
   任何一条记录都是「本地注册 + 复核 + 使用审批」的模拟结果，**不**是制造授权，也
   **不**是产品质量结论：evaluate 的结果里 authentication_verified /
   formal_drawing_approval / product_quality_conclusion 三个都为 false，UI 必须照实说。 */

export interface DimensionSourceEntry {
  source_path: string
  name: string
  provenance: string
  source_sha256: string
}

export interface DimensionSourceListResult {
  sources: DimensionSourceEntry[]
}

/** 列表只回 id + 当前版本；要读内容必须再取详情 */
export interface DimensionMeasurementHead {
  measurement_id: string
  current_version: number
}

export interface DimensionBindingHead {
  binding_id: string
  current_version: number
}

export interface DimensionMeasurementList {
  measurements: DimensionMeasurementHead[]
  /** true = 后端只回了前 200 条：不得当作全集 */
  truncated: boolean
}

export interface DimensionBindingList {
  bindings: DimensionBindingHead[]
  truncated: boolean
}

/** 审计包裹层：所有尺寸记录都以 {payload, sha256, actor, registered_at} 返回 */
export interface DimensionAuditRecord<T> {
  payload: T
  sha256: string
  actor: { user_id: string; label: string; roles: string[] }
  registered_at: string
}

export interface DimensionMeasurementPayload {
  measurement_id: string
  source: { source_id: string; source_sha256: string; record_version: number; provenance: string }
  part_id: string
  batch_id: string
  operation_no: string
  process_revision: string
  metric: string
  feature_role: string
  method: string
  measured_at: string
  /** 原值文本；null = 源文件里就没有值（unknown unit 等） */
  value: string | null
  unit: string
}

export interface DimensionBindingPayload {
  binding_id: string
  binding_version: number
  document_id: string
  document_version: number
  source_sha256: string
  parser_version: string
  bundle_sha256: string
  feature_id: string
  feature_sha256: string
  target_handle: string
  drawing_id: string
  drawing_revision: string
  part_id: string
  measurement_id: string
  measurement_sha256: string
  measurement_source_sha256: string
  measurement_record_version: number
  batch_id: string
  operation_no: string
  process_revision: string
  metric: string
  feature_role: string
  method: string
  review_id: string
  review_sha256: string
  valid_from: string
  valid_to: string
  registrar_user_id: string
  registrar_roles: string[]
  rationale: string
}

export interface DimensionApprovalPayload {
  approval_id: string
  binding_id: string
  binding_version: number
  review_id: string
  review_sha256: string
  measurement_sha256: string
  binding_sha256: string
  previous_approval_id: string | null
  decision: ApprovalDecision
  reason: string
  approver_user_id: string
  approver_roles: string[]
  created_at: string
}

/** evaluate() 的结论：status='blocked' 时 comparison 必为 null、reason 给出阻断码 */
export interface DimensionEvaluation {
  contract_version: string
  status: 'compared' | 'blocked' | string
  reason: string | null
  comparison: DimensionComparison | null
  evidence: Record<string, unknown>
  /** 后端恒为 false：本地会话不等于外部认证 */
  authentication_verified: boolean
  /** 后端恒为 false：本流程不做图纸正式批准 */
  formal_drawing_approval: boolean
  /** 后端恒为 false：对照结果不是产品质量判定 */
  product_quality_conclusion: boolean
  semantics?: string
  [key: string]: unknown
}

/** 原值与归一值**都要**显示：inch/mm 换算后才有可比性，但原始记录不得被替换 */
export interface DimensionComparison {
  status: 'within_annotation_range' | 'outside_annotation_range' | string
  measurement_original: { value: string | null; unit: string }
  annotation_original: { raw: string; lower: string; upper: string; unit: string }
  normalized_mm: { measurement: string; lower: string; upper: string }
  annotation_handle: string
  target_handle: string
}

export interface DimensionMeasurementResult {
  measurement: DimensionAuditRecord<DimensionMeasurementPayload>
}

export interface DimensionBindingResult {
  binding: DimensionAuditRecord<DimensionBindingPayload>
  approvals: DimensionAuditRecord<DimensionApprovalPayload>[]
  current_result: DimensionEvaluation
  authentication: { verified: boolean; scope: string }
}

/** `POST /v1/dimensions/bindings` 只回新登记的绑定，**不含**审批链与当前结论
    （那些要 GET 详情才有），所以不能复用 DimensionBindingResult。 */
export interface DimensionBindingRegistrationResult {
  binding: DimensionAuditRecord<DimensionBindingPayload>
}

export interface DimensionApprovalResult {
  approval: DimensionAuditRecord<DimensionApprovalPayload>
}

/** 一次对照的持久化记录；replay=true 表示这次是**重放**既有记录而不是新算的 */
export interface DimensionComparisonRecord extends DimensionAuditRecord<DimensionComparisonPayload> {
  record_sha256: string
  replay: boolean
}

export interface DimensionComparisonPayload {
  comparison_id: string
  evaluation: Record<string, unknown>
  result: DimensionEvaluation
  authentication: { verified: boolean; scope: string }
}

/** 对照历史的一行：只给结论与绑定版本，细节要按 comparison_id 重放 */
export interface DimensionComparisonSummary {
  comparison_id: string
  binding_version: number
  status: string
  reason: string | null
  comparison: DimensionComparison | null
  registered_at: string
  record_sha256: string
}

export interface DimensionComparisonHistory {
  comparisons: DimensionComparisonSummary[]
  /** null = 没有更多；否则作为下一次 offset */
  next_offset: number | null
}

export interface DimensionMeasurementInput {
  request_id: string
  measurement_id: string
  source_path: string
  source_sha256: string
  provenance: 'synthetic_demo' | 'authorized_external'
  previous_version: number
  /** 后端是 required-but-nullable：首次登记必须显式传 null，省略会 422 */
  previous_sha256: string | null
}

export interface DimensionBindingInput {
  request_id: string
  binding_id: string
  previous_version: number
  /** required-but-nullable：首次登记必须显式传 null */
  previous_sha256: string | null
  document_id: string
  document_version: number
  source_sha256: string
  bundle_sha256: string
  feature_id: string
  feature_sha256: string
  target_handle: string
  measurement_id: string
  measurement_record_version: number
  measurement_sha256: string
  review_id: string
  review_sha256: string
  valid_from: string
  valid_to: string
  rationale: string
}

export interface DimensionApprovalInput {
  request_id: string
  binding_version: number
  binding_sha256: string
  /** required-but-nullable：链尾为空时必须显式传 null */
  previous_approval_id: string | null
  decision: ApprovalDecision
  reason: string
}

export interface DimensionComparisonInput {
  request_id: string
  binding_version: number
  binding_sha256: string
}

/* ── 我的文件 / 上传管道（ingestion/upload_pipeline.py）────────────────────
   三条必须照实呈现的事实：
     1. `POST /v1/uploads` 是 **raw bytes**（application/octet-stream + 查询参数），
        不是 multipart，也没有别的上传入口；
     2. 解析由**独立 worker 进程**推进（`python -m ingestion.upload_worker`），关闭页面
        不会推进它，所以 worker.alive=false 时必须说「后台处理器未运行」，
        不能把 queued 渲染成「解析中」；
     3. 审批只解除「未复核」闸门；解析波次会建立 chunks 与向量（vector_index='embedded'），
       但未复核内容对检索器不可见。approve 之后 searchable 才可能为 true；
        建索引失败时 vector_index='failed' 且 index_error_code 非空，approve 只会得到
        index_state='no_chunks'（不会把没有索引的文档说成可检索）。
   `indexed`（approve 响应）就是 `searchable`，任一为 false 都不能说「已可检索」。 */

export type UploadJobStatus =
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'partial_failure'
  | 'cancelled'

export interface UploadWorkerStatus {
  alive: boolean
  last_seen_at: string | null
  alive_seconds: number
  /** 心跳读不出来时的原因码（UPLOAD_WORKER_HEARTBEAT_UNREADABLE）；不是「未运行」的同义词 */
  error?: string
}

/** `POST /v1/uploads` 的 201 返回体（enqueue）：只是**排队成功**，不含解析结果 */
export interface UploadCreateResult {
  upload_id: string
  job_id: string
  filename: string
  media_type: string
  status: UploadJobStatus
  review_required: true
}

/** 列表/详情共有的部分（_item） */
export interface UploadListItem {
  upload_id: string
  job_id: string
  document_id: string
  filename: string | null
  media_type: string | null
  provenance: string | null
  /** **队列**状态（不是资产状态） */
  status: UploadJobStatus
  created_at: string | null
  review_state: 'approved' | 'pending_review' | string
  review_required: boolean
  /** 检索器（chunks）非空且已发布才为 true */
  searchable: boolean
  index_state: 'indexed' | 'no_chunks' | 'not_approved' | string
  index_note: string | null
  /** 建索引失败的原因码（UPLOAD_INDEX_FAILED …）；未失败/未尝试时为 null */
  index_error_code: string | null
  block_count: number
  locator_count: number
  unsupported_features: string[]
}

export interface UploadAnalysis {
  parse_status: string
  adapter?: string
  blocks: number
  locators_written: number
  relations: number
  block_types: Record<string, number>
  unsupported_features: string[]
  quality_findings: string[]
  [key: string]: unknown
}

/** 审批落库的审计块（quality_detail.review_approval），未审批时为 null */
export interface UploadReviewApproval {
  user_id?: string
  display_name?: string
  roles?: string[]
  approved_at?: string
  reason?: string | null
  indexed?: boolean
  block_count?: number
  chunk_count?: number
  [key: string]: unknown
}

export interface UploadDetail extends UploadListItem {
  source_sha256: string
  parser_version: string | null
  page_count: number | null
  collection: string
  analysis: UploadAnalysis
  /** 失败时的原因码 */
  error_code: string | null
  attempt: number
  /** 索引事实：embedded（切片+向量已写入）/ failed（尝试过但失败）/ deferred（无可索引内容） */
  vector_index: string
  vector_index_note: string
  /** 检索器实际读到的 chunks 行数 */
  chunk_count: number
  wave_deadline_seconds: number
  wave_timeouts: number
  locator_stores: { contract_blocks: number; page_shaped: number }
  contract_url: string
  approval: UploadReviewApproval | null
  started_at: string | null
  finished_at: string | null
  /** 只有 GET /v1/uploads/{id} 带（with_worker=True） */
  worker?: UploadWorkerStatus
}

/** `POST /v1/uploads/{id}/approve`：detail + indexed（indexed === searchable） */
export interface UploadApproveResult extends Omit<UploadDetail, 'worker'> {
  indexed: boolean
}

export interface UploadListResult {
  uploads: UploadListItem[]
  count: number
  worker: UploadWorkerStatus
}

export interface UploadListParams {
  status?: UploadJobStatus | string
  media_type?: string
  /** ISO-8601 或 epoch 秒；其它格式后端 422 UPLOAD_FILTER_INVALID */
  since?: string
  limit?: number
}

export type UploadProvenance = 'synthetic_demo' | 'authorized_external'

/* ── 身份（GET /v1/auth/me；仅 CSRF 缓存用，其余界面不依赖登录）────────────
   `user` 为 null = 匿名（此时 csrf_token 也是 null）。角色：viewer|reviewer|approver|admin，
   admin 通过一切角色闸门；默认拒绝的中间件对未列入公开清单的路由一律要求身份。 */
export type UserRole = 'viewer' | 'reviewer' | 'approver' | 'admin'

export interface AuthUser {
  user_id: string
  username?: string | null
  display_name: string
  roles: UserRole[] | string[]
  [key: string]: unknown
}

export interface AuthMe {
  user: AuthUser | null
  csrf_token: string | null
}

/* ══════════════════════════════════════════════════════════════════════════
   Wiki 知识图谱（契约 docs/WIKI_API.md；后端 datamind/wiki_query.py + wiki.py）

   五条已经上线的读接口共用同一个信封：

     { "data": {...}, "meta": {...}, "paging": {...}, "warnings": [...] }

   字段名**照抄后端 JSON**，不重命名、不脑补默认值。三条必须照实呈现的事实：

     1. **界面称之为"当前授权范围图"，不得描述为全库全图**（契约 §5.2 第 4 条）。
        服务端为此专门下发 `scope_note` / `scope_description`，前端直接显示它，
        不要自己另写一句更"好听"的话。
     2. `meta.authorized_scope.nodes_considered !== nodes_visible` 时必须给出原因码
        （`warnings` 里的 `AUTHORIZATION_FILTERED`）：**"我看不到"不等于"不存在"**。
     3. `paging.truncated=true` 是**因为上限而截断**，不是"没有更多"；
        `limit_reached` 非空时 `next_cursor` 必为 null —— 不许靠继续加载绕过上限。
   ══════════════════════════════════════════════════════════════════════════ */

/** 契约 §5.1 的四个词条类型（后端 `wiki.ENTRY_TYPES`）。 */
export type WikiEntryType = 'business_entity' | 'metric' | 'norm' | 'conclusion'

/** **发布流程**状态。⚠ 它不是核验状态，也不是来源有效性（三套状态见 §7.1）。 */
export type WikiPublicationStatus = 'draft' | 'in_review' | 'published' | 'archived'

/** 主张的核验状态（契约 §7.1 的第二套状态）。 */
export type WikiAssertionStatus = 'unverified' | 'verified' | 'insufficient' | 'conflicted'

/** 来源有效性（契约 §7.1 的第三套状态 / §7.5 的判定）。 */
export type WikiSourceValidity = 'current' | 'stale' | 'unavailable'

/** 契约 §5.2 的三个图层。`q`/`type`/`category`/`status` **只对 `entries` 有意义**，
 *  对另外两层传这些参数后端会 422（它拒绝静默忽略，这是刻意的）。 */
export type WikiLayer = 'entries' | 'evidence' | 'runs'

/**
 * 三套状态**必须分别显示，任何接口都不得只给一个合并状态**（契约 §7.1）。
 * 因此这里刻意不提供任何"总状态"派生字段。
 */
export interface WikiStatus {
  publication: WikiPublicationStatus | string
  assertion: WikiAssertionStatus | string
  source_validity: WikiSourceValidity | string
}

export interface WikiBusinessKey {
  space_id: string
  namespace: string
  entry_type: WikiEntryType | string
  /** 业务键的值部分（库里的分隔符是 `\x1f`，原样透传，不在前端重新拼） */
  key_part: string
  /** 业务键的人工确认状态；`pending` 不等于"已确认" */
  confirmation: string
}

/** 连接数统计。`scope` 恒为 `authorized_loaded`。 */
export interface WikiRelationCounts {
  visible: number
  truncated: boolean
  /** 恒为 `authorized_loaded`：当前授权 + 当前图层 + **已加载**，不是全库度数 */
  scope: string
}

/** 目录 / 详情 / 图节点三处共用的同一份投影（契约 §5.1，字段一字不差）。 */
export interface WikiEntry {
  entry_id: string
  entry_type: WikiEntryType | string
  title: string
  aliases: string[]
  category: string
  business_key: WikiBusinessKey | null
  confirmation: string
  summary: string
  status: WikiStatus
  head_revision_id: string | null
  revision_count: number
  evidence_ref_count: number
  relation_counts: WikiRelationCounts
  updated_at: string
  /** 本库全部数据都是合成演示数据；`true` 时界面**必须**标出来（需求 §1 第 20 行） */
  demo_data: boolean
  [key: string]: unknown
}

/**
 * 本次检索**实际覆盖**了哪些字段（契约 §5.1）。
 * `body_searched=false` 时正文**没有被搜过**，界面不能让人以为搜了正文：
 * 中文正文检索策略未定（`reason_code` 会给出原因）。
 */
export interface WikiSearchInfo {
  q: string | null
  scope: string[]
  body_searched: boolean
  uncovered_fields: string[]
  reason_code: string | null
  [key: string]: unknown
}

export interface WikiEntriesData {
  entries: WikiEntry[]
  search: WikiSearchInfo
  [key: string]: unknown
}

/**
 * 授权投影的自证（契约 §5 的统一信封）。
 * 两者不等 = 有对象被授权过滤掉了，"我看不到"不等于"不存在"。
 */
export interface WikiAuthorizedScope {
  nodes_considered: number
  nodes_visible: number
}

export interface WikiMeta {
  space_id: string
  as_of: string
  authorized_scope: WikiAuthorizedScope
  [key: string]: unknown
}

/** 分页四件套（契约 §4：读接口**恒带**这四个字段）。 */
export interface WikiPaging {
  limit: number
  has_more: boolean
  /** 不透明字符串，客户端**不得**解析（契约 §4 语义 1）；null = 没有下一页 */
  next_cursor: string | null
  /** true = **因为上限**被截断，不是"没有更多" */
  truncated: boolean
}

/** 统一信封。`warnings` 是原因码数组（`AUTHORIZATION_FILTERED` / `WIKI_GRAPH_LIMIT_REACHED` …）。 */
export interface WikiEnvelope<T> {
  data: T
  meta: WikiMeta
  paging: WikiPaging
  warnings: string[]
}

/**
 * 图节点。`node_kind` 是判别键：`entries` 图层恒为 `entry`，
 * `evidence` 图层为 `evidence_ref`，`runs` 图层是 `run` 与 `evidence_ref` 混合
 * （`wiki_query._runs_layer_candidates`）。所以 `entry` / `run` / `evidence_ref`
 * 三个载荷字段都是可选，读之前必须先看 `node_kind`。
 */
export interface WikiGraphNode {
  /** `entry` / `evidence_ref` / `run` */
  node_kind: string
  /** 节点自身 id（与关系两端 `src`/`dst` 同一命名空间） */
  node_id: string
  entry?: WikiEntry | null
  run?: WikiRunSummary | null
  evidence_ref?: WikiEvidenceRef | null
  /** **只统计当前授权 + 当前图层 + 已加载**的连接数；不返回隐藏内容的总度数 */
  degree?: number
  /** 恒为 `authorized_loaded` */
  degree_scope?: string
  position_hint?: unknown
  [key: string]: unknown
}

/**
 * 关系的依据（契约 §5.6 / §5.2 `basis`）。键随来源表而变，故按开放字典处理——
 * 界面只展示**存在**的键，不替它补一个看起来正常的默认值。
 */
export interface WikiRelationBasis {
  source_table?: string
  provenance_link_id?: string
  run_id?: string
  evidence_id?: string
  claim_id?: string | null
  event_id?: string | null
  revision_id?: string | null
  assertion_id?: string | null
  [key: string]: unknown
}

/**
 * 一条关系。`label` 是投影层的展示词表：**未支持的类型**会被写成
 * "未支持关系类型"（契约 §3.4 规则 3/4），不是最近似的已知类型。
 */
export interface WikiGraphRelation {
  relation_id: string
  src: string
  dst: string
  src_kind?: string
  dst_kind?: string
  relation_type: string
  /** `directed` / `undirected`（无向关系在 in/out/both 三种方向下都返回） */
  direction: 'directed' | 'undirected' | string
  origin?: string
  raw_type?: string | null
  supported: boolean
  unsupported_reason?: string | null
  rule_version?: string | null
  basis_note?: string | null
  basis?: WikiRelationBasis | null
  derivation?: unknown
  provenance_incomplete?: { applies?: boolean; [key: string]: unknown } | null
  created_at?: string | null
  /** 展示词；未支持类型为"未支持关系类型" */
  label?: string
  /** 运行图层的出处映射边会带它（`provenance_link`） */
  link_kind?: string
  [key: string]: unknown
}

/** `limit_reached` 非空 = **已达到** 100 节点 / 300 关系上限（此时 `next_cursor` 必为 null）。 */
export interface WikiGraphLimitReached {
  nodes: number
  relations: number
}

export interface WikiGraphLoaded {
  nodes: number
  relations: number
}

export interface WikiGraphLimits {
  nodes: number
  relations: number
}

/** 契约 §5.2 / §5.6 的图数据。字段是两处共用的，邻域接口额外多四个。 */
export interface WikiGraphData {
  nodes: WikiGraphNode[]
  relations: WikiGraphRelation[]
  layer: WikiLayer | string
  /** 累计已加载（**不是**全库）的节点与关系数 */
  loaded: WikiGraphLoaded
  limits: WikiGraphLimits
  limit_reached: WikiGraphLimitReached | null
  /** `NODE_LIMIT` / `RELATION_LIMIT` / `DEPTH_LIMIT`，优先级同序 */
  truncated_reason: string | null
  /** 恒为 `CURRENT_AUTHORIZED_SCOPE_GRAPH` */
  scope_note: string
  /** 服务端写好的范围说明（"当前授权范围图（不是全库全图）…"）——界面直接显示它 */
  scope_description: string
  /** 只有邻域接口（#6）才有的四个字段 */
  root?: string
  truncated?: boolean
  unreached_neighbors?: number
  [key: string]: unknown
}

/** 运行图层节点里的运行摘要（`wiki_query.run_object`）。 */
export interface WikiRunSummary {
  run_id: string
  question: string | null
  route?: string | null
  status: string | null
  confidence_level?: string | null
  started_at?: string | null
  ended_at?: string | null
  elapsed_ms?: number | null
  /** 三种形状：有错误 / 显式无错误 `{error_code:null,none:true}` / 读不到 `{unavailable:true}` */
  error_summary?: Record<string, unknown> | null
  event_count?: number
  event_types?: string[]
  /** false = `runs` 行已经不在了（`wiki_provenance_links` 刻意没有外键） */
  run_row_present?: boolean
  conversation_id?: string | null
  [key: string]: unknown
}

/**
 * 一条证据引用（契约 §5.7）。`excerpt_withheld=true` 表示**有原文但被授权扣留**，
 * 与"检索到但没有可引用原文"（`excerpt_available=false`）是两件事，不得合并。
 */
export interface WikiEvidenceRef {
  evidence_ref_id: string
  fingerprint: string | null
  source_kind: string | null
  source_ref: string | null
  source_version?: {
    document_version?: number | null
    source_sha256?: string | null
    source_snapshot_id?: string | null
    source_publication_id?: string | null
    [key: string]: unknown
  } | null
  locator?: Record<string, unknown> | null
  content_sha256: string | null
  excerpt: string
  excerpt_available: boolean
  excerpt_withheld: boolean
  /** `current` / `stale` / `unavailable`（读时复查，不缓存） */
  source_validity: WikiSourceValidity | string | null
  raw_node_types: string[]
  supported: boolean
  unsupported_reason?: string | null
  run_scoped?: boolean
  run_scoped_run_id?: string | null
  provenance_incomplete?: { applies?: boolean; [key: string]: unknown } | null
  created_at?: string | null
  [key: string]: unknown
}

/**
 * `provenance_incomplete`（契约 §6）。`applies=true` 时必须原样渲染理由码与缺失字段，
 * **不得**推测补全，也不得因为它在 Markdown 里就省略。
 */
export interface WikiProvenanceIncomplete {
  applies: boolean
  missing_fields?: string[]
  reason_code?: string
  detected_at?: string
  [key: string]: unknown
}

export interface WikiProvenanceIncompleteSummary {
  applies: boolean
  missing_fields?: string[]
  reason_code?: string
  detected_at?: string
}

export interface WikiProvenanceSummary {
  evidence_ref_count: number
  run_count: number
  event_count: number
  error_count: number
  source_validity: Record<string, number>
  provenance_complete: boolean
  provenance_incomplete: WikiProvenanceIncompleteSummary | null
  [key: string]: unknown
}

/** 一次运行的执行事件摘要（`/runs` 与 `/events` 两个接口共用的形状） */
export interface WikiTraceEventSummary {
  event_id: string
  run_id: string
  event_type: string
  status: string
  agent_name?: string | null
  tool_name?: string | null
  started_at?: string | null
  ended_at?: string | null
  duration_ms?: number | null
  input_summary?: Record<string, unknown> | null
  output_summary?: Record<string, unknown> | null
  error_code?: string | null
  retry_index?: number
  visibility?: string
  [key: string]: unknown
}

export interface WikiProvenanceData {
  summary: WikiProvenanceSummary
  evidence_refs: WikiEvidenceRef[]
  runs: WikiRunSummary[]
  events: WikiTraceEventSummary[]
  [key: string]: unknown
}

/** 版本摘要（契约 §5.4）。正文不在这里，要按 `revision_id` 再取 #5。 */
export interface WikiRevisionSummary {
  revision_id: string
  base_revision_id: string | null
  status: WikiPublicationStatus | string
  title: string
  author?: { user_id?: string; display_name?: string | null } | null
  generation_method?: string | null
  citation_count?: number
  assertion_count?: number
  /** 用来判断"两版正文是否真的不同"，不必拉全文 */
  body_sha256?: string | null
  created_at?: string | null
  events?: Array<{ event: string; at?: string; actor?: string }>
  [key: string]: unknown
}

export interface WikiRevisionListData {
  revisions: WikiRevisionSummary[]
  [key: string]: unknown
}

/** 版本对象（契约 §5.3）。**不可变**：同一 revision_id 的正文与引用逐字节一致。 */
export interface WikiRevision {
  revision_id: string
  entry_id: string
  /** 首版为 null；后端**不接受**"以当前头为基线"的隐式语义 */
  base_revision_id: string | null
  title: string
  body_markdown?: string | null
  body_sha256?: string | null
  citations: Array<{ evidence_ref_id: string; marker: string; [key: string]: unknown }>
  assertions: WikiAssertion[]
  author?: { user_id?: string; display_name?: string | null } | null
  generation?: { method?: string | null; template_version?: string | null; model_id?: string | null } | null
  change_note?: string | null
  publication_status?: WikiPublicationStatus | string | null
  created_at?: string | null
  [key: string]: unknown
}

/** 一条主张。`verification` 的五项记账（契约 §7.2）必须逐项显示，不得合并成一句"已核验"。 */
export interface WikiAssertion {
  assertion_id: string
  comparison_key?: string | null
  entity_key?: Record<string, unknown> | null
  metric?: string | null
  raw_value?: string | null
  raw_unit?: string | null
  normalized_value?: string | null
  normalized_unit?: string | null
  time_scope?: Record<string, unknown> | null
  applicability?: Record<string, unknown> | null
  record_scope?: Record<string, unknown> | null
  statistic?: string | null
  evidence_refs?: string[]
  verification?: {
    status?: string
    checked_at?: string | null
    checked_by?: { user_id?: string; method?: string } | null
    coverage?: Record<string, unknown> | null
    rule_version?: string | null
    accounting?: Record<string, unknown> | null
    [key: string]: unknown
  } | null
  [key: string]: unknown
}

/** #3 `GET /v1/wiki/entries/{id}` 的 `data`：版本对象 + 该版本的关系与核验记录。 */
export interface WikiEntryDetailData {
  entry: WikiEntry
  revision: WikiRevision
  relations: WikiGraphRelation[]
  review_events: WikiReviewEvent[]
  provenance_summary: WikiProvenanceSummary | null
  [key: string]: unknown
}

export interface WikiReviewEvent {
  review_event_id: string
  action: string
  actor?: { user_id?: string } | null
  at?: string | null
  expected_head_revision_id?: string | null
  based_on_head_revision_id?: string | null
  scope?: unknown
  verdict?: { assertion?: string } | null
  basis?: unknown
  conflicted_assertions?: string[]
  note?: string | null
  [key: string]: unknown
}

/* ── 请求参数（契约 §4 / §5 的冻结边界，前端不越界）─────────────────────── */

/** #1 词条目录：`limit` 默认 20、上限 50；`cursor` 不透明。 */
export interface WikiEntryListParams {
  q?: string
  /** 可重复；这里用单值数组表达"可重复"的查询参数 */
  type?: string[]
  /** **只**过滤 `publication`（核验状态要另传 assertion_status，本期不做） */
  status?: string
  category?: string
  /** ISO8601 最近更新窗口 */
  updated_since?: string
  space_id?: string
  limit?: number
  cursor?: string | null
}

/** #2 空间图：`limit` 固定 50（后端不接受更大值）；累加上限 100 节点 / 300 关系。 */
export interface WikiSpaceGraphParams {
  layer?: WikiLayer
  q?: string
  type?: string
  category?: string
  status?: string
  limit?: number
  cursor?: string | null
}

/** #6 邻域：`depth` 1..2（超出 422）；`limit` 默认 50、上限 100。 */
export interface WikiEntryGraphParams {
  depth?: number
  limit?: number
  layer?: WikiLayer
  direction?: 'in' | 'out' | 'both'
}

/** #7 证据与事件：`limit` 默认 50、**上限 200**。 */
export interface WikiProvenanceParams {
  revision_id?: string
  limit?: number
  cursor?: string | null
  layer?: 'evidence' | 'runs' | 'both'
}

/** 服务端写好的原因码（`warnings` 数组里的取值），只列读接口会遇到的。 */
export type WikiWarningCode =
  | 'AUTHORIZATION_FILTERED'
  | 'CURSOR_RESET_ON_FILTER_CHANGE'
  | 'WIKI_GRAPH_LIMIT_REACHED'
  | string
