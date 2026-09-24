"""End-to-end controller tests: fake OMP transport, real Git checkpoints.

The fake executable is first on PATH and never contacts a model provider.
"""

import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest


SOURCE = Path(__file__).resolve().parents[1]
FAKE_OMP = r'''
import json, os, pathlib, select, subprocess, sys, time

state_path = pathlib.Path(os.environ["SPEC_BUDGET_STATE"])
audit_path = pathlib.Path(os.environ["SPEC_FAKE_AUDIT"])
mode = os.environ["SPEC_FAKE_MODE"]
budget = float(os.environ["SPEC_BUDGET_USD"])

def record(kind, **values):
    with audit_path.open("a") as stream:
        stream.write(json.dumps({"kind": kind, **values}) + "\n")

def emit(frame):
    print(json.dumps(frame), flush=True)

def state(spent, stopped=False, reserved=0, **updates):
    value = json.loads(state_path.read_text()) if state_path.exists() else {}
    value.update({"version": 1, "pid": os.getpid(), "limit": budget, "reserve": 1, "spent": spent,
                  "remaining": budget-spent, "reserved": reserved,
                  "requests": value.get("requests", 0) + int(reserved > 0),
                  "settledRequests": value.get("settledRequests", 0),
                  "inFlight": int(reserved > 0), "stopped": stopped,
                  "stopKind": "budget" if stopped else "", "retryable": False,
                  "reason": "Test budget reserve reached" if stopped else ""})
    value.update(updates)
    temporary = state_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value))
    temporary.replace(state_path)

record("started", argv=sys.argv[1:],
       budget=json.loads(state_path.read_text()) if state_path.exists() else None)
if mode == "missing_guard":
    pass
elif mode == "continuation" and any(json.loads(entry)["kind"] == "prompt" for entry in audit_path.read_text().splitlines()):
    state(budget-1, True)
elif not state_path.exists():
    state(0)
else:
    previous = json.loads(state_path.read_text())
    previous["pid"] = os.getpid()
    state_path.write_text(json.dumps(previous))
emit({"type": "ready"})
for line in sys.stdin:
    request = json.loads(line)
    kind = request["type"]
    record(kind, **({"message": request["message"],
                     "files": sorted(str(path) for path in pathlib.Path("specification").glob("*")),
                     "head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                     "budget": json.loads(state_path.read_text())} if kind == "prompt" else {}))
    if kind == "abort" and mode == "complete_corrupt_shutdown":
        state_path.write_text("broken JSON")
    elif kind == "abort" and mode == "complete_reserved_shutdown":
        state(json.loads(state_path.read_text())["spent"], reserved=0.25)
    elif kind == "abort" and mode == "complete_budget_shutdown":
        state(budget-1, True)
    if kind == "get_state":
        emit({"type": "response", "id": request["id"], "success": True,
              "data": {"model": {"provider": "other" if mode == "unsupported_model" else "deepseek",
                                   "id": "fake", "api": "openai-completions"},
                       "isStreaming": False, "isCompacting": False}})
    else:
        emit({"type": "response", "id": request["id"], "success": True, "data": {}})
    if kind != "prompt":
        continue
    rounds = sum(json.loads(entry)["kind"] == "prompt" for entry in audit_path.read_text().splitlines())
    if mode != "no_changes":
        pathlib.Path(f"specification/improvement-{rounds}.md").write_text(f"Improvement {rounds}\n")
    if mode == "cutoff_changes":
        overview = pathlib.Path("specification/overview.md")
        overview.write_text("Staged incomplete edit\n")
        subprocess.run(["git", "add", str(overview)], check=True)
        overview.write_text("Unstaged incomplete edit\n")
    if mode in ("cutoff_commit", "cutoff_commit_push") or (mode == "retry_success" and rounds <= 2):
        subprocess.run(["git", "add", "specification"], check=True)
        subprocess.run(["git", "commit", "--quiet", "-m", "Incomplete worker commit"], check=True)
        record("worker_commit", head=subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip())
        if mode in ("cutoff_commit_push", "retry_success"):
            push = subprocess.run(["git", "push", "origin", "main"], capture_output=True, text=True)
            record("worker_push", returncode=push.returncode, stderr=push.stderr)
    previous = json.loads(state_path.read_text())
    state(round(previous["spent"] + 0.1, 8),
          requests=previous["requests"] + 1, settledRequests=previous["settledRequests"] + 1)
    emit({"type": "tool_execution_start", "toolName": "write"})
    if mode == "streaming_error":
        print("stderr diagnostic sentinel", file=sys.stderr, flush=True)
        partial = {"role": "assistant", "content": [{"type": "text", "text": "snapshot-only-" * 8000}]}
        for _ in range(60):
            update = {"type": "message_update", "message": partial,
                      "assistantMessageEvent": {"type": "text_delta", "delta": "x", "partial": partial}}
            emit(update)
            emit({"type": "subagent_event", "payload": {"event": update}})
    if mode == "fatal":
        sys.exit(7)
    if mode in ("detached_child", "detached_child_retry"):
        ready_path = audit_path.with_suffix(f".child-ready-{rounds}" if mode == "detached_child_retry" else ".child-ready")
        marker_path = audit_path.with_suffix(".late-write")
        child_code = r"""
import os, pathlib, sys, time
pathlib.Path(sys.argv[2]).write_text("ready")
while os.getppid() == int(sys.argv[1]):
    time.sleep(0.01)
time.sleep(0.4)
pathlib.Path("specification/late.md").write_text("orphan wrote after parent exit\n")
pathlib.Path(sys.argv[3]).write_text("late write happened")
"""
        child = subprocess.Popen(
            [sys.executable, "-c", child_code, str(os.getpid()), str(ready_path), str(marker_path)],
            start_new_session=True, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        record("detached_child", pid=child.pid)
        deadline = time.monotonic() + 2
        while not ready_path.exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("Detached child did not start")
            time.sleep(0.01)
        if mode == "detached_child_retry":
            sys.exit(7)
        state(budget-1, True)
        continue
    if mode == "corrupt_accounting":
        state_path.write_text("broken JSON")
        continue
    if mode in ("loop_error", "socket_error", "guard_config_error"):
        reason = {"loop_error": "Thinking loop detected",
                  "socket_error": "Connection closed unexpectedly during response",
                  "guard_config_error": "Unsupported usage accounting configuration"}[mode]
        previous = json.loads(state_path.read_text())
        state(round(previous["spent"] + 0.25, 8), True, stopKind="error",
              retryable=mode != "guard_config_error", estimatedSpend=True,
              reason=f"Request usage was incomplete; its reserved maximum was charged conservatively: {reason}")
        emit({"type": "message_end", "message": {"role": "assistant", "content": [],
              "stopReason": "error", "errorMessage": reason}})
        continue
    if mode == "retry_budget_exhausted":
        state(budget-1, True, stopKind="error", retryable=True, estimatedSpend=True,
              reason="Request usage was incomplete; its reserved maximum was charged conservatively")
        continue
    if mode in ("wait", "wait_reserved"):
        if mode == "wait_reserved":
            state(0.1, reserved=0.25)
        record("waiting")
        continue
    if mode == "continuation":
        emit({"type": "agent_end", "isTerminal": False})
        # OMP can be temporarily idle while a continuation is scheduled. The
        # controller must honor the nonterminal marker instead of finalizing.
        if select.select([sys.stdin], [], [], 0.15)[0]:
            early = json.loads(sys.stdin.readline())
            record("premature_completion_probe")
            emit({"type": "response", "id": early["id"], "success": True,
                  "data": {"isStreaming": False, "isCompacting": False}})
            time.sleep(0.15)
        pathlib.Path("specification/continued.md").write_text("Continuation edit\n")
    if mode == "unexpected_child_abort":
        emit({"type": "subagent_event", "payload": {"event": {
            "type": "message_end", "message": {"role": "assistant", "content": [],
            "stopReason": "aborted", "errorMessage": "Reviewer request was aborted"}}}})
    if (mode in ("model_error", "streaming_error") or
            (mode == "retry_success" and rounds <= 2) or
            (mode == "complete_then_error" and rounds >= 2)):
        emit({"type": "message_end", "message": {"role": "assistant", "content": [],
              "stopReason": "error", "errorMessage": "Synthetic provider failure"}})
    elif mode == "unexpected_abort":
        emit({"type": "message_end", "message": {"role": "assistant", "content": [],
              "stopReason": "aborted", "errorMessage": "Request was aborted"}})
    elif (mode in ("cutoff", "cutoff_changes", "cutoff_commit", "cutoff_commit_push") or
          (mode == "rounds" and rounds == 2) or (mode == "retry_success" and rounds == 4)):
        state(budget-1, True)
        continue
    elif mode == "missing_final_message":
        pass
    else:
        emit({"type": "message_end", "message": {"role": "assistant", "stopReason": "length" if mode == "length" else "stop",
              "content": [{"type": "text", "text": "Reviewed specification improvement."}]}})
    emit({"type": "agent_end", "isTerminal": True})
'''


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "checkout"
        self.remote = self.base / "remote.git"
        self.fake_bin = self.base / "bin"
        self.audit = self.base / "audit.jsonl"
        self.state_home = self.base / "state"
        (self.root / "tools").mkdir(parents=True)
        (self.root / "specification").mkdir()
        self.fake_bin.mkdir()
        for filename in ("spec", "spec_git.py", "spec_logs.py", "spec-budget.ts"):
            shutil.copy2(SOURCE / "tools" / filename, self.root / "tools" / filename)
        (self.root / "prompt.md").write_text("Improve specification; commit and push.\n")
        (self.root / "specification" / "overview.md").write_text("Terminal art.\n")
        (self.root / ".gitignore").write_text("__pycache__/\n")
        fake = self.fake_bin / "omp"
        fake.write_text(f"#!{sys.executable}\n" + FAKE_OMP)
        fake.chmod(0o755)
        self.env = dict(os.environ)
        self.env.update(
            PATH=str(self.fake_bin) + os.pathsep + os.environ["PATH"],
            SPEC_FAKE_MODE="cutoff", SPEC_FAKE_AUDIT=str(self.audit),
            XDG_STATE_HOME=str(self.state_home), GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
        )
        self.git(self.base, "init", "--bare", str(self.remote))
        self.git(self.root, "init", "-b", "main")
        self.git(self.root, "config", "user.name", "Spec Runner Test")
        self.git(self.root, "config", "user.email", "spec@example.invalid")
        self.git(self.root, "add", ".")
        self.git(self.root, "commit", "-m", "Initial")
        self.git(self.root, "remote", "add", "origin", str(self.remote))
        self.git(self.root, "push", "-u", "origin", "main")
        self.initial_head = self.git(self.root, "rev-parse", "HEAD")

    def git(self, cwd, *args):
        return subprocess.check_output(
            ["git", *args], cwd=cwd, env=self.env, stderr=subprocess.PIPE, text=True
        ).strip()

    def command(self, *args):
        return [sys.executable, str(self.root / "tools" / "spec"), *args]

    def run_spec(self, *args, mode=None):
        env = dict(self.env)
        if mode:
            env["SPEC_FAKE_MODE"] = mode
        return subprocess.run(
            self.command(*args), cwd=self.base, env=env,
            capture_output=True, text=True, timeout=20,
        )

    def events(self):
        if not self.audit.exists():
            return []
        return [json.loads(line) for line in self.audit.read_text().splitlines()]

    def prompts(self):
        return [entry for entry in self.events() if entry["kind"] == "prompt"]

    def assert_saved(self):
        self.assertEqual(self.git(self.root, "status", "--porcelain"), "")
        self.assertEqual(
            self.git(self.root, "rev-parse", "HEAD"),
            self.git(self.remote, "rev-parse", "main"),
        )

    def assert_round_reset(self, head=None, round_number=1):
        self.assert_saved()
        self.assertEqual(self.git(self.root, "rev-parse", "HEAD"), head or self.initial_head)
        self.assertFalse((self.root / f"specification/improvement-{round_number}.md").exists())

    def test_invalid_arguments_never_launch_worker(self):
        for args in ((), ("1.99",), ("-2",), ("NaN",), ("inf",), ("2.001",),
                     ("2", "3"), ("$2.00",), ("9000000001",)):
            with self.subTest(args=args):
                result = self.run_spec(*args)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.events(), [])

    def test_minimum_budget_resets_round_and_preserves_user_checkpoint(self):
        (self.root / "existing-user-work.md").write_text("Existing untracked work\n")
        overview = self.root / "specification/overview.md"
        overview.write_text("Existing staged user work\n")
        self.git(self.root, "add", str(overview))
        overview.write_text("Existing unstaged user work\n")
        result = self.run_spec("2.00")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.prompts()), 1)
        self.assertIn("$1.0000 remaining", result.stdout)
        self.assertIn("Round 1", result.stdout)
        self.assertIn("Test budget reserve reached", result.stdout)
        self.assert_saved()
        self.assertEqual(
            self.git(self.remote, "show", "main:existing-user-work.md"),
            "Existing untracked work",
        )
        self.assertEqual(self.git(self.remote, "show", "main:specification/overview.md"),
                         "Existing unstaged user work")
        self.assertFalse((self.root / "specification/improvement-1.md").exists())
        self.assertEqual(self.git(self.root, "rev-parse", "HEAD^"), self.initial_head)

    def test_incomplete_round_resets_staged_and_unstaged_tracked_edits(self):
        result = self.run_spec("2", mode="cutoff_changes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_round_reset()
        self.assertEqual((self.root / "specification/overview.md").read_text(), "Terminal art.\n")

    def test_incomplete_round_resets_worker_created_commit(self):
        result = self.run_spec("2", mode="cutoff_commit")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        commits = [event["head"] for event in self.events() if event["kind"] == "worker_commit"]
        self.assertEqual(len(commits), 1)
        self.assertNotEqual(commits[0], self.initial_head)
        self.assert_round_reset()

    def test_worker_cannot_push_its_incomplete_commit(self):
        result = self.run_spec("2", mode="cutoff_commit_push")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        pushes = [event for event in self.events() if event["kind"] == "worker_push"]
        self.assertEqual(len(pushes), 1)
        self.assertNotEqual(pushes[0]["returncode"], 0, pushes[0]["stderr"])
        self.assert_round_reset()

    def test_complete_round_continues_then_budget_stops_more_prompts(self):
        result = self.run_spec("2", mode="rounds")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.prompts()), 2)
        self.assertIn("Reviewed specification improvement.", result.stdout)
        self.assertIn("Starting improvement round 2", result.stdout)
        self.assertNotIn("Starting improvement round 3", result.stdout)
        self.assert_saved()
        self.assertEqual(self.git(self.remote, "show", "main:specification/improvement-1.md"),
                         "Improvement 1")
        self.assertFalse((self.root / "specification/improvement-2.md").exists())
        self.assertEqual(self.git(self.root, "rev-parse", "HEAD^"), self.initial_head)

    def test_completed_round_is_preserved_when_supervisor_push_is_rejected(self):
        push_audit = self.base / "rejected-pushes.txt"
        hook = self.remote / "hooks/pre-receive"
        hook.write_text(
            f"#!{sys.executable}\n"
            "from pathlib import Path\n"
            f"with Path({str(push_audit)!r}).open('a') as stream:\n"
            "    stream.write('rejected push\\n')\n"
            "raise SystemExit(1)\n"
        )
        hook.chmod(0o755)
        result = self.run_spec("2", mode="complete")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(len(self.prompts()), 1)
        self.assertEqual(self.git(self.root, "status", "--porcelain"), "")
        self.assertEqual(self.git(self.root, "show", "HEAD:specification/improvement-1.md"),
                         "Improvement 1")
        self.assertEqual(self.git(self.root, "rev-parse", "HEAD^"), self.initial_head)
        self.assertEqual(self.git(self.remote, "rev-parse", "main"), self.initial_head)
        # The unchanged startup push does not call this hook. A second entry
        # would reveal an unconditional cleanup push after the first rejection.
        self.assertEqual(push_audit.read_text().splitlines(), ["rejected push"])

    def test_worker_fatal_exit_retries_same_round_three_times(self):
        result = self.run_spec("2", mode="fatal")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(len(self.prompts()), 3)
        for label in ("1A", "1B", "1C"):
            self.assertIn(f"Starting improvement round {label}", result.stdout)
        self.assertNotIn("Starting improvement round 1D", result.stdout)
        self.assertNotIn("Starting improvement round 2", result.stdout)
        self.assert_round_reset()

    def test_model_error_resets_round_before_each_of_three_attempts(self):
        result = self.run_spec("2", mode="model_error")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Synthetic provider failure", result.stdout)
        self.assertEqual(len(self.prompts()), 3)
        for prompt in self.prompts():
            self.assertEqual(prompt["files"], ["specification/overview.md"])
            self.assertEqual(prompt["head"], self.initial_head)
        self.assert_round_reset()

    def test_two_failed_attempts_then_success_advance_to_next_round(self):
        result = self.run_spec("2", mode="retry_success")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        prompts = self.prompts()
        self.assertEqual(len(prompts), 4)
        for label, prompt in zip(("1A", "1B", "1C", "2A"), prompts):
            self.assertIn(f"Starting improvement round {label}", result.stdout)
            self.assertIn(f"round {label}", prompt["message"])
        for prompt in prompts[:3]:
            self.assertEqual(prompt["files"], ["specification/overview.md"])
            self.assertEqual(prompt["head"], self.initial_head)
        self.assertEqual(prompts[3]["files"],
                         ["specification/improvement-3.md", "specification/overview.md"])
        self.assertEqual([prompt["budget"]["spent"] for prompt in prompts], [0, 0.1, 0.2, 0.3])
        self.assertEqual([prompt["budget"]["requests"] for prompt in prompts], [0, 1, 2, 3])
        self.assertEqual(len([event for event in self.events() if event["kind"] == "started"]), 4)
        pushes = [event for event in self.events() if event["kind"] == "worker_push"]
        self.assertEqual(len(pushes), 2)
        self.assertTrue(all(event["returncode"] != 0 for event in pushes))
        self.assert_saved()
        self.assertEqual(self.git(self.remote, "rev-list", "--count", "main"), "2")
        self.assertEqual(self.git(self.remote, "show", "main:specification/improvement-3.md"),
                         "Improvement 3")
        for attempt in (1, 2, 4):
            self.assertFalse((self.root / f"specification/improvement-{attempt}.md").exists())

    def test_completed_round_is_preserved_after_next_round_exhausts_retries(self):
        result = self.run_spec("2", mode="complete_then_error")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        prompts = self.prompts()
        self.assertEqual(len(prompts), 4)
        for label, prompt in zip(("1A", "2A", "2B", "2C"), prompts):
            self.assertIn(f"round {label}", prompt["message"])
        for prompt in prompts[1:]:
            self.assertEqual(prompt["files"],
                             ["specification/improvement-1.md", "specification/overview.md"])
        self.assert_saved()
        self.assertEqual(self.git(self.remote, "rev-list", "--count", "main"), "2")
        self.assertEqual(self.git(self.remote, "show", "main:specification/improvement-1.md"),
                         "Improvement 1")
        self.assertEqual(self.git(self.root, "rev-parse", "HEAD^"), self.initial_head)
        for attempt in (2, 3, 4):
            self.assertFalse((self.root / f"specification/improvement-{attempt}.md").exists())

    def test_missing_usage_model_failures_retry_with_conservative_costs_preserved(self):
        for mode, reason in (("loop_error", "Thinking loop detected"),
                             ("socket_error", "Connection closed unexpectedly during response")):
            with self.subTest(mode=mode):
                self.audit.unlink(missing_ok=True)
                result = self.run_spec("4", mode=mode)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(reason, result.stdout)
                prompts = self.prompts()
                self.assertEqual(len(prompts), 3)
                self.assertEqual([prompt["budget"]["spent"] for prompt in prompts], [0, 0.35, 0.7])
                self.assertEqual([prompt["budget"]["requests"] for prompt in prompts], [0, 1, 2])
                for prompt in prompts[1:]:
                    self.assertTrue(prompt["budget"]["estimatedSpend"])
                    self.assertFalse(prompt["budget"]["stopped"])
                    self.assertFalse(prompt["budget"].get("stopKind"))
                    self.assertFalse(prompt["budget"].get("reason"))
                    self.assertFalse(prompt["budget"].get("retryable"))
                self.assertIn("$1.0500 accounted (includes estimates)", result.stdout)
                self.assert_round_reset()

    def test_accounting_configuration_error_is_not_retried(self):
        result = self.run_spec("2", mode="guard_config_error")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Unsupported usage accounting configuration", result.stdout)
        self.assertEqual(len(self.prompts()), 1)
        self.assert_round_reset()

    def test_unexpected_model_abort_retries_up_to_three_attempts(self):
        for mode, reason in (("unexpected_abort", "Request was aborted"),
                             ("unexpected_child_abort", "Reviewer request was aborted")):
            with self.subTest(mode=mode):
                self.audit.unlink(missing_ok=True)
                result = self.run_spec("2", mode=mode)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(reason, result.stdout)
                self.assertEqual(len(self.prompts()), 3)
                self.assert_round_reset()

    def test_failed_request_that_exhausts_budget_does_not_retry(self):
        result = self.run_spec("2", mode="retry_budget_exhausted")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.prompts()), 1)
        self.assertIn("$1.0000 remaining", result.stdout)
        self.assert_round_reset()

    def test_streaming_snapshots_are_not_logged_but_final_error_is(self):
        result = self.run_spec("2", mode="streaming_error")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Synthetic provider failure", result.stdout)
        self.assert_round_reset()
        logs = list(self.state_home.glob("spec/*/events.jsonl"))
        self.assertEqual(len(logs), 1)
        text = logs[0].read_text()
        self.assertLess(len(text), 64 * 1024)
        self.assertNotIn("snapshot-only-", text)
        self.assertNotIn("message_update", text)
        self.assertIn("Synthetic provider failure", text)
        self.assertIn("stderr diagnostic sentinel", text)
        self.assertFalse(list(self.state_home.glob("spec/*/round-*.stderr.log")))
        self.assertTrue(any(json.loads(line).get("type") == "round_start" for line in text.splitlines()))

    def test_corrupt_accounting_stops_and_resets_round(self):
        result = self.run_spec("2", mode="corrupt_accounting")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Budget accounting is unavailable", result.stdout)
        self.assertEqual(len(self.prompts()), 1)
        self.assert_round_reset()

    def test_terminal_round_resets_when_shutdown_accounting_is_unconfirmed(self):
        result = self.run_spec("2", mode="complete_corrupt_shutdown")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Reviewed specification improvement.", result.stdout)
        self.assertEqual(len(self.prompts()), 1)
        self.assert_round_reset()

    def test_unreported_shutdown_usage_is_charged_before_retry(self):
        result = self.run_spec("4", mode="complete_reserved_shutdown")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Reviewed specification improvement.", result.stdout)
        prompts = self.prompts()
        self.assertEqual(len(prompts), 3)
        self.assertEqual([prompt["budget"]["spent"] for prompt in prompts], [0, 0.35, 0.7])
        self.assertEqual([prompt["budget"]["requests"] for prompt in prompts], [0, 2, 4])
        self.assertEqual([prompt["budget"]["settledRequests"] for prompt in prompts], [0, 2, 4])
        for prompt in prompts[1:]:
            self.assertTrue(prompt["budget"]["estimatedSpend"])
            self.assertEqual(prompt["budget"]["reserved"], 0)
            self.assertEqual(prompt["budget"]["inFlight"], 0)
        self.assertIn("$1.0500 accounted (includes estimates)", result.stdout)
        self.assert_round_reset()

    def test_terminal_round_resets_when_budget_stops_during_shutdown(self):
        result = self.run_spec("2", mode="complete_budget_shutdown")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Reviewed specification improvement.", result.stdout)
        self.assertEqual(len(self.prompts()), 1)
        self.assert_round_reset()

    def test_terminal_event_requires_successful_final_assistant_message(self):
        for mode in ("length", "missing_final_message"):
            with self.subTest(mode=mode):
                self.audit.unlink(missing_ok=True)
                result = self.run_spec("2", mode=mode)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(len(self.prompts()), 3)
                self.assert_round_reset()

    def test_missing_guard_never_sends_paid_prompt(self):
        result = self.run_spec("2", mode="missing_guard")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Budget accounting is unavailable", result.stdout)
        self.assertEqual(len(self.prompts()), 0)
        self.assert_saved()

    def test_unsupported_model_never_sends_paid_prompt(self):
        result = self.run_spec("2", mode="unsupported_model")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(len(self.prompts()), 0)
        self.assert_saved()

    def test_nonterminal_agent_end_waits_for_continuation_edits(self):
        result = self.run_spec("2", mode="continuation")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.prompts()), 1)
        self.assertFalse(any(entry["kind"] == "premature_completion_probe" for entry in self.events()))
        self.assert_saved()
        for filename, content in (("improvement-1.md", "Improvement 1"),
                                  ("continued.md", "Continuation edit")):
            self.assertEqual(self.git(self.remote, "show", f"main:specification/{filename}"), content)

    def test_detached_writer_is_stopped_before_round_reset(self):
        result = self.run_spec("2", mode="detached_child")
        child_events = [event for event in self.events() if event["kind"] == "detached_child"]
        try:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(len(child_events), 1)
            self.assertTrue(self.audit.with_suffix(".child-ready").exists())
            # The child would write after its OMP parent died even with all stdio
            # closed and a separate session/process group. Give that write time.
            time.sleep(0.6)
            self.assertFalse(self.audit.with_suffix(".late-write").exists())
            self.assertFalse((self.root / "specification/late.md").exists())
            self.assert_round_reset()
        finally:
            for event in child_events:
                try:
                    os.kill(event["pid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_detached_writer_from_failed_attempt_is_stopped_before_retry(self):
        result = self.run_spec("2", mode="detached_child_retry")
        child_events = [event for event in self.events() if event["kind"] == "detached_child"]
        try:
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertEqual(len(child_events), 3)
            self.assertEqual(len(self.prompts()), 3)
            for prompt in self.prompts():
                self.assertEqual(prompt["files"], ["specification/overview.md"])
            time.sleep(0.6)
            self.assertFalse(self.audit.with_suffix(".late-write").exists())
            self.assertFalse((self.root / "specification/late.md").exists())
            self.assert_round_reset()
        finally:
            for event in child_events:
                try:
                    os.kill(event["pid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_three_empty_rounds_stop(self):
        before = self.git(self.root, "rev-parse", "HEAD")
        result = self.run_spec("2", mode="no_changes")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Three rounds made no commits", result.stdout)
        self.assertEqual(len(self.prompts()), 3)
        self.assertEqual(before, self.git(self.root, "rev-parse", "HEAD"))
        self.assert_saved()

    def test_existing_lock_refuses_run_without_launching_or_committing(self):
        with (self.root / ".git/spec.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            (self.root / "pending.md").write_text("preserve\n")
            before = self.git(self.root, "rev-parse", "HEAD")
            result = self.run_spec("2")
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("already holds", result.stdout)
            self.assertEqual(self.events(), [])
            self.assertEqual(before, self.git(self.root, "rev-parse", "HEAD"))
            self.assertEqual((self.root / "pending.md").read_text(), "preserve\n")

    def interrupt_run(self, mode, interrupt_signal=signal.SIGINT):
        env = dict(self.env, SPEC_FAKE_MODE=mode)
        process = subprocess.Popen(
            self.command("2"), cwd=self.base, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if any(entry["kind"] == "waiting" for entry in self.events()):
                    break
                if process.poll() is not None:
                    self.fail("Runner exited before the interrupt test was ready")
                time.sleep(0.02)
            else:
                self.fail("Fake worker never reached its waiting state")
            process.send_signal(interrupt_signal)
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 130, stdout + stderr)
            self.assertIn("Interrupted.", stdout)
            self.assertEqual(len(self.prompts()), 1)
            self.assert_round_reset()
            return stdout
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()

    def test_control_c_stops_worker_then_resets_round(self):
        self.interrupt_run("wait")

    def test_termination_and_hangup_stop_without_retry(self):
        for interrupt_signal in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=interrupt_signal):
                self.audit.unlink(missing_ok=True)
                self.interrupt_run("wait", interrupt_signal)

    def test_interrupted_request_accounts_for_outstanding_reservation(self):
        stdout = self.interrupt_run("wait_reserved")
        states = list(self.state_home.glob("spec/*/budget.json"))
        self.assertEqual(len(states), 1)
        state = json.loads(states[0].read_text())
        self.assertEqual(state["spent"], 0.35)
        self.assertEqual(state["reserved"], 0)
        self.assertEqual(state["inFlight"], 0)
        self.assertTrue(state["estimatedSpend"])
        self.assertIn("$0.3500 accounted (includes estimates)", stdout)
        self.assertIn("$1.6500 remaining", stdout)


if __name__ == "__main__":
    unittest.main()
