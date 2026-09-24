"""Failure-path checks for supervised workers; never contact a model provider."""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / "tools"))
import ceps_runtime as runtime
from spec_git import GitRepo


def controller_module():
    loader = importlib.machinery.SourceFileLoader("ceps_runtime_test_controller", str(SOURCE / "tools/ceps"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


CONTROLLER = controller_module()
MESSAGE = {"type": "item.completed", "item": {"type": "agent_message", "text": "Reviewed."}}
COMPLETED = {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 4,
                                                  "cached_input_tokens": 3}}


def frames(*events):
    return b"".join(json.dumps(event).encode() + b"\n" for event in events)


class AttemptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.control = runtime.Control()
        self.events = []
        self.configuration = SimpleNamespace(env=lambda: dict(os.environ))
        self.attempt = runtime.Attempt(self.configuration, self.root, self.root / "attempt",
                                       self.control, lambda: None, self.events.append)
        self.addCleanup(self.attempt.close)

    def execute(self, data, *, check=None):
        if check:
            self.attempt.check_quota = check
        code = "import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write(" + repr(data) + "); sys.stdout.flush()"
        with mock.patch.object(runtime, "build_exec_command", return_value=[sys.executable, "-c", code]):
            return self.attempt.run_worker("child", "luna-1", "Review this.")

    def start_background(self, action):
        outcome = {}

        def run():
            try:
                outcome["result"] = action()
            except BaseException as exc:
                outcome["error"] = exc

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, outcome

    def finish_background(self, thread, outcome):
        thread.join(timeout=8)
        self.assertFalse(thread.is_alive(), "Delegation did not finish")
        if "error" in outcome:
            raise outcome["error"]
        return outcome["result"]

    def test_completion_requires_all_successful_phases(self):
        with mock.patch.object(self.attempt, "start_broker"), \
                mock.patch.object(self.attempt, "run_worker", return_value="Complete"):
            for phases in (set(), {"understand"}, {"understand", "plan"}):
                with self.subTest(phases=phases):
                    self.attempt.phases = phases
                    with self.assertRaises(runtime.RoundError):
                        self.attempt.run("Improve one detail")
            self.attempt.phases = set(runtime.PHASES)
            self.assertEqual(self.attempt.run("Improve one detail"), "Complete")
            self.attempt.failed_child = True
            with self.assertRaises(runtime.RoundError):
                self.attempt.run("Improve one detail")

    def test_failed_child_cannot_mark_phase_or_be_hidden_by_later_success(self):
        with mock.patch.object(self.attempt, "run_worker", side_effect=runtime.RoundError("bad worker")):
            with self.assertRaises(runtime.RoundError):
                self.attempt.delegate("Understand the documents", "understand")
        self.assertEqual(self.attempt.phases, set())
        self.assertTrue(self.attempt.failed_child)
        with mock.patch.object(self.attempt, "run_worker", return_value="Understood"):
            self.assertTrue(self.attempt.delegate("Try understanding again", "understand")["ok"])
        self.assertTrue(self.attempt.failed_child)
        failures = [event for event in self.events if event["type"] == "child_failed"]
        completions = [event for event in self.events if event["type"] == "phase_completed"]
        self.assertTrue(failures)
        self.assertEqual(len(completions), 1)
        self.assertEqual(completions[0]["phase"], "understand")

    def test_delegate_starts_independent_workers_together_and_awaits_the_whole_fleet(self):
        width = self.attempt.fleet.workers
        self.assertGreater(width, 1)
        started = threading.Barrier(width + 1)
        release = threading.Event()
        others_finished = threading.Event()
        workers = {}
        lock = threading.Lock()
        completed = 0

        def worker(role, worker_id, prompt):
            nonlocal completed
            self.assertEqual(role, "child")
            with lock:
                delayed = not workers
                workers[worker_id] = prompt
            started.wait(timeout=4)
            if delayed:
                if not release.wait(timeout=8):
                    raise AssertionError("Test did not release fleet")
            else:
                with lock:
                    completed += 1
                    if completed == width - 1:
                        others_finished.set()
            return f"Independent finding from {worker_id}"

        with mock.patch.object(self.attempt, "run_worker", side_effect=worker):
            thread, outcome = self.start_background(
                lambda: self.attempt.delegate("Inspect the specification", "understand"))
            try:
                started.wait(timeout=4)
                self.assertTrue(others_finished.wait(timeout=4))
                self.assertEqual(len(workers), width)
                self.assertEqual(len(set(workers.values())), width)
                self.assertNotIn("understand", self.attempt.phases)
                self.assertEqual(outcome, {})
            finally:
                release.set()
                thread.join(timeout=8)
            reply = self.finish_background(thread, outcome)
        self.assertTrue(reply["ok"])
        self.assertIn("understand", self.attempt.phases)
        for worker_id in workers:
            self.assertIn(f"Independent finding from {worker_id}", reply["text"])

    def test_concurrent_delegate_calls_admit_only_one_fleet_at_a_time(self):
        width = self.attempt.fleet.workers
        started = threading.Barrier(width + 1)
        release = threading.Event()
        second_requested = threading.Event()
        second_started = threading.Event()
        lock = threading.Lock()
        active = 0
        peak = 0
        workers = set()

        def worker(role, worker_id, prompt):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                workers.add(worker_id)
            try:
                if "First request" in prompt:
                    started.wait(timeout=4)
                    if not release.wait(timeout=8):
                        raise AssertionError("Test did not release first fleet")
                else:
                    second_started.set()
                return "Complete independent review"
            finally:
                with lock:
                    active -= 1

        def second_request():
            second_requested.set()
            return self.attempt.delegate("Second request", "understand")

        with mock.patch.object(self.attempt, "run_worker", side_effect=worker):
            first_thread, first_outcome = self.start_background(
                lambda: self.attempt.delegate("First request", "understand"))
            second_thread = None
            try:
                started.wait(timeout=4)
                second_thread, second_outcome = self.start_background(second_request)
                self.assertTrue(second_requested.wait(timeout=2))
                self.assertFalse(second_started.wait(timeout=0.15))
            finally:
                release.set()
                first_thread.join(timeout=8)
                if second_thread is not None:
                    second_thread.join(timeout=8)
            self.assertTrue(self.finish_background(first_thread, first_outcome)["ok"])
            self.assertTrue(self.finish_background(second_thread, second_outcome)["ok"])
        self.assertTrue(second_started.is_set())
        self.assertEqual(peak, width)
        self.assertEqual(len(workers), 2 * width)

    def test_one_failed_worker_prevents_fleet_phase_completion(self):
        width = self.attempt.fleet.workers
        started = threading.Barrier(width)
        lock = threading.Lock()
        first = True

        def worker(role, worker_id, prompt):
            nonlocal first
            with lock:
                fail = first
                first = False
            started.wait(timeout=4)
            if fail:
                raise runtime.RoundError("One adversarial reviewer failed")
            return "Other reviewers succeeded"

        with mock.patch.object(self.attempt, "run_worker", side_effect=worker):
            with self.assertRaisesRegex(runtime.RoundError, "adversarial reviewer failed"):
                self.attempt.delegate("Find independent problems", "understand")
        self.assertNotIn("understand", self.attempt.phases)
        self.assertTrue(self.attempt.failed_child)
        self.assertFalse(any(event["type"] == "phase_completed" for event in self.events))

    def test_review_covers_added_and_moved_documents_without_expanding_the_attempt(self):
        specification = self.root / "specification"
        specification.mkdir()
        original = specification / "original.md"
        original.write_text("# Original requirements\n\nKeep product intent.\n")
        attempt = runtime.Attempt(self.configuration, self.root, self.root / "fresh-review",
                                  self.control, lambda: None, self.events.append)
        self.addCleanup(attempt.close)
        original_width = attempt.fleet.workers
        original.rename(specification / "relocated.md")
        (specification / "added.md").write_text("New requirement.\n" * 10000)
        attempt.phases = {"understand", "plan"}

        with mock.patch.object(attempt, "run_worker", return_value="Reviewed current documents") as worker:
            reply = attempt.delegate("Review all edited requirements", "review")
        self.assertTrue(reply["ok"])
        self.assertEqual(worker.call_count, original_width)
        self.assertEqual(attempt.fleet.workers, original_width)
        prompts = "\n".join(call.args[2] for call in worker.call_args_list)
        self.assertIn("specification/added.md", prompts)
        self.assertIn("specification/relocated.md", prompts)
        self.assertNotIn("specification/original.md", prompts)

    def test_parent_cannot_finish_with_accepted_delegates_before_process_launch(self):
        width = self.attempt.fleet.workers
        started = threading.Barrier(width + 1)
        release = threading.Event()

        def worker(role, worker_id, prompt):
            if role == "parent":
                return "Parent completed too soon"
            started.wait(timeout=4)
            if not release.wait(timeout=8):
                raise AssertionError("Test did not release delegates")
            return "Delayed delegate result"

        self.attempt.phases = set(runtime.PHASES)
        with mock.patch.object(self.attempt, "start_broker"), \
                mock.patch.object(self.attempt, "run_worker", side_effect=worker):
            thread, outcome = self.start_background(
                lambda: self.attempt.delegate("Accepted but not launched", "review"))
            try:
                started.wait(timeout=4)
                self.assertFalse(self.attempt.processes)
                with self.assertRaises(runtime.RoundError):
                    self.attempt.run("Finish now")
            finally:
                release.set()
                thread.join(timeout=8)
        self.assertFalse(thread.is_alive())

    def test_failed_worker_terminates_its_running_siblings(self):
        ready = self.root / "fleet-ready"
        width = self.attempt.fleet.workers
        lock = threading.Lock()
        launches = 0
        processes = []
        real_popen = subprocess.Popen

        def record(event):
            with lock:
                self.events.append(event)
                if sum(item["type"] == "worker_started" for item in self.events) == width:
                    ready.touch()

        def command(*args, **kwargs):
            nonlocal launches
            with lock:
                launches += 1
                first = launches == 1
            if first:
                code = ("import pathlib,sys,time; sys.stdin.buffer.read(); "
                        f"ready=pathlib.Path({str(ready)!r})\n"
                        "while not ready.exists(): time.sleep(0.01)\n"
                        "sys.exit(1)\n")
            else:
                code = "import sys,time; sys.stdin.buffer.read(); time.sleep(60)"
            return [sys.executable, "-c", code]

        def popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            return process

        self.attempt.record = record
        started = time.monotonic()
        with mock.patch.object(runtime, "build_exec_command", side_effect=command), \
                mock.patch.object(runtime.subprocess, "Popen", side_effect=popen):
            with self.assertRaises(runtime.CepsError):
                self.attempt.delegate("Failure must stop this fleet", "understand")
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(len(processes), width)
        self.assertTrue(all(process.poll() is not None for process in processes))
        self.assertFalse(self.attempt.processes)
        self.assertFalse(self.attempt.phases)
        self.assertTrue(self.attempt.failed_child)

    def test_quota_and_close_stop_all_active_workers_and_reject_queued_fleets(self):
        for stop_kind in ("quota", "close"):
            with self.subTest(stop=stop_kind):
                control = runtime.Control()
                events = []
                started = threading.Event()
                queued = threading.Event()
                event_lock = threading.Lock()
                attempt = None

                def record(event):
                    with event_lock:
                        events.append(event)
                        if (event["type"] == "worker_started"
                                and sum(item["type"] == "worker_started" for item in events)
                                == attempt.fleet.workers):
                            started.set()

                attempt = runtime.Attempt(self.configuration, self.root, self.root / stop_kind,
                                          control, lambda: None, record)
                code = "import sys,time; sys.stdin.buffer.read(); time.sleep(60)"
                processes = []
                real_popen = subprocess.Popen

                def popen(*args, **kwargs):
                    process = real_popen(*args, **kwargs)
                    processes.append(process)
                    return process

                def second_request():
                    queued.set()
                    return attempt.delegate("Queued fleet must not start", "understand")

                with mock.patch.object(runtime, "build_exec_command", return_value=[sys.executable, "-c", code]), \
                        mock.patch.object(runtime.subprocess, "Popen", side_effect=popen):
                    thread, outcome = self.start_background(
                        lambda: attempt.delegate("Run the first fleet", "understand"))
                    second_thread = None
                    try:
                        self.assertTrue(started.wait(timeout=5), "Entire fleet did not start")
                        second_thread, second_outcome = self.start_background(second_request)
                        self.assertTrue(queued.wait(timeout=2))
                        deadline = time.monotonic() + 2
                        while attempt.pending_fleets < 2 and time.monotonic() < deadline:
                            time.sleep(0.01)
                        self.assertEqual(attempt.pending_fleets, 2, "Second fleet was not queued")
                        if stop_kind == "quota":
                            control.stop("quota", "Usage floor reached")
                        else:
                            attempt.close()
                        thread.join(timeout=8)
                        second_thread.join(timeout=8)
                        self.assertFalse(thread.is_alive())
                        self.assertFalse(second_thread.is_alive())
                        self.assertIsInstance(outcome.get("error"), runtime.Stopped)
                        self.assertIsInstance(second_outcome.get("error"), runtime.Stopped)
                        self.assertEqual(len(processes), attempt.fleet.workers)
                        self.assertTrue(all(process.poll() is not None for process in processes))
                        self.assertFalse(attempt.processes)
                        self.assertFalse(attempt.phases)
                    finally:
                        attempt.close()
                        thread.join(timeout=8)
                        if second_thread is not None:
                            second_thread.join(timeout=8)

    def test_close_waits_for_a_worker_already_entering_process_launch(self):
        launching = threading.Event()
        release = threading.Event()
        close_requested = threading.Event()
        close_returned = threading.Event()
        processes = []
        real_popen = subprocess.Popen
        code = "import sys,time; sys.stdin.buffer.read(); time.sleep(60)"

        def popen(*args, **kwargs):
            launching.set()
            if not release.wait(timeout=8):
                raise AssertionError("Test did not release process launch")
            process = real_popen(*args, **kwargs)
            processes.append(process)
            return process

        def close():
            close_requested.set()
            self.attempt.close()
            close_returned.set()

        with mock.patch.object(runtime, "build_exec_command", return_value=[sys.executable, "-c", code]), \
                mock.patch.object(runtime.subprocess, "Popen", side_effect=popen):
            thread, outcome = self.start_background(
                lambda: self.attempt.run_worker("child", "luna-race", "Read only"))
            close_thread = None
            try:
                self.assertTrue(launching.wait(timeout=4))
                close_thread, close_outcome = self.start_background(close)
                self.assertTrue(close_requested.wait(timeout=2))
                self.assertFalse(close_returned.wait(timeout=0.15))
            finally:
                release.set()
                thread.join(timeout=8)
                if close_thread is not None:
                    close_thread.join(timeout=8)
            self.assertFalse(thread.is_alive())
            self.assertIsInstance(outcome.get("error"), runtime.Stopped)
            self.finish_background(close_thread, close_outcome)
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())
        self.assertFalse(self.attempt.processes)

    def test_phase_order_rejection_does_not_start_a_worker(self):
        with mock.patch.object(self.attempt, "run_worker") as worker:
            for phase in ("plan", "review"):
                with self.assertRaises(runtime.RoundError):
                    self.attempt.delegate("Skip prerequisites", phase)
            worker.assert_not_called()
        self.assertEqual(self.attempt.count, 0)

    def test_child_reply_is_bounded_by_utf8_bytes(self):
        reports = []

        def worker(role, worker_id, prompt):
            marker = f"Finding from {worker_id}: "
            reports.append(marker)
            return marker + "é" * runtime.MAX_TEXT

        with mock.patch.object(self.attempt, "run_worker", side_effect=worker):
            reply = self.attempt.delegate("Understand", "understand")
        self.assertLessEqual(len(reply["text"].encode()), runtime.MAX_TEXT)
        self.assertEqual(len(reports), self.attempt.fleet.workers)
        for marker in reports:
            self.assertIn(marker, reply["text"])

    def test_broker_rejects_overlong_utf8_and_model_overrides_before_work(self):
        self.attempt.start_broker()
        with mock.patch.object(self.attempt, "delegate") as delegate:
            for value in ({"task": "é" * (runtime.MAX_TEXT // 2 + 1), "phase": "understand"},
                          {"task": "valid", "phase": "understand", "model": "wrong"}):
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(2)
                    client.connect(str(self.attempt.socket_path))
                    client.sendall(json.dumps(value).encode() + b"\n")
                    with client.makefile("rb") as response:
                        self.assertFalse(json.loads(response.readline())["ok"])
            delegate.assert_not_called()

    def test_malformed_or_truncated_events_cannot_be_completed(self):
        malformed = (b"not JSON\n", b"[]\n", b'{"type":"turn.completed"}',
                     frames({"type": "turn.completed", "usage": {"input_tokens": True, "output_tokens": 1}}),
                     frames({"type": "item.completed", "item": None}))
        for data in malformed:
            with self.subTest(data=data):
                with self.assertRaises(runtime.RoundError):
                    self.execute(frames(MESSAGE, COMPLETED) + data)
                self.assertFalse(self.attempt.processes)

    def test_second_completed_turn_is_rejected(self):
        with self.assertRaisesRegex(runtime.RoundError, "second completed"):
            self.execute(frames(MESSAGE, COMPLETED, COMPLETED))

    def test_missing_final_text_or_usage_is_incomplete(self):
        for data in (frames(MESSAGE), frames(COMPLETED)):
            with self.subTest(data=data):
                with self.assertRaises(runtime.RoundError):
                    self.execute(data)

    def test_native_delegation_or_rerouting_stops_the_entire_run(self):
        for event in ({"type": "model.rerouted"},
                      {"type": "item.completed", "item": {"type": "collab_tool_call"}}):
            with self.subTest(event=event):
                self.control = runtime.Control()
                self.attempt.control = self.control
                with self.assertRaises(runtime.Stopped):
                    self.execute(frames(event, MESSAGE, COMPLETED))
                self.assertEqual(self.control.kind, "error")

    def test_quota_stop_terminates_active_worker_and_does_not_complete(self):
        def quota_check():
            if any(event["type"] == "worker_started" for event in self.events):
                self.control.stop("quota", "Floor reached")
                self.control.check()

        self.attempt.check_quota = quota_check
        code = "import sys,time; sys.stdin.buffer.read(); time.sleep(60)"
        started = time.monotonic()
        with mock.patch.object(runtime, "build_exec_command", return_value=[sys.executable, "-c", code]):
            with self.assertRaises(runtime.Stopped):
                self.attempt.run_worker("child", "luna-1", "Review")
        self.assertLess(time.monotonic() - started, 4)
        self.assertFalse(self.attempt.processes)
        self.assertEqual(self.control.kind, "quota")
        self.assertFalse(self.events[-1]["usage_complete"])

    def test_output_still_open_after_exit_cannot_be_accepted_as_complete(self):
        def delayed_output(stream, process, shutdown):
            if stream is process.stdout:
                yield frames(MESSAGE, COMPLETED)
                shutdown.wait(8)

        with mock.patch.object(runtime, "output_chunks", side_effect=delayed_output):
            with self.assertRaises(runtime.RoundError):
                self.execute(b"")

    def test_start_failure_does_not_leave_an_active_worker_counter(self):
        stats = CONTROLLER.Statistics(self.root, 20, 2)
        self.addCleanup(stats.close)
        self.attempt.record = stats.record
        with contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(runtime, "build_exec_command", return_value=["/ceps-test/no-such-command"]):
            with self.assertRaises((OSError, runtime.RoundError)):
                self.attempt.run_worker("child", "luna-1", "Review")
        self.assertEqual(stats.state["workers_active"], 0)


class StatisticsAndScopeTests(unittest.TestCase):
    def test_duplicate_token_events_count_once_per_worker_and_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            stats = CONTROLLER.Statistics(Path(directory), 20, 2)
            try:
                stats.update(attempt="1A")
                stats.record({**COMPLETED, "worker": "astra"})
                stats.record({**COMPLETED, "worker": "astra"})
                stats.record({**COMPLETED, "worker": "luna-1"})
                stats.update(attempt="1B")
                stats.record({**COMPLETED, "worker": "astra"})
                self.assertEqual(stats.state["tokens"]["input_tokens"], 30)
                self.assertEqual(stats.state["tokens"]["output_tokens"], 12)
                self.assertEqual(stats.state["tokens"]["cached_input_tokens"], 9)
            finally:
                stats.close()

    def test_external_scope_writes_are_rejected_with_real_git(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args):
                return subprocess.check_output(["git", *args], cwd=root, text=True, stderr=subprocess.DEVNULL)

            git("init", "--initial-branch=main")
            git("config", "user.name", "ceps test")
            git("config", "user.email", "ceps-test@example.invalid")
            (root / "specification").mkdir()
            (root / "specification/index.md").write_text("Original\n")
            (root / "prompt.md").write_text("Keep this prompt\n")
            git("add", ".")
            git("commit", "-m", "baseline")
            before = git("rev-parse", "HEAD").strip()
            repo = GitRepo(root)
            (root / "specification/index.md").write_text("Allowed\n")
            CONTROLLER.validate_scope(repo, before)
            for relative in ("prompt.md", "outside.md"):
                with self.subTest(relative=relative):
                    target = root / relative
                    target.write_text("Forbidden\n")
                    with self.assertRaisesRegex(runtime.CepsError, "outside specification"):
                        CONTROLLER.validate_scope(repo, before)
                    if relative == "prompt.md":
                        target.write_text("Keep this prompt\n")
                    else:
                        target.unlink()


class DescendantTests(unittest.TestCase):
    def test_detached_pipe_holder_cannot_deadlock_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "detached.pid"
            child = "import time; time.sleep(60)"
            worker = ("import json,pathlib,subprocess,sys; sys.stdin.buffer.read(); "
                      f"p=subprocess.Popen([sys.executable,'-c',{child!r}],start_new_session=True); "
                      f"pathlib.Path({str(marker)!r}).write_text(str(p.pid)); "
                      f"print({json.dumps(MESSAGE)!r},flush=True); "
                      f"print({json.dumps(COMPLETED)!r},flush=True)")
            script = textwrap.dedent(f"""
                import os,sys
                from pathlib import Path
                from types import SimpleNamespace
                sys.path.insert(0, {str(SOURCE / 'tools')!r})
                import ceps_runtime as r
                r.enable_supervision()
                r.build_exec_command = lambda *a, **k: [sys.executable, '-c', {worker!r}]
                attempt = r.Attempt(SimpleNamespace(env=lambda: dict(os.environ)), {directory!r},
                                    Path({directory!r}) / 'attempt', r.Control(), lambda: None,
                                    lambda event: None)
                try:
                    try:
                        attempt.run_worker('child', 'luna-1', 'Review')
                    except r.CepsError:
                        pass
                finally:
                    attempt.close()
                    r.drain_descendants()
                print('stopped', flush=True)
            """)
            process = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, start_new_session=True)
            try:
                output, error = process.communicate(timeout=12)
                self.assertEqual(process.returncode, 0, error.decode())
                self.assertIn(b"stopped", output)
                if marker.exists():
                    with self.assertRaises(ProcessLookupError):
                        os.kill(int(marker.read_text()), 0)
            finally:
                if marker.exists():
                    try:
                        os.kill(int(marker.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
