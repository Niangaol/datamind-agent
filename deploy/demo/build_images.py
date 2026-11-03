"""Build the two offline Docker application images from a bounded context.

Run from the repository, on a Docker builder supporting both target architectures:
    python deploy/demo/build_images.py --platform arm64 --target runtime
    python deploy/demo/build_images.py --platform arm64
    python deploy/demo/build_images.py --platform amd64 --target runtime
    python deploy/demo/build_images.py --platform amd64
No local virtual environment, API key, private upload or Git history is copied.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[2]
CONTEXT = ROOT.parent / "datamind-demo-build" / "build_context"

# 只收运行所需的目录，构建上下文由这份白名单生成，不做整仓库拷贝。
APP_DIRS = ("datamind", "ingestion", "db", "scripts", "deploy", "data", "web")
APP_ROOT_FILES = ("README.md", "requirements.lock", "requirements-formula.txt")
SKIP_DIR_NAMES = {
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "node_modules", ".venv", ".venv-formula", ".git", ".cache", "dist", ".vite",
}
SKIP_REL_PREFIXES = ("data/uploads", "data/object_store")
# 冻结评测 JSON 引用的三份源 PDF 位于 output/（不入库），评测样本需要它们随镜像提供。
EXTRA_FILES = (
    "output/pdf/industrial_forms_holdout_v1.pdf",
    "output/pdf/industrial_forms_development_v2.pdf",
    "output/pdf/industrial_forms_acceptance_v2.pdf",
)


def _ignore(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names
            if name in SKIP_DIR_NAMES or name.endswith((".pyc", ".pyo", ".log"))}


def prepare_context() -> None:
    CONTEXT.mkdir(parents=True, exist_ok=True)
    app = CONTEXT / "app"
    if app.exists():
        shutil.rmtree(app)
    app.mkdir()
    for name in APP_DIRS:
        source = ROOT / name
        if not source.is_dir():
            continue
        shutil.copytree(source, app / name, ignore=_ignore)
    for name in APP_ROOT_FILES:
        shutil.copy2(ROOT / name, app / name)
    for rel in EXTRA_FILES:
        source = ROOT / rel
        if source.is_file():
            target = app / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    for prefix in SKIP_REL_PREFIXES:
        shutil.rmtree(app / prefix, ignore_errors=True)
    cache = CONTEXT / "model_cache"
    cache.mkdir(exist_ok=True)
    for name in ("fastembed", "ocr_models", "formula_models"):
        source = ROOT / ".cache" / name
        if not source.is_dir():
            raise SystemExit(f"Missing offline model cache: {source}")
        for link in source.rglob("*"):
            if link.is_symlink() and (link.readlink().is_absolute() or not link.resolve().is_relative_to(source.resolve())):
                raise SystemExit(f"Model cache contains an external or absolute symlink: {link}")
        target = cache / name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target, symlinks=True,
                        ignore=shutil.ignore_patterns("*.lock", "*.log", "__pycache__"))
    (CONTEXT / "wheelhouse").mkdir(exist_ok=True)
    for name in ("Dockerfile", "Dockerfile.runtime", ".dockerignore"):
        shutil.copy2(ROOT / "deploy/demo" / name, CONTEXT / name)
    for name in ("requirements.lock", "requirements-formula.txt"):
        shutil.copy2(ROOT / name, CONTEXT / name)
    shutil.copy2(ROOT / "deploy/demo/requirements-formula.lock", CONTEXT / "requirements-formula.lock")
