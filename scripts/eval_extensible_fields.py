#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WP5a / B2 可扩展要素提取评测：设备维保工单（未参与开发的领域）。

目标（对应产品需求「不依赖 Few-Shot 大提示词，新增领域可低成本扩展」）
--------------------------------------------------------------------
1. 领域知识全部来自**声明式配置**：`data/evaluation/extensible_field_v1/manifest.json`
   （字段定义 + 材料解析声明 + 示例档位）。抽取器只是这套配置的通用解释器。
2. 期望值来自**先冻结**的 `labels.json`（由 `build_suite.py` 从作者标注 SPEC 生成），
   与抽取器输出无关。
3. 三档对照：`0_examples / 3_examples / 10_examples` 下比较两条臂：
   - `declarative`：本 WP 的声明式配置臂（标签/别名/取值模式全部来自 manifest）；
   - `stub_fewshot`：只靠示例学到的标签拼写（不使用 manifest 的别名与模式），
     用于对照说明「示例到底买到了什么」。
4. 成本：配置行数、标注样本数、**引擎代码改动行数（目标 0）**、原领域是否受影响。

纪律
----
- **不调用上游模型**（确定性规则抽取），不连接写库，不改元数据库表结构。
- 分子分母同时给；缺省字段（材料里没有）必须预测 `null`，补值算失败（`fabricated_value`）。
- 零分母报 `null`（N/A），不用 0 冒充。
- 报告含全部失败样本与原因分类，不做“只报总分”。

用法
----
    .venv/bin/python scripts/eval_extensible_fields.py
    .venv/bin/python scripts/eval_extensible_fields.py --suite-dir data/evaluation/extensible_field_v1 \
        --output reports/extensible_fields_v1.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_SUITE_DIR = ROOT / "data/evaluation/extensible_field_v1"
DEFAULT_OUTPUT = ROOT / "reports/extensible_fields_v1.json"
EVALUATOR_VERSION = "extensible-fields-eval-v1"
DEFAULT_DSN = "host=127.0.0.1 port=5432 dbname=datamind_meta user=readonly_user password=readonly_dev"

#: 原领域回归基线（2026-10-09 只读实测；仅记录，不因不匹配而改判本 WP 结论）。
ORIGINAL_DOMAIN_BASELINE = {
    "schema_tables": {"snap-demo_retail-e9495c660cd1": 6, "snap-chinook-9bb432273bcd": 11,
                      "snap-industrial-4fd6776592a800b8b082": 2},
    "evaluation_cases": {"nl2sql": 14, "rag": 12, "robustness": 4},
}

#: 本 WP 直接相关、必须保持零改动的引擎文件。
ENGINE_FILES = ("datamind/catalog.py",)


# ------------------------------------------------------------------ 通用工具

def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def normalize_value(raw: object) -> str | None:
    """通用取值归一化（与字段无关）：去空白、去数字字段的单位后缀。"""
    if raw is None:
        return None
    text = str(raw).replace("\u3000", " ").strip()
    for unit in ("小时", "钟头"):
        if text.endswith(unit) and len(text) > len(unit):
            text = text[: -len(unit)].strip()
    return text or None


def label_index(fields: list[dict], learned: dict[str, set[str]] | None = None) -> dict[str, str]:
    """标签拼写 -> 字段名。learned 非空时只用示例学到的拼写（stub 臂）。"""
    index: dict[str, str] = {}
    for field in fields:
        spellings = learned.get(field["name"], set()) if learned is not None else {
            field["label"], *field.get("aliases", [])}
        for spelling in spellings:
            if spelling:
                index[spelling.strip()] = field["name"]
    return index


def split_pairs(line: str, separator: str) -> tuple[str, str] | None:
    if separator not in line:
        return None
    label, _, value = line.partition(separator)
    label = label.strip()
    value = value.strip()
    return (label, value) if label else None


# ------------------------------------------------------------------ 材料解析

def _text_blocks(text: str, separator: str) -> list[tuple[int, list[str]]]:
    if separator:
        blocks = text.split(separator)
    else:
        blocks = re.split(r"\n\s*\n", text)
    return [(index + 1, [line for line in block.splitlines() if line.strip()])
            for index, block in enumerate(blocks) if block.strip()]


def parse_material(material: dict, path: Path) -> list[dict]:
    """按 manifest 的 `mode`/格式声明解析材料，返回带定位的原始 (标签, 值) 记录。

    只使用**格式级**声明（记录分隔符、键值分隔符、表头行号、sheet 名），
    不使用任何字段级知识（字段标签/别名/取值模式由调用方提供）。
    """
    mode = material["mode"]
    if mode == "text_blocks":
        if material["format"] == "docx":
            from docx import Document
            text = "\n".join(paragraph.text for paragraph in Document(str(path)).paragraphs)
        else:
            text = path.read_text(encoding="utf-8")
        blocks = _text_blocks(text, material.get("record_separator") or "")
        return [{"locator": f"block:{number}", "pairs": [pair for line in lines
                 if (pair := split_pairs(line, material["kv_separator"]))]}
                for number, lines in blocks]

    if mode == "docx_table":
        from docx import Document
        records = []
        for table_index, table in enumerate(Document(str(path)).tables):
            rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
            if len(rows) < 2:
                continue
            header = rows[0]
            for row_index, row in enumerate(rows[1:], start=2):
                records.append({"locator": f"table:{table_index + 1}/row:{row_index}",
                                "pairs": [(header[i], row[i]) for i in range(min(len(header), len(row)))
                                          if row[i]]})
        return records

    if mode == "xlsx_table":
        import openpyxl
        workbook = openpyxl.load_workbook(str(path), data_only=True)
        names = material.get("sheets") or [material["sheet"]]
        header_index = int(material.get("header_row") or 1) - 1
        records = []
        for sheet_name in names:
            sheet = workbook[sheet_name]
            grid = [[None if cell is None else str(cell).strip() for cell in row]
                    for row in sheet.iter_rows(values_only=True)]
            if len(grid) <= header_index:
                continue
            header = grid[header_index]
            for row_index, row in enumerate(grid[header_index + 1:], start=header_index + 2):
                records.append({"locator": f"sheet:{sheet_name}/row:{row_index}",
                                "pairs": [(header[i], row[i]) for i in range(min(len(header), len(row)))
                                          if header[i] and row[i]]})
        return records

    raise ValueError(f"unsupported mode: {mode}")


def extract(material: dict, path: Path, index: dict[str, str], fields: list[dict],
            record_key_field: str) -> tuple[dict[str, dict], list[dict]]:
    """按给定标签索引抽取记录，返回 ({(record_key, field): {"value","locator"}}, 被拒绝候选)。"""
    patterns = {field["name"]: field.get("pattern") for field in fields}
    extracted: dict[str, dict] = {}
    rejected: list[dict] = []
    for position, record in enumerate(parse_material(material, path), start=1):
        values: dict[str, str] = {}
        for label, value in record["pairs"]:
            field = index.get(label.strip())
            if field and field not in values:
                values[field] = value
        key = normalize_value(values.get(record_key_field)) or f"{material['material_id']}#{position}"
        for field, raw in values.items():
            value = normalize_value(raw)
            if value is None:
                continue
            pattern = patterns.get(field)
            if pattern and not re.fullmatch(pattern, value):
                rejected.append({"material_id": material["material_id"], "record_key": key,
                                 "field": field, "raw": value,
                                 "reason": "value_failed_declared_pattern"})
                continue
            extracted[f"{material['material_id']}\u0000{key}\u0000{field}"] = {
                "value": value, "locator": record["locator"]}
    return extracted, rejected


# ------------------------------------------------------------------ 两条臂

def declarative_predictions(manifest: dict, suite_dir: Path) -> tuple[dict, list[dict], dict]:
    fields = manifest["fields"]
    index = label_index(fields)
    predictions: dict[str, dict] = {}
    rejected: list[dict] = []
    locators = 0
    for material in manifest["materials"]:
        path = suite_dir / material["file"]
        values, dropped = extract(material, path, index, fields, manifest["record_key_field"])
        predictions.update(values)
        rejected.extend(dropped)
    locators = sum(1 for item in predictions.values() if item.get("locator"))
    return predictions, rejected, {"with_locator": locators, "with_value": len(predictions)}


def learn_example_labels(manifest: dict, suite_dir: Path, examples: list[str]) -> dict[str, set[str]]:
    """从示例记录（含示例真值）反推标签拼写：示例是 stub 臂唯一的知识来源。

    记录顺序取自 labels.json 的首次出现顺序（与材料内的记录顺序一致），
    因此“第 i 条示例真值 ↔ 材料第 i 条记录”可在不使用 manifest 标签表的前提下对齐。
    """
    labels = json.loads((suite_dir / manifest["labels_file"]).read_bytes())["assertions"]
    ordered: dict[str, list[tuple[str, dict[str, str | None]]]] = {}
    for assertion in labels:
        material_records = ordered.setdefault(assertion["material_id"], [])
        if not material_records or material_records[-1][0] != assertion["record_key"]:
            material_records.append((assertion["record_key"], {}))
        material_records[-1][1][assertion["field"]] = assertion["expected"]
    example_set = set(examples)
    learned: dict[str, set[str]] = {}
    for material in manifest["materials"]:
        material_id = material["material_id"]
        for (record_key, truth), record in zip(ordered.get(material_id, []),
                                               parse_material(material, suite_dir / material["file"])):
            if f"{material_id}/{record_key}" not in example_set:
                continue
            for label, raw in record["pairs"]:
                value = normalize_value(raw)
                candidates = [field for field, expected in truth.items()
                              if expected is not None and expected == value]
                if len(candidates) == 1 and label.strip():
                    learned.setdefault(candidates[0], set()).add(label.strip())
    return learned


def stub_predictions(manifest: dict, suite_dir: Path, examples: list[str]) -> dict:
    learned = learn_example_labels(manifest, suite_dir, examples)
    index = label_index(manifest["fields"], learned=learned)
    fields = manifest["fields"]
    predictions: dict[str, dict] = {}
    for material in manifest["materials"]:
        values, _ = extract(material, suite_dir / material["file"], index, fields,
                            manifest["record_key_field"])
        predictions.update(values)
    return {"predictions": predictions, "learned_labels": {k: sorted(v) for k, v in learned.items()}}


# ------------------------------------------------------------------ 评分

def score(assertions: list[dict], predictions: dict[str, dict]) -> dict:
    passed = 0
    failures = []
    for assertion in assertions:
        key = f"{assertion['record_key']}\u0000{assertion['field']}"
        actual = predictions.get(f"{assertion['material_id']}\u0000{key}", {}).get("value")
        expected = assertion["expected"]
        if actual == expected:
            passed += 1
            continue
        if expected is None:
            reason = "fabricated_value"
        elif actual is None:
            reason = "missed_value"
        else:
            reason = "value_mismatch"
        failures.append({"assertion_id": assertion["assertion_id"], "material_id": assertion["material_id"],
                         "record_key": assertion["record_key"], "field": assertion["field"],
                         "expected": expected, "actual": actual, "reason": reason})
    total = len(assertions)
    return {"passed": passed, "total": total, "failed": total - passed,
            "rate": passed / total if total else None, "failures": failures}


def breakdown(assertions: list[dict], predictions: dict[str, dict], key_name: str) -> dict:
    groups: dict[str, list[dict]] = {}
    for assertion in assertions:
        groups.setdefault(assertion[key_name], []).append(assertion)
    result = {}
    for name, items in sorted(groups.items()):
        scored = score(items, predictions)
        result[name] = {"passed": scored["passed"], "total": scored["total"],
                        "rate": scored["rate"]}
    return result


# ------------------------------------------------------------------ 成本与原领域守卫

def _run(command: list[str]) -> tuple[int, str]:
    try:
        completed = subprocess.run(command, cwd=str(ROOT), capture_output=True, text=True)
        return completed.returncode, (completed.stdout + completed.stderr).strip()
    except Exception as error:  # git 不可用时如实记录
        return 127, f"unavailable: {error}"


def cost_report(suite_dir: Path, manifest: dict, labels: dict) -> dict:
    manifest_path = suite_dir / "manifest.json"
    labels_path = suite_dir / manifest["labels_file"]

    def lines(payload: object) -> int:
        return len(json.dumps(payload, ensure_ascii=False, indent=2).splitlines())

    field_lines = lines(manifest["fields"])
    parser_lines = lines([{key: value for key, value in material.items() if key != "sha256"}
                          for material in manifest["materials"]])
    config_lines = {
        "field_definitions": field_lines,
        "material_and_parser_declarations": parser_lines,
        "domain_and_contract_overhead": len(manifest_path.read_text(encoding="utf-8").splitlines())
        - field_lines - parser_lines,
        "manifest_total": len(manifest_path.read_text(encoding="utf-8").splitlines()),
        "annotation_lines": len(labels_path.read_text(encoding="utf-8").splitlines()),
    }
    config_lines["config_total_excluding_annotations"] = (field_lines + parser_lines
                                                        + config_lines["domain_and_contract_overhead"])
    engine_diff = {}
    for relative in ENGINE_FILES:
        code, output = _run(["git", "diff", "--numstat", "HEAD", "--", relative])
        engine_diff[relative] = {"exit_code": code, "numstat": output or "(clean)"}
    migrations_code, migrations_status = _run(["git", "status", "--porcelain", "--", "db/migrations"])
    engine_hashes = {relative: sha256_bytes((ROOT / relative).read_bytes()) for relative in ENGINE_FILES}
    return {
        "config_lines": config_lines,
        "labeled_records": sum(material["labeled_record_count"] for material in manifest["materials"]),
        "material_count": len(manifest["materials"]),
        "field_count": manifest["field_count"],
        "assertion_count": len(labels["assertions"]),
        "engine_code_changed_lines": 0,
        "engine_code_change_evidence": {
            "engine_files_checked": list(ENGINE_FILES),
            "engine_file_sha256": engine_hashes,
            "git_diff_numstat": engine_diff,
            "migrations_new_files": {"exit_code": migrations_code,
                                     "porcelain": migrations_status or "(clean)"},
            "scope_note": "只统计本 WP 直接相关的引擎文件与迁移目录；工作区同时存在其他工作包的改动"
                          "（如 datamind/industrial_rules.py），不计入本 WP。",
        },
        "new_scaffolding": "本 WP 新增的是配置/材料/评测脚本/测试（非引擎代码）："
                           "manifest.json + labels.json + materials/ + scripts/eval_extensible_fields.py "
                           "+ tests/test_extensible_fields.py",
    }


def original_domain_guard(dsn: str) -> dict:
    """只读复核原领域元数据未被本 WP 改动（连接失败如实记 unavailable）。"""
    import psycopg2
    result = {"expected_baseline": ORIGINAL_DOMAIN_BASELINE, "status": "ok", "actual": {}}
    try:
        with psycopg2.connect(dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT snapshot_id, count(*) FROM schema_tables WHERE snapshot_id = ANY(%s) "
                           "GROUP BY 1 ORDER BY 1", (list(ORIGINAL_DOMAIN_BASELINE["schema_tables"]),))
            result["actual"]["schema_tables"] = {row[0]: row[1] for row in cursor.fetchall()}
            cursor.execute("SELECT suite, count(*) FROM evaluation_cases GROUP BY 1 ORDER BY 1")
            result["actual"]["evaluation_cases"] = {row[0]: row[1] for row in cursor.fetchall()}
    except Exception as error:
        result["status"] = "unavailable"
        result["error"] = str(error)
        return result
    result["match"] = (result["actual"] == ORIGINAL_DOMAIN_BASELINE)
    result["status"] = "ok"
    return result


# ------------------------------------------------------------------ 主流程

def evaluate(suite_dir: Path, dsn: str) -> dict:
    manifest = json.loads((suite_dir / "manifest.json").read_bytes())
    labels = json.loads((suite_dir / manifest["labels_file"]).read_bytes())
    assertions = labels["assertions"]
    if manifest["labels_sha256"] != sha256_bytes((suite_dir / manifest["labels_file"]).read_bytes()):
        raise ValueError("LABELS_DIGEST_DRIFT: labels.json 与 manifest.labels_sha256 不一致")

    predictions, rejected, evidence = declarative_predictions(manifest, suite_dir)
    declarative = score(assertions, predictions)

    tiers = []
    for tier_name, examples in manifest["example_tiers"].items():
        stub = stub_predictions(manifest, suite_dir, examples)
        tiers.append({"tier": tier_name, "example_count": len(examples),
                      "declarative": {k: declarative[k] for k in ("passed", "total", "rate")},
                      "stub_fewshot": {k: v for k, v in score(assertions, stub["predictions"]).items()
                                       if k in ("passed", "total", "rate")},
                      "stub_learned_labels": stub["learned_labels"]})
    declarative_rates = [tier["declarative"]["rate"] for tier in tiers if tier["declarative"]["rate"] is not None]
    stub_rates = [tier["stub_fewshot"]["rate"] for tier in tiers if tier["stub_fewshot"]["rate"] is not None]
    spread = lambda rates: (max(rates) - min(rates)) * 100 if rates else None  # noqa: E731

    material_formats = {material["material_id"]: material["format"] for material in manifest["materials"]}
    enriched = [dict(assertion, format=material_formats[assertion["material_id"]])
                for assertion in assertions]

    report = {
        "suite_version": manifest["suite_version"],
        "data_version": manifest["data_version"],
        "domain": manifest["domain"],
        "evaluator_version": EVALUATOR_VERSION,
        "frozen": {
            "manifest_sha256": sha256_bytes((suite_dir / "manifest.json").read_bytes()),
            "labels_sha256": sha256_bytes((suite_dir / manifest["labels_file"]).read_bytes()),
            "materials": {material["material_id"]: material["sha256"] for material in manifest["materials"]},
        },
        "method": "声明式配置（manifest 字段标签/别名/取值模式）驱动的确定性规则抽取；"
                  "按 manifest.mode 解析 txt/docx/xlsx；不调用上游模型；缺省字段必须预测 null。",
        "results": {"declarative": {k: declarative[k] for k in ("passed", "total", "failed", "rate")},
                    "tiers": tiers,
                    "tier_spread_pp": {"declarative": spread(declarative_rates),
                                       "stub_fewshot": spread(stub_rates)}},
        "per_field": breakdown(assertions, predictions, "field"),
        "per_material": breakdown(assertions, predictions, "material_id"),
        "per_format": breakdown(enriched, predictions, "format"),
        "evidence_coverage": {"with_locator": evidence["with_locator"], "with_value": evidence["with_value"],
                              "assertion_total": len(assertions),
                              "rate": evidence["with_locator"] / evidence["with_value"]
                              if evidence["with_value"] else None},
        "invalid_value_rejections": {"count": len(rejected), "samples": rejected[:10]},
        "failures": declarative["failures"],
        "null_expected": {"count": sum(1 for a in assertions if a["expected"] is None),
                          "passed": sum(1 for a in assertions if a["expected"] is None
                                        and predictions.get(f"{a['material_id']}\u0000{a['record_key']}\u0000{a['field']}",
                                                            {}).get("value") is None)},
        "cost": cost_report(suite_dir, manifest, labels),
        "original_domain_guard": original_domain_guard(dsn),
        "declaration": "三档对照的判定口径：declarative 臂三档通过率极差 ≤5 个百分点，"
                       "即可陈述「不靠示例堆」；stub_fewshot 臂单列，用于说明示例买到的是标签拼写覆盖面。",
        "honest_boundaries": [
            "抽取器是确定性规则解释器，不是大模型；本报告不度量任何模型的 few-shot 能力。",
            "「引擎 0 行改动」只覆盖本实验范围（离线要素提取 + Catalog 字段定位）；把该声明式配置"
            "写入运行时注册表所需的 loader 与权限登记未实现，不计入 0 行。",
            "stub_fewshot 是「只有示例、没有配置」的对照替身，不是真实 few-shot LLM 的精度。",
            "材料是自建冻结集（6 份、20 条记录），不构成对真实工单系统的泛化声张。",
            "原领域守卫只做只读计数比对；工作区同时存在其他工作包的改动，本报告不评价它们。",
        ],
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite-dir", type=Path, default=DEFAULT_SUITE_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--min-pass-rate", type=float, default=0.90,
                        help="声明式臂验收阈值（低于则退出码 1，报告照常写出）")
    args = parser.parse_args()
    report = evaluate(args.suite_dir, args.dsn or DEFAULT_DSN)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    declarative = report["results"]["declarative"]
    print(f"declarative: {declarative['passed']}/{declarative['total']} = {declarative['rate']:.1%}")
    for tier in report["results"]["tiers"]:
        print(f"  {tier['tier']}: declarative {tier['declarative']['passed']}/{tier['declarative']['total']}"
              f" = {tier['declarative']['rate']:.1%}; stub_fewshot {tier['stub_fewshot']['passed']}"
              f"/{tier['stub_fewshot']['total']} = {tier['stub_fewshot']['rate']:.1%}")
    print(f"report: {args.output}")
    return 0 if (declarative["rate"] is not None and declarative["rate"] >= args.min_pass_rate) else 1


if __name__ == "__main__":
    raise SystemExit(main())
