"""Integration tests for worktree create, list, open, and reuse using temporary Git repos."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from cubrid_worktree.cli import main


GIT = shutil.which("git")


@unittest.skipUnless(GIT, "git is required")
class CreateAndListTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.parent = self.root / "cb"
        self.parent.mkdir()
        self.remote = self.root / "origin.git"
        self.seed = self.root / "seed"
        self.control = self.parent / "develop"
        self.env = {
            "HOME": os.fspath(self.root / "home"),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "CUBRID_WORKTREE_CONTROL_DIR": os.fspath(self.control),
            "CUBRID_WORKTREE_PARENT": os.fspath(self.parent),
            "HERDR_ENV": "0",
        }
        (self.root / "home").mkdir()

        self._git(self.root, "init", "--bare", "--initial-branch=develop", self.remote)
        self._git(self.root, "clone", self.remote, self.seed)
        (self.seed / "README.md").write_text("base\n")
        self._git(self.seed, "add", ".")
        self._git(self.seed, "commit", "-m", "initial")
        self._git(self.seed, "push", "-u", "origin", "develop")
        self._git(self.seed, "checkout", "-b", "release/11.4")
        (self.seed / "VERSION").write_text("11.4\n")
        self._git(self.seed, "add", ".")
        self._git(self.seed, "commit", "-m", "11.4")
        self._git(self.seed, "push", "-u", "origin", "release/11.4")
        self._git(self.seed, "checkout", "develop")

        self._git(self.root, "clone", self.remote, self.control)

    def _git(self, cwd: Path, *args: object) -> str:
        return subprocess.run(
            [GIT, "-C", os.fspath(cwd), *map(str, args)],
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def _run_cli(self, *argv: str, extra_env: dict[str, str] | None = None) -> tuple[int, str, str]:
        merged_env = dict(self.env)
        if extra_env:
            merged_env.update(extra_env)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(list(argv), env=merged_env)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_create_primary_from_arbitrary_cwd_fetches_prepares_and_sets_role(self) -> None:
        (self.seed / "README.md").write_text("advanced upstream\n")
        self._git(self.seed, "add", ".")
        self._git(self.seed, "commit", "-m", "advance develop")
        self._git(self.seed, "push", "origin", "develop")

        marker_cmd = f"{os.fspath(Path(shutil.which('python3')))} -c 'from pathlib import Path; Path(\".prepared\").write_text(\"ok\")'"
        code, stdout, stderr = self._run_cli(
            "create",
            "cbrd-28099",
            "--json",
            extra_env={"CUBRID_WORKTREE_PREPARE_CMD": marker_cmd},
        )
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertTrue(payload["ok"])
        data = payload["data"]
        wt = self.parent / "CBRD-28099"
        self.assertEqual(data["name"], "CBRD-28099")
        self.assertEqual(data["branch"], "CBRD-28099")
        self.assertEqual(data["role"], "implement")
        self.assertEqual(data["prepare"]["status"], "prepared")
        self.assertTrue((wt / ".prepared").is_file())
        self.assertEqual((wt / "README.md").read_text(), "advanced upstream\n")
        self.assertEqual(
            self._git(self.control, "config", "--local", "--get", "branch.CBRD-28099.cubrid-role"),
            "implement",
        )

    def test_non_pr_summary_allowed_without_primary_and_allows_primary_later(self) -> None:
        code1, out1, err1 = self._run_cli("create", "CBRD-28099-bench", "--no-prepare", "--json")
        self.assertEqual(code1, 0, err1)
        data1 = json.loads(out1)["data"]
        self.assertEqual(data1["name"], "CBRD-28099-bench")
        self.assertEqual(data1["siblings"], [])
        self.assertTrue((self.parent / "CBRD-28099-bench").is_dir())

        # Similar ticket prefix CBRD-2809 must NOT appear as a sibling of CBRD-28099.
        code_other, _, err_other = self._run_cli("create", "CBRD-2809", "--no-prepare", "--json")
        self.assertEqual(code_other, 0, err_other)

        code2, out2, err2 = self._run_cli("create", "CBRD-28099", "--no-prepare", "--json")
        self.assertEqual(code2, 0, err2)
        data2 = json.loads(out2)["data"]
        sibling_names = [s["name"] for s in data2["siblings"]]
        self.assertEqual(sibling_names, ["CBRD-28099-bench"])

        code3, out3, err3 = self._run_cli(
            "create", "CBRD-28099", "--summary", "11.4", "--base", "origin/release/11.4", "--no-prepare", "--json"
        )
        self.assertEqual(code3, 0, err3)
        data3 = json.loads(out3)["data"]
        self.assertEqual(data3["name"], "CBRD-28099-11.4")
        self.assertEqual((self.parent / "CBRD-28099-11.4" / "VERSION").read_text(), "11.4\n")

    def test_exact_conflict_fails_without_reuse_and_succeeds_with_reuse(self) -> None:
        self._run_cli("create", "CBRD-28099", "--no-prepare")
        code, stdout, stderr = self._run_cli("create", "CBRD-28099", "--no-prepare", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        err_json = json.loads(stderr)
        self.assertFalse(err_json["ok"])
        self.assertEqual(err_json["error"]["code"], "ALREADY_EXISTS")

        code_reuse, out_reuse, err_reuse = self._run_cli("create", "CBRD-28099", "--reuse", "--no-prepare", "--json")
        self.assertEqual(code_reuse, 0, err_reuse)
        data_reuse = json.loads(out_reuse)["data"]
        self.assertTrue(data_reuse["reused"])
        self.assertFalse(data_reuse["created"])

    def test_dry_run_performs_no_mutations(self) -> None:
        code, stdout, stderr = self._run_cli("create", "CBRD-99999-exp", "--dry-run", "--json")
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["data"]["dry_run"])
        self.assertFalse((self.parent / "CBRD-99999-exp").exists())

    def test_list_filters_tickets_with_word_boundary(self) -> None:
        self._run_cli("create", "CBRD-123", "--no-prepare")
        self._run_cli("create", "CBRD-123-bench", "--no-prepare")
        self._run_cli("create", "CBRD-1234", "--no-prepare")

        code, stdout, stderr = self._run_cli("list", "cbrd-123", "--json")
        self.assertEqual(code, 0, stderr)
        names = [row["name"] for row in json.loads(stdout)["data"]["worktrees"]]
        self.assertEqual(sorted(names), ["CBRD-123", "CBRD-123-bench"])

        code_plain, plain_out, _ = self._run_cli("list", "CBRD-123", "--plain")
        self.assertEqual(code_plain, 0)
        self.assertIn("CBRD-123\tCBRD-123\timplement\t", plain_out)


if __name__ == "__main__":
    unittest.main()
