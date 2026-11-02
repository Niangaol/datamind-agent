#!/usr/bin/env python3
"""Offline paired OHR-Bench text retrieval with current production functions.

No models, API requests, database connections, or index writes are performed.
This evaluates the supplied gt_text/noise_text, not a pixel OCR pipeline.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import inspect
import json
from pathlib import Path
import platform
import re
import sys
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ingestion import ingest
from datamind import knowledge


DEFAULT_LABELS = ROOT / "data/evaluation/ohrbench_existing_v1.json"
DEFAULT_OUTPUT = ROOT / "reports/dataset_tests_20261010/ohr_noise_retrieval.json"
MANIFEST = ROOT / "data/ohr_bench_samples/manifest.json"
CONDITIONS = ("gt_text", "noise_text")
TOP_K = 5


def sha(data: bytes | str) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def canonical_sha(data: object) -> str:
    return sha(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def fraction(numerator: int, denominator: int) -> dict:
    return {"hits": numerator, "total": denominator,
            "rate": numerator / denominator if denominator else None}


def contains(text: str, evidence: str) -> bool:
    return knowledge.normalize(evidence) in knowledge.normalize(text)


def check_terms(texts: list[str], groups: list[list[str]]) -> list[dict]:
    return [{"alternatives": group,
             "matched_alternatives": [term for term in group if any(contains(text, term) for text in texts)]}
            for group in groups]


def function_provenance(function) -> dict:
    return {"qualified_name": function.__module__ + "." + function.__name__,
            "source_sha256": sha(inspect.getsource(function))}


def run(labels_path: Path) -> dict:
    label_bytes = labels_path.read_bytes()
    suite = json.loads(label_bytes)
    manifest_bytes = MANIFEST.read_bytes()
    manifest = json.loads(manifest_bytes)
    cases = suite["cases"]
    if len(cases) != 14 or len({c["sample_id"] for c in cases}) != 14:
        raise ValueError("Expected one frozen case per existing OHR sample")
    sample_meta = {m["sample_id"]: m for m in manifest["samples"]}
    if set(sample_meta) != {c["sample_id"] for c in cases}:
        raise ValueError("Label and manifest sample identities differ")

    samples, snapshots, source_details = {}, {}, []
    for case in cases:
        path = ROOT / case["source_file"]
        expected_path = MANIFEST.parent / sample_meta[case["sample_id"]]["file"]
        if path.resolve() != expected_path.resolve():
            raise ValueError("Frozen source path does not match manifest")
        raw = path.read_bytes()
        record = json.loads(raw)
        if record["sample_id"] != case["sample_id"] or sha(raw) != case["source_sha256"]:
            raise ValueError("Frozen source identity/hash mismatch: " + case["sample_id"])
        for condition in CONDITIONS:
            if sha(record[condition]) != case[condition + "_sha256"]:
                raise ValueError("Frozen text hash mismatch: " + case["sample_id"] + "/" + condition)
        if not all(excerpt and excerpt in record["gt_text"] for excerpt in case["gold_evidence_excerpts"]):
            raise ValueError("Gold evidence is not a continuous verbatim GT excerpt")
        samples[record["sample_id"]] = record
        snapshots[path] = sha(raw)
        source_details.append({"sample_id": record["sample_id"], "source_file": case["source_file"],
            "source_sha256": sha(raw), "domain": record["domain"], "source": record["source"],
            "license": record.get("license"), "page_idx": record["page_idx"],
            **{condition: {"text_sha256": sha(record[condition]), "chars": len(record[condition]),
                          "normalized_chars": len(knowledge.normalize(record[condition]))}
               for condition in CONDITIONS},
            "raw_text_changed": record["gt_text"] != record["noise_text"],
            "normalized_text_changed": knowledge.normalize(record["gt_text"]) != knowledge.normalize(record["noise_text"])})

    code_paths = (ROOT / "ingestion/ingest.py", ROOT / "datamind/knowledge.py")
    code_snapshots = {path: sha(path.read_bytes()) for path in code_paths}
    rows, chunks_by_sample = {}, {}
    for condition in CONDITIONS:
        rows[condition], chunks_by_sample[condition] = [], {}
        for sample_id, record in samples.items():
            # ohr_block reads gt_text. Substitute only in a fresh in-memory copy.
            input_record = dict(record, gt_text=record[condition])
            blocks = ingest.ensure_block_ids([ingest.ohr_block(input_record)], sample_id)
            chunks = ingest.build_chunks(blocks, sample_id)
            chunks_by_sample[condition][sample_id] = chunks
            for chunk_index, chunk in enumerate(chunks):
                rows[condition].append({**chunk, "title": "", "source_chunk_index": chunk_index,
                                        "content_hash": sha(chunk["exact_text"])})

    paired_results = []
    for case in cases:
        # Never call select_documents: the label ID is used only for scoring.
        query = re.sub(r"\bohr-\d{3}\b", "", case["question"], flags=re.IGNORECASE)
        result = {"case_id": case["case_id"], "sample_id": case["sample_id"], "domain": case["domain"],
                  "original_question": case["question"], "retrieval_question": query,
                  "query_sample_id_removed": query != case["question"],
                  "original_explicit_sample_scope": case["explicit_sample_scope_in_question"],
                  "gold_evidence_excerpts": case["gold_evidence_excerpts"]}
        for condition in CONDITIONS:
            by_chunk_id = {row["chunk_id"]: row for row in rows[condition]}
            ranking = knowledge.lexical_ranking(query, rows[condition])
            ranked_documents = list(dict.fromkeys(by_chunk_id[chunk_id]["document_id"] for chunk_id, _ in ranking))
            expected_hits = [(rank, chunk_id, score) for rank, (chunk_id, score) in enumerate(ranking, 1)
                             if by_chunk_id[chunk_id]["document_id"] == case["sample_id"]]
            top5 = ranking[:TOP_K]
            top5_expected_rows = [by_chunk_id[chunk_id] for chunk_id, _ in top5
                                 if by_chunk_id[chunk_id]["document_id"] == case["sample_id"]]
            full_text = samples[case["sample_id"]][condition]
            expected_chunks = chunks_by_sample[condition][case["sample_id"]]
            evidence_results = []
            for excerpt in case["gold_evidence_excerpts"]:
                evidence_chunks = [chunk["chunk_id"] for chunk in expected_chunks if contains(chunk["exact_text"], excerpt)]
                top5_evidence_chunks = [row["chunk_id"] for row in top5_expected_rows if contains(row["exact_text"], excerpt)]
                evidence_results.append({"excerpt": excerpt, "raw_continuous_in_document": excerpt in full_text,
                    "normalized_continuous_in_document": contains(full_text, excerpt),
                    "normalized_continuous_in_chunk_ids": evidence_chunks,
                    "normalized_continuous_in_top5_chunk_ids": top5_evidence_chunks})
            term_document = check_terms([full_text], case["required_evidence_checks"])
            term_top5 = check_terms([row["exact_text"] for row in top5_expected_rows], case["required_evidence_checks"])
            document_rank = ranked_documents.index(case["sample_id"]) + 1 if case["sample_id"] in ranked_documents else None
            first_chunk_rank = expected_hits[0][0] if expected_hits else None
            result[condition] = {"eligible_chunks": len(rows[condition]), "ranked_chunks": len(ranking),
                "expected_document_rank": document_rank, "expected_document_first_chunk_rank": first_chunk_rank,
                "expected_document_in_top5_documents": document_rank is not None and document_rank <= TOP_K,
                "expected_document_in_top5_chunks": first_chunk_rank is not None and first_chunk_rank <= TOP_K,
                "expected_document_best_chunk_score": expected_hits[0][2] if expected_hits else None,
                "ranked_document_ids": ranked_documents,
                "top5_chunks": [{"rank": rank, "chunk_id": chunk_id, "score": score,
                    **{key: by_chunk_id[chunk_id][key] for key in ("document_id", "source_chunk_index", "page_start", "page_end", "content_hash")}}
                    for rank, (chunk_id, score) in enumerate(top5, 1)],
                "gold_evidence": evidence_results,
                "all_gold_excerpts_continuous_in_document": all(e["normalized_continuous_in_document"] for e in evidence_results),
                "all_gold_excerpts_continuous_in_any_chunks": all(e["normalized_continuous_in_chunk_ids"] for e in evidence_results),
                "all_gold_excerpts_continuous_in_top5_chunks": all(e["normalized_continuous_in_top5_chunk_ids"] for e in evidence_results),
                "required_evidence_terms_in_document": term_document,
                "all_required_evidence_terms_in_document": all(t["matched_alternatives"] for t in term_document),
                "required_evidence_terms_in_top5_chunks": term_top5,
                "all_required_evidence_terms_in_top5_chunks": all(t["matched_alternatives"] for t in term_top5)}
        paired_results.append(result)

    def summarize(selected: list[dict], condition: str) -> dict:
        total = len(selected)
        vals = [result[condition] for result in selected]
        evidence = [e for result in vals for e in result["gold_evidence"]]
        return {"cases": total,
            "document_recall_at_5_unique_documents": fraction(sum(v["expected_document_in_top5_documents"] for v in vals), total),
            "document_recall_at_5_chunks": fraction(sum(v["expected_document_in_top5_chunks"] for v in vals), total),
            "document_mrr": sum(1 / v["expected_document_rank"] if v["expected_document_rank"] else 0 for v in vals) / total if total else None,
            "first_relevant_chunk_mrr": sum(1 / v["expected_document_first_chunk_rank"] if v["expected_document_first_chunk_rank"] else 0 for v in vals) / total if total else None,
            "all_gold_excerpts_continuous_in_document": fraction(sum(v["all_gold_excerpts_continuous_in_document"] for v in vals), total),
            "gold_excerpt_continuous_in_document": fraction(sum(e["normalized_continuous_in_document"] for e in evidence), len(evidence)),
            "all_gold_excerpts_continuous_in_any_chunks": fraction(sum(v["all_gold_excerpts_continuous_in_any_chunks"] for v in vals), total),
            "all_gold_excerpts_continuous_in_top5_chunks": fraction(sum(v["all_gold_excerpts_continuous_in_top5_chunks"] for v in vals), total),
            "all_required_evidence_terms_in_document": fraction(sum(v["all_required_evidence_terms_in_document"] for v in vals), total),
            "all_required_evidence_terms_in_top5_chunks": fraction(sum(v["all_required_evidence_terms_in_top5_chunks"] for v in vals), total)}

    per_sample_chunk_differences = []
    for sample_id in samples:
        gt = chunks_by_sample["gt_text"][sample_id]
        noise = chunks_by_sample["noise_text"][sample_id]
        paired = list(zip(gt, noise))
        per_sample_chunk_differences.append({"sample_id": sample_id,
            "gt_chunks": len(gt), "noise_chunks": len(noise), "aligned_by_index_pairs": len(paired),
            "same_index_raw_text_changed": sum(g["exact_text"] != n["exact_text"] for g, n in paired),
            "same_index_normalized_text_changed": sum(knowledge.normalize(g["exact_text"]) != knowledge.normalize(n["exact_text"]) for g, n in paired),
            "unpaired_gt_chunks": max(0, len(gt) - len(noise)), "unpaired_noise_chunks": max(0, len(noise) - len(gt))})

    content_cases = [r for r in paired_results if not r["original_explicit_sample_scope"]]
    explicit_cases = [r for r in paired_results if r["original_explicit_sample_scope"]]
    report = {"test": "ohrbench-existing-v1-paired-text-lexical-retrieval",
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "runtime": {"python": platform.python_version(), "executable": sys.executable},
        "labels": {"path": str(labels_path), "sha256": sha(label_bytes), "suite_version": suite["suite_version"],
                   "frozen_at": suite["label_frozen_at"]},
        "source_provenance": {"manifest_path": str(MANIFEST), "manifest_sha256": sha(manifest_bytes),
            "source_set_sha256": canonical_sha(source_details), "sample_count": len(samples), "samples": source_details},
        "code_provenance": {"files": [{"path": str(p), "sha256": h} for p, h in code_snapshots.items()],
            "functions": [function_provenance(fn) for fn in (ingest.ohr_block, ingest.ensure_block_ids, ingest.build_chunks,
                          ingest.md5, knowledge.lexical_ranking, knowledge.tokens, knowledge.normalize)],
            "chunk_settings": {"CHUNK_TARGET": ingest.CHUNK_TARGET, "CHUNK_MAX": ingest.CHUNK_MAX, "CHUNK_OVERLAP": ingest.CHUNK_OVERLAP},
            "evaluation_script_sha256": sha(Path(__file__).read_bytes())},
        "method": {"retrieval": "Current production lexical_ranking over all independently rebuilt text chunks in each condition",
            "pipeline": "ohr_block -> ensure_block_ids -> build_chunks -> lexical_ranking",
            "scope": "All 14 samples for every query; no select_documents, document_refs, file filter, or gold-based query expansion",
            "query_rule": "Remove only case-insensitive ohr-XXX occurrences from original frozen questions; retain all other characters",
            "title_rule": "All row titles are empty. Section paths are the unmodified output of build_chunks (empty for the OHR paragraph adapter).",
            "noise_rule": "Use a fresh in-memory record with gt_text replaced by existing noise_text; no file changes",
            "normalization": "Production knowledge.normalize: Unicode NFKC, remove whitespace, lowercase. Keep punctuation/markup.",
            "top_k": TOP_K,
            "document_recall_at_5_chunks": "At least one chunk of the expected document appears among the five highest scoring chunks",
            "document_recall_at_5_unique_documents": "Deduplicate full chunk ranking by first document occurrence; expected document among first five documents",
            "expected_document_rank": "Rank after first-occurrence document deduplication; null if no positive lexical hit",
            "evidence_rule": "Every frozen GT excerpt must remain a continuous substring after the same production normalization in both conditions; no markup repair",
            "chunk_difference_rule": "Compare text by sample_id plus chunk index; extra tail chunks counted separately. This is not an edit distance or semantic alignment.",
            "real_model_calls": 0, "api_calls": 0, "database_connections": 0, "database_or_index_writes": 0},
        "limitations": ["Existing 14 extracted pages with manually selected questions/excerpts, not the complete OHR-Bench nor a public QA benchmark.",
            "This is an offline lexical component test, not end-to-end generation, hybrid/dense retrieval, or answer accuracy.",
            "Provided gt_text and noise_text are evaluated directly. No image rendering or real OCR inference is run.",
            "Strict unchanged-excerpt retention can fail because of formatting even if a reader could reconstruct meaning; it is not a semantic correctness test.",
            "rag-011/ohr-005 originally names the sample. Removing its ID leaves a generic topic question; report that case separately from the 13 content questions."],
        "text_and_chunk_differences": {
            "raw_text_changed_samples": sum(s["raw_text_changed"] for s in source_details),
            "normalized_text_changed_samples": sum(s["normalized_text_changed"] for s in source_details),
            "chars": {condition: sum(len(record[condition]) for record in samples.values()) for condition in CONDITIONS},
            "normalized_chars": {condition: sum(len(knowledge.normalize(record[condition])) for record in samples.values()) for condition in CONDITIONS},
            "chunks": {condition: len(rows[condition]) for condition in CONDITIONS},
            "per_sample_chunk_differences": per_sample_chunk_differences,
            "same_index_raw_text_changed_chunks": sum(d["same_index_raw_text_changed"] for d in per_sample_chunk_differences),
            "same_index_normalized_text_changed_chunks": sum(d["same_index_normalized_text_changed"] for d in per_sample_chunk_differences),
            "unpaired_gt_chunks": sum(d["unpaired_gt_chunks"] for d in per_sample_chunk_differences),
            "unpaired_noise_chunks": sum(d["unpaired_noise_chunks"] for d in per_sample_chunk_differences)},
        "summary": {group: {condition: summarize(selected, condition) for condition in CONDITIONS}
                    for group, selected in (("all_14", paired_results), ("content_13", content_cases),
                                            ("originally_explicit_scope_1_without_filter", explicit_cases))},
        "paired_changes": {"top5_chunk_document_recall": dict(Counter(
            f"gt={r['gt_text']['expected_document_in_top5_chunks']},noise={r['noise_text']['expected_document_in_top5_chunks']}"
            for r in paired_results)),
            "document_rank_worse_in_noise": [r["case_id"] for r in paired_results
                if r["gt_text"]["expected_document_rank"] is not None and
                (r["noise_text"]["expected_document_rank"] is None or
                 r["noise_text"]["expected_document_rank"] > r["gt_text"]["expected_document_rank"])],
            "first_chunk_rank_worse_in_noise": [r["case_id"] for r in paired_results
                if r["gt_text"]["expected_document_first_chunk_rank"] is not None and
                (r["noise_text"]["expected_document_first_chunk_rank"] is None or
                 r["noise_text"]["expected_document_first_chunk_rank"] > r["gt_text"]["expected_document_first_chunk_rank"])]},
        "cases": paired_results}
    for path, expected_hash in {**snapshots, **code_snapshots, labels_path: sha(label_bytes), MANIFEST: sha(manifest_bytes)}.items():
        if sha(path.read_bytes()) != expected_hash:
            raise RuntimeError("Input or production code changed during evaluation: " + str(path))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Refusing to overwrite an existing report; choose another --output")
    report = run(args.labels.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(json.dumps({"output": str(args.output.resolve()), "summary": report["summary"],
                      "paired_changes": report["paired_changes"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
