<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import VerificationTable from '../industrial/VerificationTable.vue'
import { csrf, listDocuments } from '../../api/client'
import type { ChatMessage, Citation, DocumentRef, DocumentSummary, TraceEvent } from '../../api/types'
import { errorTextFrom, indexNoteText } from '../files/uploadHonesty'

const props = defineProps<{
  messages: ChatMessage[]
  running: boolean
}>()

const emit = defineEmits<{
  (e: 'send', payload: { question: string; mode: string; documentRefs?: DocumentRef[] }): void
  /**
   * 「整理为知识词条」的生命周期事件。
   *
   * WHY 要 emit 而不是纯内部状态：整理任务是一个**跨页面的对象**（提交后落在
   * `/#/wiki/:entryId` 的草稿上，状态还要靠轮询 `GET /v1/wiki/builds/{id}`），
   * 外壳（例如侧栏的「知识图谱」入口）可能需要据此提示或刷新。
   * 本组件**自己**完成请求、状态与轮询 —— 父组件 MainPage 不在本次改动范围内，
   * 也没有必要让父组件替每条消息记住整理状态；emit 只作通知，没有监听者时
   * 什么也不会发生（Vue 对无人监听的 emit 是静默的）。
   */
  (e: 'organize', payload: { messageId: string; runId: string; phase: BuildPhase; buildId: string }): void
}>()

/** 路由实例：整理成功后「查看草稿」要跳到 `/#/wiki/:entryId?revision=…` 的深链。 */
const router = useRouter()

const input = ref('')
const mode = ref('auto')
const textarea = ref<HTMLTextAreaElement | null>(null)
const scrollBox = ref<HTMLElement | null>(null)

const MODES = [
  { value: 'auto', label: '自动判断' },
  { value: 'retail', label: '零售问数' },
  { value: 'chinook', label: 'Chinook 音乐库' },
  { value: 'rag', label: '文档问答' },
  { value: 'industrial', label: '工业温度核验' },
  { value: 'industrial_sql', label: '工业问数（模型）' },
]

/** 建议问题：沿用原有的示例问题，只补一个前置小图标。 */
const CHIPS: { label: string; icon: string; question: string; mode: string }[] = [
  { label: '销售额前三', icon: '▤', question: '2025年销售额最高的前三个产品是什么？', mode: 'retail' },
  { label: 'VIP 客户数', icon: '◉', question: 'VIP客户有多少人？', mode: 'retail' },
  { label: '各大区销售', icon: '◫', question: '2024年每个大区的销售额分别是多少？', mode: 'retail' },
  { label: '退款条件', icon: '▢', question: '七天无理由退款的条件是什么？', mode: 'rag' },
  { label: '耳机续航', icon: '⌕', question: '无线降噪耳机Pro的续航时间是多少？', mode: 'rag' },
  {
    label: '工业温度核验',
    icon: '⌖',
    question: '2026年9月A102工序30哪些批次温度超限？生产记录和质检报告是否一致？',
    mode: 'industrial',
  },
  { label: '工业批次数', icon: '⚙', question: '2026年9月A102工序30有多少个已测量批次？', mode: 'industrial_sql' },
]

const EVENT_LABEL: Record<string, string> = {
  'run.started': '开始运行',
  'query.parsed': '理解问题',
  'plan.created': '生成执行计划',
  'tool.started': '调用工具',
  'retrieval.completed': '检索完成',
  'sql.validated': 'SQL 校验通过',
  'sql.executed': '查询执行完成',
  'verification.passed': '验证通过',
  'verification.failed': '验证未通过',
  'evidence.attached': '证据已关联',
  'answer.completed': '生成答案',
  'answer.abstained': '证据不足，拒绝作答',
  'answer.clarification': '需要澄清',
  'run.failed': '运行失败',
}

const TOOL_LABEL: Record<string, string> = {
  nl2sql: 'SQL 生成',
  'knowledge.retrieve': '文档检索',
  'industrial.compare': '工业跨源核验',
  postgresql: '只读数据库',
}

function eventLabel(ev: TraceEvent): string {
  const base = EVENT_LABEL[ev.event_type] ?? ev.event_type
  if (ev.tool_name && TOOL_LABEL[ev.tool_name]) return `${base} · ${TOOL_LABEL[ev.tool_name]}`
  return base
}

const isEmpty = computed(() => props.messages.length === 0)

/* ══════════════════════════════════════════════════════════════════════════
   「限定文件」：把 `document_refs` 的四元组从 `/v1/documents` 交给问答。

   WHY 必须有这一块：上传的文件只有在 `searchable === true` 时才可以被回答引用，
   而在此之前前端根本没有 `document_refs` 的调用方 —— 用户传了文件、也复核了，
   问答却永远只用固定集合，选择文件这件事在界面上不存在。

   四条不许含糊的规则：
   1. **只在可检索时才能勾选**：`searchable===false` 的行照常列出（并说明原因），
      但勾选框禁用 —— 不给一个「选了也不会有证据」的入口；
   2. **四元组必须齐全**：`document_id` / `document_version` / `source_sha256` /
      `parser_version` 缺一不可（服务端 `document_selection` 逐字段校验）。缺
      `parser_version` 的行同样禁用并说明原因，不拿空串去撞 422；
   3. **最多 8 个**（服务端 `MAX_SELECTED_DOCUMENTS`），超出由本地拦下；
   4. **指定文件只对文档问答有意义**：一旦选中文件，本次请求按 `rag` 提交，
      模式选择器同时禁用并说明原因（服务端对其它路由返回
      `DOCUMENT_SELECTION_MODE_INVALID`，不在客户端制造这次失败）。
   ══════════════════════════════════════════════════════════════════════════ */
const MAX_SELECTED_DOCUMENTS = 8
const documents = ref<DocumentSummary[]>([])
const documentsLoading = ref(false)
const documentsError = ref('')
const documentsLoaded = ref(false)
const fileMenuOpen = ref(false)
const selectedIds = ref<string[]>([])

const selectedDocuments = computed(() =>
  documents.value.filter((d) => selectedIds.value.includes(d.document_id)),
)

/** 只有检索器真的读得到、且四元组齐全的文件才允许勾选。 */
function selectable(d: DocumentSummary): boolean {
  return d.searchable === true
    && typeof d.parser_version === 'string'
    && d.parser_version.length > 0
}

function unselectableReason(d: DocumentSummary): string {
  if (d.searchable !== true) return indexNoteText(d.index_note)
  if (!selectable(d)) return '缺少 parser_version，不能作为显式来源提交'
  return ''
}

function isSelected(d: DocumentSummary): boolean {
  return selectedIds.value.includes(d.document_id)
}

function toggleDoc(d: DocumentSummary): void {
  if (!selectable(d)) return
  if (isSelected(d)) {
    selectedIds.value = selectedIds.value.filter((id) => id !== d.document_id)
    return
  }
  if (selectedIds.value.length >= MAX_SELECTED_DOCUMENTS) return
  selectedIds.value = [...selectedIds.value, d.document_id]
}

function clearDocs(): void {
  selectedIds.value = []
}

/** `/v1/documents` 的列表口径（doc_version / sha256）→ 请求口径（document_version / source_sha256）。 */
const documentRefs = computed<DocumentRef[]>(() =>
  selectedDocuments.value.map((d) => ({
    document_id: d.document_id,
    document_version: d.doc_version,
    source_sha256: d.sha256,
    parser_version: d.parser_version as string,
  })),
)

async function loadDocuments(force = false): Promise<void> {
  if (documentsLoading.value) return
  if (documentsLoaded.value && !force) return
  documentsLoading.value = true
  documentsError.value = ''
  try {
    const result = await listDocuments()
    documents.value = result.documents ?? []
    documentsLoaded.value = true
    // 已选文件若已从列表中消失（被撤回/改版），不要继续带着一份过期的引用提交。
    const present = new Set(documents.value.map((d) => d.document_id))
    selectedIds.value = selectedIds.value.filter((id) => present.has(id))
  } catch (e) {
    documentsError.value = errorTextFrom(e, '读取文件列表失败')
  } finally {
    documentsLoading.value = false
  }
}

function toggleFileMenu(): void {
  fileMenuOpen.value = !fileMenuOpen.value
  if (fileMenuOpen.value) void loadDocuments()
}

function submit() {
  const q = input.value.trim()
  if (!q || props.running) return
  const refs = documentRefs.value
  // 指定了文件就按文档问答提交（服务端只在该路由接受 document_refs）。
  emit('send', { question: q, mode: refs.length ? 'rag' : mode.value, documentRefs: refs })
  input.value = ''
  nextTick(() => {
    if (textarea.value) textarea.value.style.height = 'auto'
  })
}

function askChip(c: { question: string; mode: string }) {
  if (props.running) return
  // 建议问题自带模式：清掉限定文件，避免「点了销售额却仍只查文件」。
  clearDocs()
  mode.value = c.mode
  emit('send', { question: c.question, mode: c.mode })
}

/** 输入框随内容长高，最多 180px，超过后内部滚动 */
function autoGrow() {
  const el = textarea.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = `${Math.min(el.scrollHeight, 180)}px`
}

/** 新消息 / 流式增量都跟着滚到底，否则答案会停在看不见的地方 */
function scrollToEnd() {
  const el = scrollBox.value
  if (el) el.scrollTop = el.scrollHeight
}

watch(
  () => [props.messages.length, props.messages[props.messages.length - 1]?.content?.length ?? 0, props.running],
  () => nextTick(scrollToEnd),
)

function statusText(m: ChatMessage): string {
  if (m.status === 'running') return '正在思考…'
  if (m.status === 'error') return '运行失败'
  // 终态里"没给出可用答案"的几种要各自说清，否则会掉到下面「置信度 X」那一行，
  // 把失败/证据不足显示成置信度（实测漏过 NEEDS_CLARIFICATION 与两个证据类状态）。
  if (m.runStatus === 'ABSTAINED' || m.runStatus === 'INSUFFICIENT_EVIDENCE') return '证据不足'
  if (m.runStatus === 'NEEDS_CLARIFICATION') return '需要澄清'
  if (m.runStatus === 'FAILED' || m.runStatus === 'FAILED_WITH_EVIDENCE') return '运行失败'
  return m.confidence ? `置信度 ${m.confidence}` : ''
}

/* ── 引用卡片：字段名读取逻辑统一收在这两个函数里 ──
   后端 citation 真实返回 quote / page_start / page_end / section_path /
   page_url / source_url；excerpt / text / page_no 只是旧数据的兜底。 */

/** 摘录文本：quote → excerpt → text，都没有则空串 */
function citationQuote(c: Citation | any): string {
  const v = c?.quote ?? c?.excerpt ?? c?.text
  return typeof v === 'string' ? v : ''
}

/** 页码标签：优先 page_start / page_end，「第 N 页」或「第 N–M 页」，无则空串 */
function citationPage(c: Citation | any): string {
  const n = (v: any) => (typeof v === 'number' && Number.isFinite(v) ? v : null)
  const start = n(c?.page_start) ?? n(c?.page_end) ?? n(c?.page_no)
  if (start == null) return ''
  const end = n(c?.page_end) ?? start
  return end !== start ? `第 ${start}–${end} 页` : `第 ${start} 页`
}

/** 章节路径：section_path 用 › 连接 */
function citationSections(c: Citation | any): string {
  const p = c?.section_path
  if (!Array.isArray(p)) return ''
  return p.filter((s: any) => typeof s === 'string' && s.trim()).join(' › ')
}

/** 链接补全：后端给的是相对路径，同源直接可用；绝对 URL 原样保留 */
function citationUrl(c: Citation | any, key: 'page_url' | 'source_url'): string {
  const v = c?.[key]
  if (typeof v !== 'string' || !v) return ''
  return /^(https?:)?\/\//i.test(v) ? v : v.startsWith('/') ? v : `/${v}`
}

/** 摘录可能很长，超过 max-height 才给「展开」；键就是卡片序号 */
const expanded = ref<Set<number>>(new Set())
const overflowing = ref<Set<number>>(new Set())
const quoteEls = new Map<number, HTMLElement>()

function setQuoteEl(ci: number, el: any) {
  if (el) quoteEls.set(ci, el as HTMLElement)
  else quoteEls.delete(ci)
}

function isExpanded(ci: number): boolean {
  return expanded.value.has(ci)
}

function isOverflowing(ci: number): boolean {
  return overflowing.value.has(ci)
}

function toggleCite(ci: number) {
  const next = new Set(expanded.value)
  if (next.has(ci)) next.delete(ci)
  else next.add(ci)
  expanded.value = next
}

/** 标记哪些摘录被截断；在引用卡片渲染后与窗口变化时重算。
    注意：必须先量「已折叠」状态 —— scrollHeight 是完整内容高度、clientHeight 是
    折叠后的高度，两者可比；若在未折叠状态下量，两者恒相等，永远测不出溢出。
    量完把元素的 class 交回模板（模板按 isOverflowing / isExpanded 决定是否折叠）。 */
function markOverflow() {
  const next = new Set<number>()
  quoteEls.forEach((el, ci) => {
    const wasClamped = el.classList.contains('is-clamp')
    el.classList.add('is-clamp')
    if (el.scrollHeight > el.clientHeight + 1) next.add(ci)
    if (!wasClamped) el.classList.remove('is-clamp')
  })
  overflowing.value = next
}

watch(
  () => [props.messages.length, props.messages[props.messages.length - 1]?.citations?.length ?? 0],
  () => nextTick(markOverflow),
  { flush: 'post' },
)
onMounted(() => {
  nextTick(markOverflow)
  window.addEventListener('resize', markOverflow)
})
onBeforeUnmount(() => window.removeEventListener('resize', markOverflow))

/* ══════════════════════════════════════════════════════════════════════════
   「整理为知识词条」——把一条**已完成的回答**（按它的 `run_id`）交给
   `POST /v1/wiki/builds` 生成候选草稿（契约 §5.9），成功后链接到草稿深链。

   五条刻意的设计（每一条都对应一个会撒谎的地方）：

   1. **挂载点**：与 `.meta` 同级、在助手气泡的最后。对所有助手回答统一可见 ——
      不只是有正文的那条，SQL / 工业核验 / 证据不足（ABSTAINED）的回答同样能看到入口。
      WHY：整理的对象是**这次运行**，不是"答案那段文字"；把入口贴在正文里会让人以为
      整理的是文案。
   2. **排队中就是排队中**：后端 `execute_build()` 目前**没有生产调用方**（整理 worker
      正在接入），提交后任务会停在 `queued`。界面如实显示"排队中（尚未开始）"，
      **不**假装完成、**不**把 queued 说成失败、也不无限轮询后编一个结论。
   3. **契约 §8.4 的七类失败逐条有话说**（`BUILD_ERROR_TEXT`）：加载失败 / 无权限 /
      无证据 / 引用失效 / 定位不可用 / 整理失败 / 版本冲突 —— 每一类都给中文解释与
      "接下来该怎么办"，而不是一句"操作失败"。
   4. **参数不足就明说**：回答没有 `run_id`（或还在生成）时按钮不可用，并写清楚为什么；
      不做一个点了没反应、或者发一个空 `run_id` 去撞 422 的入口。
   5. **幂等**：`request_id` 由 `消息 id + run_id` 派生。重复点击 = 同一次整理意图，
      服务端按 `(owner, request_id)` 幂等返回首次结果，不会生成重复版本（§5.9）。
   ══════════════════════════════════════════════════════════════════════════ */

type BuildPhase = 'idle' | 'submitting' | 'queued' | 'running' | 'succeeded' | 'failed' | 'blocked'

interface BuildObject {
  build_id?: string
  status?: string
  run_id?: string
  result_entry_id?: string | null
  result_revision_id?: string | null
  candidate_entry_id?: string | null
  error_code?: string | null
  error_detail?: string | null
  attempts?: number | null
  suggested_merges?: Array<{ entry_id?: string; reason?: string; requires_human_confirmation?: boolean }>
}

interface BuildState {
  phase: BuildPhase
  buildId: string
  /** 服务端原样返回的任务状态（queued/running/succeeded/failed），前端不改词 */
  status: string
  resultEntryId: string
  resultRevisionId: string
  errorCode: string
  /** 服务端给的说明（error_detail / 请求错误消息） */
  detail: string
  reused: boolean
  attempts: number | null
  polls: number
  suggestMerges: string[]
}

function blankState(): BuildState {
  return {
    phase: 'idle', buildId: '', status: '', resultEntryId: '', resultRevisionId: '',
    errorCode: '', detail: '', reused: false, attempts: null, polls: 0, suggestMerges: [],
  }
}

/** 服务端错误码 → 人能看懂的一句话 + 下一步。覆盖契约 §8.1 的七类场景（见文件头第 3 条）。 */
const BUILD_ERROR_TEXT: Record<string, string> = {
  WIKI_PERMISSION_DENIED: '无权限：整理需要 reviewer（复核员）或 admin（管理员）角色。重试不会改变结果。',
  WIKI_SOURCE_FORBIDDEN: '来源不可访问：这次回答引用的来源不在当前授权范围内。',
  WIKI_RUN_FORBIDDEN: '运行不可访问：这条运行不属于当前身份。',
  WIKI_RUN_NOT_FOUND: '目标运行不存在（可能已被清理）。',
  WIKI_NO_EVIDENCE: '这条运行没有可投影的证据，无法整理成词条。这是「确实没有」，重试解决不了。',
  WIKI_ENTRY_NOT_FOUND: '目标词条不存在，或当前身份无权写入它（服务端对这两种情况返回同一个结果）。',
  WIKI_REFERENCE_INVALID: '引用的证据引用已失效：需要先移除该引用再整理。',
  WIKI_LOCATOR_UNAVAILABLE: '某个来源的定位不可用（页框缺失、页号越界或记录已删）：摘要仍会保留，定位不猜。',
  WIKI_BUILD_FAILED: '整理失败：失败详情在任务里，原始问答不受影响。',
  WIKI_BUILD_NOT_RETRYABLE: '这个任务不是失败状态，不能重试；刷新一下任务状态即可。',
  WIKI_BUILD_REQUEST_INVALID: '整理请求的内容不合法（例如缺少运行标识）。',
  WIKI_IDEMPOTENCY_CONFLICT: '这次提交的标识已经描述过另一份内容，需要换一个标识再提交。',
  WIKI_VERSION_CONFLICT: '版本冲突：词条的基线已经前进了（期间有人发布了新版本）。要以最新版本为基线重新整理，重试同一个请求没用。',
  WIKI_REVIEW_REQUIRED: '发布前需要先有一条复核记录。',
  WIKI_ENTRY_IDENTITY_PENDING: '实体键还是待确认状态：发布前必须先人工确认实体键。',
  LOGIN_REQUIRED: '未登录：知识库接口不对匿名访问开放。',
  CSRF_REQUIRED: '安全令牌已失效：刷新页面重新取一次令牌后再试。',
  WIKI_LOAD_FAILED: '服务端读取失败：可以重试；界面不会把它显示成「没有数据」。',
}

function buildErrorText(code: string): string {
  if (BUILD_ERROR_TEXT[code]) return `${BUILD_ERROR_TEXT[code]}（错误码：${code}）`
  if (/^HTTP_5\d\d$/.test(code)) return `服务端出错了：这次整理请求失败（不是「没有可整理的内容」），可以重试。状态码：${code}`
  if (/^HTTP_4\d\d$/.test(code)) return `请求被拒绝：服务端返回了错误码表之外的状态码，原值保留在这里（${code}）。`
  return `这个错误码不在已知清单里，原值保留、不翻译成更好听的说法：${code}`
}

/** 与 client.ts 的 `apiError` 同形（本组件不 import 私有内核；`csrf()` 是 client.ts 的公开导出）。 */
async function buildError(res: Response, name: string): Promise<Error & { errorCode?: string }> {
  let message = ''
  let code = ''
  try {
    const body = await res.json()
    const detail = body?.detail
    if (typeof detail === 'string') {
      message = detail
    } else if (Array.isArray(detail)) {
      code = 'VALIDATION_ERROR'
      message = detail.map((item: any) => String(item?.msg ?? JSON.stringify(item))).join('; ')
    } else if (detail && typeof detail === 'object') {
      code = typeof detail.error_code === 'string' ? detail.error_code : ''
      const hint = typeof detail.hint === 'string' && detail.hint ? ` ${detail.hint}` : ''
      message = `${code || (typeof detail.message === 'string' ? detail.message : JSON.stringify(detail))}${hint}`
    }
  } catch {
    /* 空体 / 非 JSON：回落到状态码 */
  }
  const err = new Error(message || `${name} HTTP ${res.status}`) as Error & { errorCode?: string }
  err.errorCode = code || `HTTP_${res.status}`
  return err
}

/** 写内核：POST + `X-CSRF-Token`（令牌来自 client.ts 的 `csrf()`，它缓存自 GET /v1/auth/me）。 */
async function wikiPost<T>(path: string, body?: unknown): Promise<T> {
  const token = await csrf()
  const headers: Record<string, string> = { 'X-CSRF-Token': token }
  const init: RequestInit = { method: 'POST', headers }
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json'
    init.body = JSON.stringify(body)
  }
  const res = await fetch(path, init)
  if (!res.ok) throw await buildError(res, `POST ${path}`)
  return res.json() as Promise<T>
}

async function wikiGet<T>(path: string): Promise<T> {
  const res = await fetch(path, { headers: { Accept: 'application/json' } })
  if (!res.ok) throw await buildError(res, path)
  return res.json() as Promise<T>
}

/* ── 每条消息各自的整理状态（同一页里可以同时整理多条回答）────────────────── */
const buildStates = ref<Record<string, BuildState>>({})
const buildTimers = new Map<string, number>()

const POLL_INTERVAL_MS = 3000
/** 最多自动轮询 40 次（约 2 分钟）。到点就**停下并说出来**，而不是一直转圈。 */
const MAX_POLLS = 40

function stateOf(messageId: string): BuildState {
  return buildStates.value[messageId] ?? blankState()
}

function setState(messageId: string, patch: Partial<BuildState>): void {
  const next = { ...stateOf(messageId), ...patch }
  buildStates.value = { ...buildStates.value, [messageId]: next }
}

function clearTimer(messageId: string): void {
  const timer = buildTimers.get(messageId)
  if (timer != null) {
    window.clearTimeout(timer)
    buildTimers.delete(messageId)
  }
}

onBeforeUnmount(() => {
  buildTimers.forEach((timer) => window.clearTimeout(timer))
  buildTimers.clear()
})

/** `request_id` 幂等键：同一条消息 + 同一次运行 = 同一次整理意图。 */
function requestIdFor(m: ChatMessage): string {
  const raw = `ui-wiki-${m.id}-${m.runId ?? ''}`
  return raw.replace(/[^A-Za-z0-9._-]/g, '').slice(0, 64)
}

function organizeBlockedReason(m: ChatMessage): string {
  if (m.status === 'running') return '这次运行还在进行中：等回答结束、证据落库之后才能整理。'
  if (!m.runId) return '这条回答没有关联到一次运行，无法按运行整理。界面不提供没有依据的整理入口。'
  return ''
}

const busyPhases: BuildPhase[] = ['submitting', 'queued', 'running']

function organizeBusy(m: ChatMessage): boolean {
  return busyPhases.includes(stateOf(m.id).phase)
}

function organizeLabel(m: ChatMessage): string {
  const state = stateOf(m.id)
  if (state.phase === 'submitting') return '提交中…'
  if (state.phase === 'queued') return '排队中…'
  if (state.phase === 'running') return '整理中…'
  if (state.phase === 'succeeded') return '重新整理'
  if (state.phase === 'failed') return '再提交一次'
  return '整理为知识词条'
}

/** 状态行：每一句都是**这次**任务的实情（排队中不会被说成完成） */
function organizeStateText(m: ChatMessage): string {
  const state = stateOf(m.id)
  switch (state.phase) {
    case 'submitting':
      return '正在提交整理任务…'
    case 'queued':
      return `已提交，正在排队（整理处理器还没开始）。任务编号 ${state.buildId || '（未返回）'}。`
    case 'running':
      return `正在整理…任务编号 ${state.buildId}`
    case 'succeeded':
      return `整理完成，已生成版本 ${state.resultRevisionId || '（未返回）'}。草稿仍需复核与发布。`
    case 'failed':
      return `整理失败：${state.errorCode || '服务端没有给出错误码'}`
    case 'blocked':
      return state.detail
    default:
      return ''
  }
}

function organizeDetailText(m: ChatMessage): string {
  const state = stateOf(m.id)
  if (state.phase === 'failed') {
    const code = state.errorCode || 'HTTP_ERROR'
    return `${buildErrorText(code)}${state.detail ? `　服务端说明：${state.detail}` : ''}`
  }
  if (state.phase === 'queued' && state.polls >= MAX_POLLS) {
    return `已连续查询 ${MAX_POLLS} 次，任务仍在排队，所以停止自动刷新。它还在队列里（不是失败），点「刷新状态」可以再查一次。`
  }
  if (state.phase === 'succeeded') {
    const merges = state.suggestMerges.length
      ? `　合并建议（${state.suggestMerges.join('、')}）：只是建议，必须人工确认后才会合并。`
      : ''
    return `整理只生成候选版本：它不会自动发布，也不会改动原来的问答。如果打开正文时提示找不到，那是权限或存在性被隐藏，不是「没有这条词条」。${merges}`
  }
  if (state.reused) {
    return '这次提交命中了同一个提交标识的首次结果（幂等）：没有创建重复任务。'
  }
  return ''
}

function applyBuild(messageId: string, build: BuildObject, reused: boolean): void {
  const status = String(build.status || '')
  const phase: BuildPhase =
    status === 'succeeded' ? 'succeeded'
      : status === 'failed' ? 'failed'
        : status === 'running' ? 'running'
          : 'queued'
  const merges = (build.suggested_merges || [])
    .filter((item) => item && item.entry_id)
    .map((item) => `${item.entry_id}（${item.reason || '未给理由'}，需人工确认=${item.requires_human_confirmation ? 'true' : 'false'}）`)
  setState(messageId, {
    phase,
    status,
    buildId: String(build.build_id || ''),
    resultEntryId: String(build.result_entry_id || ''),
    resultRevisionId: String(build.result_revision_id || ''),
    errorCode: String(build.error_code || ''),
    detail: String(build.error_detail || ''),
    reused,
    attempts: build.attempts ?? null,
    suggestMerges: merges,
  })
}

/**
 * 轮询任务状态。**只在 queued / running 时**继续；到上限就停并说明。
 * 用 `setTimeout` 链而不是 `setInterval`：一次慢请求不会叠出多个并发轮询。
 */
function schedulePoll(m: ChatMessage): void {
  clearTimer(m.id)
  const tick = async () => {
    buildTimers.delete(m.id)
    let state = stateOf(m.id)
    if (!state.buildId || !busyPhases.includes(state.phase)) return
    const polls = state.polls + 1
    // 到上限：停下并**说出来**（任务仍在队列里，不是失败，也不是完成）。
    if (polls > MAX_POLLS) {
      setState(m.id, { polls: MAX_POLLS })
      return
    }
    setState(m.id, { polls })
    try {
      const body = await wikiGet<{ build?: BuildObject }>(`/v1/wiki/builds/${encodeURIComponent(state.buildId)}`)
      applyBuild(m.id, body?.build ?? {}, false)
    } catch (e: any) {
      const code = String(e?.errorCode || 'HTTP_ERROR')
      // 读取任务状态失败**不**把任务说成失败：任务本身可能还在跑。
      setState(m.id, { detail: `读取任务状态失败（${code}）：${buildErrorText(code)} 任务本身的状态没有被这次失败改变。` })
    }
    state = stateOf(m.id)
    if (busyPhases.includes(state.phase)) {
      buildTimers.set(m.id, window.setTimeout(tick, POLL_INTERVAL_MS))
    }
  }
  buildTimers.set(m.id, window.setTimeout(tick, POLL_INTERVAL_MS))
}

/** 提交整理（POST /v1/wiki/builds）。失败按错误码给中文解释，不吞掉服务端的话。 */
async function organize(m: ChatMessage): Promise<void> {
  const blocked = organizeBlockedReason(m)
  if (blocked) {
    setState(m.id, { phase: 'blocked', detail: blocked })
    emit('organize', { messageId: m.id, runId: '', phase: 'blocked', buildId: '' })
    return
  }
  if (organizeBusy(m)) return
  clearTimer(m.id)
  setState(m.id, { ...blankState(), phase: 'submitting' })
  emit('organize', { messageId: m.id, runId: String(m.runId), phase: 'submitting', buildId: '' })
  try {
    const body = await wikiPost<{ build?: BuildObject; reused?: boolean }>('/v1/wiki/builds', {
      request_id: requestIdFor(m),
      run_id: m.runId,
      mode: 'template',
    })
    applyBuild(m.id, body?.build ?? {}, !!body?.reused)
    const phase = stateOf(m.id).phase
    emit('organize', {
      messageId: m.id, runId: String(m.runId), phase, buildId: stateOf(m.id).buildId,
    })
    if (busyPhases.includes(phase)) schedulePoll(m)
  } catch (e: any) {
    const code = String(e?.errorCode || 'HTTP_ERROR')
    setState(m.id, {
      phase: 'failed',
      status: 'failed',
      errorCode: code,
      detail: String(e?.message || ''),
    })
    emit('organize', { messageId: m.id, runId: String(m.runId), phase: 'failed', buildId: '' })
  }
}

/** 手动刷新一次状态（排队中停留太久时的出口） */
async function refreshBuild(m: ChatMessage): Promise<void> {
  const state = stateOf(m.id)
  if (!state.buildId) return
  try {
    const body = await wikiGet<{ build?: BuildObject }>(`/v1/wiki/builds/${encodeURIComponent(state.buildId)}`)
    applyBuild(m.id, body?.build ?? {}, false)
    const after = stateOf(m.id)
    emit('organize', { messageId: m.id, runId: String(m.runId), phase: after.phase, buildId: after.buildId })
    if (busyPhases.includes(after.phase)) schedulePoll(m)
  } catch (e: any) {
    const code = String(e?.errorCode || 'HTTP_ERROR')
    setState(m.id, { detail: `刷新任务状态失败（${code}）：${buildErrorText(code)}` })
  }
}

/** 重试（POST /v1/wiki/builds/{id}/retry，仅 failed 可重试；其它状态服务端给 409） */
async function retryBuild(m: ChatMessage): Promise<void> {
  const state = stateOf(m.id)
  if (!state.buildId) return
  setState(m.id, { phase: 'submitting', detail: '' })
  try {
    const body = await wikiPost<{ build?: BuildObject }>(
      `/v1/wiki/builds/${encodeURIComponent(state.buildId)}/retry`,
      { request_id: `${requestIdFor(m)}-r${(state.attempts ?? 0) + 1}`.slice(0, 64) },
    )
    applyBuild(m.id, body?.build ?? {}, false)
    const after = stateOf(m.id)
    emit('organize', { messageId: m.id, runId: String(m.runId), phase: after.phase, buildId: after.buildId })
    if (busyPhases.includes(after.phase)) schedulePoll(m)
  } catch (e: any) {
    const code = String(e?.errorCode || 'HTTP_ERROR')
    setState(m.id, { phase: 'failed', errorCode: code, detail: String(e?.message || '') })
  }
}

/** 打开草稿：深链 + 版本 query（刷新后仍停在同一词条与同一版本） */
function openDraft(m: ChatMessage): void {
  const state = stateOf(m.id)
  if (!state.resultEntryId) return
  const query: Record<string, string> = {}
  if (state.resultRevisionId) query.revision = state.resultRevisionId
  void router.push({ name: 'wiki-entry', params: { entryId: state.resultEntryId }, query })
}
</script>

<template>
  <section class="chat">
    <div class="stage" :class="{ 'is-empty': isEmpty }">
      <!-- 空态：巨大的品牌字 -->
      <div v-if="isEmpty" class="hero">
        <h1 class="brand-word">Data<span class="accent">Mind</span></h1>
        <p class="brand-sub">可解释 · 可溯源的精准问数智能体</p>
      </div>

      <!-- 消息列表 -->
      <div v-if="!isEmpty" ref="scrollBox" class="messages">
        <div class="messages-inner">
          <template v-for="m in messages" :key="m.id">
            <!-- 用户 -->
            <div v-if="m.role === 'user'" class="row user">
              <div class="bubble user-bubble">{{ m.content }}</div>
            </div>

            <!-- 助手 -->
            <div v-else class="row assistant">
              <div class="avatar">DM</div>
              <div class="bubble">
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

                <div v-if="m.status === 'running' && !(m.thinking && m.thinking.length)" class="thinking-line">
                  <span class="pulse-dot"></span> 正在思考…
                </div>

                <!-- 答案正文 -->
                <div v-if="m.content" class="answer">{{ m.content }}</div>

                <!-- 工业核验表：industrial_result.rows 才是「核验表」的正源 -->
                <VerificationTable
                  v-if="m.industrialResult && m.industrialResult.rows && m.industrialResult.rows.length"
                  :result="m.industrialResult"
                />

                <!-- 结果表 -->
                <div v-if="m.sqlResult && m.sqlResult.columns" class="result-table">
                  <div class="result-caption">查询结果 · {{ m.sqlResult.row_count ?? m.sqlResult.rows?.length ?? 0 }} 行</div>
                  <div class="twrap">
                    <table>
                      <thead><tr><th v-for="c in m.sqlResult.columns" :key="c">{{ c }}</th></tr></thead>
                      <tbody>
                        <tr v-for="(r, i) in m.sqlResult.rows" :key="i">
                          <td v-for="(c, j) in r" :key="j">{{ c }}</td>
                        </tr>
                      </tbody>
                    </table>
                  </div>
                </div>

                <!-- 引用卡片 -->
                <div v-if="m.citations && m.citations.length" class="citations">
                  <div class="cite-title">引用来源</div>
                  <div v-for="(c, ci) in m.citations" :key="ci" class="cite">
                    <span class="cite-idx">{{ ci + 1 }}</span>
                    <div class="cite-body">
                      <div class="cite-head">
                        <span class="cite-doc">{{ c.title || c.document_id }}</span>
                        <span v-if="c.document_version != null" class="cite-ver">v{{ c.document_version }}</span>
                        <span v-if="citationPage(c)" class="cite-page">{{ citationPage(c) }}</span>
                      </div>

                      <div v-if="citationSections(c)" class="cite-path">{{ citationSections(c) }}</div>

                      <div class="cite-quote-wrap">
                        <div
                          :ref="(el) => setQuoteEl(ci, el)"
                          class="cite-text"
                          :class="{ 'is-clamp': isOverflowing(ci) && !isExpanded(ci) }"
                        >{{ citationQuote(c) }}</div>
                        <button
                          v-if="isOverflowing(ci) || isExpanded(ci)"
                          class="cite-toggle"
                          type="button"
                          @click="toggleCite(ci)"
                        >{{ isExpanded(ci) ? '收起' : '展开原文' }}</button>
                      </div>

                      <div class="cite-links">
                        <a
                          v-if="citationUrl(c, 'page_url')"
                          class="cite-link"
                          :href="citationUrl(c, 'page_url')"
                          target="_blank"
                          rel="noopener"
                        >查看原页 ↗</a>
                        <a
                          v-if="citationUrl(c, 'source_url')"
                          class="cite-link"
                          :href="citationUrl(c, 'source_url')"
                          target="_blank"
                          rel="noopener"
                        >原文件 ↗</a>
                      </div>
                    </div>
                  </div>
                </div>

                <div class="meta">
                  <span :class="['st', m.status === 'error' ? 'err' : 'ok']">{{ statusText(m) }}</span>
                  <span v-if="m.runId" class="runid">{{ m.runId }}</span>
                </div>

                <!-- 「整理为知识词条」：与 .meta 同级、在助手气泡末尾 —— 对所有助手回答
                     （SQL / 工业核验 / 证据不足 / 普通问答）统一可见，因为整理的对象是
                     这次**运行**，不是答案那段文字。失败、排队、成功都如实写在这里；
                     `organizeStateText` / `organizeDetailText` 的每一句都对应一个真实状态。 -->
                <div class="org" data-testid="organize-actions">
                  <button
                    class="org-btn"
                    type="button"
                    data-testid="organize-entry"
                    :disabled="organizeBusy(m) || m.status === 'running'"
                    :title="organizeBlockedReason(m) || '按这条回答的运行生成候选知识词条（不自动发布）'"
                    @click="organize(m)"
                  >
                    {{ organizeLabel(m) }}
                  </button>

                  <button
                    v-if="stateOf(m.id).phase === 'queued' || stateOf(m.id).phase === 'running'"
                    class="org-btn org-btn-ghost"
                    type="button"
                    data-testid="organize-refresh"
                    @click="refreshBuild(m)"
                  >
                    刷新状态
                  </button>

                  <button
                    v-if="stateOf(m.id).phase === 'failed' && stateOf(m.id).buildId"
                    class="org-btn"
                    type="button"
                    data-testid="organize-retry"
                    @click="retryBuild(m)"
                  >
                    重试整理
                  </button>

                  <button
                    v-if="stateOf(m.id).phase === 'succeeded' && stateOf(m.id).resultEntryId"
                    class="org-btn org-btn-primary"
                    type="button"
                    data-testid="organize-open-draft"
                    @click="openDraft(m)"
                  >
                    查看草稿 →
                  </button>

                  <span
                    v-if="organizeStateText(m)"
                    class="org-state"
                    :class="`is-${stateOf(m.id).phase}`"
                    data-testid="organize-state"
                  >{{ organizeStateText(m) }}</span>
                </div>
                <p
                  v-if="organizeDetailText(m)"
                  class="org-detail"
                  :class="{ 'is-error': stateOf(m.id).phase === 'failed' }"
                  data-testid="organize-detail"
                >{{ organizeDetailText(m) }}</p>
              </div>
            </div>
          </template>
        </div>
      </div>

      <!-- 输入区：大圆角输入框 + 建议胶囊 -->
      <div class="composer-wrap">
        <div class="composer">
          <div class="composer-row">
            <a
              class="round plus"
              href="/#files"
              title="上传并入库：在「我的文件」里上传 DXF / PDF / Office 文件"
              aria-label="上传并入库"
            >＋</a>

            <textarea
              ref="textarea"
              v-model="input"
              rows="1"
              placeholder="输入你的问题，例如：2025 年销售额最高的前三个产品是什么？"
              @input="autoGrow"
              @keydown.enter.exact.prevent="submit"
            ></textarea>

            <label class="scope" title="问数范围：决定查库、查文档还是两者联合">
              <select
                v-model="mode"
                aria-label="问数范围"
                :disabled="selectedDocuments.length > 0"
                :title="selectedDocuments.length > 0 ? '已限定文件：本次按文档问答提交（document_refs 只在文档问答路由生效）' : '问数范围'"
              >
                <option v-for="m in MODES" :key="m.value" :value="m.value">{{ m.label }}</option>
              </select>
            </label>

            <button
              class="round send"
              type="button"
              :disabled="running || !input.trim()"
              :title="running ? '正在运行…' : '发送（Enter）'"
              aria-label="发送"
              @click="submit"
            >
              <span v-if="running" class="pulse-dot"></span>
              <span v-else>↑</span>
            </button>
          </div>

          <!-- 限定文件：只列检索器读得到的文件；不可选的行也照常列出并说明原因 -->
          <div class="docsel">
            <div class="docsel-bar">
              <button
                class="docsel-toggle"
                type="button"
                data-testid="chat-doc-toggle"
                :aria-expanded="fileMenuOpen"
                @click="toggleFileMenu"
              >
                限定文件<span v-if="selectedDocuments.length">（{{ selectedDocuments.length }}）</span>
              </button>
              <span v-if="selectedDocuments.length" class="docsel-active">
                已限定 {{ selectedDocuments.length }} 个文件 · 本次按文档问答提交
                <button class="docsel-clear" type="button" data-testid="chat-doc-clear" @click="clearDocs">清除</button>
              </span>
              <span v-else class="docsel-hint">不选则按上面的问数范围检索全部已发布文档</span>
            </div>

            <div v-if="fileMenuOpen" class="docsel-menu" data-testid="chat-doc-menu">
              <p v-if="documentsLoading" class="docsel-state">正在读取文件列表…</p>
              <p v-else-if="documentsError" class="docsel-state is-error">{{ documentsError }}</p>
              <p v-else-if="!documents.length" class="docsel-state">
                没有可选择的文件。上传的文件需要在「我的文件」里复核通过并建立索引后才会出现在这里。
              </p>
              <template v-else>
                <label
                  v-for="d in documents"
                  :key="d.document_id"
                  class="docsel-item"
                  :class="{ 'is-disabled': !selectable(d) }"
                >
                  <input
                    type="checkbox"
                    :checked="isSelected(d)"
                    :disabled="!selectable(d)"
                    @change="toggleDoc(d)"
                  />
                  <span class="docsel-name">{{ d.title || d.document_id }}</span>
                  <span class="docsel-meta">{{ d.origin }} · v{{ d.doc_version }}</span>
                  <span v-if="!selectable(d)" class="docsel-why">{{ unselectableReason(d) }}</span>
                </label>
                <p v-if="selectedIds.length >= MAX_SELECTED_DOCUMENTS" class="docsel-state">
                  最多同时选择 {{ MAX_SELECTED_DOCUMENTS }} 个文件（服务端上限）。
                </p>
              </template>
            </div>
          </div>
        </div>

        <div v-if="isEmpty" class="chips">
          <button
            v-for="c in CHIPS"
            :key="c.label"
            class="chip"
            type="button"
            :disabled="running"
            @click="askChip(c)"
          >
            {{ c.label }}
          </button>
        </div>

        <p v-if="isEmpty" class="tip">Enter 发送 · Ctrl K 新对话 · 证据在「证据轨迹」里逐条可点</p>
      </div>
    </div>
  </section>
</template>

<style scoped>
.chat {
  flex: 1;
  min-width: 0;
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--color-void);
}
.stage {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  padding: 0 28px;
}
/* 空态：品牌字 + 输入框 + 建议胶囊整体居中 */
.stage.is-empty {
  justify-content: center;
  padding-bottom: 6vh;
}

/* ── 空态品牌字 ── */
.hero {
  text-align: center;
  margin-bottom: 26px;
}
.brand-word {
  margin: 0;
  font-size: clamp(3rem, 7.2vw, 5.6rem);
  font-weight: 800;
  line-height: 1;
  letter-spacing: -0.045em;
  color: var(--color-text);
}
.brand-word .accent {
  color: var(--color-accent);
}
.brand-sub {
  margin: 16px 0 0;
  font-size: 15.5px;
  color: var(--color-text);
}

/* ── 输入框 ── */
.composer-wrap {
  width: 100%;
  max-width: 780px;
  margin: 0 auto;
  flex-shrink: 0;
  padding-bottom: 18px;
}
/* 圆角收敛到「卡片/面板」档 12px（原来是 24px，是本站最离群的圆角；
   它是有描边+阴影、内含一行控件的容器，而不是一个裸输入框）。 */
.composer {
  /* 定位上下文，配合下面的 ::before 灰底板 */
  position: relative;
  border: 1px solid var(--color-line);
  border-radius: 12px;
  background: var(--color-panel);
  padding: 10px 12px 10px 12px;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04), 0 10px 28px rgba(15, 23, 42, 0.05);
  transition: border-color 0.15s, box-shadow 0.15s;
}
/* 灰色底板：比输入框左右各宽 12px、下方露出一截，模仿参考图的层叠卡片效果。
   ::before + z-index:-1 —— .composer 是 position:relative 且 z-index:auto，
   不构成层叠上下文，所以伪元素落在它自己的背景之后、祖先背景之前。 */
.composer::before {
  content: '';
  position: absolute;
  left: -12px;
  right: -12px;
  top: 58%;
  bottom: -14px;
  z-index: -1;
  background: var(--color-ink);
  border-radius: 12px;
}

.composer:focus-within {
  border-color: rgba(37, 99, 235, 0.45);
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04), 0 10px 28px rgba(37, 99, 235, 0.10);
}
.composer-row {
  display: flex;
  align-items: flex-end;
  gap: 8px;
}
.composer textarea {
  flex: 1;
  min-width: 0;
  border: none;
  outline: none;
  background: transparent;
  resize: none;
  color: var(--color-text);
  font-family: inherit;
  font-size: 16px;
  line-height: 1.6;
  min-height: 34px;
  max-height: 180px;
  padding: 7px 2px;
}
.composer textarea::placeholder {
  color: #9aa3af;
}
.round {
  width: 34px;
  height: 34px;
  flex-shrink: 0;
  border-radius: 50%;
  display: grid;
  place-items: center;
  font-size: 16px;
  font-family: inherit;
  cursor: pointer;
  text-decoration: none;
  transition: background 0.15s, border-color 0.15s, color 0.15s;
}
.plus {
  border: 1px solid var(--color-line);
  background: var(--color-panel);
  color: var(--color-body);
  line-height: 1;
}
.plus:hover {
  background: var(--color-ink);
  color: var(--color-text);
}
.scope {
  flex-shrink: 0;
  display: flex;
  align-items: center;
}
.scope select {
  appearance: none;
  border: 1px solid var(--color-line);
  border-radius: 999px;
  background: var(--color-ink);
  color: var(--color-body);
  font-family: inherit;
  font-size: 14px;
  padding: 8px 12px;
  max-width: 150px;
  cursor: pointer;
}
.scope select:hover {
  color: var(--color-text);
  border-color: rgba(15, 23, 42, 0.2);
}
.scope select:focus {
  outline: none;
  border-color: rgba(37, 99, 235, 0.45);
}
.send {
  border: none;
  background: var(--color-accent);
  color: #ffffff;
  font-weight: 600;
}
.send:hover:not(:disabled) {
  background: #1d4ed8;
}
.send:disabled {
  background: rgba(15, 23, 42, 0.12);
  color: #ffffff;
  cursor: default;
}

/* ── 限定文件 ── */
.docsel {
  margin-top: 6px;
  padding-top: 6px;
  border-top: 1px dashed var(--color-line);
}
.docsel-bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 12.5px;
  color: var(--color-muted);
}
.docsel-toggle {
  border: 1px solid var(--color-line);
  border-radius: 999px;
  background: var(--color-panel);
  color: var(--color-body);
  font-family: inherit;
  font-size: 12.5px;
  padding: 3px 10px;
  cursor: pointer;
}
.docsel-toggle:hover {
  color: var(--color-text);
}
.docsel-active {
  color: var(--color-text);
}
.docsel-clear {
  margin-left: 6px;
  border: none;
  background: none;
  padding: 0;
  color: var(--color-accent);
  font-family: inherit;
  font-size: 12.5px;
  cursor: pointer;
  text-decoration: underline;
}
.docsel-hint {
  color: var(--color-muted);
}
.docsel-menu {
  margin-top: 8px;
  max-height: 240px;
  overflow-y: auto;
  border: 1px solid var(--color-line);
  border-radius: 10px;
  background: var(--color-ink);
  padding: 6px;
}
.docsel-item {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding: 6px 8px;
  border-radius: 8px;
  cursor: pointer;
}
.docsel-item:hover {
  background: var(--color-panel);
}
.docsel-item.is-disabled {
  cursor: default;
  opacity: 0.75;
}
.docsel-name {
  font-size: 13.5px;
  color: var(--color-text);
  word-break: break-word;
}
.docsel-meta {
  font-size: 12px;
  color: var(--color-muted);
}
.docsel-why {
  width: 100%;
  padding-left: 22px;
  font-size: 12px;
  line-height: 1.5;
  color: #b45309;
}
.docsel-state {
  margin: 4px 8px;
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--color-muted);
}
.docsel-state.is-error {
  color: #b91c1c;
}

/* ── 建议胶囊 ── */
.chips {
  display: flex;
  /* 七个建议胶囊要保持**一行**（换行会让最后孤零零掉到第二行，很突兀）。
     收紧内边距与间距、并缩短两个最长标签，使总宽留在可用宽度内；
     nowrap 只是兜底——真的放不下时宁可整体略微超出，也不要出孤行。 */
  flex-wrap: nowrap;
  justify-content: center;
  gap: 7px;
  margin-top: 30px;
}
.chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 7px 11px;
  border-radius: 999px;
  /* 标签绝不允许在胶囊内折行——宁可让整行略微超出，也不要两行文字 */
  white-space: nowrap;
  border: 1px solid transparent;
  background: var(--color-accent);
  color: #ffffff;
  font-family: inherit;
  font-size: 14px;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s, color 0.15s;
}
.chip:hover:not(:disabled) { background: #1d4ed8; border-color: transparent; color: #ffffff; }
.chip:disabled {
  opacity: 0.5;
  cursor: default;
}
.tip {
  margin: 14px 0 0;
  text-align: center;
  font-size: 13px;
  color: var(--color-text);
}

/* ── 消息列表 ── */
.messages {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 18px 0 10px;
}
.messages-inner {
  width: 100%;
  max-width: 780px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 22px;
}
.row {
  display: flex;
  gap: 10px;
}
.row.user {
  justify-content: flex-end;
}
.bubble {
  max-width: 100%;
  min-width: 0;
}
.user-bubble {
  background: var(--color-ink);
  border: 1px solid var(--color-line);
  color: var(--color-text);
  padding: 10px 15px;
  border-radius: 16px 16px 4px 16px;
  font-size: 15px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}
.avatar {
  width: 30px;
  height: 30px;
  border-radius: 50%;
  flex-shrink: 0;
  display: grid;
  place-items: center;
  font-size: 12.5px;
  font-weight: 700;
  color: #ffffff;
  background: linear-gradient(135deg, #2563eb, #6366f1);
}
.assistant .bubble {
  flex: 1;
  padding-top: 2px;
  font-size: 15px;
}
.thinking {
  font-size: 14px;
  color: var(--color-body);
  background: var(--color-ink);
  border: 1px solid var(--color-line);
  border-radius: 12px;
  padding: 10px 12px;
  margin-bottom: 10px;
}
.thinking summary {
  cursor: pointer;
  list-style: none;
  color: var(--color-accent);
  user-select: none;
}
.thinking summary::-webkit-details-marker {
  display: none;
}
.thinking ol {
  margin: 8px 0 2px;
  padding-left: 2px;
  list-style: none;
  border-left: 1px solid var(--color-line);
}
.thinking li {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 3px 0 3px 12px;
}
.mark {
  width: 16px;
  height: 16px;
  border-radius: 50%;
  display: grid;
  place-items: center;
  font-size: 9px;
  flex-shrink: 0;
}
.mark.succeeded {
  background: rgba(22, 163, 74, 0.12);
  color: #16a34a;
}
.mark.failed {
  background: rgba(225, 29, 72, 0.12);
  color: #e11d48;
}
.mark.warning {
  background: rgba(217, 119, 6, 0.12);
  color: #d97706;
}
.label {
  color: var(--color-text);
}
.agent {
  margin-left: auto;
  font-size: 12.5px;
  color: var(--color-muted);
}
.thinking-line {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--color-accent);
  font-size: 14.5px;
  margin-bottom: 8px;
}
.pulse-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: currentColor;
  animation: pulse 1.2s ease-in-out infinite;
}
@keyframes pulse {
  0%, 100% { opacity: 0.3; transform: scale(0.8); }
  50% { opacity: 1; transform: scale(1.1); }
}
.answer {
  color: var(--color-text);
  line-height: 1.8;
  white-space: pre-wrap;
  word-break: break-word;
}
.result-table {
  margin-top: 10px;
}
.result-caption {
  /* 原名撞上 Tailwind 工具类（它会生成 display:table-caption）。组件的 scoped 规则
     只对**自己声明过的属性**有更高优先级，没声明 display 就被工具类接管，
     元素变成表格标题盒、按 min-content 收缩，结果「查询结果 · 6 行」每个汉字占一行。
     这与 .collapse（visibility:collapse 让按钮永远不可见）是同一类事故。
     改名 + 显式声明 display 双保险。 */
  display: block;
  font-size: 13.5px;
  color: var(--color-text);
  margin-bottom: 6px;
  white-space: nowrap;
}
.twrap {
  overflow-x: auto;
  border: 1px solid var(--color-line);
  border-radius: 12px;
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13.5px;
}
th, td {
  padding: 6px 10px;
  text-align: left;
  border-bottom: 1px solid var(--color-line);
  white-space: nowrap;
}
th {
  color: var(--color-muted);
  font-weight: 600;
  background: var(--color-ink);
}
td {
  color: var(--color-text);
}
tr:last-child td {
  border-bottom: none;
}
.citations {
  margin-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
/* 引用区小标题与其它页统一：蓝色、15px、600（原来是 13.5px 灰字） */
.cite-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--color-accent);
}
.cite {
  display: flex;
  gap: 10px;
  padding: 10px 12px;
  background: var(--color-ink);
  border: 1px solid var(--color-line);
  border-radius: 12px;
}
.cite-idx {
  width: 20px;
  height: 20px;
  border-radius: 6px;
  background: rgba(37, 99, 235, 0.1);
  color: var(--color-accent);
  font-size: 12.5px;
  display: grid;
  place-items: center;
  flex-shrink: 0;
}
.cite-body {
  flex: 1;
  min-width: 0;
}
.cite-head {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 6px;
}
.cite-doc {
  font-size: 14px;
  color: var(--color-text);
  font-weight: 600;
}
.cite-ver {
  font-size: 12.5px;
  color: var(--color-muted);
  border: 1px solid var(--color-line);
  border-radius: 4px;
  padding: 0 4px;
  line-height: 1.5;
}
.cite-page {
  margin-left: auto;
  flex-shrink: 0;
  font-size: 12.5px;
  color: var(--color-muted);
}
.cite-path {
  font-size: 13px;
  color: var(--color-text);
  margin-top: 4px;
  word-break: break-word;
}
.cite-quote-wrap {
  margin: 9px 0 0;
}
.cite-text {
  font-size: 13.5px;
  color: var(--color-body);
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
  padding-left: 9px;
  border-left: 2px solid var(--color-line);
}
.cite-text.is-clamp {
  display: -webkit-box;
  -webkit-line-clamp: 4;
  -webkit-box-orient: vertical;
  overflow: hidden;
  max-height: 7.2em;
}
.cite-toggle {
  margin: 4px 0 0;
  padding: 0;
  border: none;
  background: none;
  cursor: pointer;
  font-family: inherit;
  font-size: 12.5px;
  color: var(--color-accent);
}
.cite-toggle:hover {
  text-decoration: underline;
}
.cite-links {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-top: 8px;
}
.cite-link {
  font-size: 13px;
  color: var(--color-accent);
  text-decoration: none;
  border-bottom: 1px solid rgba(37, 99, 235, 0.35);
  padding-bottom: 1px;
  cursor: pointer;
}
.cite-link:hover {
  border-bottom-color: var(--color-accent);
}
.meta {
  margin-top: 8px;
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 12.5px;
  color: var(--color-muted);
}
.st.ok {
  color: #16a34a;
}
.st.err {
  color: #e11d48;
}
.runid {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  color: var(--color-muted);
}

/* ── 「整理为知识词条」操作区 ──
   类名用 `org-` 前缀：Tailwind v4 的 @layer utilities 会生成一批同名工具类，
   `.collapse`（visibility:collapse）与 `.result-caption` / `.table-caption`
   （display:table-caption）已经各坑过一次 —— `org-` 不落在任何工具类的名字空间里
   （工具类里只有 `order-*`，没有 `org-*`），构建产物里也已核对无同名规则。 */
.org {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed var(--color-line);
}

/* 「整理为知识词条」的三个按钮：统一蓝底白字加粗椭圆（原来是白底细描边） */
.org-btn {
  padding: 5px 13px;
  border: 1px solid transparent;
  border-radius: 999px;
  background: var(--color-accent);
  color: #ffffff;
  font-family: inherit;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  white-space: nowrap;
  transition: border-color 0.15s, color 0.15s, background 0.15s;
}

.org-btn:hover:not(:disabled) {
  background: #1d4ed8;
  color: #ffffff;
}

.org-btn:focus-visible {
  outline: 2px solid var(--color-accent);
  outline-offset: 1px;
}

.org-btn:disabled {
  cursor: not-allowed;
  color: var(--color-muted);
  background: var(--color-ink);
}

/* ghost / primary 两个变体现在与基础样式等价（保留类名，避免动模板） */
.org-btn-ghost {
  background: var(--color-accent);
  color: #ffffff;
}

.org-btn-primary {
  border-color: transparent;
  background: var(--color-accent);
  color: #ffffff;
}

/* 状态文字：排队中 ≠ 完成 ≠ 失败 —— 用文字说清，不靠颜色；颜色只做辅助 */
.org-state {
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--color-text);
}

.org-state.is-queued,
.org-state.is-running,
.org-state.is-submitting {
  color: #92400e;
}

.org-state.is-succeeded {
  color: #15803d;
}

.org-state.is-failed,
.org-state.is-blocked {
  color: #b91c1c;
}

.org-detail {
  margin: 6px 0 0;
  font-size: 12.5px;
  line-height: 1.65;
  color: var(--color-body);
  word-break: break-word;
}

.org-detail.is-error {
  color: #b91c1c;
}

@media (max-width: 900px) {
  .stage {
    padding: 0 16px;
  }
  .brand-word {
    font-size: clamp(2.4rem, 12vw, 3.4rem);
  }
  .scope select {
    max-width: 104px;
  }
}
</style>
