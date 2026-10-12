import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import gateway


def config(repo):
    return {
        "repo": str(repo),
        "github_repo": "owner/repo",
        "allowed_paths": ["src/*.py", "tests/*.py", "docs/*.md"],
        "validation": {"unit": ["python3", "-c", "assert True"]},
        "full_validation": ["unit"],
        "required_checks": ["check-run:CI/unit"],
        "repair_attempts": 1,
    }


def git(repo, *args):
    return subprocess.check_output(
        ["git", "-c", "core.hooksPath=/dev/null", *args], cwd=repo, text=True
    ).strip()


class LocalGateway(gateway.Gateway):
    """Real Git/validation; fake service boundary, never sends data externally."""

    def __init__(self, *args, fail_task=False, fail_plan=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = []
        self.fail_task = fail_task
        self.fail_plan = fail_plan
        self.verifications = 0

    def gh(self, *args):
        self.calls.append(args)
        if args[:2] == ("issue", "list"):
            return json.dumps(
                [
                    {
                        "number": 1,
                        "title": "Add greeting",
                        "body": "$(touch should-never-exist)",
                        "labels": [],
                    }
                ]
            )
        return ""

    def claude(self, payload, schema, edit=False):
        if schema == gateway.PLAN_SCHEMA:
            return {
                "tasks": [
                    {
                        "title": "Add greeting",
                        "criteria": ["Returns hello"],
                        "tests": ["unit"],
                    }
                ]
            }
        return {
            "edits": [
                {
                    "path": "src/greeting.py",
                    "content": 'def greet():\n    return "hello"\n',
                }
            ]
        }

    def verify(self, payload):
        self.verifications += 1
        assert payload["verification_phase"] == ("TASK" if "task" in payload else "PLAN")
        if "plan" in payload and self.fail_plan:
            return {"verdict": "FAIL", "reasons": ["Missing criterion"]}
        if "diff" in payload:
            assert [x["name"] for x in payload["test_results"]] == payload["tests"]
            for result in payload["test_results"]:
                assert result["argv"] == self.cfg["validation"][result["name"]]
                assert result["status"] == "PASS"
                assert isinstance(result["seconds"], float)
                assert "output_tail" in result
            assert "def greet" in payload["diff"], (
                "New file must appear in verified diff"
            )
            if self.fail_task and self.verifications == 2:
                return {"verdict": "FAIL", "reasons": ["Repair this task"]}
        return {"verdict": "PASS", "reasons": []}


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Gateway Test")
        (self.repo / "docs").mkdir()
        (self.repo / "docs/readme.md").write_text("Baseline")
        git(self.repo, "add", "docs/readme.md")
        git(self.repo, "commit", "-m", "baseline")
        git(self.repo, "remote", "add", "origin", "https://github.com/owner/repo.git")
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.cfg = config(self.repo)

    def tearDown(self):
        self.temp.cleanup()

    def worker(self, **kwargs):
        w = LocalGateway(self.cfg, self.root / "state", **kwargs)
        original = w.git
        w.git = lambda *args: "" if args[0] == "fetch" else original(*args)
        return w

    def test_local_end_to_end_repair_new_file_and_duplicate_prevention(self):
        w = self.worker(fail_task=True)
        self.assertTrue(w.once())
        state = json.loads((w.state / "issue-1.json").read_text())
        self.assertEqual(state["state"], "pr-ready")
        self.assertEqual(w.verifications, 3)
        worktree = Path(state["worktree"])
        self.assertIn("hello", (worktree / "src/greeting.py").read_text())
        self.assertEqual(git(worktree, "status", "--porcelain"), "")
        self.assertFalse((worktree / "should-never-exist").exists())
        self.assertFalse(w.once())
        self.assertFalse(any(c[:2] == ("pr", "create") for c in w.calls))

    def test_bootstrap_order_worktree_once_across_tasks_and_repairs(self):
        # Ignored dependencies exist only in the original checkout initially.
        (self.repo / ".gitignore").write_text("dependencies/\n")
        git(self.repo, "add", ".gitignore")
        git(self.repo, "commit", "-m", "ignore generated dependencies")
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        (self.repo / "dependencies").mkdir()
        (self.repo / "dependencies/ready").write_text("original")
        self.cfg["bootstrap"] = [
            ["python3", "-c", 'from pathlib import Path; p=Path("dependencies"); assert not p.exists(); p.mkdir(); (p/"ready").write_text("first")'],
            ["python3", "-c", 'from pathlib import Path; p=Path("dependencies/ready"); assert p.read_text()=="first"; p.write_text(str(Path.cwd()))'],
        ]
        self.cfg["validation"]["unit"] = ["python3", "-c", 'from pathlib import Path; assert Path("dependencies/ready").read_text()==str(Path.cwd())']
        w = self.worker(fail_task=True)
        original = w.claude

        def claude(payload, schema):
            self.assertEqual((w.cwd / "dependencies/ready").read_text(), str(w.cwd))
            result = original(payload, schema)
            if schema == gateway.PLAN_SCHEMA:
                result["tasks"] *= 2
            else:
                result["edits"][0]["content"] = f'def greet(value={w.verifications}):\n    return "hello"\n'
            return result

        with patch.object(w, "claude", side_effect=claude), patch.object(w, "run", wraps=w.run) as run:
            w.once()
        state = json.loads((w.state / "issue-1.json").read_text())
        self.assertEqual(state["state"], "pr-ready")
        self.assertEqual([t["attempts"] for t in state["completed_tasks"]], [2, 1])
        for argv in self.cfg["bootstrap"]:
            calls = [c for c in run.call_args_list if c.args[0] == argv]
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0].kwargs["cwd"], Path(state["worktree"]))
        self.assertEqual((self.repo / "dependencies/ready").read_text(), "original")

    def test_bootstrap_failure_stops_without_implementation_or_repair(self):
        for failure in (gateway.ApprovalRequired("Command failed"),
                        gateway.ApprovalRequired("Timeout: tool"),
                        FileNotFoundError("missing executable")):
            with self.subTest(failure=failure):
                self.cfg["bootstrap"] = [["prepare", "dependencies"], ["never-run"]]
                w = self.worker()
                original = w.run
                executed = []

                def run(argv, stdin=None, cwd=None, executed=executed, failure=failure, original=original):
                    if argv in self.cfg["bootstrap"]:
                        executed.append(argv)
                        raise failure
                    return original(argv, stdin, cwd)

                with (
                    patch.object(w, "run", side_effect=run),
                    patch.object(w, "claude") as claude,
                    patch.object(w, "tests") as tests,
                    patch.object(w, "verify") as verify,
                    self.assertRaisesRegex(gateway.BootstrapFailure, r"Bootstrap/environment failure.*bootstrap\[0\].*prepare"),
                ):
                    w.process({"number": 1, "title": "test", "body": ""})
                claude.assert_not_called()
                tests.assert_not_called()
                verify.assert_not_called()
                self.assertEqual(executed, [["prepare", "dependencies"]])
                state = json.loads((w.state / "issue-1.json").read_text())
                self.assertEqual(state["state"], "approval-required")
                self.assertNotIn("Task validation/disagreement unresolved", state["reason"])
                self.assertNotIn("completed_tasks", state)

    def test_bootstrap_real_command_failure_and_literal_shell_arguments(self):
        self.cfg["bootstrap"] = [[
            "python3", "-c",
            'import sys; assert sys.argv[1] == "$(touch injected)"; raise SystemExit(7)',
            "$(touch injected)",
        ]]
        w = self.worker()
        with patch.object(w, "claude") as claude, self.assertRaises(gateway.BootstrapFailure):
            w.once()
        claude.assert_not_called()
        state = json.loads((w.state / "issue-1.json").read_text())
        self.assertFalse((Path(state["worktree"]) / "injected").exists())
        logs = [json.loads(p.read_text()) for p in w.state.glob("1-*.json")]
        self.assertTrue(any(log["argv"] == self.cfg["bootstrap"][0] and log["status"] == 7 for log in logs))

    def test_empty_bootstrap_preserves_existing_behavior(self):
        self.cfg["bootstrap"] = []
        w = self.worker()
        w.once()
        self.assertEqual(json.loads((w.state / "issue-1.json").read_text())["state"], "pr-ready")

    def test_bootstrap_config_requires_argv_arrays(self):
        for value in (None, "echo hi", {}, ["echo hi"], [[]], [[""]], [[1]], [["a\x00b"]]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                gateway.validate_config({**self.cfg, "bootstrap": value})
        gateway.validate_config({**self.cfg, "bootstrap": []})

    def test_bootstrap_never_uses_issue_or_agent_commands(self):
        w = self.worker()
        original = w.claude
        injected = [["untrusted-bootstrap", "$(touch injected)"]]

        def claude(payload, schema):
            return {**original(payload, schema), "bootstrap": injected}

        with patch.object(w, "claude", side_effect=claude), patch.object(w, "run", wraps=w.run) as run:
            w.process({"number": 1, "title": "bootstrap", "body": json.dumps({"bootstrap": injected}), "bootstrap": injected})
        self.assertFalse(any(c.args[0] == injected[0] for c in run.call_args_list))
        self.assertEqual(json.loads((w.state / "issue-1.json").read_text())["state"], "pr-ready")

    def test_plan_disagreement_stops_before_edits(self):
        w = self.worker(fail_plan=True)
        with self.assertRaises(gateway.ApprovalRequired):
            w.once()
        state = json.loads((w.state / "issue-1.json").read_text())
        self.assertEqual(state["state"], "approval-required")
        self.assertFalse((Path(state["worktree"]) / "src/greeting.py").exists())

    def test_full_validation_failure_stops_before_publish(self):
        self.cfg["validation"]["full"] = [
            "python3",
            "-c",
            'import pathlib, sys; sys.exit(pathlib.Path("src/greeting.py").exists())',
        ]
        self.cfg["full_validation"] = ["full"]
        w = self.worker()
        with self.assertRaises(gateway.ApprovalRequired):
            w.once()
        self.assertEqual(
            json.loads((w.state / "issue-1.json").read_text())["state"],
            "approval-required",
        )
        self.assertFalse(any(c[:2] == ("pr", "create") for c in w.calls))

    def test_task_test_source_mutation_rejected(self):
        self.cfg["validation"]["unit"] = [
            "python3",
            "-c",
            (
                'from pathlib import Path; p = Path("src/greeting.py");'
                ' p.exists() and p.write_text("changed")'
            ),
        ]
        w = self.worker()
        with self.assertRaisesRegex(gateway.ApprovalRequired, "modified"):
            w.once()

    def test_baseline_proves_target_was_green_before_any_edit(self):
        self.cfg["validation"]["unit"] = ["python3", "-c", "raise SystemExit(1)"]
        w = self.worker()
        with (
            patch.object(w, "claude") as claude,
            patch.object(w, "verify") as verify,
            self.assertRaisesRegex(
                gateway.BootstrapFailure, "Baseline validation failed before any edit"
            ),
        ):
            w.process({"number": 1, "title": "test", "body": ""})
        claude.assert_not_called()
        verify.assert_not_called()
        state = json.loads((w.state / "issue-1.json").read_text())
        self.assertEqual(state["state"], "approval-required")
        self.assertNotIn("completed_tasks", state)
        self.assertFalse((Path(state["worktree"]) / "src/greeting.py").exists())

    def test_baseline_rejects_validation_that_modifies_the_checkout(self):
        self.cfg["validation"]["unit"] = [
            "python3",
            "-c",
            'from pathlib import Path; Path("docs/readme.md").write_text("changed")',
        ]
        w = self.worker()
        with (
            patch.object(w, "claude") as claude,
            self.assertRaisesRegex(
                gateway.BootstrapFailure, "Baseline validation modified the checkout"
            ),
        ):
            w.process({"number": 1, "title": "test", "body": ""})
        claude.assert_not_called()

    def test_measured_results_recorded_for_baseline_tasks_and_pull_request(self):
        w = self.worker()
        self.assertTrue(w.once())
        state = json.loads((w.state / "issue-1.json").read_text())
        self.assertEqual(state["baseline"], [{"name": "unit", "status": "PASS",
                                              "seconds": state["baseline"][0]["seconds"]}])
        self.assertIsInstance(state["baseline"][0]["seconds"], float)
        self.assertEqual(
            [x["status"] for x in state["completed_tasks"][0]["test_results"]], ["PASS"]
        )
        body = Path(state["audit"]).read_text()
        self.assertIn("Baseline before any edit: unit PASS", body)
        self.assertIn("Full validation after changes: unit PASS", body)
        self.assertIn("Test files changed: none", body)
        record = json.loads(body.split("```json\n")[1].split("\n```")[0])
        self.assertEqual(record["tests_changed"], [])
        self.assertEqual([x["status"] for x in record["full_validation_results"]], ["PASS"])
        self.assertNotIn("output_tail", record["baseline"][0])

    def test_failed_validation_output_reaches_repair_and_operator(self):
        self.cfg["validation"]["unit"] = [
            "python3",
            "-c",
            (
                "from pathlib import Path; import sys;"
                ' sys.exit(sys.stderr.write("greeting assertion exploded") and 1'
                ' if Path("src/greeting.py").exists() else 0)'
            ),
        ]
        w = self.worker()
        with self.assertRaises(gateway.ApprovalRequired):
            w.once()
        state = json.loads((w.state / "issue-1.json").read_text())
        self.assertEqual(state["state"], "approval-required")
        self.assertTrue(
            any("greeting assertion exploded" in x for x in state["task_feedback"]),
            state["task_feedback"],
        )

    def test_stop_is_explained_on_the_issue_without_leaking_evidence(self):
        self.cfg["validation"]["unit"] = [
            "python3",
            "-c",
            (
                "from pathlib import Path; import sys;"
                ' sys.exit(sys.stderr.write("secret assertion detail") and 1'
                ' if Path("src/greeting.py").exists() else 0)'
            ),
        ]
        w = self.published_worker()
        with self.assertRaises(gateway.ApprovalRequired):
            w.once()
        comments = [c for c in w.calls if c[:2] == ("issue", "comment")]
        self.assertEqual(len(comments), 1, w.calls)
        self.assertEqual(comments[0][2], "1")
        body = comments[0][comments[0].index("--body") + 1]
        self.assertIn("GATE_FAILED", body)
        self.assertIn("Last task gate verdict: **FAIL**", body)
        # Command output, verifier wording and private paths never reach GitHub.
        self.assertNotIn("secret assertion detail", body)
        self.assertNotIn(str(w.state), body)
        self.assertFalse(any(c[:2] == ("pr", "create") for c in w.calls))

    def test_dry_run_never_comments_on_the_issue(self):
        w = self.worker(fail_plan=True)
        with self.assertRaises(gateway.ApprovalRequired):
            w.once()
        self.assertFalse(any(c[:2] == ("issue", "comment") for c in w.calls))

    def test_failed_explanation_does_not_mask_the_original_stop(self):
        w = self.published_worker()
        w.fail_plan = True
        original = w.gh

        def gh(*args):
            if args[:2] == ("issue", "comment"):
                raise gateway.ApprovalRequired("Command failed: gh")
            return original(*args)

        with (
            patch.object(w, "gh", side_effect=gh),
            self.assertRaisesRegex(gateway.ApprovalRequired, "Plan"),
        ):
            w.once()
        state = json.loads((w.state / "issue-1.json").read_text())
        self.assertEqual(state["state"], "approval-required")
        self.assertFalse(any(c[:2] == ("pr", "create") for c in w.calls))

    def test_plan_context_states_bootstrap_already_ran(self):
        self.cfg["bootstrap"] = [["npm", "ci"], ["wasm-pack", "build", "core"]]
        w = self.worker()
        payloads = []
        original = w.claude

        def claude(payload, schema):
            payloads.append(payload)
            return original(payload, schema)

        with patch.object(w, "run", side_effect=lambda argv, stdin=None, cwd=None:
                          "" if argv in self.cfg["bootstrap"] else gateway.Gateway.run(w, argv, stdin, cwd)):
            with patch.object(w, "claude", side_effect=claude):
                self.assertTrue(w.once())
        self.assertTrue(payloads)
        for payload in payloads:
            self.assertEqual(payload["bootstrap"], self.cfg["bootstrap"])
        contract = gateway.VERIFICATION_CONTRACTS["PLAN"]
        self.assertIn("ALREADY executed", contract)
        self.assertIn("already performed", contract)
        self.assertNotIn("bootstrap", gateway.VERIFICATION_CONTRACTS["TASK"])

    def test_risk_and_escape_policy(self):
        for name in [
            "../src/x.py",
            "/src/x.py",
            "src/../x.py",
            ".env",
            "src/.env.test",
            ".github/workflows/test.yml",
            "src/auth.py",
            "src/config.py",
            "package.json",
            "Cargo.lock",
            "migrations/1.sql",
            "infra/main.tf",
            "gateway.py",
            "docs/secret.md",
            "AGENTS.md",
            "other/file.py",
        ]:
            with self.subTest(name=name), self.assertRaises(gateway.ApprovalRequired):
                gateway.check_paths([name], self.cfg)
        gateway.check_paths(["src/greeting.py", "docs/readme.md"], self.cfg)

    def test_symlink_edit_rejected(self):
        w = self.worker()
        (self.repo / "src").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(gateway.ApprovalRequired):
            w.apply({"edits": [{"path": "src/x.py", "content": "oops"}]})
        self.assertFalse((self.root / "x.py").exists())

    def test_unknown_test_rejected(self):
        with self.assertRaises(gateway.ApprovalRequired):
            self.worker().tests(["$(echo hacked)"])

    def test_merge_gate_fail_closed(self):
        for required in (None, True, False):
            with self.subTest(require_branch_protection=required):
                if required is not None:
                    self.cfg["require_branch_protection"] = required
                self.check_merge_gate_fail_closed()

    def test_required_check_config_is_canonical(self):
        for checks in (
            "check-run:CI/unit",
            [None],
            [123],
            ["unit"],
            ["CI/core (pull_request)"],
            ["check-run:CI/"],
            ["check-run:CI/a/b"],
            ["status-context:"],
            ["check-run:CI/a b"],
            ["check-run:CI/a%2fb"],
            ["status-context:%GG"],
            ["status-context:%FF"],
        ):
            with self.subTest(checks=checks), self.assertRaises((ValueError, TypeError)):
                gateway.validate_config({**self.cfg, "required_checks": checks})
        gateway.validate_config(
            {
                **self.cfg,
                "required_checks": [
                    "check-run:CI/unit",
                    "check-run:/unit",
                    "check-run:CI%2Fx/a%20b",
                    "status-context:external%2Ftest",
                ],
            }
        )

    def check_merge_gate_fail_closed(self):
        w = self.worker()
        good = {
            "headRefOid": "head",
            "baseRefOid": "base",
            "baseRefName": "main",
            "state": "OPEN",
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "files": [{"path": "src/greeting.py"}],
            "statusCheckRollup": [
                {
                    "__typename": "CheckRun",
                    "workflowName": "CI",
                    "status": "COMPLETED",
                    "name": "unit",
                    "conclusion": "SUCCESS",
                }
            ],
        }
        w.cfg["protected_paths"] = ["src/payments.py"]
        protection = {
            "enforce_admins": {"enabled": True},
            "required_status_checks": {"strict": True, "contexts": ["unit"]},
        }
        w.gh = lambda *args: json.dumps(good)
        w.run = lambda *args, **kwargs: json.dumps(protection)
        w.merge_gate("pr", "head", "base")
        for key, value in [
            ("headRefOid", "other"),
            ("baseRefOid", "new"),
            ("baseRefName", "other"),
            ("state", "CLOSED"),
            ("isDraft", True),
            ("mergeable", "CONFLICTING"),
            ("mergeable", "UNKNOWN"),
            ("mergeStateStatus", "UNKNOWN"),
            ("mergeStateStatus", "BEHIND"),
            ("mergeStateStatus", "BLOCKED"),
            ("statusCheckRollup", []),
            (
                "statusCheckRollup",
                [
                    {
                        "__typename": "CheckRun",
                        "workflowName": "CI",
                        "status": "COMPLETED",
                        "name": "unit",
                        "conclusion": "SKIPPED",
                    }
                ],
            ),
            (
                "statusCheckRollup",
                [
                    {
                        "__typename": "CheckRun",
                        "workflowName": "CI",
                        "status": "COMPLETED",
                        "name": "unit",
                        "conclusion": None,
                    }
                ],
            ),
            ("files", [{"path": "src/auth.py"}]),
            ("files", [{"path": "outside/file.py"}]),
            ("files", [{"path": "src/payments.py"}]),
            *[
                (
                    "statusCheckRollup",
                    [
                        {
                            "__typename": "CheckRun",
                            "workflowName": "CI",
                            "status": "COMPLETED",
                            "name": "unit",
                            "conclusion": status,
                        }
                    ],
                )
                for status in (
                    "FAILURE",
                    "CANCELLED",
                    "TIMED_OUT",
                    "UNKNOWN",
                    "NEUTRAL",
                )
            ],
            (
                "statusCheckRollup",
                [
                    {
                        "__typename": "CheckRun",
                        "workflowName": "CI",
                        "status": "COMPLETED",
                        "name": "other",
                        "conclusion": "SUCCESS",
                    }
                ],
            ),
            (
                "statusCheckRollup",
                [
                    {
                        "__typename": "CheckRun",
                        "workflowName": "CI",
                        "status": "COMPLETED",
                        "name": "unit",
                        "conclusion": "SUCCESS",
                    },
                    {
                        "__typename": "CheckRun",
                        "workflowName": "CI",
                        "status": "COMPLETED",
                        "name": "unit",
                        "conclusion": "FAILURE",
                    },
                ],
            ),
        ]:
            changed = copy.deepcopy(good)
            changed[key] = value
            w.gh = lambda *args, d=changed: json.dumps(d)
            with (
                self.subTest(key=key, value=value),
                self.assertRaises(gateway.ApprovalRequired),
            ):
                w.merge_gate("pr", "head", "base")
        w.gh = lambda *args: json.dumps(good)
        protection["required_status_checks"]["strict"] = False
        if self.cfg.get("require_branch_protection", True):
            with self.assertRaises(gateway.ApprovalRequired):
                w.merge_gate("pr", "head", "base")
        else:
            w.merge_gate("pr", "head", "base")

    def test_optional_branch_protection_and_api_errors(self):
        good = {
            "headRefOid": "head",
            "baseRefOid": "base",
            "baseRefName": "main",
            "state": "OPEN",
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "files": [{"path": "src/greeting.py"}],
            "statusCheckRollup": [
                {
                    "__typename": "CheckRun",
                    "workflowName": "CI",
                    "status": "COMPLETED",
                    "name": "unit",
                    "conclusion": "SUCCESS",
                }
            ],
        }
        for required in (None, True, False):
            if required is not None:
                self.cfg["require_branch_protection"] = required
            w = self.worker()
            for status in (403, 404, 500):
                with (
                    self.subTest(required=required, status=status),
                    patch.object(w, "gh", return_value=json.dumps(good)),
                    patch.object(
                        w, "run", side_effect=gateway.ApprovalRequired(f"HTTP {status}")
                    ) as api,
                ):
                    if required is False:
                        w.merge_gate("pr", "head", "base")
                        api.assert_not_called()
                    else:
                        with self.assertRaises(gateway.ApprovalRequired):
                            w.merge_gate("pr", "head", "base")
                        api.assert_called_once()
            with (
                patch.object(w, "gh", side_effect=gateway.ApprovalRequired("HTTP 403")),
                self.assertRaises(gateway.ApprovalRequired),
            ):
                w.merge_gate("pr", "head", "base")

    def test_branch_protection_option_must_be_boolean(self):
        for value in (None, 0, 1, "false", "true", [], {}):
            with self.subTest(value=value), self.assertRaises(TypeError):
                gateway.validate_config(
                    {**self.cfg, "require_branch_protection": value}
                )

    def test_claude_command_is_restricted_and_prompt_is_stdin(self):
        w = self.worker()
        with patch.object(
            w,
            "run",
            return_value=json.dumps(
                {"session_id": "id", "structured_output": {"tasks": []}}
            ),
        ) as run:
            gateway.Gateway.claude(w, {"issue": "$(touch bad)"}, gateway.PLAN_SCHEMA)
        argv, stdin = run.call_args.args
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertNotIn("$(touch bad)", argv)
        self.assertIn("$(touch bad)", stdin)

    def test_config_rejects_untrusted_commands_and_missing_ci(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["validation"]["unit"] = "echo unsafe"
        with self.assertRaises(ValueError):
            gateway.validate_config(cfg)
        cfg = copy.deepcopy(self.cfg)
        cfg["required_checks"] = []
        with self.assertRaises(ValueError):
            gateway.validate_config(cfg)

    def test_timeout_stops_child_process_and_escalates(self):
        w = self.worker()
        w.cfg["timeout_seconds"] = 1
        with self.assertRaisesRegex(gateway.ApprovalRequired, "Timeout"):
            w.run(["python3", "-c", "import time; time.sleep(10)"])

    def published_worker(self, blocked=False):
        w = self.worker(publish=True)
        original_git = w.git
        w.git = lambda *args: "" if args[0] == "push" else original_git(*args)

        def gh(*args):
            w.calls.append(args)
            if args[:2] == ("issue", "list"):
                return json.dumps(
                    [
                        {
                            "number": 1,
                            "title": "Add greeting",
                            "body": "safe",
                            "labels": [],
                        }
                    ]
                )
            if args[:2] == ("pr", "create"):
                return "https://github.com/owner/repo/pull/1"
            if args[:2] == ("pr", "view"):
                if args[-1] == "state,headRefOid":
                    return json.dumps(
                        {"state": "MERGED", "headRefOid": w.job_data["head"]}
                    )
                return json.dumps(
                    {
                        "headRefOid": w.job_data["head"],
                        "baseRefOid": w.job_data["base"],
                        "baseRefName": "main",
                        "state": "OPEN",
                        "isDraft": False,
                        "mergeable": "MERGEABLE",
                        "mergeStateStatus": "CLEAN",
                        "files": [{"path": "src/greeting.py"}],
                        "statusCheckRollup": [
                            {
                                "__typename": "CheckRun",
                                "workflowName": "CI",
                                "status": "COMPLETED",
                                "name": "unit",
                                "conclusion": "FAILURE" if blocked else "SUCCESS",
                            }
                        ],
                    }
                )
            return ""

        w.gh = gh
        original_run = w.run

        def run(argv, *args, **kwargs):
            if argv[:2] == ["gh", "api"]:
                return json.dumps(
                    {
                        "enforce_admins": {"enabled": True},
                        "required_status_checks": {
                            "strict": True,
                            "contexts": ["unit"],
                        },
                    }
                )
            return original_run(argv, *args, **kwargs)

        w.run = run
        return w

    def test_publish_safe_gate_merges_exact_head_then_closes_issue(self):
        w = self.published_worker()
        self.assertTrue(w.once())
        merge = next(c for c in w.calls if c[:2] == ("pr", "merge"))
        self.assertEqual(merge[-2:], ("--match-head-commit", w.job_data["head"]))
        close_index = next(
            i for i, c in enumerate(w.calls) if c[:2] == ("issue", "close")
        )
        self.assertGreater(close_index, w.calls.index(merge))
        self.assertEqual(
            json.loads((w.state / "issue-1.json").read_text())["state"], "done"
        )

    def test_publish_without_protection_preserves_verification_gates(self):
        self.cfg["require_branch_protection"] = False
        for failure in (None, "plan", "task", "full", "risk", "ci", "api"):
            with self.subTest(failure=failure):
                self.check_publish_verification_gates(failure)

    def check_publish_verification_gates(self, failure):
        w = self.published_worker(blocked=failure == "ci")
        original_verify = w.verify
        original_run = w.run
        original_gh = w.gh

        def verify(payload):
            if failure == "risk":
                raise gateway.ApprovalRequired("Risky change")
            if (failure == "plan" and "plan" in payload) or (
                failure == "task" and "diff" in payload
            ):
                return {"verdict": "FAIL", "reasons": ["Rejected"]}
            return original_verify(payload)

        def run(argv, *args, **kwargs):
            if argv[:2] == ["gh", "api"]:
                self.fail("Branch-protection API must not be called")
            return original_run(argv, *args, **kwargs)

        def gh(*args):
            if failure == "api" and args[:2] == ("pr", "view"):
                raise gateway.ApprovalRequired("HTTP 500")
            return original_gh(*args)

        w.verify = verify
        w.run = run
        w.gh = gh
        w.cfg = copy.deepcopy(self.cfg)
        if failure == "full":
            w.cfg["validation"]["full"] = [
                "python3",
                "-c",
                "raise SystemExit(1)",
            ]
            w.cfg["full_validation"] = ["full"]
        issue = {
            "number": 10 + len(list(w.state.glob("issue-*.json"))),
            "title": "Add greeting",
            "body": "safe",
        }
        if failure is None:
            w.process(issue)
            self.assertEqual(w.verifications, 2)
            merge = next(c for c in w.calls if c[:2] == ("pr", "merge"))
            self.assertEqual(merge[-2:], ("--match-head-commit", w.job_data["head"]))
        else:
            with self.assertRaises(gateway.ApprovalRequired):
                w.process(issue)
            self.assertFalse(
                any(c[:2] in (("pr", "merge"), ("issue", "close")) for c in w.calls)
            )
        state = json.loads((w.state / f"issue-{issue['number']}.json").read_text())
        self.assertEqual(
            state["state"], "done" if failure is None else "approval-required"
        )

    def test_publish_failed_ci_retains_pr_and_never_merges(self):
        w = self.published_worker(blocked=True)
        with self.assertRaises(gateway.ApprovalRequired):
            w.once()
        state = json.loads((w.state / "issue-1.json").read_text())
        self.assertIn("/pull/1", state["pr"])
        self.assertEqual(state["state"], "approval-required")
        self.assertFalse(
            any(c[:2] in (("pr", "merge"), ("issue", "close")) for c in w.calls)
        )

    def test_fast_mode_never_auto_merges(self):
        self.cfg["mode"] = "FAST"
        w = self.published_worker()
        w.once()
        self.assertEqual(w.verifications, 0)
        self.assertFalse(any(c[:2] == ("pr", "merge") for c in w.calls))

    def test_ci_pending_retries_then_passes_and_timeout_escalates(self):
        w = self.worker()
        with patch.object(
            w, "merge_gate", side_effect=[gateway.GatePending("pending"), None]
        ) as gate:
            with patch("gateway.time.sleep"):
                w.wait_merge_gate("pr", "head", "base")
            self.assertEqual(gate.call_count, 2)
        with (
            patch.object(w, "merge_gate", side_effect=gateway.GatePending("pending")),
            patch("gateway.time.monotonic", side_effect=[0, 301]),
            self.assertRaisesRegex(gateway.ApprovalRequired, "timed out"),
        ):
            w.wait_merge_gate("pr", "head", "base")

    def test_codex_adapter_fresh_read_only_and_risk_escalation(self):
        w = self.worker()
        captured = []
        verdict = {"verdict": "PASS", "reasons": [], "risky": False}

        def run(argv, *args, **kwargs):
            captured.append(argv)
            Path(argv[argv.index("--output-last-message") + 1]).write_text(
                json.dumps(verdict)
            )
            self.assertNotEqual(Path(kwargs["cwd"]), w.repo)
            return ""

        w.run = run
        self.assertEqual(gateway.Gateway.verify(w, {"verification_phase": "TASK", "task": "safe"})["verdict"], "PASS")
        self.assertIn("read-only", captured[0])
        self.assertNotIn("--approve-for-me", captured[0])
        self.assertNotIn("--resume", captured[0])
        verdict["risky"] = True
        with self.assertRaisesRegex(gateway.ApprovalRequired, "risk"):
            gateway.Gateway.verify(w, {"verification_phase": "TASK", "task": "risky"})

    def test_plan_context_workspace_read_scope_is_not_write_scope(self):
        self.cfg["allowed_paths"] = ["README.md"]
        self.cfg["read_context_paths"] = ["package.json", "Cargo.toml", "prepare-wasm.mjs", "playwright.config.ts"]
        files = {"README.md": "Build instructions", "package.json": '{"scripts":{"build":"node prepare-wasm.mjs"}}',
                 "Cargo.toml": '[package]\nname = "demo"', "prepare-wasm.mjs": "// build wasm",
                 "playwright.config.ts": "// test configuration"}
        for name, content in files.items():
            (self.repo / name).write_text(content)
        git(self.repo, "add", "--", *files)
        git(self.repo, "commit", "-m", "context fixture")
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        w = self.worker()
        original_run = w.run
        seen = []

        def run(argv, stdin=None, cwd=None):
            if argv[0] != "codex":
                return original_run(argv, stdin, cwd)
            workspace = Path(cwd)
            seen.append(workspace)
            for name, content in files.items():
                expected = "Updated build instructions" if name == "README.md" and len(seen) > 1 else content
                self.assertEqual((workspace / name).read_text(), expected)
                self.assertEqual((workspace / name).stat().st_mode & 0o222, 0)
            self.assertIn("read-only", argv)
            self.assertNotIn("--approve-for-me", argv)
            Path(argv[argv.index("--output-last-message") + 1]).write_text(
                json.dumps({"verdict": "PASS", "reasons": [], "risky": False}))
            return ""

        w.run = run
        w.verify = lambda payload: gateway.Gateway.verify(w, payload)
        original_claude = w.claude

        def claude(payload, schema):
            if schema == gateway.PLAN_SCHEMA:
                self.assertEqual(payload["source_files"], files)
                return original_claude(payload, schema)
            return {"edits": [{"path": "README.md", "content": "Updated build instructions"}]}

        w.claude = claude
        w.process({"number": 9, "title": "Document build", "body": "Use package.json and Cargo.toml"})
        self.assertEqual(len(seen), 2)
        worktree = Path(w.job_data["worktree"])
        self.assertEqual(git(worktree, "diff", "--name-only", w.job_data["base"], "HEAD"), "README.md")
        self.assertEqual(json.loads((w.state / "issue-9.json").read_text())["state"], "pr-ready")
        gateway.check_paths(["README.md"], w.cfg)
        for name in files.keys() - {"README.md"}:
            with self.assertRaises(gateway.ApprovalRequired):
                w.apply({"edits": [{"path": name, "content": "changed"}]})

    def test_laplogos_nested_context_and_absent_root_manifest(self):
        self.cfg["allowed_paths"] = ["README.md"]
        files = {"README.md": "Build instructions", "laplogos_web/package.json": '{"scripts":{"build":"node prepare-wasm.mjs"}}',
                 "laplogos_core/Cargo.toml": '[package]\nname = "demo"', "scripts/prepare-wasm.mjs": "// build wasm",
                 "laplogos_web/playwright.config.ts": "// test configuration"}
        for name, content in files.items():
            (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / name).write_text(content)
        git(self.repo, "add", "--", *files)
        git(self.repo, "commit", "-m", "context fixture")
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        w = self.worker()
        original_run = w.run
        seen = []

        def run(argv, stdin=None, cwd=None):
            if argv[0] != "codex":
                return original_run(argv, stdin, cwd)
            workspace = Path(cwd)
            seen.append(workspace)
            payload = json.loads(stdin.split("Give concrete reasons for FAIL/ERROR.\n", 1)[1])
            self.assertIn("Cargo.toml", payload["repository_manifest"]["absent"])
            self.assertFalse((workspace / "Cargo.toml").exists())
            for name, content in files.items():
                expected = "Updated build instructions" if name == "README.md" and len(seen) > 1 else content
                self.assertEqual((workspace / name).read_text(), expected)
                self.assertEqual((workspace / name).stat().st_mode & 0o222, 0)
            if payload["verification_phase"] == "TASK":
                self.assertEqual(payload["issue"], {"title": "Document build", "body": "Use package.json and Cargo.toml"})
            self.assertIn("read-only", argv)
            self.assertNotIn("--approve-for-me", argv)
            Path(argv[argv.index("--output-last-message") + 1]).write_text(
                json.dumps({"verdict": "PASS", "reasons": [], "risky": False}))
            return ""

        w.run = run
        w.verify = lambda payload: gateway.Gateway.verify(w, payload)
        original_claude = w.claude

        def claude(payload, schema):
            if schema == gateway.PLAN_SCHEMA:
                self.assertEqual(payload["source_files"], files)
                return original_claude(payload, schema)
            return {"edits": [{"path": "README.md", "content": "Updated build instructions"}]}

        w.claude = claude
        w.process({"number": 9, "title": "Document build", "body": "Use package.json and Cargo.toml"})
        self.assertEqual(len(seen), 2)
        worktree = Path(w.job_data["worktree"])
        self.assertEqual(git(worktree, "diff", "--name-only", w.job_data["base"], "HEAD"), "README.md")
        self.assertEqual(json.loads((w.state / "issue-9.json").read_text())["state"], "pr-ready")
        gateway.check_paths(["README.md"], w.cfg)
        for name in files.keys() - {"README.md"}:
            with self.assertRaises(gateway.ApprovalRequired):
                w.apply({"edits": [{"path": name, "content": "changed"}]})

    def test_read_selection_is_independent_of_write_scope(self):
        (self.repo / "package.json").write_text('{}')
        git(self.repo, "add", "package.json")
        w = self.worker()
        first = w.read_context()
        w.cfg = {**w.cfg, "allowed_paths": ["README.md"]}
        self.assertEqual(w.read_context(), first)
        self.assertEqual(first, {"package.json": "{}"})
        self.assertIn("docs/readme.md", w.repository_manifest["tracked"])
        self.assertNotIn("docs/readme.md", w.repository_manifest["copied"])
        self.assertIn("Cargo.toml", w.repository_manifest["absent"])
        (self.repo / "Cargo.toml").write_text("untracked but present")
        w.read_context()
        self.assertNotIn("Cargo.toml", w.repository_manifest["absent"])

    def test_manifest_missing_copied_file_is_infrastructure_failure(self):
        w = self.worker()
        with patch.object(w, "run") as run:
            with self.assertRaisesRegex(gateway.VerificationFailure, "manifest context"):
                gateway.Gateway.verify(w, {
                    "verification_phase": "PLAN",
                    "source_files": {"README.md": "instructions"},
                    "repository_manifest": {
                        "tracked": ["README.md", "laplogos_core/Cargo.toml"],
                        "copied": ["README.md", "laplogos_core/Cargo.toml"],
                        "absent": ["Cargo.toml"],
                    },
                })
            run.assert_not_called()

    def test_phase_contract_and_invalid_root_command_replanning(self):
        w = self.worker()
        w.job = 9
        sources = {"README.md": "Old instructions", "core/Cargo.toml": '[package]\nname = "core"'}
        context = {"source_files": sources, "repository_manifest": {
            "tracked": sorted(sources), "copied": sorted(sources), "absent": ["Cargo.toml"]}}
        feedback = "Root Cargo.toml is absent; use cargo test --manifest-path core/Cargo.toml."
        plans, workspaces = [], []

        def claude(payload, schema):
            plans.append(payload)
            self.assertIn("prefer 1 task", payload["stage"])
            self.assertIn("prefer 1-3 tasks", payload["stage"])
            command = "cargo test -p core" if len(plans) == 1 else "cargo test --manifest-path core/Cargo.toml"
            return {"tasks": [{"title": "Update README", "criteria": [command], "tests": ["unit"]}]}

        def run(argv, stdin=None, cwd=None):
            prompt, raw = stdin.split("Give concrete reasons for FAIL/ERROR.\n", 1)
            payload = json.loads(raw)
            self.assertEqual(payload["verification_phase"], "PLAN")
            self.assertIn("MUST NOT require", prompt)
            for evidence in ("post-change README", "implementation diff", "test execution results",
                             "modified worktree", "git diff/status", "implementation artifacts"):
                self.assertIn(evidence, prompt)
            self.assertIn("Invalid commands, paths, task ordering, or write scope => FAIL", prompt)
            self.assertIn(".github/workflows", prompt)
            self.assertIn("Do not demand hidden reasoning or historical proof", prompt)
            self.assertNotIn("diff", payload)
            self.assertNotIn("test_results", payload)
            self.assertIn("--ephemeral", argv)
            self.assertIn("read-only", argv)
            self.assertNotIn("--resume", argv)
            workspaces.append(cwd)
            invalid = payload["plan"]["tasks"][0]["criteria"] == ["cargo test -p core"]
            Path(argv[argv.index("--output-last-message") + 1]).write_text(json.dumps({
                "verdict": "FAIL" if invalid else "PASS", "risky": False,
                "reasons": [feedback] if invalid else []}))
            return ""

        w.claude, w.run = claude, run
        w.verify = lambda payload: gateway.Gateway.verify(w, payload)
        result = w.plan_gate(context, "NORMAL")
        self.assertEqual(len(plans), 2)
        self.assertEqual(plans[1]["feedback"], [feedback])
        self.assertEqual(plans[1]["previous_plan"]["tasks"][0]["criteria"], ["cargo test -p core"])
        self.assertIn("--manifest-path", result["tasks"][0]["criteria"][0])
        self.assertEqual(len(set(workspaces)), 2)
        self.assertEqual(w.job_data["plan_gate"], "PASS")

    def test_task_contract_requires_post_change_evidence(self):
        w = self.worker()

        def run(argv, stdin=None, cwd=None):
            self.assertIn("TASK verification", stdin)
            self.assertIn("post-change sources, diff, and task-specific validation evidence", stdin)
            Path(argv[argv.index("--output-last-message") + 1]).write_text(json.dumps({
                "verdict": "ERROR", "risky": False, "reasons": ["Missing task diff"]}))
            return ""

        w.run = run
        with self.assertRaisesRegex(gateway.VerificationFailure, "Missing task diff"):
            gateway.Gateway.verify(w, {"verification_phase": "TASK", "task": {}})

    def test_task_outcome_contract_and_verdicts_without_action_history(self):
        # Stub only the external model: assert its actual prompt/context contract
        # and exercise real verdict handling. This is not a live model evaluation.
        cases = (
            ("correct", "build", True, False, None, "PASS", ""),
            ("wrong command", "deploy", True, False, None, "FAIL", "Unknown npm script: deploy"),
            ("missing context", "build", False, False, None, "ERROR", "Missing laplogos_web/package.json"),
            ("audit present", "build", True, True, "Inspected package.json before editing", "PASS", ""),
            ("audit absent", "build", True, True, None, "FAIL", "Required audit.md artifact is absent"),
        )
        for label, command, has_package, audit_required, audit, verdict, reason in cases:
            with self.subTest(case=label):
                w = self.worker()
                sources = {"README.md": f"cd laplogos_web && npm run {command}"}
                if has_package:
                    sources["laplogos_web/package.json"] = '{"scripts":{"build":"vite build"}}'
                if audit is not None:
                    sources["audit.md"] = audit
                issue = "Inspect package.json before editing; check repository structure and existing configuration. Document the build command."
                if audit_required:
                    issue += " Deliver audit.md recording pre-edit inspection."
                payload = {
                    "verification_phase": "TASK",
                    "issue": {"title": "Document build", "body": issue},
                    "task": {"criteria": [issue], "tests": ["unit"]},
                    "source_files": sources,
                    "repository_manifest": {
                        "tracked": sorted(set(sources) | {"laplogos_web/package.json"}),
                        "copied": sorted(sources),
                        "absent": ["Cargo.toml"] + (["audit.md"] if audit is None else []),
                    },
                    "diff": f"+cd laplogos_web && npm run {command}",
                    "test_results": [{"name": "unit", "argv": ["true"], "status": "PASS"}],
                }

                def run(argv, stdin=None, cwd=None):
                    prompt, raw = stdin.split("Give concrete reasons for FAIL/ERROR.\n", 1)
                    self.assertIn("Verify observable outcomes", prompt)
                    self.assertIn("implementation instructions, not audit requirements", prompt)
                    self.assertIn("MUST NOT require chronological agent-action evidence", prompt)
                    self.assertIn("read a file before editing", prompt)
                    self.assertIn("executed exploratory commands", prompt)
                    self.assertIn("hidden reasoning", prompt)
                    self.assertIn("explicitly requires an audit/log artifact", prompt)
                    self.assertIn("confirmed absent required artifact => FAIL", prompt)
                    self.assertIn("correct observable result => PASS", prompt)
                    self.assertIn("observable mismatch => FAIL", prompt)
                    self.assertIn("genuinely missing required trusted context => ERROR", prompt)
                    self.assertEqual(json.loads(raw), payload)
                    for name, content in sources.items():
                        self.assertEqual((Path(cwd) / name).read_text(), content)
                    # Deterministic model substitute compares the supplied artifacts;
                    # it receives no transcript or chronological action records.
                    package = Path(cwd) / "laplogos_web/package.json"
                    if not package.exists():
                        observed = "ERROR"
                    elif (Path(cwd) / "README.md").read_text().split()[-1] not in json.loads(package.read_text())["scripts"]:
                        observed = "FAIL"
                    elif "Deliver audit.md" in json.loads(raw)["issue"]["body"] and not (Path(cwd) / "audit.md").exists():
                        observed = "FAIL"
                    else:
                        observed = "PASS"
                    Path(argv[argv.index("--output-last-message") + 1]).write_text(json.dumps({
                        "verdict": observed, "reasons": [reason] if reason else [], "risky": False}))
                    return ""

                w.run = run
                if verdict == "ERROR":
                    with self.assertRaises(gateway.VerificationFailure) as caught:
                        gateway.Gateway.verify(w, payload)
                    self.assertEqual(str(caught.exception), "Verifier context/infrastructure failure: " + reason)
                else:
                    self.assertEqual(gateway.Gateway.verify(w, payload)["verdict"], verdict)

    def test_verifier_error_prefix_once_in_both_phases(self):
        for phase in ("PLAN", "TASK"):
            with self.subTest(phase=phase):
                w = self.worker()
                reason = "Required trusted context unavailable; original diagnostic."

                def run(argv, stdin=None, cwd=None):
                    Path(argv[argv.index("--output-last-message") + 1]).write_text(json.dumps({
                        "verdict": "ERROR", "reasons": [reason], "risky": False}))
                    return ""

                w.run = run
                with self.assertRaises(gateway.VerificationFailure) as caught:
                    gateway.Gateway.verify(w, {"verification_phase": phase})
                self.assertEqual(str(caught.exception), "Verifier context/infrastructure failure: " + reason)

    def test_verification_phase_is_required_and_closed(self):
        w = self.worker()
        for phase in (None, "FINAL", "unknown", []):
            with self.subTest(phase=phase), patch.object(w, "run") as run:
                payload = {} if phase is None else {"verification_phase": phase}
                with self.assertRaisesRegex(gateway.VerificationFailure, "verification_phase"):
                    gateway.Gateway.verify(w, payload)
                run.assert_not_called()

    def test_debate_critique_uses_preimplementation_contract(self):
        self.cfg["mode"] = "DEBATE"
        w = self.worker()
        original = w.verify
        payloads = []

        def verify(payload):
            payloads.append(payload)
            return original(payload)

        w.verify = verify
        w.once()
        self.assertEqual([p["verification_phase"] for p in payloads], ["PLAN", "PLAN", "TASK"])
        self.assertEqual(payloads[0]["stage"], "requirements critique")
        self.assertEqual(payloads[1]["required_checks"], self.cfg["required_checks"])
        self.assertEqual(payloads[1]["validation_commands"], self.cfg["validation"])

    def test_plan_repair_feedback_bound_and_fresh_verifiers(self):
        for limit, failures in ((0, 1), (2, 2), (2, 3)):
            with self.subTest(limit=limit, failures=failures):
                w = self.worker()
                w.job = 9
                w.cfg = {**self.cfg, "plan_repair_attempts": limit}
                payloads, workspaces = [], []
                original_claude = w.claude

                def claude(payload, schema):
                    payloads.append(payload)
                    return original_claude(payload, schema)

                def run(argv, stdin=None, cwd=None):
                    workspaces.append(cwd)
                    verdict = "FAIL" if len(workspaces) <= failures else "PASS"
                    Path(argv[argv.index("--output-last-message") + 1]).write_text(json.dumps(
                        {"verdict": verdict, "risky": False,
                         "reasons": ["Include Cargo build prerequisite"] if verdict == "FAIL" else []}))
                    return ""

                w.claude = claude
                w.run = run
                w.verify = lambda payload: gateway.Gateway.verify(w, payload)
                if failures > limit:
                    with self.assertRaisesRegex(gateway.ApprovalRequired, "repair limit"):
                        w.plan_gate({"source_files": {}}, "NORMAL")
                else:
                    self.assertIn("tasks", w.plan_gate({"source_files": {}}, "NORMAL"))
                self.assertEqual(len(payloads), min(failures + 1, limit + 1))
                self.assertEqual(len(set(workspaces)), len(workspaces))
                if len(payloads) > 1:
                    self.assertEqual(payloads[1]["feedback"], ["Include Cargo build prerequisite"])
                    self.assertIn("tasks", payloads[1]["previous_plan"])

    def test_verifier_failures_do_not_repair_plan(self):
        for failure in ("context", "missing-output", "malformed", "timeout", "workspace"):
            with self.subTest(failure=failure):
                w = self.worker()
                w.job = 9

                def run(argv, stdin=None, cwd=None):
                    if failure == "timeout":
                        raise gateway.ApprovalRequired("Timeout: codex")
                    if failure == "workspace":
                        raise FileNotFoundError("Missing verifier workspace")
                    output = Path(argv[argv.index("--output-last-message") + 1])
                    if failure == "context":
                        output.write_text(json.dumps({"verdict": "ERROR", "risky": False,
                            "reasons": ["package.json/Cargo.toml unavailable; cannot verify"]}))
                    elif failure == "malformed":
                        output.write_text("invalid JSON")
                    return ""

                w.run = run
                w.verify = lambda payload: gateway.Gateway.verify(w, payload)
                with patch.object(w, "claude", wraps=w.claude) as planner:
                    with self.assertRaisesRegex(gateway.VerificationFailure, "context/infrastructure") as exc:
                        w.plan_gate({"source_files": {"README.md": "readme"}}, "NORMAL")
                    self.assertNotIn("Plan disagreement", str(exc.exception))
                    self.assertEqual(planner.call_count, 1)
                self.assertEqual(w.job_data["plan_gate"], "ERROR")

    def test_context_denies_secrets_symlinks_and_incomplete_sources(self):
        w = self.worker()
        for name in (".env", ".env.local", ".envrc", ".git/config", ".ssh/id_rsa",
                     ".aws/credentials", "credentials.json", "secrets/data.json", "server.key",
                     "../README.md", "/tmp/README.md", ".npmrc",
                     "node_modules/demo/package.json", "target/Cargo.toml",
                     "build/package.json", "dist/package.json", "cache/package.json",
                     "generated/package.json", "vendor/package.json", "scripts/demo.wasm"):
            with self.subTest(name=name):
                with patch.object(w, "run", return_value=name + "\x00"):
                    w.cfg = {**self.cfg, "read_context_paths": ["*"]}
                    self.assertEqual(w.read_context(), {})
                with self.assertRaises(gateway.VerificationFailure):
                    gateway.Gateway.verify(w, {"verification_phase": "PLAN", "source_files": {name: "never copied"}})
        w.cfg = {**self.cfg, "read_context_paths": ["package.json"]}
        with patch.object(w, "run", return_value="package.json\x00"):
            with self.assertRaisesRegex(gateway.VerificationFailure, "Missing/unreadable"):
                w.read_context()
            (self.repo / "package.json").symlink_to(self.repo / "docs/readme.md")
            with self.assertRaisesRegex(gateway.VerificationFailure, "Symlink"):
                w.read_context()

    def test_verifier_diagnostic_lifecycle(self):
        cases = [
            (phase, outcome)
            for phase in ("PLAN", "TASK", "CRITIQUE")
            for outcome in ("PASS-risk", "FAIL-risk", "ERROR-risk", "context", "timeout")
        ] + [(phase, "retry") for phase in ("PLAN", "TASK")]
        for number, (phase, outcome) in enumerate(cases, 200):
            with self.subTest(phase=phase, outcome=outcome):
                w = self.worker()
                w.cfg = {**self.cfg, "mode": "DEBATE" if phase == "CRITIQUE" else "NORMAL"}
                reasons = ["Permission boundary requires review", "Protected operation needs approval"]
                calls, proposals = [], []
                original_run, original_claude = w.run, w.claude

                def claude(payload, schema):
                    proposals.append((schema, payload))
                    return original_claude(payload, schema)

                def run(argv, stdin=None, cwd=None):
                    if argv[0] != "codex":
                        return original_run(argv, stdin, cwd)
                    payload = json.loads(stdin.rsplit("\n", 1)[1])
                    selected = (payload["verification_phase"] == phase or
                                phase == "CRITIQUE" and payload.get("stage") == "requirements critique")
                    verdict = {"verdict": "PASS", "risky": False, "reasons": []}
                    if selected:
                        calls.append(payload)
                        if outcome == "timeout":
                            raise gateway.ApprovalRequired("Timeout: verifier fixture")
                        if outcome == "context":
                            verdict = {"verdict": "ERROR", "risky": False,
                                       "reasons": ["Missing required context"]}
                        elif outcome.endswith("-risk"):
                            verdict = {"verdict": outcome.split("-")[0], "risky": True, "reasons": reasons}
                        elif len(calls) == 1:
                            verdict = {"verdict": "FAIL", "risky": False, "reasons": ["Correct the criterion"]}
                    Path(argv[argv.index("--output-last-message") + 1]).write_text(json.dumps(verdict))
                    return ""

                w.run = run
                w.claude = claude
                w.verify = lambda payload: gateway.Gateway.verify(w, payload)
                issue = {"number": number, "title": "Fixture", "body": "Deterministic verification"}
                if outcome == "retry":
                    w.process(issue)
                else:
                    with self.assertRaises(gateway.ApprovalRequired) as caught:
                        w.process(issue)
                    self.assertEqual(isinstance(caught.exception, gateway.VerificationFailure),
                                     outcome in ("context", "timeout"))
                    if outcome.endswith("-risk"):
                        self.assertEqual(caught.exception.reasons, reasons)
                state = json.loads((w.state / f"issue-{number}.json").read_text())
                prefix = "task" if phase == "TASK" else "plan"
                if outcome == "retry":
                    self.assertEqual(state["state"], "pr-ready")
                    self.assertEqual(len(calls), 2)
                    matching = [p for schema, p in proposals if schema ==
                                (gateway.EDIT_SCHEMA if phase == "TASK" else gateway.PLAN_SCHEMA)]
                    self.assertEqual(matching[1]["feedback"], ["Correct the criterion"])
                    self.assertEqual(state[prefix + "_gate"], "PASS")
                    self.assertEqual(state[prefix + "_attempts"], 2)
                else:
                    self.assertEqual(state["state"], "approval-required")
                    self.assertEqual(len(calls), 1, "Risk/errors must never trigger repair")
                    self.assertEqual(len(proposals), {"PLAN": 1, "TASK": 2, "CRITIQUE": 0}[phase])
                    self.assertEqual(state[prefix + "_attempts"], 0 if phase == "CRITIQUE" else 1)
                    self.assertEqual(state[prefix + "_gate"], "RISK" if outcome.endswith("-risk") else "ERROR")
                    if outcome.endswith("-risk"):
                        self.assertEqual(state[prefix + "_feedback"], reasons)
                        for reason in reasons:
                            self.assertIn(reason, state["reason"])
                    else:
                        self.assertIn("context/infrastructure", state["reason"])
                        self.assertEqual(state[prefix + "_feedback"], [state["reason"]])
                    self.assertFalse(state.get("completed_tasks"))

    def test_task_read_context_failure_persists_diagnostics_without_repair(self):
        w = self.worker()
        original_read_context = w.read_context
        reason = "Missing/unreadable task context"

        def read_context():
            if reader.call_count == 1:
                return original_read_context()
            raise gateway.VerificationFailure(reason)

        with patch.object(w, "read_context", side_effect=read_context) as reader, \
             patch.object(w, "claude", wraps=w.claude) as proposer, \
             patch.object(w, "verify", wraps=w.verify) as verifier:
            with self.assertRaisesRegex(gateway.VerificationFailure, reason):
                w.process({"number": 300, "title": "Fixture", "body": "Task context failure"})
            self.assertEqual(reader.call_count, 2)
            self.assertEqual([call.args[1] for call in proposer.call_args_list],
                             [gateway.PLAN_SCHEMA, gateway.EDIT_SCHEMA])
            self.assertEqual(verifier.call_count, 1)
        state = json.loads((w.state / "issue-300.json").read_text())
        self.assertEqual(state["state"], "approval-required")
        self.assertEqual(state["task_gate"], "ERROR")
        self.assertEqual(state["task_attempts"], 1)
        self.assertEqual(state["task_feedback"], [reason])
        self.assertEqual(state["reason"], reason)
        self.assertEqual(state["completed_tasks"], [])

    def test_plan_repair_config_requires_bounded_integer(self):
        for value in (-1, 6, True, 1.5, "2", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                gateway.validate_config({**self.cfg, "plan_repair_attempts": value})
        self.assertEqual(gateway.validate_config(self.cfg).get("plan_repair_attempts", 2), 2)

    def test_second_worker_lock_rejected(self):
        import fcntl

        w = self.worker()
        with (w.state / "worker.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                w.once()


if __name__ == "__main__":
    unittest.main()
