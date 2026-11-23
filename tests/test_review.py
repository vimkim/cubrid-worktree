"""Tests for PR review checkout naming, metadata safety guards, interactive/all modes."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from cubrid_worktree.cli import main


class ReviewWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.parent = self.root / "cb"
        self.control = self.parent / "develop"
        self.control.mkdir(parents=True)
        self.justfile = self.root / "personal.just"
        self.justfile.write_text("prepare-build:\n\t@true\n")

        self.pr_titles = {7887: "[cbrd-27325] Rediscover free heap pages"}
        self.registered_paths = [self.control]
        self.branches: dict[str, dict[str, str]] = {}
        self.candidates = [
            {
                "number": 7887,
                "title": self.pr_titles[7887],
                "url": "https://github.com/CUBRID/cubrid/pull/7887",
                "author_login": "reviewer1",
                "created_at": "2026-11-20T01:02:03Z",
                "is_draft": False,
                "approved_count": 0,
                "reviewer_pool_count": 3,
                "requested": True,
                "review_state": "not reviewed",
            }
        ]
        self.calls: list[tuple[list[str], Path]] = []
        self.fzf_returncode = 0
        self.fzf_selection = "https://github.com/CUBRID/cubrid/pull/7887"
        self.fzf_input = ""
        self.prepare_returncode = 0
        self.checkout_returncodes: dict[int, int] = {}

    def _env(self) -> dict[str, str]:
        return {
            "HOME": os.fspath(self.root),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "CUBRID_WORKTREE_CONTROL_DIR": os.fspath(self.control),
            "CUBRID_WORKTREE_PARENT": os.fspath(self.parent),
            "CUBRID_WORKTREE_PREPARE_JUSTFILE": os.fspath(self.justfile),
            "HERDR_ENV": "0",
        }

    def _subprocess_run(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        cwd = Path(os.fspath(kwargs.get("cwd") or self.control))
        self.calls.append((list(command), cwd))

        if command[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(command, 0, f"{self.control}\n", "")
        if command[:4] == ["git", "worktree", "list", "--porcelain"]:
            chunks = []
            for wt_path in self.registered_paths:
                branch = next(
                    (b for b, meta in self.branches.items() if meta.get("path") == os.fspath(wt_path)),
                    "develop" if wt_path == self.control else wt_path.name,
                )
                chunks.append(f"worktree {wt_path}\0HEAD deadbeef\0branch refs/heads/{branch}\0\0")
            return subprocess.CompletedProcess(command, 0, "".join(chunks), "")
        if command[:4] == ["git", "config", "--local", "--get-regexp"]:
            lines = []
            for branch, meta in self.branches.items():
                for key in ("pr-url", "cubrid-role"):
                    if key in meta:
                        lines.append(f"branch.{branch}.{key} {meta[key]}")
            return subprocess.CompletedProcess(command, 0 if lines else 1, "\n".join(lines), "")
        if command[:4] == ["git", "show-ref", "--verify", "--quiet"]:
            ref = command[4]
            branch = ref.removeprefix("refs/heads/")
            exists = branch in self.branches or any(p.name == branch for p in self.registered_paths if p != self.control)
            return subprocess.CompletedProcess(command, 0 if exists else 1, "", "")
        if command[:4] == ["git", "config", "--local", "--replace-all"]:
            key = command[4]
            val = command[5]
            rem = key.removeprefix("branch.")
            branch_name, attr = rem.rsplit(".", 1)
            self.branches.setdefault(branch_name, {})[attr] = val
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["gh", "pr", "view"]:
            number = int(command[3].rsplit("/", 1)[-1])
            metadata = {
                "number": number,
                "title": self.pr_titles[number],
                "url": f"https://github.com/CUBRID/cubrid/pull/{number}",
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(metadata), "")
        if command[:3] == ["gh", "pr", "checkout"]:
            number = int(command[3])
            rc = self.checkout_returncodes.get(number, 0)
            if rc == 0:
                target = Path(command[command.index("--worktree") + 1])
                branch = command[command.index("--branch") + 1]
                target.mkdir(parents=True, exist_ok=True)
                self.registered_paths.append(target)
                self.branches.setdefault(branch, {})["path"] = os.fspath(target)
            return subprocess.CompletedProcess(command, rc, "", "checkout failed" if rc else "")
        if command[0] == "cubrid-dev2-pr":
            return subprocess.CompletedProcess(command, 0, json.dumps(self.candidates), "")
        if command[0] == "fzf":
            self.fzf_input = str(kwargs.get("input", ""))
            return subprocess.CompletedProcess(command, self.fzf_returncode, self.fzf_selection, "")
        if command[0] == "just":
            return subprocess.CompletedProcess(
                command,
                self.prepare_returncode,
                "",
                "prepare failed" if self.prepare_returncode else "",
            )
        raise AssertionError(f"Unexpected command: {command}")

    def _run_cli(self, *argv: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch("subprocess.run", side_effect=self._subprocess_run),
            mock.patch("shutil.which", side_effect=lambda name: f"/usr/bin/{name}" if name == "cubrid-dev2-pr" else None),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = main(list(argv), env=self._env())
        return code, stdout.getvalue(), stderr.getvalue()

    def test_first_review_uses_ticket_name_and_sets_review_role(self) -> None:
        code, stdout, stderr = self._run_cli("review", "7887", "--json")
        self.assertEqual(code, 0, stderr)
        data = json.loads(stdout)["data"]
        self.assertEqual(data["name"], "CBRD-27325")
        self.assertEqual(data["branch"], "CBRD-27325")
        self.assertEqual(data["role"], "review")
        self.assertEqual(self.branches["CBRD-27325"]["cubrid-role"], "review")
        self.assertEqual(
            self.branches["CBRD-27325"]["pr-url"],
            "https://github.com/CUBRID/cubrid/pull/7887",
        )

    def test_occupied_ticket_falls_back_to_ticket_pr_number(self) -> None:
        occupied_path = self.parent / "CBRD-27325"
        occupied_path.mkdir()
        self.registered_paths.append(occupied_path)
        self.branches["CBRD-27325"] = {"path": os.fspath(occupied_path), "cubrid-role": "implement"}

        code, stdout, stderr = self._run_cli(
            "review", "https://github.com/CUBRID/cubrid/pull/7887", "--json"
        )
        self.assertEqual(code, 0, stderr)
        data = json.loads(stdout)["data"]
        self.assertEqual(data["name"], "CBRD-27325-pr-7887")
        self.assertEqual(data["branch"], "CBRD-27325-pr-7887")
        self.assertEqual(data["role"], "review")

    def test_unticketed_pr_uses_pr_number(self) -> None:
        self.pr_titles[9001] = "Fix typo in docs"
        code, stdout, stderr = self._run_cli("review", "9001", "--no-prepare", "--json")
        self.assertEqual(code, 0, stderr)
        data = json.loads(stdout)["data"]
        self.assertEqual(data["name"], "PR-9001")
        self.assertIsNone(data["ticket"])

    def test_interactive_detects_legacy_existing_review_and_reuses_when_requested(self) -> None:
        legacy = self.parent / "review-CBRD-27325-pr-7887"
        legacy.mkdir()
        self.registered_paths.append(legacy)
        self.branches["review-CBRD-27325-pr-7887"] = {
            "path": os.fspath(legacy),
            "pr-url": "https://github.com/CUBRID/cubrid/pull/7887",
            "cubrid-role": "review",
        }
        code, _, stderr = self._run_cli("review", "--interactive")
        self.assertEqual(code, 1)
        self.assertIn("[existing] #7887", self.fzf_input)
        self.assertIn("ALREADY_EXISTS", stderr)

        code_reuse, stdout_reuse, err_reuse = self._run_cli("review", "7887", "--reuse", "--no-prepare", "--json")
        self.assertEqual(code_reuse, 0, err_reuse)
        data_reuse = json.loads(stdout_reuse)["data"]
        self.assertTrue(data_reuse["reused"])
        self.assertEqual(data_reuse["name"], "review-CBRD-27325-pr-7887")

    def test_review_all_continues_on_failure_and_skips_existing(self) -> None:
        self.pr_titles[7888] = "[CBRD-28888] Another fix"
        self.candidates.append(
            {
                "number": 7888,
                "title": self.pr_titles[7888],
                "url": "https://github.com/CUBRID/cubrid/pull/7888",
                "author_login": "reviewer2",
                "created_at": "2026-11-21T01:02:03Z",
                "is_draft": False,
                "approved_count": 1,
                "reviewer_pool_count": 3,
                "requested": True,
                "review_state": "commented",
            }
        )
        self.checkout_returncodes[7887] = 5
        code, stdout, stderr = self._run_cli("review", "--all", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        err_payload = json.loads(stderr)
        self.assertFalse(err_payload["ok"])
        self.assertEqual(err_payload["error"]["code"], "PARTIAL_FAILURE")
        details = err_payload["error"]["details"]
        self.assertEqual([item["pr_number"] for item in details["created"]], [7888])
        self.assertEqual([item["pr_number"] for item in details["failed"]], [7887])
        # Verify 7888 was still processed even though 7887 failed.
        self.assertIn("CBRD-28888", self.branches)
        self.assertEqual(self.branches["CBRD-28888"]["cubrid-role"], "review")

    def test_prepare_failure_preserves_worktree_and_returns_retry_fix(self) -> None:
        self.prepare_returncode = 9
        code, stdout, stderr = self._run_cli("review", "7887", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        err = json.loads(stderr)["error"]
        self.assertEqual(err["code"], "PREPARE_FAILED")
        self.assertIn("Retry preparation with:", err["fix"])
        self.assertTrue((self.parent / "CBRD-27325").is_dir())


if __name__ == "__main__":
    unittest.main()
