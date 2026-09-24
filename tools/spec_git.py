"""Git checkpoints for the autonomous specification runner.

Checkpoints preserve work on Git failures and never force-push. Incomplete rounds
are explicitly reset to their starting checkpoint. Stop all agent writers before
calling ``finalize`` or ``reset_round``.
"""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
from typing import Callable


class GitError(RuntimeError):
    """A checkpoint could not be completed safely."""


class GitRepo:
    def __init__(
        self, path: str | Path, log: Callable[[str], None] = print, timeout: float = 60
    ) -> None:
        self.root = Path(path).resolve()
        self.log = log
        self.timeout = timeout
        self.env = os.environ.copy()
        self.env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")
        self.env["GIT_SSH_COMMAND"] = (
            self.env.get("GIT_SSH_COMMAND", "ssh")
            + " -oBatchMode=yes -oConnectTimeout=10"
        )
        self.root = Path(self._git("rev-parse", "--show-toplevel").strip())

    def _command(self, *args: str) -> subprocess.CompletedProcess[str]:
        command = ["git", *args]
        try:
            proc = subprocess.Popen(
                command,
                cwd=self.root,
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            raise GitError(f"Cannot run git {args[0]}: {exc}") from exc
        try:
            stdout, stderr = proc.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired as exc:
            # Include credential helpers and SSH children in the timeout.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
            raise GitError(
                f"git {args[0]} timed out after {self.timeout:g}s; "
                "work is preserved. Check authentication/network access and retry."
            ) from exc
        except BaseException:
            # Never leave a detached Git writer running if the caller is
            # interrupted and then starts its own recovery checkpoint.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
            raise
        return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)

    def _git(self, *args: str) -> str:
        result = self._command(*args)
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise GitError(
                f"git {' '.join(args)} failed (exit {result.returncode}): "
                f"{detail or 'no diagnostic output'}. Work is preserved."
            )
        return result.stdout

    def validate(self) -> None:
        """Check branch, upstream, identity, and unfinished Git operations."""
        branch = self._command("symbolic-ref", "--quiet", "--short", "HEAD")
        if branch.returncode or branch.stdout.strip() != "main":
            raise GitError("Run spec on the main branch; branch switching is not automatic.")
        for operation in (
            "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge",
            "rebase-apply", "sequencer", "BISECT_START",
        ):
            path = Path(self._git("rev-parse", "--git-path", operation).strip())
            if not path.is_absolute():
                path = self.root / path
            if path.exists():
                raise GitError(
                    f"Unfinished Git operation ({operation}); resolve it before running spec."
                )
        if self._git("ls-files", "--unmerged").strip():
            raise GitError("Unmerged entries exist; resolve conflicts before running spec.")
        upstream = self._command("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
        if upstream.returncode or upstream.stdout.strip() != "origin/main":
            raise GitError("main must track origin/main before running spec.")
        self._git("remote", "get-url", "origin")
        for key in ("user.name", "user.email"):
            result = self._command("config", "--get", key)
            if result.returncode or not result.stdout.strip():
                raise GitError(f"Set git {key} before running spec.")
        self._git("var", "GIT_AUTHOR_IDENT")
        self._git("var", "GIT_COMMITTER_IDENT")

    def _commit(self, message: str) -> None:
        self._git("add", "--all", "--", ".")
        staged = self._command("diff", "--cached", "--quiet", "--exit-code")
        if staged.returncode not in (0, 1):
            raise GitError(f"Cannot inspect staged changes: {staged.stderr.strip()}")
        if staged.returncode:
            self.log("Committing all current changes.")
            self._git("commit", "-m", message)

    def _verify(self) -> str:
        if self._git("status", "--porcelain", "--untracked-files=all").strip():
            raise GitError("Working tree is still dirty after checkpoint; work is preserved.")
        head = self._git("rev-parse", "HEAD").strip()
        remote = self._git("ls-remote", "--exit-code", "origin", "refs/heads/main").split()
        if not remote or remote[0] != head:
            raise GitError("Local HEAD differs from live origin/main; checkpoint is incomplete.")
        return head

    def prepare(self) -> str:
        """Save existing work, synchronize main, and verify a clean starting point."""
        self.validate()
        self._commit("Checkpoint existing work before autonomous specification run")
        self.log("Fetching origin/main.")
        self._git("fetch", "--no-tags", "origin", "refs/heads/main:refs/remotes/origin/main")
        # A clean checkout may simply be behind. Fast-forward it before pushing.
        behind = self._command("merge-base", "--is-ancestor", "HEAD", "origin/main")
        if behind.returncode == 0:
            self._git("pull", "--ff-only", "--no-rebase", "origin", "main")
        elif behind.returncode != 1:
            raise GitError(f"Cannot compare main and origin/main: {behind.stderr.strip()}")
        self.log("Pushing existing work to origin/main.")
        self._git("push", "origin", "HEAD:refs/heads/main")
        self._git("pull", "--ff-only", "--no-rebase", "origin", "main")
        return self._verify()

    def finalize(self, message: str = "Checkpoint autonomous specification improvements") -> str:
        """Commit and push all work after agent writers stop; verify the remote."""
        self.validate()
        self._commit(message)
        self.log("Pushing checkpoint to origin/main.")
        self._git("push", "origin", "HEAD:refs/heads/main")
        head = self._verify()
        self.log(f"Checkpoint saved and pushed: {head[:12]}")
        return head

    def reset_round(self, checkpoint: str) -> None:
        """Discard an incomplete round, including its local commits and new files.

        The checkpoint must come from a clean, prepared checkout before starting
        the worker. Ignored files are left alone; no remote operation is needed.
        """
        branch = self._command("symbolic-ref", "--quiet", "--short", "HEAD")
        if branch.returncode or branch.stdout.strip() != "main":
            raise GitError("Cannot reset an incomplete round: main is no longer checked out.")
        target = self._git("rev-parse", "--verify", f"{checkpoint}^{{commit}}").strip()
        self.log(f"Resetting incomplete round to {target[:12]}.")
        self._git("reset", "--hard", target)
        self._git("clean", "-fd", "--", ".")
        if self._git("status", "--porcelain", "--untracked-files=all").strip():
            raise GitError("Working tree is still dirty after resetting the incomplete round.")
