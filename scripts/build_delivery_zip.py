#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DataMind Agent 源码 ZIP 构建脚本（默认 dry-run，不落盘）。

设计原则
--------
1. 默认只列出将包含的文件与总大小，不创建 ZIP。
2. 逐项对应官方 ZIP 提交要求：完整源代码（含目录结构说明与 README）/ 数据库 schema /
   知识库文档样本（不少于 10 份、涵盖不同类型格式）/ 模型权重文件（若适用）/
   运行环境配置说明（依赖包列表及版本号）/ 可执行 Demo 的快速启动脚本。
3. 类型缺口如实输出（缺什么写什么），不伪造「已涵盖不同类型格式」。
4. 源码使用根目录白名单；书面竞赛材料由提交者另行选定，避免混入多套历史稿。
5. 真实打包必须显式加 --write 和 --i-know-this-writes-a-zip。

用法
----
  python scripts/build_delivery_zip.py                 # dry-run，列出文件与总大小
  python scripts/build_delivery_zip.py --json out.json  # dry-run + 机读清单
  python scripts/build_delivery_zip.py --strict         # 有任何缺口时退出码 2
  python scripts/build_delivery_zip.py --write --i-know-this-writes-a-zip
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "output" / "datamind_agent_delivery.zip"

SOURCE_DIRS = {
    "datamind", "ingestion", "frontend", "web", "scripts", "tests", "deploy",
    "db", "docs", "data", "reports", "delivery_template", "技术文档",
}
SOURCE_ROOT_FILES = {"README.md", "PROGRESS.md", ".gitignore", "requirements.lock"}
# 这三份 PDF 是冻结评测 JSON 的 source_file；虽然在 output/ 下，却不是可丢弃的临时件。
REQUIRED_OUTPUT_SAMPLES = {
    "output/pdf/industrial_forms_holdout_v1.pdf",
    "output/pdf/industrial_forms_development_v2.pdf",
    "output/pdf/industrial_forms_acceptance_v2.pdf",
}

# 绝不进入交付包的目录/文件名（防污染）
EXCLUDE_DIRS = {
    ".git", ".venv", ".venv-formula", ".cache", "__pycache__", "node_modules",
    "dist", "dist-ssr", ".vite",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "tmp", "output", "交付/_build",
    ".repo-state", ".uploads", ".idea", ".vscode",
}
EXCLUDE_GLOBS = [
    "*.pyc", "*.pyo", "*.log", "*.tmp", "*.bak", "*.swp", ".DS_Store",
    "*.sqlite", "*.db", "*.zip", "*.tar", "*.gz",
    "*.key", "*.pem", "*.p12", "*.pfx", "*.p8", ".npmrc", ".netrc",
]
# 显式排除：禁止把演示数据库、容器卷、上传临时件、私有大文件打进包
EXCLUDE_PREFIX = [
    "data/object_store/",              # 页面缓存、上传源和私有 PDF 中间产物
    "data/uploads/",                   # 上传暂存区（开发临时件，非交付样本）
]


def is_excluded(rel: str) -> bool:
    parts = rel.split("/")
    if rel in REQUIRED_OUTPUT_SAMPLES:
        return False
    if parts[0] not in SOURCE_DIRS and not (
        len(parts) == 1 and (rel in SOURCE_ROOT_FILES or fnmatch.fnmatch(rel, "requirements*.txt"))
    ):
        return True
    name = parts[-1].lower()
    # 只允许配置示例进入包，真实 .env / model_config.env 不能随源码分发。
    if not name.endswith(".env.example") and name != ".env.example":
        if name == ".env" or name.startswith(".env.") or name.endswith(".env"):
            return True
    if any(part.startswith("upload-") for part in parts) and rel.startswith((
        "data/cad_demo/", "data/dimension_demo/",
    )):
        return True
    for i in range(len(parts)):
        prefix = "/".join(parts[: i + 1])
        if parts[i] in EXCLUDE_DIRS or prefix in EXCLUDE_DIRS:
            return True
    for pref in EXCLUDE_PREFIX:
        if rel.startswith(pref):
            return True
    for pat in EXCLUDE_GLOBS:
        if fnmatch.fnmatch(name, pat.lower()):
            return True
    return False


def git_files() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=str(ROOT),
                             capture_output=True, text=True, check=True)
    except Exception:
        return []
    return [p for p in out.stdout.split("\0") if p]


def walk_files() -> list[str]:
    found = []
    for root_dir in sorted(SOURCE_DIRS):
        if (ROOT / root_dir).is_symlink():
            continue
        for base, dirs, files in os.walk(ROOT / root_dir, followlinks=False):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not (Path(base) / d).is_symlink()]
            for f in files:
                path = Path(base) / f
                rel = path.relative_to(ROOT).as_posix()
                if not path.is_symlink() and not is_excluded(rel):
                    found.append(rel)
    for path in ROOT.iterdir():
        if path.is_file() and not path.is_symlink() and not is_excluded(path.name):
            found.append(path.name)
    found.extend(p for p in REQUIRED_OUTPUT_SAMPLES if (ROOT / p).is_file())
    return sorted(found)


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sample_reference_issues(files: list[str]) -> list[dict[str, str]]:
    """Check repository-relative data references and the knowledge-base manifest."""
    included = set(files)
    issues: list[dict[str, str]] = []

    def check(origin: str, reference: str) -> None:
        reference = reference.split("#", 1)[0].rstrip("/")
        path = ROOT / reference
        # Some manifests append a Chinese explanatory parenthesis to a source path.
        # Prefer an exact file name (which may itself contain parentheses).
        if not path.exists() and "（" in reference:
            plain_reference = reference.split("（", 1)[0]
            if (ROOT / plain_reference).exists():
                reference, path = plain_reference, ROOT / plain_reference
        present = reference in included or (
            path.is_dir() and any(p.startswith(reference + "/") for p in included)
        )
        if not present:
            issues.append({"manifest": origin, "missing_from_package": reference})

    def visit(origin: str, value: object) -> None:
        if isinstance(value, dict):
            for item in value.values():
                visit(origin, item)
        elif isinstance(value, list):
            for item in value:
                visit(origin, item)
        elif isinstance(value, str) and value.startswith(("data/", "output/")):
            check(origin, value)

    for rel in files:
        if not rel.startswith("data/") or not rel.endswith(".json"):
            continue
        try:
            payload = json.loads((ROOT / rel).read_text(encoding="utf-8"))
        except (ValueError, UnicodeError) as exc:
            issues.append({"manifest": rel, "invalid_json": str(exc)})
            continue
        visit(rel, payload)
        if rel == "data/knowledge_base/manifest.json":
            for document in payload.get("documents", []):
                check(rel, "data/knowledge_base/" + document["file"])
    return sorted(issues, key=lambda item: json.dumps(item, sort_keys=True))


def collect() -> dict:
    tracked = set(git_files())
    # The filesystem is authoritative: approved samples may be intentionally ignored
    # by Git, while tracked files can already have been deleted from the worktree.
    files = walk_files()
    references = sample_reference_issues(files)
    kb_pdfs = sorted(p for p in files if p.startswith("data/knowledge_base/") and p.endswith(".pdf"))
    kb_main_pdfs = [p for p in kb_pdfs if "/native_counterparts/" not in p]
    kb_native_pdfs = [p for p in kb_pdfs if "/native_counterparts/" in p]
    ohr_json = sorted(p for p in files if p.startswith("data/ohr_bench_samples/") and p.endswith(".json"))
    cad_dxf = sorted(p for p in files if p.startswith("data/cad_demo/") and p.endswith(".dxf"))
    office_samples = [p for p in files if p.startswith("data/") and p.endswith((".docx", ".pptx", ".xlsx"))]
    sample_formats = sorted({Path(p).suffix for p in kb_pdfs + cad_dxf + office_samples})
    migrations = sorted(p for p in files if p.startswith("db/migrations/") and p.endswith(".sql"))
    required_source = ["README.md", "datamind/api.py", "frontend/package.json", "web/vue/index.html"]
    source_missing = [p for p in required_source if p not in files]
    dependency_paths = sorted(p for p in files if "/" not in p and p.startswith("requirements"))
    quickstart_paths = ["scripts/start_local.sh", "scripts/start_local.ps1", "scripts/start_api_background.py"]
    sections = [
        {
            "id": "source_code",
            "requirement": "项目源码、前端源码与已构建页面、启动与打包模板",
            "status": "partial" if source_missing else "satisfied",
            "paths": [p + "/" for p in sorted(SOURCE_DIRS)] + ["README.md", "PROGRESS.md", ".gitignore"],
            "missing": source_missing,
            "note": "按当前工作区白名单收集，包含未跟踪样例；不包含本机环境、私有上传和历史交付稿。" +
                    (" 缺少：" + ", ".join(source_missing) if source_missing else ""),
        },
        {
            "id": "db_schema",
            "requirement": "数据库 schema 定义文件",
            "status": "satisfied" if migrations and "db/chinook/Chinook_PostgreSql.sql" in files else "partial",
            "paths": migrations + [p for p in files if p.startswith("deploy/init/") or p.startswith("db/chinook/")],
            "note": f"实际包含 {len(migrations)} 份迁移；未进行空库初始化演练。",
        },
        {
            "id": "kb_samples",
            "requirement": "不少于 10 份知识库文档样本，涵盖不同文档格式",
            "status": "satisfied" if len(kb_main_pdfs) >= 10 and len(sample_formats) >= 2 else "partial",
            "paths": kb_pdfs + ohr_json + cad_dxf + office_samples,
            "counts": {"knowledge_base_pdf_main": len(kb_main_pdfs),
                       "knowledge_base_pdf_native_counterparts": len(kb_native_pdfs),
                       "ohr_bench_json": len(ohr_json), "cad_demo_dxf": len(cad_dxf),
                       "office_docx_pptx_xlsx": len(office_samples)},
            "formats": sample_formats,
            "note": "数量和格式由当前文件实测；data/ 下的 manifest、JSON、图片、样例和冻结代码一并收录。",
        },
        {
            "id": "sample_references",
            "requirement": "数据清单中的本仓库路径引用完整",
            "status": "partial" if references else "satisfied",
            "paths": sorted(REQUIRED_OUTPUT_SAMPLES),
            "issues": references,
            "note": f"逐项检查 data/ JSON 中 data/、output/ 引用和知识库 manifest，发现 {len(references)} 项缺口。"
                    " 不验证外部下载资源或历史报告中本机绝对路径。",
        },
        {
            "id": "model_weights",
            "requirement": "模型权重获取与缓存说明",
            "status": "satisfied_by_list" if "docs/MODEL_WEIGHTS.md" in files else "partial",
            "paths": ["docs/MODEL_WEIGHTS.md"],
            "note": "源码包不含 .cache/ 中的模型权重；获取方式和缺失降级见该清单。源码包不是离线便携运行包。",
        },
        {
            "id": "dependencies",
            "requirement": "运行环境配置说明、依赖与版本锁",
            "status": "satisfied" if "requirements.lock" in files and "requirements.txt" in files else "partial",
            "paths": dependency_paths,
            "note": "包含依赖分组清单和版本锁；未重新安装或验证所有依赖。",
        },
        {
            "id": "quickstart",
            "requirement": "Demo 快速启动脚本",
            "status": "satisfied" if all(p in files for p in quickstart_paths) else "partial",
            "paths": quickstart_paths,
            "note": "提供现有启动脚本及 README；打包检查不等于干净环境启动验收。",
        },
    ]
    total = sum((ROOT / p).stat().st_size for p in files)
    by_ext: dict[str, int] = {}
    for p in files:
        ext = Path(p).suffix.lower() or "(无扩展名)"
        by_ext[ext] = by_ext.get(ext, 0) + 1
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "repo_root": str(ROOT),
        "source_listing": "当前工作区白名单（Git 仅用于标记未跟踪文件）",
        "dry_run": True, "zip_written": False,
        "zip_path_if_written": str(DEFAULT_OUT),
        "source_dirs": sorted(SOURCE_DIRS),
        "excluded_dirs": sorted(EXCLUDE_DIRS),
        "excluded_globs": EXCLUDE_GLOBS,
        "excluded_prefixes": EXCLUDE_PREFIX,
        "required_output_samples": sorted(REQUIRED_OUTPUT_SAMPLES),
        "sections": sections,
        "untracked_included": sorted(p for p in files if p not in tracked),
        "stats": {"file_count": len(files), "total_bytes": total,
                  "total_mib": round(total / 1048576, 2),
                  "by_extension": dict(sorted(by_ext.items(), key=lambda kv: -kv[1])),
                  "tracked_file_count": len(tracked)},
        "files": [{"path": p, "bytes": (ROOT / p).stat().st_size, "sha256": sha256(ROOT / p)} for p in files],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="DataMind Agent 源码 ZIP 构建（默认 dry-run）")
    ap.add_argument("--json", dest="json_out", default=None, help="把机读清单写到该路径")
    ap.add_argument("--strict", action="store_true", help="存在 partial 项时退出码 2")
    ap.add_argument("--write", action="store_true", help="真正写出 ZIP（需再给确认标志）")
    ap.add_argument("--i-know-this-writes-a-zip", dest="confirm", action="store_true",
                    help="确认写出源码压缩包")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="ZIP 输出路径（仅在 --write 时生效）")
    args = ap.parse_args()

    plan = collect()
    print(f"[dry-run] 清单来源: {plan['source_listing']}")
    print(f"[dry-run] 将包含 {plan['stats']['file_count']} 个文件、"
          f"{plan['stats']['total_bytes']} 字节（{plan['stats']['total_mib']} MiB）")
    print(f"[dry-run] 排除目录: {', '.join(plan['excluded_dirs'])}")
    print("[dry-run] 官方 ZIP 要求逐项：")
    for s in plan["sections"]:
        mark = {"satisfied": "OK  ", "satisfied_by_list": "OK*", "partial": "部分"}.get(s["status"], "??")
        cnt = s.get("counts") or {}
        cnt_s = (" " + json.dumps(cnt, ensure_ascii=False)) if cnt else ""
        print(f"  [{mark}] {s['requirement']}{cnt_s}")
        if s["status"] == "partial":
            print(f"        缺口/说明: {s['note']}")
    print("[dry-run] 按扩展名统计（前 12）：")
    for ext, n in list(plan["stats"]["by_extension"].items())[:12]:
        print(f"         {ext:<14} {n}")

    status = 0
    if args.strict and any(s["status"] == "partial" for s in plan["sections"]):
        print("[strict] 存在 partial 项，退出码 2；不写 ZIP", file=sys.stderr)
        status = 2
    if args.write:
        if not args.confirm:
            print("拒绝写盘：需要同时给出 --i-know-this-writes-a-zip（防误用）。", file=sys.stderr)
            return 3
        if status == 0:
            out = Path(args.out)
            if out.resolve() in {(ROOT / item["path"]).resolve() for item in plan["files"]}:
                print("拒绝写盘：ZIP 输出路径不能覆盖待交付的源文件。", file=sys.stderr)
                return 3
            out.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
                for item in plan["files"]:
                    zf.write(ROOT / item["path"], arcname=item["path"])
            plan.update(dry_run=False, zip_written=True, zip_path=str(out.resolve()),
                        zip_bytes=out.stat().st_size, zip_sha256=sha256(out))
            print(f"[write] 已写出 {out}（{out.stat().st_size} 字节）")
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[manifest] 机读清单已写: {args.json_out}")
    return status


if __name__ == "__main__":
    sys.exit(main())
