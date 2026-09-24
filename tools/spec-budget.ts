// Oh My Pi 18.2.7 extension. This file deliberately uses JavaScript syntax so
// its accounting can also be exercised with Node's built-in test runner.
import { readFileSync, writeFileSync, renameSync } from "node:fs";
import { createHash } from "node:crypto";

const NANO = 1_000_000_000;
const RESERVE = NANO;
const MAX_OUTPUT_TOKENS = 16_384;
const MIN_OUTPUT_TOKENS = 256;
const SHARED_KEY = Symbol.for("pixels.spec.budget.v1");
const FETCH_KEY = Symbol.for("pixels.spec.fetch.v1");

function fingerprint(body) {
  return createHash("sha256").update(body).digest("hex");
}

function nano(value) {
  if (!Number.isFinite(value) || value < 0) throw new Error("Invalid cost in budget accounting");
  const result = Math.ceil(value * NANO - 1e-7);
  if (!Number.isSafeInteger(result)) throw new Error("Budget is too large to account for safely");
  return result;
}

function usd(value) {
  return value / NANO;
}

export class BudgetLedger {
  constructor(budget, statePath) {
    this.path = statePath;
    this.budget = nano(budget);
    if (this.budget < 2 * NANO) throw new Error("The minimum budget is $2.00");
    this.spent = 0;
    this.requests = 0;
    this.settledRequests = 0;
    this.pending = new Map();
    this.stopped = false;
    this.stopKind = "";
    this.retryable = false;
    this.reason = "";
    this.estimatedSpend = false;
    this.blockedAuxiliaryRequests = 0;
    this.contexts = new Map();
    let previous;
    try {
      previous = JSON.parse(readFileSync(statePath, "utf8"));
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
    }
    if (previous) {
      if (previous.version !== 1 || nano(previous.limit) !== this.budget || previous.reserve !== 1) {
        throw new Error("Budget state does not match this run");
      }
      this.spent = nano(previous.spent);
      this.requests = previous.requests;
      this.settledRequests = previous.settledRequests;
      this.stopped = previous.stopped;
      this.stopKind = previous.stopKind || "";
      this.retryable = previous.retryable === true;
      this.reason = previous.reason;
      this.estimatedSpend = previous.estimatedSpend;
      this.blockedAuxiliaryRequests = previous.blockedAuxiliaryRequests || 0;
      if (previous.reserved > 0) {
        this.spent += nano(previous.reserved);
        this.settledRequests += previous.inFlight;
        this.estimatedSpend = true;
        this.stop("A previous process ended before reporting request usage", "error", true);
      }
    }
    this.persist();
  }

  snapshot() {
    const reserved = [...this.pending.values()].reduce((sum, request) => sum + request.amount, 0);
    return {
      version: 1,
      pid: process.pid,
      limit: usd(this.budget),
      reserve: 1,
      spent: usd(this.spent),
      reserved: usd(reserved),
      remaining: usd(this.budget - this.spent),
      available: usd(Math.max(0, this.budget - RESERVE - this.spent - reserved)),
      requests: this.requests,
      settledRequests: this.settledRequests,
      inFlight: this.pending.size,
      stopped: this.stopped,
      stopKind: this.stopKind,
      retryable: this.retryable,
      reason: this.reason,
      estimatedSpend: this.estimatedSpend,
      blockedAuxiliaryRequests: this.blockedAuxiliaryRequests,
      updatedAt: new Date().toISOString(),
    };
  }

  persist() {
    const temporary = `${this.path}.${process.pid}.tmp`;
    writeFileSync(temporary, `${JSON.stringify(this.snapshot())}\n`, { mode: 0o600 });
    renameSync(temporary, this.path);
  }

  stop(reason, kind = "error", retryable = false) {
    // Child requests may settle after another request stopped the run. An
    // uncertain response must never make a pricing/accounting failure retryable.
    // Accounting failures also supersede an ordinary budget cutoff.
    const priority = retryable ? 0 : kind === "budget" ? 1 : 2;
    const previousPriority = this.retryable ? 0 : this.stopKind === "budget" ? 1 : 2;
    if (!this.stopped || priority > previousPriority) {
      this.stopKind = kind;
      this.reason = reason;
      this.retryable = retryable;
    }
    this.stopped = true;
    this.persist();
  }

  remember(ctx) {
    const id = ctx.sessionManager.getSessionId();
    this.contexts.set(id, ctx);
    return id;
  }

  abortAll() {
    for (const ctx of this.contexts.values()) {
      try { ctx.abort(); } catch { /* The rejected payload also blocks transmission. */ }
    }
  }

  before(sessionId, model, original) {
    if (this.stopped) throw new Error(this.reason || "Specification budget stopped");
    if (this.pending.has(sessionId)) {
      this.uncertain(sessionId, "A request retried before reporting its usage");
      throw new Error(this.reason);
    }
    // The installed machine uses DeepSeek's text Chat Completions endpoint.
    // Reject other routes instead of silently estimating incompatible billing.
    if (model?.provider !== "deepseek" || model.api !== "openai-completions") {
      throw new Error("Budget enforcement requires a DeepSeek Chat Completions model");
    }
    const rates = model.cost;
    if (!rates || ![rates.input, rates.output, rates.cacheRead, rates.cacheWrite].every(Number.isFinite) ||
        rates.input <= 0 || rates.output <= 0 || rates.cacheRead < 0 || rates.cacheWrite < 0 ||
        !Number.isSafeInteger(model.contextWindow) || model.contextWindow <= 0) {
      throw new Error("Model has no usable dollar pricing or context limit");
    }
    if (!original || !Array.isArray(original.messages) || original.n > 1) {
      throw new Error("Unsupported provider request shape for budget enforcement");
    }
    for (const message of original.messages) {
      if (Array.isArray(message.content) && message.content.some(part => part.type !== "text")) {
        throw new Error("Budget enforcement only supports text requests");
      }
    }
    const payload = { ...original };
    // UTF-8 bytes upper-bound text token count; the extra allowance covers
    // message framing. Count JSON tool schemas too, and assume no cache hits.
    const inputBound = Math.min(model.contextWindow,
      Buffer.byteLength(JSON.stringify(payload), "utf8") + 4096 + 256 * payload.messages.length);
    const inputRate = Math.max(rates.input, rates.cacheRead, rates.cacheWrite);
    const inputCost = nano(inputBound * inputRate / 1_000_000);
    const held = [...this.pending.values()].reduce((sum, request) => sum + request.amount, 0);
    const available = this.budget - RESERVE - this.spent - held;
    const affordableOutput = Math.floor((available - inputCost) * 1_000_000 / (NANO * rates.output));
    const requestedOutput = payload.max_completion_tokens ?? payload.max_tokens ?? model.maxTokens;
    const maxOutput = Math.min(MAX_OUTPUT_TOKENS, model.maxTokens || MAX_OUTPUT_TOKENS,
      Number.isFinite(requestedOutput) && requestedOutput > 0 ? requestedOutput : MAX_OUTPUT_TOKENS,
      affordableOutput);
    if (maxOutput < MIN_OUTPUT_TOKENS) {
      this.stop("Stopped before the next request would use the $1.00 reserve", "budget");
      throw new Error(this.reason);
    }
    if ("max_completion_tokens" in payload) payload.max_completion_tokens = Math.floor(maxOutput);
    else payload.max_tokens = Math.floor(maxOutput);
    // Avoid two conflicting output limits on compatible providers.
    if ("max_completion_tokens" in payload) delete payload.max_tokens;
    payload.stream_options = { ...payload.stream_options, include_usage: true };
    const amount = inputCost + nano(Math.floor(maxOutput) * rates.output / 1_000_000);
    if (amount > available) {
      this.stop("Stopped before the next request would use the $1.00 reserve", "budget");
      throw new Error(this.reason);
    }
    const endpoint = new URL(`${model.baseUrl.replace(/\/+$/, "")}/chat/completions`).href;
    this.pending.set(sessionId, { amount, endpoint, fingerprint: fingerprint(JSON.stringify(payload)), sent: false });
    this.requests += 1;
    this.persist();
    return payload;
  }

  uncertain(sessionId, reason) {
    const request = this.pending.get(sessionId);
    if (request) {
      this.pending.delete(sessionId);
      this.spent += request.amount;
      this.settledRequests += 1;
      this.estimatedSpend = true;
    }
    this.stop(reason, "error", true);
  }

  settle(sessionId, message) {
    if (message.role !== "assistant") return;
    const request = this.pending.get(sessionId);
    if (!request) return; // Includes the synthetic error after a denied request.
    const usage = message.usage;
    if (message.stopReason === "error" || message.stopReason === "aborted" ||
        !usage || !Number.isFinite(usage.cost?.total) || usage.cost.total < 0 ||
        !Number.isFinite(usage.totalTokens) || usage.totalTokens <= 0 || usage.cost.total === 0) {
      const detail = typeof message.errorMessage === "string"
        ? message.errorMessage.replace(/\s+/g, " ").trim().slice(0, 500) : "";
      this.uncertain(sessionId, "Request usage was incomplete; its reserved maximum was charged conservatively" +
        (detail ? `: ${detail}` : ""));
      return;
    }
    const actual = nano(usage.cost.total);
    this.pending.delete(sessionId);
    this.spent += actual;
    this.settledRequests += 1;
    if (actual > request.amount) {
      this.stop("Reported request cost exceeded its conservative reservation; stopped for review");
    } else {
      this.persist();
    }
  }
}

function installFetchGate(ledger) {
  // OMP's transport retries below before_provider_request (six attempts by
  // default), even with retry.enabled=false. Require a one-use admission for
  // each actual HTTP request, so a connection failure cannot multiply spending.
  let gate = globalThis[FETCH_KEY];
  if (!gate) {
    gate = { native: globalThis.fetch, ledgers: new Set() };
    globalThis[FETCH_KEY] = gate;
    globalThis.fetch = async (input, options) => {
      const url = new URL(typeof input === "string" || input instanceof URL ? input : input.url);
      const method = String(options?.method ?? input?.method ?? "GET").toUpperCase();
      const modelEndpoint = /(?:\/(?:chat\/completions|responses|messages)|[:/](?:generateContent|streamGenerateContent))\/?$/.test(url.pathname);
      if (method !== "POST" || !modelEndpoint) {
        return gate.native(input, options);
      }
      let admission;
      let repeated;
      const hash = typeof options?.body === "string" ? fingerprint(options.body) : undefined;
      for (const candidate of gate.ledgers) {
        if (candidate.stopped) continue;
        for (const [sessionId, pending] of candidate.pending) {
          if (pending.endpoint === url.href && pending.fingerprint === hash) {
            if (pending.sent) repeated = { ledger: candidate, sessionId };
            else {
              admission = { ledger: candidate, sessionId, pending };
              break;
            }
          }
        }
        if (admission) break;
      }
      if (!admission) {
        if (repeated) {
          try { repeated.ledger.uncertain(repeated.sessionId, "Blocked an automatic provider transport retry"); } catch {}
          repeated.ledger.abortAll();
        } else {
          // Subagent labels use completeSimple directly, bypassing provider
          // hooks even with --no-title. Decline optional unbudgeted requests
          // locally; their callers can use the normal no-label fallback.
          for (const candidate of gate.ledgers) {
            candidate.blockedAuxiliaryRequests += 1;
            try { candidate.persist(); } catch { candidate.abortAll(); }
          }
        }
        return new Response(JSON.stringify({ error: { message: "SPEC_BUDGET_STOP: no unused request admission" } }), {
          status: 402, headers: { "Content-Type": "application/json" },
        });
      }
      const { ledger: owner, sessionId, pending } = admission;
      pending.sent = true;
      try {
        const response = await gate.native(input, { ...options, redirect: "error" });
        if (!response.ok) throw new Error(`Provider returned HTTP ${response.status}`);
        return response;
      } catch {
        // The provider may already have processed a failed connection. Keep
        // the request's full allowance rather than treating its cost as zero.
        try { owner.uncertain(sessionId, "Provider transport failed; its reserved maximum was charged and retries blocked"); } catch {}
        owner.abortAll();
        throw new Error("SPEC_BUDGET_STOP: provider transport failed; no retry permitted");
      }
    };
  }
  gate.ledgers.add(ledger);
}

function blockedPayload(reason) {
  // OMP logs and swallows extension-handler exceptions. Returning a payload
  // whose serialization fails blocks fetch even for auxiliary requests with a
  // different abort signal. The Chat Completions transport stringifies before
  // calling fetch. Normal agent requests are also aborted synchronously.
  return { toJSON() { throw new Error(`SPEC_BUDGET_STOP: ${reason}`); } };
}

export default function specBudget(pi) {
  const statePath = process.env.SPEC_BUDGET_STATE;
  const budget = Number(process.env.SPEC_BUDGET_USD);
  if (!statePath || !Number.isFinite(budget)) throw new Error("Missing spec budget environment");
  const shared = globalThis[SHARED_KEY] ??= new Map();
  const ledger = shared.get(statePath) ?? new BudgetLedger(budget, statePath);
  shared.set(statePath, ledger);
  installFetchGate(ledger);
  pi.on("session_start", (_event, ctx) => ledger.remember(ctx));
  pi.on("before_provider_request", (event, ctx) => {
    try {
      return ledger.before(ledger.remember(ctx), ctx.model, event.payload);
    } catch (error) {
      // Even a failed state-file write must never permit an unaccounted call.
      try { if (!ledger.stopped) ledger.stop(error.message || "Budget enforcement failed"); } catch {}
      ledger.abortAll();
      return blockedPayload(error.message || "Budget enforcement failed");
    }
  });
  pi.on("message_end", (event, ctx) => {
    try {
      ledger.settle(ledger.remember(ctx), event.message);
    } catch (error) {
      try { ledger.stop(error.message || "Budget accounting failed"); } catch {}
      ledger.abortAll();
    }
    if (ledger.stopped) ledger.abortAll();
  });
  pi.on("session_shutdown", (_event, ctx) => {
    const id = ctx.sessionManager.getSessionId();
    if (ledger.pending.has(id)) ledger.uncertain(id, "Session stopped before reporting request usage");
    ledger.contexts.delete(id);
  });
}
