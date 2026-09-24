"""Read Codex account quotas without running a model, and enforce a remaining floor.

Quota percentages are account-wide observations, not token-derived cost estimates.
The guard consumes complete account/rateLimits/read responses; sparse notifications
must never be passed to it as replacement snapshots.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import threading
import time
from typing import Callable, Iterable


class QuotaError(RuntimeError):
    """Quota or account identity cannot safely be established."""


class RetryableQuotaError(QuotaError):
    """A transient account read failed; retry only after stopping model work."""


class QuotaReached(QuotaError):
    """The requested reserve or a backend usage restriction has been reached."""


def _number(value, name, *, minimum=0, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise QuotaError(f"{name} must be a finite number")
    try:
        value = float(value)
    except OverflowError as exc:
        raise QuotaError(f"{name} is out of range") from exc
    if not math.isfinite(value) or value < minimum or (maximum is not None and value > maximum):
        raise QuotaError(f"{name} is out of range")
    return value


def _optional_string(value, name):
    if value is not None and (not isinstance(value, str) or not value or len(value) > 1024):
        raise QuotaError(f"Invalid {name}")
    return value


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _reject_json_constant(_):
    raise ValueError("Non-finite JSON number")


def _rpc_error(method, error):
    """Classify errors without putting server text or account data in diagnostics."""
    if (not isinstance(error, dict) or type(error.get("code")) is not int
            or not -(2 ** 31) <= error["code"] < 2 ** 31
            or not isinstance(error.get("message"), str)):
        return QuotaError(f"Codex {method} returned a malformed RPC error")
    code = error["code"]
    # RPC frames bound the inspected text; diagnostics use fixed categories instead
    # of attempting to redact arbitrary credentials embedded in server messages.
    message = error["message"].lower()
    data = error.get("data")
    statuses = {code} if 100 <= code <= 599 else set()
    if isinstance(data, dict):
        for key in ("httpStatusCode", "http_status_code", "statusCode", "status_code", "status"):
            status = data.get(key)
            if type(status) is int and 100 <= status <= 599:
                statuses.add(status)
    # Some app-server errors carry upstream HTTP status only in their message.
    for match in re.finditer(
        r"\b(?:http(?:\s+status)?|status(?:\s+code)?)\s*[:=(]?\s*([1-5][0-9]{2})\b"
        r"|\b(?:client|server)\s+error\s*\(([1-5][0-9]{2})\b", message,
    ):
        statuses.add(int(match.group(1) or match.group(2)))
    authentication = any(word in message for word in (
        "unauthorized", "forbidden", "authentication", "not authenticated", "permission denied",
        "login", "log in", "logged in", "sign in", "signed in", "expired token", "token expired",
        "invalid token", "refresh token", "api key", "account changed", "workspace changed",
    ))
    protocol = any(word in message for word in (
        "invalid request", "method not found", "invalid params", "invalid parameters",
        "invalid argument", "parse error", "malformed", "unsupported", "not supported",
        "bad request", "configuration",
    ))
    retryable = False
    if authentication or any(status in (401, 403) for status in statuses):
        category = "authentication or permission failure"
    elif code in (-32700, -32600, -32601, -32602) or protocol:
        category = "request or protocol failure"
    elif any(400 <= status < 500 and status not in (408, 429) for status in statuses):
        category = "request rejected"
    elif any(status in (408, 429) or 500 <= status <= 599 for status in statuses):
        category, retryable = "temporary service failure", True
    elif any(word in message for word in ("timed out", "timeout")):
        category, retryable = "request timeout", True
    elif any(word in message for word in (
        "connection reset", "connection refused", "connection closed", "broken pipe",
        "temporarily unavailable", "temporary failure", "service unavailable", "overloaded",
    )):
        category, retryable = "temporary connection or service failure", True
    elif code in (-32603, -32000):
        category, retryable = "internal server failure", True
    else:
        category = "unclassified RPC failure"
    details = [f"code {code}"]
    if statuses:
        details.append("HTTP " + ", ".join(map(str, sorted(statuses)[:4])))
    details.append(category)
    error_class = RetryableQuotaError if retryable else QuotaError
    return error_class(f"Codex {method} request failed ({'; '.join(details)})")


def account_fingerprint(account_response: dict, limits: dict | None = None) -> str:
    """Hash account/read identity plus the quota account ID; never expose either.

    Including routing prevents a workspace switch on the same email from being
    mistaken for a new quota epoch on the original account.
    """
    account = account_response.get("account")
    if not isinstance(account, dict) or account.get("type") != "chatgpt":
        raise QuotaError("ceps requires ChatGPT account authentication")
    if account_response.get("requiresOpenaiAuth") is not True:
        raise QuotaError("The configured provider does not use ChatGPT account authentication")
    email = _optional_string(account.get("email"), "account email")
    routing = account_response.get("workspaceRouting")
    if routing is not None and not isinstance(routing, dict):
        raise QuotaError("Invalid account workspace routing")
    workspace_id = _optional_string((routing or {}).get("chatgptAccountId"), "workspace identity")
    quota_id = _optional_string((limits or {}).get("accountId"), "quota account identity")
    if limits is not None and not any((email, workspace_id, quota_id)):
        raise QuotaError("The quota response cannot be bound to an account identity")
    return _digest({"type": "chatgpt", "email": email,
                    "workspace_account_id": workspace_id, "quota_account_id": quota_id})


class AccountClient:
    """A private, read-only app-server client with bounded I/O and cleanup.

    It never starts/resumes a thread or calls a model. No authentication material
    or raw server diagnostic output is written to ceps logs. Each public
    operation has one overall timeout, including initialization when needed.
    """

    MAX_FRAME_BYTES = 2 * 1024 * 1024

    def __init__(self, codex: str, cwd: Path, env: dict | None = None, *, timeout: float = 15):
        self.codex = str(codex)
        self.cwd = Path(cwd)
        self.env = None if env is None else dict(env)
        self.timeout = _number(timeout, "Account client timeout", minimum=0.001)
        self._lock = threading.RLock()
        self._process = None
        self._buffer = bytearray()
        self._next_id = 0
        self._account_identity = None
        self._quota_identity = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()

    def _wait_fd(self, file, event, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RetryableQuotaError("Codex account request timed out")
        with selectors.DefaultSelector() as selector:
            selector.register(file, event)
            if not selector.select(remaining):
                raise RetryableQuotaError("Codex account request timed out")

    def _send(self, message, deadline):
        data = json.dumps(message, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        offset = 0
        while offset < len(data):
            self._wait_fd(self._process.stdin, selectors.EVENT_WRITE, deadline)
            try:
                written = os.write(self._process.stdin.fileno(), data[offset:])
            except BlockingIOError:
                continue
            except OSError as exc:
                raise RetryableQuotaError("Codex account server closed its input") from exc
            if not written:
                raise RetryableQuotaError("Codex account server closed its input")
            offset += written

    def _receive(self, deadline):
        while True:
            # Check even when buffered notifications are arriving continuously.
            if time.monotonic() >= deadline:
                raise RetryableQuotaError("Codex account request timed out")
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                if newline > self.MAX_FRAME_BYTES:
                    raise QuotaError("Codex account response exceeds the size limit")
                line = bytes(self._buffer[:newline])
                del self._buffer[:newline + 1]
                try:
                    message = json.loads(line, parse_constant=_reject_json_constant)
                except (UnicodeDecodeError, ValueError, RecursionError) as exc:
                    raise QuotaError("Codex account server returned malformed JSON") from exc
                if not isinstance(message, dict):
                    raise QuotaError("Codex account server returned an invalid RPC message")
                return message
            if len(self._buffer) > self.MAX_FRAME_BYTES:
                raise QuotaError("Codex account response exceeds the size limit")
            self._wait_fd(self._process.stdout, selectors.EVENT_READ, deadline)
            try:
                chunk = os.read(self._process.stdout.fileno(), 65536)
            except BlockingIOError:
                continue
            except OSError as exc:
                raise RetryableQuotaError("Codex account server closed its output") from exc
            if not chunk:
                raise RetryableQuotaError("Codex account server exited before replying")
            self._buffer.extend(chunk)

    def _request(self, method, params, deadline):
        self._next_id += 1
        request_id = self._next_id
        self._send({"id": request_id, "method": method, "params": params}, deadline)
        while True:
            response = self._receive(deadline)
            if "method" in response:
                if "id" in response:
                    # No login/refresh/approval interaction is delegated to ceps.
                    try:
                        self._send({"id": response["id"], "error": {
                            "code": -32601, "message": "Read-only ceps account client",
                        }}, deadline)
                    except RetryableQuotaError:
                        pass  # A closed pipe cannot make authentication retryable.
                    raise QuotaError("Codex account server requested interactive authentication")
                # Quota notifications are sparse and are deliberately ignored.
                continue
            if type(response.get("id")) is not int or response["id"] != request_id:
                raise QuotaError("Codex account server returned an unexpected response ID")
            if "error" in response:
                if "result" in response:
                    raise QuotaError(f"Codex {method} returned an invalid RPC response")
                raise _rpc_error(method, response["error"])
            result = response.get("result")
            if not isinstance(result, dict):
                raise QuotaError(f"Codex {method} response is malformed")
            return result

    def _read_account(self, deadline):
        account = self._request("account/read", {"refreshToken": False}, deadline)
        fingerprint = account_fingerprint(account)
        if self._account_identity is not None and fingerprint != self._account_identity:
            raise QuotaError("The ChatGPT account or workspace changed during ceps")
        self._account_identity = fingerprint
        return account

    def _start(self, deadline):
        if self._process is not None:
            if self._process.poll() is not None:
                raise RetryableQuotaError("Codex account server is no longer running")
            return
        try:
            self._process = subprocess.Popen(
                [self.codex, "app-server", "--stdio"], cwd=self.cwd, env=self.env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                start_new_session=True, bufsize=0,
            )
            os.set_blocking(self._process.stdin.fileno(), False)
            os.set_blocking(self._process.stdout.fileno(), False)
            self._request("initialize", {
                "clientInfo": {"name": "ceps", "title": "ceps quota monitor", "version": "1"},
                "capabilities": {"experimentalApi": False, "requestAttestation": False},
            }, deadline)
            self._send({"method": "initialized"}, deadline)
            self._read_account(deadline)
        except (OSError, QuotaError) as exc:
            self._close()
            if isinstance(exc, QuotaError):
                raise
            raise QuotaError("Cannot start the Codex account server") from exc

    def start(self):
        with self._lock:
            self._start(time.monotonic() + self.timeout)
        return self

    def read_limits(self) -> dict:
        with self._lock:
            deadline = time.monotonic() + self.timeout
            self._start(deadline)
            account = self._read_account(deadline)
            limits = self._request("account/rateLimits/read", {
                "excludeResetCreditDetails": True, "supportsLunaReserve": False,
            }, deadline)
            # Catch an account switch that races the quota read.
            self._read_account(deadline)
            fingerprint = account_fingerprint(account, limits)
            if self._quota_identity is not None and fingerprint != self._quota_identity:
                raise QuotaError("The quota account identity changed during ceps")
            self._quota_identity = fingerprint
            result = dict(limits)
            result.pop("accountId", None)
            result.pop("rateLimitUpsell", None)
            result["_ceps_account_identity"] = fingerprint
            return result

    def model_catalog(self) -> dict:
        with self._lock:
            deadline = time.monotonic() + self.timeout
            self._start(deadline)
            models, cursor, seen = [], None, set()
            for _ in range(100):
                params = {"includeHidden": True, "limit": 100}
                if cursor is not None:
                    params["cursor"] = cursor
                result = self._request("model/list", params, deadline)
                if not isinstance(result.get("data"), list) or not all(isinstance(x, dict) for x in result["data"]):
                    raise QuotaError("Codex model catalog is malformed")
                models.extend(result["data"])
                cursor = result.get("nextCursor")
                if cursor is None:
                    return {"data": models, "nextCursor": None}
                if not isinstance(cursor, str) or not cursor or cursor in seen:
                    raise QuotaError("Codex model catalog pagination is invalid")
                seen.add(cursor)
            raise QuotaError("Codex model catalog has too many pages")

    def _close(self):
        process, self._process = self._process, None
        self._buffer.clear()
        if process is None:
            return
        # Only signal the session this client created, never a shared daemon.
        for sig, timeout in ((signal.SIGTERM, 2), (signal.SIGKILL, 1)):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                continue
            if sig == signal.SIGKILL:
                break
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()

    def close(self):
        with self._lock:
            self._close()


class QuotaGuard:
    """Thread-safe floor checks over fresh complete snapshots.

    A detected reserve/backend stop is latched for this run: a later reset must
    not silently restart an already stopped budget. Reset timestamps alone never
    create fresh allowance; only new complete responses can change percentages.
    """

    def __init__(self, floor: float, buffer: float = 2, max_age: float = 30, *,
                 models: Iterable[str] | None = None, clock: Callable[[], float] = time.monotonic):
        self.floor = _number(floor, "Remaining percentage", maximum=100)
        self.buffer = _number(buffer, "Quota buffer", maximum=100)
        self.max_age = _number(max_age, "Quota maximum age", minimum=0.001)
        self.stop_at = min(100.0, self.floor + self.buffer)
        if isinstance(models, str):
            raise QuotaError("Quota models must be a collection of model slugs")
        self.models = None if models is None else frozenset(models)
        if self.models is not None and (not self.models or any(not isinstance(x, str) or not x for x in self.models)):
            raise QuotaError("Quota models must be a nonempty collection of model slugs")
        self._clock = clock
        self._lock = threading.RLock()
        self._sample = None
        self._identity = None
        self._invalid = None
        self._identity_error = None
        self._stop_reason = None
        self._reset_count = 0

    def _bucket(self, bucket, bucket_id):
        if not isinstance(bucket, dict):
            raise QuotaError("Malformed quota bucket")
        actual_id = _optional_string(bucket.get("limitId"), "quota bucket ID")
        if actual_id is not None and actual_id != bucket_id:
            raise QuotaError("Quota bucket ID disagrees with its map key")
        model = _optional_string(bucket.get("normalModelSlug"), "quota model")
        name = _optional_string(bucket.get("limitName"), "quota bucket name")
        if self.models is not None and model is not None and model not in self.models:
            return [], [], True
        reached = _optional_string(bucket.get("rateLimitReachedType"), "quota reached state")
        spend = bucket.get("spendControlReached")
        if spend is not None and not isinstance(spend, bool):
            raise QuotaError("Malformed quota spend-control state")
        blocked = []
        if reached is not None:
            blocked.append(f"Backend usage restriction for {bucket_id}: {reached}")
        if spend is True:
            blocked.append(f"Backend spend control reached for {bucket_id}")
        windows = []
        for kind in ("primary", "secondary"):
            if kind not in bucket:
                raise QuotaError("Incomplete quota snapshot: missing quota window field")
            window = bucket[kind]
            if window is None:
                continue
            if not isinstance(window, dict):
                raise QuotaError("Malformed quota window")
            used = _number(window.get("usedPercent"), "Quota used percentage")
            duration = window.get("windowDurationMins")
            reset = window.get("resetsAt")
            for value, field, minimum in ((duration, "quota window duration", 1), (reset, "quota reset timestamp", 0)):
                if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < minimum):
                    raise QuotaError(f"Invalid {field}")
            windows.append({"bucket_id": bucket_id, "window": kind, "limit_name": name,
                            "normal_model": model, "used_percent": used,
                            "remaining": max(0.0, 100.0 - used), "duration_mins": duration,
                            "resets_at": reset})
        return windows, blocked, False

    def _parse(self, response):
        if not isinstance(response, dict):
            raise QuotaError("Malformed quota response")
        allowed = response.get("ordinaryUsageAllowed")
        if not isinstance(allowed, bool):
            raise QuotaError("Ordinary account usage permission is unavailable")
        identity = response.get("_ceps_account_identity")
        if identity is None:
            account_id = _optional_string(response.get("accountId"), "quota account identity")
            if account_id is not None:
                identity = _digest({"quota_account_id": account_id})
        if not isinstance(identity, str) or len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
            raise QuotaError("Quota account identity is unavailable")
        if self._identity is not None and identity != self._identity:
            self._identity_error = "The quota account identity changed during ceps"
            raise QuotaError(self._identity_error)
        mapping = response.get("rateLimitsByLimitId")
        if mapping is not None and (not isinstance(mapping, dict) or len(mapping) > 1024):
            raise QuotaError("Malformed quota bucket map")
        buckets = dict(mapping or {})
        legacy = response.get("rateLimits")
        if not isinstance(legacy, dict):
            raise QuotaError("Complete quota response is missing its ordinary quota bucket")
        legacy_id = _optional_string(legacy.get("limitId"), "quota bucket ID") or "__default__"
        if legacy_id not in buckets:
            buckets[legacy_id] = legacy
        else:
            # The two views of the same meter must agree about actual windows.
            mapped = buckets[legacy_id]
            if not isinstance(mapped, dict) or any(legacy.get(k) != mapped.get(k) for k in (
                    "primary", "secondary", "normalModelSlug", "spendControlReached", "rateLimitReachedType")):
                raise QuotaError("The single- and multi-bucket quota views disagree")
        windows, blocked, excluded = [], [], []
        if not allowed:
            blocked.append("Backend has blocked ordinary account usage")
        for bucket_id, bucket in buckets.items():
            if _optional_string(bucket_id, "quota bucket map key") is None:
                raise QuotaError("Invalid quota bucket map key")
            items, reasons, ignored = self._bucket(bucket, bucket_id)
            windows.extend(items)
            blocked.extend(reasons)
            if ignored:
                excluded.append(bucket_id)
        if not windows and not blocked:
            raise QuotaError("No applicable account quota windows are available")
        windows.sort(key=lambda x: (x["bucket_id"], x["window"]))
        previous = {} if self._sample is None else {
            (x["bucket_id"], x["window"]): x for x in self._sample["windows"]
        }
        changed = []
        for window in windows:
            prior = previous.get((window["bucket_id"], window["window"]))
            if prior and prior["resets_at"] is not None and window["resets_at"] is not None and prior["resets_at"] != window["resets_at"]:
                changed.append({"bucket_id": window["bucket_id"], "window": window["window"],
                                "previous_resets_at": prior["resets_at"], "resets_at": window["resets_at"]})
        minimum = min((x["remaining"] for x in windows), default=None)
        return {"account_identity": identity, "sampled_at": self._clock(), "observed_at": time.time(),
                "windows": windows, "excluded_buckets": sorted(excluded), "min_remaining": minimum,
                "ordinary_usage_allowed": allowed, "backend_blocks": blocked,
                "floor": self.floor, "buffer": self.buffer, "stop_at": self.stop_at,
                "reset_windows": changed, "reset_count": self._reset_count + len(changed)}

    def update(self, response: dict) -> dict:
        with self._lock:
            try:
                if self._identity_error is not None:
                    raise QuotaError(self._identity_error)
                sample = self._parse(response)
            except QuotaError as exc:
                self._invalid = str(exc)
                raise
            self._sample = sample
            self._identity = sample["account_identity"]
            self._reset_count = sample["reset_count"]
            self._invalid = None
            if self._stop_reason is None:
                if sample["backend_blocks"]:
                    self._stop_reason = sample["backend_blocks"][0]
                elif sample["min_remaining"] <= self.stop_at:
                    self._stop_reason = f"Account quota reserve reached ({sample['min_remaining']:g}% remaining; stop at {self.stop_at:g}%)"
            return self.snapshot()

    def snapshot(self) -> dict | None:
        with self._lock:
            if self._sample is None:
                return None
            result = copy.deepcopy(self._sample)
            age = max(0.0, self._clock() - result["sampled_at"])
            result.update(age_seconds=age, stale=age >= self.max_age,
                          valid=self._invalid is None and age < self.max_age,
                          error=self._invalid, stop_reason=self._stop_reason)
            return result

    def check(self) -> dict:
        with self._lock:
            if self._identity_error is not None:
                raise QuotaError(self._identity_error)
            if self._invalid is not None:
                raise QuotaError(self._invalid)
            if self._stop_reason is not None:
                raise QuotaReached(self._stop_reason)
            sample = self.snapshot()
            if sample is None:
                raise QuotaError("No account quota snapshot is available")
            if sample["stale"]:
                raise QuotaError("Account quota telemetry is stale")
            return sample
