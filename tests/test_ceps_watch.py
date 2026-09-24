"""Quota polling recovery never permits work on a failed account reading."""

import contextlib
import copy
import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / "tools"))
LOADER = importlib.machinery.SourceFileLoader("ceps_watch_test_controller",
                                            str(SOURCE / "tools/ceps"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
CONTROLLER = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(CONTROLLER)


def response(used=10):
    bucket = {"limitId": "codex", "limitName": None, "normalModelSlug": None,
              "primary": {"usedPercent": used, "windowDurationMins": 10080,
                          "resetsAt": 2_000_000_000},
              "secondary": None, "spendControlReached": False,
              "rateLimitReachedType": None}
    return {"ordinaryUsageAllowed": True, "accountId": "fixture-account",
            "rateLimits": copy.deepcopy(bucket), "rateLimitsByLimitId": {"codex": bucket}}


class QuotaWatchTests(unittest.TestCase):
    def setUp(self):
        self.client = mock.Mock()
        self.guard = CONTROLLER.QuotaGuard(10)
        self.control = CONTROLLER.Control()
        self.stats = mock.Mock()
        self.watch = CONTROLLER.QuotaWatch(self.client, self.guard, self.control, self.stats)
        self.addCleanup(self.watch.close)
        self.backoff = mock.patch.object(self.control.event, "wait", return_value=False)
        self.wait = self.backoff.start()
        self.addCleanup(self.backoff.stop)

    def events(self, kind):
        return [call.args[0] for call in self.stats.record.call_args_list
                if call.args[0]["type"] == kind]

    def poll_once(self):
        with mock.patch.object(self.watch.closed, "wait", return_value=False):
            self.watch.start()
            self.watch.thread.join(timeout=2)
        self.assertFalse(self.watch.thread.is_alive())

    def test_initial_recovery_requires_new_read_and_uses_bounded_backoff(self):
        self.client.read_limits.side_effect = [
            CONTROLLER.RetryableQuotaError("server unavailable"),
            CONTROLLER.RetryableQuotaError("request timed out"), response(17)]

        snapshot = self.watch.recover()

        self.assertEqual(snapshot["min_remaining"], 83)
        self.assertEqual(self.client.read_limits.call_count, 3)
        self.assertEqual(self.client.close.call_count, 2)
        self.assertEqual(self.wait.call_args_list, [mock.call(1), mock.call(2)])
        self.assertEqual(len(self.events("quota_recovered")), 1)
        self.assertFalse(self.control.event.is_set())
        self.assertIsNone(self.watch.failure)

    def test_successful_initial_read_does_not_retry_or_reconnect(self):
        self.client.read_limits.return_value = response()

        self.assertEqual(self.watch.recover()["min_remaining"], 90)

        self.client.read_limits.assert_called_once_with()
        self.client.close.assert_not_called()
        self.wait.assert_not_called()
        self.assertEqual(self.events("quota_retry"), [])
        self.assertEqual(self.events("quota_recovered"), [])

    def test_live_failure_counts_toward_three_read_limit(self):
        failure = CONTROLLER.RetryableQuotaError("live poll disconnected")
        self.client.read_limits.side_effect = [failure, failure, failure, response()]
        with self.assertRaises(CONTROLLER.RetryableQuotaError):
            self.watch.refresh()

        with self.assertRaisesRegex(CONTROLLER.QuotaError, "three read attempts.*disconnected"):
            self.watch.recover(self.watch.failure)

        self.assertEqual(self.client.read_limits.call_count, 3)
        self.assertEqual(self.wait.call_args_list, [mock.call(1), mock.call(2)])
        self.assertEqual(len(self.events("quota_error")), 3)
        self.assertEqual(len(self.events("quota_retry")), 2)
        self.assertEqual(self.events("quota_recovered"), [])

    def test_fresh_cached_quota_is_rejected_until_live_failure_recovers(self):
        failure = CONTROLLER.RetryableQuotaError("temporary transport failure")
        self.client.read_limits.side_effect = [response(10), failure, response(25)]
        self.watch.refresh()
        with self.assertRaises(CONTROLLER.RetryableQuotaError):
            self.watch.refresh()

        self.assertEqual(self.guard.check()["min_remaining"], 90)
        for _ in range(2):
            with self.assertRaisesRegex(CONTROLLER.RoundError, "temporary transport failure"):
                self.watch.check()
        saved = self.stats.update.call_args.kwargs["quota"]
        self.assertFalse(saved["valid"])
        self.assertIn("transport failure", saved["error"])
        self.assertFalse(self.control.event.is_set())

        self.watch.recover(self.watch.failure)

        self.assertEqual(self.watch.check()["min_remaining"], 75)
        self.assertIsNone(self.watch.failure)
        self.assertFalse(self.control.event.is_set())
        self.assertEqual(self.client.read_limits.call_count, 3)

    def test_permanent_error_is_not_retried(self):
        self.client.read_limits.side_effect = CONTROLLER.QuotaError("account identity changed")

        with self.assertRaisesRegex(CONTROLLER.QuotaError, "identity changed"):
            self.watch.recover()

        self.client.read_limits.assert_called_once_with()
        self.client.close.assert_not_called()
        self.wait.assert_not_called()
        self.assertEqual(self.events("quota_retry"), [])

    def test_permanent_error_does_not_restore_cached_quota_validity(self):
        self.client.read_limits.side_effect = [
            response(), CONTROLLER.QuotaError("account no longer authorized")]
        self.watch.refresh()
        with self.assertRaises(CONTROLLER.QuotaError):
            self.watch.refresh()

        with self.assertRaisesRegex(CONTROLLER.Stopped, "no longer authorized"):
            self.watch.check()

        self.assertEqual(self.control.kind, "error")
        saved = self.stats.update.call_args.kwargs["quota"]
        self.assertFalse(saved["valid"])
        self.assertEqual(saved["error"], "account no longer authorized")
        self.assertEqual(saved["min_remaining"], 90)

    def test_quota_floor_is_not_retried(self):
        self.client.read_limits.return_value = response(88)

        with self.assertRaises(CONTROLLER.QuotaReached):
            self.watch.recover()

        self.client.read_limits.assert_called_once_with()
        self.client.close.assert_not_called()
        self.wait.assert_not_called()
        self.assertEqual(self.events("quota_retry"), [])

    def test_signal_during_backoff_prevents_another_read(self):
        self.client.read_limits.side_effect = [
            CONTROLLER.RetryableQuotaError("server unavailable"), response()]
        self.wait.side_effect = lambda delay: self.control.stop("signal", "Interrupted")

        with self.assertRaisesRegex(CONTROLLER.Stopped, "Interrupted"):
            self.watch.recover()

        self.client.read_limits.assert_called_once_with()
        self.assertEqual(self.control.kind, "signal")
        self.wait.assert_called_once_with(1)
        self.assertEqual(self.events("quota_recovered"), [])

    def test_initial_exhaustion_preserves_each_error_in_bounded_events(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory)
            stats = CONTROLLER.Statistics(root, 10, 2)
            self.watch.stats = stats
            self.client.read_limits.side_effect = [
                CONTROLLER.RetryableQuotaError(f"upstream failure {index}")
                for index in range(3)]
            try:
                with self.assertRaisesRegex(CONTROLLER.QuotaError, "upstream failure 2"):
                    self.watch.recover()
            finally:
                stats.close()
            events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
            errors = [event for event in events if event["type"] == "quota_error"]
            self.assertEqual([event["error"] for event in errors],
                             [f"upstream failure {index}" for index in range(3)])
            self.assertEqual(len(events), 5)
            self.assertIsNone(json.loads((root / "stats.json").read_text())["quota"])

    def test_poll_failure_remains_visible_after_reader_closes(self):
        self.client.read_limits.side_effect = [
            response(), CONTROLLER.RetryableQuotaError("late account poll failure")]
        self.watch.refresh()

        self.poll_once()
        self.watch.close()

        self.assertFalse(self.control.event.is_set())
        with self.assertRaisesRegex(CONTROLLER.RoundError, "late account poll failure"):
            self.watch.check()
        self.client.close.assert_called_once_with()
        self.assertEqual(self.client.read_limits.call_count, 2)

    def test_poll_stops_globally_on_permanent_error_or_quota_floor(self):
        for failure, kind in [(CONTROLLER.QuotaError("identity changed"), "error"),
                              (CONTROLLER.QuotaReached("quota reserve reached"), "quota")]:
            with self.subTest(kind=kind):
                client = mock.Mock()
                client.read_limits.side_effect = failure
                control = CONTROLLER.Control()
                watch = CONTROLLER.QuotaWatch(client, self.guard, control, self.stats)
                try:
                    with mock.patch.object(watch.closed, "wait", return_value=False):
                        watch.start()
                        watch.thread.join(timeout=2)
                    self.assertFalse(watch.thread.is_alive())
                    self.assertEqual(control.kind, kind)
                    with self.assertRaises(CONTROLLER.Stopped):
                        watch.check()
                    client.read_limits.assert_called_once_with()
                finally:
                    watch.close()

    def test_close_refuses_to_release_client_if_reader_does_not_stop(self):
        thread = mock.Mock()
        thread.is_alive.return_value = True
        self.watch.thread = thread

        try:
            with self.assertRaisesRegex(CONTROLLER.CepsError, "refusing Git changes"):
                self.watch.close()
            self.assertTrue(self.watch.closed.is_set())
            self.client.close.assert_not_called()
            thread.join.assert_called_once()
        finally:
            thread.is_alive.return_value = False


if __name__ == "__main__":
    unittest.main()
