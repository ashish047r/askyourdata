import datetime as dt
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Max

from agent.core.chains import EXAMPLES_PATH, judge_answer, load_jsonl
from agent.core.evals import compare_results, percentile
from agent.core.executor import run_sql
from agent.core.graph import answer_question
from agent.core.guards import same_constraints, validate_sql
from agent.core.llm import MODEL, add_usage, embed, flush_traces
from agent.core.prompts import PROMPT_VERSION
from data.models import AdsCampaignDaily, Client, Ga4Daily

EVALS = Path(settings.BASE_DIR) / "evals"
GATE_TOLERANCE = 0.03  # fail CI if accuracy drops more than 3 points below baseline


def norm(q: str) -> str:
    return " ".join(q.lower().split())


class Command(BaseCommand):
    help = "Golden-set evaluation: execution accuracy, faithfulness (LLM judge), latency, cost; plus CI gate."

    def add_arguments(self, p):
        p.add_argument("--client", help="run every case against this client slug (CI uses 'demo')")
        p.add_argument("--tag", help="only cases with this tag (ga4 / ads / cross)")
        p.add_argument("--limit", type=int)
        p.add_argument("--judge", action="store_true", help="LLM-as-judge faithfulness on ok answers")
        p.add_argument("--workers", type=int, default=4)
        p.add_argument("--gate", help="baseline JSON; exit 1 on regression")
        p.add_argument("--write-baseline", help="save this run's summary as the new baseline")
        p.add_argument("--cache-pairs", action="store_true", help="tune the semantic cache threshold")
        p.add_argument("--calibrate-judge", action="store_true", help="judge vs human labels")
        p.add_argument("--export-judge-sample", type=int, help="write N answers for human labelling")

    def handle(self, *args, **o):
        if o["cache_pairs"]:
            return self.cache_pairs()
        if o["calibrate_judge"]:
            return self.calibrate_judge()

        cases = load_jsonl(EVALS / "golden.jsonl")
        leaked = {norm(c["question"]) for c in cases} & {norm(e["question"]) for e in load_jsonl(EXAMPLES_PATH)}
        if leaked:
            raise CommandError(f"golden questions also in few-shot examples (leakage): {leaked}")
        for c in cases:
            validate_sql(c["gold_sql"])  # a broken gold query is a dataset bug, fail fast
        if o["tag"]:
            cases = [c for c in cases if o["tag"] in c["tags"]]
        cases = cases[: o["limit"]] if o["limit"] else cases
        slugs = {o["client"] or c["client_slug"] for c in cases}
        clients = {c.slug: c.id for c in Client.objects.filter(slug__in=slugs)}
        if missing := slugs - set(clients):
            raise CommandError(f"unknown client(s) {missing}; run `python manage.py seed_demo` for 'demo'")

        for slug, cid in clients.items():
            latest = min(filter(None, [Ga4Daily.objects.filter(client_id=cid).aggregate(m=Max("date"))["m"],
                                       AdsCampaignDaily.objects.filter(client_id=cid).aggregate(m=Max("date"))["m"]]),
                         default=None)
            if latest is None or latest < dt.date.today() - dt.timedelta(days=1):
                self.stderr.write(f"WARNING: '{slug}' data ends {latest}; questions about yesterday/this week will "
                                  "fail. Run `python manage.py seed_demo` (demo) or `sync_data` first.")

        def run_case(case):
            try:
                return score_case(case)
            except Exception as e:  # one network/API failure must not kill a 100-question run
                self.stdout.write(f"✗ {case['id']} [crash] {type(e).__name__}: {str(e).splitlines()[0][:120]}")
                return {"id": case["id"], "tags": case["tags"], "question": case["question"], "status": "crash",
                        "correct": False, "retries": 0, "sql": "", "gold_sql": case["gold_sql"],
                        "error": f"{type(e).__name__}: {e}", "answer": "", "latency_ms": 0, "cost_usd": 0}

        def score_case(case):
            cid = clients[o["client"] or case["client_slug"]]
            t0 = time.perf_counter()
            vector, usage = embed(case["question"])
            res = answer_question(case["question"], cid, vector, metadata={"eval_case": case["id"]})
            usage = add_usage(usage, res["usage"])
            gold = run_sql(validate_sql(case["gold_sql"]), cid)
            correct = res["status"] == "ok" and compare_results(gold["rows"], res["rows"], case.get("ordered", False))
            out = {"id": case["id"], "tags": case["tags"], "question": case["question"], "status": res["status"],
                   "correct": correct, "retries": res["retries"], "sql": res["sql"], "gold_sql": case["gold_sql"],
                   "error": res["error"], "answer": res["answer"],
                   "clarifying_question": res.get("clarifying_question", ""), "latency_ms": round((time.perf_counter() - t0) * 1000),
                   "cost_usd": usage.get("cost_usd", 0)}
            if o["judge"] and res["status"] == "ok":
                verdict, ju = judge_answer(case["question"], res["columns"], res["rows"], res["answer"])
                out.update(faithful=verdict.faithful, judge_reason=verdict.reason, cost_usd=out["cost_usd"] + ju["cost_usd"])
            if o["export_judge_sample"]:
                out["_judge_item"] = {"question": case["question"], "columns": res["columns"], "rows": res["rows"][:50],
                                      "answer": res["answer"], "human_faithful": None}
            mark = "✓" if correct else "✗"
            self.stdout.write(f"{mark} {case['id']} [{res['status']}] {case['question'][:70]}")
            return out

        with ThreadPoolExecutor(max_workers=o["workers"]) as pool:
            results = list(pool.map(run_case, cases))
        flush_traces()

        summary = self.summarize(results)
        self.print_summary(summary)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        (EVALS / "reports").mkdir(exist_ok=True)
        report = EVALS / "reports" / f"{stamp}.json"
        report.write_text(json.dumps({"summary": summary, "results": [
            {k: v for k, v in r.items() if k != "_judge_item"} for r in results]}, indent=2))
        self.stdout.write(f"report: {report}")

        if o["export_judge_sample"]:
            items = [r["_judge_item"] for r in results if "_judge_item" in r and r["status"] == "ok"]
            out = EVALS / "judge_labels.todo.jsonl"
            out.write_text("".join(json.dumps(i) + "\n" for i in items[: o["export_judge_sample"]]))
            self.stdout.write(f"label human_faithful true/false in {out}, then save it as evals/judge_labels.jsonl")
        if o["gate"]:  # gate BEFORE writing, so a regression can never overwrite the baseline
            self.gate(summary, Path(o["gate"]))
        if o["write_baseline"]:
            Path(o["write_baseline"]).write_text(json.dumps(summary, indent=2))
            self.stdout.write(f"baseline written: {o['write_baseline']}")

    @staticmethod
    def summarize(results):
        def acc(rs):
            return round(sum(r["correct"] for r in rs) / len(rs), 3) if rs else None

        lat = [r["latency_ms"] for r in results]
        judged = [r for r in results if "faithful" in r]
        tags = sorted({t for r in results for t in r["tags"]})
        return {
            "prompt_version": PROMPT_VERSION, "model": MODEL, "cases": len(results),
            "execution_accuracy": acc(results),
            "by_tag": {t: acc([r for r in results if t in r["tags"]]) for t in tags},
            "valid_sql_rate": round(sum(r["status"] == "ok" for r in results) / len(results), 3),
            "retry_rate": round(sum(r["retries"] > 0 for r in results) / len(results), 3),
            "clarify_rate": round(sum(r["status"] == "clarify" for r in results) / len(results), 3),
            "crashed_cases": sum(r["status"] == "crash" for r in results),  # infra failures, not model mistakes
            "faithfulness": round(sum(r["faithful"] for r in judged) / len(judged), 3) if judged else None,
            "latency_p50_ms": percentile(lat, 50), "latency_p95_ms": percentile(lat, 95),
            "avg_cost_usd": round(sum(r["cost_usd"] for r in results) / len(results), 5),
            "total_cost_usd": round(sum(r["cost_usd"] for r in results), 4),
        }

    def print_summary(self, s):
        self.stdout.write("\n=== eval summary ===")
        for k, v in s.items():
            self.stdout.write(f"{k:>20}: {v}")

    def gate(self, summary, baseline_path):
        if not baseline_path.exists():
            self.stdout.write(f"no baseline at {baseline_path}; gate skipped (create one with --write-baseline)")
            return
        base = json.loads(baseline_path.read_text())
        failures = []
        if summary["execution_accuracy"] < base["execution_accuracy"] - GATE_TOLERANCE:
            failures.append(f"execution accuracy {summary['execution_accuracy']} < baseline {base['execution_accuracy']}")
        if base.get("faithfulness") and summary.get("faithfulness") is not None \
                and summary["faithfulness"] < base["faithfulness"] - GATE_TOLERANCE:
            failures.append(f"faithfulness {summary['faithfulness']} < baseline {base['faithfulness']}")
        if failures:
            self.stderr.write("EVAL GATE FAILED: " + "; ".join(failures))
            sys.exit(1)
        self.stdout.write("eval gate passed")

    def cache_pairs(self):
        """For each threshold: how many paraphrases would hit (good) and how many different questions would
        wrongly share an answer (bad), with and without the constraint guard."""
        import numpy as np

        pairs = load_jsonl(EVALS / "cache_pairs.jsonl")
        rows = []
        for p in pairs:
            a, b = np.array(embed(p["a"])[0]), np.array(embed(p["b"])[0])
            dist = 1 - float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))
            rows.append((dist, p["same"], same_constraints(p["a"], p["b"])))
            self.stdout.write(f"{dist:.3f} same={p['same']!s:5} guard={rows[-1][2]!s:5} {p['a']} | {p['b']}")
        self.stdout.write("\nthreshold  good_hits  false_hits(no guard)  false_hits(with guard)")
        for t in (0.02, 0.04, 0.06, 0.08, 0.10, 0.12, 0.15, 0.20):
            good = sum(d <= t and s and g for d, s, g in rows)
            bad_raw = sum(d <= t and not s for d, s, g in rows)
            bad = sum(d <= t and not s and g for d, s, g in rows)
            self.stdout.write(f"{t:>9.2f}  {good:>9}  {bad_raw:>20}  {bad:>22}")
        self.stdout.write(f"current SEMANTIC_CACHE_MAX_DISTANCE={settings.SEMANTIC_CACHE_MAX_DISTANCE}: pick the "
                          "largest threshold with 0 false hits (with guard).")

    def calibrate_judge(self):
        path = EVALS / "judge_labels.jsonl"
        items = [i for i in load_jsonl(path) if i.get("human_faithful") is not None] if path.exists() else []
        if not items:
            raise CommandError("no labels: run `run_evals --export-judge-sample 30`, label the file, save as "
                               "evals/judge_labels.jsonl")
        agree, tp, fp, fn, tn = 0, 0, 0, 0, 0
        for i in items:
            v, _ = judge_answer(i["question"], i["columns"], i["rows"], i["answer"])
            h = i["human_faithful"]
            agree += v.faithful == h
            tp += v.faithful and h
            fp += v.faithful and not h
            fn += (not v.faithful) and h
            tn += (not v.faithful) and not h
        self.stdout.write(f"judge-human agreement: {agree}/{len(items)} = {agree / len(items):.1%}")
        self.stdout.write(f"judge says faithful & human agrees: {tp} | judge too lenient: {fp} | "
                          f"judge too strict: {fn} | both unfaithful: {tn}")
