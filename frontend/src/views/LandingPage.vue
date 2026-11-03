<script setup lang="ts">
import { useRouter } from 'vue-router'
import FoldText from '../components/bits/FoldText/FoldText.vue'
import BlurText from '../components/bits/BlurText/BlurText.vue'
import RotatingText from '../components/bits/RotatingText/RotatingText.vue'
import TrueFocus from '../components/bits/TrueFocus/TrueFocus.vue'
import PixelSwap from '../components/bits/PixelSwap/PixelSwap.vue'
import Folder from '../components/bits/Folder/Folder.vue'
import StaggeredMenu from '../components/bits/StaggeredMenu/StaggeredMenu.vue'
import ScrollStack from '../components/bits/ScrollStack/ScrollStack.vue'
import ScrollStackItem from '../components/bits/ScrollStack/ScrollStackItem.vue'
import FlipCard from '../components/FlipCard.vue'
/* 「知识图谱 / 推理卡片」那两张展示图：从本机跑着的应用里截的真图，
   放在 assets/landing/ 下，由 Vite 按 import 生成带 hash 的 URL。 */
import wikiGraphShot from '../assets/landing/wiki-graph.webp'
import reasoningCardShot from '../assets/landing/reasoning-card.webp'
import { useReveal } from '../composables/useReveal'

const router = useRouter()
useReveal()

/**
 * 区块滚动。
 * 本应用路由是 hash 模式（地址栏的 # 就是路由本身），所以不能用 <a href="#xxx">
 * 做页内锚点——那会被路由当成路径解析、跳到不存在的路由后白屏
 * （队友实测点「原理」后进入 #/principle 空白页）。改为直接滚动，不碰地址栏。
 */
function scrollTo(id: string) {
  document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

/**
 * 去历史对话：跳工作台并带 `?panel=history`，由 MainPage 展开历史区并把焦点放进搜索框
 * （见 docs/CONVERSATION_HISTORY_DESIGN.md P0-1）。
 *
 * WHY 只是一个跳转参数：落地页拿不到、也不应该拿会话列表 —— 它是对外页面，
 * 在这里渲染会话标题等于把私有的对话记录搬到公开入口上。
 */
function openHistory() {
  void router.push({ path: '/app', query: { panel: 'history' } })
}

/**
 * 「一条问题，四步落地」那四张翻转卡的键盘分支。
 * FlipCard 内部只监听 click（见 components/FlipCard.vue），键盘用户没有鼠标就用不了；
 * 卡片在外面补了 role="button" + tabindex="0"，回车/空格时替它补一次 click。
 * 按住不放产生的重复 keydown 要挡掉，否则会连续翻面。
 */
function flipByKeyboard(e: KeyboardEvent) {
  if (e.repeat) return
  const card = e.currentTarget as HTMLElement | null
  card?.click()
}

/** Hero 副标题里轮换的能力词。 */
const rotating = ['精准问数', '文档问答', '跨源核验']

/** 顶部粘稠导航的三项（纯展示，不跳转，替代原来的静态标签）。 */
const navItems = [
  { label: '多模态数据驱动' },
  { label: '可解释' },
  { label: '可溯源' },
]

/**
 * 右下角那个大文件夹里飞出的三张纸——产品的三条核心主张。
 * 原来是顶部粘稠导航的三项，用户要求移进文件夹并删掉导航。
 *
 * Folder 的纸张是向左、向右、向上扇形展开的，左右两张展开后仍有部分压在
 * 文件夹翻盖下面（翻盖 z-30、纸张 z-20，组件的原始设计）。size=3.4 时纸张
 * 有 238–306px 宽，但『多模态数据驱动』7 个字横排在左右两张纸上仍会被翻盖截断，
 * 故用 \\n 折成两行（CSS 用 white-space: pre-line 让其生效）。
 * 数量上限是组件写死的 maxItems = 3。
 */
const topFeatures = ['多模态数据驱动', '可解释', '可溯源']

/** 关键词条：TrueFocus 会逐个聚焦。 */
const keywords = '可解释 可溯源 跨源核验 图文对照 冲突并陈'

/**
 * 「看看我们」下面那四条 —— 现在交给 Vue Bits 的错开菜单（StaggeredMenu）渲染。
 * 序号 01–04 由组件内的 CSS 计数器生成，这里只给文案。
 * 文案与改造前逐字一致；原来行首的 ✓ 是列表符号，改由序号承担，文字本身没动。
 */
const claims = [
  { label: 'SQL 全文可见' },
  { label: '检索片段可点回原文' },
  { label: '冲突来源并陈' },
  { label: '推理链路可回放' },
]

const features = [
  {
    icon: '◉',
    bg: '#e8f1ff',
    title: '可解释',
    desc: '答案不是终点。从自然语言到 SQL、检索、验证的完整推理链路，全程可回放。',
  },
  {
    icon: '⌖',
    bg: '#cfe0ff',
    title: '可溯源',
    desc: '每条证据都定位到原文页码、数据库行、字段。点开星点，回到它诞生的地方。',
  },
  {
    icon: '✦',
    bg: '#b3ccff',
    title: '跨源多跳',
    desc: '结构化数据库与非结构化文档自动路由、联合核验，冲突来源并陈、保留双方原值。',
  },
]

/**
 * 「一条问题，四步落地」四张翻转卡的背面文案。
 *
 * d 现在是一行一句的短语数组，不再是带标点的整句：用户要求卡面里不要任何标点、
 * 文字居中、一行一行分开。数组就是把原来那句按标点（，、/）断开的，
 * **文字本身与改造前逐字一致**——只删了标点、换成换行，没有改写、增删任何一个词。
 * 例：`解析意图，判断该查库、查文档，还是两者联合` → 四行。
 */
const steps = [
  { n: '01', t: '理解问题', d: ['解析意图', '判断该查库', '查文档', '还是两者联合'] },
  { n: '02', t: '路由与规划', d: ['选择数据源', '生成带预算的执行计划'] },
  { n: '03', t: '执行与验证', d: ['生成 SQL', '检索片段', '逐条校验数字与引用'] },
  { n: '04', t: '答案 + 证据', d: ['输出可点击的证据图', '冲突如实呈现'] },
]

// 原来这里有一份「证据星图」的静态演示数据（sampleNodes / sampleEdges）。这一节改成
// 两张真实截图后，画布与那份数据一并删掉（ConstellationGraph.vue 组件本身保留，
// 工作台的问答页仍在用它）。
</script>

<template>
  <div class="landing">
    <!-- 导航 -->
    <header class="nav">
      <div class="brand">
        <span class="logo">DM</span>
        <strong>DataMind Agent</strong>
      </div>
      <nav class="links">
        <a href="#principle" @click.prevent="scrollTo('principle')">原理</a>
        <a href="#features" @click.prevent="scrollTo('features')">能力</a>
        <a href="#starmap" @click.prevent="scrollTo('starmap')">星图</a>
        <a href="#how" @click.prevent="scrollTo('how')">流程</a>
      </nav>
      <div class="nav-actions">
        <!-- 历史对话：首页到历史的入口（此前是断头路，只能点「进入体验」再自己找）。
             这里**只放一个跳转按钮**，绝不渲染任何会话标题/摘要 —— 落地页是对外页面，
             会话内容是私有的（见 docs/CONVERSATION_HISTORY_DESIGN.md P0-1 的红线）。 -->
        <button class="ghost-cta" data-testid="landing-history" @click="openHistory">
          历史对话
        </button>
        <button class="cta" @click="router.push('/app')">进入体验</button>
      </div>
    </header>

    <!-- Hero -->
    <section id="top" class="hero">
      <div class="hero-stack">
        <div class="hero-cols">
          <div class="hero-inner">

        <!-- 巨大标题：分片渲染是为了让关键词单独着色（曲速文本是 WebGL 画布，
             整行只能一个颜色，做不出这种局部强调），入场用逐字模糊。 -->
        <h1 class="sr-only">多模态数据驱动的可解释精准问数智能体</h1>
        <div class="hero-title" aria-hidden="true">
          <div class="title-line">
            <BlurText text="多模态数据驱动" :delay="55" animate-by="letters" class="title-piece" />
          </div>
          <div class="title-line">
            <BlurText text="可解释" :delay="55" animate-by="letters" class="title-piece accent" />
            <BlurText text="精准问数" :delay="55" animate-by="letters" class="title-piece" />
          </div>
        </div>

        <!-- 小字介绍：整段连续，不人为断行 -->
        <p class="sub">
          DataMind Agent —— 面向数据库与知识库的
          <RotatingText :texts="rotating" class="rotator" :rotation-interval="2200"
                        :transition="{ type: 'spring', damping: 26, stiffness: 320 }" />
          智能体。每一句答案，都能回溯到它的 SQL、检索片段与原始数据。
        </p>

        <div class="hero-cta">
          <button class="primary" @click="router.push('/app')">点击即试 →</button>
          <a class="ghost" href="#principle" @click.prevent="scrollTo('principle')">了解原理</a>
        </div>
          </div>

          <!-- 右下角：一个大文件夹，向左倾斜使出口朝 10 点钟方向；点开飞出三张纸 -->
          <aside class="folder-corner">
            <div class="folder-tilt">
              <Folder color="#2563eb" :size="3.4" :items="topFeatures">
                <template #item-1><div class="paper">{{ topFeatures[0] }}</div></template>
                <template #item-2><div class="paper">{{ topFeatures[1] }}</div></template>
                <template #item-3><div class="paper">{{ topFeatures[2] }}</div></template>
              </Folder>
            </div>
          </aside>
        </div>
      </div>
    </section>

    <!-- 关键词条：真正的专注 -->
    <section class="keyword-strip">
      <TrueFocus :sentence="keywords" :blur-amount="4" border-color="#2563eb"
                  glow-color="rgba(37, 99, 235, 0.35)" :animation-duration="0.45"
                  :pause-between-animations="0.9" />
    </section>

    <!-- 原理：翻牌卡 + 折叠文字 -->
    <section id="principle" class="section">
      <div class="sec-head reveal">
        <div class="eyebrow">Why DataMind</div>
        <!-- 「看看我们」从 FoldText 里拆出来单独一段，好让它单独上蓝色；
             前半句仍走 FoldText 的翻折入场。 -->
        <div class="principle-title">
          <FoldText text="答案要经得起追问——" split-by="word" hinge="top" font-size="clamp(1.8rem, 4vw, 3rem)"
                    font-weight="800" color="#0b1220" :duration="0.6" :stagger="0.06" trigger="scroll" />
          <span class="principle-see-us">看看我们</span>
        </div>
        <!-- 原来是翻牌卡正面的「AI 黑箱？」，卡片去掉后这段按用户要求留在这里当小字注解。 -->
        <p class="principle-note">
          AI 黑箱？一句话进去，一个答案出来——中间发生了什么，没人知道。
        </p>
      </div>

      <!-- 用户最后的意见：不要框（翻牌卡整块去掉），四行字直接铺在页面上，
           而且要更大。所以这一节只剩「标题 + 小字注解 + 四行错开菜单」。 -->
      <div class="claims-band reveal">
        <StaggeredMenu :items="claims" accent-color="#2563eb"
                       font-size="clamp(1.9rem, 4.2vw, 3.4rem)" :delay="0.2" />
      </div>
    </section>

    <!-- 能力：三个关键词 = Vue Bits 的卷轴堆栈（ScrollStack），竖向居中排列。
         等高线背景（Topography）已按用户要求整块删掉；原来叠在卡片下面的液态玻璃
         （GlassSurface）与镜面高光（SpecularButton）也一并撤掉 —— 卷轴堆栈会把每张卡
         整体 scale + translate，WebGL 玻璃层跟着 transform 缩放会发糊，多层描边叠起来
         也不成形。三个板块的图标、标题、说明一字未改，只换呈现方式。 -->
    <section id="features" class="feat-band">
      <div class="feat-inner">
        <div class="sec-head center">
          <div class="eyebrow">Capabilities</div>
          <BlurText text="三个关键词" :delay="120" animate-by="words" direction="top"
                    class="blur-heading" />
        </div>

        <!-- item-distance 是卡片之间的滚动距离，item-stack-distance 是堆起来时错开的像素：
             两者一起决定「钉住 → 叠成一摞」要滚多长。 -->
        <ScrollStack class="feat-stack" :item-distance="112" :item-stack-distance="42"
                     :base-scale="0.87" :item-scale="0.02" :blur-amount="0.5">
          <ScrollStackItem v-for="f in features" :key="f.title" :style="{ '--feat-bg': f.bg }">
            <div class="feat-card-inner">
              <!-- 左列：图标 + 大标题成一组；右列：描述。两列才不会各占一格。 -->
              <div class="feat-lead">
                <h3 class="feat-title">{{ f.title }}</h3>
              </div>
              <p class="feat-desc">{{ f.desc }}</p>
            </div>
          </ScrollStackItem>
        </ScrollStack>
      </div>
    </section>
    <!-- 知识图谱与推理卡片：两张真实截图并排（这一节不再有画布，也不再讲星图） -->
    <section id="starmap" class="section">
      <div class="sec-head">
        <div class="eyebrow">Wiki &amp; Reasoning</div>
        <BlurText text="图谱看结构，卡片看依据" :delay="90" animate-by="words" class="blur-heading" />
        <p class="sec-note">
          一张是当前授权范围图：节点是词条，连线是词条之间已保存的关系。另一张是一次问答的证据轨迹：一条答案主张、它的来源文档与检索片段，连线表示推导关系。
        </p>
      </div>

      <div class="showcase reveal">
        <figure class="showcase-item">
          <div class="showcase-frame">
            <img class="showcase-img" :src="wikiGraphShot"
                 alt="知识图谱：当前授权范围内的词条与它们之间的关系"
                 loading="lazy" decoding="async" />
          </div>
          <figcaption class="showcase-cap">知识图谱 · 当前授权范围图</figcaption>
        </figure>

        <figure class="showcase-item">
          <div class="showcase-frame">
            <img class="showcase-img" :src="reasoningCardShot"
                 alt="证据轨迹：答案主张、来源文档与检索片段连成的推理链"
                 loading="lazy" decoding="async" />
          </div>
          <figcaption class="showcase-cap">证据轨迹 · 推理链与检索片段</figcaption>
        </figure>
      </div>
    </section>

    <!-- 流程 -->
    <section id="how" class="section soft how-band">
      <div class="sec-head">
        <div class="eyebrow">How it works</div>
        <BlurText text="一条问题，四步落地" :delay="90" animate-by="words" class="blur-heading" />
      </div>
      <div class="steps">
        <!-- 四步卡：Vue Bits 的可翻转卡（components/FlipCard.vue），点一下翻面。
             正面只有步骤小标题（大号克莱因蓝、居中占满），背面是步骤描述（同样大字）。
             FlipCard 的两面都是绝对定位，卡片高度必须由 .steps .how-card 给，否则整卡塌成 0。
             FlipCard 只监听 click，卡片自己补 role/tabindex 与回车、空格的键盘分支。 -->
        <FlipCard v-for="(s, i) in steps" :key="s.n" class="how-card reveal"
                  :data-testid="`how-card-${s.n}`" role="button" tabindex="0"
                  :style="{ transitionDelay: i * 90 + 'ms' }"
                  @keydown.enter.prevent="flipByKeyboard" @keydown.space.prevent="flipByKeyboard">
          <template #front>
            <span class="how-card-title">{{ s.t }}</span>
          </template>
          <template #back>
            <div class="how-card-back">
              <span class="how-card-n">{{ s.n }}</span>
              <!-- 一句一行：s.d 是按标点断开后的短语数组，逐条占一行、居中 -->
              <p class="how-card-desc">
                <span v-for="line in s.d" :key="line" class="how-card-line">{{ line }}</span>
              </p>
            </div>
          </template>
        </FlipCard>
      </div>
    </section>

    <!-- 最终 CTA：像素交换做宣传语 -->
    <section id="try" class="final">
      <div class="final-inner reveal">
        <div class="swap-slot">
          <PixelSwap trigger="hover" pattern="center" :pixel-size="52" :duration="1100"
                     :pixel-duration="420" aspect-ratio="16 / 5" easing="cubic-bezier(0.22, 1, 0.36, 1)">
            <template #first>
              <div class="swap-face"><span>现在就问一句</span></div>
            </template>
            <template #second>
              <div class="swap-face alt"><span>看证据如何点亮</span></div>
            </template>
          </PixelSwap>
        </div>
        <p class="final-note">鼠标悬停上方文字可切换</p>
        <button class="primary big" @click="router.push('/app')">开始体验 →</button>
      </div>
    </section>

    <footer class="foot">
      <span>DataMind Agent · 多模态数据驱动的可解释精准问数/问答智能体</span>
      <span>演示数据，不代表真实生产环境</span>
    </footer>
  </div>
</template>

<style scoped>
.landing {
  /* 纯色背景，不使用任何星空/渐变叠层 */
  background: var(--color-void);
  color: var(--color-text);
  overflow-x: hidden;
}

/* 仅给读屏与搜索引擎，视觉上隐藏（大标题是逐字分片渲染的，要有语义标题兜底） */
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

/* ── 导航 ── */
.nav {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  z-index: 50;
  display: flex;
  align-items: center;
  gap: 28px;
  padding: 16px 40px;
  background: rgba(255, 255, 255, 0.82);
  backdrop-filter: blur(16px);
  border-bottom: 1px solid var(--color-line);
}
.brand {
  display: flex;
  align-items: center;
  gap: 10px;
}
.logo {
  width: 32px;
  height: 32px;
  border-radius: 9px;
  display: grid;
  place-items: center;
  font-weight: 700;
  color: #ffffff;
  background: linear-gradient(135deg, #2563eb, #6366f1);
  font-size: 14.5px;
}
.brand strong {
  font-size: 16px;
  font-weight: 600;
}
.links {
  display: flex;
  gap: 22px;
  margin-left: auto;
}
.links a {
  color: var(--color-body);
  text-decoration: none;
  font-size: 15px;
  transition: color 0.2s;
}
.links a:hover {
  color: var(--color-text);
}
.cta {
  padding: 8px 18px;
  border-radius: 999px;
  border: none;
  background: var(--color-text);
  color: #ffffff;
  font-size: 15px;
  font-weight: 500;
  cursor: pointer;
  transition: opacity 0.2s;
}
.cta:hover {
  opacity: 0.85;
}
/* 顶栏右侧的两个按钮：历史对话（次要）与进入体验（主要）并排 */
.nav-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.ghost-cta {
  padding: 8px 16px;
  border-radius: 999px;
  border: 1px solid var(--color-line);
  background: transparent;
  color: var(--color-body);
  font-family: inherit;
  font-size: 15px;
  cursor: pointer;
  transition: border-color 0.2s, color 0.2s;
}
.ghost-cta:hover {
  border-color: rgba(37, 99, 235, 0.45);
  color: #2563eb;
}

/* ── Hero ── */
.hero {
  position: relative;
  /* 整宽 + overflow: hidden：文件夹要向右下溢出、被页面边缘切掉，
     裁切必须发生在视口边缘而不是 1360px 的内容边界上。 */
  overflow: hidden;
  padding: 148px 0 0;
  display: flex;
  justify-content: flex-start;
}
/* 外层铺满整个 hero 内容宽度，让粘稠导航可以相对整页居中；
   内层仍是黄金分割两栏：左 61.8% 正文，右 38.2% 放特点文件夹。 */
.hero-stack {
  width: 100%;
  max-width: 1360px;
  margin: 0 auto;
  padding: 0 40px 70px;
}
.hero-cols {
  display: grid;
  grid-template-columns: 61.8% 38.2%;
  gap: 28px;
  /* 文件夹区贴底，落在右下角 */
  align-items: end;
}
/* 左栏正文：左对齐，非对称留白更有设计感 */
.hero-inner {
  width: 100%;
  text-align: left;
}

/* ── 右下角的大文件夹 ── */
.folder-corner {
  grid-column: 2;
  align-self: end;
  justify-self: end;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  /* 旋转不改变布局盒、视觉会外扩，右侧必须留够余量：
     size=3 的文件夹是 300×240，转 38° 后外接矩形约 384×374，
     左右各外扩约 42px——原来只留 30px，实测被视口右边缘切掉。 */
  /* 只向右出血：负的右边距把它推出内容区，交给 .hero 的 overflow: hidden
     在屏幕右边缘截断。下方不做出血——底部要完整可见，不能被下边界切。 */
  margin: 0;
  /* 整体再往下挪一点。用 relative + top 而不是负外边距：负外边距会把它推到
     .hero 的 overflow: hidden 之外、被下边界切掉（用户明确不要被下边遮住）。
     .hero-stack 底部有 70px 内边距，44px 不会越界。 */
  position: relative;
  top: 44px;
}
/* 向左倾斜，让文件夹的出口（原本朝正上方）指向 10 点钟方向。
   钟面上 10 点位于正上方逆时针 60°，但整只文件夹连同纸张一起转会
   让纸上的字也倒 60°、基本读不了；实测 -38° 时出口落在 10–11 点之间，
   倾斜感明确而文字仍可读。要严格对准 10 点，把这里改成 -60deg 即可。 */
.folder-tilt {
  /* Folder 内部用 transform: scale(size) 放大，而 transform 不改变布局占位——
     它的布局盒仍是 100×80，视觉却有 300×240，导致下面的说明被压住
     （实测只露出「逐条」两个字）。这里按缩放后的真实尺寸撑开容器。 */
  width: 340px;
  height: 272px;
  display: grid;
  place-items: center;
  transform: rotate(-26deg);
  transform-origin: 50% 55%;
}

/* Folder 组件不给纸张设任何排版，slot 内容默认贴左上，这里接管。
   纸张点开后彼此重叠、可见面积比看上去小，所以纸上只放 4 字的特性名，
   放「标题 + 说明」实测说明会被截断。 */
.folder-tilt :deep(div) {
  font-size: 14.5px;
  line-height: 1.3;
}
/* :deep 是必需的——.paper 经 slot 渲染进子组件内部，拿不到本组件的 scope 属性。
   每个特性名排成两行、每行 2 个字，形成一个方块：Folder 的左右两张纸展开后
   大部分仍压在翻盖下（翻盖 z-30、纸张 z-20），横排 4 字必然被截，
   排成 2×2 的方块则任何角度都放得下。 */
.folder-tilt :deep(.paper) {
  padding: 12px 10px;
  font-size: 15px;
  font-weight: 700;
  line-height: 1.3;
  letter-spacing: 0.04em;
  color: var(--color-text);
  text-align: center;
  /* pre-line 让 \n 生效，同时不合并空格 */
  white-space: nowrap;
}

/* 巨大标题：参考 Dify 的hero——极大字号、行高收紧、字距微负。
   字号上限 7rem 是按列宽反推的安全值：7 个字 × 112px ≈ 757px，列宽 840px 留有余量。
   再大就会让 BlurText 内部逐字换行，把标题拆散。 */
.hero-title {
  margin: 22px 0 26px;
  font-size: clamp(3rem, 7.8vw, 7rem);
  font-weight: 800;
  line-height: 1.08;
  letter-spacing: -0.035em;
  color: var(--color-text);
}
.title-line {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-start;
  align-items: baseline;
}
/* BlurText 的根元素是 p.flex.flex-wrap：这里让它按行内块参与标题排版 */
.title-piece {
  display: inline-flex;
  flex-wrap: wrap;
  justify-content: flex-start;
  margin: 0;
  font-size: inherit;
  font-weight: inherit;
  line-height: inherit;
  letter-spacing: inherit;
  color: inherit;
}
.title-piece.accent {
  color: var(--color-accent);
}

.sub {
  margin: 0;
  /* 收在大标题宽度之内：大标题 7 个字约占内容列 96%，这里取 92% 保证永不超出，
     同时右边缘看起来是与标题对齐的 */
  max-width: 92%;
  color: var(--color-body);
  font-size: clamp(0.98rem, 1.25vw, 1.2rem);
  line-height: 1.85;
}
/* 轮换词胶囊。
   RotatingText 的根与「分片层」都带 flex-wrap，列一窄就会把单词逐个字符换行
   （实测出现「跨/源/核/验」竖排）。
   注意：该组件里的 Motion 虽然写了 tag="span"，实际渲染出来的是 <div>，
   所以必须用通配后代选择器——写成 :deep(span) 会一条都匹配不到。
   .rotator[data-v-x] * 的特异性 (0,2,0) 高于 Tailwind 的 .flex-wrap (0,1,0)，
   不需要 !important。 */
.rotator,
.rotator :deep(*) {
  flex-wrap: nowrap;
  white-space: nowrap;
}
.rotator {
  display: inline-flex;
  align-items: center;
  /* 固定一行高：进出场时新旧两个词会同时留在 DOM 里，不锁高会把整段顶下去 */
  height: 1.75em;
  vertical-align: baseline;
  margin: 0 4px;
  padding: 0 10px;
  border-radius: 8px;
  background: rgba(37, 99, 235, 0.08);
  color: var(--color-accent);
  font-weight: 600;
  overflow: hidden;
}
.hero-cta {
  display: flex;
  gap: 12px;
  justify-content: flex-start;
  margin-top: 32px;
}
.primary {
  padding: 15px 32px;
  border: none;
  border-radius: 10px;
  background: var(--color-accent);
  color: #ffffff;
  font-size: 18px;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.2s, transform 0.2s;
}
.primary:hover {
  background: #1d4ed8;
  transform: translateY(-1px);
}
.primary.big {
  padding: 17px 40px;
  font-size: 18px;
}
.ghost {
  padding: 15px 30px;
  border-radius: 10px;
  border: 1px solid var(--color-line);
  color: var(--color-text);
  text-decoration: none;
  font-size: 18px;
  background: #ffffff;
  transition: border-color 0.2s, background 0.2s;
}
.ghost:hover {
  border-color: rgba(15, 23, 42, 0.24);
  background: var(--color-ink);
}

/* ── 关键词条 ── */
.keyword-strip {
  max-width: 1360px;
  margin: 0 auto;
  padding: 34px 40px 6px;
  display: flex;
  justify-content: flex-start;
}
/* TrueFocus 把每个词写死成 Tailwind 的 text-7xl（72px），直接用在页面上会撑破一行。
   这里覆盖成受控字号；选择器特异性高于工具类，无需 !important。 */
.keyword-strip :deep(span.font-black) {
  font-size: clamp(0.95rem, 1.9vw, 1.45rem);
  line-height: 1.5;
  letter-spacing: -0.01em;
  color: var(--color-text);
}

/* ── 通用区块 ── */
.section {
  padding: 92px 40px;
  max-width: 1360px;
  margin: 0 auto;
}
.section.soft {
  max-width: none;
  background: var(--color-ink);
  padding: 92px 40px;
}
.section.soft > * {
  max-width: 1360px;
  margin-left: auto;
  margin-right: auto;
}
/* 区块标题同样左对齐：只有 hero 左对齐、其余居中会显得两套版式打架。
   文字列仍按黄金分割收窄，保持右侧留白。 */
.sec-head {
  text-align: left;
  max-width: 720px;
  margin-bottom: 44px;
}
.eyebrow {
  font-size: 13.5px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--color-muted);
  margin-bottom: 12px;
}
.sec-note {
  margin: 14px 0 0;
  max-width: 620px;
  color: var(--color-body);
  font-size: 16px;
  line-height: 1.8;
}
/* BlurText 根元素是 <p>，这里把它当标题用 */
.blur-heading {
  justify-content: flex-start;
  margin: 0;
  font-size: clamp(1.8rem, 4vw, 3rem);
  font-weight: 800;
  color: var(--color-text);
  letter-spacing: -0.02em;
}

/* ── 原理 ── */
/* 最后一轮改造：外层那个圆角边框容器（.principle-box）连同右栏「答案必须经得起追问」
   已按用户要求删掉，这一节只剩标题 + 一张翻牌卡。
   卡片比原来大：背面要塞下错开菜单的四行大字（原来四行是 15px 的清单）。 */
/* 四行错开菜单：不要卡片框（用户要求），直接铺在页面上、字号更大。 */
/* 标题行：「答案要经得起追问——」走 FoldText，「看看我们」单独取蓝色。 */
.principle-title {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 0 4px;
}
.principle-see-us {
  font-size: clamp(1.8rem, 4vw, 3rem);
  font-weight: 800;
  line-height: 1.1;
  color: var(--color-accent);
}
/* 原翻牌卡正面的「AI 黑箱？」：卡片去掉后按用户要求做标题下的小字注解。 */
.principle-note {
  margin: 10px 0 0;
  font-size: 13px;
  line-height: 1.7;
  color: var(--color-muted);
}

.claims-band {
  margin: 28px 0 0;
  max-width: 980px;
}
.flipcard {
  height: 396px;
}
.face-content {
  padding: 34px 40px 36px;
  width: 100%;
  height: 100%;
  align-self: stretch;
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 12px;
  /* FlipCard 的两面都是居中的；背面要装左对齐的菜单，所以基础值改成左对齐 */
  align-items: flex-start;
  text-align: left;
}
/* 正面「AI 黑箱？」是提问，仍居中，翻过来才有明显变化 */
.face-content.front {
  align-items: center;
  text-align: center;
}
/* 背面：标题在上、菜单在下，整体垂直居中 */
.face-content.back {
  justify-content: center;
  gap: 22px;
}
/* 字号随框宽走：原来固定 30px，塞在 1200×700 的框里显得空。 */
.face-content .big {
  font-size: clamp(2.4rem, 4.4vw, 3.8rem);
  font-weight: 800;
  letter-spacing: -0.02em;
  line-height: 1.15;
}
.face-content .big.light {
  color: var(--color-accent);
}
.face-content p {
  margin: 0;
  color: var(--color-body);
  font-size: clamp(1.05rem, 1.7vw, 1.4rem);
  line-height: 1.75;
}
.face-content .hint {
  font-size: clamp(0.9rem, 1.1vw, 1.05rem);
  color: var(--color-muted);
}

/* ── 能力区：三个关键词 = 卷轴堆栈（ScrollStack） ──
   这一节自己铺背景，所以不再用 .soft 的灰底：留一层很淡的冷蓝渐变做底。
   等高线画布（Topography）已按用户要求删掉，所以不再需要通栏背景层与径向补光。 */
.feat-band {
  position: relative;
  /* 通栏：宽度锁到视口宽再居中，左右两侧就没有内边距把它夹住 */
  width: 100vw;
  margin-left: calc(50% - 50vw);
  padding: 84px 0 92px;
  overflow: hidden;
  background: linear-gradient(180deg, #ffffff 0%, #f4f8fd 46%, #e9f1fb 100%);
}
.feat-inner {
  position: relative;
  z-index: 1;
  max-width: 1360px;
  margin: 0 auto;
  padding: 0 40px;
}
/* 堆叠卡片竖向居中：这一节的标题也跟着居中（其余区块仍是左对齐） */
.sec-head.center {
  max-width: none;
  text-align: center;
  margin-bottom: 40px;
}
.sec-head.center .blur-heading {
  justify-content: center;
}
/* 卡片放大到 4:3（横 4 纵 3），容器也跟着放宽。 */
.feat-stack {
  max-width: 940px;
  margin: 0 auto;
}
/* 卡片本体由 ScrollStackItem 提供，这里覆盖它的几何与底色：
   4:3 比例、底色取调用方给的 --feat-bg（三张由浅到深的蓝）。 */
.feat-stack :deep(.ss-card) {
  aspect-ratio: 4 / 3;
  min-height: 0;
  padding: clamp(28px, 4vw, 56px);
  background: var(--feat-bg, #ffffff);
  border-color: rgba(37, 99, 235, 0.14);
}
/* 卡片内容：图标 / 标题 / 说明，居中排布。
   min-width: 0 是必需的：flex 子项默认 min-width:auto，长中文段会把卡片撑破。 */
/* 大标题放左、描述放右：两列。窄屏自动堆成上下。 */
.feat-card-inner {
  display: grid;
  grid-template-columns: max-content minmax(0, 1fr);
  align-items: center;
  gap: clamp(16px, 3vw, 40px);
  min-width: 0;
  height: 100%;
  text-align: left;
}
@media (max-width: 720px) {
  .feat-card-inner {
    grid-template-columns: minmax(0, 1fr);
    align-content: center;
    gap: 10px;
  }
}
/* 左列那一组：图标在上、标题在下，整体在列内垂直居中。 */
.feat-lead {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-width: 0;
}

.feat-icon {
  display: block;
  min-width: 0;
  font-size: 26px;
  line-height: 1;
  color: var(--color-accent);
}
/* 标题放大：22px -> clamp(2rem, 3.4vw, 3rem)，跟 4:3 的大卡片相称。 */
.feat-title {
  display: block;
  min-width: 0;
  margin: 0;
  font-size: clamp(2.6rem, 4.6vw, 4.2rem);
  /* 标题不换行：「跨源多跳」四个字在 0.42fr 的列里会被折成两行。 */
  white-space: nowrap;
  font-weight: 800;
  color: var(--color-text);
  letter-spacing: -0.02em;
  line-height: 1.2;
}
/* 描述同步放大：16px -> clamp(1.05rem, 1.5vw, 1.35rem)。 */
.feat-desc {
  display: block;
  min-width: 0;
  margin: 0;
  color: var(--color-body);
  font-size: clamp(1.3rem, 2vw, 1.75rem);
  line-height: 1.8;
  font-weight: 400;
  overflow-wrap: anywhere;
}

/* ── 知识图谱 / 推理卡片 ──
   这一节现在是两张真实截图：画布、落差文字与图例都已删掉，不再有 canvas。
   两个框等宽并排、整体居中；窄屏（≤900）上下堆叠。
   类名统一 showcase- 前缀：Tailwind v4 的工具类里没有这个前缀，不会出现
   「只声明了部分属性、其余被工具类接管」的撞名（本仓库已出过三次）。 */
.showcase {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 24px;
  align-items: start;
}
.showcase-item {
  margin: 0;
  min-width: 0;
}
/* 框：白底 + 细描边 + 很轻的投影 + 圆角，和这一页其它白卡是同一套语言。
   aspect-ratio 取两张截图自身的比例（934×576），图片按 cover 铺满，不拉伸不变形。 */
.showcase-frame {
  border: 1px solid var(--color-line);
  border-radius: 16px;
  background: #ffffff;
  box-shadow: 0 16px 36px rgba(15, 23, 42, 0.07);
  overflow: hidden;
  aspect-ratio: 934 / 576;
}
.showcase-img {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: cover;
  object-position: top center;
}
.showcase-cap {
  margin: 12px 2px 0;
  color: var(--color-muted);
  font-size: 14px;
}
@media (max-width: 900px) {
  .showcase {
    grid-template-columns: minmax(0, 1fr);
    gap: 20px;
  }
}

/* ── 流程（How it works） ──
   用户要求：这块的灰色长条底换成克莱因蓝（var(--color-accent)）；四张卡改成 Vue Bits 的可翻转卡
   （frontend/src/components/FlipCard.vue），正面是占满整个框的蓝色大字小标题，
   背面是同样大字的描述文案。四列并排保持原样，窄屏照旧折行。

   配色选择：底色已经是克莱因蓝，卡片正面若也用蓝就和背景糊成一片，所以
     正面 = 白卡 + 克莱因蓝大字（var(--color-accent) on #ffffff，对比度 10.7:1），
        「纸」是白的、字是这一节的主色，蓝色大字才最跳；
     背面 = 克莱因蓝的浅色调（#e7edff）+ 深藏青字（#0a1f5c，对比度 13.2:1），
        翻过去仍是同一套颜色语言，又和正面的白卡区分得开。
   牌面类名（.flip-*）在 FlipCard 内部是 scoped 的，这里只能用 :deep 覆写。 */
.section.soft.how-band {
  background: var(--color-accent);
}
/* 蓝底上所有字都要能读：eyebrow 与标题原本取深灰/墨色，这里换成白与半透明白。
   选择器带 #how 是为了压过 .eyebrow / .blur-heading 自身的 color（不是猜格式，见上文定义）。 */
#how .eyebrow {
  color: rgba(255, 255, 255, 0.74);
}
#how .blur-heading {
  color: #ffffff;
}
.steps {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 22px;
}
/* FlipCard 的两面都是 position:absolute，高度必须由外面给，否则整卡塌成 0 高度；
   container-type 是给正面标题用的：标题字号按卡片自身宽度（cqw）取，
   四列 / 三列 / 单列的占满比例因此一致，不会换个视口就一行变两行。 */
.steps .how-card {
  height: clamp(228px, 17.5vw, 258px);
  container-type: inline-size;
}
.how-card :deep(.flip-face) {
  padding: 20px 18px;
  border-radius: 20px;
}
.how-card :deep(.flip-front) {
  background: #ffffff;
  border: 1px solid rgba(255, 255, 255, 0.6);
  box-shadow: 0 18px 36px rgba(0, 12, 60, 0.28);
}
.how-card :deep(.flip-back) {
  background: #e7edff;
  border: 1px solid rgba(0, 47, 167, 0.22);
  box-shadow: 0 18px 36px rgba(0, 12, 60, 0.22);
}
/* 正面：只有一个步骤小标题，字号按卡片宽度顶格（16cqw ≈ 五个字占到卡内宽九成）。
   前一行是给不认 cqw 的老浏览器兜底的：新写法解析失败时它还会生效。 */
.how-card-title {
  font-size: clamp(2rem, 3.6vw, 3.4rem);
  font-size: clamp(2rem, 16cqw, 3.9rem);
  font-weight: 800;
  line-height: 1.12;
  letter-spacing: -0.02em;
  color: var(--color-accent);
  text-wrap: balance;
}
/* 背面：小序号 + 一行一句的说明文案。
   用户要求「卡面里不要标点、文字居中、一行一行分开」，所以 s.d 是按原标点断开的
   短语数组，这里逐条 display:block 排成独立的行，序号与正文一起居中。
   四行（01 那张）必须装进 clamp(228px, 17.5vw, 258px) 的卡面：字号比原来收一号、
   行高收到 1.55；.flip-face 是 overflow: hidden，装不下会被直接裁掉，
   所以字号不能只按「看起来够大」定（1440 下四行 134px + 序号 26px，卡内高 212px）。 */
.how-card-back {
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  text-align: center;
}
.how-card-n {
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.14em;
  color: var(--color-accent);
  opacity: 0.72;
}
.how-card-desc {
  margin: 0;
  font-size: clamp(1.02rem, 1.5vw, 1.35rem);
  font-weight: 500;
  line-height: 1.55;
  letter-spacing: 0.005em;
  color: #0a1f5c;
}
/* 一句一行：短语自己绝不折行，否则 01 那张的四行会被挤成五六行 */
.how-card-line {
  display: block;
  white-space: nowrap;
}
/* 窄屏列数变少、卡片变宽，卡面别再那么高，否则字和框差得太远 */
@media (max-width: 900px) {
  .steps .how-card {
    height: 208px;
  }
}
@media (max-width: 560px) {
  .steps .how-card {
    height: 168px;
  }
  /* 单列时卡面变宽、但高度只有 168px：字号收一档，四行 + 序号才装得下 */
  .how-card-desc {
    font-size: 1rem;
    line-height: 1.5;
  }
}
/* 翻面是动画：系统开了「减少动态效果」就直接切面，不走 3D 过渡 */
@media (prefers-reduced-motion: reduce) {
  .how-card :deep(.flip-inner) {
    transition: none;
  }
}

/* ── 底部 CTA ── */
.final {
  padding: 96px 24px 80px;
  text-align: center;
  border-top: 1px solid var(--color-line);
}
.final-inner {
  max-width: 760px;
  margin: 0 auto;
}
.swap-slot {
  border-radius: 18px;
  overflow: hidden;
  border: 1px solid var(--color-line);
  cursor: pointer;
}
/* PixelSwap 的两个插槽层本身就是绝对定位铺满，这里只负责排版 */
.swap-face {
  display: grid;
  place-items: center;
  height: 100%;
  background: var(--color-ink);
}
.swap-face span {
  font-size: clamp(1.5rem, 4.4vw, 3rem);
  font-weight: 800;
  letter-spacing: -0.02em;
  color: var(--color-text);
}
.swap-face.alt {
  background: var(--color-accent);
}
.swap-face.alt span {
  color: #ffffff;
}
.final-note {
  margin: 12px 0 26px;
  color: var(--color-muted);
  font-size: 14px;
}

/* ── 页脚 ── */
.foot {
  border-top: 1px solid var(--color-line);
  padding: 26px 40px;
  display: flex;
  flex-wrap: wrap;
  gap: 8px 24px;
  justify-content: space-between;
  color: var(--color-muted);
  font-size: 14px;
}

@media (max-width: 900px) {
  .flip-wrap {
    grid-template-columns: 1fr;
  }
  /* 窄屏卡片要放得下四条错开菜单，纵向拉高一点（但别拉成一整屏空白） */
  .flipcard {
    height: 368px;
  }
  .face-content {
    padding: 26px 24px 28px;
  }
  .nav {
    padding: 14px 20px;
  }
  .links {
    display: none;
  }
  /* 窄屏下黄金分割列太窄，正文改回整宽 */
  .hero {
    padding: 120px 20px 20px;
  }
  .hero-inner {
    width: 100%;
    max-width: none;
  }
  .section,
  .section.soft {
    padding-left: 20px;
    padding-right: 20px;
  }
  .keyword-strip {
    padding-left: 20px;
    padding-right: 20px;
  }
  /* 堆栈卡片在小屏上不再收窄，跟着内容走 */
  .feat-stack {
    max-width: none;
  }
  .foot {
    padding: 22px 20px;
  }
}
</style>
