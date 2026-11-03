"""Re-run frozen Chinook questions through the real model, SQL guard and database.

Does not download, seed, reset or change source data/model profiles. New reports
are reserved exclusively so an earlier run can never be replaced.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
import signal
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from datamind import sql_engine as engine
from datamind.catalog import Catalog
from datamind.sql_guard import guard_sql


def now():
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def canonical(value):
    if value is None:
        return ("null", "")
    if isinstance(value, bool):
        return ("bool", str(value))
    if isinstance(value, (int, float, Decimal)):
        return ("number", str(Decimal(str(value)).normalize()))
    if isinstance(value, (datetime, date)):
        return ("date", value.isoformat())
    return ("text", str(value))


def same_result(actual, gold, ordered):
    """Compare full rows, never permit extra columns or subsequence matches."""
    if len(actual) != len(gold):
        return False
    left = [tuple(canonical(v) for v in row) for row in actual]
    right = [tuple(canonical(v) for v in row) for row in gold]
    return left == right if ordered else Counter(left) == Counter(right)


def safe_error(exc):
    code = getattr(exc, "reason", None) or getattr(exc, "pgcode", None)
    if code:
        return str(code)
    text = str(exc)
    if text.startswith("MODEL_") and text.replace("_", "").isalnum():
        return text
    return type(exc).__name__


def deadline(_signum, _frame):
    raise TimeoutError("CASE_TIME_BUDGET")


def save(path, report):
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2,
                               default=json_default) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path,
                        default=ROOT / "data/evaluation/chinook_existing_v1.json")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "reports/dataset_tests_20261010/chinook.json")
    parser.add_argument("--case-timeout", type=float, default=120)
    parser.add_argument("--total-timeout", type=float, default=900)
    args = parser.parse_args()
    suite = json.loads(args.suite.read_text(encoding="utf-8"))
    cases = suite["cases"]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Claim a new pathname before making any model calls.
    with args.output.open("x", encoding="utf-8") as f:
        f.write("{}\n")
    started = time.monotonic()
    report = {"suite": suite["suite"], "suite_version": suite["suite_version"],
              "source_id": "chinook", "started_at": now(), "finished_at": None,
              "run_status": "preflight", "input_path": str(args.suite.resolve()),
              "input_sha256": sha256(args.suite),
              "source_sql_sha256": sha256(ROOT / "db/chinook/Chinook_PostgreSql.sql"),
              "case_timeout_s": args.case_timeout, "total_timeout_s": args.total_timeout,
              "comparison": suite["comparison"], "cases": [],
              "denominator": len(cases), "passed": 0, "failed": len(cases),
              "first_attempt_passed": 0, "first_attempt_denominator": len(cases),
              "model_calls": 0,
              "method": "Original ask(question, 'chinook') with observational wrappers around original _call_model and _execute; no generator injected; production repair budget retained."}
    save(args.output, report)
    cfg = engine.settings()
    report["model"] = cfg.get("NEW_API_MODEL")
    report["model_protocol"] = cfg.get("NEW_API_PROTOCOL")
    report["model_config_source"] = cfg.get("MODEL_CONFIG_SOURCE")
    report["model_key_available"] = bool(cfg.get("NEW_API_KEY"))
    report["model_timeout_s"] = cfg.get("NEW_API_TIMEOUT")
    report["model_max_tokens"] = cfg.get("NEW_API_MAX_TOKENS")
    report["prompt_version"] = engine.PROMPT_VERSION
    report["system_prompt_sha256"] = hashlib.sha256(engine.SYSTEM_PROMPT.encode()).hexdigest()
    code_paths = [Path(__file__), ROOT / "datamind/sql_engine.py",
                  ROOT / "datamind/sql_guard.py", ROOT / "datamind/catalog.py",
                  ROOT / "datamind/providers.py", ROOT / "datamind/settings_store.py"]
    report["code_sha256"] = {str(p.relative_to(ROOT)): sha256(p) for p in code_paths}
    report["git_commit"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    meta_dsn = cfg.get("DATAMIND_META_DSN") or (
        "host=127.0.0.1 port=5432 dbname=datamind_meta user=readonly_user password=readonly_dev")
    catalog = Catalog(meta_dsn, "chinook")
    report["source_snapshot_id"] = catalog.snapshot_id
    report["catalog_version"] = catalog.version
    report["catalog_context_sha256"] = hashlib.sha256(catalog.prompt_context().encode()).hexdigest()
    execute = engine._execute
    model_call = engine._call_model
    gold_results = {}
    report["table_row_counts"] = {}
    for table in sorted(catalog.tables):
        result = execute(guard_sql("SELECT COUNT(*) FROM " + table, catalog), catalog, cfg)
        report["table_row_counts"][table] = result["rows"][0][0]
    for case in cases:
        gold = execute(guard_sql(case["gold_sql"], catalog), catalog, cfg)
        if any(len(row) != case["expected_column_count"] for row in gold["rows"]):
            raise ValueError("GOLD_COLUMN_COUNT")
        gold_results[case["case_id"]] = gold
    # Freeze all gold execution results before observing model answers.
    report["preflight_gold_results"] = gold_results
    report["preflight_finished_at"] = now()
    report["run_status"] = "running"
    save(args.output, report)
    print(f"Chinook preflight: {len(catalog.tables)} tables, {len(cases)} gold queries; "
          f"model={report['model']}, snapshot={catalog.snapshot_id}", flush=True)
    active = None

    def observed_model(question, live_catalog, feedback=None, *rest):
        attempt = {"attempt": len(active["model_attempts"]) + 1,
                   "started_at": now(), "feedback": feedback,
                   "model": engine.model_profile().get("model")}
        active["model_attempts"].append(attempt)
        call_started = time.monotonic()
        try:
            draft = model_call(question, live_catalog, feedback, *rest)
            attempt["draft"] = draft
            return draft
        except Exception as exc:
            attempt["error_code"] = safe_error(exc)
            raise
        finally:
            attempt["elapsed_s"] = round(time.monotonic() - call_started, 3)

    def observed_execute(query, live_catalog, live_cfg):
        execution = {"model_attempt": len(active["model_attempts"]),
                     "sql": query.sql, "bound_params": query.params}
        active["executions"].append(execution)
        try:
            result = execute(query, live_catalog, live_cfg)
            execution.update(result)
            return result
        except Exception as exc:
            execution["error_code"] = safe_error(exc)
            raise

    engine._call_model = observed_model
    engine._execute = observed_execute
    signal.signal(signal.SIGALRM, deadline)
    try:
        for index, case in enumerate(cases):
            gold = gold_results[case["case_id"]]
            active = {**case, "gold_rows": gold["rows"], "gold_columns": gold["columns"],
                      "status": "not_run", "match": False, "model_attempts": [],
                      "executions": [], "attempts": [], "error_code": None,
                      "started_at": now(), "sql": None, "rows": [],
                      "first_attempt": {"match": False, "status": "not_run"}}
            report["cases"].append(active)
            remaining = args.total_timeout - (time.monotonic() - started)
            if remaining <= 0:
                active["error_code"] = "TOTAL_TIME_BUDGET"
                continue
            case_started = time.monotonic()
            print(f"[{index + 1}/{len(cases)}] {case['case_id']} START", flush=True)
            signal.setitimer(signal.ITIMER_REAL, min(args.case_timeout, remaining))
            try:
                result = engine.ask(case["question"], "chinook")
                active.update({"result": result, "status": result["status"],
                               "sql": result.get("sql"), "rows": result.get("rows") or [],
                               "attempts": result.get("attempts") or [],
                               "error_code": result.get("error_code"),
                               "source_snapshot_id": result.get("source_snapshot_id"),
                               "prompt_version": result.get("prompt_version")})
                active["match"] = result["status"] == "completed" and same_result(
                    active["rows"], gold["rows"], case["ordered"])
                first_execution = next((r for r in active["executions"]
                                        if r["model_attempt"] == 1), None)
                first_draft = (active["model_attempts"][0].get("draft")
                               if active["model_attempts"] else None)
                first_match = bool(first_execution and not first_execution.get("error_code")
                                   and same_result(first_execution["rows"], gold["rows"],
                                                   case["ordered"]))
                active["first_attempt"] = {"draft": first_draft, "execution": first_execution,
                                           "match": first_match,
                                           "status": "executed" if first_execution else
                                                     "rejected_or_unavailable"}
            except Exception as exc:
                active["status"] = "failed"
                active["error_code"] = ("CASE_TIME_BUDGET" if isinstance(exc, TimeoutError)
                                        else safe_error(exc))
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                active["elapsed_s"] = round(time.monotonic() - case_started, 3)
                active["finished_at"] = now()
                report["passed"] = sum(c["match"] for c in report["cases"])
                report["failed"] = len(cases) - report["passed"]
                report["first_attempt_passed"] = sum(c["first_attempt"]["match"]
                                                       for c in report["cases"])
                report["model_calls"] = sum(len(c["model_attempts"]) for c in report["cases"])
                save(args.output, report)
            print(f"{case['case_id']}: {'PASS' if active['match'] else 'FAIL'} "
                  f"({active['status']}, calls={len(active['model_attempts'])}, "
                  f"error={active['error_code']}, {active['elapsed_s']:.1f}s)", flush=True)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        engine._call_model = model_call
        engine._execute = execute
        report["run_status"] = "completed"
        report["finished_at"] = now()
        report["elapsed_s"] = round(time.monotonic() - started, 3)
        report["execution_match_rate"] = report["passed"] / len(cases)
        report["first_attempt_match_rate"] = report["first_attempt_passed"] / len(cases)
        save(args.output, report)
    print(f"Execution match: {report['passed']}/{len(cases)}; first attempt: "
          f"{report['first_attempt_passed']}/{len(cases)}; report={args.output}", flush=True)
    return 0 if report["passed"] == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
