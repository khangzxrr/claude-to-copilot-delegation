#!/usr/bin/env python3
"""Benchmark: Claude alone vs Claude + /delegate on the same tasks.

Each task is bench/tasks/<name>/ with:
  repo/     starting project (copied fresh for every run; contains TASK.md)
  hidden/   acceptance tests never shown to the agents; copied in after the run
            (*.test.js run with `node --test`, *.py run with `python3 -m unittest`)

For every (task, mode, run) it records Claude's cost/tokens (from `claude -p --output-format json`),
Copilot's AI credits (from the delegate logs), wall time, and the hidden test pass rate.

  python3 bench/run.py                         # all tasks, both modes, 1 run each
  python3 bench/run.py --tasks cron --runs 3 --jobs 2 --model sonnet
"""

import argparse
import concurrent.futures as cf
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

BENCH = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH.parent / "skills" / "delegate"))
from detect import parse_test_counts  # noqa: E402

PROMPTS = {
    "alone": "Implement the task described in TASK.md in this repository. Verify your work before finishing.",
    "delegate": "/delegate Implement the task described in TASK.md in this repository.",
    "force": "/delegate force Implement the task described in TASK.md in this repository.",
}
DEFAULT_MODES = ["alone", "delegate"]
ALLOWED_TOOLS = ["Bash", "Read", "Edit", "Write", "Glob", "Grep", "Skill"]


def sh(cmd, cwd, **kw):
    return subprocess.run(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, **kw)


def run_hidden_tests(task, work):
    """Copy the hidden tests in (only now, after the agent finished) and run them."""
    hidden = BENCH / "tasks" / task / "hidden"
    if any(hidden.glob("*.py")):
        target = work / "_hidden_tests"
        shutil.copytree(hidden, target)
        (target / "__init__.py").touch()
        cmd = ["python3", "-m", "unittest", "discover", "-v", "-s", "_hidden_tests", "-t", "."]
    else:
        target = work / ".hidden"
        shutil.copytree(hidden, target)
        cmd = ["node", "--test", *sorted(str(p.relative_to(work)) for p in target.glob("*.test.js"))]
    try:
        output = sh(cmd, work, timeout=300).stdout
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + "\nTIMEOUT"
    counts = parse_test_counts(output) or {"total": 0, "passed": 0}
    return counts, output


def run_one(task, mode, n, out_dir, model, timeout):
    work = out_dir / f"{task}-{mode}-{n}"
    shutil.copytree(BENCH / "tasks" / task / "repo", work)
    sh(["git", "init", "-q"], work)
    sh(["git", "add", "-A"], work)
    sh(["git", "-c", "user.name=bench", "-c", "user.email=bench@local", "commit", "-qm", "start"], work)

    argv = ["claude", "-p", PROMPTS[mode], "--output-format", "json", "--no-session-persistence",
            "--permission-mode", "acceptEdits", "--allowedTools", *ALLOWED_TOOLS]
    if model:
        argv += ["--model", model]
    started = time.time()
    try:
        # No live-view windows during benchmark runs.
        proc = sh(argv, work, timeout=timeout, env={**os.environ, "DELEGATE_LIVE_VIEW": "off"})
        raw = proc.stdout
    except subprocess.TimeoutExpired as exc:
        raw = exc.stdout or ""
    wall = round(time.time() - started)
    raw = raw if isinstance(raw, str) else raw.decode()
    (work.parent / f"{work.name}.claude.json").write_text(raw)
    # The JSON result is the last line starting with "{" (the CLI may print warnings before it).
    json_line = next((line for line in reversed(raw.splitlines()) if line.startswith("{")), "")
    try:
        claude = json.loads(json_line)
    except ValueError:
        claude = {"is_error": True, "result": raw[-2000:]}
    usage = claude.get("usage") or {}

    counts, output = run_hidden_tests(task, work)
    (work.parent / f"{work.name}.hidden.txt").write_text(output)

    logs = work / ".delegate" / "logs"
    ends = [line for f in (sorted(logs.glob("*.log")) if logs.exists() else [])
            if f.name != "latest.log" and not f.name.startswith("test-")
            for line in f.read_text(errors="ignore").splitlines() if line.startswith("=== end:")]
    credits = [float(m[1]) for line in ends if (m := re.search(r"AI credits ([\d.]+)", line))]
    return {
        "task": task, "mode": mode, "run": n,
        "ok": not claude.get("is_error", False),
        "cost_usd": claude.get("total_cost_usd"),
        "input_tokens": usage.get("input_tokens", 0),
        "cache_write_tokens": usage.get("cache_creation_input_tokens", 0),
        "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "turns": claude.get("num_turns"),
        "wall_seconds": wall,
        "worker_rounds": sum(1 for line in ends if not line.startswith(
            ("=== end: review", "=== end: test review", "=== end: test writer"))),
        "reviews": sum(1 for line in ends if line.startswith("=== end: review")),
        "test_reviews": sum(1 for line in ends if line.startswith("=== end: test review")),
        "test_writer_runs": sum(1 for line in ends if line.startswith("=== end: test writer")),
        "worker_credits": round(sum(credits), 2) if credits else 0.0,
        "hidden_passed": counts.get("passed", 0),
        "hidden_total": counts.get("total", 0),
        "permission_denials": len(claude.get("permission_denials") or []),
        "models": sorted((claude.get("modelUsage") or {}).keys()),
    }


def _stat(values, fmt):
    values = [v for v in values if v is not None]
    if not values:
        return "n/a"
    mean = fmt.format(statistics.mean(values))
    return mean if len(values) == 1 else f"{mean} ({fmt.format(min(values))}–{fmt.format(max(values))})"


def summarize(results):
    lines = [
        "| task | mode | runs | hidden tests | Claude cost | Copilot AI credits | Claude output tokens | turns | wall |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    groups, expected = {}, {}
    for r in results:
        groups.setdefault((r["task"], r["mode"]), []).append(r)
        # A solution that fails to import reports 1 failing test; use the full count as denominator.
        expected[r["task"]] = max(expected.get(r["task"], 0), r["hidden_total"])
    for (task, mode), rs in sorted(groups.items()):
        passed = sum(r["hidden_passed"] for r in rs)
        total = expected[task] * len(rs)
        errors = sum(1 for r in rs if not r["ok"])
        lines.append(
            f"| {task} | {mode} | {len(rs)}{f' ({errors} errored)' if errors else ''} | {passed}/{total} "
            f"| {_stat([r['cost_usd'] for r in rs], '${:.2f}')} "
            f"| {_stat([r['worker_credits'] for r in rs], '{:.1f}')} "
            f"| {_stat([r['output_tokens'] for r in rs], '{:,.0f}')} "
            f"| {_stat([r['turns'] for r in rs], '{:.0f}')} | {_stat([r['wall_seconds'] for r in rs], '{:.0f}s')} |")
    lines += ["", "Values are means, with (min–max) over the runs.", "", "Per run:", "",
              "| task | mode | run | hidden | Claude cost | credits | rounds | reviews | turns | wall |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(results, key=lambda r: (r["task"], r["mode"], r["run"])):
        cost = f"${r['cost_usd']:.3f}" if r["cost_usd"] is not None else "n/a"
        lines.append(f"| {r['task']} | {r['mode']}{'' if r['ok'] else ' (error)'} | {r['run']} "
                     f"| {r['hidden_passed']}/{expected[r['task']]} | {cost} | {r['worker_credits']} "
                     f"| {r['worker_rounds']} | {r['reviews']} | {r['turns']} | {r['wall_seconds']}s |")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tasks", nargs="*", help="task names (default: all)")
    parser.add_argument("--modes", nargs="*", default=DEFAULT_MODES, choices=list(PROMPTS),
                        help="alone, delegate (size check decides), force (always delegate)")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--jobs", type=int, default=1, help="parallel runs")
    parser.add_argument("--model", help="Claude model for both modes (default: your Claude Code default)")
    parser.add_argument("--timeout", type=int, default=2400, help="seconds per Claude run")
    args = parser.parse_args()

    tasks = args.tasks or sorted(p.name for p in (BENCH / "tasks").iterdir() if p.is_dir())
    out_dir = BENCH / "results" / time.strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True)
    jobs = [(t, m, n) for t in tasks for m in args.modes for n in range(1, args.runs + 1)]
    print(f"{len(jobs)} runs -> {out_dir}", flush=True)

    results = []
    with cf.ThreadPoolExecutor(args.jobs) as pool:
        futures = {pool.submit(run_one, t, m, n, out_dir, args.model, args.timeout): (t, m, n) for t, m, n in jobs}
        for fut in cf.as_completed(futures):
            r = fut.result()
            results.append(r)
            print(f"done {r['task']}/{r['mode']}#{r['run']}: hidden {r['hidden_passed']}/{r['hidden_total']}, "
                  f"cost {r['cost_usd']}, credits {r['worker_credits']}, {r['wall_seconds']}s", flush=True)
            (out_dir / "results.json").write_text(json.dumps(results, indent=2))

    summary = summarize(results)
    (out_dir / "summary.md").write_text(summary + "\n")
    print("\n" + summary)


if __name__ == "__main__":
    main()
