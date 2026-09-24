"""Retry accounting must retain every charge and fail closed on uncertainty."""

import decimal
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "tools" / "spec"
LOADER = importlib.machinery.SourceFileLoader("spec_retry_helpers", str(SOURCE))
MODULE = importlib.util.spec_from_loader(LOADER.name, LOADER)
runner = importlib.util.module_from_spec(MODULE)
LOADER.exec_module(runner)


class RetryAccountingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "budget.json"
        self.budget = decimal.Decimal("2.00")

    def write_state(self, **changes):
        state = {
            "version": 1, "pid": 123, "limit": 2, "reserve": 1,
            "spent": 0.15, "reserved": 0, "remaining": 1.85,
            "available": 0.85, "requests": 5, "settledRequests": 5,
            "inFlight": 0, "stopped": True, "stopKind": "error",
            "retryable": True, "reason": "Provider disconnected",
            "estimatedSpend": True, "blockedAuxiliaryRequests": 3,
            "updatedAt": "2026-09-23T00:00:00Z",
        }
        state.update(changes)
        self.path.write_text(json.dumps(state))
        return state

    def read_state(self):
        return json.loads(self.path.read_text())

    def test_crashed_requests_are_charged_once_before_rearming(self):
        self.write_state(reserved=0.3, inFlight=2, settledRequests=3,
                         stopped=False, stopKind="", retryable=False, reason="")

        settled = runner.settle_shutdown(self.path)

        self.assertEqual(settled["spent"], 0.45)
        self.assertEqual(settled["settledRequests"], 5)
        self.assertEqual(settled["reserved"], 0)
        self.assertEqual(settled["inFlight"], 0)
        self.assertTrue(settled["retryable"])
        self.assertTrue(settled["estimatedSpend"])
        self.assertEqual(runner.settle_shutdown(self.path), settled)
        self.assertTrue(runner.prepare_retry(self.path, self.budget))
        rearmed = runner.settle_shutdown(self.path)
        self.assertEqual(rearmed["spent"], 0.45)
        self.assertEqual(rearmed["settledRequests"], 5)
        self.assertFalse(rearmed["stopped"])

    def test_pending_usage_cannot_make_hard_errors_or_budget_stops_retryable(self):
        for kind, retryable in (("error", False), ("error", None), ("budget", False)):
            with self.subTest(kind=kind, retryable=retryable):
                before = self.write_state(
                    reserved=0.3, inFlight=2, settledRequests=3,
                    stopKind=kind, retryable=retryable, reason="Original terminal reason",
                )
                if retryable is None:
                    del before["retryable"]
                    self.path.write_text(json.dumps(before))
                settled = runner.settle_shutdown(self.path)
                self.assertEqual(settled["spent"], 0.45)
                self.assertEqual(settled["settledRequests"], 5)
                for key in ("stopped", "stopKind", "reason", "retryable"):
                    self.assertEqual(settled.get(key), before.get(key))
                if kind == "budget":
                    self.assertFalse(runner.prepare_retry(self.path, self.budget))
                else:
                    with self.assertRaises(runner.SpecError):
                        runner.prepare_retry(self.path, self.budget)
                self.assertEqual(self.read_state(), settled)

    def test_rearming_preserves_cumulative_accounting_and_audit_metadata(self):
        before = self.write_state(spent=0.45, remaining=1.55, available=0.55,
                                  audit={"previousAttempts": 2})

        self.assertTrue(runner.prepare_retry(self.path, self.budget))

        after = self.read_state()
        expected = dict(before, stopped=False, stopKind="", reason="", retryable=False)
        self.assertEqual(after, expected)

    def test_unknown_or_unsettled_accounting_cannot_be_rearmed(self):
        invalid = (
            {"spent": -0.1}, {"spent": float("nan")}, {"spent": True},
            {"spent": "0.15"}, {"reserved": 0.1}, {"inFlight": 1},
            {"settledRequests": 4}, {"settledRequests": 6}, {"requests": 5.0},
            {"version": 2}, {"version": True}, {"limit": 3},
            {"reserve": 0}, {"reserve": True}, {"stopped": 0}, {"stopped": False},
            {"retryable": False}, {"retryable": "true"}, {"retryable": 1},
            {"stopKind": "unknown"},
        )
        for changes in invalid:
            with self.subTest(changes=changes):
                self.write_state(**changes)
                original = self.path.read_text()
                with self.assertRaises(runner.SpecError):
                    runner.prepare_retry(self.path, self.budget)
                self.assertEqual(self.path.read_text(), original)
        for contents in ("broken JSON", "{}"):
            with self.subTest(contents=contents):
                self.path.write_text(contents)
                with self.assertRaises(runner.SpecError):
                    runner.prepare_retry(self.path, self.budget)
                self.assertEqual(self.path.read_text(), contents)
        self.path.unlink()
        with self.assertRaises(runner.SpecError):
            runner.prepare_retry(self.path, self.budget)
        self.assertFalse(self.path.exists())

    def test_conservative_charges_exhaust_allowance_without_another_attempt(self):
        self.write_state(spent=0.8, reserved=0.2, inFlight=1, settledRequests=4)
        settled = runner.settle_shutdown(self.path)
        self.assertEqual(settled["spent"], 1)

        self.assertFalse(runner.prepare_retry(self.path, self.budget))

        final = self.read_state()
        self.assertTrue(final["stopped"])
        self.assertEqual(final["stopKind"], "budget")
        self.assertFalse(final["retryable"])
        self.assertEqual(final["spent"], 1)
        self.assertEqual(final["remaining"], 1)
        self.assertEqual(final["available"], 0)
        self.assertEqual(final["requests"], final["settledRequests"])
        self.assertEqual(runner.settle_shutdown(self.path), final)


if __name__ == "__main__":
    unittest.main()
