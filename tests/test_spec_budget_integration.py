"""Exercise the real OMP extension with a local, unpaid Chat Completions server."""
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


ROOT = Path(__file__).resolve().parents[1]
OMP = shutil.which("omp")


@unittest.skipUnless(OMP, "Oh My Pi is not installed")
class BudgetIntegration(unittest.TestCase):
    def run_worker(self, spent, failure=None, with_task=False):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                if failure == "http":
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error":{"message":"temporary server failure"}}')
                    return
                if failure == "disconnect":
                    self.close_connection = True
                    self.connection.close()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                chunks = [
                    {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "Review complete."}, "finish_reason": None}]},
                    {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}},
                ]
                if with_task:
                    body = received[-1]
                    tool_names = [tool.get("function", {}).get("name") for tool in body.get("tools", [])]
                    if "yield" in tool_names:
                        tool_name, arguments = "yield", {"data": "Child review complete."}
                    elif len(received) == 1:
                        tool_name, arguments = "task", {"task": "Review the specification and report completion.", "agent": "task"}
                    else:
                        tool_name = None
                    if tool_name:
                        chunks[0]["choices"][0]["delta"] = {"role": "assistant", "tool_calls": [{
                            "index": 0, "id": f"call_{len(received)}", "type": "function",
                            "function": {"name": tool_name, "arguments": json.dumps(arguments)},
                        }]}
                        chunks[1]["choices"][0]["finish_reason"] = "tool_calls"
                for chunk in chunks:
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix="spec-omp-test-") as temporary:
                directory = Path(temporary)
                state_path = directory / "budget.json"
                state_path.write_text(json.dumps({
                    "version": 1, "limit": 2, "reserve": 1, "spent": spent,
                    "requests": 0, "settledRequests": 0, "reserved": 0,
                    "stopped": False, "stopKind": "", "reason": "", "estimatedSpend": False,
                }))
                provider = directory / "provider.ts"
                provider.write_text("export default pi => pi.registerProvider('deepseek', " + json.dumps({
                    "baseUrl": f"http://127.0.0.1:{server.server_port}/v1",
                    "apiKey": "unpaid-local-test", "api": "openai-completions",
                    "models": [{"id": "spec-budget-test", "name": "Local budget test", "reasoning": False,
                                "input": ["text"], "cost": {"input": 0.3, "output": 1.2, "cacheRead": 0.006, "cacheWrite": 0},
                                "contextWindow": 1_000_000, "maxTokens": 384_000}],
                }) + ");\n")
                config = directory / "config.json"
                config.write_text(json.dumps({
                    "advisor": {"enabled": False}, "memory": {"backend": "off"},
                    "compaction": {"enabled": False, "midTurnEnabled": False, "asyncEnabled": False, "idleEnabled": False},
                    "retry": {"enabled": False, "modelFallback": False},
                    "modelRoles": {role: "deepseek/spec-budget-test" for role in ["default", "smol", "slow", "tiny", "plan"]},
                    "async": {"enabled": False}, "task": {"maxConcurrency": 1, "isolation": {"enabled": False}},
                }))
                environment = dict(os.environ, PI_CODING_AGENT_DIR=str(directory / "agent"),
                                   SPEC_BUDGET_STATE=str(state_path), SPEC_BUDGET_USD="2.00")
                command = [OMP, "--mode", "rpc", "--no-session", "--no-extensions", "--no-title",
                           "--no-skills", "--no-rules", "--no-lsp", "--config", str(config),
                           "--model", "deepseek/spec-budget-test", "--thinking", "off",
                           "--extension", str(provider), "--extension", str(ROOT / "tools/spec-budget.ts")]
                command.extend(["--tools", "task"] if with_task else ["--no-tools"])
                process = subprocess.Popen(command, cwd=directory, env=environment, stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                frames = queue.Queue()
                stderr = []

                def read_frames():
                    for line in process.stdout:
                        try:
                            frames.put(json.loads(line))
                        except json.JSONDecodeError:
                            pass

                readers = [threading.Thread(target=read_frames, daemon=True),
                           threading.Thread(target=lambda: stderr.extend(process.stderr.readlines()), daemon=True)]
                for reader in readers:
                    reader.start()

                def send(frame):
                    process.stdin.write(json.dumps(frame) + "\n")
                    process.stdin.flush()

                def wait_for(predicate):
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline:
                        if process.poll() is not None and frames.empty():
                            raise AssertionError("OMP exited: " + "".join(stderr)[-2000:])
                        try:
                            frame = frames.get(timeout=0.1)
                        except queue.Empty:
                            continue
                        if predicate(frame):
                            return frame
                    raise AssertionError("OMP did not emit the expected frame: " + "".join(stderr)[-2000:])

                try:
                    wait_for(lambda f: f.get("type") == "ready")
                    self.assertEqual(json.loads(state_path.read_text())["pid"], process.pid)
                    send({"id": "state", "type": "get_state"})
                    response = wait_for(lambda f: f.get("id") == "state")
                    # Confirm the endpoint is local before authorizing any prompt.
                    self.assertEqual(response["data"]["model"]["baseUrl"], f"http://127.0.0.1:{server.server_port}/v1")
                    send({"id": "prompt", "type": "prompt", "message": "Reply with Review complete."})
                    wait_for(lambda f: f.get("type") == "agent_end")
                    process.stdin.close()
                    process.wait(timeout=15)
                    state = json.loads(state_path.read_text())
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
                    for reader in readers:
                        reader.join(timeout=2)
                    process.stdout.close()
                    process.stderr.close()
                return received, state
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_real_provider_usage_is_accounted(self):
        requests, state = self.run_worker(0)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["max_tokens"], 16384)
        self.assertAlmostEqual(state["spent"], 0.000042, places=9)
        self.assertEqual(state["reserved"], 0)
        self.assertEqual(state["settledRequests"], 1)
        self.assertFalse(state["stopped"])

    def test_reserve_cutoff_prevents_any_http_request(self):
        requests, state = self.run_worker(0.999999)
        self.assertEqual(requests, [])
        self.assertTrue(state["stopped"])
        self.assertEqual(state["stopKind"], "budget")
        self.assertFalse(state["retryable"])
        self.assertGreaterEqual(state["remaining"], 1)

    def test_http_error_cannot_trigger_unbudgeted_transport_retries(self):
        requests, state = self.run_worker(0, failure="http")
        self.assertEqual(len(requests), 1)
        self.assertTrue(state["stopped"])
        self.assertEqual(state["stopKind"], "error")
        self.assertTrue(state["retryable"])
        self.assertTrue(state["estimatedSpend"])
        self.assertGreater(state["spent"], 0)
        self.assertEqual(state["reserved"], 0)

    def test_disconnect_after_receipt_cannot_repeat_the_request(self):
        requests, state = self.run_worker(0, failure="disconnect")
        self.assertEqual(len(requests), 1)
        self.assertTrue(state["stopped"])
        self.assertEqual(state["stopKind"], "error")
        self.assertTrue(state["retryable"])
        self.assertTrue(state["estimatedSpend"])

    def test_task_inherits_budget_and_optional_labels_are_not_billed(self):
        requests, state = self.run_worker(0, with_task=True)
        self.assertGreaterEqual(len(requests), 3)
        self.assertAlmostEqual(state["spent"], 0.000042 * len(requests), places=9)
        self.assertEqual(state["settledRequests"], len(requests))
        self.assertEqual(state["reserved"], 0)
        self.assertGreaterEqual(state["blockedAuxiliaryRequests"], 1)
        self.assertFalse(state["stopped"])


if __name__ == "__main__":
    unittest.main()
