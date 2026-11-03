# -*- coding: utf-8 -*-
"""
sample_ohrbench.py — OHR-Bench 分层抽样，导出真实语料作为 RAG 鲁棒性评测素材
每领域抽 2 页: gt_text(人工校准) + formatting_noise_moderate(模拟 OCR 噪声) 成对导出。
使用公开基准数据集 OHR-Bench (ICCV 2025) 作为外部评测来源。

用法: py -3.13 ingestion/sample_ohrbench.py
输出: data/ohr_bench_samples/*.json + manifest.json
"""
import json
import random
from pathlib import Path

import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "ohr_bench_samples"
OUT.mkdir(parents=True, exist_ok=True)

PER_DOMAIN = 2
RNG = random.Random(42)


def main():
    f = pq.ParquetFile(ROOT / "OHR-Bench" / "OHR-Bench_v2.parquet")
    domains = {}
    manifest = []
    idx = 0
    for batch in f.iter_batches(batch_size=1000, columns=["domain", "doc_name", "page_idx", "gt_text", "formatting_noise_moderate"]):
        for domain, doc, page, gt, noise in zip(
                batch.column("domain").to_pylist(),
                batch.column("doc_name").to_pylist(),
                batch.column("page_idx").to_pylist(),
                batch.column("gt_text").to_pylist(),
                batch.column("formatting_noise_moderate").to_pylist()):
            if not gt or len(gt.strip()) < 200:      # 过滤过短页
                continue
            domains.setdefault(domain, []).append(
                {"doc_name": doc, "page_idx": page, "gt": gt, "noise": noise})
            idx += 1
    print("candidate pages per domain:",
          {d: len(v) for d, v in domains.items()})

    n = 0
    for domain, pages in sorted(domains.items()):
        picked = RNG.sample(pages, min(PER_DOMAIN, len(pages)))
        for p in picked:
            n += 1
            rec = {
                "sample_id": f"ohr-{n:03d}",
                "source": "OHR-Bench_v2",
                "domain": domain,
                "doc_name": p["doc_name"],
                "page_idx": p["page_idx"],
                "license": "CC-BY-4.0",
                "gt_text": p["gt"],
                "noise_text": p["noise"],
            }
            safe_doc = p["doc_name"].replace("/", "_").replace("\\", "_")
            path = OUT / f"{rec['sample_id']}_{domain}_{safe_doc}_p{p['page_idx']}.json"
            path.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
            manifest.append({"sample_id": rec["sample_id"], "file": path.name,
                             "domain": domain, "doc_name": p["doc_name"],
                             "page_idx": p["page_idx"], "gt_chars": len(p["gt"])})
    (OUT / "manifest.json").write_text(
        json.dumps({"note": "OHR-Bench 分层抽样；gt_text 用于知识库摄取，noise_text 用于 OCR 噪声鲁棒性评测。",
                    "samples": manifest}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"exported {n} samples to {OUT}")


if __name__ == "__main__":
    main()
