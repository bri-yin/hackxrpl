#!/usr/bin/env python3
"""CI scope gate for task PRs. Reads reservation metadata with a read-only token."""
import base64
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import quote

from agents import CoordinationError, normalize_scope, run, scope_violations


def validate(event, repository, claim, changed):
    pr = event["pull_request"]
    branch = pr["head"]["ref"]
    match = re.fullmatch(r"agent/issue-(\d+)-[0-9a-f]{8}", branch)
    if not match or pr["head"]["repo"]["full_name"] != repository:
        raise CoordinationError("Task PRs must use a reserved agent branch in this repository")
    if (claim.get("version") != 1 or claim.get("issue") != int(match.group(1))
            or claim.get("branch") != branch or claim.get("base") != pr["base"]["ref"]):
        raise CoordinationError("PR does not match its active issue reservation")
    scopes = [normalize_scope(p) for p in claim["scopes"]]
    violations = scope_violations(changed, scopes)
    if not scopes or violations:
        raise CoordinationError("PR changes exceed its reserved scope: " + ", ".join(violations))


def main():
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    repository = os.environ["GITHUB_REPOSITORY"]
    pr = event["pull_request"]
    match = re.fullmatch(r"agent/issue-(\d+)-[0-9a-f]{8}", pr["head"]["ref"])
    if not match:
        raise CoordinationError("PR branch was not created by the coordination CLI")
    ref = quote(f"coordination/issue-{int(match.group(1))}", safe="")
    data = json.loads(run(["gh", "api", f"repos/{repository}/contents/claim.json?ref={ref}"]))
    claim = json.loads(base64.b64decode(data["content"], validate=False))
    # Full history is checked out by CI. No issue text is ever executed.
    output = run(["git", "diff", "--name-only", "--no-renames", "-z",
                  f"{pr['base']['sha']}...{pr['head']['sha']}"])
    changed = [path for path in output.split("\0") if path]
    validate(event, repository, claim, changed)
    print(f"PR scope matches issue #{claim['issue']} ({len(changed)} changed paths).")


if __name__ == "__main__":
    try:
        main()
    except (CoordinationError, OSError, ValueError, KeyError) as error:
        print(f"Scope verification failed: {error}", file=sys.stderr)
        sys.exit(1)
