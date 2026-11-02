<!--
  ScrollStack —— 「卷轴堆栈」：卡片随滚动逐张钉住、缩小、叠成一摞。

  移植自 Vue Bits：https://vue-bits.dev/components/scroll-stack
  上游源：DavidHDev/vue-bits → src/content/Components/ScrollStack/ScrollStack.vue

  为适配本项目改了这些（逐条对照上游）：
  1. **去掉 Lenis**。上游用 `lenis` 做平滑滚动（滚动容器模式与 window 模式各一套）。
     `frontend/package.json` 里没有 lenis，装它要改 lock、打包产物要重装 —— 本组件的
     transform 数学本身与 Lenis 无关，Lenis 只负责把 scrollTop 变平滑。这里换成
     原生 `window` 滚动事件 + requestAnimationFrame 合帧，逐张卡的位移/缩放公式与
     上游一字不差（triggerStart / triggerEnd / pinStart / pinEnd / scaleProgress 全保留）。
  2. **只保留 window 滚动模式**。上游默认是「组件自己当滚动容器」（自带 `overflow-y:auto`
     + `pt-[20vh]` + `pb-[50rem]`）。落地页是整页滚动，套一个页中页的滚动条既违反
     营销页习惯，静态截图里也只看得到第一张卡。上游的 `useWindowScroll` 分支在这里
     成为唯一模式，`useWindowScroll` prop 移除。
  3. **量位置的方式改了**（这条是修上游 window 模式的反馈回路）：上游 window 模式用
     `getBoundingClientRect().top + scrollY` 当卡片在文档里的位置，但 rect 会把本组件
     上一帧写进去的 transform 一起算进去 —— 越滚越偏。这里 `measure()` 先清 transform
     再量，量的是**布局位置**；另外每帧开头用根元素的 rect 做一次偏移校验，页面别处
     高度变了（字体加载、上面的动画块收起）会自动重量。
  4. 上游 Tailwind 工具类 → scoped CSS，类名统一 `ss-` 前缀（`.ss-scroll-stack` /
     `.ss-scroll-inner` / `.ss-card` / `.ss-end`）。本仓库被 Tailwind 类名撞名坑过三次
     （`.collapse`、`.table-caption`），不再往这条河里踩。
  5. 上游 `ScrollStackItem` 是用 `h()` 拼出来的（`h-80 my-8 p-12 rounded-[40px]` 写死）。
     这里拆成同目录的 `ScrollStackItem.vue`，几何与卡片底色走 scoped CSS，
     内容走 slot（调用方写自己的标题/说明，不复制 DOM）。
  6. 上游的 `scaleDuration` prop 声明了但从未使用、`onStackComplete` 回调没人接、
     `className` 只是拼字符串 —— 三个都没移植；需要时再加。
  7. 尊重 prefers-reduced-motion：不装滚动监听、不写 transform，卡片就是普通的一列。

  依赖：无（gsap / motion 都没用上；只有原生 scroll + rAF）。

  用法（卡片用同目录的 ScrollStackItem 包住）：
    <ScrollStack :item-distance="110" :item-stack-distance="40">
      <ScrollStackItem v-for="f in features" :key="f.title">…</ScrollStackItem>
    </ScrollStack>
-->
<template>
  <div ref="rootRef" class="ss-scroll-stack">
    <div class="ss-scroll-inner">
      <slot />
      <!-- 上游的立场标记：卡片钉住阶段在这里结束。保持一个空元素，不占视觉。 -->
      <div class="ss-end" aria-hidden="true" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, onUpdated, watch } from 'vue'
import { ref } from 'vue'

const props = withDefaults(
  defineProps<{
    /** 卡片之间的滚动距离（上游 itemDistance: 100） */
    itemDistance?: number
    /** 后面一张卡比前一张多缩多少（上游 itemScale: 0.03） */
    itemScale?: number
    /** 堆叠时每张卡在视口里错开的像素（上游 itemStackDistance: 30） */
    itemStackDistance?: number
    /** 卡片钉在视口高度的什么位置（上游 stackPosition: '20%'） */
    stackPosition?: string
    /** 缩放到什么位置结束（上游 scaleEndPosition: '10%'） */
    scaleEndPosition?: string
    /** 最底那张卡的最终缩放（上游 baseScale: 0.85） */
    baseScale?: number
    /** 每张卡的旋转量（上游 rotationAmount: 0） */
    rotationAmount?: number
    /** 堆里靠后卡片的模糊量（上游 blurAmount: 0） */
    blurAmount?: number
  }>(),
  {
    itemDistance: 100,
    itemScale: 0.03,
    itemStackDistance: 30,
    stackPosition: '20%',
    scaleEndPosition: '10%',
    baseScale: 0.85,
    rotationAmount: 0,
    blurAmount: 0,
  },
)

const rootRef = ref<HTMLElement | null>(null)

let cards: HTMLElement[] = []
let offsets: number[] = []
let endOffset = 0
let rootOffset = 0
let lastTransforms: string[] = []
let frame = 0
let listening = false

function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined' && window.matchMedia
    ? window.matchMedia('(prefers-reduced-motion: reduce)').matches
    : false
}

/** 上游 parsePercentage：'20%' 按容器高换算，数字原样用。 */
function parsePercentage(value: string | number, containerHeight: number): number {
  if (typeof value === 'string' && value.includes('%')) {
    return (parseFloat(value) / 100) * containerHeight
  }
  return parseFloat(String(value))
}

/** 上游 calculateProgress：夹在 0–1。 */
function progressOf(scrollTop: number, start: number, end: number): number {
  if (end === start) return scrollTop >= end ? 1 : 0
  if (scrollTop < start) return 0
  if (scrollTop > end) return 1
  return (scrollTop - start) / (end - start)
}

const round = (value: number, places: number): number => {
  const factor = 10 ** places
  return Math.round(value * factor) / factor
}

/**
 * 只落卡片间距（纯布局，和动效无关）。
 * 必须与「是否降级」分开：prefers-reduced-motion 时不做滚动动画，但卡片之间
 * 仍然要有 itemDistance 的间距 —— 否则三张卡会贴成一坨（实测 margin 0）。
 */
function applySpacing() {
  const root = rootRef.value
  if (!root) return
  cards = Array.from(root.querySelectorAll<HTMLElement>('.ss-card'))
  cards.forEach((card, index) => {
    card.style.marginBottom = index < cards.length - 1 ? `${props.itemDistance}px` : ''
  })
}

/** 量布局位置：先把上一帧写进去的 transform 清掉，再读 rect。 */
function measure() {
  const root = rootRef.value
  if (!root) return
  applySpacing()
  if (!cards.length) return

  // 卡片间距先落定，再量 —— 否则量到的是「还没加间距」的位置
  cards.forEach((card) => {
    card.style.transform = ''
    card.style.filter = ''
    card.style.willChange = 'transform'
    card.style.transformOrigin = 'top center'
    card.style.backfaceVisibility = 'hidden'
  })
  lastTransforms = cards.map(() => '')

  const scrollY = window.scrollY
  offsets = cards.map((card) => card.getBoundingClientRect().top + scrollY)
  const end = root.querySelector<HTMLElement>('.ss-end')
  endOffset = end ? end.getBoundingClientRect().top + scrollY : 0
  rootOffset = root.getBoundingClientRect().top + scrollY
}

/** 上游 updateCardTransforms 的 window 分支，逐项保留。 */
function update() {
  const root = rootRef.value
  if (!root || !cards.length) return

  const scrollTop = window.scrollY
  const containerHeight = window.innerHeight

  // 页面别处高度变了就重量（transform 改动不影响布局，所以这一步不会自己触发）
  const currentRootOffset = root.getBoundingClientRect().top + scrollTop
  if (Math.abs(currentRootOffset - rootOffset) > 1) {
    measure()
    if (!cards.length) return
  }

  const stackPositionPx = parsePercentage(props.stackPosition, containerHeight)
  const scaleEndPositionPx = parsePercentage(props.scaleEndPosition, containerHeight)
  const pinEnd = endOffset - containerHeight / 2

  cards.forEach((card, index) => {
    const cardTop = offsets[index]
    const triggerStart = cardTop - stackPositionPx - props.itemStackDistance * index
    const triggerEnd = cardTop - scaleEndPositionPx
    const pinStart = triggerStart

    const scaleProgress = progressOf(scrollTop, triggerStart, triggerEnd)
    const targetScale = props.baseScale + index * props.itemScale
    const scale = 1 - scaleProgress * (1 - targetScale)
    const rotation = props.rotationAmount ? index * props.rotationAmount * scaleProgress : 0

    let blur = 0
    if (props.blurAmount) {
      let topCardIndex = 0
      cards.forEach((_, other) => {
        const otherStart = offsets[other] - stackPositionPx - props.itemStackDistance * other
        if (scrollTop >= otherStart) topCardIndex = other
      })
      if (index < topCardIndex) blur = Math.max(0, (topCardIndex - index) * props.blurAmount)
    }

    let translateY = 0
    const isPinned = scrollTop >= pinStart && scrollTop <= pinEnd
    if (isPinned) {
      translateY = scrollTop - cardTop + stackPositionPx + props.itemStackDistance * index
    } else if (scrollTop > pinEnd) {
      translateY = pinEnd - cardTop + stackPositionPx + props.itemStackDistance * index
    }

    const transform =
      `translate3d(0, ${round(translateY, 2)}px, 0) ` +
      `scale(${round(scale, 3)}) rotate(${round(rotation, 2)}deg)`
    const filter = blur > 0 ? `blur(${round(blur, 2)}px)` : ''

    if (lastTransforms[index] !== transform || card.style.filter !== filter) {
      card.style.transform = transform
      card.style.filter = filter
      lastTransforms[index] = transform
    }
  })
}

function schedule() {
  if (frame) return
  frame = requestAnimationFrame(() => {
    frame = 0
    update()
  })
}

function start() {
  // 间距永远落：降级模式下卡片也必须有间距，只是不做滚动动效
  applySpacing()
  if (listening || prefersReducedMotion() || !cards.length) return
  listening = true
  window.addEventListener('scroll', schedule, { passive: true })
  window.addEventListener('resize', handleResize)
  measure()
  update()
}

function handleResize() {
  measure()
  schedule()
}

function stop() {
  if (frame) {
    cancelAnimationFrame(frame)
    frame = 0
  }
  if (listening) {
    window.removeEventListener('scroll', schedule)
    window.removeEventListener('resize', handleResize)
    listening = false
  }
  cards.forEach((card) => {
    card.style.transform = ''
    card.style.filter = ''
    card.style.marginBottom = ''
  })
  cards = []
  offsets = []
  lastTransforms = []
}

function restart() {
  stop()
  start()
}

onMounted(async () => {
  await nextTick()
  start()
})

onBeforeUnmount(() => {
  stop()
})

// 调用方换了卡片数量/顺序后重装（本组件不接管内容渲染，只能在这里兜一次）
onUpdated(() => {
  if (!listening) return
  const count = rootRef.value?.querySelectorAll('.ss-card').length ?? 0
  if (count !== cards.length) restart()
})

watch(
  () => [
    props.itemDistance,
    props.itemScale,
    props.itemStackDistance,
    props.stackPosition,
    props.scaleEndPosition,
    props.baseScale,
    props.rotationAmount,
    props.blurAmount,
  ],
  () => restart(),
)
</script>

<style scoped>
/* 上游是「组件自己滚动」的容器；这里是整页滚动，所以只是一个普通块。 */
.ss-scroll-stack {
  position: relative;
  width: 100%;
}

.ss-scroll-inner {
  position: relative;
  width: 100%;
}

/* 立场标记：卡片钉住阶段在它这里结束，本身不可见 */
.ss-end {
  width: 100%;
  height: 1px;
}
</style>
