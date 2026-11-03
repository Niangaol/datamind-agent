"""Evaluate frozen facts on the existing OHR-Bench index and real production RAG.

No corpus, application settings, production code, or database rows are modified.
Provider instrumentation delegates unchanged to the real provider and records its
actual prompts/responses. Labels must already exist and are never inferred from an
answer. The output refuses overwrite, retaining the first outcome per case.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import signal
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx

from datamind.knowledge import KnowledgeRetriever, normalize
from datamind import rag_engine
from datamind.sql_engine import settings


class CaseDeadline(BaseException):
    pass


def deadline_handler(signum, frame):
    raise CaseDeadline()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def has_terms(text, checks):
    normalized = normalize(text)
    return [any(normalize(term) in normalized for term in alternatives)
            for alternatives in checks]


def is_gold(hit, case):
    return (hit.get("title", "").startswith(case["document_prefix"])
            and hit.get("document_sha256") == case["source_sha256"]
            and hit.get("page_start", 0) <= case["page"] <= hit.get("page_end", 0))


class RecordingRetriever(KnowledgeRetriever):
    """Only records real retrieval; never adds, substitutes, or reorders evidence."""
    def __init__(self, *, mode):
        super().__init__(mode=mode)
        self.actual = None
        self.retrieval_elapsed_ms = None

    def retrieve(self, question, **kwargs):
        started = time.monotonic()
        result = super().retrieve(question, **kwargs)
        self.retrieval_elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        self.actual = copy.deepcopy(result)
        return result


def summarize(report):
    cases = report["cases"]
    attempted = [c for c in cases if c.get("attempted")]
    unscoped = [c for c in cases if not c["explicit_sample_scope_in_question"]]
    scoped = [c for c in cases if c["explicit_sample_scope_in_question"]]
    report["summary"] = {
        "case_count": len(cases), "attempted": len(attempted),
        "passed": sum(c.get("passed", False) for c in cases),
        "pass_rate_all_cases": sum(c.get("passed", False) for c in cases) / len(cases),
        "completed": sum(c.get("status") == "completed" for c in cases),
        "abstained": sum(c.get("status") == "abstained" for c in cases),
        "failed": sum(c.get("status") == "failed" for c in cases),
        "not_attempted": sum(not c.get("attempted") for c in cases),
        "document_recall_at_5_all_cases": sum(
            c.get("gold_document_rank_at_5") is not None for c in cases) / len(cases),
        "document_mrr_at_5_all_cases": sum(
            1 / c["gold_document_rank_at_5"] if c.get("gold_document_rank_at_5") else 0
            for c in cases) / len(cases),
        "all_gold_evidence_terms_retrieved_at_5": sum(
            c.get("retrieved_gold_evidence_terms_all_present", False) for c in cases),
        "content_question_cases": len(unscoped),
        "content_question_passed": sum(c.get("passed", False) for c in unscoped),
        "explicit_sample_scope_cases": len(scoped),
        "explicit_sample_scope_passed": sum(c.get("passed", False) for c in scoped),
        "retrieval_modes": {mode: sum(c.get("retrieval", {}).get("mode") == mode
                                     for c in cases) for mode in ("hybrid", "lexical")},
        "warning_cases": [c["case_id"] for c in cases if c.get("retrieval", {}).get("warnings")],
    }


def save(report, path):
    summarize(report)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
                    encoding="utf-8")
    temp.replace(path)


def evaluate_citations(result, actual, case, base_url):
    hits = {hit["chunk_id"]: hit for hit in actual.get("hits", [])}
    checks = []
    gold_quotes = []
    for claim_index, claim in enumerate(result.get("claims", [])):
        for citation in claim.get("citations", []):
            hit = hits.get(citation.get("chunk_id"))
            quote = citation.get("quote") or ""
            continuous = bool(hit and len(normalize(quote)) >= 6
                              and normalize(quote) in normalize(hit["exact_text"]))
            chunk_hash_valid = bool(hit and digest(hit["exact_text"].encode()) == hit["content_hash"])
            gold_identity = bool(hit and is_gold(hit, case))
            checks.append({"claim_index": claim_index, "chunk_id": citation.get("chunk_id"),
                           "quote": quote, "continuous_verbatim_excerpt": continuous,
                           "chunk_content_hash_valid": chunk_hash_valid,
                           "required_source_identity_and_page": gold_identity})
            if continuous and chunk_hash_valid and gold_identity:
                gold_quotes.append(quote)
    endpoints = []
    for citation in result.get("citations", []):
        url = citation.get("source_url")
        if not url or any(item["source_url"] == url for item in endpoints):
            continue
        try:
            with httpx.Client(timeout=8, trust_env=False) as client:
                response = client.get(base_url.rstrip("/") + url)
            downloaded_digest = digest(response.content)
            endpoints.append({"source_url": url, "status_code": response.status_code,
                              "content_bytes": len(response.content),
                              "actual_sha256": downloaded_digest,
                              "expected_sha256": citation.get("document_sha256"),
                              "source_hash_matches": response.status_code == 200
                                  and downloaded_digest == citation.get("document_sha256")})
        except Exception as exc:
            endpoints.append({"source_url": url, "status_code": None,
                              "source_hash_matches": False, "error_type": type(exc).__name__})
    evidence_terms = has_terms("\n".join(gold_quotes), case["required_evidence_checks"])
    return {"citation_checks": checks, "cited_gold": any(
                item["required_source_identity_and_page"] for item in checks),
            "all_citations_continuous_and_hash_valid": bool(checks) and all(
                item["continuous_verbatim_excerpt"] and item["chunk_content_hash_valid"]
                for item in checks),
            "gold_evidence_terms_in_valid_quotes": evidence_terms,
            "gold_evidence_terms_all_cited": all(evidence_terms),
            "source_endpoint_checks": endpoints,
            "all_source_endpoints_resolved_with_matching_hash": bool(endpoints) and all(
                item["source_hash_matches"] for item in endpoints)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=ROOT / "data/evaluation/ohrbench_existing_v1.json")
    parser.add_argument("--output", type=Path, default=ROOT / "reports/dataset_tests_20261010/ohrbench.json")
    parser.add_argument("--mode", choices=("hybrid", "lexical"), default="hybrid")
    parser.add_argument("--api-base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--case-timeout-seconds", type=int, default=165)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Output already exists; use another output path to preserve first outcomes.")
    suite_bytes = args.suite.read_bytes()
    suite = json.loads(suite_bytes)
    for case in suite["cases"]:
        source = ROOT / case["source_file"]
        if digest(source.read_bytes()) != case["source_sha256"]:
            raise SystemExit("Frozen source hash mismatch: " + case["case_id"])
        text = json.loads(source.read_text(encoding="utf-8"))["gt_text"]
        if not all(excerpt in text for excerpt in case["gold_evidence_excerpts"]):
            raise SystemExit("Frozen evidence no longer exists: " + case["case_id"])
    cfg = settings()
    inventory_rows = KnowledgeRetriever(mode="lexical")._rows(["ohrbench_kb"])
    documents = {}
    for row in inventory_rows:
        doc = documents.setdefault(row["document_id"], {
            "document_id": row["document_id"], "title": row["title"],
            "document_version": row["document_version"], "source_sha256": row["document_sha256"],
            "parser_version": row["parser_version"], "eligible_chunks": 0})
        doc["eligible_chunks"] += 1
    report = {"suite_version": suite["suite_version"], "data_version": suite["data_version"],
              "started_at": datetime.now(timezone.utc).isoformat(),
              "suite_path": str(args.suite), "suite_sha256": digest(suite_bytes),
              "method": suite["method"], "scope": suite["scope"],
              "limitations": suite["limitations"], "requested_retrieval_mode": args.mode,
              "actual_execution": "KnowledgeRetriever -> production ask_documents -> real provider_chat; no injected generator, no frozen mock retrieval, no gold input to model",
              "model": cfg.get("NEW_API_MODEL"), "protocol": cfg.get("NEW_API_PROTOCOL"),
              "model_config_source": cfg.get("MODEL_CONFIG_SOURCE"),
              "configured_model_timeout_s": cfg.get("NEW_API_TIMEOUT"),
              "case_timeout_s": args.case_timeout_seconds,
              "prompt_version": rag_engine.PROMPT_VERSION,
              "production_system_prompt_sha256": digest(rag_engine.SYSTEM_PROMPT.encode()),
              "inventory": {"eligible_documents": len(documents), "eligible_chunks": len(inventory_rows),
                            "documents": list(documents.values()),
                            "chunks_snapshot_sha256": digest(json.dumps([
                                {k: r.get(k) for k in ("chunk_id", "content_hash", "document_id", "document_version", "document_sha256")}
                                for r in inventory_rows], sort_keys=True).encode())},
              "code_hashes": {name: digest((ROOT / name).read_bytes()) for name in (
                  "scripts/eval_ohrbench_existing.py", "datamind/knowledge.py", "datamind/rag_engine.py",
                  "datamind/providers.py", "datamind/sql_engine.py", "datamind/visibility.py")},
              "cases": [{"case_id": c["case_id"], "sample_id": c["sample_id"], "domain": c["domain"],
                         "question": c["question"], "expected_answer_checks": c["answer_checks"],
                         "expected_evidence_checks": c["required_evidence_checks"],
                         "gold_evidence_excerpts": c["gold_evidence_excerpts"],
                         "expected_source_sha256": c["source_sha256"],
                         "expected_document_prefix": c["document_prefix"], "expected_page": c["page"],
                         "explicit_sample_scope_in_question": c["explicit_sample_scope_in_question"],
                         "attempted": False, "status": "not_attempted", "passed": False}
                        for c in suite["cases"]]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    save(report, args.output)
    original_provider = rag_engine.provider_chat
    signal.signal(signal.SIGALRM, deadline_handler)
    consecutive_model_failures = 0
    for case, detail in zip(suite["cases"], report["cases"]):
        retriever = RecordingRetriever(mode=args.mode)
        calls = []
        def recorded_provider(profile, system, user, **kwargs):
            call = {"model_requested": profile.get("model"), "protocol": profile.get("protocol"),
                    "system_prompt": system, "user_prompt": user,
                    "system_prompt_sha256": digest((system or "").encode()),
                    "user_prompt_sha256": digest(user.encode()), "max_tokens": kwargs.get("max_tokens")}
            calls.append(call)
            response = original_provider(profile, system, user, **kwargs)
            call["response"] = response
            return response
        rag_engine.provider_chat = recorded_provider
        started = time.monotonic()
        detail.update(attempted=True, started_at=datetime.now(timezone.utc).isoformat())
        signal.alarm(args.case_timeout_seconds)
        try:
            result = rag_engine.ask_documents(case["question"], collections=["ohrbench_kb"], retriever=retriever)
            detail.update(status=result.get("status"), answer=result.get("answer", ""),
                          error_code=result.get("error_code"), rag_result=result)
            actual = retriever.actual or {}
            hits = actual.get("hits", [])
            detail["retrieval"] = {k: v for k, v in actual.items() if k != "hits"}
            detail["retrieved_evidence"] = hits
            detail["gold_document_rank_at_5"] = next(
                (rank for rank, hit in enumerate(hits, 1) if is_gold(hit, case)), None)
            gold_hits = [hit for hit in hits if is_gold(hit, case)]
            detail["gold_evidence_excerpt_ranks_at_5"] = [next(
                (rank for rank, hit in enumerate(hits, 1) if is_gold(hit, case)
                 and normalize(excerpt) in normalize(hit["exact_text"])), None)
                for excerpt in case["gold_evidence_excerpts"]]
            retrieved_checks = has_terms("\n".join(hit["exact_text"] for hit in gold_hits),
                                         case["required_evidence_checks"])
            detail["retrieved_gold_evidence_terms_checks"] = retrieved_checks
            detail["retrieved_gold_evidence_terms_all_present"] = all(retrieved_checks)
            detail["actual_answer_checks"] = has_terms(detail["answer"], case["answer_checks"])
            detail.update(evaluate_citations(result, actual, case, args.api_base_url))
            detail["passed"] = (result.get("status") == "completed"
                and all(detail["actual_answer_checks"]) and detail["cited_gold"]
                and detail["all_citations_continuous_and_hash_valid"]
                and detail["gold_evidence_terms_all_cited"]
                and detail["all_source_endpoints_resolved_with_matching_hash"])
        except CaseDeadline:
            detail.update(status="failed", error_code="EVALUATION_CASE_TIMEOUT", passed=False)
        except Exception as exc:
            detail.update(status="failed", error_code="EVALUATION_EXCEPTION", error_type=type(exc).__name__, passed=False)
        finally:
            signal.alarm(0)
            rag_engine.provider_chat = original_provider
            detail["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
            detail["retrieval_elapsed_ms"] = retriever.retrieval_elapsed_ms
            detail["real_provider_calls"] = calls
            if retriever.actual and "retrieved_evidence" not in detail:
                detail["retrieval"] = {k: v for k, v in retriever.actual.items() if k != "hits"}
                detail["retrieved_evidence"] = retriever.actual["hits"]
            save(report, args.output)
        print(case["case_id"], "status=" + str(detail["status"]),
              "rank=" + str(detail.get("gold_document_rank_at_5")),
              "passed=" + str(detail["passed"]), "elapsed_ms=" + str(detail["elapsed_ms"]),
              "error=" + str(detail.get("error_code")), flush=True)
        if str(detail.get("error_code") or "").startswith("MODEL_"):
            consecutive_model_failures += 1
        else:
            consecutive_model_failures = 0
        if consecutive_model_failures >= 2:
            report["stopped_reason"] = "Two consecutive model errors; remaining cases retained as not_attempted."
            break
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["suite_unchanged_after_run"] = digest(args.suite.read_bytes()) == report["suite_sha256"]
    save(report, args.output)
    print(json.dumps(report["summary"], ensure_ascii=False), flush=True)
    print(args.output, flush=True)
    return 0 if report["summary"]["passed"] == len(suite["cases"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
