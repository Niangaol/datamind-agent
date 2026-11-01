# -*- coding: utf-8 -*-
"""
ingest.py — 知识库摄取管线（里程碑 M6/M7 简化版）
流程: 登记文档资产 → 解析(块/表格/页图) → 标题树切片 → BGE 向量化 →
      Qdrant 入库 + PostgreSQL 元数据落库

说明:
- 扫描页只读取原 PDF 像素，通过本地 OCR 识别，不读取原生对照。
- OHR-Bench 样张仅摄取 gt_text；noise_text 保留在 manifest 供鲁棒性评测。

用法: py -3.13 ingestion/ingest.py [--reset]
"""
import argparse
import hashlib
import json
import uuid
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras
from psycopg2.extensions import make_dsn
import pymupdf
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from datamind import task_budget
from ingestion import formula, facts
KB_DIR = ROOT / "data" / "knowledge_base"
OHR_DIR = ROOT / "data" / "ohr_bench_samples"
PAGE_IMG_DIR = ROOT / "data" / "object_store" / "page_images"

PG = dict(host="localhost", port=5432, user="datamind_app",
          password="datamind_app_dev", dbname="datamind_meta")
META_DSN = make_dsn(**PG)
FORMULA_SLOTS = 1
OCR_SLOTS = 2
QDRANT = "http://localhost:6333"
COLLECTION = "datamind_knowledge"
EMBED_MODEL = "BAAI/bge-small-zh-v1.5"
EMBED_DIM = 512
EMBED_CACHE = ROOT / ".cache" / "fastembed"

CHUNK_TARGET = 500   # 字符（中文）
CHUNK_MAX = 800
CHUNK_OVERLAP = 80
PARSER_VERSION = "pdf-ocr-0.4"

# ---------- 摄取顺序协议（审计 F10 / outbox-lite，不引入新表） ----------
# 依据（实测，不是推测）：`document_assets` 上**没有** status 的 CHECK 约束
# （在库中查 pg_constraint 只得到 document_assets_pkey 与
# document_assets_collection_sha256_key），所以复用既有值即可，无需迁移。
# 对外可见性一律由 `status='indexed'` 过滤：datamind/knowledge.py、
# datamind/api.py（文档检索/统计）、datamind/authority.py、
# datamind/industrial.py、datamind/approvals.py、datamind/fact_reviews.py。
# 选 'parsing' 作"不可检索"状态：它是**本文件既有**写入的值
# （登记资产的 INSERT ... ON CONFLICT 已经写 'parsing'），且非 'indexed'，
# 因此任何检索入口都选不中它。不臆造新状态。
NON_SEARCHABLE_STATUS = "parsing"
SEARCHABLE_STATUS = "indexed"
FAILED_STATUS = "failed"

# ---------- F08：`document_assets.status` 的唯一取值来源与写入点 ----------
# 复核方法：只读 SQL 统计库内真实分布 + 通读本文件。非测试代码里写
# `document_assets.status` 的只有本文件与 db/seed_industrial.py；真实写入的取值恰好
# 三个：
#   'parsing' → 登记资产、发布第 1 步（不可检索）
#   'indexed' → 发布终态且至少落了一条切片（检索可见；读取侧的门就是它）
#   'failed'  → 发布终态但零切片（needs_review=TRUE）
# 本文件所有写 status 的语句都集中在下面 REGISTER_ASSET_SQL 与 mark_asset_status()
# 两处定义，取值只从 ASSET_STATUS_WRITES 取，改状态不必再翻遍全文。
ASSET_STATUS_WRITES = (NON_SEARCHABLE_STATUS, SEARCHABLE_STATUS, FAILED_STATUS)
#
# 契约侧缺口（**不由本文件修复**，供协调者转交 datamind/parse_contract.py 负责人）：
# 该模块的 INGEST_STATES/INGEST_TRANSITIONS 描述的是另一套生命周期
# ('queued'→'classifying'→'parsing'→'validating'→'indexing'→'ready')，其中
# **没有 'indexed'**，而 'indexed' 恰是真实可见性门（datamind/knowledge.py 等）
# 与摄取真正写入的终态。也就是说 validate_ingest_state('indexed') 会抛
# PARSE_STATUS_INVALID，契约状态机无法表达摄取的真实终态；另外 ingest_step()
# 在本仓库没有任何调用者。datamind/parse_contract.py 属其他子代理的文件，本文件
# 不越权修改，只在此显式记录缺口，并由 tests/test_ingest_contract_coverage.py 锁住。
INGEST_STATE_CONTRACT_GAPS = ("indexed",)


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()[:12]


# ============================================================
# 解析
# ============================================================
def detect_heading(span_size):
    if span_size >= 17:
        return 1
    if span_size >= 13:
        return 2
    if span_size >= 11.5:
        return 3
    return 0


_PDF_ADAPTER = 'pdf-native'
# F01: OHR-Bench 记录的适配器与"无定位"缺口码。JSON 没有文件签名可嗅探，所以由
# 摄取侧显式按这个 media type 派发，而不是让 detect_media_type 去猜。
OHR_ADAPTER = 'ohr-bench-json'
OHR_MEDIA_TYPE = 'application/vnd.ohr-bench+json'
OHR_LOCATOR_GAP = 'locator_unavailable'


def _pdf_contract_payload(*, pdf_path, doc_id, blocks, page_sizes, detail, parser_version):
    """Adapter handler: map real PDF blocks into a contract payload.

    `dispatch` validates whatever this returns; it never fills a missing value in.
    """
    from datamind import parse_contract

    mapped = parse_contract.from_pdf_blocks(
        blocks, page_sizes=page_sizes, parser_version=parser_version, document_id=doc_id,
        source_version=detail["source_sha256"])
    return {
        "classification": {"detected_media_type": "application/pdf", "source_name": pdf_path.name},
        "adapter_capabilities": sorted({block["block_type"] for block in blocks if block.get("block_type")}) or ["text"],
        "blocks": mapped, "relations": [],
        "parsed_scope": [{"kind": "page", "ref": page, "status": "parsed"} for page in sorted(page_sizes)],
        "failed_scope": [{"kind": "page", "ref": page["page_no"], "status": "failed",
                          "detail": page.get("error_code")}
                         for page in detail["pages"] if page.get("error_code")],
        "quality_findings": [], "unsupported_features": [], "artifact_refs": [],
        "manifest": {"parser_version": parser_version, "source_sha256": detail["source_sha256"]},
        "parse_status": detail["parse_status"],
    }


def _ensure_pdf_adapter():
    """Register the PDF adapter once; routing then finds it by detected media type.

    Bootstrapping the plugin context here keeps the legacy call sites (which only know
    this function) and the parser registry in step: whoever routes a PDF has both.
    """
    from datamind import parse_contract

    from .plugins import default_context

    default_context()
    if _PDF_ADAPTER not in parse_contract.registered_adapters():
        parse_contract.register_adapter(
            _PDF_ADAPTER, media_types=["application/pdf"],
            capabilities=["native_text", "pixel_ocr", "tables", "formula"],
            handler=_pdf_contract_payload)


# ---------- F01：OHR-Bench JSON 分支（诚实缺口，不伪造坐标） ----------
def ohr_block(record):
    """One OHR-Bench record as an ingest block, carrying **no invented geometry**.

    OHR-Bench 记录只有 `page_idx` 与人工校准的 `gt_text`；记录本身与
    `data/ohr_bench_samples/manifest.json` 都没有 bbox、也没有页面尺寸（已逐条核对）。
    `document_blocks.bbox` 是 nullable 的，所以这里保留 `bbox=None`，而不是这条分支
    以前写的 `[0, 0, 0, 0]`：那个值不是"未知"，在契约看来是**倒置**的非法框
    （`bbox_inverted`），写进去只会谎报一个并不存在的定位。
    """
    return {"page_no": record["page_idx"] + 1, "block_type": "paragraph", "bbox": None,
            "text": record["gt_text"], "heading_level": None, "table_cells": None,
            "reading_order": 0, "ocr_confidence": 1.0}


def _ohr_contract_payload(*, record, doc_id, source_name, parser_version, source_version):
    """Contract mapping for the OHR-Bench JSON branch — the explicit-gap choice (F01).

    为什么不是"诚实的 pdf_region 定位"：契约的 `pdf_region` 需要
    `page_width`/`page_height` 与一个真实 bbox，而 OHR 样本两者都没有，任何坐标都只能
    是编的；契约也没有"只有页码、没有区域"的 locator 种类。为什么 `blocks=[]`：
    `validate_block` 要求每个 block 都带 locator，而 locator 没有"未知"取值，因此这条
    记录**无法**被表示成一个合法的 contract block —— 与其伪造一块，不如如实声明
    "该记录没有块级定位"。
    缺口通过 `unsupported_features=[OHR_LOCATOR_GAP]` 与
    `failed_scope(status='unsupported')` 显式声明；文本仍落在 `document_blocks`（无定位），
    切片/向量照常，不会被静默丢弃。
    `parse_status` 取 'unsupported' 而不是 'partial'：契约对空 blocks 只接受
    'unsupported'/'failed'（'partial' 会被判 PARSE_RESULT_INVALID: empty_for_status），
    而且语义上"不具备定位能力"比"部分解析"更准确——并没有任何内容解析失败。
    """
    page_no = record["page_idx"] + 1
    return {
        "classification": {"detected_media_type": OHR_MEDIA_TYPE, "source_name": source_name},
        "adapter_capabilities": ["native_text"],
        "blocks": [],
        "relations": [],
        "parsed_scope": [],
        "failed_scope": [{"kind": "page", "ref": page_no, "status": "unsupported",
                          "detail": OHR_LOCATOR_GAP}],
        "quality_findings": [],
        "unsupported_features": [OHR_LOCATOR_GAP],
        "artifact_refs": [],
        "manifest": {"parser_version": parser_version, "source_sha256": source_version},
        "parse_status": "unsupported",
    }


def _ensure_ohr_adapter():
    """Register the OHR-Bench JSON adapter once (explicit dispatch: JSON has no signature).

    Like its PDF counterpart, it bootstraps the plugin context so both worlds agree.
    """
    from datamind import parse_contract

    from .plugins import default_context

    default_context()
    if OHR_ADAPTER not in parse_contract.registered_adapters():
        parse_contract.register_adapter(
            OHR_ADAPTER, media_types=[OHR_MEDIA_TYPE], capabilities=["native_text"],
            handler=_ohr_contract_payload)


BLOCK_INSERT_SQL = """
    INSERT INTO document_blocks(block_id, document_id, page_no, bbox, block_type,
        text, table_cells, formula_latex, reading_order, heading_level,
        ocr_confidence, parser_version, content_hash)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"""

LOCATOR_INSERT_SQL = """
    INSERT INTO document_block_locators(block_id, document_id, contract_version, locator)
    VALUES (%s,%s,%s,%s)"""

# ---------- F08：写 `document_assets.status` 的两条语句（唯一的 SQL 定义处） ----------
REGISTER_ASSET_SQL = """
    INSERT INTO document_assets(document_id, title, collection, file_type, origin,
        sha256, object_uri, page_count, language, acl, quality_score, quality_detail,
        parser_version, status, uploaded_at)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
    ON CONFLICT (document_id) DO UPDATE SET status=EXCLUDED.status"""
MARK_NON_SEARCHABLE_SQL = "UPDATE document_assets SET status=%s, parser_version=%s WHERE document_id=%s"
MARK_TERMINAL_SQL = """UPDATE document_assets SET status=%s, needs_review=%s, page_count=%s,
       quality_score=%s, quality_detail=%s, doc_version=1, parser_version=%s
    WHERE document_id=%s"""


def register_asset(cur, *, doc_id, title, collection, file_type, origin, sha256, object_uri):
    """F08: 登记/开新版本，状态只从 ASSET_STATUS_WRITES 取（这里恒为不可检索）。"""
    cur.execute(REGISTER_ASSET_SQL, (doc_id, title, collection, file_type, origin, sha256,
                                     object_uri, 0, "zh", ["public"], None, None, PARSER_VERSION,
                                     NON_SEARCHABLE_STATUS))


def mark_asset_status(cur, doc_id, status, *, parser_version=PARSER_VERSION, terminal=None):
    """F08: 本文件**唯一**写 `document_assets.status` 的函数。

    `status` 只能取 ASSET_STATUS_WRITES 中的值。`terminal=None` 用于登记资产与发布第 1
    步（只改 status + parser_version）；`terminal` 给出 needs_review/page_count/
    quality_score/quality_detail 时用于发布终态。
    """
    if status not in ASSET_STATUS_WRITES:
        raise ValueError(f"UNKNOWN_ASSET_STATUS:{status!r}")
    if terminal is None:
        cur.execute(MARK_NON_SEARCHABLE_SQL, (status, parser_version, doc_id))
        return
    cur.execute(MARK_TERMINAL_SQL, (status, terminal["needs_review"], terminal["page_count"],
                                    terminal["quality_score"], terminal["quality_detail"],
                                    parser_version, doc_id))


def _block_row(block, doc_id):
    """Row for `document_blocks`; unchanged historical shape (no contract columns).

    `bbox` is written as SQL NULL when the producer has no geometry (the OHR-Bench JSON
    branch), instead of fabricating a zero box; the column is nullable for exactly this.
    """
    bbox = block.get("bbox")
    return (block["block_id"], doc_id, block["page_no"],
            json.dumps(bbox) if bbox is not None else None, block["block_type"], block["text"],
            json.dumps(block["table_cells"], ensure_ascii=False) if block.get("table_cells") else None,
            block.get("formula_latex"), block["reading_order"], block.get("heading_level"),
            block.get("ocr_confidence"), PARSER_VERSION,
            hashlib.sha256(block["text"].encode()).hexdigest())


def _locator_row(block, doc_id):
    """Row for `document_block_locators`, or None when the block has no validated locator.

    A block parsed before the contract existed writes nothing instead of a fabricated
    locator, so `document_block_locators` only ever holds validated rows.
    """
    locator = block.get("contract_locator")
    if not locator:
        return None
    return (block["block_id"], doc_id, block["contract_version"],
            json.dumps(locator, ensure_ascii=False))


def _contract_error_summary(version, error, blocks):
    """A contract violation is recorded, never raised: the gap stays visible in quality_detail."""
    code = error.detail.get("error_code") if isinstance(error.detail, dict) else None
    return {"contract_version": version, "validation": "failed",
            "error_code": code or "PARSE_CONTRACT_FAILED", "blocks": len(blocks)}


def _carry_and_summarize(result, blocks, adapter):
    """Shared contract tail: carry each validated locator onto its block, then summarize.

    Both producers (PDF and OHR-Bench JSON) end here, so "走同一契约路径" is one code path
    and not two look-alike copies.
    """
    payload = result.payload
    locators = {block["block_id"]: block["locator"] for block in payload["blocks"]}
    for block in blocks:
        # Carry the validated locator with the block so persistence writes the same object
        # that was validated, instead of a re-derived copy. A block the contract did not
        # return (no representable locator) gets None and therefore writes no locator row.
        block["contract_locator"] = locators.get(block.get("block_id"))
        block["contract_version"] = payload["contract_version"]
    summary = {"contract_version": payload["contract_version"], "validation": "parsed",
               "adapter": adapter, "parse_status": payload["parse_status"],
               "blocks": len(payload["blocks"]), "source_blocks": len(blocks),
               "locators_written": sum(1 for block in blocks if block.get("contract_locator")),
               "locator_kinds": sorted({block["locator"]["locator_kind"] for block in payload["blocks"]})}
    if payload["unsupported_features"]:
        # F01: an explicit gap declaration travels with the summary instead of being hidden.
        summary["unsupported_features"] = list(payload["unsupported_features"])
    return summary


def _contract_summary(document, pdf_path, doc_id, blocks, detail):
    """Route the parsed blocks through the registered adapter for this media type.

    Ingestion is never aborted by a contract violation: the stable error code is written
    into the quality detail so the gap stays visible instead of being hidden, and the
    existing block output is left untouched.
    """
    from fastapi import HTTPException

    from datamind import parse_contract

    version = parse_contract.CONTRACT_VERSION
    if not blocks:
        return {"contract_version": version, "validation": "skipped", "reason": "no_blocks"}
    page_sizes = {index + 1: (float(page.rect.width), float(page.rect.height))
                  for index, page in enumerate(document)}
    _ensure_pdf_adapter()
    try:
        detected = parse_contract.detect_media_type(pdf_path)
        result = parse_contract.dispatch(detected, pdf_path=pdf_path, doc_id=doc_id, blocks=blocks,
                                        page_sizes=page_sizes, detail=detail,
                                        parser_version=PARSER_VERSION)
    except HTTPException as error:
        return _contract_error_summary(version, error, blocks)
    return _carry_and_summarize(result, blocks, _PDF_ADAPTER)


def _ohr_contract_summary(record, json_path, doc_id, blocks, source_version):
    """F01: route the OHR-Bench JSON branch through the same contract validation.

    Same `parse_contract.dispatch` + same `_carry_and_summarize` tail as the PDF path; the
    difference is only the adapter, because an OHR record has no bbox/page size and so
    yields an explicit `unsupported_features` gap rather than a fabricated locator.
    """
    from fastapi import HTTPException

    from datamind import parse_contract

    version = parse_contract.CONTRACT_VERSION
    _ensure_ohr_adapter()
    try:
        result = parse_contract.dispatch(OHR_MEDIA_TYPE, record=record, doc_id=doc_id,
                                         source_name=Path(json_path).name,
                                         parser_version=PARSER_VERSION,
                                         source_version=source_version)
    except HTTPException as error:
        return _contract_error_summary(version, error, blocks)
    return _carry_and_summarize(result, blocks, OHR_ADAPTER)


def parse_pdf(pdf_path: Path, doc_id: str, ocr_proxy: Path | None = None, *, include_quality=False,
              ocr_engine=None, formula_engine=None, formula_dsn=None, formula_slots=None,
              formula_deadline=None, ocr_dsn=None, ocr_deadline=None, ocr_slots=None):
    """Parse native text or original pixels. Reference text is never an input."""
    if ocr_proxy is not None:
        raise ValueError("OCR_REFERENCE_INPUT_FORBIDDEN")
    blocks = []
    detail = {"pages": [], "formula_candidates": [], "reference_inputs_used": False,
              "parser_version": PARSER_VERSION, "source_sha256": sha256_file(pdf_path),
              "quality_calibration": "not_calibrated", "note": "原生 PDF 文本/图片与原扫描像素解析"}
    doc = pymupdf.open(pdf_path)
    page_count = len(doc)

    # 页面截图（供前端证据定位）
    img_dir = PAGE_IMG_DIR / doc_id
    img_dir.mkdir(parents=True, exist_ok=True)
    for pno in range(page_count):
        pix = doc[pno].get_pixmap(dpi=110)
        (img_dir / f"page_{pno + 1:03d}.png").write_bytes(pix.tobytes("png"))

    figure_inputs = []
    for pno in range(page_count):
        page = doc[pno]
        tpage = page
        native_chars = len(page.get_text().strip())
        if native_chars < 30:
            try:
                from ingestion import ocr
                if ocr_deadline is not None:
                    # Opt-in: the native OCR session runs in its own process group under
                    # the shared budget instead of the parsing process.
                    page_ocr, page_detail = ocr.page_blocks_bounded(
                        pdf_path, pno + 1, dsn=ocr_dsn, deadline_s=ocr_deadline, slots=ocr_slots)
                else:
                    page_ocr, page_detail = ocr.page_blocks(page, pno + 1, ocr=ocr_engine)
                blocks.extend(page_ocr)
                detail["ocr_model"] = ocr.model_manifest(ocr_engine)
                detail["pages"].append(page_detail)
            except Exception as exc:
                detail["pages"].append({"page_no": pno + 1, "route": "pixel_ocr",
                                        "error_code": getattr(exc, "code", None) or "OCR_FAILED",
                                        "exception_type": type(exc).__name__})
            continue
        detail["pages"].append({"page_no": pno + 1, "route": "native_text", "native_chars": native_chars})

        # ---- 原生文本页表格识别 ----
        tables = None
        try:
            tables = tpage.find_tables()
        except Exception:
            pass
        table_bboxes = []
        for ti, tab in enumerate(tables.tables if tables else []):
            bbox = tab.bbox
            table_bboxes.append(bbox)
            cells = tab.extract()
            blocks.append({
                "page_no": pno + 1, "block_type": "table",
                "bbox": [round(x, 1) for x in bbox],
                "text": tab.to_markdown(clean=False).replace("\x00", ""),
                "table_cells": {"n_rows": tab.row_count, "n_cols": tab.col_count,
                                "cells": [[("" if v is None else str(v).replace("\x00", ""))
                                           for v in row] for row in cells]},
                "ocr_confidence": None,
            })

        # ---- 文本块 ----
        d = tpage.get_text("dict")
        for blk in d["blocks"]:
            if blk["type"] != 0:
                if blk["type"] == 1:
                    figure = {"page_no": pno + 1, "block_type": "figure", "bbox": list(blk["bbox"]),
                              "text": "", "heading_level": None, "table_cells": None, "ocr_confidence": None}
                    blocks.append(figure)
                    bb = pymupdf.Rect(blk["bbox"])
                    if bb.width > 2 * bb.height and bb.height < page.rect.height * .15 and len(figure_inputs) < 6:
                        crop_dir = ROOT / ".cache/formula" / detail["source_sha256"]
                        crop_dir.mkdir(parents=True, exist_ok=True)
                        crop_path = crop_dir / f"page-{pno+1}-figure-{len(figure_inputs)}.png"
                        crop_path.write_bytes(blk["image"])
                        figure_inputs.append((figure, crop_path))
                continue
            # 跳过落入表格区域的块
            bb = blk["bbox"]
            if any(not (bb[2] < tb[0] or bb[0] > tb[2] or bb[3] < tb[1] or bb[1] > tb[3])
                   for tb in table_bboxes):
                continue
            # 合并 span 文本，取最大字号判断标题
            text_parts, max_size = [], 0
            for line in blk["lines"]:
                for span in line["spans"]:
                    text_parts.append(span["text"])
                    max_size = max(max_size, span["size"])
            text = "".join(text_parts).replace("\x00", "").strip()
            if not text:
                continue
            level = detect_heading(max_size) if len(text) < 40 else 0
            blocks.append({
                "page_no": pno + 1,
                "block_type": {1: "title", 2: "title", 3: "title"}.get(level, "paragraph"),
                "bbox": [round(x, 1) for x in bb],
                "text": text,
                "heading_level": level or None,
                "table_cells": None,
                "ocr_confidence": None,
            })

    if figure_inputs:
        parsed = (formula_engine or (lambda crops: formula.recognize(
            crops, dsn=formula_dsn, slots=formula_slots, deadline_s=formula_deadline)))(
                [path for _, path in figure_inputs])
        detail["formula_model"] = parsed.get("manifest")
        detail["formula_error"] = parsed.get("error_code")
        for (block, crop), candidate in zip(figure_inputs, parsed.get("results", [])):
            accepted = formula.valid_latex(candidate.get("latex"))
            detail["formula_candidates"].append({**candidate, "page_no": block["page_no"], "bbox": block["bbox"],
                "structural_check": "passed" if accepted else "failed", "semantic_verification": "not_independently_verified"})
            if accepted:
                block.update({"block_type": "formula", "formula_latex": candidate["latex"],
                              "text": candidate["latex"], "ocr_confidence": None})
    detail["unparsed_figures"] = sum(b["block_type"] == "figure" for b in blocks)
    detail["parse_status"] = "partial" if detail["unparsed_figures"] or any(
        p.get("error_code") or p.get("warnings") for p in detail["pages"]) else "parsed"
    if not any(b["text"] for b in blocks):
        detail["parse_status"] = "failed"
    # 阅读顺序: 页内按 y,x 排序
    blocks.sort(key=lambda b: (b["page_no"], b["bbox"][1], b["bbox"][0]))
    for i, b in enumerate(blocks):
        b["reading_order"] = i
        # Assigned here so the contract mapping and the database row share one identity.
        b["block_id"] = f"blk-{md5(doc_id + str(i))}"
    detail["parse_contract"] = _contract_summary(doc, pdf_path, doc_id, blocks, detail)
    doc.close()
    return (blocks, page_count, detail) if include_quality else (blocks, page_count)


# ============================================================
# 切片（标题树 + 段落聚合 + 表格独立切片）
# ============================================================
def build_chunks(blocks, doc_id):
    chunks = []
    heading_stack = []          # [(level, text)]
    buf, buf_pages, buf_blocks = [], [], []

    def flush():
        nonlocal buf, buf_pages, buf_blocks
        text = "\n".join(buf).strip()
        if not text:
            return
        # 嵌入文本带标题路径前缀（提升检索召回）
        sec_path = [t for _, t in heading_stack]
        embed_text = (" > ".join(sec_path) + "\n" + text) if sec_path else text
        chunks.append({
            "chunk_id": f"chk-{md5(doc_id + str(len(chunks)))}",
            "document_id": doc_id,
            "chunk_type": "text",
            "section_path": sec_path,
            "page_start": min(buf_pages), "page_end": max(buf_pages),
            "block_ids": list(buf_blocks),
            "text_for_embedding": embed_text[:4000],
            "exact_text": text,
        })
        buf, buf_pages, buf_blocks = [], [], []

    for bi, b in enumerate(blocks):
        if not b["text"] or b["text"].startswith("DataMind Agent 演示知识库") or b["text"].strip().startswith("第 ") and b["text"].strip().endswith(" 页"):
            continue
        if buf_pages and b["page_no"] != buf_pages[-1]:
            flush()
        if b["block_type"] == "table":
            flush()
            caption = next((candidate for candidate in blocks[bi + 1:bi + 3]
                if candidate["page_no"] == b["page_no"] and candidate["text"].startswith("表 ")), None)
            table_text = ((caption["text"] + "\n") if caption else "") + b["text"]
            chunks.append({
                "chunk_id": f"chk-{md5(doc_id + 'tbl' + str(len(chunks)))}",
                "document_id": doc_id,
                "chunk_type": "table",
                "section_path": [t for _, t in heading_stack],
                "page_start": b["page_no"], "page_end": b["page_no"],
                "block_ids": [b["block_id"]] + ([caption["block_id"]] if caption else []),
                "text_for_embedding": (" > ".join(t for _, t in heading_stack) + "\n" +
                                       table_text)[:4000],
                "exact_text": table_text,
                "table_json": b["table_cells"],
            })
            continue
        if b["block_type"] == "title" and b.get("heading_level"):
            flush()
            level = b["heading_level"]
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, b["text"]))
            buf.append(f"【{b['text']}】")       # 标题保留在正文切片开头
            buf_pages.append(b["page_no"])
            buf_blocks.append(b["block_id"])
            continue
        text = b["text"]
        while text:
            available = CHUNK_MAX - sum(len(x) + 1 for x in buf)
            if available < CHUNK_OVERLAP:
                flush()
                available = CHUNK_MAX
            part = text[:available]
            buf.append(part)
            buf_pages.append(b["page_no"])
            buf_blocks.append(b["block_id"])
            if len(text) <= available:
                break
            text = text[max(1, available - CHUNK_OVERLAP):]
            flush()
    flush()

    # 二次切分过长 chunk（标题树已保证基本粒度，这里仅防御）
    return chunks


# ============================================================
# 发布顺序协议（一次性写完向量 + 元数据，任何失败都停在"不可检索"）
# ============================================================
def ensure_block_ids(blocks, doc_id):
    """Give every block a stable id before it is chunked or written.

    Ids are normally assigned where the blocks are parsed, so the contract mapping and the
    database row share one identity. Other producers build blocks directly — the OHR-Bench
    JSON branch does — and used to rely on the write loop to name them. This keeps that
    guarantee in one place instead of depending on each producer remembering.
    """
    used = {block["block_id"] for block in blocks if block.get("block_id")}
    for position, block in enumerate(blocks):
        if block.get("block_id"):
            continue
        # Position, not reading_order: two blocks can legitimately share a reading_order
        # (the OHR branch emits one per record with 0), and an id collision would collide
        # on the primary key and silently merge two blocks.
        candidate = f"blk-{md5(doc_id + str(position))}"
        attempt = 0
        while candidate in used:
            attempt += 1
            candidate = f"blk-{md5(doc_id + str(position) + '#' + str(attempt))}"
        used.add(candidate)
        block["block_id"] = candidate
    return blocks


def publish_document(cur, qc, doc_id, collection, points, write_metadata, *, commit,
                     page_count, quality_score, quality_detail, parser_version=PARSER_VERSION):
    """按 F10 复审给出的顺序发布一个文档：非检索提交 → Qdrant → PG 元数据 + 终态提交。

    `write_metadata()` 是写 PG 的 blocks/locators/facts/chunks 的回调，返回落库的
    chunk 数（0 表示没有可检索切片）。`commit` 传连接上的提交函数。

    顺序与保证：
    1. 先把 `document_assets.status` 置为 NON_SEARCHABLE_STATUS 并**立即 commit**，
       这一步发生在第一次 Qdrant 调用之前。因此从此刻到第 3 步提交之间，所有
       检索入口（`status='indexed'` 过滤）都选不中该文档，不会出现"旧文本 + 新
       向量"或"新文本 + 旧向量"的对外可见组合——任何失败都停在这个不可检索态。
    2. 执行 Qdrant 的 delete(document_id) + upsert(points)。
    3. 调 `write_metadata()`，再按 chunk 数把终态置为 'indexed' / 'failed' 并 commit。

    异常传播：第 2、3 步抛错时本函数不捕获、也不执行终态 UPDATE/commit，异常直接
    向上传，已提交的库里状态仍是 NON_SEARCHABLE_STATUS，绝不会被置成 'indexed'。

    诚实边界（这不是分布式事务）：第 2 步成功而第 3 步最终失败时，PG 收敛在
    不可检索态，但 Qdrant 里会留下这一版新写入的向量（孤儿向量），检索侧读不到
    旧文本配新向量，代价是向量存储可能短暂多于元数据。重试入口是既有的：重跑本
    文件（默认不加 --reset）——失败文档的 status != 'indexed'，不会被跳过分支跳过，
    第 2 步的 delete(document_id) 会先清掉上次的孤儿向量再重写；私有 PDF 集合另有
    datamind/pdf_maintenance.py 的 orphan_scan / reclaim_orphans
    （HTTP /v1/pdf-knowledge/orphans 与 /v1/pdf-knowledge/orphans/reclaim）兜底。
    """
    # 1) 先落"不可检索"状态并提交（提交后 Qdrant 才可能被改动）
    mark_asset_status(cur, doc_id, NON_SEARCHABLE_STATUS, parser_version=parser_version)
    commit()

    # 2) Qdrant 覆盖式重建该文档的向量
    from qdrant_client.models import FieldCondition, Filter, FilterSelector, MatchValue
    qc.delete(collection, points_selector=FilterSelector(filter=Filter(must=[
        FieldCondition(key="document_id", match=MatchValue(value=doc_id))])), wait=True)
    if points:
        qc.upsert(collection, points)

    # 3) PG 元数据 + 终态提交（只有走到这里才可能变成 'indexed'）
    chunk_count = write_metadata() or 0
    # F08: 终态取值同样只从 ASSET_STATUS_WRITES 取；'indexed' 才是检索可见态。
    mark_asset_status(cur, doc_id, SEARCHABLE_STATUS if chunk_count else FAILED_STATUS,
                      parser_version=parser_version,
                      terminal={"needs_review": not chunk_count, "page_count": page_count,
                                "quality_score": quality_score,
                                "quality_detail": json.dumps(quality_detail, ensure_ascii=False)})
    commit()
    return chunk_count


# ============================================================
# 主流程
# ============================================================
def knowledge_base_jobs(kb_dir=None):
    """Enumerate knowledge-base PDFs from the manifest — never by globbing the directory.

    The manifest *is* the knowledge base definition. Globbing meant any PDF dropped into
    the directory (by another tool, for example) was ingested into the served corpus
    without review; that is a contamination vector, not a convenience. Undeclared files and
    declared-but-missing files are both returned so the caller can report them.
    """
    kb_dir = Path(kb_dir) if kb_dir is not None else KB_DIR
    manifest = json.loads((kb_dir / "manifest.json").read_text(encoding="utf-8"))
    declared = [entry["file"] for entry in manifest["documents"]]
    jobs, missing = [], []
    for entry in manifest["documents"]:
        pdf = kb_dir / entry["file"]
        if not pdf.is_file():
            missing.append(entry["file"])
            continue
        jobs.append({"path": pdf, "origin": "scan" if "扫描版" in entry["file"] else "curated",
                     "file_type": "pdf", "title": pdf.stem, "info": entry,
                     "collection": "demo_kb"})
    undeclared = sorted(path.name for path in kb_dir.glob("*.pdf") if path.name not in set(declared))
    return jobs, missing, undeclared


# ---------- F12：`--reset` 的清理范围 ----------
# reset 会重建 document_blocks/document_assets，若不清契约三表，就会留下指不到任何
# 块的孤儿定位（document_block_locators）与孤儿关系/契约块
# （document_block_relations / document_contract_blocks）。子表在前、父表在后
# （当前没有外键，顺序只为语义清晰；这些表名是模块常量，不是外部输入）。
RESET_DELETE_TABLES = ("document_block_relations", "document_contract_blocks",
                       "document_block_locators", "chunks", "document_blocks",
                       "ingest_jobs",
                       # 事实行同样是"按文档"的：只删 assets/facts 会留下指向已不存在事实的
                       # 复核与审批（它们对 document_facts 无外键，因此不会被级联清掉）。
                       # 审批引用复核，故 approvals 先删。
                       "document_fact_approvals", "document_fact_reviews", "document_facts",
                       "document_assets")


def reset_metadata(cur):
    """F12: 清空重建所需的所有元数据行，契约三表包含在内。"""
    for table in RESET_DELETE_TABLES:
        cur.execute(f"DELETE FROM {table}")


def main():
    global FORMULA_SLOTS
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="清空后重建索引")
    ap.add_argument("--refresh", action="store_true", help="保留文档资产，重新解析并刷新切片/向量")
    ap.add_argument("--formula-slots", type=int, default=FORMULA_SLOTS, metavar="N",
                    help="跨进程公式会话槽上限（1–8）；无空槽时该文档公式保留为解析缺口")
    ap.add_argument("--formula-deadline", type=float, metavar="SECONDS",
                    help="公式会话挂钟时限（0.5–3600），默认 90")
    ap.add_argument("--ocr-deadline", type=float, metavar="SECONDS",
                    help="扫描页 OCR 在独立进程组内的挂钟时限（0.5–3600）；默认在解析进程内执行")
    ap.add_argument("--ocr-slots", type=int, default=OCR_SLOTS, metavar="N",
                    help="跨进程 OCR 槽上限（1–8）；无空槽时该页保留为解析缺口")
    args = ap.parse_args()
    if not 1 <= args.formula_slots <= 8:
        ap.error("--formula-slots must be between 1 and 8")
    if not 1 <= args.ocr_slots <= 8:
        ap.error("--ocr-slots must be between 1 and 8")
    for name in ("formula_deadline", "ocr_deadline"):
        value = getattr(args, name)
        if value is None:
            continue
        try:
            setattr(args, name, task_budget.validate_deadline(value))
        except ValueError:
            ap.error(f"--{name.replace('_', '-')} must be between 0.5 and 3600 seconds")
    FORMULA_SLOTS = args.formula_slots

    from db.migrate import migrate
    migrate()
    meta = psycopg2.connect(**PG)
    cur = meta.cursor()

    if args.reset:
        print("resetting knowledge index ...")
        reset_metadata(cur)
        meta.commit()
        qc = QdrantClient(url=QDRANT, trust_env=False)
        if qc.collection_exists(COLLECTION):
            qc.delete_collection(COLLECTION)

    # ---------- 向量模型 ----------
    print(f"loading embedding model {EMBED_MODEL} ...")
    from fastembed import TextEmbedding
    model = TextEmbedding(model_name=EMBED_MODEL, cache_dir=str(EMBED_CACHE), threads=2)

    qc = QdrantClient(url=QDRANT, trust_env=False)
    rebuild_vectors = not qc.collection_exists(COLLECTION)
    if rebuild_vectors:
        qc.create_collection(COLLECTION,
                             vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE))
        qc.create_payload_index(COLLECTION, "document_id", "keyword")
        qc.create_payload_index(COLLECTION, "chunk_type", "keyword")
        print(f"created collection {COLLECTION}")

    # ---------- 收集待摄取文档 ----------
    jobs, missing, undeclared = knowledge_base_jobs()
    if missing:
        print(f"  [missing] manifest 声明但文件不存在（跳过）：{missing}")
    if undeclared:
        print(f"  [skipped] 目录中未在 manifest 声明的 PDF（**不摄取**）：{undeclared}")
    for js in sorted(OHR_DIR.glob("ohr-*.json")):
        rec = json.loads(js.read_text(encoding="utf-8"))
        jobs.append({"path": js, "origin": "ohrbench", "file_type": "json",
                     "title": f"[{rec['sample_id']}/OHR-Bench/{rec['domain']}] {rec['doc_name']} p{rec['page_idx'] + 1}",
                     "info": {"desc": f"OHR-Bench 真实语料 ({rec['domain']})"},
                     "record": rec, "collection": "ohrbench_kb"})

    print(f"ingesting {len(jobs)} documents ...")
    total_chunks = 0
    for job in jobs:
        path, doc_id = job["path"], "doc-" + md5(job["title"] + sha256_file(job["path"])[:8])
        digest = sha256_file(path)

        cur.execute("""SELECT status,parser_version FROM document_assets WHERE document_id=%s""", (doc_id,))
        row = cur.fetchone()
        if row and row == ("indexed", PARSER_VERSION) and not args.refresh and not rebuild_vectors:
            print(f"  skip (indexed): {job['title']}")
            continue

        register_asset(cur, doc_id=doc_id, title=job["title"], collection=job["collection"],
                       file_type=job["file_type"], origin=job["origin"], sha256=digest,
                       object_uri=str(path))
        # F10 第 1 步（提前）：登记为非检索状态后**立即提交**，让解析/嵌入这段
        # 长事务期间该文档也选不中；失败时不会退回旧的已提交状态。
        meta.commit()

        if job["file_type"] == "pdf":
            blocks, page_count, q_detail = parse_pdf(path, doc_id, include_quality=True,
                                                     formula_dsn=META_DSN, formula_slots=FORMULA_SLOTS,
                                                     formula_deadline=args.formula_deadline,
                                                     ocr_dsn=META_DSN, ocr_deadline=args.ocr_deadline,
                                                     ocr_slots=args.ocr_slots)
            q_detail["document_type"] = job["info"].get("type")
            q_score = None  # Model confidence is not a calibrated document quality score.
        else:
            rec = job["record"]
            page_count = 1
            # F01: 不再写死 bbox=[0,0,0,0]（契约会判 bbox_inverted，且那是个编造的定位）。
            blocks = [ohr_block(rec)]
            q_detail = {"text_coverage": 1.0, "source_page_no": rec['page_idx'] + 1,
                        "note": "OHR-Bench 人工校准 gt_text（原始样本转录，不是本项目 OCR）"}
            q_score = 1.0

        ensure_block_ids(blocks, doc_id)
        if job["file_type"] != "pdf":
            # F01：JSON/OHR 分支与 PDF 分支走同一套契约校验（parse_contract.dispatch +
            # 同一个 _carry_and_summarize 收尾），只是适配器不同。该分支的记录没有 bbox
            # 与页面尺寸，因此产出的是**显式缺口声明**，而不是编造坐标；文本仍照常落
            # document_blocks / 切片 / 向量，不会被丢。
            q_detail["parse_contract"] = _ohr_contract_summary(
                job["record"], path, doc_id, blocks, digest)
        extracted, field_detail = facts.extract(blocks, document_id=doc_id, source_sha256=digest,
                                               parser_version=PARSER_VERSION)
        q_detail["fact_extraction"] = field_detail
        chunks = build_chunks(blocks, doc_id)

        # 嵌入（纯计算，不碰存储）
        texts = [c["text_for_embedding"] for c in chunks]
        vectors = list(model.embed(texts)) if texts else []
        points = []
        for c, vec in zip(chunks, vectors):
            vid = str(uuid.uuid5(uuid.NAMESPACE_DNS, c["chunk_id"]))
            c["vector_id"] = vid
            points.append(PointStruct(id=vid, vector=list(vec), payload={
                "document_id": doc_id, "chunk_id": c["chunk_id"],
                "chunk_type": c["chunk_type"], "section_path": c["section_path"],
                "page_start": c["page_start"], "page_end": c["page_end"],
                "acl": ["public"], "collection": job["collection"],
                "document_version": 1, "embedding_model": EMBED_MODEL,
                "title": job["title"], "origin": job["origin"],
            }))

        def write_metadata():
            """第 3 步：Qdrant 写成功后再落 PG 的 blocks/locators/facts/chunks。

            返回落库的 chunk 数，供终态判断使用。
            """
            cur.execute("DELETE FROM document_blocks WHERE document_id=%s", (doc_id,))
            cur.execute("DELETE FROM document_block_locators WHERE document_id=%s", (doc_id,))
            for b in blocks:
                cur.execute(BLOCK_INSERT_SQL, _block_row(b, doc_id))
            for b in blocks:
                if (locator_row := _locator_row(b, doc_id)) is not None:
                    cur.execute(LOCATOR_INSERT_SQL, locator_row)
            facts.persist(cur, doc_id, extracted)
            cur.execute("DELETE FROM chunks WHERE document_id=%s", (doc_id,))
            for ci, c in enumerate(chunks):
                cur.execute("""
                    INSERT INTO chunks(chunk_id, document_id, document_version, chunk_index,
                        chunk_type, section_path, page_start, page_end, block_ids,
                        text_for_embedding, exact_text, table_json, token_count,
                        quality_score, acl, embedding_model, vector_id, content_hash)
                    VALUES (%s,%s,1,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (c["chunk_id"], doc_id, ci, c["chunk_type"], c["section_path"],
                     c["page_start"], c["page_end"], c["block_ids"],
                     c["text_for_embedding"], c["exact_text"],
                     json.dumps(c["table_json"], ensure_ascii=False) if c.get("table_json") else None,
                     int(len(c["exact_text"]) * 0.6), q_score, ["public"], EMBED_MODEL,
                     c["vector_id"], hashlib.sha256(c["exact_text"].encode()).hexdigest()))
            return len(chunks)

        chunk_count = publish_document(cur, qc, doc_id, COLLECTION, points, write_metadata,
                                       commit=meta.commit, page_count=page_count,
                                       quality_score=q_score, quality_detail=q_detail)
        total_chunks += chunk_count
        print(f"  [{job['origin']:8s}] {job['title'][:44]:46s} blocks={len(blocks):3d} chunks={chunk_count:3d}")

    # 汇总
    cur.execute("""SELECT origin, count(*), sum(page_count) FROM document_assets GROUP BY origin""")
    print("assets:", cur.fetchall())
    cur.execute("SELECT count(*) FROM chunks")
    print("total chunks:", cur.fetchone()[0], "| vectors:", qc.count(COLLECTION, exact=True).count)


if __name__ == "__main__":
    main()
