import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
// No TypeScript runtime is needed: the extension intentionally contains only
// JavaScript syntax, and a data URL avoids Node treating its .ts suffix specially.
const source = readFileSync(new URL("../tools/spec-budget.ts", import.meta.url), "utf8");
const { default: specBudget, BudgetLedger } = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);

const model = {
  provider: "deepseek", api: "openai-completions", contextWindow: 1_000_000,
  baseUrl: "https://api.deepseek.com",
  maxTokens: 384_000, cost: { input: 0.3, output: 1.2, cacheRead: 0.006, cacheWrite: 0 },
};
const payload = { messages: [{ role: "user", content: "Review the specification" }], max_tokens: 384_000 };
function fixture(t, budget = 2) {
  const dir = mkdtempSync(join(tmpdir(), "spec-budget-test-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const path = join(dir, "budget.json");
  return { path, ledger: new BudgetLedger(budget, path) };
}
const result = cost => ({ role: "assistant", stopReason: "stop", usage: { cost: { total: cost }, totalTokens: 100 } });
function extension(t, path, selectedModel = model) {
  const previous = { state: process.env.SPEC_BUDGET_STATE, usd: process.env.SPEC_BUDGET_USD };
  const handlers = new Map();
  process.env.SPEC_BUDGET_STATE = path;
  process.env.SPEC_BUDGET_USD = "2.00";
  try {
    specBudget({ on: (event, callback) => handlers.set(event, callback) });
  } finally {
    if (previous.state === undefined) delete process.env.SPEC_BUDGET_STATE;
    else process.env.SPEC_BUDGET_STATE = previous.state;
    if (previous.usd === undefined) delete process.env.SPEC_BUDGET_USD;
    else process.env.SPEC_BUDGET_USD = previous.usd;
  }
  t.after(() => {
    const shared = globalThis[Symbol.for("pixels.spec.budget.v1")];
    globalThis[Symbol.for("pixels.spec.fetch.v1")].ledgers.delete(shared.get(path));
    shared.delete(path);
  });
  const ctx = {
    model: selectedModel, sessionManager: { getSessionId: () => "parent" },
    aborted: false, abort() { this.aborted = true; },
  };
  return { handlers, ctx };
}

test("reserves before transmission, caps output, and settles provider cost", t => {
  const { ledger, path } = fixture(t);
  const bounded = ledger.before("parent", model, payload);
  assert.equal(bounded.max_tokens, 16_384);
  assert.equal(bounded.stream_options.include_usage, true);
  assert.ok(ledger.snapshot().reserved > 0);
  assert.equal(JSON.parse(readFileSync(path)).inFlight, 1);
  ledger.settle("parent", result(0.001));
  assert.equal(ledger.snapshot().spent, 0.001);
  assert.equal(ledger.snapshot().reserved, 0);
  assert.equal(ledger.snapshot().requests, 1);
});

test("parent and children share reservations and costs without tool-result double counting", t => {
  const { ledger } = fixture(t);
  ledger.before("parent", model, payload);
  ledger.before("child", model, payload);
  assert.equal(ledger.snapshot().inFlight, 2);
  ledger.settle("child", result(0.002));
  ledger.settle("parent", { role: "toolResult", details: { usage: result(0.002).usage } });
  ledger.settle("parent", result(0.001));
  assert.equal(ledger.snapshot().spent, 0.003);
  assert.equal(ledger.snapshot().inFlight, 0);
});

test("stops before $1 reserve and does not reserve a rejected request", t => {
  const { ledger } = fixture(t);
  ledger.spent = 999_999_000;
  assert.throws(() => ledger.before("parent", model, payload), /reserve/);
  assert.equal(ledger.snapshot().stopped, true);
  assert.equal(ledger.snapshot().stopKind, "budget");
  assert.equal(ledger.snapshot().retryable, false);
  assert.equal(ledger.snapshot().requests, 0);
  assert.ok(ledger.snapshot().remaining >= 1);
});

test("reduces output allowance close to the boundary", t => {
  const { ledger } = fixture(t);
  ledger.spent = 995_000_000;
  const bounded = ledger.before("parent", model, payload);
  assert.ok(bounded.max_tokens >= 256 && bounded.max_tokens < 16_384);
  assert.ok(ledger.snapshot().reserved <= 0.005);
});

test("missing usage and retry both preserve the outstanding maximum and stop", t => {
  const { ledger } = fixture(t);
  ledger.before("parent", model, payload);
  const reserved = ledger.snapshot().reserved;
  ledger.settle("parent", { role: "assistant", stopReason: "error" });
  assert.equal(ledger.snapshot().spent, reserved);
  assert.equal(ledger.snapshot().estimatedSpend, true);
  assert.equal(ledger.snapshot().stopped, true);
  assert.equal(ledger.snapshot().retryable, true);
  const other = fixture(t).ledger;
  other.before("child", model, payload);
  assert.throws(() => other.before("child", model, payload), /retried/);
  assert.equal(other.snapshot().stopped, true);
  assert.equal(other.snapshot().retryable, true);
});

test("clean process restarts keep totals; incomplete restarts charge outstanding maximum", t => {
  const { ledger, path } = fixture(t);
  ledger.before("parent", model, payload);
  ledger.settle("parent", result(0.002));
  const continued = new BudgetLedger(2, path);
  assert.equal(continued.snapshot().spent, 0.002);
  continued.before("child", model, payload);
  const reserved = continued.snapshot().reserved;
  const interrupted = new BudgetLedger(2, path);
  assert.ok(Math.abs(interrupted.snapshot().spent - 0.002 - reserved) < 1e-9);
  assert.equal(interrupted.snapshot().stopped, true);
  assert.equal(interrupted.snapshot().retryable, true);
  assert.equal(interrupted.snapshot().inFlight, 0);
  assert.equal(interrupted.snapshot().settledRequests, interrupted.snapshot().requests);
});

test("retry requires an explicit rearm and preserves all cumulative budget totals", t => {
  const { ledger, path } = fixture(t);
  ledger.before("parent", model, payload);
  ledger.settle("parent", result(0.002));
  ledger.before("parent", model, payload);
  ledger.settle("parent", { role: "assistant", stopReason: "aborted" });
  const failed = ledger.snapshot();
  const stillStopped = new BudgetLedger(2, path);
  assert.throws(() => stillStopped.before("parent", model, payload), /usage was incomplete/);
  assert.equal(stillStopped.snapshot().requests, failed.requests);
  writeFileSync(path, JSON.stringify({
    ...stillStopped.snapshot(), stopped: false, stopKind: "", reason: "", retryable: false,
  }));
  const retry = new BudgetLedger(2, path);
  assert.equal(retry.snapshot().spent, failed.spent);
  assert.equal(retry.snapshot().requests, failed.requests);
  assert.equal(retry.snapshot().settledRequests, failed.settledRequests);
  assert.equal(retry.snapshot().estimatedSpend, true);
  retry.before("parent", model, payload);
  retry.settle("parent", result(0.003));
  assert.ok(Math.abs(retry.snapshot().spent - failed.spent - 0.003) < 1e-9);
  assert.equal(retry.snapshot().requests, failed.requests + 1);
  assert.equal(retry.snapshot().settledRequests, failed.settledRequests + 1);
});

test("incomplete usage keeps a bounded original provider error and remains retryable", t => {
  const { ledger } = fixture(t);
  ledger.before("parent", model, payload);
  ledger.settle("parent", {
    role: "assistant", stopReason: "error", errorMessage: `Loop detector stopped the model.\n${"x".repeat(800)}`,
  });
  const state = ledger.snapshot();
  assert.equal(state.retryable, true);
  assert.match(state.reason, /Loop detector stopped the model\. x/);
  assert.ok(state.reason.length < 650);
});

test("rearming a failed request cannot replenish the budget or spend the reserve", t => {
  const { ledger, path } = fixture(t);
  ledger.spent = 996_000_000;
  ledger.before("parent", model, payload);
  ledger.settle("parent", { role: "assistant", stopReason: "aborted" });
  const failed = ledger.snapshot();
  writeFileSync(path, JSON.stringify({ ...failed, stopped: false, stopKind: "", reason: "", retryable: false }));
  const retry = new BudgetLedger(2, path);
  assert.throws(() => retry.before("parent", model, payload), /reserve/);
  assert.equal(retry.snapshot().spent, failed.spent);
  assert.equal(retry.snapshot().requests, failed.requests);
  assert.equal(retry.snapshot().retryable, false);
  assert.equal(retry.snapshot().stopKind, "budget");
  assert.ok(retry.snapshot().remaining >= 1);
});

test("cost overruns stay nonretryable regardless of when other requests become uncertain", t => {
  for (const uncertaintyFirst of [false, true]) {
    const { ledger, path } = fixture(t);
    ledger.before("parent", model, payload);
    const maximum = ledger.snapshot().reserved;
    ledger.before("child", model, payload);
    if (uncertaintyFirst) ledger.uncertain("child", "Child aborted");
    ledger.settle("parent", result(maximum + 0.001));
    if (!uncertaintyFirst) ledger.uncertain("child", "Child aborted");
    const state = ledger.snapshot();
    assert.equal(state.retryable, false);
    assert.match(state.reason, /exceeded its conservative reservation/);
    assert.equal(state.inFlight, 0);
    const restarted = new BudgetLedger(2, path);
    assert.equal(restarted.snapshot().retryable, false);
    assert.throws(() => restarted.before("parent", model, payload), /exceeded/);
  }
});

test("a pending cost overrun supersedes a budget cutoff and survives later uncertainty", t => {
  const { ledger } = fixture(t);
  ledger.spent = 994_000_000;
  const smallRequest = { ...payload, max_tokens: 256 };
  ledger.before("parent", model, smallRequest);
  const maximum = ledger.snapshot().reserved;
  ledger.before("child", model, smallRequest);
  assert.throws(() => ledger.before("next", model, {
    messages: [{ role: "user", content: "x".repeat(10_000) }],
  }), /reserve/);
  assert.equal(ledger.snapshot().stopKind, "budget");
  ledger.settle("parent", result(maximum + 0.001));
  const fatal = ledger.snapshot();
  assert.equal(fatal.stopKind, "error");
  assert.equal(fatal.retryable, false);
  assert.match(fatal.reason, /exceeded its conservative reservation/);
  ledger.uncertain("child", "Child aborted after the budget cutoff");
  ledger.stop("A subsequent ordinary budget cutoff", "budget");
  ledger.stop("A subsequent accounting error");
  assert.equal(ledger.snapshot().stopKind, "error");
  assert.equal(ledger.snapshot().retryable, false);
  assert.equal(ledger.snapshot().reason, fatal.reason);
});

test("restart charges outstanding reservations without overriding an existing hard stop", t => {
  const { ledger, path } = fixture(t);
  ledger.before("child", model, payload);
  const maximum = ledger.snapshot().reserved;
  ledger.stop("Unsupported pricing");
  const restarted = new BudgetLedger(2, path);
  assert.equal(restarted.snapshot().spent, maximum);
  assert.equal(restarted.snapshot().retryable, false);
  assert.equal(restarted.snapshot().reason, "Unsupported pricing");
  const restartedAgain = new BudgetLedger(2, path);
  assert.equal(restartedAgain.snapshot().spent, maximum);
});

test("old stopped state without retry classification defaults to nonretryable", t => {
  const { ledger, path } = fixture(t);
  ledger.stop("Old failure");
  const oldState = ledger.snapshot();
  delete oldState.retryable;
  writeFileSync(path, JSON.stringify(oldState));
  assert.equal(new BudgetLedger(2, path).snapshot().retryable, false);
});

test("extension aborts and returns an unserializable payload when denied", t => {
  const { ledger, path } = fixture(t);
  ledger.spent = 999_999_000;
  ledger.persist();
  const { handlers, ctx } = extension(t, path);
  const blocked = handlers.get("before_provider_request")({ payload }, ctx);
  assert.equal(ctx.aborted, true);
  assert.throws(() => JSON.stringify(blocked), /SPEC_BUDGET_STOP/);
  assert.equal(JSON.parse(readFileSync(path)).stopped, true);
});

test("unknown pricing, non-text requests, and unsupported APIs are refused", t => {
  const { ledger } = fixture(t);
  assert.throws(() => ledger.before("parent", { ...model, provider: "unknown" }, payload), /DeepSeek/);
  assert.throws(() => ledger.before("parent", { ...model, cost: { ...model.cost, output: 0 } }, payload), /pricing/);
  assert.throws(() => ledger.before("parent", model, {
    messages: [{ role: "user", content: [{ type: "image_url", image_url: { url: "https://example.invalid/image.png" } }] }],
  }), /text requests/);
  assert.equal(ledger.snapshot().requests, 0);
});

test("extension records unsupported requests as nonretryable failures", t => {
  const denied = [
    [{ ...model, provider: "unknown" }, payload],
    [{ ...model, cost: { ...model.cost, output: 0 } }, payload],
    [model, { messages: "invalid" }],
    [model, { messages: [{ role: "user", content: [{ type: "image_url" }] }] }],
  ];
  for (const [selectedModel, request] of denied) {
    const { path } = fixture(t);
    const { handlers, ctx } = extension(t, path, selectedModel);
    const blocked = handlers.get("before_provider_request")({ payload: request }, ctx);
    assert.throws(() => JSON.stringify(blocked), /SPEC_BUDGET_STOP/);
    const state = JSON.parse(readFileSync(path));
    assert.equal(state.stopped, true);
    assert.equal(state.retryable, false);
    assert.equal(state.requests, 0);
  }
});

test("transport failures and blocked automatic retries permit a supervised round retry", async t => {
  for (const failure of ["transport", "repeat"]) {
    const { path } = fixture(t);
    const { handlers, ctx } = extension(t, path);
    const bounded = handlers.get("before_provider_request")({ payload }, ctx);
    const reserved = JSON.parse(readFileSync(path)).reserved;
    const gate = globalThis[Symbol.for("pixels.spec.fetch.v1")];
    const native = gate.native;
    let sent = 0;
    gate.native = async () => {
      sent += 1;
      if (failure === "transport") throw new Error("Connection closed unexpectedly");
      return new Response("{}", { status: 200 });
    };
    try {
      const send = () => fetch(`${model.baseUrl}/chat/completions`, { method: "POST", body: JSON.stringify(bounded) });
      if (failure === "transport") await assert.rejects(send, /provider transport failed/);
      else {
        await send();
        assert.equal((await send()).status, 402);
      }
    } finally {
      gate.native = native;
    }
    const state = JSON.parse(readFileSync(path));
    assert.equal(sent, 1);
    assert.equal(state.stopped, true);
    assert.equal(state.stopKind, "error");
    assert.equal(state.retryable, true);
    assert.equal(state.spent, reserved);
    assert.equal(state.reserved, 0);
    assert.equal(state.estimatedSpend, true);
    assert.equal(ctx.aborted, true);
  }
});
