"""Exercise ceps against fake Codex transports and real local Git remotes.

Every Codex subprocess resolves to the fixture executable; no model or account
request leaves the machine. Authentication fixtures contain no real credentials.
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
LAUNCH = """
from pathlib import Path
import runpy, sys
from unittest.mock import patch
entry, fixture_home = sys.argv[1:3]
sys.argv = [entry, *sys.argv[3:]]
with patch.object(Path, 'home', return_value=Path(fixture_home)):
    runpy.run_path(entry, run_name='__main__')
"""
FAKE_CODEX = r'''
import json, os, pathlib, socket, subprocess, sys, time, tomllib

audit = pathlib.Path(os.environ["CEPS_FAKE_AUDIT"])
repo = pathlib.Path(os.environ["CEPS_FAKE_REPO"])
mode = os.environ["CEPS_FAKE_MODE"]
args = sys.argv[1:]

def record(kind, **values):
    line = (json.dumps({"kind": kind, "time": time.monotonic(), **values}) + "\n").encode()
    fd = os.open(audit, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)

def emit(value):
    print(json.dumps(value), flush=True)

def overrides():
    config = {}
    for index, item in enumerate(args[:-1]):
        if item in ("-c", "--config"):
            key, value = args[index + 1].split("=", 1)
            config[key] = tomllib.loads("value=" + value)["value"]
    return config

def models():
    return [{"slug": model, "id": model, "model": model, "display_name": model,
             "displayName": model, "visibility": "list", "supported_in_api": True,
             "default_reasoning_level": "high", "defaultReasoningEffort": "high",
             "supported_reasoning_levels": [{"effort": effort, "description": effort}
                                             for effort in efforts],
             "supportedReasoningEfforts": [{"reasoningEffort": effort, "description": effort}
                                            for effort in efforts]}
            for model, efforts in (
                ("gpt-6-astra", ["low", "high", "ultra"]),
                ("gpt-6-luna", ["low", "high"] if mode == "invalid_model" else
                 ["low", "high", "max"]))]

def limits():
    tracked = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", "HEAD", "--", "specification"],
        cwd=repo, text=True)
    low = (mode == "initial_low" or
           (mode in ("success", "retry_success", "fleet_barrier", "initial_quota_transient",
                     "initial_quota_transport", "fleet_quota_transient",
                     "quota_parent_completion_race") and
            "specification/improvement-" in tracked) or
           (mode == "fleet_quota_recovered_low" and
            any(event["kind"] == "quota_failure" for event in audit_events())) or
           (mode in ("quota_during_work", "quota_during_fleet") and
            audit.with_suffix(".working").exists()))
    snapshot = {"limitId": "codex", "limitName": "Codex", "normalModelSlug": None,
                "spendControlReached": False, "rateLimitReachedType": None,
                "primary": {"usedPercent": 69 if low else 20,
                            "windowDurationMins": 300, "resetsAt": 2000000000},
                "secondary": {"usedPercent": 25, "windowDurationMins": 10080,
                              "resetsAt": 2000100000},
                "credits": None}
    if mode == "missing_quota" or (mode in ("missing_quota_during_work", "missing_quota_during_fleet") and
                                   audit.with_suffix(".working").exists()):
        return {"ordinaryUsageAllowed": True, "accountId": "test-account",
                "rateLimits": None, "rateLimitsByLimitId": {}}
    return {"ordinaryUsageAllowed": True, "accountId": "test-account",
            "rateLimits": snapshot, "rateLimitsByLimitId": {"codex": snapshot}}

def audit_events():
    return [json.loads(line) for line in audit.read_text().splitlines()]

def quota_read(request):
    events = audit_events()
    failures = [event for event in events if event["kind"] == "quota_failure"]
    active_failure = (mode in ("fleet_quota_transient", "fleet_quota_persistent",
                              "fleet_quota_recovered_low", "quota_parent_completion_race") and
                      audit.with_suffix(".working").exists())
    fail = (mode == "initial_quota_persistent" or
            (mode in ("initial_quota_transient", "initial_quota_transport") and not failures) or
            (active_failure and (not failures or mode == "fleet_quota_persistent")))
    # Every read after the first failure must happen after all abandoned workers
    # have been reaped. A recovered response is required before replacement work.
    if failures:
        alive = []
        for event in events:
            if event["kind"] == "exec" and event["time"] < failures[0]["time"]:
                try:
                    os.kill(event["pid"], 0)
                except ProcessLookupError:
                    pass
                else:
                    alive.append(event["pid"])
        record("quota_recovery_read", workers_alive=alive,
               files=sorted(str(path.relative_to(repo))
                            for path in (repo / "specification").glob("*")))
    if fail:
        if mode == "quota_parent_completion_race":
            audit.with_suffix(".polling").touch()
            parent = next(event for event in events
                          if event["kind"] == "exec" and event["role"] == "parent")
            deadline = time.monotonic() + 8
            while True:
                try:
                    os.kill(parent["pid"], 0)
                except ProcessLookupError:
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError("Reviewed parent did not finish during quota read")
                time.sleep(0.01)
            # Let the runner reach the poller join before the read fails.
            time.sleep(0.5)
        record("quota_failure")
        if mode == "initial_quota_transport":
            sys.exit(1)
        emit({"id": request["id"], "error": {
            "code": -32000, "message": "Synthetic quota backend unavailable"}})
        return
    result = limits()
    record("quota_success")
    emit({"id": request["id"], "result": result})

if "--version" in args:
    record("version", argv=args)
    print(os.environ.get("CEPS_FAKE_VERSION", "codex-cli 0.156.1"))
    sys.exit(0)
if "debug" in args and "models" in args:
    record("catalog", argv=args)
    emit({"models": models()})
    sys.exit(0)
if "app-server" in args:
    record("app_server", argv=args)
    if "--strict-config" in args:
        config = overrides()
        record("configuration", argv=args, config=config,
               head=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip())
        if mode == "invalid_configuration":
            print("Synthetic strict configuration failure", file=sys.stderr)
            sys.exit(2)
    for line in sys.stdin:
        request = json.loads(line)
        method = request.get("method")
        record("rpc", method=method)
        if "id" not in request:
            continue
        if method == "initialize":
            result = {"userAgent": "ceps-fake", "platformFamily": "unix",
                      "platformOs": "linux"}
        elif method == "account/read":
            result = {"account": {"type": "chatgpt", "email": "fake@example.invalid",
                                  "planType": "pro"}, "requiresOpenaiAuth": True,
                      "workspaceRouting": {"chatgptAccountId": "test-account"}}
        elif method == "account/rateLimits/read":
            quota_read(request)
            continue
        elif method == "model/list":
            result = {"data": models(), "nextCursor": None}
        elif method == "config/read":
            fixed = overrides()
            result = {"config": {"model": fixed["model"],
                                 "model_reasoning_effort": fixed["model_reasoning_effort"],
                                 "approval_policy": fixed["approval_policy"],
                                 "agents": {"enabled": fixed["agents.enabled"]}}}
        else:
            emit({"id": request["id"], "error": {"code": -32601, "message": method}})
            continue
        emit({"id": request["id"], "result": result})
    sys.exit(0)
if "exec" not in args:
    record("unknown", argv=args)
    sys.exit(2)

config = overrides()
model = args[args.index("--model") + 1]
role = "parent" if model == "gpt-6-astra" else "child"
prompt = sys.stdin.read()
events = [json.loads(line) for line in audit.read_text().splitlines()]
number = 1 + sum(event["kind"] == "exec" and event["role"] == "parent" for event in events)
record("exec", argv=args, config=config, model=model, role=role, prompt=prompt,
       files=sorted(str(path.relative_to(repo)) for path in (repo / "specification").glob("*")),
       head=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip(),
       pid=os.getpid())
emit({"type": "thread.started", "thread_id": f"fake-{os.getpid()}"})
emit({"type": "turn.started"})

def finish():
    emit({"type": "item.completed", "item": {"id": "final", "type": "agent_message",
                                               "text": "Reviewed specification improvement."}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 25,
                                               "output_tokens": 40, "reasoning_output_tokens": 20}})

if role == "child":
    interrupted_fleet = (mode in ("fleet_quota_transient", "fleet_quota_persistent",
                                  "fleet_quota_recovered_low") and
                         not any(event["kind"] == "quota_failure" for event in events))
    if mode in ("fleet_barrier", "quota_during_fleet", "missing_quota_during_fleet") or interrupted_fleet:
        phase = next(value for value in ("understand", "plan", "review")
                     if f"You are the {value} subagent" in prompt)
        ready = audit.parent / f"ready-{phase}"
        ready.mkdir(exist_ok=True)
        (ready / str(os.getpid())).touch()
        deadline = time.monotonic() + 8
        while len(list(ready.iterdir())) < 4:
            if time.monotonic() > deadline:
                raise RuntimeError("Fleet failed to launch four overlapping workers")
            time.sleep(0.01)
        record("fleet_overlap", phase=phase, pid=os.getpid())
        if mode != "fleet_barrier":
            audit.with_suffix(".working").write_text("working")
            while True:
                time.sleep(1)
    finish()
    sys.exit(0)

def edit():
    if mode != "no_changes":
        (repo / f"specification/improvement-{number}.md").write_text(f"Improvement {number}\n")

if mode in ("quota_during_work", "missing_quota_during_work"):
    edit()
    audit.with_suffix(".working").write_text("working")
    record("waiting")
    while True:
        time.sleep(1)
if mode == "wait":
    edit()
    marker = audit.with_suffix(".late-write")
    ready = audit.with_suffix(".child-ready")
    code = """
import os, pathlib, sys, time
pathlib.Path(sys.argv[2]).write_text('ready')
while os.getppid() == int(sys.argv[1]):
    time.sleep(0.01)
time.sleep(0.4)
pathlib.Path(sys.argv[3]).write_text('late write')
pathlib.Path(sys.argv[4]).write_text('unfinished detached edit')
"""
    child = subprocess.Popen([sys.executable, "-c", code, str(os.getpid()), str(ready), str(marker),
                              str(repo / "specification/late.md")],
                             start_new_session=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 3
    while not ready.exists():
        if time.monotonic() > deadline:
            raise RuntimeError("detached child did not start")
        time.sleep(0.01)
    record("waiting", child_pid=child.pid)
    while True:
        time.sleep(1)
if mode == "retry_success" and number <= 2:
    edit()
    emit({"type": "turn.failed", "error": {"message": "Synthetic provider failure"}})
    sys.exit(1)
if mode == "missing_phases":
    edit()
    finish()
    sys.exit(0)
if mode == "native_spawn":
    edit()
    emit({"type": "item.completed", "item": {"type": "collab_tool_call"}})
    finish()
    sys.exit(0)

bridge_args = config["mcp_servers.ceps.args"]
socket_path = bridge_args[bridge_args.index("--socket") + 1]
if mode in ("quota_during_fleet", "missing_quota_during_fleet", "fleet_quota_transient",
            "fleet_quota_persistent", "fleet_quota_recovered_low"):
    edit()
for phase in ("understand", "plan", "review"):
    if phase == "review":
        edit()
    delegation = {"task": f"Perform {phase} thoroughly.", "phase": phase}
    if mode == "fleet_barrier":
        request = {"jsonrpc": "2.0", "id": phase, "method": "tools/call",
                   "params": {"name": "delegate", "arguments": delegation}}
        response = subprocess.run([config["mcp_servers.ceps.command"], *bridge_args],
                                  input=json.dumps(request) + "\n", capture_output=True,
                                  text=True, timeout=15, check=True)
        envelope = json.loads(response.stdout)
        assert envelope["id"] == phase
        reply = json.loads(envelope["result"]["content"][0]["text"])
    else:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(15)
            connection.connect(socket_path)
            connection.sendall((json.dumps(delegation) + "\n").encode())
            with connection.makefile("rb") as stream:
                reply = json.loads(stream.readline())
    record("delegated", phase=phase, reply=reply)
    if not reply.get("ok"):
        emit({"type": "turn.failed", "error": {"message": "Subagent failed"}})
        sys.exit(1)
if mode == "missing_completion":
    emit({"type": "item.completed", "item": {"type": "agent_message", "text": "Incomplete"}})
    sys.exit(0)
if mode == "outside_scope":
    (repo / "prompt.md").write_text("Unexpected prompt change\n")
if mode == "quota_parent_completion_race" and number == 1:
    audit.with_suffix(".working").touch()
    deadline = time.monotonic() + 8
    while not audit.with_suffix(".polling").exists():
        if time.monotonic() > deadline:
            raise RuntimeError("Quota poll did not overlap reviewed parent completion")
        time.sleep(0.01)
    record("reviewed_parent_complete")
finish()
'''


class CepsRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ceps-e2e-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "repo.with.dots"
        self.remote = self.base / "origin.git"
        self.bin = self.base / "bin"
        self.audit = self.base / "audit.jsonl"
        self.state = self.base / "state"
        self.fixture_home = self.base / "user"
        self.bin.mkdir()
        self.root.mkdir()
        shutil.copytree(SOURCE / "tools", self.root / "tools", ignore=shutil.ignore_patterns("__pycache__"))
        (self.root / "specification").mkdir()
        (self.root / "metaspecification").mkdir()
        (self.root / "specification/overview.md").write_text("Terminal art.\n")
        (self.root / "metaspecification/style.md").write_text("Use clear language.\n")
        (self.root / "prompt.md").write_text("Understand, plan, improve, review; commit and push.\n")
        (self.root / ".gitignore").write_text("__pycache__/\n")
        codex_home = self.fixture_home / ".codex"
        codex_home.mkdir(parents=True)
        (codex_home / "auth.json").write_text(json.dumps({
            "auth_mode": "chatgpt", "last_refresh": "2026-09-23T00:00:00Z",
            "tokens": {"access_token": "test-access", "id_token": "test-id",
                       "refresh_token": "test-refresh", "account_id": "test-account"},
        }))
        fake = self.bin / "codex"
        fake.write_text(f"#!{sys.executable}\n" + FAKE_CODEX)
        fake.chmod(0o755)
        self.env = dict(os.environ)
        self.env.pop("CODEX_HOME", None)
        self.env.update(PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        CEPS_FAKE_MODE="success", CEPS_FAKE_AUDIT=str(self.audit),
                        CEPS_FAKE_REPO=str(self.root), XDG_STATE_HOME=str(self.state),
                        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        self.git(self.base, "init", "--bare", str(self.remote))
        self.git(self.root, "init", "-b", "main")
        self.git(self.root, "config", "user.name", "Ceps Test")
        self.git(self.root, "config", "user.email", "ceps@example.invalid")
        self.git(self.root, "add", ".")
        self.git(self.root, "commit", "-m", "Initial")
        self.git(self.root, "remote", "add", "origin", str(self.remote))
        self.git(self.root, "push", "-u", "origin", "main")
        self.initial_head = self.git(self.root, "rev-parse", "HEAD")

    def git(self, cwd, *args):
        return subprocess.check_output(["git", *args], cwd=cwd, env=self.env,
                                       stderr=subprocess.PIPE, text=True).strip()

    def command(self, *args):
        return [sys.executable, "-c", LAUNCH, str(self.root / "tools/ceps"),
                str(self.fixture_home), *args]

    def run_ceps(self, mode="success", args=("30", "--buffer", "2"), timeout=35):
        env = dict(self.env, CEPS_FAKE_MODE=mode)
        return subprocess.run(self.command(*args), cwd=self.base, env=env,
                              capture_output=True, text=True, timeout=timeout)

    def events(self, kind=None):
        if not self.audit.exists():
            return []
        events = [json.loads(line) for line in self.audit.read_text().splitlines()]
        return [event for event in events if kind is None or event["kind"] == kind]

    def assert_saved(self):
        self.assertEqual(self.git(self.root, "status", "--porcelain"), "")
        head = self.git(self.root, "rev-parse", "HEAD")
        self.assertEqual(head, self.git(self.remote, "rev-parse", "main"))
        return head

    def statistics(self):
        latest = json.loads((self.state / "ceps/latest.json").read_text())
        return json.loads((Path(latest["directory"]) / "stats.json").read_text())

    def test_complete_round_pins_parent_and_children_and_stops_before_next(self):
        result = self.run_ceps()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotEqual(self.assert_saved(), self.initial_head)
        launches = self.events("exec")
        self.assertEqual([event["role"] for event in launches], ["parent"] + ["child"] * 12)
        checks = self.events("configuration")
        self.assertEqual([check["config"]["model"] for check in checks],
                         ["gpt-6-astra", "gpt-6-luna"])
        self.assertEqual([check["config"]["model_reasoning_effort"] for check in checks],
                         ["ultra", "max"])
        activity = [event["kind"] for event in self.events()
                    if event["kind"] in ("configuration", "exec")]
        self.assertEqual(activity[:2], ["configuration", "configuration"])
        for event in launches:
            parent = event["role"] == "parent"
            self.assertEqual(event["model"], "gpt-6-astra" if parent else "gpt-6-luna")
            self.assertEqual(event["config"]["model_reasoning_effort"], "ultra" if parent else "max")
            self.assertIs(event["config"]["agents.enabled"], False)
            self.assertIs(event["config"]["features.multi_agent"], False)
            self.assertIn("--strict-config", event["argv"])
            self.assertEqual("mcp_servers.ceps.args" in event["config"], parent)
            self.assertEqual(event["config"].get("mcp_servers.ceps.tools.delegate.approval_mode"),
                             "approve" if parent else None)
            self.assertEqual(event["config"]["projects"],
                             {str(self.root): {"trust_level": "untrusted"}})
        self.assertEqual([event["phase"] for event in self.events("delegated")],
                         ["understand", "plan", "review"])
        self.assertTrue((self.root / "specification/improvement-1.md").exists())
        stats = self.statistics()
        self.assertEqual(stats["completed_rounds"], 1)
        self.assertEqual(stats["tokens"]["input_tokens"], 1300)
        self.assertEqual(stats["tokens"]["output_tokens"], 520)
        self.assertEqual(stats["workers_started"], 13)
        self.assertEqual(stats["workers_active"], 0)
        self.assertEqual(stats["quota"]["min_remaining"], 31)
        self.assertEqual(stats["usage_incomplete_workers"], 0)
        previous_events = self.events()
        status = self.run_ceps(args=("--status",))
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertEqual(json.loads(status.stdout), stats)
        self.assertEqual(self.events(), previous_events)
        directory = Path(json.loads((self.state / "ceps/latest.json").read_text())["directory"])
        self.assertEqual((directory / "stats.json").stat().st_mode & 0o777, 0o600)
        self.assertFalse((directory / "runtime/auth.json").exists())

    def test_each_delegation_launches_overlapping_fleet_processes(self):
        result = self.run_ceps("fleet_barrier")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_saved()
        for phase in ("understand", "plan", "review"):
            overlap = [event for event in self.events("fleet_overlap") if event["phase"] == phase]
            self.assertEqual(len(overlap), 4)
            self.assertEqual(len({event["pid"] for event in overlap}), 4)
            delegated = next(event for event in self.events("delegated") if event["phase"] == phase)
            self.assertTrue(delegated["reply"]["ok"])
        children = [event for event in self.events("exec") if event["role"] == "child"]
        self.assertEqual(len({event["prompt"] for event in children}), 12)

    def test_quota_stop_drains_entire_fleet_before_rollback(self):
        for mode, code in (("quota_during_fleet", 0), ("missing_quota_during_fleet", 1)):
            with self.subTest(mode=mode):
                self.audit.unlink(missing_ok=True)
                self.audit.with_suffix(".working").unlink(missing_ok=True)
                shutil.rmtree(self.base / "ready-understand", ignore_errors=True)
                result = self.run_ceps(mode)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertEqual(self.assert_saved(), self.initial_head)
                children = [event for event in self.events("exec") if event["role"] == "child"]
                self.assertEqual(len(children), 4)
                self.assertEqual(len(self.events("fleet_overlap")), 4)
                for event in children:
                    with self.assertRaises(ProcessLookupError):
                        os.kill(event["pid"], 0)
                self.assertEqual(self.statistics()["workers_active"], 0)

    def test_initial_transient_quota_error_reconnects_before_any_model_work(self):
        result = self.run_ceps("initial_quota_transient")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotEqual(self.assert_saved(), self.initial_head)
        self.assertEqual(len(self.events("quota_failure")), 1)
        recovered = self.events("quota_recovery_read")[0]
        self.assertEqual(recovered["workers_alive"], [])
        self.assertEqual(recovered["files"], ["specification/overview.md"])
        successful_read = next(event for event in self.events("quota_success")
                               if event["time"] > recovered["time"])
        self.assertGreater(self.events("exec")[0]["time"], successful_read["time"])
        self.assertGreaterEqual(recovered["time"] - self.events("quota_failure")[0]["time"], 0.9)
        self.assertGreaterEqual(len(self.events("app_server")), 4)
        self.assertIn("round 1A", self.events("exec")[0]["prompt"])
        self.assertEqual(self.statistics()["completed_rounds"], 1)

    def test_initial_quota_transport_disconnect_can_recover(self):
        result = self.run_ceps("initial_quota_transport", args=("--check", "30"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.events("quota_failure")), 1)
        self.assertTrue(self.events("quota_success"))
        self.assertEqual(self.events("exec"), [])
        self.assertEqual(self.assert_saved(), self.initial_head)

    def test_persistent_initial_quota_failure_has_three_reads_and_no_model_work(self):
        (self.root / "existing.md").write_text("user work\n")
        result = self.run_ceps("initial_quota_persistent")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        failures = self.events("quota_failure")
        self.assertEqual(len(failures), 3)
        self.assertGreaterEqual(failures[1]["time"] - failures[0]["time"], 0.9)
        self.assertGreaterEqual(failures[2]["time"] - failures[1]["time"], 1.9)
        self.assertEqual(self.events("exec"), [])
        self.assertEqual(self.git(self.root, "rev-parse", "HEAD"), self.initial_head)
        self.assertEqual(self.git(self.root, "status", "--porcelain"), "?? existing.md")
        self.assertIn("internal server failure", self.statistics()["reason"])
        self.assertNotIn("Synthetic quota backend unavailable", self.statistics()["reason"])
        self.assertIn("-32000", self.statistics()["reason"])

    def test_active_quota_error_drains_fleet_and_resets_before_recovery_and_retry(self):
        result = self.run_ceps("fleet_quota_transient")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotEqual(self.assert_saved(), self.initial_head)
        self.assertEqual(len(self.events("quota_failure")), 1)
        parents = [event for event in self.events("exec") if event["role"] == "parent"]
        self.assertEqual(len(parents), 2)
        for parent, label in zip(parents, ("1A", "1B")):
            self.assertIn(f"round {label}", parent["prompt"])
            self.assertEqual(parent["files"], ["specification/overview.md"])
            self.assertEqual(parent["head"], self.initial_head)
        self.assertEqual(len(self.events("fleet_overlap")), 4)
        recovered = self.events("quota_recovery_read")[0]
        self.assertEqual(recovered["workers_alive"], [])
        self.assertEqual(recovered["files"], ["specification/overview.md"])
        successful_read = next(event for event in self.events("quota_success")
                               if event["time"] > recovered["time"])
        self.assertGreater(parents[1]["time"], successful_read["time"])
        self.assertFalse((self.root / "specification/improvement-1.md").exists())
        self.assertTrue((self.root / "specification/improvement-2.md").exists())
        stats = self.statistics()
        self.assertEqual(stats["workers_active"], 0)
        self.assertEqual(stats["completed_rounds"], 1)
        self.assertEqual(stats["failed_attempts"], 1)

    def test_persistent_active_quota_failure_drains_fleet_without_new_model_work(self):
        result = self.run_ceps("fleet_quota_persistent")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.assert_saved(), self.initial_head)
        self.assertEqual(len(self.events("quota_failure")), 3)
        launches = self.events("exec")
        self.assertEqual([event["role"] for event in launches], ["parent"] + ["child"] * 4)
        self.assertEqual(len(self.events("fleet_overlap")), 4)
        for event in self.events("quota_recovery_read"):
            self.assertEqual(event["workers_alive"], [])
            self.assertEqual(event["files"], ["specification/overview.md"])
        self.assertEqual(self.statistics()["workers_active"], 0)
        self.assertIn("internal server failure", self.statistics()["reason"])
        self.assertIn("-32000", self.statistics()["reason"])
        self.assertNotIn("Synthetic quota backend unavailable", self.statistics()["reason"])

    def test_poll_failure_racing_reviewed_parent_completion_discards_attempt_before_publish(self):
        result = self.run_ceps("quota_parent_completion_race")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotEqual(self.assert_saved(), self.initial_head)
        parents = [event for event in self.events("exec") if event["role"] == "parent"]
        self.assertEqual(len(parents), 2)
        for parent, label in zip(parents, ("1A", "1B")):
            self.assertIn(f"round {label}", parent["prompt"])
            self.assertEqual(parent["head"], self.initial_head)
            self.assertEqual(parent["files"], ["specification/overview.md"])
        self.assertLess(self.events("reviewed_parent_complete")[0]["time"],
                        self.events("quota_failure")[0]["time"])
        self.assertEqual(len(self.events("quota_failure")), 1)
        self.assertEqual(self.events("quota_recovery_read")[0]["workers_alive"], [])
        self.assertFalse((self.root / "specification/improvement-1.md").exists())
        self.assertTrue((self.root / "specification/improvement-2.md").exists())
        self.assertEqual(self.statistics()["completed_rounds"], 1)
        self.assertEqual(self.statistics()["failed_attempts"], 1)

    def test_recovered_quota_cutoff_stops_without_replacement_workers(self):
        result = self.run_ceps("fleet_quota_recovered_low")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.assert_saved(), self.initial_head)
        self.assertEqual(len(self.events("quota_failure")), 1)
        recovered = self.events("quota_recovery_read")[0]
        self.assertEqual(recovered["workers_alive"], [])
        self.assertEqual(recovered["files"], ["specification/overview.md"])
        self.assertEqual(len(self.events("exec")), 5)
        self.assertEqual(self.statistics()["workers_active"], 0)
        self.assertEqual(self.statistics()["quota"]["min_remaining"], 31)

    def test_signal_during_quota_backoff_stops_without_waiting_for_retries(self):
        env = dict(self.env, CEPS_FAKE_MODE="initial_quota_persistent")
        process = subprocess.Popen(self.command("30"), cwd=self.base, env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   start_new_session=True)
        try:
            deadline = time.monotonic() + 15
            while not self.events("quota_failure"):
                if process.poll() is not None or time.monotonic() > deadline:
                    stdout, stderr = process.communicate(timeout=3)
                    self.fail("quota reader did not reach backoff: " + stdout + stderr)
                time.sleep(0.025)
            time.sleep(0.1)
            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 130, stdout + stderr)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=3)
        self.assertEqual(len(self.events("quota_failure")), 1)
        self.assertEqual(self.events("exec"), [])
        self.assertEqual(self.assert_saved(), self.initial_head)

    def test_check_uses_no_model_turn_and_does_not_checkpoint_dirty_work(self):
        (self.root / "existing.md").write_text("user work\n")
        result = self.run_ceps(args=("--check", "30"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.events("exec"), [])
        checks = self.events("configuration")
        self.assertEqual(len(checks), 2)
        for check in checks:
            self.assertIn("--strict-config", check["argv"])
            self.assertEqual(check["head"], self.initial_head)
            self.assertEqual(check["config"]["projects"],
                             {str(self.root): {"trust_level": "untrusted"}})
        self.assertEqual(self.git(self.root, "rev-parse", "HEAD"), self.initial_head)
        self.assertEqual(self.git(self.root, "status", "--porcelain"), "?? existing.md")

    def test_previous_verified_codex_version_still_passes_preflight(self):
        self.env["CEPS_FAKE_VERSION"] = "codex-cli 0.156.0"
        result = self.run_ceps(args=("--check", "30"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.events("configuration")), 2)
        self.assertEqual(self.events("exec"), [])
        self.assertEqual(self.assert_saved(), self.initial_head)

    def test_unverified_codex_version_stops_before_account_or_git_work(self):
        self.env["CEPS_FAKE_VERSION"] = "codex-cli 0.157.0"
        (self.root / "existing.md").write_text("user work\n")
        result = self.run_ceps(args=("--check", "30"))
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("found codex-cli 0.157.0", result.stderr)
        self.assertEqual([event["kind"] for event in self.events()], ["version"])
        self.assertEqual(self.git(self.root, "rev-parse", "HEAD"), self.initial_head)
        self.assertEqual(self.git(self.root, "status", "--porcelain"), "?? existing.md")

    def test_invalid_effective_configuration_stops_before_git_or_model_work(self):
        (self.root / "existing.md").write_text("user work\n")
        result = self.run_ceps("invalid_configuration")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.events("configuration"))
        self.assertEqual(self.events("exec"), [])
        self.assertEqual(self.git(self.root, "rev-parse", "HEAD"), self.initial_head)
        self.assertEqual(self.git(self.root, "status", "--porcelain"), "?? existing.md")

    def test_initial_quota_cutoff_does_not_checkpoint_existing_work(self):
        (self.root / "existing.md").write_text("user work\n")
        result = self.run_ceps("initial_low")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.events("exec"), [])
        self.assertEqual(self.git(self.root, "rev-parse", "HEAD"), self.initial_head)
        self.assertEqual(self.git(self.root, "status", "--porcelain"), "?? existing.md")

    def test_missing_quota_or_required_model_stops_before_git_or_model_work(self):
        for mode in ("missing_quota", "invalid_model"):
            with self.subTest(mode=mode):
                result = self.run_ceps(mode)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.events("exec"), [])
                self.assertEqual(self.assert_saved(), self.initial_head)

    def test_existing_work_is_checkpointed_before_model_work(self):
        (self.root / "existing.md").write_text("user work\n")
        result = self.run_ceps()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_saved()
        parent = self.events("exec")[0]
        self.assertNotEqual(parent["head"], self.initial_head)
        self.assertEqual(self.git(self.root, "show", parent["head"] + ":existing.md"), "user work")
        self.assertEqual(self.git(self.root, "show", "HEAD:existing.md"), "user work")

    def test_spec_lock_prevents_competing_runner_without_git_writes(self):
        with (self.root / ".git/spec.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_ceps()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.events("exec"), [])
        self.assertEqual(self.assert_saved(), self.initial_head)

    def test_missing_required_phases_retries_three_times_and_discards_every_edit(self):
        result = self.run_ceps("missing_phases")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.assert_saved(), self.initial_head)
        launches = self.events("exec")
        self.assertEqual(len(launches), 3)
        self.assertTrue(all(event["files"] == ["specification/overview.md"] for event in launches))

    def test_missing_normal_completion_rolls_back_even_after_all_review_phases(self):
        result = self.run_ceps("missing_completion")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.assert_saved(), self.initial_head)
        self.assertEqual(len([event for event in self.events("exec") if event["role"] == "parent"]), 3)
        self.assertEqual(len(self.events("delegated")), 9)

    def test_two_failed_attempts_then_success_preserve_round_number_and_clean_baseline(self):
        result = self.run_ceps("retry_success")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotEqual(self.assert_saved(), self.initial_head)
        parents = [event for event in self.events("exec") if event["role"] == "parent"]
        self.assertEqual(len(parents), 3)
        for event, label in zip(parents, ("1A", "1B", "1C")):
            self.assertIn(f"round {label}", event["prompt"])
            self.assertEqual(event["files"], ["specification/overview.md"])
            self.assertEqual(event["head"], self.initial_head)
        self.assertFalse((self.root / "specification/improvement-1.md").exists())
        self.assertFalse((self.root / "specification/improvement-2.md").exists())
        self.assertTrue((self.root / "specification/improvement-3.md").exists())

    def test_three_completed_rounds_without_changes_stop(self):
        result = self.run_ceps("no_changes")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.assert_saved(), self.initial_head)
        self.assertEqual(len([event for event in self.events("exec") if event["role"] == "parent"]), 3)

    def test_native_subagent_event_stops_without_retry_and_rolls_back(self):
        result = self.run_ceps("native_spawn")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.assert_saved(), self.initial_head)
        self.assertEqual(len(self.events("exec")), 1)

    def test_scope_violation_discards_reviewed_edits_without_retry(self):
        result = self.run_ceps("outside_scope")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.assert_saved(), self.initial_head)
        self.assertEqual(len([event for event in self.events("exec") if event["role"] == "parent"]), 1)

    def test_quota_cutoff_or_missing_telemetry_stops_active_work_without_retry(self):
        for mode, code in (("quota_during_work", 0), ("missing_quota_during_work", 1)):
            with self.subTest(mode=mode):
                self.audit.unlink(missing_ok=True)
                self.audit.with_suffix(".working").unlink(missing_ok=True)
                result = self.run_ceps(mode)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertEqual(self.assert_saved(), self.initial_head)
                self.assertEqual(len(self.events("exec")), 1)
                self.assertEqual(self.statistics()["workers_active"], 0)

    def test_signal_stops_detached_descendant_before_reset(self):
        env = dict(self.env, CEPS_FAKE_MODE="wait")
        process = subprocess.Popen(self.command("30"), cwd=self.base, env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   start_new_session=True)
        try:
            deadline = time.monotonic() + 15
            while not self.events("waiting"):
                if process.poll() is not None or time.monotonic() > deadline:
                    stdout, stderr = process.communicate(timeout=3)
                    self.fail("worker did not reach signal checkpoint: " + stdout + stderr)
                time.sleep(0.025)
            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 130, stdout + stderr)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=3)
        self.assertEqual(self.assert_saved(), self.initial_head)
        self.assertEqual(len(self.events("exec")), 1)
        child = self.events("waiting")[0]["child_pid"]
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)
        self.assertFalse((self.root / "specification/late.md").exists())


if __name__ == "__main__":
    unittest.main()
