import json
import unittest
from unittest.mock import Mock, patch

import gateway


def check(name="unit", workflow="CI", status="COMPLETED", conclusion="SUCCESS"):
    return {
        "__typename": "CheckRun",
        "workflowName": workflow,
        "name": name,
        "status": status,
        "conclusion": conclusion,
    }


class MergeChecksTests(unittest.TestCase):
    def setUp(self):
        self.worker = object.__new__(gateway.Gateway)
        self.worker.cfg = {
            "required_checks": ["check-run:CI/unit"],
            "allowed_paths": ["src/*.py"],
            "require_branch_protection": False,
        }
        self.data = {
            "headRefOid": "head",
            "baseRefOid": "base",
            "baseRefName": "main",
            "state": "OPEN",
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "files": [{"path": "src/example.py"}],
            "statusCheckRollup": [check()],
        }
        self.worker.gh = Mock(side_effect=lambda *args: json.dumps(self.data))

    def gate(self):
        self.worker.merge_gate("pr", "head", "base")

    def test_queued_check_run(self):
        self.data["statusCheckRollup"] = [check(status="QUEUED", conclusion="")]
        with self.assertRaises(gateway.GatePending):
            self.gate()

    def test_pending_statuses(self):
        for status in ("QUEUED", "IN_PROGRESS", "PENDING", "REQUESTED", "WAITING"):
            for conclusion in (None, "", "absent"):
                run = check(status=status, conclusion=conclusion)
                if conclusion == "absent":
                    del run["conclusion"]
                self.data["statusCheckRollup"] = [run]
                with self.subTest(status=status, conclusion=conclusion):
                    with self.assertRaises(gateway.GatePending) as error:
                        self.gate()
                    self.assertIn("check-run:CI/unit", str(error.exception))
                    self.assertIn(f"status='{status}'", str(error.exception))

    def test_success(self):
        self.gate()

    def test_unsuccessful_conclusions(self):
        for conclusion in (
            "FAILURE",
            "CANCELLED",
            "TIMED_OUT",
            "ACTION_REQUIRED",
            "STARTUP_FAILURE",
            "SKIPPED",
            "NEUTRAL",
            "STALE",
        ):
            with self.subTest(conclusion=conclusion):
                self.data["statusCheckRollup"] = [check(conclusion=conclusion)]
                with self.assertRaises(gateway.ApprovalRequired) as error:
                    self.gate()
                self.assertNotIsInstance(error.exception, gateway.GatePending)
                self.assertIn("check-run:CI/unit", str(error.exception))
                self.assertIn("status='COMPLETED'", str(error.exception))
                self.assertIn(f"conclusion='{conclusion}'", str(error.exception))

    def test_missing_required(self):
        for checks in (None, [], [check(workflow="Other")]):
            with self.subTest(checks=checks):
                self.data["statusCheckRollup"] = checks
                with self.assertRaisesRegex(
                    gateway.GatePending, "Missing required CI: check-run:CI/unit"
                ):
                    self.gate()

    def test_duplicate_required_checks(self):
        for other, expected in (
            (check(), None),
            (check(status="QUEUED", conclusion=""), gateway.GatePending),
            (check(status="IN_PROGRESS", conclusion=None), gateway.GatePending),
            (check(conclusion="FAILURE"), gateway.ApprovalRequired),
            (check(conclusion="NEUTRAL"), gateway.ApprovalRequired),
        ):
            for checks in ([check(), other], [other, check()]):
                with self.subTest(checks=checks):
                    self.data["statusCheckRollup"] = checks
                    if expected is None:
                        self.gate()
                    else:
                        with self.assertRaises(expected) as error:
                            self.gate()
                        self.assertIs(type(error.exception), expected)
        for reverse in (False, True):
            checks = [
                check(status="QUEUED", conclusion=""),
                check(conclusion="FAILURE"),
            ]
            self.data["statusCheckRollup"] = checks[::-1] if reverse else checks
            with self.assertRaises(gateway.ApprovalRequired) as error:
                self.gate()
            self.assertIs(type(error.exception), gateway.ApprovalRequired)

    def test_nonrequired_checks_and_mergeability(self):
        for other in (
            check(name="optional", conclusion="SKIPPED"),
            check(name="optional", conclusion="NEUTRAL"),
            check(name="optional", conclusion="FAILURE"),
            check(name="optional", status="QUEUED", conclusion=""),
        ):
            with self.subTest(other=other):
                self.data["statusCheckRollup"] = [check(), other]
                self.data["mergeStateStatus"] = "CLEAN"
                self.gate()
                self.data["mergeStateStatus"] = "BLOCKED"
                with self.assertRaisesRegex(gateway.ApprovalRequired, "Merge blocked"):
                    self.gate()

    def test_status_context(self):
        self.worker.cfg["required_checks"] = ["status-context:external%2Ftest"]
        for state, expected in (
            ("SUCCESS", None),
            ("PENDING", gateway.GatePending),
            ("EXPECTED", gateway.GatePending),
            ("ERROR", gateway.ApprovalRequired),
            ("FAILURE", gateway.ApprovalRequired),
            (None, gateway.ApprovalRequired),
            ("UNKNOWN", gateway.ApprovalRequired),
        ):
            with self.subTest(state=state):
                self.data["statusCheckRollup"] = [
                    {
                        "__typename": "StatusContext",
                        "context": "external/test",
                        "state": state,
                    }
                ]
                if expected is None:
                    self.gate()
                else:
                    with self.assertRaises(expected) as error:
                        self.gate()
                    self.assertIs(type(error.exception), expected)
                    self.assertIn(f"state={state!r}", str(error.exception))

    def test_malformed_rollup_never_passes(self):
        for checks in (
            {},
            "unexpected",
            [None],
            [{}],
            [check(status="UNKNOWN")],
            [check(status=None)],
            [check(conclusion=None)],
            [check(conclusion="")],
            [check(conclusion="UNKNOWN")],
            [check(workflow=None)],
            [check(name="")],
            [check(status="QUEUED", conclusion="SUCCESS")],
            [check(), check(name="optional", status="UNKNOWN")],
        ):
            with self.subTest(checks=checks):
                self.data["statusCheckRollup"] = checks
                with self.assertRaises(gateway.ApprovalRequired) as error:
                    self.gate()
                self.assertIs(type(error.exception), gateway.ApprovalRequired)

    def test_canonical_identity_and_no_workflow_fallback(self):
        for workflow, name, identifier in (
            ("CI", "unit", "check-run:CI/unit"),
            ("", "unit", "check-run:/unit"),
            (
                "CI/core",
                "a /%:日本",
                "check-run:CI%2Fcore/a%20%2F%25%3A%E6%97%A5%E6%9C%AC",
            ),
        ):
            with self.subTest(identifier=identifier):
                self.worker.cfg["required_checks"] = [identifier]
                self.data["statusCheckRollup"] = [check(name=name, workflow=workflow)]
                self.gate()
        self.worker.cfg["required_checks"] = ["check-run:/unit"]
        self.data["statusCheckRollup"] = [check()]
        with self.assertRaises(gateway.GatePending):
            self.gate()

    def test_pr_11_shape(self):
        self.worker.cfg["required_checks"] = [
            "check-run:CI/core",
            "check-run:ci-medium/node-tests",
            "check-run:ci-fast/rust-tests",
            "check-run:CI/web",
        ]
        self.data["statusCheckRollup"] = [
            check("core"),
            check("core"),
            check("rust-tests", "ci-fast"),
            check("node-tests", "ci-medium"),
            check("web"),
            check("web"),
        ]
        self.gate()

    def test_real_gate_polling_and_timeout(self):
        snapshots = []
        for status, conclusion in (
            ("QUEUED", ""),
            ("IN_PROGRESS", None),
            ("COMPLETED", "SUCCESS"),
        ):
            snapshots.append(
                json.dumps(
                    {
                        **self.data,
                        "statusCheckRollup": [
                            check(status=status, conclusion=conclusion)
                        ],
                    }
                )
            )
        self.worker.gh = Mock(side_effect=snapshots)
        with (
            patch("gateway.time.sleep") as sleep,
            patch("gateway.time.monotonic", side_effect=[0, 1, 2]),
        ):
            self.worker.wait_merge_gate("pr", "head", "base")
        self.assertEqual(self.worker.gh.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.data["statusCheckRollup"] = [check(status="IN_PROGRESS", conclusion="")]
        self.worker.gh = Mock(return_value=json.dumps(self.data))
        self.worker.cfg["ci_wait_seconds"] = 7
        with (
            patch("gateway.time.sleep") as sleep,
            patch("gateway.time.monotonic", side_effect=[0, 5, 7]),
            self.assertRaisesRegex(
                gateway.ApprovalRequired, "timed out.*check-run:CI/unit.*IN_PROGRESS"
            ),
        ):
            self.worker.wait_merge_gate("pr", "head", "base")
        sleep.assert_called_once_with(2)

    def test_protection_uses_actual_contexts(self):
        self.worker.cfg.update(require_branch_protection=True, github_repo="owner/repo")
        protection = {
            "enforce_admins": {"enabled": True},
            "required_status_checks": {"strict": True, "checks": [{"context": "unit"}]},
        }
        self.worker.run = Mock(side_effect=lambda *args: json.dumps(protection))
        self.gate()
        for context in ("other", "check-run:CI/unit", "CI/unit (pull_request)"):
            protection["required_status_checks"]["checks"] = [{"context": context}]
            with self.assertRaisesRegex(
                gateway.ApprovalRequired, "Strict branch protection"
            ):
                self.gate()
