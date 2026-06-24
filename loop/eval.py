"""Golden-set evaluation runner for Loop (Pillar 01 — evaluation).

Runs the golden dataset (`evals/golden.json`) through the *real* agent
(`agent.run`, i.e. real MiniMax/Anthropic calls) and scores each case against
its expectations: which tools were used, what the reply must / must not contain,
and that no guardrail crash occurred. Prints a scorecard (overall + per-category
pass rate, tool-selection accuracy) and exits non-zero on any failure so it can
gate CI. Results are also persisted to the `eval_results` table for trend tracking.

Usage:
    loop-eval                 # run the whole golden set
    loop-eval --limit 5       # first 5 cases (smoke test)
    loop-eval --case calc-multiply mem-recall-standup
    loop-eval --json          # machine-readable summary to stdout

Each case in golden.json:
    { "id", "category", "prompt", "context?": {user, channel, channel_type},
      "expect": { "must_use_tools"[], "must_not_use_tools"[],
                  "must_contain"[], "must_not_contain"[],
                  "min_guardrail_hits"?, "max_latency_ms"? } }
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv

DATASET = Path(__file__).resolve().parent.parent / "evals" / "golden.json"


def _load(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def _score(run, expect: dict) -> list[str]:
    """Return a list of failure reasons; empty list == pass."""
    reasons: list[str] = []
    text = (run.text or "").lower()
    used = list(run.tool_calls or [])

    for tool in expect.get("must_use_tools", []):
        if tool not in used:
            reasons.append(f"missing tool '{tool}' (used: {used or 'none'})")
    for tool in expect.get("must_not_use_tools", []):
        if tool in used:
            reasons.append(f"forbidden tool '{tool}' was used")
    for sub in expect.get("must_contain", []):
        if sub.lower() not in text:
            reasons.append(f"reply missing '{sub}'")
    for sub in expect.get("must_not_contain", []):
        if sub.lower() in text:
            reasons.append(f"reply contains forbidden '{sub}'")
    if "min_guardrail_hits" in expect:
        if run.guardrail_hits < expect["min_guardrail_hits"]:
            reasons.append(f"guardrail_hits {run.guardrail_hits} < {expect['min_guardrail_hits']}")
    return reasons


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="loop-eval", description="Run Loop's golden eval set.")
    parser.add_argument("--dataset", default=os.environ.get("LOOP_EVAL_DATASET", str(DATASET)))
    parser.add_argument("--limit", type=int, default=None, help="run only the first N cases")
    parser.add_argument("--case", nargs="+", default=None, help="run only these case ids")
    parser.add_argument("--json", action="store_true", help="emit a JSON summary")
    args = parser.parse_args(argv)

    # Import after load_dotenv so the model env is in place.
    from loop import observability as obs
    from loop.agent import run as run_agent

    cases = _load(Path(args.dataset))
    if args.case:
        wanted = set(args.case)
        cases = [c for c in cases if c["id"] in wanted]
    if args.limit:
        cases = cases[: args.limit]

    run_id = uuid.uuid4().hex[:12]
    model_id = os.environ.get("ANTHROPIC_MODEL", "?")
    results: list[dict] = []

    if not args.json:
        print(f"\nLoop eval · run {run_id} · model {model_id} · {len(cases)} cases\n" + "─" * 64)

    for c in cases:
        expect = c.get("expect", {})
        t0 = time.perf_counter()
        try:
            run = run_agent(c["prompt"], context=c.get("context"))
            err = None
        except Exception as exc:  # noqa: BLE001
            run = None
            err = f"{type(exc).__name__}: {exc}"
        latency_ms = int((time.perf_counter() - t0) * 1000)

        if run is None:
            reasons = [f"run raised: {err}"]
            tool_calls: list[str] = []
        else:
            reasons = _score(run, expect)
            tool_calls = list(run.tool_calls or [])
            max_lat = expect.get("max_latency_ms")
            slow = max_lat is not None and latency_ms > max_lat  # soft: reported, not a failure

        passed = not reasons
        obs.record_eval(run_id, c["id"], c.get("category", "?"), model_id,
                        passed, latency_ms, tool_calls, reasons)
        results.append({
            "id": c["id"], "category": c.get("category", "?"), "passed": passed,
            "latency_ms": latency_ms, "tools": tool_calls, "reasons": reasons,
        })

        if not args.json:
            mark = "✅ PASS" if passed else "❌ FAIL"
            slow_tag = " ⚠️slow" if (run is not None and expect.get("max_latency_ms") and latency_ms > expect["max_latency_ms"]) else ""
            print(f"{mark}  {c['id']:<24} {c.get('category',''):<10} {latency_ms:>6}ms{slow_tag}  tools={tool_calls or '-'}")
            for r in reasons:
                print(f"        ↳ {r}")

    # --- scorecard ---------------------------------------------------------
    total = len(results)
    passed = sum(r["passed"] for r in results)
    by_cat: dict[str, list[bool]] = {}
    for r in results:
        by_cat.setdefault(r["category"], []).append(r["passed"])
    tool_cases = [c for c in cases if c.get("expect", {}).get("must_use_tools") or c.get("expect", {}).get("must_not_use_tools")]
    tool_ids = {c["id"] for c in tool_cases}
    tool_passed = sum(1 for r in results if r["id"] in tool_ids and not [x for x in r["reasons"] if "tool" in x])
    tool_acc = (tool_passed / len(tool_ids) * 100) if tool_ids else 100.0

    summary = {
        "run_id": run_id, "model_id": model_id,
        "total": total, "passed": passed,
        "pass_rate": round(passed / total * 100, 1) if total else 0.0,
        "tool_selection_accuracy": round(tool_acc, 1),
        "by_category": {k: f"{sum(v)}/{len(v)}" for k, v in by_cat.items()},
    }

    if args.json:
        print(json.dumps({"summary": summary, "results": results}, ensure_ascii=False))
    else:
        print("─" * 64)
        print(f"Overall: {passed}/{total} ({summary['pass_rate']}%)   "
              f"tool-selection: {summary['tool_selection_accuracy']}%")
        print("By category: " + "  ".join(f"{k} {v}" for k, v in summary["by_category"].items()))
        print(f"Stored under run_id={run_id} in eval_results.\n")

    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
