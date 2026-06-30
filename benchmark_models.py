"""Free-model benchmark.

Runs every candidate free model over a curated evaluation set
(benchmark/eval_jobs.yaml) and ranks them by how well they separate in-scope
from out-of-scope jobs, plus token usage, latency and parse reliability — so we
can pick the best free model for our objective (senior Data Scientist / ML-AI
Engineer matching).

This is a one-off benchmarking tool, NOT part of the scheduled pipeline: it
fans out across ALL models on purpose (which would be token-wasteful as a
steady-state strategy). Once a winner is chosen, pin it via OPENROUTER_MODEL.

Usage:
    python benchmark_models.py                      # test the configured free models
    python benchmark_models.py --models a:free,b:free
    python benchmark_models.py --concurrency 6 --threshold 70

Requires OPENROUTER_API_KEY in the environment / .env.
"""

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
import yaml
from dotenv import load_dotenv

sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from src import matcher
from src.settings import env_str
from main import format_candidate_profile

EVAL_PATH = "benchmark/eval_jobs.yaml"
DEFAULT_THRESHOLD = 70  # match_score >= threshold => model predicts "in scope"


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def load_eval_jobs(path=EVAL_PATH):
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("jobs", [])


def load_profile():
    """Load the candidate profile the same way the pipeline does, falling back
    to the example config so the benchmark runs even without a private config."""
    path = env_str("KEYWORDS_CONFIG_PATH", "config/keywords.yaml")
    if not os.path.exists(path):
        path = "config/keywords.example.yaml"
    with open(path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    return format_candidate_profile(config.get("perfil_candidato"))


def candidate_models(cli_models=None):
    """Models to benchmark: --models, else BENCHMARK_MODELS, else the configured
    OpenRouter list. Each is tested INDIVIDUALLY (no fallback chain here)."""
    if cli_models:
        return [m.strip() for m in cli_models.split(",") if m.strip()]
    env = os.getenv("BENCHMARK_MODELS")
    if env:
        return [m.strip() for m in env.split(",") if m.strip()]
    return list(matcher._openrouter_models())


# --------------------------------------------------------------------------- #
# Calling a single model (captures usage + latency)
# --------------------------------------------------------------------------- #
def call_model(model, prompt):
    headers = {
        "Authorization": f"Bearer {matcher._openrouter_key()}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": matcher.llm_temperature(),
        "response_format": {"type": "json_object"},
        "max_tokens": matcher.openrouter_max_tokens(),
        "reasoning": {"effort": "low"},
    }
    t0 = time.monotonic()
    try:
        resp = requests.post(
            matcher.OPENROUTER_URL, headers=headers, json=body,
            timeout=matcher.llm_request_timeout(),
        )
        latency = time.monotonic() - t0
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}", "latency": latency, "usage": {}}
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return {"content": content, "usage": data.get("usage") or {}, "latency": latency, "error": None}
    except requests.RequestException as exc:
        return {"error": f"network: {str(exc)[:100]}", "latency": time.monotonic() - t0, "usage": {}}
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        return {"error": f"bad response: {exc}", "latency": time.monotonic() - t0, "usage": {}}


# --------------------------------------------------------------------------- #
# Scoring (pure — unit tested)
# --------------------------------------------------------------------------- #
def build_row(model, job, call_result, threshold=DEFAULT_THRESHOLD):
    """Turn a raw call result into a scored row. Pure: no network."""
    parsed = None
    if not call_result.get("error") and call_result.get("content"):
        parsed = matcher._parse_result(call_result["content"])
    match_score = parsed.get("match_score") if parsed else None
    usage = call_result.get("usage") or {}
    expected = bool(job.get("expected_in_scope"))

    if match_score is None:
        predicted, correct = None, None
    else:
        predicted = match_score >= threshold
        correct = predicted == expected

    return {
        "model": model,
        "job_title": job.get("job_title"),
        "expected_in_scope": expected,
        "match_score": match_score,
        "predicted_in_scope": predicted,
        "correct": correct,
        "total_tokens": usage.get("total_tokens"),
        "latency": round(call_result.get("latency", 0.0), 2),
        "error": call_result.get("error"),
        "parse_fail": (not call_result.get("error")) and parsed is None,
    }


def _avg(values):
    values = [v for v in values if v is not None]
    return (sum(values) / len(values)) if values else None


def aggregate(rows):
    """Per-model stats, ranked by accuracy desc then avg tokens asc. Pure."""
    by_model = {}
    for r in rows:
        by_model.setdefault(r["model"], []).append(r)

    stats = []
    for model, rs in by_model.items():
        n = len(rs)
        scored = [r for r in rs if r["correct"] is not None]
        correct = sum(1 for r in scored if r["correct"])
        in_scores = [r["match_score"] for r in rs if r["match_score"] is not None and r["expected_in_scope"]]
        out_scores = [r["match_score"] for r in rs if r["match_score"] is not None and not r["expected_in_scope"]]
        mean_in, mean_out = _avg(in_scores), _avg(out_scores)
        stats.append({
            "model": model,
            "n": n,
            "scored": len(scored),
            "correct": correct,
            # Effective accuracy over ALL eval jobs: a model that cannot return a
            # parseable answer is useless, so failures count against it. This is
            # the ranking key. `accuracy` (over scored) is kept as a quality hint.
            "effective_accuracy": (correct / n) if n else None,
            "accuracy": (correct / len(scored)) if scored else None,
            "coverage": (len(scored) / n) if n else None,
            "errors": sum(1 for r in rs if r["error"]),
            "parse_fails": sum(1 for r in rs if r["parse_fail"]),
            "avg_total_tokens": _avg([r["total_tokens"] for r in rs]),
            "sum_total_tokens": sum(r["total_tokens"] for r in rs if r["total_tokens"]),
            "avg_latency": _avg([r["latency"] for r in rs]),
            "mean_in_score": mean_in,
            "mean_out_score": mean_out,
            "separation": (mean_in - mean_out) if (mean_in is not None and mean_out is not None) else None,
        })

    stats.sort(key=lambda s: (-(s["effective_accuracy"] if s["effective_accuracy"] is not None else -1),
                              s["avg_total_tokens"] if s["avg_total_tokens"] is not None else 1e9))
    return stats


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _fmt(value, spec=""):
    return ("{:" + spec + "}").format(value) if value is not None else "-"


def render_markdown(stats, rows, threshold, models, n_jobs):
    lines = [
        "# Free-model benchmark",
        "",
        f"- Models: {len(models)} | Eval jobs: {n_jobs} | Threshold (in-scope): {threshold}",
        f"- Ranking metric: **effective accuracy** = correct answers over ALL "
        f"{n_jobs} jobs (failures count against). Coverage = share that returned "
        f"a parseable answer; quality = accuracy among those.",
        "",
        "## Ranking",
        "",
        "| # | Model | Eff. acc (correct/total) | Coverage | Quality | Sep (in-out) | Avg tokens | Avg latency (s) | Errors | Parse fails |",
        "|---|-------|--------------------------|----------|---------|--------------|------------|-----------------|--------|-------------|",
    ]
    for i, s in enumerate(stats, 1):
        eff = f"{s['effective_accuracy']*100:.0f}% ({s['correct']}/{s['n']})" if s["effective_accuracy"] is not None else "-"
        cov = f"{s['coverage']*100:.0f}%" if s["coverage"] is not None else "-"
        qual = f"{s['accuracy']*100:.0f}%" if s["accuracy"] is not None else "-"
        lines.append(
            f"| {i} | `{s['model']}` | {eff} | {cov} | {qual} | {_fmt(s['separation'], '.0f')} | "
            f"{_fmt(s['avg_total_tokens'], '.0f')} | {_fmt(s['avg_latency'], '.2f')} | "
            f"{s['errors']} | {s['parse_fails']} |"
        )

    lines += ["", "## Per-job scores", "",
              "| Model | Job | Expected | Score | Correct |",
              "|-------|-----|----------|-------|---------|"]
    for r in rows:
        exp = "in" if r["expected_in_scope"] else "out"
        if r["error"]:
            mark = f"ERR ({r['error']})"
        elif r["match_score"] is None:
            mark = "parse-fail"
        else:
            mark = "✅" if r["correct"] else "❌"
        lines.append(f"| `{r['model']}` | {r['job_title']} | {exp} | {_fmt(r['match_score'])} | {mark} |")
    return "\n".join(lines) + "\n"


def print_console(stats, threshold):
    print(f"\n=== Free-model benchmark (threshold={threshold}) ===")
    print(f"{'rank':<5}{'model':<48}{'eff_acc':<14}{'cov':<6}{'sep':<6}{'avg_tok':<9}{'lat':<7}{'err':<5}{'pfail'}")
    for i, s in enumerate(stats, 1):
        eff = f"{s['effective_accuracy']*100:.0f}% {s['correct']}/{s['n']}" if s["effective_accuracy"] is not None else "-"
        cov = f"{s['coverage']*100:.0f}%" if s["coverage"] is not None else "-"
        print(f"{i:<5}{s['model']:<48}{eff:<14}{cov:<6}"
              f"{_fmt(s['separation'], '.0f'):<6}{_fmt(s['avg_total_tokens'], '.0f'):<9}"
              f"{_fmt(s['avg_latency'], '.2f'):<7}{s['errors']:<5}{s['parse_fails']}")


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run(models, jobs, profile, concurrency, threshold):
    tasks = [(m, j) for m in models for j in jobs]
    rows = []
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {}
        for model, job in tasks:
            prompt = matcher._build_prompt(job, profile)
            futures[ex.submit(call_model, model, prompt)] = (model, job)
        done = 0
        for fut in as_completed(futures):
            model, job = futures[fut]
            rows.append(build_row(model, job, fut.result(), threshold))
            done += 1
            print(f"\r  {done}/{len(tasks)} calls done", end="", flush=True)
    print()
    return rows


def main():
    parser = argparse.ArgumentParser(description="Benchmark free LLM models for job matching.")
    parser.add_argument("--models", help="Comma-separated model ids (overrides config).")
    parser.add_argument("--concurrency", type=int, default=4, help="Parallel calls (respect rate limits).")
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD, help="In-scope match_score cutoff.")
    parser.add_argument("--eval", default=EVAL_PATH, help="Path to the eval YAML.")
    args = parser.parse_args()

    load_dotenv()
    if not matcher._openrouter_key():
        print("ERROR: OPENROUTER_API_KEY not set (.env). Cannot benchmark.", file=sys.stderr)
        sys.exit(1)

    jobs = load_eval_jobs(args.eval)
    profile = load_profile()
    models = candidate_models(args.models)
    if not jobs or not models:
        print("ERROR: no jobs or no models to benchmark.", file=sys.stderr)
        sys.exit(1)

    print(f"Benchmarking {len(models)} model(s) over {len(jobs)} job(s), "
          f"concurrency={args.concurrency}, threshold={args.threshold}...")
    rows = run(models, jobs, profile, args.concurrency, args.threshold)
    stats = aggregate(rows)
    print_console(stats, args.threshold)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    os.makedirs("data", exist_ok=True)
    out = os.path.join("data", f"benchmark_{ts}.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(render_markdown(stats, rows, args.threshold, models, len(jobs)))
    print(f"\nReport written to {out}")


if __name__ == "__main__":
    main()
