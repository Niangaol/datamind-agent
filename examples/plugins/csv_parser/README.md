# CSV 示例解析插件

这是**第三方插件接口的可运行示例**，不是内置插件：`ingestion.plugins.builtin_manifests()`
不包含它，默认进程也不会加载它。只有运维显式设置 `DATAMIND_PLUGIN_PATH` 指向本目录，
它才会被发现并启用。插件只使用公开接口（`datamind.plugin.manifest.ParserPluginManifest`
与 `datamind.parse_contract.validate_parse_result`），不导入 `ingestion.plugins` 的内部实现，
也**不需要修改任何核心解析代码**。

- 解析器名：`csv-example`（`1.0.0`）
- 媒体类型：`text/csv`；扩展名：`.csv`
- 能力：`native_text`
- 声明里的 loader 写的是本文件自己的模块名 `csv_parser:parse`；发现流程会把它改写成合成
  模块名，因此插件目录不必出现在 `sys.path` 上（见 `docs/PLUGIN_SYSTEM.md`）。

## 运行方式

在仓库根目录：

```bash
# 1. 让进程发现本目录（os.pathsep 分隔；只扫描一层目录下的顶层 *.py）
export DATAMIND_PLUGIN_PATH=examples/plugins/csv_parser

# 2. 让 worker 领取并解析队列中的 .csv 上传
PYTHONPATH=. .venv/Scripts/python.exe -m ingestion.upload_worker --once
```

也可以不经过上传管道，直接解析一个文件验证契约：

```bash
PYTHONPATH=. DATAMIND_PLUGIN_PATH=examples/plugins/csv_parser \
  .venv/Scripts/python.exe - <<'PY'
from pathlib import Path
from ingestion.plugins import ensure_loaded

manifest = (ensure_loaded().parser.lookup_by_extension('.csv'))
result, extra = manifest.resolve()(Path('sample.csv'), 'doc-demo-1', 'a' * 64)
print(result.payload['parse_status'], len(result.payload['blocks']), extra)
PY
```

上传管道本身走的是另一条缝：`upload_pipeline.target(name)` 做准入，`upload_pipeline.PARSERS[suffix]`
执行解析（背后是 `invoke_plugin`，`tests/test_plugin_example.py` 的 `SCRIPT_DISPATCH` 就是这么调的）。

（`sample.csv` 可自行创建，例如 `printf 'name,value\nalpha,1\n' > sample.csv`。）

## 预期行为

- 设置路径后，`POST /v1/uploads` 接受 `.csv` 文件；未设置时该后缀返回 415
  `UNSUPPORTED_MEDIA_TYPE`（没有已启用插件声明它）。
- 每条 CSV 记录产出一个 `table_row` 块：`text` 为逗号连接的各列、`native_attributes.cells`
  为原始单元格列表、`reading_order = 序号 - 1`、`content_hash` 为 `text` 的 SHA-256；
  locator 为 `sheet_range`，`sheet` 取文件主名（如 `sample`），`range` 为 `A{序号}:Z{序号}`。
- **空行不是记录**：`csv` 对空行返回 `[]`，本插件跳过它——不产出空块、不计入 `rows`。
  序号（`block_id`、`reading_order`、`range`）按保留下来的记录连续编号，因此
  `a,b\n\nc,d\n` 得到两块，`range` 是 `A1:Z1`、`A2:Z2`。
- `parse_status='parsed'`，`analysis.adapter` 与 `analysis.plugin` 都是 `csv-example`，
  `quality_detail.analysis.rows` 为非空记录数。
- **空文件不伪造成功**：返回 `parse_status='unsupported'`、无块、
  `unsupported_features=['empty_source']`、`extra={'rows': 0}`，上传任务终态为
  `partial_failure`。只有空行的文件与空文件等价。
- 通过 `DATAMIND_PLUGIN_CONFIG` 可以停用它，停用后该后缀的上传返回 415 且
  `details.reason='plugin_disabled'`：

```json
{"csv-example": {"enabled": false}}
```

```bash
export DATAMIND_PLUGIN_CONFIG=/path/to/plugin-config.json
```

## 文件

| 文件 | 作用 |
| --- | --- |
| `csv_parser.py` | 唯一被发现的插件模块：声明 `MANIFEST` 并实现 `parse(path, document_id, source_version, options=None)` |
| `README.md` | 本文件；不参与发现（只扫描 `*.py`） |
