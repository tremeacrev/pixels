"""Quota enforcement uses real account percentages and fails closed on uncertainty."""

import copy
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from unittest import mock

from tools.ceps_quota import (
    AccountClient, QuotaError, QuotaGuard, QuotaReached, RetryableQuotaError, account_fingerprint,
)


def bucket(used=10, *, bucket_id="codex", model=None, secondary=None, reset=2_000_000_000):
    return {"limitId": bucket_id, "limitName": None, "normalModelSlug": model,
            "primary": {"usedPercent": used, "windowDurationMins": 10080, "resetsAt": reset},
            "secondary": secondary, "spendControlReached": False, "rateLimitReachedType": None}


def response(used=10, **kwargs):
    meter = bucket(used, **kwargs)
    return {"ordinaryUsageAllowed": True, "accountId": "fixture-account",
            "rateLimits": copy.deepcopy(meter), "rateLimitsByLimitId": {meter["limitId"]: meter}}


class QuotaGuardTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.guard = QuotaGuard(10, clock=lambda: self.now)

    def test_weekly_primary_is_not_assumed_to_be_five_hours(self):
        snapshot = self.guard.update(response(17.5))
        self.assertEqual(snapshot["min_remaining"], 82.5)
        self.assertEqual(snapshot["windows"][0]["duration_mins"], 10080)
        self.assertEqual(len(snapshot["windows"]), 1)
        self.assertEqual(self.guard.check()["min_remaining"], 82.5)
        self.assertNotIn("fixture-account", json.dumps(snapshot))

    def test_every_applicable_window_and_bucket_is_protected(self):
        data = response(10, secondary={"usedPercent": 80, "windowDurationMins": 300, "resetsAt": 2_000_001_000})
        data["rateLimitsByLimitId"]["luna"] = bucket(88, bucket_id="luna", model="gpt-6-luna")
        snapshot = self.guard.update(data)
        self.assertEqual(len(snapshot["windows"]), 3)
        self.assertEqual(snapshot["min_remaining"], 12)
        with self.assertRaises(QuotaReached):
            self.guard.check()

    def test_threshold_has_an_explicit_buffer_and_inclusive_boundary(self):
        self.guard.update(response(87.99))
        self.guard.check()
        self.guard.update(response(88))
        with self.assertRaisesRegex(QuotaReached, "stop at 12%"):
            self.guard.check()

    def test_overused_quota_is_zero_remaining(self):
        guard = QuotaGuard(0, buffer=0)
        self.assertEqual(guard.update(response(102.5))["min_remaining"], 0)
        with self.assertRaises(QuotaReached):
            guard.check()

    def test_stale_at_maximum_age_even_if_server_reset_time_has_passed(self):
        self.guard.update(response(10, reset=1))
        self.now += 29.9
        self.guard.check()
        self.now += 0.1
        with self.assertRaisesRegex(QuotaError, "stale"):
            self.guard.check()
        self.assertFalse(self.guard.snapshot()["valid"])

    def test_missing_initial_quota_is_not_unlimited(self):
        self.assertIsNone(self.guard.snapshot())
        with self.assertRaises(QuotaError):
            self.guard.check()

    def test_unknown_permission_rejects_even_with_fresh_full_allowance(self):
        for permission in (None, "true", 1):
            data = response(0)
            data["ordinaryUsageAllowed"] = permission
            with self.subTest(permission=permission), self.assertRaises(QuotaError):
                self.guard.update(data)

    def test_backend_blocks_override_good_percentages(self):
        for field, value in (("rateLimitReachedType", "rate_limit_reached"),
                             ("spendControlReached", True)):
            data = response(0)
            data["rateLimits"][field] = value
            data["rateLimitsByLimitId"]["codex"][field] = value
            guard = QuotaGuard(10)
            guard.update(data)
            with self.subTest(field=field), self.assertRaises(QuotaReached):
                guard.check()
        data = response(0)
        data["ordinaryUsageAllowed"] = False
        data["rateLimits"]["primary"] = None
        data["rateLimitsByLimitId"]["codex"]["primary"] = None
        self.guard.update(data)
        with self.assertRaises(QuotaReached):
            self.guard.check()

    def test_no_windows_or_only_unrelated_windows_fail_closed(self):
        data = response()
        data["rateLimits"]["primary"] = None
        data["rateLimitsByLimitId"]["codex"]["primary"] = None
        with self.assertRaisesRegex(QuotaError, "No applicable"):
            self.guard.update(data)
        guard = QuotaGuard(10, models={"gpt-6-astra", "gpt-6-luna"})
        with self.assertRaisesRegex(QuotaError, "No applicable"):
            guard.update(response(model="unrelated-model"))

    def test_explicit_model_mapping_can_exclude_unrelated_limit(self):
        data = response()
        data["rateLimitsByLimitId"]["unrelated"] = bucket(100, bucket_id="unrelated", model="other-model")
        guard = QuotaGuard(10, models={"gpt-6-astra", "gpt-6-luna"})
        guard.update(data)
        self.assertEqual(guard.check()["excluded_buckets"], ["unrelated"])
        data["rateLimitsByLimitId"]["unrelated"]["normalModelSlug"] = None
        guard.update(data)
        with self.assertRaises(QuotaReached):
            guard.check()

    def test_missing_mapping_never_excludes_a_bucket_by_its_name(self):
        data = response()
        data["rateLimitsByLimitId"]["spark"] = bucket(100, bucket_id="spark")
        self.guard.update(data)
        with self.assertRaises(QuotaReached):
            self.guard.check()

    def test_unknown_reset_and_duration_metadata_is_accepted(self):
        data = response()
        for meter in (data["rateLimits"], data["rateLimitsByLimitId"]["codex"]):
            meter["primary"]["windowDurationMins"] = None
            meter["primary"]["resetsAt"] = None
        self.guard.update(data)
        self.guard.check()

    def test_full_snapshot_replaces_removed_windows_without_inheriting(self):
        data = response(10, secondary={"usedPercent": 75, "windowDurationMins": 300, "resetsAt": 1})
        self.guard.update(data)
        self.assertEqual(self.guard.check()["min_remaining"], 25)
        self.guard.update(response(5))
        self.assertEqual(self.guard.check()["min_remaining"], 95)
        self.assertEqual(len(self.guard.snapshot()["windows"]), 1)

    def test_reset_is_reported_only_from_fresh_response(self):
        self.guard.update(response(60, reset=1000))
        self.now += 1
        result = self.guard.update(response(5, reset=2000))
        self.assertEqual(result["reset_count"], 1)
        self.assertEqual(result["reset_windows"][0]["previous_resets_at"], 1000)
        self.assertEqual(self.guard.check()["min_remaining"], 95)
        self.assertEqual(self.guard.update(response(6, reset=2000))["reset_windows"], [])

    def test_reaching_reserve_latches_stop_even_after_reset(self):
        self.guard.update(response(90, reset=1000))
        self.guard.update(response(0, reset=2000))
        with self.assertRaises(QuotaReached):
            self.guard.check()

    def test_reached_backend_state_cannot_be_cleared_with_nullable_metadata(self):
        data = response()
        for meter in (data["rateLimits"], data["rateLimitsByLimitId"]["codex"]):
            meter["spendControlReached"] = True
        self.guard.update(data)
        for meter in (data["rateLimits"], data["rateLimitsByLimitId"]["codex"]):
            meter["spendControlReached"] = None
        self.guard.update(data)
        with self.assertRaises(QuotaReached):
            self.guard.check()

    def test_changed_identity_is_permanently_rejected_even_if_switched_back(self):
        self.guard.update(response())
        changed = response()
        changed["accountId"] = "different-account"
        with self.assertRaisesRegex(QuotaError, "identity changed"):
            self.guard.update(changed)
        with self.assertRaises(QuotaError):
            self.guard.update(response())
        with self.assertRaises(QuotaError):
            self.guard.check()

    def test_prehashed_client_identity_works_across_new_readers(self):
        data = response()
        data.pop("accountId")
        data["_ceps_account_identity"] = "a" * 64
        self.guard.update(data)
        self.guard.update(copy.deepcopy(data))
        self.assertEqual(self.guard.check()["account_identity"], "a" * 64)

    def test_inconsistent_legacy_view_does_not_hide_a_reached_limit(self):
        for field, value in (("primary", {"usedPercent": 95}), ("spendControlReached", True),
                             ("rateLimitReachedType", "rate_limit_reached")):
            data = response()
            data["rateLimits"][field] = value
            with self.subTest(field=field), self.assertRaises(QuotaError):
                self.guard.update(data)

    def test_legacy_only_and_unlabelled_buckets_are_supported(self):
        data = response()
        data.pop("rateLimitsByLimitId")
        data["rateLimits"]["limitId"] = None
        self.guard.update(data)
        self.assertEqual(self.guard.check()["windows"][0]["bucket_id"], "__default__")

    def test_legacy_bucket_missing_from_map_is_still_protected(self):
        data = response(90)
        data["rateLimitsByLimitId"] = {"luna": bucket(10, bucket_id="luna")}
        self.guard.update(data)
        with self.assertRaises(QuotaReached):
            self.guard.check()

    def test_malformed_snapshot_invalidates_previous_good_sample(self):
        malformed = [None, [], {}, {"rateLimits": bucket()},
                     {**response(), "rateLimitsByLimitId": []},
                     {**response(), "accountId": None},
                     {**response(), "_ceps_account_identity": "not-a-hash"}]
        for bad in malformed:
            guard = QuotaGuard(10)
            guard.update(response())
            with self.subTest(bad=bad):
                with self.assertRaises(QuotaError):
                    guard.update(bad)
                with self.assertRaises(QuotaError):
                    guard.check()
                self.assertFalse(guard.snapshot()["valid"])

    def test_invalid_window_numbers_and_partial_snapshots_are_rejected(self):
        cases = [("usedPercent", x) for x in (None, "30", True, -1, float("nan"), float("inf"), 10 ** 500)]
        cases += [("resetsAt", -1), ("resetsAt", 1.5), ("windowDurationMins", 0), ("windowDurationMins", False)]
        for field, value in cases:
            data = response()
            for meter in (data["rateLimits"], data["rateLimitsByLimitId"]["codex"]):
                meter["primary"][field] = value
            with self.subTest(field=field, value=str(value)), self.assertRaises(QuotaError):
                self.guard.update(data)
        data = response()
        del data["rateLimits"]["secondary"]
        del data["rateLimitsByLimitId"]["codex"]["secondary"]
        with self.assertRaisesRegex(QuotaError, "Incomplete"):
            self.guard.update(data)

    def test_snapshots_do_not_allow_callers_to_mutate_the_guard(self):
        data = response()
        returned = self.guard.update(data)
        returned["windows"][0]["remaining"] = 0
        data["rateLimits"]["primary"]["usedPercent"] = 99
        self.assertEqual(self.guard.check()["min_remaining"], 90)

    def test_configuration_validation_and_full_reserve(self):
        for args in ((-1,), (101,), (float("nan"),), (True,), (10, -1), (10, 2, 0)):
            with self.subTest(args=args), self.assertRaises(QuotaError):
                QuotaGuard(*args)
        for models in ([], "gpt-6-luna", [""]):
            with self.subTest(models=models), self.assertRaises(QuotaError):
                QuotaGuard(10, models=models)
        guard = QuotaGuard(100)
        guard.update(response(0))
        with self.assertRaises(QuotaReached):
            guard.check()


FAKE_CODEX = r'''
import json, os, signal, subprocess, sys, time
from pathlib import Path
mode = os.environ.get("FAKE_MODE", "ok")
log = Path(os.environ["FAKE_LOG"])
child = None
if mode == "child":
    child = subprocess.Popen([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"])
    Path(os.environ["FAKE_CHILD"]).write_text(str(child.pid))
calls = 0
for line in sys.stdin:
    value = json.loads(line)
    with log.open("a") as stream: stream.write(json.dumps(value) + "\n")
    method = value["method"]
    if "id" not in value: continue
    if method == "initialize": result = {}
    elif method == "account/read":
        calls += 1
        email = "other@example.test" if mode == "other_account" or (mode == "switch" and calls >= 3) else "fixture@example.test"
        result = {"account":{"type":"chatgpt","email":email,"planType":"pro"}, "requiresOpenaiAuth":True,
                  "workspaceRouting":{"chatgptAccountId":"fixture-account"}}
        if mode == "other_workspace": result["workspaceRouting"]["chatgptAccountId"] = "other-workspace"
        if mode == "apikey": result["account"] = {"type":"apiKey"}
    elif method == "account/rateLimits/read":
        if mode == "timeout": time.sleep(60)
        if mode == "eof": sys.exit(0)
        if mode == "malformed": print("not json", flush=True); continue
        if mode == "oversized": print("x" * (2 * 1024 * 1024 + 10), flush=True); continue
        if mode == "rpc_error":
            print(json.dumps({"id":value["id"],"error":{"message":"sensitive-server-diagnostic"}}), flush=True); continue
        if mode == "interactive":
            print(json.dumps({"id":999,"method":"account/chatgptAuthTokens/refresh","params":{}}),flush=True); continue
        meter = {"limitId":"codex", "limitName":None,"normalModelSlug":None,
                 "primary":{"usedPercent":10,"windowDurationMins":10080,"resetsAt":2000000000},
                 "secondary":None,"spendControlReached":False,"rateLimitReachedType":None}
        result = {"ordinaryUsageAllowed":True,"accountId":"fixture-account", "rateLimits":meter,
                  "rateLimitsByLimitId":{"codex":meter}, "rateLimitUpsell":{"sensitive":"private"}}
        if mode == "other_quota": result["accountId"] = "other-account"
        print(json.dumps({"method":"account/rateLimits/updated","params":{"rateLimits":{"primary":{"usedPercent":99}}}}), flush=True)
    elif method == "model/list":
        cursor = value["params"].get("cursor")
        if mode == "bad_cursor": result = {"data":[],"nextCursor":"repeated"}
        else: result = {"data":[{"id":"astra" if cursor is None else "luna"}],"nextCursor":"next" if cursor is None else None}
    else: raise AssertionError("Unexpected method: " + method)
    answer = {"id": value["id"] + (1 if mode == "bad_id" else 0), "result":result}
    encoded = json.dumps(answer) + "\n"
    if mode == "split":
        sys.stdout.write(encoded[:7]); sys.stdout.flush(); time.sleep(.005)
        sys.stdout.write(encoded[7:]); sys.stdout.flush()
    else: sys.stdout.write(encoded); sys.stdout.flush()
'''


class AccountClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.executable = self.root / "codex"
        self.executable.write_text(f"#!{sys.executable}\n" + FAKE_CODEX)
        self.executable.chmod(0o700)
        self.log = self.root / "rpc.jsonl"

    def client(self, mode="ok", *, timeout=2):
        env = {**os.environ, "FAKE_MODE": mode, "FAKE_LOG": str(self.log), "FAKE_CHILD": str(self.root / "child.pid")}
        client = AccountClient(str(self.executable), self.root, env, timeout=timeout)
        self.addCleanup(client.close)
        return client

    def records(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_read_only_handshake_account_verification_and_sanitized_identity(self):
        client = self.client()
        limits = client.read_limits()
        self.assertNotIn("accountId", limits)
        self.assertNotIn("rateLimitUpsell", limits)
        self.assertEqual(len(limits["_ceps_account_identity"]), 64)
        guard = QuotaGuard(10)
        guard.update(limits)
        self.assertEqual(guard.check()["min_remaining"], 90)
        calls = self.records()
        self.assertEqual([x["method"] for x in calls], ["initialize", "initialized", "account/read",
                         "account/read", "account/rateLimits/read", "account/read"])
        request = next(x for x in calls if x["method"] == "account/rateLimits/read")
        self.assertEqual(request["params"], {"excludeResetCreditDetails": True, "supportsLunaReserve": False})
        self.assertFalse(any(x["method"].startswith(("turn/", "thread/")) for x in calls))

    def test_sparse_notifications_are_ignored_and_fragmented_json_is_read(self):
        limits = self.client("split").read_limits()
        self.assertEqual(limits["rateLimits"]["primary"]["usedPercent"], 10)

    def test_client_reopen_preserves_identity_and_starts_new_owned_process(self):
        client = self.client()
        first = client.read_limits()
        pid = client._process.pid
        client.close()
        self.assertIsNone(client._process)
        second = client.read_limits()
        self.assertNotEqual(client._process.pid, pid)
        self.assertEqual(first["_ceps_account_identity"], second["_ceps_account_identity"])

    def test_authentication_and_racing_identity_changes_are_rejected(self):
        for mode in ("apikey", "switch"):
            with self.subTest(mode=mode), self.assertRaises(QuotaError) as error:
                self.client(mode).read_limits()
            self.assertNotIsInstance(error.exception, RetryableQuotaError)

    def test_reconnecting_cannot_change_the_pinned_account_identity(self):
        for mode in ("other_account", "other_workspace", "other_quota"):
            with self.subTest(mode=mode):
                client = self.client()
                client.read_limits()
                client.close()
                client.env["FAKE_MODE"] = mode
                with self.assertRaisesRegex(QuotaError, "changed") as error:
                    client.read_limits()
                self.assertNotIsInstance(error.exception, RetryableQuotaError)

    def test_bad_protocol_cannot_be_used_as_fresh_quota(self):
        for mode in ("malformed", "oversized", "rpc_error", "interactive", "bad_id"):
            with self.subTest(mode=mode), self.assertRaises(QuotaError) as error:
                self.client(mode).read_limits()
            self.assertNotIn("sensitive-server-diagnostic", str(error.exception))
            self.assertNotIsInstance(error.exception, RetryableQuotaError)

    def test_server_exit_is_retryable_and_reconnection_recovers(self):
        client = self.client("eof")
        with self.assertRaisesRegex(RetryableQuotaError, "exited before replying"):
            client.read_limits()
        client._process.wait(timeout=2)
        with self.assertRaisesRegex(RetryableQuotaError, "no longer running"):
            client.read_limits()
        client.close()
        client.env["FAKE_MODE"] = "ok"
        self.assertEqual(client.read_limits()["rateLimits"]["primary"]["usedPercent"], 10)

    def test_closed_account_pipes_are_retryable(self):
        client = self.client()
        client.start()
        with mock.patch("tools.ceps_quota.os.write", side_effect=BrokenPipeError):
            with self.assertRaisesRegex(RetryableQuotaError, "closed its input"):
                client.read_limits()
        client.close()
        client.start()
        with mock.patch("tools.ceps_quota.os.read", side_effect=ConnectionResetError):
            with self.assertRaisesRegex(RetryableQuotaError, "closed its output"):
                client.read_limits()

    def rpc_failure(self, error):
        client = self.client()
        reply = {"id": 1, "error": error}
        with mock.patch.object(client, "_send"), mock.patch.object(client, "_receive", return_value=reply):
            with self.assertRaises(QuotaError) as failure:
                client._request("account/rateLimits/read", {}, time.monotonic() + 1)
        return failure.exception

    def test_transient_rpc_errors_preserve_safe_diagnostics(self):
        cases = [
            ({"code": -32603, "message": "internal failure"}, "internal server failure"),
            ({"code": -32000, "message": "sensitive-server-diagnostic"}, "internal server failure"),
            ({"code": 503, "message": "private"}, "HTTP 503"),
            ({"code": 429, "message": "private"}, "HTTP 429"),
            ({"code": -32099, "message": "connection reset"}, "temporary connection"),
            ({"code": -32099, "message": "request timed out"}, "request timeout"),
            ({"code": -32099, "message": "private", "data": {"httpStatusCode": 408}}, "HTTP 408"),
            ({"code": -32000, "message": "private upstream HTTP500 error"}, "HTTP 500"),
            ({"code": -32000, "message": "HTTP status server error (502 Bad Gateway) for url (private)"}, "HTTP 502"),
        ]
        for rpc, diagnostic in cases:
            with self.subTest(rpc=rpc):
                error = self.rpc_failure(rpc)
                self.assertIsInstance(error, RetryableQuotaError)
                self.assertIn(f"code {rpc['code']}", str(error))
                self.assertIn(diagnostic, str(error))
                self.assertNotIn("sensitive-server-diagnostic", str(error))
                self.assertNotIn("private", str(error))

    def test_authentication_and_invalid_rpc_requests_are_not_retried(self):
        cases = [
            {"code": -32000, "message": "Authentication required"},
            {"code": -32603, "message": "refresh token expired"},
            {"code": -32000, "message": "prefix " * 1000 + " authentication required"},
            {"code": -32000, "message": "private", "data": {"httpStatusCode": 401}},
            {"code": -32000, "message": "private", "data": {"statusCode": 403}},
            {"code": -32000, "message": "private", "data": {"status": 400}},
            {"code": -32000, "message": "HTTP status 401: private"},
            {"code": -32000, "message": "HTTP status client error (404 Not Found) for url (private)"},
            {"code": -32602, "message": "temporary failure"},
            {"code": -32700, "message": "private"},
            {"code": -32000, "message": "invalid parameters"},
            {"code": -32000, "message": "unsupported configuration"},
            {"code": -32099, "message": "private"},
        ]
        for rpc in cases:
            with self.subTest(rpc=rpc):
                self.assertNotIsInstance(self.rpc_failure(rpc), RetryableQuotaError)

    def test_rpc_diagnostics_never_include_arbitrary_server_content(self):
        secrets = "fixture@example.test sk-private fixture-account sensitive-server-diagnostic\n\x1b[31m"
        for rpc in (
            {"code": -32000, "message": secrets * 1000, "data": {"token": secrets, "statusCode": 503}},
            {"code": -32000, "message": secrets, "data": {"statusCode": secrets}},
            {"code": -32000, "message": "; ".join(f"HTTP {status}" for status in range(100, 600))},
            {"code": secrets, "message": secrets},
            {"code": True, "message": secrets},
            {"code": 10 ** 100, "message": secrets},
            {"code": -32000, "message": {"token": secrets}},
            None,
        ):
            with self.subTest(rpc_type=type(rpc).__name__):
                error = self.rpc_failure(rpc)
                text = str(error)
                self.assertLess(len(text), 200)
                for secret in secrets.split():
                    self.assertNotIn(secret, text)
                self.assertNotIn("\n", text)
                self.assertNotIn("\x1b", text)

    def test_interactive_authentication_stays_fatal_if_rejection_cannot_be_sent(self):
        client = self.client()
        reply = {"id": 999, "method": "account/chatgptAuthTokens/refresh", "params": {}}
        with (mock.patch.object(client, "_send", side_effect=[None, RetryableQuotaError("closed")]),
              mock.patch.object(client, "_receive", return_value=reply)):
            with self.assertRaisesRegex(QuotaError, "interactive authentication") as error:
                client._request("account/rateLimits/read", {}, time.monotonic() + 1)
        self.assertNotIsInstance(error.exception, RetryableQuotaError)

    def test_response_cannot_have_both_result_and_retryable_error(self):
        client = self.client()
        reply = {"id": 1, "result": {}, "error": {"code": -32000, "message": "internal error"}}
        with mock.patch.object(client, "_send"), mock.patch.object(client, "_receive", return_value=reply):
            with self.assertRaisesRegex(QuotaError, "invalid RPC response") as error:
                client._request("account/rateLimits/read", {}, time.monotonic() + 1)
        self.assertNotIsInstance(error.exception, RetryableQuotaError)

    def test_timeout_and_shutdown_are_bounded(self):
        client = self.client("timeout", timeout=0.2)
        started = time.monotonic()
        with self.assertRaisesRegex(RetryableQuotaError, "timed out"):
            client.read_limits()
        process = client._process
        client.close()
        self.assertIsNotNone(process.returncode)
        self.assertLess(time.monotonic() - started, 4)

    def test_close_terminates_owned_helpers_even_if_parent_exits_first(self):
        client = self.client("child")
        client.read_limits()
        pid = int((self.root / "child.pid").read_text())
        client.close()
        # A killed child can briefly remain as an adopted zombie; it cannot write.
        for _ in range(100):
            stat_path = Path(f"/proc/{pid}/stat")
            if not stat_path.exists() or stat_path.read_text().split(")", 1)[1].strip().startswith("Z"):
                break
            time.sleep(0.01)
        else:
            os.kill(pid, signal.SIGKILL)
            self.fail("Account-client helper survived owned process-group cleanup")

    def test_catalog_pages_are_read_without_starting_a_model(self):
        result = self.client().model_catalog()
        self.assertEqual([x["id"] for x in result["data"]], ["astra", "luna"])
        calls = self.records()
        self.assertEqual([x["method"] for x in calls].count("model/list"), 2)
        self.assertTrue(all(x["params"]["includeHidden"] for x in calls if x["method"] == "model/list"))

    def test_repeating_catalog_cursor_is_rejected(self):
        with self.assertRaisesRegex(QuotaError, "pagination"):
            self.client("bad_cursor").model_catalog()

    def test_unstartable_binary_is_a_quota_error(self):
        client = AccountClient(str(self.root / "missing"), self.root)
        with self.assertRaisesRegex(QuotaError, "Cannot start") as error:
            client.start()
        self.assertNotIsInstance(error.exception, RetryableQuotaError)
        client.close()


class AccountFingerprintTests(unittest.TestCase):
    def test_workspace_identity_changes_even_when_email_stays_the_same(self):
        account = {"account": {"type": "chatgpt", "email": "same@example.test"},
                   "requiresOpenaiAuth": True, "workspaceRouting": {"chatgptAccountId": "one"}}
        first = account_fingerprint(account, {"accountId": "one"})
        account["workspaceRouting"]["chatgptAccountId"] = "two"
        self.assertNotEqual(first, account_fingerprint(account, {"accountId": "two"}))

    def test_quota_identity_can_bind_an_account_without_email(self):
        account = {"account": {"type": "chatgpt", "email": None}, "requiresOpenaiAuth": True}
        self.assertEqual(len(account_fingerprint(account, {"accountId": "one"})), 64)
        with self.assertRaisesRegex(QuotaError, "bound"):
            account_fingerprint(account, {})


if __name__ == "__main__":
    unittest.main()
