#!/usr/bin/env python3
"""Expose supervised ceps delegation as a small stdio MCP server.

The supervisor owns worker creation, model selection, and usage accounting. This
process can only forward a bounded task and phase over its supplied Unix socket.
"""

import argparse
import json
import signal
import socket
import sys


MAX_TEXT_BYTES = 65_536
MAX_LINE_BYTES = 1_048_576
SOCKET_TIMEOUT = 3700
PHASES = ("understand", "plan", "review")
TOOL = {
    "name": "delegate",
    "description": (
        "Launch a concurrent fleet of supervised, read-only Luna workers to understand "
        "the specification, plan a change, or review it. Every task fans out to distinct "
        "specialist and independent adversarial assignments. Fleet size scales with the "
        "specification's documents, words, sections, and links. Supply the task and phase; "
        "ceps selects the model and maximum reasoning effort. Waits for the entire fleet "
        "and returns each worker's findings; synthesize them before continuing."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "minLength": 1, "maxLength": MAX_TEXT_BYTES},
            "phase": {"type": "string", "enum": list(PHASES)},
        },
        "required": ["task", "phase"],
        "additionalProperties": False,
    },
    # The supervisor enforces the worker's read-only sandbox. Its provider call
    # still reaches an external service; these hints do not authorize the tool.
    "annotations": {"readOnlyHint": True, "openWorldHint": True},
}


class ProtocolError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON field")
        value[key] = item
    return value


def _constant(value):
    raise ValueError("Non-finite JSON number")


def decode(line):
    return json.loads(line.decode("utf-8"), object_pairs_hook=_object,
                      parse_constant=_constant)


def encode(value):
    return (json.dumps(value, ensure_ascii=True, separators=(",", ":"),
                       allow_nan=False) + "\n").encode("utf-8")


def _text(value, maximum=MAX_TEXT_BYTES, allow_empty=False):
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        return False
    try:
        return len(value.encode("utf-8")) <= maximum
    except UnicodeEncodeError:
        return False


def _params(value, allowed):
    if not isinstance(value, dict) or set(value) - set(allowed) - {"_meta"}:
        raise ProtocolError(-32602, "Invalid parameters")
    if "_meta" in value and not isinstance(value["_meta"], dict):
        raise ProtocolError(-32602, "Invalid metadata")
    return value


def _error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}}


class Bridge:
    def __init__(self, socket_path, timeout=SOCKET_TIMEOUT, stderr=None):
        self.socket_path = str(socket_path)
        self.timeout = timeout
        self.stderr = stderr if stderr is not None else sys.stderr

    def delegate(self, arguments):
        if (not isinstance(arguments, dict) or set(arguments) != {"task", "phase"}
                or not _text(arguments.get("task"))
                or arguments.get("phase") not in PHASES):
            raise ProtocolError(-32602, "Expected only a bounded task and a valid phase")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self.timeout)
                connection.connect(self.socket_path)
                connection.sendall(encode(arguments))
                with connection.makefile("rb") as stream:
                    line = stream.readline(MAX_LINE_BYTES + 1)
            if not line.endswith(b"\n") or len(line) > MAX_LINE_BYTES:
                raise ValueError("Supervisor response missing, incomplete, or too large")
            reply = decode(line)
            if (not isinstance(reply, dict)
                    or set(reply) != {"text", "worker_id", "model", "effort", "ok"}
                    or type(reply["ok"]) is not bool
                    or not _text(reply["text"], allow_empty=True)
                    or any(not _text(reply[key], maximum=256, allow_empty=not reply["ok"])
                           for key in ("worker_id", "model", "effort"))):
                raise ValueError("Invalid supervisor response")
        except (OSError, ValueError, RecursionError) as error:
            print(f"ceps MCP delegation failed: {error}", file=self.stderr, flush=True)
            return {"content": [{"type": "text", "text": "Supervised delegation failed; "
                                 "the ceps supervisor did not return a valid fleet result."}],
                    "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(reply, ensure_ascii=True)}],
                "isError": not reply["ok"]}

    def handle(self, request):
        request_id = None
        if (not isinstance(request, dict)
                or set(request) - {"jsonrpc", "id", "method", "params"}
                or request.get("jsonrpc") != "2.0"
                or not isinstance(request.get("method"), str)
                or ("id" in request and type(request["id"]) not in (str, int))):
            return _error(None, -32600, "Invalid request")
        if "id" not in request:
            # Notifications never start workers and have no JSON-RPC response.
            return None
        request_id = request["id"]
        method = request["method"]
        try:
            params = request.get("params", {})
            if method == "initialize":
                params = _params(params, {"protocolVersion", "capabilities", "clientInfo"})
                if (not _text(params.get("protocolVersion"), maximum=128)
                        or not isinstance(params.get("capabilities", {}), dict)
                        or not isinstance(params.get("clientInfo", {}), dict)):
                    raise ProtocolError(-32602, "Invalid initialization parameters")
                result = {"protocolVersion": params["protocolVersion"],
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "ceps", "version": "1.0.0"}}
            elif method == "ping":
                _params(params, set())
                result = {}
            elif method == "tools/list":
                _params(params, set())
                result = {"tools": [TOOL]}
            elif method == "tools/call":
                params = _params(params, {"name", "arguments"})
                if params.get("name") != "delegate":
                    raise ProtocolError(-32602, "Unknown tool")
                result = self.delegate(params.get("arguments"))
            else:
                raise ProtocolError(-32601, "Method not found")
        except ProtocolError as error:
            return _error(request_id, error.code, str(error))
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def serve(self, source, destination):
        while True:
            line = source.readline(MAX_LINE_BYTES + 1)
            if not line:
                return 0
            if len(line) > MAX_LINE_BYTES or not line.endswith(b"\n"):
                destination.write(encode(_error(None, -32700, "Message incomplete or too large")))
                destination.flush()
                return 1
            try:
                request = decode(line)
            except (ValueError, UnicodeError, RecursionError):
                response = _error(None, -32700, "Parse error")
            else:
                response = self.handle(request)
            if response is not None:
                destination.write(encode(response))
                destination.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True, help="ceps supervisor Unix socket")
    args = parser.parse_args(argv)

    def stop(signum, frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        return Bridge(args.socket).serve(sys.stdin.buffer, sys.stdout.buffer)
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
