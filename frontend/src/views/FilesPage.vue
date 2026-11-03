<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import SideNav from '../components/SideNav.vue'
import FilesUploadPanel from '../components/files/FilesUploadPanel.vue'
import FilesUploadRow from '../components/files/FilesUploadRow.vue'
import { approveUpload, getUpload, listUploads, uploadFile } from '../api/client'
import type {
  UploadDetail,
  UploadListItem,
  UploadProvenance,
  UploadWorkerStatus,
} from '../api/types'
import {
  APPROVE_GATE_NOTE,
  MAX_BATCH_FILES,
  MAX_CONCURRENT_UPLOADS,
  POLL_INTERVAL_MS,
  POLL_MAX_ATTEMPTS,
  RETRY_HONESTY_TEXT,
  UPLOAD_STATUS_OPTIONS,
  WORKER_NOT_RUNNING_TEXT,
  approveResultText,
  buildFilesRow,
  errorTextFrom,
  formatWhen,
  isTerminal,
  precheckFile,
  statusLabel,
  type FilesApproveNotice,
  type FilesRowVM,
} from '../components/files/uploadHonesty'

/**
 * 我的文件（/v1/uploads，全部接口要求 admin）。
 *
 * 这一屏存在的意义是**把三件常被混为一谈的事分开说清**：
 *   解析完成（status）≠ 人工复核（review_state）≠ 可检索（searchable）。
 * 解析波次会写 chunks 与向量（vector_index='embedded'），但未复核内容对检索器不可见；
 * 复核通过后 `searchable` 才可能为 true。建索引失败（vector_index='failed'）时，
 * 即使复核通过也只能得到 index_state='no_chunks' —— 界面必须如实说出来，而不是给一个
 * 让人以为「传完就能问」的绿灯。
 *
 * 另外两条硬事实（写在这里是因为它们决定了整个交互，而不是注释级的细节）：
 *   1. **推进任务的唯一入口是独立 worker**（`python -m ingestion.upload_worker --watch`）。
 *      create/list/detail 三个路由都不会解析任何东西 —— GET 是纯读。所以
 *      `status=queued` 且 `worker.alive=false` 时必须说「后台处理器未运行」，不能显示
 *      「解析中」；`run` 状态也只是「有人领取过」，不等于一定在跑。
 *   2. **重试不是断点恢复**：重新领取会从头解析整个文件，没有检查点；而且本界面
 *      触发不了重试/取消（上传管道没有这两个 HTTP 路由）。
 */

/* ── 列表筛选用的 media_type 选项（值与 ingestion/plugins/ 各 manifest 声明一致）── */
const MEDIA_TYPE_OPTIONS = [
  { value: 'application/pdf', label: 'PDF' },
  {
    value: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    label: 'DOCX',
  },
  {
    value: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    label: 'PPTX',
  },
  { value: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', label: 'XLSX' },
  { value: 'application/json', label: 'JSON' },
  { value: 'image/vnd.dxf', label: 'DXF' },
]

const router = useRouter()

/* ── 列表状态 ─────────────────────────────────────────────────────────── */
const uploads = ref<UploadListItem[]>([])
/** res.count 是**本页条数**：接口没有总数，绝不拿它当全集。 */
const pageCount = ref<number | null>(null)
const worker = ref<UploadWorkerStatus | null>(null)
const loading = ref(false)
const error = ref('')

const filters = reactive({ status: '', media_type: '', since: '', limit: 50 })
/** 已应用的筛选（点「应用筛选」才同步）：空态文案不能因为用户正在输入而变。 */
const applied = reactive({ status: '', media_type: '', since: '', limit: 50 })

/* ── 详情 / 复核（列表行与本批上传行共用同一份缓存）────────────────────── */
const details = reactive<Record<string, UploadDetail>>({})
const detailErrors = reactive<Record<string, string>>({})
const detailLoadingId = ref<string | null>(null)
const approvingId = ref<string | null>(null)
const approveNotices = reactive<Record<string, FilesApproveNotice>>({})
const openId = ref<string | null>(null)

/* ── 本批上传状态 ─────────────────────────────────────────────────────── */
type BatchPhase = 'rejected' | 'pending' | 'uploading' | 'polling' | 'settled' | 'error'

interface BatchItem {
  key: string
  filename: string
  mediaType: string | null
  createdAt: string | null
  phase: BatchPhase
  rejectedReason: string
  uploadId: string | null
  /** 201 返回的当前状态或轮询得到的状态（不假设一定是 queued） */
  status: string | null
  errorText: string
  pollExhausted: boolean
}

const batchItems = ref<BatchItem[]>([])
const batchRunning = ref(false)
/** File 对象放在响应式之外：它是浏览器宿主对象，没必要（也不该）被 Vue 深度代理。 */
const batchFiles = new Map<string, File>()
/** 本批的来源快照：批量进行中用户改选择器不应改变已提交文件的服务端登记值。 */
let batchProvenance: UploadProvenance = 'synthetic_demo'

/** 组件卸载后停止轮询：离开页面不该继续打网络请求，也不该往已卸载的组件写状态。 */
let disposed = false

const provenance = ref<UploadProvenance>('synthetic_demo')

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms)
  })
}

function clampLimit(raw: unknown): number {
  const n = Math.trunc(Number(raw))
  if (!Number.isFinite(n) || n < 1) return 50
  return Math.min(200, n)
}

/* ── 列表读取 ─────────────────────────────────────────────────────────── */

async function load(): Promise<void> {
  loading.value = true
  const limit = clampLimit(applied.limit)
  applied.limit = limit
  try {
    const res = await listUploads({
      status: applied.status || undefined,
      media_type: applied.media_type || undefined,
      since: applied.since.trim() || undefined,
      limit,
    })
    uploads.value = Array.isArray(res?.uploads) ? res.uploads : []
    pageCount.value = typeof res?.count === 'number' ? res.count : uploads.value.length
    worker.value = res?.worker ?? null
    error.value = ''
  } catch (e: unknown) {
    // 读不到就清空，绝不保留上一次的列表假装这次也读到了（403/422 时尤其重要）。
    uploads.value = []
    pageCount.value = 0
    worker.value = null
    error.value = errorTextFrom(e, '上传记录加载失败')
  } finally {
    loading.value = false
  }
}

function applyFilters(): void {
  Object.assign(applied, filters)
  void load()
}

/* ── 详情读取 ─────────────────────────────────────────────────────────── */

/** 详情是派生字段（searchable/index_state/index_note）的最新口径，读到就同步回列表行。 */
function syncListRow(
  uploadId: string,
  src: Pick<
    UploadListItem,
    | 'status'
    | 'review_state'
    | 'review_required'
    | 'searchable'
    | 'index_state'
    | 'index_note'
    | 'block_count'
    | 'locator_count'
  >,
): void {
  const hit = uploads.value.find((u) => u.upload_id === uploadId)
  if (!hit) return
  hit.status = src.status
  hit.review_state = src.review_state
  hit.review_required = src.review_required
  hit.searchable = src.searchable
  hit.index_state = src.index_state
  hit.index_note = src.index_note
  hit.block_count = src.block_count
  hit.locator_count = src.locator_count
}

async function ensureDetail(uploadId: string): Promise<void> {
  detailLoadingId.value = uploadId
  detailErrors[uploadId] = ''
  try {
    const detail = await getUpload(uploadId)
    details[uploadId] = detail
    syncListRow(uploadId, detail)
  } catch (e: unknown) {
    detailErrors[uploadId] = errorTextFrom(e, '读取上传详情失败')
  } finally {
    if (detailLoadingId.value === uploadId) detailLoadingId.value = null
  }
}

function toggleRow(row: FilesRowVM): void {
  const id = row.uploadId
  if (!id) return // 本地预检被拒的文件从未上传，也就没有服务端详情
  if (openId.value === id) {
    openId.value = null
    return
  }
  openId.value = id
  if (!details[id]) void ensureDetail(id)
}

/* ── 复核闸门 ─────────────────────────────────────────────────────────── */

async function approveRow(row: FilesRowVM): Promise<void> {
  const id = row.uploadId
  if (!id || approvingId.value) return
  approvingId.value = id
  delete approveNotices[id]
  try {
    const res = await approveUpload(id)
    // approve 响应就是 detail + indexed（契约保证 indexed === searchable）。
    // worker 字段 approve 响应不给，保留上一次读到的值，绝不编一个。
    const prevWorker = details[id]?.worker
    details[id] = { ...res, ...(prevWorker ? { worker: prevWorker } : {}) }
    // indexed === true 才允许说「复核已通过」；false 时必须说「尚未建立问答索引」。
    approveNotices[id] = approveResultText(res.indexed === true)
    syncListRow(id, res)
    const bit = batchItems.value.find((i) => i.uploadId === id)
    if (bit) {
      bit.status = res.status
      bit.errorText = ''
    }
  } catch (e: unknown) {
    approveNotices[id] = { text: errorTextFrom(e, '复核失败'), tone: 'bad' }
  } finally {
    approvingId.value = null
  }
}

/* ── 本批上传：本地预检 → 并发 2 的原始字节上传 → 轮询到终态 ───────────── */

function onPick(files: File[]): void {
  if (batchRunning.value) return
  batchProvenance = provenance.value
  batchFiles.clear()

  const items: BatchItem[] = []
  const accepted: BatchItem[] = []
  const stamp = new Date().toISOString()
  files.forEach((file, index) => {
    const key = `batch-${Date.now().toString(36)}-${index}`
    const base: BatchItem = {
      key,
      filename: file.name,
      mediaType: null,
      createdAt: stamp,
      phase: 'pending',
      rejectedReason: '',
      uploadId: null,
      status: null,
      errorText: '',
      pollExhausted: false,
    }
    const check = precheckFile(file.name, file.size)
    if (!check.ok) {
      // 本地预检不通过：逐条说明原因，绝不静默丢弃。
      items.push({ ...base, phase: 'rejected', rejectedReason: check.reason })
      return
    }
    if (accepted.length >= MAX_BATCH_FILES) {
      items.push({
        ...base,
        phase: 'rejected',
        rejectedReason: `本批超出上限：每批最多 ${MAX_BATCH_FILES} 个文件，本文件未上传`,
      })
      return
    }
    items.push(base)
    accepted.push(base)
    batchFiles.set(key, file)
  })

  batchItems.value = items
  if (accepted.length) void runBatch(accepted)
}

/** 2-at-a-time 工作池：上传与轮询共用同一个槽位，保证任何时刻并发 ≤ 2。 */
async function runBatch(accepted: BatchItem[]): Promise<void> {
  batchRunning.value = true
  let cursor = 0
  const runner = async (): Promise<void> => {
    while (!disposed) {
      const index = cursor++
      if (index >= accepted.length) return
      await uploadAndPoll(accepted[index])
    }
  }
  const size = Math.min(MAX_CONCURRENT_UPLOADS, accepted.length)
  await Promise.all(Array.from({ length: size }, () => runner()))
  batchRunning.value = false
  if (!disposed) void load() // 本批结束后刷新列表（列表才是服务端口径）
}

async function uploadAndPoll(item: BatchItem): Promise<void> {
  const file = batchFiles.get(item.key)
  if (!file) {
    item.phase = 'error'
    item.errorText = '原始 File 对象已丢失，未上传。'
    return
  }
  item.phase = 'uploading'
  try {
    // 原始字节上传：File 直接作为 body（application/octet-stream + 查询参数），
    // 不是 FormData/multipart；也用不到退役的 /v1/files/uploads 两步家族。
    const created = await uploadFile(file, item.filename, batchProvenance)
    item.uploadId = created.upload_id
    item.mediaType = created.media_type
    // 201 的 status 是任务**当前**状态，不一定是 queued（重传相同字节是幂等的）。
    // 一律按响应原样渲染，不替服务端假设。
    item.status = created.status
    item.phase = 'polling'
    if (isTerminal(created.status)) {
      await readDetailOnce(item)
      item.phase = 'settled'
      return
    }
    await pollUntilTerminal(item)
  } catch (e: unknown) {
    item.phase = 'error'
    item.errorText = errorTextFrom(e, '上传失败')
  }
}

async function readDetailOnce(item: BatchItem): Promise<void> {
  if (!item.uploadId) return
  const detail = await getUpload(item.uploadId)
  details[item.uploadId] = detail
  item.status = detail.status
  item.errorText = ''
}

async function pollUntilTerminal(item: BatchItem): Promise<void> {
  for (let attempt = 1; attempt <= POLL_MAX_ATTEMPTS; attempt++) {
    if (disposed || !item.uploadId) return
    try {
      const detail = await getUpload(item.uploadId)
      details[item.uploadId] = detail
      item.status = detail.status
      item.errorText = ''
      if (isTerminal(detail.status)) {
        item.phase = 'settled'
        return
      }
    } catch (e: unknown) {
      item.errorText = errorTextFrom(e, '读取上传详情失败')
      const code = (e as any)?.errorCode
      // 这几种错误继续轮询没有意义（记录不存在 / 没权限 / 没身份），如实停在这里。
      if (
        code === 'UPLOAD_NOT_FOUND' ||
        code === 'PERMISSION_DENIED' ||
        code === 'LOGIN_REQUIRED' ||
        code === 'SESSION_INVALID'
      ) {
        item.phase = 'error'
        return
      }
    }
    if (attempt < POLL_MAX_ATTEMPTS) await sleep(POLL_INTERVAL_MS)
  }
  // 用尽 120 次仍非终态：**不能**假装完成，也不说「解析中」以外的任何结论。
  item.phase = 'settled'
  item.pollExhausted = true
}

/* ── 视图模型 ─────────────────────────────────────────────────────────── */

function batchPhaseText(item: BatchItem): string {
  if (item.phase === 'rejected') return `本地预检未通过：${item.rejectedReason}`
  if (item.phase === 'pending') return `等待上传（同时最多 ${MAX_CONCURRENT_UPLOADS} 个）…`
  if (item.phase === 'uploading') return '正在上传文件内容…'
  if (item.phase === 'polling') {
    if (item.status && item.status !== 'queued') {
      return `服务端受理后，当前状态是「${statusLabel(item.status)}」（重传相同内容是幂等的，不一定是排队中）；这里按响应原样显示，正在轮询详情。`
    }
    return '上传已受理，正在轮询解析状态…'
  }
  return ''
}

const batchRows = computed<FilesRowVM[]>(() =>
  batchItems.value.map((item) => {
    const detail = item.uploadId ? (details[item.uploadId] ?? null) : null
    return buildFilesRow({
      key: item.key,
      uploadId: item.uploadId,
      filename: item.filename,
      mediaType: item.mediaType,
      createdAt: item.createdAt,
      status: item.status,
      reviewState: detail?.review_state ?? null,
      searchable: detail ? detail.searchable : null,
      indexState: detail ? detail.index_state : null,
      indexNote: detail ? detail.index_note : null,
      indexErrorCode: detail ? detail.index_error_code : null,
      blockCount: detail ? detail.block_count : null,
      locatorCount: detail ? detail.locator_count : null,
      worker: detail?.worker ?? null,
      phaseText: batchPhaseText(item),
      pollExhausted: item.pollExhausted,
      errorText: item.errorText,
      open: !!item.uploadId && openId.value === item.uploadId,
      detail,
      detailLoading: !!item.uploadId && detailLoadingId.value === item.uploadId,
      detailError: item.uploadId ? (detailErrors[item.uploadId] ?? '') : '',
      approveBusy: !!item.uploadId && approvingId.value === item.uploadId,
      approveNotice: item.uploadId ? (approveNotices[item.uploadId] ?? null) : null,
    })
  }),
)

const listRows = computed<FilesRowVM[]>(() =>
  uploads.value.map((u) =>
    buildFilesRow({
      key: u.upload_id,
      uploadId: u.upload_id,
      // filename 可能为 null（元数据缺失的行）：如实说「接口未给出文件名」，不填占位假名。
      filename: u.filename || '（接口未给出文件名）',
      mediaType: u.media_type,
      createdAt: u.created_at,
      status: u.status,
      reviewState: u.review_state,
      searchable: u.searchable,
      indexState: u.index_state,
      indexNote: u.index_note,
      indexErrorCode: u.index_error_code,
      blockCount: u.block_count,
      locatorCount: u.locator_count,
      // 列表行只有接口顶层的 worker（逐行没有自己的心跳）。
      worker: worker.value,
      phaseText: '',
      pollExhausted: false,
      errorText: '',
      open: openId.value === u.upload_id,
      detail: details[u.upload_id] ?? null,
      detailLoading: detailLoadingId.value === u.upload_id,
      detailError: detailErrors[u.upload_id] ?? '',
      approveBusy: approvingId.value === u.upload_id,
      approveNotice: approveNotices[u.upload_id] ?? null,
    }),
  ),
)

const batchSummary = computed(() => {
  if (!batchItems.value.length) return ''
  const total = batchItems.value.length
  const rejected = batchItems.value.filter((i) => i.phase === 'rejected').length
  const settled = batchItems.value.filter((i) => i.phase === 'settled').length
  const failed = batchItems.value.filter((i) => i.phase === 'error').length
  const pending = total - rejected - settled - failed
  const parts = [
    `本批 ${total} 个文件`,
    `本地拒绝 ${rejected} 个`,
    `已到终态 ${settled} 个`,
  ]
  if (failed) parts.push(`出错 ${failed} 个`)
  if (pending > 0) parts.push(`仍在处理 ${pending} 个`)
  if (batchRunning.value) parts.push('（正在处理，完成后自动刷新列表）')
  return parts.join('，')
})

const hasFilter = computed(() => !!(applied.status || applied.media_type || applied.since))

const countText = computed(() => {
  const n = pageCount.value
  if (n == null) return '本页读取数量未知'
  return `本页读取 ${n} 份`
})

const emptyText = computed(() =>
  hasFilter.value
    ? '当前筛选下本页没有记录。接口不返回总数，不能据此判断「一条上传都没有」。'
    : '还没有任何上传记录。选择文件上传后，这里会显示解析与复核状态。',
)

/** 心跳横幅：alive=false 时说「未运行」；探测失败说「读不出来」，两者不能混。 */
const workerBanner = computed(() => {
  if (!worker.value) return '后台处理器状态未知：本次列表响应里没有 worker 字段。'
  if (worker.value.error === 'UPLOAD_WORKER_HEARTBEAT_UNREADABLE') {
    return '后台处理器心跳读不出来（UPLOAD_WORKER_HEARTBEAT_UNREADABLE）：这不等于「未运行」，只是这次探测失败。'
  }
  if (worker.value.alive) {
    const last = formatWhen(worker.value.last_seen_at) || '未给出'
    return `后台处理器运行中：最后心跳 ${last}（判定阈值 ${worker.value.alive_seconds}s）。`
  }
  return WORKER_NOT_RUNNING_TEXT
})

const workerTone = computed(() => (worker.value && worker.value.alive ? 'is-ok' : 'is-warn'))

function backToChat(): void {
  void router.push('/app')
}

onMounted(() => {
  void load()
})

onBeforeUnmount(() => {
  disposed = true
})
</script>

<template>
  <div class="files-page">
    <SideNav @new-chat="backToChat" />

    <div class="files-center">
      <header class="files-topbar">
        <div class="files-tb-left">
          <span class="files-tb-title">我的文件</span>
          <span class="files-tb-note">
            上传原始字节 → 独立后台处理器解析 → 人工复核闸门；解析完成 ≠ 可检索
          </span>
        </div>
        <button
          class="files-tb-action"
          type="button"
          :disabled="loading"
          data-testid="files-refresh"
          @click="load"
        >
          {{ loading ? '刷新中…' : '刷新' }}
        </button>
      </header>

      <div class="files-body">
        <FilesUploadPanel
          v-model:provenance="provenance"
          :running="batchRunning"
          :rows="batchRows"
          :summary="batchSummary"
          @pick="onPick"
          @toggle="toggleRow"
          @approve="approveRow"
        />

        <section class="files-list-wrap">
          <div class="files-list-head">
            <h2 class="files-h2">上传记录</h2>
            <span class="files-count" data-testid="files-count">{{ countText }}</span>
          </div>
          <p class="files-count-note">
            服务端不支持翻页，也不给总数：上面这个数字只是本页条数，不能当成全部记录数。
          </p>

          <p class="files-worker" :class="workerTone" data-testid="files-worker-banner">
            {{ workerBanner }}
          </p>

          <div class="files-filters">
            <label class="files-fl" for="files-filter-status">状态</label>
            <select
              id="files-filter-status"
              v-model="filters.status"
              class="files-fs"
              data-testid="files-filter-status"
            >
              <option value="">全部状态</option>
              <option v-for="opt in UPLOAD_STATUS_OPTIONS" :key="opt.value" :value="opt.value">
                {{ opt.label }}（{{ opt.value }}）
              </option>
            </select>

            <label class="files-fl" for="files-filter-media">文件类型</label>
            <select
              id="files-filter-media"
              v-model="filters.media_type"
              class="files-fs"
              data-testid="files-filter-media"
            >
              <option value="">全部类型</option>
              <option v-for="opt in MEDIA_TYPE_OPTIONS" :key="opt.value" :value="opt.value">
                {{ opt.label }}
              </option>
            </select>

            <label class="files-fl" for="files-filter-since">此时间之后</label>
            <input
              id="files-filter-since"
              v-model="filters.since"
              class="files-fi"
              type="text"
              placeholder="如 2026-10-01"
              data-testid="files-filter-since"
            />

            <label class="files-fl" for="files-filter-limit">最多显示</label>
            <input
              id="files-filter-limit"
              v-model.number="filters.limit"
              class="files-fi files-fi-num"
              type="number"
              min="1"
              max="200"
              data-testid="files-filter-limit"
            />

            <button
              class="files-apply"
              type="button"
              :disabled="loading"
              data-testid="files-apply"
              @click="applyFilters"
            >
              应用筛选
            </button>
          </div>

          <p class="files-gate-note" data-testid="files-approve-gate">{{ APPROVE_GATE_NOTE }}</p>

          <p v-if="error" class="files-error" data-testid="files-error">{{ error }}</p>
          <p v-if="loading" class="files-hint" data-testid="files-loading">
            正在加载上传记录…
          </p>
          <p v-else-if="!listRows.length && !error" class="files-empty" data-testid="files-empty">
            {{ emptyText }}
          </p>
          <ul v-else class="files-rows" data-testid="files-list">
            <FilesUploadRow
              v-for="row in listRows"
              :key="row.key"
              :row="row"
              @toggle="toggleRow"
              @approve="approveRow"
            />
          </ul>

          <p class="files-retry" data-testid="files-retry-note">{{ RETRY_HONESTY_TEXT }}</p>
        </section>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 类名统一 files- 前缀：Tailwind v4 的 @layer utilities 会生成同名工具类，
   撞上就会出现「只声明了部分属性、其余被工具类接管」的怪象（仓库已出过两次事故：
   .collapse、.result-caption）。 */
.files-page {
  display: flex;
  height: 100vh;
  width: 100vw;
  overflow: hidden;
  background: var(--color-void);
}
.files-center {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  height: 100%;
}
.files-topbar {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 14px 28px;
  border-bottom: 1px solid var(--color-line);
}
.files-tb-left {
  display: flex;
  align-items: baseline;
  gap: 12px;
  min-width: 0;
}
/* 标题与「模型接入」页一致：蓝色、15px、600（原来是 16px 灰黑，属离群值） */
.files-tb-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--color-accent);
}
.files-tb-note {
  font-size: 14px;
  color: var(--color-text);
}
/* 顶栏按钮统一蓝底白字加粗椭圆（原来是白底细描边） */
.files-tb-action {
  margin-left: auto;
  font: inherit;
  font-size: 14px;
  font-weight: 600;
  padding: 6px 14px;
  border-radius: 999px;
  border: 1px solid transparent;
  background: var(--color-accent);
  color: #ffffff;
  cursor: pointer;
}
.files-tb-action:hover:not(:disabled) {
  background: #1d4ed8;
  color: #ffffff;
}
.files-tb-action:disabled {
  opacity: 0.6;
  cursor: default;
}
.files-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 18px 28px 40px;
  max-width: 1180px;
  width: 100%;
}
.files-list-head {
  display: flex;
  align-items: baseline;
  gap: 12px;
}
.files-h2 {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  color: var(--color-accent);
}
.files-count {
  font-size: 13.5px;
  color: var(--color-body);
}
.files-count-note,
.files-gate-note,
.files-retry {
  margin: 4px 0 0;
  font-size: 13px;
  line-height: 1.7;
  color: var(--color-text);
  /* 这段文案里用 \n 分段，靠 pre-line 真的换行 */
  white-space: pre-line;
}
.files-retry {
  margin-top: 16px;
}
/* 去框：后台处理器状态原来也是「描边 + 圆角 + 浅灰底」。它的类名不含 warn/notice 之类关键词，上一轮全量扫描漏了。改成无框 + 左侧 2px 细条（正常态蓝，is-warn 橙）。 */
.files-worker {
  margin: 12px 0 0;
  font-size: 13.5px;
  line-height: 1.7;
  padding: 2px 0 2px 14px;
  border: 0;
  border-left: 2px solid var(--color-accent);
  border-radius: 0;
  background: none;
  color: var(--color-text);
}
.files-worker.is-warn {
  border-left-color: #d97706;
  color: var(--color-text);
}
/* 筛选栏不再拉成一整条：限制最大宽度、加大间距、控件底对齐。 */
.files-filters {
  display: flex;
  align-items: center;
  gap: 10px 18px;
  flex-wrap: wrap;
  max-width: 1140px;
  margin-top: 18px;
}
.files-fl {
  font-size: 13px;
  font-weight: 500;
  color: var(--color-text);
}
.files-fs,
.files-fi {
  font: inherit;
  font-size: 13.5px;
  padding: 5px 8px;
  border-radius: 8px;
  border: 1px solid var(--color-line);
  background: var(--color-panel);
  color: var(--color-text);
}
/* 固定宽度：原来只给 max-width，几个控件宽窄不一，排起来参差。 */
.files-fs {
  width: 160px;
  min-width: 0;
}
.files-fi {
  width: 200px;
}
.files-fi-num {
  width: 72px;
}
/* 筛选按钮统一成实心蓝（原来是浅蓝底蓝字，同页其它按钮已是实心蓝） */
.files-apply {
  font: inherit;
  font-size: 13.5px;
  font-weight: 600;
  padding: 6px 14px;
  border-radius: 999px;
  border: 1px solid transparent;
  background: var(--color-accent);
  color: #ffffff;
  cursor: pointer;
}
.files-apply:hover:not(:disabled) {
  background: #1d4ed8;
  color: #ffffff;
}
.files-apply:disabled {
  opacity: 0.6;
  cursor: default;
}
.files-rows {
  list-style: none;
  margin: 10px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.files-hint,
.files-empty {
  font-size: 14px;
  color: var(--color-text);
  line-height: 1.7;
}
.files-empty {
  padding: 24px 0;
}
.files-error {
  font-size: 14px;
  color: #e11d48;
  padding: 10px 0;
  word-break: break-all;
}
</style>
