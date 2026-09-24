"""RPC diagnostics stay bounded while preserving the controller's evidence."""

import concurrent.futures
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from tools.spec_logs import BoundedJsonlLog, compact_event, prune_rpc_logs


class CompactEventTests(unittest.TestCase):
    def test_parent_stream_keeps_only_activity_kind_without_mutating_input(self):
        snapshot = {"role": "assistant", "content": [{"type": "text", "text": "x" * 100000}]}
        event = {"type": "message_update", "message": snapshot,
                 "assistantMessageEvent": {"type": "text_delta", "partial": snapshot,
                                           "delta": "x" * 100000, "contentIndex": 0}}
        self.assertEqual(compact_event(event), {
            "type": "message_update", "assistantMessageEvent": {"type": "text_delta"},
        })
        self.assertIs(event["message"], snapshot)
        self.assertIs(event["assistantMessageEvent"]["partial"], snapshot)

    def test_child_stream_retains_envelope_without_cumulative_snapshots(self):
        event = {"type": "subagent_event", "payload": {
            "agentId": "reviewer", "taskId": "task-1", "event": {
                "type": "message_update", "message": {"content": "large"},
                "assistantMessageEvent": {"type": "thinking_delta", "partial": "large", "delta": "large"},
            },
        }}
        result = compact_event(event)
        self.assertEqual(result["payload"]["agentId"], "reviewer")
        self.assertEqual(result["payload"]["event"], {
            "type": "message_update", "assistantMessageEvent": {"type": "thinking_delta"},
        })
        self.assertIn("message", event["payload"]["event"])

    def test_tool_updates_drop_snapshots_but_final_and_control_events_stay_intact(self):
        self.assertEqual(compact_event({
            "type": "tool_execution_update", "toolName": "bash", "toolCallId": "tool-1",
            "partialResult": {"content": "large"},
        }), {"type": "tool_execution_update", "toolName": "bash", "toolCallId": "tool-1"})
        for event in (
            {"type": "message_end", "message": {"role": "assistant", "stopReason": "error",
                                                   "errorMessage": "provider closed", "usage": {"input": 10}}},
            {"type": "response", "id": "spec-1", "success": True, "data": {"isStreaming": False}},
            {"type": "agent_end", "isTerminal": False},
        ):
            self.assertIs(compact_event(event), event)


class BoundedLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "events.jsonl"

    def records(self):
        result = []
        for path in (self.path.with_name(self.path.name + ".1"), self.path):
            if path.exists():
                data = path.read_bytes()
                self.assertTrue(not data or data.endswith(b"\n"))
                result.extend(json.loads(line) for line in data.splitlines())
        return result

    def test_streams_are_not_persisted_but_lifecycle_and_errors_are(self):
        final = {"type": "message_end", "message": {"role": "assistant", "stopReason": "error",
                                                      "errorMessage": "Connection closed", "usage": {"input": 37}}}
        with BoundedJsonlLog(self.path) as log:
            for kind in ("message_update", "tool_execution_update"):
                stream = {"type": kind, "message": {"content": "x" * 100000}}
                self.assertFalse(log.write(stream))
                self.assertFalse(log.write({"type": "subagent_event", "payload": {"event": stream}}))
            for event in ({"type": "agent_start"}, final, {"type": "agent_end", "isTerminal": True}):
                self.assertTrue(log.write(event))
        self.assertEqual(self.records(), [{"type": "agent_start"}, final, {"type": "agent_end", "isTerminal": True}])

    def test_rotation_reopen_and_permissions_enforce_two_file_bound(self):
        limit = 1024
        for batch in range(3):
            with BoundedJsonlLog(self.path, max_bytes=limit) as log:
                for index in range(20):
                    log.write({"type": "message_end", "index": batch * 20 + index, "content": "x" * 150})
        records = self.records()
        self.assertEqual(records[-1]["index"], 59)
        self.assertEqual([event["index"] for event in records], list(range(records[0]["index"], 60)))
        files = list(Path(self.tmp.name).iterdir())
        self.assertEqual(len(files), 2)
        for path in files:
            self.assertLessEqual(path.stat().st_size, limit)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_huge_single_final_event_preserves_error_accounting(self):
        usage = {"input": 234, "output": 56, "cost": {"total": 0.04}}
        event = {"type": "subagent_event", "payload": {"agentId": "reviewer", "event": {
            "type": "message_end", "message": {
                "role": "assistant", "stopReason": "error", "errorMessage": "provider disconnected",
                "usage": usage, "content": [{"type": "text", "text": "x" * 4_000_000}],
            },
        }}}
        with BoundedJsonlLog(self.path, max_bytes=1024) as log:
            log.write(event)
        record = self.records()[0]
        final = record["payload"]["event"]["message"]
        self.assertEqual(final["role"], "assistant")
        self.assertEqual(final["stopReason"], "error")
        self.assertEqual(final["errorMessage"], "provider disconnected")
        self.assertEqual(final["usage"], usage)
        self.assertTrue(record["log_truncated"])
        self.assertLessEqual(self.path.stat().st_size, 1024)

    def test_huge_control_error_preserves_request_identity_and_failure(self):
        with BoundedJsonlLog(self.path, max_bytes=1024) as log:
            log.write({"type": "response", "id": "spec-42", "success": False,
                       "error": "request aborted", "data": {"transcript": "x" * 100000}})
        record = self.records()[0]
        self.assertEqual({key: record[key] for key in ("type", "id", "success", "error")}, {
            "type": "response", "id": "spec-42", "success": False, "error": "request aborted",
        })

    def test_escape_expansion_and_large_metadata_still_produce_bounded_json(self):
        with BoundedJsonlLog(self.path, max_bytes=1024) as log:
            log.write({"type": "extension_error", "error": "bad extension", "content": "\x00" * 700})
            self.assertTrue(log.write({
                "type": "message_end", "id": "spec-1", "message": {
                    "role": "assistant", "stopReason": "error", "errorMessage": "provider failed",
                    "content": "x" * 100000,
                    "usage": {str(index): {str(child): list(range(24)) for child in range(24)} for index in range(24)},
                },
            }))
        for record in self.records():
            self.assertTrue(record["log_truncated"])
        last = self.records()[-1]
        self.assertEqual(last["message"]["errorMessage"], "provider failed")
        self.assertLessEqual(self.path.stat().st_size, 1024)

    def test_concurrent_rpc_and_stderr_writers_leave_valid_bounded_records(self):
        with BoundedJsonlLog(self.path, max_bytes=2048) as log:
            def writer(kind):
                for index in range(150):
                    log.write({"type": kind, "index": index, "text": "x" * 50})
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(writer, kind) for kind in ("response", "worker_stderr")]
                for future in futures:
                    future.result()
        self.assertTrue(self.records())
        self.assertTrue(all(path.stat().st_size <= 2048 for path in Path(self.tmp.name).iterdir()))

    def test_existing_oversized_logs_and_public_modes_are_repaired(self):
        self.path.write_bytes(b"x" * 3000)
        backup = self.path.with_name(self.path.name + ".1")
        backup.write_bytes(b"x" * 3000)
        self.path.chmod(0o644)
        with BoundedJsonlLog(self.path, max_bytes=1024) as log:
            log.write({"type": "ready"})
        self.assertEqual(self.records(), [{"type": "ready"}])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.old = self.make_run("20260920T000000Z-999999991")
        self.recent = self.make_run("20260921T000000Z-999999992")
        self.active = self.make_run("20260923T000000Z-999999993")

    def make_run(self, name):
        path = self.root / name
        path.mkdir()
        return path

    def fill(self, run, name="events.jsonl", size=100):
        path = run / name
        path.write_bytes(b"x" * size)
        return path

    def test_oldest_rpc_logs_removed_and_full_active_growth_reserved(self):
        old_log = self.fill(self.old, "round-1.jsonl")
        recent_log = self.fill(self.recent)
        active_log = self.fill(self.active, size=10)
        removed = prune_rpc_logs(self.root, self.active, max_bytes=250, reserve_bytes=150)
        self.assertEqual(removed, 100)
        self.assertFalse(old_log.exists())
        self.assertTrue(recent_log.exists())
        self.assertTrue(active_log.exists())

    def test_unknown_files_sessions_accounting_and_symlinks_are_preserved(self):
        old_log = self.fill(self.old)
        protected = [self.fill(self.old, name) for name in ("budget.json", "config.yml", "notes.jsonl", "round-x.jsonl")]
        sessions = self.old / "sessions"
        sessions.mkdir()
        protected.append(self.fill(sessions, "conversation.jsonl"))
        unknown = self.make_run("unrelated")
        protected.append(self.fill(unknown))
        linked_log = self.recent / "round-1.jsonl"
        linked_log.symlink_to(protected[0])
        protected.append(linked_log)
        linked_run = self.root / "20260919T000000Z-999999990"
        linked_run.symlink_to(unknown, target_is_directory=True)
        prune_rpc_logs(self.root, self.active, max_bytes=0, reserve_bytes=0)
        self.assertFalse(old_log.exists())
        self.assertTrue(all(path.exists() for path in protected))
        self.assertTrue(linked_run.is_symlink())

    def test_live_budget_owner_and_supervisor_are_preserved(self):
        worker_log = self.fill(self.old)
        (self.old / "budget.json").write_text(json.dumps({"pid": os.getpid()}))
        live_supervisor = self.make_run(f"20260919T000000Z-{os.getpid()}")
        supervisor_log = self.fill(live_supervisor)
        stale_log = self.fill(self.recent)
        prune_rpc_logs(self.root, self.active, max_bytes=0, reserve_bytes=0)
        self.assertTrue(worker_log.exists())
        self.assertTrue(supervisor_log.exists())
        self.assertFalse(stale_log.exists())

    def test_io_failure_is_reported_instead_of_claiming_retention_succeeded(self):
        self.fill(self.old)
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("cannot remove")):
            with self.assertRaises(PermissionError):
                prune_rpc_logs(self.root, self.active, max_bytes=0, reserve_bytes=0)


if __name__ == "__main__":
    unittest.main()
