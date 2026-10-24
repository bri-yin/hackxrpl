#!/usr/bin/env python3
"""Shared issue reservations and isolated workspaces; Python standard library only."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import uuid

NAMESPACE = "refs/heads/coordination/"
GATE = NAMESPACE + "scheduler"


class CoordinationError(RuntimeError):
    pass


def run(argv, *, cwd=None, data=None):
    result = subprocess.run(argv, cwd=cwd, input=data, text=True, capture_output=True)
    if result.returncode:
        raise CoordinationError(f"{shlex.join(argv[:3])} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.rstrip("\n")


def section(body, heading):
    match = re.search(rf"^### {re.escape(heading)}\s*\n(.*?)(?=^### |\Z)", body or "", re.M | re.S)
    return match.group(1).strip() if match else ""


def normalize_scope(value):
    value = value.strip().strip("`")
    if value == ".":
        return "."
    if not value or value.startswith("/") or "\\" in value or any(c in value for c in "*?[]:\t\n"):
        raise CoordinationError(f"Invalid scope: {value!r}; use repository-relative files/directories or '.'")
    parts = value.rstrip("/").split("/")
    if any(p in ("", ".", "..", ".git") for p in parts):
        raise CoordinationError(f"Invalid scope: {value!r}")
    return "/".join(parts)


def task_scopes(issue):
    raw = section(issue.get("body"), "Allowed paths")
    values = [line.removeprefix("- ").strip() for line in raw.splitlines() if line.strip()]
    if not values or values == ["_No response_"]:
        raise CoordinationError(f"Issue #{issue['number']} needs an '### Allowed paths' section")
    return sorted(set(normalize_scope(v) for v in values))


def dependencies(issue):
    raw = section(issue.get("body"), "Depends on")
    if raw.lower() in ("", "none", "_no response_"):
        return []
    if not re.fullmatch(r"[\s,#\d-]+", raw):
        raise CoordinationError("Depends on must contain issue numbers, e.g. '#12, #15', or 'none'")
    result = [int(n) for n in re.findall(r"\d+", raw)]
    if not result or issue["number"] in result:
        raise CoordinationError("Invalid or self-referencing dependency")
    return result


def overlap(left, right):
    return any(a == "." or b == "." or a == b or a.startswith(b + "/") or b.startswith(a + "/")
               for a in left for b in right)


def scope_violations(paths, scopes):
    return [p for p in paths if not any(s == "." or p == s or p.startswith(s + "/") for s in scopes)]


def successful_checks(pr, required):
    checks = pr.get("statusCheckRollup") or []
    names = set()
    for check in checks:
        name = check.get("name") or check.get("context")
        names.add(name)
        if check.get("__typename") == "CheckRun":
            passed = check.get("status") == "COMPLETED" and check.get("conclusion") == "SUCCESS"
        else:
            passed = check.get("state") == "SUCCESS"
        if not passed:
            raise CoordinationError(f"Check {name!r} is pending, skipped, or unsuccessful")
    missing = set(required) - names
    if missing or not checks:
        raise CoordinationError(f"Required checks have not passed: {', '.join(sorted(missing or set(required)))}")


class Repo:
    def __init__(self, cwd=None):
        self.root = Path(run(["git", "rev-parse", "--show-toplevel"], cwd=cwd)).resolve()
        common = self.git("rev-parse", "--git-common-dir")
        self.common = (self.root / common).resolve()
        self.state_dir = self.common / "agent-coordination"
        self.config = json.loads((self.root / "coordination.json").read_text())
        self.remote = self.config.get("remote", "origin")
        self.base = self.config.get("base", "main")
        self.identity = self.config["repository"]

    def git(self, *args, data=None):
        return run(["git", *args], cwd=self.root, data=data)

    def gh(self, *args):
        return run(["gh", *args, "--repo", self.identity], cwd=self.root)

    def issue(self, number):
        return json.loads(self.gh("issue", "view", str(number), "--json", "number,title,body,state,url"))

    def refs(self):
        output = self.git("ls-remote", "--refs", self.remote, NAMESPACE + "*")
        return {line.split("\t")[1]: line.split("\t")[0] for line in output.splitlines()}

    def read_claim(self, oid):
        self.git("fetch", "--quiet", "--no-write-fetch-head", self.remote, oid)
        claim = json.loads(self.git("show", f"{oid}:claim.json"))
        if claim.get("version") != 1:
            raise CoordinationError("Unknown reservation format; update the coordinator before continuing")
        return claim

    def claims(self):
        result = []
        for ref, oid in self.refs().items():
            if ref == GATE:
                continue
            claim = self.read_claim(oid)
            claim["oid"] = oid
            claim["ref"] = ref
            claim["scopes"] = [normalize_scope(p) for p in claim["scopes"]]
            result.append(claim)
        return sorted(result, key=lambda c: c["issue"])

    def metadata_commit(self, value):
        content = json.dumps({"version": 1, **value}, sort_keys=True) + "\n"
        blob = self.git("hash-object", "-w", "--stdin", data=content)
        tree = self.git("mktree", data=f"100644 blob {blob}\tclaim.json\n")
        return self.git("-c", "user.name=Agent Coordinator", "-c", "user.email=coordinator@localhost",
                        "-c", "commit.gpgsign=false", "commit-tree", tree, "-m", "Agent reservation metadata")

    def cas(self, ref, expected, new):
        # Empty expected means the ref MUST NOT exist. Never use unconditional force.
        self.git("push", "--porcelain", f"--force-with-lease={ref}:{expected}", self.remote, f"{new}:{ref}")

    @contextmanager
    def gate(self, worker):
        oid = self.metadata_commit({"worker": worker, "created_at": datetime.now(timezone.utc).isoformat(),
                                    "id": str(uuid.uuid4()), "kind": "scheduler"})
        try:
            self.cas(GATE, "", oid)
        except CoordinationError as error:
            raise CoordinationError("Scheduler reservation unavailable. Run 'status' and retry. "
                                    "Recover a crashed scheduler only after confirming it stopped.\n" + str(error)) from error
        try:
            yield
        finally:
            self.cas(GATE, oid, "")

    def local_path(self, number):
        return self.state_dir / f"issue-{number}.json"

    def local(self, number):
        path = self.local_path(number)
        if not path.exists():
            raise CoordinationError("This clone does not own the reservation. Use status to find its worker.")
        return json.loads(path.read_text())

    def save_local(self, number, state):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.local_path(number)
        temporary = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
        temporary.write_text(json.dumps(state, indent=2) + "\n")
        os.replace(temporary, path)

    def current_claim(self, number, *, owned=False):
        claim = next((c for c in self.claims() if c["issue"] == number), None)
        if claim is None:
            raise CoordinationError(f"Issue #{number} is not reserved")
        if owned and self.local(number)["oid"] != claim["oid"]:
            raise CoordinationError("Reservation has changed; this clone no longer owns it")
        return claim

    def claim(self, issue, worker):
        if issue["state"] != "OPEN":
            raise CoordinationError("Only open issues can be reserved")
        scopes = task_scopes(issue)
        for number in dependencies(issue):
            if self.issue(number)["state"] != "CLOSED":
                raise CoordinationError(f"Dependency #{number} is still open")
        number = issue["number"]
        if self.local_path(number).exists():
            raise CoordinationError("Local reservation state already exists; inspect status before proceeding")
        claim = {"version": 1, "issue": number, "url": issue["url"], "title": issue["title"],
                 "worker": worker, "scopes": scopes, "base": self.base,
                 "branch": f"agent/issue-{number}-{uuid.uuid4().hex[:8]}",
                 "created_at": datetime.now(timezone.utc).isoformat(), "id": str(uuid.uuid4())}
        with self.gate(worker):
            for existing in self.claims():
                if existing["issue"] == number or overlap(scopes, existing["scopes"]):
                    raise CoordinationError(f"Conflicts with issue #{existing['issue']} held by {existing['worker']}")
            oid = self.metadata_commit(claim)
            self.cas(NAMESPACE + f"issue-{number}", "", oid)
            claim["oid"] = oid
            self.save_local(number, claim)
        return claim

    def base_oid(self):
        ref = "refs/agent-coordination/base/" + uuid.uuid4().hex
        try:
            self.git("fetch", "--quiet", "--no-write-fetch-head", self.remote, f"refs/heads/{self.base}:{ref}")
            return self.git("rev-parse", ref)
        finally:
            self.git("update-ref", "-d", ref)

    def start(self, number, worker, worktree=None, here=False):
        if not worker.strip():
            raise CoordinationError("Worker must identify a person and tool, e.g. alice-codex-1")
        if here and self.git("status", "--porcelain"):
            raise CoordinationError("--here requires a clean checkout")
        target = Path(worktree).expanduser().resolve() if worktree else self.root
        if worktree and target.exists():
            raise CoordinationError("Worktree path already exists; choose a new path")
        base = self.base_oid()  # Verify the published base exists BEFORE taking a reservation.
        claim = self.claim(self.issue(number), worker)
        try:
            if worktree:
                self.git("worktree", "add", "-b", claim["branch"], str(target), base)
            else:
                self.git("switch", "-c", claim["branch"], base)
            state = self.local(number)
            state.update(workspace=str(target), base_oid=base)
            self.save_local(number, state)
        except Exception:
            self.release(number)
            raise
        prompt = (f"Work on {claim['url']} in {target}. Worker: {worker}.\n"
                  f"Use branch {claim['branch']}; issue #{number} is already reserved.\n"
                  f"Allowed paths: {', '.join(claim['scopes'])}. Read AGENTS.md and the full issue.\n"
                  "Do not spawn extra workers or widen scope. Stop and report dependencies or shared-file changes.\n"
                  f"Run python3 scripts/agents.py check {number}, commit your work, rerun check, then\n"
                  f"python3 scripts/agents.py pr {number} --ready --summary '<what changed>'.\n"
                  "Keep the reservation until the PR is merged. Do not merge without the merge command's checks.\n")
        print(prompt)
        return claim

    def release(self, number):
        with self.gate("release"):
            claim = self.current_claim(number, owned=True)
            self.cas(claim["ref"], claim["oid"], "")
            self.local_path(number).unlink()

    def assert_branch(self, number):
        claim = self.current_claim(number, owned=True)
        state = self.local(number)
        if self.git("branch", "--show-current") != claim["branch"]:
            raise CoordinationError(f"Run this command inside the task workspace: {state.get('workspace', claim['branch'])}")
        return claim, state

    def check(self, number):
        claim, state = self.assert_branch(number)
        # A later failed attempt must invalidate an earlier success at the same commit.
        state.pop("validation", None)
        self.save_local(number, state)
        base = self.base_oid()
        ancestor = self.git("merge-base", base, "HEAD")
        changed = set(self.git("diff", "--name-only", "--no-renames", "-z", ancestor, "HEAD").split("\0"))
        changed.update(self.git("diff", "--name-only", "--no-renames", "-z", "HEAD").split("\0"))
        changed.update(self.git("ls-files", "--others", "--exclude-standard", "-z").split("\0"))
        changed.discard("")
        violations = scope_violations(changed, claim["scopes"])
        if violations:
            raise CoordinationError("Changes outside the reserved scope: " + ", ".join(sorted(violations)))
        commands = self.config.get("checks", [])
        if not commands:
            raise CoordinationError("Configure at least one real validation command in coordination.json")
        for command in commands:
            if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
                raise CoordinationError("checks must be nonempty argument arrays, not shell commands")
            print("Running:", shlex.join(command), flush=True)
            result = subprocess.run(command, cwd=self.root)
            if result.returncode:
                raise CoordinationError(f"Validation failed ({result.returncode}): {shlex.join(command)}")
        state["validation"] = {"head": self.git("rev-parse", "HEAD"),
                               "clean": not bool(self.git("status", "--porcelain")),
                               "commands": commands}
        self.save_local(number, state)
        print("Scope and validation checks passed.")

    def pr(self, number, summary, ready=False):
        claim, state = self.assert_branch(number)
        head = self.git("rev-parse", "HEAD")
        validation = state.get("validation", {})
        if self.git("status", "--porcelain") or validation.get("head") != head or not validation.get("clean"):
            raise CoordinationError("Commit changes, then run 'check' on the clean task branch before opening a PR")
        if state.get("pr"):
            print(state["pr"])
            return
        self.git("push", "--set-upstream", self.remote, claim["branch"])
        body = (f"{summary}\n\nCloses #{number}\n\n"
                f"Reserved paths: {', '.join(claim['scopes'])}\n\nValidation:\n" +
                "\n".join(f"- `{shlex.join(c)}`" for c in validation["commands"]) + "\n")
        with tempfile.TemporaryDirectory(prefix="agent-pr-") as temporary:
            path = Path(temporary) / "body.md"
            path.write_text(body)
            args = ["pr", "create", "--base", claim["base"], "--head", claim["branch"],
                    "--title", f"{claim['title']} (#{number})", "--body-file", str(path)]
            if not ready:
                args.append("--draft")
            url = self.gh(*args)
        state["pr"] = url
        self.save_local(number, state)
        print(url)

    def pr_info(self, value):
        return json.loads(self.gh("pr", "view", value, "--json",
                                 "url,state,isDraft,headRefName,headRefOid,baseRefName,reviewDecision,statusCheckRollup"))

    def merge(self, number, pr_url):
        with self.gate("merge"):
            claim = self.current_claim(number)
            pr = self.pr_info(pr_url)
            if pr["headRefName"] != claim["branch"] or pr["baseRefName"] != claim["base"]:
                raise CoordinationError("PR branches do not match the issue reservation")
            if pr["state"] != "OPEN" or pr["isDraft"]:
                raise CoordinationError("PR must be open and ready for review")
            if pr.get("reviewDecision") in ("CHANGES_REQUESTED", "REVIEW_REQUIRED"):
                raise CoordinationError("GitHub requires review before merging")
            required = self.config.get("required_checks", [])
            if not required:
                raise CoordinationError("Configure required_checks before enabling orchestrator merges")
            successful_checks(pr, required)
            latest_base = self.base_oid()
            self.git("fetch", "--quiet", "--no-write-fetch-head", self.remote, pr["headRefOid"])
            try:
                self.git("merge-base", "--is-ancestor", latest_base, pr["headRefOid"])
            except CoordinationError as error:
                raise CoordinationError("Update the task branch from the current base and rerun CI before merging") from error
            # The server also enforces repository protection; never bypass it with --admin.
            self.gh("pr", "merge", pr["url"], "--squash", "--match-head-commit", pr["headRefOid"])
            self.complete_claim(number, self.pr_info(pr["url"]))

    def complete_claim(self, number, pr):
        claim = self.current_claim(number)
        if (pr["state"] != "MERGED" or pr["headRefName"] != claim["branch"]
                or pr["baseRefName"] != claim["base"]):
            raise CoordinationError("Only the matching merged PR can complete this reservation")
        self.cas(claim["ref"], claim["oid"], "")
        if self.local_path(number).exists() and self.local(number)["oid"] == claim["oid"]:
            self.local_path(number).unlink()
        print(f"Issue #{number} reservation released. Worktree and branch retained for inspection.")

    def finish(self, number, pr_url):
        with self.gate("finish"):
            self.complete_claim(number, self.pr_info(pr_url))

    def recover(self, number, scheduler, expected, reason):
        ref = GATE if scheduler else NAMESPACE + f"issue-{number}"
        if not reason.strip():
            raise CoordinationError("Recovery requires a reason")
        if self.refs().get(ref) != expected:
            raise CoordinationError("Reservation changed; inspect status again")
        if scheduler:
            self.cas(ref, expected, "")
        else:
            with self.gate("recover"):
                self.cas(ref, expected, "")
            if self.local_path(number).exists() and self.local(number)["oid"] == expected:
                self.local_path(number).unlink()
        print(f"Removed {ref} at {expected}. Reason: {reason}. No files or branches were deleted.")


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Show shared reservations, including crashed scheduler ownership")
    sub.add_parser("doctor", help="Check Git transport and GitHub issue API access")
    sub.add_parser("issues", help="List ready issues")
    for name in ("start", "dispatch"):
        c = sub.add_parser(name, help="Reserve a task and prepare an isolated workspace")
        if name == "start":
            c.add_argument("issue", type=int)
        c.add_argument("--worker", required=True)
        mode = c.add_mutually_exclusive_group(required=True)
        mode.add_argument("--worktree", help="New worktree path; dispatch appends issue-N")
        mode.add_argument("--here", action="store_true", help="Use this already isolated, clean checkout")
    for name in ("check", "release"):
        sub.add_parser(name).add_argument("issue", type=int)
    c = sub.add_parser("pr")
    c.add_argument("issue", type=int)
    c.add_argument("--summary", required=True)
    c.add_argument("--ready", action="store_true", help="Open a non-draft PR")
    for name in ("merge", "finish"):
        c = sub.add_parser(name)
        c.add_argument("issue", type=int)
        c.add_argument("--pr", required=True, help="PR number or URL")
    c = sub.add_parser("recover", help="Explicit recovery AFTER confirming the previous worker stopped")
    target = c.add_mutually_exclusive_group(required=True)
    target.add_argument("--issue", type=int)
    target.add_argument("--scheduler", action="store_true")
    c.add_argument("--expected", required=True, help="Exact current lock commit from status")
    c.add_argument("--reason", required=True)
    return p


def main():
    args = parser().parse_args()
    repo = Repo()
    if args.command == "doctor":
        repo.git("ls-remote", repo.remote, "HEAD")
        print(repo.gh("issue", "list", "--limit", "1", "--json", "number,title"))
        print("Git reads and GitHub issue API access work. A reservation will also require Git push permission.")
    elif args.command == "status":
        refs = repo.refs()
        if GATE in refs:
            print(json.dumps({"scheduler": repo.read_claim(refs[GATE]), "oid": refs[GATE]}, indent=2))
        print(json.dumps(repo.claims(), indent=2))
    elif args.command == "issues":
        print(repo.gh("issue", "list", "--state", "open", "--label", "agent:ready", "--limit", "100",
                      "--json", "number,title,url"))
    elif args.command == "start":
        repo.start(args.issue, args.worker, args.worktree, args.here)
    elif args.command == "dispatch":
        issues = json.loads(repo.gh("issue", "list", "--state", "open", "--label", "agent:ready",
                                    "--limit", "100", "--json", "number"))
        for issue in sorted(issues, key=lambda i: i["number"]):
            number = issue["number"]
            try:
                target = str(Path(args.worktree) / f"issue-{number}") if args.worktree else None
                repo.start(number, args.worker, target, args.here)
                break
            except CoordinationError as error:
                print(f"Skipped #{number}: {error}", file=sys.stderr)
        else:
            raise CoordinationError("No eligible task could be started; check diagnostics above")
    elif args.command == "check":
        repo.check(args.issue)
    elif args.command == "pr":
        repo.pr(args.issue, args.summary, args.ready)
    elif args.command == "release":
        repo.release(args.issue)
        print("Reservation released; files, worktrees, and branches retained. Stop the old worker before reassignment.")
    elif args.command == "recover":
        repo.recover(args.issue, args.scheduler, args.expected, args.reason)
    else:
        getattr(repo, args.command)(args.issue, args.pr)


if __name__ == "__main__":
    try:
        main()
    except (CoordinationError, OSError, ValueError, KeyError) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
