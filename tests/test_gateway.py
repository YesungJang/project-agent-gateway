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
            ("isDraft", True),
            ("mergeStateStatus", "BLOCKED"),
            ("statusCheckRollup", []),
            ("statusCheckRollup", [{"name": "unit", "conclusion": "SKIPPED"}]),
            ("statusCheckRollup", [{"name": "unit", "conclusion": None}]),
            ("files", [{"path": "src/auth.py"}]),
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
        with self.assertRaises(gateway.ApprovalRequired):
            w.merge_gate("pr", "head", "base")

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

    def test_second_worker_lock_rejected(self):
        import fcntl

        w = self.worker()
        with (w.state / "worker.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                w.once()


if __name__ == "__main__":
    unittest.main()
