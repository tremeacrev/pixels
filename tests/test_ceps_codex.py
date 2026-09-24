"""Model locks, credential isolation, and paid-free Codex wire validation."""

import copy
import http.server
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from ceps_codex import (  # noqa: E402
    CHILD_MODEL, PARENT_MODEL, CodexError, build_exec_command,
    prepare_runtime, validate_catalog, validate_launch_configuration,
)


def catalog_fixture():
    return {"models": [
        {"slug": PARENT_MODEL, "visibility": "list", "supported_reasoning_levels":
         [{"effort": level} for level in ("low", "medium", "high", "xhigh", "max", "ultra")]},
        {"slug": CHILD_MODEL, "visibility": "list", "supported_reasoning_levels":
         [{"effort": level} for level in ("low", "medium", "high", "xhigh", "max")]},
    ]}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ceps-codex-unit-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.auth = {"auth_mode": "chatgpt", "OPENAI_API_KEY": None,
                     "tokens": {"access_token": "access-secret", "id_token": "id-secret",
                                "refresh_token": "refresh-secret", "account_id": "account"},
                     "last_refresh": "2026-09-23T18:00:00Z"}
        (self.source / "auth.json").write_text(json.dumps(self.auth))
        self.runtime = prepare_runtime(self.root / "runtime", self.source,
                                       catalog_fixture(), codex=sys.executable)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()

    def test_catalog_requires_exact_models_and_maximum_efforts(self):
        self.assertEqual(validate_catalog(catalog_fixture())["parent"]["reasoning_effort"], "ultra")
        for mutate in (
            lambda d: d["models"].pop(),
            lambda d: d["models"][0].update(slug="astra"),
            lambda d: d["models"][0]["supported_reasoning_levels"].pop(),
            lambda d: d["models"][1]["supported_reasoning_levels"].append({"effort": "ultra"}),
            lambda d: d["models"].append(copy.deepcopy(d["models"][0])),
        ):
            with self.subTest(mutate=mutate):
                value = catalog_fixture()
                mutate(value)
                with self.assertRaises(CodexError):
                    validate_catalog(value)

    def test_credentials_never_copy_refresh_token_and_reuse_same_account(self):
        env = self.runtime.env({"HOME": "/real/home", "PATH": "/bin", "CODEX_HOME": "/wrong",
                                "CODEX_ACCESS_TOKEN": "wrong", "CODEX_API_KEY": "wrong",
                                "OPENAI_API_KEY": "wrong", "OPENAI_BASE_URL": "http://wrong"})
        auth_file = self.runtime.home / "auth.json"
        snapshot = json.loads(auth_file.read_text())
        self.assertEqual(snapshot["tokens"]["refresh_token"], "")
        self.assertEqual(snapshot["tokens"]["account_id"], "account")
        self.assertNotIn("refresh-secret", auth_file.read_text())
        self.assertEqual(json.loads((self.source / "auth.json").read_text()), self.auth)
        self.assertEqual(stat.S_IMODE(auth_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.runtime.home.stat().st_mode), 0o700)
        self.assertEqual(env["HOME"], "/real/home")
        self.assertEqual(env["CODEX_HOME"], str(self.runtime.home))
        self.assertFalse(any("TOKEN" in key or "KEY" in key for key in env))
        self.auth["tokens"]["access_token"] = "new-access-secret"
        (self.source / "auth.json").write_text(json.dumps(self.auth))
        self.runtime.env({})
        self.assertEqual(json.loads(auth_file.read_text())["tokens"]["access_token"], "new-access-secret")
        original_identity = self.runtime.metadata["chatgpt_account_sha256"]
        self.assertNotEqual(original_identity, "account")
        self.auth["tokens"]["account_id"] = "different-account"
        (self.source / "auth.json").write_text(json.dumps(self.auth))
        with self.assertRaisesRegex(CodexError, "account changed"):
            self.runtime.env({})
        self.assertEqual(self.runtime.metadata["chatgpt_account_sha256"], original_identity)
        self.assertEqual(json.loads(auth_file.read_text())["tokens"]["account_id"], "account")
        self.runtime.clear_credentials()
        self.assertFalse(auth_file.exists())

    def test_api_key_auth_and_runtime_in_workspace_are_rejected(self):
        (self.source / "auth.json").write_text(json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "secret"}))
        with self.assertRaises(CodexError):
            self.runtime.env({})
        with self.assertRaises(CodexError):
            build_exec_command(self.runtime, "child", self.root)

    def test_fixed_commands_and_parent_requires_private_broker(self):
        child = build_exec_command(self.runtime, "child", self.worktree)
        self.assertEqual(child[child.index("--model") + 1], CHILD_MODEL)
        self.assertIn('model_reasoning_effort="max"', child)
        self.assertIn("agents.enabled=false", child)
        self.assertIn("features.hooks=false", child)
        self.assertIn("sandbox_workspace_write.network_access=false", child)
        self.assertFalse(any("mcp_servers" in arg for arg in child))
        self.assertEqual(child[-1], "-")
        with self.assertRaises(CodexError):
            build_exec_command(self.runtime, "parent", self.worktree)
        with self.assertRaises(CodexError):
            build_exec_command(self.runtime, "parent", self.worktree, self.root / "broker.sock")
        (self.runtime.home / "ceps_mcp.py").write_text("# test bridge")
        parent = build_exec_command(self.runtime, "parent", self.worktree, self.root / "broker.sock")
        self.assertEqual(parent[parent.index("--model") + 1], PARENT_MODEL)
        self.assertIn('model_reasoning_effort="ultra"', parent)
        self.assertIn("mcp_servers.ceps.required=true", parent)
        with self.assertRaises(CodexError):
            build_exec_command(self.runtime, "child", self.worktree, self.root / "broker.sock")


FAKE_BRIDGE = r'''
import json, sys, pathlib
pathlib.Path(__file__).with_suffix(".started").write_text("started")
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request["method"]
    if method == "initialize":
        result = {"protocolVersion": request["params"]["protocolVersion"],
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "ceps-wire-test", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "delegate", "description": "CEPS_FIXED_LUNA_BROKER_SENTINEL",
                             "inputSchema": {"type": "object", "properties": {}}}]}
    elif method == "tools/call":
        pathlib.Path(__file__).with_suffix(".called").write_text(json.dumps(request["params"]))
        result = {"content": [{"type": "text", "text": "CEPS_BROKER_CALL_SUCCEEDED"}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
'''


@unittest.skipUnless(shutil.which("codex"), "installed Codex CLI required for local wire capture")
class LocalWireTests(unittest.TestCase):
    """The only inference endpoint is localhost and it always returns HTTP 400."""

    @classmethod
    def setUpClass(cls):
        raw = subprocess.run(["codex", "debug", "models", "--bundled"],
                             capture_output=True, text=True, timeout=15, check=True)
        cls.catalog = json.loads(raw.stdout)
        # The bundled catalog predates Luna 6 in CLI 0.156. Use a complete
        # bundled model shape with the *explicit* current Luna fixture. This
        # does not claim remote account entitlement or perform remote lookup.
        astra = next((m for m in cls.catalog["models"] if m["slug"] == PARENT_MODEL), None)
        if astra is None:
            raise unittest.SkipTest("bundled catalog does not contain Astra")
        if not any(m["slug"] == CHILD_MODEL for m in cls.catalog["models"]):
            luna = copy.deepcopy(astra)
            luna["slug"] = CHILD_MODEL
            luna["display_name"] = "GPT-6-Luna"
            luna["supported_reasoning_levels"] = [
                item for item in luna["supported_reasoning_levels"] if item["effort"] != "ultra"]
            cls.catalog["models"].append(luna)

    def test_real_cli_pins_models_and_skips_conflicting_project_configuration(self):
        captures = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                captures.append(request)
                if request["model"] == PARENT_MODEL and "ceps_fixture_call" not in json.dumps(request):
                    script = ('const t = ALL_TOOLS.find(t => t.name.includes("ceps") '
                              '&& t.name.endsWith("delegate")); '
                              'text(await tools[t.name]({task:"fixture",phase:"understand"}));')
                    call = {"type": "custom_tool_call", "id": "fixture_tool", "name": "exec",
                            "namespace": "functions", "call_id": "ceps_fixture_call", "input": script}
                    events = [
                        {"type": "response.created", "response": {"id": "fixture_response"}},
                        {"type": "response.output_item.done", "output_index": 0, "item": call},
                        {"type": "response.completed", "response": {"id": "fixture_response",
                            "output": [call], "usage": {"input_tokens": 1, "output_tokens": 1,
                                                       "total_tokens": 2}}},
                    ]
                    body = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                                   for event in events).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                body = b'{"error":{"message":"CEPS_LOCAL_CAPTURE_ONLY"}}'
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        with tempfile.TemporaryDirectory(prefix="ceps-wire-") as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            worktree = root / ".local" / "project.with.dots"
            worktree.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(worktree)], check=True)
            project = worktree / ".codex"
            project.mkdir()
            (project / "config.toml").write_text('''model = "gpt-5.6-terra"
model_reasoning_effort = "low"
[agents]
enabled = true
[features]
hooks = true
memories = true
[mcp_servers.unrelated]
command = "CEPS_UNRELATED_MCP_MUST_NOT_START"
''')
            runtime = prepare_runtime(root / "runtime", source, self.catalog)
            (runtime.home / "ceps_mcp.py").write_text(FAKE_BRIDGE)
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(("CODEX_", "OPENAI_"))}
            env["CODEX_HOME"] = str(runtime.home)
            with mock.patch.object(type(runtime), "env", return_value=env):
                validate_launch_configuration(runtime, worktree, root / "broker.sock")
            overrides = {
                "model_provider": "ceps_capture",
                "model_providers.ceps_capture.name": "Local test capture",
                "model_providers.ceps_capture.base_url": f"http://127.0.0.1:{server.server_port}",
                "model_providers.ceps_capture.wire_api": "responses",
                "model_providers.ceps_capture.requires_openai_auth": False,
                "model_providers.ceps_capture.supports_websockets": False,
                "model_providers.ceps_capture.request_max_retries": 0,
            }
            for role, expected in (("parent", PARENT_MODEL), ("child", CHILD_MODEL)):
                with self.subTest(role=role):
                    command = build_exec_command(runtime, role, worktree,
                                                 root / "broker.sock" if role == "parent" else None)
                    # Transport replacement exists only in this paid-free test.
                    for key, value in overrides.items():
                        command[-1:-1] = ["-c", f"{key}={json.dumps(value)}"]
                    before = len(captures)
                    result = subprocess.run(command, input="Reply OK.", env=env,
                                            capture_output=True, text=True, timeout=25)
                    self.assertEqual(len(captures), before + (2 if role == "parent" else 1),
                                     result.stderr[-2000:])
                    self.assertNotEqual(result.returncode, 0)  # Deliberate local HTTP 400.
                    body = captures[-1]
                    self.assertEqual(body["model"], expected)
                    self.assertEqual(body["reasoning"]["effort"],
                                     "xhigh" if role == "parent" else "max")
                    serialized = json.dumps(body)
                    self.assertNotIn("spawn_agent", serialized)
                    self.assertNotIn("CEPS_UNRELATED_MCP_MUST_NOT_START", result.stderr)
                    # Code mode defers MCP descriptions, so first-request text
                    # need not enumerate the broker's tools. Confirm the
                    # configured bridge actually started instead.
                    marker = runtime.home / "ceps_mcp.started"
                    self.assertEqual(marker.exists(), role == "parent")
                    marker.unlink(missing_ok=True)
                    called = runtime.home / "ceps_mcp.called"
                    self.assertEqual(called.exists(), role == "parent", result.stdout[-3000:])
                    if role == "parent":
                        self.assertIn("CEPS_BROKER_CALL_SUCCEEDED", serialized)
                        self.assertEqual(json.loads(called.read_text())["arguments"],
                                         {"task": "fixture", "phase": "understand"})
                    called.unlink(missing_ok=True)

            # A missing broker must stop the parent before any model request;
            # running Astra without its only delegation route is invalid.
            (runtime.home / "ceps_mcp.py").write_text("raise SystemExit(1)\n")
            command = build_exec_command(runtime, "parent", worktree, root / "broker.sock")
            for key, value in overrides.items():
                command[-1:-1] = ["-c", f"{key}={json.dumps(value)}"]
            before = len(captures)
            failed = subprocess.run(command, input="Reply OK.", env=env,
                                    capture_output=True, text=True, timeout=25)
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual(len(captures), before)


if __name__ == "__main__":
    unittest.main()
