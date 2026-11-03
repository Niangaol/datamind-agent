<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import SideNav from '../components/SideNav.vue'
import ChatPanel from '../components/chat/ChatPanel.vue'
import ConstellationGraph from '../components/stargraph/ConstellationGraph.vue'
import NodeDetail from '../components/stargraph/NodeDetail.vue'
import { streamChat, getEvidence } from '../api/client'
import { useModelAccess } from '../composables/useModelAccess'
import { useSessions } from '../composables/useSessions'
import type {
  ChatMessage,
  ChatPayload,
  DocumentRef,
  EvidenceGraph,
  EvidenceNode,
  FinalResult,
  SessionMessage,
  SessionRunResult,
} from '../api/types'

const route = useRoute()
const router = useRouter()

const messages = ref<ChatMessage[]>([])
const running = ref(false)
const graph = reactive<EvidenceGraph>({ nodes: [], edges: [], claims: [] })
const selectedNode = ref<EvidenceNode | null>(null)
/** 服务健康来自 /health，与「模型可用」是两件事（见 useModelAccess） */
const { serviceState: healthState, bootstrap: bootstrapModelAccess } = useModelAccess()

/**
 * 主区两个视图：对话 / 证据轨迹。
 *
 * 布局是「侧边栏 + 主区」两栏，主区顶部切换，选中者占满整个主区宽度
 * （1440 视口下约 1200px，1600 下约 1360px），不再是原来那个 490px 的常驻右栏。
 *
 * 两个视图都用 v-show 而不是 v-if：ConstellationGraph / NodeDetail / ChatPanel
 * 都只有一份实例，DOM 与组件状态都不重建。切走再切回来，正在跑的运行、
 * 已产出的证据图、选中的节点、滚动位置全都还在（v-if 会卸载聊天面板，
 * 正在流式写入的消息会跟着一起没）。
 */
type MainView = 'chat' | 'graph'
const activeView = ref<MainView>('chat')
/** 对话侧有新证据产出时的提示点：只提示，不强制把用户从对话里拽走 */
const evidenceHint = ref(false)

/** 从对话切到证据轨迹时清掉提示点 */
watch(activeView, (v) => {
  if (v === 'graph') evidenceHint.value = false
})

/**
 * 会话状态与侧边栏共用同一份（useSessions 是模块级单例）。
 * 这里只读 activeId：侧边栏点行、深链进入、删除活动会话，都会让 activeId 变化，
 * 于是「换会话」只有 watch(activeId) 这一条路径，不存在两套切换逻辑。
 */
const {
  activeId: sessionId,
  bootstrap: bootstrapSessions,
  refresh: refreshSessions,
  loadTranscript,
  create: createSession,
  setBusyGuard,
  revealHistory,
} = useSessions()

/** 会话 id 的严格形状：`sess-` + uuid4().hex[:24]（与后端 sessions._session_id 一致） */
const SESSION_ID_RE = /^sess-[0-9a-f]{24}$/

/** 会话归属字段由 types.ts 补（另一位同学负责），这里按可选字段传 */
type ChatPayloadWithSession = ChatPayload & { conversation_id?: string }

/**
 * 切换会话 = 换一段对话 + 换一份证据，两件事必须同时做。
 * 此前证据图只在 newChat 里被清空过；如果切会话不动它，就会出现
 * 「消息是 B 的、星图还停在 A 的」这种最容易骗人的状态。
 */
function resetEvidence() {
  graph.nodes = []
  graph.edges = []
  graph.claims = []
  selectedNode.value = null
  evidenceHint.value = false
}

/** 把「一个 run 一条」的服务端投影摊平成「用户气泡 + 助手气泡」，顺序即 started_at 顺序 */
function rebuildMessages(rows: SessionMessage[], rich: SessionRunResult[] = []) {
  // 富内容按 run_id 建索引：响应是**最近优先**（与 rows 相反），所以只能按 id 对齐，
  // 不能按下标 zip —— 那是这套接口最容易写错、又最难在页面上看出来的一处。
  const payloads = new Map<string, FinalResult>()
  for (const item of Array.isArray(rich) ? rich : []) {
    if (item && typeof item.run_id === 'string' && item.result) payloads.set(item.run_id, item.result)
  }
  const next: ChatMessage[] = []
  for (const row of Array.isArray(rows) ? rows : []) {
    const runId = String(row?.run_id ?? '')
    const answer = typeof row?.answer === 'string' ? row.answer : ''
    const status = String(row?.status ?? '').toUpperCase()
    // 历史里可能有「还没产出 answer」的 run：仍在跑的渲染成进行中，已结束的按失败呈现，
    // 不能因为 answer 为空就把它显示成一条正常答案。
    const pending = status === 'RUNNING' || status === 'PENDING' || status === 'QUEUED'
    const final = runId ? payloads.get(runId) : undefined
    next.push({
      id: `hist-${runId}-u`,
      role: 'user',
      content: typeof row?.question === 'string' ? row.question : '',
    })
    next.push({
      id: `hist-${runId}-a`,
      role: 'assistant',
      // 正文以 `runs.answer` 为准（它与富内容里的 answer 同源；取 run 行能让「有正文但
      // 没有终态事件」的老 run 照常显示），富内容只补它拿不到的那几块。
      content: answer,
      status: pending ? 'running' : answer ? 'done' : 'error',
      runId: runId || undefined,
      runStatus: (row?.status as ChatMessage['runStatus']) ?? undefined,
      confidence: typeof row?.confidence_level === 'string' ? row.confidence_level : undefined,
      // 历史富内容：SQL 结果表 / 引用 / 工业核验表。这是「打开历史会话只剩纯文本」
      // 的修复点——流式路径在 send() 里做同样三件事（见下面的 final.* 赋值）。
      sqlResult: final?.sql_result,
      citations: final?.rag_result?.citations,
      industrialResult: final?.industrial_result,
      error: final?.error_code ?? undefined,
    })
  }
  messages.value = next
}

/**
 * 历史会话的证据星图：只点亮**最近一个走到终态的 run**。
 *
 * 为什么是「最近一个」而不是全部：星图一次只画一张（与流式路径一致——每次提问都会
 * resetEvidence 再画当前这一轮）。窗口外的更早轮次不再单独取证据，否则打开一个长会话
 * 会变成几十次请求。取不到（该 run 没有证据/接口失败）就保持空图，不猜。
 *
 * 竞态：请求回来的那一刻会话可能已经切走了（`id` 不再是 activeId，或用户已经开始新一轮
 * 流式回答）。这时**必须不写**——否则会出现「消息是 B 的、星图停在 A 的」这种最容易骗人的
 * 状态；调用方也不能在事后补一次 resetEvidence 兜底，那会把 B 刚点亮的证据一起清掉。
 */
async function showEvidenceForHistory(id: string, rows: SessionMessage[], rich: SessionRunResult[]) {
  const withResult = new Set(
    (Array.isArray(rich) ? rich : [])
      .filter((item) => item && item.result)
      .map((item) => item.run_id),
  )
  for (let index = rows.length - 1; index >= 0; index -= 1) {
    const runId = String(rows[index]?.run_id ?? '')
    if (!runId || !withResult.has(runId)) continue
    try {
      const evidence = await getEvidence(runId)
      if (sessionId.value !== id || running.value) return
      graph.nodes = evidence.nodes ?? []
      graph.edges = evidence.edges ?? []
      graph.claims = evidence.claims ?? []
      if (graph.nodes.length) evidenceHint.value = true
    } catch {
      /* 证据图不可用不阻断历史浏览 */
    }
    return
  }
}

/**
 * 读某个会话的 transcript 并铺进消息区。
 * loadTranscript 内部按会话 id 合并并发请求，watch 与 onMounted 同时调用也只发一次。
 */
async function showTranscript(id: string) {
  const history = await loadTranscript(id)
  // 期间又切走了、或正在流式写答案：绝不覆盖本地 messages
  // （取舍理由见 setBusyGuard 的注释）。
  if (sessionId.value !== id || running.value) return
  rebuildMessages(history.messages, history.results)
  await showEvidenceForHistory(id, history.messages, history.results)
}

/**
 * 运行中禁止切换/删除会话 —— 守卫注册进 useSessions，侧边栏的行点击也要过这一关。
 * 取舍：**阻塞切换**而不是「快照后丢弃」。流式回答期间 assistant 那一条还在被
 * SSE 回调持续写入，中途重建 messages 会让已产出的内容凭空消失；阻塞只多一句提示。
 */
setBusyGuard(() => running.value)
onBeforeUnmount(() => setBusyGuard(null))

// activeId 是唯一的切换信号：侧边栏选中、深链启动、删除活动会话都会走到这里
watch(
  sessionId,
  (id) => {
    resetEvidence()
    if (!id) {
      messages.value = []
      return
    }
    void showTranscript(id)
  },
  { immediate: true },
)

function buildPayload(question: string, mode: string, documentRefs?: DocumentRef[]): ChatPayload {
  // 指定文件 = 只看这几份文档：服务端只在文档问答路由接受 document_refs
  // （其它路由会以 DOCUMENT_SELECTION_MODE_INVALID 失败，客户端不制造这次失败）。
  if (documentRefs && documentRefs.length) {
    return { question, mode: 'rag', scope: { document_refs: documentRefs } }
  }
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

/**
 * 新对话：真在服务端建一个新会话并切过去（不再是只清空本地数组），
 * 于是新会话立刻出现在侧边栏且处于选中态。运行中不建（同 setBusyGuard 的理由）。
 */
async function newChat() {
  if (running.value) return
  messages.value = []
  resetEvidence()
  // 失败（接口未就绪/网络）时组合式函数只写 error，不抛：这里保持「本地已清空」，
  // 不会把上一段对话留在屏幕上，用户可以继续提问（send 会再试一次建会话）。
  await createSession()
}

function selectNode(node: EvidenceNode | null) {
  selectedNode.value = node
}

async function send(payload: { question: string; mode: string; documentRefs?: DocumentRef[] }) {
  if (running.value || !payload.question.trim()) return
  const question = payload.question.trim()

  // 会话归属：没有活动会话就先建一个 —— 每个 run 都必须挂在某个会话下，
  // 否则这段问答在侧边栏里永远看不到（runs.conversation_id 为空）。
  let conversationId = sessionId.value
  if (!conversationId) {
    const created = await createSession()
    conversationId = created?.session_id ?? null
  }

  messages.value.push({ id: uid(), role: 'user', content: question })
  const assistant = reactive<ChatMessage>({
    id: uid(),
    role: 'assistant',
    content: '',
    status: 'running',
    thinking: [],
  })
  messages.value.push(assistant)

  // 新一问清空上一问的证据（与 resetEvidence 同一套语义，别再抄一遍）
  resetEvidence()
  running.value = true

  // conversation_id 只在真有会话时才写进 body：没有会话时请求体与旧版逐字节一致
  //（后端也只在收到该键时才给 run() 传 conversation_id，见 api.py::_chat_kwargs）。
  const body: ChatPayloadWithSession = buildPayload(question, payload.mode, payload.documentRefs)
  if (conversationId) body.conversation_id = conversationId

  try {
    const final = await streamChat(body, (ev) => {
      assistant.thinking!.push(ev)
    })

    assistant.runId = final.run_id
    assistant.content = final.answer
    assistant.runStatus = final.status
    assistant.confidence = final.confidence_level
    assistant.error = final.error_code ?? undefined
    if (final.sql_result) assistant.sqlResult = final.sql_result
    if (final.rag_result?.citations) assistant.citations = final.rag_result.citations
    // 工业核验：摘要里的「详见核验表」指的就是这个结果，必须落到消息上才会渲染
    if (final.industrial_result) assistant.industrialResult = final.industrial_result
    assistant.status = final.status === 'FAILED_WITH_EVIDENCE' ? 'error' : 'done'

    // 拉取证据图，点亮证据轨迹
    try {
      const ev = await getEvidence(final.run_id)
      graph.nodes = ev.nodes ?? []
      graph.edges = ev.edges ?? []
      graph.claims = ev.claims ?? []
      // 证据刚到位时如果用户还在对话侧，给提示点（不自动切换，免得打断阅读）
      if (graph.nodes.length) evidenceHint.value = true
    } catch {
      /* 证据图不可用不阻断对话 */
    }
  } catch (e: any) {
    assistant.status = 'error'
    assistant.content = e?.message || '请求失败，请确认后端服务已启动。'
  } finally {
    running.value = false
    // 让侧边栏拿到自动命名后的标题、更新后的排序与轮数；失败也不影响对话展示
    if (conversationId) void refreshSessions()
  }
}

/**
 * 画布尺寸现在是兜底值：ConstellationGraph 以容器实测宽度为准重算画布，
 * 这里只给出首帧容器宽度未知时的期望尺寸。旧版为 490px 侧栏调过的小画布
 * （300×272 / radiusScale 0.46）连同那个 prop 一起不再需要。
 */
const GRAPH_W = 900
const GRAPH_H = 600

const healthText = computed(() =>
  healthState.value === 'ok' ? '服务在线' : healthState.value === 'down' ? '服务离线' : '检测中',
)

let uidSeq = 0
const uid = () => `m-${Date.now()}-${uidSeq++}`

onMounted(async () => {
  // 服务健康 + 模型配置 + 读回上次连通性检测（三件事各自独立）
  void bootstrapModelAccess()

  // 会话深链：#/app?session=<sess-id>（运行记录里的「会话 xxx」、收藏、外部跳转都用它）。
  // 必须在 bootstrapSessions() 之前写 activeId —— 那个函数只在 activeId 为空时才读
  // localStorage，所以「先写」就等于让深链**优先于**上次记住的会话。
  // 刻意**不落盘**：深链是一次性导航，刷新后应当回到用户自己的上次会话，而不是被这个
  // 参数永久劫持（下面 replace 清掉参数，同一道理）。
  const deepLink = typeof route.query.session === 'string' ? route.query.session : ''
  if (SESSION_ID_RE.test(deepLink)) sessionId.value = deepLink

  // 先把会话状态就位（含 localStorage 里记住的活动会话）再处理深链提问，
  // 这样 ?q= 那一次运行会挂进已有会话，而不是又新建一个。
  await bootstrapSessions()

  // #/app?panel=history：落地页的「历史对话」按钮。展开历史区并把焦点放进搜索框
  //（revealHistory 只发信号，DOM 聚焦在 SideNav 里做）。
  let consumedQuery = false
  if (route.query.panel === 'history') {
    revealHistory()
    consumedQuery = true
  }

  // 深链提问：#/app?q=…&mode=retail —— 与旧版控制台的 hash 深链同一套用法
  const q = route.query.q
  const m = route.query.mode
  if (typeof q === 'string' && q.trim()) {
    consumedQuery = true
    await send({ question: q, mode: typeof m === 'string' ? m : 'auto' })
  }
  if (deepLink || consumedQuery) {
    // 清掉一次性参数：否则刷新会重新套用深链、把用户手动切走的会话又拽回来。
    await router.replace({ path: '/app' })
  }
})
</script>

<template>
  <div class="mainpage">
    <SideNav @new-chat="newChat" />

    <div class="center">
      <!-- 顶栏：左侧标题，中间「对话 / 证据轨迹」切换，右侧服务状态与入口 -->
      <header class="topbar">
        <div class="tb-left">
          <span class="tb-title">问数与问答</span>
          <span class="tb-note">自然语言 → SQL / 检索 → 验证 → 带证据的答案</span>
        </div>

        <div class="viewtabs" role="tablist" aria-label="主视图切换">
          <button
            class="viewtab"
            type="button"
            role="tab"
            data-testid="tab-chat"
            :aria-selected="activeView === 'chat'"
            :class="{ 'is-active': activeView === 'chat' }"
            @click="activeView = 'chat'"
          >
            对话
          </button>
          <button
            class="viewtab"
            type="button"
            role="tab"
            data-testid="tab-graph"
            :aria-selected="activeView === 'graph'"
            :class="{ 'is-active': activeView === 'graph' }"
            @click="activeView = 'graph'"
          >
            证据轨迹
            <span v-if="graph.nodes.length" class="tab-count">{{ graph.nodes.length }}</span>
            <span v-if="evidenceHint && activeView !== 'graph'" class="tab-dot" aria-label="有新证据"></span>
          </button>
        </div>

        <div class="tb-right">
          <span class="pill" :class="healthState">
            <span class="dot"></span>{{ healthText }}
          </span>
          <button class="ghost-btn" type="button" @click="router.push('/')">了解原理</button>
        </div>
      </header>

      <!-- 对话视图：不用 v-if 卸载（卸载会丢正在流式写入的消息），只切显示。
           v-show 不够用 —— .view 上的 display:flex 会盖掉 v-show 的 inline display:none，
           两个视图会同时留在布局里（实测截图里对话被挤到证据图下面），所以显式加 .is-hidden。 -->
      <section
        class="view view-chat"
        :class="{ 'is-hidden': activeView !== 'chat' }"
        data-testid="view-chat"
      >
        <ChatPanel :messages="messages" :running="running" @send="send" />
      </section>

      <!-- 证据轨迹视图 -->
      <section
        class="view view-graph"
        :class="{ 'is-hidden': activeView !== 'graph' }"
        data-testid="view-graph"
      >
        <div class="graph-head">
          <div class="gh-titles">
            <span class="eyebrow">Evidence Graph</span>
            <h2>推理星链</h2>
          </div>
          <span class="count">{{ graph.nodes.length }} 节点</span>
          <span class="gh-note">点任意卡片看它的 SQL、检索片段、原始数据或推导关系</span>
        </div>

        <div class="graph-body" :class="{ 'has-detail': !!selectedNode }">
          <div class="graph-wrap">
            <ConstellationGraph
              :nodes="graph.nodes"
              :edges="graph.edges"
              :width="GRAPH_W"
              :height="GRAPH_H"
              @select="selectNode"
            />
            <div v-if="running" class="overlay">
              <span class="pulse-dot"></span> 正在生成证据…
            </div>
            <div v-else-if="graph.nodes.length === 0" class="overlay static">
              <p>提问后，这里会点亮一张证据星图：SQL、检索片段、原始数据与答案主张。点任意卡片看细节。</p>
            </div>
          </div>

          <aside class="detail-col">
            <div class="detail-head">
              <span class="dh-title">节点详情</span>
              <button v-if="selectedNode" class="dh-clear" type="button" @click="selectNode(null)">收起 ✕</button>
            </div>
            <div class="detail">
              <NodeDetail :node="selectedNode" />
            </div>
          </aside>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.mainpage {
  display: flex;
  height: 100vh;
  width: 100vw;
  overflow: hidden;
  background: var(--color-void);
}

/* ── 主区：顶栏 + 视图（对话 / 证据轨迹 二选一，占满整宽） ── */
.center {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  height: 100%;
}
.topbar {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 14px 28px;
  border-bottom: 1px solid var(--color-line);
}
.tb-left {
  display: flex;
  align-items: baseline;
  gap: 12px;
  min-width: 0;
  flex: 1;
}
/* 标题与「模型接入」页一致：蓝色、15px、600（原来是 15.5px 灰黑，属离群值） */
.tb-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--color-accent);
  white-space: nowrap;
}
.tb-note {
  font-size: 14px;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tb-right {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
/* 后端健康度胶囊：判据色在里面的圆点上（绿/红/橙），胶囊本身保持白底描边，
   否则蓝底会把绿点吞掉（与「语义色不动」的约定一致）。 */
.pill {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  font-size: 14px;
  color: var(--color-body);
  border: 1px solid var(--color-line);
  background: var(--color-panel);
  border-radius: 999px;
  padding: 6px 12px;
  white-space: nowrap;
}
.pill .dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #cbd5e1;
}
.pill.ok .dot {
  background: #16a34a;
}
.pill.down .dot {
  background: #e11d48;
}
.pill.checking .dot {
  background: #d97706;
  animation: pill-breathe 1.6s ease-in-out infinite;
}
@keyframes pill-breathe {
  0%, 100% { opacity: 0.45; }
  50% { opacity: 1; }
}
/* 顶栏按钮统一蓝底白字加粗椭圆（原来是白底细描边） */
.ghost-btn {
  border: 1px solid transparent;
  background: var(--color-accent);
  color: #ffffff;
  border-radius: 999px;
  padding: 7px 16px;
  font-family: inherit;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  white-space: nowrap;
  transition: background 0.15s, border-color 0.15s;
}
.ghost-btn:hover {
  background: #1d4ed8;
  color: #ffffff;
}

/* ── 视图切换 ──
   命名注意：类名一律带 view/tab 前缀，避开 Tailwind 生成的 @layer utilities
   （`.collapse` 已经坑过一次：visibility:collapse 让按钮永远看不见）。 */
.viewtabs {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px;
  border: 1px solid var(--color-line);
  border-radius: 999px;
  background: var(--color-ink);
}
.viewtab {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 7px;
  border: none;
  background: transparent;
  color: var(--color-body);
  font-family: inherit;
  font-size: 14px;
  line-height: 1;
  padding: 7px 15px;
  border-radius: 999px;
  cursor: pointer;
  white-space: nowrap;
  transition: background 0.15s, color 0.15s, box-shadow 0.15s;
}
.viewtab:hover {
  color: var(--color-text);
}
.viewtab.is-active {
  background: var(--color-panel);
  color: var(--color-text);
  font-weight: 600;
  box-shadow: 0 1px 3px rgba(15, 23, 42, 0.10);
}
.tab-count {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--color-accent);
  background: rgba(37, 99, 235, 0.10);
  border-radius: 999px;
  padding: 2px 7px;
}
/* 有新证据时的提示点：只提示，不自动切换视图 */
.tab-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--color-accent);
}

.view {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
/* 非活动视图彻底移出布局（组件实例仍在，状态不丢）。
   !important 是刻意的：这一条必须压过 .view 的 display:flex，不能有歧义。 */
.view.is-hidden {
  display: none !important;
}

/* ── 证据轨迹视图 ── */
.graph-head {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 20px 10px;
  border-bottom: 1px solid var(--color-line);
}
.gh-titles {
  min-width: 0;
  display: flex;
  align-items: baseline;
  gap: 10px;
}
/* 英文大写小标签（eyebrow）不是标题：保留小字号与字间距、保持灰字。 */
.eyebrow {
  font-size: 12px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--color-muted);
}
.graph-head h2 {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  color: var(--color-accent);
}
.count {
  font-size: 12.5px;
  color: var(--color-muted);
  border: 1px solid var(--color-line);
  border-radius: 999px;
  padding: 3px 9px;
  white-space: nowrap;
}
.gh-note {
  flex: 1;
  min-width: 0;
  font-size: 13.5px;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 证据轨迹正文：宽屏「图 + 详情」两列，未选中节点时图占满整宽。
   ≤1180px 改成上下排：1000 视口下两列会把图挤到 ~350px，卡片放不下、
   字号掉到最小档（实测 48 节点在那里会横向溢出 50px）。
   上下排之后图拿整宽、详情在图下方，两边都能读。 */
.graph-body {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  overflow: hidden;
}
.graph-body.has-detail {
  grid-template-columns: minmax(0, 1fr) 372px;
}
@media (max-width: 1180px) {
  .graph-body.has-detail {
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: minmax(0, 1fr) minmax(200px, 42%);
  }
  .detail-col {
    border-left: none;
    border-top: 1px solid var(--color-line);
  }
}
.graph-wrap {
  position: relative;
  min-width: 0;
  min-height: 0;
  padding: 14px 18px 16px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.overlay {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: var(--color-accent);
  font-size: 14.5px;
  pointer-events: none;
}
.overlay.static p {
  max-width: 320px;
  text-align: center;
  color: var(--color-text);
  line-height: 1.7;
  margin: 0;
  font-size: 14px;
}
.pulse-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--color-accent);
  animation: pulse 1.2s ease-in-out infinite;
}
@keyframes pulse {
  0%, 100% { opacity: 0.3; transform: scale(0.8); }
  50% { opacity: 1; transform: scale(1.1); }
}
.detail-col {
  display: none;
  min-width: 0;
  min-height: 0;
  flex-direction: column;
  border-left: 1px solid var(--color-line);
  background: var(--color-panel);
}
.graph-body.has-detail .detail-col {
  display: flex;
}
.detail-head {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 16px 8px;
}
/* 详情头的小标签同属大写小标签：字号与灰字都保留 */
.dh-title {
  flex: 1;
  font-size: 12.5px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--color-muted);
}
/* 「收起 ✕」：与其它按钮统一成蓝底白字椭圆（原来是白底细描边） */
.dh-clear {
  border: 1px solid transparent;
  background: var(--color-accent);
  color: #ffffff;
  border-radius: 999px;
  padding: 4px 12px;
  font-family: inherit;
  font-size: 12.5px;
  font-weight: 600;
  cursor: pointer;
}
.dh-clear:hover {
  background: #1d4ed8;
  color: #ffffff;
}
.detail {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 0 16px 16px;
}

/* ── 窄屏（≤1100px）：侧边栏仍在，主区变窄 ──
   没有抽屉、没有遮罩、没有浮动按钮：切换式在两个视图之间切，
   本身就适配窄屏，不需要第二套入口。
   （证据轨迹在图 + 详情两列的断点在上面的 1180px 处。） */
@media (max-width: 1100px) {
  .topbar {
    padding: 12px 16px;
  }
  .tb-note {
    display: none;
  }
  .gh-note {
    display: none;
  }
  .graph-wrap {
    padding: 12px;
  }
}
</style>
