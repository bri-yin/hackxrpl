import importlib.util
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
SPEC = importlib.util.spec_from_file_location("verify_pr", Path(__file__).resolve().parents[1] / "scripts/verify_pr.py")
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)


class CIScopeTests(unittest.TestCase):
    def setUp(self):
        self.event = {"pull_request": {
            "head": {"ref": "agent/issue-12-abcd1234", "repo": {"full_name": "example/project"}},
            "base": {"ref": "main"}}}
        self.claim = {"version": 1, "issue": 12, "branch": "agent/issue-12-abcd1234", "base": "main", "scopes": ["app"]}

    def test_reserved_scope_passes(self):
        verify.validate(self.event, "example/project", self.claim, ["app/one.py", "app/two.py"])

    def test_outside_scope_fails(self):
        with self.assertRaises(verify.CoordinationError):
            verify.validate(self.event, "example/project", self.claim, ["shared.txt"])

    def test_reassigned_issue_fails_old_branch(self):
        self.claim["branch"] = "agent/issue-12-efab5678"
        with self.assertRaises(verify.CoordinationError):
            verify.validate(self.event, "example/project", self.claim, ["app/one.py"])

    def test_fork_cannot_reuse_a_reservation_name(self):
        self.event["pull_request"]["head"]["repo"]["full_name"] = "other/project"
        with self.assertRaises(verify.CoordinationError):
            verify.validate(self.event, "example/project", self.claim, ["app/one.py"])

    def test_wrong_base_fails(self):
        self.event["pull_request"]["base"]["ref"] = "other"
        with self.assertRaises(verify.CoordinationError):
            verify.validate(self.event, "example/project", self.claim, ["app/one.py"])
