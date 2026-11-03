import type { UploadDetail, UploadProvenance, UploadWorkerStatus } from '../../api/types'

/**
 * 我的文件（/v1/uploads）的**唯一**诚实文案与本地预检实现。
 *
 * WHY 单独一个 .ts，而不是散在各个 .vue 里：
 *   「待复核 / 已解析但尚未建立问答索引 / 后台处理器未运行 / 轮询已达上限 / 复核通过」
 *   这些句子是本屏的诚实契约 —— 列表行、本批上传行、详情块必须说**同一句话**。
 *   一旦它们分散在三个模板里，迟早会漂移成「一处说已建立索引、另一处说暂不可问答」，
 *   那正是本仓库反复强调的失败模式（渲染了值，却丢掉了它的前提）。
 *
 * 服务端事实（ingestion/upload_pipeline.py、datamind/file_uploads.py）：
 *   1. 唯一上传入口是 `POST /v1/uploads?filename=&provenance=`，**raw bytes**，
 *      Content-Type: application/octet-stream，**不是** multipart；退役的
 *      `/v1/files/uploads` + `/v1/files/jobs` 两步家族不再使用；
 *   2. 解析只由独立 worker（`python -m ingestion.upload_worker --watch`）推进，
 *      没有任何 HTTP 路由会推进它，所以 `queued` + `worker.alive=false` 必须说
 *      「后台处理器未运行」，**不能**渲染成「解析中」；
 *   3. 审批只解开复核闸门。解析波次会建立切片与向量（vector_index='embedded'），但未复核
 *      内容对检索器不可见；approve 之后 `searchable` 才可能为 true。建索引失败时
 *      vector_index='failed'、index_error_code 非空，此时 approve 只会得到
 *      index_state='no_chunks' —— 那时**不能说**「复核已通过、可用于问答」。
 */

/* ── 服务端硬约束：与 datamind/file_uploads.py::UPLOAD_LIMITS 一一对应 ──────
   本地先检一遍只是为了让用户少等一个往返；服务端仍会独立按同一组上限拒绝
   （UPLOAD_TOO_LARGE / UPLOAD_EMPTY），所以这里的数字改了就必须同步。 */
export const MIB = 1024 * 1024

export const ALLOWED_SUFFIXES = ['.dxf', '.json', '.docx', '.pptx', '.xlsx', '.pdf'] as const
export type AllowedSuffix = (typeof ALLOWED_SUFFIXES)[number]

export const SIZE_LIMITS: Record<string, number> = {
  '.dxf': 8 * MIB,
  '.json': 1 * MIB,
  '.docx': 16 * MIB,
  '.pptx': 16 * MIB,
  '.xlsx': 16 * MIB,
  '.pdf': 32 * MIB,
}

/** 每批最多 20 个文件（超出部分本地拦下，绝不悄悄丢弃）。 */
export const MAX_BATCH_FILES = 20

/** 上传并发上限：2。服务端另有 UPLOAD_SLOTS=4 的流式信号量，客户端不必更激进。 */
export const MAX_CONCURRENT_UPLOADS = 2

/** 轮询：每 1.5s 一次 `GET /v1/uploads/{id}`，最多 120 次（≈3 分钟）。 */
export const POLL_INTERVAL_MS = 1500
export const POLL_MAX_ATTEMPTS = 120

/** <input type="file"> 的 accept：只放行接口真正接受的后缀。 */
export const ACCEPT_ATTR = ALLOWED_SUFFIXES.join(',')

/** `.dwg` 由**服务端内容/后缀检测**以 415 CAD_DWG_CONVERTER_UNAVAILABLE 拒绝；
 *  这里先在本地拦下，省一次往返，但错误码映射里同样保留该码（检测在服务端）。 */
export const DWG_LOCAL_MESSAGE = 'DWG 需要外部转换器，暂不支持'

export const PROVENANCE_OPTIONS: { value: UploadProvenance; label: string; hint: string }[] = [
  {
    value: 'synthetic_demo',
    label: '合成演示来源（synthetic_demo）',
    hint: '仓库自带的合成/演示数据',
  },
  {
    value: 'authorized_external',
    label: '授权外部来源（authorized_external）',
    hint: '确认已有外部授权才可选；服务端按所选值登记，不会替你判断',
  },
]

/* ── 状态枚举（队列语义）────────────────────────────────────────────────────
   文案是冻结的：queued 排队中 / running 解析中 / succeeded 已入库（待复核）/
   failed 失败 / partial_failure 部分失败 / cancelled 已取消。未知取值原样带出，
   绝不吞成空白。 */
export const UPLOAD_STATUS_LABELS: Record<string, string> = {
  queued: '排队中',
  running: '解析中',
  succeeded: '已入库（待复核）',
  failed: '失败',
  partial_failure: '部分失败',
  cancelled: '已取消',
}

export const UPLOAD_STATUS_OPTIONS: { value: string; label: string }[] = Object.entries(
  UPLOAD_STATUS_LABELS,
).map(([value, label]) => ({ value, label }))

/** 终态：只有这四个可以让轮询停下来；其余一律「仍在处理中」。 */
export const TERMINAL_STATUSES = ['succeeded', 'failed', 'partial_failure', 'cancelled']

export function isTerminal(status?: string | null): boolean {
  return typeof status === 'string' && TERMINAL_STATUSES.includes(status)
}

export function statusLabel(status?: string | null): string {
  if (!status) return '未知状态'
  return UPLOAD_STATUS_LABELS[status] ?? `未知状态：${status}`
}

export type RowTone = 'ok' | 'warn' | 'bad' | 'muted'

export function statusTone(status?: string | null): RowTone {
  if (status === 'succeeded') return 'ok'
  if (status === 'partial_failure') return 'warn'
  if (status === 'failed') return 'bad'
  if (status === 'cancelled') return 'muted'
  // queued / running 都**不是**成功：running 只说明有人领取了，queued 只是排队。
  return 'muted'
}

/** 只有 succeeded / partial_failure 才允许点「复核通过」（服务端同样 409 拦截）。 */
export function isApprovable(status?: string | null, reviewState?: string | null): boolean {
  if (status !== 'succeeded' && status !== 'partial_failure') return false
  return reviewState !== 'approved'
}

/* ── 索引三态：审批 ≠ 可检索（本屏的核心）────────────────────────────────── */

export const INDEX_STATE_LABELS: Record<string, string> = {
  indexed: '已建立索引，可用于问答',
  no_chunks: '已解析，尚未建立问答索引（暂不可问答）',
  not_approved: '待复核',
}

export function indexStateText(state?: string | null): string {
  if (!state) return '索引状态：未知（该响应没有 index_state 字段）'
  return INDEX_STATE_LABELS[state] ?? `索引状态：${state}`
}

export const INDEX_NOTE_LABELS: Record<string, string> = {
  UPLOAD_NOT_APPROVED: '待复核',
  UPLOAD_HAS_NO_CHUNKS: '已复核，但尚未建立问答索引，暂不可问答',
  UPLOAD_HAS_NO_CONTRACT_BLOCKS: '解析未产出可入库的块',
  UPLOAD_STATUS_NOT_INDEXED: '暂不可问答（该文档状态不是 indexed）',
}

/** 未知原因码必须以 `索引说明：<code>` 原样呈现，**绝不吞掉** —— 静默丢弃一个
 *  服务端明确给出的原因码，等于把「不知道为什么不可检索」伪装成「没有问题」。 */
export function indexNoteText(note?: string | null): string {
  if (note == null || String(note).trim() === '') {
    // 可检索时服务端按设计给 null；此时没有原因码可展示，也不能编一个。
    return '索引说明：无（服务端未给出原因码）'
  }
  return INDEX_NOTE_LABELS[note] ?? `索引说明：${note}`
}

export function searchableText(searchable?: boolean | null): string {
  if (searchable === true) return '检索器读得到'
  if (searchable === false) return '检索器读不到'
  return '能否被检索：未知（该响应没有这个字段）'
}

/** 本管道**真的**在解析波次内建立切片与向量（ingestion/upload_index.py），且与知识库摄取
 *  共用同一发布顺序协议：向量 upsert 成功之后才写 chunks。所以这里**不再**挂
 *  「未建立向量索引」的旧说明 —— 它在本次改动后会是假话。 */
export function searchableCaveat(_searchable?: boolean | null): string {
  return ''
}

/* ── 索引事实（vector_index）与失败原因 ─────────────────────────────────────
   `index_state` 是**可检索性**（派生判断）；`vector_index` 是**索引事实**：
   embedded = 切片与向量已写入；failed = 尝试过但失败；deferred = 没有可索引内容。
   两者不是同一件事，界面也不能用一句话把它们混在一起。 */
export const VECTOR_INDEX_LABELS: Record<string, string> = {
  embedded: '已建立切片与向量索引',
  failed: '建索引失败：没有可用于问答的索引',
  deferred: '没有可索引的内容（未建立索引）',
}

export function vectorIndexText(state?: string | null): string {
  if (!state) return '索引事实：未知（该响应没有 vector_index 字段）'
  return VECTOR_INDEX_LABELS[state] ?? `索引事实：${state}`
}

export const INDEX_ERROR_LABELS: Record<string, string> = {
  UPLOAD_INDEX_FAILED: '建索引失败：文件已解析入库，但没有写入检索索引，因此暂不可用于问答。重新上传同一文件会重新解析并重建索引。',
  UPLOAD_ASSET_MISSING: '建索引失败：找不到这份上传资产的记录。',
}

/** 未知原因码必须原样带出，绝不吞成「没问题」。 */
export function indexErrorText(code?: string | null): string {
  if (code == null || String(code).trim() === '') return ''
  return INDEX_ERROR_LABELS[code] ?? `建索引失败（错误码：${code}）`
}

/* ── worker 诚实（mandatory）────────────────────────────────────────────── */

export const WORKER_NOT_RUNNING_TEXT =
  '后台处理器未运行：任务不会自行推进。请先启动上传处理器（scripts/start_local.sh 会一并启动），再刷新本页查看。'

export const POLL_EXHAUSTED_TEXT = '轮询已达上限，仍在处理中；请稍后刷新查看。'

/** 重试不是断点恢复：新代次会**从头解析整个文件**，没有检查点；而且本界面根本
 *  触发不了重试 —— 上传管道只有 create/list/detail/approve 四个 HTTP 路由，
 *  retry/cancel 只存在于进程内函数，没有对外路由。 */
export const RETRY_HONESTY_TEXT =
  '重试会从头重新解析整个文件，没有断点或检查点。\n「被领取次数」只表示这个文件被处理器拿起来过几次。\n本界面无法触发重试或取消：上传流程没有开放这两个动作。'

export const APPROVE_GATE_NOTE = '未复核前不会出现在可检索文档中。'

/** 审批结果的**唯一**措辞。approve 响应里 `indexed === searchable`：
 *  true 才可以说「复核已通过」；false 时服务端明确表示该文件不会进入可检索集合。 */
export interface FilesApproveNotice {
  text: string
  tone: 'ok' | 'warn' | 'bad'
}

export function approveResultText(indexed: boolean): FilesApproveNotice {
  if (indexed === true) {
    return {
      tone: 'ok',
      text: '复核已通过：该文件已进入可检索文档集合，可用于问答。',
    }
  }
  // indexed=false：**不得**出现「复核已通过」。闸门开了，但检索器仍然读不到它。
  return {
    tone: 'warn',
    text: '已复核，但尚未建立问答索引，暂不可用于问答；该文件不会作为可选问答文件。',
  }
}

/**
 * 行内 worker 诚实提示。只有 `queued` + `alive=false` 才有冻结文案；
 * `running` + `alive=false` 是「可能已中断」（行仍卡在 running，要等 worker 恢复后
 * 由后续波次重新领取），如实说出来比假装还在解析更安全。
 */
export function workerHonesty(
  status?: string | null,
  worker?: UploadWorkerStatus | null,
): string {
  if (worker && worker.error === 'UPLOAD_WORKER_HEARTBEAT_UNREADABLE') {
    // 心跳读不出来 ≠ 没有 worker：探测失败不能猜成「未运行」。
    return '这次没能读到后台处理器的心跳，但这不等于「它没在运行」，只是这次探测失败了。'
  }
  if (!worker || status == null) return ''
  if (worker.alive === false) {
    if (status === 'queued') return WORKER_NOT_RUNNING_TEXT
    if (status === 'running') {
      return '状态显示「解析中」，但后台处理器已不在运行：这次解析很可能已经中断。要等处理器恢复后由后续批次重新领取，而重新领取是从头解析，不是断点续传。'
    }
  }
  return ''
}

/* ── 错误信封 → 中文 ──────────────────────────────────────────────────────
   信封恒为 {"detail":{"error_code":"CODE"}}；FastAPI 自身的 422 是
   {"detail":[{loc,msg}]}（没有 error_code，client.ts 归一成 VALIDATION_ERROR）。
   `{ok:false,…}` 只是模型探针的响应体，这里不认它。 */
const ERROR_TEXT: Record<string, string> = {
  PERMISSION_DENIED: '权限不足：上传相关的全部接口（包括读取）都要求 admin（管理员）角色。',
  LOGIN_REQUIRED: '需要先登录：上传接口不对匿名访问开放。',
  SESSION_INVALID: '会话已失效，请重新登录。',
  CSRF_REQUIRED: '缺少安全令牌：写操作必须带上令牌，本页会自动获取，刷新页面即可重试。',
  ORIGIN_NOT_ALLOWED: '来源不被允许：上传必须从本站页面发起，不要用外部地址直接连过来。',
  VALIDATION_ERROR: '服务端校验没通过（422）：这类错误不附带错误码，下面显示的是服务端原文。',
  UPLOAD_FILTER_INVALID: '筛选条件不合法：「状态」必须是固定选项，「此时间之后」必须是标准时间或时间戳。',
  UPLOAD_NOT_FOUND: '这条上传记录不存在（编号写错，或记录已被清理）。',
  UPLOAD_NOT_REVIEWABLE: '当前状态不能复核：只有「已入库」与「部分失败」可以通过复核闸门。',
  UPLOAD_TOO_LARGE: '文件超过该类型的大小上限（服务端返回 413）。',
  UPLOAD_EMPTY: '文件是空的，服务端拒绝接收。',
  UPLOAD_LENGTH_INVALID: '文件长度信息不合法。',
  FILE_TYPE_OR_NAME_INVALID: '文件名或后缀不合法：只能填纯文件名，且后缀必须在允许清单里。',
  UNSUPPORTED_MEDIA_TYPE: '不支持这种文件类型（服务端返回 415）。',
  CAD_DWG_CONVERTER_UNAVAILABLE: `${DWG_LOCAL_MESSAGE}（服务端返回 415；本页也会先在本地拦一次）。`,
  UPLOAD_STAGED_SOURCE_INVALID: '暂存的源文件已不可用，无法解析。',
  UPLOAD_STAGED_SOURCE_CHANGED: '暂存源文件的内容与登记时的哈希不一致，服务端拒绝解析。',
}

export function errorTextFrom(e: unknown, fallback: string): string {
  const code = typeof (e as any)?.errorCode === 'string' ? ((e as any).errorCode as string) : ''
  if (code && ERROR_TEXT[code]) return `${ERROR_TEXT[code]}（错误码：${code}）`
  const raw = typeof (e as any)?.message === 'string' ? ((e as any).message as string) : ''
  if (raw) return code ? `${fallback}：${raw}（错误码：${code}）` : `${fallback}：${raw}`
  return code ? `${fallback}（错误码：${code}）` : fallback
}

/* ── 本地预检（镜像服务端，给用户一个快速明确的答案）────────────────────── */

export type PrecheckResult = { ok: true } | { ok: false; reason: string }

export function suffixOf(filename: string): string {
  const idx = filename.lastIndexOf('.')
  return idx >= 0 ? filename.slice(idx).toLowerCase() : ''
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '未知大小'
  if (bytes >= MIB) return `${(bytes / MIB).toFixed(2)} MiB`
  return `${(bytes / 1024).toFixed(1)} KiB`
}

export function precheckFile(filename: string, size: number): PrecheckResult {
  const suffix = suffixOf(filename)
  if (suffix === '.dwg') {
    return { ok: false, reason: DWG_LOCAL_MESSAGE }
  }
  if (!(ALLOWED_SUFFIXES as readonly string[]).includes(suffix)) {
    const shown = suffix || '（无后缀）'
    return {
      ok: false,
      reason: `不支持的文件类型：${shown}（仅支持 ${ALLOWED_SUFFIXES.join(' ')}）`,
    }
  }
  const limit = SIZE_LIMITS[suffix]
  if (size <= 0) {
    return { ok: false, reason: '文件为空（服务端会以 422 UPLOAD_EMPTY 拒绝）' }
  }
  if (size > limit) {
    return {
      ok: false,
      reason: `${suffix} 最大 ${limit / MIB} MiB，本文件 ${formatBytes(size)}，超过上限`,
    }
  }
  return { ok: true }
}

/* ── 视图模型 ─────────────────────────────────────────────────────────── */

/** 后端给的是带时区的 ISO 串；解析失败时原样返回，不猜时间。 */
export function formatWhen(iso?: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

export interface FilesRowInput {
  key: string
  uploadId: string | null
  filename: string
  mediaType: string | null
  createdAt: string | null
  status: string | null
  reviewState: string | null
  searchable: boolean | null
  indexState: string | null
  indexNote: string | null
  /** 建索引失败的原因码（列表/详情都会带）；未失败时为 null */
  indexErrorCode: string | null
  blockCount: number | null
  locatorCount: number | null
  /** 列表行用接口顶层的 worker；本批上传行用详情的 worker（没有就 null=未知）。 */
  worker: UploadWorkerStatus | null
  /** 本地阶段说明（预检被拒 / 正在上传 / 正在轮询…）；列表行为空串。 */
  phaseText: string
  pollExhausted: boolean
  errorText: string
  open: boolean
  detail: UploadDetail | null
  detailLoading: boolean
  detailError: string
  approveBusy: boolean
  approveNotice: FilesApproveNotice | null
}

export interface FilesRowVM extends FilesRowInput {
  statusText: string
  tone: RowTone
  createdAtText: string
  approvable: boolean
  approved: boolean
  searchableText: string
  searchableCaveat: string
  indexStateText: string
  indexNoteText: string
  indexErrorText: string
  workerWarning: string
}

/** 把接口字段摊成模板只读的视图模型：所有诚实措辞都经这里，模板里不再拼文案。 */
export function buildFilesRow(input: FilesRowInput): FilesRowVM {
  return {
    ...input,
    statusText: statusLabel(input.status),
    tone: statusTone(input.status),
    createdAtText: formatWhen(input.createdAt),
    approvable: input.uploadId != null && isApprovable(input.status, input.reviewState),
    approved: input.reviewState === 'approved',
    searchableText: searchableText(input.searchable),
    searchableCaveat: searchableCaveat(input.searchable),
    indexStateText: indexStateText(input.indexState),
    indexNoteText: indexNoteText(input.indexNote),
    indexErrorText: indexErrorText(input.indexErrorCode),
    workerWarning: workerHonesty(input.status, input.worker),
  }
}
