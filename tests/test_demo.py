"""The demo TUI's event parsing and bookkeeping (bench/demo.py); no models, no terminal."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import REPO

sys.path.insert(0, str(REPO / "bench"))

import demo  # noqa: E402

USAGE = {"input_tokens": 10, "cache_creation_input_tokens": 8000, "cache_read_input_tokens": 12000, "output_tokens": 3}


class ClaudeEvents(unittest.TestCase):
    def test_stream_to_lane(self):
        stream = [
            {"type": "system", "subtype": "init", "model": "claude-opus-5"},
            # One API message arrives as one event per content block, with the same id and usage.
            {"type": "assistant", "message": {"id": "m1", "usage": USAGE, "content": [{"type": "thinking"}]}},
            {"type": "assistant", "message": {"id": "m1", "usage": USAGE, "content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "cd /w/x && npm test\nmore"}}]}},
            {"type": "user", "message": {"content": []}},
            {"type": "assistant", "message": {"id": "m2", "usage": {"input_tokens": 5, "cache_read_input_tokens": 20000},
                                              "content": [{"type": "text", "text": "\nAll done.\nDetails"}]}},
        ]
        lane = demo.Lane("alone")
        for event in stream:
            for ev in demo.claude_events(event, root="/w/x/"):
                lane.apply(ev)
        self.assertEqual(lane.model, "claude-opus-5")
        self.assertEqual(lane.turns, 2)
        self.assertEqual(lane.tokens_read, 20010 + 20005)
        self.assertEqual([text for _, _, text in lane.activity], ["▸ Bash cd . && npm test", "💬 All done."])

        result = {"type": "result", "total_cost_usd": 0.5, "num_turns": 2, "is_error": False,
                  "usage": {**USAGE, "output_tokens": 143}}
        for ev in demo.claude_events(result):
            lane.apply(ev)
        self.assertEqual((lane.cost, lane.output_tokens, lane.tokens_read), (0.5, 143, 20010))


class CopilotLog(unittest.TestCase):
    def test_lines(self):
        self.assertEqual(demo.copilot_event("11:25:42 ▸ create src/sheet.js"),
                         {"kind": "copilot", "text": "▸ create src/sheet.js"})
        self.assertEqual(demo.copilot_event("=== model: claude-sonnet-5"), {"kind": "copilot", "text": "model: claude-sonnet-5"})
        self.assertEqual(demo.copilot_event("[parser] === end: done · 29s · AI credits 6.80"),
                         {"kind": "copilot_end", "text": "[parser] end: done · 29s · AI credits 6.80", "credits": 6.8})
        for noise in ("         │ ok 1 - adds", "11:25:44   ✓ bash", "11:25:40 ── turn 1", "11:25:41 💬", ""):
            self.assertIsNone(demo.copilot_event(noise), noise)

    def test_tail_reads_only_new_complete_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp) / ".delegate" / "logs"
            logs.mkdir(parents=True)
            (logs / "latest.log").write_text("11:00:00 ▸ ignored\n")
            (logs / "test-1.log").write_text("11:00:00 ▸ ignored\n")
            run = logs / "worker-1.log"
            run.write_text("11:00:01 ▸ create a.js\n11:00:02 ▸ bash np")
            tail = demo.LogTail(tmp)
            self.assertEqual([ev["text"] for ev in tail.poll()], ["▸ create a.js"])
            with open(run, "a") as fh:
                fh.write("m test\n=== end: done · AI credits 2.5\n")
            self.assertEqual([ev["kind"] for ev in tail.poll()], ["copilot", "copilot_end"])
            self.assertEqual(tail.poll(), [])


class Results(unittest.TestCase):
    def test_hidden_cases(self):
        tap = "TAP version 13\n# Subtest: adds\nok 1 - adds\n  ---\nnot ok 2 - divides # TODO\n    ok 1 - nested\n"
        self.assertEqual(demo.hidden_cases(tap), [["adds", True], ["divides", False]])
        unittest_v = ("test_fixed_time (m.NextRun.test_fixed_time) ... ok\n"
                      "test_steps (m.NextRun.test_steps) ... FAIL\n\nRan 2 tests in 0.1s\n")
        self.assertEqual(demo.hidden_cases(unittest_v), [["fixed time", True], ["steps", False]])

    def test_changed_files_counts_new_and_edited_files_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "src").mkdir()
            (work / "src" / "a.js").write_text("1\n2\n")
            demo.sh(["git", "init", "-q"], work)
            demo.sh(["git", "add", "-A"], work)
            demo.sh(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "start"], work)
            (work / "src" / "a.js").write_text("1\nchanged\n3\n")
            (work / "test").mkdir()
            (work / "test" / "a.test.js").write_text("x\n")
            for skipped in (".delegate", ".hidden"):
                (work / skipped).mkdir()
                (work / skipped / "f.js").write_text("x\n")
            files = demo.changed_files(work)
            self.assertEqual(sorted(files), [["src/a.js", 2, 1], ["test/a.test.js", 1, 0]])
            self.assertEqual(demo.sh(["git", "status", "--porcelain"], work).stdout.count("??"), 3)  # index untouched
            self.assertEqual([demo.is_test_file(f[0]) for f in sorted(files)], [False, True])


class Replay(unittest.TestCase):
    def test_replay_reaches_the_recorded_end(self):
        events = [
            {"t": 0.0, "mode": "alone", "kind": "status", "status": "running"},
            {"t": 0.0, "mode": "delegate", "kind": "status", "status": "running"},
            {"t": 0.1, "mode": "delegate", "kind": "copilot_end", "text": "end", "credits": 4.0},
            {"t": 0.2, "mode": "alone", "kind": "result", "cost": 2.0, "tokens_read": 100, "turns": 3},
            {"t": 0.2, "mode": "alone", "kind": "status", "status": "done"},
        ]  # the delegate run was stopped before it finished
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "meta.json").write_text(json.dumps({"task": "x", "modes": ["alone", "delegate"], "model": None}))
            (Path(tmp) / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
            session = demo.ReplaySession(tmp, speed=100)
            for _ in range(100):
                if session.finished():
                    break
                demo.time.sleep(0.01)
            self.assertTrue(session.finished())
            self.assertEqual(session.lanes["alone"].cost, 2.0)
            self.assertEqual(session.lanes["delegate"].credits, 4.0)
            self.assertEqual(session.lanes["delegate"].status, "stopped")

    def test_replay_backfills_per_test_results_from_the_run_files(self):
        events = [{"t": 0.0, "mode": "alone", "kind": "status", "status": "running"},
                  {"t": 1.0, "mode": "alone", "kind": "hidden", "passed": 1, "total": 1},
                  {"t": 1.0, "mode": "alone", "kind": "status", "status": "done"}]
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "meta.json").write_text(json.dumps({"task": "x", "modes": ["alone"], "model": None}))
            (Path(tmp) / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
            (Path(tmp) / "alone.hidden.txt").write_text("ok 1 - adds\n")
            session = demo.ReplaySession(tmp, speed=100)
            for _ in range(100):
                if session.finished():
                    break
                demo.time.sleep(0.01)
            self.assertEqual(session.lanes["alone"].cases, [["adds", True]])


if __name__ == "__main__":
    unittest.main()
