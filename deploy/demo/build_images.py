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
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[2]
CONTEXT = ROOT.parent / "delivery_crossplatform" / "build_context"
IMAGE_DIRS = {"datamind", "ingestion", "db", "scripts", "data", "web", "deploy"}


def prepare_context() -> None:
    CONTEXT.mkdir(parents=True, exist_ok=True)
    app = CONTEXT / "app"
    if app.exists():
        shutil.rmtree(app)
    app.mkdir()
    spec = importlib.util.spec_from_file_location("delivery_plan", ROOT / "scripts/build_delivery_zip.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    for rel in module.walk_files():
        if (rel.split("/")[0] not in IMAGE_DIRS and "/" in rel
                and rel not in module.REQUIRED_OUTPUT_SAMPLES):
            continue
        target = app / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, target)
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("arm64", "amd64"), required=True)
    parser.add_argument("--target", choices=("runtime", "application"), default="application")
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    prepare_context()
    if args.prepare_only:
        print(CONTEXT)
        return
    kind = "app" if args.target == "application" else "runtime"
    tag = f"datamind-demo-{kind}:20261019-{args.platform}"
    command = ["docker", "buildx", "build", "--load", "--progress=plain",
               "--platform", f"linux/{args.platform}",
               "--file", str(CONTEXT / ("Dockerfile.runtime" if args.target == "runtime" else "Dockerfile")),
               "--build-arg", "HTTP_PROXY=", "--build-arg", "HTTPS_PROXY=",
               "--build-arg", "ALL_PROXY="]
    if args.target == "application":
        command += ["--build-arg", f"RUNTIME_IMAGE=datamind-demo-runtime:20261019-{args.platform}"]
    command += ["-t", tag, str(CONTEXT)]
    print("Building", tag, flush=True)
    # The desktop host may have stale localhost proxy variables. They must not
    # reach the builder or be baked into the container configuration.
    environment = {key: value for key, value in os.environ.items()
                   if key.lower() not in {"http_proxy", "https_proxy", "all_proxy"}}
    subprocess.run(command, check=True, env=environment)


if __name__ == "__main__":
    main()
