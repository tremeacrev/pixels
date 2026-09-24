"""Small RPC diagnostics; OMP session files hold the complete transcripts."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import re
import stat
import threading


MAX_LOG_BYTES = 8 * 1024 * 1024
MAX_TOTAL_RPC_BYTES = 256 * 1024 * 1024
_STREAM_EVENTS = {"message_update", "tool_execution_update"}
_RUN_NAME = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9]+\Z")
_RPC_NAME = re.compile(r"(?:events|round-[0-9]+)\.jsonl(?:\.1)?\Z")


def compact_event(frame):
    """Remove repeated snapshots before enqueueing an RPC event.

    Only the update kind drives the progress display. Completed messages and
    request responses remain intact for the controller's correctness checks.
    Do not mutate the decoded frame: callers may still need the original.
    """
    kind = frame.get("type")
    if kind == "message_update":
        update = frame.get("assistantMessageEvent")
        return {
            "type": kind,
            "assistantMessageEvent": {
                "type": update.get("type") if isinstance(update, dict) else None,
            },
        }
    if kind == "tool_execution_update":
        return {key: frame[key] for key in ("type", "toolCallId", "toolName") if key in frame}
    if kind == "subagent_event":
        payload = frame.get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("event"), dict):
            return {**frame, "payload": {**payload, "event": compact_event(payload["event"])}}
    return frame


def _stream_event(frame):
    while frame.get("type") == "subagent_event":
        payload = frame.get("payload")
        if not isinstance(payload, dict) or not isinstance(payload.get("event"), dict):
            return False
        frame = payload["event"]
    return frame.get("type") in _STREAM_EVENTS


def _minimum_json_size(value, limit):
    """Stop before encoding giant scalar strings into another giant allocation."""
    if isinstance(value, str):
        return len(value) + 2
    if isinstance(value, dict):
        size = 2
        for key, child in value.items():
            size += len(key) + 4
            if size > limit:
                return size
            size += _minimum_json_size(child, limit - size)
            if size > limit:
                return size
        return size
    if isinstance(value, list):
        size = 2
        for child in value:
            size += 1 + _minimum_json_size(child, limit - size)
            if size > limit:
                return size
        return size
    return 1


def _encode_bounded(event, limit):
    if _minimum_json_size(event, limit) > limit:
        return None
    # iterencode avoids materializing a second copy of an arbitrarily large
    # object. The preflight above also bounds individual encoder chunks.
    output = io.BytesIO()
    encoder = json.JSONEncoder(ensure_ascii=True, separators=(",", ":"))
    for chunk in encoder.iterencode(event):
        encoded = chunk.encode("ascii")
        if output.tell() + len(encoded) + 1 > limit:
            return None
        output.write(encoded)
    output.write(b"\n")
    return output.getvalue()


def _brief(value, text_limit, depth=0):
    if isinstance(value, str):
        return value if len(value) <= text_limit else value[:text_limit] + "... [truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= 3:
        return "[truncated]"
    if isinstance(value, dict):
        result = {}
        for index, (key, child) in enumerate(value.items()):
            if index == 24:
                result["log_truncated"] = True
                break
            result[key[:text_limit]] = _brief(child, text_limit, depth + 1)
        return result
    if isinstance(value, list):
        return [_brief(child, text_limit, depth + 1) for child in value[-4:]]
    return "[unsupported value]"


_SUMMARY_FIELDS = (
    "type", "id", "success", "error", "errors", "errorMessage", "role",
    "stopReason", "usage", "isTerminal", "toolName", "toolCallId", "isError",
    "command", "exitCode", "reason", "provider", "model",
)


def _summary(event, text_limit, depth=0):
    result = {key: _brief(event[key], text_limit) for key in _SUMMARY_FIELDS if key in event}
    result["log_truncated"] = True
    if depth >= 4:
        return result
    for key in ("message", "data", "event"):
        if isinstance(event.get(key), dict):
            result[key] = _summary(event[key], text_limit, depth + 1)
    if isinstance(event.get("messages"), list):
        result["messages"] = [
            _summary(message, text_limit, depth + 1)
            for message in event["messages"][-4:] if isinstance(message, dict)
        ]
    payload = event.get("payload")
    if isinstance(payload, dict):
        result["payload"] = _summary(payload, text_limit, depth + 1)
        for key in ("agentId", "taskId", "id"):
            if key in payload:
                result["payload"][key] = _brief(payload[key], text_limit)
    return result


def _minimal_summary(event, text_limit):
    """Prefer the terminal error over ancillary metadata at very small limits."""
    def scalar(value):
        if isinstance(value, str):
            return _brief(value, text_limit)
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and abs(value) < 10 ** 20:
            return value
        return "[truncated]"

    result = {"log_truncated": True}
    for key in ("type", "id", "success", "error", "errorMessage"):
        if key in event:
            result[key] = scalar(event[key])
    leaf = event
    for _ in range(8):
        payload = leaf.get("payload")
        if not isinstance(payload, dict) or not isinstance(payload.get("event"), dict):
            break
        leaf = payload["event"]
    message = leaf.get("message")
    if not isinstance(message, dict):
        messages = leaf.get("messages")
        message = messages[-1] if isinstance(messages, list) and messages else leaf
    if isinstance(message, dict):
        result["message"] = {
            key: scalar(message[key])
            for key in ("role", "stopReason", "errorMessage") if key in message
        }
        usage = message.get("usage")
        if isinstance(usage, dict):
            # Standard OMP token counters remain useful even when richer cost
            # details cannot fit in a tiny caller-selected record limit.
            result["message"]["usage"] = {
                key: value for key in ("input", "output", "cacheRead", "cacheWrite", "totalTokens")
                if isinstance(value := usage.get(key), (int, float))
                and not isinstance(value, bool) and abs(value) < 10 ** 20
            }
    return result


class BoundedJsonlLog:
    """Append valid JSONL with one backup and a hard per-file size limit."""

    def __init__(self, path, max_bytes=MAX_LOG_BYTES):
        if max_bytes < 1024:
            raise ValueError("RPC log size must be at least 1024 bytes")
        self.path = Path(path)
        self.backup = self.path.with_name(self.path.name + ".1")
        self.max_bytes = max_bytes
        self.lock = threading.RLock()
        self.stream = self._open()
        self.size = os.fstat(self.stream.fileno()).st_size
        if self.size > self.max_bytes:
            # A previous, unbounded version may have created this path. Do not
            # retain its oversized backup after adopting bounded logging.
            self.stream.close()
            self.path.unlink()
            self.stream = self._open()
            self.size = 0
        if self.backup.exists():
            info = self.backup.lstat()
            if not stat.S_ISREG(info.st_mode):
                self.stream.close()
                raise OSError(f"RPC log backup is not a regular file: {self.backup}")
            if info.st_size > self.max_bytes:
                self.backup.unlink()
            else:
                self.backup.chmod(0o600)

    def _open(self):
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(f"RPC log is not a regular file: {self.path}")
            os.fchmod(fd, 0o600)
            return os.fdopen(fd, "ab")
        except BaseException:
            os.close(fd)
            raise

    def write(self, event):
        with self.lock:
            return self._write(event)

    def _write(self, event):
        if _stream_event(event):
            return False
        encoded = _encode_bounded(event, self.max_bytes)
        if encoded is None:
            # Usually the oversized part is message content or tool output;
            # keep accounting and error metadata even when that content is huge.
            text_limit = min(2048, self.max_bytes // 16)
            encoded = _encode_bounded(_summary(event, text_limit), self.max_bytes)
            # Remove ancillary metadata before shortening the primary error.
            while encoded is None:
                encoded = _encode_bounded(_minimal_summary(event, text_limit), self.max_bytes)
                text_limit //= 2
        if self.size + len(encoded) > self.max_bytes:
            self.stream.close()
            self.path.replace(self.backup)
            self.stream = self._open()
            self.size = 0
        self.stream.write(encoded)
        self.stream.flush()
        self.size += len(encoded)
        return True

    def flush(self):
        with self.lock:
            self.stream.flush()

    def close(self):
        with self.lock:
            self.stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _pid_alive(pid):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


def _run_alive(run):
    """Protect a live supervisor, including gaps between worker processes."""
    if _pid_alive(int(run.name.rsplit("-", 1)[1])):
        return True
    budget = run / "budget.json"
    try:
        if budget.is_symlink() or budget.stat().st_size > 64 * 1024:
            return True
        value = json.loads(budget.read_text())
        return _pid_alive(value.get("pid"))
    except PermissionError:
        return True
    except (OSError, ValueError, AttributeError):
        return False


def prune_rpc_logs(state_dir, active_run, max_bytes=MAX_TOTAL_RPC_BYTES,
                   reserve_bytes=2 * MAX_LOG_BYTES):
    """Remove oldest inactive RPC files to bound diagnostic history.

    Session transcripts, budget records, and unknown files are never candidates.
    Reserve a complete active run's allowance before starting its writers. Other
    live runs count toward retention but cannot be deleted. Concurrent live runs
    can therefore prevent reaching the requested limit.
    """
    if max_bytes < 0 or reserve_bytes < 0:
        raise ValueError("RPC retention allowance cannot be negative")
    state_dir = Path(state_dir)
    active_run = Path(active_run).absolute()
    candidates = []
    active_allowance = min(max_bytes, reserve_bytes)
    total = active_allowance
    active_size = 0
    try:
        runs = sorted(state_dir.iterdir(), key=lambda path: path.name)
    except FileNotFoundError:
        return 0
    for run in runs:
        try:
            if not _RUN_NAME.fullmatch(run.name) or run.is_symlink() or not run.is_dir():
                continue
            is_active = run.absolute() == active_run
            protected = is_active or _run_alive(run)
            paths = sorted(run.iterdir(), key=lambda path: path.name)
        except FileNotFoundError:
            continue
        for path in paths:
            if not _RPC_NAME.fullmatch(path.name):
                continue
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            if is_active:
                active_size += info.st_size
            else:
                total += info.st_size
            if not protected:
                candidates.append((path, info.st_size))
    total += max(0, active_size - active_allowance)
    removed = 0
    for path, size in candidates:
        if total <= max_bytes:
            break
        try:
            path.unlink()
        except FileNotFoundError:
            total -= size
            continue
        total -= size
        removed += size
    return removed
