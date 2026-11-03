"""Vue 前端的契约回归：六个 legacy 视图迁移后的「诚实性 / 安全性」护栏。

本仓库没有 JavaScript 测试运行器，也没有真实浏览器。这里沿用 commit 7fc0035 一并删除的
旧契约测试（``test_upload_ui.py`` / ``test_upload_selector_ui.py``，
以及 ``test_frontend_locators.py`` 里针对 ``web/app.js`` 的那一半）的做法：
把前端源码当**文本**读，钉住那些一旦丢失就会导致「过度声称」「白屏」或「画错证据位置」
的规则。这些断言不启动 DOM，也不声称验证了运行时的真实渲染结果——它们只证明规则
**写在源码里**。

诚实性是本仓库的功能，不是文案修饰：一个把值渲染出来却不带保留条件的屏幕是失败，不是成功。
所以最后一段钉的就是那些「必须活下来」的中文文案。

反向验证（证明断言不是空转）：凡是「不得出现 X」的规则，都会先用一次**变异**
（把规则从文本里抹掉/塞进违规内容）再跑同一个判据，并断言判据在变异文本上**必须变红**。
只在当前源码上恰好为真的断言，比没有断言更糟。

被钉住的规则：

* 旋转页永远不得叠画在未旋转的页面图上（``locator.ts``；之前只有散文在守）；
* 六个 legacy 路由真实存在，且兜底路由仍在最后（否则旧深链会白屏）；
* 侧栏不再指向退役的旧版控制台深链（``/#…``）；
* 上传走 raw bytes（``POST /v1/uploads`` + ``application/octet-stream``），
  不得回落到 multipart ``FormData`` 或退役的 ``/v1/files/uploads``；
* ``key_masked`` 只作为 placeholder/hint 出现，绝不绑进输入框的 value，
  ``__clear__`` 绝不当成 ``api_key`` 的哨兵；
* 六条诚实文案必须仍然存在于对应屏幕的源码里。
"""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / 'frontend' / 'src'

LOCATOR_TS = FRONTEND / 'components' / 'fields' / 'locator.ts'
ROUTER_TS = FRONTEND / 'router' / 'index.ts'
SIDE_NAV = FRONTEND / 'components' / 'SideNav.vue'
CLIENT_TS = FRONTEND / 'api' / 'client.ts'
MODEL_PAGE = FRONTEND / 'views' / 'ModelPage.vue'
PROFILE_EDITOR = FRONTEND / 'components' / 'model' / 'ProfileEditor.vue'
FILES_PAGE = FRONTEND / 'views' / 'FilesPage.vue'
CHAT_PANEL = FRONTEND / 'components' / 'chat' / 'ChatPanel.vue'
AUTHORITY_PAGE = FRONTEND / 'views' / 'AuthorityPage.vue'
FIELDS_PAGE = FRONTEND / 'views' / 'FieldsPage.vue'
MAIN_PAGE = FRONTEND / 'views' / 'MainPage.vue'
SESSIONS_COMPOSABLE = FRONTEND / 'composables' / 'useSessions.ts'
LANDING_PAGE = FRONTEND / 'views' / 'LandingPage.vue'
RUNS_PAGE = FRONTEND / 'views' / 'RunsPage.vue'


# ══════════════════════════════════════════════════════════════════════════
# 文本工具
# ══════════════════════════════════════════════════════════════════════════

def read(path):
    return path.read_text(encoding='utf-8')


def strip_comments(source):
    """把 JS/TS 的 ``//``、``/* */`` 与 Vue 模板的 ``<!-- -->`` 注释抹成等长空白。

    WHY 要看「代码」而不是「原文」：退役契约的名字与「不许这样做」的说明会**故意**写进注释，
    例如 ``client.ts`` 的「本文件不提供 /v1/files/uploads」、``ModelPage.vue`` 的
    「__clear__ 只用于敏感头与代理」。若拿字面量直接断言「不得出现」，我们只会逼着后来者
    删掉解释——规则还在，说明没了。所以红线只对**真正的代码**判定。

    实现上保持**等长**并逐字符保留换行，这样按行断言的下标与原文完全一致。
    字符串字面量原样保留（``//`` 出现在 URL 里不会被误判成注释）。
    """
    out = []
    i = 0
    n = len(source)
    state = None
    while i < n:
        ch = source[i]
        if state is None:
            if source.startswith('//', i):
                out.append('  ')
                state = '//'
                i += 2
                continue
            if source.startswith('/*', i):
                out.append('  ')
                state = '/*'
                i += 2
                continue
            if source.startswith('<!--', i):
                out.append('    ')
                state = '<!--'
                i += 4
                continue
            if ch in ('"', "'", '`'):
                state = ch
            out.append(ch)
            i += 1
            continue
        if state == '//':
            out.append('\n' if ch == '\n' else ' ')
            if ch == '\n':
                state = None
            i += 1
            continue
        if state == '/*':
            if source.startswith('*/', i):
                out.append('  ')
                state = None
                i += 2
                continue
            out.append('\n' if ch == '\n' else ' ')
            i += 1
            continue
        if state == '<!--':
            if source.startswith('-->', i):
                out.append('   ')
                state = None
                i += 3
                continue
            out.append('\n' if ch == '\n' else ' ')
            i += 1
            continue
        # 字符串字面量内部：原样保留；处理转义，避免 \' 提前闭合。
        if ch == '\\':
            out.append(source[i:i + 2])
            i += 2
            continue
        out.append(ch)
        if ch == state:
            state = None
        i += 1
    return ''.join(out)


RELATIVE_IMPORT_RE = re.compile(r"""from\s+['"](\.{1,2}/[^'"]+)['"]""")


def module_surface(entry):
    """入口 ``.vue`` 及其（递归）相对导入的本地模块 → ``{路径: 源码}``。

    WHY 不只看单个文件：诚实文案往往沉在底层的纯函数模块里（例如 ``uploadHonesty.ts``），
    页面只负责渲染。要钉的规则是「这块屏幕上必须出现」，所以断言对象必须是
    **这个屏幕真正会加载的源码集合**，否则会把「文案搬进组件」误判成「文案丢了」。
    """
    surface = {}
    pending = [entry]
    while pending:
        path = pending.pop().resolve()
        if path in surface or not path.is_file():
            continue
        if path.suffix not in ('.vue', '.ts'):
            continue
        text = read(path)
        surface[path] = text
        for raw in RELATIVE_IMPORT_RE.findall(text):
            base = path.parent / raw
            for candidate in (base, base.with_suffix('.ts'), base.with_suffix('.vue'),
                              base.parent / (base.name + '.ts'),
                              base.parent / (base.name + '.vue')):
                if candidate.is_file():
                    pending.append(candidate)
                    break
    return surface


def files_view_modules(entry):
    """FilesPage 依赖图里**本屏自己的视图层**模块（``components/files/``）。

    WHY 要收窄：共享的 ``api/client.ts`` 也会在注释里同时出现「审批」「可检索」，
    把扫描面放到整张依赖图上，文案真被删掉时断言照样变绿——那是空转。
    """
    return {path: text for path, text in module_surface(entry).items()
            if path.parent.name == 'files'}


def function_body(source, name):
    """用花括号配平取出 ``function name(...) {...}``（含 async/export 形式）的函数体。"""
    match = re.search(r'(?:export\s+)?(?:async\s+)?function\s+' + re.escape(name) + r'\s*\(', source)
    if not match:
        return ''
    start = source.index('{', match.end() - 1)
    depth = 0
    for position in range(start, len(source)):
        character = source[position]
        if character == '{':
            depth += 1
        elif character == '}':
            depth -= 1
            if depth == 0:
                return source[start:position + 1]
    return source[start:]


# ══════════════════════════════════════════════════════════════════════════
# 1) 定位框：旋转页不得叠画在未旋转的页面图上
# ══════════════════════════════════════════════════════════════════════════

ROTATION_FIELD = 'rotation_degrees'

# 护栏不是「声明了 rotation_degrees」，而是「把它读进一个拒绝判定」。
# 只在 interface 里列一个字段却从不判定，等于没有护栏——那正是「猜一个位置」。
ROTATION_GUARD_RE = re.compile(
    r'(?:!\s*[\w.\[\]?()]*' + re.escape(ROTATION_FIELD) + r')'  # !ref.rotation_degrees
    + r'|\b(?:if|while|return)\b[^\n;]*' + re.escape(ROTATION_FIELD)  # if (...rotation_degrees...)
    + r'|' + re.escape(ROTATION_FIELD) + r'\s*\)?\s*(?:===|!==|==|!=|>|<)'  # 直接比较
)


def rotation_guard_present(source):
    """源文本里是否存在「非 0 旋转就拒绝绘制」的护栏。"""
    if ROTATION_FIELD not in source:
        return False
    return bool(ROTATION_GUARD_RE.search(source))


class LocatorDrawabilityTests(unittest.TestCase):
    """证据定位框的判定：坐标系必须是 pdf_points，旋转页必须拒绝。"""

    def setUp(self):
        self.source = read(LOCATOR_TS)

    def test_the_helper_declares_the_pdf_point_coordinate_contract(self):
        # 四个名字缺一不可：没有 rotation_degrees 就无法判断页面图能不能叠；
        # 没有 page_size/bbox 就无法用 viewBox 直接铺（只能靠缩放推断 = 猜）；
        # 没有 pdf_points 就无法证明 bbox 与页面图同坐标系。
        # 用 assertTrue + 短消息：assertIn 失败时会把整份源码打进报告，淹没真正的结论。
        for needle in ('rotation_degrees', 'page_size', 'bbox', 'pdf_points'):
            self.assertTrue(needle in self.source, 'locator.ts 缺少 %s' % needle)
        # 判定必须是导出的**纯函数**谓词，视图层只允许「画」或「出标签」两种结果。
        self.assertRegex(self.source, r'export\s+function\s+locatorVerdict\b')
        self.assertIn('drawable', self.source)
        self.assertIn('无法绘制', self.source)

    def test_a_rotated_page_is_never_drawn_over_the_un_rotated_page_image(self):
        # WHY 这是本文件最重要的一条：本客户端取到的页面图是**未旋转**的。
        # 若后端声明 rotation_degrees != 0 而前端照画，bbox 会落在错误的位置上，
        # 用户会把一个猜出来的区域当成证据来源。宁可不画，也不能画错。
        self.assertTrue(
            rotation_guard_present(self.source),
            'locator.ts 缺少旋转护栏：rotation_degrees 未参与拒绝判定，'
            '旋转页会被叠画在未旋转的页面图上（应返回 drawable:false 并给出中文原因）')

    def test_the_rotation_guard_predicate_is_not_vacuous(self):
        # 先证明判据能认出真正的护栏，再证明抽掉令牌后它会变红。
        # 这两步都不依赖当前源码是否有护栏，所以即使上面那条红了，这条仍能证明断言不是空转。
        guarded = (self.source
                   + "\n  if (ref.rotation_degrees !== 0) "
                     "return { drawable: false, reason: '页面被旋转：无法对齐未旋转的页面图' }\n")
        self.assertTrue(rotation_guard_present(guarded), '判据必须能识别真正的旋转护栏')
        self.assertFalse(
            rotation_guard_present(guarded.replace('rotation_degrees', 'rotation_deg')),
            '抽掉 rotation_degrees 令牌后判据必须变红，否则断言是空转')


# ══════════════════════════════════════════════════════════════════════════
# 2) 六条路由真实存在，兜底仍在最后
# ══════════════════════════════════════════════════════════════════════════

SIX_VIEW_PATHS = ('/model', '/fields', '/authority', '/cad', '/dimensions', '/files')
CATCH_ALL_PATH = '/:pathMatch(.*)*'
ROUTE_ENTRY_RE = re.compile(r"\{\s*path:\s*'([^']+)'")


class RouterMigrationTests(unittest.TestCase):
    """六个 legacy 视图必须是真正的站内路由，不能只留一条兜底。"""

    def setUp(self):
        self.source = read(ROUTER_TS)
        self.paths = ROUTE_ENTRY_RE.findall(strip_comments(self.source))

    def test_all_six_legacy_views_have_real_routes(self):
        for path in SIX_VIEW_PATHS:
            self.assertIn(path, self.paths, path)
        # 每个路由都要指向一个真实存在的视图文件，否则点进去仍是白屏。
        views = {p.stem for p in (FRONTEND / 'views').glob('*.vue')}
        for name in ('ModelPage', 'FieldsPage', 'AuthorityPage', 'CadPage', 'DimensionsPage',
                     'FilesPage'):
            self.assertIn(name, views, name)

    def test_the_catch_all_route_stays_last(self):
        # WHY：旧版深链（/#model 等）会被 hash 模式解析成 /model；路由表顺序错了，
        # 兜底页会把它们全部吃掉，用户看到的就是「未迁移」而不是真正的视图。
        self.assertEqual(self.paths[-1], CATCH_ALL_PATH,
                         '兜底路由必须留在最后，当前顺序：%r' % (self.paths,))
        for path in SIX_VIEW_PATHS:
            self.assertLess(self.paths.index(path), len(self.paths) - 1, path)


# ══════════════════════════════════════════════════════════════════════════
# 3) 侧栏：不再有指向退役旧版控制台的深链
# ══════════════════════════════════════════════════════════════════════════

#: 侧栏里**直接指向站内路由**的入口。
#:
#: 这里原先列的是 6 条：数据治理的 4 屏（/fields、/authority、/cad、/dimensions）各占一行。
#: 现在它们合并成 1 条 `/governance`，四个板块改为该页**页内分栏**（见 GovernancePageTests）。
#: 注意：四条旧路由本身仍然保留在路由表里，那属于 RouterMigrationTests 的断言，不在这里删。
NAV_ROUTE_ITEMS = (
    ('model', '/model', '模型接入'),
    ('governance', '/governance', '数据治理'),
    ('files', '/files', '我的文件'),
    # Wiki 知识图谱：`/#/wiki` 是关系图入口，`/#/wiki/:entryId` 是词条深链。
    # 它同样是「直接指向站内路由的入口」，所以并入同一组断言：条目必须带 to:、
    # 不得带 href（带 href 会回落到旧版控制台的深链 = 白屏）、也不得挂「未迁移」标签。
    ('wiki', '/wiki', '知识图谱'),
)

#: 合并之后，这四个 key **不得**再作为独立导航条目出现在侧栏里。
MERGED_GOVERNANCE_KEYS = ('fields', 'authority', 'cad', 'dimensions')

GOVERNANCE_PAGE = FRONTEND / 'views' / 'GovernancePage.vue'

#: `/governance` 页内的四个分栏：(`?tab` 取值, 分栏标题, 被复用的页面组件)
GOVERNANCE_TABS = (
    ('fields', '字段复核', 'FieldsPage'),
    ('authority', '来源权威规则', 'AuthorityPage'),
    ('cad', '图纸与实体', 'CadPage'),
    ('dimensions', '尺寸核验', 'DimensionsPage'),
)


class SideNavMigrationTests(unittest.TestCase):
    """侧栏各入口全部走站内路由，旧 ``/#…`` 深链与「未迁移」标签不得再对它们生效。

    数据治理那 4 条已合并成 1 条（分栏在 GovernancePageTests 里钉）；这里钉的是
    「导航条目本身必须是站内路由」，以及合并之后那 4 个条目真的不再各占一行。
    """

    def setUp(self):
        self.source = read(SIDE_NAV)
        self.code = strip_comments(self.source)

    def test_the_nav_entries_are_router_targets_not_legacy_hrefs(self):
        for key, path, label in NAV_ROUTE_ITEMS:
            items = re.findall(r"\{[^{}]*key:\s*'%s'[^{}]*\}" % re.escape(key), self.code)
            self.assertTrue(items, '侧栏缺少 %s 入口（key=%s）' % (label, key))
            item = items[0]
            self.assertIn("to: '%s'" % path, item, item)
            self.assertIn(label, item, item)
            # 只要还带 href，就会回落到旧版控制台的深链。
            self.assertNotIn('href', item, item)

    def test_the_four_governance_entries_are_merged_into_one_nav_item(self):
        # 合并的反向判据：这 4 条若还在侧栏里各占一行，导航就又堆回 4 行（本次改动的前提）。
        for key in MERGED_GOVERNANCE_KEYS:
            with self.subTest(key=key):
                items = re.findall(r"\{[^{}]*key:\s*'%s'[^{}]*\}" % re.escape(key), self.code)
                self.assertEqual(items, [],
                                 '侧栏仍有独立的 %s 条目：它应与「数据治理」合并成一个入口' % key)
        # 合并的正面判据：那 4 条现在只剩 1 个入口，且指向 /governance。
        governance = re.findall(r"\{[^{}]*key:\s*'governance'[^{}]*\}", self.code)
        self.assertEqual(len(governance), 1, '「数据治理」入口应恰好有 1 条：%r' % (governance,))
        self.assertIn("to: '/governance'", governance[0], governance[0])

    def test_there_is_no_data_governance_group_container_left(self):
        """侧栏里不得再有「数据治理」**分组标题**：条目已经是平级的，标题只会与它同名重复。

        只认 GROUPS 里的 `title:` 字段（NavItem 用的是 `label`，模板用的是 `title=`），
        所以这条不会误伤条目本身或历史区。
        """
        titles = re.findall(r"title:\s*'([^']*)'", self.code)
        # 先证明这个正则真的取到了分组标题（否则「不含数据治理」是空转）。
        self.assertIn('工作台', titles, titles)
        self.assertNotIn('数据治理', titles, titles)

    def test_the_un_migrated_badge_cannot_reach_the_route_entries(self):
        # 「未迁移」只能挂在兜底分支上；上面已断言这些入口都有 to:，
        # 模板里 `v-if="it.to"` 优先，因此它们永远走不到带「未迁移」的 v-else。
        self.assertRegex(self.code, r'v-if="it\.to"')
        for key, _path, _label in NAV_ROUTE_ITEMS:
            items = re.findall(r"\{[^{}]*key:\s*'%s'[^{}]*\}" % re.escape(key), self.code)
            self.assertNotIn('未迁移', items[0], items[0])

    def test_no_legacy_hash_href_remains_in_the_side_nav(self):
        # 规格：侧栏不得再出现以 /# 开头的 legacy href（含页脚用户区）。
        hits = [(number, line.strip())
                for number, line in enumerate(strip_comments(self.source).splitlines(), 1)
                if re.search(r'href\s*[=:]\s*["\']?/#', line)]
        self.assertEqual(hits, [],
                         '仍有指向退役旧版控制台的 /# 深链（行号, 内容）：%r' % (hits,))


class GovernancePageTests(unittest.TestCase):
    """合并后的 ``/governance``：四个板块必须是**页内分栏**，不是从界面上消失。

    只钉「侧栏有 1 个数据治理入口」是不够的 —— 入口在、点进去却少了三个板块时，
    用户丢掉的正是那三屏的能力。所以这里同时钉住「四个分栏各自可在地址栏寻址」与
    「复用的是原来那四个页面组件（没有另写一份）」。
    """

    def setUp(self):
        self.source = read(GOVERNANCE_PAGE)
        self.code = strip_comments(self.source)

    def test_the_page_reuses_the_four_existing_view_components(self):
        for key, label, component in GOVERNANCE_TABS:
            with self.subTest(tab=key):
                # 分栏条目必须**自己**指向原来那个页面组件。只断言「组件名在本文件里出现过」
                # 是不够的：把 cad 换成 FieldsPage 时 import 行仍带着 CadPage.vue，那种
                # 张冠李戴查不出来（实测变异为绿）。
                entry = re.search(r"\{\s*key:\s*'%s'[^}]*\}" % re.escape(key), self.code)
                self.assertIsNotNone(entry, '缺少 %s 分栏（?tab=%s）' % (label, key))
                self.assertIn('component: %s' % component, entry.group(0), entry.group(0))
                self.assertIn(label, entry.group(0), entry.group(0))
                # 组件确实来自 views/ 下那个文件（不是同名的别的东西）。
                self.assertIn('%s.vue' % component, self.code, component)

    def test_the_governance_route_really_exists(self):
        # 侧栏入口指向 /governance；路由表里没有它，点进去就会掉进兜底页（「尚未迁移」）。
        router = strip_comments(read(ROUTER_TS))
        entry = re.search(r"\{\s*path:\s*'/governance'[^}]*\}", router)
        self.assertIsNotNone(entry, '缺少 /governance 路由：侧栏入口会落到兜底页')
        self.assertIn('GovernancePage.vue', entry.group(0), entry.group(0))

    def test_the_active_section_comes_from_the_query_so_a_link_can_share_it(self):
        # 初始分栏必须**取自** query：写死成默认值也能让 'route.query.tab' 出现在 watch 里，
        # 那种写法在刷新页面或打开分享链接时会回到默认板块（地址栏与内容不一致）。
        self.assertRegex(self.code, r"ref\(\s*normalizeTab\(\s*route\.query\.tab\s*\)\s*\)")
        # 地址栏变化（手输、前进/后退）时也要跟着走。
        self.assertRegex(self.code, r"watch\(\s*\(\)\s*=>\s*route\.query\.tab")
        self.assertIn("query: { tab:", self.code)
        # 缺失/非法的 tab 必须有明确定义的回落，并把地址栏改写成实际显示的那个分栏。
        self.assertIn('router.replace', self.code)
        self.assertRegex(self.code, r"path:\s*'/governance'")

    def test_the_old_deep_links_still_reach_their_own_pages(self):
        """四条旧路由保留可访问性：仍直接指向原页面，或显式 redirect 到 /governance。

        WHY 单列一条：合并入口最容易的「顺手清理」就是把旧路由删掉，而它们被
        docs/FRONTEND_VIEWS.md、README 与用户书签引用着（RouterMigrationTests 另有一组）。
        """
        router = strip_comments(read(ROUTER_TS))
        for key, _label, component in GOVERNANCE_TABS:
            with self.subTest(path='/' + key):
                entry = re.search(r"\{\s*path:\s*'/%s'[^}]*\}" % re.escape(key), router)
                self.assertIsNotNone(entry, '旧路由 /%s 被删掉了：深链会白屏' % key)
                self.assertRegex(entry.group(0),
                                 r"(redirect|%s\.vue)" % re.escape(component),
                                 '/%s 既没指向 %s，也没有 redirect' % (key, component))


# ══════════════════════════════════════════════════════════════════════════
# 4) 上传：raw bytes，不是 multipart，也不是退役的 file_jobs
# ══════════════════════════════════════════════════════════════════════════

FORBIDDEN_UPLOAD_TOKENS = ('FormData', '/v1/files/uploads', '/v1/files/jobs')


def upload_code_violations(source):
    """返回在**代码**（注释已抹除）里仍然出现的退役/错误上传令牌。"""
    code = strip_comments(source)
    return [token for token in FORBIDDEN_UPLOAD_TOKENS if token in code]


class RawBytesUploadTests(unittest.TestCase):
    """唯一上传入口是 raw bytes；多部分表单与退役接口都必须是死路。"""

    def setUp(self):
        self.source = read(CLIENT_TS)
        self.body = function_body(self.source, 'uploadFile')

    def test_upload_posts_raw_bytes_with_octet_stream(self):
        self.assertTrue(self.body, 'client.ts 必须导出 uploadFile')
        self.assertIn('application/octet-stream', self.body)
        self.assertIn("method: 'POST'", self.body)
        # File 本体作为 body——不是包装成表单字段。
        self.assertIn('body: file', self.body)
        self.assertRegex(self.body, r'`/v1/uploads\?')
        self.assertIn('URLSearchParams', self.body)
        self.assertIn("'X-CSRF-Token'", self.body)

    def test_no_multipart_form_data_and_no_retired_file_jobs_endpoint(self):
        # 字面量层面：FormData 连注释里都不该出现。
        self.assertNotIn('FormData', self.source)
        # 代码层面：退役契约只允许出现在解释它退役的注释里（见 strip_comments 的 WHY）。
        self.assertEqual(upload_code_violations(self.source), [],
                         '上传路径退回了 multipart / 退役接口')

    def test_the_forbidden_token_scan_is_not_vacuous(self):
        self.assertEqual(upload_code_violations(self.source), [])
        dirty = self.source + ("\nconst fd = new FormData()\n"
                               "fetch('/v1/files/uploads?filename=x', { body: fd })\n"
                               "fetch('/v1/files/jobs', { body: fd })\n")
        self.assertEqual(upload_code_violations(dirty), list(FORBIDDEN_UPLOAD_TOKENS))
        # 注释里的说明不算违规：抹除后必须扫不到。
        self.assertEqual(upload_code_violations('// 本文件不提供 /v1/files/uploads\n'), [])


# ══════════════════════════════════════════════════════════════════════════
# 5) 密钥纪律：掩码只进 placeholder，__clear__ 绝不是 api_key 的哨兵
# ══════════════════════════════════════════════════════════════════════════

RE_BIND_VALUE_KEY_MASKED = re.compile(
    r'''(?:v-model|:value|v-bind:value|value)\s*=\s*"[^"]*(?:key_masked|keyMasked|loadedKeyMasked)''')


def mask_only_reaches_the_placeholder(source):
    """掩码是否**只**流向 placeholder：不绑进 value，且 placeholder 由掩码推导。"""
    code = strip_comments(source)
    if 'key_masked' not in code:
        return False
    if RE_BIND_VALUE_KEY_MASKED.search(code):
        return False
    # 掩码变量（loadedKeyMasked，来自 profile.key_masked）被喂给 placeholder 计算属性；
    # 两者之间隔着 computed(() =>\n  loadedKeyMasked.value，所以不能按单行匹配。
    return bool(re.search(r'apiKeyPlaceholder[\s\S]{0,200}?loadedKeyMasked', code))


class ModelSecretBindingTests(unittest.TestCase):
    """模型接入页的密钥纪律：回填掩码会永久毁掉用户真实密钥。"""

    def setUp(self):
        self.page_source = read(MODEL_PAGE)
        self.page_code = strip_comments(self.page_source)
        self.editor_source = read(PROFILE_EDITOR)
        self.editor_code = strip_comments(self.editor_source)

    def test_key_masked_is_never_bound_into_an_input_value(self):
        for name, source in (('ModelPage.vue', self.page_source),
                             ('ProfileEditor.vue', self.editor_source)):
            code = strip_comments(source)
            self.assertIsNone(RE_BIND_VALUE_KEY_MASKED.search(code),
                              '%s 把密钥掩码绑进了输入框的 value' % name)
            for number, line in enumerate(code.splitlines(), 1):
                # 掩码既可能叫 key_masked（服务端字段），也可能叫 loadedKeyMasked（本组件的 ref）。
                if 'key_masked' in line or 'KeyMasked' in line:
                    self.assertNotIn('v-model', line, '%s:%d' % (name, number))

    def test_key_masked_only_flows_into_the_placeholder(self):
        # 规则 (a)：输入框的值永远来自用户输入（apiKey），掩码只喂给 placeholder 计算属性。
        self.assertRegex(self.editor_code, r'v-model="apiKey"')
        self.assertRegex(self.editor_code, r':placeholder="apiKeyPlaceholder"')
        self.assertRegex(self.editor_code, r'loadedKeyMasked\.value\s*=[^\n]*key_masked')
        self.assertTrue(mask_only_reaches_the_placeholder(self.editor_source),
                        'placeholder 必须由掩码推导，且掩码不得绑进任何 value；'
                        '否则掩码会被回填并提交，把真实密钥覆盖成掩码字符串')
        self.assertRegex(self.editor_code, r"apiKey\.value\s*=\s*''",
                         '读取配置时必须先把输入框清空，不能从掩码回填')

    def test_the_key_masked_placeholder_check_is_not_vacuous(self):
        # 用一个自足的正例证明判据可能为真，再把「掩码只喂 placeholder」改成
        # 「掩码直接绑进输入框」，判据必须变红。不依赖当前源码是否有该规则。
        good = ("const loadedKeyMasked = ref('')\n"
                "function load(profile) { loadedKeyMasked.value = profile.key_masked || '' }\n"
                'const apiKeyPlaceholder = computed(() =>\n'
                '  loadedKeyMasked.value ? `已保存 ${loadedKeyMasked.value}` : \'\',\n'
                ')\n'
                '<input v-model="apiKey" :placeholder="apiKeyPlaceholder" />\n')
        self.assertTrue(mask_only_reaches_the_placeholder(good))
        mutated = good.replace('v-model="apiKey"', 'v-model="loadedKeyMasked"')
        self.assertFalse(mask_only_reaches_the_placeholder(mutated),
                         '掩码被绑进输入框后判据必须变红，否则断言是空转')

    def test_the_clear_sentinel_is_never_submitted_as_an_api_key(self):
        # __clear__ 只对敏感请求头与代理有效；把它当 api_key 提交，
        # 服务端会把字面量当成新密钥存下来。
        self.assertNotRegex(self.editor_code,
                            r'''api_key\s*=\s*(?:CLEAR_SENTINEL|['"]__clear__)''')
        self.assertNotRegex(self.editor_code, r'payload\.api_key\s*=[^\n]*__clear__')
        self.assertRegex(self.editor_code, r'payload\.api_key\s*=\s*key\b')
        # 哨兵只允许用在 header/proxy 上。
        self.assertRegex(self.editor_code, r'headers\[row\.name\]\s*=\s*CLEAR_SENTINEL')
        self.assertRegex(self.editor_code, r'payload\.proxy\s*=\s*CLEAR_SENTINEL')

    def test_clear_sentinel_never_shares_a_code_line_with_api_key(self):
        # 规格：__clear__ 不得与 api_key 出现在同一行。注释里解释「为什么不能用」
        # 是必须保留的说明（ModelPage.vue 的块注释、messages.ts 的告警文案），
        # 所以这里只对**代码**逐行判定；渲染文案里的同现是解释，不是提交。
        for name, code in (('ModelPage.vue', self.page_code),
                           ('ProfileEditor.vue', self.editor_code)):
            offenders = [(number, line.strip())
                         for number, line in enumerate(code.splitlines(), 1)
                         if '__clear__' in line and 'api_key' in line]
            self.assertEqual(offenders, [], '%s 里 __clear__ 与 api_key 同处一行：%r'
                             % (name, offenders))


# ══════════════════════════════════════════════════════════════════════════
# 6) 诚实文案：这些句子就是功能本身
# ══════════════════════════════════════════════════════════════════════════

class HonestyCopyTests(unittest.TestCase):
    """六条必须活下来的中文断言：删掉它们，屏幕就开始过度声称。"""

    def test_files_page_keeps_parse_review_and_searchability_apart(self):
        # 解析完成 ≠ 人工复核 ≠ 可检索。这三件事混成一个「已完成」，用户就会
        # 以为刚上传的文件已经能拿来问答。
        source = read(FILES_PAGE)
        self.assertIn('可检索', source)
        self.assertIn('后台处理器未运行', source)

    def test_the_files_screen_still_says_approval_is_not_searchability(self):
        # 「审批 ≠ 可检索」是本屏的核心。文案落在 FilesPage 真正加载的**视图层**模块里
        # （当前是 components/files/uploadHonesty.ts），而不是必须写死在页面文件本身。
        # WHY 只认 components/files/：共享的 api/client.ts 也会在两个词的注释里同现，
        # 若把扫描面放宽到整张依赖图，这条断言会在文案被删掉后仍然变绿（空转）。
        self.assertRegex(read(FILES_PAGE), r'components/files/uploadHonesty')
        holders = sorted(str(path.relative_to(ROOT))
                         for path, text in files_view_modules(FILES_PAGE).items()
                         if '审批' in text and '可检索' in text)
        self.assertTrue(holders, '「审批」与「可检索」必须同框出现，说明二者不是同一件事')

    def test_the_files_screen_reports_the_vector_index_honestly(self):
        # 解析波次现在真的会写 chunks 与向量（ingestion/upload_index.py），所以界面
        # **不得**再说「vector_index 恒为 deferred / 未建立向量索引」——那是旧事实。
        # 但也不能反过来把「复核通过」当成「索引已建立」：建索引失败必须单独说清。
        # 用 strip_comments 判定真正的代码，避免只有注释里提到这些词也能变绿。
        code = '\n'.join(strip_comments(text)
                         for text in files_view_modules(FILES_PAGE).values())
        self.assertNotIn('vector_index 恒为', code,
                         '解析波次已建立索引，不能说 vector_index 恒为某个值')
        self.assertIn('建索引失败', code, '建索引失败必须有一句可读的说明')
        self.assertIn('indexErrorText', code, '建索引失败码必须经过映射函数渲染')

    def test_the_chat_screen_can_limit_the_answer_to_selected_files(self):
        """上传闭环的用户可见终点：按文件提问，并提交 document_refs 四元组。

        没有这块，用户上传并复核了文件，问答仍然只会用固定集合——文件「可用」这件事
        在界面上不存在。四条必须留在代码里的事实：拉取 /v1/documents、只允许
        searchable 的文件、组装四元组、以文档问答提交。
        """
        chat = strip_comments(read(CHAT_PANEL))
        main = strip_comments(read(MAIN_PAGE))
        self.assertIn('listDocuments', chat, '文件选择器必须从 /v1/documents 拉取')
        self.assertIn('d.searchable === true', chat,
                      '只有检索器读得到的文件才允许勾选')
        for field in ('document_id', 'document_version', 'source_sha256', 'parser_version'):
            self.assertIn(field, chat, f'document_refs 四元组缺 {field}')
        self.assertIn('document_refs', main, '选中的文件必须真正随问答提交')
        self.assertIn("mode: 'rag'", main,
                      '指定文件只能走文档问答路由（服务端对其它路由 fail closed）')

    def test_authority_page_keeps_the_registry_integrity_warning(self):
        # 注册表里有校验不过的规则时，运行时整体 fail closed——必须明说，
        # 不能让用户以为「其余的规则还在正常裁决」。
        self.assertIn('登记完整性校验失败', read(AUTHORITY_PAGE))

    def test_fields_page_keeps_the_historical_unauthenticated_label(self):
        # reviewer_user_id 为空的复核是历史未认证记录，不能显示成一次正常复核，
        # 也不能作为审批依据。
        self.assertIn('历史未认证', read(FIELDS_PAGE))


# ══════════════════════════════════════════════════════════════════════════
# 7) 历史会话的富内容：打开旧会话不能只剩纯文本
# ══════════════════════════════════════════════════════════════════════════

#: 历史重建时必须从终态负载里补回来的三块富内容（与 send() 流式路径同名同源）。
HISTORY_RICH_FIELDS = ('sqlResult', 'citations', 'industrialResult')


def history_rich_fields_restored(source):
    """``rebuildMessages`` 是否把三块富内容从 run 的终态负载里补进气泡。

    只认**重建函数体内**的赋值：写在别处（例如只在流式路径里赋值）等于历史会话
    打开后仍然只剩纯文本——那正是这条护栏要防的回归。
    """
    body = function_body(source, 'rebuildMessages')
    if not body:
        return False
    return all(re.search(r'\b' + re.escape(field) + r'\s*:', body) for field in HISTORY_RICH_FIELDS)


class HistoryRichContentTests(unittest.TestCase):
    """历史会话的富内容必须真的被还原，而不是「接口有了、页面没用」。"""

    def setUp(self):
        self.source = read(MAIN_PAGE)

    def test_rebuild_messages_restores_the_three_rich_fields(self):
        self.assertTrue(history_rich_fields_restored(self.source),
                        'rebuildMessages 没有再挂回 sqlResult / citations / industrialResult：'
                        '打开历史会话会退回纯文本')

    def test_the_rich_field_check_is_not_vacuous(self):
        """删掉任意一块赋值，判据必须变红——否则这条断言只是恰好为真。"""
        for field in HISTORY_RICH_FIELDS:
            with self.subTest(field=field):
                mutated = re.sub(r'\b' + re.escape(field) + r'\s*:', 'removedField:', self.source)
                self.assertNotEqual(mutated, self.source, '变异没有生效，判据失去意义')
                self.assertFalse(history_rich_fields_restored(mutated),
                                 f'{field} 被删掉后判据仍为真，说明它没有真正检查这一项')

    def test_rich_content_is_fetched_from_the_results_endpoint(self):
        """富内容只能来自 /results（终态事件），不能在客户端凭正文重新拼一个。"""
        composable = read(SESSIONS_COMPOSABLE)
        self.assertIn('listSessionResults', composable)
        self.assertRegex(composable, r'Promise\.all')

    def test_history_evidence_only_lights_runs_that_reached_a_terminal_state(self):
        """证据图只点亮「有终态负载」的最近一个 run。

        没有这条限定，空图的会话会退化成「对每个 run 各发一次 evidence 请求」，
        打开一个 200 轮的会话就是 200 次请求。
        """
        body = function_body(self.source, 'showEvidenceForHistory')
        self.assertIn('withResult', body, '缺少「只取走到终态的那个 run」的限定')
        self.assertIn('for ', body, '必须显式从最近往回找')
        self.assertNotRegex(
            body, r'\.map\([^)]*getEvidence',
            '不得对每个 run 都发一次证据请求：那是打开长会话时的请求爆炸')

    def test_history_evidence_write_is_guarded_against_session_switching(self):
        """证据请求回来时会话可能已经切走：写 graph 之前必须重新校验 activeId。

        没有这道校验就是「消息是 B 的、星图停在 A 的」；而在 showTranscript 末尾补一次
        resetEvidence 兜底又会把 B 刚点亮的证据一起清掉——两步组合的净效果是**空图**。
        所以这里同时钉住「有校验」与「不靠事后 reset 兜底」。
        """
        body = function_body(self.source, 'showEvidenceForHistory')
        self.assertRegex(body, r'sessionId\.value\s*!==\s*id',
                         '写 graph 之前没有重新校验会话归属')
        # 校验必须落在 getEvidence 之后、写 graph 之前，否则等于没校验。
        self.assertLess(body.index('getEvidence'), body.index('sessionId.value'))
        self.assertLess(body.index('sessionId.value'), body.index('graph.nodes'))
        self.assertNotIn('resetEvidence()', function_body(self.source, 'showTranscript'),
                         'showTranscript 不得用事后 resetEvidence 兜底：会清掉新会话的证据')


def history_bucket_uses_calendar_days(source):
    """时间分档是否按**本地日历日**算（`getDate() - n`），而不是减固定毫秒数。

    WHY 这是一条红线：减 `n * 86400000` 在夏令时切换那天会把「昨天」算错一档；
    而这两种写法在**功能测试里都看不出来**（只有那一天、那个时区才暴露）。
    """
    body = function_body(source, 'historyBucket')
    if not body:
        return False
    if 'getDate() - offsetDays' not in body:
        return False
    return '86400000' not in body.replace('_', '')


class ConversationHistoryTests(unittest.TestCase):
    """历史对话的 P0 四项：入口与命名 / 重命名 / 时间分组 / 深链。

    设计依据：docs/CONVERSATION_HISTORY_DESIGN.md（验收 A1–A5）。
    """

    def setUp(self):
        self.side_nav = read(SIDE_NAV)
        self.composable = read(SESSIONS_COMPOSABLE)
        self.main_page = read(MAIN_PAGE)
        self.landing = read(LANDING_PAGE)
        self.runs = read(RUNS_PAGE)

    # ---------- A1 入口与命名 ----------

    def test_sidebar_calls_it_history_and_offers_a_collapse_toggle(self):
        self.assertIn('历史对话', self.side_nav)
        self.assertIn('data-testid="history-toggle"', self.side_nav)
        # 折叠是持久偏好：落盘键必须存在，否则刷新后总是回到展开/折叠之一
        self.assertIn('COLLAPSED_STORAGE_KEY', self.composable)
        self.assertIn('datamind.sessions.collapsed', self.composable)

    def test_landing_page_links_to_history_without_rendering_any_session(self):
        """落地页只能放**跳转按钮**：它是对外页面，渲染会话标题等于把私有记录搬上公开入口。"""
        self.assertIn('data-testid="landing-history"', self.landing)
        self.assertIn("panel: 'history'", self.landing)
        # 红线：落地页不得取会话列表/引用会话状态（变异见下一条）
        for forbidden in ('useSessions', 'listSessions', 'session_id'):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, strip_comments(self.landing),
                                 f'落地页不得出现 {forbidden}：那是私有会话数据')

    def test_the_nav_has_no_separate_history_entry(self):
        """导航里**不再**有独立的「历史对话」入口。

        会话列表本身就是侧栏里的常驻区块（标题「历史对话」+ 搜索 + 行菜单），
        再放一个「跳到它」的导航按钮是重复的——导航里那一格与「问数与问答」同路由，
        既不路由也不高亮，价值只有一个跳转，用户明确要求去掉。

        保留的是：常驻列表本体、它的折叠开关（history-toggle）、以及**落地页**那个
        跳转按钮（`landing-history`，另一条通道，见下一条测试）。
        """
        items = re.findall(r"\{[^{}]*key:\s*'history'[^{}]*\}", strip_comments(self.side_nav))
        self.assertEqual(items, [], '侧栏导航里不应再有「历史对话」条目')
        self.assertNotIn('data-testid="nav-history"', self.side_nav)
        self.assertNotIn('function openHistory', self.side_nav)
        # 常驻列表还在，别把它一起删了
        self.assertIn('data-testid="history-toggle"', self.side_nav)
        self.assertIn("class=\"sessions\"", self.side_nav)

    def test_the_landing_page_privacy_rule_is_not_vacuous(self):
        mutated = self.landing + '\nconst rows = useSessions().rows\n'
        self.assertIn('useSessions', mutated)
        with self.assertRaises(AssertionError):
            self.assertNotIn('useSessions', strip_comments(mutated))

    # ---------- A2 重命名 ----------

    def test_rename_is_reachable_from_the_sidebar(self):
        """后端 PATCH、组合式函数的 rename() 都早已存在，此前**界面零入口**。"""
        self.assertIn('rename: renameSession', self.side_nav)
        self.assertIn('data-testid="session-rename"', self.side_nav)
        self.assertIn('data-testid="session-rename-input"', self.side_nav)
        self.assertIn('commitRename', self.side_nav)
        # 改名必须真的调用组合式函数，而不是只改本地数组
        self.assertIn('await renameSession(id, next)', self.side_nav)

    def test_esc_cancels_rename_without_saving(self):
        """Esc 取消后那次 blur 不能再保存一次（否则「取消」会变成「保存」）。"""
        body = function_body(self.side_nav, 'commitRename')
        self.assertTrue(body)
        # 开头就用 editingId 兜住「已经取消过」的那次 blur
        self.assertRegex(body, r'if\s*\(!id\)\s*return')
        self.assertLess(body.index('if (!id) return'), body.index('renameSession'))

    def test_row_menu_replaces_the_always_visible_delete_button(self):
        """行内动作收进 ⋯ 菜单，删除保留两步确认（数据安全不降级）。"""
        self.assertIn('data-testid="session-menu"', self.side_nav)
        self.assertIn('data-testid="session-delete-confirm"', self.side_nav)
        self.assertNotIn('class="sess-del"', self.side_nav)

    # ---------- A3 时间分组 ----------

    def test_history_groups_and_search_state_are_both_present(self):
        for name in ('今天', '昨天', '前 7 天', '更早'):
            with self.subTest(group=name):
                self.assertIn(f"'{name}'", self.composable)
        body = function_body(self.composable, 'useSessions')
        grouped = re.search(r'groupedRows\s*=\s*computed', self.composable)
        self.assertIsNotNone(grouped, '缺少 groupedRows 计算属性')
        self.assertIn('query.value.trim()', self.composable,
                      '搜索态必须绕过分组（结果顺序是 updated_at，不是相关度）')
        self.assertIsNotNone(body)

    def test_history_buckets_use_calendar_days_not_fixed_milliseconds(self):
        self.assertTrue(history_bucket_uses_calendar_days(self.composable),
                        '时间分档必须按本地日历日算（夏令时那天才不会错一档）')

    def test_the_calendar_day_rule_is_not_vacuous(self):
        mutated = self.composable.replace('getDate() - offsetDays', 'getDate()')
        self.assertNotEqual(mutated, self.composable, '变异没有生效，判据失去意义')
        self.assertFalse(history_bucket_uses_calendar_days(mutated))
        mutated = self.composable.replace('getDate() - offsetDays',
                                          'getDate() - offsetDays * 86400000')
        self.assertFalse(history_bucket_uses_calendar_days(mutated))

    # ---------- A4 深链 ----------

    def test_main_page_accepts_a_session_deep_link_and_clears_it(self):
        self.assertIn('route.query.session', self.main_page)
        self.assertRegex(self.main_page, r'SESSION_ID_RE\s*=\s*/\^sess-')
        self.assertIn('router.replace({ path: \'/app\' })', self.main_page)
        # 深链必须先于 bootstrap 写入 activeId，否则会被 localStorage 里的旧会话盖掉
        self.assertLess(self.main_page.index('sessionId.value = deepLink'),
                        self.main_page.index('await bootstrapSessions()'))

    def test_main_page_expands_history_for_the_landing_entry(self):
        self.assertIn("route.query.panel === 'history'", self.main_page)
        self.assertIn('revealHistory()', self.main_page)

    def test_runs_page_can_open_the_owning_session(self):
        body = function_body(self.runs, 'openSession')
        self.assertTrue(body, '运行记录缺少「打开该会话」的入口')
        self.assertIn("path: '/app'", body)
        self.assertIn('session: conversationId', body)
        self.assertIn('data-testid="open-session"', self.runs)


#: 侧栏历史区的可用高度下限（CSS 里是 216px；这里只要求「不是 0」这类明显退化）
HISTORY_MIN_HEIGHT_RE = re.compile(r'\.sessions\s*\{[^}]*min-height:\s*(\d+)px', re.S)


def history_min_height(source):
    """`.sessions` 的最小高度（px）；没有声明或退化成 0 都返回 0。"""
    block = re.search(r'\.sessions\s*\{([^}]*)\}', source, re.S)
    if not block:
        return 0
    matched = re.search(r'min-height:\s*(\d+)px', block.group(1))
    return int(matched.group(1)) if matched else 0


class SidebarLayoutRegressionTests(unittest.TestCase):
    """钉住两个**真实发生过**的侧栏布局缺陷（源码级判据 + 浏览器级脚本）。

    源码判据只是快筛，真正的判据在 `scripts/verify_frontend_layout.py`：它用无头 Chrome
    量高度、做命中测试、发真实鼠标事件。原因见下面每条测试的 docstring —— 这两个缺陷
    **元素都在 DOM 里**，源码与 DOM 断言全都看不出来。
    """

    def setUp(self):
        self.side_nav = read(SIDE_NAV)

    def test_history_area_cannot_be_squeezed_to_nothing(self):
        """缺陷一：历史区被 flex 压成 4px，连「历史对话」标题都被裁掉。

        侧栏里导航（449px）与底部状态区（298px）的 `min-height` 都是 auto、缩不动，
        空间不足时压力全落在 `.sessions` 上；`min-height: 0` 等于允许它变成 0。
        实测 1440x900 与 800x600 **都是 4px、完整可见行 0**。
        """
        self.assertGreaterEqual(history_min_height(self.side_nav), 120,
                                '`.sessions` 必须有可用的 min-height，否则会被压没')

    def test_the_min_height_guard_is_not_vacuous(self):
        self.assertEqual(history_min_height(''), 0)
        self.assertEqual(history_min_height('.sessions { min-height: 0; }'), 0)
        mutated = re.sub(r'(\.sessions\s*\{[^}]*min-height:\s*)216px', r'\g<1>0px',
                         self.side_nav, count=1, flags=re.S)
        self.assertNotEqual(mutated, self.side_nav, '变异没有生效，判据失去意义')
        self.assertLess(history_min_height(mutated), 120)

    def test_sidebar_scrolls_instead_of_clipping_the_bottom(self):
        """缺陷一的下半段：`overflow: hidden` 让底部状态区在矮窗口永远够不到。"""
        block = re.search(r'\.sidenav\s*\{([^}]*)\}', self.side_nav, re.S).group(1)
        self.assertRegex(block, r'overflow-y:\s*auto',
                         '侧栏必须可滚：否则矮窗口下底部状态区被裁掉且无法到达')

    def test_row_menu_flips_up_near_the_bottom_of_the_list(self):
        """缺陷二：列表底部那几行的 `⋯` 菜单被滚动容器裁掉，**真实鼠标点不到**。

        `elementFromPoint(菜单项中心)` 返回的是侧栏本身，`Input.dispatchMouseEvent`
        点下去没有任何反应。程序化 `.click()` 会绕过命中测试，所以它测不出这个问题。
        """
        self.assertIn('is-up', self.side_nav)
        self.assertRegex(self.side_nav, r'\.sess-menu\.is-up\s*\{[^}]*bottom:')
        body = function_body(self.side_nav, 'toggleMenu')
        self.assertIn('elementFromPoint', read(ROOT / 'scripts' / 'verify_frontend_layout.py'),
                      '浏览器级验证脚本必须保留命中测试（这是唯一能发现该缺陷的判据）')
        self.assertIn('getBoundingClientRect', body,
                      '打开菜单时要量一次几何，才能决定向上还是向下展开')

    def test_history_header_stays_outside_the_scroll_container(self):
        """标题与搜索框必须钉在滚动容器**之外**，否则列表一滚就滚没了。"""
        head = self.side_nav.index('class="sess-head"')
        body = self.side_nav.index('class="sess-body"')
        self.assertLess(head, body, '标题应在滚动容器之前（同级，不在其中）')
        block = re.search(r'\.sess-body\s*\{([^}]*)\}', self.side_nav, re.S).group(1)
        self.assertRegex(block, r'overflow-y:\s*auto', '.sess-body 才是滚动容器')
        sessions_block = re.search(r'\.sessions\s*\{([^}]*)\}', self.side_nav, re.S).group(1)
        self.assertRegex(sessions_block, r'overflow:\s*hidden',
                         '外层历史区不滚：两层各司其职，避免嵌套滚动条')

    def test_collapsing_the_history_actually_reclaims_the_space(self):
        """缺陷四：折叠只是藏掉内容，`min-height` 还在，216px 空白照样占着。

        用户看到的就是「点了收不起来」。判据不能只看「搜索框消失 / aria-expanded=false」
        —— 那些在**有 bug 的版本上也是绿的**（实测：注入缺陷后那两条仍然 PASS）。
        必须同时钉住「折叠态有取消最小高度的规则」与「模板真的挂了这个类」。
        """
        # 模板必须把折叠态挂到容器上，否则 CSS 无从感知
        self.assertRegex(self.side_nav, r'class="sessions"[^>]*:class="\{[^}]*is-collapsed',
                         '折叠态必须挂到 .sessions 上（CSS 不知道 Vue 状态）')
        block = re.search(r'\.sessions\.is-collapsed\s*\{([^}]*)\}', self.side_nav, re.S)
        self.assertIsNotNone(block, '缺少 .sessions.is-collapsed 规则：折叠不会收回空间')
        self.assertRegex(block.group(1), r'min-height:\s*0', '折叠态必须取消 min-height')
        self.assertRegex(block.group(1), r'flex:\s*0\s+0\s+auto', '折叠态不应再参与 flex 增长')

    def test_the_collapse_rule_check_is_not_vacuous(self):
        block = re.search(r'\.sessions\.is-collapsed\s*\{([^}]*)\}', self.side_nav, re.S)
        mutated = self.side_nav[:block.start()] + self.side_nav[block.end():]
        self.assertIsNone(re.search(r'\.sessions\.is-collapsed\s*\{', mutated),
                          '变异没有生效，判据失去意义')
        # 只有展开态的 min-height 时，折叠就会留下空白 —— 这正是被修掉的缺陷
        sessions_block = re.search(r'\.sessions\s*\{([^}]*)\}', mutated, re.S).group(1)
        self.assertIn('min-height', sessions_block)


if __name__ == '__main__':
    unittest.main()
