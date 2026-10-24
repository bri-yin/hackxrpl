import contextlib
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock

SPEC = importlib.util.spec_from_file_location("agents", Path(__file__).resolve().parents[1] / "scripts/agents.py")
agents = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agents)


def task(number=1, scope="app/", depends="none"):
    return {"number": number, "title": f"Task {number}", "state": "OPEN",
            "url": f"https://github.com/example/project/issues/{number}",
            "body": f"### Allowed paths\n{scope}\n\n### Depends on\n{depends}\n\n### Acceptance criteria\nWorks.\n"}


class ParsingTests(unittest.TestCase):
    def test_scopes_and_boundaries(self):
        self.assertEqual(agents.task_scopes(task(scope="- `app/`\n- shared.txt")), ["app", "shared.txt"])
        self.assertTrue(agents.overlap(["app"], ["app/left.txt"]))
        self.assertFalse(agents.overlap(["app"], ["apple"]))
        self.assertTrue(agents.overlap(["."], ["anything"]))

    def test_invalid_scopes_fail_closed(self):
        for scope in ("", "../other", "/tmp", "app/*", ".git/config", "a/../b", "a\\b", "a//b"):
            with self.subTest(scope=scope), self.assertRaises(agents.CoordinationError):
                agents.task_scopes(task(scope=scope))

    def test_dependencies(self):
        self.assertEqual(agents.dependencies(task(1, depends="#2, #3")), [2, 3])
        self.assertEqual(agents.dependencies(task()), [])
        for value in ("#1", "ask Bob", "https://example.com/2"):
            with self.subTest(value=value), self.assertRaises(agents.CoordinationError):
                agents.dependencies(task(depends=value))

    def test_missing_or_unsuccessful_ci_blocks_merge(self):
        for checks in ([], [{"context": "coordination", "state": "PENDING"}],
                       [{"context": "another-check", "state": "SUCCESS"}],
                       [{"__typename": "CheckRun", "name": "coordination", "status": "COMPLETED", "conclusion": "SKIPPED"}]):
            with self.subTest(checks=checks), self.assertRaises(agents.CoordinationError):
                agents.successful_checks({"statusCheckRollup": checks}, ["coordination"])
        agents.successful_checks({"statusCheckRollup": [
            {"__typename": "CheckRun", "name": "coordination", "status": "COMPLETED", "conclusion": "SUCCESS"}
        ]}, ["coordination"])


class GitIntegrationTests(unittest.TestCase):
    """Real pushes to a temporary bare remote, with independent collaborator clones."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="coordination-test-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.remote = self.path / "remote.git"
        self.cmd("git", "init", "--bare", str(self.remote))
        seed = self.path / "seed"
        self.cmd("git", "init", "-b", "main", str(seed))
        self.configure(seed)
        (seed / "app").mkdir()
        (seed / "app/left.txt").write_text("left\n")
        (seed / "app/right.txt").write_text("right\n")
        (seed / "shared.txt").write_text("shared\n")
        (seed / "coordination.json").write_text(json.dumps({
            "repository": "example/project", "base": "main", "remote": "origin",
            "checks": [["python3", "-c", "from pathlib import Path; assert Path('app/right.txt').read_text() == 'right\\n'"]],
            "required_checks": ["coordination"]}))
        self.cmd("git", "-C", str(seed), "add", ".")
        self.cmd("git", "-C", str(seed), "commit", "-m", "Initial")
        self.cmd("git", "-C", str(seed), "remote", "add", "origin", str(self.remote))
        self.cmd("git", "-C", str(seed), "push", "origin", "main")
        self.a = self.clone("alice")
        self.b = self.clone("bob")
        self.a.issue = Mock(side_effect=lambda n: task(n))
        self.b.issue = Mock(side_effect=lambda n: task(n))

    def cmd(self, *argv):
        return agents.run(list(argv))

    def configure(self, path):
        self.cmd("git", "-C", str(path), "config", "user.name", "Test Worker")
        self.cmd("git", "-C", str(path), "config", "user.email", "worker@example.invalid")
        self.cmd("git", "-C", str(path), "config", "commit.gpgsign", "false")

    def clone(self, name):
        path = self.path / name
        self.cmd("git", "clone", "--branch", "main", str(self.remote), str(path))
        self.configure(path)
        return agents.Repo(path)

    def reserve(self, repo, number=1, scope="app/"):
        return repo.claim(task(number, scope), "test-worker")

    def test_two_clones_cannot_claim_same_issue(self):
        claim = self.reserve(self.a)
        with self.assertRaises(agents.CoordinationError):
            self.reserve(self.b)
        self.assertEqual(self.b.claims()[0]["oid"], claim["oid"])
        self.assertNotIn(agents.GATE, self.a.refs())

    def test_overlapping_scopes_block_different_issues(self):
        self.reserve(self.a, 1, "app/")
        with self.assertRaisesRegex(agents.CoordinationError, "Conflicts"):
            self.reserve(self.b, 2, "app/left.txt")
        self.assertEqual(len(self.a.claims()), 1)

    def test_disjoint_scopes_allow_parallel_workers(self):
        self.reserve(self.a, 1, "app/left.txt")
        self.reserve(self.b, 2, "app/right.txt")
        self.assertEqual([c["issue"] for c in self.a.claims()], [1, 2])

    def test_concurrent_claims_have_one_winner(self):
        barrier = threading.Barrier(2)

        def compete(repo, number):
            barrier.wait(timeout=10)
            try:
                self.reserve(repo, number)
                return True
            except agents.CoordinationError:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(compete, self.a, 1), pool.submit(compete, self.b, 2)]
            results = [f.result(timeout=30) for f in futures]
        self.assertEqual(sum(results), 1)
        self.assertEqual(len(self.a.claims()), 1)
        self.assertNotIn(agents.GATE, self.a.refs())

    def test_non_owner_cannot_release(self):
        self.reserve(self.a)
        with self.assertRaises(agents.CoordinationError):
            self.b.release(1)
        self.a.release(1)
        self.reserve(self.b)
        self.assertEqual(len(self.b.claims()), 1)

    def test_stale_lease_cannot_delete_reassigned_task(self):
        old = self.reserve(self.a)
        self.a.release(1)
        new = self.reserve(self.b)
        with self.assertRaises(agents.CoordinationError):
            self.a.cas(agents.NAMESPACE + "issue-1", old["oid"], "")
        self.assertEqual(self.a.claims()[0]["oid"], new["oid"])

    def test_scheduler_crash_requires_exact_recovery(self):
        oid = self.a.metadata_commit({"kind": "scheduler", "worker": "crashed"})
        self.a.cas(agents.GATE, "", oid)
        with self.assertRaisesRegex(agents.CoordinationError, "Scheduler reservation"):
            self.reserve(self.b)
        with self.assertRaises(agents.CoordinationError):
            self.b.recover(None, True, "0" * 40, "worker stopped")
        self.assertEqual(self.a.refs()[agents.GATE], oid)
        with contextlib.redirect_stdout(io.StringIO()):
            self.b.recover(None, True, oid, "worker stopped")
        self.reserve(self.b)

    def test_open_dependency_blocks_reservation(self):
        with self.assertRaisesRegex(agents.CoordinationError, "still open"):
            self.a.claim(task(1, depends="#2"), "alice")
        self.assertEqual(self.a.refs(), {})

    def test_closed_issue_cannot_be_claimed(self):
        issue = task()
        issue["state"] = "CLOSED"
        with self.assertRaises(agents.CoordinationError):
            self.a.claim(issue, "alice")

    def start_worktree(self, scope="app/"):
        target = self.path / "worktree-1"
        self.a.issue = Mock(return_value=task(scope=scope))
        with contextlib.redirect_stdout(io.StringIO()):
            self.a.start(1, "alice-codex", str(target))
        return agents.Repo(target)

    def commit(self, repo, path, text):
        (repo.root / path).write_text(text)
        repo.git("add", path)
        repo.git("commit", "-m", "Task change")

    def test_worktree_isolation_and_successful_validation(self):
        work = self.start_worktree()
        self.assertEqual(self.a.git("branch", "--show-current"), "main")
        self.commit(work, "app/left.txt", "updated\n")
        self.assertEqual((self.a.root / "app/left.txt").read_text(), "left\n")
        with contextlib.redirect_stdout(io.StringIO()):
            work.check(1)
        self.assertTrue(work.local(1)["validation"]["clean"])
        self.assertEqual(work.local(1)["validation"]["head"], work.git("rev-parse", "HEAD"))

    def test_out_of_scope_changes_fail_validation(self):
        work = self.start_worktree("app/left.txt")
        (work.root / "shared.txt").write_text("unauthorized\n")
        with self.assertRaisesRegex(agents.CoordinationError, "outside"):
            work.check(1)

    def test_rename_out_of_scope_is_detected(self):
        work = self.start_worktree("app/left.txt")
        work.git("mv", "app/left.txt", "outside.txt")
        work.git("commit", "-m", "Rename")
        with self.assertRaisesRegex(agents.CoordinationError, "outside"):
            work.check(1)

    def test_failed_real_validation_does_not_record_success(self):
        work = self.start_worktree()
        self.commit(work, "app/right.txt", "broken\n")
        with self.assertRaisesRegex(agents.CoordinationError, "Validation failed"):
            work.check(1)
        self.assertNotIn("validation", work.local(1))

    def test_later_failed_validation_invalidates_previous_success(self):
        work = self.start_worktree()
        with contextlib.redirect_stdout(io.StringIO()):
            work.check(1)
        self.assertIn("validation", work.local(1))
        (work.root / "shared.txt").write_text("out of scope\n")
        with self.assertRaises(agents.CoordinationError):
            work.check(1)
        self.assertNotIn("validation", work.local(1))

    def test_scope_check_preserves_leading_whitespace_in_filenames(self):
        work = self.start_worktree("app/left.txt")
        (work.root / " app").mkdir()
        (work.root / " app/left.txt").write_text("different path\n")
        with self.assertRaisesRegex(agents.CoordinationError, "outside"):
            work.check(1)

    def test_dirty_checkout_rejects_here_without_claim(self):
        (self.a.root / "shared.txt").write_text("user changes\n")
        with self.assertRaisesRegex(agents.CoordinationError, "clean checkout"):
            self.a.start(1, "alice", here=True)
        self.assertEqual(self.a.refs(), {})

    def test_pr_requires_validation_of_current_commit(self):
        work = self.start_worktree()
        work.gh = Mock()
        with self.assertRaisesRegex(agents.CoordinationError, "check"):
            work.pr(1, "Work")
        work.gh.assert_not_called()
        with contextlib.redirect_stdout(io.StringIO()):
            work.check(1)
        self.commit(work, "app/left.txt", "new commit\n")
        with self.assertRaises(agents.CoordinationError):
            work.pr(1, "Work")
        work.gh.assert_not_called()

    def test_checked_work_can_be_pushed_and_opened_as_pr(self):
        work = self.start_worktree()
        self.commit(work, "app/left.txt", "complete\n")
        captured = {}

        def create_pr(*args):
            captured["args"] = args
            captured["body"] = Path(args[args.index("--body-file") + 1]).read_text()
            return "https://github.com/example/project/pull/10"

        work.gh = Mock(side_effect=create_pr)
        with contextlib.redirect_stdout(io.StringIO()):
            work.check(1)
            work.pr(1, "Finished task", ready=True)
        self.assertIn("Closes #1\n", captured["body"])
        self.assertIn("Finished task\n", captured["body"])
        self.assertNotIn("--draft", captured["args"])
        state = work.local(1)
        advertised = work.git("ls-remote", "origin", "refs/heads/" + state["branch"])
        self.assertTrue(advertised.startswith(work.git("rev-parse", "HEAD") + "\t"))
        self.assertEqual(state["pr"], "https://github.com/example/project/pull/10")
        self.assertEqual(len(work.claims()), 1)

    def pr_info(self, claim, state="OPEN"):
        return {"url": "https://github.com/example/project/pull/10", "state": state, "isDraft": False,
                "headRefName": claim["branch"], "headRefOid": self.a.git("rev-parse", "HEAD"),
                "baseRefName": "main", "reviewDecision": "", "statusCheckRollup": [
                    {"__typename": "CheckRun", "name": "coordination", "status": "COMPLETED", "conclusion": "SUCCESS"}]}

    def test_orchestrator_merges_only_checked_matching_head(self):
        claim = self.reserve(self.a)
        self.b.pr_info = Mock(side_effect=[self.pr_info(claim), self.pr_info(claim, "MERGED")])
        self.b.gh = Mock(return_value="")
        with contextlib.redirect_stdout(io.StringIO()):
            self.b.merge(1, "10")
        args = self.b.gh.call_args.args
        self.assertIn("--match-head-commit", args)
        self.assertNotIn("--admin", args)
        self.assertEqual(self.b.refs(), {})

    def test_merge_rejects_pending_ci_and_keeps_claim(self):
        claim = self.reserve(self.a)
        pr = self.pr_info(claim)
        pr["statusCheckRollup"][0]["status"] = "IN_PROGRESS"
        self.b.pr_info = Mock(return_value=pr)
        self.b.gh = Mock()
        with self.assertRaises(agents.CoordinationError):
            self.b.merge(1, "10")
        self.b.gh.assert_not_called()
        self.assertEqual(len(self.b.claims()), 1)
        self.assertNotIn(agents.GATE, self.b.refs())

    def test_merge_rejects_a_branch_behind_current_main(self):
        claim = self.reserve(self.a)
        pr = self.pr_info(claim)
        self.commit(self.a, "shared.txt", "new main\n")
        self.a.git("push", "origin", "main")
        self.b.pr_info = Mock(return_value=pr)
        self.b.gh = Mock()
        with self.assertRaisesRegex(agents.CoordinationError, "current base"):
            self.b.merge(1, "10")
        self.b.gh.assert_not_called()
        self.assertEqual(len(self.b.claims()), 1)

    def test_unrelated_merged_pr_cannot_release_reservation(self):
        claim = self.reserve(self.a)
        pr = self.pr_info(claim, "MERGED")
        pr["headRefName"] = "unrelated-branch"
        self.b.pr_info = Mock(return_value=pr)
        with self.assertRaises(agents.CoordinationError):
            self.b.finish(1, "10")
        self.assertEqual(len(self.b.claims()), 1)


if __name__ == "__main__":
    unittest.main()
