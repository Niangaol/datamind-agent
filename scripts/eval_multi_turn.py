"""Multi-turn quality evaluation: per-turn accuracy, segment accuracy, inheritance
traceability and permission-change rejection (WP7).

Two modes:

``--offline`` (default)
    Deterministic, no upstream model, no database. Each turn is resolved with
    :func:`datamind.frame.resolve_turn` — the *same* function ``DataMindAgent.run`` calls for a
    history-bearing request — so this measures the shipped pre-routing decision path (frame,
    scope gate, route, clarification) rather than a copy of it. The transcript's assistant turns
    are the ones the suite carries for the client to send.

``--live``
    Additionally runs the real agent (real retriever, real upstream model) turn by turn, carrying
    the model's own answers as history, and reports status / ``turns_used`` / run ids. A live turn
    counts as correct when the resolution checks pass *and* the end-to-end status matches the
    expected action; answer wording is not judged unless the suite supplies ``answer_checks``.

Reported metrics always come with numerator and denominator. A rejection (scope or read-only
refusal) is a *correct* outcome for the turns flagged ``must_reject``: those turns stay in the
denominators.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from datamind.frame import (DOCUMENT_TERMS, METRIC_SURFACES, REGION_TERMS, RETAIL_TERMS,  # noqa: E402
                            CHINOOK_TERMS, INDUSTRIAL_TERMS, OHRBENCH_TERMS, resolve_turn)

DEFAULT_SUITE = ROOT / "data/evaluation/multi_turn_v1.json"
DEFAULT_OUTPUT = ROOT / "reports/multi_turn_v1.json"
SCOPE_ERROR_CODES = {"SOURCE_NOT_ALLOWED", "COLLECTION_NOT_ALLOWED"}
CLARIFICATION_KINDS = ("time_range", "refund_metric", "write_refusal")
#: Codes the suite uses for the clarification texts the SQL engine produces.
CLARIFICATION_TEXT_KINDS = {}
_LIVE_STATUS = {"sql": {"COMPLETED"}, "rag": {"COMPLETED", "ABSTAINED"},
                "clarify": {"NEEDS_CLARIFICATION"}, "reject": {"FAILED_WITH_EVIDENCE"}}


def _clarification_kind(text: str | None) -> str | None:
    if text is None:
        return None
    from datamind.sql_engine import (CLARIFY_REFUND_METRIC, CLARIFY_TIME_RANGE,
                                     CLARIFY_WRITE_REFUSAL)
    return {CLARIFY_WRITE_REFUSAL: "write_refusal", CLARIFY_REFUND_METRIC: "refund_metric",
            CLARIFY_TIME_RANGE: "time_range"}.get(text, "other")


def _action_of(decision: dict) -> str:
    if decision["error_code"]:
        return "reject"
    if decision["clarification"]:
        return "clarify"
    return decision["route"]


def _as_set(values):
    return sorted(str(item) for item in (values or []))


def _time_equal(expected, actual) -> bool:
    if expected is None or actual is None:
        return expected is None and actual is None
    return all(expected.get(key) == actual.get(key) for key in ("kind", "from", "to"))


def _frame_mismatches(expected: dict, frame: dict) -> list[str]:
    problems = []
    for key, wanted in expected.items():
        got = frame.get(key)
        if key == "time_range":
            ok = _time_equal(wanted, got)
        elif key == "inherited_from_turn":
            ok = wanted == got
        else:
            ok = _as_set(wanted) == _as_set(got)
        if not ok:
            problems.append(f"{key}: expected {wanted!r}, got {got!r}")
    return problems


def _overrides_missing(expected, frame) -> list[str]:
    actual = frame.get("overrides") or []
    missing = []
    for want in expected:
        for got in actual:
            if got.get("field") != want["field"]:
                continue
            if "rule" in want and got.get("rule") != want["rule"]:
                continue
            break
        else:
            missing.append(f"missing override {want!r}")
    return missing


def _value_supported_by_turn(field: str, value, text: str) -> bool:
    """Independent check that the referenced turn really carries the inherited value.

    Uses the shipped closed vocabulary to look for a *surface* of the value in that turn's text;
    it does not re-run the frame code, so a wrong ``inherited_from_turn`` cannot pass by accident.
    """
    lowered = text.lower()
    if field == "time_range":
        if not isinstance(value, dict):
            return False
        for marker in ("上一年", "前一年"):
            if value.get("rule") == "上一年" and marker in text:
                return True
        year = str(value.get("from", ""))[:4]
        if not year:
            return False
        if year in text:
            return True
        # A derived "上一年" traces to the turn that stated the *base* year.
        return value.get("rule") == "上一年" and str(int(year) + 1) in text
    if field == "entities":
        return all(any(item in text for item in (value,)) for value in value) and bool(value)
    if field == "metrics":
        for metric in value:
            surfaces = METRIC_SURFACES.get(metric, ())
            if not any(surface.lower() in lowered for surface in surfaces):
                return False
        return bool(value)
    if field == "sources":
        words = {"chinook": CHINOOK_TERMS, "demo_industrial": INDUSTRIAL_TERMS,
                 "demo_retail": RETAIL_TERMS + REGION_TERMS}
        return all(any(word.lower() in lowered for word in words.get(source, ())) for source in value)
    if field == "document_collections":
        words = {"demo_kb": DOCUMENT_TERMS, "ohrbench_kb": OHRBENCH_TERMS,
                 "industrial_demo": INDUSTRIAL_TERMS + DOCUMENT_TERMS}
        return all(any(word.lower() in lowered for word in words.get(collection, ()))
                   for collection in value)
    return False


def _trace_problems(decision: dict, history: list[dict]) -> list[str]:
    frame, provenance = decision["frame"], decision["frame"]["provenance"]
    problems = []
    for field, index in sorted(provenance.items()):
        if not isinstance(index, int) or index < 1 or index > len(history):
            problems.append(f"{field}: inherited_from_turn {index} is outside the transcript")
            continue
        turn = history[index - 1]
        if not _value_supported_by_turn(field, frame.get(field), turn["content"]):
            problems.append(f"{field}: turn {index} ({turn['role']}) does not carry the value")
    return problems


def _scope_of(segment: dict, turn: dict, suite: dict) -> tuple[list, list]:
    scope = turn.get("scope") or segment.get("scope") or suite["default_scope"]
    return list(scope["sources"]), list(scope["collections"])


def evaluate_offline(suite: dict, only_segments=None, only_turns=None) -> dict:
    turns, segments = [], []
    for segment in suite["segments"]:
        if only_segments and segment["segment_id"] not in only_segments:
            continue
        history, segment_ok, segment_turns = [], True, 0
        for turn in segment["turns"]:
            if only_turns and turn["turn"] not in only_turns:
                continue
            sources, collections = _scope_of(segment, turn, suite)
            decision = resolve_turn(turn["question"], history, mode=turn.get("mode", "auto"),
                                    allowed_sources=sources, allowed_collections=collections)
            action = _action_of(decision)
            problems = []
            if action != turn["expected_action"]:
                problems.append(f"action: expected {turn['expected_action']}, got {action}")
            problems += _frame_mismatches(turn["expected_frame"], decision["frame"])
            problems += _overrides_missing(turn.get("expected_overrides", []), decision["frame"])
            if turn.get("expected_source") is not None and decision["source"] != turn["expected_source"]:
                problems.append(f"source: expected {turn['expected_source']}, got {decision['source']}")
            if turn.get("expected_collections") is not None \
                    and _as_set(decision["collections"]) != _as_set(turn["expected_collections"]):
                problems.append(f"collections: expected {turn['expected_collections']}, "
                                f"got {decision['collections']}")
            if turn.get("expected_error_code") and decision["error_code"] != turn["expected_error_code"]:
                problems.append(f"error_code: expected {turn['expected_error_code']}, "
                                f"got {decision['error_code']}")
            if turn.get("expected_clarification_kind") is not None \
                    and _clarification_kind(decision["clarification"]) != turn["expected_clarification_kind"]:
                problems.append(f"clarification: expected {turn['expected_clarification_kind']}, "
                                f"got {_clarification_kind(decision['clarification'])}")
            trace = _trace_problems(decision, history) if decision["frame"]["provenance"] else []
            passed = not problems and not trace
            segment_ok = segment_ok and passed
            segments_rec = None
            turns.append({"segment_id": segment["segment_id"], "type": segment["type"],
                          "turn": turn["turn"], "question": turn["question"],
                          "expected_action": turn["expected_action"], "action": action,
                          "expected_frame": turn["expected_frame"], "frame": decision["frame"],
                          "route": decision["route"], "source": decision["source"],
                          "collections": decision["collections"],
                          "error_code": decision["error_code"],
                          "clarification": decision["clarification"],
                          "query_text": decision["query"]["text"],
                          "must_reject": bool(turn.get("must_reject")),
                          "must_reject_kind": turn.get("must_reject_kind"),
                          "inherited": bool(decision["frame"]["provenance"]),
                          "traceability_problems": trace,
                          "problems": problems, "passed": passed})
            segment_turns += 1
            history = history + [{"role": "user", "content": turn["question"]},
                                 {"role": "assistant", "content": turn.get("assistant", "")}]
        segments.append({"segment_id": segment["segment_id"], "type": segment["type"],
                         "title": segment["title"], "turns": segment_turns, "passed": segment_ok})
    return {"turns": turns, "segments": segments}


def _summarise(evidence: dict) -> dict:
    turns = evidence["turns"]
    passed = [turn for turn in turns if turn["passed"]]
    inherited = [turn for turn in turns if turn["inherited"]]
    traceable = [turn for turn in inherited if not turn["traceability_problems"]]
    scope_reject = [turn for turn in turns if turn["must_reject_kind"] == "scope"]
    scope_refused = [turn for turn in scope_reject if turn["passed"]
                     and turn["error_code"] in SCOPE_ERROR_CODES]
    danger = [turn for turn in turns if turn["must_reject_kind"] == "dangerous_write"]
    danger_refused = [turn for turn in danger
                      if turn["action"] == "clarify" and turn["clarification"]]
    return {
        "turn_accuracy": {"numerator": len(passed), "denominator": len(turns)},
        "segment_accuracy": {"numerator": sum(1 for s in evidence["segments"] if s["passed"]),
                             "denominator": len(evidence["segments"])},
        "inheritance_traceability": {"numerator": len(traceable), "denominator": len(inherited)},
        "scope_change_rejection": {"numerator": len(scope_refused), "denominator": len(scope_reject)},
        "dangerous_turn_refusal": {"numerator": len(danger_refused), "denominator": len(danger)},
        "turns_with_inheritance": len(inherited),
        "must_reject_turns": len(scope_reject) + len(danger),
    }


def _failures(evidence: dict) -> list[dict]:
    samples = []
    for turn in evidence["turns"]:
        if turn["passed"]:
            continue
        samples.append({"segment_id": turn["segment_id"], "turn": turn["turn"],
                        "question": turn["question"], "expected_action": turn["expected_action"],
                        "action": turn["action"], "error_code": turn["error_code"],
                        "clarification": turn["clarification"],
                        "problems": turn["problems"], "traceability_problems": turn["traceability_problems"],
                        "expected_frame": turn["expected_frame"], "frame": turn["frame"]})
    return samples


def evaluate_live(suite: dict, offline: dict, only_segments=None, only_turns=None) -> dict:
    """Run the real agent turn by turn; the model's own answers become the next history."""
    from datamind.agent import DataMindAgent
    from datamind.sql_engine import settings
    records, run_ids = [], []
    by_segment = {(turn["segment_id"], turn["turn"]): turn for turn in offline["turns"]}
    for segment in suite["segments"]:
        if only_segments and segment["segment_id"] not in only_segments:
            continue
        history = []
        for turn in segment["turns"]:
            if only_turns and turn["turn"] not in only_turns:
                continue
            sources, collections = _scope_of(segment, turn, suite)
            agent = DataMindAgent()
            try:
                result = agent.run(turn["question"], database_sources=sources,
                                   document_collections=collections, mode=turn.get("mode", "auto"),
                                   history=history)
            finally:
                agent.close()
            run_ids.append(result["run_id"])
            base = by_segment.get((segment["segment_id"], turn["turn"]), {})
            allowed = _LIVE_STATUS.get(turn["expected_action"], set())
            status_ok = result["status"] in allowed
            if turn.get("expected_error_code"):
                status_ok = status_ok and result.get("error_code") == turn["expected_error_code"]
            checks = [any(term in (result.get("answer") or "") for term in group)
                      for group in turn.get("answer_checks", [])]
            answer_ok = all(checks) if checks else None
            records.append({"segment_id": segment["segment_id"], "turn": turn["turn"],
                            "question": turn["question"], "expected_action": turn["expected_action"],
                            "status": result["status"], "error_code": result.get("error_code"),
                            "turns_used": result.get("turns_used"), "run_id": result["run_id"],
                            "answer": result.get("answer"), "answer_checks": checks,
                            "resolution_passed": bool(base.get("passed")), "status_ok": status_ok,
                            "passed": bool(base.get("passed")) and status_ok
                                      and (answer_ok is not False)})
            history = history + [{"role": "user", "content": turn["question"]},
                                 {"role": "assistant", "content": result.get("answer") or ""}]
    return {"model": settings().get("NEW_API_MODEL"), "records": records, "run_ids": run_ids,
            "turn_accuracy": {"numerator": sum(1 for r in records if r["passed"]),
                              "denominator": len(records)}}


def _git_commit() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(ROOT), check=True,
                              capture_output=True, text=True).stdout.strip() or None
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--live", action="store_true", help="also run the real agent + model")
    parser.add_argument("--offline", action="store_true", help="explicitly select offline only")
    parser.add_argument("--segment", action="append", default=[])
    parser.add_argument("--turn", action="append", type=int, default=[])
    args = parser.parse_args()
    if args.live and args.offline:
        parser.error("--live and --offline are mutually exclusive")

    suite = json.loads(args.suite.read_text(encoding="utf-8"))
    evidence = evaluate_offline(suite, only_segments=args.segment or None,
                                only_turns=args.turn or None)
    report = {
        "suite_version": suite["suite_version"], "suite_sha256":
            hashlib.sha256(args.suite.read_bytes()).hexdigest(),
        "evaluator_version": "multi-turn-eval-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "commit": _git_commit(), "mode": "live" if args.live else "offline",
        "method": suite["method"],
        "offline_scope": "resolution layer only (frame + scope gate + route + clarification); "
                         "no database, no upstream model",
        "metrics": _summarise(evidence),
        "segment_results": evidence["segments"],
        "turns": evidence["turns"],
        "failures": _failures(evidence),
        "code_hashes": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in
                        ("datamind/frame.py", "datamind/agent.py", "datamind/sql_engine.py",
                         "scripts/eval_multi_turn.py")},
    }
    if args.live:
        report["live"] = evaluate_live(suite, evidence, only_segments=args.segment or None,
                                       only_turns=args.turn or None)
        report["metrics"]["live_turn_accuracy"] = report["live"]["turn_accuracy"]
    report["honest_boundaries"] = [
        "offline 只判定解析与决策层（帧、范围闸门、路由、澄清），不判定答案正确性；"
        "答案正确性只在 --live（真实模型）里按状态与可选 answer_checks 判定。",
        "语料是自建的 10 段冻结对话，不是公开标准评测集；本报告不代表任何外部基准得分。",
        "帧的词表是演示库的封闭词表（地区/品类/产品/指标/工业零件号 + 文档关键词），"
        "词表外的措辞解析为空，因此这不是通用实体链接或 NER 能力。",
        "「上一年」相对继承到的年份推导；不使用 去年/今年，冻结期望不随运行日期漂移。",
        "拒绝轮（must_reject）计入分母：拒绝是这些轮次的正确答案，不是被剔除的失败。",
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
                           encoding="utf-8")
    metrics = report["metrics"]
    print(json.dumps({key: metrics[key] for key in
                      ("turn_accuracy", "segment_accuracy", "inheritance_traceability",
                       "scope_change_rejection", "dangerous_turn_refusal")}, ensure_ascii=False))
    if report["failures"]:
        for sample in report["failures"][:10]:
            print("FAIL", sample["segment_id"], sample["turn"], sample["question"],
                  sample["problems"] or sample["traceability_problems"])
    print(f"report -> {args.output}")
    return 0 if metrics["turn_accuracy"]["numerator"] == metrics["turn_accuracy"]["denominator"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
