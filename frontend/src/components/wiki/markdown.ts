/**
 * 词条正文的**极简 Markdown 渲染器**：只支持 `body_markdown` 真正用到的语法子集。
 *
 * WHY 自己写，而不是装 `marked`（这是本轮被明确问到的取舍）
 * ---------------------------------------------------------
 * 1. **防 XSS 要靠结构，不靠自觉。** 本文件只用一条路径：先把**所有**源字符
 *    `escapeHtml()`，再在转义后的文本上插标签。源文本里的 `<` / `>` / `&` / 引号在
 *    进入任何状态机之前就已经变成实体，因此不存在"某条分支忘了净化"的可能。
 *    `marked` 默认**原样透传 HTML**，要用它就得再叠一个净化库（DOMPurify 之类），
 *    两份配置漏一处就是注入。正文的引文来自 PDF chunk（`excerpt` 是抽取结果），
 *    里面出现 `<img onerror=…>` 是排版意外，不是攻击者的想象。
 * 2. **语法集合是封闭的**：正文由服务端模板生成
 *    （`datamind/wiki_build.py::body_markdown()`），只会出现标题 / 段落 / 无序列表 /
 *    引用 / 行内 code / 表格 / 粗体这七种。为一个封闭集合引一个 40KB 通用解析器，
 *    多出来的是我们不会用、却要跟着升级与审计的代码。
 * 3. **不引依赖就不动 `package.json` / lock**，打包产物不必因为一个渲染器重装。
 *
 * 语法子集（逐条有断言：`tmp/verify_markdown_renderer.mjs`）
 * ---------------------------------------------------------
 * - `#` / `##` / `###` 标题 → `h3` / `h4` / `h5`：卡片标题本身是 `h2`，标题层级
 *   不能进反（页面里再出现一个 `h1` 会把大纲打乱）。
 * - 无序列表 `-` `*`、有序列表 `1.`；列表项下可以缩进嵌套任意块 —— 正文里的表格
 *   就挂在证据列表项下面。
 * - `>` 引用块。本模板把它当"段末小字注释"，所以**每个 `>` 行单独成段**（普通
 *   Markdown 会把连续行软换行成一段；这里按小字注释的排版来）。
 * - `| a | b |` + `|---|---|` 表格 → 真正的 `<table>`（有边框，见 `WikiEntryPage.vue`
 *   的 `.wk-md` 样式）。
 * - 行内 `` `code` `` 与 `**粗体**`。
 * - **其他一律当普通文字**：不认 HTML、不认链接、不认图片。不认识的语法原样显示，
 *   不会变成可执行的东西 —— 这是"最小攻击面"的直接后果，不是漏做的功能。
 */

const ESCAPES: Record<string, string> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
}

/** 源文本 → HTML 文本节点。**所有**源字符都必须先过这里，这是防注入的唯一入口。 */
export function escapeHtml(value: string): string {
  return String(value).replace(/[&<>"']/g, (char) => ESCAPES[char])
}

/**
 * 行内标记：在**已转义**的文本上加标签。
 *
 * 行内 code 先按反引号切段再处理（而不是先用正则替换）：这样 `` `a**b**` `` 里的
 * `**` 不会被当成粗体标记 —— 标签只能由本函数插入，源文本进不去。
 */
function inline(text: string): string {
  return String(text)
    .split('`')
    .map((part, index) => {
      const escaped = escapeHtml(part)
      if (index % 2 === 1) return `<code>${escaped}</code>`
      return escaped
        .replace(/\*\*([^*]+)\*\*/g, (_all, bold: string) => `<strong>${bold}</strong>`)
        .replace(/\[(\d{1,3})\]/g, (_all, number: string) => `<span class="wkm-marker">[${number}]</span>`)
    })
    .join('')
}

const BLANK = /^\s*$/
const HEADING = /^\s{0,3}(#{1,6})\s+(.*)$/
const UL_ITEM = /^([-*+])\s+(.*)$/
const OL_ITEM = /^(\d+)[.)]\s+(.*)$/
const QUOTE = /^\s{0,3}>\s?(.*)$/
/** GitHub 表格的分隔行：`|---|---|` / `|:--|--:|`。 */
const TABLE_SEPARATOR = /^\|?[\s:|-]+\|?$/
/** 列表项的缩进续行（正文里表格就是这样挂在证据项下面的）。 */
const INDENTED = /^\s+/

function isTableRow(line: string): boolean {
  return line.trim().startsWith('|')
}

function splitCells(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map((cell) => cell.trim())
}

function isTableStart(lines: string[], index: number): boolean {
  if (!isTableRow(lines[index]) || index + 1 >= lines.length) return false
  const separator = lines[index + 1].trim()
  return isTableRow(separator) && separator.includes('-') && TABLE_SEPARATOR.test(separator)
}

function startsBlock(line: string): boolean {
  return HEADING.test(line) || UL_ITEM.test(line) || OL_ITEM.test(line) || QUOTE.test(line) || isTableRow(line)
}

function renderTable(lines: string[], start: number): { html: string; next: number } {
  const head = splitCells(lines[start])
  const rows: string[][] = []
  let index = start + 2
  while (index < lines.length && isTableRow(lines[index])) {
    rows.push(splitCells(lines[index]))
    index += 1
  }
  const headHtml = `<thead><tr>${head.map((cell) => `<th>${inline(cell)}</th>`).join('')}</tr></thead>`
  const bodyHtml = rows.length
    ? `<tbody>${rows
        .map((row) => `<tr>${row.map((cell) => `<td>${inline(cell)}</td>`).join('')}</tr>`)
        .join('')}</tbody>`
    : ''
  return { html: `<table>${headHtml}${bodyHtml}</table>`, next: index }
}

function renderList(lines: string[], start: number): { html: string; next: number } {
  const ordered = OL_ITEM.test(lines[start])
  const itemPattern = ordered ? OL_ITEM : UL_ITEM
  const items: string[] = []
  let index = start
  while (index < lines.length) {
    const item = itemPattern.exec(lines[index])
    if (!item) break
    const children: string[] = []
    index += 1
    while (index < lines.length) {
      if (BLANK.test(lines[index])) {
        // 空行只在后面还有属于这一项的缩进行时才保留（表格前的那个空行就是这种）。
        let look = index
        while (look < lines.length && BLANK.test(lines[look])) look += 1
        if (look < lines.length && INDENTED.test(lines[look])) {
          children.push('')
          index = look
          continue
        }
        break
      }
      if (INDENTED.test(lines[index])) {
        children.push(lines[index].replace(/^\s{1,4}/, ''))
        index += 1
        continue
      }
      break
    }
    const inner = children.length ? renderBlocks(children) : ''
    items.push(`<li>${inline(item[2])}${inner ? `<div class="wkm-li-child">${inner}</div>` : ''}</li>`)
  }
  const tag = ordered ? 'ol' : 'ul'
  return { html: `<${tag}>${items.join('')}</${tag}>`, next: index }
}

function renderBlocks(lines: string[]): string {
  const out: string[] = []
  let index = 0
  while (index < lines.length) {
    const line = lines[index]
    if (BLANK.test(line)) {
      index += 1
      continue
    }
    const heading = HEADING.exec(line)
    if (heading) {
      // 卡片标题是 h2：正文的 `#` 从 h3 起步，标题层级只往下走。
      const level = Math.min(heading[1].length + 2, 5)
      out.push(`<h${level}>${inline(heading[2])}</h${level}>`)
      index += 1
      continue
    }
    if (isTableStart(lines, index)) {
      const table = renderTable(lines, index)
      out.push(table.html)
      index = table.next
      continue
    }
    if (QUOTE.test(line)) {
      const quoted: string[] = []
      while (index < lines.length) {
        const match = QUOTE.exec(lines[index])
        if (!match) break
        if (match[1].trim()) quoted.push(`<p>${inline(match[1])}</p>`)
        index += 1
      }
      if (quoted.length) out.push(`<blockquote>${quoted.join('')}</blockquote>`)
      continue
    }
    if (UL_ITEM.test(line) || OL_ITEM.test(line)) {
      const list = renderList(lines, index)
      out.push(list.html)
      index = list.next > index ? list.next : index + 1
      continue
    }
    const paragraph: string[] = []
    while (index < lines.length && !BLANK.test(lines[index]) && (paragraph.length === 0 || !startsBlock(lines[index]))) {
      paragraph.push(lines[index].trim())
      index += 1
    }
    out.push(`<p>${paragraph.map(inline).join(' ')}</p>`)
  }
  return out.join('')
}

/**
 * Markdown 文本 → 安全 HTML。
 *
 * 返回值只含本文件写出的标签，源文本里的每个 `<` / `>` / `&` 都已经是实体，
 * 因此可以交给 `v-html`（`WikiEntryPage.vue` 的 `bodyHtml`）。
 */
export function renderMarkdown(source?: string | null): string {
  const text = String(source ?? '').replace(/\r\n?/g, '\n')
  if (!text.trim()) return ''
  return renderBlocks(text.split('\n'))
}
