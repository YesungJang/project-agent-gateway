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
        "required_checks": ["unit"],
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
        if "plan" in payload and self.fail_plan:
            return {"verdict": "FAIL", "reasons": ["Missing criterion"]}
        if "diff" in payload:
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

    def test_plan_disagreement_stops_before_edits(self):
        w = self.worker(fail_plan=True)
        with self.assertRaises(gateway.ApprovalRequired):
            w.once()
        state = json.loads((w.state / "issue-1.json").read_text())
        self.assertEqual(state["state"], "approval-required")
        self.assertFalse((Path(state["worktree"]) / "src/greeting.py").exists())

    def test_full_validation_failure_stops_before_publish(self):
        self.cfg["validation"]["full"] = ["python3", "-c", "raise SystemExit(1)"]
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
            'from pathlib import Path; Path("src/greeting.py").write_text("changed")',
        ]
        w = self.worker()
        with self.assertRaisesRegex(gateway.ApprovalRequired, "modified"):
            w.once()

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
            "statusCheckRollup": [{"name": "unit", "conclusion": "SUCCESS"}],
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
            ("statusCheckRollup", [{"name": "unit", "conclusion": "SKIPPED"}]),
            ("statusCheckRollup", [{"name": "unit", "conclusion": None}]),
            ("files", [{"path": "src/auth.py"}]),
            ("files", [{"path": "outside/file.py"}]),
            ("files", [{"path": "src/payments.py"}]),
            *[
                ("statusCheckRollup", [{"name": "unit", "conclusion": status}])
                for status in (
                    "FAILURE",
                    "CANCELLED",
                    "TIMED_OUT",
                    "UNKNOWN",
                    "NEUTRAL",
                )
            ],
            ("statusCheckRollup", [{"name": "other", "conclusion": "SUCCESS"}]),
            (
                "statusCheckRollup",
                [
                    {"name": "unit", "conclusion": "SUCCESS"},
                    {"name": "unit", "conclusion": "FAILURE"},
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
            "statusCheckRollup": [{"name": "unit", "conclusion": "SUCCESS"}],
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
        self.assertEqual(gateway.Gateway.verify(w, {"task": "safe"})["verdict"], "PASS")
        self.assertIn("read-only", captured[0])
        self.assertNotIn("--approve-for-me", captured[0])
        self.assertNotIn("--resume", captured[0])
        verdict["risky"] = True
        with self.assertRaisesRegex(gateway.ApprovalRequired, "risk"):
            gateway.Gateway.verify(w, {"task": "risky"})

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
                     "../README.md", "/tmp/README.md", ".npmrc"):
            with self.subTest(name=name):
                with patch.object(w, "run", return_value=name + "\x00"):
                    w.cfg = {**self.cfg, "read_context_paths": ["*"]}
                    self.assertEqual(w.read_context(), {})
                with self.assertRaises(gateway.VerificationFailure):
                    gateway.Gateway.verify(w, {"source_files": {name: "never copied"}})
        w.cfg = {**self.cfg, "read_context_paths": ["package.json"]}
        with patch.object(w, "run", return_value="package.json\x00"):
            with self.assertRaisesRegex(gateway.VerificationFailure, "Missing/unreadable"):
                w.read_context()
            (self.repo / "package.json").symlink_to(self.repo / "docs/readme.md")
            with self.assertRaisesRegex(gateway.VerificationFailure, "Symlink"):
                w.read_context()

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
