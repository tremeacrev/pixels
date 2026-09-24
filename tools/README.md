# Specification runner

For the Codex runner with a remaining-usage percentage floor, see [ceps](CEPS.md).

Run `tools/install-spec` once, then run `spec 42.42` from any directory. The installed
command always targets the repository containing this script. Python 3, Git, and Oh My Pi
(`omp`) must be on PATH. This installation targets Linux and Oh My Pi 18.2.7 with the
configured DeepSeek Chat Completions models.

`spec` accepts exactly one dollar amount, with up to two decimal places and a minimum
of $2.00. The amount is the budget for this invocation, including a $1.00 reserve.
It repeatedly runs `prompt.md`, using fresh sessions for small specification improvements.
Each round uses subagents for understanding, planning, and review. Status shows the current
activity, round, elapsed time, spending, remaining budget, and requests in flight.
Rounds have up to three attempts, labeled `1A`, `1B`, `1C`, then `2A` once round 1 completes.
A failed attempt stops its workers and resets all its changes before starting a fresh session
at the same round number. Failed attempts are never committed or pushed. Three failed attempts
stop the run with an error; completed rounds remain saved.

The budget extension guards each parent and subagent request before transmission. It reserves
a conservative input cost and caps output tokens so admitted requests fit within the budget
minus $1.00. It then reconciles the reservation against Oh My Pi's reported model cost.
The runner can stop slightly early when another request cannot fit safely. It does not spend
the final $1.00 on a cleanup prompt: Git cleanup requires no model credits.

Costs use Oh My Pi's model catalog and reported usage, not a live provider account balance.
They do not measure unrelated applications or separately billed external services. Missing
usage is accounted at the reserved maximum before retrying the round. Spending, request counts,
and estimates accumulate across attempts; retries do not replenish the budget. Unsupported
providers, missing pricing, and non-text requests stop before transmission. Automatic compaction,
advisors, model fallback, background tasks, and title generation are disabled to keep model accounting
complete. These settings apply only to the run; the user's Oh My Pi configuration is unchanged.
Optional subagent label requests that bypass usage hooks are declined locally without spending.
Automatic HTTP retries also require a new budget admission and cannot silently multiply charges.
Network failures, model loops, unexpected model cancellations, worker crashes, and truncated
responses can restart the round within its three-attempt limit. Ctrl-C and other supervisor
stop signals, exhausted budget, invalid accounting, and Git failures stop without retrying.

The runner commits and pushes existing work before starting, then checkpoints only completed
rounds after stopping all workers and confirming their accounting. The supervisor handles Git
writes; workers cannot use Git transports and leave their reviewed edits for the supervisor.
Main must track `origin/main`, and Git identity and noninteractive push authentication must
work. Untracked, nonignored files are included. Run from a checkout whose current work is
ready to be committed.

If a round is incomplete because of a budget stop, Ctrl-C, worker failure, or invalid accounting,
the runner stops all workers, resets to that round's starting commit, and removes new nonignored
files. This also discards any local commits made during the incomplete round. Previously completed
rounds and the initial checkpoint remain saved. A round needs a normal final assistant response
and a terminal, idle session to count as complete; truncated responses are discarded.

Merge conflicts, rejected pushes, invalid accounting, exhausted retry limits, and three completed
rounds without commits stop the run with a nonzero exit status. Git checkpoint failures preserve
completed work for recovery; the runner never force-pushes.

Only one `spec` process can use this repository at a time. Avoid editing it with other tools
during a run. Logs and accounting are stored with private permissions in
`~/.local/state/spec/` (or `$XDG_STATE_HOME/spec/`). A forced kill or power loss can prevent
cleanup. Inspect and discard any incomplete round before starting again, since the next
invocation treats existing work as ready to checkpoint.

RPC diagnostics and worker stderr use `events.jsonl` for the entire run, rotating at 8 MiB
with one backup (`events.jsonl.1`), for at most 16 MiB per run. Streaming snapshots are omitted;
lifecycle events, final messages, and errors are retained. Oversized events are summarized to stay
within the limit. Full conversation transcripts remain in Oh My Pi's `sessions/` directory.
At startup and between rounds, the runner removes the oldest inactive RPC logs to keep
them within 256 MiB, reserving space for the active run. Accounting and session files are
preserved, and other live runs are left alone.

Run the Python integration tests with `python3 -m unittest discover -s tests -v` and the
budget unit tests with `node --test tests/test-spec-budget.mjs`.
Tests use local fake workers/providers and temporary Git remotes without spending API credits.

The integration follows the upstream [RPC protocol](https://github.com/can1357/oh-my-pi/blob/main/docs/rpc.md)
and [extension API](https://github.com/can1357/oh-my-pi/blob/main/docs/extensions.md).
