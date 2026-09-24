# Codex specification runner

Run `tools/install-ceps` once, then use `ceps` from any directory. The installed command
always targets the repository containing the script. It requires Linux, Python 3, Git,
and Codex CLI 0.156.0 or 0.156.1 with a file-based ChatGPT login, the required models,
and live quota data.
Other CLI versions stop at preflight until their model and protocol behavior is verified.

```sh
ceps --check 20
ceps 20
ceps 20 --buffer 3
ceps --status
```

`ceps PERCENT` repeatedly improves the specification until the account's remaining usage
approaches the requested percentage. `PERCENT` is a remaining-usage floor from 0 to 100,
not a percentage to consume during this invocation. `--buffer POINTS` adds a margin of
0 to 100 percentage points, defaulting to 2. For example, `ceps 20` stops when any relevant
usage window has 22% or less remaining. A window already at that boundary prevents work
from starting.

`ceps --check PERCENT` checks configuration, model support, authentication, and live quota
without running a model turn or changing Git. It accepts `--buffer` too. `ceps --status`
shows the saved statistics from the latest run; it does not start work or refresh live quota.

## Models and instructions

Every improvement uses a fresh `codex exec` parent session with `gpt-6-astra` at ultra
reasoning, represented as `xhigh` in its provider request. Subagents use `gpt-6-luna` at `max`,
its maximum supported reasoning level. The runner validates the installed model catalog
and fixes these settings rather than relying on the user's default model.

Native subagent spawning is disabled. Parent sessions delegate through a controlled task
MCP service. Each delegation launches a concurrent fleet of children with fixed model and
reasoning arguments. The isolated runtime configuration reuses Codex authentication without
changing the user's normal configuration. Missing model support or an unverifiable
configuration stops the run; there is no fallback to another model or a lower reasoning level.
The private credential snapshot contains no refresh token and is removed during cleanup.
The normal login owns token refresh; an expired worker fails rather than changing accounts.
Luna workers have a read-only sandbox. Parent shell commands cannot access the network.

The original workflow is the repository's root `prompt.md`: understand the specification
and metaspecification, use subagents for understanding and planning, make a small valuable
change, and obtain adversarial subagent review. The metaspecification contains writing and
organization conventions; it is not a separate product prompt. Each round reads the current
instructions and specification, preserving the product's established intent.

Every understanding, planning, and review call dispatches a complete fleet. A phase succeeds
only after all its specialists finish successfully; the parent incorporates their findings
before proceeding. Document specialists cover the corpus in groups balanced by size and
structure. Complex documents are split into contiguous word ranges so multiple specialists
cover distinct portions while checking surrounding context; compact or link-heavy documents
use character ranges. Two specialists also assess the whole specification for integration,
contradictions, and omissions. Each worker receives the complete delegated task, its own
perspective, and its document scope.
Workers are read-only; the parent combines their recommendations and makes the edits.

The supervisor reads the current Markdown files under `specification/` and
`metaspecification/` at the start of every attempt to set its fleet width. It refreshes
document scopes before each delegation, including new, moved, or expanded files in review.
Fleet size is the largest of document count, words divided by 1,200, headings divided by 12,
and relative links to other files
divided by 12, rounding each quotient up, then adding two cross-cutting specialists.
It launches at least 4 and at most 32 children concurrently per fleet. These structural
counts are a reproducible proxy for complexity, not a judgment of semantic difficulty.
The parent requests additional focused fleets when ambiguity or dependencies warrant them.
The concurrency cap bounds simultaneous provider calls; the quota guard still applies to
every launch and stops active workers when the remaining-usage threshold is reached.

Workers change only `specification/`. The runner's wrapper overrides `prompt.md`'s Git
instructions so the supervisor handles checkpoints. Workers leave reviewed changes in the
working tree and finish with a concise account of the improvement.

## Quota and statistics

The supervisor obtains complete, live account rate-limit snapshots through authenticated
Codex app-server requests and refreshes them every five seconds. Remaining usage is
`100 - used_percent` for each relevant returned window. Any window reaching the buffered
floor stops further model work. Missing, stale, invalid, or failed quota readings also stop
work; a previously healthy reading is not permission to continue indefinitely.
Transient transport and server failures allow three quota read attempts, reconnecting after
one and then two seconds. A failed live poll counts as the first read. Active workers stop
and the incomplete attempt resets before recovery; a fresh, valid reading is required to
start the next attempt of the same round. Exhausted quota retries, authentication or account
changes, malformed readings, and quota cutoffs stop the run. Ctrl-C cancels retry waits.

The buffer is a margin, not an exact quota reservation. Reporting latency, requests already
in flight, other sessions using the account, and quota resets can change the reading between
checks. Concurrent fleets consume quota faster and increase the work already in flight when
the cutoff is detected. The runner therefore cannot guarantee an exact remaining percentage.
It starts no new model call after detecting the cutoff and stops active workers before
cleanup. Git cleanup uses no model turn.

Status and saved statistics report progress, rounds and attempts, elapsed time, model usage,
and observed account quota. Fleet reporting includes the planned size and complexity counts,
active Luna children separately from the Astra parent, peak child concurrency, and fleet
completion and failure counts. Account quota changes include unrelated sessions and resets;
they are distinct from the tokens reported by this run's parent and child sessions. Token
counts are not converted into a subscription percentage or a dollar charge.

## Checkpoints and recovery

`ceps` and `spec` share the repository lock, so they cannot run against this checkout
simultaneously. Main must track `origin/main`; Git identity and noninteractive push
authentication must work. Before model work, the supervisor checkpoints all existing
nonignored changes, synchronizes main, and pushes. Start from a checkout whose existing
work is ready to commit, and avoid editing it with other tools during a run.

Each completed, reviewed round is committed and pushed after its workers stop. An incomplete
round is reset to its starting commit, including local commits and new nonignored files.
Quota stops and interruptions discard incomplete work without asking a model to clean up.
Previously completed rounds and the initial checkpoint remain saved.

Recoverable failures allow up to three fresh attempts per round, labeled `1A`, `1B`, `1C`,
then `2A` after round 1 succeeds. Three failed attempts or three completed rounds without
commits stop the run. Git failures preserve completed work for recovery; the supervisor
never force-pushes. A forced kill or power loss can prevent cleanup. The Git directory's
`ceps-incomplete.json` records the checkpoint and blocks restart until recovery is complete.
For an `incomplete` round, inspect and discard its changes back to the recorded checkpoint;
for a `reviewed` round, preserve and finish committing/pushing the reviewed work. Remove the
marker only after recovery. The next initial checkpoint includes any remaining local work.

Private run directories live under `~/.local/state/ceps/` or `$XDG_STATE_HOME/ceps/`.
Statistics are saved atomically. Bounded JSONL diagnostics retain activity, terminal events,
and errors without retaining repeated streaming snapshots. Quota retries report their delay
and read attempt; RPC errors retain safe error codes and recognized diagnostic categories
without logging raw account responses or authentication details. `ceps --status` makes the latest
saved result available after the process exits.
Tokens from terminated workers can be unavailable; the statistics identify incomplete usage
instead of estimating a subscription charge. Codex workers are ephemeral; these diagnostics
and statistics are retained, but separate full Codex conversation transcripts are not.

Run `python3 -m unittest discover -s tests -v` and
`node --test tests/test-spec-budget.mjs` to verify the tools. Tests use fake accounts, local
model transports, and temporary Git remotes, without spending model credits.
