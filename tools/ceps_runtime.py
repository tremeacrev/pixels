"""Owned Codex workers and the task-only delegation service for ceps."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import ctypes
from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import select
import socketserver
import subprocess
import threading
import time

from ceps_codex import CodexError, build_exec_command
from ceps_fleet import build_fleet

MAX_LINE = 4 * 1024 * 1024
MAX_TEXT = 65536
PHASES = {"understand", "plan", "review"}
TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                "output_tokens", "reasoning_output_tokens")


def bounded_text(value, maximum=MAX_TEXT):
    return str(value).encode("utf-8")[:maximum].decode("utf-8", "ignore")


def fleet_text(fleet_id, phase, workers, results):
    """Give every specialist a fair share of the bounded MCP response."""
    header = f"Fleet {fleet_id}: all {len(workers)} {phase} specialists completed."
    labels = [f"\n\n### {worker_id}\n" for worker_id in workers]
    available = MAX_TEXT - len(header.encode()) - sum(len(label.encode()) for label in labels)
    share = available // len(workers)
    sections = []
    for label, result in zip(labels, results):
        text = str(result)
        if len(text.encode("utf-8")) > share:
            suffix = "\n[Specialist result truncated.]"
            text = bounded_text(text, share - len(suffix.encode())) + suffix
        sections.append(label + text)
    return header + "".join(sections)


def output_chunks(stream, process, shutdown):
    """A detached descendant holding a pipe must not hold cleanup hostage."""
    fd = stream.fileno()
    os.set_blocking(fd, False)
    while not shutdown.is_set():
        ready, _, _ = select.select([fd], [], [], 0.2)
        if not ready:
            if process.poll() is not None:
                return
            continue
        try:
            chunk = os.read(fd, 65536)
        except BlockingIOError:
            continue
        if not chunk:
            return
        yield chunk


class CepsError(Exception):
    pass


class RoundError(CepsError):
    """A failed attempt can be retried after all writers stop and edits reset."""


class Stopped(CepsError):
    pass


class Control:
    def __init__(self):
        self.event = threading.Event()
        self.lock = threading.RLock()
        self.kind = ""
        self.reason = ""

    def stop(self, kind, reason):
        with self.lock:
            priority = {"": 0, "quota": 1, "signal": 2, "error": 3}
            if priority[kind] >= priority[self.kind]:
                self.kind, self.reason = kind, str(reason)
            self.event.set()

    def check(self):
        if self.event.is_set():
            raise Stopped(self.reason)


def enable_supervision():
    # PTY tools can call setsid(). Adopt their orphans, not just process groups.
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
        raise CepsError("ceps requires Linux child-process supervision.")


def descendants():
    parents = {}
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                stat = (entry / "stat").read_text()
                parents[int(entry.name)] = int(stat[stat.rfind(")") + 2:].split()[1])
            except (OSError, ValueError, IndexError):
                continue
    owned = {os.getpid()}
    while True:
        more = {pid for pid, parent in parents.items() if parent in owned} - owned
        if not more:
            return owned - {os.getpid()}
        owned.update(more)


def drain_descendants():
    """Call after closing quota RPC and worker readers; Git must not be running."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        children = descendants()
        if not children:
            return
        for pid in children:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for pid in children:
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
        time.sleep(0.02)
    raise CepsError("Worker descendants remain alive; refusing to change Git.")


def stop_process(process):
    for sig, timeout in ((signal.SIGINT, 2), (signal.SIGTERM, 2), (signal.SIGKILL, 3)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=timeout)
            break
        except subprocess.TimeoutExpired:
            continue
    # Group members can outlive the leader.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=3)


def make_prompt(repo, label, quota):
    fleet = build_fleet(repo)
    return (
        f"Autonomous specification improvement, round {label}.\n\n"
        "Complete one small, valuable improvement to specification/. Preserve the current "
        "product intent and follow metaspecification/. Work autonomously without questions. "
        "Use the ceps MCP delegate tool aggressively for independent work. Every call "
        f"dispatches a concurrent fleet of {fleet.workers} supervised Luna specialists, sized "
        "from the specification's document count, words, sections, and cross-file links. "
        "Call it with phase understand to understand the entire specification and "
        "metaspecification, then phase plan to help plan the change before editing, then "
        "phase review for thorough adversarial review of your edits. A phase succeeds only "
        "when its entire fleet succeeds. Synthesize every specialist's findings, reconcile "
        "disagreements against cited specification passages, and incorporate the review. "
        "Launch additional focused fleets for "
        "uncertain requirements or dependencies: measured corpus structure is a complexity "
        "proxy, not a substitute for your judgment. Supply full context and concrete goals "
        "in each delegation, and await every fleet before finishing. All three phases must "
        "succeed. The delegate tool supplies Luna at maximum reasoning; you are Astra at "
        "Ultra. Native agent tools "
        "are intentionally replaced by this launcher. Do not start other model clients, "
        "background processes, or detached processes. Do not modify prompt.md, "
        "metaspecification/, tools/, configuration, or supervisor state. "
        "Finish review and return a concise account of the completed improvement, then stop. "
        "The supervisor repeats this prompt with a fresh session while quota remains. "
        f"Current lowest applicable quota: {quota['min_remaining']:g}% remaining; "
        f"requested floor {quota['floor']:g}%, buffer {quota['buffer']:g} percentage points. "
        "Keep changes small: interrupted or incomplete rounds will be discarded.\n\n"
        + (Path(repo) / "prompt.md").read_text()
        + "\n\nFor this supervised run, the supervisor alone handles ALL version control "
        "writes. This overrides the prepare and finish version control instructions above. "
        "Do not pull, stage, commit, push, reset, or switch branches. Read-only Git inspection "
        "is allowed. Leave the reviewed changes in the working tree for the supervisor.\n"
    )


class Attempt:
    """A parent and bounded concurrent Luna fleets owned by this supervisor."""

    def __init__(self, runtime, repo, directory, control, check_quota, record):
        self.runtime, self.repo = runtime, Path(repo)
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700)
        self.control, self.check_quota, self.record = control, check_quota, record
        self.closing = threading.Event()
        self.lock = threading.RLock()
        self.fleets_idle = threading.Condition(self.lock)
        self.fleet_slot = threading.Lock()
        self.fleet = build_fleet(self.repo)
        self.pending_fleets = 0
        self.fleet_count = 0
        self.child_cancellations = {}
        self.processes = set()
        self.phases = set()
        self.count = 0
        self.failed_child = False
        self.server = None
        self.server_thread = None
        self.socket_path = self.directory / "delegate.sock"
        self.record({"type": "fleet_planned", "profile": self.fleet.summary,
                     "workers": self.fleet.workers})

    def check(self):
        self.control.check()
        if self.closing.is_set():
            raise Stopped("Round is closing.")
        self.check_quota()

    def start_broker(self):
        owner = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(30)
                try:
                    raw = self.rfile.readline(1024 * 1024 + 1)
                    if len(raw) > 1024 * 1024 or not raw.endswith(b"\n"):
                        raise ValueError("Invalid delegation frame.")
                    request = json.loads(raw)
                    if (not isinstance(request, dict) or set(request) != {"task", "phase"}
                            or request["phase"] not in PHASES
                            or not isinstance(request["task"], str)
                            or not request["task"].strip()
                            or len(request["task"].encode()) > MAX_TEXT):
                        raise ValueError("Expected a task and understand, plan, or review phase.")
                    reply = owner.delegate(request["task"], request["phase"])
                except Exception as exc:
                    reply = {"ok": False, "text": str(exc)[:4000], "worker_id": "",
                             "model": "gpt-6-luna", "effort": "max"}
                try:
                    self.wfile.write(json.dumps(reply).encode() + b"\n")
                except (BrokenPipeError, OSError):
                    pass

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = False
            block_on_close = True

        self.server = Server(str(self.socket_path), Handler)
        os.chmod(self.socket_path, 0o600)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    def delegate(self, task, phase):
        with self.lock:
            self.check()
            self.pending_fleets += 1
        acquired = False
        try:
            while not self.fleet_slot.acquire(timeout=0.2):
                self.check()
            acquired = True
            self.check()
            with self.lock:
                if phase not in PHASES:
                    raise RoundError("Unknown delegation phase.")
                if phase == "plan" and "understand" not in self.phases:
                    raise RoundError("Complete an understanding fleet before planning.")
                if phase == "review" and "plan" not in self.phases:
                    raise RoundError("Complete a planning fleet before review.")
                self.check()
                # Keep this attempt's advertised width, but cover additions,
                # moves, and expanded documents in the parent's latest edits.
                current_fleet = replace(build_fleet(self.repo), workers=self.fleet.workers)
                assignments = current_fleet.assignments(task, phase)
                if len(assignments) != self.fleet.workers:
                    raise RoundError("Fleet assignments do not match the planned capacity.")
                self.fleet_count += 1
                fleet_id = f"fleet-{self.fleet_count}"
                workers = [f"luna-{self.count + index + 1}" for index in range(len(assignments))]
                self.count += len(workers)
                cancelled = threading.Event()
                self.child_cancellations.update((worker_id, cancelled) for worker_id in workers)
            metadata = {"worker": fleet_id, "phase": phase, "workers": len(workers)}
            self.record({"type": "fleet_started", **metadata})

            def specialist(worker_id, assignment):
                prompt = (
                    f"You are the {phase} subagent in supervised {fleet_id}, working alongside "
                    f"{len(workers) - 1} independent specialists. "
                    "Read the current specification/ and metaspecification/ as needed. "
                    "Report findings and concrete recommendations; do not edit any files. "
                    "Do not use other model clients or delegate further. Do not run background "
                    "processes or write Git state. Cite relevant document paths and passages. "
                    f"Keep the final findings within {max(200, 6000 // len(workers))} words "
                    "so every specialist's result fits the parent's combined report.\n\nTask:\n"
                    + assignment
                )
                try:
                    return self.run_worker("child", worker_id, prompt)
                except Exception as exc:
                    cancelled.set()
                    with self.lock:
                        self.failed_child = True
                    self.record({"type": "child_failed", "worker": worker_id, "phase": phase,
                                 "fleet": fleet_id, "error": str(exc)})
                    raise

            executor = None
            futures = {}
            try:
                executor = ThreadPoolExecutor(max_workers=len(workers), thread_name_prefix=fleet_id)
                for index, (worker_id, assignment) in enumerate(zip(workers, assignments)):
                    self.check()
                    futures[executor.submit(specialist, worker_id, assignment)] = index
                results = [None] * len(workers)
                pending = set(futures)
                while pending:
                    self.check()
                    completed, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                    for future in completed:
                        results[futures[future]] = future.result()
                with self.lock:
                    self.check()
                    self.phases.add(phase)
                self.record({"type": "fleet_completed", **metadata})
                self.record({"type": "phase_completed", "worker": fleet_id, "phase": phase})
                return {"ok": True, "text": fleet_text(fleet_id, phase, workers, results),
                        "worker_id": fleet_id, "model": "gpt-6-luna", "effort": "max"}
            except Exception as exc:
                cancelled.set()
                with self.lock:
                    self.failed_child = True
                for future in futures:
                    future.cancel()
                self.record({"type": "fleet_failed", **metadata, "error": str(exc)})
                raise
            finally:
                if executor is not None:
                    executor.shutdown(wait=True, cancel_futures=True)
                with self.lock:
                    for worker_id in workers:
                        self.child_cancellations.pop(worker_id, None)
        finally:
            if acquired:
                self.fleet_slot.release()
            with self.fleets_idle:
                self.pending_fleets -= 1
                self.fleets_idle.notify_all()

    def run_worker(self, role, worker_id, prompt):
        with self.lock:
            cancelled = self.child_cancellations.get(worker_id)

        def check():
            self.check()
            if cancelled is not None and cancelled.is_set():
                raise Stopped("A fleet specialist failed; stopping its peers.")

        check()
        try:
            command = build_exec_command(
                self.runtime, role, self.repo,
                broker_socket=self.socket_path if role == "parent" else None,
            )
            env = self.runtime.env()
        except CodexError as exc:
            self.control.stop("error", exc)
            raise Stopped(str(exc)) from exc
        result = {"complete": False, "text": "", "error": None, "usage": None}
        model = "gpt-6-astra" if role == "parent" else "gpt-6-luna"
        effort = "ultra" if role == "parent" else "max"
        with self.lock:
            # Admission, spawn, and registration are atomic against round closure.
            check()
            process = subprocess.Popen(
                command, cwd=self.repo, env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
            )
            self.processes.add(process)
        self.record({"type": "worker_started", "worker": worker_id, "role": role,
                     "model": model, "effort": effort,
                     "wire_effort": "xhigh" if role == "parent" else "max"})
        reader_shutdown = threading.Event()

        def read_events():
            try:
                buffer = bytearray()
                for chunk in output_chunks(process.stdout, process, reader_shutdown):
                    buffer.extend(chunk)
                    while b"\n" in buffer:
                        end = buffer.index(b"\n")
                        if end > MAX_LINE:
                            raise ValueError("Codex emitted an oversized event.")
                        line = bytes(buffer[:end])
                        del buffer[:end + 1]
                        event = json.loads(line)
                        if not isinstance(event, dict):
                            raise ValueError("Codex emitted a non-object event.")
                        kind = event.get("type")
                        if kind in ("turn.failed", "error"):
                            result["error"] = str(event.get("error", event.get("message", kind)))
                        if kind == "turn.completed":
                            if result["complete"]:
                                raise ValueError("Unexpected second completed turn.")
                            usage = event.get("usage")
                            if (not isinstance(usage, dict)
                                    or any(type(usage.get(key)) is not int or usage[key] < 0
                                           for key in ("input_tokens", "output_tokens"))
                                    or any(type(value) is not int or value < 0
                                           for key, value in usage.items() if key in TOKEN_FIELDS)):
                                raise ValueError("Codex did not provide valid token usage.")
                            result["usage"] = usage
                            result["complete"] = True
                        self.record({**event, "worker": worker_id})
                        item = event.get("item", {})
                        if kind == "item.completed" and item.get("type") == "agent_message":
                            result["text"] = bounded_text(item.get("text", ""))
                        # A native child or model reroute violates the fixed-model contract.
                        if ("rerout" in str(kind).lower()
                                or item.get("type") in ("collab_tool_call", "collaboration_tool_call")):
                            self.control.stop("error", "Unexpected native delegation or model reroute.")
                    if len(buffer) > MAX_LINE:
                        raise ValueError("Codex emitted an oversized event.")
                if buffer:
                    raise ValueError("Codex emitted a truncated event.")
            except Exception as exc:
                result["error"] = str(exc)

        def read_stderr():
            try:
                for chunk in output_chunks(process.stderr, process, reader_shutdown):
                    self.record({"type": "worker_stderr", "worker": worker_id,
                                 "text": chunk.decode("utf-8", "replace")})
            except Exception as exc:
                result["error"] = str(exc)

        readers = [threading.Thread(target=read_events, daemon=True),
                   threading.Thread(target=read_stderr, daemon=True)]
        for reader in readers:
            reader.start()
        started = time.monotonic()
        try:
            data = prompt.encode()
            fd = process.stdin.fileno()
            os.set_blocking(fd, False)
            offset = 0
            while offset < len(data):
                check()
                if time.monotonic() - started > 30:
                    raise RoundError("Codex did not accept its prompt within 30 seconds.")
                _, ready, _ = select.select([], [fd], [], 0.1)
                if ready:
                    try:
                        offset += os.write(fd, data[offset:])
                    except BlockingIOError:
                        pass
            process.stdin.close()
            while process.poll() is None:
                check()
                if result["error"]:
                    raise RoundError(result["error"])
                if time.monotonic() - started > 3600:
                    raise RoundError("Worker exceeded the one-hour attempt limit.")
                self.control.event.wait(0.1)
            for reader in readers:
                reader.join(timeout=2)
            if any(reader.is_alive() for reader in readers):
                raise RoundError("Codex output did not finish with its process.")
            check()
            if (process.returncode != 0 or result["error"] or not result["complete"]
                    or not result["text"].strip()):
                raise RoundError(result["error"] or
                                 f"Codex exited without a complete response (exit {process.returncode}).")
            return result["text"]
        except (BrokenPipeError, OSError) as exc:
            raise RoundError(f"Codex worker failed: {exc}") from exc
        finally:
            stop_process(process)
            with self.lock:
                self.processes.discard(process)
            reader_shutdown.set()
            for reader in readers:
                reader.join(timeout=5)
            if any(reader.is_alive() for reader in readers):
                raise CepsError("Worker output readers did not stop; refusing Git changes.")
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream and not stream.closed:
                    stream.close()
            self.record({"type": "worker_stopped", "worker": worker_id,
                         "usage_complete": result["usage"] is not None,
                         "elapsed_seconds": round(time.monotonic() - started, 3)})

    def run(self, prompt):
        self.start_broker()
        result = self.run_worker("parent", "astra", prompt)
        # No new task may race completion, and the parent must await its workers.
        with self.lock:
            self.closing.set()
            if self.processes or self.pending_fleets:
                raise RoundError("The parent finished while a delegated fleet was still pending or active.")
            if self.failed_child or self.phases != PHASES:
                raise RoundError("The round lacks successful understanding, planning, or review fleets.")
        return result

    def close(self):
        with self.lock:
            self.closing.set()
            for cancelled in self.child_cancellations.values():
                cancelled.set()
            processes = list(self.processes)
        if processes:
            with ThreadPoolExecutor(max_workers=min(32, len(processes))) as executor:
                # Stop every process promptly, including parents and all fleet members.
                list(executor.map(stop_process, processes))
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server_thread.join(timeout=5)
            self.socket_path.unlink(missing_ok=True)
        with self.fleets_idle:
            if not self.fleets_idle.wait_for(lambda: not self.pending_fleets, timeout=30):
                raise CepsError("Delegated fleets did not stop; refusing Git changes.")
