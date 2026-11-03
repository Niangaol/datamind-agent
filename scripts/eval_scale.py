"""Multi-scale timing and peak-memory report (system efficiency at several data scales).

Everything here is measured, not estimated. Each scale runs in its **own subprocess** so the
reported peak RSS belongs to that scale instead of being the high-water mark of every earlier
one (`ru_maxrss` never decreases within a process).

What this is not: a production throughput claim. It is one machine, one process at a time, no
CPU pinning, no cgroup isolation, no concurrent load, and the file cache is warm after the
first run of each shape. The report prints those caveats next to the numbers.

Usage (legacy sweep, unchanged):
    python scripts/eval_scale.py                      # full sweep, writes reports/scale_v1.json
    python scripts/eval_scale.py --entity-scales 100,1000
    python scripts/eval_scale.py --json-only

WP8 additions (writes/merges `reports/scale_v2.json`; `--kind v1` above is untouched):
    python scripts/eval_scale.py --kind sql           # SQL 侧 1千/1万/10万行 × 固定查询集 + 索引优化前后
    python scripts/eval_scale.py --kind documents     # 文档侧 10/100/500 页（仓库现有 PDF 拼接）
    python scripts/eval_scale.py --kind concurrency   # 并发 1/2/4/8：成功率、每秒完成数、峰值内存
    python scripts/eval_scale.py --kind profile       # 分段计时（路由/召回/检索/执行/模型/首字/完整）
    python scripts/eval_scale.py --kind profile_live  # 真实端到端会话分段（新增，见下）
    python scripts/eval_scale.py --kind retrieval_scan# 检索候选扫描 O(n) 实测 + 剪枝原型对照
    python scripts/eval_scale.py --kind all           # 依次跑上面五项

R3 追加（**只新增 section，不改既有模式与字段**）：
    python scripts/eval_scale.py --kind profile --profile-live
        → 既有工具链分段写入 sections.profile（字段不变），真实会话分段另写 sections.profile_live
    python scripts/eval_scale.py --kind profile_live   # 只跑真实会话分段
    真实会话分段用回环免登录身份（`GET /v1/auth/me` 取 CSRF，不伪造会话）流式调用
    `POST /v1/chat/stream`，按 SSE trace 事件的时间戳切出六段：
    路由 / 召回 / 执行 / 模型 / 首字 / 完整响应。上游模型 `stream=false`（`datamind/providers.py`），
    因此「首字」= 客户端收到第一个 SSE trace 事件（首个可见输出）的耗时，报告里显式声明这一点。
    顶层 `model_segment_policy` 由**文件里实际存在的** profile_live 模型样本推导（跑过→measured+n，
    没跑过→empty+原因），任何一次 save_v2 都不会把它写回过期声明。

纪律（写进报告，不只是写在这里）：
* 模型段（首字/完整响应）在未真实调用上游时**留空（null）**，原因一并写入，绝不用 mock 延迟冒充。
* 每个数字都带样本数 `n`；n<20 时 p95 等于观测最大值，报告里显式声明这是最近秩法（nearest-rank）。
* 零分母一律写 `"N/A"`，不用 0 冒充。
* SQL 侧规模数据只进**临时库**（`datamind_scale_*`，跑完即删），不写 `datamind_meta`、不碰演示库。
"""
import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))          # the parent imports the package too (retrieval section)
REPORT = ROOT / "reports/scale_v1.json"
REPEATS = 3

#: Each child prints one JSON object; the parent never imports the heavy modules itself.
CHILD = r'''
import json, resource, sys, time
kind = sys.argv[1]
payload = json.loads(sys.argv[2])
start = time.perf_counter()
if kind == "cad":
    from ingestion import cad_adapter
    result = cad_adapter.parse(payload["path"], document_id="scale-probe",
                               source_version="v1", provenance="synthetic_demo")
    limits = (result.payload.get("manifest") or {}).get("limits") or {}
    detail = {"parse_status": result.payload["parse_status"],
              "blocks": len(result.payload["blocks"]),
              "max_blocks": limits.get("max_blocks")}
elif kind == "retrieval":
    # Measure the *selection* path the product actually uses when N files are chosen, instead
    # of reading every row and slicing: the earlier version reported a flat ~100 ms for 1..8
    # documents because the row read dominated and the limit changed nothing.
    import psycopg2
    from datamind.agent import _read_dsn
    from datamind.knowledge import KnowledgeRetriever
    dsn = _read_dsn()
    retriever = KnowledgeRetriever(dsn, mode="lexical")
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("""SELECT a.document_id, a.doc_version, a.sha256, a.parser_version, a.title
                       FROM document_assets a
                       WHERE a.status='indexed' AND 'public'=ANY(a.acl) AND NOT a.needs_review
                         AND a.collection=ANY(%s)
                         AND (SELECT count(*) FROM chunks c WHERE c.document_id=a.document_id)>0
                       ORDER BY a.document_id LIMIT %s""", (payload["collections"], payload["limit"]))
        refs = [{"document_id": r[0], "document_version": r[1], "source_sha256": r[2],
                 "parser_version": r[3], "title": r[4]} for r in cur.fetchall()]
    rows, snapshots = retriever._selected_rows(payload["collections"], refs)
    detail = {"rows": len(rows), "documents": len(snapshots)}
else:
    raise SystemExit("unknown kind: " + kind)
elapsed_ms = (time.perf_counter() - start) * 1000.0
_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
# `ru_maxrss` is bytes on macOS and kibibytes on Linux; multiplying unconditionally reported
# absurd values (a 183 MiB peak came out as 187120 MiB).
print(json.dumps({"elapsed_ms": elapsed_ms,
                  "peak_rss_bytes": _rss if sys.platform == "darwin" else _rss * 1024,
                  "detail": detail}))
'''


def machine() -> dict:
    cpu = ""
    try:
        cpu = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True,
                             text=True, check=True).stdout.strip()
    except Exception:
        cpu = platform.processor() or "unknown"
    return {"platform": platform.platform(), "cpu": cpu,
            "cpu_count": os.cpu_count(), "python": platform.python_version(),
            "memory_bytes": _total_memory()}


def _total_memory() -> int | None:
    try:
        return int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                                  text=True, check=True).stdout.strip())
    except Exception:
        return None


def measure(kind: str, payload: dict) -> dict:
    """Run one scale `REPEATS` times in fresh subprocesses; report the median and the peak."""
    runs = []
    for _ in range(REPEATS):
        completed = subprocess.run([sys.executable, "-c", CHILD, kind, json.dumps(payload)],
                                   cwd=str(ROOT), capture_output=True, text=True,
                                   env={**os.environ, "PYTHONPATH": str(ROOT)})
        if completed.returncode != 0:
            return {"error": (completed.stderr or completed.stdout).strip().splitlines()[-1:] or
                    ["failed"], "returncode": completed.returncode}
        runs.append(json.loads(completed.stdout.strip().splitlines()[-1]))
    return {"elapsed_ms_median": round(statistics.median(r["elapsed_ms"] for r in runs), 1),
            "elapsed_ms_runs": [round(r["elapsed_ms"], 1) for r in runs],
            "peak_rss_bytes": max(r["peak_rss_bytes"] for r in runs),
            "detail": runs[-1]["detail"], "repeats": len(runs)}


def build_dxf(path: Path, entities: int) -> None:
    """`entities` LINEs plus a little of everything else, so the mix stays realistic."""
    import ezdxf
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 4
    model = doc.modelspace()
    for index in range(entities):
        x = float(index % 1000)
        y = float(index // 1000)
        model.add_line((x, y), (x + 1.0, y + 0.5))
        if index % 50 == 0:
            model.add_circle((x, y), radius=0.5)
        if index % 200 == 0:
            model.add_text(f"L{index}", dxfattribs={"height": 2.0}).set_placement((x, y + 2))
    doc.saveas(path)


def run_v1(args) -> int:
    """The pre-WP8 sweep, byte-for-byte in behaviour: same report path, same field names."""
    scales = [int(item) for item in args.entity_scales.split(",") if item.strip()]
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "machine": machine(),
              "caveats": [
                  "one machine, one process at a time; no CPU pinning or cgroup isolation",
                  "no concurrent load; file cache warm after the first run of each shape",
                  "peak RSS is the child process high-water mark for that scale only",
                  "not a production throughput or capacity claim",
              ],
              "repeats_per_scale": REPEATS, "cad_parse": [], "retrieval": []}

    with tempfile.TemporaryDirectory() as directory:
        for entities in scales:
            path = Path(directory) / f"scale-{entities}.dxf"
            build_dxf(path, entities)
            entry = {"entities_requested": entities, "bytes": path.stat().st_size}
            entry.update(measure("cad", {"path": str(path)}))
            report["cad_parse"].append(entry)
            if not args.json_only:
                _print_cad(entry)

    if not args.json_only:
        print()
    from datamind.agent import _read_dsn  # noqa: F401  (import only to fail fast if misconfigured)
    from datamind.knowledge import COLLECTIONS
    for limit in (1, 2, 5, 8):
        entry = {"selected_documents": limit}
        entry.update(measure("retrieval", {"collections": sorted(COLLECTIONS), "limit": limit}))
        report["retrieval"].append(entry)
        if not args.json_only:
            _print_retrieval(entry)

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    failures = [item for group in ("cad_parse", "retrieval") for item in report[group]
                if "error" in item]
    if not args.json_only:
        print(f"\n报告：{REPORT.relative_to(ROOT)}")
        print("注意：以上为单机单进程实测，不是生产吞吐或容量承诺。")
    return 1 if failures else 0


def _print_cad(entry: dict) -> None:
    if "error" in entry:
        print(f"CAD {entry['entities_requested']:>6} 实体: 失败 {entry['error']}")
        return
    cap = entry['detail'].get('max_blocks')
    truncated = ' ← 块数触顶（上限 %s，属声明式 partial，非静默截断）' % cap \
        if cap and entry['detail']['blocks'] >= cap else ''
    print(f"CAD {entry['entities_requested']:>6} 实体 ({entry['bytes']/1024:>8.1f} KiB): "
          f"解析中位 {entry['elapsed_ms_median']:>9.1f} ms  "
          f"峰值内存 {entry['peak_rss_bytes']/1048576:>7.1f} MiB  "
          f"块={entry['detail']['blocks']} 状态={entry['detail']['parse_status']}{truncated}")


def _print_retrieval(entry: dict) -> None:
    if "error" in entry:
        print(f"检索 选 {entry['selected_documents']} 份: 失败 {entry['error']}")
        return
    print(f"检索 选 {entry['selected_documents']} 份: "
          f"中位 {entry['elapsed_ms_median']:>9.1f} ms  "
          f"峰值内存 {entry['peak_rss_bytes']/1048576:>7.1f} MiB  "
          f"命中行={entry['detail']['rows']} 实际文档={entry['detail']['documents']}")


# ============================================================================
# WP8: scale_v2 —— SQL 侧规模 / 文档侧规模 / 并发 / 分段计时 / 候选扫描
# ============================================================================

V2_REPORT = ROOT / "reports/scale_v2.json"
V2_KINDS = ("v1", "sql", "documents", "concurrency", "profile", "profile_live", "retrieval_scan",
            "locator_ab", "all")
PERCENTILE_METHOD = "nearest-rank (ceil(q*n))；n<20 时 p95 即观测最大值，不是统计分位"
PG_CONTAINER = os.environ.get("DATAMIND_PG_CONTAINER", "datamind-postgres")
PG_ADMIN_USER, PG_ADMIN_PASSWORD = "postgres", "datamind_dev"
PG_READONLY_USER, PG_READONLY_PASSWORD = "readonly_user", "readonly_dev"
SCALE_DB_PREFIX = "datamind_scale_"
DATA_DIR = ROOT / "tmp" / "scale_v2_data"
DOC_SOURCES = ("data/knowledge_base/01_重点产品市场分析报告_2025.pdf",
               "data/knowledge_base/06_2025年度经营分析报告.pdf")
DOC_SIZES = (10, 100, 500)
DOC_SAMPLES = (3, 2, 1)          # 每档样本数：500 页每次要渲染 500 页位图，样本数如实写进报告
CONCURRENCY = (1, 2, 4, 8)
SCAN_SIZES = (500, 1000, 5000, 10000)
SCAN_QUESTION = "2025年华东地区数码品类的销售额是多少"
#: 取自冻结登记 `data/evaluation/registry/nl2sql_v1.json` 的真实题面（只读引用，不改题）。
PROFILE_QUESTIONS = ("有多少个客户注册了大区信息？", "2024年一共有多少笔订单？",
                     "华东地区的客户下了多少笔订单？")
#: 模型段留空的原因（写进报告，可核对）。只在**真的没有** profile_live 模型样本时使用。
MODEL_SEGMENT_REASON = (
    "本轮未真实调用上游模型：WP8 只做工具链测量；服务 `POST /v1/chat` 实测返回 "
    "`{\"detail\":{\"error_code\":\"CSRF_REQUIRED\"}}`，取会话要写 datamind_meta 的 auth_sessions，"
    "超出本轮允许的写入范围。完整问答/首字时延因此留空（null），不用 mock 延迟冒充。")

#: 固定查询集：单表过滤 / 两表 JOIN 聚合 / 三表 JOIN（对应「复杂度 + 规模」两项设计目标）。
FIXED_QUERIES = (
    {"id": "q1_single_filter", "label": "单表过滤：orders.created_at 区间 + 状态计数",
     "sql": "SELECT count(*) AS orders, "
            "sum(CASE WHEN order_status='completed' THEN 1 ELSE 0 END) AS completed "
            "FROM orders WHERE created_at >= %s",
     "params": ["2024-07-01T00:00:00+08:00"]},
    {"id": "q2_two_table_join", "label": "两表 JOIN 聚合：order_items × orders 按渠道汇总销售额",
     "sql": "SELECT o.channel, count(*) AS items, "
            "round(sum(oi.quantity*oi.unit_price*(1-oi.discount)),2) AS amount "
            "FROM order_items oi JOIN orders o ON o.order_id=oi.order_id "
            "GROUP BY o.channel ORDER BY amount DESC", "params": []},
    {"id": "q3_three_table_join", "label": "三表 JOIN：order_items × orders × products 按品类汇总",
     "sql": "SELECT p.category, count(DISTINCT o.customer_id) AS buyers, "
            "round(sum(oi.quantity*oi.unit_price*(1-oi.discount)),2) AS amount "
            "FROM order_items oi JOIN orders o ON o.order_id=oi.order_id "
            "JOIN products p ON p.product_id=oi.product_id "
            "GROUP BY p.category ORDER BY amount DESC", "params": []},
    {"id": "q4_customer_detail", "label": "客户维度明细：customer_id + created_at 过滤，排序 Top-20",
     "sql": "SELECT oi.order_item_id, oi.quantity, oi.unit_price, o.created_at "
            "FROM order_items oi JOIN orders o ON o.order_id=oi.order_id "
            "WHERE o.customer_id=%s AND o.created_at >= %s "
            "ORDER BY o.created_at DESC, oi.order_item_id LIMIT 20",
     "params": [1375, "2024-01-01T00:00:00+08:00"]},
)

V2_CAVEATS = [
    "单机单进程/多进程实测，无 CPU 绑定、无 cgroup 隔离，不是生产吞吐或容量承诺",
    "SQL 侧数据是本生成器合成的同 schema 数据（tmp/scale_v2_data），不是真实企业数据",
    "SQL 侧规模实验在临时库（datamind_scale_*）中进行，跑完即删；不写 datamind_meta、不碰演示库",
    "模型段（首字/完整响应）在未真实调用上游时留空（null），不用 mock 延迟冒充",
    "文档侧 10/100/500 页由仓库现有 PDF 拼接重复而成，内容重复会放大缓存命中，属受限路径测量",
    "已知受限输出路径：CAD 解析的后两档（5000/20000 实体）在 reports/scale_v1.json 中均为 partial，"
    "块数触顶上限 2000 → 那是「受限输出路径」的耗时，不是完整解析 2 万实体并输出全部证据的证明",
    "SQL 侧只测执行层：模型生成 SQL、工具链重试与安全门禁不计入这些毫秒数",
    PERCENTILE_METHOD,
]

V2_SQL_CHILD = r'''
import json, resource, sys, time
import psycopg2
payload = json.loads(sys.argv[1])
def peak():
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform == "darwin" else value * 1024
begin = time.perf_counter()
conn = psycopg2.connect(payload["dsn"])
conn.set_session(readonly=True)
connect_ms = (time.perf_counter() - begin) * 1000.0
samples, rows = [], None
with conn.cursor() as cur:
    for _ in range(payload.get("warmups", 1)):
        cur.execute(payload["sql"], payload.get("params") or None)
        cur.fetchall()
    for _ in range(payload["samples"]):
        start = time.perf_counter()
        cur.execute(payload["sql"], payload.get("params") or None)
        fetched = cur.fetchall()
        samples.append((time.perf_counter() - start) * 1000.0)
        rows = len(fetched)
conn.close()
print(json.dumps({"samples_ms": samples, "connect_ms": connect_ms, "rows": rows,
                  "peak_rss_bytes": peak()}))
'''

V2_DOC_CHILD = r'''
import json, resource, sys, time
from pathlib import Path
payload = json.loads(sys.argv[1])
import ingestion.ingest as ingest
# 截图目录改到临时目录：规模探针不往 data/object_store 里写任何东西
ingest.PAGE_IMG_DIR = Path(payload["image_dir"])
def peak():
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform == "darwin" else value * 1024
start = time.perf_counter()
blocks, pages = ingest.parse_pdf(Path(payload["path"]), payload["doc_id"])
parse_ms = (time.perf_counter() - start) * 1000.0
ingest.ensure_block_ids(blocks, payload["doc_id"])
begin = time.perf_counter()
chunks = ingest.build_chunks(blocks, payload["doc_id"])
slice_ms = (time.perf_counter() - begin) * 1000.0
images = len(list(Path(payload["image_dir"]).glob("*.png")))
print(json.dumps({"pages": pages, "blocks": len(blocks), "chunks": len(chunks),
                  "parse_ms": parse_ms, "slice_ms": slice_ms,
                  "pages_rendered": images, "peak_rss_bytes": peak()}))
'''

V2_CONC_WORKER = r'''
import json, resource, sys, time
import psycopg2
payload = json.loads(sys.argv[1])
def peak():
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform == "darwin" else value * 1024
try:
    conn = psycopg2.connect(payload["dsn"])
    conn.set_session(readonly=True)
    cur = conn.cursor()
    for _ in range(payload.get("warmups", 1)):
        first = payload["queries"][0]
        cur.execute(first["sql"], first.get("params") or None)
        cur.fetchall()
    done = 0
    begin = time.perf_counter()
    for index in range(payload["iterations"]):
        query = payload["queries"][index % len(payload["queries"])]
        cur.execute(query["sql"], query.get("params") or None)
        cur.fetchall()
        done += 1
    elapsed_ms = (time.perf_counter() - begin) * 1000.0
    conn.close()
    print(json.dumps({"ok": True, "queries": done, "elapsed_ms": elapsed_ms,
                      "peak_rss_bytes": peak()}))
except Exception as exc:
    print(json.dumps({"ok": False, "error": type(exc).__name__ + ": " + str(exc)[:200],
                      "peak_rss_bytes": peak()}))
'''

V2_PROFILE_CHILD = r'''
import json, resource, sys, time
sys.path.insert(0, sys.argv[2])
payload = json.loads(sys.argv[1])
import psycopg2
from datamind.agent import route_for
from datamind.catalog import Catalog
from datamind.knowledge import KnowledgeRetriever
def peak():
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform == "darwin" else value * 1024
segments = {name: [] for name in
            ("route", "catalog_build", "catalog_recall", "knowledge_retrieve",
             "scale_connect", "execute_sql", "toolchain_total")}
detached = {}
for index, question in enumerate(payload["questions"]):
    begin = time.perf_counter()
    route = route_for(question)
    route_ms = (time.perf_counter() - begin) * 1000.0
    if index == 0:                       # 冷启动：本进程第一次构建 Catalog / 检索器 / 连接
        begin = time.perf_counter()
        catalog = Catalog(payload["meta_dsn"], "demo_retail")
        catalog_ms = (time.perf_counter() - begin) * 1000.0
        begin = time.perf_counter()
        retriever = KnowledgeRetriever(payload["meta_dsn"])
        retriever_ms = (time.perf_counter() - begin) * 1000.0
        begin = time.perf_counter()
        conn = psycopg2.connect(payload["scale_dsn"])
        conn.set_session(readonly=True)
        connect_ms = (time.perf_counter() - begin) * 1000.0
        detached = {"retriever_build_ms": retriever_ms, "cold_catalog_build_ms": catalog_ms}
    else:
        catalog_ms = None
        begin = time.perf_counter()
        conn = psycopg2.connect(payload["scale_dsn"])
        conn.set_session(readonly=True)
        connect_ms = (time.perf_counter() - begin) * 1000.0
    begin = time.perf_counter()
    recall = catalog.retrieve(question)
    recall_ms = (time.perf_counter() - begin) * 1000.0
    begin = time.perf_counter()
    retrieved = retriever.retrieve(question, collections=payload["collections"])
    retrieve_ms = (time.perf_counter() - begin) * 1000.0
    begin = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute(payload["scale_sql"])
        rows = len(cur.fetchall())
    execute_ms = (time.perf_counter() - begin) * 1000.0
    conn.close()
    values = {"route": route_ms, "catalog_recall": recall_ms,
              "knowledge_retrieve": retrieve_ms, "scale_connect": connect_ms,
              "execute_sql": execute_ms,
              "toolchain_total": route_ms + recall_ms + retrieve_ms + connect_ms + execute_ms}
    if catalog_ms is not None:
        values["catalog_build"] = catalog_ms
    for name, value in values.items():
        segments[name].append(value)
    detached.setdefault("routes", []).append(route)
    detached.setdefault("retrieval_modes", []).append(retrieved.get("mode"))
    detached.setdefault("retrieval_warnings", []).extend(retrieved.get("warnings") or [])
    detached.setdefault("eligible_chunks", []).append(retrieved.get("eligible_chunks"))
    detached.setdefault("scale_rows", []).append(rows)
print(json.dumps({"segments_ms": segments, "detail": detached, "peak_rss_bytes": peak()}))
'''

V2_SCAN_CHILD = r'''
import json, resource, sys, time
sys.path.insert(0, sys.argv[2])
payload = json.loads(sys.argv[1])
from datamind.knowledge import lexical_ranking, normalize, tokens
def peak():
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform == "darwin" else value * 1024
FILLER = ("设备维保 工单 巡检 记录 现场 作业 许可 安全 培训 环境 温度 湿度 噪声 台账 "
          "编号 责任人 完成 时间 部门 备注 附件 签名 复核 归档 序号 登记 项目 流程 规范 "
          "条款 说明 状态 范围 附件 清单 意见 会签 归档 ")
HOT = ("华东 数码 品类 销售额 2025 客户 等级 钻石 退款 时限 七日 无理由 ")
rows = []
for index in range(max(payload["sizes"])):
    text = (HOT if index % 20 == 0 else "") + FILLER * payload["repeat"]
    rows.append({"chunk_id": "chk-%05d" % index, "document_id": "doc-%03d" % (index % 50),
                 "title": "演示知识库文档", "section_path": ["三、指标口径", "3.2 计算公式"],
                 "exact_text": text + " 行号%05d" % index})
question = payload["question"]
query_tokens = [term for term in set(tokens(question)) if len(term) >= 2]
def pruned(question, rows):
    # 原型剪枝：先做「查询词是否作为子串出现」的廉价过滤，再进入完整打分。
    # 被丢掉的行走不到任何打分分支（token 一定是文本子串），但 BM25 的长度均值会变，
    # 所以只声称「打分候选集不变 + 实测 top-5 相同」，不声称分数逐位相同。
    kept = []
    for row in rows:
        text = normalize(row["title"] + "\n" + " > ".join(row["section_path"]) + "\n" + row["exact_text"])
        if any(term in text for term in query_tokens):
            kept.append(row)
    return kept
report = []
for size in payload["sizes"]:
    subset = rows[:size]
    full_samples, full_top, full_scored = [], None, None
    for _ in range(payload["samples"]):
        begin = time.perf_counter()
        ranked = lexical_ranking(question, subset)
        full_samples.append((time.perf_counter() - begin) * 1000.0)
        full_top = [item[0] for item in ranked[:5]]
        full_scored = len(ranked)
    begin = time.perf_counter()
    kept = pruned(question, subset)
    prefilter_ms = (time.perf_counter() - begin) * 1000.0
    pruned_samples, pruned_top, pruned_scored = [], None, None
    for _ in range(payload["samples"]):
        begin = time.perf_counter()
        ranked = lexical_ranking(question, kept)
        pruned_samples.append((time.perf_counter() - begin) * 1000.0)
        pruned_top = [item[0] for item in ranked[:5]]
        pruned_scored = len(ranked)
    report.append({"size": size, "full_ms": full_samples, "pruned_ms": pruned_samples,
                   "prefilter_ms": prefilter_ms, "kept_rows": len(kept),
                   "full_scored_rows": full_scored, "pruned_scored_rows": pruned_scored,
                   "candidate_set_equal": full_scored == pruned_scored,
                   "top5_equal": full_top == pruned_top, "full_top5": full_top,
                   "pruned_top5": pruned_top,
                   "row_chars": len(subset[0]["exact_text"]), "hot_every": 20})
print(json.dumps({"points": report, "query_tokens": sorted(query_tokens),
                  "peak_rss_bytes": peak()}))
'''


class ScaleDatabaseUnavailable(RuntimeError):
    """The temporary scale database could not be created/loaded; the reason is reported as-is."""


#: 块定位查询对照：A=产品现状（`datamind/knowledge.py::_read_rows` 里按 chunk 行重复执行的关联
#: 子查询，每返回一行就跑一次子计划），B=批量取回（一次 JOIN + 聚合）。表形状取自
#: `datamind_meta` 的 document_blocks / chunks（只取本对照用到的列）。
LOCATOR_STORE_DDL = """
CREATE TABLE scale_blocks (
    block_id       VARCHAR(64) PRIMARY KEY,
    document_id    VARCHAR(64) NOT NULL,
    page_no        INTEGER NOT NULL,
    bbox           JSONB,
    content_hash   CHAR(64) NOT NULL,
    ocr_confidence NUMERIC(4,3),
    formula_latex  TEXT,
    parser_version VARCHAR(64),
    reading_order  INTEGER
);
CREATE INDEX scale_blocks_doc ON scale_blocks(document_id, page_no);
CREATE TABLE scale_chunks (
    chunk_id         VARCHAR(64) PRIMARY KEY,
    document_id      VARCHAR(64) NOT NULL,
    document_version INTEGER NOT NULL DEFAULT 1,
    chunk_index      INTEGER NOT NULL,
    section_path     TEXT[] NOT NULL DEFAULT '{}',
    page_start       INTEGER,
    page_end         INTEGER,
    block_ids        TEXT[] NOT NULL DEFAULT '{}',
    exact_text       TEXT,
    content_hash     CHAR(64) NOT NULL,
    acl              TEXT[] NOT NULL DEFAULT '{public}'
);
"""

#: A=产品现状：与 `datamind/knowledge.py:117-130` 的 source_blocks 子查询同一形状。
LOCATOR_QUERY_PER_ROW = """
SELECT c.chunk_id, COALESCE((SELECT jsonb_agg(jsonb_build_object(
    'block_id', b.block_id, 'page_no', b.page_no, 'bbox', b.bbox,
    'content_hash', b.content_hash, 'ocr_confidence', b.ocr_confidence,
    'formula_latex', b.formula_latex, 'parser_version', b.parser_version)
    ORDER BY b.reading_order) FROM scale_blocks b
    WHERE b.document_id=c.document_id AND b.block_id=ANY(c.block_ids)), '[]'::jsonb) AS source_blocks
FROM scale_chunks c
WHERE c.document_version=1 AND 'public'=ANY(c.acl)
ORDER BY c.chunk_id LIMIT %s
"""

#: B=批量取回：同样输出形状，一次 JOIN + 分组，不再每行一次子计划。
LOCATOR_QUERY_BATCHED = """
SELECT c.chunk_id, COALESCE(jsonb_agg(jsonb_build_object(
    'block_id', b.block_id, 'page_no', b.page_no, 'bbox', b.bbox,
    'content_hash', b.content_hash, 'ocr_confidence', b.ocr_confidence,
    'formula_latex', b.formula_latex, 'parser_version', b.parser_version)
    ORDER BY b.reading_order), '[]'::jsonb) AS source_blocks
FROM scale_chunks c LEFT JOIN scale_blocks b
  ON b.document_id=c.document_id AND b.block_id=ANY(c.block_ids)
WHERE c.document_version=1 AND 'public'=ANY(c.acl)
GROUP BY c.chunk_id ORDER BY c.chunk_id LIMIT %s
"""

V2_LOCATOR_CHILD = r'''
import hashlib, json, resource, sys, time
import psycopg2
payload = json.loads(sys.argv[1])
def peak():
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform == "darwin" else value * 1024
conn = psycopg2.connect(payload["dsn"])
conn.set_session(readonly=True)
out = []
with conn.cursor() as cur:
    for shape, sql in (("per_row", payload["per_row_sql"]), ("batched", payload["batched_sql"])):
        cur.execute(sql, (payload["limit"],))
        cur.fetchall()                                   # warmup，不计时
        samples, rows = [], None
        for _ in range(payload["samples"]):
            begin = time.perf_counter()
            cur.execute(sql, (payload["limit"],))
            rows = cur.fetchall()
            samples.append((time.perf_counter() - begin) * 1000.0)
        filled = sum(1 for row in rows if row[1])
        digest = hashlib.sha256(json.dumps(
            sorted((row[0], json.dumps(row[1], sort_keys=True)) for row in rows),
            ensure_ascii=False).encode("utf-8")).hexdigest()
        out.append({"shape": shape, "samples_ms": samples, "rows": len(rows),
                    "rows_with_blocks": filled, "result_sha256": digest})
    cur.execute(payload["per_row_sql"], (payload["limit"],))
    per_row = {row[0]: json.dumps(row[1], sort_keys=True) for row in cur.fetchall()}
    cur.execute(payload["batched_sql"], (payload["limit"],))
    batched = {row[0]: json.dumps(row[1], sort_keys=True) for row in cur.fetchall()}
conn.close()
mismatch = [key for key in set(per_row) | set(batched) if per_row.get(key) != batched.get(key)]
print(json.dumps({"shapes": out, "equivalent": not mismatch, "mismatch_count": len(mismatch),
                  "mismatch_ids": sorted(mismatch)[:5], "peak_rss_bytes": peak()}))
'''


def percentile(values, fraction: float) -> float | None:
    """Nearest-rank percentile; an empty sample gives None (never 0)."""
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    rank = min(len(ordered), max(1, math.ceil(fraction * len(ordered))))
    return round(ordered[rank - 1], 3)


def summarize(values) -> dict:
    values = [float(value) for value in values]
    return {"n": len(values), "p50_ms": percentile(values, 0.5), "p95_ms": percentile(values, 0.95),
            "min_ms": round(min(values), 3) if values else None,
            "max_ms": round(max(values), 3) if values else None,
            "samples_ms": [round(value, 3) for value in values]}


def ratio(numerator, denominator, digits: int = 4):
    """A rate with an explicit `N/A` for a zero denominator."""
    if not denominator:
        return "N/A"
    return round(numerator / denominator, digits)


def null_segment(reason: str, *, status: str = "not_measured") -> dict:
    """A segment that was not measured: every numeric field is null, never 0.

    Keeps the same key set as a measured segment (`cold_ms` / `cold_definition` included) so a
    consumer can read a segment without first asking which kind it is.
    """
    return {"n": 0, "p50_ms": None, "p95_ms": None, "min_ms": None, "max_ms": None,
            "samples_ms": [], "cold_ms": None,
            "cold_definition": "未测量：本轮没有可用的真实调用", "status": status, "reason": reason}


def _peak_rss_bytes() -> int:
    import resource
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform == "darwin" else value * 1024


def docker_exec(argv, *, timeout: float = 300.0):
    try:
        return subprocess.run(["docker", "exec", PG_CONTAINER, *argv], capture_output=True,
                              text=True, timeout=timeout)
    except Exception as exc:                                    # docker missing / not running
        return subprocess.CompletedProcess(["docker", *argv], 127, "", f"{type(exc).__name__}: {exc}")


def admin_dsn(dbname: str) -> str:
    return (f"host=127.0.0.1 port=5432 dbname={dbname} user={PG_ADMIN_USER} "
            f"password={PG_ADMIN_PASSWORD}")


def readonly_dsn(dbname: str) -> str:
    return (f"host=127.0.0.1 port=5432 dbname={dbname} user={PG_READONLY_USER} "
            f"password={PG_READONLY_PASSWORD}")


def container_facts() -> dict:
    facts: dict = {"postgres_container": PG_CONTAINER}
    version = docker_exec(["psql", "-U", PG_ADMIN_USER, "-d", "postgres", "-tAc",
                           "select version()"])
    facts["postgres_version"] = version.stdout.strip() if version.returncode == 0 else None
    if version.returncode != 0:
        facts["postgres_probe_error"] = (version.stderr or "").strip()[:400] or "docker exec failed"
    for name in (PG_CONTAINER, "datamind-qdrant"):
        inspected = subprocess.run(["docker", "inspect", name, "--format",
                                    "{{.Config.Image}}|{{.State.StartedAt}}|{{.RestartCount}}"],
                                   capture_output=True, text=True)
        facts[name] = inspected.stdout.strip() if inspected.returncode == 0 else None
    return facts


def api_version() -> str | None:
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/openapi.json", timeout=5) as response:
            return json.loads(response.read().decode("utf-8")).get("info", {}).get("version")
    except Exception:
        pass
    import re
    match = re.search(r'version="([^"]+)"', (ROOT / "datamind/api.py").read_text(encoding="utf-8"))
    return match.group(1) if match else None


def git_commit() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(ROOT),
                              capture_output=True, text=True, check=True).stdout.strip() or None
    except Exception:
        return None


def environment() -> dict:
    return {"machine": machine(), "api_version": api_version(), "commit": git_commit(),
            "containers": container_facts(), "scale_data_dir": str(DATA_DIR.relative_to(ROOT)),
            "cold_start_definition": "冷启动=该测量子进程的第一次调用（含导入/建连/模型加载）；预热=同进程后续调用",
            "samples_are_per_point": True,
            "percentile_method": PERCENTILE_METHOD}


def model_segment_policy(report: dict) -> dict:
    """Derive the model-segment claim from the measurements the report actually contains.

    `--kind profile_live` is the only mode that contacts the upstream model, so it is the only
    thing that may turn this claim into "measured". The previous version wrote a hard-coded
    "模型段为空 / 本轮未真实调用上游模型" on *every* save, so after a real `profile_live` run
    (model p50 1674.2 ms, n=12) the top-level field still denied the measurement that sat right
    below it — a stale side-claim, exactly what the round-3 review flags. Now: real model samples
    in `profile_live` -> `measured` with n; otherwise -> `empty` with the honest reason.
    """
    section = (report.get("sections") or {}).get("profile_live")
    payload = report.get("profile_live") or {}
    segment = (payload.get("segments") or {}).get("model") or {}
    samples = int(segment.get("n") or 0)
    if section and samples:
        return {"status": "measured", "segment": "model", "n": samples,
                "p50_ms": segment.get("p50_ms"), "p95_ms": segment.get("p95_ms"),
                "min_ms": segment.get("min_ms"), "max_ms": segment.get("max_ms"),
                "live_status": payload.get("status"), "source": "sections.profile_live",
                "reason": None,
                "policy": ("模型/首字/完整响应三段由真实端到端会话实测：回环免登录身份流式调用 "
                           "`POST /v1/chat/stream`，按 SSE trace 事件时间戳切段；未跑 profile_live 时"
                           "本字段才写 empty，绝不用 mock 延迟或 0 冒充。")}
    if section:
        return {"status": "empty", "segment": "model", "n": 0,
                "live_status": payload.get("status"), "source": "sections.profile_live",
                "reason": ("profile_live 已运行但没有可引用的模型分段样本（profile_live.status="
                           + str(payload.get("status")) + "），因此不声称模型时延。")}
    return {"status": "empty", "reason": MODEL_SEGMENT_REASON}


def load_v2() -> dict:
    if V2_REPORT.is_file():
        try:
            return json.loads(V2_REPORT.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_v2(section: str, payload: dict, *, command: str) -> dict:
    report = load_v2()
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    report["report_version"] = "scale_v2"
    report["environment"] = environment()
    report["caveats"] = V2_CAVEATS
    report.setdefault("sections", {})[section] = {
        "measured_at": datetime.now(timezone.utc).isoformat(), "command": command,
        "cwd": str(ROOT), "python": platform.python_version()}
    report[section] = payload
    # After the merge, so the claim is derived from the sections the file really holds (a run of
    # any other kind must not downgrade a model segment that was measured by profile_live).
    report["model_segment_policy"] = model_segment_policy(report)
    V2_REPORT.parent.mkdir(parents=True, exist_ok=True)
    V2_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    return report


def _scale_module():
    sys.path.insert(0, str(ROOT / "scripts"))
    import build_scale_data
    return build_scale_data


def setup_scale_db(tier: int, data_dir: Path, *, keep: bool = False,
                   load_data: bool = True) -> dict:
    """Create a throwaway database, load one tier from `data_dir`, return handles + facts."""
    module = _scale_module()
    name = SCALE_DB_PREFIX + uuid.uuid4().hex[:8]
    created = docker_exec(["createdb", "-U", PG_ADMIN_USER, "-O", "datamind_app", name])
    if created.returncode != 0:
        raise ScaleDatabaseUnavailable(
            f"createdb 失败（exit={created.returncode}）：{(created.stderr or created.stdout).strip()[:400]}")
    facts = {"container": PG_CONTAINER, "temp_db": name, "tier_order_items": tier,
             "owner": "datamind_app", "measured_as": PG_READONLY_USER, "indexes": "primary keys only",
             "dropped_after_run": (not keep)}
    try:
        begin = time.perf_counter()
        with psycopg2_connect(admin_dsn(name)) as conn:
            with conn.cursor() as cur:
                cur.execute(module.SCHEMA_SQL)
                counts = {}
                if load_data:
                    for table in module.TABLE_ORDER:
                        columns = ",".join(module.COPY_COLUMNS[table])
                        path = data_dir / str(tier) / f"{table}.tsv"
                        if not path.is_file():
                            raise ScaleDatabaseUnavailable(
                                f"缺少规模数据文件 {path}；先跑 scripts/build_scale_data.py")
                        with path.open("r", encoding="utf-8") as handle_file:
                            cur.copy_expert(
                                f"COPY {table}({columns}) FROM STDIN WITH (FORMAT text, NULL '\\N')",
                                handle_file)
                        cur.execute(f"SELECT count(*) FROM {table}")
                        counts[table] = cur.fetchone()[0]
                    cur.execute("ANALYZE")
                    cur.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO readonly_user")
                cur.execute("GRANT USAGE ON SCHEMA public TO readonly_user")
            facts["rows"] = counts if load_data else {}
        facts["load_ms"] = round((time.perf_counter() - begin) * 1000.0, 1)
        facts["load_finished_at"] = datetime.now(timezone.utc).isoformat()
    except Exception:
        if not keep:
            drop_scale_db(name)
        raise
    return {"name": name, "dsn_admin": admin_dsn(name), "dsn_readonly": readonly_dsn(name),
            "facts": facts, "keep": keep}


def psycopg2_connect(dsn: str):
    import psycopg2
    return psycopg2.connect(dsn)


def drop_scale_db(name: str) -> dict:
    """Terminate leftovers inside *our own* temporary database, then drop it."""
    out = {}
    try:
        with psycopg2_connect(admin_dsn("postgres")) as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname=%s AND pid <> pg_backend_pid()", (name,))
        dropped = docker_exec(["dropdb", "-U", PG_ADMIN_USER, "--if-exists", name])
        out = {"dropped": dropped.returncode == 0,
               "stderr": (dropped.stderr or "").strip()[:200] or None}
    except Exception as exc:
        out = {"dropped": False, "stderr": f"{type(exc).__name__}: {exc}"[:200]}
    return out


def data_integrity(tiers) -> dict:
    """Verify the on-disk scale data against its manifest by regeneration (read-only)."""
    module = _scale_module()
    problems = module.verify(DATA_DIR, tiers)
    manifest = {}
    path = DATA_DIR / "manifest.json"
    if path.is_file():
        manifest = json.loads(path.read_text(encoding="utf-8"))
    return {"ok": not problems, "problems": problems,
            "manifest_marker": manifest.get("marker"),
            "schema_sql_sha256": manifest.get("schema_sql_sha256"),
            "generator": manifest.get("generated_by"),
            "seed": manifest.get("seed"),
            "files": manifest.get("files", {}),
            "index_source": manifest.get("index_source")}


def run_child(source: str, payload: dict, *, timeout: float = 1800.0) -> dict:
    import uuid as _uuid
    script = ROOT / "tmp" / f"wp8_child_{_uuid.uuid4().hex[:8]}.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(source, encoding="utf-8")
    try:
        completed = subprocess.run([sys.executable, str(script), json.dumps(payload), str(ROOT)],
                                   cwd=str(ROOT), capture_output=True, text=True, timeout=timeout,
                                   env={**os.environ, "PYTHONPATH": str(ROOT)})
    finally:
        script.unlink(missing_ok=True)
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout).strip().splitlines()[-3:]
        raise ScaleDatabaseUnavailable(f"子进程失败 exit={completed.returncode}: {tail}")
    return json.loads(completed.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------- sql 模式

def run_sql(args) -> int:
    module = _scale_module()
    tiers = [int(item) for item in args.tiers.split(",") if item.strip()]
    integrity = data_integrity(tiers)
    if not integrity["ok"]:
        payload = {"status": "blocked", "reason": "规模数据校验失败",
                   "problems": integrity["problems"]}
        save_v2("sql_scale", payload, command=" ".join(sys.argv))
        print("规模数据校验失败：" + "; ".join(integrity["problems"]))
        return 1
    try:
        handle = setup_scale_db(max(tiers), DATA_DIR, keep=args.keep_db)
    except ScaleDatabaseUnavailable as exc:
        payload = {"status": "blocked", "reason": str(exc), "data": integrity,
                   "environment_note": "临时库无法建立 → SQL 侧规模与并发档均为 not_attempted"}
        save_v2("sql_scale", payload, command=" ".join(sys.argv))
        print(f"SQL 侧规模测量阻塞：{exc}")
        return 1
    points, index_ab, failure = [], {}, None
    try:
        for tier in tiers:
            # 同一临时库里逐档重建：只装入当前档的行数，避免把大档数据算进小档。
            reload_tier(handle, tier, DATA_DIR)
            handle["facts"].setdefault("tiers_loaded", {})[str(tier)] = table_counts(handle)
            for query in FIXED_QUERIES:
                result = run_child(V2_SQL_CHILD, {"dsn": handle["dsn_readonly"],
                                                  "sql": query["sql"], "params": query["params"],
                                                  "samples": args.samples, "warmups": 1})
                point = {"tier_order_items": tier, "query": query["id"], "label": query["label"],
                         "connect_ms": round(result["connect_ms"], 3), "rows": result["rows"],
                         "peak_rss_bytes": result["peak_rss_bytes"]}
                point.update(summarize(result["samples_ms"]))
                points.append(point)
                if not args.json_only:
                    print(f"SQL {tier:>7} 行 {query['id']:<20} p50={point['p50_ms']:>9.3f} ms "
                          f"p95={point['p95_ms']:>9.3f} ms n={point['n']} rows={point['rows']}")
        if not args.no_index_ab:
            index_ab = measure_index_ab(handle, args)
    except Exception as exc:                      # 如实记录，不静默
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        if not args.keep_db:
            handle["facts"]["drop"] = drop_scale_db(handle["name"])
        else:
            handle["facts"]["drop"] = {"dropped": False, "kept": True}
    payload = {"status": "measured" if failure is None else "failed",
               "failure_reason": failure, "data": integrity, "database": handle["facts"],
               "queries": [dict(query) for query in FIXED_QUERIES],
               "samples_per_point": args.samples, "points": points,
               "index_ab": index_ab,
               "notes": ["execute 段是 PostgreSQL 真实执行（readonly_user 只读连接），不含模型生成 SQL 的耗时",
                         "同一临时库内逐档重建数据（TRUNCATE+COPY）；每档独立生成，档位间只差规模",
                         "每个点一个全新子进程：峰值 RSS 含该进程的导入开销"]}
    save_v2("sql_scale", payload, command=" ".join(sys.argv))
    if not args.json_only:
        print(f"\n报告：{V2_REPORT.relative_to(ROOT)}")
    return 1 if failure else 0


def table_counts(handle: dict) -> dict:
    module = _scale_module()
    with psycopg2_connect(handle["dsn_admin"]) as conn, conn.cursor() as cur:
        counts = {}
        for table in module.TABLE_ORDER:
            cur.execute(f"SELECT count(*) FROM {table}")
            counts[table] = cur.fetchone()[0]
    return counts


def reload_tier(handle: dict, tier: int, data_dir: Path) -> None:
    module = _scale_module()
    with psycopg2_connect(handle["dsn_admin"]) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE " + ", ".join(reversed(module.TABLE_ORDER)) + " RESTART IDENTITY")
        for table in module.TABLE_ORDER:
            columns = ",".join(module.COPY_COLUMNS[table])
            with (data_dir / str(tier) / f"{table}.tsv").open("r", encoding="utf-8") as fp:
                cur.copy_expert(
                    f"COPY {table}({columns}) FROM STDIN WITH (FORMAT text, NULL '\\N')", fp)
        cur.execute("ANALYZE")


def measure_index_ab(handle: dict, args) -> dict:
    """在最大的档位上做一次真实、可回退的优化对照：只有主键 vs 演示库索引集。"""
    module = _scale_module()
    tier = handle["facts"]["tier_order_items"]
    reload_tier(handle, tier, DATA_DIR)          # 对照必须建立在最大档上，与 --tiers 顺序无关
    measurements: dict[str, dict] = {}

    def snapshot(label: str) -> dict:
        block = {}
        for query in FIXED_QUERIES:
            result = run_child(V2_SQL_CHILD, {"dsn": handle["dsn_readonly"], "sql": query["sql"],
                                              "params": query["params"],
                                              "samples": args.samples, "warmups": 1})
            block[query["id"]] = summarize(result["samples_ms"])
        measurements[label] = block
        return block

    before = snapshot("before")
    with psycopg2_connect(handle["dsn_admin"]) as conn, conn.cursor() as cur:
        cur.execute("SELECT indexname FROM pg_indexes WHERE schemaname='public' ORDER BY indexname")
        handle["facts"]["indexes_before"] = [row[0] for row in cur.fetchall()]
        for statement in module.PRODUCT_INDEX_DDL:
            cur.execute(statement)
        cur.execute("ANALYZE")
        cur.execute("SELECT indexname FROM pg_indexes WHERE schemaname='public' ORDER BY indexname")
        handle["facts"]["indexes_after"] = [row[0] for row in cur.fetchall()]
    after = snapshot("after")
    with psycopg2_connect(handle["dsn_admin"]) as conn, conn.cursor() as cur:
        for statement in module.PRODUCT_INDEX_DDL:
            name = statement.split(" ON ")[0].split()[-1]
            cur.execute(f"DROP INDEX IF EXISTS {name}")
        cur.execute("SELECT indexname FROM pg_indexes WHERE schemaname='public' ORDER BY indexname")
        handle["facts"]["indexes_reverted"] = [row[0] for row in cur.fetchall()]
    reverted = snapshot("reverted")
    deltas = {}
    for query in FIXED_QUERIES:
        key = query["id"]
        deltas[key] = {
            "p50_before_ms": before[key]["p50_ms"], "p50_after_ms": after[key]["p50_ms"],
            "p50_reverted_ms": reverted[key]["p50_ms"],
            "delta_p50_ms": round(before[key]["p50_ms"] - after[key]["p50_ms"], 3)
            if before[key]["p50_ms"] is not None else None,
            "p95_before_ms": before[key]["p95_ms"], "p95_after_ms": after[key]["p95_ms"],
            "p95_reverted_ms": reverted[key]["p95_ms"],
            "delta_p95_ms": round(before[key]["p95_ms"] - after[key]["p95_ms"], 3)
            if before[key]["p95_ms"] is not None else None,
            "speedup_p50": ratio(before[key]["p50_ms"], after[key]["p50_ms"])
            if after[key]["p50_ms"] else "N/A",
        }
    return {"tier_order_items": tier, "indexes": list(module.PRODUCT_INDEX_DDL),
            "index_source": module.INDEX_SOURCE, "samples_per_label": args.samples,
            "measurements": measurements, "deltas": deltas,
            "revert": handle["facts"].get("indexes_reverted"),
            "caveat": ("before 组同时承担冷缓存与冷计划器统计的代价，after 组偏快；reverted 组在 after "
                       "之后执行，因此「回退」读数同时说明差距主要来自索引而不是缓存"
                       "（若 reverted ≈ before 而 after 明显更快，则索引是主因）")}


# ------------------------------------------------------------ documents 模式

def build_document(path: Path, pages: int) -> dict:
    """用仓库现有 PDF 拼接出 `pages` 页的确定性文档（只写临时目录）。"""
    import pymupdf
    sources = [ROOT / name for name in DOC_SOURCES]
    for source in sources:
        if not source.is_file():
            raise ScaleDatabaseUnavailable(f"仓库缺少文档源：{source}")
    out = pymupdf.open()
    source_pages = 0
    cycle = 0
    while len(out) < pages:
        for source in sources:
            with pymupdf.open(source) as document:
                out.insert_pdf(document)
            source_pages += 1
        cycle += 1
        if cycle > pages:                                  # 防御：源 PDF 全为空页
            break
    if len(out) > pages:
        out.delete_pages(range(pages, len(out)))
    out.save(path)
    page_count = len(out)
    out.close()
    return {"pages": page_count, "sources": [str(Path(name).name) for name in DOC_SOURCES],
            "bytes": path.stat().st_size, "cycles": cycle}


def run_documents(args) -> int:
    sizes = [int(item) for item in args.doc_sizes.split(",") if item.strip()]
    samples_per_size = [int(item) for item in args.doc_samples.split(",") if item.strip()]
    if len(samples_per_size) != len(sizes):
        samples_per_size = [samples_per_size[-1]] * len(sizes)
    points = []
    for size, samples in zip(sizes, samples_per_size):
        parse_ms, slice_ms, total_ms, meta = [], [], [], {}
        blocked = None
        for index in range(samples):
            with tempfile.TemporaryDirectory() as directory:
                directory = Path(directory)
                pdf = directory / f"scale-{size}-{index}.pdf"
                meta = build_document(pdf, size)
                image_dir = directory / "images"
                image_dir.mkdir()
                try:
                    result = run_child(V2_DOC_CHILD,
                                       {"path": str(pdf), "doc_id": f"scale-doc-{size}",
                                        "image_dir": str(image_dir)},
                                       timeout=args.timeout)
                except ScaleDatabaseUnavailable as exc:
                    blocked = str(exc)
                    break
                parse_ms.append(result["parse_ms"])
                slice_ms.append(result["slice_ms"])
                total_ms.append(result["parse_ms"] + result["slice_ms"])
                meta.update({key: result[key] for key in
                             ("pages", "blocks", "chunks", "pages_rendered",
                              "peak_rss_bytes")})
        if blocked:
            points.append({"pages_requested": size, "status": "blocked", "reason": blocked,
                           "n": 0, "parse": summarize([]), "slice": summarize([]),
                           "total_ms": summarize([])})
            continue
        point = {"pages_requested": size, "document": meta, "status": "measured"}
        point["parse"] = summarize(parse_ms)
        point["slice"] = summarize(slice_ms)
        point["total_ms"] = summarize(total_ms)
        points.append(point)
        if not args.json_only:
            print(f"文档 {size:>4} 页: 解析 p50={point['parse']['p50_ms']} ms "
                  f"切片 p50={point['slice']['p50_ms']} ms n={point['parse']['n']} "
                  f"块={meta.get('blocks')} 切片={meta.get('chunks')}")
    payload = {"status": "measured", "points": points, "samples_per_point": samples_per_size,
               "sources": list(DOC_SOURCES),
               "notes": ["每档由仓库现有 PDF 循环拼接而成：内容重复会放大缓存命中与表格识别的稳定性",
                         "截图写入临时目录（ingest.PAGE_IMG_DIR 指向 tempdir），不写 data/object_store",
                         "解析=parse_pdf（含每页 dpi=110 位图渲染），切片=build_chunks；不含入库与向量化",
                         "500 页档样本数为 1，p50/p95 同为该次观测值"]}
    save_v2("documents", payload, command=" ".join(sys.argv))
    return 0


# ---------------------------------------------------------- concurrency 模式

def run_concurrency(args) -> int:
    workers_list = [int(item) for item in args.workers.split(",") if item.strip()]
    try:
        handle = setup_scale_db(args.scale_tier, DATA_DIR, keep=args.keep_db)
    except ScaleDatabaseUnavailable as exc:
        payload = {"status": "blocked", "reason": str(exc),
                   "environment_note": "临时库无法建立 → 并发矩阵 not_attempted"}
        save_v2("concurrency", payload, command=" ".join(sys.argv))
        print(f"并发测量阻塞：{exc}")
        return 1
    points = []
    try:
        for workers in workers_list:
            payload = {"dsn": handle["dsn_readonly"], "queries": [dict(q) for q in FIXED_QUERIES],
                       "iterations": args.queries_per_worker, "warmups": 1}
            scripts = []
            processes = []
            begin = time.perf_counter()
            try:
                for _ in range(workers):
                    script = ROOT / "tmp" / f"wp8_worker_{uuid.uuid4().hex[:8]}.py"
                    script.parent.mkdir(parents=True, exist_ok=True)
                    script.write_text(V2_CONC_WORKER, encoding="utf-8")
                    scripts.append(script)
                    processes.append(subprocess.Popen(
                        [sys.executable, str(script), json.dumps(payload), str(ROOT)],
                        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                        env={**os.environ, "PYTHONPATH": str(ROOT)}))
                results = []
                for process in processes:
                    stdout, stderr = process.communicate(timeout=args.timeout)
                    try:
                        results.append(json.loads(stdout.strip().splitlines()[-1]))
                    except Exception:
                        results.append({"ok": False, "queries": 0,
                                        "error": (stderr or stdout).strip()[-200:] or "no output"})
            finally:
                for script in scripts:
                    script.unlink(missing_ok=True)
            wall_s = time.perf_counter() - begin
            point = concurrency_point(workers, args.queries_per_worker, results, wall_s)
            points.append(point)
            if not args.json_only:
                print(f"并发 {workers}: 成功率={point['request_success_rate']} "
                      f"每秒完成数={point['completed_per_second']} "
                      f"峰值内存={point['peak_rss_bytes_max']} wall={point['wall_s']}s")
    finally:
        if not args.keep_db:
            handle["facts"]["drop"] = drop_scale_db(handle["name"])
        else:
            handle["facts"]["drop"] = {"dropped": False, "kept": True}
    payload = {"status": "measured", "database": handle["facts"], "points": points,
               "repetitions_per_point": 1,
               "notes": ["每个 worker 是独立子进程 + 独立只读连接；warmups=1 次不计入计时",
                         "每秒完成数 = 本点完成的查询数 / 墙钟时间（单次重复，不构成吞吐承诺）",
                         "峰值内存按 worker 分别取 ru_maxrss 后报告 max 与 sum；不是 cgroup 口径",
                         "success_rate 零分母为 \"N/A\"；失败保留在分母里"]}
    save_v2("concurrency", payload, command=" ".join(sys.argv))
    return 0


def concurrency_point(workers: int, iterations: int, results: list, wall_s: float) -> dict:
    """One concurrency matrix cell. `N/A` whenever the denominator is zero; failures stay in it."""
    ok_workers = sum(1 for result in results if result.get("ok"))
    completed = sum(result.get("queries") or 0 for result in results if result.get("ok"))
    peaks = [result.get("peak_rss_bytes") or 0 for result in results]
    return {"workers": workers, "iterations_per_worker": iterations,
            "requests_planned": workers * iterations,
            "requests_completed": completed,
            "request_success_rate": ratio(completed, workers * iterations),
            "workers_started": workers, "workers_succeeded": ok_workers,
            "worker_success_rate": ratio(ok_workers, workers),
            "failed": workers - ok_workers,
            "wall_s": round(wall_s, 3),
            "completed_per_second": ratio(completed, wall_s),
            "peak_rss_bytes_max": max(peaks) if peaks else None,
            "peak_rss_bytes_sum_over_workers": sum(peaks),
            "worker_elapsed_ms": [round(result.get("elapsed_ms") or 0, 1) for result in results],
            "errors": [result.get("error") for result in results if not result.get("ok")],
            "repetitions": 1}


# -------------------------------------------------------------- profile 模式

def profile_sections(segments_ms: dict) -> dict:
    """Raw per-round segment timings → cold/warm summaries, plus the *empty* model segments.

    Cold is the first call in that child process (imports, connections, model load); warm is
    every later call. The model/first-token/complete-response segments are `null` with a reason
    — never 0 — because no upstream model was called in this run.
    """
    segments = {}
    for name, values in segments_ms.items():
        warm = values[1:] if len(values) > 1 else []
        section = summarize(warm)
        section["cold_ms"] = round(values[0], 3) if values else None
        section["cold_definition"] = ("该子进程第一次调用，含模块导入/建连/模型加载；"
                                      "catalog_build 只在第一次测量")
        segments[name] = section
    segments["model"] = null_segment(MODEL_SEGMENT_REASON)
    segments["first_token"] = null_segment(MODEL_SEGMENT_REASON)
    segments["complete_response"] = null_segment(MODEL_SEGMENT_REASON + "（完整响应含模型生成）")
    return segments


def run_profile(args) -> int:
    try:
        handle = setup_scale_db(args.scale_tier, DATA_DIR, keep=args.keep_db)
    except ScaleDatabaseUnavailable as exc:
        payload = {"status": "blocked", "reason": str(exc),
                   "segments": {"model": null_segment(MODEL_SEGMENT_REASON)},
                   "environment_note": "临时库无法建立 → 执行段 not_attempted"}
        save_v2("profile", payload, command=" ".join(sys.argv))
        print(f"分段计时阻塞：{exc}")
        return 1
    try:
        from datamind.knowledge import COLLECTIONS
        try:
            result = run_child(V2_PROFILE_CHILD, {
                "meta_dsn": (os.environ.get("DATAMIND_META_DSN") or
                             "host=127.0.0.1 port=5432 dbname=datamind_meta user=readonly_user "
                             "password=readonly_dev"),
                "scale_dsn": handle["dsn_readonly"],
                "questions": list(PROFILE_QUESTIONS) * max(args.profile_repeats, 1),
                "collections": sorted(COLLECTIONS),
                # 执行段用无参数的组合查询：本轮没有模型生成 SQL，参数化查询由 sql 模式覆盖
                "scale_sql": next(query["sql"] for query in FIXED_QUERIES
                                  if query["id"] == "q3_three_table_join")},
                timeout=args.timeout)
        except ScaleDatabaseUnavailable as exc:
            payload = {"status": "blocked", "reason": str(exc),
                       "segments": {"model": null_segment(MODEL_SEGMENT_REASON),
                                    "first_token": null_segment(MODEL_SEGMENT_REASON),
                                    "complete_response": null_segment(MODEL_SEGMENT_REASON)},
                       "database": handle["facts"]}
            save_v2("profile", payload, command=" ".join(sys.argv))
            print(f"分段计时失败：{exc}")
            return 1
        segments = profile_sections(result["segments_ms"])
        payload = {"status": "measured_toolchain_only", "database": handle["facts"],
                   "questions": list(PROFILE_QUESTIONS),
                   "questions_per_round": len(PROFILE_QUESTIONS),
                   "rounds": max(args.profile_repeats, 1),
                   "samples_per_segment": {"cold": 1,
                                           "warm": max(len(PROFILE_QUESTIONS)
                                                       * max(args.profile_repeats, 1) - 1, 0)},
                   "segments": segments, "detail": result["detail"],
                   "peak_rss_bytes": result["peak_rss_bytes"],
                   "notes": ["分段为工具链实测：路由→Catalog 召回→词法/混合检索→SQL 执行",
                             "执行段跑的是固定 SQL（无模型生成 SQL）；模型段/首字/完整响应留空",
                             "检索段包含 BGE 查询嵌入与 Qdrant，冷启动含模型加载",
                             f"模型段留空原因：{MODEL_SEGMENT_REASON}"]}
    finally:
        if not args.keep_db:
            handle["facts"]["drop"] = drop_scale_db(handle["name"])
        else:
            handle["facts"]["drop"] = {"dropped": False, "kept": True}
    save_v2("profile", payload, command=" ".join(sys.argv))
    if not args.json_only:
        for name in ("route", "catalog_build", "catalog_recall", "knowledge_retrieve",
                     "scale_connect", "execute_sql", "toolchain_total"):
            section = segments[name]
            print(f"分段 {name:<18} 冷={section['cold_ms']} 预热 p50={section['p50_ms']} "
                  f"p95={section['p95_ms']} n={section['n']}")
        print("分段 model/first_token/complete_response: 留空（null），未调用上游模型")
    return 0


# --------------------------------------------------- profile_live 模式（R3 新增）

#: 真实端到端会话分段的固定题集：两条 SQL 路径 + 两条 RAG 路径（RAG 侧固定到单份文档，
#: 避免检索随机性把分段时延和「命中哪份文档」混在一起）。
PROFILE_LIVE_QUESTIONS = (
    {"id": "live-sql-orders", "route": "sql", "question": "2024年一共有多少笔订单？",
     "document_title": None},
    {"id": "live-sql-region", "route": "sql", "question": "华东地区的客户下了多少笔订单？",
     "document_title": None},
    {"id": "live-rag-refund", "route": "rag", "question": "一般商品退款多久到账？",
     "document_title": "04_售后服务与退款管理制度"},
    {"id": "live-rag-warranty", "route": "rag", "question": "无线降噪耳机Pro整机保修多长时间？",
     "document_title": "09_无线降噪耳机Pro产品手册"},
)
FINAL_EVENT_TYPES = ("answer.completed", "answer.abstained", "answer.clarification", "run.failed")
PROFILE_LIVE_FIRST_TOKEN_NOTE = (
    "上游模型 `stream=False`（datamind/providers.py），没有 token 级流式；因此「首字」定义为"
    "客户端收到第一个 SSE trace 事件（首个可见输出）的耗时，不等于模型首 token 时延。")


class ProfileLiveBlocked(RuntimeError):
    """真实会话不可用——如实写 blocked，不用 mock 或 0 顶替。"""


def profile_live_reference(document_title: str) -> dict:
    """只读取出题集里指定文档的版本化引用（document_id/version/sha256/parser_version）。"""
    import psycopg2
    from datamind.knowledge import meta_dsn

    with psycopg2.connect(meta_dsn()) as conn, conn.cursor() as cur:
        cur.execute("""SELECT document_id, doc_version, sha256, parser_version, status,
                              (SELECT count(*) FROM chunks c WHERE c.document_id=a.document_id)
                       FROM document_assets a WHERE collection='demo_kb' AND title=%s""",
                    (document_title,))
        row = cur.fetchone()
    if not row:
        raise ProfileLiveBlocked(f"document_not_found: {document_title}")
    document_id, version, sha, parser, status, chunks = row
    if status != "indexed" or not chunks:
        raise ProfileLiveBlocked(f"document_not_searchable: {document_title} status={status} chunks={chunks}")
    return {"document_id": document_id, "document_version": version, "source_sha256": sha,
            "parser_version": parser}


def profile_live_session(base_url: str, timeout: float, username: str | None = None,
                         password: str | None = None) -> dict:
    """回环免登录身份（只读 `GET /v1/auth/me` 取 CSRF）或显式账号密码登录。"""
    import http.cookiejar
    import urllib.error
    import urllib.request

    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    if username and password:
        request = urllib.request.Request(
            base_url + "/v1/auth/login",
            data=json.dumps({"username": username, "password": password}).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with opener.open(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode())
        except urllib.error.HTTPError as error:
            raise ProfileLiveBlocked(f"login_failed: http={error.code}")
        return {"base_url": base_url, "opener": opener, "csrf": payload["csrf_token"],
                "identity_source": "explicit_login", "username": username}
    try:
        with urllib.request.urlopen(base_url + "/v1/auth/me", timeout=timeout) as response:
            payload = json.loads(response.read().decode())
    except Exception as exc:
        raise ProfileLiveBlocked(f"auth_me_failed: {type(exc).__name__}: {exc}")
    csrf = (payload or {}).get("csrf_token")
    if not csrf:
        raise ProfileLiveBlocked("auth_me_without_csrf_token")
    return {"base_url": base_url, "opener": opener, "csrf": csrf,
            "identity_source": "loopback_no_login",
            "username": ((payload or {}).get("user") or {}).get("username")}


def profile_live_stream(session: dict, question: str, reference: dict | None,
                        timeout: float) -> dict:
    """一次流式问答：记录每个 SSE trace 事件的到达时刻（客户端单调时钟）。"""
    import urllib.error
    import urllib.request

    payload: dict = {"question": question, "mode": "auto"}
    if reference:
        payload["scope"] = {"document_collections": ["demo_kb"], "document_refs": [reference]}
    request = urllib.request.Request(
        session["base_url"] + "/v1/chat/stream", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream",
                 "X-CSRF-Token": session["csrf"]})
    started = time.monotonic()
    events: list = []
    data_lines: list = []
    try:
        with session["opener"].open(request, timeout=timeout) as response:
            for raw in response:
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if line.startswith("data:"):
                    data_lines.append(line[5:].strip())
                    continue
                if line.strip():
                    continue
                if data_lines:
                    try:
                        body = json.loads("".join(data_lines))
                    except ValueError:
                        body = None
                    if isinstance(body, dict):
                        events.append({"arrival_ms": round((time.monotonic() - started) * 1000, 1),
                                       "event_type": body.get("event_type"),
                                       "status": body.get("status"),
                                       "error_code": body.get("error_code"),
                                       "body": body})
                    data_lines = []
    except Exception as exc:
        return {"wall_ms": round((time.monotonic() - started) * 1000, 1), "events": events,
                "transport_error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    return {"wall_ms": round((time.monotonic() - started) * 1000, 1), "events": events,
            "transport_error": None}


def profile_live_segments(stream: dict) -> dict:
    """把 SSE 事件切成分段耗时（毫秒）。缺失的段一律 None，不填 0。"""
    events = stream.get("events") or []
    first = {}
    for event in events:
        first.setdefault(event["event_type"], event)
    final = next((event for event in events if event["event_type"] in FINAL_EVENT_TYPES), None)
    route_event = first.get("query.parsed")
    tool_event = first.get("tool.started")
    retrieval_event = first.get("retrieval.completed")
    validated_event = first.get("sql.validated")
    executed_event = first.get("sql.executed")
    final_payload = ((final or {}).get("body") or {}).get("output_summary") or {}
    route = ((route_event or {}).get("body") or {}).get("output_summary", {}).get("route")
    if route not in {"sql", "rag", "cross_source", "industrial"}:
        route = "rag" if "rag_result" in final_payload else ("sql" if "sql_result" in final_payload
                                                             else route)
    segments = {
        "route_ms": route_event["arrival_ms"] if route_event else None,
        "recall_ms": (retrieval_event["arrival_ms"] - tool_event["arrival_ms"]
                      if retrieval_event and tool_event else None),
        "execute_ms": (executed_event["arrival_ms"] - validated_event["arrival_ms"]
                       if executed_event and validated_event else None),
        "model_ms": None,
        "first_token_ms": events[0]["arrival_ms"] if events else None,
        "complete_response_ms": stream.get("wall_ms"),
    }
    if retrieval_event and final and route == "rag":
        segments["model_ms"] = round(final["arrival_ms"] - retrieval_event["arrival_ms"], 1)
    elif tool_event and validated_event:
        # SQL 路径：模型生成 SQL 的时段（tool.started → sql.validated 之间没有别的可观测边界）
        segments["model_ms"] = round(validated_event["arrival_ms"] - tool_event["arrival_ms"], 1)
    elif tool_event and final:
        segments["model_ms"] = round(final["arrival_ms"] - tool_event["arrival_ms"], 1)
    engine = (final_payload.get("sql_result") or {}).get("elapsed_ms") if route == "sql" else \
        (final_payload.get("rag_result") or {}).get("elapsed_ms")
    return {"route": route, "segments_ms": segments,
            "engine_reported_elapsed_ms": engine,
            "final_status": final_payload.get("status"),
            "final_error_code": final_payload.get("error_code"),
            "event_types": [event["event_type"] for event in events]}


def profile_live_segment_summary(values, *, reason: str) -> dict:
    if not values:
        return null_segment(reason)
    values = [float(value) for value in values]
    section = summarize(values)
    section["cold_ms"] = round(values[0], 3)
    section["cold_definition"] = "该题集第一轮第一次调用（含建连/首轮检索缓存冷启动）"
    return section


def run_profile_live(args) -> int:
    base_url = getattr(args, "profile_live_base_url", "http://127.0.0.1:8000").rstrip("/")
    rounds = max(int(getattr(args, "profile_live_repeats", 3)), 1)
    wanted = [item.strip() for item in (getattr(args, "profile_live_questions", "") or "").split(",")
              if item.strip()]
    questions = [item for item in PROFILE_LIVE_QUESTIONS if not wanted or item["id"] in wanted]
    if not questions:
        raise SystemExit("--profile-live-questions 没有匹配到任何题面")
    try:
        session = profile_live_session(
            base_url, getattr(args, "profile_live_timeout", 180.0),
            getattr(args, "profile_live_username", None),
            getattr(args, "profile_live_password", None))
    except ProfileLiveBlocked as exc:
        payload = {"status": "blocked", "reason": str(exc), "base_url": base_url,
                   "segments": {name: null_segment(f"会话不可用：{exc}") for name in
                                ("route", "recall", "execute", "model", "first_token",
                                 "complete_response")},
                   "environment_note": "拿不到真实会话 → 六段全部 not_attempted，不用 mock 或 0 填充"}
        save_v2("profile_live", payload, command=" ".join(sys.argv))
        print(f"真实会话分段阻塞：{exc}")
        return 1
    call_timeout = float(getattr(args, "profile_live_timeout", 180.0))
    calls = []
    for round_index in range(1, rounds + 1):
        for item in questions:
            reference = (profile_live_reference(item["document_title"])
                         if item.get("document_title") else None)
            stream = profile_live_stream(session, item["question"], reference, call_timeout)
            sliced = profile_live_segments(stream)
            calls.append({"round": round_index, "id": item["id"], "expected_route": item["route"],
                          "question": item["question"], "document_title": item.get("document_title"),
                          "wall_ms": stream.get("wall_ms"),
                          "transport_error": stream.get("transport_error"),
                          "event_count": len(stream.get("events") or []),
                          **sliced})
            print(f"[{round_index}/{rounds}] {item['id']:<18} route={sliced['route']} "
                  f"首字={sliced['segments_ms']['first_token_ms']}ms "
                  f"完整={sliced['segments_ms']['complete_response_ms']}ms "
                  f"状态={sliced['final_status']}", flush=True)
    by_route: dict = {}
    for route in ("sql", "rag"):
        subset = [call for call in calls if call["route"] == route]
        if not subset:
            continue
        by_route[route] = {
            "calls": len(subset),
            **{name: profile_live_segment_summary(
                [call["segments_ms"][name] for call in subset if call["segments_ms"][name] is not None],
                reason=f"{route} 路径没有该分段")
               for name in ("route_ms", "recall_ms", "execute_ms", "model_ms", "first_token_ms",
                            "complete_response_ms")},
            "engine_reported_elapsed_ms": profile_live_segment_summary(
                [call["engine_reported_elapsed_ms"] for call in subset
                 if call["engine_reported_elapsed_ms"] is not None],
                reason=f"{route} 路径没有引擎自报耗时"),
        }
    segments = {}
    for name, reason in (("route", "没有 route 样本"), ("recall", "SQL 路径没有召回段"),
                         ("execute", "RAG 路径没有 SQL 执行段"), ("model", "没有 model 样本"),
                         ("first_token", "没有 first_token 样本"),
                         ("complete_response", "没有 complete_response 样本")):
        values = [call["segments_ms"][f"{name}_ms"] for call in calls
                  if call["segments_ms"].get(f"{name}_ms") is not None]
        segments[name] = profile_live_segment_summary(values, reason=reason)
    transport_errors = [call for call in calls if call.get("transport_error")]
    payload = {
        "status": "measured_live_end_to_end" if not transport_errors else "measured_with_transport_errors",
        "session": {"identity_source": session["identity_source"], "username": session["username"],
                    "base_url": base_url,
                    "note": "回环免登录身份：GET /v1/auth/me 只读取 CSRF；POST /v1/chat/stream 会让服务端"
                            "幂等确保 user-local-admin 审计行存在（只落 datamind_meta 身份表，不碰演示库）"},
        "questions": [item["id"] for item in questions],
        "rounds": rounds,
        "calls_total": len(calls),
        "samples_per_segment": {name: segments[name]["n"] for name in segments},
        "segments": segments,
        "by_route": by_route,
        "api_version": api_version(),
        "notes": [
            "六段口径：路由=请求发出→query.parsed；召回=RAG 的 tool.started→retrieval.completed；"
            "执行=SQL 的 sql.validated→sql.executed；模型=RAG 的 retrieval.completed→终态事件、"
            "SQL 的 tool.started→sql.validated（生成 SQL 段）；首字/完整响应为客户端实测",
            PROFILE_LIVE_FIRST_TOKEN_NOTE,
            "全部为串行单并发调用；n<20 时 p95 等于观测最大值（最近秩法）",
            f"上游/传输失败：{len(transport_errors)}/{len(calls)}",
        ],
        "transport_error_calls": [{"round": call["round"], "id": call["id"],
                                   "transport_error": call["transport_error"]} for call in transport_errors],
        "calls": calls,
        "peak_rss_bytes": _peak_rss_bytes(),
    }
    save_v2("profile_live", payload, command=" ".join(sys.argv))
    if not args.json_only:
        for name in ("route", "recall", "execute", "model", "first_token", "complete_response"):
            section = segments[name]
            print(f"真实会话分段 {name:<18} n={section['n']} p50={section['p50_ms']} "
                  f"p95={section['p95_ms']}")
    return 1 if transport_errors else 0


# -------------------------------------------------------- retrieval_scan 模式

def run_retrieval_scan(args) -> int:
    sizes = [int(item) for item in args.scan_sizes.split(",") if item.strip()]
    try:
        result = run_child(V2_SCAN_CHILD, {"sizes": sizes, "samples": args.samples,
                                           "question": SCAN_QUESTION,
                                           "repeat": args.scan_repeat}, timeout=args.timeout)
    except ScaleDatabaseUnavailable as exc:
        payload = {"status": "blocked", "reason": str(exc)}
        save_v2("retrieval_scan", payload, command=" ".join(sys.argv))
        print(f"候选扫描测量阻塞：{exc}")
        return 1
    points = []
    for item in result["points"]:
        point = {"size_chunks": item["size"], "n": len(item["full_ms"]),
                 "row_chars": item["row_chars"], "hot_every": item["hot_every"],
                 "product_lexical_ranking": summarize(item["full_ms"]),
                 "pruned_prototype": summarize([value for value in item["pruned_ms"]]),
                 "prune_prefilter_ms": round(item["prefilter_ms"], 3),
                 "kept_rows": item["kept_rows"],
                 "full_scored_rows": item["full_scored_rows"],
                 "pruned_scored_rows": item["pruned_scored_rows"],
                 "candidate_set_equal": item["candidate_set_equal"],
                 "top5_equal": item["top5_equal"],
                 "full_top5": item["full_top5"], "pruned_top5": item["pruned_top5"]}
        total_pruned = [value + item["prefilter_ms"] for value in item["pruned_ms"]]
        point["pruned_total_p50_ms"] = percentile(total_pruned, 0.5)
        point["pruned_total_p95_ms"] = percentile(total_pruned, 0.95)
        point["speedup_p50"] = ratio(point["product_lexical_ranking"]["p50_ms"],
                                     point["pruned_total_p50_ms"])
        point["ms_per_1000_rows_full"] = round(
            (point["product_lexical_ranking"]["p50_ms"] or 0) / item["size"] * 1000, 3)
        points.append(point)
        if not args.json_only:
            print(f"候选扫描 {item['size']:>6} 行: 产品打分 p50={point['product_lexical_ranking']['p50_ms']} ms "
                  f"剪枝后 p50={point['pruned_total_p50_ms']} ms "
                  f"top5相同={point['top5_equal']} 候选集相同={point['candidate_set_equal']}")
    payload = {"status": "measured", "query": SCAN_QUESTION, "query_tokens": result["query_tokens"],
               "points": points, "samples_per_point": args.samples,
               "peak_rss_bytes": result["peak_rss_bytes"],
               "notes": ["被测函数是产品真实代码 datamind.knowledge.lexical_ranking（逐行 tokenize + BM25 打分，O(n)）",
                         "剪枝原型只在本脚本内，不写回产品代码（datamind/knowledge.py 不在 WP8 允许改动的文件内）",
                         "被剪掉的行不可能进入打分（查询 token 一定是文本子串），但 BM25 的长度均值会变 → "
                         "只声称候选集不变 + 实测 top-5 相同，不声称分数逐位相同",
                         "合成行按真实 row 形状构造（title/section_path/exact_text），1/20 行含热词"]}
    save_v2("retrieval_scan", payload, command=" ".join(sys.argv))
    return 0


# ------------------------------------------------------------ locator_ab 模式

def load_locator_store(handle: dict, chunks: int, blocks_per_chunk: int = 4,
                       documents: int = 50) -> dict:
    """在临时库里放一份块定位探针数据；每个 chunk 的块都属于它自己的 document。"""
    import io
    per_document = max(chunks // documents, 1)
    blocks_per_document = max(per_document * 2, blocks_per_chunk)
    chunk_lines, block_lines = [], []
    for document in range(documents):
        for local in range(blocks_per_document):
            block_id = f"doc{document:03d}-blk{local:05d}"
            block_lines.append("\t".join((
                block_id, f"doc{document:03d}", str(1 + local % 20), '{"x":1,"y":2}',
                hashlib_sha(block_id), "0.900", "\\N", "pdf-ocr-0.4", str(local))))
    for index in range(chunks):
        document = index % documents
        local = index // documents
        block_ids = [f"doc{document:03d}-blk{(local * 2 + step) % blocks_per_document:05d}"
                     for step in range(blocks_per_chunk)]
        chunk_lines.append("\t".join((
            f"chk-{index:07d}", f"doc{document:03d}", "1", str(index), "{指标,口径}",
            str(1 + index % 20), str(1 + index % 20), "{" + ",".join(block_ids) + "}",
            "正文" * 40, hashlib_sha(f"chk-{index:07d}"), "{public}")))
    with psycopg2_connect(handle["dsn_admin"]) as conn, conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS scale_chunks, scale_blocks")
        cur.execute(LOCATOR_STORE_DDL)
        cur.copy_expert("COPY scale_blocks(block_id,document_id,page_no,bbox,content_hash,"
                        "ocr_confidence,formula_latex,parser_version,reading_order) "
                        "FROM STDIN WITH (FORMAT text, NULL '\\N')", io.StringIO(
                            "\n".join(block_lines) + "\n"))
        cur.copy_expert("COPY scale_chunks(chunk_id,document_id,document_version,chunk_index,"
                        "section_path,page_start,page_end,block_ids,exact_text,content_hash,acl) "
                        "FROM STDIN WITH (FORMAT text, NULL '\\N')", io.StringIO(
                            "\n".join(chunk_lines) + "\n"))
        cur.execute("ANALYZE scale_blocks")
        cur.execute("ANALYZE scale_chunks")
        cur.execute("GRANT SELECT ON scale_blocks, scale_chunks TO readonly_user")
    return {"chunks": chunks, "blocks": len(block_lines), "blocks_per_chunk": blocks_per_chunk,
            "documents": documents, "blocks_per_document": blocks_per_document}


def hashlib_sha(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def run_locator_ab(args) -> int:
    sizes = [int(item) for item in args.locator_sizes.split(",") if item.strip()]
    try:
        handle = setup_scale_db(1, DATA_DIR, keep=args.keep_db, load_data=False)
    except ScaleDatabaseUnavailable as exc:
        payload = {"status": "blocked", "reason": str(exc),
                   "environment_note": "临时库无法建立 → 块定位对照 not_attempted"}
        save_v2("locator_ab", payload, command=" ".join(sys.argv))
        print(f"块定位对照阻塞：{exc}")
        return 1
    points = []
    try:
        for size in sizes:
            store = load_locator_store(handle, size)
            result = run_child(V2_LOCATOR_CHILD, {
                "dsn": handle["dsn_readonly"], "limit": size, "samples": args.samples,
                "per_row_sql": LOCATOR_QUERY_PER_ROW, "batched_sql": LOCATOR_QUERY_BATCHED},
                timeout=args.timeout)
            shapes = {item["shape"]: item for item in result["shapes"]}
            point = {"chunks": size, "store": store,
                     "per_row": summarize(shapes["per_row"]["samples_ms"]),
                     "batched": summarize(shapes["batched"]["samples_ms"]),
                     "rows": shapes["batched"]["rows"],
                     "rows_with_blocks": shapes["batched"]["rows_with_blocks"],
                     "equivalent": result["equivalent"],
                     "mismatch_count": result["mismatch_count"],
                     "mismatch_ids": result["mismatch_ids"],
                     "per_row_result_sha256": shapes["per_row"]["result_sha256"],
                     "batched_result_sha256": shapes["batched"]["result_sha256"]}
            point["delta_p50_ms"] = (round(point["per_row"]["p50_ms"] - point["batched"]["p50_ms"], 3)
                                     if point["batched"]["p50_ms"] is not None else None)
            point["delta_p95_ms"] = (round(point["per_row"]["p95_ms"] - point["batched"]["p95_ms"], 3)
                                     if point["batched"]["p95_ms"] is not None else None)
            point["speedup_p50"] = ratio(point["per_row"]["p50_ms"], point["batched"]["p50_ms"])
            points.append(point)
            if not args.json_only:
                print(f"块定位 {size:>5} 切片: 逐行 p50={point['per_row']['p50_ms']} ms → "
                      f"批量 p50={point['batched']['p50_ms']} ms "
                      f"(x{point['speedup_p50']}) 等价={point['equivalent']}")
    finally:
        if not args.keep_db:
            handle["facts"]["drop"] = drop_scale_db(handle["name"])
        else:
            handle["facts"]["drop"] = {"dropped": False, "kept": True}
    payload = {"status": "measured", "database": handle["facts"], "points": points,
               "samples_per_point": args.samples,
               "per_row_sql": LOCATOR_QUERY_PER_ROW.strip(), "batched_sql": LOCATOR_QUERY_BATCHED.strip(),
               "per_row_source": "datamind/knowledge.py:117-130（source_blocks 关联子查询，逐行执行）",
               "notes": ["A 组是产品当前形状（每个返回的 chunk 行跑一次子计划）；B 组是一次 JOIN + 聚合",
                         "B 组不是已发布代码：把它落到产品上要改 datamind/knowledge.py，不在 WP8 的允许文件内",
                         "两组的输出都做了 JSON 规范化比对（equivalent/mismatch_count），不是只看时间",
                         "探针数据装在临时库的 scale_blocks/scale_chunks 里，形状取自 document_blocks/chunks"]}
    save_v2("locator_ab", payload, command=" ".join(sys.argv))
    return 0


# ------------------------------------------------------------------- 入口

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entity-scales", default="100,1000,5000,20000")
    parser.add_argument("--json-only", action="store_true")
    parser.add_argument("--kind", default="v1", choices=list(V2_KINDS),
                        help="v1=既有 CAD+检索 扫描（默认，行为与输出字段不变）；其余为 WP8 新增")
    parser.add_argument("--tiers", default="1000,10000,100000",
                        help="SQL 侧档位（order_items 行数），必须已在 --data-dir 生成")
    parser.add_argument("--data-dir", default=str(DATA_DIR))
    parser.add_argument("--samples", type=int, default=5, help="每个测量点的样本数")
    parser.add_argument("--doc-sizes", default="10,100,500")
    parser.add_argument("--doc-samples", default="3,2,1")
    parser.add_argument("--workers", default="1,2,4,8")
    parser.add_argument("--queries-per-worker", type=int, default=8)
    parser.add_argument("--scan-sizes", default="500,1000,5000,10000")
    parser.add_argument("--scan-repeat", type=int, default=8, help="合成行文本长度倍数")
    parser.add_argument("--locator-sizes", default="1000,2000,5000",
                        help="块定位对照的切片数（每切片 4 块）")
    parser.add_argument("--profile-repeats", type=int, default=4,
                        help="分段计时把固定题集循环几轮（第 1 轮第 1 题为冷启动）")
    parser.add_argument("--scale-tier", type=int, default=100000,
                        help="并发/分段计时所用的最大档")
    parser.add_argument("--keep-db", action="store_true", help="保留临时库（默认跑完即删）")
    parser.add_argument("--no-index-ab", action="store_true", help="跳过索引优化前后对照")
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--profile-live", action="store_true",
                        help="profile 模式后追加真实端到端会话分段（写入 sections.profile_live）")
    parser.add_argument("--profile-live-base-url", default="http://127.0.0.1:8000",
                        help="真实服务地址（默认本机 8000 端口）")
    parser.add_argument("--profile-live-repeats", type=int, default=3,
                        help="真实会话分段把固定题集循环几轮")
    parser.add_argument("--profile-live-questions", default="",
                        help="只跑指定题 id（逗号分隔，空=全部）")
    parser.add_argument("--profile-live-username", default=None,
                        help="显式账号；缺省用回环免登录身份（只读 /v1/auth/me 取 CSRF）")
    parser.add_argument("--profile-live-password", default=None)
    parser.add_argument("--profile-live-timeout", type=float, default=180.0)
    return parser


def main() -> int:
    global DATA_DIR
    args = build_parser().parse_args()
    DATA_DIR = Path(args.data_dir) if Path(args.data_dir).is_absolute() else ROOT / args.data_dir

    if args.kind == "v1":
        return run_v1(args)
    failures = 0
    modes = (["sql", "locator_ab", "documents", "concurrency", "profile", "retrieval_scan"]
             if args.kind == "all" else [args.kind])
    for mode in modes:
        print(f"== --kind {mode} ==")
        code = {"sql": run_sql, "documents": run_documents, "concurrency": run_concurrency,
                "profile": run_profile, "profile_live": run_profile_live,
                "retrieval_scan": run_retrieval_scan,
                "locator_ab": run_locator_ab}[mode](args)
        failures += 1 if code else 0
        if mode == "profile" and getattr(args, "profile_live", False):
            print("== profile_live（真实端到端会话分段）==")
            failures += 1 if run_profile_live(args) else 0
    if not args.json_only:
        print(f"\n报告：{V2_REPORT.relative_to(ROOT)}（计划外失败模式数={failures}）")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
