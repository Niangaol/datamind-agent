# 解析插件系统（Python-native）

本文件说明 `feat/plugin-parsing` 引入的解析插件系统：一个 Cordis 风格、**纯 Python** 的
插件内核，声明式 parser manifest，以及由注册表驱动的上传路由。实现位于：

| 路径 | 职责 |
| --- | --- |
| `datamind/plugin/context.py`、`events.py` | 内核：服务、注入、可逆 effect、作用域加载/卸载、同步事件总线 |
| `datamind/plugin/manifest.py` | `ParserPluginManifest` 与 `ParserRegistry`（media type / extension 注册表） |
| `datamind/plugin/invocation.py` | `invoke_plugin`：唯一的 handler 调用与失败归属入口 |
| `ingestion/plugins/` | 内置插件声明、`DATAMIND_PLUGIN_PATH` 发现、`DATAMIND_PLUGIN_CONFIG` 配置 |
| `ingestion/upload_pipeline.py` | 上传后缀准入与解析派发（注册表决定，`PARSERS` 只是入口缓存） |
| `datamind/api.py` | 只读清单 `GET /v1/parsers` |

第三方插件只需要公开接口：

```python
from datamind.plugin.manifest import ParserPluginManifest
```

不需要、也不应该导入 `ingestion.plugins` 的内部实现。

## 1. Manifest 字段与校验规则

`ParserPluginManifest` 在**构造时**完成归一化与校验；构造成功即声明合法。

| 字段 | 必填 | 规则 |
| --- | --- | --- |
| `name` | 是 | 非空字符串（去除首尾空白）；注册表内唯一 |
| `version` | 是 | 非空字符串；仅声明用途，不参与比较 |
| `media_types` | 是 | list/tuple（**不接受裸字符串**），至少一个 `type/subtype`，统一小写，不允许重复 |
| `extensions` | 否 | list/tuple，统一小写并补前导点（如 `.csv`），不允许重复；仅有签名可识别、无扩展名的格式可以省略 |
| `capabilities` | 是 | 关键字参数；至少一个非空字符串；没有中央词表，插件自述即可 |
| `loader` | 是 | 关键字参数；可调用对象，或 `"module:function"` 引用。**声明时不 import** |
| `priority` | 否 | 整数（拒绝 `bool`），默认 `100`；仅是声明元数据，注册表不据此排序 |
| `config_schema` | 否 | 扁平声明式 schema，字段 spec 只允许 `type`/`default`/`required`/`choices` |
| `enabled` | 否 | 布尔，默认 `True`；由插件配置翻转，见 §4.4 |

注册表的约束与失败：

- 同一 `name`、同一 media type、同一 extension 都只能有一个声明者；冲突整体拒绝，
  **不插入任何部分**。
- 声明/查询失败抛 `ManifestError`，带稳定错误码与 `details.subject`/`details.reason`；
  `as_detail()` 生成与 `datamind.parse_contract.fail` 相同的 `{"error_code": ...}` 结构。
- 查询未知 media type / extension → `PARSE_ADAPTER_UNKNOWN`；声明了但被停用 →
  `PARSE_ADAPTER_UNSUPPORTED`，`reason='plugin_disabled'`（可从"没人处理"中区分出
  "插件被关掉了"）。
- 每个注册返回 disposer；卸载或重载时释放该声明占用的名字，二次调用是 no-op。

## 2. 三种插件声明形态

### 2.1 单个 `MANIFEST`

最常见形态，模块顶层声明一个 manifest：

```python
from datamind.plugin.manifest import ParserPluginManifest

MANIFEST = ParserPluginManifest(
    'json-lines', '1.0.0', ('application/x-jsonl',), ('.jsonl',),
    capabilities=('native_text',), loader='jsonl_plugin:parse')
```

### 2.2 `MANIFEST` 为可迭代序列（Office 风格）

一个模块可声明多个 manifest（每个格式一个 media type / 扩展名 / 能力集），共享同一个
loader。内置 Office 插件就是这样用一组声明覆盖 DOCX/PPTX/XLSX：

```python
from datamind.plugin.manifest import ParserPluginManifest

_LOADER = 'acme_office:parse'
MANIFEST = (
    ParserPluginManifest('acme-docx', '1.0.0', ('application/vnd...wordprocessingml.document',),
                         ('.docx',), capabilities=('text', 'tables'), loader=_LOADER),
    ParserPluginManifest('acme-xlsx', '1.0.0', ('application/vnd...spreadsheetml.sheet',),
                         ('.xlsx',), capabilities=('text', 'sheet_rows'), loader=_LOADER),
)
```

发现流程对模块级 `MANIFEST`（单个或序列）逐个注册，任一失败则回滚该模块已占用的名字。

### 2.3 `register(ctx)`

模块提供可调用的 `register(ctx)` 时，由它自己向 `ctx.parser` 注册；发现流程把
`register` 执行后**新增**的 manifest 视为该模块的声明。至少要声明一个，否则加载失败：

```python
from datamind.plugin.manifest import ParserPluginManifest


def register(ctx):
    ctx.parser.register(ParserPluginManifest(
        'acme-csv', '1.0.0', ('text/csv',), ('.csv',),
        capabilities=('native_text',), loader='acme_csv:parse'))
```

三种形态都受 §4.2 的 stem loader 改写约束；`register(ctx)` 与 `MANIFEST` 同时存在时，
`register` 优先。

与 `MANIFEST` 形态一样，`register(ctx)` 形态也是可回滚的：只要 `register` 抛异常、它一个
manifest 都没声明，或随后的 loader 改写/配置应用失败，该模块已注册的 parser manifest 会被
全部撤销，模块被跳过（§4.1），注册表里不留半成品。`register` 用 `ctx` 注册的其它东西
（服务、事件处理器、它自己的 disposer）不在此保证内，由插件自己负责。

## 3. Handler 契约

### 3.1 签名与返回值

```python
def parse(path, document_id, source_version, options=None):
    ...
    return result, extra
```

- `path` 是暂存源文件的 `pathlib.Path`；`document_id` 与 `source_version` 标识文档修订；
  `options` 是统一签名里的可选映射（上传路径会传入 claim 字典，插件可按需忽略）。
- 返回 `(ParseResult, extra)`：`ParseResult` 由
  `datamind.parse_contract.validate_parse_result(payload)` 产出（或 `dispatch` 的返回值），
  `extra` 是附加的分析计数（例如 `{'rows': 3}`），会并入
  `quality_detail.analysis`。
- 解析结果必须满足[统一解析契约](PARSE_CONTRACT.md)：`parse_status='parsed'` 至少一个
  block；空源不能伪造成功，应返回 `parse_status='unsupported'` 并声明
  `unsupported_features` 与 `failed_scope`。

### 3.2 惰性导入

插件模块顶层只应导入标准库与 `datamind.plugin`（G2/G7）：

- 解析库（`pymupdf`、`ezdxf`、`python-docx`、`openpyxl`…）与 `fastapi`、契约模块在
  handler 内部导入。这样"列出插件"不会把重型依赖拉进 API 进程。
- `loader` 是 `"module:function"` 字符串时，只有显式调用 `manifest.resolve()` 才会
  import；注册、查询、清单都不解析 loader。

### 3.3 失败归属

所有 handler 调用统一经过 `datamind.plugin.invocation.invoke_plugin`：

| handler 行为 | 调用方得到 | `details.reason` |
| --- | --- | --- |
| 正常返回 | 原样返回 | 日志 `ok` |
| 抛 `fastapi.HTTPException` | **原异常透传**（插件自己的稳定错误码） | 日志 `passed_through` |
| 抛 `ImportError`（含 `ModuleNotFoundError`） | 422 `PARSE_ADAPTER_UNSUPPORTED`，`details={subject:'plugin', plugin, adapter, reason}` | `library_missing` |
| 抛其它 `Exception` | 422 `PARSE_ADAPTER_UNSUPPORTED`，同上 | `handler_failed` |

- 映射错误 `raise ... from error`，原始异常保留在 `__cause__`。
- 每次调用产生一条结构化日志：`plugin`、`media_type`、`reason`、`duration_ms`；失败以
  `WARNING` + traceback 记录在**日志**里。
- **插件调用路径的 HTTP 响应体绝不携带异常文本**：`invoke_plugin` 只回传稳定的错误码与
  `details`，`str(error)` 只进日志——它可能泄露路径、连接串或 SQL。唯一的例外在契约分发：
  `parse_contract.dispatch` 的 `handler_signature` 失败会把 `detail=str(error)` 放进
  `PARSE_RESULT_INVALID`，用于定位签名不符的适配器。
- `KeyboardInterrupt`/`SystemExit` 不归插件报告，原样向上传播。

## 4. 发现与配置

### 4.1 `DATAMIND_PLUGIN_PATH`

- `os.pathsep` 分隔的**目录**列表（Linux `:`，Windows `;`）；空项忽略，目录不存在是配置
  错误（`ConfigError`），不会静默当成"没有外部插件"。
- 每个目录只扫描**一层**顶层的 `*.py`；不递归、不扫 `site-packages`、不扫代码仓库；
  以 `_` 开头的模块跳过（如 `_helper.py`）。
- 多个目录按声明顺序扫描，文件按名称排序，绝对路径去重；同一文件只导入一次。
- 单个模块导入或声明失败：记录 warning 并**跳过该模块**，不影响其它插件；配置错误则
  整轮失败（fail-fast）。跳过是彻底跳过——两种声明形态（`MANIFEST` 与 `register(ctx)`）
  失败时都会撤销该模块已占用的 manifest，注册表里不留半成品。
- 设置该变量后，内置插件与外部插件共用同一个进程级 parser 注册表；外部插件的
  `loaded_from` 是该模块的绝对路径。

### 4.2 合成模块名与 stem loader 改写

插件目录不会加入 `sys.path`。发现流程用
`datamind_external_plugin_<sha1(绝对路径)[:16]>` 作为**合成模块名**导入文件，并且只把
合成名登记进 `sys.modules`（绝不登记裸文件名，避免遮蔽真实模块）。

因此 loader 可以写自己文件的 stem：

```python
MANIFEST = ParserPluginManifest(..., loader='csv_parser:parse')   # 文件 csv_parser.py
```

发现时该引用被改写为 `<合成模块名>:parse`；声明 callable 或指向其它可导入模块的引用保持
不变。`resolve()` 在调用时读取 loader，所以两种声明形态都能受益。

### 4.3 `DATAMIND_PLUGIN_CONFIG`

值是一个 **JSON 文件路径**，内容为 `插件名 -> {"enabled": bool, "config": object|null}`：

```json
{
  "pdf-native": {"enabled": false},
  "csv-example": {"enabled": true, "config": {"delimiter": ","}}
}
```

- 每个条目只允许 `enabled` 与 `config` 两个键；`enabled` 必须是布尔；`config` 必须是对象
  或 `null`。
- `config` 按该 manifest 的 `config_schema` 校验（`validate_config`，失败路径形如
  `plugin.<name>.<field>`）。当前版本只把校验后的 config 记录在 `LoadedPlugin.config`，
  **尚未注入 handler**，清单端点也不回显配置值。
- fail-fast：文件不可读、JSON 非法、条目形状错误、`enabled` 非布尔、`config` 违反 schema、
  或插件名不存在，都会抛 `ConfigError`。API 进程在启动时（FastAPI lifespan）与 `upload_worker`
  在启动时都会跑一次发现并校验配置，所以错误配置会让进程**起不来**，而不是在第一次上传或
  第一次 `GET /v1/parsers` 时才变成一个无标注的 500。配置错误与模块加载错误不同，不会被跳过。
- 发现是显式且非原子的：配置错误可能在部分插件已注册后才暴露；修正配置后重启即可。

### 4.4 启停语义

- `enabled=false` 的 manifest **仍然注册、仍出现在清单里**（`known_extensions()` 默认也
  仍报告它的扩展名），但每次 lookup 都拒绝。
- 上传准入（`upload_pipeline.target`）与解析派发（`run_parse`）都先查注册表：
  停用插件的后缀返回 **415**：

```json
{"detail": {"error_code": "UNSUPPORTED_MEDIA_TYPE",
            "details": {"suffix": ".csv", "reason": "plugin_disabled", "plugin": "csv-example"}}}
```

- 停用只影响后续准入/派发；已经解析并落库的记录仍按声明保留 `plugin` 名字。

## 5. 安全边界

**导入被配置的插件文件就是执行任意代码。** 这是插件系统的本质，不是缺陷：

- 只扫描**运维显式配置**的目录（`DATAMIND_PLUGIN_PATH`），不扫描代码仓库、用户上传目录、
  `site-packages` 或递归子目录；默认（未设置）时只有内置插件。
- 发现发生在 `ensure_loaded()`：API 进程启动时（FastAPI lifespan）、`upload_worker` 启动时、
  每个解析子进程的**第一次解析之前**（不是子进程启动时，G8），以及首次上传准入和首次请求
  `GET /v1/parsers`。也就是说，配置进来的代码在这些入口已经被执行过。
- 外部插件的 `loaded_from` 是绝对路径，会暴露部署目录结构，所以清单端点只对 `admin`
  开放，且永不缓存。
- 插件失败被归属为 422 并在日志里留下 traceback，但错误文本不进入 HTTP 响应体。

## 6. 错误码

| HTTP | `error_code` | `details.reason` | 触发 |
| --- | --- | --- | --- |
| 415 | `UNSUPPORTED_MEDIA_TYPE` | 无 / `plugin_disabled` | 上传后缀没有启用插件声明；停用时额外带 `plugin`。注册表原语对"没声明"抛 422 `PARSE_ADAPTER_UNKNOWN`，对"声明了但停用"抛 422 `PARSE_ADAPTER_UNSUPPORTED`（`reason='plugin_disabled'`）；`target`/`create`/`run_parse` 一律转成该 415 |
| 422 | `PARSE_ADAPTER_UNKNOWN` | `media_type_not_registered` / `extension_not_registered` / `invalid_media_type` / `invalid_extension` / `plugin_not_registered` | 注册表或契约适配器查询未声明的类型/插件 |
| 422 | `PARSE_ADAPTER_UNSUPPORTED` | `library_missing` | handler 内 `ImportError`（可选解析库缺失） |
| 422 | `PARSE_ADAPTER_UNSUPPORTED` | `handler_failed` | handler 抛其它异常 |
| 422 | `PARSE_ADAPTER_UNSUPPORTED` | `no_handler` | 契约适配器只声明能力、没有 dispatch handler |
| 422 | `PARSE_RESULT_INVALID` | `unresolvable_loader` / `loader_not_callable` | `manifest.resolve()` 无法导入或目标不可调用 |
| 422 | `PARSE_RESULT_INVALID` | `handler_signature` | `dispatch` 调用 handler 时 `TypeError`（签名不符） |
| 422 | `PARSE_RESULT_INVALID` | 各声明字段 reason（`invalid_loader`、`duplicate_extension`…） | manifest 声明非法；结果校验失败同样用该码 |

> `unresolvable_loader` 与 `handler_signature` 属于"声明/分发形状错误"，当前实现随
> `PARSE_RESULT_INVALID` 返回；`reason` 字段是稳定信号，跨版本不变。

## 7. 清单端点 `GET /v1/parsers`

- **仅 `admin`**；响应 `Cache-Control: no-store`；响应体标注 `process_local: true`——
  发现路径与配置都是**按进程**读取的，两个 worker 可以给出不同答案。
- 它列出本进程注册表中的**每个** manifest（含停用项），每项恰好八个字段：

| 字段 | 含义 |
| --- | --- |
| `name` / `version` | 声明身份 |
| `media_types` / `extensions` | 声明的类型与后缀 |
| `capabilities` | 声明能力 |
| `enabled` | 当前是否可被 lookup |
| `loaded_from` | 内置为 `"builtin"`，外部为绝对文件路径，直接注册无记录时为 `null` |
| `registered_in_contract` | 同名适配器是否也登记在 `datamind.parse_contract` |

- **不回显配置值**：配置可能含运维私有参数，只读清单不重新发布。
- 该调用不解析任何 loader（G7）：内置插件的 `pymupdf`/`ezdxf`/`python-docx` 不会被拉进
  API 进程。该保证只覆盖内置插件——外部插件模块是 `ensure_loaded()` 自己导入的。

## 8. 与 npm cordis 的差异（诚实说明）

本实现取 Cordis 的**语义**，不追求 API 或字节兼容：

- 没有 npm `cordis`、没有 Node sidecar、没有跨语言 RPC；全部是 Python 进程内代码。
- 事件总线是**同步**的：`emit`/`serial`/`waterfall`/`bail` 在调用线程执行；`parallel` 用
  线程池，但不是 `async`/`await` 调度器。
- 服务通过 `ctx.get(name)` / 属性访问（`ctx.parser`）获取，没有 JavaScript 属性代理。
- `inject` 是显式的：依赖缺失时插件**立即**加载失败（`InjectError`），不会静默等待。
- 三种声明形态由本项目归一化；npm cordis 的插件对象/类形态不是逐字段对应的。
- 插件加载是作用域化的、可逆的：每个插件一个子 context，卸载按相反顺序执行 disposer。

内核与注册表的完整接口以 `datamind/plugin/` 的 docstring 为准。

## 9. 可运行示例

`examples/plugins/csv_parser/` 是一个最小第三方插件：CSV 每条非空记录一个 `table_row` 块，使用
公开 manifest/契约接口，不进入 `builtin_manifests()`，只有设置路径才启用：

```bash
export DATAMIND_PLUGIN_PATH=examples/plugins/csv_parser
PYTHONPATH=. .venv/Scripts/python.exe -m ingestion.upload_worker --once
```

细节（含空文件行为与停用方式）见该目录的 `README.md`，回归见
`tests/test_plugin_example.py`。
