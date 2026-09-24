"""Exercise checkpoints against actual local and bare Git repositories."""

from pathlib import Path
import os
import subprocess
import tempfile
import unittest

from tools.spec_git import GitError, GitRepo


class GitLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.remote = self.base / "remote.git"
        self.root = self.base / "work"
        self.root.mkdir()
        self.env = os.environ.copy()
        self.env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        self.git(self.base, "init", "--bare", str(self.remote))
        self.git(self.root, "init", "-b", "main")
        self.git(self.root, "config", "user.name", "Spec Test")
        self.git(self.root, "config", "user.email", "spec@example.invalid")
        (self.root / "existing.md").write_text("initial\n")
        self.git(self.root, "add", ".")
        self.git(self.root, "commit", "-m", "Initial")
        self.git(self.root, "remote", "add", "origin", str(self.remote))
        self.git(self.root, "push", "-u", "origin", "main")
        self.logs = []
        self.repo = GitRepo(self.root, self.logs.append, timeout=10)
        self.repo.env.update(self.env)

    def git(self, cwd, *args):
        return subprocess.check_output(
            ["git", *args], cwd=cwd, env=self.env, stderr=subprocess.PIPE, text=True
        ).strip()

    def assert_saved(self):
        self.assertEqual(self.git(self.root, "status", "--porcelain"), "")
        head = self.git(self.root, "rev-parse", "HEAD")
        self.assertEqual(head, self.git(self.remote, "rev-parse", "main"))
        return head

    def test_prepare_commits_existing_and_finalize_commits_all_changes(self):
        (self.root / "existing.md").write_text("user changes\n")
        (self.root / "new.md").write_text("new file\n")
        (self.root / ".gitignore").write_text("ignored.txt\n")
        (self.root / "ignored.txt").write_text("do not commit\n")
        self.repo.prepare()
        initial_checkpoint = self.assert_saved()
        self.assertEqual(self.git(self.root, "ls-files", "ignored.txt"), "")
        (self.root / "existing.md").unlink()
        (self.root / "new.md").rename(self.root / "renamed.md")
        (self.root / "autonomous.md").write_text("improvement\n")
        final_checkpoint = self.repo.finalize()
        self.assertEqual(final_checkpoint, self.assert_saved())
        self.assertNotEqual(initial_checkpoint, final_checkpoint)
        self.assertEqual(self.git(self.root, "ls-files", "existing.md", "new.md"), "")
        self.assertEqual(self.git(self.root, "show", "HEAD:autonomous.md"), "improvement")

    def test_no_changes_does_not_create_empty_commits(self):
        before = self.assert_saved()
        self.assertEqual(self.repo.prepare(), before)
        self.assertEqual(self.repo.finalize(), before)

    def test_reset_round_discards_edits_commits_and_new_files(self):
        (self.root / "existing.md").write_text("user changes\n")
        (self.root / ".gitignore").write_text("ignored.txt\n")
        (self.root / "ignored.txt").write_text("keep ignored file\n")
        before = self.repo.prepare()
        (self.root / "existing.md").write_text("partial committed edit\n")
        self.git(self.root, "commit", "-am", "Incomplete worker commit")
        (self.root / "existing.md").unlink()
        (self.root / "staged.md").write_text("partial staged file\n")
        self.git(self.root, "add", "--all")
        (self.root / "drafts").mkdir()
        (self.root / "drafts" / "new.md").write_text("partial untracked file\n")

        self.repo.reset_round(before)

        self.assertEqual(self.assert_saved(), before)
        self.assertEqual((self.root / "existing.md").read_text(), "user changes\n")
        self.assertEqual((self.root / "ignored.txt").read_text(), "keep ignored file\n")
        self.assertFalse((self.root / "staged.md").exists())
        self.assertFalse((self.root / "drafts").exists())

    def test_reset_round_needs_valid_checkpoint_before_discarding_changes(self):
        (self.root / "existing.md").write_text("pending edit\n")
        (self.root / "pending.md").write_text("pending file\n")
        with self.assertRaises(GitError):
            self.repo.reset_round("missing-checkpoint")
        self.assertEqual((self.root / "existing.md").read_text(), "pending edit\n")
        self.assertTrue((self.root / "pending.md").exists())

    def test_reset_round_refuses_to_discard_another_branch(self):
        before = self.assert_saved()
        self.git(self.root, "checkout", "-b", "topic")
        (self.root / "existing.md").write_text("topic edit\n")
        with self.assertRaisesRegex(GitError, "main is no longer checked out"):
            self.repo.reset_round(before)
        self.assertEqual((self.root / "existing.md").read_text(), "topic edit\n")

    def test_prepare_fast_forwards_clean_behind_checkout(self):
        other = self.base / "other"
        self.git(self.base, "clone", "-b", "main", str(self.remote), str(other))
        self.git(other, "config", "user.name", "Other")
        self.git(other, "config", "user.email", "other@example.invalid")
        (other / "remote.md").write_text("new remote work\n")
        self.git(other, "add", ".")
        self.git(other, "commit", "-m", "Remote work")
        self.git(other, "push")
        self.repo.prepare()
        self.assert_saved()
        self.assertEqual((self.root / "remote.md").read_text(), "new remote work\n")

    def test_rejected_push_preserves_local_commit(self):
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'test rejection' >&2\nexit 1\n")
        hook.chmod(0o755)
        before = self.git(self.remote, "rev-parse", "main")
        (self.root / "pending.md").write_text("must survive\n")
        with self.assertRaisesRegex(GitError, "test rejection"):
            self.repo.finalize()
        self.assertNotEqual(before, self.git(self.root, "rev-parse", "HEAD"))
        self.assertEqual(before, self.git(self.remote, "rev-parse", "main"))
        self.assertEqual(self.git(self.root, "show", "HEAD:pending.md"), "must survive")

    def test_push_timeout_preserves_work(self):
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nsleep 30\n")
        hook.chmod(0o755)
        (self.root / "pending.md").write_text("must survive\n")
        self.repo.timeout = 0.25
        with self.assertRaisesRegex(GitError, "timed out"):
            self.repo.finalize()
        self.assertEqual(self.git(self.root, "show", "HEAD:pending.md"), "must survive")

    def test_divergence_never_forces_remote_or_discards_local_work(self):
        other = self.base / "other"
        self.git(self.base, "clone", "-b", "main", str(self.remote), str(other))
        self.git(other, "config", "user.name", "Other")
        self.git(other, "config", "user.email", "other@example.invalid")
        (other / "remote.md").write_text("remote work\n")
        self.git(other, "add", ".")
        self.git(other, "commit", "-m", "Remote work")
        self.git(other, "push")
        remote_head = self.git(self.remote, "rev-parse", "main")
        (self.root / "pending.md").write_text("local work\n")
        with self.assertRaisesRegex(GitError, "push"):
            self.repo.prepare()
        self.assertEqual(remote_head, self.git(self.remote, "rev-parse", "main"))
        self.assertEqual(self.git(self.root, "show", "HEAD:pending.md"), "local work")
        self.assertNotEqual(remote_head, self.git(self.root, "rev-parse", "HEAD"))

    def test_wrong_branch_is_rejected_before_staging(self):
        self.git(self.root, "checkout", "-b", "topic")
        (self.root / "pending.md").write_text("preserve\n")
        with self.assertRaisesRegex(GitError, "main branch"):
            self.repo.prepare()
        self.assertEqual(self.git(self.root, "diff", "--cached", "--name-only"), "")

    def test_missing_upstream_is_rejected(self):
        self.git(self.root, "branch", "--unset-upstream")
        with self.assertRaisesRegex(GitError, "track origin/main"):
            self.repo.prepare()

    def test_missing_identity_is_rejected(self):
        self.git(self.root, "config", "--unset", "user.email")
        with self.assertRaisesRegex(GitError, "user.email"):
            self.repo.prepare()

    def test_real_merge_conflict_is_preserved(self):
        self.git(self.root, "checkout", "-b", "topic")
        (self.root / "existing.md").write_text("topic\n")
        self.git(self.root, "commit", "-am", "Topic")
        self.git(self.root, "checkout", "main")
        (self.root / "existing.md").write_text("main\n")
        self.git(self.root, "commit", "-am", "Main")
        merge = subprocess.run(
            ["git", "merge", "topic"], cwd=self.root, env=self.env, capture_output=True
        )
        self.assertNotEqual(merge.returncode, 0)
        before = (self.root / "existing.md").read_text()
        with self.assertRaisesRegex(GitError, "Unfinished Git operation"):
            self.repo.finalize()
        self.assertEqual(before, (self.root / "existing.md").read_text())
        self.assertTrue(self.git(self.root, "ls-files", "--unmerged"))


if __name__ == "__main__":
    unittest.main()
