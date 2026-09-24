"""The MCP bridge exposes only supervised, bounded delegation requests."""

import io
import json
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from tools.ceps_mcp import Bridge, MAX_LINE_BYTES, MAX_TEXT_BYTES, TOOL, encode


SOURCE = Path(__file__).resolve().parents[1]


def request(method, params=None, request_id=1):
    result = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        result["params"] = params
    return result


def call(arguments=None):
    return request("tools/call", {"name": "delegate", "arguments": arguments or {
        "task": "Review the proposed change", "phase": "review"}})


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.bridge = Bridge("/unused/socket", stderr=io.StringIO())

    def test_handshake_notification_listing_and_ping(self):
        source = io.BytesIO(b"".join(encode(value) for value in (
            request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "test", "version": "1"}}),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            request("tools/list", request_id="listing"),
            request("ping", request_id=3),
        )))
        output = io.BytesIO()
        self.assertEqual(self.bridge.serve(source, output), 0)
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(replies), 3)
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(replies[1]["id"], "listing")
        self.assertEqual(replies[1]["result"]["tools"], [TOOL])
        self.assertEqual(replies[2]["result"], {})
        self.assertEqual(set(TOOL["inputSchema"]["properties"]), {"task", "phase"})
        self.assertFalse(TOOL["inputSchema"]["additionalProperties"])
        self.assertEqual(TOOL["annotations"], {"readOnlyHint": True, "openWorldHint": True})

    def test_invalid_arguments_never_connect_to_supervisor(self):
        arguments = [
            None, [], {}, {"task": "x"}, {"phase": "review"},
            {"task": "x", "phase": "write"}, {"task": 2, "phase": "review"},
            {"task": " ", "phase": "review"},
            {"task": "x", "phase": "review", "model": "wrong"},
            {"task": "x", "phase": "review", "effort": "low"},
            {"task": "x" * (MAX_TEXT_BYTES + 1), "phase": "review"},
            {"task": "é" * (MAX_TEXT_BYTES // 2 + 1), "phase": "review"},
            {"task": "\ud800", "phase": "review"},
        ]
        with mock.patch("tools.ceps_mcp.socket.socket") as connect:
            for value in arguments:
                with self.subTest(arguments=str(value)[:100]):
                    response = self.bridge.handle(request("tools/call", {
                        "name": "delegate", "arguments": value}))
                    self.assertEqual(response["error"]["code"], -32602)
            connect.assert_not_called()

    def test_invalid_requests_and_methods_are_json_rpc_errors(self):
        for value in ([], {}, {"jsonrpc": "2.0", "id": True, "method": "ping"},
                      {"jsonrpc": "2.0", "id": 1, "method": "ping", "extra": 1}):
            with self.subTest(value=value):
                self.assertEqual(self.bridge.handle(value)["error"]["code"], -32600)
        self.assertEqual(self.bridge.handle(request("other"))["error"]["code"], -32601)
        for value in (request("tools/list", {"model": "wrong"}),
                      request("tools/call", {"name": "other", "arguments": {}}),
                      request("initialize", {"protocolVersion": 3}),
                      request("ping", {"_meta": 3})):
            self.assertEqual(self.bridge.handle(value)["error"]["code"], -32602)

    def test_notifications_cannot_start_workers(self):
        notification = call()
        del notification["id"]
        with mock.patch.object(self.bridge, "delegate") as delegate:
            self.assertIsNone(self.bridge.handle(notification))
            delegate.assert_not_called()

    def test_malformed_json_is_rejected_then_stream_continues(self):
        for invalid in (b"not json\n", b"\xff\n", b'{"id":1,"id":2}\n',
                        b'{"value":NaN}\n'):
            output = io.BytesIO()
            self.assertEqual(self.bridge.serve(io.BytesIO(invalid + encode(request("ping"))), output), 0)
            replies = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(replies[0]["error"]["code"], -32700)
            self.assertEqual(replies[1]["result"], {})

    def test_deeply_nested_json_cannot_crash_the_stream(self):
        output = io.BytesIO()
        source = io.BytesIO(b"[" * 2000 + b"]" * 2000 + b"\n" + encode(request("ping")))
        self.assertEqual(self.bridge.serve(source, output), 0)
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        # Python versions differ in the JSON decoder's nesting limit.
        self.assertIn(replies[0]["error"]["code"], (-32600, -32700))
        self.assertEqual(replies[1]["result"], {})

    def test_oversized_or_unterminated_transport_stops_without_dispatch(self):
        for invalid in (b"x" * (MAX_LINE_BYTES + 1), encode(request("ping"))[:-1]):
            output = io.BytesIO()
            with mock.patch.object(self.bridge, "handle") as handle:
                self.assertEqual(self.bridge.serve(io.BytesIO(invalid), output), 1)
                handle.assert_not_called()
            self.assertEqual(json.loads(output.getvalue())["error"]["code"], -32700)


class SocketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "supervisor.sock"
        self.stderr = io.StringIO()

    def exchange(self, response):
        received = []
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(self.path))
            server.listen(1)
            server.settimeout(3)

            def respond():
                connection, _ = server.accept()
                with connection, connection.makefile("rb") as stream:
                    received.append(json.loads(stream.readline()))
                    if response:
                        connection.sendall(response)

            thread = threading.Thread(target=respond)
            thread.start()
            result = Bridge(self.path, timeout=2, stderr=self.stderr).handle(call())
            thread.join(3)
            self.assertFalse(thread.is_alive())
        return result, received

    def reply(self, **updates):
        return {"text": "No contradictions found.", "worker_id": "worker-1",
                "model": "gpt-6-luna", "effort": "max", "ok": True, **updates}

    def test_exact_request_and_success_metadata_cross_real_unix_socket(self):
        reply = self.reply()
        result, received = self.exchange(encode(reply))
        self.assertEqual(received, [{"task": "Review the proposed change", "phase": "review"}])
        self.assertFalse(result["result"]["isError"])
        self.assertEqual(json.loads(result["result"]["content"][0]["text"]), reply)
        self.assertEqual(self.stderr.getvalue(), "")

    def test_supervisor_failure_is_a_tool_error(self):
        reply = self.reply(ok=False, text="Usage floor reached")
        result, _ = self.exchange(encode(reply))
        self.assertTrue(result["result"]["isError"])
        self.assertEqual(json.loads(result["result"]["content"][0]["text"]), reply)

    def test_missing_socket_returns_tool_error_without_retry(self):
        result = Bridge(self.path, timeout=0.1, stderr=self.stderr).handle(call())
        self.assertTrue(result["result"]["isError"])
        self.assertIn("delegation failed", self.stderr.getvalue())

    def test_invalid_supervisor_responses_fail_closed(self):
        responses = (b"", b"not json\n", b"{}\n", encode(self.reply(ok=1)),
                     encode(self.reply(model="")), encode(self.reply(extra=True)),
                     encode(self.reply(text="x" * (MAX_TEXT_BYTES + 1))),
                     encode(self.reply())[:-1], b"x" * (MAX_LINE_BYTES + 1),
                     b"[" * 2000 + b"]" * 2000 + b"\n")
        for index, response in enumerate(responses):
            with self.subTest(index=index):
                result, _ = self.exchange(response)
                self.assertTrue(result["result"]["isError"])
                self.path.unlink()

    def test_socket_timeout_returns_tool_error(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(self.path))
            server.listen(1)
            result = Bridge(self.path, timeout=0.02, stderr=self.stderr).handle(call())
        self.assertTrue(result["result"]["isError"])

    def test_sigterm_exits_while_waiting_for_worker(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(self.path))
            server.listen(1)
            server.settimeout(3)
            process = subprocess.Popen(
                [sys.executable, str(SOURCE / "tools/ceps_mcp.py"), "--socket", str(self.path)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            try:
                process.stdin.write(encode(call()))
                process.stdin.flush()
                connection, _ = server.accept()
                with connection:
                    process.send_signal(signal.SIGTERM)
                    output, error = process.communicate(timeout=3)
                    self.assertEqual(process.returncode, 128 + signal.SIGTERM)
                    self.assertEqual(output, b"")
                    self.assertEqual(error, b"")
                    self.assertTrue(connection.recv(4096))
                    self.assertEqual(connection.recv(1), b"")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()


if __name__ == "__main__":
    unittest.main()
