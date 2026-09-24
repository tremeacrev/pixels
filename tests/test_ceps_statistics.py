"""Concurrent CEPS fleet accounting, using no model or account requests."""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest


SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / "tools"))
LOADER = importlib.machinery.SourceFileLoader("ceps_statistics_test_controller",
                                          str(SOURCE / "tools/ceps"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
CONTROLLER = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(CONTROLLER)


class FleetStatisticsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.stats = CONTROLLER.Statistics(self.root, 20, 2)
        self.addCleanup(self.stats.close)
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)
        self.stats.update(attempt="1A")

    def start(self, worker, role):
        self.stats.record({"type": "worker_started", "worker": worker, "role": role,
                           "model": "gpt-6-luna" if role == "child" else "gpt-6-astra",
                           "effort": "max" if role == "child" else "ultra",
                           "wire_effort": "max" if role == "child" else "xhigh"})

    def stop(self, worker, usage_complete=True):
        self.stats.record({"type": "worker_stopped", "worker": worker,
                           "usage_complete": usage_complete})

    def saved(self):
        return json.loads((self.root / "stats.json").read_text())

    def test_overlapping_children_have_distinct_accounting_and_saved_peak(self):
        width = 8
        started = threading.Barrier(width + 1)
        released = threading.Event()
        failures = []
        self.start("astra", "parent")

        def run(index):
            try:
                worker = f"luna-{index}"
                self.start(worker, "child")
                event = {"type": "turn.completed", "worker": worker,
                         "usage": {"input_tokens": 10, "output_tokens": 4}}
                self.stats.record(event)
                self.stats.record(event)
                started.wait(timeout=10)
                if not released.wait(timeout=10):
                    raise TimeoutError("Test did not release the workers")
                self.stop(worker)
            except BaseException as exc:
                failures.append(exc)

        threads = [threading.Thread(target=run, args=(index,)) for index in range(width)]
        for thread in threads:
            thread.start()
        try:
            started.wait(timeout=10)
            active = self.saved()
            self.assertEqual(active["workers_active"], width + 1)
            self.assertEqual(active["children_active"], width)
            self.assertEqual(active["parents_active"], 1)
            self.assertEqual(active["children_peak"], width)
        finally:
            released.set()
            for thread in threads:
                thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])
        self.stop("astra", usage_complete=False)
        final = self.saved()
        self.assertEqual(final["workers_started"], width + 1)
        self.assertEqual(final["workers_active"], 0)
        self.assertEqual(final["children_active"], 0)
        self.assertEqual(final["parents_active"], 0)
        self.assertEqual(final["children_peak"], width)
        self.assertEqual(final["tokens"]["input_tokens"], 10 * width)
        self.assertEqual(final["tokens"]["output_tokens"], 4 * width)
        self.assertEqual(final["usage_incomplete_workers"], 1)

    def test_fleet_lifecycle_reports_sizing_and_successful_vs_failed_fleets(self):
        profile = {"documents": 6, "words": 8000, "headings": 45,
                   "cross_file_links": 20, "workers": 12, "max_workers": 32}
        self.stats.record({"type": "fleet_planned", "workers": 12, "profile": profile})
        fleet = {"worker": "fleet-1", "phase": "understand", "workers": 12}
        self.stats.record({"type": "fleet_started", **fleet})
        self.start("astra", "parent")
        self.start("luna-1", "child")
        self.stats.status(force=True)
        self.stop("luna-1")
        self.stats.record({"type": "fleet_completed", **fleet})
        fleet = {"worker": "fleet-2", "phase": "plan", "workers": 12}
        self.stats.record({"type": "fleet_started", **fleet})
        self.stats.record({"type": "fleet_failed", "error": "Worker interrupted", **fleet})
        saved = self.saved()
        self.assertEqual(saved["fleet_profile"], profile)
        self.assertEqual(saved["fleet_width"], 12)
        self.assertEqual(saved["fleets_started"], 2)
        self.assertEqual(saved["fleets_completed"], 1)
        self.assertEqual(saved["fleets_failed"], 1)
        self.assertIn("12 concurrent Luna workers per phase", self.output.getvalue())
        self.assertIn("6 documents, 8,000 words, 45 headings, 20 cross-file links", self.output.getvalue())
        self.assertIn("1/12 Luna + 1 Astra | peak 1 Luna", self.output.getvalue())
        self.assertIn("astra: gpt-6-astra / ultra (request effort xhigh)", self.output.getvalue())
        self.assertIn("luna-1: gpt-6-luna / max (request effort max)", self.output.getvalue())
        self.assertIn("understand fleet completed (12 Luna workers)", self.output.getvalue())

    def test_reused_worker_ids_in_new_attempts_preserve_peak_and_usage(self):
        for attempt in ("1A", "1B"):
            self.stats.update(attempt=attempt)
            self.start("luna-1", "child")
            self.stats.record({"type": "turn.completed", "worker": "luna-1",
                               "usage": {"input_tokens": 10, "output_tokens": 4}})
            self.stop("luna-1")
        saved = self.saved()
        self.assertEqual(saved["workers_started"], 2)
        self.assertEqual(saved["workers_active"], 0)
        self.assertEqual(saved["children_peak"], 1)
        self.assertEqual(saved["tokens"]["input_tokens"], 20)


if __name__ == "__main__":
    unittest.main()
