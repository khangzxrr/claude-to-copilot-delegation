#!/usr/bin/env python3
"""Live demo: Claude alone vs Claude + /delegate on the same task, side by side.

Both runs start at once, each on a fresh copy of a benchmark task. The screen shows, per run,
Claude's turns and tokens as they happen and what Claude and Copilot are doing; at the end, Claude's
cost, Copilot's AI credits and the hidden acceptance tests. Every run is recorded under
bench/demo-runs/, so it can be replayed (faster, if you like) without calling any model: a backup
for when a live run is too slow or the network is not cooperating.

  python3 bench/demo.py                           # live: spreadsheet task, alone vs delegate
  python3 bench/demo.py --task cron --modes alone force --model sonnet
  python3 bench/demo.py --replay --speed 4        # replay the latest recording at 4x
  python3 bench/demo.py --replay bench/demo-runs/20260924-120000-spreadsheet

When every run has finished, the screen switches to the result: each hidden test, passed or failed,
per run, and the code each run wrote. Keys: v switch between the result and the activity · r run again
(a new live run, or the same replay) · p replay the latest recording · q quit
(stops running Claude and Copilot processes). Needs `rich` (pip install rich).
"""

import argparse
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
import threading
import time
import tty
from collections import deque
from contextlib import contextmanager
from pathlib import Path

BENCH = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH))
from run import ALLOWED_TOOLS, PROMPTS, run_hidden_tests, sh  # noqa: E402

DEMO_DIR = BENCH / "demo-runs"
TITLES = {"alone": "Claude alone", "delegate": "Claude + /delegate", "force": "Claude + /delegate force"}
COLORS = {"alone": "dark_orange", "delegate": "cyan", "force": "cyan"}
FINISHED = ("done", "error", "stopped")
# GitHub bills Copilot usage in AI credits at a fixed $0.01 each, for plan allowances and extra usage alike
# (docs.github.com/copilot/concepts/billing/usage-based-billing-for-individuals). --credit-usd overrides it.
CREDIT_USD = 0.01


# ---------------------------------------------------------------- events
# Both live runs and replays feed the screen the same small events: {"t", "mode", "kind", ...}.

def tokens_read(usage):
    """Tokens Claude processed as input: new input plus cache writes and reads (re-read every turn)."""
    return sum(usage.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))


def describe_block(block, root=""):
    if block.get("type") == "text":
        line = next((line for line in (block.get("text") or "").splitlines() if line.strip()), "")
        return f"💬 {line.strip()}" if line else ""
    if block.get("type") == "tool_use":
        args = block.get("input") or {}
        detail = next((args[k] for k in ("command", "file_path", "skill", "pattern", "path", "description")
                       if isinstance(args.get(k), str) and args[k].strip()), "")
        if root:
            detail = detail.replace(root, "").replace(root.rstrip("/"), ".")
        detail = detail.strip().splitlines()[0] if detail.strip() else ""
        return f"▸ {block.get('name', '?')} {detail}".rstrip()
    return ""


def claude_events(event, root=""):
    """Events from one line of `claude -p --output-format stream-json`."""
    kind = event.get("type")
    if kind == "system" and event.get("subtype") == "init":
        return [{"kind": "init", "model": event.get("model")}]
    if kind == "assistant":
        msg = event.get("message") or {}
        # One API message arrives as several events (one per content block) with the same id and usage.
        out = [{"kind": "turn", "id": msg.get("id"), "tokens_read": tokens_read(msg.get("usage") or {})}]
        out += [{"kind": "claude", "text": text} for block in msg.get("content") or []
                if (text := describe_block(block, root))]
        return out
    if kind == "result":
        usage = event.get("usage") or {}
        return [{"kind": "result", "cost": event.get("total_cost_usd"), "tokens_read": tokens_read(usage),
                 "output_tokens": usage.get("output_tokens"), "turns": event.get("num_turns"),
                 "ok": not event.get("is_error", False)}]
    return []


PREFIX = re.compile(r"^(\[[^\]]+\] )?")
ACTIVITY = re.compile(r"^\d\d:\d\d:\d\d (▸|💬|!|  ✗)")


def copilot_event(line):
    """An event from one line of a delegate live log, or None for noise (tool output, turn markers)."""
    prefix = PREFIX.match(line).group(1) or ""
    body = line[len(prefix):]
    if body.startswith("=== end:"):
        credits = re.search(r"AI credits ([\d.]+)", body)
        return {"kind": "copilot_end", "text": prefix + body[4:], "credits": float(credits[1]) if credits else 0.0}
    if body.startswith("=== "):
        return {"kind": "copilot", "text": prefix + body[4:]}
    if ACTIVITY.match(body) and body[9:].strip() != "💬":
        return {"kind": "copilot", "text": prefix + body[9:].strip()}
    return None


class LogTail:
    """Reads new complete lines from the delegate logs of one working copy."""

    def __init__(self, work):
        self.dir = Path(work) / ".delegate" / "logs"
        self.offsets = {}

    def poll(self):
        out = []
        if not self.dir.is_dir():
            return out
        for path in sorted(self.dir.glob("*.log"), key=lambda p: p.stat().st_mtime):
            if path.name == "latest.log" or path.name.startswith("test-"):
                continue
            offset = self.offsets.get(path.name, 0)
            try:
                with open(path, "rb") as fh:
                    fh.seek(offset)
                    data = fh.read()
            except OSError:
                continue
            cut = data.rfind(b"\n") + 1
            self.offsets[path.name] = offset + cut
            out += [ev for line in data[:cut].decode(errors="replace").splitlines() if (ev := copilot_event(line))]
        return out


TAP = re.compile(r"^(not )?ok \d+ - (.+?)(?: # .*)?$")
UNITTEST = re.compile(r"^(test\w*) \([\w.]+\) \.\.\. (ok|FAIL|ERROR)")
NOT_WRITTEN = (".delegate", ".hidden", "_hidden_tests", "node_modules")


def hidden_cases(output):
    """[name, passed] per hidden test, from node --test (TAP) or unittest -v output."""
    cases = []
    for line in output.splitlines():
        if match := TAP.match(line):
            cases.append([match[2], not match[1]])
        elif match := UNITTEST.match(line):
            cases.append([match[1].removeprefix("test_").replace("_", " "), match[2] == "ok"])
    return cases


def changed_files(work):
    """[path, added, deleted] per file changed since the working copy's first commit (untracked included)."""
    first = sh(["git", "rev-list", "--max-parents=0", "HEAD"], work).stdout.split()
    if not first:
        return []
    with tempfile.TemporaryDirectory() as tmp:
        # A throwaway index: the working copy's own index is left alone.
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        sh(["git", "add", "-A", "--", ".", *(f":(exclude){p}" for p in NOT_WRITTEN)], work, env=env)
        out = sh(["git", "diff", "--cached", "--numstat", first[-1]], work, env=env).stdout
    files = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            files.append([parts[2], *(int(n) if n.isdigit() else 0 for n in parts[:2])])
    return files


def is_test_file(path):
    return bool(re.search(r"(^|/)(tests?|__tests__)/|\.(test|spec)\.|_test\.|(^|/)test_", path))


class Lane:
    """What the screen knows about one run."""

    def __init__(self, mode):
        self.mode = mode
        self.status = "waiting"
        self.start = self.end = None
        self.model = None
        self.ids = set()
        self.turns = 0
        self.tokens_read = 0
        self.output_tokens = self.cost = self.hidden = self.cases = self.files = None
        self.ok = True
        self.copilot_runs = 0
        self.credits = 0.0
        self.activity = deque(maxlen=300)

    def apply(self, ev):
        kind, t = ev["kind"], ev.get("t", 0.0)
        if kind == "status":
            self.status = ev["status"]
            if self.start is None:
                self.start = t
            if self.status in FINISHED:
                self.end = t
            if ev.get("text"):
                self.activity.append(("note", t, ev["text"]))
        elif kind == "init":
            self.model = ev.get("model")
        elif kind == "turn" and ev.get("id") not in self.ids:
            self.ids.add(ev.get("id"))
            self.turns = len(self.ids)
            self.tokens_read += ev.get("tokens_read") or 0
        elif kind == "claude":
            self.activity.append(("claude", t, ev["text"]))
        elif kind == "result":
            # The final usage is exact (streamed usage has placeholder output counts).
            self.cost, self.output_tokens, self.ok = ev.get("cost"), ev.get("output_tokens"), ev.get("ok", True)
            self.tokens_read = ev.get("tokens_read") or self.tokens_read
            self.turns = ev.get("turns") or self.turns
        elif kind == "copilot":
            self.activity.append(("copilot", t, ev["text"]))
        elif kind == "copilot_end":
            self.copilot_runs += 1
            self.credits += ev.get("credits") or 0.0
            self.activity.append(("copilot", t, ev["text"]))
        elif kind == "hidden":
            self.hidden = (ev.get("passed", 0), ev.get("total", 0))
            self.activity.append(("note", t, f"hidden acceptance tests: {self.hidden[0]}/{self.hidden[1]} passed"))
        elif kind == "cases":
            self.cases = ev.get("cases") or []
        elif kind == "changes":
            self.files = ev.get("files") or []

    def copilot_usd(self):
        return self.credits * CREDIT_USD

    def elapsed(self, now):
        if self.start is None:
            return 0.0
        return (self.end if self.end is not None else now) - self.start


# ---------------------------------------------------------------- sessions

class Session:
    """A live run or a replay: lanes, a clock and the thread(s) feeding them."""

    def __init__(self, meta, speed=1.0, replay=False):
        self.meta, self.speed, self.replay = meta, speed, replay
        self.lanes = {mode: Lane(mode) for mode in meta["modes"]}
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.t0 = time.monotonic()
        self.view = None  # None: the results once every run finished; "live" / "results" once toggled

    def now(self):
        return (time.monotonic() - self.t0) * self.speed

    def apply(self, ev):
        with self.lock:
            self.lanes[ev["mode"]].apply(ev)

    def finished(self):
        return all(lane.status in FINISHED for lane in self.lanes.values())

    def showing_results(self):
        return self.finished() and self.view != "live"

    def clock(self):
        if self.finished():
            return max((lane.end or 0.0) for lane in self.lanes.values())
        return self.now()


class LiveSession(Session):
    def __init__(self, task, modes, model, timeout):
        run_dir = DEMO_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{task}"
        run_dir.mkdir(parents=True)
        meta = {"task": task, "modes": modes, "model": model, "dir": str(run_dir)}
        (run_dir / "meta.json").write_text(json.dumps(meta, indent=2))
        super().__init__(meta)
        self.dir, self.timeout = run_dir, timeout
        self.record = open(run_dir / "events.jsonl", "a", buffering=1)
        self.procs = {}
        for mode in modes:
            threading.Thread(target=self._lane, args=(mode,), daemon=True).start()

    def emit(self, mode, **ev):
        ev = {"t": round(self.now(), 2), "mode": mode, **ev}
        with self.lock:
            self.record.write(json.dumps(ev) + "\n")
            self.lanes[mode].apply(ev)

    def _lane(self, mode):
        work = self.dir / mode
        try:
            shutil.copytree(BENCH / "tasks" / self.meta["task"] / "repo", work)
            sh(["git", "init", "-q"], work)
            sh(["git", "add", "-A"], work)
            sh(["git", "-c", "user.name=demo", "-c", "user.email=demo@local", "commit", "-qm", "start"], work)
        except OSError as exc:
            self.emit(mode, kind="status", status="error", text=f"setup failed: {exc}")
            return
        argv = ["claude", "-p", PROMPTS[mode], "--output-format", "stream-json", "--verbose",
                "--no-session-persistence", "--permission-mode", "acceptEdits", "--allowedTools", *ALLOWED_TOOLS]
        if self.meta["model"]:
            argv += ["--model", self.meta["model"]]
        self.emit(mode, kind="status", status="running", text=f"prompt: {PROMPTS[mode]}")
        try:
            # The TUI shows Copilot's work itself: no live-view windows.
            proc = subprocess.Popen(argv, cwd=work, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, text=True, bufsize=1, errors="replace",
                                    start_new_session=True, env={**os.environ, "DELEGATE_LIVE_VIEW": "off"})
        except FileNotFoundError:
            self.emit(mode, kind="status", status="error", text="`claude` is not on PATH")
            return
        self.procs[mode] = proc
        timer = threading.Timer(self.timeout, lambda: self._kill(mode))
        timer.start()

        tail, tail_done = LogTail(work), threading.Event()

        def follow():
            while not tail_done.wait(0.5):
                for ev in tail.poll():
                    self.emit(mode, **ev)
        follower = threading.Thread(target=follow, daemon=True)
        follower.start()

        root, got_result = f"{work}/", False
        with open(self.dir / f"{mode}.stream.jsonl", "w") as raw:
            for line in proc.stdout:
                raw.write(line)
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    for ev in claude_events(event, root):
                        got_result |= ev["kind"] == "result"
                        self.emit(mode, **ev)
        proc.wait()
        timer.cancel()
        tail_done.set()
        follower.join()
        for ev in tail.poll():
            self.emit(mode, **ev)
        if self.stopped.is_set():
            self.emit(mode, kind="status", status="stopped", text="stopped")
            return
        self.emit(mode, kind="changes", files=changed_files(work))
        self.emit(mode, kind="status", status="testing", text="running the hidden acceptance tests")
        counts, output = run_hidden_tests(self.meta["task"], work)
        (self.dir / f"{mode}.hidden.txt").write_text(output)
        self.emit(mode, kind="hidden", passed=counts.get("passed", 0), total=counts.get("total", 0))
        self.emit(mode, kind="cases", cases=hidden_cases(output))
        self.emit(mode, kind="status", status="done" if got_result else "error",
                  text=None if got_result else f"claude exited with code {proc.returncode} and no result")

    def _kill(self, mode):
        proc = self.procs.get(mode)
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        # Copilot workers run in their own sessions (and may outlive Claude): stop whatever runs in the copy.
        work = str(self.dir / mode)
        for pid in (p for p in os.listdir("/proc") if p.isdigit()) if os.path.isdir("/proc") else []:
            try:
                if os.readlink(f"/proc/{pid}/cwd").startswith(work):
                    os.kill(int(pid), signal.SIGTERM)
            except OSError:
                continue

    def stop(self):
        self.stopped.set()
        for mode in self.lanes:
            self._kill(mode)


class ReplaySession(Session):
    def __init__(self, run_dir, speed):
        run_dir = Path(run_dir)
        meta = json.loads((run_dir / "meta.json").read_text())
        super().__init__(meta, speed=speed, replay=True)
        self.dir = run_dir
        events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines() if line.strip()]
        events = self._backfill(sorted(events, key=lambda e: e["t"]))
        threading.Thread(target=self._play, args=(events,), daemon=True).start()

    def _backfill(self, events):
        """Older recordings have no per-test results or file changes: read them from the run's files."""
        recorded = {(e["mode"], e["kind"]) for e in events}
        out = []
        for ev in events:
            out.append(ev)
            mode = ev["mode"]
            if ev["kind"] != "hidden":
                continue
            hidden_txt, work = self.dir / f"{mode}.hidden.txt", self.dir / mode
            if (mode, "cases") not in recorded and hidden_txt.is_file():
                out.append({**ev, "kind": "cases", "cases": hidden_cases(hidden_txt.read_text(errors="replace"))})
            if (mode, "changes") not in recorded and (work / ".git").is_dir():
                out.append({**ev, "kind": "changes", "files": changed_files(work)})
        return out

    def _play(self, events):
        for ev in events:
            wait = ev["t"] / self.speed - (time.monotonic() - self.t0)
            if wait > 0 and self.stopped.wait(wait):
                return
            self.apply(ev)
        # A recording of a stopped run: close the lanes it left open.
        for mode, lane in self.lanes.items():
            if lane.status not in FINISHED:
                self.apply({"t": events[-1]["t"] if events else 0.0, "mode": mode, "kind": "status", "status": "stopped"})

    def stop(self):
        self.stopped.set()


def latest_recording():
    runs = sorted(p for p in DEMO_DIR.glob("*") if (p / "events.jsonl").is_file()) if DEMO_DIR.is_dir() else []
    return runs[-1] if runs else None


# ---------------------------------------------------------------- screen

def fmt_time(seconds):
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes}:{seconds:02d}"


def change(base, value):
    if not base or value is None:
        return ""
    pct = (value - base) / base * 100
    return f"[{'green' if pct < 0 else 'red'}]{pct:+.0f}%[/]"


def lane_panel(lane, session, activity_lines):
    from rich.console import Group
    from rich.panel import Panel
    from rich.rule import Rule
    from rich.table import Table
    from rich.text import Text

    now = session.clock()
    status = {
        "waiting": "[dim]○ waiting[/]",
        "running": f"[yellow]● running[/]  {fmt_time(lane.elapsed(now))}",
        "testing": f"[yellow]● hidden tests[/]  {fmt_time(lane.elapsed(now))}",
        "done": f"[green]✓ done[/] in {fmt_time(lane.elapsed(now))}",
        "error": f"[red]✗ error[/] after {fmt_time(lane.elapsed(now))}",
        "stopped": f"[red]■ stopped[/] after {fmt_time(lane.elapsed(now))}",
    }[lane.status]
    pending = "[dim]at the end[/]" if lane.status not in FINISHED else "[dim]n/a[/]"

    metrics = Table.grid(padding=(0, 2))
    metrics.add_column(style="dim", no_wrap=True)
    metrics.add_column(no_wrap=True)
    metrics.add_row("Claude turns", f"[bold]{lane.turns}[/]")
    metrics.add_row("Claude tokens read", f"[bold]{lane.tokens_read:,}[/]")
    metrics.add_row("Claude output tokens",
                    f"[bold]{lane.output_tokens:,}[/]" if lane.output_tokens is not None else pending)
    metrics.add_row("Claude cost", f"[bold]${lane.cost:.2f}[/]" if lane.cost is not None else pending)
    metrics.add_row("Copilot", f"[bold]{lane.copilot_runs}[/] run{'s' if lane.copilot_runs != 1 else ''} · "
                    f"{lane.credits:.1f} AI credits = [bold]${lane.copilot_usd():.2f}[/]"
                    if lane.copilot_runs else "[dim]not used[/]")
    if lane.hidden:
        passed, total = lane.hidden
        mark = "[green]✓[/]" if total and passed == total else "[red]✗[/]"
        metrics.add_row("Hidden tests", f"[bold]{passed}/{total}[/] {mark}")
    else:
        metrics.add_row("Hidden tests", "[dim]after the run[/]")

    lines = []
    for who, t, text in list(lane.activity)[-activity_lines:] if activity_lines > 0 else []:
        style, label = {"claude": ("magenta", "Claude "), "copilot": ("cyan", "Copilot"), "note": ("dim", "·      ")}[who]
        line = Text(no_wrap=True, overflow="ellipsis")
        line.append(f"{fmt_time(t - (lane.start or 0))} ", style="dim")
        line.append(label + " ", style=f"bold {style}")
        line.append(text, style="dim" if who == "note" else "")
        lines.append(line)

    title = TITLES[lane.mode] + (f" [dim]({lane.model})[/]" if lane.model else "")
    return Panel(Group(Text.from_markup(status), metrics, Rule("activity", style="dim"), *lines),
                 title=title, title_align="left", border_style=COLORS[lane.mode], padding=(0, 1))


def compare_panel(session, width):
    from rich.panel import Panel
    from rich.table import Table

    lanes = list(session.lanes.values())
    base = session.lanes.get("alone")
    finished = session.finished()
    bar_width = max(10, (width - 40) // max(1, len(lanes)) - 16)
    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim", no_wrap=True)
    for lane in lanes:
        table.add_column(no_wrap=True)
    rows = [("Tokens read", lambda lane: lane.tokens_read, "{:,}"),
            ("Turns", lambda lane: lane.turns, "{}"),
            ("Claude cost", lambda lane: lane.cost, "${:.2f}"),
            ("Copilot cost", lambda lane: lane.copilot_usd(), "${:.2f}")]
    for label, get, fmt in rows:
        values = [get(lane) for lane in lanes]
        top = max((v for v in values if v), default=0)
        cells = []
        for lane, value in zip(lanes, values):
            if value is None:
                cells.append("[dim]at the end[/]")
                continue
            bar = "█" * max(1 if value else 0, round(value / top * bar_width)) if top else ""
            delta = change(get(base), value) if base and lane is not base else ""
            cells.append(f"[{COLORS[lane.mode]}]{bar}[/] {fmt.format(value)} {delta}")
        table.add_row(label, *cells)
    header = "  ".join(f"[{COLORS[lane.mode]}]■[/] {TITLES[lane.mode]}" for lane in lanes)
    note = "" if finished else "  [dim](so far)[/]"
    return Panel(table, title=f"Usage: {header}{note}", title_align="left", border_style="white",
                 subtitle=f"[dim]Copilot: 1 AI credit = ${CREDIT_USD:g}[/]", subtitle_align="right")


def verdict(lanes):
    """One line saying whether the runs reached the same result on the hidden tests."""
    scores = [(lane, lane.hidden) for lane in lanes]
    if any(hidden is None or not hidden[1] for _, hidden in scores):
        return "[yellow]No hidden test result for every run[/]"
    if all(passed == total for _, (passed, total) in scores):
        return (f"[bold green]✓ Same result: every run passes all {scores[0][1][1]} hidden acceptance tests[/]"
                if len({total for _, (_, total) in scores}) == 1 else "[bold green]✓ Every run passes all hidden tests[/]")
    return "[bold yellow]Different results: [/]" + "  ".join(
        f"{TITLES[lane.mode]} [bold]{passed}/{total}[/]" for lane, (passed, total) in scores)


def tests_panel(session, rows_available, width):
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    lanes = list(session.lanes.values())
    names = []
    for lane in lanes:
        names += [name for name, _ in lane.cases or [] if name not in names]
    body = [Text.from_markup(verdict(lanes)), Text.from_markup(
        "[dim]Written before the demo, never shown to Claude or Copilot, copied in after each run.[/]"), Text("")]
    if names:
        per_group = max(1, rows_available)
        groups = [names[i:i + per_group] for i in range(0, len(names), per_group)]
        # Each group: the name column, an 8-wide mark per run, 1 space of padding per side of every column.
        name_width = (width - 4 - 4 * (len(groups) - 1)) // len(groups) - 2 - 10 * len(lanes)
    if names and name_width < 18:
        # Too many tests for this screen to name them all: one mark per test and run, failures by name.
        for lane in lanes:
            results = dict((n, ok) for n, ok in lane.cases or [])
            marks = "".join("[green]✓[/]" if results.get(n) else "[red]✗[/]" if n in results else "[dim]–[/]"
                            for n in names)
            body.append(Text.from_markup(f"[{COLORS[lane.mode]}]{lane.mode:<9}[/] {marks}"))
            for name in (n for n in names if results.get(n) is False):
                body.append(Text.from_markup(f"[dim]{'':<9}[/] [red]✗ {name}[/]"))
    elif names:
        tables = []
        for group in groups:
            table = Table(box=None, padding=(0, 1), show_edge=False, header_style="dim")
            table.add_column("hidden test", no_wrap=True, overflow="ellipsis", max_width=name_width)
            for lane in lanes:
                table.add_column(lane.mode, justify="center", width=8, style=COLORS[lane.mode])
            for name in group:
                marks = []
                for lane in lanes:
                    result = dict((n, ok) for n, ok in lane.cases or []).get(name)
                    marks.append("[dim]–[/]" if result is None else "[green]✓[/]" if result else "[red]✗[/]")
                table.add_row(name, *marks)
            tables.append(table)
        grid = Table.grid(padding=(0, 4))
        for _ in tables:
            grid.add_column(no_wrap=True)
        grid.add_row(*tables)
        body.append(grid)
    else:
        body.append(Text.from_markup("[dim]No per-test names in this recording; totals: [/]" + "  ".join(
            f"{TITLES[lane.mode]} {lane.hidden[0]}/{lane.hidden[1]}" if lane.hidden else TITLES[lane.mode] + " n/a"
            for lane in lanes)))
    return Panel(Group(*body), title="Result: hidden acceptance tests", title_align="left", border_style="green")


def files_panel(lane, rows):
    from rich.panel import Panel
    from rich.table import Table

    table = Table.grid(padding=(0, 2))
    table.add_column(no_wrap=True, overflow="ellipsis")
    table.add_column(justify="right", no_wrap=True)
    files = lane.files or []
    source = [f for f in files if not is_test_file(f[0])]
    tests = [f for f in files if is_test_file(f[0])]
    shown = source[: max(1, rows - 2)]
    for path, added, deleted in shown:
        table.add_row(path, f"[green]+{added}[/]" + (f" [red]-{deleted}[/]" if deleted else ""))
    if len(source) > len(shown):
        rest = source[len(shown):]
        table.add_row(f"[dim]… {len(rest)} more files[/]", f"[green]+{sum(f[1] for f in rest)}[/]")
    if tests:
        table.add_row(f"[dim]tests: {len(tests)} file{'s' if len(tests) != 1 else ''}[/]",
                      f"[green]+{sum(f[1] for f in tests)}[/]")
    if lane.files is None:
        table.add_row("[dim]no file changes recorded[/]", "")
    title = f"{TITLES[lane.mode]} wrote {sum(f[1] for f in source)} lines of code"
    return Panel(table, title=title, title_align="left", border_style=COLORS[lane.mode])


def results_screen(session, console, header, footer):
    from rich.layout import Layout
    from rich.text import Text

    width, height = console.size
    lanes = list(session.lanes.values())
    compare_height = 6
    space = height - 3 - compare_height - 1
    names = {name for lane in lanes for name, _ in lane.cases or []}
    most_files = max([len([f for f in lane.files or [] if not is_test_file(f[0])]) for lane in lanes] + [1])
    files_height = min(most_files + 4, max(5, space // 3))  # the file lists get at most a third
    # The test table: borders, 3 lines of text, a header row, then the tests (in columns if they don't fit).
    tests_height = min(space - files_height, len(names) + 6 if names else 6)
    files_height = min(most_files + 4, space - tests_height)
    layout = Layout()
    layout.split_column(Layout(header, size=3),
                        Layout(tests_panel(session, tests_height - 6, width), size=tests_height),
                        Layout(name="files", size=files_height),
                        Layout(compare_panel(session, width), size=compare_height), Layout(footer, size=1),
                        Layout(Text("")))
    layout["files"].split_row(*(Layout(files_panel(lane, files_height - 2)) for lane in lanes))
    return layout


def screen(session, console):
    from rich.layout import Layout
    from rich.text import Text

    width, height = console.size
    meta = session.meta
    mode = f"[black on yellow] REPLAY {session.speed:g}x [/]" if session.replay else "[black on green] LIVE [/]"
    header = Text.from_markup(
        f"{mode}  [bold]Claude alone vs Claude + /delegate[/]  ·  task [bold]{meta['task']}[/]"
        f"  ·  {fmt_time(session.clock())}\n"
        "[dim]tokens read = input + cache Claude processes each turn (drives Claude's cost) · "
        "Copilot is paid in AI credits[/]")
    keys = ("[dim]v[/] " + ("activity" if session.showing_results() else "results") +
            "  [dim]r[/] run again  [dim]p[/] replay latest recording  [dim]q[/] quit") if session.finished() \
        else "[dim]q[/] stop and quit"
    footer = Text.from_markup(f"{keys}    [dim]{meta.get('dir', '')}[/]", overflow="ellipsis")
    if session.showing_results():
        return results_screen(session, console, header, footer)

    compare_height = 6
    body_height = max(8, height - 3 - compare_height - 1)
    activity_lines = body_height - 2 - 1 - 7 - 1  # borders, status, 7 metric rows, rule
    layout = Layout()
    layout.split_column(Layout(header, size=3), Layout(name="body", size=body_height),
                        Layout(compare_panel(session, width), size=compare_height), Layout(footer, size=1))
    layout["body"].split_row(*(Layout(lane_panel(lane, session, activity_lines)) for lane in session.lanes.values()))
    return layout


@contextmanager
def cbreak():
    if not sys.stdin.isatty():
        yield
        return
    saved = termios.tcgetattr(sys.stdin)
    try:
        tty.setcbreak(sys.stdin.fileno())
        yield
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, saved)


def read_key(timeout):
    if not sys.stdin.isatty():
        time.sleep(timeout)
        return None
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    return sys.stdin.read(1).lower() if ready else None


def summary(session):
    lines = [f"{session.meta['task']} ({'replay of ' if session.replay else ''}{session.dir})"]
    base = session.lanes.get("alone")
    for lane in session.lanes.values():
        cost = f"${lane.cost:.2f}" if lane.cost is not None else "n/a"
        hidden = f"{lane.hidden[0]}/{lane.hidden[1]}" if lane.hidden else "n/a"
        vs = ""
        if base and lane is not base and base.cost and lane.cost is not None:
            vs = f"  ({(lane.cost - base.cost) / base.cost * 100:+.0f}% Claude cost vs alone)"
        lines.append(f"  {TITLES[lane.mode]:<26} {lane.status:<8} {fmt_time(lane.elapsed(session.clock()))}  "
                     f"Claude {cost}  Copilot ${lane.copilot_usd():.2f} ({lane.credits:.1f} credits)  "
                     f"tokens read {lane.tokens_read:,}  turns {lane.turns}  hidden {hidden}{vs}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--task", default="spreadsheet", help="a task in bench/tasks (default: spreadsheet)")
    parser.add_argument("--modes", nargs="+", default=["alone", "delegate"], choices=list(PROMPTS))
    parser.add_argument("--model", help="Claude model for every run (default: your Claude Code default)")
    parser.add_argument("--timeout", type=int, default=2400, help="seconds per Claude run")
    parser.add_argument("--replay", nargs="?", const="latest", metavar="DIR",
                        help="replay a recording instead of calling any model (default: the latest)")
    parser.add_argument("--speed", type=float, default=1.0, help="replay speed (e.g. 4 = four times faster)")
    parser.add_argument("--credit-usd", type=float, default=CREDIT_USD,
                        help="USD per Copilot AI credit (default: GitHub's $0.01)")
    args = parser.parse_args()
    globals()["CREDIT_USD"] = args.credit_usd

    try:
        from rich.console import Console
        from rich.live import Live
    except ImportError:
        sys.exit("The demo needs rich: pip install rich")
    if not args.replay and not (BENCH / "tasks" / args.task / "repo").is_dir():
        sys.exit(f"no such task: {args.task} (see bench/tasks/)")

    def replay():
        run_dir = latest_recording() if args.replay in (None, "latest") else Path(args.replay)
        if not run_dir or not (run_dir / "events.jsonl").is_file():
            return None
        return ReplaySession(run_dir, args.speed)

    def new(kind):
        return replay() if kind == "replay" else LiveSession(args.task, args.modes, args.model, args.timeout)

    kind = "replay" if args.replay else "live"
    session = new(kind)
    if session is None:
        sys.exit("no recording to replay: run the demo live first (python3 bench/demo.py)")

    console = Console()
    try:
        with cbreak(), Live(console=console, screen=True, auto_refresh=False) as live:
            while True:
                with session.lock:
                    live.update(screen(session, console), refresh=True)
                key = read_key(0.25)
                if key == "q":
                    break
                if key == "v" and session.finished():
                    session.view = "live" if session.showing_results() else "results"
                if key in ("r", "p") and session.finished():
                    if key == "p":
                        kind, args.replay = "replay", "latest"
                    session = new(kind) or session
    except KeyboardInterrupt:
        pass
    finally:
        session.stop()
    print(summary(session))


if __name__ == "__main__":
    main()
